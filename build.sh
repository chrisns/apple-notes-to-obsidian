#!/usr/bin/env bash
# Compile the two Swift decoders. Requires Xcode command-line tools and an
# Xcode install (we lean on the macOSX SDK's PrivateFrameworks for the
# Coherence + PaperKit linkage).
set -euo pipefail

cd "$(dirname "$0")"

SDK_PRIVATE_F="$(xcode-select -p)/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/PrivateFrameworks"
if [ ! -d "$SDK_PRIVATE_F" ]; then
  echo "Xcode SDK PrivateFrameworks dir not found at: $SDK_PRIVATE_F" >&2
  echo "Install Xcode (not just CommandLineTools) and run 'sudo xcode-select -s /Applications/Xcode.app'." >&2
  exit 1
fi

echo "→ building decode_pkdrawing"
xcrun swiftc decode_pkdrawing.swift -o decode_pkdrawing

echo "→ building decode_paper (Coherence + PaperKit shim)"
xcrun swiftc \
  -I shims/CoherenceShim \
  -I shims/PaperKitShim \
  decode_paper.swift \
  -F "$SDK_PRIVATE_F" \
  -framework Coherence -framework PaperKit \
  -o decode_paper

echo "✓ built decode_pkdrawing and decode_paper"
