import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tools.psvr_camera.analyze_static_trial import analyze, depth_spectrum, analyze_runtime
from tools.psvr_camera.driver_position import validate_driver_position_packet
from tools.psvr_camera.geometry import rodrigues
from tools.psvr_camera.static_tracking_trial import main


class StaticAnalysisTests(unittest.TestCase):
    def test_spectral_band_rms_and_linear_drift_removal(self):
        t=np.arange(0,20,.02)
        d=.1*t+.005*np.sin(2*np.pi*t)
        result=depth_spectrum(t,d)
        self.assertAlmostEqual(result['rms_0_3_to_3_hz_m'],.005/np.sqrt(2),delta=.00015)
        self.assertLess(result['rms_above_3_hz_m'],.0001)
        self.assertIsNone(depth_spectrum(t[:2],d[:2]))

    def test_static_replay_keeps_timestamps_axis_and_no_ground_truth(self):
        a=rodrigues([.3,.7,0])
        r=a.T@np.diag([.002**2,.002**2,.005**2])@a
        data=[{'event':'session','diagnostic_only':True,'position_config':{}}]
        for i in range(200):
            p=a.T@np.array([0.,0.,1.+.002*np.sin(i)])
            stamp=1+i/75
            data.append({'event':'pose','monotonic_s':stamp,'led_labels':['a','b','c','d','e'],
                'estimator_input':{'packet':{'session_id':'local-diagnostic:test','recenter_epoch':0,
                    'valid':True,'state':'tracking','position_m':p.tolist()},
                    'alignment':a.tolist(),'covariance':r.tolist(),'quality':{'reliable':True,'led_count':5},
                    'context_valid':True,'prediction_allowed':True}})
        before=copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'static.jsonl'
            path.write_text('\n'.join(json.dumps(row) for row in data),encoding='utf-8')
            variants={'euro':{'one_euro':{'min_cutoff_hz':2.5,'beta':8}},'kalman':{'kalman':{}}}
            first=analyze(path,variants,warmup_s=.2)
            self.assertEqual(first,analyze(path,variants,warmup_s=.2))
            self.assertIsNone(first['ground_truth_position'])
            self.assertFalse(first['hardware_accepted'])
            self.assertEqual(first['input_provenance'],{'exact_recorded_inputs':200})
            self.assertAlmostEqual(first['variants']['raw']['camera_depth_std_m'],
                                   np.std([1.+.002*np.sin(i) for i in range(15,200)]),places=12)
            self.assertGreater(first['common_valid_frames'],150)
            with self.assertRaises(ValueError):
                validate_driver_position_packet(data[1]['estimator_input']['packet'])
        self.assertEqual(data,before)

    def test_optical_cannot_publish_position_or_probe_runtime(self):
        for extra in ('--publish-position','--capture-runtime-poses'):
            with patch('tools.psvr_camera.static_tracking_trial.capture_trial') as capture:
                with self.assertRaises(SystemExit):
                    main(['--output-dir','unused','--mode','optical',extra])
                capture.assert_not_called()

    def test_bad_duration_cannot_open_hardware(self):
        for duration in ('0','nan','inf'):
            with patch('tools.psvr_camera.static_tracking_trial.capture_trial') as capture:
                with self.assertRaises(SystemExit):
                    main(['--output-dir','unused','--seconds',duration])
                capture.assert_not_called()

    def test_runtime_requires_real_live_phase_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'not_driver.jsonl'
            path.write_text(json.dumps({'event':'pose','monotonic_s':1}),encoding='utf-8')
            with self.assertRaises(ValueError):
                analyze_runtime(path)


if __name__=='__main__':
    unittest.main()
