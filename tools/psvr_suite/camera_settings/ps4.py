"""PS4 profile projection; explicit legacy reopen uses numeric-only verification."""
import copy
import math
from types import MappingProxyType

from .. import i18n
from .common import CameraModule, Control, current_controls, text, native_manual_range


class Ps4Module(CameraModule):
    source_id = "ps4-stereo"
    # Never advertise a device range until read by its owner. Profile bounds
    # are a separate policy, not a substitute for the actual GetRange result.
    controls = MappingProxyType({"exposure": Control(None, None, device_range_required=True),
                                 "gain": Control(None, None, device_range_required=True),
                                 "brightness": Control(None, None, device_range_required=True)})

    def view(self, root, requested, status):
        view = self.empty_view()
        try:
            from tools.psvr_camera.ps4_suite_source import load_suite_profile
            profile = load_suite_profile(root)
        except Exception as exc:
            view["error"] = str(exc)
            return view
        view["profile"] = copy.deepcopy(profile)
        view["requested"] = copy.deepcopy(profile["controls"])
        saved = requested.get("camera_profiles", {})
        if not isinstance(saved, dict):
            view["error"] = "camera_profiles: object required"
            return view
        overrides = saved.get(self.source_id)
        if overrides is not None:
            try:
                overrides = self.validate_saved_configuration(overrides)
            except ValueError as exc:
                view["error"] = str(exc)
                return view
            view["requested"].update(overrides["controls"])
        view["effective_config"] = {key: profile[key] for key in ("width", "height", "fps")}
        view["effective_config"].update(copy.deepcopy(profile["detector"]))
        view["effective_config"].update(copy.deepcopy(view["requested"]))
        view["effective_config"].update(intrinsicsPath=profile["calibration_path"],
                                        rigPath=profile["rig_path"], imu_rig=None,
                                        eye_order=list(profile["eye_order"]))
        view["camera_controls"] = current_controls(status, self.source_id)
        view["capabilities"] = self.editing_capabilities(status)
        view["restore"] = copy.deepcopy(status.get("camera_restore")) if isinstance(status, dict) else None
        view["edit"] = copy.deepcopy(status.get("control_edit")) if isinstance(status, dict) else None
        return view

    def refresh_status(self, view, status):
        updated = super().refresh_status(view, status)
        updated["restore"] = copy.deepcopy(status.get("camera_restore")) if isinstance(status, dict) else None
        updated["edit"] = copy.deepcopy(status.get("control_edit")) if isinstance(status, dict) else None
        return updated

    def validate_saved_configuration(self, values):
        if (not isinstance(values, dict) or set(values) != {"controls"}
                or not isinstance(values["controls"], dict)
                or set(values["controls"]) - set(self.controls)):
            raise ValueError("PS4 camera profile: only native controls overrides supported")
        controls = values["controls"]
        from tools.psvr_camera.ps4_suite_source import _profile_controls_valid
        candidate = {"brightness": 0, "exposure": None, "gain": None}
        candidate.update(controls)
        if (any(type(v) is not int for v in controls.values())
                or not _profile_controls_valid(candidate)):
            raise ValueError("unsupported PS4 saved control targets (not device confirmation)")
        return {"controls": dict(controls)}

    def validate_controls(self, values, device_ranges=None):
        """Preparation for explicit reopen editing, NOT permission to apply.

        Exposure/gain validation requires a measured native range with Manual
        capability and a valid step. No PS3 units or assumed device bounds.
        """
        if not isinstance(values, dict) or set(values) - set(self.controls):
            raise ValueError("unsupported PS4 controls")
        for name, value in values.items():
            if type(value) is not int:
                raise ValueError(f"invalid PS4 {name}: integer required")
            spec = self.controls[name]
            if spec.device_range_required or name == "brightness":
                info = device_ranges.get(name) if isinstance(device_ranges, dict) else None
                low, high, step = native_manual_range(info)
                if not low <= value <= high or (value - low) % step:
                    raise ValueError(f"PS4 {name}: unsupported Manual target")
            elif not spec.minimum <= value <= spec.maximum:
                raise ValueError(f"invalid PS4 {name}")
        # Also honor the acquisition profile's policy validator, independently
        # of hardware support. Reuse it rather than duplicating its bounds.
        from tools.psvr_camera.ps4_suite_source import _profile_controls_valid
        candidate = {"brightness": 0, "exposure": None, "gain": None}
        candidate.update(values)
        if not _profile_controls_valid(candidate):
            raise ValueError("unsupported PS4 profile controls")
        return dict(values)

    def render(self, view, lang):
        def tr(key, **kwargs):
            return i18n.t(lang, key, **kwargs)

        if view["error"] is not None:
            return tr("camera.profile_error", errore=view["error"])
        profile = view["profile"]
        controls = view["requested"]
        caps = view.get("capabilities")
        lines = [tr("camera.ps4_controls_ready", mode=caps["apply_mode"]) if caps
                 else tr("camera.ps4_readonly"),
                 tr("camera.profile_target", path=profile["profile_path"]),
                 tr("camera.profile_format", width=profile["width"], height=profile["height"],
                    fps=profile["fps"]),
                 tr("camera.profile_controls",
                    exposure=(text(controls["exposure"], lang) if controls["exposure"] is not None
                              else tr("camera.owner_selected")),
                    gain=(text(controls["gain"], lang) if controls["gain"] is not None
                          else tr("camera.owner_selected")), brightness=controls["brightness"]),
                 tr("camera.profile_artifacts", calibration=profile["calibration_path"],
                    rig=profile["rig_path"], eyes=" / ".join(profile["eye_order"])),
                 tr("camera.profile_detector", **profile["detector"]),
                 tr("camera.profile_imu_rig")]
        # Always visible for the selected PS4 source, including disconnected or
        # transitioning states; never let the warning depend on write success.
        lines.append(tr("camera.ps4_legacy_warning"))
        observed = view["camera_controls"]
        if observed is None:
            lines.append(tr("camera.readback_unavailable"))
        else:
            lines.append(tr("camera.readback_heading"))
            readback, confirmed = observed.get("readback"), observed.get("readback_confirmed")
            for name in ("exposure", "gain", "brightness"):
                value = readback.get(name) if isinstance(readback, dict) else None
                readable = type(value) in (int, float) and math.isfinite(value)
                flag = confirmed.get(name) if isinstance(confirmed, dict) else None
                confirmation = flag if readable and type(flag) is bool else None
                lines.append(tr("camera.readback_value", control=name,
                                value=text(value if readable else None, lang),
                                confirmed=text(confirmation, lang)))
            native = observed.get("native_exposure")
            limits = native.get("range") if isinstance(native, dict) else None
            if isinstance(limits, dict):
                lines.append(tr("camera.device_exposure_range",
                                minimum=text(limits.get("minimum"), lang),
                                maximum=text(limits.get("maximum"), lang),
                                 step=text(limits.get("step"), lang)))
            legacy = observed.get("legacy_opencv")
            if isinstance(legacy, dict):
                lines.append(tr("camera.ps4_legacy_result", state=observed.get("transaction", {}).get("state", "not_requested")))
                if legacy.get("error"):
                    lines.append(str(legacy["error"]))
                transaction_error = observed.get("transaction", {}).get("error")
                if transaction_error:
                    lines.append(str(transaction_error))
        restore = view.get("restore")
        edit = view.get("edit")
        if isinstance(edit, dict) and edit.get("state") in ("rejected", "failed"):
            lines.append(str(edit.get("error") or edit["state"]))
        if isinstance(restore, dict) and restore.get("source") == self.source_id:
            legacy = restore.get("legacy_opencv", {})
            lines.append(tr("camera.ps4_legacy_restore", state=legacy.get("restore_state", "unconfirmed"),
                pending=text(restore.get("cleanup_pending"), lang)))
            for name, flag in legacy.get("restore_readback_confirmed", {}).items():
                lines.append(tr("camera.readback_value", control="restore " + name,
                    value=text(restore.get("restored", {}).get(name), lang), confirmed=text(flag, lang)))
            if restore.get("error"):
                lines.append(str(restore["error"]))
        return "\n\n".join(lines)
