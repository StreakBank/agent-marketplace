#!/bin/sh
# Install a `ui-probe` launcher that runs THIS copy's version from a versioned directory.
#
# Usage:
#   scripts/install.sh                    # print the plan, change nothing
#   scripts/install.sh --apply            # copy the scripts and write the launcher
#   scripts/install.sh --apply --force    # also replace a same-version copy whose files differ
#   PREFIX=<dir> scripts/install.sh --apply   # launcher directory (default: $HOME/.local/bin)
#
# The scripts are copied to ${XDG_DATA_HOME:-$HOME/.local/share}/ui-probe/<version>/ so the
# launcher keeps pointing at one validated version until this installer is run again. A copy of
# the same version with different content is not replaced without --force: one version number,
# one set of files. An existing launcher is replaced only if this installer wrote it.
set -eu

HERE="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
APPLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    *) echo "install: unknown argument $arg (use --apply, --force)" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "install: python3 is required" >&2; exit 1; }
VERSION="$(python3 -B "$HERE/ui_probe.py" --version | awk '{print $2}')"
[ -n "$VERSION" ] || { echo "install: could not read the version" >&2; exit 1; }

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ui-probe/$VERSION"
BIN="${PREFIX:-$HOME/.local/bin}"
LAUNCHER="$BIN/ui-probe"
MARK="# ui-probe launcher (written by the plugin's install.sh)"
FILES="ui_probe.py ui_video.py pftrace.py shapeproxy.py tlsfront.py httppipe.py httpfront.py"
# The input agent for the iOS Simulator, copied beside the scripts (built on first use, into a cache).
AGENT_FILES="ios-agent/Agent.xcodeproj/project.pbxproj ios-agent/Agent.xcodeproj/xcshareddata/xcschemes/Agent.xcscheme ios-agent/AgentUITests/AgentUITests.swift"
AGENT_SRC="$HERE/.."
for f in $AGENT_FILES; do
  [ -f "$AGENT_SRC/$f" ] || { echo "install: $AGENT_SRC/$f is missing" >&2; exit 1; }
done

if [ -e "$LAUNCHER" ] && ! grep -qF "$MARK" "$LAUNCHER" 2>/dev/null; then
  echo "install: $LAUNCHER exists and was not written by this installer; not replacing it" >&2
  exit 1
fi

DIFFERS=""
if [ -d "$DATA" ]; then
  for f in $FILES; do
    if ! cmp -s "$HERE/$f" "$DATA/$f"; then DIFFERS="$DIFFERS $f"; fi
  done
  for f in $AGENT_FILES; do
    if ! cmp -s "$AGENT_SRC/$f" "$DATA/$f"; then DIFFERS="$DIFFERS $f"; fi
  done
fi

echo "version:  $VERSION"
echo "scripts:  $DATA"
echo "launcher: $LAUNCHER"
if [ -n "$DIFFERS" ]; then
  echo "note: $DATA already holds version $VERSION with different files:$DIFFERS"
fi
if [ "$APPLY" -ne 1 ]; then
  echo "plan only; rerun with --apply to install"
  exit 0
fi
if [ -n "$DIFFERS" ] && [ "$FORCE" -ne 1 ]; then
  echo "install: refusing to replace a different copy of version $VERSION; rerun with --force to replace it" >&2
  exit 1
fi

mkdir -p "$DATA" "$BIN"
for f in $FILES; do
  cp "$HERE/$f" "$DATA/$f"
done
for f in $AGENT_FILES; do
  mkdir -p "$(dirname -- "$DATA/$f")"
  cp "$AGENT_SRC/$f" "$DATA/$f"
done

# Quote the path for the launcher so that any character in it is taken literally: wrap it in
# single quotes and write each single quote inside it as '\''. The trailing x keeps a path that
# ends in a newline intact through the command substitution.
quoted="$(printf '%s' "$DATA/ui_probe.py" | sed "s/'/'\\\\''/g"; printf x)"
quoted="${quoted%x}"
{
  echo "#!/bin/sh"
  echo "$MARK"
  printf "exec python3 -B '%s' \"\$@\"\n" "$quoted"
} > "$LAUNCHER"
chmod +x "$LAUNCHER"
echo "installed: $("$LAUNCHER" --version)"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on PATH; call the launcher by its full path or add it" ;;
esac
