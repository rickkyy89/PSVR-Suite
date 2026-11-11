"""Bounded PS4 stereo-edge diagnostics, never alternate calibration or pose gates.

Inputs are raw distorted per-eye pixels in the pinned Suite right,left crop
order. Eligible edges/rejections are evaluated by the production matcher with
the exact same configuration; no alternate geometry is chosen for success.
"""
from dataclasses import asdict

import numpy as np

from .camera_backend import import_cv2
from .stereo_leds import StereoLedConfig, triangulate_candidates


def diagnose_pair(calibration, left, right, config=None):
    """Return <=16x16 costs and first rejection reasons, plus full assignment."""
    config = StereoLedConfig() if config is None else config
    result = triangulate_candidates(calibration, left, right, config)
    report = {"schema": "psvr.camera.ps4.geometry_diagnostics", "version": 1,
              "input_space": "raw_distorted_pixels", "epipolar_space": "undistorted_pixels",
              "coordinate_frame": "calibration_left_camera",
              "left_candidates": left, "right_candidates": right,
              "matching": asdict(config), "assignment_result": result,
              "edges": [], "epipolar_cost_px": [], "raw_pixel_F_distance_px": []}
    if result["state"] == "candidate_limit" or not left or not right:
        return report
    cv2 = import_cv2()
    raw = [np.asarray([v["center_px"] for v in eye], np.float64) for eye in (left, right)]
    und = [cv2.undistortPoints(points.reshape(-1, 1, 2), intr.camera_matrix,
                               intr.distortion_coeffs, P=intr.camera_matrix).reshape(-1, 2)
           for points, intr in zip(raw, (calibration.left, calibration.right))]
    report["undistorted_pixels"] = {"left": und[0].tolist(), "right": und[1].tolist()}
    fundamental = calibration.F / np.linalg.norm(calibration.F)
    report["F_normalized"] = fundamental.tolist()
    chosen = {(v["left_index"], v["right_index"]) for v in result["matches"]}
    withheld = {(v["left_index"], v["right_index"]) for v in result["withheld_pairs"]}

    def distance(lp, rp):
        lh, rh = np.r_[lp, 1.0], np.r_[rp, 1.0]
        line_r, line_l = fundamental @ lh, fundamental.T @ rh
        denom = min(np.linalg.norm(line_r[:2]), np.linalg.norm(line_l[:2]))
        return None if denom < 1e-15 else float(abs(rh @ line_r) / denom)

    for i, lc in enumerate(left):
        epipolar, raw_distances = [], []
        for j, rc in enumerate(right):
            epi = distance(und[0][i], und[1][j])
            epipolar.append(epi)
            raw_distances.append(distance(raw[0][i], raw[1][j]))
            edge_result = triangulate_candidates(calibration, [lc], [rc], config)
            edge = {"left_index": i, "right_index": j, "epipolar_px": epi,
                    "eligible": bool(edge_result["matches"]),
                    "rejection": next(iter(edge_result["rejections"]), None),
                    "selected": (i, j) in chosen, "withheld": (i, j) in withheld}
            if edge_result["matches"]:
                match = edge_result["matches"][0]
                edge.update({k: v for k, v in match.items() if k not in ("left_index", "right_index")})
                edge["assignment_cost"] = ((match["epipolar_px"] / config.max_epipolar_px) ** 2
                                            + (match["reprojection_px"] / config.max_reprojection_px) ** 2)
            report["edges"].append(edge)
        report["epipolar_cost_px"].append(epipolar)
        report["raw_pixel_F_distance_px"].append(raw_distances)
    return report
