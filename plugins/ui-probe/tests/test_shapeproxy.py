"""shapeproxy.py against a local echo server on 127.0.0.1. No other network is used."""
import sys

sys.dont_write_bytecode = True

import asyncio
import io
import json
import socket
import struct
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import shapeproxy as sp  # noqa: E402

READ_CHUNK = 16384     # the proxy's read size: one delay-line chunk is at most this long
EARLY = 0.005          # tolerance for a timer that fires a hair early


async def echo(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass            # the proxy closed the upstream socket abruptly (see the half-close test)
    finally:
        writer.close()


def payload(n):
    return (bytes(range(256)) * (n // 256 + 1))[:n]


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.echo = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.echo_port = self.echo.sockets[0].getsockname()[1]
        self.log = io.StringIO()
        self.proxies = []

    async def asyncTearDown(self):
        for server in self.proxies + [self.echo]:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def proxy(self, *argv):
        args = sp.parse_args(["--port", "0", *argv])
        server = await sp.start(args, self.log)
        self.proxies.append(server)
        return server.sockets[0].getsockname()[1]

    async def connect(self, proxy_port, target_port=None):
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(f"CONNECT 127.0.0.1:{target_port or self.echo_port} HTTP/1.1\r\nHost: example\r\n\r\n".encode())
        await w.drain()
        return r, w

    async def roundtrip(self, r, w, data, half_close=False):
        t0 = time.monotonic()
        w.write(data)
        await w.drain()
        if half_close:
            w.write_eof()
        got = b""
        while len(got) < len(data):
            chunk = await asyncio.wait_for(r.read(65536), 2.5)
            if not chunk:
                break
            got += chunk
        return got, time.monotonic() - t0

    async def log_lines(self, n=1):
        for _ in range(60):
            lines = [l for l in self.log.getvalue().splitlines() if l]
            if len(lines) >= n:
                return [json.loads(l) for l in lines]
            await asyncio.sleep(0.025)
        self.fail(f"expected {n} log line(s), got: {self.log.getvalue()!r}")

    async def close(self, w):
        w.close()
        try:
            await asyncio.wait_for(w.wait_closed(), 2)
        except (asyncio.TimeoutError, ConnectionError):
            pass

    async def test_connect_echo_delay_and_log(self):
        delay = 0.1
        port = await self.proxy("--delay-ms", str(delay * 1000))
        r, w = await self.connect(port)
        reply = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"), reply)
        data = payload(5000)
        got, elapsed = await self.roundtrip(r, w, data)
        self.assertEqual(got, data)
        self.assertGreaterEqual(elapsed, 2 * delay - EARLY)     # one delay each way
        self.assertLess(elapsed, 2 * delay + 0.5)
        await self.close(w)
        lines = await self.log_lines()
        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual(entry["target"], f"127.0.0.1:{self.echo_port}")
        self.assertEqual(entry["bytes_up"], len(data))
        self.assertEqual(entry["bytes_down"], len(data))
        self.assertGreater(entry["duration_s"], 0)
        self.assertIn("start_epoch", entry)

    async def test_rate_limit_slows_a_large_payload(self):
        kbit = 800
        bytes_per_s = kbit * 1000 / 8
        port = await self.proxy("--kbit", str(kbit))
        r, w = await self.connect(port)
        await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        data = payload(61440)
        got, elapsed = await self.roundtrip(r, w, data)
        self.assertEqual(got, data)
        # The limiter charges each chunk after writing it, so the last chunk (at most READ_CHUNK
        # bytes) is not waited for: the transfer takes at least (size - one chunk) / rate.
        self.assertGreaterEqual(elapsed, (len(data) - READ_CHUNK) / bytes_per_s - EARLY)
        await self.close(w)
        entry = (await self.log_lines())[0]
        self.assertEqual((entry["bytes_up"], entry["bytes_down"]), (len(data), len(data)))

    async def test_rate_limit_applies_to_a_single_chunk(self):
        """Fixed defect: a payload that fits one read (here 8000 bytes at 160 kbit/s, 0.4 s per direction)
        passes at full speed: the limiter sleeps after writing, so a short response is never shaped."""
        kbit = 160
        port = await self.proxy("--kbit", str(kbit))
        r, w = await self.connect(port)
        await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        data = payload(8000)
        got, elapsed = await self.roundtrip(r, w, data)
        await self.close(w)
        self.assertEqual(got, data)
        self.assertGreaterEqual(elapsed, len(data) * 8 / (kbit * 1000) - EARLY)

    async def test_half_close_keeps_the_response(self):
        """Fixed defect: when the client shuts down its sending side, the proxy closes the whole upstream
        connection, and the response still on its way back is lost (bytes_down 0)."""
        port = await self.proxy("--delay-ms", "50")
        r, w = await self.connect(port)
        await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        data = payload(2000)
        got, _ = await self.roundtrip(r, w, data, half_close=True)
        await self.close(w)
        self.assertEqual(got, data)

    async def test_refused_target_is_logged(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        port = await self.proxy()
        r, w = await self.connect(port, closed_port)
        # a refused target is answered, not dropped: 502 and the connection closed
        self.assertTrue((await asyncio.wait_for(r.read(), 2)).startswith(b"HTTP/1.1 502 Bad Gateway\r\n"))
        await self.close(w)
        entry = (await self.log_lines())[0]
        self.assertEqual(entry["target"], f"127.0.0.1:{closed_port} (ConnectionRefusedError)")
        self.assertEqual((entry["bytes_up"], entry["bytes_down"]), (0, 0))


class ProxyFailureTest(unittest.IsolatedAsyncioTestCase):
    """Findings 20, 21 and 22: failures tear a connection down, a slow reader slows the sender, and
    plain HTTP carries one request per connection."""

    async def asyncSetUp(self):
        self.log = io.StringIO()
        self.servers = []
        self.server_done = asyncio.Event()
        self.sent = [0]

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def proxy(self, *argv):
        server = await sp.start(sp.parse_args(["--port", "0", *argv]), self.log)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def tunnel(self, proxy_port, target_port):
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: example\r\n\r\n".encode())
        await w.drain()
        await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 2)
        return r, w

    async def firehose(self, reader, writer, limit=None):
        """Send until the connection fails (or limit bytes); count what the network accepted."""
        chunk = b"x" * 65536
        try:
            while limit is None or self.sent[0] < limit:
                writer.write(chunk)
                await writer.drain()
                self.sent[0] += len(chunk)
        except (ConnectionError, OSError):
            pass
        finally:
            self.server_done.set()
            writer.close()

    async def log_lines(self, timeout=3.0):
        for _ in range(int(timeout / 0.025)):
            lines = [l for l in self.log.getvalue().splitlines() if l]
            if lines:
                return [json.loads(l) for l in lines]
            await asyncio.sleep(0.025)
        return []

    async def test_a_client_that_goes_away_ends_the_connection(self):
        """Finding 20: when the client is gone, the reader of the other direction stops too, both
        sockets are released and the connection is logged."""
        target = await self.serve(self.firehose)
        port = await self.proxy()
        r, w = await self.tunnel(port, target)
        await asyncio.wait_for(r.readexactly(100000), 2)
        w.transport.abort()
        lines = await self.log_lines()
        self.assertEqual(len(lines), 1, "the connection never finished")
        self.assertIsNotNone(lines[0]["error"])
        await asyncio.wait_for(self.server_done.wait(), 2)       # the upstream socket was closed as well

    async def test_an_upstream_reset_is_not_passed_on_as_a_clean_end(self):
        """Finding 21: a reset from the server reaches the client as an error, not as end of stream."""
        async def reset_after_hello(reader, writer):
            writer.write(b"hello")
            await writer.drain()
            await asyncio.sleep(0.1)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.transport.abort()

        target = await self.serve(reset_after_hello)
        port = await self.proxy()
        r, w = await self.tunnel(port, target)
        got = b""
        with self.assertRaises(ConnectionError):
            while True:
                chunk = await asyncio.wait_for(r.read(1024), 2)
                if not chunk:
                    break       # a clean end of stream: the defect
                got += chunk
        w.close()
        entry = (await self.log_lines())[0]
        self.assertIn("ConnectionResetError", entry["error"])

    async def test_a_slow_reader_slows_the_sender(self):
        """Finding 22: the proxy holds a bounded amount; it does not read the whole transfer."""
        limit = 48 * 1024 * 1024
        target = await self.serve(lambda r, w: self.firehose(r, w, limit))
        port = await self.proxy()
        r, w = await self.tunnel(port, target)
        await asyncio.sleep(1.5)                      # the client reads nothing
        self.assertLess(self.sent[0], 16 * 1024 * 1024, "the proxy read on without a reader")
        w.transport.abort()
        await self.log_lines()

    async def test_plain_http_is_one_request_per_connection(self):
        """Finding 22: the request is forwarded with Connection: close and the connection ends with
        the response."""
        heads = []

        async def http_server(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            heads.append(head)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            if b"connection: close" in head.lower():
                writer.close()
            else:
                await asyncio.sleep(5)        # keep-alive: the connection would stay open
                writer.close()

        target = await self.serve(http_server)
        port = await self.proxy()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET http://127.0.0.1:{target}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Connection: keep-alive\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        await w.drain()
        reply = await asyncio.wait_for(r.read(), 2)          # read to the end: the connection must close
        self.assertTrue(reply.endswith(b"\r\n\r\nok"), reply)
        self.assertIn(b"\r\nConnection: close\r\n", heads[0])
        self.assertNotIn(b"keep-alive", heads[0].lower())
        w.close()
        entry = (await self.log_lines())[0]
        self.assertIsNone(entry["error"])

    def test_close_after_one(self):
        head = b"GET http://h/ HTTP/1.1\r\nHost: h\r\nConnection: keep-alive\r\nKeep-Alive: 5\r\nAccept: */*\r\n\r\n"
        self.assertEqual(sp._close_after_one(head),
                         b"GET http://h/ HTTP/1.1\r\nHost: h\r\nAccept: */*\r\nConnection: close\r\n\r\n")


class ProcessTest(unittest.TestCase):
    """The proxy as a process: it says when it is listening, and a second one on the same port
    fails instead of leaving the caller talking to the first (found on a device run, where an older
    proxy still held the port and its log stayed empty)."""

    def start(self, port, log):
        import subprocess
        cli = str(Path(__file__).resolve().parent.parent / "scripts" / "ui_probe.py")
        return subprocess.Popen([sys.executable, "-B", cli, "proxy", "--port", str(port), "--log", log],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_ready_line_then_a_second_start_on_the_port_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            first = self.start(0, tmp + "/a.jsonl")
            try:
                line = first.stdout.readline().strip()
                self.assertRegex(line, r"^listening on 127\.0\.0\.1:\d+$")
                port = int(line.rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", port), timeout=5):
                    pass
                second = self.start(port, tmp + "/b.jsonl")
                out, err = second.communicate(timeout=20)
                self.assertEqual(second.returncode, 2)
                self.assertEqual(out, "")
                self.assertIn("ui-probe: proxy:", err)
                self.assertIn("in use", err)
            finally:
                first.terminate()
                first.communicate(timeout=20)


    def test_a_connection_open_when_the_proxy_stops_is_logged(self):
        """Seen on a device: an app kept one tunnel open for a whole run, the proxy was stopped, and
        the log held no line for it. A stopped proxy logs what it was still carrying."""
        import signal
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, socket.create_server(("127.0.0.1", 0)) as upstream:
            log = tmp + "/tunnels.jsonl"
            proxy = self.start(0, log)
            try:
                port = int(proxy.stdout.readline().strip().rsplit(":", 1)[1])
                target = upstream.getsockname()[1]
                with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                    client.sendall(f"CONNECT 127.0.0.1:{target} HTTP/1.1\r\n\r\n".encode())
                    self.assertIn(b"200", client.recv(200))
                    upstream.settimeout(5)
                    conn, _ = upstream.accept()
                    with conn:
                        client.sendall(b"ping")
                        conn.settimeout(5)
                        self.assertEqual(conn.recv(4), b"ping")
                        self.assertEqual(Path(log).read_text(), "")      # still open: no line yet
                        proxy.send_signal(signal.SIGTERM)
                        _, err = proxy.communicate(timeout=20)
                self.assertEqual(proxy.returncode, 0)
                self.assertEqual(err, "")                                # a quiet stop: no traceback
                lines = [json.loads(line) for line in Path(log).read_text().splitlines()]
                self.assertEqual(len(lines), 1)
                self.assertEqual((lines[0]["target"], lines[0]["error"], lines[0]["bytes_up"]),
                                 (f"127.0.0.1:{target}", "open when the proxy stopped", 4))
            finally:
                if proxy.poll() is None:
                    proxy.kill()
                    proxy.communicate(timeout=20)


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        a = sp.parse_args([])
        self.assertEqual((a.port, a.bind, a.delay_ms, a.kbit, a.log), (8899, "127.0.0.1", 0, 0, "tunnels.jsonl"))

    def test_values(self):
        a = sp.parse_args(["--port", "0", "--delay-ms", "150", "--kbit", "400", "--log", "x.jsonl"])
        self.assertEqual((a.port, a.delay_ms, a.kbit, a.log), (0, 150.0, 400.0, "x.jsonl"))


if __name__ == "__main__":
    unittest.main()
