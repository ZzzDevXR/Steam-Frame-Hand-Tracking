import argparse
import itertools
import json
import os
from pathlib import Path
import signal
import time
import numpy as np

from .camera_access import CameraAccessSource
from .external_roi import controls as acquisition_controls
from .geometry import load_rig
from .hand_status import HandStatus, frame_status
from .mercury import SHIPPED_GUARDS
from .optimizer_controls import mercury_optimizer_controls, mercury_reset_signature

CAMERA_ORDER = ['slam_left', 'slam_right', 'upper_left', 'upper_right']
PAIRS = list(itertools.combinations(CAMERA_ORDER, 2))


class StatusFile:
    def __init__(self):
        base = os.environ.get('XDG_RUNTIME_DIR')
        self.path = Path(base) / 'frame-hand-tracking' / 'status.json' if base else None
        self.last_write, self.window_start, self.processed = 0., time.monotonic(), 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, state, message, hands=0, rate=0., hand_status=('not_seen', 'not_seen')):
        if self.path is None:
            return
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'pid': os.getpid(), 'updated_unix': time.time(), 'state': state,
                                   'message': message, 'hands': hands, 'rate_hz': round(rate, 1),
                                   'left': hand_status[0], 'right': hand_status[1]}),
                       encoding='utf-8')
        os.replace(tmp, self.path)

    def update(self, record, source, hand_status=('not_seen', 'not_seen')):
        status = record.get('status', '')
        if status not in ('camera_source_wait', 'worker_error'):
            self.processed += 1
        now = time.monotonic()
        if now - self.last_write < .5:
            return
        rate = self.processed / max(1e-3, now - self.window_start)
        self.last_write, self.window_start, self.processed = now, now, 0
        if status == 'worker_error':
            state, message = 'error', record.get('error', 'error')
        elif status == 'camera_source_wait':
            state, message = 'waiting', record.get('error', 'waiting for camera frames')
        else:
            state, message = 'streaming', getattr(source, 'message', 'tracking')
        self.write(state, message, sum(1 for h in record.get('hands') or [] if h.get('valid', True)), rate,
                   hand_status)


class RetryPending(Exception):
    pass


def next_retry_delay(previous_s, first_s=.25, longest_s=10.):
    return first_s if previous_s <= 0 else min(previous_s * 2, longest_s)


class ErrorLog:
    def __init__(self, repeat_s=60.):
        self.repeat_s = repeat_s
        self.message, self.logged_s, self.attempts = None, 0., 0

    def failed(self, message, now_s):
        is_new = message != self.message
        if is_new:
            self.message, self.attempts = message, 0
        self.attempts += 1
        if not is_new and now_s - self.logged_s < self.repeat_s:
            return None
        self.logged_s = now_s
        return f'error: {message}' if is_new else f'error, still after {self.attempts} attempts: {message}'

    def recovered(self):
        if self.message is None:
            return None
        line = f'recovered after {self.attempts} failed attempts: {self.message}'
        self.message, self.attempts = None, 0
        return line


def controls_stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def load_controls(path, previous):
    if not path.exists():
        return previous
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return previous
    if not isinstance(value, dict):
        raise ValueError('Controls must be an object')
    if value.get('schema_version') != 1:
        raise ValueError('Unsupported control schema')
    if value.get('tracker') not in ('mercury', 'mercury_nview'):
        raise ValueError('Unknown tracker')
    for key, default in [('min_confidence', .3), ('smoothing', .2)]:
        number = value.get(key, default)
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not np.isfinite(number) or \
                not 0 <= number <= 1:
            raise ValueError(f'Invalid {key}')
    if type(value.get('revision', 0)) is not int or value.get('revision', 0) < 0:
        raise ValueError('Invalid control revision')
    if type(value.get('paused', False)) is not bool:
        raise ValueError('Invalid pause state')
    selected = value.get('cameras')
    if not isinstance(selected, dict) or set(selected) != set(CAMERA_ORDER) or \
            any(type(flag) is not bool for flag in selected.values()):
        raise ValueError('Cameras must specify four known boolean selections')
    if type(value.get('pair_index', 0)) is not int or not 0 <= value.get('pair_index', 0) < 6:
        raise ValueError('Invalid pair selection')
    if value.get('detector_orientation_degrees', 0) not in (0, 90, 180, 270):
        raise ValueError('Invalid detector_orientation_degrees')
    if type(value.get('phase_filter', 'bright')) is not str or value.get('phase_filter', 'bright') not in ('bright','raw'):
        raise ValueError('Invalid phase filter')
    mercury_optimizer_controls(value)
    for key in ('nview_parameters', 'mercury_parameters'):
        native = value.get(key, {})
        if not isinstance(native, dict) or any(type(k) is not str or not isinstance(v, (int, float)) or
                                               not np.isfinite(v) for k, v in native.items()):
            raise ValueError(f'{key} must map setting names to finite numbers')
    acquisition_controls(value)
    return value


