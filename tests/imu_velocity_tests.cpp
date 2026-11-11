// SPDX-License-Identifier: MIT
#include "pose_bridge.hpp"
#include <array>
#include <iostream>
#include <stdexcept>

namespace {
using namespace psvr;
void check(bool ok,const char* message) { if (!ok) throw std::runtime_error(message); }
void near(Vec3 actual,Vec3 expected,double tolerance,const char* message) {
    if ((actual-expected).norm()>tolerance) {
        std::cerr<<message<<": error="<<(actual-expected).norm()<<'\n';
        throw std::runtime_error(message);
    }
}
struct Rig {
    ImuConfig config;
    DriverTracker tracker;
    Quaternion truth;
    unsigned index = 0;
    std::uint32_t tick = 0xffff00;
    Rig(ImuConfig c,Quaternion mount) : config(c),tracker(c,DriverClock::time_point{}),truth(mount) {
        for (int i=0;i<600;++i) advance({});
        check(tracker.snapshot().state==ImuState::tracking,"tilted calibration failed");
    }
    void advance(Vec3 world_rate) {
        std::array<unsigned char,64> bytes{};
        bytes[63] = static_cast<unsigned char>(index);
        const auto write16 = [&](unsigned at,double value) {
            const auto raw = static_cast<std::uint16_t>(static_cast<std::int16_t>(std::lround(value)));
            bytes[at] = static_cast<unsigned char>(raw); bytes[at+1] = static_cast<unsigned char>(raw>>8);
        };
        for (unsigned i=0;i<2;++i) {
            const auto body_rate = truth.conjugate().rotate(world_rate);
            truth = (rotation_vector(world_rate*.0005)*truth).normalized();
            const auto accel = truth.conjugate().rotate({0,9.81,0});
            const auto offset = 16+16*i;
            const auto sample_tick = (tick+i*500)&0xffffffu;
            for (unsigned j=0;j<4;++j) bytes[offset+j] = static_cast<unsigned char>(sample_tick>>(8*j));
            write16(offset+4,body_rate.y/config.gyro_scale);
            write16(offset+6,body_rate.x/config.gyro_scale);
            write16(offset+8,-body_rate.z/config.gyro_scale);
            write16(offset+10,accel.y/config.accel_scale);
            write16(offset+12,accel.x/config.accel_scale);
            write16(offset+14,-accel.z/config.accel_scale);
        }
        tracker.packet(bytes.data(),bytes.size(),DriverClock::time_point{}+std::chrono::milliseconds(index));
        tick = (tick+1000)&0xffffffu; ++index;
        check(tracker.snapshot().state!=ImuState::lost,"continuous motion falsely invalidated tracking");
    }
    Vec3 derivative(Quaternion previous) const {
        const auto q = (tracker.snapshot().orientation*previous.conjugate()).normalized();
        const Vec3 v{q.x,q.y,q.z};
        return v.norm()<1e-12 ? Vec3{} : v*(2*std::atan2(v.norm(),q.w)/v.norm()/.001);
    }
    void settle_and_check(Vec3 rate) {
        Quaternion previous;
        for (int i=0;i<1200;++i) { previous = tracker.snapshot().orientation; advance(rate); }
        const auto s = tracker.snapshot();
        near(s.angular_velocity,derivative(previous),1e-5,"velocity differs from quaternion world derivative");
        const auto pose = driver_pose(s,DriverClock::time_point{}+std::chrono::milliseconds(index-1));
        check(pose.poseIsValid,"valid trajectory rejected");
        near({pose.vecAngularVelocity[0],pose.vecAngularVelocity[1],pose.vecAngularVelocity[2]},
             s.angular_velocity,1e-12,"OpenVR rate differs from snapshot");
    }
};
}
int main() {
    try {
        unsigned cases = 0;
        for (const auto mode : {"full","yaw"}) for (const auto smoothing : {0.,50.})
        for (const auto mount : {rotation_vector({30/degrees,0,0}),
                                rotation_vector({30/degrees,0,0})*rotation_vector({0,0,-20/degrees})})
        for (const auto rate : {Vec3{0,-.63,0},Vec3{.315,-.63,.21}}) {
            ImuConfig c; c.startup_discard_ms = 0; c.calibration_ms = 500;
            c.gravity_gain = 0; c.recenter_mode = mode; c.smoothing_ms = smoothing;
            Rig rig(c,mount);
            rig.settle_and_check(rate);
            for (int repeat=0;repeat<3;++repeat) {
                const auto before = rig.tracker.snapshot();
                check(rig.tracker.recenter(),"tracking recenter failed");
                const auto after = rig.tracker.snapshot();
                const auto change = (after.world_rotation*before.world_rotation.conjugate()).normalized();
                near(after.angular_velocity,change.rotate(before.angular_velocity),1e-10,
                     "recenter tilted EMA history through the neutral head offset");
                rig.settle_and_check(rate);
            }
            ++cases;
        }
        std::cout<<"PASS velocity trajectories="<<cases
                 <<" (tilted/mixed neutral, full/yaw, smoothing 0/50ms, pure/mixed rates, 3 repeated recenters)\n";
        return 0;
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}
