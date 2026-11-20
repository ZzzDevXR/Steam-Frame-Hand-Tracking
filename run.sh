#!/usr/bin/env bash
set -euo pipefail
usage() {
  cat <<'EOF'
Runs the hand tracking on the Frame: Frame Camera Access -> Mercury -> frame_hand_ray SteamVR driver.
  ./run.sh             live cameras, hand ray on, until --duration (default 1 h) or Ctrl-C
Options: --tracker nview|two (default: $FHT_TRACKER, else nview: the N-view fork on all four tracking cameras;
         two: Mercury on the SLAM pair),
         --duration SECONDS, --no-hand-ray,
         --calibration FILE (default: $FHT_CALIBRATION, else the shared estimated calibration
         calibration/estimated-calibration.json),
         --service (what the hand tracking panel's service runs: no time limit)
The cameras come from the Frame Camera Access service (frame-camera-accessd, from the Steam-Frame-Camera-Access
repository, checked out next to this one); the headset asks once whether hand tracking may use them. Without the headset
on, the cameras stop and the worker waits. The worker reports what it is doing in
$XDG_RUNTIME_DIR/frame-hand-tracking/status.json (the hand tracking panel shows it) and re-reads its settings
from controls.json in the same folder when that file changes.
EOF
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python=${FHT_PYTHON:-$root/runtime/bin/python3}
duration=3600
hand_ray=1
calibration=${FHT_CALIBRATION:-$root/calibration/estimated-calibration.json}
tracker=${FHT_TRACKER:-nview}
while (($#)); do
  case "$1" in
    --duration) duration=$2; shift 2;;
    --calibration) calibration=$2; shift 2;;
    --no-hand-ray) hand_ray=0; shift;;
    --service)
      test -n "${XDG_RUNTIME_DIR:-}" || { echo '--service needs XDG_RUNTIME_DIR (run it as a user service)' >&2; exit 2; }
      duration=315360000; shift;;
    --tracker) tracker=$2; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
case $tracker in two|nview) ;; *) echo "--tracker must be two or nview" >&2; exit 2;; esac

export FHL_MERCURY_LIBRARY=${FHL_MERCURY_LIBRARY:-$root/build/lib/libframehand_mercury_v10.so}
export FHL_MERCURY_MODELS_DIR=${FHL_MERCURY_MODELS_DIR:-$root/models/fp16}
export FHL_KEYPOINT_IS_HAND_MIN=${FHL_KEYPOINT_IS_HAND_MIN:-0.90}
export FHL_FRAMES_BEFORE_DISPLAY=${FHL_FRAMES_BEFORE_DISPLAY:-5}
export FHL_CPU_AFFINITY=${FHL_CPU_AFFINITY-2-7}
export FHL_FCA_LIBRARY=${FHL_FCA_LIBRARY:-$(dirname -- "$root")/Steam-Frame-Camera-Access/build/libframe_camera_access.so}
export PYTHONPATH="$root/python"
export PYTHONDONTWRITEBYTECODE=1
export LD_LIBRARY_PATH="$root/build/lib"
unset LD_PRELOAD PYTHONHOME

test "$(uname -m)" = aarch64 || { echo 'Run on the Frame (Linux aarch64).' >&2; exit 2; }
test -x "$python" || { echo "Python runtime missing ($python): bash scripts/setup-python.sh" >&2; exit 2; }
test -f "$FHL_MERCURY_LIBRARY" || { echo "Mercury library missing: mercury/build.sh" >&2; exit 2; }
test -d "$FHL_MERCURY_MODELS_DIR" || { echo "Models missing: runtime/bin/python3 models/fetch_models.py --fp16" >&2; exit 2; }
test -f "$calibration" || { echo "Camera rig missing: $calibration" >&2; exit 2; }
test -f "$FHL_FCA_LIBRARY" || { echo "Camera access client missing ($FHL_FCA_LIBRARY): build ../Steam-Frame-Camera-Access (bash scripts/build.sh), or set FHL_FCA_LIBRARY" >&2; exit 2; }
state=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/frame-hand-tracking
mkdir -p -- "$state"

"$python" - "$state/controls.json" "$tracker" <<'PY'
import json, sys
from pathlib import Path
nview = sys.argv[2] == 'nview'
Path(sys.argv[1]).write_text(json.dumps({'schema_version': 1, 'revision': 0,
    'tracker': 'mercury_nview' if nview else 'mercury', 'pair_index': 0,
    'cameras': {'slam_left': True, 'slam_right': True, 'upper_left': nview, 'upper_right': nview},
    'min_confidence': .5, 'smoothing': .2, 'paused': False, 'detector_orientation_degrees': 0,
    'acquisition_method': 'mercury_rot', 'mercury_crop_roll_degrees': 270,
    'mercury_opt_smooth_factor': 2.0, 'mercury_optimize_hand_size': True, 'phase_filter': 'bright'}, indent=2))
PY

args=(--control "$state/controls.json" --calibration "$calibration" --duration "$duration" --fps 60
      --live-max-age-ms 500 --phase-filter bright)
if ((hand_ray)); then args+=(--hand-ray); fi
affinity=(); if [[ -n $FHL_CPU_AFFINITY ]]; then affinity=(taskset -c "$FHL_CPU_AFFINITY"); fi
printf 'Hand tracking (%s), settings: %s\n' "$tracker" "$state/controls.json"
exec "${affinity[@]}" "$python" -m frame_hand_tracking.worker "${args[@]}"
