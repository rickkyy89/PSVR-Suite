import copy
import json

import numpy as np
try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("PnP covariance tests require pytest")

from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.led_pose import LedPose
from tools.psvr_camera.pose_uncertainty import estimate_pose_covariance
from tools.psvr_camera import pose_uncertainty
from tools.psvr_camera.camera_backend import OpenCvUnavailable

cv2 = pytest.importorskip('cv2')


@pytest.fixture
def scene():
    points = np.array([[-.09, -.04, 0], [.09, -.04, 0],
                       [-.09, .04, 0], [.09, .04, 0], [0, 0, -.035]])
    labels = ['tl', 'tr', 'bl', 'br', 'centre']
    intrinsics = Intrinsics(np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1]]),
                            np.array([.03, -.01, .001, -.002, 0]), (640, 480), True)
    return points, labels, intrinsics


def pose_for(scene, z=1., ids=None, rvec=(.05, -.08, .02)):
    points, labels, intrinsics = scene
    ids = list(range(len(points))) if ids is None else ids
    rvec = np.array(rvec, dtype=float)
    tvec = np.array([.02, -.01, z])
    image = cv2.projectPoints(points[ids], rvec, tvec, intrinsics.camera_matrix,
                              intrinsics.distortion_coeffs)[0].reshape(-1, 2)
    return LedPose(True, 'test', image, rvec, tvec,
                   led_labels=[labels[i] for i in ids])


def estimate(scene, pose=None, **kwargs):
    points, labels, intrinsics = scene
    return estimate_pose_covariance(pose or pose_for(scene), intrinsics, points,
                                    labels, kwargs.pop('alignment', np.eye(3)), **kwargs)


@pytest.mark.parametrize('guided', [False, True])
def test_real_projection_positive_and_json(scene, guided):
    cov, q = estimate(scene, guided=guided)
    assert cov is not None, q
    np.testing.assert_allclose(cov, cov.T, atol=1e-15)
    assert np.linalg.eigvalsh(cov)[0] >= .0015 ** 2
    assert q['reliable'] and q['led_count'] == 5
    assert q['rank'] == (3 if guided else 6)
    assert q['cond'] < 1e6 and q['sigma_px'] == .75
    assert q['rms_px'] == 0
    json.dumps(q, allow_nan=False)


@pytest.mark.parametrize('guided', [False, True])
def test_two_leds_always_rejected(scene, guided):
    cov, q = estimate(scene, pose_for(scene, ids=[0, 1]), guided=guided)
    assert cov is None and q['reason'] == 'insufficient_leds'
    assert not q['reliable'] and q['led_count'] == 2


@pytest.mark.parametrize('guided', [False, True])
def test_depth_anisotropy_and_distance(scene, guided):
    near, _ = estimate(scene, pose_for(scene, z=.8), guided=guided)
    far, q = estimate(scene, pose_for(scene, z=2.), guided=guided)
    assert far is not None, q
    assert near[2, 2] > 3 * max(near[0, 0], near[1, 1])
    assert far[2, 2] > 4 * near[2, 2]


def test_free_rotation_is_marginalized(scene):
    free, q = estimate(scene)
    guided, _ = estimate(scene, guided=True)
    assert free is not None, q
    assert np.trace(free) > np.trace(guided)
    # Independent full information inverse, not the conditional tvec block.
    pose = pose_for(scene)
    jac = cv2.projectPoints(scene[0], pose.rvec, pose.tvec,
                            scene[2].camera_matrix, scene[2].distortion_coeffs)[1][:, :6]
    expected = .75 ** 2 * np.linalg.inv(jac.T @ jac)[3:, 3:] + .0015 ** 2 * np.eye(3)
    np.testing.assert_allclose(free, expected, rtol=1e-9, atol=1e-12)


