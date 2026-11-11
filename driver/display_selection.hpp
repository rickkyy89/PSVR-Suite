// SPDX-License-Identifier: MIT
#pragma once
#include "display_component.hpp"
#include "display_inventory.hpp"
#include <atomic>

namespace psvr {
// Snapshot integration gate shared with synthetic tests. Never falls back to GDI
// source ordinal, primary monitor, EDID hint, or a matching screen size.
// Native mode checks contradictions only: zero matches is NOT evidence of
// desktop absence. B9-01: do not treat this predicate as native authorization;
// see native_desktop_absence_verified and docs/NATIVE_DIRECT_MODE_20261005.md.
inline bool matches_display_config(const DisplayConfig& config,
                                   const std::vector<display::Display>& inventory) {
    if (!config.enabled || config.validation_error()) return false;
    if (config.native_direct_mode) {
        // SteamVR can hide the display BEFORE loading the driver. In this path
        // the GPU/runtime owns video availability; desktop absence is expected,
        // not verification of direct scanout. Reject contradictory visible IDs
        // and ambiguous EDID matches instead of invalidating the expected hide.
        std::size_t id_matches = 0;
        for (const auto& item : inventory) {
            const bool same_ids = item.edid_ids && item.edid_ids->valid &&
                item.edid_ids->manufacturer == config.edid_vendor_id &&
                item.edid_ids->product == config.edid_product_id;
            if (same_ids) ++id_matches;
            if (item.monitor_device_path && *item.monitor_device_path == config.monitor_path && !same_ids)
                return false;
            if (same_ids && (!item.monitor_device_path || *item.monitor_device_path != config.monitor_path))
                return false;
        }
        return id_matches <= 1;
    }
    const auto selection = display::validate_extended_display(inventory,config.monitor_path,
        {config.panel_width,config.panel_height});
    if (!selection || !selection.index) return false;
    const auto& selected = inventory[*selection.index];
    return selected.source->x == config.desktop_x && selected.source->y == config.desktop_y &&
        display::same_refresh(selected.refresh,{config.refresh_numerator,config.refresh_denominator}) &&
        display::same_refresh(selected.target->signal_refresh,{config.refresh_numerator,config.refresh_denominator});
}

// Separate prerequisite for a future fail-closed native gate, NOT wired into
// matches_display_config yet. The callback must return false ONLY for confirmed
// absence of the exact monitor; present/unknown must return true or throw.
// The current Windows callback cannot guarantee this when target identity is
// unavailable. AND-ing it into the gate would still accept unknown as absent.
// This helper checks that callback contract only, not config/inventory validity
// or direct scanout. Missing callbacks and all query failures fail closed.
inline bool native_desktop_absence_verified(const DisplayConfig& config) noexcept {
    if (!config.native_direct_mode || !config.desktop_membership) return false;
    try { return !config.desktop_membership(); }
    catch (...) { return false; }
}

// Once invalidated, immutable desktop geometry cannot become valid again
// merely because a monitor appeared at the old coordinates. Restart required.
class DisplayTopologyGuard {
    std::atomic<bool> valid_{true};
public:
    bool update(bool matches) noexcept {
        if (!matches) valid_ = false;
        return valid_.load();
    }
    bool valid() const noexcept { return valid_.load(); }
};
} // namespace psvr
