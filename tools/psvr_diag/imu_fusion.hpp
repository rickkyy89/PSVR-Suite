// SPDX-License-Identifier: MIT
// Independent quaternion complementary fusion. PSVR unit/axis mapping follows
// OpenHMD drv_psvr/psvr.c 85075b0; quaternion math is in orientation_math.hpp.
#pragma once
#include "sensor_packet.hpp"
#include "orientation_math.hpp"
#include <string>

namespace psvr {
struct ImuConfig {
    int capture_ms = 15000;
    int calibration_ms = 8000;
    int startup_discard_ms = 200;
    int output_interval_ms = 100;
    int stale_timeout_ms = 100;
    int max_sample_gap_us = 5000;
    double gyro_scale = 0.00105;
    double accel_scale = 9.81/16384.0;
    double gravity_gain = 1.0;
    double accel_tolerance = 1.5;
    double calibration_gyro_std_dps = 0.75;
    double calibration_mean_shift_dps = 0.05;
    double calibration_gyro_mean_dps = 10.0;
    double calibration_accel_std = 0.25;
    double smoothing_ms = 0;
    double max_drift_dpm = 1.0;
    std::string recenter_mode = "full";
};
enum class ImuState { warming, calibrating, tracking, calibration_failed, lost };
inline const char* state_name(ImuState state) {
    switch (state) {
    case ImuState::warming: return "warming";
    case ImuState::calibrating: return "calibrating";
    case ImuState::tracking: return "tracking";
    case ImuState::calibration_failed: return "calibration-failed";
    case ImuState::lost: return "tracking-lost";
    }
    return "unknown";
}
inline Vec3 gyro_units(const SensorSample& sample, const ImuConfig& c) {
    return {sample.gyro[1]*c.gyro_scale,sample.gyro[0]*c.gyro_scale,-sample.gyro[2]*c.gyro_scale};
}
inline Vec3 accel_units(const SensorSample& sample, const ImuConfig& c) {
    return {sample.accel[1]*c.accel_scale,sample.accel[0]*c.accel_scale,-sample.accel[2]*c.accel_scale};
}
class ImuFusion {
    ImuConfig config_;
    bool have_tick_ = false;
    std::uint32_t previous_tick_ = 0;
    unsigned long long elapsed_us_ = 0, calibration_us_ = 0;
    ImuState state_ = ImuState::warming;
    Quaternion absolute_{}, reference_{}, head_offset_{}, filtered_{};
    VectorStats calibration_gyro_{}, calibration_accel_{};
    VectorStats calibration_first_{}, calibration_second_{};
    std::string calibration_error_;
    Vec3 bias_{}, gyro_{}, accel_{};
    // Monotonic generation of the world reference. Bumped whenever reference_
    // changes (any recenter, including the automatic one at calibration
    // completion, and a calibration restart). The M9 position bridge uses it as
    // the shared recenter epoch so a world-frame change always forces a new
    // seated origin, and the epoch can never be observed with a mismatched
    // world rotation.
    unsigned world_epoch_ = 0;
public:
    unsigned long long samples = 0, integrated = 0, duplicates = 0, gaps = 0, rollovers = 0;
    unsigned long long rejected = 0, recenter_count = 0, skipped_gravity = 0;
    unsigned long long integrated_us = 0;
    unsigned int min_dt_us = 0xffffffffu, max_dt_us = 0;
    double max_quaternion_error = 0;
    explicit ImuFusion(ImuConfig c = {}) : config_(c) {}
    ImuState state() const { return state_; }
    bool has_tick() const { return have_tick_; }
    std::uint32_t previous_tick() const { return previous_tick_; }
    double elapsed_s() const { return static_cast<double>(elapsed_us_)*1e-6; }
    double calibration_elapsed_ms() const { return static_cast<double>(calibration_us_)*0.001; }
    Vec3 gyro() const { return gyro_; }
    Vec3 corrected_gyro() const { return gyro_-bias_; }
    Vec3 accel() const { return accel_; }
    Vec3 bias() const { return bias_; }
    const VectorStats& calibration_gyro() const { return calibration_gyro_; }
    const VectorStats& calibration_accel() const { return calibration_accel_; }
    Vec3 calibration_mean_shift() const { return (calibration_second_.mean-calibration_first_.mean)*degrees; }
    const std::string& calibration_error() const { return calibration_error_; }
    Quaternion orientation() const { return (reference_.conjugate()*absolute_*head_offset_).normalized(); }
    // Optical rigid-body matching needs the physical remapped sensor basis,
    // before the right-hand neutral-head offset introduced by full recenter.
    Quaternion sensor_orientation() const { return (reference_.conjugate()*absolute_).normalized(); }
    Quaternion output() const { return filtered_; }
    // World-yaw reference applied by the last recenter: the rotation from the
    // gravity-aligned sensor frame to the recentered world frame. The M9
    // position bridge rotates the camera translation by this so a yaw recenter
    // keeps position and orientation in the same frame.
    Quaternion world_rotation() const { return reference_.conjugate(); }
    unsigned world_epoch() const { return world_epoch_; }
    void mark_lost() { if (state_ != ImuState::calibration_failed) state_ = ImuState::lost; }
    void restart_calibration() {
        state_ = ImuState::warming;
        elapsed_us_ = calibration_us_ = 0;
        have_tick_ = false;
        calibration_gyro_ = {}; calibration_accel_ = {};
        calibration_first_ = {}; calibration_second_ = {}; calibration_error_.clear();
        bias_ = {}; absolute_ = {}; reference_ = {}; head_offset_ = {}; filtered_ = {};
        ++world_epoch_; // reference_ reset to identity is a world-frame change
    }
    bool recenter() {
        if (state_ != ImuState::tracking) return false;
        ++world_epoch_;
        // World heading is changed on the LEFT, neutral head/sensor alignment on
        // the RIGHT. A full inverse on the left would tilt world-up and turn
        // pure world yaw into apparent roll when the neutral sensor is pitched.
        reference_ = rotation_vector({0,angles(absolute_*head_offset_).yaw/degrees,0});
        if (config_.recenter_mode == "full")
            head_offset_ = (reference_.conjugate()*absolute_).normalized().conjugate();
        filtered_ = orientation();
        ++recenter_count;
        return true;
    }
    bool update(const SensorSample& sample) {
        ++samples;
        const auto tick = sample.tick & 0xffffffu;
        if (!have_tick_) {
            previous_tick_ = tick; have_tick_ = true;
            gyro_ = gyro_units(sample,config_); accel_ = accel_units(sample,config_);
            return false;
        }
        const auto delta = (tick-previous_tick_) & 0xffffffu;
        if (delta == 0) { ++duplicates; ++rejected; return false; }
        if (tick < previous_tick_ && delta <= static_cast<unsigned>(config_.max_sample_gap_us)) ++rollovers;
        previous_tick_ = tick;
        if (delta > static_cast<unsigned>(config_.max_sample_gap_us)) {
            ++gaps; ++rejected;
            if (state_ == ImuState::warming) elapsed_us_ = 0;
            else mark_lost();
            return false;
        }
        gyro_ = gyro_units(sample,config_);
        accel_ = accel_units(sample,config_);
        elapsed_us_ += delta;
        if (state_ == ImuState::warming) {
            if (elapsed_us_ < static_cast<unsigned long long>(config_.startup_discard_ms)*1000) return true;
            state_ = ImuState::calibrating;
        }
        if (state_ == ImuState::calibrating) {
            calibration_gyro_.add(gyro_); calibration_accel_.add(accel_);
            calibration_us_ += delta;
            if (calibration_us_ <= static_cast<unsigned long long>(config_.calibration_ms)*500) calibration_first_.add(gyro_);
            else calibration_second_.add(gyro_);
            if (calibration_us_ < static_cast<unsigned long long>(config_.calibration_ms)*1000) return true;
            const auto gyro_std = calibration_gyro_.deviation()*degrees;
            const auto gyro_mean = calibration_gyro_.mean*degrees;
            const auto accel_std = calibration_accel_.deviation();
            if (calibration_gyro_.count < 100) calibration_error_ = "too few calibration samples";
            else if (max_axis(gyro_std) > config_.calibration_gyro_std_dps) calibration_error_ = "gyro variation exceeds stationary limit";
            else if (max_axis(calibration_mean_shift()) > config_.calibration_mean_shift_dps) calibration_error_ = "gyro mean changes between calibration halves";
            else if (max_axis(gyro_mean) > config_.calibration_gyro_mean_dps) calibration_error_ = "gyro mean exceeds allowed bias range";
            else if (max_axis(accel_std) > config_.calibration_accel_std) calibration_error_ = "accelerometer variation exceeds stationary limit";
            else if (std::abs(calibration_accel_.mean.norm()-9.81) > config_.accel_tolerance) calibration_error_ = "gravity magnitude outside calibration range";
            if (!calibration_error_.empty()) {
                state_ = ImuState::calibration_failed;
                return true;
            }
            bias_ = calibration_gyro_.mean;
            absolute_ = align_up(calibration_accel_.mean);
            state_ = ImuState::tracking;
            recenter();
            return true;
        }
        if (state_ != ImuState::tracking) return true;
        const auto dt = static_cast<double>(delta)*1e-6;
        auto angular = corrected_gyro();
        const auto accel_length = accel_.norm();
        if (accel_length > 1e-6 && std::abs(accel_length-9.81) <= config_.accel_tolerance && angular.norm() < 0.5) {
            const auto measured_up = accel_*(1/accel_length);
            const auto predicted_up = absolute_.conjugate().rotate({0,1,0});
            angular = angular + measured_up.cross(predicted_up)*config_.gravity_gain;
        } else ++skipped_gravity;
        absolute_ = (absolute_*rotation_vector(angular*dt)).normalized();
        max_quaternion_error = std::max(max_quaternion_error,std::abs(absolute_.norm()-1));
        const auto pose = orientation();
        filtered_ = config_.smoothing_ms > 0 ? blend(filtered_,pose,1-std::exp(-dt/(config_.smoothing_ms*0.001))) : pose;
        ++integrated; integrated_us += delta;
        min_dt_us = std::min(min_dt_us,delta); max_dt_us = std::max(max_dt_us,delta);
        return true;
    }
};
} // namespace psvr
