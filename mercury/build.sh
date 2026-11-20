#!/bin/bash
# shellcheck disable=SC2016
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(dirname -- "$here")
work=${FHT_BUILD_DIR:-$root/build/mercury}
python=${FHT_PYTHON:-$root/runtime/bin/python3}
jobs=${FHT_JOBS:-$(nproc)}
monado_url=https://gitlab.freedesktop.org/monado/monado.git
monado_commit=ec188bb137b6af93120e015a8df100046a34d8f0
flags=-mcpu=cortex-a720
stock=0
for arg in "$@"; do
  case "$arg" in
    --generic) flags=;;
    --stock) stock=1;;
    *) echo "Unknown argument: $arg (usage: mercury/build.sh [--generic] [--stock])" >&2; exit 2;;
  esac
done
test "$(uname -m)" = aarch64 || { echo 'Build on the Frame (Linux aarch64).' >&2; exit 2; }
if [ -z "${FHT_ORT_LIB:-}" ]; then
  test -x "$python" || { echo "Python runtime missing ($python): run scripts/setup-python.sh first." >&2; exit 2; }
fi
mkdir -p "$work"

if [ ! -d "$work/monado/.git" ]; then
  git clone --quiet "$monado_url" "$work/monado"
fi
git -C "$work/monado" fetch --quiet origin "$monado_commit" 2>/dev/null || true
git -C "$work/monado" checkout --quiet --force --detach "$monado_commit"
git -C "$work/monado" clean --quiet -fdx
git -C "$work/monado" apply --whitespace=nowarn "$here/monado-ec188bb.patch"
if [ $stock = 0 ]; then
  git -C "$work/monado" apply --whitespace=nowarn "$here/monado-nview.patch"
fi

deps=$work/deps
if [ ! -f "$deps/download-manifest.json" ]; then
  "${python:-python3}" "$here/fetch_deps.py" "$deps"
fi
ort_lib=${FHT_ORT_LIB:-$("$python" -c 'import onnxruntime, os; print(os.path.join(os.path.dirname(onnxruntime.__file__), "capi", "libonnxruntime.so.1.22.1"))')}
test -f "$ort_lib" || { echo "onnxruntime 1.22.1 library not found ($ort_lib)" >&2; exit 2; }
mkdir -p "$deps/onnxruntime/lib"
ln -sf "$ort_lib" "$deps/onnxruntime/lib/libonnxruntime.so"

opencv=$work/opencv
if [ ! -f "$opencv/lib/cmake/opencv4/OpenCVConfig.cmake" ]; then
  cmake -S "$deps/opencv-4.6.0" -B "$work/build-opencv" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$opencv" \
    -DBUILD_LIST=core,imgproc -DBUILD_SHARED_LIBS=ON -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF \
    -DBUILD_EXAMPLES=OFF -DBUILD_JAVA=OFF -DBUILD_opencv_python2=OFF -DBUILD_opencv_python3=OFF \
    -DWITH_OPENCL=OFF -DWITH_IPP=OFF -DWITH_TBB=OFF -DWITH_LAPACK=OFF -DWITH_OPENMP=OFF \
    -DOPENCV_GENERATE_PKGCONFIG=OFF -DCPU_BASELINE=NEON -DENABLE_FAST_MATH=OFF \
    -DCMAKE_INSTALL_RPATH='$ORIGIN'
  cmake --build "$work/build-opencv" -j "$jobs"
  cmake --install "$work/build-opencv" > /dev/null
fi

rm -rf "$work/build"
cmake -S "$here" -B "$work/build" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS="$flags" -DCMAKE_CXX_FLAGS="$flags" \
  -DMONADO_SOURCE_DIR="$work/monado" \
  -DOpenCV_DIR="$opencv/lib/cmake/opencv4" -DEigen3_DIR="$deps/usr/share/eigen3/cmake" \
  -DONNXRuntime_ROOT_DIR="$deps/onnxruntime" \
  -DCMAKE_SHARED_LINKER_FLAGS="-Wl,--version-script=$here/mercury.exports" \
  -DFHM_EXPERIMENTAL_GATE=OFF -DFHM_LIBRARY_BASENAME=framehand_mercury_v10 \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH='$ORIGIN' \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DGIT_DESC="$monado_commit"
cmake --build "$work/build" --target framehand_mercury -j "$jobs"

lib=$root/build/lib
if [ $stock = 1 ]; then lib=$root/build/lib-stock; fi
mkdir -p "$lib"
cp "$work/build/libframehand_mercury_v10.so" "$lib/"
cp "$ort_lib" "$lib/libonnxruntime.so.1.22.1"
ln -sf libonnxruntime.so.1.22.1 "$lib/libonnxruntime.so.1"
cp -P "$opencv"/lib/libopencv_core.so* "$opencv"/lib/libopencv_imgproc.so* "$lib/"
nm -D --defined-only "$lib/libframehand_mercury_v10.so" | grep -q ' T fhm_process$' || { echo 'fhm_process not exported' >&2; exit 1; }
if LD_LIBRARY_PATH=$lib ldd "$lib/libframehand_mercury_v10.so" | grep 'not found'; then exit 1; fi
sha256sum "$lib"/libframehand_mercury_v10.so
echo "Mercury built: $lib/libframehand_mercury_v10.so (flags: ${flags:-generic}, $([ $stock = 1 ] && echo stock || echo n-view))"
