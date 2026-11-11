"""0.7.0: Android 11 and older. Section counts come from the span of the trace's ftrace events (their
bundles carry no packet timestamp) and the tracing session perfetto printed, not from the stamped
packets; a device without a frame timeline reports its counts as unavailable, never 0; and the
compositor's per-layer statistics in Android 11's format (no `uid` line) are read. The trace and
the statistics are built here under a neutral package name: no captured file is used."""
import sys

sys.dont_write_bytecode = True

import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import pftrace as pt  # noqa: E402
import test_analysis as ta  # noqa: E402
import test_pftrace as tp  # noqa: E402
import ui_probe as u  # noqa: E402

PKG, PID = ta.PKG, ta.PID
NS = 1_000_000_000


def process_tree(ts):
    tree = tp.lfield(pt.F_PROCESS_TREE, tp.lfield(1, tp.vfield(1, PID) + tp.lfield(3, PKG.encode())))
    return tp.packet(tree, ts=ts)


def clean_stats():
    """TraceStats with one buffer and nothing lost, final flush succeeded (1); unstamped, as on a device."""
    return tp.packet(tp.lfield(pt.F_TRACE_STATS, tp.lfield(1, tp.vfield(12, 65536)) + tp.vfield(15, 1)))


def ftrace_stats(phase, now_s, cpus=2):
    """An FtraceStats packet (TracePacket field 34) as the ftrace data source writes it: no packet timestamp; its
    phase (1 start of trace, 2 end of trace) and per CPU a FtraceCpuStats with `now_ts` (field 7, a double in
    seconds) when now_s is given."""
    import struct
    cpu = lambda n: tp.lfield(2, tp.vfield(1, n) + (b"" if now_s is None else
                                                    tp.varint(7 << 3 | 1) + struct.pack("<d", now_s + n * 0.001)))
    return tp.packet(tp.lfield(pt.F_FTRACE_STATS, tp.vfield(1, phase) + b"".join(cpu(n) for n in range(cpus))))


def ftrace_raw(phase, nows):
    """An FtraceStats packet with exactly these per-CPU now_ts values (seconds)."""
    import struct
    cpus = b"".join(tp.lfield(2, tp.vfield(1, n) + tp.varint(7 << 3 | 1) + struct.pack("<d", now)) for n, now in enumerate(nows))
    return tp.packet(tp.lfield(pt.F_FTRACE_STATS, tp.vfield(1, phase) + cpus))


def bundle(events):
    """An ftrace bundle (TracePacket.ftrace_events, no packet timestamp): (seconds, text) print events of the app."""
    return tp.packet(tp.lfield(pt.F_FTRACE_EVENTS, tp.vfield(1, 0) + b"".join(
        tp.ftrace_event(int(s * NS), PID + 1, text) for s, text in events)))


def frames_every(lo, hi, step=0.5):
    out, s = [], lo
    while s <= hi:
        out += [(s, f"B|{PID}|Choreographer#doFrame 1"), (s + 0.01, "E")]
        s += step
    return out


def android11_trace(event_lo, event_hi):
    """Process-tree packets early (stamped), statistics, and ftrace bundles of print events from event_lo to
    event_hi seconds, split over several bundles. No frame-timeline packet."""
    events = frames_every(event_lo, event_hi)
    third = len(events) // 3
    return (process_tree(int(99.0 * NS)) + process_tree(int(100.5 * NS)) + process_tree(int(102.0 * NS))
            + clean_stats() + bundle(events[:third]) + bundle(events[third:2 * third]) + bundle(events[2 * third:]))


SF11 = """SurfaceFlinger TimeStats:
statsStart = 1
statsEnd = 2
totalFrames = {total}
missedFrames = 0
displayOnTime = 100 ms
presentToPresent histogram is as below:
0ms=0 1ms=0
layerName = {pkg}/{pkg}.Main#0
packageName =
totalFrames = {app}
droppedFrames = 0
present2present histogram is as below:
0ms=0
layerName = SurfaceView - {pkg}/{pkg}.Main#0
packageName =
totalFrames = {surface}
droppedFrames = 0
layerName = NavigationBar0#0
packageName =
totalFrames = 3
droppedFrames = 0
"""


def sf11(total, app, surface=0):
    return SF11.format(total=total, app=app, surface=surface, pkg=PKG)


class Android11Capture(ta.Capture):
    def __init__(self, root, files=None, tracer_output=None, sdk="30", **kw):
        super().__init__(root, files, **kw)
        cap = json.loads((self.dir / "capture.json").read_text())
        cap["device"]["sdk"] = sdk
        cap["tracer_output"] = tracer_output
        (self.dir / "capture.json").write_text(json.dumps(cap))


