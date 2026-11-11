"""pftrace.py on a Perfetto trace built in memory with a minimal protobuf encoder."""
import sys

sys.dont_write_bytecode = True

import os
import struct
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pftrace as pt  # noqa: E402

PKG = "com.example.app"
APP_PID = 4321
LAYER = f"{PKG}/{PKG}.MainActivity#0"


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def vfield(fno, value):
    return varint(fno << 3 | 0) + varint(value)


def lfield(fno, payload):
    return varint(fno << 3 | 2) + varint(len(payload)) + payload


def packet(*parts, ts=None):
    body = b"".join(parts)
    if ts is not None:
        body = vfield(pt.F_TIMESTAMP, ts) + body
    return lfield(1, body)          # Trace.packet


def ftl(kind, *parts):
    return lfield(pt.F_FRAME_TIMELINE_EVENT, lfield(kind, b"".join(parts)))


def ftrace_event(ts, tid, text):
    return lfield(2, vfield(1, ts) + vfield(2, tid) + lfield(3, lfield(2, text.encode())))


JANK_MASK = 64 | 1024          # App Deadline Missed + Dropped Frame


def build_trace():
    proc_tree = lfield(pt.F_PROCESS_TREE,
                       lfield(1, vfield(1, APP_PID) + lfield(3, PKG.encode()) + lfield(3, b"--flag"))
                       + lfield(1, vfield(1, 1) + lfield(3, b"init"))
                       + lfield(1, vfield(1, 77)))                       # no cmdline: left out
    packets = [
        packet(proc_tree, ts=500),
        # ActualDisplayFrameStart: cookie 7, token 500, pid 999 (compositor), present On-time, jank None
        packet(ftl(2, vfield(1, 7), vfield(2, 500), vfield(3, 999), vfield(4, 1), vfield(5, 1), vfield(7, 1)), ts=1000),
        # ExpectedSurfaceFrameStart: never recorded as a frame, and its end is ignored
        packet(ftl(3, vfield(1, 10), vfield(2, 501), vfield(3, 500), vfield(4, APP_PID), lfield(5, LAYER.encode())), ts=1500),
        # ActualSurfaceFrameStart cookie 8: Late, jank mask, prediction Valid, is_buffer, severity Full
        packet(ftl(4, vfield(1, 8), vfield(2, 501), vfield(3, 500), vfield(4, APP_PID), lfield(5, LAYER.encode()),
                   vfield(6, 2), vfield(9, JANK_MASK), vfield(10, 1), vfield(11, 1), vfield(12, 3)), ts=2000),
        # ActualSurfaceFrameStart cookie 9: never ended
        packet(ftl(4, vfield(1, 9), vfield(2, 502), vfield(3, 600), vfield(4, APP_PID), lfield(5, LAYER.encode()),
                   vfield(6, 1), vfield(11, 1)), ts=2500),
        packet(ftl(5, vfield(1, 10)), ts=2900),
        packet(ftl(5, vfield(1, 8)), ts=3000),
        packet(ftl(5, vfield(1, 7)), ts=3500),
        packet(ftl(5, vfield(1, 12345)), ts=3600),                       # end without a start
        packet(lfield(pt.F_NETWORK_PACKET, vfield(3, 1500) + vfield(4, 10123)), ts=3700),
        packet(lfield(pt.F_NETWORK_PACKET, vfield(3, 500) + vfield(4, 10123)), ts=3800),
        # ftrace bundle (TracePacket.ftrace_events = 1), events deliberately out of time order
        packet(lfield(1,
                      ftrace_event(5600, 4322, "E|4321\n")
                      + ftrace_event(5000, 4322, "B|4321|Choreographer#doFrame 123\n")
                      + ftrace_event(5100, 4322, "B|4321|traversal")
                      + ftrace_event(5200, 4322, "E")
                      + ftrace_event(5300, 4323, "B|4321|draw")             # never ended
                      + ftrace_event(5400, 4323, "C|4321|counter|5")        # counter: ignored
                      + ftrace_event(5450, 4324, "B|x|not a pid"))),        # malformed: ignored
    ]
    return b"".join(packets)


