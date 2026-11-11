// SPDX-License-Identifier: MIT
#pragma once
#include "imu_fusion.hpp"
#include "imu_runtime.hpp"
#include <openvr_driver.h>
#include <chrono>
#include <cstring>

namespace psvr {
using DriverClock = std::chrono::steady_clock;
enum class TrackingMode { invalid, orientation_only, sixdof, position_grace, fallback_3dof, reacquiring };
inline const char* tracking_mode_name(TrackingMode mode) {
    switch (mode) {
    case TrackingMode::orientation_only: return "3dof";
    case TrackingMode::sixdof: return "6dof";
    case TrackingMode::position_grace: return "position_grace";
    case TrackingMode::fallback_3dof: return "3dof_fallback";
    case TrackingMode::reacquiring: return "6dof_reacquiring";
    default: return "invalid";
    }
}
struct TrackingSnapshot {
    bool connected = false;
    ImuState state = ImuState::lost;
    Quaternion orientation{};
    // Driver-world-frame angular velocity (rad/s) for runtime prediction.
    // Smoothed bias-corrected gyro rotated by the physical sensor orientation
    // in driver world, before the right-hand neutral head offset.
    // Zero unless a fresh tracking sample backs the pose.
    Vec3 angular_velocity{};
    DriverClock::time_point sampled_at{};
    bool has_sample = false;
    // M9 6DoF. All defaults preserve the 3DoF orientation-only contract.
    bool position_enabled = false;   // opt-in; when false the pose is unchanged
    bool has_position = false;       // fresh, present and epoch-coherent
    bool position_coherent = true;   // recenter epoch matches the IMU epoch
    Vec3 position{};                 // metres, seated frame, aligned to world_rotation
    DriverClock::time_point position_at{};
    // Shared recenter epoch carried with the snapshot so the published
    // orientation and its position origin can never be read from different
    // epochs (the previous separate atomic could be observed ahead of the
    // stored orientation).
    unsigned recenter_epoch = 0;
    // IMU world-yaw reference for `recenter_epoch`; the position bridge applies
    // it to the translation so a yaw recenter rotates both channels together.
    Quaternion world_rotation{};
    Quaternion qabs{}; // physical sensor orientation before head offset/reference
    Vec3 corrected_gyro{}; // sensor/body frame, rad/s
    // Driver-only continuity authorization: NEVER substitutes has_position or
    // changes the camera packet. Set only by the publish-owned controller.
    bool position_fallback_3dof = false;
    bool position_fallback_policy_enabled = false;
    bool has_initial_position_offset = false;
    TrackingMode tracking_mode = TrackingMode::invalid;
    bool optical_position_valid = false;
    bool angular_centering = false;
    double angular_center_weight = 0;
    Vec3 recovery_offset{};
    double recovery_duration_ms = 0, recovery_elapsed_ms = 0;
};

inline bool fresh_orientation(const TrackingSnapshot& s, DriverClock::time_point now, int stale_ms) {
    const double age = std::chrono::duration<double>(now-s.sampled_at).count();
    const auto& q = s.orientation;
    return s.connected && s.state == ImuState::tracking && s.has_sample &&
        age >= 0 && age <= stale_ms*.001 && std::isfinite(q.w) && std::isfinite(q.x) &&
        std::isfinite(q.y) && std::isfinite(q.z) && std::abs(q.norm()-1) < 1e-3;
}

inline vr::DriverPose_t driver_pose(const TrackingSnapshot& sample,
                                  DriverClock::time_point now, int stale_ms = 100) {
    vr::DriverPose_t pose{};
    pose.qWorldFromDriverRotation.w = 1;
    pose.qDriverFromHeadRotation.w = 1;
    pose.qRotation.w = 1;
    pose.willDriftInYaw = true;
    // Fixed origin, rotation only. No invented position or head-model offset.
    pose.shouldApplyHeadModel = false;
    pose.deviceIsConnected = sample.connected;
    pose.result = vr::TrackingResult_Uninitialized;
    const auto age = std::chrono::duration<double>(now-sample.sampled_at).count();
    const auto& q = sample.orientation;
    const bool finite = std::isfinite(q.w) && std::isfinite(q.x) &&
                        std::isfinite(q.y) && std::isfinite(q.z);
    const bool fresh = sample.has_sample && age >= 0 && age <= stale_ms*.001;
    const bool orientation_valid = sample.connected && sample.state == ImuState::tracking &&
                                   fresh && finite && std::abs(q.norm()-1) < 1e-3;
    const auto orientation_status = [&]() {
        // A tracking IMU whose sample is stale, non-finite or non-unit is out of
        // range, not "calibrating": the stream is live but unusable.
        if ((sample.position_enabled && sample.position_fallback_policy_enabled) ||
            sample.state == ImuState::lost || sample.state == ImuState::calibration_failed ||
            (sample.state == ImuState::tracking && !orientation_valid))
            pose.result = vr::TrackingResult_Running_OutOfRange;
        else if (sample.connected)
            pose.result = vr::TrackingResult_Calibrating_InProgress;
    };
    if (!sample.position_enabled) {
        // Default 3DoF contract: unchanged, position stays exactly zero.
        if (orientation_valid) {
            pose.qRotation = {q.w,q.x,q.y,q.z};
            pose.poseTimeOffset = -age;
            pose.vecAngularVelocity[0] = sample.angular_velocity.x;
            pose.vecAngularVelocity[1] = sample.angular_velocity.y;
            pose.vecAngularVelocity[2] = sample.angular_velocity.z;
            pose.poseIsValid = true;
            pose.result = vr::TrackingResult_Running_OK;
            if (sample.has_initial_position_offset && std::isfinite(sample.position.x) &&
                std::isfinite(sample.position.y) && std::isfinite(sample.position.z)) {
                pose.vecPosition[0] = sample.position.x;
                pose.vecPosition[1] = sample.position.y;
                pose.vecPosition[2] = sample.position.z;
            }
        } else {
            orientation_status();
        }
        // Velocity stays zero on any invalid pose: the runtime must never
        // predict from a stale or uncalibrated rate.
        return pose;
    }
    // Opt-in 6DoF: orientation alone is not enough. A missing, stale,
    // out-of-order-rejected or epoch-incoherent position is an explicit
    // OutOfRange, never a silent zero. PositionBridge may explicitly authorize
    // a bounded last-good hold; orientation remains current in that case.
    if (!orientation_valid) {
        orientation_status();
        return pose;
    }
    const auto& p = sample.position;
    const bool position_finite = std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z);
    if ((sample.has_position || sample.position_fallback_3dof) && sample.position_coherent && position_finite) {
        pose.qRotation = {q.w,q.x,q.y,q.z};
        pose.vecPosition[0] = p.x; pose.vecPosition[1] = p.y; pose.vecPosition[2] = p.z;
        // Runtime angular prediction belongs to the IMU timestamp. An older
        // held translation must not cause 80ms of orientation extrapolation.
        pose.poseTimeOffset = -age;
        pose.vecAngularVelocity[0] = sample.angular_velocity.x;
        pose.vecAngularVelocity[1] = sample.angular_velocity.y;
        pose.vecAngularVelocity[2] = sample.angular_velocity.z;
        pose.poseIsValid = true;
        pose.result = vr::TrackingResult_Running_OK;
    } else {
        pose.result = vr::TrackingResult_Running_OutOfRange;
    }
    return pose;
}

