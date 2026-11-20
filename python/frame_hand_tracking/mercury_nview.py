import ctypes as C
import os
from pathlib import Path
import numpy as np

from .mercury import (GUARD_NAMES, NativeCamera, NativeQuality, NativeResult, eye_point_parameters, joints_to_reference,
                      mean_brightness, orientations_to_reference, hand_qualities, MAPPING21)
from .external_roi import detector_rotation_degrees, DETECTOR_CROP

MAX_VIEWS = 4
DEFAULT_VIEWS = ('slam_left', 'slam_right', 'upper_left', 'upper_right')


class NativeRig(C.Structure):
    _fields_ = [('num_views', C.c_int32), ('views', NativeCamera * MAX_VIEWS),
                ('view_from_first_rotation', (C.c_double * 9) * MAX_VIEWS),
                ('view_from_first_translation_m', (C.c_double * 3) * MAX_VIEWS),
                ('boundary_circle', (C.c_float * 3) * MAX_VIEWS)]


class NViewBackend:
    def __init__(self, cameras, views=DEFAULT_VIEWS, library=None, models_dir=None, parameters=None,
                 external_roi=None, lens_circles=True, min_image_mean=2., head_from_reference=None):
        self.views = tuple(views)
        if not 1 <= len(self.views) <= MAX_VIEWS or len(set(self.views)) != len(self.views):
            raise ValueError('Expected 1..4 distinct views')
        library = library or os.environ.get('FHL_MERCURY_LIBRARY')
        self.native = C.CDLL(str(Path(library).resolve()))
        n = self.native
        n.fhm_nview_abi_version.restype = C.c_uint32
        if n.fhm_nview_abi_version() != 1:
            raise RuntimeError('N-view ABI mismatch')
        n.fhm_last_error.restype = C.c_char_p
        n.fhm_create_n.argtypes = [C.POINTER(NativeRig), C.c_char_p]; n.fhm_create_n.restype = C.c_void_p
        n.fhm_process_n.argtypes = [C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_uint32), C.c_int64,
                                    C.POINTER(NativeResult)]
        n.fhm_set_parameter.argtypes = [C.c_void_p, C.c_char_p, C.c_double]
        n.fhm_set_detector_transform_n.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_float), C.POINTER(C.c_float), C.c_int]
        n.fhm_set_crop_twists_n.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_float)]
        n.fhm_destroy.argtypes = [C.c_void_p]
        self.models_dir = Path(models_dir or os.environ['FHL_MERCURY_MODELS_DIR'])
        self.cameras = cameras
        self.parameters = dict(parameters or {})
        self.has_quality = hasattr(n, 'fhm_get_quality')
        if self.has_quality:
            n.fhm_quality_abi_version.restype = C.c_uint32
            if n.fhm_quality_abi_version() != 1:
                raise RuntimeError('Unsupported native quality ABI')
            n.fhm_get_quality.argtypes = [C.c_void_p, C.POINTER(NativeQuality)]
        elif GUARD_NAMES & set(self.parameters):
            self.parameters = {k: v for k, v in self.parameters.items() if k not in GUARD_NAMES}
        self.head_from_reference = None if head_from_reference is None else np.asarray(head_from_reference, float)
        if self.parameters.get('min_hand_eye_dist', 0) > 0 and self.head_from_reference is None:
            self.parameters['min_hand_eye_dist'] = 0.0
        self.external_roi = dict(external_roi or {'method': 'stock'})
        self.lens_circles = lens_circles
        self.min_image_mean = float(min_image_mean)
        self.handle = None
        self.last_timestamp = None

    def _create(self, cameras):
        rig = NativeRig()
        rig.num_views = len(self.views)
        T = {c: np.asarray(cameras[c]['T_reference_from_camera_candidate'], float) for c in self.views}
        first = T[self.views[0]]
        for i, name in enumerate(self.views):
            cam = cameras[name]
            rig.views[i] = NativeCamera(int(cam['width']), int(cam['height']),
                                        (C.c_double * 9)(*np.asarray(cam['factory_K'], float).ravel()),
                                        (C.c_double * 4)(*cam['factory_kb4']),
                                        int(cam.get('mercury_camera_orientation_degrees', 0)))
            view_from_first = np.linalg.inv(T[name]) @ first
            rig.view_from_first_rotation[i] = (C.c_double * 9)(*view_from_first[:3, :3].ravel())
            rig.view_from_first_translation_m[i] = (C.c_double * 3)(*view_from_first[:3, 3])
            if self.lens_circles and cam.get('calibrated_image_circle_diameter_degrees'):
                rig.boundary_circle[i] = (C.c_float * 3)(*self._circle(cam))
        handle = self.native.fhm_create_n(C.byref(rig), os.fsencode(self.models_dir))
        if not handle:
            raise RuntimeError(self.native.fhm_last_error().decode())
        try:
            parameters = dict(self.parameters)
            if parameters.get('min_hand_eye_dist', 0) > 0:
                parameters.update(eye_point_parameters(self.head_from_reference, first))
            for key, value in parameters.items():
                if self.native.fhm_set_parameter(handle, key.encode(), float(value)):
                    raise ValueError(f'{key}: {self.native.fhm_last_error().decode()}')
            if self.external_roi.get('method') == 'mercury_rot':
                angles = detector_rotation_degrees(cameras)
                crop = float(self.external_roi.get('detector_crop', DETECTOR_CROP))
                a = (C.c_float * len(self.views))(*[angles[c] for c in self.views])
                k = (C.c_float * len(self.views))(*[crop] * len(self.views))
                twist = float(np.deg2rad(self.external_roi.get('crop_roll_degrees', 0)))
                t = (C.c_float * (2 * len(self.views)))(*[twist] * (2 * len(self.views)))
                if self.native.fhm_set_detector_transform_n(handle, len(self.views), a, k, 1) or \
                   self.native.fhm_set_crop_twists_n(handle, len(self.views), t):
                    raise ValueError(self.native.fhm_last_error().decode())
        except Exception:
            self.native.fhm_destroy(handle)
            raise
        self.handle = handle

    @staticmethod
    def _circle(cam):
        K = np.asarray(cam['factory_K'], float); k = cam['factory_kb4']
        th = np.deg2rad(cam['calibrated_image_circle_diameter_degrees']) / 2
        r = th * (1 + k[0] * th**2 + k[1] * th**4 + k[2] * th**6 + k[3] * th**8) * K[0, 0]
        return K[0, 2] / cam['width'], K[1, 2] / cam['height'], r / cam['width']

    def close(self):
        if self.handle:
            self.native.fhm_destroy(self.handle)
        self.handle = None

    def reset(self):
        self.close()
        self.last_timestamp = None

    def __del__(self):
        self.close()

    def process(self, images, cameras=None, timestamp_ns=0):
        cameras = cameras or self.cameras
        timestamp_ns = int(timestamp_ns)
        # Opening the ExecuTorch delegate can take longer than the ordinary frame-gap limit.
        # Keep its model handles across that first slow frame so acquisition can advance.
        reset_gap_ns = 2_000_000_000 if os.environ.get('FHL_NN_BACKEND') == 'executorch' else 250_000_000
        if self.last_timestamp is not None and (timestamp_ns <= self.last_timestamp or
                                                timestamp_ns - self.last_timestamp > reset_gap_ns):
            self.reset()
        self.last_timestamp = timestamp_ns
        result = {'hands': []}
        if any(mean_brightness(images[c]) < self.min_image_mean for c in self.views[:1]):
            result['status'] = 'low_image_mean'
            return result
        if self.handle is None:
            self._create(cameras)
        frames = [np.ascontiguousarray(images[c], np.uint8) for c in self.views]
        ptrs = (C.c_void_p * len(frames))(*[f.ctypes.data for f in frames])
        strides = (C.c_uint32 * len(frames))(*[f.strides[0] for f in frames])
        out = NativeResult()
        if self.native.fhm_process_n(self.handle, ptrs, strides, timestamp_ns, C.byref(out)):
            raise RuntimeError(self.native.fhm_last_error().decode())
        quality = None
        if self.has_quality:
            native_quality = NativeQuality()
            if self.native.fhm_get_quality(self.handle, C.byref(native_quality)):
                raise RuntimeError('Native quality query failed')
            quality = result['hand_quality'] = hand_qualities(native_quality)
        reference = np.asarray(cameras[self.views[0]]['T_reference_from_camera_candidate'], float)
        for hand_id, hand in enumerate(out.hands):
            if not hand.active:
                continue
            points = joints_to_reference(np.array([list(j.position_m) for j in hand.joints]), reference)
            if not np.isfinite(points).all():
                continue
            estimate = {
                'hand_id': hand_id, 'valid': True, 'confidence': None, 'landmarks_m': points.tolist(),
                'landmark_orientations_xyzw': orientations_to_reference(
                    [list(j.quaternion_xyzw) for j in hand.joints], reference).round(6).tolist(),
                'joints_m': points[MAPPING21].tolist()}
            if quality is not None:
                estimate.update(quality[hand_id])
            result['hands'].append(estimate)
        result['status'] = 'pose_estimate' if result['hands'] else 'no_active_hands'
        return result
