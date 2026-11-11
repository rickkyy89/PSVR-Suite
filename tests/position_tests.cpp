// SPDX-License-Identifier: MIT
// M9 position tests. Two halves:
//  - pure: strict validation of the shared "psvr.position" v1 wire contract,
//    seated frame offset, sequence handling, recenter epoch coherence and local
//    monotonic freshness;
//  - live: a real loopback UDP receiver lifecycle (bind, receive, reject,
//    recover, stop/join, RAII restart).
// No OpenVR runtime, no hardware, no SteamVR package installation.
#include "position_receiver.hpp"
#include "pose_bridge.hpp"
#include <nlohmann/json.hpp>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
using namespace std::chrono_literals;
using json = nlohmann::json;

void check(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
void close_to(double a, double b, const char* message) { check(std::abs(a - b) < 1e-9, message); }

bool wait_until(const std::function<bool()>& predicate, int timeout_ms) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        if (predicate()) return true;
        std::this_thread::sleep_for(5ms);
    }
    return predicate();
}

psvr::PositionConfig parse_config(double max_abs = 10.0) {
    psvr::PositionConfig config;
    config.enabled = true;
    config.max_abs_m = max_abs;
    return config;
}

json base_packet() {
    return {
        {"protocol", "psvr.position"}, {"version", 1}, {"frame", "psvr_world"}, {"units", "m"},
        {"seq", 0}, {"host_time_ns", 123}, {"monotonic_s", 0.0}, {"valid", true},
        {"state", "tracking"}, {"source", "aruco"}, {"position_m", {0.1, 0.2, -0.5}},
        {"orientation_wxyz", nullptr}, {"reprojection_px", nullptr}, {"inliers", nullptr},
        {"ambiguity_ratio", nullptr}, {"note", nullptr}
    };
}

std::optional<psvr::PositionSample> parse(const std::string& text, double max_abs = 10.0) {
    return psvr::parse_position_packet(text, parse_config(max_abs), psvr::PositionClock::now());
}

std::optional<psvr::PositionSample> parse_packet(const json& packet, double max_abs = 10.0) {
    return parse(packet.dump(), max_abs);
}

