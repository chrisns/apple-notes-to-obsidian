#!/usr/bin/env bash
# One-shot installer: checks deps, builds the Swift decoders, drops the CSS
# snippet into your vault, and registers `created`/`modified` as datetime
# properties. Idempotent — safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

VAULT="${1:-}"
if [ -z "$VAULT" ]; then
  echo "usage: $0 <path-to-obsidian-vault>" >&2
  echo "example: $0 ~/Documents/MyVault" >&2
  exit 1
fi

VAULT="${VAULT/#\~/$HOME}"
if [ ! -d "$VAULT" ]; then
  echo "✗ vault not found: $VAULT" >&2
  exit 1
fi
if [ ! -d "$VAULT/.obsidian" ]; then
  echo "✗ no .obsidian directory in $VAULT — open it in Obsidian once first" >&2
  exit 1
fi

echo "→ checking dependencies"

missing=()

if ! command -v python3 >/dev/null 2>&1; then missing+=("python3"); fi
if ! command -v pandoc  >/dev/null 2>&1; then missing+=("pandoc (brew install pandoc)"); fi
if ! command -v xcrun   >/dev/null 2>&1; then missing+=("xcode (full Xcode, not just CommandLineTools)"); fi

SDK_PRIVATE_F="$(xcode-select -p 2>/dev/null)/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/PrivateFrameworks"
if [ ! -d "$SDK_PRIVATE_F" ]; then
  missing+=("Xcode SDK PrivateFrameworks (run: sudo xcode-select -s /Applications/Xcode.app)")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  echo "✗ missing: ${missing[*]}" >&2
  exit 1
fi

if ! python3 -c 'import fractional_indexing' >/dev/null 2>&1; then
  echo "→ installing fractional-indexing"
  python3 -m pip install --user --quiet fractional-indexing 2>/dev/null \
    || python3 -m pip install --user --quiet --break-system-packages fractional-indexing
fi

echo "→ building Swift decoders"
./build.sh

echo "→ installing CSS snippet"
mkdir -p "$VAULT/.obsidian/snippets"
cp obsidian/snippets/ink-stroke-fill.css "$VAULT/.obsidian/snippets/"

echo "→ registering created/modified as datetime properties"
TYPES="$VAULT/.obsidian/types.json"
python3 - "$TYPES" <<'PY'
import json, os, sys
p = sys.argv[1]
data = {}
if os.path.exists(p):
    try:
        data = json.load(open(p))
    except Exception:
        data = {}
data.setdefault("types", {})
data["types"]["created"]  = "datetime"
data["types"]["modified"] = "datetime"
json.dump(data, open(p, "w"), indent=2)
PY

cat <<MSG

✓ ready

Two more one-time clicks inside Obsidian:
  1. Settings → Community plugins → install "Ink" (DefenderOfBasic/obsidian-ink)
  2. Settings → Appearance → CSS snippets → enable "ink-stroke-fill"

And one in System Settings:
  3. Privacy & Security → Full Disk Access → add your terminal
     (Apple Notes' SQLite is sandboxed; first run will fail otherwise)

Then run:
  python3 migrate.py --sync "$VAULT/Notes/Inbox"

MSG