SESSION = ("[099.000] perfetto_cmd.cc:709     Connected to the Perfetto traced service, TTL: 64s\n"
           "[119.500] perfetto_cmd.cc:858     SIGINT/SIGTERM received: disabling tracing.\n"
           "[119.502] perfetto_cmd.cc:810     Wrote 1000 bytes into /data/misc/perfetto-traces/uiprobe-1.pftrace\n")


class SectionsSpanTest(ta.Base):
    """The marks of test_analysis: launch 101.5, idle 110-112, open 112.5-115, idle2 115.6-117.6, END 118."""

    def analyse(self, trace_bytes, tracer_output=None, sdk="30"):
        cap = Android11Capture(self.root, tracer_output=tracer_output, sdk=sdk, trace=True, sections=True,
                               name=f"run-{len(list(Path(self.root).iterdir()))}")
        (cap.dir / "trace.pftrace").write_bytes(trace_bytes)
        return cap, cap.analyze()

    def test_events_over_the_whole_run_cover_every_step(self):
        cap, run = self.analyse(android11_trace(100.0, 119.0))
        st = ta.steps_by_id(run)
        self.assertEqual(run["trace_span_s"], [99.0, 102.0])          # the stamped packets: early only
        self.assertEqual(run["sections_span_s"], [100.0, 119.01])      # the last end event
        self.assertEqual(run["sections_span_from"], ["ftrace events"])
        self.assertEqual(run["sections_uncovered_steps"], [])
        for sid in ta.ORDER:
            self.assertTrue(st[sid]["sections_covered"], sid)
            self.assertIsNotNone(st[sid]["sections_all"], sid)
        self.assertEqual(st["idle"]["sections"]["Choreographer#doFrame"], 4)     # 110.0, 110.5, 111.0, 111.5
        self.assertEqual(st["open"]["sections"]["Choreographer#doFrame"], 5)     # 112.5 .. 114.5
        # no frame timeline: unavailable, never 0 frames
        self.assertIsNone(run["frametimeline"])
        self.assertEqual(run["frametimeline_unavailable"],
                         "the device has no frame timeline (Android API 30; it starts with Android 12, API 31)")
        for sid in ta.ORDER:
            self.assertIsNone(st[sid]["frames_compositor"], sid)
            self.assertIsNone(st[sid]["trace_covered"], sid)
        self.assertNotIn("compositor-trace", run["frame_sources"])
        res = cap.gate()
        self.assertIn("the device has no frame timeline (Android API 30; it starts with Android 12, API 31): the "
                      "compositor's frame-timeline counts are unavailable, not 0", res["notes"])
        self.assertFalse(any("did not cover" in n for n in res["notes"]), res["notes"])
        self.assertNotIn("frametimeline.app_buffer_frames", u.run_row(str(cap.dir), run))

    def test_a_quiet_tail_is_covered_by_the_data_sources_own_statistics(self):
        """Fix round 1, F6 (the reviewer's a11_fields.py): the kept Android 11 trace holds FtraceStats for the
        start and the end of the trace, unstamped as packets, with a `now_ts` per CPU on the trace clock;
        those are the bounds, and perfetto's console lines are not used at all."""
        trace = android11_trace(100.0, 113.5) + ftrace_stats(1, 99.2) + ftrace_stats(2, 119.4)
        _, run = self.analyse(trace, tracer_output="[010.000] perfetto: Connected to the Perfetto traced service\n"
                                                   "[500.000] perfetto: SIGTERM received: disabling tracing.\n")
        st = ta.steps_by_id(run)
        self.assertEqual(run["sections_span_s"], [99.201, 119.4])     # the latest start and the earliest end of the CPUs
        self.assertEqual(run["sections_span_from"], ["the ftrace data source's statistics (start and end of trace)"])
        self.assertTrue(all(st[sid]["sections_covered"] for sid in ta.ORDER))
        self.assertEqual(st["idle2"]["sections"]["Choreographer#doFrame"], 0)

    def test_statistics_without_times_let_the_session_widen_the_events(self):
        trace = android11_trace(100.0, 113.5) + ftrace_stats(1, None) + ftrace_stats(2, None)
        _, run = self.analyse(trace, tracer_output=SESSION)
        st = ta.steps_by_id(run)
        self.assertEqual(run["sections_span_s"], [99.0, 119.5])
        self.assertEqual(run["sections_span_from"], ["ftrace events", "the tracer's output"])
        self.assertTrue(all(st[sid]["sections_covered"] for sid in ta.ORDER))

    def test_without_both_phases_only_the_events_cover(self):
        """The reviewer's build12.py "api30-died": the data source stopped at 111 s while perfetto's session ran
        on; with no end-of-trace statistics the session is not used, and idle2 is not covered (as 0.6.0)."""
        for extra in (b"", ftrace_stats(1, 99.2)):
            trace = android11_trace(100.0, 113.5) + extra
            cap, run = self.analyse(trace, tracer_output=SESSION)
            st = ta.steps_by_id(run)
            self.assertEqual(run["sections_span_from"], ["ftrace events"])
            self.assertEqual(run["sections_uncovered_steps"], ["open", "idle2"])
            self.assertIsNone(st["idle2"]["sections_all"])
            scn = json.loads((cap.dir / "capture.json").read_text())["scenario"]
            scn["steps"][2]["expect"] = {"sections_max": {"Choreographer#doFrame": 0}}
            res = cap.gate(scn)
            self.assertEqual(res["verdict"], "FAIL")
            self.assertIn("idle2: the trace's ftrace events did not cover this step; its section counts are unavailable",
                          res["notes"])
            self.assertIn(("sections", "'Choreographer#doFrame' was not counted: the trace did not cover this step"),
                          [(v["check"], v["message"]) for v in res["violations"]])

    def test_android_12_behaves_as_0_6_0(self):
        """The reviewer's build12.py "api33": a frame timeline whose stamped packets stop at 113 s while the ftrace
        events run to 119 s. Sections are covered by the stamped packets' span, as in 0.6.0, whose own gate gave
        exactly these lines (checked against 0.6.0's code)."""
        ftl = b"".join(tp.packet(tp.ftl(2, tp.vfield(1, k), tp.vfield(2, 500 + k), tp.vfield(3, 999), tp.vfield(4, 1),
                                        tp.vfield(5, 1), tp.vfield(7, 1)), ts=int((100.0 + k) * NS)) for k in range(14))
        trace = ftl + android11_trace(100.0, 119.0) + ftrace_stats(1, 99.2) + ftrace_stats(2, 119.4)
        cap, run = self.analyse(trace, tracer_output=SESSION, sdk="33")
        st = ta.steps_by_id(run)
        self.assertEqual(run["trace_span_s"], [99.0, 113.0])
        self.assertEqual([(sid, st[sid]["sections_covered"], st[sid]["trace_covered"],
                           (st[sid]["sections"] or {}).get("Choreographer#doFrame"), st[sid]["frames_compositor"])
                          for sid in ta.ORDER],
                         [("launch", True, True, 17, 0), ("idle", True, True, 4, 0), ("open", False, False, None, None),
                          ("idle2", False, False, None, None)])
        scn = json.loads((cap.dir / "capture.json").read_text())["scenario"]
        scn["steps"][2]["expect"] = {"sections_max": {"Choreographer#doFrame": 0}}
        res = cap.gate(scn)
        self.assertEqual([(v["check"], v["step"], v["message"]) for v in res["violations"]],
                         [("sections", "idle2", "'Choreographer#doFrame' was not counted: the trace did not cover this step")])
        self.assertEqual(res["notes"], ["open: the trace did not cover this step; its trace counts are unavailable",
                                        "idle2: the trace did not cover this step; its trace counts are unavailable"])

    def test_no_ftrace_event_covers_nothing(self):
        trace = process_tree(int(99.0 * NS)) + clean_stats()
        _, run = self.analyse(trace, tracer_output=SESSION)
        self.assertIsNone(run["sections_span_s"])
        self.assertEqual(run["sections_uncovered_steps"], ta.ORDER)

    def test_tracer_session(self):
        self.assertEqual(u.tracer_session(SESSION), (99 * NS, int(119.5 * NS)))
        self.assertEqual(u.tracer_session("perfetto output"), (None, None))
        self.assertEqual(u.tracer_session(None), (None, None))

    def test_android_12_and_later_without_frame_timeline_packets_is_read_as_0_6_0(self):
        """Fix round 2, G7 (the reviewer's scripts2/build_api34_noftl.py): API 34, a trace with no frame-timeline
        packet (its data source wrote none), ftrace events over the run and FtraceStats for both phases. 0.6.0
        covers no step (the stamped packets span 99 to 102 s) and fails idle2; so does 0.7.0. The same for a
        capture that gives no API level."""
        trace = android11_trace(100.0, 119.0) + ftrace_stats(1, 99.5) + ftrace_stats(2, 119.4)
        for sdk in ("34", None, ""):
            cap, run = self.analyse(trace, tracer_output=SESSION, sdk=sdk)
            st = ta.steps_by_id(run)
            self.assertNotIn("frametimeline_unavailable", run)
            self.assertEqual(run["frametimeline"]["app_buffer_frames"], 0)          # what 0.6.0 reports
            self.assertIn("compositor-trace", run["frame_sources"])
            self.assertEqual(run["trace_span_s"], [99.0, 102.0])
            self.assertEqual([(sid, st[sid]["trace_covered"], st[sid]["sections_covered"], st[sid]["sections_all"])
                              for sid in ta.ORDER], [(sid, False, False, None) for sid in ta.ORDER])
            scn = json.loads((cap.dir / "capture.json").read_text())["scenario"]
            scn["steps"][2]["expect"] = {"sections_max": {"Choreographer#doFrame": 10}}
            res = cap.gate(scn)
            self.assertEqual([(v["check"], v["step"], v["message"]) for v in res["violations"]],
                             [("sections", "idle2", "'Choreographer#doFrame' was not counted: the trace did not cover "
                                                    "this step")], sdk)
            self.assertEqual(res["notes"], [f"{sid}: the trace did not cover this step; its trace counts are unavailable"
                                            for sid in ta.ORDER], sdk)

    def test_statistics_that_are_not_bounds(self):
        """Fix round 2, G4 (the reviewer's scripts2/build_traces2.py): FtraceStats whose now_ts is 0, or 0 on one
        CPU, or that come as two start/end pairs, bound nothing: none covers a step the events do not."""
        died = android11_trace(100.0, 111.0)
        cases = {"start-now-zero": android11_trace(104.0, 111.0) + ftrace_stats(1, 0.0, cpus=1) + ftrace_stats(2, 119.4),
                 "end-cpu-zero": died + ftrace_stats(1, 99.5) + ftrace_raw(2, [119.4, 0.0]),
                 "two-sessions": died + ftrace_stats(1, 99.5, cpus=1) + ftrace_stats(2, 104.0, cpus=1)
                 + ftrace_stats(1, 104.5, cpus=1) + ftrace_stats(2, 119.4, cpus=1)}
        for name, trace in cases.items():
            _, run = self.analyse(trace, tracer_output=SESSION)
            st = ta.steps_by_id(run)
            self.assertEqual(run["sections_span_from"], ["ftrace events"], name)
            events_lo = 104.0 if name == "start-now-zero" else 100.0
            self.assertEqual(run["sections_span_s"], [events_lo, 111.01], name)
            self.assertFalse(st["idle2"]["sections_covered"], name)
            self.assertFalse(st["open"]["sections_covered"], name)