void test_protocol() {
    // Exact compact wire form the Python sender emits (sorted keys, no spaces).
    const std::string exact =
        R"({"ambiguity_ratio":null,"frame":"psvr_world","host_time_ns":123,"inliers":null,)"
        R"("monotonic_s":0.0,"note":null,"orientation_wxyz":null,"position_m":[0.1,0.2,-0.5],)"
        R"("protocol":"psvr.position","reprojection_px":null,"seq":0,"source":"aruco",)"
        R"("state":"tracking","units":"m","valid":true,"version":1})";
    check(base_packet().dump() == exact, "test fixture matches the sender's compact encoding");
    const auto sample = parse(exact);
    check(sample.has_value(), "exact sender packet accepted");
    check(sample->seq == 0 && sample->valid, "seq/valid parsed");
    check(sample->state == psvr::PositionPacketState::tracking, "state parsed");
    check(sample->source == psvr::PositionSource::aruco, "source parsed");
    close_to(sample->position_m.x, 0.1, "axis X preserved");
    close_to(sample->position_m.y, 0.2, "axis Y preserved");
    close_to(sample->position_m.z, -0.5, "axis Z preserved");

    {
        auto packet = base_packet(); packet["protocol"] = "psvr.position.v2";
        check(!parse_packet(packet), "wrong protocol rejected");
    }
    { auto packet = base_packet(); packet["version"] = 2; check(!parse_packet(packet), "wrong version rejected"); }
    { auto packet = base_packet(); packet["frame"] = "opengl"; check(!parse_packet(packet), "wrong frame rejected"); }
    { auto packet = base_packet(); packet["units"] = "cm"; check(!parse_packet(packet), "wrong units rejected"); }
    { auto packet = base_packet(); packet.erase("version"); check(!parse_packet(packet), "missing version rejected"); }
    { auto packet = base_packet(); packet["seq"] = -1; check(!parse_packet(packet), "negative seq rejected"); }
    { auto packet = base_packet(); packet["seq"] = 1.5; check(!parse_packet(packet), "float seq rejected"); }
    { auto packet = base_packet(); packet["seq"] = "1"; check(!parse_packet(packet), "string seq rejected"); }
    { auto packet = base_packet(); packet["monotonic_s"] = -0.1; check(!parse_packet(packet), "negative monotonic_s rejected"); }
    { auto packet = base_packet(); packet["valid"] = 1; check(!parse_packet(packet), "non-bool valid rejected"); }
    { auto packet = base_packet(); packet["state"] = "bogus"; check(!parse_packet(packet), "unknown state rejected"); }
    { auto packet = base_packet(); packet["source"] = "bogus"; check(!parse_packet(packet), "unknown source rejected"); }
    // Cross-field consistency: a usable frame must be tracking. lost/error/
    // ambiguous are documented as valid=false, so valid=true with one of them
    // (or calibrating) is contradictory and must never become a position.
    { auto packet = base_packet(); packet["state"] = "lost";
      check(!parse_packet(packet), "valid=true with lost state rejected"); }
    { auto packet = base_packet(); packet["state"] = "ambiguous";
      check(!parse_packet(packet), "valid=true with ambiguous state rejected"); }
    { auto packet = base_packet(); packet["state"] = "error";
      check(!parse_packet(packet), "valid=true with error state rejected"); }
    { auto packet = base_packet(); packet["state"] = "calibrating";
      check(!parse_packet(packet), "valid=true with calibrating state rejected"); }
    { auto packet = base_packet(); packet["valid"] = false; packet["state"] = "tracking";
      packet["position_m"] = nullptr;
      check(parse_packet(packet).has_value(), "tracking with valid=false tolerated"); }
    { auto packet = base_packet(); packet["host_time_ns"] = -1; check(!parse_packet(packet), "negative host_time_ns rejected"); }
    { auto packet = base_packet(); packet["host_time_ns"] = 1.5; check(!parse_packet(packet), "float host_time_ns rejected"); }
    { auto packet = base_packet(); packet["position_m"] = nullptr; check(!parse_packet(packet), "valid needs position"); }
    { auto packet = base_packet(); packet["position_m"] = {0.0, 0.0}; check(!parse_packet(packet), "short position rejected"); }
    { auto packet = base_packet(); packet["position_m"] = {100.0, 0.0, 0.0}; check(!parse_packet(packet), "out of bounds rejected"); }
    { auto packet = base_packet(); packet["orientation_wxyz"] = {1.0, 0.0, 0.0, 0.0};
      check(parse_packet(packet).has_value(), "unit orientation accepted"); }
    { auto packet = base_packet(); packet["orientation_wxyz"] = {2.0, 0.0, 0.0, 0.0};
      check(!parse_packet(packet), "non-unit orientation rejected"); }
    { auto packet = base_packet(); packet["reprojection_px"] = -1.0; check(!parse_packet(packet), "negative reprojection rejected"); }
    { auto packet = base_packet(); packet["inliers"] = -1; check(!parse_packet(packet), "negative inliers rejected"); }
    { auto packet = base_packet(); packet["inliers"] = 2.5; check(!parse_packet(packet), "float inliers rejected"); }
    { auto packet = base_packet(); packet["note"] = 5; check(!parse_packet(packet), "non-string note rejected"); }
    { auto packet = base_packet(); packet["extra"] = 1; check(parse_packet(packet).has_value(), "unknown key tolerated"); }
    { auto packet = base_packet(); packet["valid"] = false; packet["state"] = "lost";
      packet["position_m"] = nullptr;
      check(parse_packet(packet).has_value(), "invalid packet with null position accepted"); }
    { auto packet = base_packet(); packet["valid"] = false; packet["state"] = "ambiguous";
      check(parse_packet(packet).has_value(), "invalid packet with stale position accepted"); }

    check(!parse(""), "empty rejected");
    check(!parse("not json"), "non-json rejected");
    check(!parse(std::string(2000, 'x')), "oversized rejected");
    std::string nul = base_packet().dump(); nul.push_back('\0'); nul.push_back('x');
    check(!parse(nul), "raw NUL suffix rejected");
    std::string duplicate = base_packet().dump();
    duplicate.insert(1, R"("protocol":"psvr.position",)");
    check(!parse(duplicate), "duplicate key rejected");
    std::string deep = base_packet().dump();
    deep.pop_back();
    deep += R"(,"deep":)"; deep += std::string(12, '['); deep += '1'; deep += std::string(12, ']'); deep += '}';
    check(!parse(deep), "excessive nesting rejected");

    const auto seated = psvr::seated_position({1, 2, 3}, {0.5, 0.5, 0.5});
    close_to(seated.x, 0.5, "seated X offset");
    close_to(seated.y, 1.5, "seated Y offset");
    close_to(seated.z, 2.5, "seated Z offset");
}

psvr::PositionSample valid_sample(std::uint64_t seq, psvr::Vec3 position,
                                  psvr::PositionClock::time_point received_at) {
    psvr::PositionSample sample;
    sample.valid = true;
    sample.seq = seq;
    sample.position_m = position;
    sample.state = psvr::PositionPacketState::tracking;
    sample.source = psvr::PositionSource::aruco;
    sample.received_at = received_at;
    return sample;
}

