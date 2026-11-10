"""Minimal reader for Perfetto protobuf traces: FrameTimeline events, process names, packet counts.

It reads only the packet types named below, so the probe needs no external trace tool. Field
numbers were read from the Perfetto protobuf definitions (see PROVENANCE.md), and its counts were
checked against the official trace processor on real traces.

No third-party imports. Works on any trace that contains the android.surfaceflinger.frametimeline
data source; knows nothing about any particular app.
"""
from __future__ import annotations

import sys
from collections import defaultdict

# TracePacket field numbers (perfetto.protos.TracePacket)
F_FTRACE_EVENTS = 1
F_PROCESS_TREE = 2
F_TIMESTAMP = 8
F_TRACE_STATS = 35
F_FRAME_TIMELINE_EVENT = 76
F_NETWORK_PACKET = 88
F_NETWORK_PACKET_BUNDLE = 92

JANK_BITS = {
    1: "None", 2: "SurfaceFlinger Scheduling", 4: "Prediction Error", 8: "Display HAL",
    16: "SurfaceFlinger CPU Deadline Missed", 32: "SurfaceFlinger GPU Deadline Missed",
    64: "App Deadline Missed", 128: "Buffer Stuffing", 256: "Unknown Jank",
    512: "SurfaceFlinger Stuffing", 1024: "Dropped Frame",
}
PRESENT = {0: "Unspecified", 1: "On-time", 2: "Late", 3: "Early", 4: "Dropped", 5: "Unknown"}
PREDICTION = {0: "Unspecified", 1: "Valid", 2: "Expired", 3: "Unknown"}
SEVERITY = {0: "Unknown", 1: "None", 2: "Partial", 3: "Full"}


class TraceError(ValueError):
    """The trace is malformed (or cut short inside a packet)."""


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    out = 0
    while True:
        if i >= len(buf):
            raise TraceError(f"truncated varint at {i}")
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, i
        shift += 7


def fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt in (1, 2, 5):
            if wt == 2:
                ln, i = _varint(buf, i)
            else:
                ln = 8 if wt == 1 else 4
            if i + ln > n:
                raise TraceError(f"field {fno} runs past the end of its message at {i}")
            val = buf[i:i + ln] if wt == 2 else int.from_bytes(buf[i:i + ln], "little")
            i += ln
        else:
            raise TraceError(f"unsupported wire type {wt} at {i}")
        yield fno, wt, val


def packets(data: bytes) -> tuple[list[bytes], bool]:
    """The trace's packets, and whether the file ends inside one (a trace cut short). Everything
    before the cut is returned; a malformed packet in the middle still raises TraceError."""
    out = []
    try:
        for fno, wt, pkt in fields(data):
            if fno == 1 and wt == 2:
                out.append(pkt)
    except TraceError:
        return out, True
    return out, False


def buffer_loss(stats: bytes) -> dict:
    """The data-loss counters of one TraceStats message, summed over its buffers.

    Field numbers: TraceStats{buffer_stats=1, flushes_failed=14, final_flush_outcome=15},
    BufferStats{chunks_overwritten=3, patches_failed=6, chunks_discarded=18,
    trace_writer_packet_loss=19}. The definitions call overwritten and discarded chunks "loss of
    data"; a writer reports its own packet loss; and the trace processor lists failed patches
    among a trace's data losses."""
    out = {"chunks_overwritten": 0, "chunks_discarded": 0, "patches_failed": 0, "writer_packet_loss": 0,
           "flushes_failed": 0, "final_flush": None}
    names = {3: "chunks_overwritten", 6: "patches_failed", 18: "chunks_discarded", 19: "writer_packet_loss"}
    for f, w, v in fields(stats):
        if f == 1 and w == 2:
            for bf, bw, bv in fields(v):
                if bw == 0 and bf in names:
                    out[names[bf]] += bv
        elif f == 14 and w == 0:
            out["flushes_failed"] = v
        elif f == 15 and w == 0:
            out["final_flush"] = v
    return out


def jank_names(mask: int) -> list[str]:
    if mask == 0:
        return ["Unspecified"]
    return [name for bit, name in JANK_BITS.items() if mask & bit]


