"""httppipe - a byte pipe from a loopback port to a plain local backend, and a reader of the requests
it pipes. Both fronts are built on it: `ui-probe tls-front` (TLS for the iOS Simulator) and
`ui-probe http-front` (plain, for an Android emulator).

The pipe forwards every byte as it arrives, in both directions, and never holds one back, reorders
or changes it. With a log, the bytes from the client are also read as HTTP/1.x, as a side
observation made after they were handed on: whatever the reader concludes, the bytes are piped the
same way. The log is JSON, one line per event, each line one `write` appended to the file:

  {"t", "event": "listening", "port", "to_port", "pid"}       once, when the front listens
  {"t", "event": "request", "conn", "method", "path", "query"}  when a request's head is complete:
        the shape of the path without its query string (at most 300 characters; see path_shape), and
        whether a query was there
  {"t", "event": "unparsed", "conn", "why"}    once for a connection whose bytes are not HTTP/1.x;
        its bytes go on being piped and it is not read again
  {"t", "event": "upgrade", "conn"}            after a request with `Connection: upgrade` (or a
        CONNECT): what follows on that connection is not HTTP and is not read
  {"t", "event": "closed", "conn", "duration_s", "bytes_up", "bytes_down", "requests"}
  {"t", "event": "backend-refused", "to", "error"}
  {"t", "event": "stopping", "signal"}      when the front is told to stop (SIGTERM, SIGINT, or SIGHUP unless
        it was ignored when the front started, as under `nohup`): it
        stops accepting, closes every connection it holds (each gets its `closed` line) and exits
        within STOP_WAIT_S, whatever its clients do

One log, one front: a front refuses to start on a log whose last `listening` line names another
front process that is running with `--log` on this same file.

No header value, no query string and no body is ever written to the log, and of a path only its
shape. The log is made 0600 at every write, whether it existed or not. `t` is the host's clock
(time.time(), three decimals).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import time

HEAD_MAX = 64 * 1024       # a request head, from its first byte to the empty line that ends it
LINE_MAX = 64 * 1024       # a chunk-size line; the trailer section after the last chunk
PATH_MAX = 300             # characters of a path written to the log
METHOD_MAX = 32            # a method name longer than this is not HTTP/1.x
TOKEN = frozenset(b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
VERSION_RE = re.compile(r"HTTP/1\.[0-9]")
HEX_RE = re.compile(r"[0-9A-Fa-f]{1,16}")
# A path segment written as it is: lower-case letters, dots, hyphens and underscores, starting with a
# letter, or a version (v1). Any other segment (an id, a code, a token, a camelCase word, anything with a
# digit or an upper-case letter or holding ; = @ : or %) is written as ':id'.
SEGMENT_RE = re.compile(r"[a-z][a-z._-]{0,31}|v[0-9]{1,3}")


def path_shape(path: str) -> str:
    """The shape of a path: each segment as it is if SEGMENT_RE matches it whole, else ':id'. A reduction,
    not a guarantee: a segment made only of lower-case letters, dots, hyphens and underscores stays, so a
    user name such as `alice` stays."""
    return "/".join(seg if not seg or SEGMENT_RE.fullmatch(seg) else ":id" for seg in path.split("/"))
CR, LF = 13, 10
STOP_WAIT_S = 2.0          # after a stop signal: the time the connections get to close
FRONT_COMMANDS = ("http-front", "tls-front", "httpfront.py", "tlsfront.py")

# Reader states
HEAD, BODY, CHUNK_SIZE, CHUNK_END, TRAILER, STOPPED = "head", "body", "chunk-size", "chunk-end", "trailer", "stopped"


class FrontError(RuntimeError):
    """A condition the caller must see; exit status 2."""


class RequestReader:
    """Reads HTTP/1.x requests from the bytes a client sends, fed in pieces split at any byte. Returns
    events (dicts with 'event' and its fields, no time, no connection number). Once it has returned
    an 'unparsed' or an 'upgrade' event it reads nothing more. It never raises on input."""

    def __init__(self):
        self.state = HEAD
        self.buf = bytearray()       # the line being collected in HEAD, CHUNK_SIZE and TRAILER
        self.remaining = 0           # bytes of body or chunk data still to skip in BODY
        self.after_body = HEAD
        self.cr = False              # in CHUNK_END: a CR has been seen
        self.requests = 0
        self._new_head()

    def _new_head(self):
        self.start = None            # (method, target) once the start line is read
        self.headers: list[list[str]] = []
        self.head_size = 0
        self.trailer_size = 0

    def stop(self):
        self.state = STOPPED
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        out: list[dict] = []
        i, n = 0, len(data)
        while i < n and self.state != STOPPED:
            if self.state == BODY:
                take = min(self.remaining, n - i)
                i += take
                self.remaining -= take
                if self.remaining == 0:
                    self.state = self.after_body
                    if self.state == HEAD:
                        self._new_head()
            elif self.state == CHUNK_END:
                b = data[i]
                i += 1
                if b == LF:
                    self.state, self.cr = CHUNK_SIZE, False
                elif b == CR and not self.cr:
                    self.cr = True
                else:
                    self._unparsed(out, "chunk data not followed by a line end")
            else:
                j = data.find(b"\n", i)
                if j < 0:
                    self.buf += data[i:]
                    i = n
                    self._partial(out)
                else:
                    self.buf += data[i:j + 1]
                    i = j + 1
                    line = bytes(self.buf)
                    self.buf = bytearray()
                    self._line(out, line)
        return out

    # -- one complete line (with its LF) in HEAD, CHUNK_SIZE or TRAILER ------------------------------

    def _line(self, out: list, raw: bytes) -> None:
        text = raw[:-1]
        if text.endswith(b"\r"):
            text = text[:-1]
        if self.state == HEAD:
            self.head_size += len(raw)
            if self.head_size > HEAD_MAX:
                return self._unparsed(out, f"a request head over {HEAD_MAX // 1024} KiB")
            if self.start is None:
                if not text:
                    return                      # an empty line before a request is allowed
                parsed = self._start_line(text)
                if isinstance(parsed, str):
                    return self._unparsed(out, parsed)
                self.start = parsed
            elif not text:
                self._head_done(out)
            elif text[:1] in (b" ", b"\t"):
                if not self.headers:
                    return self._unparsed(out, "a header line that does not parse")
                self.headers[-1][1] += " " + text.decode("latin-1").strip()
            else:
                name, sep, value = text.partition(b":")
                if not sep or not name or any(c not in TOKEN for c in name):
                    return self._unparsed(out, "a header line that does not parse")
                self.headers.append([name.decode("ascii").lower(), value.decode("latin-1").strip()])
        elif self.state == CHUNK_SIZE:
            size = text.split(b";", 1)[0].strip(b" \t").decode("latin-1")
            if not HEX_RE.fullmatch(size):
                return self._unparsed(out, "a chunk-size line that does not parse")
            n = int(size, 16)
            if n == 0:
                self.state = TRAILER
            else:
                self.state, self.remaining, self.after_body = BODY, n, CHUNK_END
        elif self.state == TRAILER:
            self.trailer_size += len(raw)
            if self.trailer_size > LINE_MAX:
                return self._unparsed(out, f"a trailer section over {LINE_MAX // 1024} KiB")
            if not text:
                self.state = HEAD
                self._new_head()

    def _partial(self, out: list) -> None:
        """A line not yet ended: refuse what can already be refused, and bound what is held."""
        if self.state == HEAD:
            if self.head_size + len(self.buf) > HEAD_MAX:
                return self._unparsed(out, f"a request head over {HEAD_MAX // 1024} KiB")
            if self.start is None:
                pending = bytes(self.buf).lstrip(b"\r")
                space = pending.find(b" ")
                method = pending if space < 0 else pending[:space]
                if any(c not in TOKEN for c in method) or (space < 0 and len(method) > METHOD_MAX) or space == 0:
                    return self._unparsed(out, "bytes that are not HTTP/1.x where a request should start")
        elif self.state == CHUNK_SIZE and len(self.buf) > LINE_MAX:
            return self._unparsed(out, f"a chunk-size line over {LINE_MAX // 1024} KiB")
        elif self.state == TRAILER and self.trailer_size + len(self.buf) > LINE_MAX:
            return self._unparsed(out, f"a trailer section over {LINE_MAX // 1024} KiB")

    @staticmethod
    def _start_line(text: bytes):
        """(method, target) or the reason the line is not an HTTP/1.x request line."""
        if text.startswith(b"PRI * HTTP/2"):
            return "an HTTP/2 connection preface: HTTP/2 is not read"
        parts = text.split(b" ")
        if len(parts) != 3:
            return "a start line that does not parse as HTTP/1.x"
        method, target, version = parts
        if (not method or len(method) > METHOD_MAX or any(c not in TOKEN for c in method) or not target
                or any(c <= 0x20 or c == 0x7F for c in target) or not VERSION_RE.fullmatch(version.decode("latin-1"))):
            return "a start line that does not parse as HTTP/1.x"
        return method.decode("ascii"), target.decode("latin-1")

    def _head_done(self, out: list) -> None:
        method, target = self.start
        if method == "CONNECT":
            path, query = "(tunnel)", False
        elif target == "*":
            path, query = "*", False
        elif target.startswith("/"):
            path, sep, _ = target.partition("?")
            query = bool(sep)
        elif "://" in target:
            rest = target.split("://", 1)[1]
            cut = min([k for k in (rest.find("/"), rest.find("?")) if k >= 0], default=len(rest))
            path, sep, _ = (rest[cut:] or "/").partition("?")
            path, query = path or "/", bool(sep)
        else:
            return self._unparsed(out, "a start line that does not parse as HTTP/1.x")
        self.requests += 1
        if path.startswith("/"):
            path = path_shape(path)
        out.append({"event": "request", "method": method, "path": path[:PATH_MAX], "query": query})
        values = lambda name: [v for k, v in self.headers if k == name]
        connection = {t.strip().lower() for v in values("connection") for t in v.split(",")}
        if method == "CONNECT" or "upgrade" in connection:
            out.append({"event": "upgrade"})
            return self.stop()
        codings = [c.strip().lower() for v in values("transfer-encoding") for c in v.split(",") if c.strip()]
        lengths = values("content-length")
        if values("transfer-encoding"):
            if not codings or codings[-1] != "chunked":
                return self._unparsed(out, "a Transfer-Encoding whose last coding is not chunked: the body's length "
                                           "cannot be read")
            self.state = CHUNK_SIZE
        elif lengths:
            each = {x.strip() for v in lengths for x in v.split(",")}
            if len(each) != 1 or not next(iter(each)).isdigit() or not next(iter(each)).isascii():
                return self._unparsed(out, "a Content-Length that does not parse")
            n = int(next(iter(each)))
            if n:
                self.state, self.remaining, self.after_body = BODY, n, HEAD
            else:
                self._new_head()
        else:
            self._new_head()

    def _unparsed(self, out: list, why: str) -> None:
        out.append({"event": "unparsed", "why": why})
        self.stop()


def last_listening(path: str) -> dict | None:
    """The log's last `listening` line, or None when it has none (or cannot be read)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    found = None
    for raw in data.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("event") == "listening":
            found = e
    return found


