#!/bin/bash
set -euo pipefail
usage() {
  cat <<'EOF'
Installs the "Hand tracking" panel for the SteamVR dashboard and the hand tracking service it switches, as
user services of the current user (no root):
  frame-hand-tracking-panel.service  the panel (panel/), runs with SteamVR
  frame-hand-tracking.service        hand tracking (run.sh --service); off until you turn it on in the panel
Needs the Frame Camera Access service installed and built from a Steam-Frame-Camera-Access checkout next to this one
(FCA_SOURCE_DIR overrides it): the panel shares its drawing code and the worker its client library.
  scripts/install-panel.sh [--tracker nview|two] [--prebuilt]
      --tracker   nview (default): the N-view fork on all four cameras; two: Mercury on the SLAM pair
      --prebuilt  use an already built panel/build/frame-hand-tracking-panel instead of compiling it
  scripts/install-panel.sh --uninstall   stop and remove both services
The units point at this checkout: run it again after moving the repository.
EOF
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
fca=${FCA_SOURCE_DIR:-$(dirname -- "$root")/Steam-Frame-Camera-Access}
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
panel=$root/panel/build/frame-hand-tracking-panel
tracker=nview
prebuilt=0
uninstall=0
while (($#)); do
  case $1 in
    --tracker) tracker=${2:-}; shift 2 ;;
    --prebuilt) prebuilt=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
case $tracker in two | nview) ;; *) echo "--tracker must be two or nview" >&2; exit 2 ;; esac

rm -f "$units/frame-hand-tracking.service.d/50-mercury-nview.conf"
rmdir "$units/frame-hand-tracking.service.d" 2> /dev/null || true

if ((uninstall)); then
  systemctl --user disable --now frame-hand-tracking-panel.service frame-hand-tracking.service 2> /dev/null || true
  rm -f "$units/frame-hand-tracking-panel.service" "$units/frame-hand-tracking.service"
  systemctl --user daemon-reload
  echo "removed the hand tracking panel and service"
  exit 0
fi

test "$(uname -m)" = aarch64 || { echo 'Run on the Frame (Linux aarch64).' >&2; exit 2; }
test -f "$fca/build/libframe_camera_access.so" || { echo "Build Steam-Frame-Camera-Access first (bash $fca/scripts/build.sh)" >&2; exit 2; }
if ((prebuilt)); then
  test -x "$panel" || { echo "No prebuilt panel at $panel" >&2; exit 2; }
else
  test -f "$fca/native/prompt/doodle_style.cpp" || { echo "No Steam-Frame-Camera-Access sources at $fca (set FCA_SOURCE_DIR)" >&2; exit 2; }
  cmake -S "$root/panel" -B "$root/panel/build" -DFCA_SOURCE_DIR="$fca" > /dev/null
  cmake --build "$root/panel/build" -j4
fi

mkdir -p "$units"
cat > "$units/frame-hand-tracking.service" << EOF
[Unit]
Description=Frame hand tracking (Frame Camera Access -> Mercury, $tracker -> frame_hand_ray)
PartOf=steamvr.service
After=steamvr.service
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
Environment=FHL_FCA_LIBRARY=$fca/build/libframe_camera_access.so
ExecStart=/bin/bash $root/run.sh --service --tracker $tracker
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=steamvr.service
EOF
cat > "$units/frame-hand-tracking-panel.service" << EOF
[Unit]
Description=Frame hand tracking panel (SteamVR dashboard)
PartOf=steamvr.service
After=steamvr.service

[Service]
Type=simple
ExecStart=$panel
Restart=on-failure
RestartSec=3

[Install]
WantedBy=steamvr.service
EOF
systemctl --user daemon-reload
systemctl --user enable frame-hand-tracking-panel.service
if systemctl --user is-active --quiet steamvr.service; then systemctl --user restart frame-hand-tracking-panel.service; fi
if systemctl --user is-active --quiet frame-hand-tracking.service; then systemctl --user restart frame-hand-tracking.service; fi
echo "panel: user service frame-hand-tracking-panel (journalctl --user -u frame-hand-tracking-panel)"
echo "hand tracking ($tracker): SteamVR dashboard > Hand tracking > Turn on (journalctl --user -u frame-hand-tracking)"
