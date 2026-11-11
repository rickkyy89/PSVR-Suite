# SPDX-License-Identifier: MIT
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('summary',Path(__file__).resolve().parents[1]/'tools/psvr_native_analysis/summarize_transition.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def row():
    return dict(elapsed_s=1,phase='observe',nvidia=[],windows=[],direct_enumeration=[],runtime_summary=None)

class TransitionSummaryTests(unittest.TestCase):
    def test_probe_failure_is_unknown_fullscreen_not_false(self):
        data=row();data['runtime_probe_exit']=3
        result=module.summarize([data])['samples'][0]
        self.assertEqual(result['runtime_observation'],'probe_failed')
        self.assertIsNone(result['fullscreen'])
    def test_failed_enumeration_count_is_not_a_candidate(self):
        data=row();data['direct_enumeration']=[dict(vendor=0xd94d,mode=0,status=-3,count=1024)]
        self.assertIsNone(module.summarize([data])['samples'][0]['direct_candidate_observed'])
    def test_candidate_and_fullscreen_are_independent(self):
        data=row();data['direct_enumeration']=[dict(vendor=0xd94d,mode=1,status=0,count=1)]
        data['runtime_summary']=dict(valid_count=3,sample_count=3,compositor=dict(fullscreen=False))
        result=module.summarize([data])['samples'][0]
        self.assertTrue(result['direct_candidate_observed'])
        self.assertFalse(result['fullscreen'])
        self.assertEqual(result['valid'],3)
    def test_os_visible_does_not_imply_desktop_active(self):
        data=row();data['nvidia']=[dict(edid_vendor=0xd94d,edid_product=0x6a04,os_visible=True)]
        result=module.summarize([data])['samples'][0]
        self.assertEqual(result['sony_os_visible'],[True])
        self.assertFalse(result['sony_windows_active'])

class TransitionSummaryRobustnessTests(unittest.TestCase):
    def test_malformed_rows_are_skipped_and_counted(self):
        data=row();data['direct_enumeration']=[dict(vendor=0xd94d,mode=1,status=0,count=1)]
        data['runtime_summary']=dict(valid_count=3,sample_count=3,compositor=dict(fullscreen=False))
        records=[data,{'phase':'observe'},'not-a-dict',{'elapsed_s':'soon','phase':'x'},None]
        report=module.summarize(records)
        self.assertEqual(report['records'],5)
        self.assertEqual(report['skipped'],4)
        self.assertEqual(len(report['samples']),1)
        sample=report['samples'][0]
        self.assertTrue(sample['direct_candidate_observed'])
        self.assertFalse(sample['fullscreen'])
        self.assertEqual(sample['valid'],3)
    def test_clean_input_totals_are_unchanged(self):
        data=row();data['direct_enumeration']=[dict(vendor=0xd94d,mode=1,status=0,count=1)]
        data['runtime_summary']=dict(valid_count=3,sample_count=3,compositor=dict(fullscreen=False))
        data['new_log_evidence']=[dict(line='evidence')]
        report=module.summarize([data])
        self.assertEqual(report['records'],1)
        self.assertEqual(report['skipped'],0)
        self.assertEqual(report['samples'],[{'elapsed_s':1,'phase':'observe','sony_windows_active':False,
            'sony_os_visible':[],'direct_lists':[[0xd94d,1,0,1]],'direct_candidate_observed':True,
            'runtime_probe_exit':None,'runtime_observation':'summary','fullscreen':False,
            'valid':3,'samples':3,'reason':None}])
        self.assertEqual(report['log_evidence'],[dict(line='evidence')])
    def test_large_file_is_streamed_line_by_line(self):
        data=row();data['direct_enumeration']=[dict(vendor=0xd94d,mode=1,status=0,count=1)]
        line=json.dumps(data)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.jsonl'
            with open(path,'w',encoding='utf-8') as handle:
                for _ in range(2000):
                    handle.write(line+'\n')
                handle.write('not json\n')
                handle.write('\n')
                handle.write(json.dumps({'phase':'observe'})+'\n')
            with mock.patch.object(Path,'read_text',side_effect=AssertionError('must stream, not read_text')):
                report=module.summarize_file(path)
        self.assertEqual(report['records'],2002)
        self.assertEqual(report['skipped'],2)
        self.assertEqual(len(report['samples']),2000)
    def test_cli_reports_skipped_without_crash(self):
        script=Path(__file__).resolve().parents[1]/'tools/psvr_native_analysis/summarize_transition.py'
        data=row()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.jsonl'
            with open(path,'w',encoding='utf-8') as handle:
                handle.write(json.dumps(data)+'\n')
                handle.write('broken {\n')
                handle.write(json.dumps({'phase':'observe'})+'\n')
            proc=subprocess.run([sys.executable,str(script),str(path)],capture_output=True,text=True,timeout=60)
        self.assertEqual(proc.returncode,0,proc.stderr)
        report=json.loads(proc.stdout)
        self.assertEqual(report['records'],3)
        self.assertEqual(report['skipped'],2)
        self.assertEqual(len(report['samples']),1)

if __name__=='__main__': unittest.main()
