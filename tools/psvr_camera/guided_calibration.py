"""Tk preview for headless OpenCV builds, with motion-gated timed collection."""

import json
import queue
import threading
import time
from pathlib import Path

import numpy as np


class CaptureGuide:
    def __init__(self, target=15, hold_s=2.0, move_s=3.0):
        self.target = target
        self.hold_s = hold_s
        self.move_s = move_s
        self.views = []
        self.previous = None
        self.last_saved = None
        self.stable_since = None
        self.stable_anchor = None
        self.move_until = 0.0

    def step(self, corners, now):
        if len(self.views) >= self.target:
            return "RACCOLTA COMPLETATA", False
        if corners is None:
            self.previous = None
            self.stable_since = None
            self.stable_anchor = None
            return "SCACCHIERA NON RILEVATA: mostra tutto il foglio", False
        corners = np.asarray(corners, dtype=float)
        if now < self.move_until:
            self.previous = corners.copy()
            self.stable_since = None
            return f"MUOVI: cambia posizione/inclinazione ({self.move_until-now:.1f}s)", False
        if self.last_saved is not None:
            distance = np.sqrt(np.mean(np.sum((corners - self.last_saved)**2, axis=1)))
            if distance < 8.0:
                self.previous = corners.copy()
                self.stable_since = None
                return "CAMBIA POSA: questa vista e troppo simile alla precedente", False
        motion = float("inf") if self.previous is None else np.sqrt(
            np.mean(np.sum((corners - self.previous)**2, axis=1)))
        self.previous = corners.copy()
        if motion > 1.5:
            self.stable_since = None
            return "FERMATI: attendi che il foglio sia stabile", False
        if self.stable_since is None:
            self.stable_since = now
            self.stable_anchor = corners.copy()
        drift = np.sqrt(np.mean(np.sum((corners-self.stable_anchor)**2, axis=1)))
        if drift > 1.5:
            self.stable_since = now
            self.stable_anchor = corners.copy()
            return "RESTA FERMO: movimento rilevato, conto alla rovescia riavviato", False
        remaining = self.hold_s - (now - self.stable_since)
        if remaining > 0:
            return f"RESTA FERMO: acquisizione tra {remaining:.1f}s", False
        self.views.append(corners.copy())
        self.last_saved = corners.copy()
        self.stable_since = None
        self.move_until = now + self.move_s
        return f"VISTA {len(self.views)}/{self.target} ACQUISITA! Ora cambia posa", True


def build_guided_window(title, instructions, geometry="940x900", wraplength=920):
    """Shared PS Eye/PS4 guide UI; Tk remains optional until actually opened."""
    import tkinter as tk
    from types import SimpleNamespace

    root = tk.Tk()
    root.title(title)
    root.geometry(geometry)
    status = tk.StringVar(value="Apertura camera...")
    progress = tk.StringVar(value="Anteprima: premi AVVIA quando sei pronto")
    tk.Label(root, textvariable=status, font=("Segoe UI", 15, "bold"),
             wraplength=wraplength).pack(pady=8)
    tk.Label(root, textvariable=progress, font=("Segoe UI", 12),
             wraplength=wraplength).pack()
    image_label = tk.Label(root, bg="black")
    image_label.pack(pady=8)
    tk.Label(root, text=instructions, font=("Segoe UI", 11),
             wraplength=wraplength).pack()
    button = tk.Button(root, text="AVVIA RACCOLTA", font=("Segoe UI", 13))
    button.pack(pady=10)
    return SimpleNamespace(root=root, status=status, progress=progress,
                           image_label=image_label, start_button=button)


