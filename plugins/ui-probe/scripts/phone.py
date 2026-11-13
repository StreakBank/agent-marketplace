"""ui-probe phone: record the screen of an iPhone cabled to this Mac, on its owner's go.

  ui-probe phone list
  ui-probe phone record --seconds N --out DIR --go "<the owner's words, or when they said go>"
                        --build "<repository> <revision> <configuration>" [--device <id-prefix>]

`record` takes a lock in DIR first, then starts ONE recorder process that holds the phone from the
listing to the end of the recording (once a process that saw a phone exits, the phone is not offered
to the next one for about 11 to 32 s): the recorder offers the devices it found as one line, this
module chooses one, creates DIR/film.json (never over another file) with the go, the build, the
seconds asked and this tool's version, posts the notice, and only then sends the recorder the chosen
id. A refusal or a failed notice closes the recorder's input instead, and nothing is recorded.
film.json is completed afterwards (the device's name and model, UTC start and stop, the seconds
recorded, the film's tracks, whether its sound was checked, whether the film is on disk, the exit
status), and the recording itself is DIR/screen.mov.

What it never does, each held in code and by a test: it attaches no sound (the picture only); it
sends the phone no input and installs or launches nothing (the Swift source holds no call that
starts a process or manages a device, this module imports nothing of the rest of ui-probe, and no
other verb calls it); it has no watch mode, does not start when a phone is connected and leaves
nothing running; `--go` and `--build` come from the command line only, never from the environment
or a file; a recording lasts at most CAP_S seconds and no flag raises that; with more than one
phone offered it refuses without `--device`; it refuses an output that exists; it refuses any
system but macOS. When a recording starts, the Mac posts a notification, so the owner sees every
recording begin.

The recorder is phone_screen.swift beside this file, compiled with swiftc on first use into a cache
keyed by the SHA-256 of its source; no binary ships.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "phone_screen.swift"
CAP_S = 300
STATUS_BAR = ("note: while the Mac records it, the phone's status bar reads 9:41 (the time a cabled "
              "phone shows while it is filmed); the clock in the film is not the time of day")
NOTICE_TITLE = "ui-probe"
# The notice's text and title reach AppleScript as arguments, never inside the script's own text, so
# the owner's words (curly quotes, a dash) need no quoting and cannot change the script.
NOTICE_SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")
LOCK = ".ui-probe-phone.lock"
PHONE_WORDS = ("iphone", "ipad")
COMPILE_TIMEOUT_S = 600
LIST_TIMEOUT_S = 120
# How long the recorder may take to offer its devices: macOS's camera prompt (the recorder waits at most
# 60 s for an answer), then its wait for a phone (at most 60 s), and a margin.
OFFER_TIMEOUT_S = 135
# How long a recorder told to stop (its input closed) may take to exit before it is killed.
STOP_TIMEOUT_S = 10
# Said when no phone is offered: measured on one iPhone and one Mac (macOS 26), 2026-11-13.
AGAIN = ("a phone that another program looked at a moment ago (`ui-probe phone list`, say) is offered again only "
         "after up to about half a minute (11 to 32 s, measured on one iPhone and one Mac)")


class PhoneError(RuntimeError):
    """A refusal or a failure the command reports itself (exit 2)."""


def refuse_other_systems(platform: str | None = None) -> None:
    if (platform or sys.platform) != "darwin":
        raise PhoneError(f"phone: only macOS can record a cabled iPhone's screen (this is {platform or sys.platform})")


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ui-probe" / "phone"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_key() -> str:
    return sha256_of(SOURCE)


def write_json(path: Path, record_: dict) -> None:
    """Write a JSON file whole or not at all: into a temporary file beside it, then renamed over it, so
    a full disk or an interruption never leaves it empty or cut short."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(record_, indent=1))
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def build() -> Path:
    """The compiled recorder for this source: the cached one if its SHA-256 is the one recorded when it
    was compiled, else compiled now with swiftc. A cached binary that was replaced is never run: it is
    compiled again, and a failed compile refuses."""
    folder = cache_root() / source_key()
    binary = folder / "phone-screen"
    marker = folder / "built.ok"
    if binary.is_file() and marker.is_file():
        if f"binary {sha256_of(binary)}" in marker.read_text().splitlines():
            return binary
        marker.unlink()                    # replaced since it was compiled: compile it again
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / "phone-screen.partial"
    try:
        out = subprocess.run(["swiftc", "-O", str(SOURCE), "-o", str(partial)], capture_output=True, text=True,
                             timeout=COMPILE_TIMEOUT_S)
    except OSError as exc:
        raise PhoneError(f"phone: cannot run swiftc ({exc.strerror or exc}); Xcode's command-line tools hold it") from exc
    except subprocess.TimeoutExpired as exc:
        raise PhoneError(f"phone: swiftc did not finish within {COMPILE_TIMEOUT_S} s") from exc
    if out.returncode != 0 or not partial.is_file():
        tail = (out.stderr or out.stdout).strip().splitlines()[-3:]
        raise PhoneError("phone: swiftc failed to compile the recorder: " + " / ".join(tail))
    partial.replace(binary)
    marker.write_text(f"source {source_key()}\nbinary {sha256_of(binary)}\n")
    return binary


