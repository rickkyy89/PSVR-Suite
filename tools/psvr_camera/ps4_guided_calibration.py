"""PS4 stereo extension of the existing PS Eye guided chessboard workflow.

The monitor shows both eyes; collection starts ONLY after the operator confirms
the board size and presses Start. No headset wearing or SteamVR is required.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sys
import threading
import time

import numpy as np

from .calibration import CalibrationError, ChessboardSpec, detect_chessboard
from .guided_calibration import CaptureGuide, build_guided_window
from .ps4_camera import MODES

POSE_HINTS = (
    "Centro, foglio quasi frontale",
    "Centro, inclina il foglio verso sinistra",
    "Centro, inclina il foglio verso destra",
    "Centro, inclina il bordo superiore verso la camera",
    "Centro, inclina il bordo inferiore verso la camera",
    "Sposta verso il bordo sinistro, senza tagliare il foglio",
    "Sposta verso il bordo destro, senza tagliare il foglio",
    "Sposta verso la parte alta dell'inquadratura",
    "Sposta verso la parte bassa dell'inquadratura",
    "Avvicina il foglio, mantenendolo interamente visibile",
    "Allontana il foglio e inclinalo leggermente",
    "Zona alta sinistra, con una lieve inclinazione",
    "Zona bassa destra, con una lieve inclinazione",
    "Ruota leggermente il foglio e cambia distanza",
    "Ultima posa diversa: inclina il foglio e mantienilo tutto visibile",
)


class StereoCaptureGuide:
    """Reuse CaptureGuide's countdown, requiring BOTH eyes to remain stable."""
    def __init__(self, spec, target=15, hold_s=2.0, move_s=3.0):
        self.spec = spec
        self.guide = CaptureGuide(target, hold_s, move_s)
        self.last_time = None

    @property
    def views(self):
        n = self.spec.corner_count
        return [(view[:n].copy(), view[n:].copy()) for view in self.guide.views]

    def _reset_hold(self):
        self.guide.previous = None
        self.guide.stable_since = None
        self.guide.stable_anchor = None

    def step(self, left, right, now):
        if not np.isfinite(now) or (self.last_time is not None and now <= self.last_time):
            self._reset_hold()
            return "TEMPO NON VALIDO: attendo una nuova coppia", False
        if self.last_time is not None and now - self.last_time > 0.5:
            self._reset_hold()
        self.last_time = now
        for name, corners in (("L", left), ("R", right)):
            if corners is None:
                self.guide.step(None, now)
                return f"OCCHIO {name}: mostra tutta la scacchiera", False
            if (np.asarray(corners).shape != (self.spec.corner_count, 2)
                    or not np.isfinite(corners).all()):
                self._reset_hold()
                return f"OCCHIO {name}: vertici non validi", False
        combined = np.concatenate((left, right), axis=0).astype(float)
        n = self.spec.corner_count

        def worst_eye_distance(previous):
            delta = (combined - previous).reshape(2, n, 2)
            return float(np.sqrt(np.mean(np.sum(delta ** 2, axis=2), axis=1)).max())

        # A stable eye must not hide motion in the other through an RMS average.
        if self.guide.previous is not None and worst_eye_distance(self.guide.previous) > 1.5:
            self.guide.stable_since = None
            self.guide.stable_anchor = None
        if self.guide.stable_anchor is not None and worst_eye_distance(self.guide.stable_anchor) > 1.5:
            self.guide.stable_since = None
            self.guide.stable_anchor = None
        return self.guide.step(combined, now)


def detect_board(image, spec, cv2):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if int(gray.max()) - int(gray.min()) < 8:
        return None
    if hasattr(cv2, "findChessboardCornersSB"):
        found, corners = cv2.findChessboardCornersSB(
            gray, (spec.inner_cols, spec.inner_rows), cv2.CALIB_CB_NORMALIZE_IMAGE)
        if found:
            return np.asarray(corners, dtype=float).reshape(-1, 2)
        return None
    found, corners = detect_chessboard(image, spec, module=cv2)
    return corners if found else None