def run_guided(args, spec):
    import tkinter as tk
    from .camera_backend import import_cv2
    from .camera_source import open_camera_source
    from .calibration import detect_chessboard, save_intrinsics, solve_intrinsics

    cv2 = import_cv2()
    output = Path(args.out) if args.out else None
    detections_path = output.with_suffix(".views.json") if output else None
    if output is None:
        raise ValueError("--guided requires --out to retain captured views")
    if output.exists() or detections_path.exists():
        raise ValueError("output or views file already exists; select a new --out")
    if not output.parent.is_dir():
        raise ValueError("output parent directory does not exist")
    if args.views < 3:
        raise ValueError("at least three views required")

    ui = build_guided_window("PS Eye - calibrazione guidata",
             instructions=f"Verde = tutti i {spec.corner_count} vertici rilevati. Nessuna acquisizione prima di AVVIA.\n"
              "Alterna centro/bordi, vicino/lontano e inclinazioni; mantieni tutto il foglio visibile.\n"
              "Dopo ogni vista: 3 s per muoverti, poi 2 s di stabilita prima dello scatto.")
    root, status, progress = ui.root, ui.status, ui.progress
    image_label, button = ui.image_label, ui.start_button
    start = threading.Event()
    stop = threading.Event()
    frames = queue.Queue(maxsize=1)
    results = queue.Queue()
    button.configure(command=lambda: (start.set(), button.configure(state="disabled")))
    exit_code = [3]

    def worker():
        cap = None
        guide = CaptureGuide(args.views)
        size = None
        started = None
        failures = 0
        try:
            cap, _ = open_camera_source(args.index, backend=args.backend,
                width=args.width, height=args.height, fps=args.fps,
                dll_path=args.dll, read_timeout_s=0.5, max_consecutive_failures=10)
            while not stop.is_set() and len(guide.views) < args.views:
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= args.max_read_failures:
                        raise ValueError("Camera: lettura fallita ripetutamente")
                    continue
                failures = 0
                current_size = (int(frame.shape[1]), int(frame.shape[0]))
                if size is not None and size != current_size:
                    raise ValueError("La risoluzione camera e cambiata")
                size = current_size
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Sector-based detector is more robust to low contrast and its
                # subpixel refinement avoids a fixed oversized corner window.
                if hasattr(cv2, "findChessboardCornersSB"):
                    found, corners = cv2.findChessboardCornersSB(gray,
                        (spec.inner_cols, spec.inner_rows), cv2.CALIB_CB_NORMALIZE_IMAGE)
                    corners = np.asarray(corners).reshape(-1, 2) if found else None
                else:
                    found, corners = detect_chessboard(frame, spec)
                if found:
                    cv2.drawChessboardCorners(frame, (spec.inner_cols, spec.inner_rows),
                        corners.astype(np.float32).reshape(-1, 1, 2), True)
                    xy = corners.astype(int)
                    cv2.rectangle(frame, tuple(xy.min(axis=0)), tuple(xy.max(axis=0)), (0,255,0), 2)
                now = time.monotonic()
                if start.is_set():
                    if started is None:
                        started = now
                    message, saved = guide.step(corners if found else None, now)
                    elapsed = now - started
                    detail = f"Viste {len(guide.views)}/{args.views} | Tempo {elapsed:.0f}/{args.max_seconds:.0f}s"
                else:
                    message = "SCACCHIERA RILEVATA - premi AVVIA" if found else "Mostra tutta la scacchiera alla camera"
                    saved = False
                    elapsed = 0
                    detail = "Anteprima: raccolta non ancora avviata"
                frame = cv2.resize(frame, (800, 600))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ppm = b"P6\n800 600\n255\n" + rgb.tobytes()
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                frames.put_nowait((ppm, message, detail, saved))
                if started is not None and elapsed >= args.max_seconds:
                    break
            if stop.is_set():
                raise ValueError("Raccolta interrotta dall'utente")
            if len(guide.views) < args.views:
                raise ValueError(f"Tempo terminato: {len(guide.views)}/{args.views} viste. Dati conservati per analisi.")
            results.put(("status", "Raccolta completa: calcolo calibrazione..."))
            intrinsics = solve_intrinsics(guide.views, size, spec,
                prefer_opencv=not args.no_opencv, max_rms_px=args.max_rms_px)
            save_intrinsics(output, intrinsics)
            results.put(("done", f"CALIBRAZIONE SALVATA: RMS {intrinsics.rms_reprojection_px:.3f}px\n{output}", 0))
        except Exception as exc:
            results.put(("done", f"{exc}\nViste conservate: {detections_path}", 3))
        finally:
            if cap is not None:
                cap.release()
            if size is not None:
                try:
                    with detections_path.open("x", encoding="utf-8") as stream:
                        json.dump({"image_size": list(size), "views": [v.tolist() for v in guide.views],
                                   "chessboard": spec.to_dict()}, stream)
                except Exception as exc:
                    results.put(("done", f"Errore salvataggio viste: {exc}", 3))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def tick():
        try:
            ppm, message, detail, saved = frames.get_nowait()
            photo = tk.PhotoImage(data=ppm, format="PPM")
            image_label.configure(image=photo)
            image_label.image = photo
            status.set(message)
            progress.set(detail)
            if saved:
                root.bell()
        except queue.Empty:
            pass
        try:
            while True:
                result = results.get_nowait()
                status.set(result[1])
                if result[0] == "done":
                    exit_code[0] = result[2]
                    button.configure(state="disabled")
                    progress.set("Chiudi la finestra per terminare")
        except queue.Empty:
            pass
        root.after(40, tick)

    root.protocol("WM_DELETE_WINDOW", lambda: (stop.set(), root.destroy()))
    root.after(40, tick)
    root.mainloop()
    thread.join(timeout=10)
    return exit_code[0]
