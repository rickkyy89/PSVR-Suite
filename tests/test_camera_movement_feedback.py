import json
from pathlib import Path
import tempfile
import unittest

from tools.psvr_camera.analyze_movement_feedback import summarize


def row(t,mode,raw=True,published=True,point=0,leds=5):
    return dict(event='pose',monotonic_s=t,valid=raw,led_labels=list(range(leds)),
        imu={'valid':True,'session_id':'test','recenter_epoch':1},
        estimator_input={'alignment':[[1,0,0],[0,1,0],[0,0,1]]},
        position_filter={'type':mode,'tracking_state':'tracking' if published else 'acquiring',
                         'quality':{'reason':'ok'}},
        position_output={'valid':published,'position_m':[point,0,0] if published else None})


class MovementFeedbackTests(unittest.TestCase):
    def test_filter_rejections_are_separate_from_optical_loss(self):
        data=[row(1,'kalman'),row(1.1,'kalman',published=False,leds=2),
              row(1.2,'kalman',raw=False,published=False),row(1.3,'kalman'),
              row(1.4,'one_euro'),row(1.5,'one_euro')]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'live.jsonl';p.write_text('\n'.join(json.dumps(r) for r in data)+'\n')
            r=summarize(p)['eligible_motion_groups']
        self.assertEqual(r['kalman']['optical_valid_but_not_published'],1)
        self.assertAlmostEqual(r['kalman']['optical_valid_withheld_fraction'],1/3)
        self.assertEqual(r['one_euro']['optical_valid_but_not_published'],0)

    def test_partial_snapshot_and_inactive_filters_do_not_create_steps_or_gaps(self):
        data=[row(1,'kalman'),row(1.1,'kalman',published=False),
              row(2,'one_euro',point=2),row(3,'kalman',point=3)]
        text='\n'.join(json.dumps(r) for r in data)+'\n'
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'live.jsonl';p.write_bytes(text.encode()+b'{"event":')
            report=summarize(p,len(text.encode()))
            r=report['eligible_motion_groups']['kalman']
        self.assertEqual(r['step_max_mm'],0)
        self.assertEqual(r['completed_invalid_gaps'],0)
        self.assertFalse(report['hardware_accepted'])


if __name__=='__main__':
    unittest.main()
