"""0.7.0: requests counted from outside the app. The request reader of the fronts (every split point),
both fronts end to end on loopback sockets, /proc/uptime read through a faked `adb`, the step
windows on both platforms, known and unknown counts, the gate's rules, the refusals, and runs of
earlier versions. No device, no simulator, no network beyond 127.0.0.1."""
import sys

sys.dont_write_bytecode = True

import asyncio
import contextlib
import copy
import io
import json
import os
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import httpfront  # noqa: E402
import httppipe as hp  # noqa: E402
import test_analysis as ta  # noqa: E402
import test_run_and_cli as trc  # noqa: E402
import test_simulator_040 as s040  # noqa: E402
import tlsfront as tf  # noqa: E402
import ui_probe as u  # noqa: E402

OPENSSL = shutil.which("openssl")


def _accept_forever(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        conn.close()


# A port that accepts, standing in for a running front in logs the tests write by hand: a run checks at its
# end that the port of the front's `listening` line still accepts a connection (fix round 2, G2).
LISTENER = socket.socket()
LISTENER.bind(("127.0.0.1", 0))
LISTENER.listen(64)
LISTEN_PORT = LISTENER.getsockname()[1]
__import__("threading").Thread(target=_accept_forever, args=(LISTENER,), daemon=True).start()


def listening(t, **kw):
    """A front's `listening` line for a front that runs (this process) and accepts (LISTENER)."""
    return line(t, "listening", **{"port": LISTEN_PORT, "to_port": 1, "pid": os.getpid(), **kw})


def feed_all(pieces):
    reader = hp.RequestReader()
    events = []
    for piece in pieces:
        events += reader.feed(piece)
    return reader, events


def every_split(data):
    """The events for the bytes fed whole, byte by byte, and cut in two at every point: all must agree."""
    whole = feed_all([data])[1]
    out = [whole, feed_all([data[i:i + 1] for i in range(len(data))])[1]]
    out += [feed_all([data[:k], data[k:]])[1] for k in range(1, len(data))]
    return whole, out


def req(method, path, query=False):
    return {"event": "request", "method": method, "path": path, "query": query}


# --------------------------------------------------------------------------------------------
# the request reader
# --------------------------------------------------------------------------------------------

class ReaderTest(unittest.TestCase):
    def assert_every_split(self, data, expected):
        whole, all_ = every_split(data)
        self.assertEqual(whole, expected)
        for n, events in enumerate(all_):
            self.assertEqual(events, expected, f"split variant {n}")

    def test_a_request_with_a_query_at_every_split_point(self):
        data = b"GET /api/v1/items?token=SECRET&x=1 HTTP/1.1\r\nHost: example\r\nAuthorization: Bearer HIDDEN\r\n\r\n"
        self.assert_every_split(data, [req("GET", "/api/v1/items", True)])
        self.assertNotIn("SECRET", json.dumps(feed_all([data])[1]))
        self.assertNotIn("HIDDEN", json.dumps(feed_all([data])[1]))

    def test_content_length_body_is_skipped_and_the_next_request_read(self):
        body = b"GET /not-a-request HTTP/1.1\r\n\r\n" + b"x" * 7     # a body that looks like a request
        data = (b"POST /api/v1/save HTTP/1.1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                + b"GET /api/v1/next HTTP/1.1\r\n\r\n")
        self.assert_every_split(data, [req("POST", "/api/v1/save"), req("GET", "/api/v1/next")])

    def test_chunked_with_extensions_and_trailers(self):
        data = (b"PUT /upload HTTP/1.1\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"
                b"5;name=value;flag\r\nhello\r\n"
                b"A \t;ext\r\n0123456789\r\n"
                b"0;last=1\r\nX-Checksum: abc\r\nX-Other: def\r\n\r\n"
                b"DELETE /items/7 HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
        self.assert_every_split(data, [req("PUT", "/upload"), req("DELETE", "/items/:id")])

    def test_pipelined_requests_and_leading_empty_lines(self):
        data = b"\r\nGET /a HTTP/1.1\r\n\r\nGET /b?q HTTP/1.0\r\n\r\nHEAD / HTTP/1.1\r\n\r\n"
        self.assert_every_split(data, [req("GET", "/a"), req("GET", "/b", True), req("HEAD", "/")])

    def test_a_head_over_64_kib(self):
        data = b"GET /x HTTP/1.1\r\nX-Big: " + b"a" * (64 * 1024) + b"\r\n\r\n"
        reader, events = feed_all([data])
        self.assertEqual(events, [{"event": "unparsed", "why": "a request head over 64 KiB"}])
        self.assertEqual(reader.requests, 0)
        # also when the line never ends
        self.assertEqual(feed_all([b"GET /x HTTP/1.1\r\nX: " + b"a" * (64 * 1024)])[1],
                         [{"event": "unparsed", "why": "a request head over 64 KiB"}])

    def test_an_http2_preface(self):
        data = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + b"\x00\x00\x12\x04\x00"
        self.assert_every_split(data, [{"event": "unparsed", "why": "an HTTP/2 connection preface: HTTP/2 is not read"}])

    def test_upgrade(self):
        data = (b"GET /socket HTTP/1.1\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\n\r\n"
                b"\x81\x85GET /never HTTP/1.1\r\n\r\n")
        self.assert_every_split(data, [req("GET", "/socket"), {"event": "upgrade"}])
        self.assertEqual(feed_all([b"CONNECT host:443 HTTP/1.1\r\n\r\n\x16\x03"])[1],
                         [req("CONNECT", "(tunnel)"), {"event": "upgrade"}])

    def test_binary_garbage_is_refused_before_a_line_ends(self):
        reader, events = feed_all([b"\x16\x03\x01\x02\x00\x01"])          # a TLS hello sent to the plain front
        self.assertEqual(events, [{"event": "unparsed", "why": "bytes that are not HTTP/1.x where a request should start"}])
        self.assertEqual(reader.feed(b"GET / HTTP/1.1\r\n\r\n"), [])        # never read again
        for bad in (b"GET  / HTTP/1.1\r\n\r\n", b"GET / HTTP/2\r\n\r\n", b"GET / HTTP/1.1 extra\r\n\r\n",
                    b"GET relative HTTP/1.1\r\n\r\n"):
            self.assertEqual(feed_all([bad])[1][0]["event"], "unparsed", bad)

    def test_a_path_over_300_characters(self):
        path = "/ab" * 140
        events = feed_all([f"GET {path}?q=1 HTTP/1.1\r\n\r\n".encode()])[1]
        self.assertEqual(events, [req("GET", path[:300], True)])
        self.assertEqual(len(events[0]["path"]), 300)

    def test_absolute_form_and_asterisk(self):
        events = feed_all([b"GET http://example.test:8080/a/b?c HTTP/1.1\r\n\r\nOPTIONS * HTTP/1.1\r\n\r\n"
                           b"GET http://example.test HTTP/1.1\r\n\r\n"])[1]
        self.assertEqual(events, [req("GET", "/a/b", True), req("OPTIONS", "*"), req("GET", "/")])

    def test_framing_that_cannot_be_read(self):
        cases = {
            b"POST / HTTP/1.1\r\nContent-Length: 12x\r\n\r\n": "a Content-Length that does not parse",
            b"POST / HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\n": "a Content-Length that does not parse",
            b"POST / HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n": "a Transfer-Encoding whose last coding is not chunked: "
                                                                   "the body's length cannot be read",
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n": "a chunk-size line that does not parse",
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nabX": "chunk data not followed by a line end",
            b"GET / HTTP/1.1\r\nBad Header: x\r\n\r\n": "a header line that does not parse",
        }
        for data, why in cases.items():
            events = feed_all([data])[1]
            self.assertEqual(events[-1], {"event": "unparsed", "why": why}, data)
        # the same Content-Length twice is one length
        self.assertEqual(feed_all([b"POST /a HTTP/1.1\r\nContent-Length: 2, 2\r\n\r\nokGET /b HTTP/1.1\r\n\r\n"])[1],
                         [req("POST", "/a"), req("GET", "/b")])
        # an obsolete folded header line continues the one before
        self.assertEqual(feed_all([b"GET /f HTTP/1.1\r\nX-A: 1\r\n  2\r\n\r\n"])[1], [req("GET", "/f")])

    def test_bare_line_feeds(self):
        self.assertEqual(feed_all([b"GET /lf HTTP/1.1\nHost: x\n\nGET /lf2 HTTP/1.1\n\n"])[1],
                         [req("GET", "/lf"), req("GET", "/:id")])

    def test_a_reader_defect_never_stops_the_pipe(self):
        front = hp.Front(1, 2, None)
        with tempfile.TemporaryDirectory() as tmp:
            front.log_path = str(Path(tmp) / "log")
            observe, reader = front.observer(3)
            with mock.patch.object(reader, "feed", side_effect=RuntimeError("boom")):
                observe(b"GET / HTTP/1.1\r\n\r\n")
            lines = [json.loads(line) for line in Path(front.log_path).read_text().splitlines()]
        self.assertEqual([(e["event"], e["conn"], e["why"]) for e in lines],
                         [("unparsed", 3, "the request reader failed (RuntimeError)")])
        self.assertEqual(reader.state, hp.STOPPED)


# --------------------------------------------------------------------------------------------
# both fronts end to end on loopback sockets
# --------------------------------------------------------------------------------------------

class FrontEndToEnd:
    """Shared by the plain and the TLS front. A backend that keeps every byte it got and answers with
    a response streamed in pieces."""

    STREAM = [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"5\r\nfirst\r\n", b"6\r\nsecond\r\n", b"0\r\n\r\n"]

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "front.jsonl"
        self.got = bytearray()
        self.release = asyncio.Event()
        self.head_seen = asyncio.Event()

        async def backend(reader, writer):
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.got += data
                if b"\r\n\r\n" in self.got:
                    self.head_seen.set()
                if self.got.endswith(b"GO\r\n\r\n") or self.got.endswith(b"/stream HTTP/1.1\r\n\r\n"):
                    for piece in self.STREAM:
                        writer.write(piece)
                        await writer.drain()
                        await self.release.wait()
                    break
            writer.close()
        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        self.backend_port = self.backend.sockets[0].getsockname()[1]
        self.front = self.make_front()
        self.server = await self.front.start()
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.release.set()
        # every connection's last line is written before the folder goes
        for _ in range(100):
            if sum(e["event"] in ("closed", "backend-refused") for e in self.entries()) >= self.front.conns:
                break
            await asyncio.sleep(0.01)
        for s in (self.server, self.backend):
            s.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.wait_closed(), 2)
        self.tmp.cleanup()

    def entries(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    async def wait_for(self, pred, what):
        for _ in range(200):
            if pred():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {what}; log: {self.entries()}")

    async def test_the_listening_line_comes_first(self):
        first = self.entries()[0]
        self.assertEqual((first["event"], first["port"], first["to_port"], first["pid"]),
                         ("listening", self.port, self.backend_port, os.getpid()))
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    async def test_bytes_arrive_unchanged_both_ways_while_the_log_fills(self):
        sent = (b"POST /api/v1/a?secret=QUERYVALUE HTTP/1.1\r\nHost: h\r\nCookie: COOKIEVALUE\r\nContent-Length: 9\r\n\r\n"
                b"BODYVALUE"
                b"PUT /api/v1/b HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n3;e=1\r\nabc\r\n0\r\nT: TRAILERVALUE\r\n\r\n"
                b"GET /api/v1/stream HTTP/1.1\r\n\r\n")
        r, w = await self.connect()
        for k in range(0, len(sent), 17):                  # odd pieces: heads split everywhere
            w.write(sent[k:k + 17])
            await w.drain()
            await asyncio.sleep(0.001)
        self.release.set()
        answer = await asyncio.wait_for(r.read(), 5)
        self.assertEqual(answer, b"".join(self.STREAM))
        self.assertEqual(bytes(self.got), sent)
        w.close()
        await self.wait_for(lambda: any(e["event"] == "closed" for e in self.entries()), "the closed line")
        log = self.entries()
        self.assertEqual([(e["event"], e.get("method"), e.get("path"), e.get("query")) for e in log
                          if e["event"] == "request"],
                         [("request", "POST", "/api/v1/a", True), ("request", "PUT", "/api/v1/b", False),
                          ("request", "GET", "/api/v1/stream", False)])
        closed = [e for e in log if e["event"] == "closed"][0]
        self.assertEqual((closed["conn"], closed["requests"], closed["bytes_up"], closed["bytes_down"]),
                         (1, 3, len(sent), len(answer)))
        text = self.log.read_text()
        for secret in ("QUERYVALUE", "COOKIEVALUE", "BODYVALUE", "TRAILERVALUE", "secret", "Host"):
            self.assertNotIn(secret, text)

    async def test_half_a_head_is_piped_at_once_and_logged_when_complete(self):
        r, w = await self.connect()
        w.write(b"GET /api/v1/slow HTTP/1.1\r\nHo")
        await w.drain()
        await self.wait_for(lambda: bytes(self.got) == b"GET /api/v1/slow HTTP/1.1\r\nHo", "the first half at the backend")
        self.assertEqual([e["event"] for e in self.entries()], ["listening"])     # no request yet
        w.write(b"st: x\r\n\r\n")
        await w.drain()
        await self.wait_for(lambda: any(e["event"] == "request" for e in self.entries()), "the request line")
        self.assertEqual([e for e in self.entries() if e["event"] == "request"][0]["path"], "/api/v1/slow")
        w.close()

    async def test_a_streaming_backend_reaches_the_client_piece_by_piece(self):
        r, w = await self.connect()
        w.write(b"GET /api/v1/stream HTTP/1.1\r\n\r\n")
        await w.drain()
        first = await asyncio.wait_for(r.readexactly(len(self.STREAM[0])), 5)
        self.assertEqual(first, self.STREAM[0])           # before the backend sent the rest
        self.release.set()
        self.assertEqual(await asyncio.wait_for(r.read(), 5), b"".join(self.STREAM[1:]))
        w.close()

    async def test_unparsed_bytes_are_still_piped(self):
        r, w = await self.connect()
        junk = b"\x00\x01\x02 not http\r\n\r\nGET /after HTTP/1.1\r\n\r\n"
        w.write(junk)
        await w.drain()
        await self.wait_for(lambda: bytes(self.got) == junk, "the bytes at the backend")
        w.close()
        await self.wait_for(lambda: any(e["event"] == "closed" for e in self.entries()), "the closed line")
        kinds = [(e["event"], e.get("conn")) for e in self.entries()]
        self.assertEqual(kinds[1:], [("unparsed", 1), ("closed", 1)])
        self.assertEqual([e for e in self.entries() if e["event"] == "closed"][0]["requests"], 0)


class HttpFrontTest(FrontEndToEnd, unittest.IsolatedAsyncioTestCase):
    def make_front(self):
        return hp.Front(0, self.backend_port, str(self.log))

    async def connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def test_loopback_only_and_its_line(self):
        self.assertEqual(self.server.sockets[0].getsockname()[0], "127.0.0.1")
        self.assertEqual(self.front.listening_line(18080), f"listening on http://127.0.0.1:18080 -> "
                                                           f"http://127.0.0.1:{self.backend_port}")


@unittest.skipUnless(OPENSSL, "no openssl on PATH: tls-front makes its certificate authority with it")
class TlsFrontRequestsTest(FrontEndToEnd, unittest.IsolatedAsyncioTestCase):
    def make_front(self):
        self.state, _ = tf.ensure_state(Path(self.tmp.name) / "state")
        return tf.Front(0, self.backend_port, self.state, str(self.log))

    async def connect(self):
        ctx = ssl.create_default_context(cafile=str(self.state / "ca.pem"))
        ctx.set_alpn_protocols(["h2", "http/1.1"])          # a client that would take HTTP/2 is given http/1.1
        r, w = await asyncio.open_connection("localhost", self.port, ssl=ctx, server_hostname="localhost")
        self.assertEqual(w.get_extra_info("ssl_object").selected_alpn_protocol(), "http/1.1")
        return r, w


class FrontLogWithoutLogTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_log_no_reader(self):
        front = hp.Front(0, 1, None)
        self.assertEqual(front.observer(1), (None, None))


class HttpFrontCommandTest(unittest.TestCase):
    def test_listening_line_and_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "front.jsonl"
            with free_ports(2) as (p1, p2):
                proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "http-front", "--port", str(p1),
                                         "--to-port", str(p2), "--log", str(log)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), f"listening on http://127.0.0.1:{p1} -> http://127.0.0.1:{p2}")
                entry = json.loads(log.read_text().splitlines()[0])
                self.assertEqual((entry["event"], entry["port"], entry["to_port"], entry["pid"]), ("listening", p1, p2, proc.pid))
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(10), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                proc.stderr.close()

    def test_refused_arguments(self):
        for argv in (["--port", "8080", "--to-port", "8080"], ["--port", "0", "--to-port", "1"], ["--port", "1"],
                     ["--to-port", "1"]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                httpfront.parse_args(argv)
            self.assertEqual(cm.exception.code, 2, argv)

    def test_a_log_that_cannot_be_written_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, free_ports(2) as (p1, p2):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = httpfront.main(["--port", str(p1), "--to-port", str(p2), "--log", str(Path(tmp) / "no" / "log")])
            self.assertEqual(rc, 2)
            self.assertIn("ui-probe http-front: cannot write the log", err.getvalue())


@contextlib.contextmanager
def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    yield ports


# --------------------------------------------------------------------------------------------
# the clock offset on Android, read through a faked adb on PATH
# --------------------------------------------------------------------------------------------

class UptimeProbeTest(unittest.TestCase):
    """The device's /proc/uptime read through a faked `adb` on PATH (fix round 1, F5; the reviewer's
    repro_clock.py showed the wall-clock mapping it replaces)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.answers = Path(self.tmp.name) / "answers"

    def tearDown(self):
        self.tmp.cleanup()

    def fake_adb(self, answers, exit_code=0):
        """An adb that prints the next of `answers` for each `shell` call and records its arguments."""
        self.answers.write_text("\n".join(answers) + "\n")
        calls = Path(self.tmp.name) / "calls"
        script = self.bin / "adb"
        script.write_text("#!/bin/sh\n"
                          f'printf "%s\\n" "$*" >> "{calls}"\n'
                          f'line=$(head -n 1 "{self.answers}"); tail -n +2 "{self.answers}" > "{self.answers}.t"; '
                          f'mv "{self.answers}.t" "{self.answers}"\n'
                          f'echo "$line"\nexit {exit_code}\n')
        script.chmod(0o755)
        return calls

    def probe(self, host_times):
        it = iter(host_times)
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}):
            return u.uptime_probes("<serial>", clock=lambda: next(it))

    def test_the_shortest_round_trip_is_kept(self):
        calls = self.fake_adb(["10.50 40.00", "20.05 80.00", "30.90 120.00"])
        res = self.probe([1000.0, 1001.0, 1010.0, 1010.1, 1020.0, 1020.5])
        self.assertTrue(res["known"])
        self.assertEqual((res["round_trip_s"], res["uptime"]), (0.1, 20.05))
        self.assertAlmostEqual(res["host"], 1010.05)
        self.assertAlmostEqual(res["offset_s"], 1010.05 - 20.05)
        self.assertEqual(len(res["probes"]), 3)
        self.assertEqual(calls.read_text().splitlines()[0], "-s <serial> shell cat /proc/uptime")

    def test_no_usable_value_is_unknown_with_the_reason(self):
        self.fake_adb(["sh: weird", "", "nothing"])
        res = self.probe([0, 1, 2, 3, 4, 5])
        self.assertFalse(res["known"])
        self.assertIn("the device's /proc/uptime could not be read from this machine", res["reason"])
        self.assertIn("'sh: weird'", res["reason"])
        self.fake_adb(["x", "x", "x"], exit_code=1)
        res = self.probe([0, 1, 2, 3, 4, 5])
        self.assertFalse(res["known"])
        self.assertIn("failed (exit 1)", res["reason"])
        clock, why = u.android_request_clock(res, probe_group(5000.0, 100.0))
        self.assertEqual((clock["known"], why), (False, res["reason"]))


def probe_group(host, uptime, rtt=0.004):
    return {"known": True, "uptime": uptime, "host": host, "offset_s": host - uptime, "round_trip_s": rtt}


class AndroidClockTest(unittest.TestCase):
    """Fix round 1, F5 (the reviewer's repro_clock.py): the straight line through a probe before and one
    after the device script, the drift and uncertainty limits, and the check against this machine's own
    bracket of the script."""

    def test_the_line_follows_a_slow_drift(self):
        clock, why = u.android_request_clock(probe_group(4990.0, 100.0), probe_group(5010.05, 120.0))
        self.assertIsNone(why)
        self.assertAlmostEqual(clock["uncertainty_s"], 0.004 + 0.01 + 0.05)
        self.assertAlmostEqual(u.uptime_to_host(clock, 110.0), 5000.025)

    def test_clocks_that_moved_apart_are_unknown(self):
        clock, why = u.android_request_clock(probe_group(4990.0, 100.0), probe_group(5014.0, 120.0))
        self.assertFalse(clock["known"])
        self.assertEqual(why, "the device's clock and this machine's moved apart by 4.000 s during the run (the emulator "
                              "was paused, or a clock was set)")

    def test_a_slow_adb_is_unknown(self):
        clock, why = u.android_request_clock(probe_group(4990.0, 100.0, rtt=0.6), probe_group(5010.0, 120.0, rtt=0.6))
        self.assertFalse(clock["known"])
        self.assertIn("only to within 0.610 s (above 0.2 s): adb answered too slowly", why)

    def test_the_mapped_run_must_lie_in_the_scripts_bracket(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            (raw / "marks.txt").write_text("".join(f"{k} {v}\n" for k, v in ta.MARKS.items()))
            log = raw / "front.jsonl"
            log.write_text(listening(1.0))
            start = u.log_position(log)
            # the probes say uptime 100 = host 4990, but this machine ran the script from 5100 to 5130
            u.capture_requests_android(raw, log, start, (probe_group(4990.0, 100.0), probe_group(5010.0, 120.0)),
                                       (5100.0, 5130.0), ta.SCN)
            meta = json.loads((raw / u.REQUESTS_META).read_text())
            self.assertFalse(meta["clock"]["known"])
            self.assertEqual(meta["reason"], "the clock mapping puts the launch at 4991.500 and the end of the last step "
                                             "at 5007.600, outside the time this machine ran the device script "
                                             "(5100.000 to 5130.000, widened by 0.014 s)")
            self.assertEqual(meta["window"], [5100.0, 5130.0])

    def test_the_device_script_is_byte_identical_to_0_6_0(self):
        """Nothing is added to the measured path: these are the digests of 0.6.0's device script for two
        scenarios (computed from 0.6.0's own build_device_script)."""
        import hashlib
        a = {"name": "Example", "package": "com.example.app", "steps": [
            {"id": "idle", "do": "rest", "ms": 2000}, {"id": "open", "do": "tap", "x": 0.5, "y": 0.5},
            {"id": "idle2", "do": "rest", "ms": 2000, "meminfo": True}]}
        b = {"name": "Example", "package": "com.example.app", "activity": ".Main", "until_quiet": True,
             "launch_settle_ms": 4000, "steps": [
                 {"id": "open", "do": "tap", "find": {"text": "Sign up", "scroll": {"from": [0.5, 0.8], "to": [0.5, 0.3],
                                                                                   "max": 3}}},
                 {"id": "type", "do": "text", "text": "a b@c"},
                 {"id": "back", "do": "key", "code": 4, "until_quiet": False, "settle_ms": 1500},
                 {"id": "fling", "do": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.2], "ms": 300, "quiet": {"ms": 500}},
                 {"id": "rest", "do": "rest", "ms": 3000}]}
        for scn, video, digest in ((a, False, "f84f0e5ab4db19cb95e1852fd7c29fec3246fbec423fa919b302b01349947fee"),
                                   (b, True, "cd8893a62f6170c307a5beda4ed41cf041d2ffa6bc8cce588467f329c7a7dc97")):
            text = u.build_device_script(scn, 1080, 2400, video, 120)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)


# --------------------------------------------------------------------------------------------
# the log segment, the windows and the counts
# --------------------------------------------------------------------------------------------

def line(t, event, **kw):
    return json.dumps({"t": t, "event": event, **kw}) + "\n"


def request_line(t, path, method="GET", conn=1):
    return line(t, "request", conn=conn, method=method, path=path, query=False)


class Segment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "front.jsonl"
        self.log.write_text(listening(1.0)
                            + request_line(2.0, "/before-the-run"))
        self.raw = self.root / "raw"
        self.raw.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text):
        with open(self.log, "a") as fh:
            fh.write(text)

    def meta(self):
        return json.loads((self.raw / u.REQUESTS_META).read_text())

    def kept(self):
        return [json.loads(x) for x in (self.raw / u.REQUESTS_FILE).read_text().splitlines()]


class SegmentTest(Segment):
    def test_only_what_was_appended_inside_the_window_is_kept(self):
        start = u.log_position(self.log)
        self.append(request_line(96.0, "/too-early") + request_line(98.5, "/margin") + request_line(100.0, "/a")
                    + line(101.0, "closed", conn=1) + line(105.0, "upgrade", conn=2)
                    + line(110.0, "unparsed", conn=3, why="x") + request_line(112.0, "/margin-after")
                    + request_line(112.5, "/too-late") + '{"t": 113.0, "event": "requ')
        u.save_requests(self.raw, self.log, start, (100.0, 110.0), {"platform": "x", "known": True}, None)
        self.assertEqual([(e["event"], e.get("path")) for e in self.kept()],
                         [("request", "/margin"), ("request", "/a"), ("upgrade", None), ("unparsed", None),
                          ("request", "/margin-after")])
        meta = self.meta()
        self.assertEqual((meta["offset_start"], meta["kept"], meta["partial_tail_bytes"], meta["reason"]),
                         (start["size"], 5, len('{"t": 113.0, "event": "requ'), None))
        self.assertEqual(meta["source"], str(self.log))
        self.assertEqual([(iv["conn"], iv["kind"], iv["to"]) for iv in meta["opaque"]], [(2, "upgrade", None),
                                                                                         (3, "unparsed", None)])

    def test_what_was_there_before_the_run_is_not_read(self):
        self.append(request_line(100.5, "/written-before-the-run-started"))
        start = u.log_position(self.log)
        self.append(request_line(101.0, "/during"))
        u.save_requests(self.raw, self.log, start, (100.0, 110.0), {}, None)
        self.assertEqual([e["path"] for e in self.kept()], ["/during"])

    def test_a_replaced_or_cut_log(self):
        start = u.log_position(self.log)
        self.log.write_text("")                         # cut (same file, shorter)
        u.save_requests(self.raw, self.log, start, (0, 9), {}, None)
        self.assertIn("cut during the run", self.meta()["reason"])
        self.log.unlink()
        self.log.write_text(line(1.0, "listening") * 5)
        u.save_requests(self.raw, self.log, start, (0, 9), {}, None)
        self.assertIn("replaced during the run", self.meta()["reason"])
        self.log.unlink()
        u.save_requests(self.raw, self.log, start, (0, 9), {}, None)
        self.assertIn("could not be read at the end of the run", self.meta()["reason"])


BEFORE, AFTER = probe_group(4990.0, 100.0), probe_group(5010.0, 120.0)
ANDROID_CLOCK = u.android_request_clock(BEFORE, AFTER)[0]          # host = uptime + 4890, u = 0.014 s


def host(uptime):
    return uptime + 4890.0


class AndroidWindowTest(Segment):
    """The marks of test_analysis (launch 101.5, idle 110-112, open 112.5-115, idle2 115.6-117.6) on the
    device's uptime, mapped to this machine's clock: host = uptime + 4890, within 0.014 s."""

    def capture(self, appended, clock=ANDROID_CLOCK, reason=None):
        start = u.log_position(self.log)
        self.append(appended)
        u.save_requests(self.raw, self.log, start, (host(101.5), host(117.6)), clock, reason)

    def steps(self):
        marks = ta.MARKS
        return [{"id": "launch", "start_s": marks["launch.start"], "end_s": marks["idle.start"]},
                {"id": "idle", "start_s": marks["idle.start"], "end_s": marks["idle.end"]},
                {"id": "open", "start_s": marks["open.start"], "end_s": marks["open.end"]},
                {"id": "idle2", "start_s": marks["idle2.start"], "end_s": marks["idle2.end"]}]

    def counts(self, steps=None):
        steps, result = steps or self.steps(), {}
        u.request_counts(self.raw, steps, result, u.ANDROID, ta.SCN, True, ta.MARKS)
        return {s["id"]: s["requests"] for s in steps}, result["requests"]

    def test_the_mapping(self):
        self.assertAlmostEqual(u.uptime_to_host(ANDROID_CLOCK, 110.0), 5000.0)
        self.assertIsNone(u.uptime_to_host({**ANDROID_CLOCK, "known": False}, 110.0))
        self.assertIsNone(u.uptime_to_host(ANDROID_CLOCK, None))

    def test_counts_per_step(self):
        self.capture(request_line(host(102.0), "/api/v1/config") + request_line(host(103.0), "/api/v1/feed")
                     + request_line(host(113.0), "/api/v1/feed") + request_line(host(113.2), "/api/v1/feed")
                     + request_line(host(113.4), "/api/v1/item", "POST")
                     + request_line(host(115.2), "/api/v1/between")      # between open's end and idle2's start
                     + request_line(host(116.0), "/api/v1/poll"))
        st, block = self.counts()
        self.assertEqual(st["launch"], {"count": 2, "by_path": {"GET /api/v1/config": 1, "GET /api/v1/feed": 1}})
        self.assertEqual(st["idle"], {"count": 0, "by_path": {}, "gap_s": 0.5, "in_gap": 0})
        self.assertEqual(st["open"], {"count": 3, "by_path": {"GET /api/v1/feed": 2, "POST /api/v1/item": 1}})
        self.assertEqual(st["idle2"], {"count": 1, "by_path": {"GET /api/v1/poll": 1}, "gap_s": 0.0, "in_gap": 0})
        self.assertEqual((block["known"], block["requests"], block["between_steps"], block["in_margins"]), (True, 7, 1, 0))
        self.assertEqual(block["between_paths"], {"GET /api/v1/between": 1})
        self.assertAlmostEqual(block["clock"]["uncertainty_s"], 0.014)

    def test_the_twenty_most_frequent_paths(self):
        self.capture("".join(request_line(host(113.0) + k / 1000, f"/p{k % 25:02d}") for k in range(30)))
        st, _ = self.counts()
        self.assertEqual(st["open"]["count"], 30)
        self.assertEqual(len(st["open"]["by_path"]), 20)
        self.assertEqual(list(st["open"]["by_path"].items())[:5],
                         [("GET /p00", 2), ("GET /p01", 2), ("GET /p02", 2), ("GET /p03", 2), ("GET /p04", 2)])

    def test_unknown_when_nothing_went_through_the_front(self):
        self.capture(line(host(102.0), "closed", conn=1))
        st, block = self.counts()
        self.assertFalse(block["known"])
        self.assertIn("the app's traffic is probably not going through the front", block["reason"])
        self.assertEqual(st["idle"], {"count": None, "reason": block["reason"]})

    def test_unknown_when_a_connection_was_not_http_1(self):
        self.capture(request_line(host(102.0), "/a") + line(host(113.0), "unparsed", conn=2,
                                                            why="an HTTP/2 connection preface: HTTP/2 is not read"))
        st, block = self.counts()
        self.assertFalse(block["known"])
        self.assertIn("1 connection carried bytes the front could not read as HTTP/1.x (an HTTP/2 connection preface",
                      block["reason"])
        self.assertIsNone(st["launch"]["count"])

    def test_unknown_when_the_front_restarted_or_a_line_is_not_json(self):
        self.capture(request_line(host(102.0), "/a") + listening(host(113.0)))
        self.assertIn("the front started listening again after the run began", self.counts()[1]["reason"])
        self.raw = self.root / "raw2"
        self.raw.mkdir()
        self.capture(request_line(host(102.0), "/a") + "not json\n")
        self.assertIn("1 line the front process wrote is not the front's JSON", self.counts()[1]["reason"])

    def test_unknown_without_the_clock(self):
        clock, why = u.android_request_clock(BEFORE, {"known": False, "reason": "the device's /proc/uptime could not be read"})
        self.capture(request_line(host(102.0), "/a"), clock=clock, reason=why)
        st, block = self.counts()
        self.assertFalse(block["known"])
        self.assertEqual(block["reason"], "the device's /proc/uptime could not be read")
        self.assertEqual(st["open"], {"count": None, "reason": "the device's /proc/uptime could not be read"})

    def test_a_step_without_marks_has_no_window(self):
        self.capture(request_line(host(102.0), "/a"))
        steps = self.steps()
        steps[2]["end_s"] = None
        st, _ = self.counts(steps)
        self.assertEqual(st["open"], {"count": None, "reason": "the run holds no window for this step"})
        self.assertEqual(st["launch"]["count"], 1)


class SimWindowTest(Segment):
    """On the iOS Simulator the steps are timed on this machine's clock already."""

    def test_counts_per_step(self):
        (self.raw / "launch-time.json").write_text(json.dumps({"wall": 1000.0}))
        (self.raw / "step-rest-a.json").write_text(json.dumps({"wall_before": 1007.0, "wall_after": 1012.0}))
        (self.raw / "step-tap.json").write_text(json.dumps({"wall_before": 1013.0, "wall_after": 1016.0}))
        scn = {"steps": [{"id": "rest-a", "do": "rest"}, {"id": "tap", "do": "tap"}]}
        start = u.log_position(self.log)
        self.append(request_line(999.0, "/margin") + request_line(1001.0, "/launch") + request_line(1007.0, "/rest")
                    + request_line(1012.5, "/gap") + request_line(1015.9, "/tap") + request_line(1017.9, "/margin2")
                    + request_line(1018.5, "/late"))
        u.capture_requests_sim(self.raw, self.log, start, (990.0, 1030.0), scn)
        steps = [{"id": "launch"}, {"id": "rest-a"}, {"id": "tap"}]
        result = {}
        u.request_counts(self.raw, steps, result, u.SIM_PLATFORM, scn, True)
        self.assertEqual(self.meta()["window"], [1000.0, 1016.0])
        # the rest runs on to the tap's start: /gap is a request at rest, in its 1 s gap
        self.assertEqual([s["requests"] for s in steps], [
            {"count": 1, "by_path": {"GET /launch": 1}},
            {"count": 2, "by_path": {"GET /gap": 1, "GET /rest": 1}, "gap_s": 1.0, "in_gap": 1},
            {"count": 1, "by_path": {"GET /tap": 1}}])
        self.assertEqual((result["requests"]["requests"], result["requests"]["between_steps"],
                          result["requests"]["in_margins"]), (6, 0, 2))

    def test_summary_keys_of_their_own(self):
        run = s040.sim_run()
        run["steps"][0]["requests"] = {"count": 3, "by_path": {"GET /a": 3}}
        run["steps"][1]["requests"] = {"count": None, "reason": "x"}
        row = u.sim_run_row("run-01", run)
        self.assertEqual((row["launch.sim_requests"], row["rest-a.sim_requests"]), (3, None))
        self.assertNotIn("rest-a.requests", row)
        self.assertNotIn("rest-a.sim_requests", u.sim_run_row("run-01", s040.sim_run()))

    def test_no_launch_time_is_unknown(self):
        start = u.log_position(self.log)
        self.append(request_line(1001.0, "/a"))
        u.capture_requests_sim(self.raw, self.log, start, (990.0, 1030.0), {"steps": [{"id": "rest-a"}]})
        self.assertIn("no time of its launch", self.meta()["reason"])
        self.assertEqual(self.meta()["window"], [990.0, 1030.0])


# --------------------------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------------------------

def android_run(rest_count=0, open_count=1, known=True, by_path=None, with_log=True, reason=None):
    launch = {"id": "launch", "kind": "launch", "start_s": 1.0, "end_s": 10.0, "frames": 20, "quiet": None,
              "process_restarted": False, "foreground": True}
    idle = {"id": "idle", "kind": "rest", "start_s": 10.0, "end_s": 12.0, "frames": 0, "cpu_ticks_total": 0,
            "cpu_window_s": 2.0, "quiet": None, "process_restarted": False, "foreground": True}
    tap = {"id": "open", "kind": "tap", "start_s": 12.0, "end_s": 14.0, "frames": 5, "quiet": None,
           "process_restarted": False, "foreground": True}
    run = {"device": {"clk_tck": 100}, "steps": [launch, idle, tap], "video_truncated": False,
           "clock_mismatch_suspected": False}
    if with_log:
        why = reason or "no request passed through the front"
        for st, n in ((launch, 3), (idle, rest_count), (tap, open_count)):
            st["requests"] = ({"count": n, "by_path": by_path if st is idle and by_path else
                               ({"GET /api/v1/x": n} if n else {})} if known else {"count": None, "reason": why})
        run["requests"] = {"known": known, "reason": None if known else why, "requests": 4, "unparsed": 0,
                           "upgrades": 0, "between_steps": 0, "clock": {}}
    return run


ANDROID_SCN = {"name": "Example", "package": ta.PKG, "steps": [{"id": "idle", "do": "rest", "ms": 2000},
                                                               {"id": "open", "do": "tap", "x": 0.5, "y": 0.5}]}


def scn_with(rest=None, tap=None, gate=None):
    scn = copy.deepcopy(ANDROID_SCN)
    scn["steps"][0].update(rest or {})
    scn["steps"][1].update(tap or {})
    if gate:
        scn["gate"] = gate
    u.validate_scenario(scn)
    return scn


def opts(scn):
    return {**u.GATE_DEFAULTS, **(scn.get("gate") or {})}


def gate(run, scn):
    return u.gate_run(run, scn, opts(scn))


def checks_of(res, check):
    return [v for v in res["violations"] if v["check"] == check]


class GateTest(unittest.TestCase):
    def test_a_request_at_rest_fails_and_names_the_paths(self):
        res = gate(android_run(rest_count=2, by_path={"GET /api/v1/poll": 2}), scn_with())
        v = checks_of(res, "rest-requests")
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["message"], "2 requests sent at rest (GET /api/v1/poll x2), allowed 0")
        self.assertEqual((v[0]["observed"], v[0]["allowed"], v[0]["paths"]), (2, 0, {"GET /api/v1/poll": 2}))

    def test_no_request_at_rest_passes(self):
        self.assertEqual(gate(android_run(), scn_with())["verdict"], "PASS")

    def test_the_scenario_limit_and_the_allowance(self):
        self.assertEqual(gate(android_run(rest_count=2), scn_with(gate={"rest_requests_max": 2}))["verdict"], "PASS")
        res = gate(android_run(rest_count=2), scn_with(rest={"allow": {"requests_max": 2, "reason": "a heartbeat"}}))
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("idle: 2 requests sent at rest, above the limit of 0 that would otherwise apply; passed on the "
                      "step's allowance of 2 (a heartbeat)", res["notes"])
        res = gate(android_run(rest_count=3), scn_with(rest={"allow": {"requests_max": 2, "reason": "a heartbeat"}}))
        v = checks_of(res, "rest-requests")[0]
        self.assertTrue(v["message"].endswith("allowed 2 by the step's allowance (a heartbeat)"))
        self.assertEqual(v["limit_from"], "allowance")

    def test_expect_requests_max_on_any_step(self):
        scn = scn_with(tap={"expect": {"requests_max": 1}})
        self.assertEqual(gate(android_run(open_count=1), scn)["verdict"], "PASS")
        v = checks_of(gate(android_run(open_count=2), scn), "requests")
        self.assertEqual(v[0]["message"], "2 requests (GET /api/v1/x x2), approved at most 1")
        rest_expect = scn_with(rest={"expect": {"requests_max": 0}})
        self.assertEqual([x["check"] for x in gate(android_run(rest_count=1), rest_expect)["violations"]],
                         ["rest-requests", "requests"])

    def test_unknown_fails_every_rest_and_every_budget(self):
        res = gate(android_run(known=False, reason="the clock was not read"), scn_with(tap={"expect": {"requests_max": 5}}))
        self.assertEqual([(v["check"], v["step"], v["message"]) for v in res["violations"]],
                         [("rest-requests", "idle", "requests at rest were not counted: the clock was not read"),
                          ("requests", "open", "requests were not counted: the clock was not read")])

    def test_no_request_log_means_no_check_but_a_budget_fails(self):
        run = android_run(with_log=False)
        self.assertEqual(gate(run, scn_with()), {"verdict": "PASS", "violations": [], "notes": []})
        res = gate(run, scn_with(tap={"expect": {"requests_max": 3}}))
        self.assertEqual([(v["check"], v["message"]) for v in res["violations"]],
                         [("requests", "not counted: the run was given no request log")])

    def test_a_tap_on_the_simulator_over_its_budget(self):
        scn = {"name": "Sim tap", "package": s040.BUNDLE,
               "steps": [{"id": "tap", "do": "tap", "x": 0.5, "y": 0.5, "expect": {"requests_max": 0}}]}
        u.validate_scenario(scn, u.SIM_PLATFORM)
        run = {"platform": u.SIM_PLATFORM, "launch": {"pid": 1, "error": None}, "host": {"load1_before": 1.0},
               "requests": {"known": True, "reason": None, "requests": 1, "upgrades": 0, "clock": {}},
               "steps": [{"id": "launch", "kind": "launch", "pid": 1, "requests": {"count": 0, "by_path": {}}},
                         {"id": "tap", "kind": "tap", "pid": 1, "requests": {"count": 1, "by_path": {"POST /x": 1}}}]}
        res = u.gate_sim_run(run, scn, dict(u.GATE_DEFAULTS))
        self.assertIn(("requests", "tap", "1 request (POST /x x1), approved at most 0"),
                      [(v["check"], v["step"], v["message"]) for v in res["violations"]])

    def test_simulator_rules(self):
        run = s040.sim_run(cpu_pct=0.5)
        scn = s040.sim_scn()
        rest = next(s for s in scn["steps"] if s["do"] == "rest")
        base = u.gate_sim_run(copy.deepcopy(run), scn, dict(u.GATE_DEFAULTS))
        # no log: as before
        with_log = copy.deepcopy(run)
        with_log["requests"] = {"known": True, "reason": None, "requests": 2, "upgrades": 0, "clock": {}}
        for st in with_log["steps"]:
            st["requests"] = {"count": 2 if st["id"] == rest["id"] else 0,
                              "by_path": {"GET /tick": 2} if st["id"] == rest["id"] else {}}
        res = u.gate_sim_run(with_log, scn, dict(u.GATE_DEFAULTS))
        added = [v for v in res["violations"] if v not in base["violations"]]
        self.assertEqual([(v["check"], v["step"], v["message"]) for v in added],
                         [("rest-requests", rest["id"], "2 requests sent at rest (GET /tick x2), allowed 0")])
        scn2 = copy.deepcopy(scn)
        next(s for s in scn2["steps"] if s["do"] == "rest")["expect"] = {"requests_max": 1}
        u.validate_scenario(scn2, u.SIM_PLATFORM)
        res = u.gate_sim_run(copy.deepcopy(run), scn2, dict(u.GATE_DEFAULTS))
        self.assertIn(("requests", rest["id"], u.NO_REQUEST_LOG),
                      [(v["check"], v["step"], v["message"]) for v in res["violations"]])

    def test_the_new_limit_is_in_the_gate_json_and_documented_limits(self):
        self.assertEqual(u.GATE_DEFAULTS["rest_requests_max"], 0)
        self.assertIn("requests_max", u.ALLOW_LIMITS)
        self.assertIn("requests_max", u.EXPECT_KEYS)