def read_trace(path: str) -> dict:
    """Return {'surface_frames': [...], 'display_frames': [...], 'processes': {pid: cmdline},
    'network': {uid: {'packets': n, 'bytes': n}}, 'first_ts', 'last_ts', 'truncated', 'stats'}.
    Timestamps are nanoseconds on the trace clock; first_ts and last_ts are the earliest and latest
    packet timestamps (None in a trace without any), the span the trace actually holds.

    'stats' holds the service's own data-loss counters (see buffer_loss), each the largest value
    over the trace's statistics packets (they are cumulative), and 'packets', how many statistics
    packets the trace holds. None of the timestamps reveals a ring buffer that overwrote its oldest
    data, because the service's start and stop events are written at read time; the counters do."""
    with open(path, "rb") as fh:
        data = fh.read()
    open_frames: dict[int, dict] = {}
    surface, display = [], []
    processes: dict[int, str] = {}
    net = defaultdict(lambda: {"packets": 0, "bytes": 0})
    pkts, truncated = packets(data)
    first_ts = last_ts = None
    stats = {"packets": 0, "chunks_overwritten": 0, "chunks_discarded": 0, "patches_failed": 0,
             "writer_packet_loss": 0, "flushes_failed": 0, "final_flush": None}
    for pkt in pkts:
        ts = None
        ftl = None
        for f, w, v in fields(pkt):
            if f == F_TIMESTAMP and w == 0:
                ts = v
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            elif f == F_FRAME_TIMELINE_EVENT and w == 2:
                ftl = v
            elif f == F_TRACE_STATS and w == 2:
                one = buffer_loss(v)
                stats["packets"] += 1
                for key in ("chunks_overwritten", "chunks_discarded", "patches_failed", "writer_packet_loss",
                            "flushes_failed"):
                    stats[key] = max(stats[key], one[key])
                if one["final_flush"] is not None:
                    stats["final_flush"] = one["final_flush"]
            elif f == F_PROCESS_TREE and w == 2:
                for pf, pw, pv in fields(v):
                    if pf == 1 and pw == 2:  # ProcessTree.processes
                        pid, cmd = None, []
                        for qf, qw, qv in fields(pv):
                            if qf == 1 and qw == 0:
                                pid = qv
                            elif qf == 3 and qw == 2:
                                cmd.append(qv.decode("utf-8", "replace"))
                        if pid is not None and cmd:
                            processes[pid] = cmd[0]
            elif f == F_NETWORK_PACKET and w == 2:
                uid, length = None, 0
                for nf, nw, nv in fields(v):
                    if nf == 4 and nw == 0:
                        uid = nv
                    elif nf == 3 and nw == 0:
                        length = nv
                net[uid]["packets"] += 1
                net[uid]["bytes"] += length
        if ftl is None:
            continue
        for ef, ew, ev in fields(ftl):
            if ew != 2:
                continue
            row = {"ts": ts}
            vals = {f2: v2 for f2, w2, v2 in fields(ev)}
            if ef == 1:    # ExpectedDisplayFrameStart
                row.update(kind="expected_display", token=vals.get(2), pid=vals.get(3))
            elif ef == 2:  # ActualDisplayFrameStart
                row.update(kind="actual_display", token=vals.get(2), pid=vals.get(3),
                           present=PRESENT.get(vals.get(4, 0)), on_time_finish=bool(vals.get(5, 0)),
                           gpu_composition=bool(vals.get(6, 0)), jank=jank_names(vals.get(7, 0)),
                           prediction=PREDICTION.get(vals.get(8, 0)), severity=SEVERITY.get(vals.get(9, 0)))
            elif ef == 3:  # ExpectedSurfaceFrameStart
                row.update(kind="expected_surface", token=vals.get(2), display_token=vals.get(3),
                           pid=vals.get(4), layer=(vals.get(5) or b"").decode("utf-8", "replace"))
            elif ef == 4:  # ActualSurfaceFrameStart
                row.update(kind="actual_surface", token=vals.get(2), display_token=vals.get(3),
                           pid=vals.get(4), layer=(vals.get(5) or b"").decode("utf-8", "replace"),
                           present=PRESENT.get(vals.get(6, 0)), on_time_finish=bool(vals.get(7, 0)),
                           gpu_composition=bool(vals.get(8, 0)), jank=jank_names(vals.get(9, 0)),
                           prediction=PREDICTION.get(vals.get(10, 0)), is_buffer=bool(vals.get(11, 0)),
                           severity=SEVERITY.get(vals.get(12, 0)))
            elif ef == 5:  # FrameEnd
                cookie = vals.get(1)
                started = open_frames.pop(cookie, None)
                if started is not None:
                    started["end"] = ts
                    (surface if started["kind"] == "actual_surface" else display).append(started)
                continue
            else:
                continue
            row["start"] = ts
            if row["kind"].startswith("actual"):
                open_frames[vals.get(1)] = row
    return {"packets": len(pkts), "surface_frames": surface, "display_frames": display,
            "processes": processes, "network": {str(k): v for k, v in net.items()},
            "unterminated": len(open_frames), "first_ts": first_ts, "last_ts": last_ts, "truncated": truncated,
            "stats": stats}