def training_split(pairs):
    """Every fifth accepted pair is held out from ALL fitting."""
    training = [pair for i, pair in enumerate(pairs) if i % 5 != 4]
    heldout = [pair for i, pair in enumerate(pairs) if i % 5 == 4]
    if len(training) < 8 or len(heldout) < 2:
        raise CalibrationError("servono almeno 10 coppie, incluse due di verifica")
    return training, heldout


def fit_pairs(pairs, image_size, spec, mode, max_rms_px=1.0):
    from .stereo_calibration import solve_stereo, validate_stereo
    training, heldout = training_split(pairs)
    calibration = solve_stereo([p[0] for p in training], [p[1] for p in training],
        image_size, spec, max_rms_px=max_rms_px, mode=mode)
    validation = validate_stereo(calibration, [p[0] for p in heldout],
        [p[1] for p in heldout], max_epipolar_px=max_rms_px,
        max_reprojection_px=1.5 * max_rms_px)
    if not validation.get("accepted", False):
        raise CalibrationError("Verifica su coppie escluse dal calcolo non superata")
    return calibration, {
        "accepted": True, "validation": validation,
        "rms_reprojection_px": calibration.rms,
        "baseline_m": float(np.linalg.norm(calibration.T)),
        "training_views": len(training), "heldout_views": len(heldout),
    }


