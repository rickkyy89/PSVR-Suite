"""Five-front-LED correspondence/PnP experiment with explicit model provenance.

Initial acquisition requires a roughly upright, front-facing headset and all
five lamps. Once acquired, projected points maintain identities; no sorting of
screen coordinates is used during movement. Missing/ambiguous matches publish
loss rather than keeping the last position valid.
"""

from dataclasses import dataclass
from itertools import permutations
import json
from pathlib import Path

import numpy as np

from .camera_backend import import_cv2


@dataclass
class LedPose:
    valid: bool
    reason: str
    image_points: object = None
    rvec: object = None
    tvec: object = None
    rms_px: object = None
    rms_limit_px: object = None
    image_span_px: object = None
    led_labels: object = None
    predicted_labels: object = None
    imu_age_s: object = None

    def to_dict(self):
        return {"valid": self.valid, "reason": self.reason,
                "position_camera_m": None if self.tvec is None else self.tvec.reshape(3).tolist(),
                "rvec": None if self.rvec is None else self.rvec.reshape(3).tolist(),
                "rms_px": self.rms_px,
                "rms_limit_px": self.rms_limit_px,
                "image_span_px": self.image_span_px,
                "rms_fraction": None if self.rms_px is None or not self.image_span_px else self.rms_px/self.image_span_px,
                "led_labels": self.led_labels,"predicted_labels":self.predicted_labels,
                "imu_age_s":self.imu_age_s,
                "image_points": None if self.image_points is None else self.image_points.tolist(),
                "origin": "front_centre_led", "geometry_validated": False}


