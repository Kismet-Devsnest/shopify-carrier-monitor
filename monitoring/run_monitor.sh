#!/usr/bin/env bash
# Entry point for the carrier monitor (GitHub Actions or local).
# Installs Python Playwright if missing (Chromium is pre-installed at
# /opt/pw-browsers in the cloud image), then runs the monitor (visible browser on a virtual display by default).
set -uo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import playwright" 2>/dev/null; then
    pip install -q "playwright==1.56.0" >/dev/null 2>&1
fi

export ARTIFACT_DIR="${ARTIFACT_DIR:-$PWD/artifacts}"
rm -rf "$ARTIFACT_DIR"
export NUM_RUNS="${NUM_RUNS:-3}"
if [ "${HEADLESS:-0}" != "1" ] && [ -z "${DISPLAY:-}" ]; then
    # Visible browser needs a screen; use a virtual one in the cloud.
    exec xvfb-run -a -s "-screen 0 1366x900x24" python3 shopify_carrier_monitor_cloud.py
fi
python3 shopify_carrier_monitor_cloud.py