// Pure packet/fusion adapter, also exercised with synthetic packets in CTest.
struct ImuDiagnostics {
    const char* reason = "none";
    ImuState previous_state = ImuState::warming;
    std::int64_t failure_elapsed_ms = 0, failure_progress_age_ms = 0;
    int failure_timeout_ms = 0;
    std::uint64_t reports_received = 0, reports_decoded = 0, reports_malformed = 0;
    std::uint64_t reports_duplicate = 0, reports_blocked_by_watchdog = 0;
    bool have_report = false, have_tick_delta = false;
    unsigned int last_sequence = 0;
    std::uint32_t last_report_tick = 0, previous_tick = 0, sample_tick = 0, tick_delta_us = 0;
    std::uint64_t samples_seen = 0, samples_advanced = 0, samples_rejected = 0;
    std::uint64_t sample_duplicates = 0, sample_gaps = 0;
};

class DriverTracker {
    // EMA time constant for reported angular velocity. Raw 1000Hz gyro is
    // too noisy for direct prediction; ~15ms settles in ~45ms without
    // adding perceptible lag at head-motion bandwidth.
    static constexpr double velocity_tau_s_ = 0.015;
    ImuConfig config_;
    ImuFusion fusion_;
    ImuProgressWatchdog progress_;
    DriverClock::time_point started_;
    TrackingSnapshot snapshot_{};
    Vec3 velocity_ema_{};
    bool velocity_have_ = false;
    bool have_sequence_ = false;
    unsigned int sequence_ = 0;
    std::uint32_t last_tick_ = 0;
    ImuDiagnostics diagnostics_{};
    ImuDiagnostics fusion_baseline_{};
    void record_failure(const char* reason, ImuState previous, DriverClock::time_point now, int timeout_ms) {
        if (std::strcmp(diagnostics_.reason,"none") != 0) return; // Preserve the FIRST cause.
        diagnostics_.reason = reason;
        diagnostics_.previous_state = previous;
        diagnostics_.failure_elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now-started_).count();
        diagnostics_.failure_progress_age_ms = diagnostics_.failure_elapsed_ms-progress_.last_advance_ms();
        diagnostics_.failure_timeout_ms = timeout_ms;
    }
