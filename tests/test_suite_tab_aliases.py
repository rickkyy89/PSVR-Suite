"""Regressione B12-07: --tab accetta alias EN (UI bilingue, CLI solo IT).

Difetto: --tab rifiutava i nomi EN con exit 2, mentre la UI e' bilingue
(--tab Camera passava solo per coincidenza IT/EN).
Fix: normalize_tab_argv mappa gli alias EN->IT prima di argparse, con
choices e ordine invariati (main usa tab_names.index(args.tab)).

Stile headless: solo parser/normalizzazione, nessun Tk/display/HW.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import app as suite_app

EXPECTED_ORDER = ('Sessione', 'Tracking', 'Camera', 'Posizione', 'Ottica', 'Diagnostica')

EN_TO_IT = {
    'session': 'Sessione',
    'position': 'Posizione',
    'optics': 'Ottica',
    'diagnostics': 'Diagnostica',
}


def parse(argv):
    parser = suite_app.build_arg_parser()
    return parser.parse_args(suite_app.normalize_tab_argv(list(argv)))


class TabAliasTests(unittest.TestCase):
    def test_alias_en_risolvono_tab_it(self):
        # Forma '--tab VALORE', minuscolo.
        for alias, canon in EN_TO_IT.items():
            with self.subTest(alias=alias):
                self.assertEqual(parse(['--tab', alias]).tab, canon)

    def test_alias_en_forme_uguale_e_maiuscole(self):
        # Forma '--tab=VALORE' + varianti di maiuscole (case-insensitive).
        casi = [
            (['--tab=session'], 'Sessione'),
            (['--tab=Session'], 'Sessione'),
            (['--tab=SESSION'], 'Sessione'),
            (['--tab=POSITION'], 'Posizione'),
            (['--tab=Position'], 'Posizione'),
            (['--tab=Optics'], 'Ottica'),
            (['--tab=OPTICS'], 'Ottica'),
            (['--tab=Diagnostics'], 'Diagnostica'),
            (['--tab=DIAGNOSTICS'], 'Diagnostica'),
            (['--tab', 'SESSIONE'], 'Sessione'),
            (['--tab', 'tracking'], 'Tracking'),
            (['--tab', 'CAMERA'], 'Camera'),
            (['--tab', 'Position'], 'Posizione'),
        ]
        for argv, atteso in casi:
            with self.subTest(argv=argv):
                self.assertEqual(parse(argv).tab, atteso)

    def test_it_invariati(self):
        for nome in EXPECTED_ORDER:
            with self.subTest(tab=nome):
                self.assertEqual(parse(['--tab', nome]).tab, nome)
                self.assertEqual(parse(['--tab=' + nome]).tab, nome)

    def test_tracking_camera_invarianti(self):
        # Coincidono IT/EN: passavano anche pre-fix, devono restare validi.
        self.assertEqual(parse(['--tab', 'Tracking']).tab, 'Tracking')
        self.assertEqual(parse(['--tab', 'Camera']).tab, 'Camera')

    def test_ordine_choices_invariato(self):
        parser = suite_app.build_arg_parser()
        action = next(a for a in parser._actions if '--tab' in a.option_strings)
        self.assertEqual(tuple(action.choices), EXPECTED_ORDER)
        self.assertEqual(tuple(suite_app._TAB_CANONICAL), EXPECTED_ORDER)
        # Indici usati da tabs.select(tab_names.index(args.tab)).
        for alias, canon in EN_TO_IT.items():
            with self.subTest(alias=alias):
                args = parse(['--tab', alias])
                self.assertEqual(EXPECTED_ORDER.index(args.tab),
                                 EXPECTED_ORDER.index(canon))

    def test_valore_ignoto_rifiutato_e_altri_argv_intatti(self):
        with self.assertRaises(SystemExit) as ctx:
            parse(['--tab', 'Foo'])
        self.assertEqual(ctx.exception.code, 2)
        # normalize non tocca altri argomenti.
        argv = ['--attach', '--tab', 'optics', '--root', 'C:/x']
        norm = suite_app.normalize_tab_argv(argv)
        self.assertEqual(norm, ['--attach', '--tab', 'Ottica', '--root', 'C:/x'])
        self.assertEqual(argv[2], 'optics')  # input non mutato.
        # '--tab' senza valore resta invariato (errore argparse come prima).
        self.assertEqual(suite_app.normalize_tab_argv(['--tab']), ['--tab'])
        self.assertIsNone(suite_app.normalize_tab_argv(None))


if __name__ == '__main__':
    unittest.main()
