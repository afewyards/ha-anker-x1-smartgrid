#!/usr/bin/env bash
# sync_core.sh — vendor HA-free modules from custom_components into forecast_core/
# Run from anywhere; paths resolve relative to this script's location.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/../../custom_components/anker_x1_smartgrid"
DEST="$SCRIPT_DIR/forecast_core"

MODULES=(
    const.py
    dataquality.py
    rollup.py
    loadmodel.py
    featureset.py
    recorder.py
    hgbr.py
    backtest.py
)

echo "Syncing from: $SRC"
echo "       into:  $DEST"
echo ""

for module in "${MODULES[@]}"; do
    cp "$SRC/$module" "$DEST/$module"
    echo "  copied $module"
done

echo ""
echo "Regenerating $DEST/SOURCE_SHA256 ..."
MANIFEST="$DEST/SOURCE_SHA256"
: > "$MANIFEST"
for module in "${MODULES[@]}"; do
    shasum -a 256 "$SRC/$module" | awk -v m="$module" '{print $1 "  " m}' >> "$MANIFEST"
done
sort -k2 "$MANIFEST" -o "$MANIFEST"
echo "  wrote SOURCE_SHA256 ($(wc -l < "$MANIFEST") entries)"

echo ""
echo "Done."
