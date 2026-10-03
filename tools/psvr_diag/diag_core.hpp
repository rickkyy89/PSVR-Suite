// SPDX-License-Identifier: MIT
#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace psvr {
constexpr unsigned short vendor_id = 0x054c;
constexpr unsigned short product_id = 0x09af;
constexpr int sensor_interface = 4;
constexpr int control_interface = 5;

struct Options {
    int duration_ms = 2000;
    int read_timeout_ms = 100;
    int raw_packets = 3;
    std::string log_dir = "logs";
    std::string verbosity = "info";
    bool enumerate_only = false;
    bool all_devices = false;
};

inline int bounded_integer(const std::string& value, int low, int high) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("expected unsigned integer, got: " + value);
    std::size_t used = 0;
    const auto parsed = std::stoll(value, &used);
    if (used != value.size() || parsed < low || parsed > high)
        throw std::runtime_error("integer outside range " + std::to_string(low) + ".." + std::to_string(high));
    return static_cast<int>(parsed);
}

inline void validate_options(const Options& o) {
    if (o.duration_ms < 100 || o.duration_ms > 60000 ||
        o.read_timeout_ms < 1 || o.read_timeout_ms > 1000 ||
        o.raw_packets < 0 || o.raw_packets > 100 || o.log_dir.empty() ||
        (o.verbosity != "info" && o.verbosity != "debug"))
        throw std::runtime_error("invalid diagnostic config: duration=100..60000, timeout=1..1000, raw=0..100, verbosity=info/debug");
}

inline void check_keys(const nlohmann::json& j, const std::vector<std::string>& allowed) {
    if (!j.is_object()) throw std::runtime_error("config must contain JSON objects");
    for (const auto& item : j.items())
        if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
            throw std::runtime_error("unknown config field: " + item.key());
}

inline Options config_options(const nlohmann::json& j) {
    check_keys(j, {"schema_version", "diagnostic"});
    if (!j.contains("schema_version") || !j.at("schema_version").is_number_integer() ||
        j.at("schema_version") != 1)
        throw std::runtime_error("schema_version must be integer 1");
    const auto& d = j.at("diagnostic");
    check_keys(d, {"duration_ms", "read_timeout_ms", "raw_packets", "log_dir", "logging_verbosity"});
    Options o;
    auto integer = [&](const char* key, int& destination) {
        if (!d.contains(key)) return;
        const auto& v = d.at(key);
        if (!v.is_number_integer()) throw std::runtime_error(std::string(key) + " must be integer");
        // Validate as text before narrowing, including huge unsigned JSON values.
        destination = bounded_integer(v.dump(), 0, std::numeric_limits<int>::max());
    };
    integer("duration_ms", o.duration_ms);
    integer("read_timeout_ms", o.read_timeout_ms);
    integer("raw_packets", o.raw_packets);
    if (d.contains("log_dir")) o.log_dir = d.at("log_dir").get<std::string>();
    if (d.contains("logging_verbosity")) o.verbosity = d.at("logging_verbosity").get<std::string>();
    validate_options(o);
    return o;
}

// Never select independent nth sensor/control handles: fail closed on ambiguity.
enum class Selection { absent, incomplete, ambiguous, ready };
inline Selection select_interfaces(const std::vector<int>& interfaces) {
    if (interfaces.empty()) return Selection::absent;
    const auto sensors = std::count(interfaces.begin(), interfaces.end(), sensor_interface);
    const auto controls = std::count(interfaces.begin(), interfaces.end(), control_interface);
    if (sensors > 1 || controls > 1) return Selection::ambiguous;
    return sensors == 1 && controls == 1 ? Selection::ready : Selection::incomplete;
}
} // namespace psvr
