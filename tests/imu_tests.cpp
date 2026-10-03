// SPDX-License-Identifier: MIT
#include "imu_fusion.hpp"
#include "imu_runtime.hpp"
#include <iostream>
#include <vector>

namespace {
void check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
void near(double value, double expected, double tolerance, const char* message) { check(std::abs(value-expected) <= tolerance,message); }
psvr::SensorSample sample(std::uint32_t tick, psvr::Vec3 gyro, psvr::Vec3 accel = {0,9.81,0}) {
    psvr::ImuConfig c;
    const auto raw = [](double x) { return static_cast<std::int16_t>(std::lround(x)); };
    return {tick,{raw(gyro.y/c.gyro_scale),raw(gyro.x/c.gyro_scale),raw(-gyro.z/c.gyro_scale)},
                 {raw(accel.y/c.accel_scale),raw(accel.x/c.accel_scale),raw(-accel.z/c.accel_scale)}};
}
struct Rig {
    std::uint32_t tick = 0;
    psvr::Vec3 bias{-.00735,.0126,-.00315};
    psvr::ImuFusion tracker;
    explicit Rig(std::string recenter_mode = "full") : tracker([&] {
        psvr::ImuConfig c; c.startup_discard_ms = 0; c.calibration_ms = 500; c.recenter_mode = recenter_mode; return c;
    }()) {
        for (int i = 0; i < 1200; ++i) { tracker.update(sample(tick,bias)); tick = (tick+500)&0xffffffu; }
        check(tracker.state() == psvr::ImuState::tracking,"stationary calibration did not finish");
    }
    void turn(psvr::Vec3 rate, int count) {
        psvr::Quaternion truth{};
        for (int i = 0; i < count; ++i) {
            truth = (truth*psvr::rotation_vector(rate*.0005)).normalized();
            const auto accel = truth.conjugate().rotate({0,9.81,0});
            tracker.update(sample(tick,rate+bias,accel)); tick = (tick+500)&0xffffffu;
        }
    }
};
}
int main() {
    try {
        using namespace psvr;
        const auto yaw = rotation_vector({0,pi/2,0});
        near(yaw.rotate({0,0,-1}).x,-1,1e-10,"+Y yaw must point left");
        near(rotation_vector({pi/2,0,0}).rotate({0,0,-1}).y,1,1e-10,"+X pitch must point up");
        near(rotation_vector({0,0,-pi/2}).rotate({0,1,0}).x,1,1e-10,"negative Z roll must tilt right");
        const auto combined = rotation_vector({0,.4,0})*rotation_vector({.3,0,0})*rotation_vector({0,0,-.2});
        const auto a = angles(combined);
        near(a.yaw,.4*degrees,1e-9,"Euler YXZ yaw wrong"); near(a.pitch,.3*degrees,1e-9,"Euler YXZ pitch wrong"); near(a.roll,-.2*degrees,1e-9,"Euler YXZ roll wrong");
        near(align_up({2,8,-4}).rotate(Vec3{2,8,-4}*(1/std::sqrt(84.0))).y,1,1e-9,"gravity alignment wrong");
        near(align_up({0,-9.81,0}).rotate({0,-1,0}).y,1,1e-9,"upside-down gravity singularity");
        near(angle_difference(-179,179),2,1e-12,"yaw wrap wrong");
        Rig heading;
        near((heading.tracker.bias()-heading.bias).norm(),0,1e-9,"gyro bias not removed");
        heading.turn({0,1.575,0},2000);
        near(angles(heading.tracker.orientation()).yaw,1.575*degrees,.01,"gyro yaw integration/unit scale wrong");
        check(heading.tracker.recenter(),"recenter failed");
        near(rotation_distance(heading.tracker.orientation(),{}),0,1e-8,"full recenter does not produce identity");
        Rig pitch;
        pitch.turn({1.05,0,0},2000);
        near(angles(pitch.tracker.orientation()).pitch,1.05*degrees,.05,"pitch sign/integration wrong");
        Rig roll;
        roll.turn({0,0,-.63},2000);
        near(angles(roll.tracker.orientation()).roll,-.63*degrees,.05,"roll sign/integration wrong");
        Rig yaw_only("yaw");
        yaw_only.turn({.63,0,0},1000);
        const auto before = angles(yaw_only.tracker.orientation()).pitch;
        yaw_only.tracker.recenter();
        near(angles(yaw_only.tracker.orientation()).pitch,before,1e-8,"yaw recenter erased pitch");
        near(yaw_only.tracker.output().norm(),1,1e-12,"quaternion not normalized");
        const auto duplicated = sample(yaw_only.tick-500,yaw_only.bias);
        const auto last_gyro = yaw_only.tracker.gyro(), last_accel = yaw_only.tracker.accel();
        check(!yaw_only.tracker.update(duplicated) && yaw_only.tracker.state() == ImuState::tracking,"duplicate tick corrupts tracking");
        near((yaw_only.tracker.gyro()-last_gyro).norm(),0,1e-12,"rejected duplicate replaced accepted gyro values");
        near((yaw_only.tracker.accel()-last_accel).norm(),0,1e-12,"rejected duplicate replaced accepted acceleration");
        yaw_only.tracker.update(sample(yaw_only.tick+100000,yaw_only.bias));
        check(yaw_only.tracker.state() == ImuState::lost,"large clock gap did not invalidate tracking");
        ImuConfig c; c.startup_discard_ms = 0; c.calibration_ms = 500;
        ImuFusion wraps(c); std::uint32_t tick = 0xffff00;
        for (int i = 0; i < 1200; ++i) { wraps.update(sample(tick,{})); tick = (tick+500)&0xffffffu; }
        check(wraps.state() == ImuState::tracking && wraps.rollovers == 1 && wraps.gaps == 0,"24-bit wrap failed");
        ImuFusion moving(c);
        for (int i = 0; i < 1100; ++i) moving.update(sample(i*500,{i%2 ? .2 : -.2,0,0}));
        check(moving.state() == ImuState::calibration_failed,"moving calibration accepted");
        ImuFusion no_gravity(c);
        for (int i = 0; i < 1100; ++i) no_gravity.update(sample(i*500,{},{}));
        check(no_gravity.state() == ImuState::calibration_failed,"zero acceleration calibration accepted");
        // Real headset regression: a tilted neutral sensor must not tilt WORLD
        // axes. Pure world yaw after neutral full recenter must have no roll.
        ImuFusion tilted(c);
        const auto mount = rotation_vector({30/degrees,0,0});
        auto truth = mount;
        const auto fixed_accel = mount.conjugate().rotate({0,9.81,0});
        tick = 0;
        for (int i = 0; i < 1200; ++i) { tilted.update(sample(tick,{},fixed_accel)); tick += 500; }
        check(tilted.state() == ImuState::tracking,"tilted calibration failed");
        near(rotation_distance(tilted.orientation(),{}),0,1e-8,"tilted neutral not identity");
        for (int i = 0; i < 2000; ++i) {
            truth = (rotation_vector({0,-.63*.0005,0})*truth).normalized();
            tilted.update(sample(tick,truth.conjugate().rotate({0,-.63,0}),truth.conjugate().rotate({0,9.81,0})));
            tick += 500;
        }
        const auto yaw_tilted = angles(tilted.orientation());
        near(yaw_tilted.yaw,-.63*degrees,.08,"tilted neutral world yaw wrong");
        near(yaw_tilted.pitch,0,.08,"world yaw coupled into pitch");
        near(yaw_tilted.roll,0,.08,"world yaw coupled into roll");
        tilted.recenter();
        near(rotation_distance(tilted.orientation(),{}),0,1e-8,"repeated full recenter failed");
        tilted.restart_calibration();
        for (int i = 0; i < 1200; ++i) { tilted.update(sample(tick,{})); tick += 500; }
        near(rotation_distance(tilted.orientation(),{}),0,1e-8,"head offset survives calibration restart");
        ImuFusion settling(c);
        for (int i = 0; i < 1100; ++i) settling.update(sample(i*500,{0,i < 500 ? .0021 : .0042,0}));
        check(settling.state() == ImuState::calibration_failed && settling.calibration_error().find("changes") != std::string::npos,
              "changing mean with low STD accepted as stable bias");
        ImuProgressWatchdog silence;
        check(silence.failure(500,false,100) == ProgressFailure::none,"startup timeout too early");
        check(silence.failure(3001,false,100) == ProgressFailure::no_reports,"silent startup not bounded");
        ImuProgressWatchdog malformed;
        malformed.report_received();
        check(malformed.failure(1001,false,100) == ProgressFailure::no_advancing_samples,"malformed-only stream bypasses timeout");
        ImuProgressWatchdog stalled_cal;
        stalled_cal.report_received(); stalled_cal.advanced(200);
        check(stalled_cal.failure(1201,false,100) == ProgressFailure::no_advancing_samples,"stalled calibration not bounded");
        ImuProgressWatchdog dup_stream;
        for (int i = 0; i < 100; ++i) dup_stream.report_received();
        check(dup_stream.failure(1001,false,100) == ProgressFailure::no_advancing_samples,"duplicate-only stream bypasses timeout");
        ImuProgressWatchdog live;
        live.report_received(); live.advanced(900);
        check(live.failure(950,true,100) == ProgressFailure::none && live.failure(1001,true,100) == ProgressFailure::no_advancing_samples,
              "tracking progress timeout wrong");
        std::vector<nlohmann::json> terminal_events;
        const std::function<void(const char*,const std::string&)> sink = [&](const char*,const std::string& value) { terminal_events.push_back(nlohmann::json::parse(value)); };
        try { CaptureTerminal terminal(true,sink); throw std::runtime_error("injected CSV failure"); } catch (const std::runtime_error&) {}
        check(terminal_events.size() == 1 && !terminal_events[0]["valid"].get<bool>() && terminal_events[0]["reason"] == "capture-exception",
              "exceptional capture exit did not invalidate telemetry");
        terminal_events.clear();
        { CaptureTerminal terminal(true,sink); terminal.end("completed"); }
        check(terminal_events.size() == 1,"capture finalized twice");
        const std::function<void(const char*,const std::string&)> broken_sink = [](const char*,const std::string&) { throw std::runtime_error("broken output pipe"); };
        bool preserved = false;
        try { CaptureTerminal terminal(true,broken_sink); throw std::runtime_error("original failure"); }
        catch (const std::runtime_error& e) { preserved = std::string(e.what()) == "original failure"; }
        check(preserved,"terminal sink failure masked original exception");
        std::cout << "PASS math: right-handed axes, YXZ Euler, gravity alignment, yaw wrap\n"
                  << "PASS fusion: bias, known angular trajectories, quaternion normalization, recenter modes\n"
                  << "PASS validation: motion/zero-gravity rejection, duplicate tick, large gap, 24-bit rollover\n"
                  << "PASS tilted neutral: world yaw not coupled into pitch/roll, repeat/reset lifecycle\n"
                  << "PASS calibration stability: reject changing mean even at low STD\n"
                  << "PASS capture runtime: startup/progress watchdog, rejected data, exceptional terminal invalidation\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << "FAIL " << e.what() << '\n'; return 1; }
}
