"""0.7.0, fix round 1: the twelve defects an independent review reproduced, each as a test of the new
outcome. Each test names the reviewer's script that showed the defect. Fronts run on 127.0.0.1 only; no
device, no simulator."""
import sys

sys.dont_write_bytecode = True

import asyncio
import contextlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import httppipe as hp  # noqa: E402
import test_analysis as ta  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import ui_probe as u  # noqa: E402

line, request_line, listening = t70.line, t70.request_line, t70.listening
REST_SCN = {"steps": [{"id": "rest", "do": "rest"}]}


def sim_count(root: Path, log: Path, start: dict, run_lo: float, run_hi: float, rest: tuple, scn=REST_SCN):
    """The simulator's path of a run (this machine's clock): launch [run_lo, rest start), one rest."""
    raw = root / f"raw-{os.urandom(3).hex()}"
    raw.mkdir()
    (raw / "launch-time.json").write_text(json.dumps({"wall": run_lo}))
    (raw / "step-rest.json").write_text(json.dumps({"wall_before": rest[0], "wall_after": rest[1]}))
    u.capture_requests_sim(raw, log, start, (run_lo, run_hi), scn)
    steps = [{"id": "launch"}, {"id": "rest"}]
    result = {}
    u.request_counts(raw, steps, result, u.SIM_PLATFORM, scn, True)
    return raw, steps, result["requests"]


async def keepalive_backend(reader, writer):
    """Answers every request head with 200 and a two-byte body, keeping the connection, ignoring Upgrade."""
    buf = b""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            buf += data
            while b"\r\n\r\n" in buf:
                _, buf = buf.split(b"\r\n\r\n", 1)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
    finally:
        writer.close()


