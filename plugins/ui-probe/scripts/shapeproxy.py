#!/usr/bin/env python3
"""shapeproxy - a forward proxy that adds latency and a rate limit to every connection.

It never decrypts anything. HTTPS goes through CONNECT tunnels and plain HTTP is forwarded as
bytes, so certificate pinning in the app under test is unaffected. Each direction is a delay line
(a chunk is delivered `--delay-ms` after it was read, order kept) followed by a rate limiter.
What it cannot do: drop packets. Loss can only appear as a stall at this level.

Each direction holds at most QUEUE_CHUNKS reads in flight. A receiver that reads slowly fills that
queue and the proxy stops reading from the sender, so the sender is slowed as well rather than the
proxy buffering the whole transfer. A failure on either side tears the whole connection down with
a reset, so a peer never mistakes a broken transfer for a complete one. Plain HTTP carries one
request per connection: the request is forwarded with `Connection: close`, and the connection ends
with its response.

It needs no administrator rights. The device is pointed at it with the system proxy setting, so it
shapes any app whose network stack honours the system proxy, and no app that bypasses it.

One JSON line per connection goes to --log when the connection ends: target, start, duration,
bytes each way, and the error that ended it, if any. A connection still open when the proxy is
stopped (SIGTERM or SIGINT) is logged then, with the error "open when the proxy stopped", so once
the proxy has stopped the log is a complete count of the connections it carried (connections, not
requests). While it runs, a tunnel an app keeps open has no line yet.

It prints `listening on <address>:<port>` once the port is bound, and fails if the port is taken.

usage: ui-probe proxy --port 8899 [--bind 127.0.0.1] [--delay-ms 200] [--kbit 400] [--log tunnels.jsonl]
"""
from __future__ import annotations
import argparse
import asyncio
import json
import signal
import socket
import struct
import time

READ_SIZE = 4096
QUEUE_CHUNKS = 256        # per direction: at most READ_SIZE * QUEUE_CHUNKS bytes held in flight
_FAILED = object()        # queued by a reader whose side failed: the writer aborts, it never half-closes
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
STOPPED = "open when the proxy stopped"


class Aborted(Exception):
    """One direction of a connection failed; the whole connection is torn down."""


