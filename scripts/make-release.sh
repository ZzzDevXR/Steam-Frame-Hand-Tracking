#!/usr/bin/env bash
# Packs this checkout together with its built binaries into
#   dist/frame-hand-tracking-aarch64.tar.gz  (and .sha256)
# which install.sh downloads from this repository's latest GitHub release. Attach both files to the release.
# Run on the Frame after a full build (sudo bash install.sh --build):   bash scripts/make-release.sh [version]
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=${1:-$(date -u +%Y.%m.%d)}
name=frame-hand-tracking-aarch64.tar.gz
driver=hand-ray/driver/frame_hand_ray/bin/linuxarm64/driver_frame_hand_ray.so
panel=panel/build/frame-hand-tracking-panel
opencv_license=$root/build/mercury/deps/opencv-4.6.0/LICENSE
test "$(uname -m)" = aarch64 || { echo 'Run on the Frame (Linux aarch64).' >&2; exit 2; }
for file in build/lib/libframehand_mercury_v10.so "$driver" "$panel" models/fp16/grayscale_detection_160x160.onnx \
  models/fp16/grayscale_keypoint_jan18.onnx; do
  test -f "$root/$file" || { echo "$file is missing: build first (sudo bash install.sh --build)" >&2; exit 2; }
done
test -f "$opencv_license" || { echo "OpenCV license not found ($opencv_license)" >&2; exit 2; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
tar -C "$root" --exclude=./.git --exclude=./.idea --exclude=./.venv --exclude=./runtime --exclude=./build \
  --exclude=./dist \
  --exclude=./panel/build --exclude=__pycache__ --exclude='*.pyc' -cf - . | tar -C "$stage" -xf -
mkdir -p "$stage/build" "$stage/panel/build"
cp -a "$root/build/lib" "$stage/build/lib"
cp "$root/$panel" "$stage/$panel"
cp "$opencv_license" "$stage/build/lib/LICENSE-opencv.txt"
cp "$root/mercury/third_party/onnxruntime/LICENSE" "$stage/build/lib/LICENSE-onnxruntime.txt"
cat > "$stage/build/lib/README.txt" << 'EOF'
Prebuilt for the Steam Frame (Linux aarch64) by scripts/make-release.sh:
  libframehand_mercury_v10.so  this repository's mercury/ bridge with Monado's Mercury hand tracker
                               (BSL-1.0, https://gitlab.freedesktop.org/monado/monado, patched by mercury/*.patch)
                               and Eigen 3.4.0 (MPL-2.0, https://gitlab.com/libeigen/eigen/-/tree/3.4.0)
  libopencv_core, _imgproc     OpenCV 4.6.0, unmodified (Apache-2.0, LICENSE-opencv.txt)
  libonnxruntime               ONNX Runtime 1.22.1, from the onnxruntime wheel (MIT, LICENSE-onnxruntime.txt)
The release also carries ../../runtime/: CPython 3.12 from python-build-standalone
(https://github.com/astral-sh/python-build-standalone; PSF-2.0 and the licenses of the libraries it links,
see runtime/lib/python3.12/LICENSE.txt) with the numpy and opencv-python-headless wheels, whose licenses are
in runtime/lib/python3.12/site-packages/*.dist-info/.
mercury/build.sh rebuilds all of it from source.
EOF
# The Python the tracker runs on, with only the packages it imports
FHT_RUNTIME="$stage/runtime" bash "$root/scripts/setup-python.sh" --release
printf '%s\n' "$version" > "$stage/RELEASE"

mkdir -p "$root/dist"
tar -C "$stage" --owner=0 --group=0 --numeric-owner -czf "$root/dist/$name" .
(cd "$root/dist" && sha256sum "$name" > "$name.sha256")
echo "release $version: dist/$name ($(du -h "$root/dist/$name" | cut -f1)), dist/$name.sha256"
