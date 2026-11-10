# Changelog — ui-probe

## 0.1.0 — 2026-11-10

First release. Android, driven over adb from an unmodified build of the app under test.

- `run`: play a scenario file (launch, rest, tap, swipe, key) on a device and keep independent
  witnesses of every step: the platform's frame records, the compositor's per-layer statistics
  (any toolkit, including a surface the app draws itself), a screen recording, and optionally
  the compositor's frame timeline (`--trace`) and trace-section counts (`--sections`). Steps can
  wait until the app and the display go quiet. The scenario file is checked strictly: an unknown
  key or a wrong type stops the run before it touches the device.
- `analyze`, `summary`: redo the analysis from the saved capture; statistics across runs.
- `gate`: the checks that need no baseline (frames, CPU and recorded frames while the screen is
  at rest; a screen that never goes quiet; process restarts) plus any expectations a scenario
  states per step. Exit 1 on a violation, `--advisory` to report only.
- A pass means the app was observed. A value that could not be read is unknown, never zero, and
  the gate fails or says so in a note instead of passing in silence: an app that did not start
  or drew nothing while launching, another app in front during a rest, a missing snapshot, a
  capture that was never analysed, a recording that is missing, stopped early or cannot be
  placed in the step windows, a trace that is missing, lost data or does not cover a step, a
  trace section the app never emitted.
- `compare`: base runs against head runs with a rank test (exact for small samples) and an
  effect size; answers different, same or unknown per metric.
- `sheet`, `zoom`: a labelled contact sheet per step, and a full-resolution look at what changed
  between two recorded frames.
- `proxy`: a forward proxy that adds latency and a rate limit per connection without decrypting
  anything, for imposing a slow network with no administrator rights.
- `doctor`: host and device prerequisites, including ffmpeg 5.1 or newer.
- Exit codes: 0 success, 1 a failing gate only, 2 an error the tool reports itself, 3 an
  unexpected error with a traceback.
- The device is left as it was found: the device script restores the compositor's statistics
  switch and stops the recorder however it ends, and the host stops the tracer and cleans up
  when a run fails.
- `scripts/install.sh` installs a versioned copy and a `ui-probe` launcher. An optional pinned
  installer fetches the Perfetto trace processor (v59.1); the probe itself reads traces with a
  built-in reader and does not need it.
