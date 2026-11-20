# Third-party components

| component | how it is used | license |
| --- | --- | --- |
| [Monado](https://gitlab.freedesktop.org/monado/monado) @ `ec188bb137b6af93120e015a8df100046a34d8f0` | cloned by `mercury/build.sh` and patched with `mercury/monado-ec188bb.patch` and the N-view fork `mercury/monado-nview.patch` (Mercury hand tracking, ONNX wrapper, CMake) | BSL-1.0 (the patches keep Monado's file headers) |
| Mercury models ([hand-tracking-models](https://gitlab.freedesktop.org/monado/utilities/hand-tracking-models) @ `37a8a81b`) | the two models the tracker uses are stored unmodified in `models/fp32/` (credit and source in `models/README.md`); fp16 variants are derived by `models/fetch_models.py` and included in the prebuilt release | not stated by the upstream repository; **not covered by this repository's MIT License** |
| OpenVR headers (`hand-ray/third_party/openvr/`) | driver build when SteamVR's own headers are absent | BSD-3-Clause (`LICENSE-openvr`) |
| frameeyeosc `vk_texture` and the OpenVR header, from [Steam-Frame-Camera-Access](https://github.com/ZzzDevXR/Steam-Frame-Camera-Access) (`native/prompt/third_party/`) | compiled into the dashboard panel (`panel/`), not copied here | MIT / BSD-3-Clause |
| Caveat Brush and Nunito fonts, from [Steam-Frame-Camera-Access](https://github.com/ZzzDevXR/Steam-Frame-Camera-Access) (`native/prompt/assets/`) | built into the dashboard panel (`panel/`) and the camera permission prompt | SIL OFL 1.1 (`OFL-CaveatBrush.txt`, `OFL-Nunito.txt`) |
| ONNX Runtime 1.22.1 | the six C/C++ API headers are stored unmodified in `mercury/third_party/onnxruntime/include/`; the library comes from the `onnxruntime` wheel | MIT (`mercury/third_party/onnxruntime/LICENSE`) |
| OpenCV 4.6.0 | core + imgproc built from source by `mercury/build.sh`; the prebuilt release ships them unmodified in `build/lib/` | Apache-2.0 (`build/lib/LICENSE-opencv.txt` in the release) |
| Eigen 3.4.0 | headers (Debian package, unpacked at build time), compiled into the Mercury library; source: <https://gitlab.com/libeigen/eigen/-/tree/3.4.0> | MPL-2.0 |
| CPython 3.12 ([python-build-standalone](https://github.com/astral-sh/python-build-standalone) `20261110`) | downloaded by `scripts/setup-python.sh` into `runtime/`; the prebuilt release includes it | PSF-2.0, plus the licenses of the libraries it links (`runtime/lib/python3.12/LICENSE.txt` in the release) |
| numpy, opencv-python-headless, onnxruntime | Python packages (`requirements.txt`), installed into `runtime/`; the prebuilt release includes numpy and opencv-python-headless unmodified (the tracker uses ONNX Runtime through `build/lib/`) | BSD-3-Clause / Apache-2.0 + MIT / MIT (each wheel's license files are in its `dist-info` folder) |
| onnx, onnxconverter-common, protobuf | fp16 model conversion (`requirements-tools.txt`) | Apache-2.0 / MIT / BSD-3-Clause |

Prior art credited in the driver (no code copied): frame-unboundedMouse (Andalu30, MIT) and frametop
(DeeJanuz, MIT) published the compositor-binding layout, the invisible render model and the
start-disconnected / OptOut role handling for virtual controllers on the Steam Frame.

The Index wrist offset (`hand-ray/src/hand_skeleton.h`) is a value from Valve's OpenVR sample driver
`samples/drivers/handskeletonsimulation` (BSD-3-Clause).
