"""0.5.0, ruling 12: `ui-probe tls-front`, TLS in front of a plain local backend. Uses the real
`openssl` on PATH (skipped with a reason where there is none) and servers on 127.0.0.1 only."""
import sys

sys.dont_write_bytecode = True

import asyncio
import contextlib
import datetime
import io
import json
import os
import shutil
import signal
import ssl
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tlsfront as tf  # noqa: E402
import ui_probe as u  # noqa: E402

OPENSSL = shutil.which("openssl")
NEEDS = unittest.skipUnless(OPENSSL, "no openssl on PATH: tls-front makes its certificate authority with it")


def text_of(path):
    return subprocess.run([OPENSSL, "x509", "-text", "-noout", "-in", str(path)], capture_output=True, text=True,
                          check=True).stdout


def dates(path):
    out = subprocess.run([OPENSSL, "x509", "-dates", "-noout", "-in", str(path)], capture_output=True, text=True,
                         check=True).stdout
    vals = dict(line.split("=", 1) for line in out.strip().splitlines())
    parse = lambda s: datetime.datetime.strptime(" ".join(s.split()), "%b %d %H:%M:%S %Y GMT")
    return parse(vals["notBefore"]), parse(vals["notAfter"])


@NEEDS
class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_certificates(self):
        state, made = tf.ensure_state(self.state)
        self.assertIn("made a new throwaway certificate authority", made)
        ca, leaf = text_of(state / "ca.pem"), text_of(state / "leaf.pem")
        for needed in ("CA:TRUE", "Certificate Sign", "Public-Key: (2048 bit)", "sha256WithRSAEncryption"):
            self.assertIn(needed, ca, needed)
        self.assertRegex(ca, r"Basic Constraints: critical\s+CA:TRUE, pathlen:0")
        self.assertRegex(ca, r"Name Constraints: critical\s+Permitted:\s+DNS:localhost")
        self.assertFalse((state / "ca.key").exists())                 # deleted once the leaf was signed
        self.assertTrue((state / ".ui-probe-tls-front").exists())
        for needed in ("DNS:localhost", "TLS Web Server Authentication", "CA:FALSE", "Public-Key: (2048 bit)",
                       "sha256WithRSAEncryption", "Subject: CN=localhost"):
            self.assertIn(needed, leaf, needed)
        for name in ("ca.pem", "leaf.pem"):
            lo, hi = dates(state / name)
            self.assertEqual((hi - lo).days, 365, name)

    def test_modes(self):
        state, _ = tf.ensure_state(self.state)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((state / "leaf.key").stat().st_mode), 0o600)

    def test_reused_while_valid_and_remade_when_it_expires(self):
        tf.ensure_state(self.state)
        before = {f: (self.state / f).read_bytes() for f in tf.FILES}
        _, made = tf.ensure_state(self.state)
        self.assertIsNone(made)
        self.assertEqual({f: (self.state / f).read_bytes() for f in tf.FILES}, before)
        # a certificate that expires in a day: both are made again, and the output says so
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.pem",
                        "-days", "1", "-subj", "/CN=localhost"], cwd=self.state, capture_output=True, check=True)
        os.chmod(self.state / "leaf.key", 0o600)
        _, made = tf.ensure_state(self.state)
        self.assertIn("the old one had expired or would within 7 days", made)
        self.assertIn("a simulator needs the new CA", made)
        self.assertNotEqual((self.state / "ca.pem").read_bytes(), before["ca.pem"])
        self.assertEqual((dates(self.state / "leaf.pem")[1] - dates(self.state / "leaf.pem")[0]).days, 365)

    def test_a_folder_or_key_others_can_read_is_refused(self):
        tf.ensure_state(self.state)
        os.chmod(self.state, 0o755)
        with self.assertRaises(tf.FrontError) as cm:
            tf.ensure_state(self.state)
        self.assertIn("holds keys and others can open it (mode 755); refused, not changed", str(cm.exception))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)
        os.chmod(self.state, 0o700)
        os.chmod(self.state / "leaf.key", 0o644)
        with self.assertRaises(tf.FrontError) as cm:
            tf.ensure_state(self.state)
        self.assertIn("leaf.key can be read by others (mode 644); refused", str(cm.exception))
        # a folder it did not make: refused if others can open it or if it is not empty, and left as it was
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        os.chmod(other, 0o755)
        with self.assertRaises(tf.FrontError) as cm:
            tf.ensure_state(other)
        self.assertIn("was not made by tls-front and others can open it (mode 755); refused, not changed", str(cm.exception))
        self.assertEqual((stat.S_IMODE(other.stat().st_mode), list(other.iterdir())), (0o755, []))
        os.chmod(other, 0o700)
        (other / "notes.txt").write_text("mine")
        with self.assertRaises(tf.FrontError) as cm:
            tf.ensure_state(other)
        self.assertIn("is not empty and was not made by tls-front", str(cm.exception))
        self.assertEqual([p.name for p in other.iterdir()], ["notes.txt"])
        # an empty 0700 folder is adopted as it is
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir(mode=0o700)
        tf.ensure_state(empty)
        self.assertTrue((empty / ".ui-probe-tls-front").exists())

    def test_the_ca_option(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = u.main(["tls-front", "--state", str(self.state), "--ca"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), str(self.state / "ca.pem"))
        self.assertTrue((self.state / "ca.pem").exists())

    def test_refused_arguments(self):
        for argv in (["--port", "0", "--to-port", "8080"], ["--port", "70000", "--to-port", "8080"],
                     ["--port", "8080", "--to-port", "8080"], ["--port", "x", "--to-port", "1"], ["--port", "8443"]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                tf.parse_args(["--state", str(self.state), *argv])
            self.assertEqual(cm.exception.code, 2, argv)


@NEEDS
class FrontTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state, _ = tf.ensure_state(Path(self.tmp.name) / "state")
        self.log = Path(self.tmp.name) / "front.jsonl"

        async def backend(reader, writer):
            data = await reader.read(100)
            writer.write(b"backend got: " + data)
            await writer.drain()
            writer.close()
        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        self.backend_port = self.backend.sockets[0].getsockname()[1]
        self.front = tf.Front(0, self.backend_port, self.state, str(self.log))
        self.server = await self.front.start()
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for s in (self.server, self.backend):
            s.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(s.wait_closed(), 2)
        self.tmp.cleanup()

    def client(self, trust=True):
        ctx = ssl.create_default_context(cafile=str(self.state / "ca.pem")) if trust else ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    async def lines(self, n=1):
        for _ in range(80):
            if self.log.exists() and len(self.log.read_text().splitlines()) >= n:
                return [json.loads(l) for l in self.log.read_text().splitlines()]
            await asyncio.sleep(0.025)
        self.fail("no log line")

    async def test_a_client_that_trusts_the_ca_gets_the_backend(self):
        r, w = await asyncio.open_connection("localhost", self.port, ssl=self.client(), server_hostname="localhost")
        self.assertEqual(w.get_extra_info("ssl_object").selected_alpn_protocol(), "http/1.1")
        self.assertIn(w.get_extra_info("ssl_object").version(), ("TLSv1.2", "TLSv1.3"))
        w.write(b"GET / HTTP/1.1\r\n\r\n")
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.read(), 5), b"backend got: GET / HTTP/1.1\r\n\r\n")
        w.close()
        entry = (await self.lines())[0]
        self.assertEqual((entry["event"], entry["bytes_up"], entry["bytes_down"]), ("closed", 18, 31))
        self.assertIn("t", entry)

    async def test_a_client_that_does_not_trust_it_is_refused(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            await asyncio.open_connection("localhost", self.port, ssl=self.client(trust=False), server_hostname="localhost")

    async def test_loopback_only_and_tls_12_or_newer(self):
        self.assertEqual(self.server.sockets[0].getsockname()[0], "127.0.0.1")
        self.assertEqual(tf.server_context(self.state).minimum_version, ssl.TLSVersion.TLSv1_2)

    async def test_a_refused_backend_is_logged(self):
        self.backend.close()
        await self.backend.wait_closed()
        r, w = await asyncio.open_connection("localhost", self.port, ssl=self.client(), server_hostname="localhost")
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            await asyncio.wait_for(r.read(), 5)
        w.close()
        entry = (await self.lines())[0]
        self.assertEqual((entry["event"], entry["to"]), ("backend-refused", f"127.0.0.1:{self.backend_port}"))


@NEEDS
class SignalTest(unittest.TestCase):
    def test_sigterm_ends_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with socket_port() as (p1, p2):
                proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "tls-front", "--port", str(p1),
                                         "--to-port", str(p2), "--state", str(state)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            line = proc.stdout.readline()
            self.assertEqual(line.strip(), f"listening on https://localhost:{p1} -> http://127.0.0.1:{p2}")
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(10), 0)
            proc.stdout.close()
            proc.stderr.close()


@contextlib.contextmanager
def socket_port():
    import socket
    socks = [socket.socket() for _ in range(2)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    yield ports


class DocsTest(unittest.TestCase):
    def test_documented(self):
        readme = " ".join((Path(u.HERE).parent / "README.md").read_text().split())
        for phrase in ("ui-probe tls-front", "never be added to a phone's or a Mac's own trust store",
                       "It shapes nothing", "xcrun simctl keychain <udid> add-root-cert"):
            self.assertIn(phrase, readme, phrase)


if __name__ == "__main__":
    unittest.main()
