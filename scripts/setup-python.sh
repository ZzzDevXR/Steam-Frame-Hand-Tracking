#!/bin/bash
# Sets up the Python that the hand tracking runs on, in runtime/: a standalone CPython 3.12
# (python-build-standalone, pinned by SHA-256) with packages from requirements.lock.
#   scripts/setup-python.sh             the packages of requirements.lock
#   scripts/setup-python.sh --tools     also the fp16 model converter (requirements-tools.lock); a source build
#   scripts/setup-python.sh --release   only what the tracker imports (numpy, OpenCV), without pip: what
#                                       scripts/make-release.sh ships
# FHT_RUNTIME sets another target folder.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime=${FHT_RUNTIME:-$root/runtime}
build=20261110
name=cpython-3.12.16+$build-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
url=https://github.com/astral-sh/python-build-standalone/releases/download/$build/${name/+/%2B}
sha256=b6a41defec4975d94b4ff1950236e7383e5c02ba40ea48c9ec33c66087a35aab
mode=${1:-}
case $mode in
  '' | --tools | --release) ;;
  *) echo "Unknown argument: $mode (usage: scripts/setup-python.sh [--tools|--release])" >&2; exit 2 ;;
esac
test "$(uname -m)" = aarch64 || { echo 'Run on the Frame (Linux aarch64).' >&2; exit 2; }

if [ ! -x "$runtime/bin/python3" ]; then
  cache=$root/build/downloads
  mkdir -p "$cache"
  if ! echo "$sha256  $cache/$name" | sha256sum --check --status 2> /dev/null; then
    curl -fsSL --retry 3 -o "$cache/$name" "$url"
    echo "$sha256  $cache/$name" | sha256sum --check --status ||
      { echo "$name does not match its pinned SHA-256" >&2; exit 1; }
  fi
  staging=$runtime.unpacking
  rm -rf -- "$staging"
  mkdir -p "$staging"
  tar -xzf "$cache/$name" -C "$staging" --strip-components=1
  mv "$staging" "$runtime"
fi
python=$runtime/bin/python3
install=("$python" -m pip install --quiet --disable-pip-version-check --no-warn-script-location --require-hashes
  --only-binary=:all:)
modules='numpy, cv2, onnxruntime'
case $mode in
  '') "${install[@]}" -r "$root/requirements.lock" ;;
  --tools) "${install[@]}" -r "$root/requirements-tools.lock" ;;
  --release)
    # The tracker imports only numpy and OpenCV; ONNX Runtime is used through build/lib, not from Python
    subset=$(mktemp)
    awk '/^[A-Za-z]/ { keep = ($0 ~ /^(numpy|opencv-python-headless)==/) } keep' "$root/requirements.lock" > "$subset"
    "${install[@]}" --no-deps -r "$subset"
    rm -f -- "$subset"
    packages=$runtime/lib/python3.12
    rm -rf -- "$packages"/site-packages/pip "$packages"/site-packages/pip-*.dist-info "$packages/ensurepip" \
      "$packages/idlelib" "$packages/tkinter" "$packages/turtledemo" "$runtime/include" "$runtime/bin"/pip*
    modules='numpy, cv2'
    ;;
esac
# run.sh starts Python without writing bytecode, so compile everything once here
"$python" -m compileall -q --invalidation-mode unchecked-hash "$runtime/lib" > /dev/null 2>&1 || true
"$python" -c "import sys, $modules; print('python', sys.version.split()[0], '|', ', '.join(m.__name__ + ' ' + m.__version__ for m in ($modules,)))"