@pytest.mark.parametrize('guided', [False, True])
def test_alignment_scale_and_offset(scene, guided):
    cov, _ = estimate(scene, guided=guided)
    a = cv2.Rodrigues(np.array([.4, -.2, .1]))[0]
    rotated, _ = estimate(scene, guided=guided, alignment=a)
    np.testing.assert_allclose(rotated, a.T @ cov @ a, atol=1e-14)
    scaled, _ = estimate(scene, guided=guided, scale=1.7)
    floor = .0015 ** 2 * np.eye(3)
    np.testing.assert_allclose(scaled - floor, 1.7 ** 2 * (cov - floor), atol=1e-14)
    offset = np.array([.03, -.05, .15])
    shifted, q = estimate(scene, guided=guided, head_offset_model_m=offset)
    assert shifted is not None, q
    assert not np.allclose(shifted, cov, atol=1e-10)
    # Joint scaling must scale both random contributions, excluding the floor.
    both, _ = estimate(scene, guided=guided, scale=1.7, head_offset_model_m=1.7 * offset)
    np.testing.assert_allclose(both - floor, 1.7 ** 2 * (shifted - floor), atol=1e-13)


def test_guided_angular_nuisance_includes_translation_and_offset(scene):
    pose = pose_for(scene, rvec=(0, 0, 0))
    cov, q = estimate(scene, pose, guided=True, head_offset_model_m=(0, 0, .2))
    assert cov is not None, q
    jac = cv2.projectPoints(scene[0], pose.rvec, pose.tvec,
                            scene[2].camera_matrix, scene[2].distortion_coeffs)[1]
    jr, jt = jac[:, :3], jac[:, 3:6]
    offset_derivative = np.array([[0, .2, 0], [-.2, 0, 0], [0, 0, 0]])
    response = -np.linalg.pinv(jt) @ jr + offset_derivative
    expected = (.75 ** 2 * np.linalg.inv(jt.T @ jt)
                + np.deg2rad(1.) ** 2 * response @ response.T + .0015 ** 2 * np.eye(3))
    np.testing.assert_allclose(cov, expected, rtol=1e-8, atol=1e-12)


@pytest.mark.parametrize('guided', [False, True])
def test_degenerate_and_small_span(scene, guided):
    points, labels, intrinsics = scene
    line = points.copy()
    line[:, 1:] = 0
    line[:, 0] = np.linspace(-.1, .1, 5)
    bad_scene = line, labels, intrinsics
    cov, q = estimate(bad_scene, guided=guided)
    assert cov is None and q['reason'] == 'degenerate_geometry'
    cov, q = estimate(scene, pose_for(scene, z=20), guided=guided)
    assert cov is None and q['reason'] == 'small_span'


def test_rank_relative_condition_and_worst_std_gates(scene):
    points, labels, intrinsics = scene
    planar = points.copy()
    planar[:, 2] = 0
    planar[:, 1] *= 1e-4
    bad = planar, labels, intrinsics
    cov, q = estimate(bad, pose_for(bad, rvec=(0, 0, 0)))
    assert cov is None and q['reason'] == 'rank_deficient'
    assert q['rank'] < 6 and q['cond'] > 1e6
    cov, q = estimate(scene, scale=100)
    assert cov is None and q['reason'] == 'excessive_uncertainty'
    assert q['worst_std_m'] > .08 and not q['reliable']


def test_residual_dof_and_three_led_reinit_gate(scene):
    pose = pose_for(scene)
    pose.image_points += np.array([2., -1.])
    cov, q = estimate(scene, pose, guided=True)
    assert cov is not None, q
    assert q['dof'] == 7
    np.testing.assert_allclose(q['sigma_px'], np.sqrt(25 / 7))
    cov, q = estimate(scene, pose_for(scene, ids=[0, 1, 4]), guided=True)
    assert cov is not None and not q['reliable']


def test_subset_order_and_front_without_labels(scene):
    cov, q = estimate(scene, pose_for(scene, ids=[4, 2, 0, 3, 1]))
    expected, _ = estimate(scene)
    np.testing.assert_allclose(cov, expected, atol=1e-14)
    assert q['visible_labels'] == ['centre', 'bl', 'tl', 'br', 'tr']
    pose = pose_for(scene)
    pose.led_labels = None
    cov, _ = estimate(scene, pose)
    np.testing.assert_allclose(cov, expected, atol=1e-14)


@pytest.mark.parametrize('mutation', ['invalid', 'nan', 'labels', 'shape', 'behind'])
def test_invalid_pose_inputs(scene, mutation):
    pose = pose_for(scene)
    if mutation == 'invalid':
        pose.valid = False
    elif mutation == 'nan':
        pose.rvec[0] = np.nan
    elif mutation == 'labels':
        pose.led_labels[0] = 'unknown'
    elif mutation == 'shape':
        pose.image_points = np.zeros((3, 3))
    else:
        pose.tvec[2] = -1
    cov, q = estimate(scene, pose)
    assert cov is None and not q['reliable']
    json.dumps(q, allow_nan=False)