def run(controls_path, calibration, duration=60, fps=20, detector_orientation=0, live_max_age_ms=500.,
        phase_filter='bright', hand_ray=None, device_config='/persist/device_config.json', pinch_min_confidence=0.):
    controls = {'schema_version': 1, 'revision': 0, 'tracker': 'mercury_nview', 'pair_index': 0,
                'cameras': {c: True for c in CAMERA_ORDER}, 'min_confidence': .5,
                'smoothing': .2, 'mercury_opt_smooth_factor': 2.0, 'mercury_optimize_hand_size': True,
                'paused': False, 'detector_orientation_degrees': detector_orientation, 'phase_filter': phase_filter}
    controls_path = Path(controls_path)
    controls = load_controls(controls_path, controls)
    source = CameraAccessSource(calibration, max_age_ms=live_max_age_ms,
                                min_mean=0. if controls.get('phase_filter', phase_filter) == 'raw' else 10.)
    cameras = load_rig(calibration)
    from .hand_interaction import head_from_camera_or_default
    guard_head_from_camera = head_from_camera_or_default(device_config)[0]
    hand_ray_sender = None
    if hand_ray is not None:
        from .hand_interaction import HandRaySender, head_from_camera
        device_config, pinch_begin_m, pinch_end_m = hand_ray
        hand_ray_sender = HandRaySender(head_from_camera(device_config), pinch_begin_m, pinch_end_m,
                                        min_confidence=pinch_min_confidence)
    backend, last_config, was_paused = None, None, False
    started, next_frame = time.monotonic(), time.monotonic()
    status_file = StatusFile()
    status_file.write('starting', 'starting')
    stamp = controls_stamp(controls_path)
    hand_status = (HandStatus(), HandStatus())
    errors, retry_delay, retry_at = ErrorLog(), 0., 0.
    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)

    def working():
        nonlocal retry_delay
        retry_delay = 0.
        line = errors.recovered()
        if line:
            print(line, flush=True)

    while not stop_requested and time.monotonic() - started < duration:
        now = time.monotonic()
        if now < next_frame:
            time.sleep(min(next_frame - now, .05))
            continue
        next_frame = max(next_frame + 1 / fps, now)
        record = {'hands': []}
        try:
            new_stamp = controls_stamp(controls_path)
            controls_changed = new_stamp != stamp
            if controls_changed:
                loaded = load_controls(controls_path, controls)
                if loaded is not controls:
                    stamp = new_stamp
                controls = loaded
            if now < retry_at and not controls_changed:
                raise RetryPending(errors.message)
            orientation = int(controls.get('detector_orientation_degrees', detector_orientation))
            native_parameters = mercury_optimizer_controls(controls)
            for camera in cameras.values():
                camera['mercury_camera_orientation_degrees'] = orientation
            selected = [c for c in CAMERA_ORDER if controls.get('cameras', {}).get(c, False)]
            if controls.get('paused'):
                if not was_paused and backend is not None:
                    backend.reset()
                was_paused = True
                continue
            was_paused = False
            if len(selected) < 2:
                continue
            requested_phase = controls.get('phase_filter', phase_filter)
            source.min_mean = 0. if requested_phase == 'raw' else 10.
            pairs = [p for p in PAIRS if set(p).issubset(selected)] if len(selected) > 2 else [tuple(selected)]
            config = (controls['tracker'], tuple(selected), int(controls.get('pair_index', 0)),
                      float(controls['min_confidence']), orientation, mercury_reset_signature(controls),
                      requested_phase, tuple(sorted(acquisition_controls(controls).items())),
                      tuple(sorted(controls.get('nview_parameters', {}).items()))
                      if controls['tracker'] == 'mercury_nview' else None,
                      tuple(sorted(controls.get('mercury_parameters', {}).items())))
            guards = {**SHIPPED_GUARDS, **{k: float(v) for k, v in controls.get('mercury_parameters', {}).items()}}
            if config != last_config:
                if backend is not None:
                    backend.close()
                backend = None
                if controls['tracker'] == 'mercury_nview':
                    from .mercury_nview import NViewBackend, DEFAULT_VIEWS
                    backend = NViewBackend(cameras=cameras, views=[c for c in DEFAULT_VIEWS if c in selected],
                                           external_roi=acquisition_controls(controls),
                                           parameters={'min_detection_confidence': float(controls['min_confidence']),
                                                       **native_parameters, **guards,
                                                       **{k: float(v) for k, v in controls.get('nview_parameters', {}).items()}},
                                           head_from_reference=guard_head_from_camera)
                else:
                    from .mercury import MercuryBackend
                    backend = MercuryBackend(camera_pairs=pairs, cameras=cameras,
                                             external_roi=acquisition_controls(controls),
                                             parameters={'min_detection_confidence': float(controls['min_confidence']),
                                                         **native_parameters, **guards},
                                             head_from_reference=guard_head_from_camera)
                last_config = config
            source_stamp, images = source.read(selected)
            record.update(backend.process(images, cameras, source_stamp))
            record['source_timestamp_ns'] = source_stamp
        except RetryPending as pending:
            record.update(status='worker_error', error=str(pending), hands=[])
        except TimeoutError as error:
            record.update(status='camera_source_wait', error=str(error), hands=[])
            working()
        except Exception as error:
            message = f'{type(error).__name__}: {error}'
            record.update(status='worker_error', error=message, hands=[])
            last_config = None
            failed_at = time.monotonic()
            retry_delay = next_retry_delay(retry_delay)
            retry_at = failed_at + retry_delay
            line = errors.failed(message, failed_at)
            if line:
                print(line, flush=True)
        else:
            working()
        if hand_ray_sender is not None:
            hand_ray_sender.process(record)
        hands = [None, None]
        for hand in record.get('hands') or []:
            if hand.get('hand_id') in (0, 1) and hand.get('valid', True):
                hands[hand['hand_id']] = hand
        qualities = record.get('hand_quality') or [None, None]
        too_dark = record.get('status') == 'camera_source_wait' and 'too dark' in record.get('error', '')
        frame_now = time.monotonic()
        shown = [hand_status[side].update('too_dark' if too_dark else frame_status(hands[side], qualities[side]),
                                          frame_now) for side in (0, 1)]
        status_file.update(record, source, shown)
    if backend is not None:
        backend.close()
    source.close()
    status_file.write('stopped', 'stopped')
    signal.signal(signal.SIGTERM, previous_sigterm)


