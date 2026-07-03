#!/bin/sh
# Pinned installer for the Android agent CLI launcher.
# https://developer.android.com/tools/agents/android-cli
#
# Downloads the exact launcher version this plugin was validated against,
# verifies its SHA-256, installs it as `android` in ~/.local/bin, opts out of
# telemetry via ~/.androidrc (pass --allow-metrics to skip that), and runs the
# binary once so it bootstraps its payload into ~/.android/{bin,cli}.
#
# Idempotent: exits 0 without changes if the pinned version is already installed.
# Never auto-updates: bumping the pin is a deliberate maintainer action
# (edit PINNED_VERSION + checksums, re-validate, commit).

set -eu

PINNED_VERSION="1.0.15498356"
SHA_DARWIN_ARM64="288c28a83023fb2c2385dc9f7ed4497d3ef7d39111213bcdb4cb30a93d0243fc"
SHA_DARWIN_X86_64="13f599a88471996b690446439059b1959c3c56ab25e88fc00127a48594e2412e"
SHA_LINUX_X86_64="24ff3bac5db16e5bcc5fd2a54dc58041fd06db373389cd6442dedb004fe092be"

ALLOW_METRICS=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --allow-metrics) ALLOW_METRICS=1 ;;
    --force) FORCE=1 ;;
    *) echo "unknown option: $arg (supported: --allow-metrics, --force)" >&2; exit 2 ;;
  esac
done

case "$(uname -s)/$(uname -m)" in
  Darwin/arm64)  PLATFORM="darwin_arm64";  EXPECTED_SHA="$SHA_DARWIN_ARM64" ;;
  Darwin/x86_64) PLATFORM="darwin_x86_64"; EXPECTED_SHA="$SHA_DARWIN_X86_64" ;;
  Linux/x86_64)  PLATFORM="linux_x86_64";  EXPECTED_SHA="$SHA_LINUX_X86_64" ;;
  *) echo "unsupported platform $(uname -s)/$(uname -m) — install manually: https://developer.android.com/tools/agents" >&2; exit 1 ;;
esac

URL="https://dl.google.com/android/cli/${PINNED_VERSION}/${PLATFORM}/android"
DEST_DIR="${HOME}/.local/bin"
DEST="${DEST_DIR}/android"

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else sha256sum "$1" | cut -d' ' -f1; fi
}

# Already installed at the pinned version?
EXISTING=""
if command -v android >/dev/null 2>&1; then EXISTING="$(command -v android)"
elif [ -x "$DEST" ]; then EXISTING="$DEST"; fi
if [ -n "$EXISTING" ] && [ "$FORCE" -eq 0 ]; then
  CURRENT_VERSION="$("$EXISTING" --version 2>/dev/null | head -1 || true)"
  if [ "$CURRENT_VERSION" = "$PINNED_VERSION" ]; then
    echo "android CLI ${PINNED_VERSION} already installed at ${EXISTING} — nothing to do"
    exit 0
  fi
  echo "note: existing android CLI at ${EXISTING} reports '${CURRENT_VERSION}' (pin is ${PINNED_VERSION}); installing pinned version to ${DEST}" >&2
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
echo "downloading ${URL}"
curl -fsSL -o "$TMP" "$URL"

ACTUAL_SHA="$(sha256 "$TMP")"
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  echo "CHECKSUM MISMATCH for ${PLATFORM} @ ${PINNED_VERSION}" >&2
  echo "  expected: ${EXPECTED_SHA}" >&2
  echo "  actual:   ${ACTUAL_SHA}" >&2
  echo "refusing to install — the published artifact changed; re-verify and re-pin" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
install -m 0755 "$TMP" "$DEST"
if [ "$(uname -s)" = "Darwin" ]; then
  /usr/bin/xattr -d com.apple.quarantine "$DEST" 2>/dev/null || true
fi

# Telemetry opt-out (root-level flag; ~/.androidrc applies it to every invocation).
if [ "$ALLOW_METRICS" -eq 0 ]; then
  RC="${HOME}/.androidrc"
  if [ ! -f "$RC" ] || ! grep -q -- '--no-metrics' "$RC"; then
    printf -- '--no-metrics\n' >> "$RC"
    echo "added --no-metrics to ${RC}"
  fi
fi

# First run bootstraps the payload into ~/.android/{bin,cli}.
"$DEST" --version >/dev/null 2>&1 || true

RECEIPT="${HOME}/.android/cli-install-receipt.txt"
mkdir -p "${HOME}/.android"
{
  echo "version: ${PINNED_VERSION}"
  echo "platform: ${PLATFORM}"
  echo "url: ${URL}"
  echo "sha256: ${ACTUAL_SHA}"
  echo "installed_to: ${DEST}"
  echo "installed_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$RECEIPT"

echo "installed android CLI ${PINNED_VERSION} -> ${DEST} (receipt: ${RECEIPT})"
case ":${PATH}:" in
  *":${DEST_DIR}:"*) ;;
  *) echo "note: ${DEST_DIR} is not on your PATH — add it, or invoke ${DEST} directly" ;;
esac
