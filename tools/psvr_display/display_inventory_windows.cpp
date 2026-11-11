// SPDX-License-Identifier: MIT
#ifndef _WIN32
#error This backend requires Windows.
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include "display_inventory.hpp"
#include <stdexcept>
#include <utility>

namespace psvr::display {
namespace {
constexpr UINT32 max_paths = 16384, max_modes = 32768;
constexpr DWORD max_available_modes = 4096;

[[noreturn]] void api_error(const char* api, LONG error) {
    throw std::runtime_error(std::string(api) + " failed (Windows error " +
                             std::to_string(error) + ")");
}
AdapterId adapter_id(LUID id) { return {id.LowPart, id.HighPart}; }
RefreshRate rate(DISPLAYCONFIG_RATIONAL r) { return {r.Numerator, r.Denominator}; }
template<std::size_t N> std::string utf8(const WCHAR (&text)[N]) {
    std::size_t length = 0;
    while (length < N && text[length]) ++length;
    if (length == N) throw std::runtime_error("Unterminated Windows display name");
    if (!length) return {};
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text,
        static_cast<int>(length), nullptr, 0, nullptr, nullptr);
    if (!size) api_error("WideCharToMultiByte", GetLastError());
    std::string result(static_cast<std::size_t>(size), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text,
        static_cast<int>(length), result.data(), size, nullptr, nullptr))
        api_error("WideCharToMultiByte", GetLastError());
    return result;
}
template<class T> bool device_info(T& info, DISPLAYCONFIG_DEVICE_INFO_TYPE type,
                                 LUID adapter, UINT32 id, Display& display) {
    info = {};
    info.header.type = type;
    info.header.size = sizeof(T);
    info.header.adapterId = adapter;
    info.header.id = id;
    const LONG error = DisplayConfigGetDeviceInfo(&info.header);
    if (error == ERROR_SUCCESS) return true;
    display.warnings.push_back("DisplayConfigGetDeviceInfo type " +
        std::to_string(static_cast<int>(type)) + " failed: " + std::to_string(error));
    return false;
}
void available_modes(Display& d, const WCHAR* gdi_name) {
    for (DWORD i = 0; i < max_available_modes; ++i) {
        DEVMODEW dm{};
        dm.dmSize = sizeof(dm);
        if (!EnumDisplaySettingsExW(gdi_name, i, &dm, 0)) return;
        constexpr DWORD fields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL |
            DM_DISPLAYFREQUENCY | DM_DISPLAYORIENTATION | DM_DISPLAYFLAGS;
        AvailableMode m;
        m.fields_complete = (dm.dmFields & fields) == fields;
        m.width = (dm.dmFields & DM_PELSWIDTH) ? dm.dmPelsWidth : 0;
        m.height = (dm.dmFields & DM_PELSHEIGHT) ? dm.dmPelsHeight : 0;
        m.bits_per_pixel = (dm.dmFields & DM_BITSPERPEL) ? dm.dmBitsPerPel : 0;
        // 0 and 1 mean hardware/default rate, not a known physical refresh.
        if ((dm.dmFields & DM_DISPLAYFREQUENCY) && dm.dmDisplayFrequency > 1)
            m.refresh = {dm.dmDisplayFrequency, 1};
        m.orientation = (dm.dmFields & DM_DISPLAYORIENTATION) ? dm.dmDisplayOrientation : 0;
        m.interlaced = (dm.dmFields & DM_DISPLAYFLAGS) && (dm.dmDisplayFlags & DM_INTERLACED);
        d.available_modes.push_back(m);
    }
    d.warnings.emplace_back("Available-mode enumeration reached safety limit; list truncated");
}
}

