"""LED trial; diagnostic HID and driver-only UDP modes are explicitly separate."""

import json
from pathlib import Path
import queue
import threading
import time

from .blue_leds import BlueLedConfig, BlueLedDetector
from .calibration import load_intrinsics
from .camera_source import open_camera_source
from .led_pose import FrontLedTracker, load_led_rig


def run_led_track(args):
    driver_mode = getattr(args,"driver_imu",False)
    send_position = getattr(args,"send_position",False)
    from .position_pipeline import PositionPipeline, load_position_config
    position_config = load_position_config(getattr(args,"position_config",None))
    initial_filter = getattr(args,"position_filter",None) or position_config.get("filter","one_euro")
    one_euro_settings = dict(
        min_cutoff_hz=getattr(args,"position_filter_min_cutoff_hz",8.0),
        beta=getattr(args,"position_filter_beta",15.0),
        max_cutoff_hz=getattr(args,"position_filter_max_cutoff_hz",40.0),
        derivative_cutoff_hz=getattr(args,"position_filter_derivative_cutoff_hz",4.0))
    one_euro_settings.update(position_config.get("one_euro",{}))
    if driver_mode and args.imu:
        raise ValueError("--driver-imu and --imu are mutually exclusive")
    if send_position and not driver_mode:
        raise ValueError("--send-position requires --driver-imu")
    guided = args.imu or driver_mode
    intrinsics = load_intrinsics(args.intrinsics, expected_image_size=(args.width,args.height))
    rig, points = load_led_rig(args.rig)
    initial_config = BlueLedConfig(min_area=args.min_area,value_min=args.value_min,
        saturation_min=args.saturation_min,opening_size=args.opening_size,include_white_cores=True)
    detector = BlueLedDetector(initial_config)
    settings = {"min_area":args.min_area, "value_min":args.value_min,
                "exposure":args.exposure,"gain":args.gain,"position_filter":initial_filter}
    settings_lock = threading.Lock()
    cv2 = detector.cv2
    tracker = FrontLedTracker(intrinsics, points, args.max_reprojection_px)
    if guided:
        from .imu_led_pose import ImuLedTracker, load_rotation_rig
        rig,points,normals = load_rotation_rig(args.imu_rig)
        tracker = ImuLedTracker(intrinsics,points,normals,rig["labels"],args.max_reprojection_px)
    if not args.preview and not (args.frames or args.max_seconds):
        raise ValueError("non-preview capture must have a frame or time limit")
    log_path = Path(args.log) if args.log else None
    if log_path and (not log_path.parent.is_dir() or log_path.exists()):
        raise ValueError("log parent must exist and log must be a new file")
    stop = threading.Event()
    recenter = threading.Event()
    realign = threading.Event()
    start_imu = threading.Event()
    if args.imu and not args.preview:
        start_imu.set()
    frames = queue.Queue(maxsize=1)
    results = queue.Queue()
    summary = {"schema":"psvr.camera.led_track", "captured_frames":0,"valid_frames":0,
                "read_failures":0, "geometry_validated":False,"udp_enabled":send_position,
               "rig_provenance":rig["provenance"],"origin":"front_centre_led"}

    def capture():
        cap = None
        imu_source = None
        sender = None
        position_filter = None
        position_seq = 0
        imu_failure = None
        stream = None
        origin = None
        first_valid = None
        last_good = None
        failures = 0
        last_settings = (args.min_area,args.value_min)
        last_controls = None
        last_idle_log = 0.0
        started = time.perf_counter()
        try:
            if log_path:
                stream = log_path.open("x",encoding="utf-8")
                stream.write(json.dumps({"event":"session","intrinsics":str(args.intrinsics),
                    "rig":rig,"image_size":[args.width,args.height],"udp_enabled":send_position,
                    "head_offset_model_m":getattr(args,"head_offset_model_m",(0,0,0)),
                    "position_scale":getattr(args,"position_scale",1.0),
                    "position_config":{"filter":initial_filter,"one_euro":one_euro_settings,
                        "kalman":position_config.get("kalman",{}),
                        "uncertainty":position_config.get("uncertainty",{}),
                        "prediction_output":position_config.get("prediction_output","camera")}})+"\n")
            if driver_mode:
                from .driver_imu_source import DriverImuSource
                imu_source = DriverImuSource(port=getattr(args,"driver_imu_port",4272))
                if send_position:
                    from .driver_position import DriverPositionSender
                    position_filter = PositionPipeline(initial_filter,
                        one_euro=one_euro_settings,kalman=position_config.get("kalman",{}),
                        uncertainty=position_config.get("uncertainty",{}),
                        prediction_output=position_config.get("prediction_output","camera"))
                    sender = DriverPositionSender(port=getattr(args,"position_port",4271))
            cap, _ = open_camera_source(args.index, backend=args.backend,
                width=args.width,height=args.height,fps=args.fps,dll_path=args.dll,
                read_timeout_s=0.5,max_consecutive_failures=10)
            while not stop.is_set():
                if args.imu and start_imu.is_set():
                    from .imu_source import DiagnosticImuSource
                    if imu_source is not None:
                        imu_source.close()
                    imu_source = DiagnosticImuSource(args.imu_diag,args.imu_config)
                    tracker.reset()
                    origin = None
                    imu_failure = None
                    start_imu.clear()
                if args.imu and imu_source is not None and imu_source.process.poll() is not None:
                    imu_failure = "IMU terminata (exit %s): premi AVVIA IMU e resta fermo per 8 secondi" % imu_source.process.returncode
                    summary["imu_error"] = imu_failure
                    imu_source.close()
                    imu_source = None
                    tracker.reset()
                if args.frames and summary["captured_frames"] >= args.frames:
                    break
                if args.max_seconds and time.perf_counter()-started >= args.max_seconds:
                    break
                with settings_lock:
                    requested_controls = (settings["exposure"],settings["gain"])
                    requested_filter = settings["position_filter"]
                if position_filter is not None and requested_filter != position_filter.mode:
                    position_filter = PositionPipeline(requested_filter,
                        one_euro=one_euro_settings,kalman=position_config.get("kalman",{}),
                        uncertainty=position_config.get("uncertainty",{}),
                        prediction_output=position_config.get("prediction_output","camera"))
                    if stream:
                        stream.write(json.dumps({"event":"position_reset","reason":"filter_switch",
                            "filter":requested_filter})+"\n")
                if requested_controls != last_controls:
                    if not hasattr(cap,"set_manual_controls"):
                        raise ValueError("manual LED exposure currently requires the native PS Eye backend")
                    cap.set_manual_controls(*requested_controls)
                    tracker.reset()
                    if position_filter is not None and position_filter.mode == "kalman":
                        position_filter.reset("camera_controls")
                    if stream:
                        stream.write(json.dumps({"event":"position_reset","reason":"camera_controls"})+"\n")
                    last_controls = requested_controls
                ok, frame = cap.read()
                if not ok or frame is None:
                    if sender is not None:
                        failed_at = time.perf_counter()
                        position_filter.invalid(failed_at)
                        sender.invalidate()
                        if stream:
                            stream.write(json.dumps({"event":"position_reset","reason":"camera_read_failure",
                                "monotonic_s":failed_at})+"\n")
                    if getattr(cap,"error",None):
                        raise ValueError(cap.error)
                    failures += 1
                    summary["read_failures"] += 1
                    if failures >= 10:
                        raise ValueError("camera read failed ten times consecutively")
                    continue
                failures = 0
                if (frame.shape[1],frame.shape[0]) != intrinsics.image_size:
                    raise ValueError("live camera resolution differs from calibration")
                captured_at = getattr(cap,"last_frame_monotonic",None) or time.perf_counter()
                imu_sample = None
                if imu_source is not None:
                    if realign.is_set():
                        tracker.reset()
                        if position_filter is not None:
                            position_filter.reset()
                        if stream:
                            stream.write(json.dumps({"event":"position_reset","reason":"realign"})+"\n")
                        origin = None
                        realign.clear()
                    imu_sample = imu_source.at(captured_at)
                    tracker.set_imu(imu_sample)
                roi, size_ratio = tracker.search_region(args.width,args.height,captured_at)
                with settings_lock:
                    base_settings = (settings["min_area"],settings["value_min"])
                if base_settings != last_settings:
                    tracker.reset()
                    if position_filter is not None and position_filter.mode == "kalman":
                        position_filter.reset("detector_settings")
                    if stream:
                        stream.write(json.dumps({"event":"position_reset","reason":"detector_settings"})+"\n")
                    roi,size_ratio = None,1.0
                    last_settings = base_settings
                effective_area = base_settings[0] if roi is None else max(3,
                    int(base_settings[0]*min(1.0,size_ratio**2)*.5))
                config = BlueLedConfig(min_area=effective_area,value_min=base_settings[1],
                    saturation_min=args.saturation_min,
                    opening_size=args.opening_size if roi is None else 0,include_white_cores=True)
                detector.config = config
                if roi is None:
                    candidates = detector.detect(frame)
                else:
                    x0,y0,x1,y1 = roi
                    candidates = detector.detect(frame[y0:y1,x0:x1])
                    for candidate in candidates:
                        candidate["center_px"][0] += x0
                        candidate["center_px"][1] += y0
                        candidate["bbox_px"][0] += x0
                        candidate["bbox_px"][1] += y0
                pose = tracker.update(candidates,captured_at)
                if imu_failure is not None:
                    from .led_pose import LedPose
                    pose = LedPose(False,imu_failure)
                if sender is not None:
                    from .driver_position import build_driver_position_packet
                    # Preserve current identity even when no pre-capture sample exists;
                    # such frames are invalid, never a fabricated position.
                    binding = imu_sample if imu_sample is not None else imu_source.latest()
                    position_seq = max(position_seq, (sender.last_packet or {}).get("seq",-1)+1)
                    outgoing = build_driver_position_packet(
                        pose if imu_sample is not None else None, binding, tracker.alignment, position_seq,
                        head_offset_model_m=getattr(args,"head_offset_model_m",(0,0,0)),
                        scale=getattr(args,"position_scale",1.0), now=captured_at)
                    from .pose_uncertainty import estimate_pose_covariance
                    from .driver_imu_source import MAX_AGE_S
                    covariance, quality = None, {"reason":"not_requested","reliable":False}
                    if position_filter.mode == "kalman" or stream is not None:
                        covariance, quality = estimate_pose_covariance(pose,intrinsics,points,rig["labels"],
                            tracker.alignment,scale=getattr(args,"position_scale",1.0),
                            head_offset_model_m=getattr(args,"head_offset_model_m",(0,0,0)),
                            guided=pose.reason in ("imu_guided_pattern","imu_guided_front_reacquired"))
                    context_valid = (outgoing is not None and imu_sample is not None
                        and imu_sample.get("valid") is True and imu_sample.get("connected",True) is True
                        and tracker.alignment is not None
                        and 0 <= captured_at-imu_sample["monotonic_s"] <= MAX_AGE_S)
                    optical_prediction = (pose.valid or pose.reason.startswith("Pattern parziale:")
                        or pose.reason.startswith("Pattern non associato:"))
                    prediction_allowed = bool(context_valid and optical_prediction)
                    estimator_input = {"packet":outgoing,"alignment":None if tracker.alignment is None
                        else tracker.alignment.tolist(),"covariance":None if covariance is None else covariance.tolist(),
                        "quality":quality,"prediction_allowed":prediction_allowed,"context_valid":bool(context_valid)}
                    outgoing = position_filter.filter_packet(outgoing, tracker.alignment, captured_at,
                        covariance=covariance,quality=quality,prediction_allowed=prediction_allowed,
                        context_valid=bool(context_valid))
                    if outgoing is not None:
                        if not sender.send(outgoing):
                            summary["position_error"] = sender.last_error
                        position_seq += 1
                    else:
                        sender.invalidate()
                summary["camera_controls"] = cap.controls_status()
                summary["captured_frames"] += 1
                if pose.valid:
                    summary["valid_frames"] += 1
                    last_good = pose
                    if origin is None or recenter.is_set():
                        origin = pose.tvec.reshape(3).copy()
                        recenter.clear()
                    if first_valid is None:
                        first_valid = pose.to_dict()
                relative = None if not pose.valid else (pose.tvec.reshape(3)-origin).tolist()
                packet = {"event":"pose","monotonic_s":captured_at,
                    "candidates":candidates,
                    "candidate_count":len(candidates),"relative_camera_m":relative,"search_roi":roi,
                    "detector":{"value_min":config.value_min,"min_area":config.min_area,
                        "saturation_min":config.saturation_min,"opening_size":config.opening_size},
                    "camera_controls":cap.controls_status(),**pose.to_dict()}
                if position_filter is not None:
                    packet["position_filter"] = position_filter.diagnostics()
                    packet["estimator_input"] = estimator_input
                    packet["position_output"] = outgoing
                if imu_source is not None:
                    packet["imu"] = None if imu_sample is None else {key:imu_sample.get(key) for key in
                        ("valid","state","monotonic_s","quaternion_wxyz","quaternion_sensor_wxyz",
                          "corrected_gyro_rad_s","angles_deg","recenter_count","interpolated",
                          "session_id","recenter_epoch","seq","sample_time_s","world_rotation_wxyz")}
                should_log = not args.imu or imu_source is not None or captured_at-last_idle_log >= 1.
                if stream and should_log:
                    stream.write(json.dumps(packet)+"\n")
                    if args.imu and imu_source is None:
                        last_idle_log = captured_at
                    if summary["captured_frames"] % 30 == 0:
                        stream.flush()
                if args.preview:
                    annotated = detector.annotate(frame,candidates)
                    if roi is not None:
                        cv2.rectangle(annotated,roi[:2],roi[2:],(255,180,0),1)
                    if pose.valid:
                        for label,xy in zip(pose.led_labels or rig["labels"],pose.image_points):
                            cv2.putText(annotated,label,tuple(int(v) for v in xy),
                                cv2.FONT_HERSHEY_SIMPLEX,.35,(0,255,255),1)
                        text = "TRACKING SPERIMENTALE | RMS %.2f / %.2f px" % (pose.rms_px,pose.rms_limit_px)
                        detail = "Centro LED: X %.3f  Y %.3f  Z %.3f m | Delta: %.3f %.3f %.3f" % (
                            *pose.tvec.reshape(3),*relative)
                        detail += " | Candidati: %d" % len(candidates)
                        if pose.led_labels:
                            detail += " | Associati: %s" % ", ".join(pose.led_labels)
                        if pose.imu_age_s is not None:
                            detail += " | IMU %.0f ms" % (pose.imu_age_s*1000)
                    else:
                        text = pose.reason
                        detail = "Nessuna posizione valida | Candidati blu: %d" % len(candidates)
                    detail += " | Area minima effettiva: %d px" % effective_area
                    controls = cap.controls_status()
                    detail += " | Exp %s Gain %s" % (
                        controls["exposure"] if controls else "...",controls["gain"] if controls else "...")
                    display_size = (560,420) if guided else (640,480)
                    shown = cv2.resize(annotated,display_size)
                    ppm = ("P6\n%d %d\n255\n" % display_size).encode("ascii")+cv2.cvtColor(shown,cv2.COLOR_BGR2RGB).tobytes()
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        pass
                    frames.put_nowait((ppm,text,detail))
            summary["duration_s"] = round(time.perf_counter()-started,3)
            summary["first_valid_pose"] = first_valid
            summary["last_valid_pose"] = None if last_good is None else last_good.to_dict()
            summary["ok"] = True
        except Exception as exc:
            summary["ok"] = False
            summary["error"] = str(exc)
        finally:
            if position_filter is not None:
                position_filter.reset()
            if sender is not None:
                sender.close()
            if cap is not None:
                cap.release()
            if imu_source is not None:
                imu_source.close()
            if stream:
                stream.write(json.dumps({"event":"end", "valid":False, **summary})+"\n")
                stream.close()
            results.put(summary.copy())

    if not args.preview:
        capture()
        print(json.dumps(summary,indent=2))
        return 0 if summary.get("ok") else 2

    import tkinter as tk
    root = tk.Tk()
    root.title("PSVR - tracking LED sperimentale")
    root.geometry("960x960")
    status = tk.StringVar(value="Apertura camera...")
    detail = tk.StringVar(value="Mostra i cinque LED frontali; visore dritto e frontale per iniziare")
    tk.Label(root,textvariable=status,font=("Segoe UI",15,"bold"),wraplength=940).pack(pady=10)
    tk.Label(root,textvariable=detail,font=("Segoe UI",11),wraplength=940).pack()
    image = tk.Label(root)
    image.pack(pady=8)
    tk.Label(root,text="Giallo: identita LED | Verde: luce con alone blu, incluso nucleo bianco\n"
        "Aggancia da vicino e frontale; poi allontanati lentamente. Riquadro azzurro = ricerca locale.\n"
        + ("Driver IMU UDP; posizione v2 opt-in %s. Nessuna calibrazione HID locale." %
         ("ATTIVA" if send_position else "DISATTIVA") if driver_mode else
         "Modello geometrico approssimato, origine sul LED centrale: SteamVR non collegato."),
        font=("Segoe UI",11)).pack()
    tk.Button(root,text="AZZERA SPOSTAMENTO RELATIVO",command=recenter.set).pack(pady=10)
    if args.imu:
        tk.Label(root,text="IMU: premi AVVIA, resta FERMO per 8 secondi e attendi il tracking frontale prima di ruotare.",
            font=("Segoe UI",11,"bold")).pack()
        tk.Button(root,text="AVVIA IMU / RICALIBRA (VISORE FERMO)",command=start_imu.set).pack()
        tk.Button(root,text="RIALLINEA CAMERA / IMU (TORNA FRONTALE)",command=realign.set).pack()
    elif driver_mode:
        tk.Label(root,text="IMU dal driver (UDP): attendi export fresco e mostra i LED frontali.",
            font=("Segoe UI",11,"bold")).pack()
        tk.Button(root,text="RIALLINEA CAMERA / IMU (TORNA FRONTALE)",command=realign.set).pack()
    if send_position:
        selected_filter = tk.StringVar(value=initial_filter)
        def select_filter(value):
            with settings_lock:
                settings["position_filter"] = value
        tk.Label(root,text="Filtro posizione finale (Kalman sperimentale; cambio azzera lo stato)").pack()
        tk.OptionMenu(root,selected_filter,"one_euro","kalman",command=select_filter).pack()

    def set_threshold(key, value):
        with settings_lock:
            settings[key] = int(float(value))

    brightness = tk.Scale(root,from_=40,to=250,orient="horizontal",length=700,
        label="Luminosita minima: aumenta per ignorare oggetti blu poco luminosi",
        command=lambda value: set_threshold("value_min",value))
    brightness.set(args.value_min)
    brightness.pack()
    area = tk.Scale(root,from_=6,to=400,orient="horizontal",length=700,
        label="Area minima di aggancio: durante il tracking si adatta alla distanza",
        command=lambda value: set_threshold("min_area",value))
    area.set(args.min_area)
    area.pack()
    controls_frame = tk.Frame(root)
    controls_frame.pack()
    exposure = tk.Scale(controls_frame,from_=0,to=255,orient="horizontal",length=360,
        label="Esposizione: abbassa se i LED sono bianchi/bruciati",
        command=lambda value: set_threshold("exposure",value))
    exposure.set(args.exposure)
    exposure.grid(row=0,column=0,padx=8)
    gain = tk.Scale(controls_frame,from_=0,to=63,orient="horizontal",length=360,
        label="Guadagno: tieni basso per ridurre rumore e saturazione",
        command=lambda value: set_threshold("gain",value))
    gain.set(args.gain)
    gain.grid(row=0,column=1,padx=8)
    thread = threading.Thread(target=capture,daemon=True)
    thread.start()

    def tick():
        try:
            ppm,text,extra = frames.get_nowait()
            photo = tk.PhotoImage(data=ppm,format="PPM")
            image.configure(image=photo)
            image.image = photo
            status.set(text)
            detail.set(extra)
        except queue.Empty:
            pass
        try:
            result = results.get_nowait()
            status.set("Sessione terminata" if result.get("ok") else result.get("error","Errore"))
            detail.set("Frame %d, validi %d | Log: %s" % (
                result["captured_frames"],result["valid_frames"],str(log_path)))
        except queue.Empty:
            pass
        root.after(40,tick)

    root.protocol("WM_DELETE_WINDOW",lambda: (stop.set(),root.destroy()))
    root.after(40,tick)
    root.mainloop()
    thread.join(timeout=10)
    return 0 if summary.get("ok") else 2