class ValidationTest(unittest.TestCase):
    def test_accepted_and_refused(self):
        scn_with(rest={"expect": {"requests_max": 0}, "allow": {"requests_max": 3, "reason": "polling"}},
                 tap={"expect": {"requests_max": 2}}, gate={"rest_requests_max": 1})
        for bad in ({"rest": {"expect": {"requests_max": -1}}}, {"tap": {"expect": {"requests_max": 1.5}}},
                    {"rest": {"allow": {"requests_max": True, "reason": "x"}}}, {"gate": {"rest_requests_max": "1"}}):
            with self.assertRaises(u.ProbeError, msg=bad):
                scn_with(**bad)
        sim = s040.sim_scn()
        for st in sim["steps"]:
            st.setdefault("expect", {})["requests_max"] = 1
        u.validate_scenario(sim, u.SIM_PLATFORM)


# --------------------------------------------------------------------------------------------
# a whole Android run with requests, adb faked
# --------------------------------------------------------------------------------------------

_REAL_SLEEP = time.sleep          # test_run_and_cli's fakes replace time.sleep; the fake script needs a real wait


class AndroidRunTest(unittest.TestCase):
    """The host side of `run --requests` with adb replaced by fakes (test_run_and_cli's): /proc/uptime is
    read before and after the device script; the fake script waits about a second, writes marks that lie
    in that second (the scenario's marks, compressed) and appends to the front's log while it runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "run-01"
        self.log = self.root / "front.jsonl"
        self.log.write_text(listening(1.0) + request_line(2.0, "/earlier"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_counts_per_step(self):
        files = ta.healthy_files()
        files["done.txt"] = "done\n"
        test = self
        base = time.time() - 50.0                       # host = uptime + base
        span = 0.9                                      # host seconds the scenario's 100..118 s are compressed into

        class Device(trc.FakeDevice):
            def sh(self, serial, command, **kw):
                self.commands.append(command)
                if command == "cat /proc/uptime":
                    return f"{time.time() - base:.2f} 1.00\n"
                if command.endswith("/run.sh"):
                    up0 = time.time() - base
                    _REAL_SLEEP(span + 0.1)
                    at = lambda mark: up0 + 0.05 + (mark - 100.0) / 18.0 * span
                    files["marks.txt"] = "".join(f"{k} {at(v):.3f}\n" for k, v in ta.MARKS.items())
                    with open(test.log, "a") as fh:
                        fh.write(request_line(base + at(103.0), "/api/v1/start") + request_line(base + at(111.0), "/api/v1/poll")
                                 + request_line(base + at(113.5), "/api/v1/open") + line(base + at(113.6), "closed", conn=1))
                    return ""
                if command.startswith("ls "):
                    return " ".join(files)
                return ""

            def adb(self, serial, *args, **kw):
                self.commands.append(" ".join(args))
                if args[0] == "pull":
                    Path(args[2]).write_text(files[Path(args[1]).name])
                return ""

        device = Device()
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            run = u.run_once("<serial>", ta.SCN, self.out, False, False, requests=u.check_request_log(str(self.log)))
        st = {s["id"]: s for s in run["steps"]}
        self.assertTrue(run["requests"]["known"], run["requests"])
        self.assertEqual(st["launch"]["requests"], {"count": 1, "by_path": {"GET /api/v1/start": 1}})
        self.assertEqual(st["idle"]["requests"]["by_path"], {"GET /api/v1/poll": 1})
        self.assertEqual(st["open"]["requests"]["count"], 1)
        self.assertEqual(st["idle2"]["requests"]["count"], 0)
        self.assertLess(run["requests"]["clock"]["uncertainty_s"], 0.1)
        self.assertEqual(device.commands.count("cat /proc/uptime"), 6)
        script_at = device.commands.index(f"sh {u.DEV_DIR}/run.sh")
        self.assertEqual(device.commands[script_at - 3:script_at], ["cat /proc/uptime"] * 3)
        self.assertEqual(device.commands[script_at + 1:script_at + 4], ["cat /proc/uptime"] * 3)
        meta = json.loads((self.out / "raw" / u.REQUESTS_META).read_text())
        self.assertEqual(meta["source"], str(self.log.resolve()))
        self.assertEqual(meta["front"], {"pid_at_start": os.getpid(), "alive_at_start": True, "pid_at_end": os.getpid(),
                                         "alive_at_end": True, "port": LISTEN_PORT, "accepting_at_end": True})
        cap = json.loads((self.out / "capture.json").read_text())
        self.assertEqual(cap["requests_log"], str(self.log.resolve()))
        res = u.gate([str(self.out)], {})["runs"][0]
        self.assertEqual([(v["check"], v["step"]) for v in res["violations"]], [("rest-requests", "idle")])
        self.assertIn("GET /api/v1/poll x1", res["violations"][0]["message"])
        row = u.run_row(str(self.out), run)
        self.assertEqual((row["idle.requests"], row["open.requests"], row["launch.requests"]), (1, 1, 1))
        summary = u.summary([str(self.root)])
        self.assertEqual(summary["stats"]["idle.requests"]["median"], 1)


class SimRunRequestsTest(s040.SimBase):
    """`run --simulator ... --requests` through the stand-ins of 0.4.0: a thread appends a request to the log
    every 20 ms while the run goes on, stamped with this machine's clock, as tls-front would."""

    def test_counts_reach_the_run(self):
        import threading
        log = self.root / "front.jsonl"
        log.write_text(listening(time.time()))
        (self.state / "cpu.txt").write_text("0:01.00\n" * 5)
        stop = threading.Event()

        def traffic():
            while not stop.is_set():
                with open(log, "a") as fh:
                    fh.write(request_line(time.time(), "/api/v1/poll"))
                stop.wait(0.02)
        writer = threading.Thread(target=traffic)
        writer.start()
        try:
            rc, out, err = self.run_cli("--requests", str(log))
        finally:
            stop.set()
            writer.join()
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        res = json.loads((run / "run.json").read_text())
        self.assertTrue(res["requests"]["known"], res["requests"])
        counts = [s["requests"]["count"] for s in res["steps"]]
        self.assertTrue(all(isinstance(c, int) for c in counts), counts)
        self.assertGreater(sum(counts[1:]), 0)               # the rests took 100 ms each and the writer kept writing
        block = res["requests"]
        self.assertEqual(sum(counts) + block["between_steps"], block["requests"] - block["in_margins"])
        self.assertEqual(json.loads((run / "capture.json").read_text())["requests_log"], str(log.resolve()))
        self.assertEqual(json.loads((run / "raw" / u.REQUESTS_META).read_text())["clock"]["platform"], u.SIM_PLATFORM)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [rest-requests] rest-a:", text)


