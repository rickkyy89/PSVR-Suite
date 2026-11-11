"""Regressioni B6-06 / B6-09 per tools/psvr_camera/pose_uncertainty.py.

B6-06: inflate_optical_covariance rifiuta covarianze non valide e
riflessioni; il percorso valido resta numericamente invariato.
B6-09: estimate_pose_covariance rifiuta scale non numerico con
invalid_input e label stringa con invalid_correspondences.
"""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.led_pose import LedPose
from tools.psvr_camera.pose_uncertainty import (
    estimate_pose_covariance,
    inflate_optical_covariance,
)


@pytest.fixture
def scene():
    points = np.array([[-.09, -.04, 0], [.09, -.04, 0],
                       [-.09, .04, 0], [.09, .04, 0], [0, 0, -.035]])
    labels = ['tl', 'tr', 'bl', 'br', 'centre']
    intrinsics = Intrinsics(np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1]]),
                            np.array([.03, -.01, .001, -.002, 0]), (640, 480), True)
    return points, labels, intrinsics


def _pose_for(scene, z=1., ids=None, rvec=(.05, -.08, .02)):
    points, labels, intrinsics = scene
    ids = list(range(len(points))) if ids is None else ids
    rvec = np.array(rvec, dtype=float)
    tvec = np.array([.02, -.01, z])
    image = cv2.projectPoints(points[ids], rvec, tvec, intrinsics.camera_matrix,
                              intrinsics.distortion_coeffs)[0].reshape(-1, 2)
    return LedPose(True, 'test', image, rvec, tvec,
                   led_labels=[labels[i] for i in ids])


def _estimate(scene, pose=None, **kwargs):
    points, labels, intrinsics = scene
    return estimate_pose_covariance(pose or _pose_for(scene), intrinsics, points,
                                    labels, kwargs.pop('alignment', np.eye(3)), **kwargs)


# --- B6-06: inflate rifiuta input non validi ---

def test_b606_inflate_nan_covariance_rejected():
    bad = np.eye(3) * 1e-6
    bad[0, 0] = np.nan
    with pytest.raises(ValueError, match='invalid optical covariance'):
        inflate_optical_covariance(bad, np.eye(3), 2.0)
    # Anche senza inflazione la covarianza viene validata.
    with pytest.raises(ValueError, match='invalid optical covariance'):
        inflate_optical_covariance(bad, np.eye(3))


def test_b606_inflate_bad_shape_rejected():
    with pytest.raises(ValueError, match='invalid optical covariance'):
        inflate_optical_covariance(np.eye(2), np.eye(3), 2.0)
    with pytest.raises(ValueError, match='invalid optical covariance'):
        inflate_optical_covariance(np.zeros(3), np.eye(3), 2.0)


def test_b606_inflate_asymmetric_covariance_rejected():
    bad = np.array([[1e-6, 1e-7, 0.], [0., 1e-6, 0.], [0., 0., 1e-6]])
    assert not np.allclose(bad, bad.T)
    with pytest.raises(ValueError, match='invalid optical covariance'):
        inflate_optical_covariance(bad, np.eye(3), 2.0)


def test_b606_inflate_reflection_alignment_rejected():
    cov = np.eye(3) * 1e-6
    reflection = np.diag([1., 1., -1.])
    assert np.allclose(reflection.T @ reflection, np.eye(3), atol=1e-6)
    assert not np.isclose(np.linalg.det(reflection), 1.0, atol=1e-6)
    with pytest.raises(ValueError, match='invalid optical-axis alignment'):
        inflate_optical_covariance(cov, reflection, 2.0)


def test_b606_inflate_valid_numerically_unchanged():
    rng = np.random.default_rng(7)
    m = rng.normal(size=(3, 3)) * 1e-3
    cov = m @ m.T + np.eye(3) * 1e-6
    out = inflate_optical_covariance(cov, np.eye(3), 2.0)
    u = np.array([0., 0., 1.])
    expected = cov + float(u @ cov @ u) * np.outer(u, u)
    np.testing.assert_allclose(out, (expected + expected.T) * .5, rtol=1e-12, atol=1e-18)
    # Le proiezioni trasverse restano invariate.
    np.testing.assert_allclose(out[:2, :2], cov[:2, :2], rtol=1e-12, atol=1e-18)
    # Senza inflazione: copia validata, non alias.
    plain = inflate_optical_covariance(cov, np.eye(3))
    np.testing.assert_allclose(plain, cov, rtol=0, atol=0)
    assert plain is not cov
    assert inflate_optical_covariance(None, np.eye(3), 2.0) is None


# --- B6-09: scale e label ---

@pytest.mark.parametrize('bad_scale', [True, False, '1.7', None, [1.7]])
def test_b609_non_numeric_scale_invalid_input(scene, bad_scale):
    _, quality = _estimate(scene, scale=bad_scale)
    assert quality['reason'] == 'invalid_input'


@pytest.mark.parametrize('bad_scale', [True, '1.7'])
def test_b609_non_numeric_scale_guided_invalid_input(scene, bad_scale):
    _, quality = _estimate(scene, scale=bad_scale, guided=True)
    assert quality['reason'] == 'invalid_input'


def test_b609_string_labels_invalid_correspondences(scene):
    points, _, intrinsics = scene
    pose = _pose_for(scene)
    cov, quality = estimate_pose_covariance(pose, intrinsics, points,
                                            'tltrblbrcentre', np.eye(3))
    assert cov is None and quality['reason'] == 'invalid_correspondences'


def test_b609_string_pose_labels_invalid_correspondences(scene):
    pose = _pose_for(scene)
    pose.led_labels = 'tltrblbrcentre'
    _, quality = _estimate(scene, pose)
    assert quality['reason'] == 'invalid_correspondences'


def test_b609_legitimate_numeric_scale_unchanged(scene):
    cov, quality = _estimate(scene, scale=1.7)
    assert cov is not None, quality
    assert quality['reason'] == 'ok'
