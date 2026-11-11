// SPDX-License-Identifier: MIT
#pragma once
#include "pose_bridge.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <mutex>

namespace psvr {
enum class PoseReason { valid, display_invalid, disconnected, imu_lost, calibration_failed,
    calibrating, no_sample, future_sample, stale_sample, nonfinite_orientation,
    nonunit_orientation, position_missing, position_incoherent, position_nonfinite,
    fallback_3dof, position_grace, reacquiring };
inline const char* pose_reason_name(PoseReason r) noexcept {
    switch (r) {
    case PoseReason::valid: return "valid";
    case PoseReason::display_invalid: return "display_invalid";
    case PoseReason::disconnected: return "disconnected";
    case PoseReason::imu_lost: return "imu_lost";
    case PoseReason::calibration_failed: return "calibration_failed";
    case PoseReason::calibrating: return "calibrating";
    case PoseReason::no_sample: return "no_sample";
    case PoseReason::future_sample: return "future_sample";
    case PoseReason::stale_sample: return "stale_sample";
    case PoseReason::nonfinite_orientation: return "nonfinite_orientation";
    case PoseReason::nonunit_orientation: return "nonunit_orientation";
    case PoseReason::position_missing: return "position_missing";
    case PoseReason::position_incoherent: return "position_incoherent";
    case PoseReason::position_nonfinite: return "position_nonfinite";
    case PoseReason::fallback_3dof: return "3dof_fallback";
    case PoseReason::position_grace: return "position_grace";
    case PoseReason::reacquiring: return "6dof_reacquiring";
    }
    return "unknown";
}
inline PoseReason pose_reason(const TrackingSnapshot& s, DriverClock::time_point now,
                              int stale_ms, bool display_valid, bool valid) noexcept {
    if (!display_valid) return PoseReason::display_invalid;
    if (valid) {
        if (s.tracking_mode == TrackingMode::fallback_3dof) return PoseReason::fallback_3dof;
        if (s.tracking_mode == TrackingMode::position_grace) return PoseReason::position_grace;
        if (s.tracking_mode == TrackingMode::reacquiring) return PoseReason::reacquiring;
        return PoseReason::valid;
    }
    if (!s.connected) return PoseReason::disconnected;
    if (s.state == ImuState::lost) return PoseReason::imu_lost;
    if (s.state == ImuState::calibration_failed) return PoseReason::calibration_failed;
    if (s.state != ImuState::tracking) return PoseReason::calibrating;
    if (!s.has_sample) return PoseReason::no_sample;
    const auto age = std::chrono::duration<double,std::milli>(now-s.sampled_at).count();
    if (age < 0) return PoseReason::future_sample;
    if (age > stale_ms) return PoseReason::stale_sample;
    const auto& q = s.orientation;
    if (!std::isfinite(q.w)||!std::isfinite(q.x)||!std::isfinite(q.y)||!std::isfinite(q.z)) return PoseReason::nonfinite_orientation;
    if (std::abs(q.norm()-1) >= 1e-3) return PoseReason::nonunit_orientation;
    if (!s.has_position) return PoseReason::position_missing;
    if (!s.position_coherent) return PoseReason::position_incoherent;
    return PoseReason::position_nonfinite;
}
struct PoseTransition {
    std::uint64_t sequence = 0;
    std::int64_t utc_unix_us = 0, steady_elapsed_us = 0;
    bool valid = false, connected = false, has_sample = false;
    ImuState imu_state = ImuState::lost;
    int tracking_result = 0;
    PoseReason reason = PoseReason::disconnected;
    double sample_age_ms = 0, sample_age_after_copy_ms = 0, query_to_copy_ms = 0;
    TrackingMode mode = TrackingMode::invalid;
    bool orientation_valid = false, position_valid = false;
    unsigned recenter_epoch = 0;
    Vec3 recovery_offset{};
    double recovery_duration_ms = 0, recovery_elapsed_ms = 0;
};

// GetPose can be called on several runtime threads. Its producer never waits,
// formats JSON, allocates or calls OpenVR logging. Drain/log outside the callback.
class PoseTransitionTrace {
    std::mutex mutex_;
    std::array<PoseTransition,128> pending_{};
    std::size_t count_ = 0;
    bool have_previous_ = false;
    PoseTransition previous_{};
    std::uint64_t sequence_ = 0;
    std::atomic<std::uint64_t> dropped_{0};
public:
    void observe(const TrackingSnapshot& s, const vr::DriverPose_t& pose, DriverClock::time_point query,
                 DriverClock::time_point copied, DriverClock::time_point origin, int stale_ms, bool display_valid) noexcept {
        try {
            std::unique_lock<std::mutex> lock(mutex_,std::try_to_lock);
            if (!lock.owns_lock()) { dropped_.fetch_add(1,std::memory_order_relaxed); return; }
            const auto reason = pose_reason(s,query,stale_ms,display_valid,pose.poseIsValid);
            if (have_previous_ && previous_.valid == pose.poseIsValid && previous_.connected == pose.deviceIsConnected &&
                previous_.tracking_result == static_cast<int>(pose.result) && previous_.reason == reason &&
                previous_.mode == s.tracking_mode && previous_.recenter_epoch == s.recenter_epoch) return;
            PoseTransition event;
            event.sequence = ++sequence_;
            event.utc_unix_us = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
            event.steady_elapsed_us = std::chrono::duration_cast<std::chrono::microseconds>(query-origin).count();
            event.valid = pose.poseIsValid; event.connected = pose.deviceIsConnected;
            event.has_sample = s.has_sample; event.imu_state = s.state;
            event.tracking_result = static_cast<int>(pose.result); event.reason = reason;
            event.mode = s.tracking_mode; event.recenter_epoch = s.recenter_epoch;
            event.orientation_valid = fresh_orientation(s,query,stale_ms);
            event.position_valid = s.optical_position_valid && display_valid;
            event.recovery_offset = s.recovery_offset;
            event.recovery_duration_ms = s.recovery_duration_ms;
            event.recovery_elapsed_ms = s.recovery_elapsed_ms;
            if (s.has_sample) {
                event.sample_age_ms = std::chrono::duration<double,std::milli>(query-s.sampled_at).count();
                event.sample_age_after_copy_ms = std::chrono::duration<double,std::milli>(copied-s.sampled_at).count();
            }
            event.query_to_copy_ms = std::chrono::duration<double,std::milli>(copied-query).count();
            previous_ = event; have_previous_ = true;
            if (count_ < pending_.size()) pending_[count_++] = event;
            else dropped_.fetch_add(1,std::memory_order_relaxed);
        } catch (...) { dropped_.fetch_add(1,std::memory_order_relaxed); }
    }
    std::size_t drain(std::array<PoseTransition,128>& destination) {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto size = count_;
        std::copy_n(pending_.begin(),size,destination.begin()); count_ = 0;
        return size;
    }
    std::uint64_t dropped() const noexcept { return dropped_.load(std::memory_order_relaxed); }
    void reset() {
        std::lock_guard<std::mutex> lock(mutex_);
        count_ = 0; have_previous_ = false; sequence_ = 0; dropped_ = 0;
    }
};
} // namespace psvr
