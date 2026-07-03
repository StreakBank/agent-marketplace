# Provenance — android-device

## Wrapped binary

- **Tool:** Android agent CLI (Google), https://developer.android.com/tools/agents/android-cli
- **Pinned launcher version:** `1.0.15498356`
- **Pinned URLs** (version-addressed, verified live 2026-07-03):
  - `https://dl.google.com/android/cli/1.0.15498356/darwin_arm64/android`
    — SHA-256 `288c28a83023fb2c2385dc9f7ed4497d3ef7d39111213bcdb4cb30a93d0243fc`
  - `https://dl.google.com/android/cli/1.0.15498356/darwin_x86_64/android`
    — SHA-256 `13f599a88471996b690446439059b1959c3c56ab25e88fc00127a48594e2412e`
  - `https://dl.google.com/android/cli/1.0.15498356/linux_x86_64/android`
    — SHA-256 `24ff3bac5db16e5bcc5fd2a54dc58041fd06db373389cd6442dedb004fe092be`
- Google also publishes a Homebrew cask (`android/homebrew-tap`, cask
  `android-cli`) but it tracks `version :latest` with `sha256 :no_check` — the
  pinned installer here is stricter, which is why we don't just point at brew.

## Consulted upstream skill (NOT vendored)

- **Repo:** `android/skills` (Google, Apache-2.0)
- **Commit:** `07302ca15e21d827cab5ca64d46407fb51dbe0aa` (2026-06-24)
- **Files consulted:** `devtools/android-cli/SKILL.md`,
  `devtools/android-cli/references/interact.md`,
  `devtools/android-cli/references/journeys.md`
- **Why not vendored:** the upstream SKILL.md embeds the full `android help`
  dump and promotes journeys / project creation / SDK management / CLI
  self-update — scope this plugin deliberately excludes. The interaction
  protocol in this plugin's SKILL.md (layout-first, diff-preferred,
  focused-before-typing, slow scrolls, wait-then-diff, annotate-as-fallback)
  is derived from `references/interact.md` at the commit above; refresh by
  diffing that file against the recorded commit.

## Pin scope (what the SHA-256 actually covers)

The pinned + checksummed artifact is **only the ~3.6 MB launcher binary**. On first
run the launcher fetches its payload (`main.jar` + a bundled JRE) into
`~/.android/cli/bundles/<id>/`. Whether that payload is locked to the launcher
version or floats to latest is **not controlled by this plugin** and was not
verifiable without a clean host (on the trial machine the pinned launcher reused the
already-present bundle `d0f57656…`). Consequence: the pin guarantees a reproducible
*launcher*, not a fully reproducible *toolchain*. The installer records the resolved
launcher version + on-disk bundle id in `~/.android/cli-install-receipt.txt` so drift
is auditable; treat a changed bundle id across installs as a re-validate signal. The
`docs` knowledge base is likewise a server-fetched, evolving corpus, not pinned.

## Validation + revalidation

- **Validated:** 2026-07-03, launcher `1.0.15498356`, live emulator + debug APK.
  Command set, timings, and cautions (blocking start ~15 s warm, `layout --diff`
  context savings, `describe` Gradle injection, annotation noise, journeys verdict,
  `screen resolve --screenshot` flag, no `--device` on `screen`, grouped `--diff`).
- **Revalidate by / on:** the next launcher pin bump, or if the bundle id changes, or
  ~2026-10 (quarterly) — whichever first. Revalidation = re-run the documented command
  set against a live device, update timings/cautions, then re-pin.
- **Upstream refresh (`android/skills`):** diff `devtools/android-cli/references/interact.md`
  at HEAD against recorded commit `07302ca…`; re-derive the protocol if it moved.
