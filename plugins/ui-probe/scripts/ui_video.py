"""Turn a screen recording into evidence an agent can read, and into numbers.

App-agnostic. Input is an Android `screenrecord` file, which holds one video frame per screen
change (the recorder writes nothing while the screen is still). Output per scenario step:

  * counts        : video frames, frames that changed the app region, motion bursts
  * a signature   : the class of each burst, in order (see classify_burst)
  * timings       : first and last visible change after the step started, the longest freeze
                    inside a burst (all statistical)
  * a contact sheet PNG per step, labelled with time, gap and the matched platform frame duration

Burst classes, from the frames alone:
  T   transition         the screen ends in a different state and every frame lies on the way there
  TD  transition+detour  it ends in a different state but passed through frames that resemble
                         neither the start nor the end (a skeleton, a blank, a flash)
  E   excursion          it left its state and came back to it (flicker, a replayed animation,
                         a press ripple)
  t   minor transition   a small change that stays (a highlight moves); counted, not in the signature
  m   micro              a change too small to be a state change; counted, not in the signature
  M   motion             a swipe step: frames are compared by continuity, not by state. A swipe
                         marked "judge": "state" in the scenario is classed like a tap instead
  s   system only        only the status or navigation bar changed
  n   noise              a difference below the encoder-noise floor; counted, never part of a signature

Shared with region allowances, `film` and `sheet --full`: full-resolution frames decoded on demand
(iter_frames), a block map of what changed (blocks of BLOCK_OF_WIDTH of the width), places of a
change as boxes in the app's area, and regions given as fractions of that area. analyze_film reads a
film no run made, recorded at a constant frame rate: a frame of it changed only where it holds a
connected group of changed pixels at least FILM_GROUP_SHARE of the picture.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

THUMB_W = 270
BURST_GAP_S = 0.35      # a pause longer than this ends a burst
STATE_TAU = 1.0         # mean absolute grey-level difference that counts as a different state
MICRO_TAU = 0.25        # below this an excursion is "micro"
MAJOR = ("T", "TD", "E", "M")   # the classes a signature is made of
NOISE_TAU = 0.05        # below this a burst is encoder noise: it is counted but left out of the signature
DETOUR_RATIO = 1.10     # (dist to start + dist to end) / dist(start, end) above this is a detour.
                        # Calibrated on two observed cases only, in one app: cross-fade frames measured
                        # 1.00-1.02, skeleton and placeholder frames 1.14-1.29. A tunable, not a constant.
CHANGED_PIXEL = 12      # grey-level delta for a pixel to count toward a change bounding box
VISIBLE_PIXEL = 4       # grey-level delta for a pixel to count as visibly different
VISIBLE_COUNT = 6       # this many such pixels (at thumbnail scale) make a frame "changed"
CHANGE_LEVEL = 8        # a pixel changed by this much on any colour channel counts in a frame's change measures
FLAT_RANGE = 8          # a pixel whose 5x5 neighbourhood in the previous frame spans less than this lies in a flat area
FLAT_CHECK_ABOVE = 32   # the flat-area count is taken only for frames whose largest change is at most this
ALIGN_TOL_S = 0.020
MATCH_WINDOW_S = 0.070    # a video frame is matched to an app frame completed at most this long before it

# The block map, at full resolution. A block's side is a fraction of the picture's width (24 px of
# 1080), so one rule serves any resolution. A pixel counts above BLOCK_PIXEL_LEVEL levels on any
# colour channel; a block counts when BLOCK_SHARE of its pixels count. Decisions use the change
# measures; blocks say where a change is.
BLOCK_OF_WIDTH = 24 / 1080
BLOCK_PIXEL_LEVEL = 10
BLOCK_SHARE = 0.40
# A film no run made (`ui-probe film`). Measured on one phone capture (H.264, 60 a second, 1206 x
# 2622): no two consecutive frames are bit-identical; at rest an ordinary frame has about 13 pixels
# over 10 levels (up to 135), a key frame (every 30 frames, two changed pairs each) 730 to 1,340,
# scattered, with no block at 40 %. The largest 8-connected group of such pixels was 32 px on an
# ordinary frame and 36 px on a key frame (48 px at 6 levels); a real swipe put 690,000 to 780,000
# pixels over the level. So a film's frame changed only where a connected group of pixels over the
# level reaches FILM_GROUP_SHARE of the picture (96 px there, twice the noise's largest group at 6
# levels); anything smaller is scattered, and the recording's.
FILM_LEVEL = 10               # a pixel of a film counts above this many levels on any colour channel
FILM_GROUP_SHARE = 3e-5       # the smallest 8-connected group of such pixels that is a change, of the picture
FILM_CAUTION = 0.7            # a scattered group this large against the minimum earns a caution
FILM_STEADY_SHARE = 0.25      # pixels over the level in this share of a window's frames are reported as places
# A change slower than the minimum a frame (a fade of a few levels a frame, a bar that grows a few
# pixels a frame) never shows between neighbours, so each frame is also compared with the frame
# FILM_SLOW_S earlier (30 frames at 60 a second, half a key-frame interval apart or more), and each
# window's last frame with its first, by the same group rule.
FILM_SLOW_S = 0.5
FILM_EXPLAIN_PX = 3           # a slow change within this many pixels of a counted change is that change
# A change outside the control that was touched (a step's `scope`): decided by the film's group rule
# against the last frame before the input. Defaults measured on a 1080 x 2400 emulator: a control that
# moves 8 px (held as a fraction of the width) or a band beside it that slides as far, a change outside
# that lasts 50 ms (three frames at 60 a second) before it comes back.
OUTSIDE_MOVE_PX = 8           # at 1080 px wide
OUTSIDE_SEARCH_PX = 300       # the farthest shift searched, vertically and horizontally
OUTSIDE_CAME_BACK_MS = 50
OUTSIDE_STILL_SCORE = 2.0     # mean change (levels) at no shift above which content may have moved
OUTSIDE_MATCH_RATIO = 0.5     # a shift that leaves at most this share of that change is where it went
OUTSIDE_SLIDE_GROUPS = 3      # the largest groups of a frame searched for a slide
OUTSIDE_MATCH_MAX = 4.0       # and leaves at most this mean change (levels): content that moved matches closely
OUTSIDE_JOIN_PX = 3           # pixels over the level this close (a closing by 3 px) join before grouping:
                              # a line of text that changes is one group, scattered encoder noise is not
OUTSIDE_JOIN_LEVEL = 30       # only pixels changed by more than this join within the closing; a redraw changes
                              # its pixels far more, encoder noise on dense text (libx264 at crf 23) mostly less
OUTSIDE_SETTLE_MS = 500       # the screen must not change outside the control in this long before the input
OUTSIDE_TEXTURE = 4.0         # a patch whose grey spreads less than this (standard deviation) has no shift to find
PER_PAGE = 24                 # frames on one page of a distinct sheet
PAGE_COLS = 6


class VideoError(RuntimeError):
    """The recording could not be analysed: a tool failed or an input the analysis needs is missing."""


class UnusableRecording(VideoError):
    """The recording exists but holds nothing to analyse (no video frames)."""


def _run(cmd: list[str]) -> str:
    """Run ffmpeg or ffprobe; any failure becomes a VideoError that says which tool and why."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except OSError as exc:
        raise VideoError(f"cannot run {cmd[0]}: {exc.strerror or exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["no message"]
        hint = " (an ffmpeg older than 5.1 lacks -fps_mode; `ui-probe doctor` checks the version)" \
            if cmd[0] == "ffmpeg" else ""
        raise VideoError(f"{cmd[0]} failed with status {exc.returncode}: {detail[0][:300]}{hint}") from exc


def probe_pts(video: Path) -> list[float]:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                "-of", "csv=p=0", str(video)])
    return [float(x.split(",")[0]) for x in out.split() if x and x[0].isdigit()]


def extract(video: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("f*.png"):
        old.unlink()
    _run(["ffmpeg", "-v", "error", "-i", str(video), "-fps_mode", "passthrough",
          "-vf", f"scale={THUMB_W}:-1", str(dest / "f%05d.png")])
    return sorted(dest.glob("f*.png"))


def recorder_timestamps(video: Path) -> list[float] | None:
    """Per-frame device timestamps (seconds, elapsed-realtime clock) that screenrecord embeds for
    Winscope. Layout read from a real capture on API 34: v2 = magic, u32 version, i64 realtime-to-
    elapsed offset (ns), u32 count, count x i64 ns; v1 = magic, u32 count, count x i64 microseconds."""
    data = video.read_bytes()
    try:
        i = data.find(b"#VV1NSC0PET1ME2#")
        if i >= 0:
            version, _offset, n = struct.unpack_from("<IqI", data, i + 16)
            if version == 2 and 0 < n < 10_000_000:
                return [t / 1e9 for t in struct.unpack_from(f"<{n}q", data, i + 16 + 16)]
        j = data.find(b"#VV1NSC0PET1ME!#")
        if j >= 0:
            (n,) = struct.unpack_from("<I", data, j + 16)
            if 0 < n < 10_000_000:
                return [t / 1e6 for t in struct.unpack_from(f"<{n}q", data, j + 20)]
    except struct.error:
        return None   # the track is cut short (a truncated recording): fall back to matching
    return None


def visibly_changed(a: np.ndarray, b: np.ndarray) -> bool:
    """True when two frames differ by more than encoder noise.

    A frame that follows a long still period can carry scattered single-pixel differences from the
    video encoder even where nothing was redrawn (observed: 255 to 462 isolated pixels across the
    app area when only a status-bar icon changed). A real change is locally dense, so a
    pixel counts only if at least three pixels in its 3x3 neighbourhood changed."""
    mask = (np.abs(a - b) > VISIBLE_PIXEL).astype(np.int16)
    if int(mask.sum()) < VISIBLE_COUNT:
        return False
    padded = np.pad(mask, 1)
    h, w = mask.shape
    dense = sum(padded[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))
    return int(np.sum((dense >= 3) & (mask == 1))) >= VISIBLE_COUNT


def change_measures(prev: np.ndarray | None, cur: np.ndarray) -> dict:
    """How a frame differs from the one before it, at the analysis scale and on the colour channel
    that changed most: the largest change of any pixel ("max_delta"), how many pixels changed by
    CHANGE_LEVEL or more ("changed_px"), and how many of those lie where the previous picture was
    flat ("flat_changed_px": new content on an even area, which an encoder refining a picture along
    its existing edges does not produce). The flat count is None above FLAT_CHECK_ABOVE, where it is
    not needed."""
    if prev is None:
        return {"max_delta": 0.0, "changed_px": 0, "flat_changed_px": 0}
    dc = np.abs(cur - prev).max(axis=2)
    largest = float(dc.max())
    changed = dc >= CHANGE_LEVEL
    flat_px = None
    if largest <= FLAT_CHECK_ABOVE:
        g = prev.max(axis=2)
        h, w = g.shape
        pad = np.pad(g, 2, mode="edge")
        hi = np.full_like(g, -1)
        lo = np.full_like(g, 10 ** 4)
        for dy in range(5):
            for dx in range(5):
                win = pad[dy:dy + h, dx:dx + w]
                hi = np.maximum(hi, win)
                lo = np.minimum(lo, win)
        flat_px = int((changed & ((hi - lo) < FLAT_RANGE)).sum())
    return {"max_delta": round(largest, 1), "changed_px": int(changed.sum()), "flat_changed_px": flat_px}


def dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a - b)))


