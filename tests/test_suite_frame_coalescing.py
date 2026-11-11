"""Regressione B12-05: coalescing frame nel drain display.

Difetto: ogni evento `frame` trasporta il PPM (~0,9 MB); il drain decodificava
ogni frame in coda (fino a 512 eventi, ~460 MB accodabili) sprecando CPU su
frame stantii. Fix: nel batch del drain si decodifica solo l'ultimo frame,
gli intermedi sono scartati senza decodifica; eventi non-frame sempre
elaborati in ordine. Formato PPM e producer invariati.

Stile headless test_suite_drain_reschedule.py: SuiteApp senza Tk (__new__),
FakeVar/FakeRoot, coda reale, Tk mockato (conteggio decode). Niente Tk/cv2
reali, display, hardware.
"""

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import app as suite_app


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class LogVar(FakeVar):
    """Registra ogni set con etichetta per verificare ordine e preservazione."""

    def __init__(self, log, tag, value=""):
        super().__init__(value)
        self._log = log
        self._tag = tag

    def set(self, value):
        self._log.append((self._tag, value))
        self._v = value


class FakeImage:
    def __init__(self, w=16, h=16):
        self._w = w
        self._h = h

    def width(self):
        return self._w

    def height(self):
        return self._h

    def subsample(self, x, y):
        return self


class FakeTk:
    """Tk mockato: PhotoImage conta le decodifiche senza decodificare davvero."""

    def __init__(self, log):
        self._log = log
        self.calls = []

    def PhotoImage(self, data=None, format=None):  # noqa: N802 - firma Tk
        self.calls.append(bytes(data))
        self._log.append(("decode", bytes(data)))
        return FakeImage()


class FakeRoot:
    def __init__(self):
        self.calls = []

    def after(self, delay, func):
        self.calls.append((delay, func))
        return "after-id-%d" % len(self.calls)


class FakePreviewWidget:
    def __init__(self):
        self.images = []

    def configure(self, image=None, text=None):
        self.images.append(image)


def make_ppm(fill, w=16, h=16):
    """PPM P6 minimo valido, distinto per frame (formato producer invariato)."""
    return ("P6\n%d %d\n255\n" % (w, h)).encode("ascii") + bytes([fill % 256]) * w * h * 3


def make_shell():
    log = []
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.closing = False
    app.display = queue.Queue()
    app.var_msg = LogVar(log, "msg")
    app.var_preview_info = FakeVar("")
    app.var_preview_detail = FakeVar("")
    app._L = lambda key, **kw: key if not kw else "%s|%s" % (key, kw)
    app.root = FakeRoot()
    app.tk = FakeTk(log)
    app.preview_widget = FakePreviewWidget()
    app.preview_frame = None
    app._preview_base = None
    app._preview_base_size = (0, 0)
    app._preview_ppm_raw = None
    app._preview_img = None
    # Refresh smooth disattivato: il fallback assegna la base; la decode
    # resta l'unica PhotoImage per frame elaborato. cv2 mai importato.
    app._refresh_preview = Mock(return_value=False)
    return app, log


class FrameCoalescingTests(unittest.TestCase):
    def test_n_frame_una_sola_decode_ultimo_mostrato(self):
        app, log = make_shell()
        raws = [make_ppm(i) for i in range(5)]
        for raw in raws:
            app.display.put_nowait(("frame", {"ppm": raw}))
        app._drain_display()
        # Una sola decodifica (una sola PhotoImage costruita)...
        self.assertEqual(len(app.tk.calls), 1)
        # ...sul raw dell'ultimo frame accodato.
        self.assertEqual(app.tk.calls[0], raws[-1])
        self.assertEqual(app._preview_ppm_raw, raws[-1])
        self.assertTrue(app.display.empty())
        self.assertEqual(len(app.root.calls), 1)  # throttle B12-04 invariato

    def test_frame_intermedi_scarta_eventi_status_preservati_in_ordine(self):
        app, log = make_shell()
        raws = [make_ppm(i) for i in range(4)]
        app.display.put_nowait(("frame", {"ppm": raws[0]}))
        app.display.put_nowait(("nota", {"testo": "A"}))
        app.display.put_nowait(("frame", {"ppm": raws[1]}))
        app.display.put_nowait(("frame", {"ppm": raws[2]}))
        app.display.put_nowait(("nota", {"testo": "B"}))
        app.display.put_nowait(("frame", {"ppm": raws[3]}))
        app._drain_display()
        # Solo l'ultimo frame decodificato...
        self.assertEqual(len(app.tk.calls), 1)
        self.assertEqual(app.tk.calls[0], raws[-1])
        # ...eventi non-frame tutti elaborati in ordine attorno al frame.
        self.assertEqual(log, [("msg", "nota: A"),
                               ("msg", "nota: B"),
                               ("decode", raws[-1])])

    def test_singolo_frame_invariato(self):
        app, log = make_shell()
        raw = make_ppm(7)
        app.display.put_nowait(("frame", {"ppm": raw}))
        app._drain_display()
        self.assertEqual(len(app.tk.calls), 1)
        self.assertEqual(app.tk.calls[0], raw)
        self.assertEqual(len(app.preview_widget.images), 1)

    def test_solo_non_frame_invariato(self):
        app, log = make_shell()
        app.display.put_nowait(("nota", {"testo": "A"}))
        app.display.put_nowait(("nota", {"testo": "B"}))
        app._drain_display()
        self.assertEqual(app.tk.calls, [])
        self.assertEqual([v for _, v in log], ["nota: A", "nota: B"])

    def test_oltre_throttle_resto_al_prossimo_tick(self):
        app, log = make_shell()
        raws = [make_ppm(i) for i in range(suite_app.DISPLAY_DRAIN_MAX + 8)]
        for raw in raws:
            app.display.put_nowait(("frame", {"ppm": raw}))
        app._drain_display()
        # Primo tick: batch di 32 con 1 sola decode (ultimo del batch).
        self.assertEqual(len(app.tk.calls), 1)
        self.assertEqual(app.tk.calls[0], raws[suite_app.DISPLAY_DRAIN_MAX - 1])
        self.assertEqual(app.display.qsize(), 8)
        # Secondo tick: decode dell'ultimo rimasto, coda svuotata.
        app._drain_display()
        self.assertEqual(len(app.tk.calls), 2)
        self.assertEqual(app.tk.calls[1], raws[-1])
        self.assertTrue(app.display.empty())


if __name__ == "__main__":
    unittest.main()