def front_command(pid) -> str | None:
    """The command line of pid when it is a running front (it names `http-front` or `tls-front`, or their
    scripts), else None."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not any(name in out.stdout for name in FRONT_COMMANDS):
        return None
    return out.stdout.strip()


def running_front(pid) -> bool:
    """Whether pid is a running process whose command line is a front's."""
    return front_command(pid) is not None


def process_cwd(pid: int) -> str | None:
    """The working folder of pid (Linux: /proc; elsewhere lsof), or None when it cannot be read."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for row in out.stdout.splitlines():
        if row.startswith("n/"):
            return row[1:]
    return None


def front_log(pid: int, command: str) -> str | None:
    """The file a front's `--log` argument names, made absolute against that process's working folder (or
    this one's when that cannot be read); None for a front started without `--log`. A path holding a
    space is read up to the space."""
    words = command.split()
    value = None
    for n, word in enumerate(words):
        if word == "--log" and n + 1 < len(words):
            value = words[n + 1]
        elif word.startswith("--log="):
            value = word[len("--log="):]
    if value is None:
        return None
    if os.path.isabs(value):
        return value
    return os.path.join(process_cwd(pid) or os.getcwd(), value)


def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def check_log_owner(path: str) -> None:
    """One log, one front: refuse a log whose last `listening` line names another running front whose
    `--log` is this same file. A pid that is gone, that is now some other program (after a reboot), or that
    is a front writing another log (or none) is no obstacle."""
    last = last_listening(path)
    pid = (last or {}).get("pid")
    if pid is None or pid == os.getpid():
        return
    command = front_command(pid)
    other = front_log(pid, command) if command else None
    if other is not None and same_file(other, path):
        raise FrontError(f"{path} is being written by another front (pid {pid}); stop it or give this one its own log")


def port_accepts(port, timeout: float = 2.0) -> bool:
    """Whether 127.0.0.1:port accepts a TCP connection within timeout. Nothing is sent: the connection is
    closed at once (a TLS front sees a client that left before its handshake and logs nothing)."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def append_line(path: str, event: dict) -> None:
    """One JSON line, written with one `write` to a file opened for appending, made 0600 whether it
    existed or not."""
    data = (json.dumps({"t": round(time.time(), 3), **event}) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


class Front:
    """Listens on 127.0.0.1:<port> and pipes each connection to 127.0.0.1:<to_port>."""

    def __init__(self, port: int, to_port: int, log_path: str | None = None):
        self.port, self.to_port, self.log_path = port, to_port, log_path
        self.conns = 0
        self._log_failed = False
        self.active: dict = {}          # task -> the connection's two writers, while it is piped

    def ssl_context(self) -> ssl.SSLContext | None:
        return None

    def listening_line(self, port: int) -> str:
        return f"listening on http://127.0.0.1:{port} -> http://127.0.0.1:{self.to_port}"

    def log(self, event: dict) -> None:
        """A line for an event of a connection. A log that cannot be written is said once on stderr;
        the connection goes on being piped."""
        if not self.log_path:
            return
        try:
            append_line(self.log_path, event)
        except OSError as exc:
            if not self._log_failed:
                self._log_failed = True
                print(f"ui-probe front: cannot write the log {self.log_path}: {exc.strerror or exc}; the "
                      "connections are still piped", file=sys.stderr, flush=True)

    @staticmethod
    async def pipe(reader, writer, counter: dict, key: str, observe=None) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                counter[key] += len(data)
                writer.write(data)              # handed on first; the observation comes after
                if observe is not None:
                    observe(data)
                await writer.drain()
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
                else:
                    writer.close()
            except (OSError, RuntimeError):
                pass

    def observer(self, conn: int):
        """The function that reads a connection's client bytes, or None without a log, and the reader."""
        if not self.log_path:
            return None, None
        reader = RequestReader()

        def observe(data: bytes) -> None:
            try:
                events = reader.feed(data)
            except Exception as exc:          # a defect in the reader never stops the pipe
                reader.stop()
                events = [{"event": "unparsed", "why": f"the request reader failed ({type(exc).__name__})"}]
            for ev in events:
                self.log({**{"event": ev["event"], "conn": conn}, **{k: v for k, v in ev.items() if k != "event"}})
        return observe, reader

    async def handle(self, client_reader, client_writer) -> None:
        started = time.monotonic()
        self.conns += 1
        conn = self.conns
        counter = {"up": 0, "down": 0}
        try:
            backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", self.to_port)
        except OSError as exc:
            self.log({"event": "backend-refused", "to": f"127.0.0.1:{self.to_port}", "error": str(exc)[:200]})
            client_writer.close()
            return
        observe, reader = self.observer(conn)
        task = asyncio.current_task()
        self.active[task] = (client_writer, backend_writer)
        try:
            await asyncio.gather(self.pipe(client_reader, backend_writer, counter, "up", observe),
                                 self.pipe(backend_reader, client_writer, counter, "down"))
        finally:
            self.active.pop(task, None)
            for w in (backend_writer, client_writer):
                try:
                    w.close()
                except (OSError, RuntimeError):
                    pass
            self.log({"event": "closed", "conn": conn, "duration_s": round(time.monotonic() - started, 3),
                      "bytes_up": counter["up"], "bytes_down": counter["down"],
                      "requests": reader.requests if reader is not None else None})

    async def start(self) -> asyncio.AbstractServer:
        """Listen on the loopback address only, then write the log's `listening` line (so the file
        exists before the first connection). Returned for the caller (and the tests) to own. A log that
        another running front writes is refused before anything listens."""
        if self.log_path:
            check_log_owner(self.log_path)
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, ssl=self.ssl_context())
        bound = server.sockets[0].getsockname()[1]
        if self.log_path:
            try:
                append_line(self.log_path, {"event": "listening", "port": bound, "to_port": self.to_port,
                                            "pid": os.getpid()})
            except OSError as exc:
                server.close()
                raise FrontError(f"cannot write the log {self.log_path}: {exc.strerror or exc}") from exc
        return server

    async def stop(self, server, signame: str) -> None:
        """Stop when told to: a `stopping` line, no new connection, every connection closed (each gets
        its `closed` line), all within STOP_WAIT_S whatever the clients do."""
        self.log({"event": "stopping", "signal": signame})
        server.close()
        tasks = list(self.active)
        for writers in list(self.active.values()):
            for w in writers:
                try:
                    w.transport.abort()
                except (OSError, RuntimeError, AttributeError):
                    pass
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_WAIT_S)
        for t in tasks:
            if not t.done():
                t.cancel()

    async def serve(self) -> None:
        stopped = asyncio.Event()
        received = []
        loop = asyncio.get_running_loop()

        def on_signal(sig):
            received.append(sig)
            stopped.set()
        # SIGHUP only when it is not ignored: a front started with `nohup ... &` must outlive the shell or the
        # terminal that started it, as nohup intends.
        signals = [signal.SIGINT, signal.SIGTERM]
        if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
            signals.append(signal.SIGHUP)
        for sig in signals:                                # before the listening line, which callers wait for
            try:
                loop.add_signal_handler(sig, on_signal, sig)
            except (NotImplementedError, RuntimeError):
                pass
        server = await self.start()
        print(self.listening_line(server.sockets[0].getsockname()[1]), flush=True)
        try:
            await stopped.wait()
        finally:
            name = signal.Signals(received[0]).name if received else "none"
            await self.stop(server, name)


def port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port {value} is outside 1 to 65535")
    return value