class EncoderSanityTest(unittest.TestCase):
    def test_fields_roundtrip(self):
        msg = vfield(1, 300) + lfield(2, b"abc") + varint(3 << 3 | 1) + struct.pack("<Q", 2 ** 40) \
            + varint(4 << 3 | 5) + struct.pack("<I", 7)
        self.assertEqual(list(pt.fields(msg)), [(1, 0, 300), (2, 2, b"abc"), (3, 1, 2 ** 40), (4, 5, 7)])

    def test_unsupported_wire_type(self):
        with self.assertRaises(ValueError):
            list(pt.fields(varint(1 << 3 | 3)))


class JankNamesTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(pt.jank_names(0), ["Unspecified"])
        self.assertEqual(pt.jank_names(1), ["None"])
        self.assertEqual(pt.jank_names(JANK_MASK), ["App Deadline Missed", "Dropped Frame"])
        self.assertEqual(pt.jank_names(2 | 8), ["SurfaceFlinger Scheduling", "Display HAL"])


class TraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "trace.pftrace")
        with open(cls.path, "wb") as fh:
            fh.write(build_trace())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_read_trace(self):
        tr = pt.read_trace(self.path)
        self.assertEqual(tr["packets"], 12)
        self.assertEqual(tr["processes"], {APP_PID: PKG, 1: "init"})
        self.assertEqual(tr["unterminated"], 1)
        self.assertEqual(tr["network"], {"10123": {"packets": 2, "bytes": 2000}})

        self.assertEqual(len(tr["display_frames"]), 1)
        d = tr["display_frames"][0]
        self.assertEqual((d["kind"], d["start"], d["end"], d["token"], d["pid"]), ("actual_display", 1000, 3500, 500, 999))
        self.assertEqual(d["present"], "On-time")
        self.assertTrue(d["on_time_finish"])
        self.assertFalse(d["gpu_composition"])
        self.assertEqual(d["jank"], ["None"])

        self.assertEqual(len(tr["surface_frames"]), 1)
        s = tr["surface_frames"][0]
        self.assertEqual((s["kind"], s["start"], s["end"]), ("actual_surface", 2000, 3000))
        self.assertEqual((s["token"], s["display_token"], s["pid"], s["layer"]), (501, 500, APP_PID, LAYER))
        self.assertEqual(s["present"], "Late")
        self.assertEqual(s["jank"], ["App Deadline Missed", "Dropped Frame"])
        self.assertEqual(s["prediction"], "Valid")
        self.assertEqual(s["severity"], "Full")
        self.assertTrue(s["is_buffer"])
        self.assertFalse(s["on_time_finish"])

    def test_summarize_for_pid(self):
        out = pt.summarize_for_pid(pt.read_trace(self.path), APP_PID)
        self.assertEqual(out, {"surface_frames": 1, "by_layer": {LAYER: 1},
                               "by_jank_type": {"App Deadline Missed": 1, "Dropped Frame": 1},
                               "by_present_type": {"Late": 1}})
        self.assertEqual(pt.summarize_for_pid(pt.read_trace(self.path), 1)["surface_frames"], 0)

    def test_read_atrace(self):
        begins = pt.read_atrace(self.path)
        self.assertEqual([(b["ts"], b["tid"], b["tgid"], b["name"], b["dur"]) for b in begins], [
            (5000, 4322, APP_PID, "Choreographer#doFrame 123", 600),
            (5100, 4322, APP_PID, "traversal", 100),
            (5300, 4323, APP_PID, "draw", None),
        ])

    def test_span(self):
        """Finding 14: the trace records the first and last packet times it holds."""
        tr = pt.read_trace(self.path)
        self.assertEqual((tr["first_ts"], tr["last_ts"], tr["truncated"]), (500, 3800, False))

    def test_cut_short(self):
        """Finding 17: a trace cut inside its last packet is read up to the cut, not a crash."""
        p = os.path.join(self.tmp.name, "short.pftrace")
        with open(p, "wb") as fh:
            fh.write(build_trace()[:-3])
        tr = pt.read_trace(p)
        self.assertTrue(tr["truncated"])
        self.assertEqual(tr["packets"], 11)
        self.assertEqual(tr["last_ts"], 3800)
        self.assertEqual(pt.read_atrace(p), [])     # the ftrace bundle was the packet cut short

    def test_malformed_inside_a_packet(self):
        with self.assertRaises(pt.TraceError):
            list(pt.fields(b"\x80"))                 # a varint that never ends
        with self.assertRaises(pt.TraceError):
            list(pt.fields(lfield(2, b"abc")[:-1]))  # a length past the end
        with self.assertRaises(pt.TraceError):
            list(pt.fields(varint(3 << 3 | 1) + b"\x00\x00"))   # a fixed64 past the end
        self.assertEqual(pt.packets(packet(vfield(8, 5)) + b"\x0a\x09\x40"), ([vfield(8, 5)], True))

    def test_a_trace_without_statistics(self):
        self.assertEqual(pt.read_trace(self.path)["stats"],
                         {"packets": 0, "chunks_overwritten": 0, "chunks_discarded": 0, "patches_failed": 0,
                          "writer_packet_loss": 0, "flushes_failed": 0, "final_flush": None})

    def test_loss_counters_are_summed_over_buffers(self):
        """TraceStats{buffer_stats=1, flushes_failed=14, final_flush_outcome=15};
        BufferStats{chunks_overwritten=3, patches_failed=6, chunks_discarded=18, trace_writer_packet_loss=19}."""
        stats = (lfield(1, vfield(12, 65536) + vfield(3, 4) + vfield(18, 1) + vfield(2, 900) + vfield(5, 77))
                 + lfield(1, vfield(3, 2) + vfield(19, 5) + vfield(6, 9))
                 + vfield(14, 1) + vfield(15, 2))
        p = os.path.join(self.tmp.name, "stats.pftrace")
        with open(p, "wb") as fh:
            fh.write(build_trace() + packet(lfield(pt.F_TRACE_STATS, stats), ts=4000))
        self.assertEqual(pt.read_trace(p)["stats"],
                         {"packets": 1, "chunks_overwritten": 6, "chunks_discarded": 1, "patches_failed": 9,
                          "writer_packet_loss": 5, "flushes_failed": 1, "final_flush": 2})

    def test_loss_counters_are_cumulative_across_statistics_packets(self):
        first = lfield(1, vfield(3, 2)) + vfield(15, 0)
        last = lfield(1, vfield(3, 5)) + vfield(15, 1)
        p = os.path.join(self.tmp.name, "stats2.pftrace")
        with open(p, "wb") as fh:
            fh.write(packet(lfield(pt.F_TRACE_STATS, first), ts=10) + packet(lfield(pt.F_TRACE_STATS, last), ts=20))
        st = pt.read_trace(p)["stats"]
        self.assertEqual((st["packets"], st["chunks_overwritten"], st["final_flush"]), (2, 5, 1))

    def test_events_the_kernel_dropped(self):
        """FtraceEventBundle.lost_events = 3: set on a bundle when the kernel dropped events before it."""
        bundle = lambda lost: packet(lfield(pt.F_FTRACE_EVENTS, vfield(1, 0) + ftrace_event(100, 7, "B|4321|draw")
                                            + (vfield(3, 1) if lost else b"")))
        p = os.path.join(self.tmp.name, "lost.pftrace")
        with open(p, "wb") as fh:
            fh.write(bundle(False) + bundle(True) + bundle(False))
        info = {}
        self.assertEqual(len(pt.read_atrace(p, info)), 3)
        self.assertEqual(info, {"bundles": 3, "lost_bundles": 1, "events_first_ts": 100, "events_last_ts": 100})
        info = {}
        pt.read_atrace(self.path, info)
        self.assertEqual(info, {"bundles": 1, "lost_bundles": 0, "events_first_ts": 5000, "events_last_ts": 5600})

    def test_empty_trace(self):
        p = os.path.join(self.tmp.name, "empty.pftrace")
        open(p, "wb").close()
        tr = pt.read_trace(p)
        self.assertEqual((tr["packets"], tr["surface_frames"], tr["unterminated"]), (0, [], 0))
        self.assertEqual((tr["first_ts"], tr["last_ts"], tr["truncated"]), (None, None, False))
        self.assertEqual(pt.read_atrace(p), [])


if __name__ == "__main__":
    unittest.main()
