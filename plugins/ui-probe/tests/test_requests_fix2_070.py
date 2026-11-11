"""0.7.0, fix round 2: the reviewer's second pass. Real front processes on 127.0.0.1 for the fronts' life
(started, signalled, a keep-alive connection held across the signal); no device, no simulator."""
import sys

sys.dont_write_bytecode = True

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import httppipe as hp  # noqa: E402
import test_analysis as ta  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import test_requests_fix1_070 as t71  # noqa: E402
import tlsfront as tf  # noqa: E402
import ui_probe as u  # noqa: E402

line, request_line, listening = t70.line, t70.request_line, t70.listening
OPENSSL = shutil.which("openssl")


class Backend:
    """A keep-alive backend in a thread of its own: 200 and a two-byte body for every request head."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        import threading
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(conn,), daemon=True).start()

    @staticmethod
    def one(conn):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\r\n\r\n" in buf:
                    _, buf = buf.split(b"\r\n\r\n", 1)
                    try:
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    except OSError:
                        return

    def close(self):
        self.sock.close()


class FrontProcess:
    """`ui-probe http-front` as a process of its own, started and waited for its listening line."""

    def __init__(self, log: Path, backend_port: int, port: int | None = None):
        if port is None:
            with t70.free_ports(1) as (port,):
                pass
        self.port = port
        self.proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "http-front", "--port", str(port),
                                      "--to-port", str(backend_port), "--log", str(log)], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     # the front's answer to SIGHUP depends on whether it is ignored when it
                                     # starts, and a suite started with nohup would hand that down: set it here
                                     preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL))
        self.first = self.proc.stdout.readline()

    def wait(self, timeout=10):
        try:
            return self.proc.wait(timeout)
        finally:
            self.close()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for f in (self.proc.stdout, self.proc.stderr):
            if not f.closed:
                f.close()


def get(port, path="/api/v1/poll", conn=None):
    """One request; on `conn` when given (kept open), else on a new connection closed after the answer."""
    c = conn or socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        c.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        return c.recv(100)
    finally:
        if conn is None:
            c.close()


def events(log: Path):
    return [json.loads(x) for x in log.read_text().splitlines()]


class Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = Backend()

    def tearDown(self):
        self.backend.close()
        self.tmp.cleanup()

    def wait_for(self, pred, what, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")


class RestartTest(Root):
    """G1 (the reviewer's scripts2/fix_fronts.py case N1): the front dies during the rest and a new one is
    started on the same log after the run's end."""

    def test_a_front_restarted_after_the_run_began(self):
        log = self.root / "n1.jsonl"
        front = FrontProcess(log, self.backend.port)
        port = front.port
        try:
            start = u.log_position(log)
            t0 = time.time()
            time.sleep(0.01)
            self.assertTrue(get(port, "/api/v1/config").startswith(b"HTTP/1.1 200"))
            rest_lo = time.time()
            front.proc.kill()                              # dies without a word
            front.wait()
            with self.assertRaises(OSError):
                get(port)
            rest_hi = time.time() + 0.05
            time.sleep(0.3)
            again = FrontProcess(log, self.backend.port, port)      # a supervisor starts it again, on the same log
            try:
                raw, steps, block = t71.sim_count(self.root, log, start, t0, rest_hi, (rest_lo, rest_hi))
                meta = json.loads((raw / u.REQUESTS_META).read_text())
                self.assertEqual((meta["front"]["pid_at_start"], meta["front"]["pid_at_end"]), (front.proc.pid, again.proc.pid))
                self.assertFalse(block["known"])
                self.assertEqual(block["reason"], "the front started listening again after the run began (it was "
                                                  "restarted): what the app sent while it was down was not piped and is "
                                                  "not counted")
            finally:
                again.proc.terminate()
                again.wait()
        finally:
            front.close()

    def test_a_front_whose_pid_changed(self):
        """A new front on the same log whose `listening` line the run did not read as appended (written into
        the log the run's offset already covered) still shows as a changed pid."""
        log = self.root / "pid.jsonl"
        log.write_text(listening(1.0))
        start = u.log_position(log)
        start["front_pid"] = 1                              # the front that ran when the run began
        now = time.time()
        with open(log, "a") as fh:
            fh.write(request_line(now - 3, "/a"))
        _, _, block = t71.sim_count(self.root, log, start, now - 5, now + 1, (now - 2, now))
        self.assertEqual(block["reason"], f"the front's pid changed during the run (1, then {os.getpid()}): what the "
                                          "app sent while it was down was not piped and is not counted")


class GoneButPortTakenTest(t70.Segment):
    """G2 with F2: a front that died while another program took its port still makes the counts unknown."""

    def test_a_dead_front_whose_port_answers(self):
        gone = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
        gone.wait()
        self.log.write_text(line(1.0, "listening", pid=gone.pid, port=t70.LISTEN_PORT, to_port=1))
        start = u.log_position(self.log)
        start["front_alive"] = True                      # it ran when the run began
        now = time.time()
        with open(self.log, "a") as fh:
            fh.write(request_line(now - 3, "/a"))
        raw, _, block = t71.sim_count(self.root, self.log, start, now - 5, now + 1, (now - 2, now))
        self.assertTrue(json.loads((raw / u.REQUESTS_META).read_text())["front"]["accepting_at_end"])
        self.assertEqual(block["reason"], f"the front (pid {gone.pid}) was no longer running at the end of the run: "
                                          "what the app sent after it stopped was not piped and is not counted")


class StopTest(Root):
    """G2 (the reviewer's scripts2/fix_fronts.py case N2): a front told to stop while the app holds a keep-alive
    connection writes `stopping`, closes the connection and exits within 2 s; the run is unknown."""

    def test_sigterm_with_a_keep_alive_connection_held(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig.name):
                log = self.root / f"n2-{sig.name}.jsonl"
                front = FrontProcess(log, self.backend.port)
                try:
                    start = u.log_position(log)
                    t0 = time.time()
                    held = socket.create_connection(("127.0.0.1", front.port), timeout=5)
                    self.assertTrue(get(front.port, "/api/v1/config", conn=held).startswith(b"HTTP/1.1 200"))
                    signalled = time.time()
                    front.proc.send_signal(sig)
                    self.assertEqual(front.proc.wait(5), 0)
                    # the connection is closed at once, not after the 2 s the front allows at most
                    self.assertLess(time.time() - signalled, 1.5)
                    held.settimeout(2)
                    self.assertEqual(held.recv(100), b"")              # the front closed it
                    held.close()
                    kinds = [(e["event"], e.get("signal"), e.get("conn")) for e in events(log)]
                    self.assertEqual(kinds[-2:], [("stopping", sig.name, None), ("closed", None, 1)])
                    rest_lo = time.time()
                    _, _, block = t71.sim_count(self.root, log, start, t0, rest_lo + 0.05, (rest_lo, rest_lo + 0.05))
                    self.assertEqual(block["reason"], "the front was told to stop during the run: what the app sent "
                                                      "after that was not piped and is not counted")
                finally:
                    front.close()

    def test_alive_but_not_accepting(self):
        """A front whose process lives but that no longer accepts (here in this process: its server closed)."""
        log = self.root / "na.jsonl"

        async def go():
            with t70.free_ports(1) as (fp,):
                pass
            front = hp.Front(fp, self.backend.port, str(log))
            server = await front.start()
            start = u.log_position(log)
            t0 = time.time()
            await asyncio.sleep(0.01)
            hp.append_line(str(log), {"event": "request", "conn": 1, "method": "GET", "path": "/api/v1/config",
                                      "query": False})
            await asyncio.sleep(0.01)
            server.close()
            await server.wait_closed()
            rest_lo = time.time()
            return fp, t71.sim_count(self.root, log, start, t0, rest_lo + 0.05, (rest_lo, rest_lo + 0.05))
        fp, (raw, _, block) = asyncio.run(go())
        self.assertEqual(block["reason"], f"the front (pid {os.getpid()}) was running but no longer accepted "
                                          f"connections on port {fp} at the end of the run: what the app sent was not "
                                          "piped and is not counted")
        self.assertFalse(json.loads((raw / u.REQUESTS_META).read_text())["front"]["accepting_at_end"])

    def test_the_probe_leaves_nothing_that_matters_on_http_front(self):
        log = self.root / "probe.jsonl"
        front = FrontProcess(log, self.backend.port)
        try:
            for _ in range(3):
                self.assertTrue(hp.port_accepts(front.port))
            self.wait_for(lambda: sum(e["event"] == "closed" for e in events(log)) == 3, "three closed lines")
            self.assertEqual([(e["event"], e.get("requests")) for e in events(log)[1:]], [("closed", 0)] * 3)
            # a run after the probes counts as if they had not been there
            start = u.log_position(log)
            t0 = time.time()
            time.sleep(0.01)
            get(front.port, "/api/v1/config")
            time.sleep(0.05)
            rest_lo = time.time()
            raw, steps, block = t71.sim_count(self.root, log, start, t0, rest_lo + 0.05, (rest_lo, rest_lo + 0.05))
            self.assertTrue(block["known"], block)
            self.assertEqual((steps[0]["requests"]["count"], block["requests"]), (1, 1))
            self.assertTrue(json.loads((raw / u.REQUESTS_META).read_text())["front"]["accepting_at_end"])
        finally:
            front.proc.terminate()
            front.wait()

    @unittest.skipUnless(OPENSSL, "no openssl on PATH")
    def test_the_probe_leaves_no_line_on_tls_front(self):
        log = self.root / "tls.jsonl"

        async def go():
            state, _ = tf.ensure_state(self.root / "state")
            front = tf.Front(0, self.backend.port, state, str(log))
            server = await front.start()
            port = server.sockets[0].getsockname()[1]
            before = log.read_text()
            ok = await asyncio.get_running_loop().run_in_executor(None, hp.port_accepts, port)
            await asyncio.sleep(0.2)
            after = log.read_text()
            server.close()
            await server.wait_closed()
            return ok, before, after
        ok, before, after = asyncio.run(go())
        self.assertTrue(ok)
        self.assertEqual(after, before)                       # a client that left before its handshake: nothing


class StopBoundTest(unittest.IsolatedAsyncioTestCase):
    """G2: whatever a connection does, the stop returns within STOP_WAIT_S and cancels what is left."""

    async def test_a_connection_that_does_not_end(self):
        front = hp.Front(0, 1, None)
        server = await front.start()

        class Writer:
            class transport:
                @staticmethod
                def abort():
                    pass                                   # a connection the abort does not end
        stuck = asyncio.ensure_future(asyncio.sleep(3600))
        front.active[stuck] = (Writer(), Writer())
        began = time.monotonic()
        with unittest.mock.patch.object(hp, "STOP_WAIT_S", 0.2):
            await asyncio.wait_for(front.stop(server, "SIGTERM"), 5)
        self.assertLess(time.monotonic() - began, 1.0)
        await asyncio.sleep(0)
        self.assertTrue(stuck.cancelled())


class OneLogOneFrontTest(Root):
    """G3: a front refuses a log that another running front writes."""

    def test_a_second_front_on_the_same_log(self):
        log = self.root / "one.jsonl"
        first = FrontProcess(log, self.backend.port)
        try:
            second = FrontProcess(log, self.backend.port)
            try:
                self.assertEqual(second.proc.wait(10), 2)
                err = second.proc.stderr.read()
                self.assertIn(f"ui-probe http-front: {log} is being written by another front (pid {first.proc.pid}); "
                              "stop it or give this one its own log", err)
                self.assertEqual(second.first, "")             # it never listened
            finally:
                second.close()
            self.assertEqual([e["event"] for e in events(log)], ["listening"])
        finally:
            first.proc.terminate()
            first.wait()
        # once the first one is gone, the log can be used again
        again = FrontProcess(log, self.backend.port)
        try:
            self.assertTrue(again.first.startswith("listening on http://127.0.0.1:"))
        finally:
            again.proc.terminate()
            again.wait()

    def test_a_front_that_starts_again_in_its_own_process(self):
        """A front whose own `listening` line is the log's last (it started again in the same process) is not
        refused: its own pid is no obstacle. The process's command line holds `http-front`, as a front's does."""
        log = self.root / "own.jsonl"
        code = ("import asyncio, sys; sys.path.insert(0, sys.argv[1]); import httppipe\n"
                "async def go():\n"
                "    for _ in range(2):\n"
                "        s = await httppipe.Front(0, 1, sys.argv[2]).start(); s.close(); await s.wait_closed()\n"
                "asyncio.run(go()); print('started twice')\n")
        out = subprocess.run([sys.executable, "-B", "-c", code, str(Path(u.HERE)), str(log), "http-front"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "started twice"), out.stderr)
        self.assertEqual([e["event"] for e in events(log)], ["listening", "listening"])

    def test_a_pid_that_is_not_a_front_is_no_obstacle(self):
        log = self.root / "other.jsonl"
        sleeper = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
        try:
            log.write_text(line(1.0, "listening", pid=sleeper.pid, port=1, to_port=2))
            self.assertFalse(hp.running_front(sleeper.pid))
            hp.check_log_owner(str(log))                       # no FrontError
        finally:
            sleeper.kill()
            sleeper.wait()
        hp.check_log_owner(str(log))                           # gone: no obstacle either


class ExactlyOneStepTest(t70.Segment):
    """G5 (the reviewer's scripts2/fix_clock.py "tap-request-in-rest-tail" and "one-request-two-steps"): with
    u = 0.19 s every request is counted in exactly one step; an input step (the launch too) takes the u-wide zone
    on a rest's side; between two rests the mark is the border."""

    SCN = {"name": "x", "package": ta.PKG, "steps": [{"id": "r1", "do": "rest", "ms": 2000},
                                                     {"id": "r2", "do": "rest", "ms": 2000},
                                                     {"id": "tap", "do": "tap", "x": 0.5, "y": 0.5},
                                                     {"id": "r3", "do": "rest", "ms": 2000}]}
    MARKS = {"launch.start": 101.0, "r1.start": 105.0, "r1.end": 107.0, "r2.start": 107.0, "r2.end": 109.0,
             "tap.start": 110.0, "tap.end": 112.0, "r3.start": 112.5, "r3.end": 114.5}

    def test_identity_at_every_border(self):
        clock, why = u.android_request_clock(t70.probe_group(4890.0 + 100.0, 100.0, rtt=0.36),
                                             t70.probe_group(4890.0 + 120.0, 120.0, rtt=0.0))
        self.assertIsNone(why)
        self.assertAlmostEqual(clock["uncertainty_s"], 0.19)
        h = t70.host
        times = {"launch": 102.0, "launch-edge": 104.7, "launch-zone-in-r1": 105.1, "r1": 105.3,
                 "r2-before-border": 106.99, "r1/r2-border": 107.0, "r2-tail": 109.5, "tap-zone-in-r2": 109.9,
                 "tap-own": 110.15, "tap-end": 111.9, "tap-zone-after": 112.1, "between": 112.35, "r3": 113.0,
                 "r3-end-zone": 114.6, "after-end-margin": 116.0, "before-launch-margin": 100.0}
        start = u.log_position(self.log)
        self.append("".join(request_line(h(t), "/" + name) for name, t in times.items()))
        u.save_requests(self.raw, self.log, start, (h(101.0), h(114.5)), clock, None)
        steps = [{"id": "launch", "start_s": 101.0, "end_s": 105.0}, {"id": "r1", "start_s": 105.0, "end_s": 107.0},
                 {"id": "r2", "start_s": 107.0, "end_s": 109.0}, {"id": "tap", "start_s": 110.0, "end_s": 112.0},
                 {"id": "r3", "start_s": 112.5, "end_s": 114.5}]
        result = {}
        u.request_counts(self.raw, steps, result, u.ANDROID, self.SCN, True, self.MARKS)
        got = {s["id"]: sorted(k.split(" /")[1] for k in s["requests"]["by_path"]) for s in steps}
        self.assertEqual(got, {"launch": ["launch", "launch-edge", "launch-zone-in-r1"],
                               "r1": ["r1", "r2-before-border"],
                               "r2": ["r1/r2-border", "r2-tail"],
                               "tap": ["tap-end", "tap-own", "tap-zone-after", "tap-zone-in-r2"],
                               "r3": ["r3", "r3-end-zone"]})
        block = result["requests"]
        self.assertEqual((block["between_steps"], block["between_paths"]), (1, {"GET /between": 1}))
        self.assertEqual(block["in_margins"], 2)
        self.assertEqual(sum(s["requests"]["count"] for s in steps) + block["between_steps"] + block["in_margins"],
                         block["requests"])
        self.assertEqual(block["requests"], len(times))
        self.assertEqual((steps[2]["requests"]["gap_s"], steps[2]["requests"]["in_gap"]), (1.0, 1))
        # the windows themselves are disjoint and touch where a rest meets another step
        w = u.request_windows(self.raw, steps, u.ANDROID, block_clock(self.raw), self.SCN, self.MARKS)
        spans = sorted((lo, hi) for lo, hi, _, _ in w.values())
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), spans)
        self.assertEqual(w["r2"][1], w["tap"][0])
        self.assertEqual(w["launch"][1], w["r1"][0])
        self.assertAlmostEqual(w["tap"][1], t70.host(112.0) + 0.19)
        self.assertAlmostEqual(w["r3"][0], t70.host(112.5))           # its own start, later than the tap's zone
        self.assertAlmostEqual(w["r2"][1], t70.host(110.0) - 0.19)
        self.assertAlmostEqual(w["r1"][1], t70.host(107.0))           # two rests: the mark
        self.assertAlmostEqual(w["r3"][1], t70.host(114.5) + 0.19)    # the last rest, outward

    def test_an_uncertainty_above_0_2_s_is_unknown(self):
        clock, why = u.android_request_clock(t70.probe_group(4990.0, 100.0, rtt=0.4), t70.probe_group(5010.0, 120.0, rtt=0.0))
        self.assertFalse(clock["known"])
        self.assertEqual(why, "the device's clock is mapped to this machine's only to within 0.210 s (above 0.2 s): adb "
                              "answered too slowly")


