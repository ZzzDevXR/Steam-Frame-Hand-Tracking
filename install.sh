#!/usr/bin/env bash
# Frame Hand Tracking installer for the Steam Frame. In a console on the headset:
#
#   curl -fsSL https://raw.githubusercontent.com/ZzzDevXR/Steam-Frame-Hand-Tracking/main/install.sh | sudo bash
#
# It downloads the prebuilt releases of this repository and of Steam-Frame-Camera-Access into your home folder
# (FRAME_INSTALL_DIR sets another parent folder), installs the camera service as root, and sets up the SteamVR
# driver and the dashboard panel for your user. Run it again to update.
#   --tracker nview|two   all four cameras (default), or only the two SLAM cameras
#   --build               clone the source with git and compile everything on the headset instead
# Piped, with options: curl ... | sudo bash -s -- --tracker two
# From a folder you already have: sudo bash install.sh (compiles first if the folder has no binaries).
# FHT_BUNDLE / FCA_BUNDLE=<file or URL> use those release bundles instead of the latest ones
# (sudo env FHT_BUNDLE=... FCA_BUNDLE=... bash install.sh).
set -euo pipefail

repository=https://github.com/ZzzDevXR/Steam-Frame-Hand-Tracking
asset=frame-hand-tracking-aarch64.tar.gz
camera_repository=https://github.com/ZzzDevXR/Steam-Frame-Camera-Access
camera_asset=frame-camera-access-aarch64.tar.gz
self=${BASH_SOURCE[0]:-}
scratch=