void test_sequence_and_freshness() {
    psvr::PositionConfig config = parse_config();
    config.timeout_ms = 1000;
    psvr::PositionBridge bridge(config);
    const auto t0 = psvr::PositionClock::now();

    check(bridge.ingest(valid_sample(1, {1, 2, 3}, t0)), "first sample accepted");
    auto view = bridge.view(t0 + 50ms, 0);
    check(view.enabled && view.valid && view.state == psvr::PositionState::tracking, "fresh sample valid");
    check(view.coherent, "epoch 0 coherent");
    // The first valid sample fixes the seated origin, so the raw camera
    // coordinates (whose origin is the camera) never leak as a pose.
    close_to(view.position.x, 0, "startup origin zeroes X");
    close_to(view.position.y, 0, "startup origin zeroes Y");
    close_to(view.position.z, 0, "startup origin zeroes Z");

    check(bridge.view(t0 + 1001ms, 0).state == psvr::PositionState::lost, "stale sample lost");
    check(!bridge.view(t0 + 1001ms, 0).valid, "stale sample invalid");
    check(bridge.view(t0 + 999ms, 0).valid, "just-inside timeout valid");

    // A valid=false packet advances the sequence but makes the pose unusable
    // without discarding the last good position.
    auto invalid = valid_sample(2, {9, 9, 9}, t0 + 10ms);
    invalid.valid = false; invalid.state = psvr::PositionPacketState::lost;
    check(bridge.ingest(invalid), "invalid packet accepted for sequencing");
    view = bridge.view(t0 + 20ms, 0);
    check(!view.valid && view.state == psvr::PositionState::lost, "invalid packet makes view lost");
    check(bridge.counters().invalid == 1, "invalid packet counted");

    auto calibrating = invalid; calibrating.seq = 3; calibrating.state = psvr::PositionPacketState::calibrating;
    check(bridge.ingest(calibrating), "calibrating packet accepted");
    check(bridge.view(t0 + 30ms, 0).state == psvr::PositionState::waiting, "calibrating maps to waiting");

    check(bridge.ingest(valid_sample(4, {4, 5, 6}, t0 + 40ms)), "valid packet resumes tracking");
    close_to(bridge.view(t0 + 50ms, 0).position.x, 3, "valid packet updates relative position");

    // Defence in depth: a directly-constructed valid=true/lost sample is
    // rejected even if it bypassed the wire parser.
    {
        psvr::PositionBridge strict(config);
        auto smuggled = valid_sample(1, {1, 1, 1}, t0);
        smuggled.state = psvr::PositionPacketState::lost;
        check(!strict.ingest(smuggled), "valid=true with lost state rejected by bridge");
        check(strict.counters().rejected == 1, "inconsistent sample counted rejected");
    }

    check(!bridge.ingest(valid_sample(4, {7, 7, 7}, t0 + 60ms)), "duplicate seq rejected");
    check(bridge.counters().duplicates == 1, "duplicate counted");
    check(!bridge.ingest(valid_sample(2, {7, 7, 7}, t0 + 60ms)), "in-window reorder rejected");
    check(bridge.counters().out_of_order == 1, "reorder counted");

    check(bridge.ingest(valid_sample(5004, {8, 8, 8}, t0 + 70ms)), "large forward gap accepted");
    check(bridge.counters().gaps == 1, "gap counted");
    check(bridge.ingest(valid_sample(10, {8, 8, 8}, t0 + 100000ms)), "post-stale older seq resyncs");
    check(bridge.counters().resyncs == 1, "resync counted");

    psvr::PositionBridge faulted(config);
    faulted.mark_faulted();
    check(!faulted.ingest(valid_sample(1, {0, 0, 0}, t0)), "faulted rejects ingest");
    check(!faulted.view(t0, 0).valid && faulted.view(t0, 0).state == psvr::PositionState::lost,
          "faulted reports lost");

    psvr::PositionConfig disabled;
    psvr::PositionBridge off(disabled);
    check(!off.view(t0, 0).enabled && off.view(t0, 0).state == psvr::PositionState::disabled,
          "disabled reports disabled");
}

void test_recenter_coherence() {
    psvr::PositionConfig config = parse_config();
    config.timeout_ms = 1000;
    psvr::PositionBridge bridge(config);
    const auto t0 = psvr::PositionClock::now();
    check(bridge.ingest(valid_sample(1, {2, 0, 0}, t0)), "pre-recenter sample");

    check(bridge.apply_recenter(t0 + 10ms, 1), "recenter applied with fresh valid sample");
    auto view = bridge.view(t0 + 20ms, 1);
    check(view.valid && view.coherent && view.epoch == 1, "recentered view coherent");
    close_to(view.position.x, 0, "recenter zeroes X");
    close_to(view.position.y, 0, "recenter zeroes Y");
    close_to(view.position.z, 0, "recenter zeroes Z");

    view = bridge.view(t0 + 20ms, 0);
    check(!view.valid && !view.coherent, "epoch mismatch invalidates position");

    check(bridge.ingest(valid_sample(2, {2.5, 0, 0}, t0 + 30ms)), "post-recenter sample");
    close_to(bridge.view(t0 + 40ms, 1).position.x, 0.5, "post-recenter offset");

    // Recenter while the latest packet is invalid stays pending.
    psvr::PositionBridge pending(config);
    check(pending.ingest(valid_sample(1, {5, 5, 5}, t0)), "pending first sample");
    auto invalid = valid_sample(2, {5, 5, 5}, t0 + 10ms);
    invalid.valid = false; invalid.state = psvr::PositionPacketState::lost;
    check(pending.ingest(invalid), "pending invalid packet");
    check(!pending.apply_recenter(t0 + 20ms, 2), "recenter deferred while invalid");
    check(pending.ingest(valid_sample(3, {8, 8, 8}, t0 + 30ms)), "pending valid sample");
    view = pending.view(t0 + 40ms, 2);
    check(view.valid && view.epoch == 2, "deferred recenter applied");
    close_to(view.position.x, 0, "deferred recenter zeroes X");
}