def load_led_rig(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != "psvr.camera.led_rig" or data.get("version") != 1:
        raise ValueError("unsupported LED rig schema/version")
    points = np.asarray(data.get("object_points_m"), dtype=float)
    labels = data.get("labels")
    if points.shape != (5, 3) or not np.isfinite(points).all():
        raise ValueError("front rig requires five finite 3D points")
    if not isinstance(labels, list) or len(labels) != 5 or len(set(labels)) != 5:
        raise ValueError("front rig requires five distinct labels")
    if np.linalg.matrix_rank(points-points.mean(axis=0)) < 3:
        raise ValueError("front rig must include the centre LED depth offset")
    if not data.get("provenance") or not data.get("source"):
        raise ValueError("LED rig requires explicit provenance and source")
    return data, points


class FrontLedTracker:
    def __init__(self, intrinsics, points, max_rms_px=2.5, module=None):
        if not intrinsics.calibrated:
            raise ValueError("LED pose trial requires calibrated camera intrinsics")
        if max_rms_px <= 0 or not np.isfinite(max_rms_px):
            raise ValueError("max_rms_px must be finite and positive")
        self.cv2 = import_cv2(module)
        self.points = np.asarray(points, dtype=np.float64)
        if self.points.shape != (5, 3) or not np.isfinite(self.points).all():
            raise ValueError("five finite LED coordinates required")
        self.k = np.asarray(intrinsics.camera_matrix, dtype=float)
        self.dist = np.asarray(intrinsics.distortion_coeffs, dtype=float)
        self.max_rms = max_rms_px
        self.previous = None
        self.last_valid = None
        self.acquisition_span = None
        self.confirmations = 0
        self.best_fit = None

    def reset(self):
        self.previous = None
        self.last_valid = None
        self.acquisition_span = None
        self.confirmations = 0

    def search_region(self, width, height, now):
        """Full image only for acquisition; scale-aware ROI after a pose match."""
        if self.last_valid is not None and now-self.last_valid > 0.35:
            self.reset()
        if self.previous is None:
            return None, 1.0
        projected = self.project(self.previous.rvec,self.previous.tvec)
        low, high = projected.min(axis=0), projected.max(axis=0)
        span = float(np.max(high-low))
        margin = max(12.0,span*.25)
        x0,y0 = np.maximum(0,np.floor(low-margin)).astype(int)
        x1,y1 = np.minimum([width,height],np.ceil(high+margin)).astype(int)
        return (int(x0),int(y0),int(x1),int(y1)), span/max(self.acquisition_span,1.0)

    def project(self, rvec, tvec):
        return self.cv2.projectPoints(self.points, rvec, tvec, self.k, self.dist)[0].reshape(-1, 2)

    def reprojection_limit(self, span):
        # Bounded scale term for the experimental ruler model's proportional
        # error, with an absolute pixel-noise floor. Not physical validation.
        return self.max_rms*min(3.2,max(1.0,float(span)/100.0))

    def solve(self, image_points):
        cv2 = self.cv2
        try:
            ok, rvecs, tvecs, _ = cv2.solvePnPGeneric(self.points, image_points,
                self.k, self.dist, flags=cv2.SOLVEPNP_SQPNP)
        except cv2.error:
            return []
        results = []
        if not ok:
            return results
        for rvec, tvec in zip(rvecs, tvecs):
            rvec, tvec = cv2.solvePnPRefineLM(self.points, image_points, self.k, self.dist, rvec, tvec)
            rotation = cv2.Rodrigues(rvec)[0]
            depths = (rotation @ self.points.T + tvec.reshape(3, 1))[2]
            if not np.isfinite(tvec).all() or np.min(depths) < 0.1 or not 0.15 < tvec[2,0] < 4.0:
                continue
            # Upright/front-facing acquisition convention also rejects the
            # symmetric upside-down and back-facing correspondence permutations.
            if rotation[1,1] < 0.5 or rotation[2,2] < 0.35:
                continue
            rms = float(np.sqrt(np.mean(np.sum((self.project(rvec,tvec)-image_points)**2, axis=1))))
            span = float(np.max(np.ptp(image_points,axis=0)))
            limit = self.reprojection_limit(span)
            if np.isfinite(rms):
                if self.best_fit is None or rms < self.best_fit[0]:
                    self.best_fit = (rms,limit,span)
                if rms <= limit:
                    results.append(LedPose(True, "tracking_experimental", image_points.copy(), rvec, tvec, rms,limit,span))
        return results

    def lost(self, reason):
        self.confirmations = 0
        return LedPose(False,reason)

    def update(self, candidates, now, acquisition_hint=None):
        self.best_fit = None
        if self.last_valid is not None and now-self.last_valid > 0.35:
            self.reset()
        centers = np.asarray([c["center_px"] for c in candidates], dtype=float).reshape(-1, 2)
        if len(centers) < 5:
            return self.lost("lost: servono tutti i cinque LED frontali")
        if not np.isfinite(centers).all() or len(centers) > 100:
            return self.lost("lost: troppi candidati o punti non validi")
        possibilities = []
        if self.previous is None:
            if acquisition_hint is not None:
                hint = np.asarray(acquisition_hint, dtype=float)
                if hint.shape != (5, 2) or not np.isfinite(hint).all():
                    return self.lost("acquisizione: previsione frontale non valida")
                distances = np.linalg.norm(hint[:, None]-centers[None], axis=2)
                order = np.argmin(distances, axis=1)
                separation = np.linalg.norm(hint[:, None]-hint[None], axis=2)
                np.fill_diagonal(separation, np.inf)
                gate = min(35., max(5., float(np.min(separation))*.45))
                sorted_distances = np.sort(distances, axis=1)
                if (len(set(order.tolist())) != 5 or np.any(distances[np.arange(5), order] > gate)
                        or np.any(sorted_distances[:, 1]-sorted_distances[:, 0] < 3)):
                    return self.lost("acquisizione: frontali previsti non distinti")
                possibilities.extend(self.solve(centers[order]))
            else:
                if len(centers) != 5:
                    return self.lost("acquisizione: avvicinati frontale per agganciare cinque LED")
                for order in permutations(range(5)):
                    possibilities.extend(self.solve(centers[list(order)]))
        else:
            predicted = self.project(self.previous.rvec, self.previous.tvec)
            distances = np.linalg.norm(predicted[:,None,:]-centers[None,:,:], axis=2)
            order = np.argmin(distances, axis=1)
            sorted_distances = np.sort(distances, axis=1)
            separation = np.linalg.norm(predicted[:,None,:]-predicted[None,:,:],axis=2)
            np.fill_diagonal(separation,np.inf)
            gate = min(35.0,max(5.0,float(np.min(separation))*.45))
            if (len(set(order.tolist())) != 5 or np.any(distances[np.arange(5), order] > gate)
                    or np.any(sorted_distances[:,1]-sorted_distances[:,0] < 3)):
                return self.lost("lost: associazione LED ambigua o movimento troppo rapido")
            possibilities = self.solve(centers[order])
        if not possibilities:
            if self.best_fit is not None:
                self.confirmations = 0
                rms,limit,span = self.best_fit
                return LedPose(False,"LED rilevati, modello non compatibile: RMS %.2f > %.2f px" % (rms,limit),
                               rms_px=rms,rms_limit_px=limit,image_span_px=span)
            return self.lost("LED rilevati, nessuna posa frontale compatibile")
        possibilities.sort(key=lambda p: p.rms_px)
        best = possibilities[0]
        for alternative in possibilities[1:]:
            distinct = np.max(np.linalg.norm(best.image_points-alternative.image_points, axis=1)) > 3
            # Multiple PnP branches of the same mapping can also be ambiguous.
            distinct = distinct or np.linalg.norm(best.tvec-alternative.tvec) > 0.05
            if distinct and alternative.rms_px-best.rms_px < 0.3:
                return self.lost("ambiguous: piu soluzioni; torna frontale")
        self.previous = best
        self.last_valid = now
        if self.acquisition_span is None:
            self.acquisition_span = float(np.max(np.ptp(best.image_points,axis=0)))
        self.confirmations += 1
        if self.confirmations < 4:
            return LedPose(False,"AGGANCIO: resta frontale (%d/4 conferme)" % self.confirmations)
        return best
