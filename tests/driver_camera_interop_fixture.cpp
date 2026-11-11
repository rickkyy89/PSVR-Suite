// SPDX-License-Identifier: MIT
// CLI: driver_camera_interop_fixture <Python IMU UDP port>. No HID/runtime.
#include "imu_exporter.hpp" // Winsock before any Windows headers
#include <cmath>
#include <iostream>
#include <stdexcept>

namespace {
using namespace psvr;
constexpr unsigned epoch = 3;
const std::string session = "offline-driver-camera-interop";
TrackingSnapshot fresh_snapshot() {
    TrackingSnapshot s;
    s.connected = true;
    s.state = ImuState::tracking;
    s.has_sample = true;
    s.sampled_at = DriverClock::now();
    s.recenter_epoch = epoch;
    s.qabs = {1, 0, 0, 0};
    const double half = std::sqrt(.5);
    s.world_rotation = {half, 0, half, 0}; // +90 degrees about Y
    s.orientation = s.world_rotation * s.qabs;
    return s;
}
void emit(const nlohmann::json& row) {
    std::cout << row.dump() << std::endl; // flush each JSON-line reply
}
} // namespace

int main(int argc, char** argv) {
    using namespace psvr;
    try {
        if (argc != 2) throw std::runtime_error("expected Python IMU UDP port");
        std::size_t used = 0;
        const int port = std::stoi(argv[1], &used);
        if (argv[1][used] != '\0' || port < 1 || port > 65535)
            throw std::runtime_error("invalid Python IMU UDP port");
        PositionConfig config;
        config.enabled = true;
        config.port = 0;
        config.require_bound_v2 = true;
        config.timeout_ms = 1000; // room for CI scheduling, not a timing benchmark
        config.camera_yaw_offset_deg = 37; // v2 must ignore this legacy setting
        PositionBridge bridge(config);
        const auto initial = fresh_snapshot();
        // Set the epoch floor BEFORE export. Same-epoch binds update only now/sample.
        bridge.bind_driver(session, epoch, driver_seconds(initial.sampled_at),
                           true, initial.sampled_at);
        unsigned replies = 0;
        PositionReceiver receiver(config, [&](const PositionSample& incoming) {
            auto s = fresh_snapshot();
            bridge.bind_driver(session, epoch, driver_seconds(s.sampled_at), true, s.sampled_at);
            const bool accepted = bridge.ingest(incoming);
            const auto now = DriverClock::now();
            const auto view = bridge.view(now, epoch, s.world_rotation);
            s.position_enabled = view.enabled;
            s.has_position = view.valid;
            s.position_coherent = view.coherent;
            s.position = view.position;
            s.position_at = view.received_at;
            const auto pose = driver_pose(s, now);
            emit({{"event", "pose"}, {"seq", incoming.seq}, {"accepted", accepted},
                  {"pose_valid", pose.poseIsValid}, {"connected", pose.deviceIsConnected},
                  {"position", {pose.vecPosition[0], pose.vecPosition[1], pose.vecPosition[2]}},
                  {"orientation_wxyz", {pose.qRotation.w, pose.qRotation.x,
                                        pose.qRotation.y, pose.qRotation.z}}});
            ++replies;
        });
        ImuExportConfig export_config;
        export_config.enabled = true;
        export_config.port = static_cast<std::uint16_t>(port);
        export_config.hz = 100;
        ImuExporter exporter(export_config, session, fresh_snapshot);
        emit({{"event", "ready"}, {"port", receiver.local_port()},
              {"session", session}, {"epoch", epoch}});
        const auto deadline = DriverClock::now() + std::chrono::seconds(9);
        while (replies < 8 && DriverClock::now() < deadline) {
            if (receiver.poll(25) < 0) throw std::runtime_error("receiver fatal error");
        }
        exporter.stop(); // worker joined before receiver/socket destruction
        if (replies != 8) throw std::runtime_error("deadline: expected eight position packets");
        emit({{"event", "done"}, {"replies", replies}});
        return 0;
    } catch (const std::exception& error) {
        emit({{"event", "error"}, {"message", error.what()}});
        return 1;
    }
}