def calibrate_saved_views(views_path, output, max_rms_px=1.0):
    """Replay original paired corners without camera/Tk access or threshold changes."""
    from .stereo_calibration import save_stereo
    if Path(output).exists():
        raise FileExistsError(f"rifiuto sovrascrittura: {output}")
    with Path(views_path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CalibrationError("file viste troppo grande")
    data = json.loads(raw.decode("utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != "psvr.camera.stereo_views"
            or type(data.get("version")) is not int or data["version"] != 1
            or data.get("source") != "ps4_ov580_uvc"):
        raise CalibrationError("formato viste stereo PS4 non riconosciuto")
    mode = data.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise CalibrationError("modalita delle viste non valida")
    spec_mode = MODES[mode]
    size = (spec_mode.eye_width, spec_mode.eye_height)
    if data.get("image_size") != list(size):
        raise CalibrationError("dimensioni viste incompatibili con la modalita PS4")
    spec = ChessboardSpec(**data["chessboard"])
    views = data.get("views")
    if not isinstance(views, list) or not 10 <= len(views) <= 40:
        raise CalibrationError("servono 10..40 coppie originali")
    pairs = []
    last_sequence, last_time = -1, -1.0
    for view in views:
        if not isinstance(view, dict):
            raise CalibrationError("ogni vista deve essere una coppia con metadati")
        sequence, stamp = view.get("sequence"), view.get("host_monotonic")
        if (type(sequence) is not int or sequence < 1 or sequence <= last_sequence
                or type(stamp) not in (int, float) or not np.isfinite(stamp)
                or stamp < 0 or stamp <= last_time):
            raise CalibrationError("sequenza/tempo delle coppie non valido")
        pairs.append((np.asarray(view["left"], dtype=float),
                      np.asarray(view["right"], dtype=float)))
        last_sequence, last_time = sequence, stamp
    calibration, report = fit_pairs(pairs, size, spec, mode, max_rms_px)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    save_stereo(output, calibration)
    report.update(schema="psvr.camera.ps4.guided_replay", version=1,
                  views=len(pairs), views_file=str(views_path),
                  views_sha256=hashlib.sha256(raw).hexdigest(), output=str(output),
                  camera_opened=False, source_views_modified=False)
    return report


def save_views(path, pairs, samples, spec, image_size, mode, controls):
    if len(pairs) != len(samples):
        raise CalibrationError("ogni coppia deve avere metadati della stessa acquisizione")
    data = {
        "schema": "psvr.camera.stereo_views", "version": 1,
        "source": "ps4_ov580_uvc", "mode": mode, "image_size": list(image_size),
        "chessboard": spec.to_dict(), "physical_eye_order_verified": False,
        "timing_source": "host_read_completion_not_exposure", "controls": controls,
        "views": [{"left": left.tolist(), "right": right.tolist(), **sample}
                  for (left, right), sample in zip(pairs, samples)],
    }
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)


def run_guided(args, external_stop=None, close_after_s=None):
    import tkinter as tk
    from .camera_backend import import_cv2
    from .ps4_stereo_session import StereoSession

    spec = ChessboardSpec(args.cols, args.rows, args.square_mm / 1000)
    if not 10 <= args.views <= 40:
        raise CalibrationError("views deve essere tra 10 e 40")
    if not 30 <= args.max_seconds <= 900 or not 10 <= args.idle_seconds <= 3600:
        raise CalibrationError("durata raccolta 30..900 s; attesa avvio 10..3600 s")
    if not np.isfinite(args.max_rms_px) or args.max_rms_px <= 0:
        raise CalibrationError("max_rms_px deve essere finito e positivo")
    output = Path(args.out)
    views_path = output.with_suffix(".views.json")
    if output.exists() or views_path.exists():
        raise CalibrationError("output gia presente: scegli un nuovo nome")
    output.parent.mkdir(parents=True, exist_ok=True)
    cv2 = import_cv2()
    mode = MODES[args.mode]
    ui = build_guided_window("PS4 Camera - calibrazione guidata stereo",
        f"Scacchiera: {spec.inner_cols}x{spec.inner_rows} angoli interni, quadrati {args.square_mm:g} mm. "
        "L e R devono vedere TUTTO il foglio. Verde = vertici rilevati.\n"
        "Premi AVVIA solo quando sei pronto: 2 s fermo per lo scatto, poi 3 s per cambiare posa.\n"
        "Alterna centro/bordi, vicino/lontano e inclinazioni. Guarda il monitor; non indossare il visore.\n"
        "Luminosita temporanea: i controlli LED vengono ripristinati prima del calcolo e alla chiusura.",
        geometry="1280x790", wraplength=1180)
    root = ui.root
    start, stop = threading.Event(), threading.Event()
    external_stop = external_stop if external_stop is not None else threading.Event()
    confirm = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text=f"Confermo che il lato dei quadrati stampati e {args.square_mm:g} mm",
                   variable=confirm).pack()
    frames, events = queue.Queue(maxsize=1), queue.Queue()
    state = {"closing": False, "finished": False, "report": None}

    def begin():
        if not confirm.get():
            ui.status.set("Conferma prima la misura dei quadrati stampati")
            return
        if not state["finished"]:
            start.set()
            ui.start_button.configure(state="disabled")

    def request_close():
        state["closing"] = True
        stop.set()
        ui.start_button.configure(state="disabled")
        ui.status.set("Chiusura: rilascio camera e ripristino controlli LED...")

    ui.start_button.configure(command=begin)
    tk.Button(root, text="CHIUDI / ANNULLA", command=request_close).pack(pady=4)
    root.protocol("WM_DELETE_WINDOW", request_close)

    def put_frame(item):
        try:
            frames.get_nowait()
        except queue.Empty:
            pass
        frames.put_nowait(item)

    def worker():
        guide = StereoCaptureGuide(spec, args.views)
        samples = []
        session = None
        capture_report = {}
        error = None
        preview_frames = 0
        started = None
        idle_started = time.monotonic()
        try:
            session = StereoSession(args.index, mode=args.mode, fps=args.fps,
                gain=args.gain, brightness=args.brightness,
                allow_delayed_readback=True)
            session.start()
            while not stop.is_set() and len(guide.guide.views) < args.views:
                now = time.monotonic()
                if not start.is_set() and now - idle_started >= args.idle_seconds:
                    raise CalibrationError("Anteprima terminata senza AVVIA; nessuna calibrazione creata")
                if started is not None and now - started >= args.max_seconds:
                    raise CalibrationError(f"Tempo raccolta terminato: {len(samples)}/{args.views} coppie")
                sample = session.next_pair(timeout_s=0.5)
                if sample is None:
                    report = session.report()
                    if report.get("error"):
                        raise CalibrationError(str(report["error"]))
                    continue
                left, right = sample["left"], sample["right"]
                corners_l, corners_r = detect_board(left, spec, cv2), detect_board(right, spec, cv2)
                preview_frames += 1
                stamp = sample["host_monotonic"]
                saved = False
                if start.is_set():
                    if started is None:
                        started = time.monotonic()
                    message, saved = guide.step(corners_l, corners_r, stamp)
                    if saved:
                        samples.append({"sequence": sample["sequence"], "host_monotonic": stamp})
                        events.put({"kind": "saved"})
                else:
                    message = ("SCACCHIERA RILEVATA NEI DUE OCCHI - premi AVVIA" if
                               corners_l is not None and corners_r is not None else
                               "ANTEPRIMA: mostra tutta la scacchiera in entrambi gli occhi")
                detail = (f"L: {spec.corner_count if corners_l is not None else 0}/{spec.corner_count} | "
                          f"R: {spec.corner_count if corners_r is not None else 0}/{spec.corner_count} | "
                          f"Coppie {len(samples)}/{args.views} | "
                          + (f"Tempo {time.monotonic()-started:.0f}/{args.max_seconds:.0f} s"
                           if started is not None else "Nessuno scatto prima di AVVIA"))
                if len(samples) < args.views:
                    detail += "\nProssima posa: " + POSE_HINTS[len(samples) % len(POSE_HINTS)]
                images = []
                for name, image, corners in (("L", left, corners_l), ("R", right, corners_r)):
                    annotated = image.copy()
                    if corners is not None:
                        cv2.drawChessboardCorners(annotated, (spec.inner_cols, spec.inner_rows),
                            corners.astype(np.float32).reshape(-1, 1, 2), True)
                    cv2.putText(annotated, name, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                                0.8, (0, 255, 0), 2)
                    images.append(annotated)
                image = np.concatenate(images, axis=1)
                width = min(1200, image.shape[1])
                height = round(image.shape[0] * width / image.shape[1])
                image = cv2.resize(image, (width, height))
                ppm = f"P6\n{width} {height}\n255\n".encode() + image[:, :, ::-1].tobytes()
                put_frame((ppm, message, detail))
            if stop.is_set():
                raise CalibrationError("Raccolta annullata; eventuali coppie conservate, calibrazione non attivata")
        except Exception as exc:
            error = str(exc)
        finally:
            if session is not None:
                session.stop(timeout_s=2.0)
                capture_report = session.report()
                if capture_report.get("cleanup_pending"):
                    events.put({"kind": "status", "message":
                        "Ripristino controlli ancora in corso: attendo il driver. Non forzare la chiusura."})
                # The device owner is a daemon thread. Returning/exiting here
                # would kill it BEFORE it can restore the bright camera controls.
                # Keep the process and responsive Tk window alive until actual
                # owner completion, even after a read/release deadline expired.
                while capture_report.get("cleanup_pending"):
                    time.sleep(.1)
                    capture_report = session.report()
                controls = capture_report.get("controls", {})
                if capture_report.get("error") or controls.get("errors"):
                    error = (error + "; " if error else "") + str(
                        capture_report.get("error") or controls["errors"])
                if capture_report.get("cleanup_pending") or controls.get("restoration_failed"):
                    error = (error + "; " if error else "") + "Ripristino controlli non confermato: verifica il report"
            try:
                save_views(views_path, guide.views, samples, spec,
                           (mode.eye_width, mode.eye_height), args.mode, capture_report)
            except Exception as exc:
                error = (error + "; " if error else "") + f"Salvataggio viste fallito: {exc}"
        report = {"schema": "psvr.camera.ps4.guided", "version": 1,
                  "accepted": False, "views": len(samples), "preview_frames": preview_frames,
                  "views_file": str(views_path), "output": str(output),
                  "capture": capture_report, "errors": [error] if error else []}
        if not error:
            try:
                from .stereo_calibration import save_stereo
                events.put({"kind": "status", "message": "Camera rilasciata. Calcolo calibrazione stereo..."})
                calibration, fit_report = fit_pairs(guide.views,
                    (mode.eye_width, mode.eye_height), spec, args.mode, args.max_rms_px)
                if stop.is_set():
                    raise CalibrationError("Calcolo annullato: nessuna calibrazione salvata")
                save_stereo(output, calibration)
                report.update(fit_report)
            except Exception as exc:
                report["errors"].append(str(exc))
        events.put({"kind": "done", "report": report})

    thread = threading.Thread(target=worker, daemon=True, name="ps4-guided-collector")
    thread.start()

    def tick():
        stop_file = getattr(args, "stop_file", None)
        if stop_file is not None and Path(stop_file).exists():
            external_stop.set()
        if external_stop.is_set() and not state["closing"]:
            request_close()
        try:
            ppm, message, detail = frames.get_nowait()
            photo = tk.PhotoImage(data=ppm, format="PPM", master=root)
            ui.image_label.configure(image=photo)
            ui.image_label.image = photo
            if not state["closing"]:
                ui.status.set(message)
                ui.progress.set(detail)
        except queue.Empty:
            pass
        try:
            while True:
                event = events.get_nowait()
                if event["kind"] == "saved":
                    root.bell()
                elif event["kind"] == "status":
                    ui.status.set(event["message"])
                elif event["kind"] == "done":
                    state.update(finished=True, report=event["report"])
                    ui.start_button.configure(state="disabled")
                    report = event["report"]
                    ui.status.set("CALIBRAZIONE STEREO SALVATA" if report["accepted"] else
                                  "CALIBRAZIONE NON SALVATA: " + "; ".join(report["errors"]))
                    ui.progress.set((f"RMS stereo {report['rms_reprojection_px']:.3f} px | "
                                     f"Verifica su {report['heldout_views']} coppie escluse dal calcolo superata\n"
                                     f"Nuovo profilo: {output}") if report["accepted"]
                                    else f"Dati conservati: {views_path}. Nessun profilo esistente modificato.")
        except queue.Empty:
            pass
        if state["closing"] and state["finished"] and not thread.is_alive():
            root.destroy()
            return
        root.after(50, tick)

    root.after(50, tick)
    if close_after_s is not None:
        root.after(round(close_after_s * 1000), request_close)
    root.mainloop()
    return state["report"]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--index", type=int)
    result.add_argument("--solve-views", type=Path,
                        help="Ricalcola le coppie salvate offline, senza aprire la camera")
    result.add_argument("--mode", choices=tuple(MODES), default="640x400")
    result.add_argument("--fps", type=int, default=60)
    result.add_argument("--cols", type=int, default=9)
    result.add_argument("--rows", type=int, default=6)
    result.add_argument("--square-mm", type=float, default=20)
    result.add_argument("--views", type=int, default=15)
    result.add_argument("--max-seconds", type=float, default=300)
    result.add_argument("--idle-seconds", type=float, default=900)
    result.add_argument("--max-rms-px", type=float, default=1.0)
    result.add_argument("--gain", type=float, default=8)
    result.add_argument("--brightness", type=float, default=8)
    result.add_argument("--stop-file", type=Path,
                        help="Private cooperative-stop marker used by the Suite panel")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result.add_argument("--out", type=Path,
                        default=Path("logs/ps4_camera/calibration") / f"ps4_stereo_{stamp}.json")
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.solve_views is not None:
        try:
            report = calibrate_saved_views(args.solve_views, args.out, args.max_rms_px)
            print(json.dumps(report, indent=2, allow_nan=False))
            return 0
        except (ValueError, OSError, TypeError, KeyError) as exc:
            print(json.dumps({"errors": [str(exc)]}))
            return 3
    if args.index is None or args.index < 0:
        arguments.error("specifica --index >= 0 oppure --solve-views")
    external_stop = threading.Event()

    def stdin_stop():
        for line in sys.stdin:
            if line.strip() == "stop":
                external_stop.set()
                break

    if sys.stdin is not None:
        threading.Thread(target=stdin_stop, daemon=True, name="ps4-guided-stop").start()
    try:
        report = run_guided(args, external_stop)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0 if report and report["accepted"] else 3
    except (ValueError, OSError) as exc:
        print(json.dumps({"errors": [str(exc)]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
