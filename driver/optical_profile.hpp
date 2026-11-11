// SPDX-License-Identifier: MIT
// Original offline profile loader; validation is NOT optical calibration.
#pragma once

#include "distortion_math.hpp"
#include <nlohmann/json.hpp>
#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace psvr::optics {

inline constexpr std::size_t max_profile_bytes = 65536;
inline constexpr std::size_t max_profile_depth = 16;

// Immutable owned snapshot: profile and mapper cannot drift after validation.
struct LoadedProfile {
    const std::string name;
    const distortion::Profile profile;
    const distortion::Mapper mapper;

    LoadedProfile(std::string name_value, const distortion::Profile& profile_value)
        : name(std::move(name_value)), profile(profile_value), mapper(validated(profile)) {}

private:
    static distortion::Mapper validated(const distortion::Profile& value) {
        const auto result = distortion::Mapper::create(value);
        if (!result) throw std::invalid_argument(
            "optical profile: invalid math (bounds or all-domain radial monotonicity)");
        return *result;
    }
};

namespace detail {
using Json = nlohmann::json;

inline void fail(const std::string& message) {
    throw std::invalid_argument("optical profile: " + message);
}

inline void exact_keys(const Json& value, std::initializer_list<const char*> keys,
                       const std::string& location) {
    if (!value.is_object()) fail(location + " must be an object");
    for (const char* key : keys)
        if (!value.contains(key)) fail(location + " missing key '" + key + "'");
    for (auto it = value.begin(); it != value.end(); ++it) {
        bool known = false;
        for (const char* key : keys) if (it.key() == key) known = true;
        if (!known) fail(location + " unknown key '" + it.key() + "'");
    }
}

inline double number(const Json& value, const std::string& location) {
    if (!value.is_number()) fail(location + " must be a number");
    const double result = value.get<double>();
    if (!std::isfinite(result)) fail(location + " must be finite");
    return result;
}

inline std::array<double, 2> pair(const Json& value, const std::string& location) {
    if (!value.is_array() || value.size() != 2) fail(location + " must be a two-number array");
    return {{number(value.at(0), location + "[0]"), number(value.at(1), location + "[1]")}};
}

inline void validate_name(const std::string& name) {
    // The JSON parser already validates UTF-8. Count Unicode scalar values, not
    // bytes, and reject both C0 and C1 control characters (including DEL).
    std::size_t count = 0;
    for (std::size_t i = 0; i < name.size();) {
        const auto lead = static_cast<unsigned char>(name[i++]);
        unsigned codepoint = lead;
        unsigned trailing = 0;
        if (lead >= 0xf0) { codepoint = lead & 7u; trailing = 3; }
        else if (lead >= 0xe0) { codepoint = lead & 15u; trailing = 2; }
        else if (lead >= 0xc0) { codepoint = lead & 31u; trailing = 1; }
        while (trailing--) codepoint = (codepoint << 6) | (static_cast<unsigned char>(name[i++]) & 63u);
        if (codepoint < 0x20 || (codepoint >= 0x7f && codepoint <= 0x9f))
            fail("name must not contain control characters");
        ++count;
    }
    if (count == 0 || count > 128) fail("name must contain 1..128 characters");
}

inline distortion::EyeProfile eye(const Json& value, const std::string& location) {
    exact_keys(value, {"center", "axis_scale", "polynomial", "rgb_scale"}, location);
    const auto center = pair(value.at("center"), location + ".center");
    const auto scale = pair(value.at("axis_scale"), location + ".axis_scale");
    const auto& polynomial = value.at("polynomial");
    const auto& rgb = value.at("rgb_scale");
    exact_keys(polynomial, {"a", "b", "c", "d"}, location + ".polynomial");
    exact_keys(rgb, {"red", "green", "blue"}, location + ".rgb_scale");
    distortion::EyeProfile result;
    result.center = {center[0], center[1]};
    result.scale_x = scale[0]; result.scale_y = scale[1];
    result.a = number(polynomial.at("a"), location + ".polynomial.a");
    result.b = number(polynomial.at("b"), location + ".polynomial.b");
    result.c = number(polynomial.at("c"), location + ".polynomial.c");
    result.d = number(polynomial.at("d"), location + ".polynomial.d");
    result.rgb = {{number(rgb.at("red"), location + ".rgb_scale.red"),
                   number(rgb.at("green"), location + ".rgb_scale.green"),
                   number(rgb.at("blue"), location + ".rgb_scale.blue")}};
    return result;
}
} // namespace detail