def main():
    p = argparse.ArgumentParser(
        description='Hand tracking worker: Frame Camera Access -> Mercury -> frame_hand_ray SteamVR driver.')
    p.add_argument('--calibration', required=True, help='camera rig JSON (calibration/estimated-calibration.json)')
    p.add_argument('--control', required=True, help='settings JSON, re-read when it changes (run.sh writes it)')
    p.add_argument('--duration', type=float, default=60)
    p.add_argument('--fps', type=float, default=20)
    p.add_argument('--detector-orientation', choices=[0, 90, 180, 270], type=int, default=0)
    p.add_argument('--live-max-age-ms', type=float, default=500.)
    p.add_argument('--phase-filter', choices=['bright', 'raw'], default='bright',
                   help='Which camera sets to process: bright keeps the sets whose slam_right mean brightness is at '
                        'least 10 (the cameras alternate bright and short exposures), raw keeps every set.')
    p.add_argument('--hand-ray', action='store_true',
                   help='Send pinch + hand anchors to the frame_hand_ray SteamVR driver (@frame_hand_ray socket).')
    p.add_argument('--device-config', default='/persist/device_config.json',
                   help='Frame device_config.json giving the camera-to-head transform for --hand-ray (and where the '
                        "eyes are for the tracker's too-close guard)")
    p.add_argument('--pinch-begin-m', type=float, default=.020)
    p.add_argument('--pinch-end-m', type=float, default=.055)
    from .hand_interaction import PINCH_MIN_CONFIDENCE
    p.add_argument('--pinch-min-confidence', type=float, default=PINCH_MIN_CONFIDENCE,
                   help='thumb and index tip confidence a pinch needs (Mercury keypoint confidence; 0 = no gating)')
    a = p.parse_args()
    if a.hand_ray and not 0 < a.pinch_begin_m < a.pinch_end_m <= .2:
        p.error('pinch thresholds need 0 < begin < end <= 0.2 m')
    if not np.isfinite(a.pinch_min_confidence) or not 0 <= a.pinch_min_confidence <= 1:
        p.error('pinch-min-confidence must be within 0..1')
    if not np.isfinite(a.fps) or not np.isfinite(a.duration) or a.fps <= 0 or a.duration <= 0:
        p.error('fps and duration must be finite and positive')
    if not np.isfinite(a.live_max_age_ms) or not 0 < a.live_max_age_ms <= 2000:
        p.error('live-max-age-ms must be finite and in (0,2000]')
    try:
        run(a.control, a.calibration, a.duration, a.fps, a.detector_orientation, a.live_max_age_ms, a.phase_filter,
            hand_ray=(a.device_config, a.pinch_begin_m, a.pinch_end_m) if a.hand_ray else None,
            device_config=a.device_config, pinch_min_confidence=a.pinch_min_confidence)
    except Exception as error:
        raise SystemExit(f'{type(error).__name__}: {error}') from error


if __name__ == '__main__':
    main()
