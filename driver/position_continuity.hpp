// SPDX-License-Identifier: MIT
#pragma once
#include "pose_bridge.hpp"
#include <algorithm>
#include <stdexcept>
#include <string>

namespace psvr {
struct PositionContinuityConfig {
    bool fallback_3dof_enabled = false;
    bool angular_center_enabled = false; // Suite default, independent of optical packet validity.
    int reacquire_blend_ms = 150;
    double reacquire_max_speed_mps = 1.5;
    Vec3 initial_offset{}; // DRIVER world metres: never rotated by head orientation.
    void validate() const {
        if (reacquire_blend_ms < 0 || reacquire_blend_ms > 2000 ||
            !std::isfinite(reacquire_max_speed_mps) || reacquire_max_speed_mps <= 0 ||
            reacquire_max_speed_mps > 10) throw std::invalid_argument("invalid position reacquire settings");
        for (double v : {initial_offset.x, initial_offset.y, initial_offset.z})
            if (!std::isfinite(v) || v < -10 || v > 10)
                throw std::invalid_argument("invalid position initial offset");
    }
};

// Template keeps missing-property/type-error fixtures hardware-free. Production
// passes IVRSettings; no defaults file, live settings or SDK properties are written.
template<class Settings>
PositionContinuityConfig load_position_continuity_config(Settings& settings) {
    constexpr const char* section = "driver_psvrpc";
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    PositionContinuityConfig config;
    const bool enabled = settings.GetBool(section,"positionFallback3DofEnabled",&error);
    if (error == vr::VRSettingsError_None) config.fallback_3dof_enabled = enabled;
    else if (error != vr::VRSettingsError_UnsetSettingHasNoDefault)
        throw std::runtime_error("cannot read positionFallback3DofEnabled");
    const bool angular = settings.GetBool(section,"positionAngularCenterEnabled",&error);
    if (error == vr::VRSettingsError_None) config.angular_center_enabled = angular;
    else if (error != vr::VRSettingsError_UnsetSettingHasNoDefault)
        throw std::runtime_error("cannot read positionAngularCenterEnabled");
    const auto blend = settings.GetInt32(section,"positionReacquireBlendMs",&error);
    if (error == vr::VRSettingsError_None) config.reacquire_blend_ms = blend;
    else if (error != vr::VRSettingsError_UnsetSettingHasNoDefault)
        throw std::runtime_error("cannot read positionReacquireBlendMs");
    const auto optional_float = [&](const char* key, double fallback) {
        const auto value = settings.GetFloat(section,key,&error);
        if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) return fallback;
        if (error != vr::VRSettingsError_None) throw std::runtime_error(std::string("cannot read ")+key);
        return static_cast<double>(value);
    };
    config.reacquire_max_speed_mps = optional_float("positionReacquireMaxSpeedMps",1.5);
    config.initial_offset = {optional_float("positionInitialOffsetX",0),
        optional_float("positionInitialOffsetY",0),optional_float("positionInitialOffsetZ",0)};
    config.validate();
    return config;
}

// Pure controller. Hmd's mutex protects it. ONLY RunFrame/publish calls update;
// queries use preview (no timer/history mutation, no decay on query frequency).
// Optical validity is supplied separately from a grace-authorized bridge view.
// Reliability of the camera packets, origin and epoch remains PositionBridge's
// responsibility; this class NEVER ingests or generates an optical packet.
class PositionContinuity {
    PositionContinuityConfig config_;
    bool identity_set_ = false, have_published_ = false, degraded_ = false;
    std::string session_;
    unsigned epoch_ = 0;
    Vec3 published_{}, correction_{};
    double correction_rate_ = 0, duration_ms_ = 0, elapsed_ms_ = 0;
    DriverClock::time_point last_update_{};
    bool have_update_ = false;
    bool angular_active_ = false;
    Vec3 angular_anchor_{};
    static bool finite(Vec3 p) { return std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z); }
    bool matches(const std::string& session, unsigned epoch) const {
        return identity_set_ && session == session_ && epoch == epoch_;
    }
    Vec3 target(const TrackingSnapshot& s) const {
        // Avoid even adding +0 in the legacy default path (signed-zero exactness).
        return offset_provided() ? s.position + config_.initial_offset : s.position;
    }
    void clear_blend() { correction_ = {}; correction_rate_ = duration_ms_ = elapsed_ms_ = 0; }
    static double yaw_degrees(const TrackingSnapshot& s) {
        const auto forward = s.orientation.rotate({0,0,-1});
        return std::abs(std::atan2(forward.x,-forward.z)*degrees);
    }
    bool wants_angular(const TrackingSnapshot& s, bool measured) const {
        return config_.angular_center_enabled && s.position_enabled &&
            (yaw_degrees(s) >= 90 || (angular_active_ && (!measured || yaw_degrees(s) > 85)));
    }
    bool continuity_enabled() const { return config_.fallback_3dof_enabled || config_.angular_center_enabled; }
