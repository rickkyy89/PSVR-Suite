"""ArUco detection adapter (optional OpenCV).

Only the *detection* lives here; pose is solved in ``geometry``/``estimator``.
Both the legacy ``cv2.aruco.detectMarkers`` and the newer ``ArucoDetector`` API
are supported by probing the module at call time.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .camera_backend import CameraError, import_cv2


class MarkerDetectionError(CameraError):
    pass


class ArucoDetector:
    def __init__(self, dictionary: str = "DICT_4X4_50", marker_ids: Optional[Sequence[int]] = None, module=None) -> None:
        self.cv2 = import_cv2(module)
        aruco = getattr(self.cv2, "aruco", None)
        if aruco is None:
            raise MarkerDetectionError(
                "this OpenCV build has no aruco module (opencv-contrib-python is needed)"
            )
        self.dictionary_name = dictionary
        self.marker_ids = None if marker_ids is None else {int(v) for v in marker_ids}
        getter = getattr(aruco, "getPredefinedDictionary", None) or getattr(
            aruco, "Dictionary_get", None
        )
        if getter is None:
            raise MarkerDetectionError("no ArUco dictionary accessor in this OpenCV build")
        dictionary_id = getattr(aruco, dictionary, None)
        if dictionary_id is None:
            raise MarkerDetectionError(f"unknown ArUco dictionary {dictionary!r}")
        self.dictionary = getter(dictionary_id)
        self._detector = None
        if hasattr(aruco, "ArucoDetector"):
            params = (
                aruco.DetectorParameters()
                if hasattr(aruco, "DetectorParameters")
                else None
            )
            self._detector = aruco.ArucoDetector(self.dictionary, params)
        else:
            self._params = (
                aruco.DetectorParameters_create()
                if hasattr(aruco, "DetectorParameters_create")
                else None
            )

    def detect(self, image) -> dict:
        """Return ``{marker_id: Nx2 corners}`` for the requested ids only."""
        cv2 = self.cv2
        gray = image
        if getattr(image, "ndim", 2) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if self._detector is not None:
            corners, ids, _ = self._detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(
                gray, self.dictionary, parameters=self._params
            )
        result = {}
        if ids is None:
            return result
        for marker_id, marker_corners in zip(np.asarray(ids).reshape(-1), corners):
            marker_id = int(marker_id)
            if self.marker_ids is not None and marker_id not in self.marker_ids:
                continue
            result[marker_id] = np.asarray(marker_corners, dtype=np.float64).reshape(-1, 2)
        return result


def single_marker_corners(image, detector: ArucoDetector, marker_id: int = 0):
    """Convenience: corners for one marker id, or ``None`` if not found."""
    detected = detector.detect(image)
    return detected.get(int(marker_id))
