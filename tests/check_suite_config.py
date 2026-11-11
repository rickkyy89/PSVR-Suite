# SPDX-License-Identifier: MIT
"""CLI: python tests/check_suite_config.py <suite_config_fixture.exe>.

Exercise Python export -> real native parser, without device access.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_suite import settings as S


def check(executable):
    baseline = (S.ROOT / S.IMU_BASELINE_SOURCE).read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        for index, (calibration, stale, gap) in enumerate(
                ((500, 20, 1000), (10000, 1000, 20000), (8000, 100, 5000))):
            prefs = dict(S.default_preferences(), imu_calibration_ms=calibration,
                         imu_stale_ms=stale, imu_max_gap_us=gap)
            dest = Path(tmp) / ("derived-%d.json" % index)
            S.derive_imu_config(prefs, dest)
            payload = dest.read_text(encoding="utf-8")
            run = subprocess.run([str(executable)], input=payload, text=True,
                                 capture_output=True, timeout=10)
            assert run.returncode == 0, run.stderr
            assert json.loads(run.stdout) == {
                "calibration_ms": calibration, "stale_timeout_ms": stale,
                "max_sample_gap_us": gap}
        for gap in (20001, 30000, 50000):
            prefs = dict(S.default_preferences(), imu_max_gap_us=gap)
            assert S.driver_keys_for(prefs)["imuMaxSampleGapUs"] == gap
            dest = Path(tmp) / ("driver-only-%d.json" % gap)
            try:
                S.derive_imu_config(prefs, dest)
            except ValueError as error:
                assert "20000" in str(error), error
            else:
                raise AssertionError("diagnostic-incompatible derivative created")
            assert not dest.exists()
            # Independently establish the destination's real contract.
            invalid = json.loads(payload)
            invalid["tracking"]["max_sample_gap_us"] = gap
            run = subprocess.run([str(executable)], input=json.dumps(invalid),
                                 text=True, capture_output=True, timeout=10)
            assert run.returncode == 1, "native parser unexpectedly accepted gap"
    assert (S.ROOT / S.IMU_BASELINE_SOURCE).read_bytes() == baseline
    print("PASS suite config: 3 Python exports accepted by native parser; "
          "3 driver-only gaps rejected before creation and by native parser")


if __name__ == "__main__":
    check(Path(sys.argv[1]).resolve())