class RefusalTest(unittest.TestCase):
    def test_refused_before_a_run_folder_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scn = root / "scenario.json"
            scn.write_text(json.dumps(trc.SCN))
            empty = root / "empty.jsonl"
            empty.write_text(line(1.0, "closed", conn=1))
            gone = root / "gone.jsonl"
            proc = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
            proc.wait()
            gone.write_text(line(1.0, "listening", pid=proc.pid))
            for path, fragment in ((root / "absent.jsonl", "no such file"),
                                   (empty, "holds no 'listening' line, so it is not the log of a front"),
                                   (root, "no such file"),
                                   (gone, f"the front that wrote {gone} is not running (pid {proc.pid}): start it "
                                          f"again with --log {gone}")):
                for target in (["--serial", "emulator"], ["--simulator", "ABCD"]):
                    out = root / "runs"
                    with mock.patch.object(u, "sim_app_identity", lambda *a: {}):
                        rc, _, err = trc.main("run", *target, "--scenario", str(scn), "--out", str(out),
                                              "--requests", str(path))
                    self.assertEqual(rc, 2, (path, target, err))
                    self.assertIn(fragment, err)
                    self.assertFalse(out.exists())


# --------------------------------------------------------------------------------------------
# runs of earlier versions, and the summary of one with counts
# --------------------------------------------------------------------------------------------