void test_frame_alignment() {
    const auto t0 = psvr::PositionClock::now();

    // Startup: the first valid sample fixes the seated origin, so huge raw
    // camera coordinates never become a pose.
    psvr::PositionConfig config = parse_config();
    config.timeout_ms = 1000;
    psvr::PositionBridge bridge(config);
    check(bridge.ingest(valid_sample(1, {2, 1, -3}, t0)), "alignment origin sample");
    auto view = bridge.view(t0 + 10ms, 0);
    check(view.valid, "startup origin valid");
    close_to(view.position.x, 0, "startup origin zeroes X");
    close_to(view.position.y, 0, "startup origin zeroes Y");
    close_to(view.position.z, 0, "startup origin zeroes Z");

    // With identity world rotation and zero camera offset, a camera-frame
    // displacement is preserved.
    check(bridge.ingest(valid_sample(2, {3, 1, -3}, t0 + 20ms)), "alignment moved sample");
    view = bridge.view(t0 + 30ms, 0);
    close_to(view.position.x, 1, "identity alignment preserves X");
    close_to(view.position.z, 0, "identity alignment preserves Z");

    // A yaw recenter rotates the translation into the recentered world frame:
    // a camera +X displacement under Ry(90) becomes -Z (forward). Without this
    // the position would stay in the pre-recenter frame and disagree with the
    // recentered orientation.
    psvr::PositionBridge rotated(config);
    check(rotated.ingest(valid_sample(1, {0, 0, 0}, t0)), "rotated origin sample");
    check(rotated.ingest(valid_sample(2, {1, 0, 0}, t0 + 20ms)), "rotated moved sample");
    const auto yaw90 = psvr::rotation_vector({0, psvr::pi / 2, 0});
    view = rotated.view(t0 + 30ms, 0, yaw90);
    check(view.valid, "rotated view valid");
    close_to(view.position.x, 0, "Ry(90) X");
    close_to(view.position.z, -1, "Ry(90) maps +X to -Z (forward)");

    // The explicit camera yaw offset is applied the same way and defaults to 0
    // (the physically-uncalibrated mounting yaw is an open gate).
    psvr::PositionConfig offset = parse_config();
    offset.camera_yaw_offset_deg = 90.0;
    psvr::PositionBridge offset_bridge(offset);
    check(offset_bridge.ingest(valid_sample(1, {0, 0, 0}, t0)), "offset origin sample");
    check(offset_bridge.ingest(valid_sample(2, {1, 0, 0}, t0 + 20ms)), "offset moved sample");
    view = offset_bridge.view(t0 + 30ms, 0);
    close_to(view.position.z, -1, "camera yaw offset rotates translation");

    // An epoch mismatch still invalidates even with a rotation supplied.
    view = rotated.view(t0 + 30ms, 1, yaw90);
    check(!view.valid && !view.coherent, "epoch mismatch invalidates rotated view");
}

void test_driver_pose() {
    const psvr::DriverClock::time_point origin{};
    psvr::TrackingSnapshot snapshot;
    snapshot.connected = true;
    snapshot.state = psvr::ImuState::tracking;
    snapshot.has_sample = true;
    snapshot.sampled_at = origin;
    snapshot.orientation = psvr::rotation_vector({0, -0.5, 0});
    snapshot.angular_velocity = {1,2,3};

    auto pose = psvr::driver_pose(snapshot, origin + 25ms);
    check(pose.poseIsValid && pose.deviceIsConnected, "3DoF orientation valid");
    check(pose.result == vr::TrackingResult_Running_OK, "3DoF running");
    for (int i = 0; i < 3; ++i) close_to(pose.vecPosition[i], 0, "3DoF zero position");

    snapshot.position_enabled = true;
    snapshot.has_position = false;
    pose = psvr::driver_pose(snapshot, origin + 25ms);
    check(!pose.poseIsValid, "missing position invalidates pose");
    check(pose.result == vr::TrackingResult_Running_OutOfRange, "missing position is OutOfRange");
    for (int i = 0; i < 3; ++i) close_to(pose.vecPosition[i], 0, "no invented position on loss");

    snapshot.has_position = true;
    snapshot.position_coherent = true;
    snapshot.position = {0.1, -0.2, 0.3};
    snapshot.position_at = origin + 20ms;
    pose = psvr::driver_pose(snapshot, origin + 25ms);
    check(pose.poseIsValid && pose.result == vr::TrackingResult_Running_OK, "6DoF valid");
    close_to(pose.vecAngularVelocity[1],2,"6DoF retains IMU prediction velocity");
    close_to(pose.vecPosition[0], 0.1, "6DoF X");
    close_to(pose.vecPosition[1], -0.2, "6DoF Y");
    close_to(pose.vecPosition[2], 0.3, "6DoF Z");
    close_to(pose.poseTimeOffset, -0.025, "pose offset uses IMU sample");

    snapshot.position_coherent = false;
    pose = psvr::driver_pose(snapshot, origin + 25ms);
    check(!pose.poseIsValid && pose.result == vr::TrackingResult_Running_OutOfRange,
          "epoch-incoherent position is OutOfRange");

    snapshot.position_coherent = true;
    snapshot.position.x = std::numeric_limits<double>::quiet_NaN();
    pose = psvr::driver_pose(snapshot, origin + 25ms);
    check(!pose.poseIsValid && pose.result == vr::TrackingResult_Running_OutOfRange,
          "non-finite position is OutOfRange");
}

