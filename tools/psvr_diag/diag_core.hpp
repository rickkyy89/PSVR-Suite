// SPDX-License-Identifier: MIT
#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>
#include "control_protocol.hpp"
#include "imu_fusion.hpp"

namespace psvr {
constexpr unsigned short vendor_id = 0x054c;
constexpr unsigned short product_id = 0x09af;
constexpr int sensor_interface = 4;
constexpr int control_interface = 5;

struct Options {
    int duration_ms = 2000;
    int read_timeout_ms = 100;
    int raw_packets = 3;
    int control_timeout_ms = 3000;
    std::string log_dir = "logs";
    std::string verbosity = "info";
    bool enumerate_only = false;
    bool all_devices = false;
    Action action = Action::none;
    bool dry_run = false;
    bool imu = false, stationary_test = false;
    int recenter_at_ms = 0;
    int recenter_pid = 0;
    int recalibrate_pid = 0, stop_imu_pid = 0;
    bool json_stream = false;
    std::string axis_test;
    ImuConfig tracking{};
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
    if ((o.duration_ms < 100 && !(o.imu && o.duration_ms == 0)) || o.duration_ms > (o.imu ? 180000 : 60000) ||
        o.read_timeout_ms < 1 || o.read_timeout_ms > 1000 ||
        o.raw_packets < 0 || o.raw_packets > 100 || o.control_timeout_ms < 100 ||
        o.control_timeout_ms > 15000 || o.log_dir.empty() ||
        (o.verbosity != "info" && o.verbosity != "debug"))
        throw std::runtime_error("invalid diagnostic config: duration=100..60000, timeout=1..1000, control_timeout=100..15000, raw=0..100, verbosity=info/debug");
    if (o.enumerate_only && o.action != Action::none) throw std::runtime_error("action cannot be combined with --enumerate-only");
    if (o.dry_run && !writes_command(o.action)) throw std::runtime_error("--dry-run requires one power/mode action");
    if (o.imu && (o.enumerate_only || o.action != Action::none || o.dry_run))
        throw std::runtime_error("IMU capture cannot be combined with control/enumerate/dry-run actions");
    const auto& c = o.tracking;
    if (c.capture_ms < 1000 || c.capture_ms > 180000 || c.calibration_ms < 500 || c.calibration_ms > 10000 ||
        c.startup_discard_ms < 0 || c.startup_discard_ms > 2000 || c.output_interval_ms < 20 || c.output_interval_ms > 5000 ||
        c.stale_timeout_ms < 20 || c.stale_timeout_ms > 1000 || c.max_sample_gap_us < 1000 || c.max_sample_gap_us > 20000 ||
        (c.recenter_mode != "full" && c.recenter_mode != "yaw")) throw std::runtime_error("invalid tracking integer/recenter config");
    auto number = [](double v, double low, double high) { return std::isfinite(v) && v >= low && v <= high; };
    if (!number(c.gyro_scale,1e-6,0.01) || !number(c.accel_scale,1e-6,0.01) || !number(c.gravity_gain,0,10) ||
        !number(c.accel_tolerance,0.1,3) || !number(c.calibration_gyro_std_dps,0.01,5) || !number(c.calibration_mean_shift_dps,0.001,1) ||
        !number(c.calibration_gyro_mean_dps,0.1,15) || !number(c.calibration_accel_std,0.001,1) ||
        !number(c.smoothing_ms,0,100) || !number(c.max_drift_dpm,0.01,5)) throw std::runtime_error("invalid tracking numeric config");
    if (!o.axis_test.empty() && o.axis_test != "yaw-right" && o.axis_test != "pitch-up" && o.axis_test != "roll-right")
        throw std::runtime_error("axis-test must be yaw-right, pitch-up or roll-right");
    if (o.stationary_test && (!o.axis_test.empty() || o.recenter_at_ms))
        throw std::runtime_error("stationary-test cannot be combined with axis-test/recenter");
    if (!o.axis_test.empty() && o.recenter_at_ms) throw std::runtime_error("axis-test cannot be combined with recenter");
    const int requests = (o.recenter_pid != 0)+(o.recalibrate_pid != 0)+(o.stop_imu_pid != 0);
    if (requests > 1) throw std::runtime_error("only one IMU IPC request may be specified");
    if (requests && (o.imu || o.action != Action::none || o.enumerate_only || o.dry_run))
        throw std::runtime_error("external recenter cannot be combined with other actions");
    if (o.imu && o.duration_ms != 0 && o.duration_ms <= c.startup_discard_ms+c.calibration_ms+100)
        throw std::runtime_error("IMU duration must exceed startup + calibration + tracking");
    if (o.duration_ms == 0 && (o.stationary_test || !o.axis_test.empty())) throw std::runtime_error("measurement profiles require a finite duration");
    if (o.recenter_at_ms && (!o.imu || (o.duration_ms != 0 && o.recenter_at_ms >= o.duration_ms)))
        throw std::runtime_error("recenter-at-ms requires IMU and must precede capture end");
}

inline void check_keys(const nlohmann::json& j, const std::vector<std::string>& allowed) {
    if (!j.is_object()) throw std::runtime_error("config must contain JSON objects");
    for (const auto& item : j.items())
        if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
            throw std::runtime_error("unknown config field: " + item.key());
}

inline Options config_options(const nlohmann::json& j) {
    check_keys(j, {"schema_version", "diagnostic", "tracking"});
    if (!j.contains("schema_version") || !j.at("schema_version").is_number_integer() ||
        (j.at("schema_version") != 1 && j.at("schema_version") != 2))
        throw std::runtime_error("schema_version must be integer 1 or 2");
    const auto& d = j.at("diagnostic");
    check_keys(d, {"duration_ms", "read_timeout_ms", "control_timeout_ms", "raw_packets", "log_dir", "logging_verbosity"});
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
    integer("control_timeout_ms", o.control_timeout_ms);
    if (d.contains("log_dir")) o.log_dir = d.at("log_dir").get<std::string>();
    if (d.contains("logging_verbosity")) o.verbosity = d.at("logging_verbosity").get<std::string>();
    if (j.contains("tracking")) {
        const auto& t = j.at("tracking");
        check_keys(t,{"capture_ms","calibration_ms","startup_discard_ms","output_interval_ms","stale_timeout_ms",
            "max_sample_gap_us","gyro_scale","accel_scale","gravity_gain","accel_tolerance",
            "calibration_gyro_std_dps","calibration_mean_shift_dps","calibration_gyro_mean_dps","calibration_accel_std","smoothing_ms","max_drift_dpm","recenter_mode"});
        auto ti = [&](const char* key, int& value) {
            if (!t.contains(key)) return;
            if (!t.at(key).is_number_integer()) throw std::runtime_error(std::string(key)+" must be integer");
            value = bounded_integer(t.at(key).dump(),0,std::numeric_limits<int>::max());
        };
        auto td = [&](const char* key, double& value) {
            if (!t.contains(key)) return;
            if (!t.at(key).is_number()) throw std::runtime_error(std::string(key)+" must be numeric");
            value = t.at(key).get<double>();
        };
        auto& c = o.tracking;
        ti("capture_ms",c.capture_ms); ti("calibration_ms",c.calibration_ms); ti("startup_discard_ms",c.startup_discard_ms);
        ti("output_interval_ms",c.output_interval_ms); ti("stale_timeout_ms",c.stale_timeout_ms); ti("max_sample_gap_us",c.max_sample_gap_us);
        td("gyro_scale",c.gyro_scale); td("accel_scale",c.accel_scale); td("gravity_gain",c.gravity_gain);
        td("accel_tolerance",c.accel_tolerance); td("calibration_gyro_std_dps",c.calibration_gyro_std_dps);
        td("calibration_gyro_mean_dps",c.calibration_gyro_mean_dps); td("calibration_accel_std",c.calibration_accel_std);
        td("calibration_mean_shift_dps",c.calibration_mean_shift_dps);
        td("smoothing_ms",c.smoothing_ms); td("max_drift_dpm",c.max_drift_dpm);
        if (t.contains("recenter_mode")) c.recenter_mode = t.at("recenter_mode").get<std::string>();
    }
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
