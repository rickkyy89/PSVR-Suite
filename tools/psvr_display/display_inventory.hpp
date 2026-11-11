// SPDX-License-Identifier: MIT
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>
#include <nlohmann/json_fwd.hpp>

namespace psvr::display {

struct AdapterId {
    std::uint32_t low = 0;
    std::int32_t high = 0;
};
bool operator==(const AdapterId& a, const AdapterId& b) noexcept;

struct RefreshRate {
    std::uint32_t numerator = 0;
    std::uint32_t denominator = 0;
};
bool valid_refresh(RefreshRate rate) noexcept;
std::optional<double> refresh_hz(RefreshRate rate) noexcept;
bool same_refresh(RefreshRate a, RefreshRate b) noexcept;

struct SourceGeometry {
    std::int32_t x = 0, y = 0;
    std::uint32_t width = 0, height = 0;
};
struct TargetGeometry {
    std::uint32_t width = 0, height = 0;
    RefreshRate signal_refresh; // Exact video-signal vSyncFreq, not rounded Hz.
    std::uint32_t scanline_ordering = 0; // DISPLAYCONFIG_SCANLINE_ORDERING.
};
struct AvailableMode {
    std::uint32_t width = 0, height = 0, bits_per_pixel = 0;
    RefreshRate refresh; // EnumDisplaySettingsEx only exposes integer Hz.
    std::uint32_t orientation = 0; // DEVMODE dmDisplayOrientation: 0 = identity.
    bool interlaced = false;
    bool fields_complete = false;
};
bool valid_mode(const AvailableMode& mode) noexcept;
// Matches reported GDI mode values only, not proof of fractional scanout support.
bool supports_mode(const std::vector<AvailableMode>& modes,
                   std::uint32_t width, std::uint32_t height,
                   RefreshRate refresh) noexcept;

struct EdidIds {
    std::uint16_t manufacturer = 0, product = 0;
    bool valid = false; // Windows edidIdsValid flag; zero is not availability.
};
struct Display {
    bool active = false;
    AdapterId source_adapter, target_adapter;
    std::uint32_t source_id = 0, target_id = 0;
    std::optional<std::string> source_gdi_name, monitor_device_path;
    std::optional<std::string> friendly_name, adapter_name, source_adapter_name;
    std::optional<EdidIds> edid_ids;
    std::optional<std::vector<std::uint8_t>> edid_bytes; // Not read by this backend.
    std::optional<SourceGeometry> source;
    std::optional<TargetGeometry> target;
    RefreshRate refresh; // Path target refreshRate, preserved as a rational.
    std::uint32_t rotation = 0; // DISPLAYCONFIG_ROTATION: 1 = identity.
    std::uint32_t scanline_ordering = 0; // 1 = progressive, 2/3 = interlaced.
    std::uint32_t scaling = 0, output_technology = 0;
    bool target_available = false;
    std::optional<std::string> candidate_hint; // Informational, never selection.
    std::vector<AvailableMode> available_modes;
    std::vector<std::string> warnings;
};

// Throws std::runtime_error on enumeration failure. Queries only; no setters.
std::vector<Display> enumerate_displays(bool include_available_modes = true);
nlohmann::json inventory_json(const std::vector<Display>& displays);

enum class SelectionError {
    none, empty_path, missing, duplicate, inactive, clone_shared_source,
    unavailable_geometry, rotated, interlaced_or_unknown, invalid_geometry,
    invalid_refresh, geometry_mismatch, scaled
};
struct ExtendedRequirements {
    // Both zero means any valid unscaled geometry. A partial pair is invalid.
    std::uint32_t width = 0, height = 0;
};
struct SelectionResult {
    SelectionError error = SelectionError::missing;
    std::optional<std::size_t> index;
    explicit operator bool() const noexcept { return error == SelectionError::none; }
};
const char* selection_error_name(SelectionError error) noexcept;
// Exact, case-sensitive path match; rejects clones, transforms, unknown timing.
// Pure validation of a snapshot, not a reservation against topology changes.
SelectionResult validate_extended_display(const std::vector<Display>& displays,
    const std::string& monitor_device_path, ExtendedRequirements required = {});

} // namespace psvr::display