public:
    explicit DriverTracker(ImuConfig config, DriverClock::time_point started)
        : config_(config), fusion_(config), started_(started) {
        snapshot_.connected = true;
        snapshot_.state = fusion_.state();
    }
    void packet(const unsigned char* bytes, std::size_t size, DriverClock::time_point now) {
        ++diagnostics_.reports_received;
        // Evaluate the PREVIOUS progress timestamp before accepting a buffered
        // report. Host stalls cannot resurrect old contiguous sensor ticks.
        if (!watchdog(now)) { ++diagnostics_.reports_blocked_by_watchdog; return; }
        progress_.report_received();
        const auto decoded = decode_sensor(bytes,size);
        if (!decoded) { ++diagnostics_.reports_malformed; return; }
        ++diagnostics_.reports_decoded;
        diagnostics_.have_report = true;
        diagnostics_.last_sequence = decoded->sequence;
        diagnostics_.last_report_tick = decoded->samples[1].tick;
        if (have_sequence_ && decoded->sequence == sequence_ && decoded->samples[1].tick == last_tick_) {
            ++diagnostics_.reports_duplicate; return;
        }
        have_sequence_ = true; sequence_ = decoded->sequence; last_tick_ = decoded->samples[1].tick;
        bool advanced = false;
        for (const auto& sample : decoded->samples) {
            const auto previous = fusion_.state();
            const bool have_tick = fusion_.has_tick();
            const auto previous_tick = fusion_.previous_tick();
            const auto gaps = fusion_.gaps;
            const bool accepted = fusion_.update(sample);
            advanced = accepted || advanced;
            if (accepted) ++diagnostics_.samples_advanced;
            if (std::strcmp(diagnostics_.reason,"none") == 0) {
                diagnostics_.have_tick_delta = have_tick;
                diagnostics_.previous_tick = previous_tick;
                diagnostics_.sample_tick = sample.tick & 0xffffffu;
                diagnostics_.tick_delta_us = (diagnostics_.sample_tick-previous_tick) & 0xffffffu;
            }
            if (previous != ImuState::lost && fusion_.state() == ImuState::lost && fusion_.gaps > gaps)
                record_failure("sensor_tick_gap",previous,now,0);
            if (previous != ImuState::calibration_failed && fusion_.state() == ImuState::calibration_failed)
                record_failure("calibration_failed",previous,now,0);
        }
        if (advanced) {
            progress_.advanced(std::chrono::duration_cast<std::chrono::milliseconds>(now-started_).count());
            snapshot_.sampled_at = now;
            snapshot_.has_sample = true;
            snapshot_.orientation = fusion_.output();
            if (fusion_.state() == ImuState::tracking) {
                // Gyro axes belong to the physical sensor, not to the neutral
                // head basis on the RIGHT of the published quaternion. Only
                // the left world reference changes world-frame angular rates.
                // Gated on tracking so calibration cannot seed prediction.
                const Vec3 world = fusion_.sensor_orientation().rotate(fusion_.corrected_gyro());
                // Nominal 1ms report period; per-report EMA weight.
                const double a = 1.0 - std::exp(-0.001/velocity_tau_s_);
                velocity_ema_ = velocity_have_ ? velocity_ema_*(1.0-a)+world*a : world;
                velocity_have_ = true;
                snapshot_.angular_velocity = velocity_ema_;
            }
            // Epoch and world reference are read from the same fusion state, so
            // the automatic recenter at calibration completion cannot be
            // observed as a new rotation under an old epoch.
            snapshot_.world_rotation = fusion_.world_rotation();
            snapshot_.recenter_epoch = fusion_.world_epoch();
            snapshot_.qabs = snapshot_.world_rotation.conjugate() * fusion_.sensor_orientation();
            snapshot_.corrected_gyro = fusion_.corrected_gyro();
        }
        snapshot_.state = fusion_.state();
    }
    bool watchdog(DriverClock::time_point now) {
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now-started_).count();
        const auto previous = fusion_.state();
        const auto failure = progress_.failure(elapsed,previous == ImuState::tracking,config_.stale_timeout_ms);
        if (failure == ProgressFailure::none) return true;
        record_failure(failure == ProgressFailure::no_reports ? "watchdog_no_reports" : "watchdog_no_advancing_samples",
            previous,now,failure == ProgressFailure::no_reports ? 3000 : (previous == ImuState::tracking ? config_.stale_timeout_ms : 1000));
        fusion_.mark_lost(); snapshot_.state = fusion_.state(); return false;
    }
    bool recenter() {
        const auto previous_world_rotation = snapshot_.world_rotation;
        if (!fusion_.recenter()) return false;
        snapshot_.orientation = fusion_.output();
        // Transport world-frame EMA history through the LEFT reference change
        // only. Resetting a right-hand neutral head offset (or smoothing) does
        // not rotate physical angular velocity in the world.
        if (velocity_have_) {
            const auto frame_change = (fusion_.world_rotation() * previous_world_rotation.conjugate()).normalized();
            velocity_ema_ = frame_change.rotate(velocity_ema_);
            snapshot_.angular_velocity = velocity_ema_;
        }
        snapshot_.world_rotation = fusion_.world_rotation();
        snapshot_.recenter_epoch = fusion_.world_epoch();
        snapshot_.qabs = snapshot_.world_rotation.conjugate() * fusion_.sensor_orientation();
        snapshot_.corrected_gyro = fusion_.corrected_gyro();
        return true;
    }
    void recalibrate(DriverClock::time_point now) {
        fusion_.restart_calibration(); progress_ = {}; started_ = now;
        velocity_ema_ = {}; velocity_have_ = false;
        have_sequence_ = false;
        diagnostics_ = {};
        fusion_baseline_.samples_seen = fusion_.samples;
        fusion_baseline_.samples_rejected = fusion_.rejected;
        fusion_baseline_.sample_duplicates = fusion_.duplicates;
        fusion_baseline_.sample_gaps = fusion_.gaps;
        snapshot_ = {}; snapshot_.connected = true; snapshot_.state = fusion_.state();
        // restart_calibration bumped the world epoch; keep it so the bridge
        // re-baselines instead of reading a stale epoch 0.
        snapshot_.recenter_epoch = fusion_.world_epoch();
        snapshot_.world_rotation = fusion_.world_rotation();
    }
    TrackingSnapshot snapshot() const { return snapshot_; }
    ImuDiagnostics diagnostics() const {
        auto result = diagnostics_;
        result.samples_seen = fusion_.samples-fusion_baseline_.samples_seen;
        result.samples_rejected = fusion_.rejected-fusion_baseline_.samples_rejected;
        result.sample_duplicates = fusion_.duplicates-fusion_baseline_.sample_duplicates;
        result.sample_gaps = fusion_.gaps-fusion_baseline_.sample_gaps;
        return result;
    }
    std::int64_t progress_age_ms(DriverClock::time_point now) const {
        return std::chrono::duration_cast<std::chrono::milliseconds>(now-started_).count()-progress_.last_advance_ms();
    }
    const std::string& calibration_error() const { return fusion_.calibration_error(); }
};
} // namespace psvr