void test_bound_v2() {
    auto packet = base_packet();
    packet["version"] = 2; packet["frame"] = "psvr_imu_unrecentered";
    packet["session_id"] = "activation-A"; packet["recenter_epoch"] = 1;
    packet["imu_seq"] = 20; packet["imu_sample_time_s"] = 100.0;
    check(parse_packet(packet).has_value(),"v2 codec");
    for (const char* key : {"session_id","recenter_epoch","imu_seq","imu_sample_time_s"}) {
        auto bad = packet; bad.erase(key); check(!parse_packet(bad),"missing binding rejected");
    }
    for (const char* key : {"recenter_epoch","imu_seq"}) {
        auto bad = packet; bad[key] = -1; check(!parse_packet(bad),"negative binding rejected");
        bad[key] = 1.5; check(!parse_packet(bad),"fractional binding rejected");
        bad[key] = true; check(!parse_packet(bad),"bool binding rejected");
    }
    { auto bad = packet; bad["session_id"] = ""; check(!parse_packet(bad),"empty session rejected"); }
    { auto bad = packet; bad["frame"] = "psVR_imu_unrecentered"; check(!parse_packet(bad),"case sensitive frame"); }
    { auto bad = packet; bad["imu_sample_time_s"] = -1; check(!parse_packet(bad),"negative timestamp"); }
    { auto bad = packet; bad["imu_sample_time_s"] = "100"; check(!parse_packet(bad),"typed timestamp"); }
    auto config = parse_config(); config.require_bound_v2 = true; config.camera_yaw_offset_deg = 90;
    const auto t = psvr::PositionClock::time_point{} + 100s;
    check(!psvr::parse_position_packet(base_packet().dump(),config,t),"bound mode rejects v1");
    psvr::PositionBridge bridge(config);
    bridge.bind_driver("activation-A",1,100,true,t);
    auto s = *psvr::parse_position_packet(packet.dump(),config,t);
    s.seq = 1; s.position_m = {};
    auto bad = s; bad.session_id = "activation-B";
    check(!bridge.ingest(bad),"wrong session rejected before origin");
    bad = s; bad.recenter_epoch = 0; check(!bridge.ingest(bad),"old epoch rejected");
    bad.recenter_epoch = 2; check(!bridge.ingest(bad),"future epoch rejected");
    bad = s; bad.imu_sample_time_s = 100.001; check(!bridge.ingest(bad),"future IMU timestamp rejected");
    bad.imu_sample_time_s = 99; check(!bridge.ingest(bad),"old IMU timestamp rejected");
    check(bridge.last_sequence() == 0 && !bridge.view(t,1).valid,"rejections cannot seed origin/sequence");
    check(bridge.ingest(s),"first bound fresh sample fixes origin");
    s.seq = 2; s.position_m = {1,0,0}; check(bridge.ingest(s),"bound displacement");
    auto view = bridge.view(t,1,psvr::rotation_vector({0,psvr::pi/2,0}));
    close_to(view.position.x,0,"v2 skips camera yaw"); close_to(view.position.z,-1,"world rotation exactly once");
    bridge.bind_driver("activation-A",2,100.01,true,t+10ms);
    check(!bridge.view(t+10ms,2).valid,"recenter invalidates old origin");
    check(!bridge.ingest(s),"recenter rejects old epoch");
    s.recenter_epoch = 2; s.seq = 3; s.received_at = t+10ms;
    check(!bridge.ingest(s),"new epoch cannot reuse pre-epoch timestamp");
    s.imu_sample_time_s = 100.01;
    check(bridge.ingest(s),"new epoch first fresh sample");
    close_to(bridge.view(t+10ms,2).position.x,0,"new epoch origin awaits fresh frame");
    s.seq = 4; s.valid = false; s.state = psvr::PositionPacketState::lost; s.imu_sample_time_s = 0;
    check(bridge.ingest(s) && !bridge.view(t+10ms,2).valid,"invalid clears despite old IMU timestamp");
    bridge.bind_driver("activation-B",2,100.02,true,t+20ms);
    check(!bridge.ingest(s),"retired session cannot refresh invalid/freshness");
    s.session_id = "activation-B"; s.valid = true; s.state = psvr::PositionPacketState::tracking;
    s.seq = 1; s.received_at = t+20ms; s.imu_sample_time_s = 100.02;
    check(bridge.ingest(s),"new activation sequence baseline");
    s.seq = 2; s.received_at = t+300ms;
    check(!bridge.ingest(s),"local receive time rejects stale driver-domain sample");
    check(!bridge.view(t+300ms,2).valid,"local watchdog retained");
}