class Root(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def wait_closed(self, log, n):
        for _ in range(200):
            if sum(e["event"] == "closed" for e in map(json.loads, log.read_text().splitlines())) >= n:
                return
            await asyncio.sleep(0.01)


class BackendRefusedTest(Root):
    """F1 (the reviewer's repro_fronts.py, case A)."""

    async def test_a_backend_that_refuses_makes_the_counts_unknown(self):
        log = self.root / "a.jsonl"
        with t70.free_ports(2) as (fp, dead):
            front = hp.Front(fp, dead, str(log))
            server = await front.start()
        try:
            start = u.log_position(log)
            t0 = time.time()
            hp.append_line(str(log), {"event": "request", "conn": 99, "method": "GET", "path": "/api/v1/config",
                                      "query": False})
            await asyncio.sleep(0.05)
            rest_lo = time.time()
            for _ in range(2):
                r, w = await asyncio.open_connection("127.0.0.1", fp)
                w.write(b"GET /api/v1/poll HTTP/1.1\r\nHost: x\r\n\r\n")
                await w.drain()
                await asyncio.wait_for(r.read(), 2)
                w.close()
            rest_hi = time.time() + 0.05
            raw, steps, block = sim_count(self.root, log, start, t0, rest_hi, (rest_lo, rest_hi))
        finally:
            server.close()
            await server.wait_closed()
        self.assertFalse(block["known"])
        self.assertRegex(block["reason"], r"^the backend refused 2 connections from the front during the run \(the first "
                                          r"[0-9.]+ s after the launch\): what the app sent on them was not read$")
        self.assertIsNone(steps[1]["requests"]["count"])
        kept = [json.loads(x)["event"] for x in (raw / u.REQUESTS_FILE).read_text().splitlines()]
        self.assertEqual(kept.count("backend-refused"), 2)
        fails = []
        u.gate_requests(lambda c, sid, m, **k: fails.append((c, m)), [], {"requests": block}, "rest", steps[1],
                        {"id": "rest", "do": "rest"}, dict(u.GATE_DEFAULTS), rest=True)
        self.assertEqual([c for c, _ in fails], ["rest-requests"])


class FrontRunningTest(unittest.TestCase):
    """F2 (the reviewer's repro_fronts.py, case C): a front that is not running, at the start or at the end."""

    def test_a_real_front_that_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "front.jsonl"
            with t70.free_ports(2) as (p1, p2):
                proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "http-front", "--port", str(p1),
                                         "--to-port", str(p2), "--log", str(log)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(proc.stdout.readline().startswith("listening on http://127.0.0.1:"))
                self.assertEqual(u.check_request_log(str(log)), log.resolve())
                start = u.log_position(log)
                self.assertEqual((start["front_pid"], start["front_alive"]), (proc.pid, True))
                with open(log, "a") as fh:
                    fh.write(request_line(time.time(), "/api/v1/config"))
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(10), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                proc.stderr.close()
            with self.assertRaises(u.ProbeError) as cm:
                u.check_request_log(str(log))
            self.assertEqual(str(cm.exception), f"the front that wrote {log} is not running (pid {proc.pid}): start it "
                                                f"again with --log {log}")
            now = time.time()
            raw, steps, block = sim_count(root, log, start, now - 5, now + 1, (now - 2, now))
            meta = json.loads((raw / u.REQUESTS_META).read_text())
            self.assertEqual(meta["front"], {"pid_at_start": proc.pid, "alive_at_start": True, "pid_at_end": proc.pid,
                                             "alive_at_end": False, "port": p1, "accepting_at_end": False})
            # a front told to stop says so in the log (fix round 2, G2), which decides first
            self.assertEqual(block["reason"], "the front was told to stop during the run: what the app sent after that "
                                              "was not piped and is not counted")
            # and one that was not running when a later run of the batch began
            raw, steps, block = sim_count(root, log, u.log_position(log), now - 5, now + 1, (now - 2, now))
            self.assertEqual(block["reason"], f"the front (pid {proc.pid}) was not running at the start of the run: what "
                                              "the app sent was not piped and is not counted")

    def test_a_pid_of_another_user_counts_as_running(self):
        self.assertTrue(u.process_running(1))           # launchd or init: exists, owned by root
        self.assertFalse(u.process_running(None))
        self.assertFalse(u.process_running(0))


class OpaqueConnectionTest(Root):
    """F3 (the reviewer's repro_fronts.py, case B): a connection the front stopped reading is opaque until it
    closes, whatever the server answered, and one that became opaque before the run counts too."""

    async def front(self, name):
        self.writers = []
        log = self.root / name
        with t70.free_ports(2) as (fp, bp):
            backend = await asyncio.start_server(keepalive_backend, "127.0.0.1", bp)
            front = hp.Front(fp, bp, str(log))
            server = await front.start()
        self.addAsyncCleanup(self.stop, server, backend)
        return log, fp

    async def stop(self, server, backend):
        for w in self.writers:
            w.close()
        server.close()
        backend.close()
        for x in (server, backend):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(x.wait_closed(), 2)

    async def connect(self, port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(w)
        return r, w

    async def test_a_declined_upgrade_on_a_kept_connection(self):
        log, fp = await self.front("b.jsonl")
        start = u.log_position(log)
        t0 = time.time()
        r0, w0 = await self.connect(fp)
        w0.write(b"GET /api/v1/config HTTP/1.1\r\nHost: x\r\n\r\n")
        await w0.drain()
        await r0.read(100)
        r, w = await self.connect(fp)
        w.write(b"GET /api/v1/events HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: h2c\r\n\r\n")
        await w.drain()
        self.assertTrue((await r.read(100)).startswith(b"HTTP/1.1 200"))      # the server declined it
        rest_lo = time.time()
        for _ in range(2):                                                    # two polls the front cannot read
            w.write(b"GET /api/v1/poll HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            await r.read(100)
        rest_hi = time.time() + 0.05
        for x in (w, w0):
            x.close()
        await self.wait_closed(log, 2)
        _, steps, block = sim_count(self.root, log, start, t0, rest_hi, (rest_lo, rest_hi))
        self.assertFalse(block["known"])
        self.assertEqual(block["reason"], "1 connection asked for an upgrade (`Connection: upgrade` or CONNECT) and was "
                                          "open during the run: what followed on it is not HTTP/1.x requests the front "
                                          "can count")
        self.assertEqual(u.request_notes({"requests": block}), [])

    async def test_one_that_became_opaque_before_the_run(self):
        log, fp = await self.front("c.jsonl")
        r, w = await self.connect(fp)
        w.write(b"GET /socket HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
        await w.drain()
        await r.read(100)
        await asyncio.sleep(0.05)
        start = u.log_position(log)                                           # the run starts with it open
        t0 = time.time()
        hp.append_line(str(log), {"event": "request", "conn": 9, "method": "GET", "path": "/api/v1/config", "query": False})
        rest_lo = time.time()
        rest_hi = rest_lo + 0.05
        _, _, block = sim_count(self.root, log, start, t0, rest_hi, (rest_lo, rest_hi))
        self.assertFalse(block["known"])
        self.assertIn("1 connection asked for an upgrade", block["reason"])
        w.close()

    async def test_one_closed_before_the_run_does_not_count(self):
        log, fp = await self.front("d.jsonl")
        r, w = await self.connect(fp)
        w.write(b"GET /socket HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
        await w.drain()
        await r.read(100)
        w.close()
        await self.wait_closed(log, 1)
        await asyncio.sleep(0.05)
        start = u.log_position(log)
        t0 = time.time()
        await asyncio.sleep(0.01)
        hp.append_line(str(log), {"event": "request", "conn": 9, "method": "GET", "path": "/api/v1/config", "query": False})
        await asyncio.sleep(0.01)
        rest_lo = time.time()
        _, steps, block = sim_count(self.root, log, start, t0, rest_lo + 0.05, (rest_lo, rest_lo + 0.05))
        self.assertTrue(block["known"], block)
        self.assertEqual(steps[1]["requests"]["count"], 0)


    async def test_what_an_earlier_front_process_wrote_is_not_followed(self):
        """A garbage line and a connection left open by a front process that has since been replaced lie before
        the current front's `listening` line: they say nothing about the current one."""
        log, fp = await self.front("e.jsonl")
        with open(log, "a") as fh:
            fh.write("garbage from a crash\n" + line(time.time(), "upgrade", conn=1)
                     + listening(time.time()))
        start = u.log_position(log)
        t0 = time.time()
        await asyncio.sleep(0.01)
        hp.append_line(str(log), {"event": "request", "conn": 9, "method": "GET", "path": "/api/v1/config", "query": False})
        await asyncio.sleep(0.01)
        rest_lo = time.time()
        _, _, block = sim_count(self.root, log, start, t0, rest_lo + 0.05, (rest_lo, rest_lo + 0.05))
        self.assertTrue(block["known"], block)


class NoRecordTest(ta.Base):
    """F4 (the reviewer's repro_meta_missing.py)."""

    def test_a_run_given_a_log_without_its_record_is_unknown(self):
        cap = ta.Capture(self.root)
        c = json.loads((cap.dir / "capture.json").read_text())
        c["requests_log"] = "/somewhere/front.jsonl"
        (cap.dir / "capture.json").write_text(json.dumps(c))
        run = cap.analyze()
        self.assertFalse(run["requests"]["known"])
        self.assertEqual(run["requests"]["reason"], "the run was given a request log, but raw/requests-meta.json is "
                                                    "missing: the run kept no record of it")
        self.assertIsNone(ta.steps_by_id(run)["idle"]["requests"]["count"])
        res = cap.gate()
        self.assertEqual([(v["check"], v["step"]) for v in res["violations"]], [("rest-requests", "idle"),
                                                                               ("rest-requests", "idle2")])
        (cap.dir / "raw" / u.REQUESTS_META).write_text("not json")
        self.assertIn("cannot be read", cap.analyze()["requests"]["reason"])

    def test_a_record_that_cannot_be_written_fails_the_run(self):
        raw = Path(self.root) / "ro"
        raw.mkdir()
        log = Path(self.root) / "f.jsonl"
        log.write_text(listening(1.0))
        start = u.log_position(log)
        raw.chmod(0o500)
        try:
            with self.assertRaises(u.ProbeError) as cm:
                u.save_requests(raw, log, start, (0, 9), {}, None)
        finally:
            raw.chmod(0o700)
        self.assertIn("the requests of the run could not be saved in", str(cm.exception))
        self.assertEqual(list(raw.iterdir()), [])


class ClockNoteTest(unittest.TestCase):
    """F5: the gate says when each rest's window was narrowed by more than 0.1 s; no whole-second case remains.
    (Until the certifying run of 2026-11-11 a rest was widened into its neighbour; now the input step takes the
    zone.)"""

    def test_the_note(self):
        block = {"known": True, "clock": {"uncertainty_s": 0.25}, "between_steps": 0}
        self.assertEqual(u.request_notes({"requests": block}),
                         ["the device's clock was mapped to this machine's to within 0.250 s, so each rest's request "
                          "window was narrowed by that much (at most half of the rest) where it meets an input step or "
                          "the launch: a request that close to an input step is counted in that step, not in the rest"])
        self.assertEqual(u.request_notes({"requests": {**block, "clock": {"uncertainty_s": 0.05}}}), [])
        self.assertFalse(hasattr(u, "parse_clock_stamp"))

    def test_the_launch_takes_the_zone_from_the_rest_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            seg = Path(tmp)
            log = seg / "front.jsonl"
            log.write_text(listening(1.0))
            start = u.log_position(log)
            clock = u.android_request_clock(t70.probe_group(4990.0, 100.0, rtt=0.15), t70.probe_group(5010.0, 120.0, rtt=0.15))[0]
            self.assertAlmostEqual(clock["uncertainty_s"], 0.16)
            h = lambda up: up + 4890.0
            with open(log, "a") as fh:
                fh.write(request_line(h(102.0), "/a") + request_line(h(109.85), "/just-before-idle")
                         + request_line(h(110.1), "/just-after-idle-starts"))
            u.save_requests(seg, log, start, (h(101.5), h(117.6)), clock, None)
            steps = [{"id": "launch", "start_s": 101.5, "end_s": 110.0}, {"id": "idle", "start_s": 110.0, "end_s": 112.0},
                     {"id": "open", "start_s": 112.5, "end_s": 115.0}, {"id": "idle2", "start_s": 115.6, "end_s": 117.6}]
            result = {}
            u.request_counts(seg, steps, result, u.ANDROID, ta.SCN, True, ta.MARKS)
            self.assertEqual(steps[1]["requests"]["count"], 0)
            self.assertEqual(steps[0]["requests"]["by_path"], {"GET /a": 1, "GET /just-after-idle-starts": 1,
                                                               "GET /just-before-idle": 1})


class LayerNameTest(unittest.TestCase):
    """F7 (the reviewer's repro_layers11.py): a layer without a uid is the app's only by the start of its name."""

    def test_names(self):
        pkg = "com.example.app"
        layers = {(None, f"{pkg}/{pkg}.Main#0"): 10, (None, f"SurfaceView - {pkg}/{pkg}.Main#0"): 2,
                  (None, f"{pkg}.debug/{pkg}.debug.Main#0"): 5, (None, f"{pkg}.companion/{pkg}.companion.Overlay#0"): 42,
                  (None, f"SurfaceView - com.vendor.widget/com.vendor.widget.Preview{pkg}#0"): 3,
                  (None, f"com.example.launcher/.Home[{pkg}]#0"): 7, (None, f"Splash Screen {pkg}#0"): 1}
        self.assertEqual(u.app_layer_frames({"total": 1, "layers": layers}, pkg, 10130),
                         {f"{pkg}/{pkg}.Main#0": 10, f"SurfaceView - {pkg}/{pkg}.Main#0": 2})

    def test_the_reviewers_dump(self):
        pkg = "com.example.app"
        dump = lambda app, other: (f"SurfaceFlinger TimeStats:\ntotalFrames = {app + other + 3}\nlayerName = {pkg}/{pkg}.Main#0\n"
                                   f"packageName =\ntotalFrames = {app}\nlayerName = {pkg}.companion/{pkg}.companion.Overlay#0\n"
                                   f"packageName =\ntotalFrames = {other}\nlayerName = SurfaceView - com.vendor.widget/"
                                   f"com.vendor.widget.Preview{pkg}#0\npackageName =\ntotalFrames = 3\n")
        b = u.app_layer_frames(u.parse_timestats(dump(10, 0)), pkg, 10130)
        a = u.app_layer_frames(u.parse_timestats(dump(10, 42)), pkg, 10130)
        self.assertEqual(sorted(a), [f"{pkg}/{pkg}.Main#0"])
        self.assertEqual(sum(max(0, c - b.get(n, 0)) for n, c in a.items()), 0)


class ProofTest(t70.Segment):
    """F8 (the reviewer's repro_margin_proof.py) and F11."""

    def sim(self, appended):
        (self.raw / "launch-time.json").write_text(json.dumps({"wall": 1000.0}))
        (self.raw / "step-rest.json").write_text(json.dumps({"wall_before": 1007.0, "wall_after": 1012.0}))
        start = u.log_position(self.log)
        self.append(appended)
        u.capture_requests_sim(self.raw, self.log, start, (990.0, 1020.0), REST_SCN)
        steps = [{"id": "launch"}, {"id": "rest"}]
        result = {}
        u.request_counts(self.raw, steps, result, u.SIM_PLATFORM, REST_SCN, True)
        return steps, result["requests"]

    def test_a_request_in_the_margin_proves_nothing(self):
        steps, block = self.sim(request_line(998.5, "/health"))
        self.assertFalse(block["known"])
        self.assertIn("no request passed through the front between the launch and the end of the last step",
                      block["reason"])
        self.assertIsNone(steps[1]["requests"]["count"])

    def test_a_front_restarted_after_the_run_began_counts_even_in_the_margin(self):
        """Fix round 2, G1 (the reviewer's scripts2/fix_front.py case N1): a restart after the run's start
        offset means the old front died at a time nobody knows, so the margin is no exception."""
        for t in (998.0, 1009.0, 1025.0):
            steps, block = self.sim(listening(t) + request_line(1001.0, "/a"))
            self.assertEqual(block["reason"], "the front started listening again after the run began (it was "
                                              "restarted): what the app sent while it was down was not piped and is "
                                              "not counted", t)


class GapTest(t70.Segment):
    """F9: a rest runs on to the next step's start; requests after an input step's window are noted."""

    def test_the_gap_and_the_note(self):
        clock = t70.ANDROID_CLOCK
        h = t70.host
        start = u.log_position(self.log)
        self.append(request_line(h(102.0), "/a") + request_line(h(112.2), "/api/v1/poll")
                    + request_line(h(115.3), "/api/v1/after-tap"))
        u.save_requests(self.raw, self.log, start, (h(101.5), h(117.6)), clock, None)
        steps = t70.AndroidWindowTest.steps(self)
        result = {}
        u.request_counts(self.raw, steps, result, u.ANDROID, ta.SCN, True, ta.MARKS)
        st = {s["id"]: s["requests"] for s in steps}
        self.assertEqual(st["idle"], {"count": 1, "by_path": {"GET /api/v1/poll": 1}, "gap_s": 0.5, "in_gap": 1})
        block = result["requests"]
        self.assertEqual((block["between_steps"], block["between_paths"]), (1, {"GET /api/v1/after-tap": 1}))
        fails = []
        u.gate_requests(lambda c, sid, m, **k: fails.append(m), [], {"requests": block}, "idle",
                        steps[1], ta.SCN["steps"][0], dict(u.GATE_DEFAULTS), rest=True)
        self.assertEqual(fails, ["1 request sent at rest (GET /api/v1/poll x1, 1 of them in the 0.5 s between the end of "
                                 "this rest and the next step), allowed 0"])
        self.assertEqual(u.request_notes({"requests": block}),
                         ["1 request fell between steps, after an input step's window, and was not gated: "
                          "GET /api/v1/after-tap x1"])

    def test_a_lookup_that_scrolls_ends_the_rest_at_its_first_swipe(self):
        scn = {"name": "x", "package": ta.PKG, "steps": [{"id": "idle", "do": "rest", "ms": 2000},
                                                         {"id": "open", "do": "tap", "find": {"text": "A", "scroll": {
                                                             "from": [0.5, 0.8], "to": [0.5, 0.2], "max": 2}}}]}
        marks = {**ta.MARKS, "open.find_start": 112.2}
        steps = [{"id": "launch", "start_s": 101.5, "end_s": 110.0}, {"id": "idle", "start_s": 110.0, "end_s": 112.0},
                 {"id": "open", "start_s": 112.5, "end_s": 115.0}]
        w = u.request_windows(self.raw, steps, u.ANDROID, t70.ANDROID_CLOCK, scn, marks)
        # the tap takes the zone before its lookup's first swipe (the rest's end today) from the rest
        self.assertAlmostEqual(w["idle"][1], t70.host(112.2) - 0.014)
        self.assertAlmostEqual(w["open"][0], t70.host(112.2) - 0.014)
        scn["steps"][1]["find"].pop("scroll")
        w = u.request_windows(self.raw, steps, u.ANDROID, t70.ANDROID_CLOCK, scn, marks)
        self.assertAlmostEqual(w["idle"][1], t70.host(112.5) - 0.014)
        self.assertAlmostEqual(w["open"][0], t70.host(112.5) - 0.014)


class LogContentTest(Root):
    """F10 (the reviewer's repro_fronts.py case D and fuzz_reader.py)."""

    async def test_the_log_is_0600_whether_it_existed_or_not(self):
        log = self.root / "d.jsonl"
        log.write_text("")
        os.chmod(log, 0o644)
        hp.append_line(str(log), {"event": "listening"})
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        raw = self.root / "raw"
        raw.mkdir()
        (raw / u.REQUESTS_FILE).write_text("")
        os.chmod(raw / u.REQUESTS_FILE, 0o644)
        log2 = self.root / "e.jsonl"
        log2.write_text(listening(1.0))
        u.save_requests(raw, log2, u.log_position(log2), (0, 9), {}, None)
        for name in (u.REQUESTS_FILE, u.REQUESTS_META):
            self.assertEqual(stat.S_IMODE((raw / name).stat().st_mode), 0o600, name)

    async def test_only_the_shape_of_a_path(self):
        cases = {b"/api/v1/users/e2erv01/profile": "/api/v1/users/:id/profile", b"/join/ABC123": "/join/:id",
                 b"/x;jsessionid=SECRET": "/:id", b"/a@b/c": "/:id/c", b"/a:b": "/:id", b"/a%20b": "/:id",
                 b"/api/v12/feed.json": "/api/v12/feed.json", b"/v1000": "/:id", b"/api//x/": "/api//x/",
                 b"/ana-bel_l.e": "/ana-bel_l.e", b"/" + b"a" * 33: "/:id",
                 b"http://user:pa/ss@host/x?q": "/:id/x", b"http://h.example:8080/api/7": "/api/:id"}
        for target, shape in cases.items():
            events = hp.RequestReader().feed(b"GET " + target + b" HTTP/1.1\r\n\r\n")
            self.assertEqual(events[0]["path"], shape, target)
        self.assertEqual(hp.path_shape("/a/b2"), "/a/:id")


class SummaryCompareTest(ta.Base):
    """F12: a null request count is unknown in `summary` and `compare`, never zero and never "same"."""

    def runs(self, side, counts):
        out = []
        for n, c in enumerate(counts):
            run = {"host": {"load1_before": 1.0}, "launch": {"total_time_ms": None},
                   "steps": [{"id": "idle", "frames": 0, "janky_frames": 0, "frame_ms_p50": None, "frame_ms_p90": None,
                              "frame_ms_max": None, "first_frame_completed_after_step_start_ms": None,
                              "cpu_ticks_total": 0, "cpu_ticks_by_thread": {}, "requests": {"count": c}}]}
            d = Path(self.root) / side / f"run-{n:02d}"
            d.mkdir(parents=True)
            (d / "capture.json").write_text("{}")
            (d / "run.json").write_text(json.dumps(run))
            out.append(str(d))
        return str(Path(self.root) / side)

    def test_nulls(self):
        base = self.runs("base", [None] * 5)
        head = self.runs("head", [0] * 5)
        self.assertEqual(u.summary([base])["stats"]["idle.requests"], {"n": 0})
        m = u.compare([base], [head], [], 0.01, 5)["metrics"]["idle.requests"]
        self.assertEqual((m["verdict"], m["reason"]), ("unknown", "missing on one side"))
        both = u.compare([base], [base], [], 0.01, 5)["metrics"]
        self.assertNotIn("idle.requests", both)          # no numeric value on either side: not compared at all
        m = u.compare([base], [base], ["idle.requests"], 0.01, 5)["metrics"]["idle.requests"]
        self.assertEqual((m["verdict"], m["reason"]), ("unknown", "missing on both sides"))


if __name__ == "__main__":
    unittest.main()
