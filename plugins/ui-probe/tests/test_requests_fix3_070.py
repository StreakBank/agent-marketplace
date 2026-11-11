"""0.7.0, fix round 3: the reviewer's third pass (scripts3/). Real front processes on 127.0.0.1; no device."""
import sys

sys.dont_write_bytecode = True

import json
import os
import signal
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
import test_requests_fix2_070 as t72  # noqa: E402
import ui_probe as u  # noqa: E402

request_line = t70.request_line


def front(log, backend_port, cwd=None, ignore_hup=False, with_log=True):
    """`ui-probe http-front` as a process of its own; with ignore_hup, started with SIGHUP ignored, as
    `nohup` starts it."""
    with t70.free_ports(1) as (port,):
        pass
    argv = [sys.executable, "-B", str(Path(u.__file__)), "http-front", "--port", str(port), "--to-port", str(backend_port)]
    if with_log:
        argv += ["--log", str(log)]
    # Set either way: a suite started with nohup hands an ignored SIGHUP down to every child.
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN if ignore_hup else signal.SIG_DFL))
    first = proc.stdout.readline()
    return proc, port, first


def done(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    for f in (proc.stdout, proc.stderr):
        f.close()


class NohupTest(t72.Root):
    """H1 (the reviewer's scripts3/nohup3.py): a front started with SIGHUP ignored, as `nohup ... &` starts
    it, outlives the hang-up of the shell or terminal that started it; without that it stops on SIGHUP."""

    def test_sighup_ignored_at_start_is_kept_ignored(self):
        log = self.root / "nohup.jsonl"
        proc, port, first = front(log, self.backend.port, ignore_hup=True)
        try:
            self.assertTrue(first.startswith("listening on http://127.0.0.1:"))
            proc.send_signal(signal.SIGHUP)
            time.sleep(0.5)
            self.assertIsNone(proc.poll())
            self.assertTrue(t72.get(port, "/api/v1/config").startswith(b"HTTP/1.1 200"))      # still serving
            self.assertNotIn("stopping", [e["event"] for e in t72.events(log)])
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(5), 0)
            self.assertEqual([e["signal"] for e in t72.events(log) if e["event"] == "stopping"], ["SIGTERM"])
        finally:
            done(proc)

    def test_sighup_stops_a_front_that_does_not_ignore_it(self):
        log = self.root / "hup.jsonl"
        proc, port, _ = front(log, self.backend.port)
        try:
            proc.send_signal(signal.SIGHUP)
            self.assertEqual(proc.wait(5), 0)
            self.assertEqual((t72.events(log)[-1]["event"], t72.events(log)[-1]["signal"]), ("stopping", "SIGHUP"))
        finally:
            done(proc)


class ShortTapTest(t70.Segment):
    """H2 (the reviewer's scripts3/windows3.py "short-tap"): a tap of 0.3 s between two rests with u = 0.19 s.
    Since the certifying run of 2026-11-11 the tap takes the zone on both sides (at most half of each rest):
    every request near it is the tap's, each counted once."""

    SCN = {"name": "x", "package": ta.PKG, "steps": [{"id": "r1", "do": "rest", "ms": 2000},
                                                     {"id": "t", "do": "tap", "x": 0.5, "y": 0.5},
                                                     {"id": "r2", "do": "rest", "ms": 2000}]}
    MARKS = {"launch.start": 101.0, "r1.start": 105.0, "r1.end": 110.0, "t.start": 110.0, "t.end": 110.3,
             "r2.start": 110.3, "r2.end": 115.0}

    def test_the_tap_takes_both_zones(self):
        clock, why = u.android_request_clock(t70.probe_group(4990.0, 100.0, rtt=0.18), t70.probe_group(5010.0, 120.0, rtt=0.18))
        self.assertIsNone(why)
        self.assertAlmostEqual(clock["uncertainty_s"], 0.19)
        h = t70.host
        start = u.log_position(self.log)
        self.append("".join(request_line(h(t), f"/r{n}") for n, t in enumerate((102.0, 110.05, 110.15, 110.25, 113.0))))
        u.save_requests(self.raw, self.log, start, (h(101.0), h(115.0)), clock, None)
        steps = [{"id": "launch", "start_s": 101.0, "end_s": 105.0}, {"id": "r1", "start_s": 105.0, "end_s": 110.0},
                 {"id": "t", "start_s": 110.0, "end_s": 110.3}, {"id": "r2", "start_s": 110.3, "end_s": 115.0}]
        result = {}
        u.request_counts(self.raw, steps, result, u.ANDROID, self.SCN, True, self.MARKS)
        got = {s["id"]: sorted(k.split(" /")[1] for k in s["requests"]["by_path"]) for s in steps}
        self.assertEqual(got, {"launch": ["r0"], "r1": [], "t": ["r1", "r2", "r3"], "r2": ["r4"]})
        block = result["requests"]
        self.assertEqual(sum(s["requests"]["count"] for s in steps) + block["between_steps"] + block["in_margins"],
                         block["requests"])
        self.assertEqual(block["requests"], 5)
        w = u.request_windows(self.raw, steps, u.ANDROID, t72.block_clock(self.raw), self.SCN, self.MARKS)
        spans = sorted((lo, hi) for lo, hi, _, _ in w.values())
        self.assertTrue(all(a[1] <= b[0] + 1e-9 for a, b in zip(spans, spans[1:])), spans)
        self.assertAlmostEqual(w["r1"][1], h(110.0) - 0.19)
        self.assertAlmostEqual(w["t"][0], h(110.0) - 0.19)
        self.assertAlmostEqual(w["t"][1], h(110.3) + 0.19)
        self.assertAlmostEqual(w["r2"][0], h(110.3) + 0.19)


class OwnerOfAnotherLogTest(t72.Root):
    """H3 (the reviewer's scripts3/fronts3.py "stale log naming a front of another log"): only a front whose
    `--log` is this same file is an obstacle."""

    def test_a_front_on_another_log_is_no_obstacle(self):
        log_a, log_b = self.root / "a.jsonl", self.root / "b.jsonl"
        pa, _, _ = front(log_a, self.backend.port)
        try:
            log_b.write_text(t70.line(1.0, "listening", port=1, to_port=2, pid=pa.pid))      # stale: names pa
            pb, _, first = front(log_b, self.backend.port)
            try:
                self.assertTrue(first.startswith("listening on http://127.0.0.1:"), pb.stderr.read() if not first else "")
            finally:
                done(pb)
        finally:
            done(pa)

    def test_a_front_without_a_log_is_no_obstacle(self):
        log = self.root / "c.jsonl"
        pa, _, _ = front(None, self.backend.port, with_log=False)
        try:
            log.write_text(t70.line(1.0, "listening", port=1, to_port=2, pid=pa.pid))
            hp.check_log_owner(str(log))                                                      # no FrontError
        finally:
            done(pa)

    def test_the_same_file_named_relative_to_the_other_fronts_folder(self):
        log = self.root / "d.jsonl"
        pa, _, _ = front("d.jsonl", self.backend.port, cwd=str(self.root))                   # --log d.jsonl in root
        try:
            self.assertTrue(log.exists())
            with self.assertRaises(hp.FrontError) as cm:
                hp.check_log_owner(str(log))
            self.assertIn(f"is being written by another front (pid {pa.pid})", str(cm.exception))
            self.assertTrue(hp.same_file(hp.front_log(pa.pid, hp.front_command(pa.pid)), str(log)))
        finally:
            done(pa)


if __name__ == "__main__":
    unittest.main()