void test_bound_v2_source_freshness() {
    auto config = parse_config(); config.require_bound_v2 = true; config.timeout_ms = 200;
    const auto t = psvr::PositionClock::time_point{} + 100s;
    psvr::PositionBridge bridge(config);
    bridge.bind_driver("source-age",1,99.0,true,t-1s);
    bridge.bind_driver("source-age",1,100.0,true,t);
    auto sample = valid_sample(1,{1,0,0},t);
    sample.version = 2; sample.session_id = "source-age"; sample.recenter_epoch = 1;
    sample.imu_sample_time_s = 99.810;
    check(bridge.ingest(sample),"190ms source age accepted inside 200ms timeout");
    check(bridge.view(t,1).valid && bridge.view(t+9ms,1).valid,"source fresh until its own deadline");
    auto expired = bridge.view(t+15ms,1);
    check(!expired.valid && expired.state == psvr::PositionState::lost,
        "205ms source age expires despite only 15ms receive age");
    check(expired.received_at == t && bridge.last_sequence() == 1,"view never refreshes freshness/sequence");
    sample.seq = 2; sample.received_at = t+5ms;
    check(bridge.ingest(sample),"new packet may still reference a 195ms-old source");
    check(!bridge.view(t+15ms,1).valid,"higher sequence and new receive time cannot extend source lifetime");
    sample.seq = 3; sample.received_at = t+20ms; sample.valid = false;
    sample.state = psvr::PositionPacketState::lost; sample.imu_sample_time_s = 0;
    check(bridge.ingest(sample),"bound invalid with stale source timestamp still accepted");
    check(!bridge.view(t+20ms,1).valid && bridge.counters().invalid == 1,"bound invalid clears tracking");
    sample.seq = 4; sample.state = psvr::PositionPacketState::calibrating;
    check(bridge.ingest(sample),"bound calibrating invalid accepts stale source");
    check(bridge.view(t+20ms,1).state == psvr::PositionState::waiting,
        "stale source does not override invalid calibrating state");
}

void test_position_grace() {
    const auto t = psvr::PositionClock::time_point{} + 100s;
    auto config = parse_config();
    check(config.grace_ms == 0, "grace defaults to legacy zero");
    psvr::PositionBridge legacy(config);
    auto good = valid_sample(1, {1,2,3}, t);
    check(legacy.ingest(good) && legacy.view(t+150ms,0).valid,
        "default zero retains legacy receive timeout");
    auto bad = good; bad.seq = 2; bad.valid = false;
    bad.state = psvr::PositionPacketState::lost; bad.received_at = t+40ms;
    check(legacy.ingest(bad) && !legacy.view(t+40ms,0).valid &&
        !legacy.view(t+40ms,0).holding, "default zero invalidates immediately");

    config.grace_ms = 80;
    psvr::PositionBridge bridge(config);
    check(bridge.ingest(bad) && !bridge.view(t+40ms,0).valid,
        "invalid first frame never invents origin");
    good.seq = 3; check(bridge.ingest(good), "grace origin");
    good.seq = 4; good.position_m.x = 1.5;
    check(bridge.ingest(good), "grace displacement");
    check(bridge.view(t+40ms,0).valid && bridge.view(t+40ms,0).holding,
        "missing frame holds inside grace");
    check(!bridge.view(t+81ms,0).valid, "missing frame beyond grace invalid");
    for (unsigned i=0; i<3; ++i) {
        bad.seq = 5+i; bad.received_at = t+std::chrono::milliseconds(40+i*20);
        bad.state = i == 1 ? psvr::PositionPacketState::ambiguous : psvr::PositionPacketState::lost;
        check(bridge.ingest(bad), "transient advances sequence");
        auto view = bridge.view(bad.received_at,0);
        check(view.valid && view.holding && view.received_at == t, "hold keeps good receive clock");
        close_to(view.position.x,.5,"invalid raw position cannot replace last good");
    }
    check(!bridge.view(t+81ms,0).valid, "continuous invalid frames never renew grace");
    {
        psvr::PositionBridge identity(config);
        identity.bind_driver("legacy-A",0,100,true,t);
        check(identity.ingest(valid_sample(1,{1,0,0},t)), "legacy hold identity seed");
        identity.bind_driver("legacy-B",0,100.01,true,t+10ms);
        check(!identity.view(t+10ms,0).valid, "legacy hold session invalidation immediate");
        check(identity.ingest(valid_sample(1,{5,0,0},t+10ms)), "legacy new session baseline");
        close_to(identity.view(t+10ms,0).position.x,0,"session never reuses old origin");
        identity.bind_driver("legacy-B",1,100.02,true,t+20ms);
        check(!identity.view(t+20ms,1).valid,"legacy hold epoch invalidation immediate");
        check(identity.ingest(valid_sample(1,{7,0,0},t+20ms)),"legacy new epoch baseline");
        close_to(identity.view(t+20ms,1).position.x,0,"epoch origin requires good frame");
        identity.mark_faulted();
        check(!identity.view(t+21ms,1).valid && !identity.view(t+21ms,1).holding,
            "socket fault cannot be hidden by grace");
    }
    check(!bridge.view(t+40ms,1).valid, "epoch mismatch immediate during grace");
    check(!bridge.apply_recenter(t+40ms,1) && !bridge.view(t+40ms,0).valid,
        "recenter clears hold even before new epoch is published");
    good.seq = 8; good.received_at = t+50ms;
    check(bridge.ingest(good) && bridge.view(t+50ms,1).valid, "fresh frame seeds recentered origin");
    close_to(bridge.view(t+50ms,1).position.x,0,"new origin not old displacement");

    for (auto state : {psvr::PositionPacketState::error, psvr::PositionPacketState::calibrating}) {
        psvr::PositionBridge terminal(config);
        check(terminal.ingest(valid_sample(1,{},t)), "terminal good seed");
        bad.seq = 2; bad.received_at = t+10ms; bad.state = state;
        check(terminal.ingest(bad) && !terminal.view(t+10ms,0).valid, "terminal state immediate");
        bad.seq = 3; bad.state = psvr::PositionPacketState::lost;
        check(terminal.ingest(bad) && !terminal.view(t+20ms,0).valid, "lost cannot revive terminal hold");
    }
    config.timeout_ms = 50;
    psvr::PositionBridge short_timeout(config);
    check(short_timeout.ingest(valid_sample(1,{},t)), "short timeout seed");
    check(short_timeout.view(t+40ms,0).valid && !short_timeout.view(t+51ms,0).valid,
        "grace cannot extend receive timeout");
    config.timeout_ms = 1000; config.grace_ms = 200;
    psvr::PositionBridge capped(config);
    check(capped.ingest(valid_sample(1,{},t)) && !capped.view(t+201ms,0).valid,
        "receive hard cap 200ms even with long configured timeout");

    config.timeout_ms = 200; config.grace_ms = 80; config.require_bound_v2 = true;
    psvr::PositionBridge bound(config);
    bound.bind_driver("grace",1,99,true,t-1s);
    bound.bind_driver("grace",1,100,true,t);
    good = valid_sample(1,{},t); good.version = 2; good.session_id = "grace";
    good.recenter_epoch = 1; good.imu_sample_time_s = 99.810;
    check(bound.ingest(good), "old source inside hard cap accepted");
    bad = good; bad.seq = 2; bad.valid = false; bad.state = psvr::PositionPacketState::lost;
    bad.received_at = t+5ms; bad.imu_sample_time_s = 0;
    check(bound.ingest(bad) && bound.view(t+9ms,1).valid, "lost preserves good source clock");
    check(!bound.view(t+15ms,1).valid, "old source hard cap wins over grace and new receive");
    bound.bind_driver("new-session",1,100.02,true,t+20ms);
    check(!bound.view(t+20ms,1).valid && !bound.ingest(bad), "session change clears hold and rejects retired packets");
    good.session_id = "new-session"; good.imu_sample_time_s = 100.02; good.received_at = t+20ms;
    check(bound.ingest(good), "new session baseline");
    bound.bind_driver("new-session",2,100.03,true,t+30ms);
    check(!bound.view(t+30ms,2).valid && !bound.ingest(good), "bound epoch immediately clears hold");
}

