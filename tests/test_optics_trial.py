import copy
import json
from pathlib import Path
import subprocess
import unittest

from tools.psvr_optics.trial import update,preserved


# NOTA B10-09: `peripheral` (configurations/optics_radial_peripheral.json,
# a=0.2/b=0.4) e' validato dal validator nativo (sdk_usable) ma resta fuori
# copertura trial: OPTICS_RADIAL_TRIAL_20261014.md candida solo
# mild/medium/stronger; peripheral e' counter-warp diagnostico non accettato
# visivamente (cfr README/STATUS), quindi i test sotto coprono solo i tre candidati.


class OpticalTrialTests(unittest.TestCase):
    def test_only_optical_keys_change_and_input_is_not_mutated(self):
        settings={'driver_psvrpc':{'distortionMode':'identity','distortionProfilePath':'',
            'positionEnabled':True,'positionGraceMs':80,'projectionLeftLeft':-1.,
            'displayRefreshNumerator':90},'power':{'pauseCompositorOnStandby':False},
            'another_app':{'user_setting':[1,2,3]}}
        before=copy.deepcopy(settings)
        after=update(settings,{'distortionMode':'profile','distortionProfilePath':'test.json'})
        self.assertEqual(settings,before)
        self.assertEqual(preserved(after),preserved(before))
        self.assertEqual(update(after,{k:settings['driver_psvrpc'][k] for k in
                                      ('distortionMode','distortionProfilePath')}),before)

    def test_profiles_keep_central_scale_and_use_eye_aspect(self):
        root=Path(__file__).resolve().parents[1]
        for tag,b in (('mild',.2),('medium',.4),('stronger',.6)):
            path=root/'configurations'/('optics_radial_'+tag+'_20261014.json')
            p=json.loads(path.read_text())
            self.assertEqual(p['mapping'],'display-to-render')
            for eye in p['eyes'].values():
                self.assertEqual(eye['center'],[.5,.5])
                self.assertAlmostEqual(eye['axis_scale'][0],960/1080)
                self.assertEqual(eye['polynomial'],{'a':0.,'b':b,'c':0.,'d':1.})
                self.assertEqual(eye['rgb_scale'],{'red':1.,'green':1.,'blue':1.})
                r2=(.5*960/1080)**2+.5**2
                self.assertGreaterEqual(1-3*b*r2,.1)
                self.assertGreater(1+b*r2,1)

    def test_existing_native_validator_accepts_all_candidate_profiles(self):
        root=Path(__file__).resolve().parents[1]
        validator=root/'out/optics/psvr_optics.exe'
        if not validator.is_file():
            self.skipTest('native validator is unavailable')
        for tag in ('mild','medium','stronger'):
            run=subprocess.run([str(validator),'--validate',str(root/'configurations'/
                ('optics_radial_'+tag+'_20261014.json')),'--json'],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            report=json.loads(run.stdout)
            self.assertTrue(report['math_valid']);self.assertTrue(report['sdk_usable'])
            self.assertFalse(report['optical_acceptance_verified'])


if __name__=='__main__':
    unittest.main()
