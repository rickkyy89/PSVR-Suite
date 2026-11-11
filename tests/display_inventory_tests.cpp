// SPDX-License-Identifier: MIT
#include "display_inventory.hpp"
#include <nlohmann/json.hpp>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>

using namespace psvr::display;
namespace {
unsigned checks = 0;
void check(bool condition, const char* name) {
    ++checks;
    if (!condition) throw std::runtime_error(name);
}
Display fixture() {
    Display d;
    d.active = d.target_available = true;
    d.source_adapter = d.target_adapter = {123, -7};
    d.source_id = 4;
    d.target_id = 5;
    d.monitor_device_path = "exact-monitor-path";
    d.source = SourceGeometry{-1920, 10, 1920, 1080};
    d.target = TargetGeometry{1920, 1080, {60000, 1001}, 1};
    d.refresh = {60000, 1001};
    d.rotation = d.scanline_ordering = 1;
    d.scaling = 1;
    return d;
}
SelectionResult select(const Display& d, ExtendedRequirements r = {}) {
    return validate_extended_display({d}, "exact-monitor-path", r);
}
void rejects(const Display& d, SelectionError e, const char* name) {
    const auto result = select(d);
    check(result.error == e && !result.index && !result, name);
}
}
int main() {
    try {
        const auto good = fixture();
        auto result = select(good);
        check(bool(result) && result.index == 0, "valid extended display");
        check(bool(select(good, {1920, 1080})), "required dimensions");
        check(select(good, {1280, 720}).error == SelectionError::geometry_mismatch, "wrong dimensions");
        check(select(good, {1920, 0}).error == SelectionError::invalid_geometry, "partial requirements");
        check(validate_extended_display({good}, "").error == SelectionError::empty_path, "empty path");
        check(validate_extended_display({}, "path").error == SelectionError::missing, "empty inventory");
        check(validate_extended_display({good}, "EXACT-monitor-path").error == SelectionError::missing, "exact case");
        check(validate_extended_display({good}, "exact-monitor").error == SelectionError::missing, "no substring");
        auto d = good; d.monitor_device_path.reset(); rejects(d, SelectionError::missing, "missing device info");
        d = good; d.active = false; rejects(d, SelectionError::inactive, "inactive");
        check(validate_extended_display({good, good}, "exact-monitor-path").error == SelectionError::duplicate, "duplicate");
        d = good; d.active = false;
        check(validate_extended_display({good, d}, "exact-monitor-path").error == SelectionError::duplicate, "inactive duplicate");
        d = good; d.monitor_device_path = "other"; d.target_id = 8;
        check(validate_extended_display({good, d}, "exact-monitor-path").error == SelectionError::clone_shared_source, "clone");
        d.active = false;
        check(bool(validate_extended_display({good, d}, "exact-monitor-path")), "inactive clone ignored");
        d.active = true; d.source_adapter.high = 8;
        check(bool(validate_extended_display({good, d}, "exact-monitor-path")), "source id on other adapter");
        d = good; d.target_available = false; rejects(d, SelectionError::unavailable_geometry, "target unavailable");
        d = good; d.source.reset(); rejects(d, SelectionError::unavailable_geometry, "source missing");
        d = good; d.target.reset(); rejects(d, SelectionError::unavailable_geometry, "target missing");
        for (const auto rotation : {0u, 2u, 3u, 4u, 99u}) {
            d = good; d.rotation = rotation; rejects(d, SelectionError::rotated, "rotation");
        }
        for (const auto scan : {0u, 2u, 3u, 99u}) {
            d = good; d.scanline_ordering = scan;
            rejects(d, SelectionError::interlaced_or_unknown, "path scanline");
            d = good; d.target->scanline_ordering = scan;
            rejects(d, SelectionError::interlaced_or_unknown, "signal scanline");
        }
        d = good; d.source->width = 0; rejects(d, SelectionError::invalid_geometry, "zero geometry");
        d = good; d.target->height = 0; rejects(d, SelectionError::invalid_geometry, "zero scanout");
        d = good; d.target->width = 1280; rejects(d, SelectionError::geometry_mismatch, "scaled geometry");
        d = good; d.refresh.denominator = 0; rejects(d, SelectionError::invalid_refresh, "zero denominator");
        d = good; d.target->signal_refresh.numerator = 0; rejects(d, SelectionError::invalid_refresh, "zero signal refresh");
        d = good; d.target->signal_refresh = {60,1}; rejects(d,SelectionError::invalid_refresh,"path/signal timing mismatch");
        for (const auto scaling : {0u,2u,3u,4u,5u,128u,999u}) {
            d = good; d.scaling = scaling; rejects(d,SelectionError::scaled,"nonidentity scaling rejected");
        }
        check(!refresh_hz({0, 1}) && !refresh_hz({60, 0}), "unknown Hz");
        check(std::abs(*refresh_hz({60000, 1001}) - 59.94005994005994) < 1e-10, "fractional Hz");
        check(same_refresh({60000, 1001}, {120000, 2002}), "equivalent rational");
        check(!same_refresh({60000, 1001}, {60, 1}), "59.94 is not 60");
        check(!same_refresh({0, 0}, {0, 0}), "invalid rationals not equal");
        const auto max = std::numeric_limits<std::uint32_t>::max();
        check(same_refresh({max, max}, {1, 1}), "overflow-safe equality");
        check(!same_refresh({max, max - 1}, {max - 1, max}), "overflow-safe inequality");
        AvailableMode mode{1920, 1080, 32, {60, 1}, 0, false, true};
        check(valid_mode(mode), "valid mode");
        check(supports_mode({mode}, 1920, 1080, {120, 2}), "mode equivalent rate");
        check(!supports_mode({mode}, 1920, 1080, {60000, 1001}), "no approximate rate inference");
        check(!supports_mode({mode}, 1280, 720, {60, 1}), "mode geometry mismatch");
        mode.interlaced = true;
        check(!supports_mode({mode}, 1920, 1080, {60, 1}), "interlaced mode rejected");
        mode.interlaced = false; mode.orientation = 1;
        check(!supports_mode({mode}, 1920, 1080, {60, 1}), "rotated mode rejected");
        mode.orientation = 99; check(!valid_mode(mode), "invalid orientation");
        mode.orientation = 0; mode.fields_complete = false; check(!valid_mode(mode), "missing DEVMODE fields");
        mode.fields_complete = true; mode.refresh = {0, 0}; check(!valid_mode(mode), "default mode frequency");
        mode.refresh = {60, 1}; mode.width = 0; check(!valid_mode(mode), "mode zero width");
        mode.width = 1920; mode.bits_per_pixel = 0; check(!valid_mode(mode), "mode no bpp");
        check(!supports_mode({}, 1920, 1080, {60, 1}), "empty modes");
        const auto j = inventory_json({good});
        check(j["schema_version"] == 1 && j["read_only"] == true && j["automatic_selection"] == false, "schema contract");
        check(j["displays"][0]["source"]["x"] == -1920, "signed desktop origin");
        check(j["displays"][0]["source_adapter"]["high"] == -7, "signed LUID high");
        check(j["displays"][0]["refresh"]["denominator"] == 1001, "rational JSON");
        check(j["displays"][0]["edid_ids"].is_null() && j["displays"][0]["edid_ids_available"] == false, "EDID unavailable");
        check(j["displays"][0]["edid_bytes"].is_null(), "raw EDID unavailable");
        check(j["displays"][0]["edid_bytes_available"] == false, "explicit raw EDID availability");
        d = good; d.edid_ids = EdidIds{0, 0, false};
        check(inventory_json({d})["displays"][0]["edid_ids_available"] == false, "invalid EDID flag");
        d.edid_ids = EdidIds{0x1234, 0x5678, true};
        check(inventory_json({d})["displays"][0]["edid_ids_available"] == true, "valid EDID flag");
        d = Display{};
        const auto missing = inventory_json({d})["displays"][0];
        check(missing["source"].is_null() && missing["target"].is_null() && missing["refresh"]["hz"].is_null(), "malformed JSON snapshot");
        check(inventory_json({})["displays"].empty(), "empty JSON inventory");
        check(nlohmann::json::parse(j.dump()) == j, "JSON roundtrip");
        check(std::string(selection_error_name(SelectionError::clone_shared_source)) == "clone_shared_source", "error name");
        std::cout << checks << " synthetic checks passed; no Windows enumeration invoked.\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Failed after " << checks << " checks: " << e.what() << '\n';
        return 1;
    }
}
