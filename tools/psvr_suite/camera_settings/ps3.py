"""PS3 Eye's legacy draft and sensor units; independent of PS4 profiles."""
import copy
from types import MappingProxyType

from .. import i18n
from .common import CameraModule, Control, current_controls


class Ps3Module(CameraModule):
    source_id = "pseye"
    editable = True
    legacy_fields = True
    # The one authoritative legacy baseline, not UI handler targets. Loaded user
    # preferences always override it; relocation changes no accepted values.
    baseline = MappingProxyType({
        "exposure": 13, "gain": 0, "width": 640, "height": 480, "fps": 75,
        "intrinsicsPath": "configurations/pseye_blue_640x480_calibrated.json",
        "rigPath": "configurations/psvr_front_leds_refined_20261011.json",
        "imu_rig": "configurations/psvr_nine_leds_refined_sides_20261011.json",
        "value_min": 60, "saturation_min": 90, "min_area": 6, "opening_size": 0})
    controls = MappingProxyType({"exposure": Control(0, 255, editable=True),
                                 "gain": Control(0, 63, editable=True)})
    legacy_keys = ("exposure", "gain", "width", "height", "fps", "intrinsicsPath",
                   "rigPath", "imu_rig", "value_min", "saturation_min",
                   "min_area", "opening_size")
    legacy_bindings = MappingProxyType({
        "exposure": "var_exposure", "gain": "var_gain", "width": "var_width",
        "height": "var_height", "fps": "var_fps", "intrinsicsPath": "var_intrinsics",
        "rigPath": "var_rig", "imu_rig": "var_imu_rig", "value_min": "var_value_min",
        "saturation_min": "var_saturation", "min_area": "var_min_area",
        "opening_size": "var_opening"})

    def default_configuration(self):
        return copy.deepcopy(dict(self.baseline))

    def view(self, root, requested, status):
        view = self.empty_view()
        # Non-destructive legacy projection. Even invalid, unsaved text is
        # preserved exactly; the existing collect/apply validator rejects it.
        view["requested"] = copy.deepcopy({key: requested[key]
                                           for key in self.legacy_keys if key in requested})
        view["effective_config"] = copy.deepcopy(view["requested"])
        view["camera_controls"] = current_controls(status, self.source_id)
        return view

    def validate_controls(self, values, device_ranges=None):
        if not isinstance(values, dict) or set(values) - set(self.controls):
            raise ValueError("unsupported PS3 controls")
        for name, value in values.items():
            spec = self.controls[name]
            if type(value) is not int or not spec.minimum <= value <= spec.maximum:
                raise ValueError(f"invalid PS3 {name}: {spec.minimum}..{spec.maximum}")
        return dict(values)

    def render(self, view, lang):
        return i18n.t(lang, "camera.legacy_requested",
                      exposure_min=self.controls["exposure"].minimum,
                      exposure_max=self.controls["exposure"].maximum,
                      gain_min=self.controls["gain"].minimum,
                      gain_max=self.controls["gain"].maximum)
