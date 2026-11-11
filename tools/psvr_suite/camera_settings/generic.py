"""Unknown/generic cameras must never inherit PS3 control units or capabilities."""
from .. import i18n
from .common import CameraModule
import copy
from types import MappingProxyType


class GenericModule(CameraModule):
    configuration_fields = MappingProxyType({"width": "int", "height": "int", "fps": "int",
                                             "intrinsicsPath": "path", "rigPath": "path",
                                             "imu_rig": "path"})
    def __init__(self, source_id):
        self.source_id = source_id

    def view(self, root, requested, status):
        view = self.empty_view()
        saved = requested.get("camera_profiles", {})
        if not isinstance(saved, dict):
            view["error"] = "camera_profiles: object required"
            return view
        if self.source_id is None:
            view["error"] = "camera.source_unknown"
        # Legacy config remains owned by the PS3 draft. Do not present its
        # calibration/controls as an effective profile for an arbitrary camera.
        elif self.source_id in saved:
            try:
                config = self.validate_saved_configuration(saved[self.source_id])
                view["requested"] = copy.deepcopy(config)
                view["effective_config"] = copy.deepcopy(config)
                view["effective_config"].update(exposure=None, gain=None, brightness=None)
            except ValueError as exc:
                view["error"] = str(exc)
        else:
            view["error"] = "camera.generic_configuration_required"
        return view

    def validate_saved_configuration(self, values):
        keys = {"width", "height", "fps", "intrinsicsPath", "rigPath", "imu_rig"}
        if not isinstance(values, dict) or set(values) != keys:
            raise ValueError("generic source requires its own format/calibration/rig configuration")
        for key in ("width", "height", "fps"):
            if type(values[key]) is not int or values[key] < 1:
                raise ValueError(f"invalid generic {key}")
        for key in ("intrinsicsPath", "rigPath", "imu_rig"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"generic source requires dedicated {key}")
        return copy.deepcopy(values)

    def render(self, view, lang):
        heading = i18n.t(lang, "camera.source_unknown" if self.source_id is None
                         else "camera.generic_readonly")
        if view["error"] not in (None, "camera.source_unknown", "camera.generic_configuration_required"):
            return heading + "\n\n" + i18n.t(lang, "action.prefs_invalid", dettagli=view["error"])
        if view["effective_config"]:
            return heading + "\n\n" + i18n.t(lang, "camera.generic_configuration",
                                              **view["effective_config"])
        if self.source_id is not None:
            return heading + "\n\n" + i18n.t(lang, "camera.generic_configuration_required")
        return heading
