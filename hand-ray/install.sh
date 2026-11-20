#!/bin/sh
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)/driver/frame_hand_ray"
VRPATHS="$HOME/.config/openvr/openvrpaths.vrpath"
VRPATHREG=/opt/steamvr/bin/linuxarm64/vrpathreg
[ -f "$DIR/bin/linuxarm64/driver_frame_hand_ray.so" ] || { echo "Driver not built. Run ./build.sh first."; exit 1; }
PYTHON="$(cd "$(dirname "$0")/.." && pwd)/runtime/bin/python3"
[ -x "$PYTHON" ] || PYTHON=python3
OTHER=$("$PYTHON" - "$VRPATHS" "$DIR" <<'PY'
import json, os, sys
try:
    paths = json.load(open(sys.argv[1])).get("external_drivers") or []
except FileNotFoundError:
    paths = []
for p in paths:
    if os.path.realpath(p) == os.path.realpath(sys.argv[2]):
        continue
    try:
        if json.load(open(os.path.join(p, "driver.vrdrivermanifest"))).get("name") == "frame_hand_ray":
            print(p)
    except (OSError, ValueError):
        pass
PY
)
if [ -n "$OTHER" ]; then
  echo "Another frame_hand_ray driver is already registered:"; echo "$OTHER"
  echo "Remove it first: $VRPATHREG removedriver <path>"; exit 1
fi
[ -f "$VRPATHS" ] && cp "$VRPATHS" "$VRPATHS.bak-frame_hand_ray-$(date +%Y%m%dT%H%M%S)"
"$VRPATHREG" adddriver "$DIR"
"$VRPATHREG" show | grep -A8 -i "external drivers" || true
echo "Registered. Restart the session (or reboot) to load it, then check:"
echo "  grep -a 'frame_hand_ray' ~/.local/share/Steam/logs/vrserver.txt | tail"