public:
    explicit PositionContinuity(PositionContinuityConfig config = {}) : config_(config) { config_.validate(); }
    void reset() {
        identity_set_ = have_published_ = have_update_ = degraded_ = false;
        session_.clear(); epoch_ = 0; published_ = {}; last_update_ = {}; clear_blend();
        angular_active_ = false; angular_anchor_ = {};
    }
    bool offset_provided() const {
        return config_.initial_offset.x != 0 || config_.initial_offset.y != 0 || config_.initial_offset.z != 0;
    }
    const PositionContinuityConfig& config() const { return config_; }

    TrackingSnapshot preview(TrackingSnapshot s, const std::string& session,
            DriverClock::time_point now, int stale_ms, bool display_valid,
            bool optical_valid, bool grace_holding) const {
        s.position_fallback_3dof = false;
        s.position_fallback_policy_enabled = continuity_enabled() && s.position_enabled;
        s.has_initial_position_offset = offset_provided();
        s.tracking_mode = TrackingMode::invalid;
        s.optical_position_valid = false;
        s.angular_centering = false; s.angular_center_weight = 0;
        s.recovery_offset = {}; s.recovery_duration_ms = s.recovery_elapsed_ms = 0;
        if (!display_valid || !fresh_orientation(s, now, stale_ms)) return s;
        if (!s.position_enabled) {
            s.position = config_.initial_offset;
            s.tracking_mode = TrackingMode::orientation_only;
            return s;
        }
        const bool same = matches(session, s.recenter_epoch);
        if (!s.position_coherent) return s; // Never mix incoherent epochs/frames.
        const bool measured = optical_valid && s.has_position && finite(s.position);
        s.optical_position_valid = measured;
        if (!session.empty() && wants_angular(s,measured)) {
            s.has_position = false; // Intentional render policy, never an optical measurement.
            s.position = same && have_published_ ? published_ : config_.initial_offset;
            s.position_fallback_3dof = true;
            s.tracking_mode = TrackingMode::fallback_3dof;
            s.angular_centering = true;
            const double u = std::clamp((yaw_degrees(s)-90)/30,0.0,1.0);
            s.angular_center_weight = !measured && yaw_degrees(s) < 90 ? 1 : u*u*(3-2*u);
            return s;
        }
        if (!continuity_enabled() || (!config_.fallback_3dof_enabled && !angular_active_)) {
            if (s.has_position && finite(s.position)) {
                s.position = target(s);
                s.tracking_mode = grace_holding ? TrackingMode::position_grace : TrackingMode::sixdof;
            }
            return s; // Strict legacy validity/hold policy unchanged.
        }
        if (session.empty()) { s.has_position = false; s.position_coherent = false; return s; }
        if (!measured) {
            if (!config_.fallback_3dof_enabled) {
                if (s.has_position && finite(s.position)) {
                    s.position = target(s);
                    s.tracking_mode = grace_holding ? TrackingMode::position_grace : TrackingMode::sixdof;
                }
                return s; // Forward optical loss retains the existing bounded grace.
            }
            // Opt-in ignores grace translation: direct explicit 3DoF fallback.
            s.has_position = false;
            s.position = same && have_published_ ? published_ : config_.initial_offset;
            s.position_fallback_3dof = true;
            s.tracking_mode = TrackingMode::fallback_3dof;
        } else if (same && have_published_ && degraded_ && config_.reacquire_blend_ms > 0) {
            s.position = published_; // Await the next actual publish to arm blend.
            s.tracking_mode = TrackingMode::reacquiring;
        } else {
            s.position = target(s);
            if (same && correction_.norm() > 0) {
                s.position = s.position + correction_;
                s.tracking_mode = TrackingMode::reacquiring;
                s.recovery_offset = correction_;
                s.recovery_duration_ms = duration_ms_; s.recovery_elapsed_ms = elapsed_ms_;
            } else s.tracking_mode = TrackingMode::sixdof;
        }
        return s;
    }

    TrackingSnapshot update(TrackingSnapshot s, const std::string& session,
            DriverClock::time_point now, int stale_ms, bool display_valid,
            bool optical_valid, bool grace_holding) {
        if (!matches(session, s.recenter_epoch)) {
            reset(); identity_set_ = true; session_ = session; epoch_ = s.recenter_epoch;
        }
        // A regressed clock is not an elapsed-time blend or a cross-clock cache.
        if (have_update_ && now < last_update_) {
            reset(); identity_set_ = true; session_ = session; epoch_ = s.recenter_epoch;
        }
        const double dt = have_update_ ? std::chrono::duration<double>(now-last_update_).count() : 0;
        last_update_ = now; have_update_ = true;
        auto out = preview(s, session, now, stale_ms, display_valid, optical_valid, grace_holding);
        if (out.tracking_mode == TrackingMode::invalid) {
            degraded_ = true; clear_blend();
            // An explicitly incoherent context cannot retain a baseline.
            if (!s.position_coherent || !display_valid) {
                have_published_ = false; angular_active_ = false; angular_anchor_ = {};
            }
            return out;
        }
        if (!s.position_enabled || !continuity_enabled()) return out;
        const bool measured = optical_valid && s.has_position && finite(s.position);
        if (!session.empty() && wants_angular(s,measured)) {
            if (!angular_active_) {
                angular_anchor_ = have_published_ ? published_-config_.initial_offset : Vec3{};
                angular_active_ = true;
            }
            const double u = std::clamp((yaw_degrees(s)-90)/30,0.0,1.0);
            const double weight = !measured && yaw_degrees(s) < 90 ? 1 : u*u*(3-2*u);
            const Vec3 desired = config_.initial_offset + angular_anchor_*(1-weight);
            const Vec3 previous = have_published_ ? published_ : config_.initial_offset;
            const Vec3 delta = desired-previous;
            const double length = delta.norm();
            const double maximum_step = .25*std::max(0.0,dt); // Only intentional centering, not head motion.
            out.position = length > maximum_step && length > 0
                ? previous + delta*(maximum_step/length) : desired;
            out.has_position = false; out.position_fallback_3dof = true;
            out.tracking_mode = TrackingMode::fallback_3dof;
            out.angular_centering = true; out.angular_center_weight = weight;
            degraded_ = true; clear_blend();
            published_ = out.position; have_published_ = true;
            return out;
        }
        if (!config_.fallback_3dof_enabled && !angular_active_) {
            // Merely enabling rear centering must not add recovery filtering to
            // every ordinary FRONT optical dropout. Preserve strict 6DoF here,
            // while remembering its published point for a future angular entry.
            degraded_ = false; clear_blend();
            published_ = out.position; have_published_ = true;
            return out;
        }
        if (out.tracking_mode == TrackingMode::fallback_3dof) {
            degraded_ = true; clear_blend();
        } else {
            const Vec3 desired = target(s);
            if (degraded_ && have_published_ && config_.reacquire_blend_ms > 0) {
                correction_ = published_ - desired;
                const double length = correction_.norm();
                duration_ms_ = std::max(static_cast<double>(config_.reacquire_blend_ms),
                    1000 * length / config_.reacquire_max_speed_mps);
                elapsed_ms_ = 0;
                correction_rate_ = length > 0 ? 1000 * length / duration_ms_ : 0;
                out.position = published_; // Exact first-publish continuity.
            } else {
                if (config_.reacquire_blend_ms == 0) clear_blend(); // Explicit disabled blend.
                const double length = correction_.norm();
                if (length > 0 && dt > 0) {
                    const double remaining = elapsed_ms_ + dt * 1000 >= duration_ms_ - 1e-9
                        ? 0 : std::max(0.0, length - correction_rate_ * dt);
                    correction_ = correction_ * (remaining / length);
                    elapsed_ms_ = std::min(duration_ms_, elapsed_ms_ + dt * 1000);
                }
                out.position = desired + correction_;
            }
            degraded_ = false;
            // Only the correction offset's speed is limited. A moving optical
            // target is followed WITHOUT fake velocity clamping or extra lag.
            out.tracking_mode = correction_.norm() > 0 ? TrackingMode::reacquiring : TrackingMode::sixdof;
            out.recovery_offset = correction_;
            out.recovery_duration_ms = duration_ms_; out.recovery_elapsed_ms = elapsed_ms_;
        }
        published_ = out.position; have_published_ = true;
        if (out.tracking_mode == TrackingMode::sixdof) angular_active_ = false;
        return out;
    }
};
} // namespace psvr
