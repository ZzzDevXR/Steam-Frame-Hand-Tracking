from __future__ import annotations
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import numpy as np

REVISION = 'ec188bb137b6af93120e015a8df100046a34d8f0'
MODEL_REVISION = '37a8a81bc8f433ac6cbdf2471909d2bac74beca1'
MODEL_HASHES = {'grayscale_detection_160x160.onnx':
                '1f1a039a266e13dc186bb884430ebd9c8216bdda680ab08a533d4c671f27ed36',
                'grayscale_keypoint_jan18.onnx':
                '40c0daa598cedb993b54fff17685231b7465d6db342656c401b01c2029efd1d5'}
MODEL_VARIANTS = {'fp32': MODEL_HASHES,
                  'fp16': {'grayscale_detection_160x160.onnx':
                           'bfa452bf3a4210ebde26615a5708301b562e6a7fe01fa849edb4ac1b1a977237',
                           'grayscale_keypoint_jan18.onnx':
                           '0ce8a78f96a111375c2b3c4d51def920c9081b51d103b16299312662d4f519fa'}}
MAPPING21 = [1, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13, 14, 15, 17, 18, 19, 20, 22, 23, 24, 25]
AXES = np.diag([1., -1., -1.])


class NativeCamera(C.Structure):
    _fields_ = [('width', C.c_int32), ('height', C.c_int32), ('K', C.c_double*9),
                ('kb4', C.c_double*4), ('orientation_degrees', C.c_int32)]


class NativeCalibration(C.Structure):
    _fields_ = [('views', NativeCamera*2), ('rotation', C.c_double*9), ('translation', C.c_double*3)]


class NativeJoint(C.Structure):
    _fields_ = [('position_m', C.c_float*3), ('quaternion_xyzw', C.c_float*4),
                ('radius_m', C.c_float), ('relation_flags', C.c_uint64)]


class NativeHand(C.Structure):
    _fields_ = [('active', C.c_int32), ('joints', NativeJoint*26)]


class NativeResult(C.Structure):
    _fields_ = [('timestamp_ns', C.c_int64), ('hands', NativeHand*2)]


class NativeHandQuality(C.Structure):
    _fields_ = [('views_used', C.c_int32), ('rejection_stage', C.c_int32), ('is_hand', C.c_float),
                ('reprojection_error', C.c_float), ('hand_confidence', C.c_float), ('joint_confidence', C.c_float*21)]


class NativeQuality(C.Structure):
    _fields_ = [('hands', NativeHandQuality*2), ('detector_ran', C.c_int32), ('detections_suppressed', C.c_int32),
                ('frame_index', C.c_uint64)]


SHIPPED_GUARDS = {'acquire_every_n_frames': 3, 'idle_acquire_every_n_frames': 2, 'recent_loss_frames': 30,
                  'min_hand_eye_dist': 0.16, 'fp_suppress_ms': 1000.0, 'overlap_per_view': 1}
GUARD_NAMES = frozenset(SHIPPED_GUARDS) | {'eye_x_m', 'eye_y_m', 'eye_z_m'}
FINGER_TIP_KEYPOINTS = ((3, 4), (7, 8), (11, 12), (15, 16), (19, 20))


def eye_point_parameters(head_from_reference, reference_from_first):
    eye_reference = np.linalg.inv(np.asarray(head_from_reference, float))[:3, 3]
    first_from_reference = np.linalg.inv(np.asarray(reference_from_first, float))
    x, y, z = AXES @ (first_from_reference[:3, :3] @ eye_reference + first_from_reference[:3, 3])
    return {'eye_x_m': float(x), 'eye_y_m': float(y), 'eye_z_m': float(z)}


def hand_quality(native):
    joints = [round(float(c), 4) for c in native.joint_confidence]
    fingers = [round((joints[a] + joints[b]) / 2, 4) for a, b in FINGER_TIP_KEYPOINTS]
    return {'confidence': round(float(native.hand_confidence), 4), 'is_hand': round(float(native.is_hand), 4),
            'views_used': int(native.views_used), 'reprojection_error': round(float(native.reprojection_error), 5),
            'rejection_stage': int(native.rejection_stage), 'joint_confidence': joints,
            'finger_confidence': fingers, 'pinch_confidence': min(fingers[0], fingers[1])}


def hand_qualities(native):
    return [hand_quality(native.hands[h]) for h in (0, 1)]


def mean_brightness(image):
    return float(np.asarray(image)[::8, ::8].mean())