def bbox(a: np.ndarray, b: np.ndarray):
    changed = np.abs(a - b) > CHANGED_PIXEL
    if not changed.any():
        return None
    ys, xs = np.where(changed)
    h, w = a.shape
    return [round(float(xs.min()) / w, 3), round(float(ys.min()) / h, 3),
            round(float(xs.max() + 1) / w, 3), round(float(ys.max() + 1) / h, 3)]


def align(pts: list[float], completed_s: list[float]) -> tuple[float | None, int]:
    """Offset such that pts + offset is device uptime, chosen to match the most platform frames.

    Every (video frame, platform frame) pair proposes an offset; the offset that puts the most
    video frames within the tolerance of some platform frame wins. The recording also contains
    frames the app did not draw (launcher, splash, system bars), so no pairing is assumed."""
    if len(pts) < 3 or len(completed_s) < 2:
        return None, 0
    fc = np.array(sorted(completed_s))
    v = np.array(pts[1:])
    cands = (fc[None, :] - v[:, None]).ravel()
    cands = np.unique(np.round(cands, 3))
    best, best_score = None, -1
    for off in cands:
        shifted = v + off
        idx = np.clip(np.searchsorted(fc, shifted), 1, len(fc) - 1)
        near = np.minimum(np.abs(fc[idx] - shifted), np.abs(fc[idx - 1] - shifted))
        score = int(np.sum(near < ALIGN_TOL_S))
        if score > best_score:
            best, best_score = off, score
    # refine: median residual of the matched frames
    shifted = v + best
    idx = np.clip(np.searchsorted(fc, shifted), 1, len(fc) - 1)
    lo, hi = fc[idx - 1] - shifted, fc[idx] - shifted
    resid = np.where(np.abs(lo) < np.abs(hi), lo, hi)
    resid = resid[np.abs(resid) < ALIGN_TOL_S]
    if len(resid):
        best = best + float(np.median(resid))
    return float(best), best_score


def classify_burst(kind: str, frames: list[dict], before: np.ndarray, app: list[np.ndarray]) -> dict:
    last = app[frames[-1]["i"]]
    d_ab = dist(before, last)
    app_changed = [f for f in frames if f["app_changed"]]
    info = {"n": len(frames), "start_s": frames[0]["t"], "end_s": frames[-1]["t"],
            "duration_ms": round((frames[-1]["t"] - frames[0]["t"]) * 1000, 1),
            "d_start_end": round(d_ab, 3), "max_gap_ms": 0.0, "gaps_over_34ms": 0,
            "detour_frames": 0, "detour_ms": 0.0, "excursion_max": 0.0,
            "bbox": bbox(before, last)}
    gaps = [(frames[k + 1]["t"] - frames[k]["t"]) * 1000 for k in range(len(frames) - 1)]
    if gaps:
        info["max_gap_ms"] = round(max(gaps), 1)
        info["gaps_over_34ms"] = sum(1 for g in gaps if g > 34)
    if not app_changed:
        info["class"] = "s"
        return info
    if kind == "swipe":
        info["class"] = "M"
        return info
    d_a = [dist(app[f["i"]], before) for f in frames]
    d_b = [dist(app[f["i"]], last) for f in frames]
    info["excursion_max"] = round(max(d_a), 3)
    if d_ab >= STATE_TAU:
        for k, f in enumerate(frames[:-1]):
            ratio = (d_a[k] + d_b[k]) / d_ab
            f["detour_ratio"] = round(ratio, 2)
            if ratio > DETOUR_RATIO and d_a[k] >= STATE_TAU and d_b[k] >= STATE_TAU:
                info["detour_frames"] += 1
                info["detour_ms"] += (frames[k + 1]["t"] - f["t"]) * 1000
        info["detour_ms"] = round(info["detour_ms"], 1)
        info["class"] = "TD" if info["detour_frames"] else "T"
    elif max(d_a) >= MICRO_TAU and d_ab <= 0.5 * max(d_a):
        # It went somewhere and came most of the way back: an excursion.
        info["class"] = "E"
        peak = int(np.argmax(d_a))
        info["bbox"] = bbox(before, app[frames[peak]["i"]])
    elif d_ab >= MICRO_TAU:
        # A small change that stayed (a highlight moving, a badge appearing): a minor transition.
        info["class"] = "t"
    elif max(d_a) >= NOISE_TAU:
        info["class"] = "m"
    else:
        info["class"] = "n"
    return info


