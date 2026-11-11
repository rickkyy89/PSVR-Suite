// SPDX-License-Identifier: MIT
// Pure, hardware-free. Does not activate an HMD, open HID/UDP or load SteamVR.
#include "position_continuity.hpp"
#include "position_protocol.hpp"
#include "pose_transition_trace.hpp"
#include <iostream>
#include <limits>
#include <map>
#include <variant>
#include <cstring>

namespace {
using namespace psvr;
void check(bool ok, const char* text) { if (!ok) throw std::runtime_error(text); }
void close(Vec3 a, Vec3 b, const char* text) { check((a-b).norm() < 1e-10,text); }
DriverClock::time_point time_ms(int ms) { return DriverClock::time_point{} + std::chrono::milliseconds(ms); }
TrackingSnapshot input(int ms, bool optical = true, Vec3 p = {}) {
    TrackingSnapshot s;
    s.connected = s.has_sample = true; s.state = ImuState::tracking;
    s.sampled_at = time_ms(ms); s.position_at = time_ms(ms);
    s.position_enabled = true; s.has_position = optical; s.position = p;
    s.angular_velocity = {.1,.2,.3}; s.recenter_epoch = 1;
    return s;
}
PositionContinuity enabled(int blend = 150, double speed = 1.5, Vec3 offset = {}) {
    PositionContinuityConfig c; c.fallback_3dof_enabled = true;
    c.reacquire_blend_ms = blend; c.reacquire_max_speed_mps = speed; c.initial_offset = offset;
    return PositionContinuity(c);
}
TrackingSnapshot publish(PositionContinuity& c, TrackingSnapshot s, int ms,
                         bool optical = true, bool grace = false, bool display = true,
                         const std::string& session = "activation") {
    return c.update(s,session,time_ms(ms),100,display,optical,grace);
}
vr::DriverPose_t pose(TrackingSnapshot s, int ms, bool display = true) {
    return driver_pose(display ? s : TrackingSnapshot{},time_ms(ms),100);
}

struct Settings {
    std::map<std::string,std::variant<bool,int,float>> values;
    template<class T> T read(const char* section, const char* key, vr::EVRSettingsError* error) {
        check(std::strcmp(section,"driver_psvrpc") == 0,"exact settings section");
        *error = vr::VRSettingsError_UnsetSettingHasNoDefault;
        auto found = values.find(key);
        if (found == values.end()) return {};
        const auto* result = std::get_if<T>(&found->second);
        *error = result ? vr::VRSettingsError_None : vr::VRSettingsError_JsonParseFailed;
        return result ? *result : T{};
    }
    bool GetBool(const char* s,const char* k,vr::EVRSettingsError* e) { return read<bool>(s,k,e); }
    int GetInt32(const char* s,const char* k,vr::EVRSettingsError* e) { return read<int>(s,k,e); }
    float GetFloat(const char* s,const char* k,vr::EVRSettingsError* e) { return read<float>(s,k,e); }
};
void settings_tests() {
    Settings s; const auto defaults = load_position_continuity_config(s);
    check(!defaults.fallback_3dof_enabled && defaults.reacquire_blend_ms == 150 &&
          defaults.reacquire_max_speed_mps == 1.5 && defaults.initial_offset.norm() == 0,"missing optional settings defaults");
    s.values = {{"positionFallback3DofEnabled",true},{"positionReacquireBlendMs",2000},
        {"positionReacquireMaxSpeedMps",10.f},{"positionInitialOffsetX",-10.f},
        {"positionInitialOffsetY",2.f},{"positionInitialOffsetZ",10.f}};
    const auto c = load_position_continuity_config(s);
    check(c.fallback_3dof_enabled && c.reacquire_blend_ms == 2000,"exact settings names accepted");
    close(c.initial_offset,{-10,2,10},"world offset settings");
    const auto good = s.values;
    for (const auto& bad : std::map<std::string,std::variant<bool,int,float>>{
        {"positionFallback3DofEnabled",1},{"positionReacquireBlendMs",2001},
        {"positionReacquireMaxSpeedMps",0.f},{"positionInitialOffsetX",10.1f}}) {
        s.values = good; s.values[bad.first] = bad.second;
        bool failed = false; try { load_position_continuity_config(s); } catch (const std::exception&) { failed = true; }
        check(failed,"invalid supplied property must fail closed");
    }
    for (float speed : {-1.f,10.1f,std::numeric_limits<float>::infinity(),std::numeric_limits<float>::quiet_NaN()}) {
        s.values = good; s.values["positionReacquireMaxSpeedMps"] = speed;
        bool failed = false; try { load_position_continuity_config(s); } catch (...) { failed = true; }
        check(failed,"invalid finite speed range");
    }
    s.values = good; s.values["positionReacquireBlendMs"] = -1;
    bool failed = false; try { load_position_continuity_config(s); } catch (...) { failed = true; }
    check(failed,"negative blend rejected");
}

void default_and_offsets() {
    PositionContinuity strict;
    auto raw = input(0,true,{.3,-.2,.1});
    const auto original = driver_pose(raw,time_ms(0));
    const auto old = pose(publish(strict,raw,0),0);
    // Compare every byte of these deterministically zero-initialized SDK poses.
    check(std::memcmp(&original,&old,sizeof(old)) == 0,"default strict 6DoF exact pose");
    check(!pose(publish(strict,input(100,false),100,false),100).poseIsValid,"default loss remains invalid");
    raw.position_enabled = false;
    const auto original3 = driver_pose(raw,time_ms(0));
    const auto old3 = pose(publish(strict,raw,0),0);
    check(std::memcmp(&original3,&old3,sizeof(old3)) == 0,"legacy orientation-only exact zero origin");
    PositionContinuityConfig cfg; cfg.initial_offset = {1,-2,.5};
    PositionContinuity offset(cfg);
    raw = input(0); raw.position_enabled = false; raw.orientation = rotation_vector({0,.8,0});
    auto out = publish(offset,raw,0,false); auto p = pose(out,0);
    check(p.poseIsValid,"offset orientation-only valid");
    close({p.vecPosition[0],p.vecPosition[1],p.vecPosition[2]},cfg.initial_offset,"orientation-only offset is world-fixed, not rotated");
    out = publish(offset,input(1,true,{.2,.3,.4}),1);
    close(out.position,{1.2,-1.7,.9},"6DoF initial offset added exactly once");
}

void loss_and_freshness() {
    auto c = enabled();
    auto good = publish(c,input(0,true,{.1,.2,.3}),0);
    for (int t : {1,80,100,1000,60000}) {
        auto s = input(t,false);
        s.orientation = rotation_vector({0,.001*t,0});
        const auto out = publish(c,s,t,false);
        check(out.tracking_mode == TrackingMode::fallback_3dof && !out.has_position &&
              !out.optical_position_valid && out.position_fallback_3dof,"explicit prolonged fallback, no optical promotion");
        close(out.position,good.position,"hold last actually published point");
        const auto p = pose(out,t);
        check(p.poseIsValid && p.result == vr::TrackingResult_Running_OK,"fresh IMU fallback no tracking gray");
        check(p.qRotation.y == s.orientation.y && p.vecAngularVelocity[1] == .2,"live rotation/angular velocity retained");
        check(p.vecVelocity[0] == 0 && p.vecVelocity[1] == 0 && p.vecVelocity[2] == 0,"no synthetic optical velocity");
        check(pose_reason(out,time_ms(t),100,true,true) == PoseReason::fallback_3dof,"trace not mislabeled position missing");
    }
    for (int kind = 0; kind < 7; ++kind) {
        auto broken = input(60001,false);
        if (kind == 0) broken.sampled_at = time_ms(59900);
        if (kind == 1) broken.sampled_at = time_ms(60002);
        if (kind == 2) broken.orientation.w = 2;
        if (kind == 3) broken.orientation.x = std::numeric_limits<double>::quiet_NaN();
        if (kind == 4) broken.connected = false;
        if (kind == 5) broken.state = ImuState::lost;
        if (kind == 6) broken.has_sample = false;
        const auto out = publish(c,broken,60001,false);
        check(!pose(out,60001).poseIsValid && !out.position_fallback_3dof,"bad IMU never authorized fallback");
    }
    auto out = publish(c,input(61000),61000,true,false,false);
    check(!pose(out,61000,false).poseIsValid && pose(out,61000,false).result == vr::TrackingResult_Running_OutOfRange,
          "display invalid remains gray/out of range");
    for (auto state : {ImuState::warming, ImuState::calibrating, ImuState::calibration_failed}) {
        auto calibrating = input(62000,false); calibrating.state = state;
        const auto invalid = publish(c,calibrating,62000,false);
        check(!pose(invalid,62000).poseIsValid && pose(invalid,62000).result == vr::TrackingResult_Running_OutOfRange,
              "fallback policy cannot promote calibration/warming to valid IMU");
    }
}

void recovery_and_preview() {
    auto c = enabled();
    publish(c,input(0,true,{.2,0,0}),0);
    publish(c,input(10,false),10,false);
    const auto first = publish(c,input(20,true,{3.2,0,0}),20);
    close(first.position,{.2,0,0},"huge recovery delta first publish exactly continuous");
    close(first.recovery_offset,{-3,0,0},"record recovery correction");
    check(first.recovery_duration_ms == 2000 && first.recovery_elapsed_ms == 0,"speed extends nominal 150ms for huge delta");
    for (int i = 0; i < 1000; ++i) {
        const auto query = c.preview(input(30,true,{3.2,0,0}),"activation",time_ms(30),100,true,true,false);
        close(query.position,first.position,"GetPose preview does not decay or advance timers");
        check(query.recovery_elapsed_ms == 0,"query frequency leaves timers unchanged");
    }
    const auto same = publish(c,input(20,true,{3.2,0,0}),20);
    close(same.position,first.position,"same timestamp no advance");
    auto previous = same;
    int previous_t = 20;
    for (int t : {27,43,110,200,500,1000,2020}) {
        const auto out = publish(c,input(t,true,{3.2,0,0}),t);
        check((out.recovery_offset-previous.recovery_offset).norm() <= 1.5*(t-previous_t)*.001+1e-10,"correction speed bounded using actual dt");
        check(out.position.x >= previous.position.x && std::isfinite(out.position.x),"finite monotonic recovery");
        previous = out; previous_t = t;
    }
    check(previous.tracking_mode == TrackingMode::sixdof && previous.recovery_offset.norm() == 0,"blend ends in finite time");
    close(previous.position,{3.2,0,0},"fully recovered position");
    close(publish(c,input(2030,true,{3.3,0,0}),2030).position,{3.3,0,0},"healthy 6DoF adds no filtering latency");
    // Physical target can move fast; limit applies ONLY to recovery offset.
    publish(c,input(2040,false),2040,false);
    publish(c,input(2050,true,{4.3,0,0}),2050);
    const auto moving = publish(c,input(2060,true,{5.3,0,0}),2060);
    check(moving.position.x > 4.3,"physical target speed is not falsely clamped");
    auto instant = enabled(0);
    publish(instant,input(0),0); publish(instant,input(1,false),1,false);
    close(publish(instant,input(2,true,{1,2,3}),2).position,{1,2,3},"zero blend explicitly disables smoothing");
    auto nominal = enabled();
    publish(nominal,input(0),0); publish(nominal,input(10,false),10,false);
    const auto armed = publish(nominal,input(20,true,{.1,0,0}),20);
    check(armed.recovery_duration_ms == 150,"small recovery uses configured nominal duration");
    close(armed.position,{},"small recovery also starts exactly held");
    const auto done = publish(nominal,input(170,true,{.1,0,0}),170);
    check(done.tracking_mode == TrackingMode::sixdof && done.recovery_offset.norm() == 0,"nominal blend finite at 150ms");
    close(done.position,{.1,0,0},"nominal recovered target");
    publish(nominal,input(180,false),180,false);
    const auto malformed = input(190,true,{std::numeric_limits<double>::quiet_NaN(),0,0});
    const auto finite_fallback = publish(nominal,malformed,190);
    check(finite_fallback.tracking_mode == TrackingMode::fallback_3dof && !finite_fallback.optical_position_valid,
          "nonfinite optical target is not a valid measurement");
    close(finite_fallback.position,done.position,"malformed optical position cannot poison held point");
}

void identities_and_reset() {
    auto c = enabled(150,1.5,{.1,.2,.3});
    publish(c,input(0,true,{2,0,0}),0);
    auto raw = input(10,false); raw.recenter_epoch = 2;
    auto out = publish(c,raw,10,false);
    close(out.position,{.1,.2,.3},"epoch change discards old baseline, uses initial offset");
    raw = input(20,false); raw.recenter_epoch = 2;
    out = publish(c,raw,20,false,false,true,"different_activation");
    close(out.position,{.1,.2,.3},"new session no cached position");
    auto bad = raw; bad.position_coherent = false;
    out = publish(c,bad,30,false,false,true,"different_activation");
    check(!pose(out,30).poseIsValid,"incoherent context never fallback");
    c.reset(); out = publish(c,input(40,false),40,false);
    close(out.position,{.1,.2,.3},"explicit reset zero plus initial offset");
    publish(c,input(50,true,{1,0,0}),50);
    out = publish(c,input(5,false),5,false);
    close(out.position,{.1,.2,.3},"clock regression discards cross-clock baseline");
    out = publish(c,input(6,false),6,false,false,true,"");
    check(!pose(out,6).poseIsValid,"unbound empty session may not authorize fallback");
}

void grace_and_real_packets() {
    PositionConfig config; config.enabled = true; config.timeout_ms = 200; config.grace_ms = 80;
    PositionBridge grace(config); config.grace_ms = 0; PositionBridge optical(config);
    PositionSample packet; packet.version = 1; packet.valid = true;
    packet.state = PositionPacketState::tracking; packet.seq = 1; packet.received_at = time_ms(0);
    check(grace.ingest(packet) && optical.ingest(packet),"real origin packet ingested");
    packet.seq = 2; packet.received_at = time_ms(10); packet.position_m = {.4,0,0};
    check(grace.ingest(packet) && optical.ingest(packet),"real tracked packet ingested");
    PositionContinuity strict; auto fallback = enabled();
    auto s = input(10,true,{.4,0,0}); s.recenter_epoch = 0;
    publish(strict,s,10); publish(fallback,s,10);
    packet.seq = 3; packet.received_at = time_ms(20); packet.valid = false; packet.state = PositionPacketState::lost;
    check(grace.ingest(packet) && optical.ingest(packet),"wire false retained");
    const auto held = grace.view(time_ms(20),0); const auto missing = optical.view(time_ms(20),0);
    check(held.valid && !missing.valid,"raw optical truth distinguishable from grace");
    s = input(20,held.valid,held.position); s.recenter_epoch = 0;
    auto legacy = publish(strict,s,20,missing.valid,true);
    check(pose(legacy,20).poseIsValid && legacy.tracking_mode == TrackingMode::position_grace &&
          !legacy.optical_position_valid,"strict grace preserved but explicitly degraded");
    auto out = publish(fallback,s,20,missing.valid,true);
    check(out.tracking_mode == TrackingMode::fallback_3dof && !out.has_position && pose(out,20).poseIsValid,
          "fallback immediate even within grace, never optical valid");
    check(grace.counters().accepted == 2 && grace.counters().invalid == 1 && optical.counters().invalid == 1,
          "continuity never creates packets or changes optical counters");
    PoseTransitionTrace trace; const auto p = pose(out,20);
    trace.observe(out,p,time_ms(20),time_ms(20),time_ms(0),100,true);
    std::array<PoseTransition,128> events;
    check(trace.drain(events) == 1 && events[0].reason == PoseReason::fallback_3dof &&
          events[0].orientation_valid && !events[0].position_valid,"trace carries explicit fallback mode/validities");
}
void angular_centering() {
    PositionContinuityConfig cfg; cfg.angular_center_enabled = true;
    cfg.initial_offset = {.1,.2,.3};
    PositionContinuity c(cfg);
    const auto turned = [](int ms, double yaw, bool optical, Vec3 p = Vec3{}) {
        auto s = input(ms,optical,p); s.orientation = rotation_vector({0,yaw/degrees,0}); return s;
    };
    const Vec3 displaced{.2,.1,-.1};
    publish(c,turned(0,0,true,displaced),0);
    const auto entry = publish(c,turned(10,90,false),10,false);
    close(entry.position,displaced+cfg.initial_offset,"90deg entry is exactly continuous");
    check(pose(entry,10).poseIsValid && !entry.has_position && !entry.optical_position_valid,
          "angular center authorized without inventing optical measurement");
    const auto half = publish(c,turned(510,105,false),510,false);
    close(half.position,cfg.initial_offset+displaced*.5,"105deg half translation toward seated center");
    for (int i = 0; i < 1000; ++i)
        close(c.preview(turned(600,120,false),"activation",time_ms(600),100,true,false,false).position,
              half.position,"queries do not advance angular centering");
    const auto center = publish(c,turned(1010,120,false),1010,false);
    close(center.position,cfg.initial_offset,"120deg reaches world-fixed seated center");
    check(pose(center,1010).poseIsValid,"missing LEDs behind headset do not cause gray");
    const auto still_center = publish(c,turned(1020,180,true,{8,8,8}),1020);
    close(still_center.position,cfg.initial_offset,"rear optical noise cannot move intentional 3DoF center");
    const auto returning = publish(c,turned(1030,0,false),1030,false);
    close(returning.position,cfg.initial_offset,"frontward missing optics stays in centered fallback");
    check(pose(returning,1030).poseIsValid,"return remains valid awaiting optical reacquisition");
    const auto recovered = publish(c,turned(1040,0,true,{.4,.1,.2}),1040);
    close(recovered.position,returning.position,"first reliable front recovery is exactly continuous");
    const auto brief_loss = publish(c,turned(1045,0,false),1045,false);
    check(pose(brief_loss,1045).poseIsValid,"single optical dropout during return does not flash gray");
    publish(c,turned(1050,0,true,{.4,.1,.2}),1050);
    const auto done = publish(c,turned(2040,0,true,{.4,.1,.2}),2040);
    close(done.position,cfg.initial_offset+Vec3{.4,.1,.2},"front recovery ends at measured point");
    auto stale = turned(3000,180,false); stale.sampled_at = time_ms(2800);
    check(!pose(publish(c,stale,3000,false),3000).poseIsValid,"angular policy never masks stale IMU");
    check(!pose(publish(c,turned(3010,180,false),3010,false,false,false),3010,false).poseIsValid,
          "angular policy never masks invalid display");
    auto epoch = turned(3020,180,false); epoch.recenter_epoch = 2;
    close(publish(c,epoch,3020,false).position,cfg.initial_offset,"new epoch discards old anchor");
    close(publish(c,epoch,3020,false,false,true,"replacement").position,cfg.initial_offset,
          "new session discards old anchor");
    PositionContinuity forward(cfg);
    check(!pose(publish(forward,turned(0,40,false),0,false),0).poseIsValid,
          "angular centering does not disguise forward optical loss");
    PositionContinuity front_recovery(cfg);
    publish(front_recovery,turned(0,0,true,{.2,0,0}),0);
    publish(front_recovery,turned(10,30,false),10,false);
    const auto front_again = publish(front_recovery,turned(20,30,true,{.5,0,0}),20);
    close(front_again.position,cfg.initial_offset+Vec3{.5,0,0},
          "rear policy does not add a new smoothing transient to ordinary front reacquisition");
    check(front_again.tracking_mode == TrackingMode::sixdof && front_again.recovery_offset.norm() == 0,
          "ordinary front reacquisition retains previous strict driver behavior");
    PositionContinuity left(cfg);
    publish(left,turned(0,0,true,displaced),0);
    const auto first = publish(left,turned(10,-180,false),10,false);
    check((first.position-(displaced+cfg.initial_offset)).norm() <= .25*.01+1e-10,
          "fast left rotation has bounded centering speed");
    Settings settings; settings.values["positionAngularCenterEnabled"] = true;
    check(load_position_continuity_config(settings).angular_center_enabled,"angular setting loaded");
    settings.values["positionAngularCenterEnabled"] = 1;
    bool failed = false; try { load_position_continuity_config(settings); } catch (...) { failed = true; }
    check(failed,"angular setting rejects non-boolean values");
    PositionContinuity soak(cfg);
    Vec3 previous = cfg.initial_offset;
    for (int ms = 0; ms <= 2400000; ms += 20) {
        const double yaw = 180*std::sin(ms*.001);
        const bool optical = std::abs(yaw) < 90;
        const auto out = publish(soak,turned(ms,yaw,optical,{.2*std::sin(ms*.0001),.1,-.1}),ms,optical);
        check(pose(out,ms).poseIsValid,"forty-minute angular sweep with missing rear optics stays valid");
        if (out.angular_centering)
            check((out.position-previous).norm() <= .25*.02+1e-10,"centering speed bounded across repeated sweeps");
        previous = out.position;
    }
}
} // namespace
int main() {
    try {
        settings_tests(); default_and_offsets(); loss_and_freshness(); recovery_and_preview(); identities_and_reset(); grace_and_real_packets();
        angular_centering();
        std::cout << "PASS 7 groups: settings/defaults, exact legacy/offsets, prolonged loss/IMU/display, recovery/readonly preview, identity/reset, real packets/grace/trace, angular centering\n";
    } catch (const std::exception& e) { std::cerr << "FAIL " << e.what() << '\n'; return 1; }
}
