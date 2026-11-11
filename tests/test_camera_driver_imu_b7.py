"""Regressione B7-04: at() non interpola mai tra epoche/sessioni diverse.

Solo storia sintetica iniettata in DriverImuSource.samples (nessun socket
reale oltre l'endpoint effimero, nessun hardware). _accept svuota la storia
a ogni cambio sessione/epoca, quindi la storia mista e' costruita a mano
per verificare la difesa in profondita' nel gate di interpolazione.
"""
import unittest

import numpy as np

from tools.psvr_camera.driver_imu_source import DriverImuSource


def imu(**updates):
    row = dict(protocol="psvr.imu", version=1, frame="psvr_imu_unrecentered",
               clock="driver_steady_seconds", session_id="activation-a", seq=1,
               recenter_epoch=1, sample_time_s=10., sent_time_s=10.,
               connected=True, valid=True, state="tracking",
               quaternion_sensor_wxyz=[1, 0, 0, 0], world_rotation_wxyz=[1, 0, 0, 0],
               corrected_gyro_rad_s=[0, 0, 0])
    row.update(updates)
    return row


def inject(source, past_updates, future_updates):
    with source.changed:
        source.samples.extend([imu(**past_updates), imu(**future_updates)])


class DriverImuB704Tests(unittest.TestCase):
    def test_no_interpolation_across_recenter_epoch(self):
        source = DriverImuSource(port=0)
        try:
            inject(source,
                   dict(monotonic_s=1., quaternion_sensor_wxyz=[1, 0, 0, 0]),
                   dict(monotonic_s=1.04, seq=2, sample_time_s=10.04,
                        sent_time_s=10.04, recenter_epoch=2,
                        quaternion_sensor_wxyz=[np.cos(.2), 0, np.sin(.2), 0]))
            row = source.at(1.02, wait_s=0)
            self.assertNotIn("interpolated", row)
            self.assertEqual(row["monotonic_s"], 1.)
            self.assertTrue(row["valid"])
            self.assertEqual(row["quaternion_sensor_wxyz"], [1, 0, 0, 0])
        finally:
            source.close()

    def test_no_interpolation_across_session(self):
        source = DriverImuSource(port=0)
        try:
            inject(source,
                   dict(monotonic_s=1., quaternion_sensor_wxyz=[1, 0, 0, 0]),
                   dict(monotonic_s=1.04, seq=2, sample_time_s=10.04,
                        sent_time_s=10.04, session_id="activation-b",
                        quaternion_sensor_wxyz=[np.cos(.2), 0, np.sin(.2), 0]))
            row = source.at(1.02, wait_s=0)
            self.assertFalse(row.get("interpolated", False))
            self.assertEqual(row["monotonic_s"], 1.)
            self.assertTrue(row["valid"])
            self.assertEqual(row["quaternion_sensor_wxyz"], [1, 0, 0, 0])
        finally:
            source.close()

    def test_same_epoch_still_interpolated(self):
        source = DriverImuSource(port=0)
        try:
            inject(source,
                   dict(monotonic_s=1., quaternion_sensor_wxyz=[1, 0, 0, 0]),
                   dict(monotonic_s=1.04, seq=2, sample_time_s=10.04,
                        sent_time_s=10.04,
                        quaternion_sensor_wxyz=[np.cos(.2), 0, np.sin(.2), 0]))
            row = source.at(1.02, wait_s=0)
            self.assertTrue(row.get("interpolated"))
            self.assertEqual(row["monotonic_s"], 1.02)
            np.testing.assert_allclose(
                row["quaternion_sensor_wxyz"], [np.cos(.1), 0, np.sin(.1), 0])
        finally:
            source.close()


if __name__ == "__main__":
    unittest.main()
