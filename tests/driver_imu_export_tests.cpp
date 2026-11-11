// SPDX-License-Identifier: MIT
// Synthetic fusion and real loopback UDP only; no runtime/HID/settings access.
#include "imu_exporter.hpp"
#include <iostream>
#include <limits>

namespace {
using namespace psvr;
using namespace std::chrono_literals;
void check(bool b, const char* text) { if (!b) throw std::runtime_error(text); }
std::array<unsigned char,64> report(unsigned i, int gyro = 0) {
    std::array<unsigned char,64> b{}; b[63] = static_cast<unsigned char>(i);
    for (unsigned s=0; s<2; ++s) {
        const auto tick = i*1000+s*500; const auto base = 16+s*16;
        for (unsigned j=0; j<4; ++j) b[base+j] = static_cast<unsigned char>(tick>>(8*j));
        b[base+4] = static_cast<unsigned char>(gyro); b[base+5] = static_cast<unsigned char>(gyro>>8);
        b[base+11] = 64;
    }
    return b;
}
void codec_and_fusion() {
    const auto t = DriverClock::time_point{}+10s;
    TrackingSnapshot s; s.connected = true; s.has_sample = true; s.state = ImuState::tracking;
    s.sampled_at = t; s.position_enabled = true; s.has_position = false;
    auto j = nlohmann::json::parse(encode_driver_imu(s,"session",1,t+1ms));
    check(j["valid"] == true && !driver_pose(s,t+1ms).poseIsValid,"IMU validity independent from 6DoF");
    check(j["frame"] == "psvr_imu_unrecentered" && j["clock"] == "driver_steady_seconds","wire names");
    check(j["sample_time_s"] == 10.0 && j["sent_time_s"].get<double>() >= 10.0,"driver clock");
    check(nlohmann::json::parse(encode_driver_imu(s,"session",2,t+101ms))["valid"] == false,"stale IMU invalid");
    s.connected = false;
    check(nlohmann::json::parse(encode_driver_imu(s,"session",2,t+1ms))["valid"] == false,"disconnected IMU invalid");
    s.connected = true; s.state = ImuState::calibrating;
    check(nlohmann::json::parse(encode_driver_imu(s,"session",2,t+1ms))["valid"] == false,"calibrating IMU invalid");
    s.state = ImuState::tracking;
    check(encode_driver_imu(s,std::string(129,'x'),3,t).empty(),"bounded session");
    check(encode_driver_imu(s,"session",3,t-1ms).empty(),"future sample cannot serialize");
    s.qabs.w = 2; check(encode_driver_imu(s,"session",4,t).empty(),"unit quaternion required");
    s.qabs = {}; s.corrected_gyro.x = std::numeric_limits<double>::quiet_NaN();
    check(encode_driver_imu(s,"session",5,t).empty(),"finite gyro required");
    check(new_imu_session() != new_imu_session(),"activation session unique");
    ImuConfig config; config.startup_discard_ms = 0; config.calibration_ms = 100;
    config.recenter_mode = "full";
    DriverTracker tracker(config,t);
    for (unsigned i=0; i<300; ++i) {
        auto b = report(i,i >= 200 ? 1000 : 0); tracker.packet(b.data(),b.size(),t+std::chrono::milliseconds(i));
    }
    const auto before = tracker.snapshot(); check(before.state == ImuState::tracking,"synthetic fusion tracks");
    check(tracker.recenter(),"synthetic recenter"); const auto after = tracker.snapshot();
    const auto dot = before.qabs.w*after.qabs.w+before.qabs.x*after.qabs.x+
        before.qabs.y*after.qabs.y+before.qabs.z*after.qabs.z;
    check(std::abs(std::abs(dot)-1) < 1e-9,"absolute quaternion preserved across full head-offset recenter");
    check(after.recenter_epoch > before.recenter_epoch,"epoch atomic with world reference");
    check(std::abs(after.qabs.y) > .01 && std::abs(after.orientation.y) < 1e-9,
        "export is physical absolute quaternion, not neutral-head output");
    const auto sensor = after.world_rotation * after.qabs;
    check(imu_unit(sensor) && std::abs(after.corrected_gyro.y) > .5,"physical gyro exported");
}
void held_position_prediction() {
    const auto now = DriverClock::time_point{}+10s;
    TrackingSnapshot s; s.connected = true; s.has_sample = true; s.state = ImuState::tracking;
    s.sampled_at = now-1ms; s.orientation = rotation_vector({0,.2,0});
    s.angular_velocity = {1,2,3}; s.position_enabled = true; s.has_position = true;
    s.position = {.1,.2,.3}; s.position_at = now-80ms;
    const auto p = driver_pose(s,now);
    check(p.poseIsValid && std::abs(p.poseTimeOffset+.001)<1e-9,
        "80ms held position uses only 1ms IMU prediction offset");
    check(p.qRotation.y == s.orientation.y && p.vecPosition[0] == .1,
        "current orientation and held translation retained");
    check(p.vecAngularVelocity[0] == 1 && p.vecAngularVelocity[1] == 2 && p.vecAngularVelocity[2] == 3,
        "held position retains current angular velocity");
    s.state = ImuState::calibrating;
    check(!driver_pose(s,now).poseIsValid,"position hold cannot mask invalid IMU");
    s.state = ImuState::tracking; s.sampled_at = now-101ms;
    const auto stale = driver_pose(s,now);
    check(!stale.poseIsValid && stale.vecAngularVelocity[1] == 0,"IMU stale gate remains upstream");
}
void udp_lifetime() {
    WinsockSession ws; check(ws.ok(),"Winsock");
    SOCKET receiver = ::socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
    check(receiver != INVALID_SOCKET,"receiver socket");
    struct Guard { SOCKET s; ~Guard() { ::closesocket(s); } } guard{receiver};
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    check(::bind(receiver,reinterpret_cast<sockaddr*>(&address),sizeof(address)) == 0,"ephemeral bind");
    int length = sizeof(address); check(::getsockname(receiver,reinterpret_cast<sockaddr*>(&address),&length)==0,"bound port");
    DWORD timeout = 500; ::setsockopt(receiver,SOL_SOCKET,SO_RCVTIMEO,reinterpret_cast<char*>(&timeout),sizeof(timeout));
    ImuExportConfig config; config.enabled = true; config.port = ntohs(address.sin_port);
    std::atomic<unsigned> copies{0};
    const auto source = [&] { ++copies; TrackingSnapshot s; s.connected = true; s.has_sample = true;
        s.state = ImuState::tracking; s.sampled_at = DriverClock::now(); return s; };
    {
        ImuExporter exporter(config,"udp-activation",source);
        std::uint64_t seq = 0;
        const auto start = DriverClock::now();
        for (int i=0; i<4; ++i) {
            std::array<char,2048> b{}; const auto n = ::recv(receiver,b.data(),static_cast<int>(b.size()),0);
            check(n > 0,"loopback packet received"); const auto j = nlohmann::json::parse(b.data(),b.data()+n);
            check(j["session_id"] == "udp-activation" && j["valid"] == true,"bound valid export");
            check(j["seq"].get<std::uint64_t>() > seq,"sequence advances"); seq = j["seq"].get<std::uint64_t>();
        }
        check(DriverClock::now()-start >= 20ms,"100Hz bounded cadence");
        const auto end = DriverClock::now(); exporter.stop(); exporter.stop();
        check(DriverClock::now()-end < 200ms,"idempotent bounded join");
        const auto count = copies.load(); std::this_thread::sleep_for(30ms);
        check(copies == count,"no callback after stop/join");
    }
    config.enabled = false;
    const auto count = copies.load(); { ImuExporter disabled(config,"disabled",source); }
    check(copies == count,"disabled default starts no owner");
    config.enabled = true; { ImuExporter restarted(config,"new-activation",source); std::this_thread::sleep_for(20ms); }
    check(copies > count,"restart creates new owner");
}
void recenter_prediction_frame() {
    const auto t = DriverClock::time_point{}+10s;
    for (const char* mode : {"yaw","full"}) {
        for (unsigned raw_axis : {1u,2u}) { // raw 1 -> body X; raw 2 -> body -Z
            ImuConfig config; config.startup_discard_ms = 0; config.calibration_ms = 100;
            config.recenter_mode = mode; config.gravity_gain = 0; config.smoothing_ms = 20;
            DriverTracker tracker(config,t);
            unsigned i = 0;
            for (; i<350; ++i) {
                const auto b = report(i,i >= 200 ? 1000 : 0); // establish a nonzero yaw
                tracker.packet(b.data(),b.size(),t+std::chrono::milliseconds(i));
            }
            for (; i<430; ++i) {
                auto b = report(i);
                for (unsigned s=0; s<2; ++s) {
                    const unsigned base = 16+s*16+4+raw_axis*2;
                    b[base] = static_cast<unsigned char>(1000); b[base+1] = static_cast<unsigned char>(1000>>8);
                }
                tracker.packet(b.data(),b.size(),t+std::chrono::milliseconds(i));
            }
            const auto before = tracker.snapshot();
            check(before.state == ImuState::tracking && before.angular_velocity.norm() > .8,
                "synthetic X/Z rotation has nonzero prediction EMA");
            check(tracker.recenter(),"recenter during X/Z rotation");
            const auto after = tracker.snapshot();
            // Angular velocity is a world-frame vector. The right-hand head
            // offset and pose-smoothing reset must not tilt its EMA history.
            const auto delta = (after.world_rotation * before.world_rotation.conjugate()).normalized();
            const auto expected = delta.rotate(before.angular_velocity);
            check((expected-before.angular_velocity).norm() > .05,"fixture exposes an actual X/Z frame change");
            check((after.angular_velocity-expected).norm() < 1e-9,"recenter rotates snapshot angular velocity");
            check(std::abs(after.angular_velocity.norm()-before.angular_velocity.norm()) < 1e-9,
                "recenter preserves smoothed rate magnitude");
            const auto pose = driver_pose(after,after.sampled_at);
            check(pose.poseIsValid && std::abs(pose.vecAngularVelocity[0]-expected.x) < 1e-9 &&
                std::abs(pose.vecAngularVelocity[2]-expected.z) < 1e-9,"3DoF publishes coherent X/Z prediction");
            const auto b = report(i);
            tracker.packet(b.data(),b.size(),t+std::chrono::milliseconds(i)); // next gyro is zero
            const auto next = tracker.snapshot();
            const double a = 1.0-std::exp(-.001/.015);
            const auto sensor_world = next.world_rotation*next.qabs;
            const auto expected_next = expected*(1-a)+sensor_world.rotate(next.corrected_gyro)*a;
            check((next.angular_velocity-expected_next).norm() < 1e-9,
                "next report blends transformed EMA, never the retired frame");
        }
    }
}
}
int main() {
    try { codec_and_fusion(); held_position_prediction(); recenter_prediction_frame(); udp_lifetime(); std::cout << "PASS driver IMU codec/fusion/held-position/recenter prediction/UDP/lifetime\n"; return 0; }
    catch (const std::exception& e) { std::cerr << "FAIL " << e.what() << '\n'; return 1; }
}