def canvas_transform(camera, width, height):
    source_w, source_h = int(camera['width']), int(camera['height'])
    scale = min(width/source_w, height/source_h)
    resized_w, resized_h = round(source_w*scale), round(source_h*scale)
    sx, sy = resized_w/source_w, resized_h/source_h
    x, y = (width-resized_w)//2, (height-resized_h)//2
    matrix = np.array([[sx, 0, x+(sx-1)/2], [0, sy, y+(sy-1)/2], [0, 0, 1.]])
    return matrix, {'source_size': [source_w, source_h], 'resized_size': [resized_w, resized_h],
                    'padding_left_top': [x, y], 'canvas_size': [width, height],
                    'pixel_transform': matrix.tolist()}


def prepare_calibration(cameras, pair):
    width = max(int(cameras[c]['width']) for c in pair)
    height = max(int(cameras[c]['height']) for c in pair)
    calib, preprocessing = NativeCalibration(), {}
    transforms = []
    for i, key in enumerate(pair):
        camera = cameras[key]
        transform, preprocessing[key] = canvas_transform(camera, width, height)
        K = transform @ np.asarray(camera['factory_K'], dtype=float)
        orientation = int(camera.get('mercury_camera_orientation_degrees', 0))
        if orientation not in (0, 90, 180, 270):
            raise ValueError('Mercury camera orientation must be 0/90/180/270')
        calib.views[i] = NativeCamera(width, height, (C.c_double*9)(*K.ravel()),
                                     (C.c_double*4)(*camera['factory_kb4']), orientation)
        transforms.append(np.asarray(camera['T_reference_from_camera_candidate'], dtype=float))
    second_from_first = np.linalg.inv(transforms[1]) @ transforms[0]
    calib.rotation = (C.c_double*9)(*second_from_first[:3, :3].ravel())
    calib.translation = (C.c_double*3)(*second_from_first[:3, 3])
    return calib, preprocessing, second_from_first


def prepare_image(image, spec):
    import cv2
    image = np.asarray(image)
    w, h = spec['source_size']
    if image.dtype != np.uint8 or image.ndim != 2 or image.shape != (h, w):
        raise ValueError('Mercury requires the calibrated native uint8 monochrome raster')
    rw, rh = spec['resized_size']
    if (rw, rh) == (w, h) and spec['canvas_size'] == [w, h]:
        return np.ascontiguousarray(image)
    width, height = spec['canvas_size']
    canvas = np.zeros((height, width), dtype=np.uint8)
    x, y = spec['padding_left_top']
    canvas[y:y+rh, x:x+rw] = cv2.resize(image, (rw, rh), interpolation=cv2.INTER_LINEAR)
    return canvas


def joints_to_reference(points_xr, reference_from_first):
    points_cv = np.asarray(points_xr) @ AXES
    return points_cv @ reference_from_first[:3, :3].T + reference_from_first[:3, 3]


def quaternions_to_matrices(q_xyzw):
    x, y, z, w = np.moveaxis(np.asarray(q_xyzw, float), -1, 0)
    return np.stack([np.stack([1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)], -1),
                     np.stack([2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)], -1),
                     np.stack([2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)], -1)], -2)


def matrices_to_quaternions(R):
    R = np.asarray(R, float)
    q = np.empty(R.shape[:-2] + (4,))
    q[..., 3] = np.sqrt(np.maximum(0, 1 + R[..., 0, 0] + R[..., 1, 1] + R[..., 2, 2])) / 2
    q[..., 0] = np.copysign(np.sqrt(np.maximum(0, 1 + R[..., 0, 0] - R[..., 1, 1] - R[..., 2, 2])) / 2, R[..., 2, 1] - R[..., 1, 2])
    q[..., 1] = np.copysign(np.sqrt(np.maximum(0, 1 - R[..., 0, 0] + R[..., 1, 1] - R[..., 2, 2])) / 2, R[..., 0, 2] - R[..., 2, 0])
    q[..., 2] = np.copysign(np.sqrt(np.maximum(0, 1 - R[..., 0, 0] - R[..., 1, 1] + R[..., 2, 2])) / 2, R[..., 1, 0] - R[..., 0, 1])
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def orientations_to_reference(q_xyzw, reference_from_first):
    R = reference_from_first[:3, :3] @ AXES @ quaternions_to_matrices(q_xyzw)
    return matrices_to_quaternions(R)