class EarlierRunsTest(ta.Base):
    def test_a_run_without_requests_gates_as_before(self):
        cap = ta.Capture(self.root)
        cap.analyze()
        res = cap.gate()
        self.assertEqual(res["verdict"], "PASS")
        self.assertFalse(any("request" in n for n in res["notes"]))
        out = u.gate([str(cap.dir)], {})
        self.assertEqual(out["runs"][0]["limits"], {**u.GATE_DEFAULTS})
        row = u.run_row(str(cap.dir), json.loads((cap.dir / "run.json").read_text()))
        self.assertFalse(any(k.endswith(".requests") for k in row))


if __name__ == "__main__":
    unittest.main()


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## Counting requests: `ui-probe http-front` and `run --requests`",
                       "listening on http://127.0.0.1:<listen> -> http://127.0.0.1:<backend>",
                       "It offers only `http/1.1` in ALPN, and that is what makes the requests readable",
                       "No header value, no query string and no body is ever written",
                       "A streaming response (server-sent events, a long poll) is one request",
                       "HTTP/1.x only", "nothing is added to the measured path", "A rest runs on to the next step",
                       "is necessary, not sufficient", "any other segment is written as `:id`",
                       "The front makes the file 0600 at every write, whether it existed or not",
                       "cannot have its requests counted by a front", "`FtraceStats`",
                       "writes a `stopping` line, stops accepting, closes every connection it holds",
                       "is being written by another front (pid N); stop it or give this one its own log",
                       "must be accepted within 2 s", "above 0.2 s", "0.43 to 0.46 s",
                       "cannot have its requests counted", "Every request is counted in exactly one step",
                       "so a user name such as `alice` stays", "`java.net.http`",
                       "a request such a read caused in the app would be held against that rest",
                       "values: 5 base, 0 head",
                       "not counted: the run was given no request log",
                       "`raw/requests.jsonl`", "requests-meta.json", "`<step>.requests`",
                       "`<step>.sim_requests`", "## What each Android version gives",
                       "the app's traffic is probably not going through the front"):
            self.assertIn(phrase, readme, phrase)
        skill = " ".join((root / "skills" / "ui-probe" / "SKILL.md").read_text().split())
        for phrase in ("## Counting requests", "ui-probe http-front", "--requests <file>", "rest_requests_max"):
            self.assertIn(phrase, skill, phrase)
        self.assertIn("--requests", (root / "SHIM-TEMPLATE.md").read_text())
        self.assertIn("## 0.7.0", (root / "CHANGELOG.md").read_text())
        self.assertNotIn("clock.txt", readme)
        self.assertIn("`limits` now holds `rest_requests_max`", " ".join((root / "CHANGELOG.md").read_text().split()))
        self.assertIn("### Validation, 0.7.0", (root / "PROVENANCE.md").read_text())
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], u.VERSION)
        self.assertEqual(u.VERSION, "0.7.0")
        market = json.loads((root.parent.parent / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["metadata"]["version"], "0.12.0")
        self.assertIn("## 0.12.0", (root.parent.parent / "CHANGELOG.md").read_text())

    def test_installed_and_reachable(self):
        installer = (Path(u.HERE) / "install.sh").read_text()
        files = next(line for line in installer.splitlines() if line.startswith("FILES="))
        for name in ("httppipe.py", "httpfront.py", "tlsfront.py"):
            self.assertIn(name, files)
        proc = subprocess.run([sys.executable, "-B", str(Path(u.__file__)), "--help"], capture_output=True, text=True)
        self.assertIn("http-front", proc.stdout)
        proc = subprocess.run([sys.executable, "-B", str(Path(u.__file__)), "http-front", "--help"], capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage: ui-probe http-front", proc.stdout)


class InstalledLauncherTest(unittest.TestCase):
    """The installer copies the new modules, and the launcher reaches `http-front` from the copy."""

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_the_launcher_runs_http_front(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "HOME": str(root), "XDG_DATA_HOME": str(root / "data"), "PREFIX": str(root / "bin"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run(["sh", str(Path(u.HERE) / "install.sh"), "--apply"], capture_output=True, text=True,
                                  env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = root / "data" / "ui-probe" / u.VERSION
            for name in ("httppipe.py", "httpfront.py"):
                self.assertTrue((data / name).is_file(), name)
            out = subprocess.run([str(root / "bin" / "ui-probe"), "http-front", "--help"], capture_output=True, text=True,
                                 env=env, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("usage: ui-probe http-front", out.stdout)
            self.assertEqual(list(data.rglob("__pycache__")), [])
