import unittest
from tools.psvr_camera.pseye_backend import PseyeLibrary
from tools.psvr_camera.camera_backend import CameraError


class FakeControls:
    def __init__(self):
        self.calls = []
        self.values = {}

    def psvr_pseye_set_parameter(self,cam,parameter,value):
        self.calls.append((parameter,value))
        self.values[parameter] = value
        return 0

    def psvr_pseye_get_parameter(self,cam,parameter):
        return self.values[parameter]


class ControlTests(unittest.TestCase):
    def test_manual_mode_order_ranges_and_readback(self):
        fake = FakeControls()
        lib = PseyeLibrary(fake)
        actual = lib.manual_controls(1,30,0)
        self.assertEqual(fake.calls,[(0,0),(2,0),(1,0),(3,30)])
        self.assertEqual(actual,{"auto_gain":0,"auto_white_balance":0,"gain":0,"exposure":30})
        with self.assertRaises(CameraError):
            lib.manual_controls(1,256,0)
        self.assertEqual(len(fake.calls),4)

    def test_unsupported_and_inconsistent_readback_are_errors(self):
        with self.assertRaises(CameraError):
            PseyeLibrary(object()).manual_controls(1,30,0)
        fake = FakeControls()
        fake.psvr_pseye_get_parameter = lambda cam,p: -1
        with self.assertRaises(CameraError):
            PseyeLibrary(fake).manual_controls(1,30,0)
