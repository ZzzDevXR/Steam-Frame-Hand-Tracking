# Frame Hand Tracking: technical notes

How it works, what the installer does, and how to build it yourself. The short guide is in [README.md](README.md).

Controller-free hand tracking for the **Steam Frame**, running on the headset's CPU. Point with your hand, pinch
to click, hold the pinch to drag; SteamVR and OpenXR apps also get a 26-joint hand skeleton.

```
4 tracking cameras ─► Frame Camera Access ───► hand tracking worker ───────► frame_hand_ray SteamVR driver
                      (root service,           Mercury hand tracker:         hand ray, pinch-click, 26-joint
                      read-only, asks you      4 cameras (N-view), or 2      skeleton for SteamVR / OpenXR apps
                      before an app sees them)
```

Status: research prototype, tested on one Steam Frame (SteamOS, SteamVR 2.18).

## Install

Open a console on the headset and run:

```sh
curl -fsSL https://raw.githubusercontent.com/ZzzDevXR/Steam-Frame-Hand-Tracking/main/install.sh | sudo bash
```

Then restart the headset, open **SteamVR dashboard › Hand tracking › Turn on**, and allow
`frame_hand_tracking.worker` when the camera access prompt asks.

What the command does ([install.sh](install.sh), worth reading before you pipe it into `sudo`):

- downloads the prebuilt releases of this repository and of
  [Steam-Frame-Camera-Access](https://github.com/ZzzDevXR/Steam-Frame-Camera-Access) into your home folder
  (`~/Steam-Frame-Hand-Tracking`, `~/Steam-Frame-Camera-Access`) and checks their SHA-256;
- installs the camera service as root (`/var/lib/frame-camera-access`, the only part that needs `sudo`);
- uses the Python that comes inside the release (`runtime/`, with numpy and OpenCV);
- registers the SteamVR driver and three user services. Nothing else is installed system-wide.

It takes about half a minute and needs internet access and `curl`.

Run the same command again to update. To use only the two SLAM cameras instead of all four:

```sh
curl -fsSL https://raw.githubusercontent.com/ZzzDevXR/Steam-Frame-Hand-Tracking/main/install.sh | sudo bash -s -- --tracker two
```

## Remove

```sh
bash ~/Steam-Frame-Hand-Tracking/uninstall.sh              # hand tracking only
sudo bash ~/Steam-Frame-Hand-Tracking/uninstall.sh --all   # also the camera service
```

The SteamVR driver and the user services point at the `~/Steam-Frame-Hand-Tracking` folder, so run this before
moving or deleting it.

## Build from source

To compile everything on the headset instead of downloading the release, add `--build`. It needs `git`, `cmake`,
`make`, `gcc`/`g++` and `pkg-config` with the Cairo, FreeType and Vulkan development files, and takes about five
minutes:

```sh
curl -fsSL https://raw.githubusercontent.com/ZzzDevXR/Steam-Frame-Hand-Tracking/main/install.sh | sudo bash -s -- --build
```

These are the steps it runs; use them when working on the code. Both repositories must sit next to each other.

```sh
git clone https://github.com/ZzzDevXR/Steam-Frame-Camera-Access
git clone https://github.com/ZzzDevXR/Steam-Frame-Hand-Tracking

# 1. The camera service (asks for your password)
cd Steam-Frame-Camera-Access
bash scripts/build.sh
sudo bash scripts/install-daemon.sh

# 2. The hand tracking
cd ../Steam-Frame-Hand-Tracking
bash scripts/setup-python.sh --tools
runtime/bin/python3 models/fetch_models.py --fp16
bash mercury/build.sh
sh hand-ray/build.sh && sh hand-ray/install.sh
bash scripts/install-panel.sh                 # all four cameras; --tracker two: the SLAM pair only
systemctl --user restart steamvr.service      # loads the driver; this also restarts gamescope
```

`bash run.sh --help` lists the tracker's options.

To make the release that the installer downloads: after a build on the headset, run
`bash scripts/make-release.sh <version>` here and in Steam-Frame-Camera-Access, and attach the two files each one
writes to `dist/` to a new GitHub release of its repository.

| folder | what |
| --- | --- |
| `python/frame_hand_tracking/` | the worker: reads the cameras, runs Mercury, sends hands to the driver |
| `mercury/` | builds Monado's Mercury hand tracker as a library, with the four-camera N-view fork |
| `models/` | Mercury's two ONNX models and the fp16 conversion |
| `calibration/` | the estimated camera calibration the tracker uses |
| `hand-ray/` | the `frame_hand_ray` SteamVR driver |
| `panel/` | the "Hand tracking" SteamVR dashboard panel |

## License

The code in this repository is under the MIT License ([LICENSE](LICENSE)), by
[ZzzDevXR](https://github.com/ZzzDevXR). Third-party components keep their own licenses: Monado patches
(BSL-1.0), OpenVR headers (BSD-3-Clause) and the build-time dependencies listed in
[THIRD_PARTY.md](THIRD_PARTY.md).

The Mercury hand tracking models in `models/fp32/` were made by Moshi Turner at Collabora for
[Monado](https://monado.freedesktop.org). Their upstream repository states no license, so they are included
with credit and are not covered by the MIT License ([models/README.md](models/README.md)).