async def delay_line(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     delay_s: float, bits_per_s: float, count: list[int]) -> None:
    """Copy reader to writer, each chunk delivered delay_s after it was read and paced at the rate.
    Returns after passing a clean end of stream on as a half-close. Raises Aborted when the sending
    side fails (a reset) or the receiving side goes away; the reader is stopped either way."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)

    async def read_side() -> None:
        while True:
            try:
                data = await reader.read(READ_SIZE)
            except (ConnectionError, OSError) as exc:
                await queue.put((0.0, _FAILED, exc))
                return
            await queue.put((time.monotonic() + delay_s, data, None))
            if not data:
                return

    async def write_side() -> None:
        while True:
            due, data, error = await queue.get()
            if data is _FAILED:
                raise Aborted(f"the sending side failed: {type(error).__name__}")
            wait = due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                if not data:
                    # The source finished sending: pass that on as a half-close so the reply still
                    # in flight the other way is not cut off.
                    if writer.can_write_eof():
                        writer.write_eof()
                    else:
                        writer.close()
                    return
                if bits_per_s:
                    # A chunk takes its own size over the rate to arrive, so even a reply that
                    # fits one read is paced.
                    await asyncio.sleep(len(data) * 8 / bits_per_s)
                writer.write(data)
                await writer.drain()
            except (ConnectionError, OSError) as exc:
                raise Aborted(f"the receiving side went away: {type(exc).__name__}") from exc
            count[0] += len(data)

    reading = asyncio.ensure_future(read_side())
    try:
        await write_side()
    finally:
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)


def _reset(writer: asyncio.StreamWriter | None) -> None:
    """Close a socket with a reset (RST), so the peer sees an error rather than a clean end."""
    if writer is None:
        return
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    writer.transport.abort()


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass


def _close_after_one(head: bytes) -> bytes:
    """A plain HTTP request head with any connection-persistence headers replaced by
    `Connection: close`, so the server ends the connection after its response."""
    lines = head[:-4].split(b"\r\n")
    drop = (b"connection", b"proxy-connection", b"keep-alive")
    kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(b":", 1)[0].strip().lower() not in drop]
    return b"\r\n".join(kept + [b"Connection: close"]) + b"\r\n\r\n"


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, args, log,
                 active: set | None = None) -> None:
    """Serve one client connection. `active`, when given, holds the tasks of the connections in
    progress, so that the proxy can end them (and have them logged) when it stops."""
    started = time.time()
    t0 = time.monotonic()
    target = "?"
    error = None
    up, down = [0], [0]
    server_w = None
    tasks: list = []
    me = asyncio.current_task()
    stopped = False
    if active is not None:
        active.add(me)
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=30)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, where, _ = first.split(" ", 2)
        plain = method.upper() != "CONNECT"
        if plain:
            # plain HTTP with an absolute URI: one request per connection
            rest = where.split("://", 1)[-1]
            hostport = rest.split("/", 1)[0]
            host, _, port = hostport.partition(":")
            port = port or "80"
        else:
            host, _, port = where.rpartition(":")
            host = host.strip("[]")
        target = f"{host}:{port}"
        try:
            server_r, server_w = await asyncio.open_connection(host, int(port))
        except (OSError, ValueError) as exc:
            target = f"{target} ({type(exc).__name__})"
            error = "target unreachable"
            client_w.write(BAD_GATEWAY)
            await client_w.drain()
            return
        if plain:
            request = _close_after_one(head)
            server_w.write(request)
            await server_w.drain()
            up[0] += len(request)
        else:
            client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await client_w.drain()
        upstream = asyncio.ensure_future(delay_line(client_r, server_w, args.delay_ms / 1000, args.kbit * 1000, up))
        downstream = asyncio.ensure_future(delay_line(server_r, client_w, args.delay_ms / 1000, args.kbit * 1000, down))
        tasks = [upstream, downstream]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            failed = [t for t in done if not t.cancelled() and t.exception() is not None]
            if failed:
                raise failed[0].exception()
            if plain and downstream in done:
                break       # the response has ended: the connection is done
    except asyncio.CancelledError:   # the proxy is stopping with this connection still open
        stopped = True
        error = error or STOPPED
        raise
    except Exception as exc:  # a client that went away, a reset, a malformed request: record and move on
        error = error or f"{type(exc).__name__}: {exc}"[:200]
        if target != "?" and "(" not in target:
            target = f"{target} ({type(exc).__name__})"
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if stopped:
            # This task is being cancelled: nothing here may wait. Reset both sides and log.
            _reset(server_w)
            _reset(client_w)
        else:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if error and error != "target unreachable":
                _reset(server_w)
                _reset(client_w)
            else:
                await _close(server_w)
                await _close(client_w)
        log.write(json.dumps({"target": target, "start_epoch": round(started, 3),
                              "duration_s": round(time.monotonic() - t0, 3),
                              "bytes_up": up[0], "bytes_down": down[0], "error": error}) + "\n")
        log.flush()
        if active is not None:
            active.discard(me)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ui-probe proxy", description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on; a physical device needs an address it can reach, or `adb reverse`")
    ap.add_argument("--delay-ms", type=float, default=0, help="added latency per direction")
    ap.add_argument("--kbit", type=float, default=0, help="rate limit per direction and connection, kbit/s; 0 = none")
    ap.add_argument("--log", default="tunnels.jsonl")
    return ap.parse_args(argv)


async def start(args: argparse.Namespace, log, active: set | None = None) -> asyncio.AbstractServer:
    """Start listening and return the server; the caller owns its lifetime (used by the tests)."""
    return await asyncio.start_server(lambda r, w: handle(r, w, args, log, active), args.bind, args.port)


async def serve(args: argparse.Namespace) -> None:
    active: set = set()
    with open(args.log, "a") as log:
        server = await start(args, log, active)
        # One line once the port is bound, so a caller can wait for it instead of guessing, and can
        # tell this proxy from an older one still holding the port (that start fails instead).
        host, port = server.sockets[0].getsockname()[:2]
        print(f"listening on {host}:{port}", flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass        # this platform or thread has no loop signal handlers: the default applies
        try:
            await stop.wait()
        finally:
            # End the connections still open, so that each writes its log line, then stop listening.
            server.close()
            for task in list(active):
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(serve(parse_args(argv)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
