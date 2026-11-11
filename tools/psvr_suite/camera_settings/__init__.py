"""Source-specific UI modules. Legacy preferences are projected, never migrated on disk."""
from .. import cameras
from .generic import GenericModule
from .ps3 import Ps3Module
from .ps4 import Ps4Module
import copy

PREFERENCES_KEY = "camera_profiles"


def validate_camera_profiles(profiles):
    """Optional, source-keyed saved targets; no device capability assumptions."""
    if not isinstance(profiles, dict):
        raise ValueError("camera_profiles: object required")
    checked = {}
    for source, values in profiles.items():
        if not cameras.is_valid_camera_source(source):
            raise ValueError(f"camera_profiles: unknown source {source!r}")
        if source == "pseye":
            raise ValueError("PS3 preferences use the legacy flat fields; no duplicate override")
        checked[source] = module_for_source(source).validate_saved_configuration(values)
    return copy.deepcopy(checked)


def resolve_camera_preferences(root, requested):
    """Backend integration surface: call AFTER canonical validation, BEFORE open.

    No writes/device calls. Effective native PS4 controls must NOT be fed back
    through the flat PS3 preference validator. The owner revalidates hardware
    range/capability at every explicit open/apply.
    """
    validate_camera_profiles(requested.get(PREFERENCES_KEY, {}))
    source = requested.get("camera_source")
    view = module_for_source(source).view(root, requested, {})
    if view["error"] is not None:
        raise ValueError(str(view["error"]))
    effective = copy.deepcopy(requested)
    effective.update(copy.deepcopy(view["effective_config"]))
    profile = copy.deepcopy(view["profile"])
    if profile is not None:
        overrides = requested.get(PREFERENCES_KEY, {}).get(source, {}).get("controls", {})
        if overrides:
            profile["profile_control_targets"] = copy.deepcopy(profile["controls"])
            profile["control_overrides"] = copy.deepcopy(overrides)
        profile["controls"] = copy.deepcopy(view["requested"])
    return effective, profile


def module_for_source(source_id):
    """Resolve only registered IDs; no unknown-source fallback to PS3 or PS4."""
    if not cameras.is_valid_camera_source(source_id):
        return GenericModule(None)
    if source_id == "pseye":
        return Ps3Module()
    if source_id == "ps4-stereo":
        return Ps4Module()
    return GenericModule(source_id)
