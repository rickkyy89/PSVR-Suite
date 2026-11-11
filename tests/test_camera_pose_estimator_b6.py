"""Regressione B6-03: reseed riuscito senza reason stantio né doppio conteggio."""
import numpy as np

from tools.psvr_camera.pose_estimator import PoseEstimator

R = np.eye(3) * 0.005**2


def test_b6_03_successful_reseed_has_no_stale_reason_or_double_count():
    e = PoseEstimator()
    for i in range(4):
        e.update((0.0, 0.0, 1.0), R, i * 0.01)
    assert e.diagnostics()["accepted"] == 1
    assert e.diagnostics()["rejected"] == 0
    # Primo rifiuto implied_speed: avvia anche il cluster di riacquisizione.
    out = e.update([1.0, 0, 1.0], R, 0.04)
    assert not out["valid"] and out["rejection_reason"] == "implied_speed"
    assert out["accepted"] == 1 and out["rejected"] == 1
    # Altri tre campioni coerenti completano le 4 conferme -> reseed.
    for i in range(2):
        out = e.update([1.0, 0, 1.0], R, 0.05 + i * 0.01)
        assert not out["valid"] and out["rejection_reason"] == "implied_speed"
    assert out["accepted"] == 1 and out["rejected"] == 3
    out = e.update([1.0, 0, 1.0], R, 0.07)
    assert out["valid"] and out["tracking_state"] == "tracking"
    assert out["rejection_reason"] is None
    assert out["accepted"] == 2 and out["rejected"] == 3
    np.testing.assert_allclose(out["position_m"], [1, 0, 1])
    # Il frame successivo resta tracking senza toccare il conteggio rejected.
    out = e.update([1.0, 0, 1.0], R, 0.08)
    assert out["valid"] and out["tracking_state"] == "tracking"
    assert out["accepted"] == 3 and out["rejected"] == 3


def test_b6_03_rejection_without_reseed_keeps_reason_and_rejected():
    e = PoseEstimator()
    for i in range(4):
        e.update((0.0, 0.0, 1.0), R, i * 0.01)
    out = e.update([1.0, 0, 1.0], R, 0.04)
    assert not out["valid"] and out["tracking_state"] == "rejected"
    assert out["rejection_reason"] == "implied_speed"
    assert out["accepted"] == 1 and out["rejected"] == 1
    # Cluster incoerente: nessun reseed, ogni rifiuto conserva reason e conta.
    out = e.update([-1.0, 0, 1.0], R, 0.05)
    assert not out["valid"] and out["rejection_reason"] == "implied_speed"
    assert out["accepted"] == 1 and out["rejected"] == 2