void test_live_loopback() {
    psvr::PositionConfig live;
    live.enabled = true;
    live.port = 0; // ephemeral
    live.timeout_ms = 2000;
    live.max_abs_m = 10.0;
    psvr::PositionBridge bridge(live);
    std::atomic<bool> stop{false};

    auto receiver = std::make_unique<psvr::PositionReceiver>(live,
        [&bridge](const psvr::PositionSample& sample) { bridge.ingest(sample); });
    const auto port = receiver->local_port();
    check(port != 0, "ephemeral port assigned");
    std::thread worker([&receiver, &stop] { receiver->run(stop); });

    psvr::WinsockSession sender_session;
    check(sender_session.ok(), "sender Winsock available");
    SOCKET sender = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    check(sender != INVALID_SOCKET, "sender socket created");
    sockaddr_in destination{};
    destination.sin_family = AF_INET;
    destination.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    destination.sin_port = htons(port);
    const auto send_text = [&](const std::string& text) {
        return ::sendto(sender, text.data(), static_cast<int>(text.size()), 0,
                        reinterpret_cast<sockaddr*>(&destination), sizeof(destination));
    };
    const auto send_packet = [&](const json& packet) { return send_text(packet.dump()); };

    auto tracking = base_packet();
    tracking["seq"] = 1; tracking["position_m"] = {1.0, 2.0, 3.0};
    check(send_packet(tracking) == static_cast<int>(tracking.dump().size()), "send valid packet");
    // The first valid frame establishes the seated origin (raw camera
    // coordinates must not leak), so the reported position is zero.
    check(wait_until([&] {
        const auto view = bridge.view(psvr::PositionClock::now(), 0);
        return view.valid && std::abs(view.position.x) < 1e-9 &&
               std::abs(view.position.y) < 1e-9 && std::abs(view.position.z) < 1e-9;
    }, 2000), "loopback first frame establishes seated origin");

    auto moved = base_packet();
    moved["seq"] = 2; moved["position_m"] = {1.5, 2.0, 3.0};
    check(send_packet(moved) == static_cast<int>(moved.dump().size()), "send moved packet");
    check(wait_until([&] {
        const auto view = bridge.view(psvr::PositionClock::now(), 0);
        return view.valid && std::abs(view.position.x - 0.5) < 1e-9 &&
               std::abs(view.position.y) < 1e-9 && std::abs(view.position.z) < 1e-9;
    }, 2000), "loopback position received and framed");

    check(send_packet(moved) == static_cast<int>(moved.dump().size()), "send duplicate packet");
    check(wait_until([&] { return bridge.counters().duplicates >= 1; }, 1000), "duplicate observed");

    auto invalid = base_packet();
    invalid["seq"] = 3; invalid["valid"] = false; invalid["state"] = "lost"; invalid["position_m"] = nullptr;
    check(send_packet(invalid) == static_cast<int>(invalid.dump().size()), "send invalid packet");
    check(wait_until([&] { return bridge.counters().invalid >= 1; }, 1000), "invalid packet observed");
    check(!bridge.view(psvr::PositionClock::now(), 0).valid, "invalid packet makes position unusable");

    const std::string malformed = "{not json";
    check(send_text(malformed) == static_cast<int>(malformed.size()), "send malformed packet");
    check(wait_until([&] { return receiver->counters().malformed >= 1; }, 1000), "malformed rejected");

    const std::string oversized(1500, 'x');
    check(send_text(oversized) == static_cast<int>(oversized.size()), "send oversized packet");
    check(wait_until([&] { return receiver->counters().oversized >= 1; }, 1000), "oversized rejected");

    auto out_of_bounds = base_packet();
    out_of_bounds["seq"] = 4; out_of_bounds["position_m"] = {100.0, 0.0, 0.0};
    check(send_packet(out_of_bounds) == static_cast<int>(out_of_bounds.dump().size()), "send out-of-bounds packet");
    check(wait_until([&] { return receiver->counters().malformed >= 2; }, 1000), "out-of-bounds rejected");

    auto resumed = base_packet();
    resumed["seq"] = 5; resumed["position_m"] = {0.5, 0.6, 0.7};
    check(send_packet(resumed) == static_cast<int>(resumed.dump().size()), "send resumed packet");
    check(wait_until([&] {
        const auto view = bridge.view(psvr::PositionClock::now(), 0);
        return view.valid && std::abs(view.position.x + 0.5) < 1e-9 &&
               std::abs(view.position.y + 1.4) < 1e-9;
    }, 1000), "position resumes after invalid frame relative to seated origin");

    const auto started = std::chrono::steady_clock::now();
    stop = true;
    worker.join();
    const auto stopped_after = std::chrono::steady_clock::now() - started;
    check(stopped_after < 1000ms, "receiver stop is bounded");
    const auto counters = receiver->counters();
    check(counters.parsed >= 2, "parsed datagrams counted");
    check(counters.fatal == 0, "no fatal receiver error");
    check(counters.errors == 0, "no transient socket errors");

    receiver.reset();
    ::closesocket(sender);
    {
        // Restart proves the previous socket was released by RAII.
        auto restarted = std::make_unique<psvr::PositionReceiver>(live,
            [](const psvr::PositionSample&) {});
        check(restarted->local_port() != 0, "receiver restart bound a fresh port");
    }
}

