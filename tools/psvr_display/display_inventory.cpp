// SPDX-License-Identifier: MIT
#include "display_inventory.hpp"
#include <nlohmann/json.hpp>

namespace psvr::display {
bool operator==(const AdapterId& a, const AdapterId& b) noexcept {
    return a.low == b.low && a.high == b.high;
}
bool valid_refresh(RefreshRate rate) noexcept {
    return rate.numerator != 0 && rate.denominator != 0;
}
std::optional<double> refresh_hz(RefreshRate rate) noexcept {
    if (!valid_refresh(rate)) return std::nullopt;
    return static_cast<double>(rate.numerator) / rate.denominator;
}
bool same_refresh(RefreshRate a, RefreshRate b) noexcept {
    return valid_refresh(a) && valid_refresh(b) &&
        std::uint64_t(a.numerator) * b.denominator ==
        std::uint64_t(b.numerator) * a.denominator;
}
bool valid_mode(const AvailableMode& m) noexcept {
    return m.fields_complete && m.width && m.height && m.bits_per_pixel &&
        valid_refresh(m.refresh) && m.orientation <= 3;
}
bool supports_mode(const std::vector<AvailableMode>& modes, std::uint32_t w,
                   std::uint32_t h, RefreshRate rate) noexcept {
    if (!w || !h || !valid_refresh(rate)) return false;
    for (const auto& m : modes)
        if (valid_mode(m) && !m.interlaced && m.orientation == 0 &&
            m.width == w && m.height == h && same_refresh(m.refresh, rate)) return true;
    return false;
}
const char* selection_error_name(SelectionError e) noexcept {
    switch (e) {
#define PSVR_ERROR(name) case SelectionError::name: return #name
        PSVR_ERROR(none); PSVR_ERROR(empty_path); PSVR_ERROR(missing);
        PSVR_ERROR(duplicate); PSVR_ERROR(inactive); PSVR_ERROR(clone_shared_source);
        PSVR_ERROR(unavailable_geometry); PSVR_ERROR(rotated);
        PSVR_ERROR(interlaced_or_unknown); PSVR_ERROR(invalid_geometry);
        PSVR_ERROR(invalid_refresh); PSVR_ERROR(geometry_mismatch);
        PSVR_ERROR(scaled);
#undef PSVR_ERROR
    }
    return "unknown";
}
SelectionResult validate_extended_display(const std::vector<Display>& ds,
    const std::string& path, ExtendedRequirements req) {
    const auto fail = [](SelectionError e) { return SelectionResult{e, std::nullopt}; };
    if (path.empty()) return fail(SelectionError::empty_path);
    std::optional<std::size_t> found;
    // B9-08: exact, case-sensitive device-path match (std::string operator==).
    // The PS1 gate (display_core_selection.ps1) instead uses OrdinalIgnoreCase;
    // the difference is documented, not unified, pending senior decision.
    for (std::size_t i = 0; i < ds.size(); ++i) {
        if (ds[i].monitor_device_path && *ds[i].monitor_device_path == path) {
            if (found) return fail(SelectionError::duplicate);
            found = i;
        }
    }
    if (!found) return fail(SelectionError::missing);
    const auto& d = ds[*found];
    if (!d.active) return fail(SelectionError::inactive);
    for (std::size_t i = 0; i < ds.size(); ++i)
        if (i != *found && ds[i].active && ds[i].source_adapter == d.source_adapter &&
            ds[i].source_id == d.source_id) return fail(SelectionError::clone_shared_source);
    if (!d.target_available || !d.source || !d.target)
        return fail(SelectionError::unavailable_geometry);
    if (d.rotation != 1) return fail(SelectionError::rotated);
    if (d.scaling != 1) return fail(SelectionError::scaled);
    if (d.scanline_ordering != 1 || d.target->scanline_ordering != 1)
        return fail(SelectionError::interlaced_or_unknown);
    if (!d.source->width || !d.source->height || !d.target->width || !d.target->height)
        return fail(SelectionError::invalid_geometry);
    // Conservative HMD contract: no implicit scaling or desktop/scanout mismatch.
    if (d.source->width != d.target->width || d.source->height != d.target->height)
        return fail(SelectionError::geometry_mismatch);
    if (!same_refresh(d.refresh,d.target->signal_refresh))
        return fail(SelectionError::invalid_refresh);
    if ((req.width == 0) != (req.height == 0)) return fail(SelectionError::invalid_geometry);
    if (req.width && (d.target->width != req.width || d.target->height != req.height))
        return fail(SelectionError::geometry_mismatch);
    return {SelectionError::none, found};
}

namespace {
using nlohmann::json;
json adapter_json(AdapterId a) { return {{"low", a.low}, {"high", a.high}}; }
json rate_json(RefreshRate r) {
    json j = {{"numerator", r.numerator}, {"denominator", r.denominator},
              {"available", valid_refresh(r)}, {"hz", nullptr}};
    if (const auto hz = refresh_hz(r)) j["hz"] = *hz;
    return j;
}
template<class T> json optional_json(const std::optional<T>& value) {
    return value ? json(*value) : json(nullptr);
}
}
nlohmann::json inventory_json(const std::vector<Display>& displays) {
    json rows = json::array();
    for (const auto& d : displays) {
        json modes = json::array();
        for (const auto& m : d.available_modes)
            modes.push_back({{"width", m.width}, {"height", m.height},
                {"bits_per_pixel", m.bits_per_pixel}, {"refresh", rate_json(m.refresh)},
                {"orientation", m.orientation}, {"interlaced", m.interlaced},
                {"fields_complete", m.fields_complete}, {"valid", valid_mode(m)}});
        json row = {{"active", d.active}, {"source_adapter", adapter_json(d.source_adapter)},
            {"target_adapter", adapter_json(d.target_adapter)}, {"source_id", d.source_id},
            {"target_id", d.target_id}, {"source_gdi_name", optional_json(d.source_gdi_name)},
            {"monitor_device_path", optional_json(d.monitor_device_path)},
            {"friendly_name", optional_json(d.friendly_name)},
            {"adapter_name", optional_json(d.adapter_name)},
            {"source_adapter_name", optional_json(d.source_adapter_name)},
            {"edid_ids", nullptr}, {"edid_ids_available", d.edid_ids && d.edid_ids->valid},
            {"edid_bytes", optional_json(d.edid_bytes)},
            {"edid_bytes_available", d.edid_bytes.has_value()}, {"source", nullptr}, {"target", nullptr},
            {"refresh", rate_json(d.refresh)}, {"rotation", d.rotation},
            {"scanline_ordering", d.scanline_ordering}, {"scaling", d.scaling},
            {"output_technology", d.output_technology}, {"target_available", d.target_available},
            {"candidate_hint", optional_json(d.candidate_hint)},
            {"available_modes", modes}, {"warnings", d.warnings}};
        if (d.edid_ids) row["edid_ids"] = {{"manufacturer", d.edid_ids->manufacturer},
            {"product", d.edid_ids->product}, {"valid", d.edid_ids->valid}};
        if (d.source) row["source"] = {{"x", d.source->x}, {"y", d.source->y},
            {"width", d.source->width}, {"height", d.source->height}};
        if (d.target) row["target"] = {{"width", d.target->width}, {"height", d.target->height},
            {"signal_refresh", rate_json(d.target->signal_refresh)},
            {"scanline_ordering", d.target->scanline_ordering}};
        rows.push_back(std::move(row));
    }
    return {{"schema_version", 1}, {"read_only", true},
        {"scope", "active_paths"}, {"automatic_selection", false}, {"displays", rows}};
}
} // namespace psvr::display