die() { echo "error: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
cleanup() { if [[ -n $scratch && -d $scratch ]]; then rm -rf -- "$scratch"; fi; scratch=; }
trap cleanup EXIT

main() {
  local tracker=nview build=0
  while (($#)); do
    case $1 in
      --tracker) [[ $# -ge 2 ]] || die "--tracker needs nview or two"; tracker=$2; shift 2 ;;
      --build) build=1; shift ;;
      -h | --help) sed -n '2,15s/^# \{0,1\}//p' "$self" 2> /dev/null || echo "see $repository"; return 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done
  case $tracker in nview | two) ;; *) die "--tracker must be nview or two" ;; esac

  [[ $EUID -eq 0 ]] || die "run it with sudo (the camera service is installed as root)"
  local uid=${SUDO_UID:-}
  [[ -n $uid && $uid != 0 ]] || die "run it through sudo from your own account, not from a root shell"
  local user home
  user=$(id -nu "$uid")
  home=$(getent passwd "$uid" | cut -d: -f6)
  [[ -d $home ]] || die "no home folder for $user"
  [[ -d /run/user/$uid ]] || die "$user has no running session (/run/user/$uid)"
  as_user() {
    sudo -u "$user" env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" GIT_TERMINAL_PROMPT=0 "$@"
  }
  [[ $(uname -m) = aarch64 ]] || die "this runs on the Steam Frame (Linux aarch64)"
  [[ -x /opt/steamvr/bin/linuxarm64/vrpathreg ]] || die "SteamVR not found (/opt/steamvr)"

  require() { # tools...
    local missing=() tool
    for tool in "$@"; do command -v "$tool" > /dev/null || missing+=("$tool"); done
    if ((${#missing[@]})); then
      printf 'Missing on this headset:\n' >&2
      printf '  %s\n' "${missing[@]}" >&2
      die "install them and run this command again"
    fi
  }
  require_compilers() {
    require cmake make gcc g++ pkg-config nm
    pkg-config --exists cairo freetype2 vulkan || die "the cairo, freetype2 and vulkan development files are missing"
  }
  clone() { # url folder
    if [[ -d $2/.git ]]; then
      step "Updating $2"
      as_user git -C "$2" pull --quiet --ff-only || die "could not update $2 (local changes?)"
    elif [[ -e $2 ]]; then
      die "$2 exists but is not a git checkout; move it away or set FRAME_INSTALL_DIR"
    else
      step "Downloading the source to $2"
      as_user git clone --quiet --depth 1 "$1" "$2"
    fi
  }
  unpack() { # bundle-file-or-url folder: replaces the folder with the release bundle
    local source=$1 folder=$2 expected actual
    [[ ! -e $folder || -f $folder/RELEASE ]] ||
      die "$folder exists and was not put there by this installer; move it away or set FRAME_INSTALL_DIR"
    as_user mkdir -p "$(dirname -- "$folder")"
    scratch=$(as_user mktemp -d "$(dirname -- "$folder")/.frame-install.XXXXXX")
    step "Downloading $(basename -- "$folder")"
    if [[ -f $source ]]; then
      as_user cp "$source" "$scratch/bundle.tar.gz"
      as_user cp "$source.sha256" "$scratch/bundle.sha256" || die "no $source.sha256 next to the bundle"
    else
      as_user curl -fsSL --retry 3 -o "$scratch/bundle.tar.gz" "$source" || die "could not download $source"
      as_user curl -fsSL --retry 3 -o "$scratch/bundle.sha256" "$source.sha256" || die "could not download $source.sha256"
    fi
    expected=$(cut -d' ' -f1 "$scratch/bundle.sha256")
    actual=$(sha256sum "$scratch/bundle.tar.gz" | cut -d' ' -f1)
    [[ -n $expected && $expected == "$actual" ]] || die "the download does not match its SHA-256 ($source)"
    as_user mkdir "$scratch/new"
    as_user tar -xzf "$scratch/bundle.tar.gz" -C "$scratch/new"
    [[ -f $scratch/new/RELEASE ]] || die "$source is not a release bundle"
    if [[ -e $folder ]]; then as_user rm -rf -- "$folder"; fi
    as_user mv "$scratch/new" "$folder"
    cleanup
    echo "release $(cat "$folder/RELEASE")"
  }

  # Where the two folders are, and whether they come as source (to compile) or as prebuilt releases
  local root camera piped=0
  if [[ -n $self && -f $self && -f $(dirname -- "$self")/run.sh ]]; then
    root=$(cd -- "$(dirname -- "$self")" && pwd)
    if [[ ! -f $root/RELEASE ]]; then build=1; fi
  else
    piped=1
    root=${FRAME_INSTALL_DIR:-$home}/Steam-Frame-Hand-Tracking
    cd "$home"
  fi
  camera=${FCA_SOURCE_DIR:-$(dirname -- "$root")/Steam-Frame-Camera-Access}

  require sha256sum taskset
  if ((build)); then
    require git curl tar
    require_compilers
    if ((piped)); then clone "$repository" "$root"; fi
    if [[ ! -f $camera/CMakeLists.txt || -d $camera/.git ]]; then clone "$camera_repository" "$camera"; fi
  else
    require curl tar
    if ((piped)); then unpack "${FHT_BUNDLE:-$repository/releases/latest/download/$asset}" "$root"; fi
    if ((piped)) || [[ ! -f $camera/CMakeLists.txt ]]; then
      unpack "${FCA_BUNDLE:-$camera_repository/releases/latest/download/$camera_asset}" "$camera"
    fi
  fi

  check_libraries() { # files...: stops if a prebuilt program needs a system library this headset does not have
    local file missing
    command -v ldd > /dev/null || return 0
    for file in "$@"; do
      [[ -f $file ]] || die "$file is missing from the release"
      missing=$(as_user env LD_LIBRARY_PATH="$(dirname -- "$file")" ldd "$file" 2> /dev/null |
        awk '/not found/ { print $1 }' | sort -u | tr '\n' ' ')
      [[ -z $missing ]] ||
        die "$(basename -- "$file") needs system libraries this headset does not have: $missing(please report it: $repository/issues)"
    done
  }
  if ((!build)); then
    check_libraries "$camera/build/frame-camera-accessd" "$camera/build/frame-camera-access-prompt" \
      "$camera/build/libframe_camera_access.so" "$root/build/lib/libframehand_mercury_v10.so" \
      "$root/hand-ray/driver/frame_hand_ray/bin/linuxarm64/driver_frame_hand_ray.so" \
      "$root/panel/build/frame-hand-tracking-panel" "$root/runtime/bin/python3"
  fi

  step "Camera access service"
  if ((build)) || [[ ! -f $camera/build/frame-camera-accessd ]]; then
    require_compilers
    as_user bash "$camera/scripts/build.sh"
  fi
  bash "$camera/scripts/install-daemon.sh"

  if ((build)); then
    step "Python runtime"
    as_user bash "$root/scripts/setup-python.sh" --tools
  fi

  step "Hand tracking models"
  as_user "$root/runtime/bin/python3" "$root/models/fetch_models.py" --fp16

  if ((build)); then
    step "Mercury hand tracker (the long step: it builds OpenCV the first time)"
    as_user bash "$root/mercury/build.sh"
    step "SteamVR driver"
    as_user sh "$root/hand-ray/build.sh"
  else
    step "SteamVR driver"
  fi
  as_user sh "$root/hand-ray/install.sh"

  step "Dashboard panel"
  if ((build)); then
    as_user env FCA_SOURCE_DIR="$camera" bash "$root/scripts/install-panel.sh" --tracker "$tracker"
  else
    as_user env FCA_SOURCE_DIR="$camera" bash "$root/scripts/install-panel.sh" --prebuilt --tracker "$tracker"
  fi

  cat << EOF

Frame Hand Tracking is installed in $root.
  1. Restart the headset, so SteamVR loads the driver
     (or: systemctl --user restart steamvr.service, which also restarts the session you are in).
  2. SteamVR dashboard > Hand tracking > Turn on, and allow frame_hand_tracking.worker when the camera access
     prompt asks.
Remove it: bash $root/uninstall.sh
EOF
}

main "$@"