def devices(binary: Path) -> list[dict]:
    """The capture devices the Mac offers, from the recorder's `list`."""
    try:
        out = subprocess.run([str(binary), "list"], capture_output=True, text=True, timeout=LIST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhoneError(f"phone: the recorder could not list devices ({exc})") from exc
    if out.returncode != 0:
        raise PhoneError(f"phone: the recorder could not list devices (exit {out.returncode}): "
                         + (out.stderr.strip().splitlines() or ["no message"])[-1])
    try:
        found = json.loads(out.stdout.strip().splitlines()[-1])["devices"]
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise PhoneError(f"phone: the recorder's list is not readable: {out.stdout.strip()[:200]!r}") from exc
    return [d for d in found if isinstance(d, dict) and isinstance(d.get("id"), str)]


def is_phone(device: dict) -> bool:
    text = f"{device.get('model') or ''} {device.get('name') or ''}".lower()
    return any(word in text for word in PHONE_WORDS)


def choose(found: list[dict], prefix: str | None) -> dict:
    """The one device to record: the only iPhone or iPad offered, or the one device whose id starts with
    prefix (which may name another kind of device, explicitly)."""
    if prefix is not None:
        if not prefix.strip():
            raise PhoneError("phone: --device needs the start of a device's id (see `ui-probe phone list`)")
        hits = [d for d in found if d["id"].startswith(prefix)]
        if len(hits) != 1:
            raise PhoneError(f"phone: --device {prefix!r} matches {len(hits)} of the {len(found)} devices offered; it "
                             "must match exactly one (see `ui-probe phone list`)")
        return hits[0]
    phones = [d for d in found if is_phone(d)]
    if not phones:
        others = "; ".join(f"{d['id']} {d.get('name')} (model {d.get('model')})" for d in found)
        raise PhoneError("phone: no cabled iPhone's or iPad's screen is offered to this Mac (cable, unlock and trust "
                         "the Mac)" + (f"; other devices offered, recorded only when --device names one: {others}"
                                       if found else "") + f"; {AGAIN}")
    if len(phones) > 1:
        raise PhoneError(f"phone: {len(phones)} phones are offered; say which with --device <id-prefix>: "
                         + "; ".join(f"{d['id']} {d.get('name')}" for d in phones))
    return phones[0]


def notify(text: str) -> None:
    """A notification on the Mac (an owner's ruling: every recording is seen to begin)."""
    lines = [part for line in NOTICE_SCRIPT for part in ("-e", line)]
    try:
        out = subprocess.run(["osascript", *lines, "--", text, NOTICE_TITLE], capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhoneError(f"phone: the notice that a recording starts could not be posted ({exc}); nothing was "
                         "recorded") from exc
    if out.returncode != 0:
        raise PhoneError(f"phone: the notice that a recording starts could not be posted (osascript exit "
                         f"{out.returncode}); nothing was recorded")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Letters by their Unicode category that show nothing: the Hangul fillers and the Egyptian hieroglyph
# full and half blanks.
INVISIBLE_LETTERS = frozenset("\u115f\u1160\u3164\uffa0\U00013441\U00013442")


def has_word(text: str) -> bool:
    """Whether text holds a letter or a digit of any script (its Unicode category starts with L or N) that
    can be seen: a zero-width space, the Hangul fillers and the hieroglyph blanks hold none. It refuses a
    statement with nothing a person can read in it; whether the statement is true is not its to say."""
    return any(unicodedata.category(ch)[0] in "LN" and ch not in INVISIBLE_LETTERS for ch in text)


def check_record(args) -> tuple[float, Path]:
    """Every refusal that comes before anything is built, listed or written."""
    if args.seconds is None:
        raise PhoneError("phone: --seconds is required")
    try:
        seconds = float(args.seconds)
    except ValueError:
        raise PhoneError(f"phone: --seconds must be a number of seconds, not {args.seconds!r}") from None
    if not (0 < seconds <= CAP_S):
        raise PhoneError(f"phone: a recording lasts more than 0 and at most {CAP_S} s; anything longer needs a "
                         "second go and a second recording")
    if not has_word(args.go or ""):
        raise PhoneError("phone: --go is required: the owner's words, or the time they said go, for this recording "
                         "(at least one letter or digit)")
    if len([word for word in (args.build or "").split() if has_word(word)]) < 3:
        raise PhoneError('phone: --build is required: "<repository> <revision> <configuration>" (three words, each '
                         "with a letter or digit)")
    out = Path(args.out)
    if (out / LOCK).exists():
        raise PhoneError(held(out))
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PhoneError(f"phone: {out} exists and is not an empty folder; an output is never replaced")
    return seconds, out


def held(out: Path) -> str:
    return (f"phone: {out} is held by another recording ({out / LOCK} exists). If none is running, the lock was left "
            "by one that ended without removing it: record into a new folder (ui-probe removes nothing)")


def take_lock(out: Path) -> Path:
    """An exclusive lock in the output folder (a file created only if it does not exist), so two runs
    cannot share a folder; then the folder must hold nothing but that lock. A run that finds the lock or
    any other file refuses and leaves the folder as it found it."""
    lock = out / LOCK
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise PhoneError(held(out)) from None
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{os.getpid()}\n")
    others = sorted(p.name for p in out.iterdir() if p.name != LOCK)
    if others:
        lock.unlink()
        raise PhoneError(f"phone: {out} is not an empty folder (it holds {', '.join(others)}); an output is never "
                         "replaced")
    return lock


def create_json(path: Path, record_: dict) -> None:
    """Create a JSON file that must not exist yet (O_CREAT | O_EXCL): another run's record is never
    replaced. Later rewrites of this run's own file go through write_json."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise PhoneError(f"phone: {path} appeared while this run held the folder; it is not replaced, and nothing "
                         "was recorded") from None
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(record_, indent=1))


def record(args, version: str) -> int:
    seconds, out = check_record(args)
    out.mkdir(parents=True, exist_ok=True)
    # The lock first: compiling and listing can take minutes, and no other run may use the folder meanwhile.
    lock = take_lock(out)
    try:
        print(STATUS_BAR, flush=True)
        binary = build()
        recorder_sha = sha256_of(binary)
        # One recorder process from the listing to the end of the recording: it offers its devices and
        # waits for the chosen id, so the phone is never handed from one process to another.
        try:
            proc = subprocess.Popen([str(binary), "record", f"{seconds:g}", str(out / "screen.mov")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise PhoneError(f"phone: the recorder could not be started ({exc}); nothing was recorded") from exc
        try:
            offered, early = await_offer(proc, OFFER_TIMEOUT_S)
            device = choose(offered, args.device)
            return _record(args, version, seconds, out, proc, early, recorder_sha, device)
        finally:
            stop(proc)
    finally:
        lock.unlink(missing_ok=True)


def await_offer(proc, timeout: float) -> tuple[list[dict], tuple[bytes, bytes]]:
    """The recorder's one offered line, {"offered": [...]}, waited for at most timeout seconds. What it
    says on stderr meanwhile is passed on as it comes (that it waits for a phone, say). Returns the
    devices and what it wrote beyond that line, stdout and stderr. A recorder that offers nothing in
    time, offers what cannot be read or exits first is a refusal: nothing was chosen or recorded."""
    deadline = time.monotonic() + timeout
    out, err = b"", b""
    open_ = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    while b"\n" not in out:
        if "out" not in open_.values():
            try:
                code = proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                code = None
            said = (err.decode(errors="replace").strip().splitlines() or ["no message"])[-1]
            raise PhoneError(f"phone: the recorder ended before it offered a device (exit {code}): {said}; nothing was "
                             "recorded")
        left = deadline - time.monotonic()
        if left <= 0:
            raise PhoneError(f"phone: the recorder did not offer its devices within {timeout:g} s; nothing was recorded")
        ready, _, _ = select.select(list(open_), [], [], min(left, 1.0))
        for fd in ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                del open_[fd]
            elif open_[fd] == "out":
                out += chunk
            else:
                err += chunk
                sys.stderr.write(chunk.decode(errors="replace"))
                sys.stderr.flush()
    line, rest = out.split(b"\n", 1)
    try:
        found = json.loads(line)["offered"]
        if not isinstance(found, list):
            raise TypeError("not a list")
    except (ValueError, KeyError, TypeError) as exc:
        raise PhoneError(f"phone: the recorder's offer is not readable: {line[:200]!r}; nothing was recorded") from exc
    return [d for d in found if isinstance(d, dict) and isinstance(d.get("id"), str)], (rest, err)


def drain(proc, timeout: float) -> tuple[bytes, bytes]:
    """What the recorder writes until it ends, read with its stdin left OPEN: the recorder stops its
    recording when its stdin reaches end of input, which is how it learns that this program ended (killed,
    say) while it records. Its stderr is passed on as it comes. Raises subprocess.TimeoutExpired after
    timeout seconds."""
    deadline = time.monotonic() + timeout
    out, err = b"", b""
    open_ = {proc.stdout.fileno(): "out", proc.stderr.fileno(): "err"}
    while open_:
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        ready, _, _ = select.select(list(open_), [], [], min(left, 1.0))
        for fd in ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                del open_[fd]
            elif open_[fd] == "out":
                out += chunk
            else:
                err += chunk
                sys.stderr.write(chunk.decode(errors="replace"))
                sys.stderr.flush()
    proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
    return out, err


def stop(proc) -> None:
    """Close the recorder's input (it stops without recording, if it has not been told an id) and wait
    for it, at most STOP_TIMEOUT_S, then kill it. Nothing is left running."""
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    finally:
        for stream in (proc.stdout, proc.stderr):
            stream.close()


# Track types, as the recorder writes them (AVMediaType raw values): the picture, and those that can hold sound.
PICTURE_TRACK = "vide"
SOUND_TRACKS = frozenset({"soun", "muxx"})

# The recorder's own exit statuses for the picture-only rule, each with its status in film.json. They
# hold even when its summary is missing or cut short.
EXIT_STATUS = {8: "failed: the film holds sound", 9: "failed: the film could not be checked for sound",
               10: "failed: the film was not finalised",
               12: "failed: the recording was stopped: the recorder's input ended before the recording did"}


def read_summary(stdout: str):
    """The recorder's summary, its last line that is JSON: a dict, or None when there is no such line,
    or False when that line is not a JSON object."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        return value if isinstance(value, dict) else False
    return None


def _record(args, version, seconds, out, proc, early, recorder_sha, device) -> int:
    film = out / "film.json"
    movie = out / "screen.mov"
    record_ = {"tool": {"name": "ui-probe", "version": version}, "go": args.go, "build": args.build,
               "seconds_asked": seconds, "film": "screen.mov", "status": "recording", "written_utc": utc_now(),
               "device": {"id": device["id"], "name": device.get("name"), "model": device.get("model")},
               "recorder": {"sha256": recorder_sha, "source_sha256": source_key()},
               "sound_checked": False, "film_on_disk": False}
    # Written before anything is recorded, so a recording that never ends still says who said go.
    create_json(film, record_)
    try:
        notify(f"Recording {device.get('name') or 'the phone'}'s screen for {seconds:g} s (go: {args.go})")
    except PhoneError as exc:
        record_.update({"status": "not recorded", "failure": str(exc), "completed_utc": utc_now()})
        write_json(film, record_)
        raise
    except BaseException as exc:
        # Ctrl-C while the notice is posted: the recorder never started.
        record_.update({"status": "interrupted before recording: nothing was recorded",
                        "failure": f"{type(exc).__name__}: {exc}".rstrip(": "), "completed_utc": utc_now()})
        write_json(film, record_)
        raise
    print(f"recording {device.get('name')} ({device['id']}) for {seconds:g} s into {movie}", flush=True)
    try:
        # The chosen id, the only thing ever written to the recorder, after film.json and the notice. Its
        # stdin then stays open until it has ended (stop() closes it): should this program end first, the
        # end of input tells the recorder to stop recording.
        try:
            proc.stdin.write((device["id"] + "\n").encode())
            proc.stdin.flush()
        except BrokenPipeError:
            pass                        # it has ended (its wait for the id ran out, say): its own words follow
        rest_out, rest_err = drain(proc, seconds + 120)
        code = proc.returncode
        stdout = (early[0] + rest_out).decode(errors="replace")
        stderr = (early[1] + rest_err).decode(errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            rest_out, _rest_err = drain(proc, STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rest_out = b""
        code, stdout, stderr = None, (early[0] + rest_out).decode(errors="replace"), \
            f"the recorder did not end within {seconds + 120:g} s"
    except BaseException as exc:
        # Ctrl-C, a signal: the recorder is killed, and film.json says so.
        proc.kill()
        proc.wait()
        interrupted(record_, film, movie, exc)
        raise
    try:
        return conclude(record_, film, movie, code, stdout, stderr)
    except PhoneError:
        raise
    except BaseException as exc:
        # Ctrl-C after the recorder returned, before film.json was completed: it says so all the same.
        interrupted(record_, film, movie, exc)
        raise


def written_status(film: Path):
    """The status film.json holds on disk now, or None when it cannot be read."""
    try:
        return json.loads(film.read_text()).get("status")
    except (OSError, ValueError, AttributeError):
        return None


def interrupted(record_: dict, film: Path, movie: Path, exc: BaseException) -> None:
    """Ctrl-C or a signal. film.json is rewritten only while it still says "recording": a final status
    already written (a film that holds sound, say) stays as it is."""
    kept = written_status(film)
    if kept is not None and kept != "recording":
        if movie.exists() and kept != "done" and (kept == EXIT_STATUS[8] or not record_.get("sound_checked")):
            print(f"phone: interrupted after {film} was completed ({kept}). {delete_advice(movie)}", file=sys.stderr)
        return
    record_.update({"status": "interrupted", "failure": f"{type(exc).__name__}: {exc}".rstrip(": "),
                    "completed_utc": utc_now(), "film_on_disk": movie.exists(), "sound_checked": False,
                    "audio_tracks": None})
    write_json(film, record_)
    if record_["film_on_disk"]:
        print(f"phone: interrupted; the film was not checked for sound. {delete_advice(movie)} {film} says what "
              "is known", file=sys.stderr)


def conclude(record_: dict, film: Path, movie: Path, code, stdout: str, stderr: str) -> int:
    found = read_summary(stdout)
    summary = found or {}
    audio = summary.get("audio_tracks")
    tracks = summary.get("tracks")
    tracks = tracks if isinstance(tracks, list) and all(isinstance(t, str) for t in tracks) else None
    # Checked only when the recorder went through the count and reported a whole number (never True, 0.0)
    # and the film's tracks.
    checked = summary.get("sound_checked") is True and type(audio) is int and audio >= 0 and tracks is not None
    sound = (checked and audio > 0) or code == 8
    # No sound counted is believed only for a film with a picture track and no track that can hold sound.
    if checked and audio == 0 and (PICTURE_TRACK not in tracks or SOUND_TRACKS & set(tracks)):
        checked = False
    record_.update({"exit_status": code, "start_utc": summary.get("start_utc"), "stop_utc": summary.get("stop_utc"),
                    "seconds_recorded": summary.get("seconds_recorded"), "failure": summary.get("failure"),
                    "completed_utc": utc_now(), "ports": summary.get("ports"), "connected": summary.get("connected"),
                    "tracks": tracks, "audio_tracks": audio if checked else None,
                    "sound_checked": checked, "film_on_disk": movie.exists(),
                    "recorder_message": (stderr or "").strip().splitlines()[-1:] or None})
    if summary.get("device"):
        record_["device"].update({"name": summary["device"], "model": summary.get("model")})
    # The picture-only rule, checked on the film itself: a film that holds sound, or one that could not
    # be checked, fails. Nothing is deleted: the person who ran it deletes it.
    if sound:
        status = EXIT_STATUS[8]
    elif code in EXIT_STATUS:
        status = EXIT_STATUS[code]
    elif code is None:
        status = f"failed: {stderr}"
    elif found is False or (code == 0 and found is None):
        status = "failed: the recorder's summary could not be read"
    elif code == 0 and not checked:
        status = EXIT_STATUS[9]
    elif code == 0 and type(audio) is int and audio == 0:
        status = "done"
    else:
        status = "failed"
    record_["status"] = status
    write_json(film, record_)
    if status == "done":
        print(json.dumps({"film": str(movie), "film_json": str(film), "seconds_recorded": record_["seconds_recorded"]}))
        return 0
    failure = summary.get("failure")
    if sound:
        what = (f"the film holds sound ({audio} audio track{'s' if audio != 1 else ''})" if checked
                else f"the film holds sound (exit {code})")
        reason = f"{what}: it breaks the picture-only rule"
    elif status == "failed":
        reason = (f"the recording failed (exit {code}): "
                  + str(failure or ((stderr or "").strip().splitlines() or ["no message"])[-1]))
    else:
        reason = f"{status[len('failed: '):]} (exit {code})" + (f": {failure}" if failure and str(failure) not in status else "")
    advise = record_["film_on_disk"] and (sound or not checked)
    if advise and not sound:
        reason += ", so the picture-only rule is unchecked"
    raise PhoneError(f"phone: {reason}. " + (delete_advice(movie) + " " if advise else "") + f"{film} says what is known")


def delete_advice(movie: Path) -> str:
    """Said whenever a film is on disk that holds sound or was not checked for it."""
    return f"Whoever ran this should delete {movie}; ui-probe deletes nothing."


def list_devices() -> int:
    print(STATUS_BAR, flush=True)
    found = devices(build())
    for d in found:
        print(f"{d['id']}  {d.get('name')}  (model {d.get('model')})")
    if not any(is_phone(d) for d in found):
        print(f"no phone is offered to this Mac (cable, unlock and trust the Mac); {AGAIN}", flush=True)
    return 0


def main(argv: list[str], version: str) -> int:
    ap = argparse.ArgumentParser(prog="ui-probe phone", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the cabled phones whose screen the Mac can record")
    r = sub.add_parser("record", help="record one phone's screen, on its owner's go")
    r.add_argument("--seconds", required=True, help=f"how long, at most {CAP_S}")
    r.add_argument("--out", required=True, help="a folder that does not exist or is empty")
    r.add_argument("--go", required=True, help="the owner's words, or the time they said go, for this recording")
    r.add_argument("--build", required=True, help='"<repository> <revision> <configuration>" of the app filmed')
    r.add_argument("--device", default=None, help="the start of the phone's id, when more than one is offered")
    args = ap.parse_args(argv)
    try:
        refuse_other_systems()
        return list_devices() if args.cmd == "list" else record(args, version)
    except PhoneError as exc:
        print(str(exc), file=sys.stderr)
        return 2
