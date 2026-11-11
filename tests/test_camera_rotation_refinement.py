import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from tools.psvr_camera.calibration import Intrinsics,save_intrinsics
from tools.psvr_camera.imu_led_pose import load_rotation_rig
from tools.psvr_camera.refine_rotation_rig import refine


class SideRefinementTests(unittest.TestCase):
    def test_side_point_is_refined_from_front_anchors_not_cached_translation(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable")
        root=Path(__file__).resolve().parents[1]
        rig_path=root/"configurations/psvr_nine_leds_rotation_trial.json"
        rig,reference,_=load_rotation_rig(rig_path)
        truth=reference.copy()
        truth[6]=[.102,-.004,.089]
        intrinsics=Intrinsics(np.array([[500.,0,320],[0,500.,240],[0,0,1]]),np.zeros(5),(640,480),True)
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            calibration=directory/"camera.json"
            save_intrinsics(calibration,intrinsics)
            log=directory/"trial.jsonl"
            with log.open("w",encoding="utf-8") as stream:
                for i in range(24):
                    r=np.array([.04*np.sin(i),np.deg2rad(20+i*2.5),.02*np.cos(i)])
                    t=np.array([.05*np.sin(i),.02*np.cos(i),.7+i*.01])
                    ids=[0,1,2,3,4,6]
                    image=cv2.projectPoints(truth[ids],r,t,intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(-1,2)
                    stream.write(json.dumps({"reason":"imu_guided_pattern","valid":True,
                        "rvec":r.tolist(),"position_camera_m":(t+.3).tolist(),
                        "led_labels":[rig["labels"][j] for j in ids],"image_points":image.tolist()})+"\n")
            output=directory/"refined.json"
            report=refine(log,calibration,rig_path,output)
            self.assertTrue(report["saved"])
            self.assertTrue(report["report"]["side_right"]["accepted"])
            updated=json.loads(output.read_text(encoding="utf-8"))
            points=np.asarray(updated["object_points_m"])
            np.testing.assert_allclose(points[6],truth[6],atol=1e-7)
            np.testing.assert_equal(points[:5],reference[:5])
            np.testing.assert_equal(points[5],reference[5])