void test_bound_v2_loopback() {
    auto config = parse_config(); config.port = 0; config.require_bound_v2 = true;
    psvr::PositionBridge bridge(config);
    const auto now = psvr::PositionClock::now();
    const auto seconds = std::chrono::duration<double>(now.time_since_epoch()).count();
    bridge.bind_driver("udp-bound",7,seconds,true,now);
    psvr::PositionReceiver receiver(config,[&](const psvr::PositionSample& sample) { bridge.ingest(sample); });
    psvr::WinsockSession ws;
    SOCKET sender = ::socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
    check(sender != INVALID_SOCKET,"v2 sender socket");
    struct Guard { SOCKET s; ~Guard() { ::closesocket(s); } } guard{sender};
    sockaddr_in address{}; address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK); address.sin_port = htons(receiver.local_port());
    const auto send = [&](const json& packet) {
        const auto text = packet.dump();
        check(::sendto(sender,text.data(),static_cast<int>(text.size()),0,
            reinterpret_cast<sockaddr*>(&address),sizeof(address)) == static_cast<int>(text.size()),"v2 loopback send");
        check(receiver.poll(500) == 1,"v2 loopback poll");
    };
    send(base_packet()); check(receiver.counters().malformed == 1,"bound UDP rejects legacy");
    auto packet = base_packet(); packet["version"] = 2; packet["frame"] = "psvr_imu_unrecentered";
    packet["session_id"] = "wrong"; packet["recenter_epoch"] = 7;
    packet["imu_seq"] = 1; packet["imu_sample_time_s"] = seconds; packet["seq"] = 1;
    send(packet); check(bridge.counters().rejected == 1,"bound UDP rejects identity");
    packet["session_id"] = "udp-bound";
    send(packet); check(bridge.view(psvr::PositionClock::now(),7).valid,"bound UDP accepts coherent first frame");
    packet["seq"] = 2; packet["valid"] = false; packet["state"] = "lost";
    packet["imu_sample_time_s"] = 0;
    send(packet); check(!bridge.view(psvr::PositionClock::now(),7).valid,"bound UDP invalid clears stale timestamp");
}
} // namespace

int main() {
    try {
        test_protocol();
        test_sequence_and_freshness();
        test_recenter_coherence();
        test_frame_alignment();
        test_driver_pose();
        test_bound_v2();
        test_bound_v2_source_freshness();
        test_position_grace();
        test_live_loopback();
        test_bound_v2_loopback();
        std::cout << "PASS psvr.position v1/v2 validation, binding, frame alignment, sequence/freshness, "
                     "recenter coherence, driver pose contract and live loopback lifecycle\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
