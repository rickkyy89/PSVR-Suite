"""Regressione B10-02: readiness richiede pose reale valida (valid_count>0)."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tools.psvr_optics import visual_trial

_MISSING = object()


def _summary_line(valid_count=_MISSING, fullscreen=True):
    summary = {'event': 'summary', 'compositor': {'fullscreen': fullscreen}}
    if valid_count is not _MISSING:
        summary['valid_count'] = valid_count
    return json.dumps(summary)


def _run_mock(returncode, stdout_text):
    run = MagicMock()
    run.returncode = returncode
    run.stdout = stdout_text
    return run


def _run_main(run_mock, popen_mock=None):
    with TemporaryDirectory() as tmp:
        probe = Path(tmp) / 'probe.exe'
        probe.write_text('x', encoding='utf-8')
        grid = Path(tmp) / 'grid.png'
        grid.write_bytes(b'x')
        out = Path(tmp) / 'out.jsonl'
        argv = ['--probe', str(probe), '--grid', str(grid), '--out', str(out),
                '--wait-seconds', '10', '--duration-ms', '100']
        if popen_mock is None:
            # not-ready: una iterazione poi deadline scaduta
            perf = [100.0, 101.0, 1000.0]
            with patch.object(visual_trial.subprocess, 'run', return_value=run_mock), \
                    patch.object(visual_trial.time, 'perf_counter', side_effect=perf), \
                    patch.object(visual_trial.time, 'sleep', lambda s: None):
                ret = visual_trial.main(argv)
        else:
            # ready: break alla prima iterazione, poi Popen grid
            perf = [100.0, 101.0]
            with patch.object(visual_trial.subprocess, 'run', return_value=run_mock), \
                    patch.object(visual_trial.subprocess, 'Popen', return_value=popen_mock), \
                    patch.object(visual_trial.time, 'perf_counter', side_effect=perf), \
                    patch.object(visual_trial.time, 'sleep', lambda s: None):
                ret = visual_trial.main(argv)
        parsed = []
        for raw in out.read_text(encoding='utf-8').splitlines():
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return ret, parsed


def _grid_process(returncode=0):
    proc = MagicMock()
    stdout = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    proc.communicate.return_value = ('grid-line\n', '')
    proc.wait.return_value = returncode
    return proc


class VisualTrialReadinessTests(unittest.TestCase):
    def test_fullscreen_without_valid_count_is_not_ready(self):
        ret, lines = _run_main(_run_mock(0, _summary_line() + '\n'))
        self.assertEqual(ret, 4)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness']
        self.assertEqual(len(readiness), 1)
        self.assertFalse(readiness[0]['ready'])
        self.assertIsNone(readiness[0]['valid_count'])
        self.assertIn('grid_not_shown', [e.get('event') for e in lines])

    def test_fullscreen_with_zero_valid_is_not_ready(self):
        ret, lines = _run_main(_run_mock(0, _summary_line(valid_count=0) + '\n'))
        self.assertEqual(ret, 4)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['valid_count'], 0)

    def test_fullscreen_with_none_valid_is_not_ready(self):
        ret, lines = _run_main(_run_mock(0, _summary_line(valid_count=None) + '\n'))
        self.assertEqual(ret, 4)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
        self.assertFalse(readiness['ready'])
        self.assertIsNone(readiness['valid_count'])

    def test_fullscreen_with_non_numeric_valid_is_not_ready(self):
        for bad in ('3', True, False, [1], {'n': 1}):
            with self.subTest(valid_count=bad):
                ret, lines = _run_main(_run_mock(0, _summary_line(valid_count=bad) + '\n'))
                self.assertEqual(ret, 4)
                readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
                self.assertFalse(readiness['ready'])
                self.assertEqual(readiness['valid_count'], bad)

    def test_missing_summary_is_not_ready(self):
        ret, lines = _run_main(_run_mock(0, json.dumps({'event': 'other'}) + '\n'))
        self.assertEqual(ret, 4)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
        self.assertFalse(readiness['ready'])
        self.assertIsNone(readiness['valid_count'])

    def test_non_dict_summary_is_not_ready(self):
        stdout = json.dumps([1, 2]) + '\n' + json.dumps('summary') + '\n'
        ret, lines = _run_main(_run_mock(0, stdout))
        self.assertEqual(ret, 4)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
        self.assertFalse(readiness['ready'])
        self.assertIsNone(readiness['valid_count'])

    def test_fullscreen_with_valid_count_is_ready(self):
        ret, lines = _run_main(
            _run_mock(0, _summary_line(valid_count=2) + '\n'),
            popen_mock=_grid_process(0))
        self.assertEqual(ret, 0)
        readiness = [e for e in lines if e.get('event') == 'grid_readiness'][0]
        self.assertTrue(readiness['ready'])
        self.assertEqual(readiness['valid_count'], 2)
        self.assertNotIn('grid_not_shown', [e.get('event') for e in lines])

    def test_previous_cases_still_not_ready(self):
        # rc != 0 con fullscreen + validi resta not-ready
        ret, lines = _run_main(_run_mock(1, _summary_line(valid_count=5) + '\n'))
        self.assertEqual(ret, 4)
        self.assertFalse([e for e in lines if e.get('event') == 'grid_readiness'][0]['ready'])
        # fullscreen False con validi resta not-ready
        ret, lines = _run_main(_run_mock(0, _summary_line(valid_count=5, fullscreen=False) + '\n'))
        self.assertEqual(ret, 4)
        self.assertFalse([e for e in lines if e.get('event') == 'grid_readiness'][0]['ready'])


class VisualTrialGridTimeoutTests(unittest.TestCase):
    """Regressione B10-05: grid che non termina -> kill + evento + close senza hang."""

    def test_grid_timeout_kills_closes_and_logs_event(self):
        import subprocess as _sp

        run_mock = _run_mock(0, _summary_line(valid_count=2) + '\n')
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.returncode = -9
        # prima communicate scade, seconda drena output parziale dopo kill
        proc.communicate.side_effect = [
            _sp.TimeoutExpired(cmd='probe', timeout=10.1),
            ('partial-line\n', ''),
        ]
        with TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.exe'
            probe.write_text('x', encoding='utf-8')
            grid = Path(tmp) / 'grid.png'
            grid.write_bytes(b'x')
            out = Path(tmp) / 'out.jsonl'
            argv = ['--probe', str(probe), '--grid', str(grid), '--out', str(out),
                    '--wait-seconds', '10', '--duration-ms', '100']
            perf = [100.0, 101.0]
            with patch.object(visual_trial.subprocess, 'run', return_value=run_mock), \
                    patch.object(visual_trial.subprocess, 'Popen', return_value=proc), \
                    patch.object(visual_trial.time, 'perf_counter', side_effect=perf), \
                    patch.object(visual_trial.time, 'sleep', lambda s: None):
                ret = visual_trial.main(argv)
            # timeout richiesto = duration_s + margine (100ms -> 0.1s + 10s)
            first_timeout = proc.communicate.call_args_list[0].kwargs.get(
                'timeout', proc.communicate.call_args_list[0].args[0]
                if proc.communicate.call_args_list[0].args else None)
            self.assertAlmostEqual(first_timeout, 0.1 + 10.0)
            proc.kill.assert_called_once_with()
            proc.stdout.close.assert_called_once_with()
            self.assertEqual(ret, -9)
            parsed = []
            for r in out.read_text(encoding='utf-8').splitlines():
                try:
                    parsed.append(json.loads(r))
                except json.JSONDecodeError:
                    continue
            kinds = [e.get('event') for e in parsed]
            self.assertIn('grid_timeout', kinds)
            # output parziale preservato prima dell'evento di timeout
            raw = out.read_text(encoding='utf-8')
            self.assertIn('partial-line', raw)
            self.assertLess(raw.index('partial-line'), raw.index('grid_timeout'))


if __name__ == '__main__':
    unittest.main()
