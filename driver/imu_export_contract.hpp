// SPDX-License-Identifier: MIT
#pragma once
#include "pose_bridge.hpp"
#include <nlohmann/json.hpp>
#include <atomic>
#include <random>
#include <string>

namespace psvr {
inline constexpr const char* imu_export_frame = "psvr_imu_unrecentered";
struct ImuExportConfig {
    bool enabled = false;
    std::uint16_t port = 4272;
    int hz = 100;
};
inline double driver_seconds(DriverClock::time_point t) {
    return std::chrono::duration<double>(t.time_since_epoch()).count();
}
inline std::string new_imu_session() {
    static std::atomic<std::uint64_t> counter{0};
    std::random_device random;
    return std::to_string(random()) + "-" + std::to_string(random()) + "-" +
        std::to_string(DriverClock::now().time_since_epoch().count()) + "-" +
        std::to_string(++counter);
}
inline bool imu_unit(Quaternion q) {
    return std::isfinite(q.w) && std::isfinite(q.x) && std::isfinite(q.y) &&
        std::isfinite(q.z) && std::abs(q.norm()-1) <= 1e-3;
}
// No dependency on driver_pose(): optical startup must not gate IMU validity.
inline std::string encode_driver_imu(const TrackingSnapshot& s, const std::string& session,
        std::uint64_t seq, DriverClock::time_point now, int stale_ms = 100) {
    if (session.empty() || session.size() > 128 || session.find('\0') != std::string::npos || !imu_unit(s.qabs) ||
        !imu_unit(s.world_rotation) || !std::isfinite(s.corrected_gyro.x) ||
        !std::isfinite(s.corrected_gyro.y) || !std::isfinite(s.corrected_gyro.z)) return {};
    const double sent = driver_seconds(now);
    const double sample = s.has_sample ? driver_seconds(s.sampled_at) : sent;
    if (!std::isfinite(sent) || !std::isfinite(sample) || sample < 0 || sample > sent) return {};
    const bool valid = s.connected && s.state == ImuState::tracking && s.has_sample &&
        sent-sample <= stale_ms*.001;
    const auto q = [](Quaternion v) { return nlohmann::json::array({v.w,v.x,v.y,v.z}); };
    auto text = nlohmann::json{{"protocol","psvr.imu"},{"version",1},
        {"frame",imu_export_frame},{"clock","driver_steady_seconds"},
        {"session_id",session},{"seq",seq},{"recenter_epoch",s.recenter_epoch},
        {"sample_time_s",sample},{"sent_time_s",sent},{"connected",s.connected},
        {"valid",valid},{"state",state_name(s.state)},
        {"quaternion_sensor_wxyz",q(s.qabs)},{"world_rotation_wxyz",q(s.world_rotation)},
        {"corrected_gyro_rad_s",{s.corrected_gyro.x,s.corrected_gyro.y,s.corrected_gyro.z}}}.dump();
    return text.size() <= 2048 ? text : std::string{};
}
} // namespace psvr