def sheet(path: Path, files: list[Path], frames: list[dict], title: str, cols: int = 8):
    if not frames:
        return
    if len(frames) > 48:   # keep sheets readable: head, evenly spaced middle, tail
        keep = sorted(set(list(range(12)) + list(np.linspace(12, len(frames) - 13, 24).astype(int)) +
                          list(range(len(frames) - 12, len(frames)))))
        frames = [frames[k] for k in keep]
    w, h = Image.open(files[frames[0]["i"]]).size
    pad = 30
    rows = (len(frames) + cols - 1) // cols
    img = Image.new("RGB", (cols * (w + 6), rows * (h + pad) + 22), "white")
    d = ImageDraw.Draw(img)
    d.text((4, 4), title, fill="black")
    for n, f in enumerate(frames):
        x = (n % cols) * (w + 6)
        y = 22 + (n // cols) * (h + pad)
        img.paste(Image.open(files[f["i"]]).convert("RGB"), (x, y + pad))
        tag = f"#{f['i']} +{f['rel_ms']:.0f}ms gap {f['gap_ms']:.0f}"
        hw = f.get("hwui_ms")
        line2 = (f"render {hw:.0f}ms" if hw is not None else "no platform frame") + \
                (f" r={f['detour_ratio']}" if "detour_ratio" in f else "") + \
                (" [identical]" if f["identical"] else ("" if f["app_changed"] else " [bars only]"))
        d.text((x + 2, y + 2), tag, fill="black")
        d.text((x + 2, y + 15), line2, fill=(170, 0, 0) if (hw or 0) > 34 else (60, 60, 60))
    img.save(path)


def analyze(video: Path, out: Path, hwui_rows: list[dict], marks: dict, scn: dict, facts: dict,
            locate: str = "real") -> dict:
    """locate: which frames of a rest with `allow.regions` are placed in its regions, "real" (the
    frames that count as the app changing its area: Android) or "changed" (every frame that changed
    anything: the iOS Simulator, where each of them counts)."""
    pts = probe_pts(video)
    files = extract(video, out / "frames")
    n = min(len(pts), len(files))
    pts, files = pts[:n], files[:n]
    if n == 0:
        raise UnusableRecording("the recording holds no video frames")
    grey = [np.asarray(Image.open(f).convert("L"), dtype=np.float32) for f in files]
    rgb = [np.asarray(Image.open(f).convert("RGB"), dtype=np.int16) for f in files]
    h, w = grey[0].shape
    insets = facts.get("stable_insets_top_bottom") or [int(0.035 * facts["height"]), int(0.02 * facts["height"])]
    top = int(round(insets[0] * h / facts["height"]))
    bottom = h - int(round(insets[1] * h / facts["height"]))
    app = [g[top:bottom, :] for g in grey]

    completed = [r["FrameCompleted"] / 1e9 for r in hwui_rows if r.get("FrameCompleted", 0) > 0]
    stamps = recorder_timestamps(video)
    clock = "recorder-timestamps"
    score = 0
    if stamps is not None and len(stamps) >= n:
        times = stamps[:n]
        offset = times[0] - pts[0]
        aligned = True
    else:
        offset, score = align(pts, completed)
        aligned = offset is not None and score >= max(3, int(0.5 * min(n - 1, len(completed))))
        clock = "matched-to-platform-frames"
        if not aligned:
            # Nothing to align to: fall back to the recorder start mark (coarse, flagged; the gate
            # does not place such a recording into step windows).
            if "T0" not in marks:
                raise VideoError("the recording cannot be placed in time: the step marks have no T0 mark")
            offset = marks["T0"] + 0.3
            clock = "start-mark-fallback"
        times = [p + offset for p in pts]
    fc = np.array(sorted(completed)) if completed else np.array([])
    dur_by_fc = {r["FrameCompleted"] / 1e9: (r["FrameCompleted"] - r["IntendedVsync"]) / 1e6
                 for r in hwui_rows if r.get("FrameCompleted", 0) > 0}

    frames = []
    cursor = 0
    for i in range(n):
        t = times[i]
        changed = i > 0 and visibly_changed(app[i], app[i - 1])
        whole = i > 0 and visibly_changed(grey[i], grey[i - 1])
        rec = {"i": i, "pts": pts[i], "t": t, "gap_ms": (pts[i] - pts[i - 1]) * 1000 if i else 0.0,
               "app_changed": bool(changed), "identical": bool(i > 0 and not whole),
               **change_measures(rgb[i - 1] if i else None, rgb[i])}
        if aligned and len(fc):
            # The platform frame shown by this video frame is the latest one the app completed before
            # the compositor produced it (completion precedes composition by up to a few refreshes).
            # Both sequences are in time order, so match one to one in order: skip app frames too old
            # to be this video frame, then take the next app frame if it completed before this one.
            while cursor < len(fc) and t - fc[cursor] > MATCH_WINDOW_S:
                cursor += 1
            if cursor < len(fc) and fc[cursor] <= t + 0.002:
                rec["hwui_ms"] = round(dur_by_fc[float(fc[cursor])], 1)
                rec["completed_to_composed_ms"] = round((t - fc[cursor]) * 1000, 1)
                cursor += 1
        frames.append(rec)

    # The launch window ends where the first step begins: at its element lookup if it has one, so
    # that the lookup lies in no step's window.
    first = scn["steps"][0]
    launch_end = marks.get(first["id"] + (".find_start" if "find" in first else ".start"))
    order = [("launch", "launch", marks.get("launch.start"), launch_end)]
    for s in scn["steps"]:
        # A drag is judged as motion unless the scenario says the screen should end where it began
        # ("judge": "state": a pull that springs back, an overscroll, a cancelled dismiss).
        kind = "drag-state" if s["do"] == "swipe" and s.get("judge") == "state" else s["do"]
        order.append((s["id"], kind, marks.get(f"{s['id']}.start"), marks.get(f"{s['id']}.end")))
    (out / "sheets").mkdir(exist_ok=True)
    steps_out = []
    by_step = {s["id"]: s for s in scn["steps"]}
    to_place = []       # (step record, declared regions, [(frame index, ms since the step's start)])
    for sid, kind, start, end in order:
        if start is None or end is None:
            continue
        inside = [dict(f) for f in frames if start <= f["t"] < end and f["i"] > 0]
        for f in inside:
            f["rel_ms"] = (f["t"] - start) * 1000
        bursts, cur = [], []
        for f in inside:
            if cur and f["t"] - cur[-1]["t"] > BURST_GAP_S:
                bursts.append(cur)
                cur = []
            cur.append(f)
        if cur:
            bursts.append(cur)
        infos = []
        for b in bursts:
            before = app[b[0]["i"] - 1]
            infos.append(classify_burst(kind, b, before, app))
        app_frames = [f for f in inside if f["app_changed"]]
        # Frames that belong to a burst the classifier calls encoder noise or a system-bar change
        # are not the app changing: they are left out of the change times and of the at-rest count.
        real_idx = {f["i"] for b, info in zip(bursts, infos) if info["class"] not in ("s", "n") for f in b}
        real_frames = [f for f in app_frames if f["i"] in real_idx]
        steps_out.append({
            "id": sid, "kind": kind, "start_s": start,
            # the recording's frames in the window, by index (for `sheet --full`)
            "first_frame": inside[0]["i"] if inside else None, "last_frame": inside[-1]["i"] if inside else None,
            "video_frames": len(inside), "app_region_frames": len(app_frames),
            "identical_frames": sum(1 for f in inside if f["identical"]),
            "bursts": len([x for x in infos if x["class"] in MAJOR]),
            "signature": " ".join(x["class"] for x in infos if x["class"] in MAJOR) or "-",
            "minor_bursts": len([x for x in infos if x["class"] in ("t", "m")]),
            "system_only_bursts": len([x for x in infos if x["class"] == "s"]),
            "noise_bursts": len([x for x in infos if x["class"] == "n"]),
            "app_changed_frames_real": len(real_frames),
            "first_change_after_step_start_ms": round(real_frames[0]["rel_ms"], 1) if real_frames else None,
            "last_change_after_step_start_ms": round(real_frames[-1]["rel_ms"], 1) if real_frames else None,
            "max_gap_in_burst_ms": max([x["max_gap_ms"] for x in infos], default=0.0),
            "gaps_over_34ms": sum(x["gaps_over_34ms"] for x in infos),
            "detour_frames": sum(x["detour_frames"] for x in infos),
            "detour_ms": round(sum(x["detour_ms"] for x in infos), 1),
            "largest_change": max((f["max_delta"] for f in inside), default=0.0),
            "most_changed_px": max((f["changed_px"] for f in inside), default=0),
            # None when a frame changed too much for the flat-area count to be taken
            "most_flat_changed_px": (None if any(f["flat_changed_px"] is None for f in inside)
                                     else max((f["flat_changed_px"] for f in inside), default=0)),
            "burst_detail": infos,
        })
        sheet(out / "sheets" / f"{sid}.png", files, inside,
              f"{scn['name']} / step {sid} ({kind}) - {len(inside)} video frames, signature: {steps_out[-1]['signature']}")
        regions = ((by_step.get(sid) or {}).get("allow") or {}).get("regions")
        if kind == "rest" and regions:
            located = real_frames if locate == "real" else [f for f in inside if not f["identical"]]
            to_place.append((steps_out[-1], regions, [(f["i"], round(f["rel_ms"], 1)) for f in located]))
    if to_place:
        place_regions(video, to_place, facts)
    (out / "video-frames.json").write_text(json.dumps(frames, indent=0))
    matched = sum(1 for f in frames if "hwui_ms" in f)
    lat = sorted(f["completed_to_composed_ms"] for f in frames if "completed_to_composed_ms" in f)
    return {"frames": n, "clock": clock, "aligned": bool(aligned), "alignment_matches": score,
            "offset_s": round(offset, 4), "app_region_rows": [top, bottom],
            "video_frames_matched_to_platform_frames": matched, "platform_frames": len(completed),
            "completed_to_composed_ms_median": lat[len(lat) // 2] if lat else None,
            "steps": steps_out}


def zoom(video: Path, a: int, b: int, out: Path, margin: int = 24, scale: int = 3) -> dict:
    """Full-resolution look at what changed between two video frames (by index).

    Thumbnails cannot show a small change. This extracts the two frames at native resolution, finds
    the changed rectangle, and writes a magnified before / after / difference strip the agent can read."""
    tmp = out.parent / (out.stem + "-zoomtmp")
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.glob("z*.png"):
        old.unlink()
    if not video.exists():
        raise VideoError(f"{video} does not exist")
    _run(["ffmpeg", "-v", "error", "-i", str(video), "-fps_mode", "passthrough",
          "-vf", f"select='eq(n\\,{a})+eq(n\\,{b})'", str(tmp / "z%02d.png")])
    files = sorted(tmp.glob("z*.png"))
    if len(files) < 2:
        raise VideoError(f"could not extract both frames #{a} and #{b} from the recording")
    ia, ib = Image.open(files[0]).convert("RGB"), Image.open(files[1]).convert("RGB")
    ga = np.asarray(ia.convert("L"), dtype=np.float32)
    gb = np.asarray(ib.convert("L"), dtype=np.float32)
    diff = np.abs(ga - gb)
    changed = diff > VISIBLE_PIXEL
    if not changed.any():
        return {"changed_pixels": 0}
    ys, xs = np.where(changed)
    x0, x1 = max(0, xs.min() - margin), min(ga.shape[1], xs.max() + 1 + margin)
    y0, y1 = max(0, ys.min() - margin), min(ga.shape[0], ys.max() + 1 + margin)
    box = (int(x0), int(y0), int(x1), int(y1))
    w, h = box[2] - box[0], box[3] - box[1]
    k = max(1, min(scale, 1600 // max(1, w * 3), 1800 // max(1, h)))
    heat = Image.fromarray(np.clip(diff * 4, 0, 255).astype(np.uint8)).convert("RGB")
    strip = Image.new("RGB", (w * k * 3 + 16, h * k + 20), "white")
    d = ImageDraw.Draw(strip)
    for n, (label, im) in enumerate((("before #%d" % a, ia), ("after #%d" % b, ib), ("difference x4", heat))):
        tile = im.crop(box).resize((w * k, h * k), Image.NEAREST)
        strip.paste(tile, (n * (w * k + 8), 20))
        d.text((n * (w * k + 8) + 2, 4), label, fill="black")
    strip.save(out)
    return {"changed_pixels": int(changed.sum()), "box_px": list(box), "frame_px": [ga.shape[1], ga.shape[0]],
            "max_delta": float(diff.max()), "image": str(out)}


# --------------------------------------------------------------------------------------------
# Full-resolution frames, the block map and region masks (shared by region allowances, `film` and
# `sheet --full`)
# --------------------------------------------------------------------------------------------

def probe_size(video: Path) -> tuple[int, int]:
    """The width and height of the recording's frames as stored (no rotation applied)."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                "-of", "csv=p=0", str(video)])
    parts = out.strip().splitlines()[0].split(",") if out.strip() else []
    try:
        width, height = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        raise VideoError(f"ffprobe gave no frame size for {video}: {out.strip()[:120]!r}") from None
    if width <= 0 or height <= 0:
        raise VideoError(f"ffprobe gave a frame size of {width} x {height} for {video}")
    return width, height


def probe_header_frames(video: Path) -> int | None:
    """The frame count the container's header states, or None when it states none. Not trusted: a
    film's decoded frames are counted, and a header that differs is reported."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                "-of", "csv=p=0", str(video)])
    text = out.strip().splitlines()[0].strip().rstrip(",") if out.strip() else ""
    return int(text) if text.isdigit() else None


def frame_ranges(indices) -> list[tuple[int, int]]:
    """Sorted frame indices as inclusive runs [(a, b), ...]."""
    out: list[list[int]] = []
    for i in sorted(set(indices)):
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def _select(indices) -> str:
    return "select='" + "+".join(f"between(n\\,{a}\\,{b})" for a, b in frame_ranges(indices)) + "'"


def iter_frames(video: Path, indices, size: tuple[int, int]):
    """The recording's frames at the given indices, decoded at full resolution and in order: yields
    (index, a height x width x 3 array of uint8). Raises VideoError when the decoder gives another
    number of frames than was asked for, after the last one it gave."""
    want = sorted(set(indices))
    if not want:
        return
    width, height = size
    nbytes = width * height * 3
    cmd = ["ffmpeg", "-v", "error", "-noautorotate", "-i", str(video), "-vf", _select(want), "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        except OSError as exc:
            raise VideoError(f"cannot run ffmpeg: {exc.strerror or exc}") from exc
        got, extra = 0, b""
        try:
            for i in want:
                buf = proc.stdout.read(nbytes)
                if len(buf) < nbytes:
                    break
                got += 1
                yield i, np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
            if got == len(want):
                extra = proc.stdout.read(nbytes)
        finally:
            if proc.poll() is None and got < len(want):
                proc.kill()
            proc.stdout.close()
            proc.wait()
        if proc.returncode not in (0, None) and got < len(want):
            err.seek(0)
            detail = (err.read().decode(errors="replace").strip().splitlines()[-1:] or ["no message"])[0][:300]
            raise VideoError(f"ffmpeg failed with status {proc.returncode} while decoding frames: {detail}")
        if got != len(want) or extra:
            raise VideoError(f"the decoder gave {'more than ' if extra else ''}{got} of the {len(want)} frames asked for "
                             f"(frames {want[0]} to {want[-1]} of {video.name}); the recording and its frame list disagree")


def channel_delta(prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    """Per pixel, the largest change on any colour channel (0 to 255), as uint8."""
    return (np.maximum(prev, cur) - np.minimum(prev, cur)).max(axis=2)


def block_side(width: int) -> int:
    """A block's side in pixels: BLOCK_OF_WIDTH of the picture's width (24 of 1080)."""
    return max(1, int(round(width * BLOCK_OF_WIDTH)))


def app_rows(height: int, top: float, bottom: float) -> tuple[int, int]:
    """The rows of the app's area at a frame height: top and bottom are the system bars' heights as
    fractions of the height (the run's own insets, or the simulator's bands)."""
    a, b = int(round(top * height)), height - int(round(bottom * height))
    if not 0 <= a < b <= height:
        raise VideoError(f"the system bars ({top:.3f} and {bottom:.3f} of the height) leave no app area")
    return a, b


def block_map(delta: np.ndarray, rows: tuple[int, int], level: int = BLOCK_PIXEL_LEVEL) -> dict:
    """The app's area (rows) cut into square blocks of block_side(width): for each block the share of
    its pixels that changed by more than level ('share'), and whether that share reaches BLOCK_SHARE
    ('changed'). A block at the right or bottom edge is measured over the pixels it holds."""
    top, bottom = rows
    area = delta[top:bottom] > level
    h, w = area.shape
    side = block_side(delta.shape[1])
    nr, nc = -(-h // side), -(-w // side)
    pad = np.zeros((nr * side, nc * side), dtype=np.int32)
    pad[:h, :w] = area
    ones = np.zeros_like(pad)
    ones[:h, :w] = 1
    share = pad.reshape(nr, side, nc, side).sum(axis=(1, 3)) / ones.reshape(nr, side, nc, side).sum(axis=(1, 3))
    return {"side": side, "rows": [top, bottom], "share": share, "changed": share >= BLOCK_SHARE}


def _groups(grid: np.ndarray) -> list[list[tuple[int, int]]]:
    """Cells of a boolean grid in groups that touch, corners included."""
    seen = np.zeros_like(grid, dtype=bool)
    groups = []
    for r, c in zip(*np.nonzero(grid)):
        if seen[r, c]:
            continue
        seen[r, c] = True
        stack, cells = [(int(r), int(c))], []
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for yy in range(max(0, y - 1), min(grid.shape[0], y + 2)):
                for xx in range(max(0, x - 1), min(grid.shape[1], x + 2)):
                    if grid[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        groups.append(cells)
    return groups


def _box(mask: np.ndarray) -> list[int] | None:
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def change_places(delta: np.ndarray, rows: tuple[int, int], level: int = BLOCK_PIXEL_LEVEL) -> list[list[int]]:
    """Where a frame changed the app's area, as boxes [x0, y0, x1, y1] in pixels of that area (y from
    its top row, end exclusive): one box per group of touching changed blocks, around the pixels above
    level inside the group and the blocks next to it (a change's edge often lies in a block it covers
    less than BLOCK_SHARE of), and one more box for each connected group of pixels above level outside
    those groups that reaches film_min_group() of the picture (smaller pieces are left out).
    A change too small for any block to reach BLOCK_SHARE is one box around every pixel above level,
    or else around every pixel that changed at all. [] when nothing changed."""
    bm = block_map(delta, rows, level)
    top, bottom = rows
    area = delta[top:bottom]
    above = area > level
    side = bm["side"]
    nr, nc = bm["changed"].shape
    boxes = []
    covered = np.zeros((nr, nc), dtype=bool)
    for cells in _groups(bm["changed"]):
        grown = np.zeros((nr, nc), dtype=bool)
        for r, c in cells:
            grown[max(0, r - 1):r + 2, max(0, c - 1):c + 2] = True
        covered |= grown
        r0, c0 = min(r for r, _ in cells), min(c for _, c in cells)
        r0, c0 = max(0, r0 - 1), max(0, c0 - 1)
        r1 = min(nr, max(r for r, _ in cells) + 2)
        c1 = min(nc, max(c for _, c in cells) + 2)
        sel = np.kron(grown[r0:r1, c0:c1], np.ones((side, side), dtype=bool))
        sub = above[r0 * side:r1 * side, c0 * side:c1 * side]
        box = _box(sub & sel[:sub.shape[0], :sub.shape[1]])
        boxes.append([box[0] + c0 * side, box[1] + r0 * side, box[2] + c0 * side, box[3] + r0 * side])
    if boxes:
        # Pixels over the level outside every grown group are held to a film's rule: each connected
        # group of them at the minimum (3e-5 of the picture: a glyph redrawn in a label) is one more
        # change with its own box; smaller pieces (scattered encoder noise) are left out.
        rest = above & ~np.kron(covered, np.ones((side, side), dtype=bool))[:above.shape[0], :above.shape[1]]
        least = film_min_group(delta.shape[1], delta.shape[0])
        boxes += [box for size, box in pixel_groups(rest) if size >= least] if rest.any() else []
        return boxes
    for mask in (above, area > 0):
        box = _box(mask)
        if box is not None:
            return [box]
    return []


def region_box(region, width: int, height: int) -> list[int]:
    """A region given as fractions [x0, y0, x1, y1] of the app's area, in pixels of an area of that
    size, widened to whole pixels."""
    x0, y0, x1, y1 = region
    return [int(math.floor(x0 * width)), int(math.floor(y0 * height)),
            int(math.ceil(x1 * width)), int(math.ceil(y1 * height))]


def as_fractions(box, width: int, height: int) -> list[float]:
    return [round(box[0] / width, 4), round(box[1] / height, 4), round(box[2] / width, 4), round(box[3] / height, 4)]


def _inside(box, rbox) -> bool:
    return rbox[0] <= box[0] and rbox[1] <= box[1] and box[2] <= rbox[2] and box[3] <= rbox[3]


def account_regions(changes: list[dict], regions: list[dict], width: int, height: int) -> tuple[list[int], list[dict]]:
    """Which changes count against a rest and which against its regions. changes: one per frame that
    counts as a change, in time order, {"i": frame index, "t_ms": ms since the step's start, "boxes":
    change_places(...)}; width and height: the app's area in pixels. A change wholly inside a region
    counts against that region; a change that crosses a region's edge, lies outside every region, or
    could not be placed counts against the rest. A frame with changes of both kinds counts against
    both. Returns (the frame indices counted against the rest, a report per declared region)."""
    rboxes = [region_box(r["region"], width, height) for r in regions]
    rest, held = [], [[] for _ in regions]
    for ch in changes:
        homes = [next((k for k, rb in enumerate(rboxes) if _inside(b, rb)), None) for b in ch["boxes"]]
        if not homes or None in homes:
            rest.append(ch["i"])
        for k in sorted({h for h in homes if h is not None}):
            boxes = [b for b, h in zip(ch["boxes"], homes) if h == k]
            held[k].append({"i": ch["i"], "t_ms": ch["t_ms"],
                            "box": as_fractions([min(b[0] for b in boxes), min(b[1] for b in boxes),
                                                 max(b[2] for b in boxes), max(b[3] for b in boxes)], width, height)})
    reports = []
    for reg, frames in zip(regions, held):
        bursts = []
        for f in frames:
            if bursts and f["t_ms"] - bursts[-1]["end_ms"] <= BURST_GAP_S * 1000:
                bursts[-1]["end_ms"] = f["t_ms"]
                bursts[-1]["frames"] += 1
            else:
                bursts.append({"start_ms": f["t_ms"], "end_ms": f["t_ms"], "frames": 1})
        starts = [b["start_ms"] for b in bursts]
        gaps = sorted(b - a for a, b in zip(starts, starts[1:]))
        where = None
        if frames:
            where = [min(f["box"][0] for f in frames), min(f["box"][1] for f in frames),
                     max(f["box"][2] for f in frames), max(f["box"][3] for f in frames)]
        reports.append({
            "region": list(reg["region"]), "reason": reg.get("reason"), "changed_frames_max": reg.get("changed_frames_max"),
            "changed_frames": len(frames),
            "first_ms": frames[0]["t_ms"] if frames else None, "last_ms": frames[-1]["t_ms"] if frames else None,
            "where": where, "times": len(bursts),
            "longest_ms": max((round(b["end_ms"] - b["start_ms"], 1) for b in bursts), default=None),
            "median_interval_ms": round(gaps[len(gaps) // 2], 1) if gaps else None,
            "bursts": bursts, "frames": frames,
        })
    return rest, reports


def place_regions(video: Path, to_place: list, facts: dict) -> None:
    """For each rest with `allow.regions` in a recording analysed by analyze(): place the frames it
    counts in its regions at full resolution and write the result into the step's record ('regions',
    'rest_changed_frames'), or the reason it could not ('regions_error'). The app's area comes from the
    run's own system-bar insets ('stable_insets_top_bottom' over 'height'), never from fixed rows."""
    insets = facts.get("stable_insets_top_bottom")
    if not insets or not facts.get("height"):
        for rec, _regions, _frames in to_place:
            rec["regions_error"] = "the run holds no system-bar insets, so the app's area and its regions cannot be placed"
        return
    width, height = probe_size(video)
    rows = app_rows(height, insets[0] / facts["height"], insets[1] / facts["height"])
    targets = {i for _rec, _reg, frames in to_place for i, _t in frames}
    need = {j for i in targets for j in (i - 1, i) if j >= 0}
    places, prev, prev_i = {}, None, None
    for i, frame in iter_frames(video, need, (width, height)):
        if i in targets and prev is not None and prev_i == i - 1:
            places[i] = change_places(channel_delta(prev, frame), rows)
        prev, prev_i = frame, i
    for rec, regions, frames in to_place:
        changes = [{"i": i, "t_ms": t, "boxes": places.get(i, [])} for i, t in frames]
        rest, reports = account_regions(changes, regions, width, rows[1] - rows[0])
        rec["regions"] = reports
        rec["rest_changed_frames"] = len(rest)
        rec["rest_changed_frame_indices"] = rest


# --------------------------------------------------------------------------------------------
# Sheets of distinct frames, and full-resolution frames on disk
# --------------------------------------------------------------------------------------------

def thumbnail(img: Image.Image) -> Image.Image:
    w, h = img.size
    return img.convert("RGB").resize((THUMB_W, max(1, round(h * THUMB_W / w))), Image.BILINEAR)


def distinct_pages(tiles: list[tuple[Image.Image, str]], dest: Path, stem: str, title: str) -> list[str]:
    """Pages of PER_PAGE labelled thumbnails, sheets <stem>-distinct-01.png, -02.png, ... in dest.
    Earlier pages of the same stem are removed first. Returns the page names."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob(f"{stem}-distinct-*.png"):
        old.unlink()
    pages = []
    total = (len(tiles) + PER_PAGE - 1) // PER_PAGE
    for p in range(total):
        chunk = [(thumbnail(img), label) for img, label in tiles[p * PER_PAGE:(p + 1) * PER_PAGE]]
        tw = THUMB_W
        th = max(t.size[1] for t, _ in chunk)
        pad = 18
        rows = (len(chunk) + PAGE_COLS - 1) // PAGE_COLS
        page = Image.new("RGB", (PAGE_COLS * (tw + 6), rows * (th + pad) + 22), "white")
        d = ImageDraw.Draw(page)
        d.text((4, 4), f"{title} - page {p + 1} of {total}", fill="black")
        for n, (t, label) in enumerate(chunk):
            x = (n % PAGE_COLS) * (tw + 6)
            y = 22 + (n // PAGE_COLS) * (th + pad)
            page.paste(t, (x, y + pad))
            d.text((x + 2, y + 3), label, fill="black")
        name = f"{stem}-distinct-{p + 1:02d}.png"
        page.save(dest / name)
        pages.append(name)
    return pages


def extract_full(video: Path, indices: list[int], dest: Path) -> list[Path]:
    """The recording's frames at the given indices (one run a to b) at full resolution, written as
    f<index>.png into dest (emptied of earlier frames first). Raises VideoError when ffmpeg wrote
    another number of frames than the indices name."""
    a, b = min(indices), max(indices)
    if sorted(set(indices)) != list(range(a, b + 1)):
        raise ValueError("extract_full takes one run of consecutive indices")
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("f*.png"):
        old.unlink()
    tmp = dest / ".extract"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        _run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", _select(range(a, b + 1)),
              "-fps_mode", "passthrough", str(tmp / "x%06d.png")])
        files = sorted(tmp.glob("x*.png"))
        if len(files) != b - a + 1:
            raise VideoError(f"{len(files)} frames were extracted where the recording's own list has {b - a + 1} "
                             f"(frames {a} to {b} of {video.name})")
        out = []
        for n, f in enumerate(files):
            target = dest / f"f{a + n:06d}.png"
            f.replace(target)
            out.append(target)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------------
# A film no run made: windows of a recording at a constant frame rate
# --------------------------------------------------------------------------------------------

def _label(mask: np.ndarray):
    """The runs of True pixels along the rows of a 2-D mask, joined into 8-connected groups: (row,
    first column, end column, group of each run, size of each group by its index), or None."""
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    brk = np.flatnonzero((np.diff(ys) != 0) | (np.diff(xs) != 1)) + 1
    starts = np.concatenate(([0], brk))
    ends = np.concatenate((brk, [len(ys)]))
    ry = ys[starts]
    rx0 = xs[starts]
    rx1 = xs[ends - 1] + 1
    n = len(starts)
    parent = np.arange(n)

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    rows, first = np.unique(ry, return_index=True)
    bounds = list(first) + [n]
    for k in range(1, len(rows)):
        if rows[k] != rows[k - 1] + 1:
            continue
        i, i1 = bounds[k - 1], bounds[k]
        j, j1 = bounds[k], bounds[k + 1]
        while i < i1 and j < j1:
            if rx0[i] <= rx1[j] and rx0[j] <= rx1[i]:          # touching, diagonals included
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            if rx1[i] <= rx1[j]:
                i += 1
            else:
                j += 1
    roots = np.array([find(k) for k in range(n)])
    size = np.bincount(roots, weights=rx1 - rx0, minlength=n)
    return ry, rx0, rx1, roots, size


def pixel_groups(mask: np.ndarray) -> list[tuple[int, list[int]]]:
    """The 8-connected groups of True pixels in a 2-D mask: (size, [x0, y0, x1, y1]) for each, the
    box end-exclusive. Labelled by runs along the rows, joined where runs of adjacent rows touch,
    diagonals included."""
    lab = _label(mask)
    if lab is None:
        return []
    ry, rx0, rx1, roots, size = lab
    n = len(ry)
    x0 = np.full(n, np.iinfo(np.int64).max)
    y0 = np.full(n, np.iinfo(np.int64).max)
    x1 = np.zeros(n, dtype=np.int64)
    y1 = np.zeros(n, dtype=np.int64)
    np.minimum.at(x0, roots, rx0)
    np.minimum.at(y0, roots, ry)
    np.maximum.at(x1, roots, rx1)
    np.maximum.at(y1, roots, ry + 1)
    return [(int(size[r]), [int(x0[r]), int(y0[r]), int(x1[r]), int(y1[r])]) for r in np.unique(roots)]


def group_pixels(mask: np.ndarray, least: int) -> np.ndarray:
    """The pixels of the 8-connected groups of mask of at least `least` pixels, as a mask."""
    out = np.zeros(mask.shape, dtype=bool)
    lab = _label(mask)
    if lab is None:
        return out
    ry, rx0, rx1, roots, size = lab
    keep = size[roots] >= least
    if not keep.any():
        return out
    ry, rx0, rx1 = ry[keep], rx0[keep], rx1[keep]
    lengths = rx1 - rx0
    offsets = np.repeat(np.cumsum(lengths) - lengths, lengths)
    out[np.repeat(ry, lengths), np.arange(lengths.sum()) - offsets + np.repeat(rx0, lengths)] = True
    return out


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """mask grown by r pixels in every direction, diagonals included (a square of side 2r + 1)."""
    out = mask.copy()
    for d in range(1, r + 1):
        out[:, d:] |= mask[:, :-d]
        out[:, :-d] |= mask[:, d:]
    rows = out.copy()
    for d in range(1, r + 1):
        out[d:, :] |= rows[:-d, :]
        out[:-d, :] |= rows[d:, :]
    return out


def unexplained(pixels: np.ndarray, last: np.ndarray, since: int, r: int, excused: np.ndarray | None) -> np.ndarray:
    """The pixels of `pixels` that are not within r pixels of a pixel counted as changed in frame
    `since` or later (last: per pixel, the last frame it was part of a counted change; -1 never), and
    not in `excused` (the window's regions). Worked out on the box around `pixels`, widened by r."""
    ys, xs = np.nonzero(pixels)
    out = np.zeros(pixels.shape, dtype=bool)
    if not len(ys):
        return out
    h, w = pixels.shape
    y0, y1 = max(0, int(ys.min()) - r), min(h, int(ys.max()) + 1 + r)
    x0, x1 = max(0, int(xs.min()) - r), min(w, int(xs.max()) + 1 + r)
    zone = _dilate(last[y0:y1, x0:x1] >= since, r)
    if excused is not None:
        zone |= excused[y0:y1, x0:x1]
    out[y0:y1, x0:x1] = pixels[y0:y1, x0:x1] & ~zone
    return out


def _grey_thumb(frame: np.ndarray) -> np.ndarray:
    return np.asarray(thumbnail(Image.fromarray(frame)).convert("L"), dtype=np.float32)


def film_min_group(width: int, height: int) -> int:
    """The smallest connected group of pixels over FILM_LEVEL that is a change in a film this size."""
    return max(1, math.ceil(FILM_GROUP_SHARE * width * height))


def steady_places(counts: np.ndarray, frames: int, width: int) -> list[dict]:
    """Where pixels went over the level in at least FILM_STEADY_SHARE of a window's frames: boxes of
    touching blocks holding such pixels, with how many pixels each holds. For information only."""
    if frames <= 0:
        return []
    hot = counts >= max(1, math.ceil(FILM_STEADY_SHARE * frames))
    if not hot.any():
        return []
    side = block_side(width)
    h, w = hot.shape
    nr, nc = -(-h // side), -(-w // side)
    pad = np.zeros((nr * side, nc * side), dtype=bool)
    pad[:h, :w] = hot
    grid = pad.reshape(nr, side, nc, side).any(axis=(1, 3))
    places = []
    for cells in _groups(grid)[:20]:
        sel = np.zeros_like(pad)
        for r, c in cells:
            sel[r * side:(r + 1) * side, c * side:(c + 1) * side] = True
        part = pad & sel
        places.append({"box": as_fractions(_box(part), w, h), "box_px": _box(part), "pixels": int(part.sum())})
    return places


def _over(a: np.ndarray, b: np.ndarray, rows: tuple[int, int]) -> tuple[np.ndarray, int, list]:
    """The pixels over FILM_LEVEL between two frames in the app's rows, their 8-connected groups and
    the size of the largest (0 when none)."""
    app = (channel_delta(a, b) > FILM_LEVEL)[rows[0]:rows[1]]
    groups = pixel_groups(app) if app.any() else []
    return app, max((g[0] for g in groups), default=0), groups


def analyze_film(video: Path, out: Path, windows: list[dict], insets_px: list[int], title: str,
                 pictures: bool = True, slow_frames: int | None = None, slow_min_group: int | None = None,
                 explain_px: int | None = None) -> dict:
    """The recording analysis for a film that no run made (a phone's screen filmed by another
    machine): one entry per window {"id", "start_s", "end_s", "regions"?}, judged as a rest.

    Such a film is recorded at a constant frame rate, and no two of its frames need be bit-identical:
    the encoder leaves scattered pixels changed in every frame, more on its key frames, and some
    pixels flicker in place. A frame of the film changed the app's area only where it holds an
    8-connected group of pixels over FILM_LEVEL of at least film_min_group() pixels; every smaller
    group is scattered, the recording's, and left out. Per window the record says how many frames
    held only scattered changes, the largest scattered group against the minimum, and the places
    where pixels went over the level in FILM_STEADY_SHARE of the frames. insets_px: the system bars
    in the film's own pixels [top, bottom]; [0, 0] takes the whole picture as the app's area.
    pictures False writes no picture of any kind into out (no frames-full/, no sheets): every number is
    kept, and the thumbnails the bursts are classed from stay in memory.

    A change too slow to show between neighbours is caught by two more comparisons under the same
    rule, with slow_min_group (default: the same minimum): each frame against the frame slow_frames
    earlier (default: the frames in FILM_SLOW_S) when that frame lies in the window or is the one
    before it, and the window's last frame against its first. Explained means the same pixels: per
    pixel the analysis keeps the last frame in which it was part of a counted change. Of a slow group
    of frame i, the pixels within explain_px (default FILM_EXPLAIN_PX) of a pixel counted in frames
    i - k to i of the window, or inside one of its regions, are explained; what is left is grouped
    again, and a group at the minimum counts (so a change made once is counted once). The ends work
    the same way against every pixel counted in the window, and a group left over at the minimum is
    unexplained: the gate fails it. A slow group is explained only by changes counted between
    neighbours, never by a slow change counted earlier; the ends by both. A frame counts as changed when either
    of its comparisons holds a group at its minimum; the ends are reported per window, and the gate
    fails a window whose ends differ though no frame counted."""
    pts = probe_pts(video)
    n = len(pts)
    if n == 0:
        raise UnusableRecording("the film holds no video frames")
    width, height = probe_size(video)
    header = probe_header_frames(video)
    top, bottom = int(insets_px[0]), int(insets_px[1])
    if top < 0 or bottom < 0 or top + bottom >= height:
        raise VideoError(f"the insets {insets_px} leave no app area in a film {height} px high")
    rows = (top, height - bottom)
    app_h = rows[1] - rows[0]
    min_group = film_min_group(width, height)
    gaps = sorted(b - a for a, b in zip(pts, pts[1:]) if b > a)
    interval = gaps[len(gaps) // 2] if gaps else 0.0
    k = slow_frames or (max(1, int(round(FILM_SLOW_S / interval))) if interval > 0 else 30)
    slow_min = slow_min_group or min_group
    r = FILM_EXPLAIN_PX if explain_px is None else explain_px
    plan = []
    for w in windows:
        for key in ("start_s", "end_s"):
            if w[key] > pts[-1] + interval + 1e-6:
                raise VideoError(f"window '{w['id']}' {'starts' if key == 'start_s' else 'ends'} at {w[key]:g} s, "
                                 f"beyond the film's last frame at {pts[-1]:.3f} s by more than one frame interval "
                                 f"({interval * 1000:.1f} ms)")
        idx = [i for i, p in enumerate(pts) if w["start_s"] <= p < w["end_s"]]
        if len(idx) < 2:
            raise VideoError(f"window '{w['id']}' ({w['start_s']:g} to {w['end_s']:g} s) holds {len(idx)} frame"
                             f"{'' if len(idx) == 1 else 's'} of the film, which has {n} frames from {pts[0]:.3f} to "
                             f"{pts[-1]:.3f} s")
        base = idx[0] - 1 if idx[0] > 0 else idx[0]
        plan.append({**w, "base": base, "judged": [i for i in idx if i > base]})
    app_h_ = (height - bottom) - top
    for p in plan:
        p["rboxes"] = [region_box(reg["region"], width, app_h_) for reg in p.get("regions") or []]
        p["excused"] = None
        if p["rboxes"]:
            p["excused"] = np.zeros((app_h_, width), dtype=bool)
            for x0, y0, x1, y1 in p["rboxes"]:
                p["excused"][max(0, y0 - r):y1 + r, max(0, x0 - r):x1 + r] = True
    owner = {i: p["id"] for p in plan for i in p["judged"]}
    ends = {i for p in plan for i in (p["judged"][0], p["judged"][-1])}
    need = sorted({i for p in plan for i in range(p["base"], p["judged"][-1] + 1)})
    full_dir = out / "frames-full"
    for p in plan if pictures else []:
        d_ = full_dir / p["id"]
        d_.mkdir(parents=True, exist_ok=True)
        for old in d_.glob("f*.png"):
            old.unlink()

    counts = {p["id"]: np.zeros((app_h, width), dtype=np.uint32) for p in plan}
    window_of = {i: p for p in plan for i in p["judged"]}
    kinds, info, thumbs, firsts, ends_seen = {}, {}, {}, {}, {}
    # Per pixel, the last frame it was part of a change counted between neighbours (-1: never), and the
    # pixels of the slow changes counted in the current window (for its ends only).
    last = np.full((app_h, width), -1, dtype=np.int32)
    slow_seen = np.zeros((app_h, width), dtype=bool)
    prev = prev_i = None
    # The frame k earlier comes from a second decoder that runs k frames behind the first, so that no
    # more than two frames are held at a time.
    lag = iter_frames(video, need, (width, height))
    lag_i, lag_frame = -1, None
    try:
        for i, frame in iter_frames(video, need, (width, height)):
            p = window_of.get(i)
            if p is not None and i == p["judged"][0]:
                firsts[p["id"]] = frame
                slow_seen[:] = False
            if p is not None and prev is not None and prev_i == i - 1:
                over = channel_delta(prev, frame) > FILM_LEVEL
                app = over[rows[0]:rows[1]]
                counts[p["id"]] += app
                count = int(app.sum())
                rec = {"over_px": count, "largest_group_px": 0, "boxes": []}
                if count:
                    groups = pixel_groups(app)
                    rec["largest_group_px"] = max(g[0] for g in groups)
                    rec["boxes"] = [box for size, box in groups if size >= min_group]
                    if rec["boxes"]:
                        last[group_pixels(app, min_group)] = i
                if i - k >= p["base"]:
                    while lag_i < i - k:
                        lag_i, lag_frame = next(lag)
                    slow_px, largest, groups = _over(lag_frame, frame, rows)
                    rec["slow_largest_group_px"] = largest
                    rec["slow_boxes"] = []
                    if largest >= slow_min:
                        # The pixels within r of a change counted in these k frames of the window, or in a
                        # region, are that change seen again; what is left is grouped again.
                        big = group_pixels(slow_px, slow_min)
                        left = unexplained(big, last, max(i - k, p["judged"][0]), r, p["excused"])
                        left = group_pixels(left, slow_min)
                        rec["slow_explained_px"] = int(big.sum() - left.sum())
                        if left.any():
                            # Not written into `last`: a slow change never explains a later slow group, so a
                            # fade is counted in every frame it shows. It does explain the window's ends.
                            rec["slow_boxes"] = [box for size, box in pixel_groups(left)]
                            slow_seen |= left
                by_frame, by_slow = bool(rec["boxes"]), bool(rec.get("slow_boxes"))
                if by_frame or by_slow:
                    kind = "changed"
                    rec["by"] = "both" if by_frame and by_slow else ("frame" if by_frame else "slow")
                else:
                    kind = "scattered" if count else "still"
                    if top or bottom:
                        bars = np.concatenate((over[:rows[0]], over[rows[1]:]))
                        if bars.sum() >= min_group and max(g[0] for g in pixel_groups(bars)) >= min_group:
                            kind = "bars"
                if kind == "changed":
                    thumbs[i] = _grey_thumb(frame)
                    thumbs.setdefault(i - 1, _grey_thumb(prev))
                kinds[i], info[i] = kind, rec
            if p is not None and i == p["judged"][-1]:
                ends_px, largest, groups = _over(firsts[p["id"]], frame, rows)
                left = []
                if largest >= slow_min:
                    counted = np.where(slow_seen, p["judged"][0], last)
                    rest = unexplained(group_pixels(ends_px, slow_min), counted, p["judged"][0], r, p["excused"])
                    left = [box for size, box in pixel_groups(rest) if size >= slow_min]
                ends_seen[p["id"]] = (largest, [box for size, box in groups if size >= slow_min], left)
            if pictures and p is not None and (i in ends or kinds.get(i) == "changed"):
                Image.fromarray(frame).save(full_dir / p["id"] / f"f{i:06d}.png")
            prev, prev_i = frame, i
    finally:
        lag.close()

    steps = []
    largest_scattered_all = 0
    if pictures:
        (out / "sheets").mkdir(parents=True, exist_ok=True)
    for p in plan:
        sid, start = p["id"], p["start_s"]
        judged = p["judged"]
        rel = lambda i: round((pts[i] - start) * 1000, 1)       # noqa: E731
        changed = [i for i in judged if kinds.get(i) == "changed"]
        scattered = [i for i in judged if kinds.get(i) == "scattered"]
        largest_scattered = max((info[i]["largest_group_px"] for i in judged if i in info
                                 and info[i]["largest_group_px"] < min_group), default=0)
        largest_scattered_all = max(largest_scattered_all, largest_scattered)
        slow_scattered = max((info[i].get("slow_largest_group_px", 0) for i in judged if i in info
                              and info[i].get("slow_largest_group_px", 0) < slow_min), default=0)
        ends_largest, ends_boxes, ends_unexplained = ends_seen[sid]
        rboxes = [region_box(r["region"], width, app_h) for r in p.get("regions") or []]
        ends_outside = [b for b in ends_boxes if not any(_inside(b, rb) for rb in rboxes)]
        # Bursts are formed from the frames that changed; the rest are no change at all.
        app_list, frames = [], []
        for i in changed:
            app_list.append(thumbs[i])
            frames.append({"i": len(app_list) - 1, "t": pts[i], "app_changed": True, "film_i": i})
        bursts, cur = [], []
        for f in frames:
            if cur and f["t"] - cur[-1]["t"] > BURST_GAP_S:
                bursts.append(cur)
                cur = []
            cur.append(f)
        if cur:
            bursts.append(cur)
        infos = []
        for b in bursts:
            infos.append(classify_burst("rest", b, thumbs[b[0]["film_i"] - 1], app_list))
            infos[-1]["frames"] = [f["film_i"] for f in b]
        rec = {
            "frames": len(judged), "changed_app": len(changed), "scattered": len(scattered),
            "still": sum(1 for i in judged if kinds.get(i) == "still"),
            "bars_only": sum(1 for i in judged if kinds.get(i) == "bars"),
            "min_group_px": min_group, "largest_scattered_group_px": largest_scattered,
            "caution": largest_scattered >= FILM_CAUTION * min_group,
            "steady_places": steady_places(counts[sid], len(judged), width),
            "scattered_frames": scattered,
            "signature": " ".join(x["class"] for x in infos if x["class"] in MAJOR) or "-",
            "bursts": len(infos), "burst_detail": infos,
            "largest_change_group_px": max((info[i]["largest_group_px"] for i in changed), default=0),
            "most_changed_px": max((info[i]["over_px"] for i in changed), default=0),
            "first_change_ms": rel(changed[0]) if changed else None,
            "last_change_ms": rel(changed[-1]) if changed else None,
            "drawing_frames": [{"i": i, "t_ms": rel(i), "by": info[i]["by"], "largest_group_px": info[i]["largest_group_px"],
                                "slow_largest_group_px": info[i].get("slow_largest_group_px"),
                                "boxes": [as_fractions(b, width, app_h) for b in info[i]["boxes"]],
                                "slow_boxes": [as_fractions(b, width, app_h) for b in info[i].get("slow_boxes", [])]}
                               for i in changed],
            "changed_by_slow_only": sum(1 for i in changed if info[i]["by"] == "slow"),
            "slow": {"frames_apart": k, "seconds_apart": round(k * interval, 3), "min_group_px": slow_min,
                     "explain_px": r,
                     "compared": sum(1 for i in judged if "slow_largest_group_px" in info.get(i, {})),
                     "largest_scattered_group_px": slow_scattered, "caution": slow_scattered >= FILM_CAUTION * slow_min},
            "ends": {"first_frame": judged[0], "last_frame": judged[-1], "differ": bool(ends_boxes),
                     "differ_outside_regions": bool(ends_outside), "differ_unexplained": bool(ends_unexplained),
                     "unexplained_boxes": [as_fractions(b, width, app_h) for b in ends_unexplained],
                     "largest_group_px": ends_largest,
                     "min_group_px": slow_min, "boxes": [as_fractions(b, width, app_h) for b in ends_boxes],
                     "caution": not ends_boxes and ends_largest >= FILM_CAUTION * slow_min},
            "base_frame": p["base"], "first_frame": judged[0], "last_frame": judged[-1],
            "frames_full": f"frames-full/{sid}" if pictures else None,
        }
        if p.get("regions"):
            # A frame is placed by its changes between neighbours and by those of its slow comparison
            # that lie outside every region; a slow change inside a region counts against nothing (the
            # region's limit counts frames that changed between neighbours).
            changes, slow_inside = [], 0
            for i in changed:
                boxes = info[i]["boxes"] + [b for b in info[i].get("slow_boxes", [])
                                            if not any(_inside(b, rb) for rb in rboxes)]
                if boxes:
                    changes.append({"i": i, "t_ms": rel(i), "boxes": boxes})
                else:
                    slow_inside += 1
            rec["slow_inside_regions"] = slow_inside
            rest, reports = account_regions(changes, p["regions"], width, app_h)
            rec["regions"] = reports
            rec["rest_changed_frames"] = len(rest)
            rec["rest_changed_frame_indices"] = rest
        if not pictures:
            rec["sheet"], rec["distinct_sheets"] = None, []
            steps.append({"id": sid, "kind": "rest", "start_s": start, "end_s": p["end_s"],
                          "window_s": round(p["end_s"] - start, 3), "recording": rec})
            continue
        tiles = [(Image.open(full_dir / sid / f"f{i:06d}.png"), f"#{i} +{rel(i) / 1000:.3f} s") for i in changed]
        pages = distinct_pages(tiles, out / "sheets", sid,
                               f"{title} / window {sid} - {len(changed)} frames changed the app's area")
        overview = out / "sheets" / f"{sid}.png"
        if pages:
            shutil.copyfile(out / "sheets" / pages[0], overview)
        else:
            img = thumbnail(Image.open(full_dir / sid / f"f{judged[0]:06d}.png"))
            page = Image.new("RGB", (img.size[0], img.size[1] + 40), "white")
            page.paste(img, (0, 40))
            ImageDraw.Draw(page).text((4, 4), f"{sid}: no frame changed the app's area\n#{judged[0]}, the window's "
                                      "first frame", fill="black")
            page.save(overview)
        rec["sheet"] = f"sheets/{sid}.png"
        rec["distinct_sheets"] = [f"sheets/{x}" for x in pages]
        steps.append({"id": sid, "kind": "rest", "start_s": start, "end_s": p["end_s"],
                      "window_s": round(p["end_s"] - start, 3), "recording": rec})
    floor = {"level": FILM_LEVEL, "min_group_share": FILM_GROUP_SHARE, "min_group_px": min_group,
             "slow_frames": k, "slow_min_group_px": slow_min,
             "largest_scattered_group_px": largest_scattered_all,
             "caution": largest_scattered_all >= FILM_CAUTION * min_group,
             "why": f"a frame changed the app's area only where an 8-connected group of pixels changed by more than "
                    f"{FILM_LEVEL} levels holds at least {min_group} px ({FILM_GROUP_SHARE:g} of the picture); "
                    "smaller groups are scattered, the recording's",
             "decoded_frames": n, "header_frames": header}
    return {"frames": n, "header_frames": header, "size": [width, height], "duration_s": round(pts[-1] - pts[0], 3),
            "frame_interval_ms_median": round(interval * 1000, 2) if gaps else None,
            "insets_px": [top, bottom], "app_rows": list(rows), "block_px": block_side(width),
            "recording_floor": floor, "steps": steps}


# --------------------------------------------------------------------------------------------
# A change outside the control a step touched (`scope`)
# --------------------------------------------------------------------------------------------

def _grey(frame: np.ndarray) -> np.ndarray:
    return frame.astype(np.float32).mean(axis=2)


def find_shift(a: np.ndarray, b: np.ndarray, box, limit: int) -> dict:
    """Where the content of box in grey frame a lies in grey frame b: the vertical or horizontal shift of
    at most `limit` px (one axis at a time) that makes b's pixels closest to a's, by mean absolute
    difference. Searched at about half resolution, then refined at full resolution. Returns the shift
    [dx, dy], the difference with no shift ('still') and at the shift ('best')."""
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = a.shape
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return {"shift": [0, 0], "still": 0.0, "best": 0.0, "flat": True}
    patch = a[y0:y1, x0:x1]
    if float(patch.std()) < OUTSIDE_TEXTURE:
        # A plain patch matches every plain area as well as its own place: no shift can be told.
        return {"shift": [0, 0], "still": round(float(np.abs(b[y0:y1, x0:x1] - patch).mean()), 3), "best": 0.0,
                "flat": True}

    def score(dx, dy):
        if y0 + dy < 0 or y1 + dy > h or x0 + dx < 0 or x1 + dx > w:
            return None
        return float(np.abs(b[y0 + dy:y1 + dy, x0 + dx:x1 + dx] - patch).mean())

    f = max(1, int(round(w / 540)))
    sa, sb = a[::f, ::f], b[::f, ::f]
    bx0, by0, bx1, by1 = x0 // f, y0 // f, max(x0 // f + 1, x1 // f), max(y0 // f + 1, y1 // f)
    sp = sa[by0:by1, bx0:bx1]
    sh, sw = sb.shape
    best, where = None, (0, 0)
    scores = []
    for axis in ("y", "x"):
        for d in range(-(limit // f), limit // f + 1):
            dx, dy = (d, 0) if axis == "x" else (0, d)
            if by0 + dy < 0 or by1 + dy > sh or bx0 + dx < 0 or bx1 + dx > sw:
                continue
            v = float(np.abs(sb[by0 + dy:by1 + dy, bx0 + dx:bx1 + dx] - sp).mean())
            scores.append((v, dx * f, dy * f))
            if best is None or v < best:
                best, where = v, (dx * f, dy * f)
    # The best score among shifts away from the best one (more than two steps of the search, or on the
    # other axis): content that repeats matches there as well, and then no shift can be told.
    far = [v for v, dx, dy in scores if (dx, dy) != (0, 0)
           and not (abs(dx - where[0]) <= 2 * f and abs(dy - where[1]) <= 2 * f)]
    second = min(far) if far else None
    still = score(0, 0)
    best, shift = still, (0, 0)
    for dx in range(where[0] - f, where[0] + f + 1):
        for dy in range(where[1] - f, where[1] + f + 1):
            if (dx and dy) or abs(dx) > limit or abs(dy) > limit:
                continue
            v = score(dx, dy)
            if v is not None and v < best:
                best, shift = v, (dx, dy)
    return {"shift": [int(shift[0]), int(shift[1])], "still": round(still, 3), "best": round(best, 3), "flat": False,
            "second": None if second is None else round(second, 3)}


def moved(found: dict, move_px: float) -> bool:
    """Whether a find_shift result says the content moved by move_px or more."""
    return (not found.get("flat") and found["still"] > OUTSIDE_STILL_SCORE
            and found["best"] <= min(OUTSIDE_MATCH_MAX, OUTSIDE_MATCH_RATIO * found["still"])
            and max(abs(found["shift"][0]), abs(found["shift"][1])) >= move_px)


def unique(found: dict) -> bool:
    """Whether a find_shift result's match is the only one: no other shift comes within the tolerance."""
    tolerance = min(OUTSIDE_MATCH_MAX, OUTSIDE_MATCH_RATIO * found["still"])
    return found.get("second") is None or found["second"] > tolerance


def joined_groups(mask: np.ndarray, r: int = OUTSIDE_JOIN_PX, strong: np.ndarray | None = None) -> list[tuple[int, list[int]]]:
    """Groups of the True pixels of mask, 8-connected, where the pixels of `strong` (default: all of
    mask) also join within a closing by r px (each grown by r): (pixels of mask in the group, their box)."""
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return []
    h, w = mask.shape
    y0, y1 = max(0, int(ys.min()) - r), min(h, int(ys.max()) + 1 + r)
    x0, x1 = max(0, int(xs.min()) - r), min(w, int(xs.max()) + 1 + r)
    sub = mask[y0:y1, x0:x1]
    grow = sub if strong is None else strong[y0:y1, x0:x1]
    lab = _label(sub | _dilate(grow, r))
    ry, rx0, rx1, roots, _size = lab
    labels = np.full(sub.shape, -1, dtype=np.int32)
    lengths = rx1 - rx0
    offsets = np.repeat(np.cumsum(lengths) - lengths, lengths)
    labels[np.repeat(ry, lengths), np.arange(lengths.sum()) - offsets + np.repeat(rx0, lengths)] = np.repeat(roots, lengths)
    sy, sx = np.nonzero(sub)
    ids, inv = np.unique(labels[sy, sx], return_inverse=True)
    n = len(ids)
    size = np.bincount(inv, minlength=n)
    bx0 = np.full(n, np.iinfo(np.int64).max)
    by0 = np.full(n, np.iinfo(np.int64).max)
    bx1 = np.zeros(n, dtype=np.int64)
    by1 = np.zeros(n, dtype=np.int64)
    np.minimum.at(bx0, inv, sx)
    np.minimum.at(by0, inv, sy)
    np.maximum.at(bx1, inv, sx + 1)
    np.maximum.at(by1, inv, sy + 1)
    return [(int(size[k]), [int(bx0[k]) + x0, int(by0[k]) + y0, int(bx1[k]) + x0, int(by1[k]) + y0]) for k in range(n)]


def _excused(size, box, allowed, screen: bool, rows) -> np.ndarray:
    """The pixels a step with scope may change: the system bars, the control's box widened by a block,
    every allowed box widened by FILM_EXPLAIN_PX, or all of them with the whole screen allowed."""
    width, height = size
    side = block_side(width)
    excused = np.zeros((height, width), dtype=bool)
    excused[:rows[0]] = True
    excused[rows[1]:] = True
    x0, y0, x1, y1 = (int(v) for v in box)
    excused[max(0, y0 - side):y1 + side, max(0, x0 - side):x1 + side] = True
    for ax0, ay0, ax1, ay1 in allowed:
        r = FILM_EXPLAIN_PX
        excused[max(0, int(ay0) - r):int(ay1) + r, max(0, int(ax0) - r):int(ax1) + r] = True
    if screen:
        excused[:] = True
    return excused


def outside_groups(a: np.ndarray, b: np.ndarray, excused: np.ndarray, least: int,
                   join_level: float = OUTSIDE_JOIN_LEVEL) -> list[tuple[int, list[int]]]:
    """The changes outside from frame a to frame b: groups of pixels over FILM_LEVEL, not excused, of at
    least `least` such pixels; pixels changed by more than join_level also join within OUTSIDE_JOIN_PX."""
    d = channel_delta(a, b)
    out = (d > FILM_LEVEL) & ~excused
    if not out.any():
        return []
    return [g for g in joined_groups(out, strong=out & (d > join_level)) if g[0] >= least]


def outside_check(video: Path, size: tuple[int, int], before: int, frames: list[tuple[int, float]], box,
                  allowed: list, screen: bool, rows: tuple[int, int], move_px: float, came_back_ms: float,
                  strip: Path | None = None, prior: list[int] | None = None,
                  join_level: float = OUTSIDE_JOIN_LEVEL) -> dict:
    """What a step changed outside the control it touched. before: the index of the last recorded frame
    before the input; frames: [(index, ms since the input)] of the frames after it, in order; box: the
    control's box, in pixels of the recording; allowed: the boxes of the step's may_change regions;
    screen: whether the whole screen may change. A change outside is an 8-connected group of pixels
    over FILM_LEVEL, of at least film_min_group() of the app's area, outside the control's box widened
    by a block, outside every allowed box (widened by FILM_EXPLAIN_PX) and outside the system bars,
    each frame against the frame before the input. Returns the changes (episodes in time, with their
    box, start, duration and how they ended), whether the control moved, the bands that slid, and the
    four verdicts. strip: where to draw the frame before, the frame with the most change outside and
    the last frame, with the box (None: nothing is drawn). prior: the recorded frames of the
    OUTSIDE_SETTLE_MS before the input, after the last one before that time and ending with `before`;
    when one of them changed outside since the one before it, the screen was not at rest, nothing else
    is judged, and the result holds 'unsettled'."""
    width, height = size
    app_h = rows[1] - rows[0]
    least = film_min_group(width, app_h)
    excused = _excused(size, box, allowed, screen, rows)
    x0, y0, x1, y1 = (int(v) for v in box)
    if prior and len(prior) > 1:
        previous = None
        for i, frame in iter_frames(video, prior, size):
            if previous is not None:
                groups = outside_groups(previous[1], frame, excused, least, join_level)
                if groups:
                    return {"box_px": [x0, y0, x1, y1], "unsettled": {"frame": i, "px": sum(g[0] for g in groups),
                                                                      "boxes_px": [g[1] for g in groups]}}
            previous = (i, frame)
    seen, slid = [], []
    b_frame = b_grey = last = None
    most = (0, None, None)
    order = [before] + [i for i, _t in frames]
    when = dict(frames)
    for i, frame in iter_frames(video, order, size):
        if i == before:
            b_frame, b_grey = frame, _grey(frame)
            continue
        groups = outside_groups(b_frame, frame, excused, least, join_level)
        px = sum(g[0] for g in groups)
        rec = {"i": i, "t_ms": round(when[i], 1), "groups": [g[1] for g in groups], "px": px}
        if groups:
            grey = _grey(frame)
            for size_, gbox in sorted(groups, key=lambda g: -g[0])[:OUTSIDE_SLIDE_GROUPS]:
                # Where the new content of the changed band came from in the frame before: content that
                # was already elsewhere on a screen that repeats (a list) is not taken for a slide.
                found = find_shift(grey, b_grey, gbox, OUTSIDE_SEARCH_PX)
                if moved(found, move_px) and unique(found):
                    slid.append({"i": i, "t_ms": rec["t_ms"], "box_px": gbox,
                                 "shift_px": [-found["shift"][0], -found["shift"][1]], "px": size_})
            if px > most[0]:
                most = (px, i, frame)
        seen.append(rec)
        last = (i, frame)
    end_i, end_frame = last if last is not None else (before, b_frame)
    control = find_shift(b_grey, _grey(end_frame), box, OUTSIDE_SEARCH_PX)
    # A move needs the control's content found at one place only at the step's end (another shift as
    # close is a lookalike), gone from its old place, and not already at the new place before the input:
    # a switch that changed its own look among switches like it did not move.
    already = None
    if not control.get("flat") and control["shift"] != [0, 0]:
        dx, dy = control["shift"]
        already = round(float(np.abs(b_grey[y0 + dy:y1 + dy, x0 + dx:x1 + dx] - b_grey[y0:y1, x0:x1]).mean()), 3)
    control["already_there"] = already
    tolerance = min(OUTSIDE_MATCH_MAX, OUTSIDE_MATCH_RATIO * control["still"])
    control["moved"] = bool(moved(control, move_px) and unique(control)
                            and already is not None and already > tolerance)
    changes, cur = [], None
    for rec in seen:
        if rec["groups"]:
            if cur is None:
                cur = {"start_ms": rec["t_ms"], "first_frame": rec["i"], "box_px": None, "frames": 0, "most_px": 0}
            cur["frames"] += 1
            cur["most_px"] = max(cur["most_px"], rec["px"])
            for g in rec["groups"]:
                b = cur["box_px"]
                cur["box_px"] = list(g) if b is None else [min(b[0], g[0]), min(b[1], g[1]), max(b[2], g[2]), max(b[3], g[3])]
        elif cur is not None:
            cur.update({"ended": "came back", "end_ms": rec["t_ms"], "duration_ms": round(rec["t_ms"] - cur["start_ms"], 1),
                        "below_the_hand": rec["t_ms"] - cur["start_ms"] < came_back_ms})
            changes.append(cur)
            cur = None
    if cur is not None:
        cur.update({"ended": "still there at the step's end", "end_ms": None, "duration_ms": None, "below_the_hand": False})
        changes.append(cur)
    fails = {"moved": control["moved"], "still_there": any(c["ended"] != "came back" for c in changes),
             "slid": bool(slid), "came_back": any(c["ended"] == "came back" and not c["below_the_hand"] for c in changes)}
    strip_name = None
    if strip is not None:
        tiles = [(b_frame, f"#{before} before the input"), (most[2], f"#{most[1]} most change outside"),
                 (end_frame, f"#{end_i} the step's end")]
        imgs = []
        for frame, label in tiles:
            if frame is None:
                continue
            img = Image.fromarray(frame).convert("RGB")
            d = ImageDraw.Draw(img)
            d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 0, 0), width=max(2, width // 270))
            if frame is most[2]:
                for c in changes:
                    if c["box_px"]:
                        d.rectangle([c["box_px"][0], c["box_px"][1], c["box_px"][2] - 1, c["box_px"][3] - 1],
                                    outline=(255, 200, 0), width=max(2, width // 270))
            imgs.append((thumbnail(img), label))
        th = max(t.size[1] for t, _ in imgs)
        page = Image.new("RGB", (len(imgs) * (THUMB_W + 6), th + 22), "white")
        for n, (t, label) in enumerate(imgs):
            page.paste(t, (n * (THUMB_W + 6), 22))
            ImageDraw.Draw(page).text((n * (THUMB_W + 6) + 2, 4), label, fill="black")
        strip.parent.mkdir(parents=True, exist_ok=True)
        page.save(strip)
        strip_name = strip.name
    return {"box_px": [x0, y0, x1, y1], "before_frame": before, "end_frame": end_i, "frames": len(frames),
            "min_group_px": least, "move_px": round(move_px, 2), "came_back_ms_min": came_back_ms,
            "control": control, "slid": slid, "changes": changes, "fails": fails,
            "below_the_hand": sum(1 for c in changes if c["below_the_hand"]), "strip": strip_name}
