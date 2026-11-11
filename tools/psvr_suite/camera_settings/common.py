"""Pure camera UI contract; no Tk, device access, persistence or tracking writes."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import math
from types import MappingProxyType

from .. import i18n


@dataclass(frozen=True)
class Control:
    minimum: int | None
    maximum: int | None
    editable: bool = False
    device_range_required: bool = False


def text(value, lang):
    if value is None:
        return i18n.unknown(lang)
    if isinstance(value, bool):
        return i18n.bool_text(value, lang)
    if isinstance(value, float):
        return f"{value:.3f}" if math.isfinite(value) else i18n.unknown(lang)
    return str(value)


def current_controls(status, source):
    """Only current running source status can provide readback, never logs."""
    status = status if isinstance(status, dict) else {}
    prefs = status.get("prefs")
    if (status.get("running") is True and status.get("state") == "running"
            and isinstance(prefs, dict) and prefs.get("camera_source") == source
            # Requested prefs alone do not prove the acquiring device during
            # a reopen transition. Require an explicit current-source identity.
            and status.get("camera_source") == source
            and isinstance(status.get("camera_controls"), dict)):
        return copy.deepcopy(status["camera_controls"])
    return None


def native_manual_range(info):
    """Validate measured range/capability shape, independently of any target."""
    keys = ("minimum", "maximum", "step", "capabilities")
    if not isinstance(info, dict) or any(type(info.get(k)) is not int for k in keys):
        raise ValueError("measured device range required")
    from tools.psvr_camera.ps4_dshow_controls import MANUAL
    low, high, step = (info[k] for k in keys[:3])
    if low > high or step <= 0 or not info["capabilities"] & MANUAL:
        raise ValueError("invalid Manual device range/capability")
    return low, high, step


class CameraModule:
    """Each source owns config projection, control validation and rendering.

    `view` returns detached requested/effective_config/readback snapshots.
    Effective_config means configuration resolved for OPEN, NOT hardware truth.
    `validate_controls` is pure validation, never an apply API. Editing capability
    is explicit; a valid value alone cannot enable a read-only module's widgets.
    """
    source_id = None
    controls = MappingProxyType({})
    editable = False
    legacy_fields = False
    configuration_fields = MappingProxyType({})

    def empty_view(self):
        return {"source": self.source_id, "legacy": self.legacy_fields,
                "editable": self.editable, "requested": {},
                "effective_config": {}, "profile": None, "error": None,
                "camera_controls": None}

    def view(self, root, requested, status):
        raise NotImplementedError

    def render(self, view, lang):
        raise NotImplementedError

    def refresh_status(self, view, status):
        """Update observations without repeating profile/artifact IO per poll."""
        updated = copy.deepcopy(view)
        updated["camera_controls"] = (current_controls(status, self.source_id)
                                      if updated["error"] is None else None)
        updated["capabilities"] = self.editing_capabilities(status)
        return updated

    def validate_controls(self, values, device_ranges=None):
        raise ValueError("camera controls unavailable for this source")

    def validate_saved_configuration(self, values):
        raise ValueError("dedicated configuration unavailable for this source")

    def editing_capabilities(self, status):
        """Explicit acknowledgment from the backend; no pretend editable UI."""
        status = status if isinstance(status, dict) else {}
        prefs = status.get("prefs")
        caps = status.get("camera_settings_capabilities")
        if (status.get("running") is not True or status.get("state") != "running"
                or status.get("camera_source") != self.source_id
                or not isinstance(prefs, dict) or prefs.get("camera_source") != self.source_id
                or not isinstance(caps, dict) or caps.get("source") != self.source_id
                or caps.get("preference_key") != "camera_profiles"
                or caps.get("apply_mode") not in ("reopen", "live")
                or not isinstance(caps.get("controls"), dict)):
            return None
        return copy.deepcopy(caps)
