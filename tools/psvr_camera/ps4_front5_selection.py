"""Conservative five-LED constellation selection amid certified stereo clutter.

This is only a necessary rigid-distance test, never a pose estimator. All five
original pose inlier/reprojection/ambiguity gates must still pass afterwards.
More than one plausible subset is rejected, not ranked to force a result.
"""
from functools import lru_cache
from itertools import combinations

import numpy as np


@lru_cache(maxsize=12)
def _combinations(count):
    # StereoLedConfig bounds each eye to <=16, thus at most C(16,5)=4368.
    result = np.asarray(list(combinations(range(count), 5)), dtype=np.intp)
    result.setflags(write=False)
    return result


def unique_front5_subset(matches, model, point_inlier_limit_m):
    """Return (indices or None, number of distance-compatible subsets).

    If each of five inlier residuals is <=r, every pair distance differs by
    <=2r (triangle inequality). Sorting the ten pair distances preserves this
    necessary bound without guessing LED identities or spatial sort order.
    This filter never widens r and never replaces the strict five-point solver.
    """
    count = len(matches)
    if count < 5 or count > 16:
        return None, 0
    points = np.asarray([m["point_left_camera_m"] for m in matches], dtype=float)
    model = np.asarray(model, dtype=float)
    if (points.shape != (count, 3) or model.shape != (5, 3)
            or not np.isfinite(points).all() or not np.isfinite(model).all()
            or not np.isfinite(point_inlier_limit_m) or point_inlier_limit_m <= 0):
        raise ValueError("finite stereo/model points and a positive inlier bound required")
    if count == 5:
        return np.arange(5, dtype=np.intp), 1
    a, b = np.triu_indices(5, 1)
    signature = np.sort(np.linalg.norm(model[a] - model[b], axis=1))
    indices = _combinations(count)
    clouds = points[indices]
    distances = np.sort(np.linalg.norm(clouds[:, a] - clouds[:, b], axis=2), axis=1)
    viable = np.flatnonzero(np.all(np.abs(distances - signature) <= 2 * point_inlier_limit_m, axis=1))
    return (indices[viable[0]].copy() if len(viable) == 1 else None), int(len(viable))