def block_clock(raw):
    return json.loads((raw / u.REQUESTS_META).read_text())["clock"]


class ShapeTest(unittest.TestCase):
    """G6 (the reviewer's scripts2/fix_misc.py, 10c): only lower case stays."""

    def test_lower_case_only(self):
        for path, shape in (("/recovery/revoke/kTqZpWmXbHrVdLcYfNsJgA", "/recovery/revoke/:id"),
                            ("/auth/magic/aBcDeFgHiJkLmNoPqRsTuVwXyZ", "/auth/magic/:id"),
                            ("/users/alice", "/users/alice"), ("/api/getUser", "/api/:id"), ("/a.b_c-d", "/a.b_c-d"),
                            ("/v2/Feed", "/v2/:id")):
            self.assertEqual(hp.path_shape(path), shape, path)


class CompareRowTest(ta.Base):
    """G8: a row of `compare` shows how many values each side had; a run whose count is unknown is left out."""

    def test_the_row(self):
        base = t71.SummaryCompareTest.runs(self, "base", [0, 0, None, 0, 0, 0])
        head = t71.SummaryCompareTest.runs(self, "head", [None] * 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = u.main(["compare", "--base", base, "--head", head, "--metric", "idle.requests"])
        self.assertEqual(rc, 0)
        self.assertIn("unknown   idle.requests", out.getvalue())
        self.assertIn("(missing on one side; values: 5 base, 0 head)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