class FrameTimelineCountTest(unittest.TestCase):
    def test_frame_timeline_packets_are_counted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.pftrace"
            path.write_bytes(tp.build_trace())
            self.assertEqual(pt.read_trace(str(path))["frametimeline_packets"], 8)
            path.write_bytes(android11_trace(100.0, 101.0))
            self.assertEqual(pt.read_trace(str(path))["frametimeline_packets"], 0)


class TimeStats11Test(ta.Base):
    def test_layers_without_a_uid_line(self):
        stats = u.parse_timestats(sf11(130, 40, 5))
        self.assertEqual(stats["total"], 130)
        self.assertEqual(stats["layers"], {(None, f"{PKG}/{PKG}.Main#0"): 40,
                                           (None, f"SurfaceView - {PKG}/{PKG}.Main#0"): 5,
                                           (None, "NavigationBar0#0"): 3})
        # the app's uid is known, the layers' is not: the name decides
        self.assertEqual(u.app_layer_frames(stats, PKG, 10100), {f"{PKG}/{PKG}.Main#0": 40,
                                                                  f"SurfaceView - {PKG}/{PKG}.Main#0": 5})

    def test_the_per_layer_count_is_known_on_android_11(self):
        files = ta.healthy_files()
        for sid in ta.ORDER:
            files[f"sf-{sid}.txt"] = sf11(ta.SF_TOTAL[sid], ta.SF_APP[sid])
        run = ta.Capture(self.root, files).analyze()
        st = ta.steps_by_id(run)
        self.assertTrue(run["compositor_stats"])
        self.assertEqual([st[sid]["frames_layers"] for sid in ta.ORDER], [30, 0, 12, 0])
        self.assertEqual(st["idle"]["frames_display"], 10)

    def test_the_format_with_uid_lines_gives_what_it_gave(self):
        stats = u.parse_timestats(ta.sf_text(100, 30))
        self.assertEqual(stats, {"total": 100, "layers": {(ta.UID, f"{PKG}/{PKG}.Main#1"): 30, (1000, "StatusBar#2"): 5}})


if __name__ == "__main__":
    unittest.main()
