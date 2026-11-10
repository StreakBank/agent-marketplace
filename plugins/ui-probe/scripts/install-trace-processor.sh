#!/bin/sh
# Optional: install Google's Perfetto trace processor, pinned by version and SHA-256.
#
# ui-probe reads traces with its own reader and never calls this binary. Install it when you
# want to query a saved trace yourself (scheduling, startup, memory tables).
#
# Usage:
#   scripts/install-trace-processor.sh            # print the plan, change nothing
#   scripts/install-trace-processor.sh --apply    # download, verify, install
#
# Pins: version, URLs and checksums below come from the vendor's own launcher manifest for
# this release. Bumping the pin = edit all three together, re-validate, commit. Never auto-update.
set -eu

PIN_VERSION="v59.1"
BASE="https://commondatastorage.googleapis.com/perfetto-luci-artifacts/$PIN_VERSION"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS-$ARCH" in
  Darwin-arm64)   SLUG="mac-arm64";   SUM="8488edbefadfc4be152cda1d42d9bfcede8b47638295eb85eb3bad8cd148fdf4" ;;
  Darwin-x86_64)  SLUG="mac-amd64";   SUM="df38039845244c353c399994748599345cf08f740491b5832e7feb9a4dbcc476" ;;
  Linux-x86_64)   SLUG="linux-amd64"; SUM="2434129a3af4dc9e2034a476f7b76447b490975213e301769310d3bd0d5d6458" ;;
  Linux-aarch64)  SLUG="linux-arm64"; SUM="a08e4836171c87be3ba5834eef3489aa8803476d35f292ed7feaccae252747d0" ;;
  *) echo "install-trace-processor: no pinned build for $OS $ARCH" >&2; exit 1 ;;
esac

URL="$BASE/$SLUG/trace_processor_shell"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/ui-probe/tools"
DEST="$DEST_DIR/trace_processor_shell-$PIN_VERSION"
BIN="${PREFIX:-$HOME/.local/bin}"
LINK="$BIN/trace_processor_shell"
MARK="# trace processor launcher (written by ui-probe's install-trace-processor.sh)"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

echo "version:  $PIN_VERSION ($SLUG)"
echo "url:      $URL"
echo "sha256:   $SUM"
echo "binary:   $DEST"
echo "launcher: $LINK"
if [ "$APPLY" -ne 1 ]; then
  echo "plan only; rerun with --apply to install"
  exit 0
fi

if [ -e "$LINK" ] && ! grep -qF "$MARK" "$LINK" 2>/dev/null; then
  echo "install-trace-processor: $LINK exists and was not written by this installer; not replacing it" >&2
  exit 1
fi

mkdir -p "$DEST_DIR" "$BIN"
if [ ! -f "$DEST" ] || [ "$(sha256_of "$DEST")" != "$SUM" ]; then
  TMP="$DEST.download"
  if ! curl -fsSL "$URL" -o "$TMP"; then
    rm -f "$TMP"
    echo "install-trace-processor: download failed; nothing installed" >&2
    exit 1
  fi
  GOT="$(sha256_of "$TMP")"
  if [ "$GOT" != "$SUM" ]; then
    rm -f "$TMP"
    echo "install-trace-processor: checksum mismatch (got $GOT); nothing installed" >&2
    exit 1
  fi
  mv "$TMP" "$DEST"
fi
chmod +x "$DEST"
# Single-quote the path (each ' inside written as '\'') so any character in it is taken literally.
quoted="$(printf '%s' "$DEST" | sed "s/'/'\\\\''/g"; printf x)"
quoted="${quoted%x}"
{
  echo "#!/bin/sh"
  echo "$MARK"
  printf "exec '%s' \"\$@\"\n" "$quoted"
} > "$LINK"
chmod +x "$LINK"
echo "installed: $("$LINK" --version | head -1)"
