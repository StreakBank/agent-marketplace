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
"""
from __future__ import annotations

import json
import struct
import subprocess
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
ALIGN_TOL_S = 0.020
MATCH_WINDOW_S = 0.070    # a video frame is matched to an app frame completed at most this long before it


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


def analyze(video: Path, out: Path, hwui_rows: list[dict], marks: dict, scn: dict, facts: dict) -> dict:
    pts = probe_pts(video)
    files = extract(video, out / "frames")
    n = min(len(pts), len(files))
    pts, files = pts[:n], files[:n]
    if n == 0:
        raise UnusableRecording("the recording holds no video frames")
    grey = [np.asarray(Image.open(f).convert("L"), dtype=np.float32) for f in files]
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
               "app_changed": bool(changed), "identical": bool(i > 0 and not whole)}
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
            "id": sid, "kind": kind, "video_frames": len(inside), "app_region_frames": len(app_frames),
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
            "burst_detail": infos,
        })
        sheet(out / "sheets" / f"{sid}.png", files, inside,
              f"{scn['name']} / step {sid} ({kind}) - {len(inside)} video frames, signature: {steps_out[-1]['signature']}")
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