std::vector<Display> enumerate_displays(bool include_available_modes) {
    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    bool complete = false;
    // Topology can change between sizing and query; bound retries and allocations.
    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        UINT32 path_count = 0, mode_count = 0;
        LONG error = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &path_count, &mode_count);
        if (error == ERROR_INSUFFICIENT_BUFFER) continue;
        if (error != ERROR_SUCCESS) api_error("GetDisplayConfigBufferSizes", error);
        if (path_count > max_paths || mode_count > max_modes)
            throw std::runtime_error("Display topology exceeds safety limits");
        if (path_count == 0) return {};
        // Keep pointers non-null even if Windows reports zero modes.
        paths.resize(path_count);
        modes.resize(mode_count ? mode_count : 1);
        error = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &path_count, paths.data(),
                                   &mode_count, modes.data(), nullptr);
        if (error == ERROR_INSUFFICIENT_BUFFER) continue;
        if (error != ERROR_SUCCESS) api_error("QueryDisplayConfig", error);
        if (path_count > paths.size() || mode_count > modes.size())
            throw std::runtime_error("Windows returned invalid display buffer counts");
        paths.resize(path_count);
        modes.resize(mode_count);
        complete = true;
        break;
    }
    if (!complete) throw std::runtime_error("Display topology did not stabilize after 8 queries");

    std::vector<Display> result;
    result.reserve(paths.size());
    for (const auto& path : paths) {
        Display d;
        const auto& s = path.sourceInfo;
        const auto& t = path.targetInfo;
        d.active = (path.flags & DISPLAYCONFIG_PATH_ACTIVE) != 0;
        d.source_adapter = adapter_id(s.adapterId);
        d.target_adapter = adapter_id(t.adapterId);
        d.source_id = s.id;
        d.target_id = t.id;
        d.target_available = t.targetAvailable != FALSE;
        d.rotation = static_cast<std::uint32_t>(t.rotation);
        d.scanline_ordering = static_cast<std::uint32_t>(t.scanLineOrdering);
        d.scaling = static_cast<std::uint32_t>(t.scaling);
        d.output_technology = static_cast<std::uint32_t>(t.outputTechnology);
        d.refresh = rate(t.refreshRate);
        if (s.modeInfoIdx < modes.size()) {
            const auto& m = modes[s.modeInfoIdx];
            if (m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE && m.id == s.id &&
                adapter_id(m.adapterId) == d.source_adapter)
                d.source = SourceGeometry{m.sourceMode.position.x, m.sourceMode.position.y,
                                          m.sourceMode.width, m.sourceMode.height};
        }
        if (!d.source) d.warnings.emplace_back("Source geometry unavailable or inconsistent");
        if (t.modeInfoIdx < modes.size()) {
            const auto& m = modes[t.modeInfoIdx];
            if (m.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_TARGET && m.id == t.id &&
                adapter_id(m.adapterId) == d.target_adapter) {
                const auto& signal = m.targetMode.targetVideoSignalInfo;
                d.target = TargetGeometry{signal.activeSize.cx, signal.activeSize.cy,
                    rate(signal.vSyncFreq), static_cast<std::uint32_t>(signal.scanLineOrdering)};
            }
        }
        if (!d.target) d.warnings.emplace_back("Target scanout geometry unavailable or inconsistent");

        DISPLAYCONFIG_SOURCE_DEVICE_NAME source_name{};
        if (device_info(source_name, DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, s.adapterId, s.id, d)) {
            const auto name = utf8(source_name.viewGdiDeviceName);
            if (!name.empty()) {
                d.source_gdi_name = name;
                if (include_available_modes) {
                    available_modes(d, source_name.viewGdiDeviceName);
                    if (d.available_modes.empty()) d.warnings.emplace_back("No available GDI modes returned");
                }
            } else d.warnings.emplace_back("Empty GDI source name; modes not queried");
        }
        DISPLAYCONFIG_TARGET_DEVICE_NAME target_name{};
        if (device_info(target_name, DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME, t.adapterId, t.id, d)) {
            const auto name = utf8(target_name.monitorFriendlyDeviceName);
            const auto device_path = utf8(target_name.monitorDevicePath);
            if (!name.empty()) d.friendly_name = name;
            if (!device_path.empty()) d.monitor_device_path = device_path;
            d.edid_ids = EdidIds{target_name.edidManufactureId, target_name.edidProductCodeId,
                                target_name.flags.edidIdsValid != 0};
        }
        DISPLAYCONFIG_ADAPTER_NAME adapter_name{};
        if (device_info(adapter_name, DISPLAYCONFIG_DEVICE_INFO_GET_ADAPTER_NAME, t.adapterId, 0, d)) {
            const auto name = utf8(adapter_name.adapterDevicePath);
            if (!name.empty()) d.adapter_name = name;
        }
        if (d.source_adapter == d.target_adapter) d.source_adapter_name = d.adapter_name;
        else if (device_info(adapter_name, DISPLAYCONFIG_DEVICE_INFO_GET_ADAPTER_NAME, s.adapterId, 0, d)) {
            const auto name = utf8(adapter_name.adapterDevicePath);
            if (!name.empty()) d.source_adapter_name = name;
        }
        // Minimum EDID contract only: never guess a registry monitor from a substring.
        d.warnings.emplace_back("Raw EDID bytes not queried; IDs are Windows EDID-reported values");
        if (d.target && d.target->width == 1920 && d.target->height == 1080)
            d.candidate_hint = "1920x1080 scanout only; not proof of PSVR identity or suitability";
        result.push_back(std::move(d));
    }
    return result;
}
} // namespace psvr::display
