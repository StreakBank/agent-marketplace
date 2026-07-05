#!/bin/sh
# The mechanical admission gate: prove a plugin's core carries zero project coupling.
# Single canonical owner of the grep test — CONTRIBUTING.md and the discipline doc
# point HERE rather than re-inlining the patterns (which drifted at day zero).
#
# Usage:
#   scripts/check-coupling.sh                 # check every plugin
#   scripts/check-coupling.sh android-device  # check one plugin
#   PROJECT_NAMES="streakbank ladderpicks" scripts/check-coupling.sh
#
# Exit 0 = clean; exit 1 = coupling found (or a plugin is missing required files).
#
# The attribution carve-out (CONTRIBUTING §1): the hosting org's name is allowed in
# repo-address install lines and in .claude-plugin/*.json author/owner fields — that
# is distribution metadata, not model-facing content. Everything else is scanned.

set -eu

ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

# Project/org names that must never appear as content. Override via PROJECT_NAMES.
# Default list = the orgs/projects that have authored plugins here so far.
PROJECT_NAMES="${PROJECT_NAMES:-streakbank ladderpicks}"
NAME_RE="$(printf '%s' "$PROJECT_NAMES" | tr ' ' '|')"

PLUGINS="${1:-}"
if [ -z "$PLUGINS" ]; then
  PLUGINS="$(ls plugins 2>/dev/null || true)"
fi

fail=0
for p in $PLUGINS; do
  dir="plugins/$p"
  [ -d "$dir" ] || { echo "FAIL[$p]: no such plugin dir ($dir)"; fail=1; continue; }

  # Model-facing content = everything EXCEPT the manifest json (author/owner exempt).
  # We scan skills/, scripts/, references/, PROVENANCE.md, README.md, and any *.md.
  content_files="$(find "$dir" -type f \
    \( -name '*.md' -o -path '*/skills/*' -o -path '*/scripts/*' -o -path '*/references/*' \) \
    -not -path '*/.git/*' | sort -u)"

  # Carve-out (CONTRIBUTING §1): distribution metadata is not model-facing content.
  # Two kinds, handled differently:
  #  (a) whole install/clone lines — the marketplace's own slug + clone/install verbs.
  #      Dropped whole via ADDR_RE (case-insensitively).
  #  (b) attribution / schema URLs (any scheme:// URL, and bare github.com /
  #      raw.githubusercontent.com hosts) — only the URL TOKEN is exempt, so a plugin's
  #      own repo/schema URL may name the org while coupling PROSE sharing that SAME
  #      physical line is still caught. Blanked per-token (case-folded) before the name
  #      grep, NOT dropped as a whole line.
  # The name grep is case-INSENSITIVE, so mixed-case "StreakBank" in prose is caught;
  # the URL blanking folds case identically so a "GitHub.com" attribution URL is not
  # spuriously flagged.
  ADDR_RE='(agent-marketplace(\.git)?|repo clone |marketplace add )'

  # *TEMPLATE* files are shim templates by design — they legitimately show shim SHAPE,
  # including a destination like .claude/rules/<name>.md. Exempt them from the
  # named-rule-ref check (but NOT from project-name / absolute-path checks; a template
  # must still never hardcode a real project name or a real /Users path).
  scan_no_addr() { printf '%s\n' "$content_files" | while IFS= read -r f; do
    [ -n "$f" ] && sed -E \
        -e 's#[A-Za-z][A-Za-z0-9+.-]*://[^ )"]*##g' \
        -e 's#[Gg]it[Hh]ub\.com/[^ )"]*##g' \
        -e 's#[Rr]aw\.[Gg]ithubusercontent\.com/[^ )"]*##g' \
        "$f" 2>/dev/null | grep -IniE "$1" | grep -viE "$ADDR_RE" | sed "s|^|$f:|"
  done; }

  # 1) project names (case-insensitive) in content, excluding repo-address lines
  if scan_no_addr "($NAME_RE)" | grep -iq .; then
    echo "FAIL[$p]: project name(s) matching /($NAME_RE)/i in model-facing content:"
    scan_no_addr "($NAME_RE)" | grep -iE "($NAME_RE)"; fail=1
  fi
  # 2) absolute project paths
  if printf '%s\n' "$content_files" | xargs grep -rlE '/Users/|/home/|~/Projects' 2>/dev/null; then
    echo "FAIL[$p]: absolute project path(s) in content"; fail=1
  fi
  # 3) NAMED project rule/skill references (templates exempt — see above)
  non_template="$(printf '%s\n' "$content_files" | grep -v 'TEMPLATE' || true)"
  if [ -n "$non_template" ] && printf '%s\n' "$non_template" | xargs grep -rlE '\.claude/(rules|skills)/[a-z0-9._-]+' 2>/dev/null; then
    echo "FAIL[$p]: named .claude/(rules|skills)/<file> reference in non-template content"; fail=1
  fi

  # 4) required files present
  [ -f "$dir/.claude-plugin/plugin.json" ] || { echo "FAIL[$p]: missing .claude-plugin/plugin.json"; fail=1; }

  # 5) vendored content requires PROVENANCE (heuristic: a references/ dir or a LICENSE
  #    adjacent to vendored files implies vendoring → PROVENANCE.md must exist).
  if [ -d "$dir/references" ] && [ ! -f "$dir/PROVENANCE.md" ]; then
    echo "FAIL[$p]: has references/ (vendored content) but no PROVENANCE.md"; fail=1
  fi

  [ "$fail" -eq 0 ] && echo "ok[$p]: coupling-clean, manifest present"
done

# 6) marketplace manifest is valid JSON and lists every plugin dir
if command -v python3 >/dev/null 2>&1; then
  python3 - "$ROOT" <<'PY' || fail=1
import json, os, sys
root = sys.argv[1]
mp = os.path.join(root, ".claude-plugin", "marketplace.json")
data = json.load(open(mp))
listed = {p["name"] for p in data.get("plugins", [])}
ondisk = {d for d in os.listdir(os.path.join(root, "plugins"))
          if os.path.isdir(os.path.join(root, "plugins", d))}
missing = ondisk - listed
if missing:
    print(f"FAIL[marketplace.json]: plugins on disk not listed: {sorted(missing)}")
    sys.exit(1)
for p in data.get("plugins", []):
    pj = os.path.join(root, "plugins", p["name"], ".claude-plugin", "plugin.json")
    json.load(open(pj))  # raises if invalid
print(f"ok[marketplace.json]: valid, {len(listed)} plugin(s) listed and on disk")
PY
fi

if [ "$fail" -ne 0 ]; then
  echo "COUPLING CHECK FAILED"; exit 1
fi
echo "COUPLING CHECK PASSED"