@pytest.mark.parametrize('kwargs', [dict(scale=0), dict(scale=np.inf),
    dict(alignment=np.zeros((3, 3))), dict(head_offset_model_m=(np.nan, 0, 0))])
def test_invalid_transform(scene, kwargs):
    cov, q = estimate(scene, **kwargs)
    assert cov is None and q['reason'] == 'invalid_input'


def test_invalid_intrinsics_and_no_mutation(scene):
    before = copy.deepcopy(scene)
    estimate(scene, head_offset_model_m=(.01, .02, .03), guided=True)
    np.testing.assert_array_equal(scene[0], before[0])
    np.testing.assert_array_equal(scene[2].camera_matrix, before[2].camera_matrix)
    np.testing.assert_array_equal(scene[2].distortion_coeffs, before[2].distortion_coeffs)
    scene[2].calibrated = False
    cov, q = estimate(scene)
    assert cov is None and q['reason'] == 'invalid_input'


@pytest.mark.parametrize('guided', [False, True])
def test_offset_covariance_independent_finite_difference(scene, guided):
    pose = pose_for(scene, rvec=(.35, -.25, .1))
    offset = np.array([.04, -.03, .15])
    scale = 1.3
    cov, q = estimate(scene, pose, guided=guided, scale=scale,
                      head_offset_model_m=offset)
    assert cov is not None, q
    jac = cv2.projectPoints(scene[0], pose.rvec, pose.tvec,
                            scene[2].camera_matrix, scene[2].distortion_coeffs)[1]
    step = 1e-5
    rotation = cv2.Rodrigues(pose.rvec)[0]
    if guided:
        # Derive nuisance image/offset Jacobians directly from rotated points,
        # independent of the implementation's Rodrigues coordinate conversion.
        image_derivative, offset_derivative = [], []
        for axis in np.eye(3):
            rp = cv2.Rodrigues(step * axis)[0] @ rotation
            rm = cv2.Rodrigues(-step * axis)[0] @ rotation
            ip = cv2.projectPoints((rp @ scene[0].T).T, np.zeros(3), pose.tvec,
                                  scene[2].camera_matrix, scene[2].distortion_coeffs)[0]
            im = cv2.projectPoints((rm @ scene[0].T).T, np.zeros(3), pose.tvec,
                                  scene[2].camera_matrix, scene[2].distortion_coeffs)[0]
            image_derivative.append(((ip - im) / (2 * step)).reshape(-1))
            offset_derivative.append((rp @ offset - rm @ offset) / (2 * step))
        jt = jac[:, 3:6]
        response = (-scale * np.linalg.pinv(jt) @ np.column_stack(image_derivative)
                    + np.column_stack(offset_derivative))
        expected = (scale ** 2 * .75 ** 2 * np.linalg.inv(jt.T @ jt)
                    + np.deg2rad(1.) ** 2 * response @ response.T)
    else:
        derivative = np.column_stack([
            (cv2.Rodrigues(pose.rvec + step * axis)[0] @ offset
             - cv2.Rodrigues(pose.rvec - step * axis)[0] @ offset) / (2 * step)
            for axis in np.eye(3)])
        head_j = np.column_stack((derivative, scale * np.eye(3)))
        j = jac[:, :6]
        expected = .75 ** 2 * head_j @ np.linalg.inv(j.T @ j) @ head_j.T
    expected += .0015 ** 2 * np.eye(3)
    np.testing.assert_allclose(cov, expected, rtol=1e-7, atol=1e-12)


@pytest.mark.parametrize('unavailable', [True, False])
def test_opencv_errors_never_return_fallback(scene, monkeypatch, unavailable):
    def fail():
        if unavailable:
            raise OpenCvUnavailable('test')
        raise cv2.error('test')
    monkeypatch.setattr(pose_uncertainty, 'import_cv2', fail)
    cov, q = estimate(scene)
    assert cov is None and not q['reliable']
    assert q['reason'] == ('opencv_unavailable' if unavailable else 'projection_error')
    json.dumps(q, allow_nan=False)