class MercuryBackend:
    def __init__(self, camera_pairs=None, cameras=None, library=None, models_dir=None,
                 parameters=None, min_image_mean=2., external_roi=None, head_from_reference=None,
                 **unused):
        self.camera_pairs = [tuple(p) for p in (camera_pairs or [('slam_left', 'slam_right')])]
        if not 1 <= len(self.camera_pairs) <= 6 or any(len(p) != 2 or p[0] == p[1] for p in self.camera_pairs) or \
           len({frozenset(p) for p in self.camera_pairs}) != len(self.camera_pairs):
            raise ValueError('Expected 1..6 unique two-camera pairs')
        library = library or os.environ.get('FHL_MERCURY_LIBRARY')
        if not library:
            raise RuntimeError('Set FHL_MERCURY_LIBRARY to the genuine built framehand_mercury library')
        self.library_path = Path(library).resolve()
        self.native = C.CDLL(str(self.library_path))
        self.native.fhm_abi_version.restype = C.c_uint32
        self.native.fhm_source_revision.restype = C.c_char_p
        self.native.fhm_last_error.restype = C.c_char_p
        self.native.fhm_create.argtypes = [C.POINTER(NativeCalibration), C.c_char_p]
        self.native.fhm_create.restype = C.c_void_p
        self.native.fhm_destroy.argtypes = [C.c_void_p]
        self.native.fhm_set_parameter.argtypes = [C.c_void_p, C.c_char_p, C.c_double]
        self.native.fhm_get_native_second_from_first.argtypes = [C.c_void_p, C.POINTER(C.c_double)]
        self.native.fhm_process.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p,
                                           C.c_uint32, C.c_int64, C.POINTER(NativeResult)]
        if self.native.fhm_abi_version() != 1 or self.native.fhm_source_revision().decode() != REVISION:
            raise RuntimeError('Mercury bridge ABI or pinned upstream revision mismatch')
        models_dir = models_dir or os.environ.get('FHL_MERCURY_MODELS_DIR')
        executorch = os.environ.get('FHL_NN_BACKEND') == 'executorch'
        default_dir = Path(__file__).resolve().parents[2]/'models'/('executorch' if executorch else 'fp16')
        self.models_dir = Path(models_dir) if models_dir else default_dir
        if executorch:
            # The .pte files are generated from the pinned fp32 models by models/export_executorch.py, which
            # checks them against ONNX Runtime and records them in manifest.json.
            for name in MODEL_HASHES:
                path = self.models_dir / name.replace('.onnx', '.pte')
                if not path.is_file():
                    raise ValueError(f'Missing native Mercury model: {path}')
        else:
            digests = {}
            for name in MODEL_HASHES:
                path = self.models_dir / name
                if not path.is_file():
                    raise ValueError(f'Missing native Mercury model: {path}')
                digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            if digests not in MODEL_VARIANTS.values():
                raise ValueError(f'Unpinned native Mercury models in {self.models_dir}')
        self.parameters = dict(parameters or {})
        self.has_quality = hasattr(self.native, 'fhm_get_quality')
        if self.has_quality:
            self.native.fhm_quality_abi_version.restype = C.c_uint32
            if self.native.fhm_quality_abi_version() != 1:
                raise RuntimeError('Unsupported native quality ABI')
            self.native.fhm_get_quality.argtypes = [C.c_void_p, C.POINTER(NativeQuality)]
        elif GUARD_NAMES & set(self.parameters):
            self.parameters = {k: v for k, v in self.parameters.items() if k not in GUARD_NAMES}
        self.head_from_reference = None if head_from_reference is None else np.asarray(head_from_reference, float)
        if self.parameters.get('min_hand_eye_dist', 0) > 0 and self.head_from_reference is None:
            self.parameters['min_hand_eye_dist'] = 0.0
        self.min_image_mean = float(min_image_mean)
        self.states, self.last_timestamp = {}, None
        self.cameras = cameras
        self.external_roi = dict(external_roi or {'method':'stock'})
        self.detector_transform = None
        if self.external_roi['method'] == 'mercury_rot':
            from .external_roi import detector_rotation_degrees, DETECTOR_CROP
            if not hasattr(self.native, 'fhm_set_detector_transform'):
                raise RuntimeError('This Mercury library does not support mercury_rot acquisition')
            self.native.fhm_set_detector_transform.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.POINTER(C.c_float), C.c_int]
            self.native.fhm_set_external_crop_twists.argtypes = [C.c_void_p, C.POINTER(C.c_float)]
            angles = detector_rotation_degrees(cameras or {})
            self.detector_transform = {'angles_degrees': angles, 'crop': float(self.external_roi.get('detector_crop', DETECTOR_CROP))}
        elif self.external_roi['method'] != 'stock':
            raise ValueError('Invalid acquisition method')

    def _error(self):
        return self.native.fhm_last_error().decode('utf-8', errors='replace')

    def close(self):
        for state in self.states.values():
            self.native.fhm_destroy(state['handle'])
        self.states.clear()

    def reset(self):
        self.close()
        self.last_timestamp = None

    def __del__(self):
        if hasattr(self, 'states'):
            self.close()

    def _state(self, pair, cameras):
        rows = [cameras[c] for c in pair]
        cached = self.states.get(pair)
        if cached is not None and all(a is b for a, b in zip(cached['rows'], rows)):
            return cached
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        if pair in self.states and self.states[pair]['fingerprint'] != fingerprint:
            self.native.fhm_destroy(self.states.pop(pair)['handle'])
        if pair in self.states:
            self.states[pair]['rows'] = rows
        if pair not in self.states:
            calib, spec, relative = prepare_calibration(cameras, pair)
            handle = self.native.fhm_create(C.byref(calib), os.fsencode(self.models_dir))
            if not handle:
                raise RuntimeError(self._error())
            try:
                parameters = dict(self.parameters)
                if parameters.get('min_hand_eye_dist', 0) > 0:
                    parameters.update(eye_point_parameters(self.head_from_reference,
                                                           cameras[pair[0]]['T_reference_from_camera_candidate']))
                for key, value in parameters.items():
                    if self.native.fhm_set_parameter(handle, key.encode(), float(value)):
                        raise ValueError(f'{key}: {self._error()}')
                actual = (C.c_double*16)()
                if self.native.fhm_get_native_second_from_first(handle, actual):
                    raise RuntimeError('Native axis audit query failed')
                expected = relative.copy()
                expected[:3, :3] = AXES @ relative[:3, :3] @ AXES
                expected[:3, 3] = AXES @ relative[:3, 3]
                error = float(np.max(np.abs(np.asarray(actual).reshape(4, 4)-expected)))
                if error > 2e-5:
                    raise RuntimeError(f'Native stereo axes audit failed: {error}')
                if self.detector_transform is not None:
                    angles = (C.c_float*2)(*[self.detector_transform['angles_degrees'][c] for c in pair])
                    crops = (C.c_float*2)(self.detector_transform['crop'], self.detector_transform['crop'])
                    twists = (C.c_float*4)(*([float(np.deg2rad(self.external_roi['crop_roll_degrees']))]*4))
                    if self.native.fhm_set_detector_transform(handle, angles, crops, 1) or \
                       self.native.fhm_set_external_crop_twists(handle, twists):
                        raise ValueError(self._error())
                self.states[pair] = {'handle': handle, 'preprocessing': spec, 'fingerprint': fingerprint, 'rows': rows}
            except Exception:
                self.native.fhm_destroy(handle)
                raise
        return self.states[pair]

    def process(self, images, cameras=None, timestamp_ns=0):
        cameras = cameras or self.cameras
        timestamp_ns = int(timestamp_ns)
        if self.last_timestamp is not None and (timestamp_ns <= self.last_timestamp or
                                                timestamp_ns-self.last_timestamp > 250_000_000):
            self.reset()
        self.last_timestamp = timestamp_ns
        result = {'hands': []}
        candidates = {0: [], 1: []}
        for pair in self.camera_pairs:
            if any(c not in images or c not in cameras for c in pair):
                continue
            if any(mean_brightness(images[c]) < self.min_image_mean for c in pair):
                continue
            state = self._state(pair, cameras)
            frames = [prepare_image(images[c], state['preprocessing'][c]) for c in pair]
            output = NativeResult()
            rc = self.native.fhm_process(state['handle'], frames[0].ctypes.data, frames[0].strides[0],
                                         frames[1].ctypes.data, frames[1].strides[0], timestamp_ns, C.byref(output))
            if rc:
                raise RuntimeError(self._error())
            reference = np.asarray(cameras[pair[0]]['T_reference_from_camera_candidate'])
            quality = None
            if self.has_quality:
                native_quality = NativeQuality()
                if self.native.fhm_get_quality(state['handle'], C.byref(native_quality)):
                    raise RuntimeError('Native quality query failed')
                quality = hand_qualities(native_quality)
                result.setdefault('hand_quality', quality)
            for hand_id, hand in enumerate(output.hands):
                if not hand.active:
                    continue
                points = joints_to_reference(np.array([list(j.position_m) for j in hand.joints]), reference)
                orientations = orientations_to_reference([list(j.quaternion_xyzw) for j in hand.joints], reference)
                estimate = {'hand_id': hand_id, 'valid': True, 'confidence': None, 'landmarks_m': points.tolist(),
                            'landmark_orientations_xyzw': orientations.round(6).tolist(),
                            'joints_m': points[MAPPING21].tolist()}
                if quality is not None:
                    estimate.update(quality[hand_id])
                if not np.isfinite(points).all():
                    continue
                candidates[hand_id].append(estimate)
        for estimates in candidates.values():
            if not estimates:
                continue
            points = np.asarray([e['joints_m'] for e in estimates])
            distances = np.linalg.norm(points[:, None]-points[None, :], axis=-1).mean(axis=-1)
            result['hands'].append(estimates[int(np.argmin(distances.sum(axis=1)))])
        result['status'] = 'pose_estimate' if result['hands'] else 'no_active_hands'
        return result