// Schema v1 is display-to-render: OpenVR ComputeDistortion receives an
// eye-local output/display UV and returns render-texture sampling UVs for RGB.
// Use mapper.forward_rgb, NOT inverse; top-left origin, no implicit Y flip.
inline LoadedProfile parse_profile(const std::string& text) {
    if (text.size() > max_profile_bytes) detail::fail("JSON exceeds 65536-byte limit");
    // nlohmann treats a raw NUL as end-of-input, even for iterator/string
    // input. JSON permits only escaped NULs; never silently ignore a suffix.
    if (text.find('\0') != std::string::npos) detail::fail("JSON contains a raw NUL byte");
    try {
        // DOM parsers normally overwrite duplicate keys. Track decoded keys
        // before insertion, including escaped aliases, and abort before deep
        // input can consume an unbounded recursive parser stack.
        std::vector<std::set<std::string>> keys;
        auto callback = [&](int, detail::Json::parse_event_t event, detail::Json& value) {
            using Event = detail::Json::parse_event_t;
            if (event == Event::object_start || event == Event::array_start) {
                if (keys.size() >= max_profile_depth) detail::fail("JSON nesting exceeds depth 16");
                keys.emplace_back();
            } else if (event == Event::object_end || event == Event::array_end) {
                keys.pop_back();
            } else if (event == Event::key) {
                const auto key = value.get<std::string>();
                if (!keys.back().insert(key).second) detail::fail("duplicate key '" + key + "'");
            }
            return true;
        };
        const auto root = detail::Json::parse(text, callback);
        detail::exact_keys(root, {"schema_version", "name", "uv_convention", "mapping", "eyes"}, "root");
        const auto& version = root.at("schema_version");
        if (!version.is_number_integer() || version != 1) detail::fail("schema_version must be integer 1");
        if (!root.at("name").is_string()) detail::fail("name must be a string");
        const auto name = root.at("name").get<std::string>();
        detail::validate_name(name);
        if (root.at("uv_convention") != "eye-local-top-left")
            detail::fail("uv_convention must be 'eye-local-top-left'");
        if (root.at("mapping") != "display-to-render")
            detail::fail("mapping must be 'display-to-render'");
        const auto& eyes = root.at("eyes");
        detail::exact_keys(eyes, {"left", "right"}, "eyes");
        distortion::Profile profile;
        profile.eyes = {{detail::eye(eyes.at("left"), "eyes.left"),
                         detail::eye(eyes.at("right"), "eyes.right")}};
        return LoadedProfile(name, profile);
    } catch (const detail::Json::exception& e) {
        detail::fail(std::string("invalid JSON: ") + e.what());
    }
    throw std::invalid_argument("optical profile: invalid JSON");
}

inline LoadedProfile load_profile(const std::filesystem::path& path) {
    // Pass the native path directly: preserves Unicode Windows filenames.
    // Read at most limit+1 bytes, detecting oversized input without trusting
    // file_size (which can race) or allocating proportional to file size.
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("optical profile: cannot open profile file");
    std::string text(max_profile_bytes + 1, '\0');
    input.read(text.data(), static_cast<std::streamsize>(text.size()));
    const auto count = input.gcount();
    if (input.bad() || (input.fail() && !input.eof()))
        throw std::runtime_error("optical profile: failed reading profile file");
    if (count > static_cast<std::streamsize>(max_profile_bytes))
        throw std::runtime_error("optical profile: file exceeds 65536-byte limit");
    text.resize(static_cast<std::size_t>(count));
    return parse_profile(text);
}

} // namespace psvr::optics