def read_atrace(path: str, info: dict | None = None) -> list[dict]:
    """Userspace trace sections (android.os.Trace / ATRACE) from the ftrace print events of a trace.

    Returns begin events as {'ts', 'tid', 'tgid', 'name', 'dur'} (dur in ns, None if unterminated)
    and counters/instants are ignored. Field numbers: TracePacket.ftrace_events=1,
    FtraceEventBundle{event=2, lost_events=3}, FtraceEvent{timestamp=1,pid=2,print=3},
    PrintFtraceEvent.buf=2.

    If info is given it receives 'bundles' and 'lost_bundles': how many event bundles the trace
    holds and how many of them report that the kernel dropped events before them."""
    with open(path, "rb") as fh:
        data = fh.read()
    out = []
    stacks: dict[int, list[dict]] = {}
    events = []
    bundles = lost = 0
    for pkt in packets(data)[0]:
        for f, w, v in fields(pkt):
            if f != F_FTRACE_EVENTS or w != 2:
                continue
            bundles += 1
            for bf, bw, bv in fields(v):
                if bf == 3 and bw == 0 and bv:
                    lost += 1
                if bf != 2 or bw != 2:
                    continue
                ts = tid = None
                buf = None
                for ef, ew, ev in fields(bv):
                    if ef == 1 and ew == 0:
                        ts = ev
                    elif ef == 2 and ew == 0:
                        tid = ev
                    elif ef == 3 and ew == 2:
                        for pf, pw, pv in fields(ev):
                            if pf == 2 and pw == 2:
                                buf = pv
                if buf is not None and ts is not None:
                    events.append((ts, tid, buf))
    events.sort(key=lambda e: e[0])
    for ts, tid, buf in events:
        text = buf.decode("utf-8", "replace").rstrip("\n")
        if text.startswith("B|"):
            parts = text.split("|", 2)
            if len(parts) == 3 and parts[1].isdigit():
                row = {"ts": ts, "tid": tid, "tgid": int(parts[1]), "name": parts[2], "dur": None}
                stacks.setdefault(tid, []).append(row)
                out.append(row)
        elif text.startswith("E"):
            st = stacks.get(tid)
            if st:
                row = st.pop()
                row["dur"] = ts - row["ts"]
    if info is not None:
        info.update(bundles=bundles, lost_bundles=lost)
    return out


def summarize_for_pid(trace: dict, pid: int) -> dict:
    """Counts for one process's presented surface frames. Counts are exact; durations are timings."""
    rows = [r for r in trace["surface_frames"] if r.get("pid") == pid]
    by_layer = defaultdict(int)
    by_jank = defaultdict(int)
    by_present = defaultdict(int)
    for r in rows:
        by_layer[r.get("layer", "")] += 1
        by_present[r.get("present")] += 1
        for j in r.get("jank", []):
            by_jank[j] += 1
    return {"surface_frames": len(rows), "by_layer": dict(by_layer), "by_jank_type": dict(by_jank),
            "by_present_type": dict(by_present)}


if __name__ == "__main__":
    import json
    tr = read_trace(sys.argv[1])
    out = {"packets": tr["packets"], "surface_frames": len(tr["surface_frames"]),
           "display_frames": len(tr["display_frames"]), "processes": len(tr["processes"]),
           "unterminated": tr["unterminated"], "network_uids": len(tr["network"]),
           "first_ts": tr["first_ts"], "last_ts": tr["last_ts"], "truncated": tr["truncated"], "stats": tr["stats"]}
    if len(sys.argv) > 2:
        needle = sys.argv[2]
        pids = [p for p, c in tr["processes"].items() if needle in c]
        out["matching_pids"] = {str(p): tr["processes"][p] for p in pids}
        frame_pids = sorted({r.get("pid") for r in tr["surface_frames"]})
        out["pids_with_surface_frames"] = frame_pids
        for p in pids:
            out[f"pid_{p}"] = summarize_for_pid(tr, p)
    print(json.dumps(out, indent=1))
