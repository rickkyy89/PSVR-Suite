// SPDX-License-Identifier: MIT
#include "pose_bridge.hpp"
#include "imu_loss_report.hpp"
#include "device_session.hpp"
#include <array>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
void check(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
void close_to(double x, double y, const char* message) { check(std::abs(x-y)<1e-8,message); }
std::array<unsigned char,64> packet(std::uint32_t tick, unsigned int sequence, int gyro_y = 0) {
    std::array<unsigned char,64> bytes{};
    bytes[63] = static_cast<unsigned char>(sequence);
    for (int i = 0; i < 2; ++i) {
        const int base = 16+16*i;
        const auto time = (tick+i*500)&0xffffffu;
        for (int j = 0; j < 4; ++j) bytes[base+j] = static_cast<unsigned char>(time>>(8*j));
        const auto raw = static_cast<unsigned short>(gyro_y);
        bytes[base+4] = static_cast<unsigned char>(raw); bytes[base+5] = static_cast<unsigned char>(raw>>8);
        // raw accel X -> mapped +Y gravity, 16384 counts.
        bytes[base+10] = 0; bytes[base+11] = 64;
    }
    return bytes;
}
// Offline ownership name: per-process isolation so this test never contends
// with the production mutex (Local\PSVR-PC-DeviceSession) held by a live
// SteamVR/driver. Production default constructor is unchanged.
std::wstring test_session_name() {
    return L"Local\\PSVR-PC-DeviceSession-Test-" + std::to_wstring(GetCurrentProcessId());
}
}
int main(int argc, char** argv) {
    try {
        using namespace psvr;
        const DriverClock::time_point origin{};
        TrackingSnapshot sample;
        auto pose = driver_pose(sample,origin);
        check(!pose.poseIsValid && !pose.deviceIsConnected,"absent device must be invalid");
        close_to(pose.qRotation.w,1,"invalid pose identity");
        close_to(pose.qWorldFromDriverRotation.w,1,"world transform identity");
        close_to(pose.qDriverFromHeadRotation.w,1,"head transform identity");
        sample.connected = true; sample.state = ImuState::calibrating;
        check(driver_pose(sample,origin).result == vr::TrackingResult_Calibrating_InProgress,"calibration status");
        sample.state = ImuState::tracking; sample.has_sample = true; sample.sampled_at = origin;
        sample.orientation = rotation_vector({0,-.5,0});
        pose = driver_pose(sample,origin+std::chrono::milliseconds(25));
        check(pose.poseIsValid && pose.deviceIsConnected,"fresh pose must be valid");
        close_to(pose.poseTimeOffset,-.025,"pose age in negative seconds");
        close_to(pose.qRotation.y,sample.orientation.y,"WXYZ quaternion basis");
        check(pose.willDriftInYaw && !pose.shouldApplyHeadModel,"3DoF flags");
        for (int axis=0; axis<3; ++axis) {
            close_to(pose.vecPosition[axis],0,"no invented position");
            close_to(pose.vecAngularVelocity[axis],0,"prediction disabled");
        }
        check(!driver_pose(sample,origin+std::chrono::milliseconds(101)).poseIsValid,"stale pose invalidation");
        check(!driver_pose(sample,origin-std::chrono::milliseconds(1)).poseIsValid,"future sample rejected");
        sample.orientation.w = std::numeric_limits<double>::quiet_NaN();
        check(!driver_pose(sample,origin).poseIsValid,"NaN quaternion rejected");
        sample.orientation = {2,0,0,0};
        check(!driver_pose(sample,origin).poseIsValid,"non-unit quaternion rejected");
        sample.orientation = {}; sample.state = ImuState::lost;
        check(!driver_pose(sample,origin).poseIsValid,"lost snapshot rejected");
        sample.state = ImuState::calibration_failed;
        check(!driver_pose(sample,origin).poseIsValid,"failed calibration rejected");

        ImuConfig config; config.startup_discard_ms=0; config.calibration_ms=100;
        DriverTracker tracker(config,origin);
        std::array<unsigned char,64> last{};
        auto now = origin;
        for (unsigned int i=0; i<200; ++i) {
            last = packet(i*1000,i);
            now = origin+std::chrono::milliseconds(i);
            tracker.packet(last.data(),last.size(),now);
        }
        check(driver_pose(tracker.snapshot(),now).poseIsValid,"packet calibration to OpenVR pose");
        const auto good_diagnostics = tracker.diagnostics();
        check(good_diagnostics.reports_received == 200 && good_diagnostics.reports_decoded == 200,
              "diagnostic received/decoded report counters");
        check(good_diagnostics.samples_seen == 400 && good_diagnostics.samples_advanced == 399,
              "diagnostic sample counters include warmup/calibration progress");
        {
            // Stationary calibration: prediction velocity must be ~zero.
            const auto calm = driver_pose(tracker.snapshot(),now);
            check(std::abs(calm.vecAngularVelocity[0])+std::abs(calm.vecAngularVelocity[1])
                  +std::abs(calm.vecAngularVelocity[2]) < 0.05,"stationary prediction velocity near zero");
            // Constant yaw: gyro[0]=1000 counts -> 1.05 rad/s about world Y.
            auto spin = tracker;
            auto spin_now = now;
            for (unsigned int j=0; j<60; ++j) {
                const auto report = packet(200000+j*1000,200+j,1000);
                spin_now = now+std::chrono::milliseconds(j+1);
                spin.packet(report.data(),report.size(),spin_now);
            }
            check(spin.snapshot().state == ImuState::tracking,"rotation must not break tracking");
            const auto vel = spin.snapshot().angular_velocity;
            check(vel.y > 0.85 && vel.y < 1.25 && std::abs(vel.x) < 0.2 && std::abs(vel.z) < 0.2,
                  "world-frame yaw rate follows bias-corrected gyro with smoothing");
            const auto spinning = driver_pose(spin.snapshot(),spin_now);
            check(spinning.poseIsValid
                  && std::abs(spinning.vecAngularVelocity[1]-vel.y) < 1e-12,
                  "valid pose carries smoothed angular velocity for prediction");
        }
        auto sample_duplicate_tracker = tracker;
        auto sample_duplicate_packet = packet(200000,200);
        for (int j=0; j<4; ++j) sample_duplicate_packet[32+j] = sample_duplicate_packet[16+j];
        sample_duplicate_tracker.packet(sample_duplicate_packet.data(),sample_duplicate_packet.size(),now+std::chrono::milliseconds(1));
        check(sample_duplicate_tracker.diagnostics().sample_duplicates == 1
              && sample_duplicate_tracker.diagnostics().samples_rejected == 1
              && sample_duplicate_tracker.snapshot().state == ImuState::tracking,"sample duplicates counted without inventing a fault");
        DriverTracker rollover_tracker(config,origin);
        const auto rollover_packet = packet(0xffff00,0);
        rollover_tracker.packet(rollover_packet.data(),rollover_packet.size(),origin);
        check(std::string(rollover_tracker.diagnostics().reason) == "none"
              && rollover_tracker.diagnostics().tick_delta_us == 500,"24-bit sensor rollover is not diagnosed as a gap");
        auto gap_tracker = tracker;
        const auto gap_packet = packet(210000,200);
        gap_tracker.packet(gap_packet.data(),gap_packet.size(),now+std::chrono::milliseconds(1));
        auto gap_diagnostics = gap_tracker.diagnostics();
        check(gap_tracker.snapshot().state == ImuState::lost && std::string(gap_diagnostics.reason) == "sensor_tick_gap",
              "sensor gap distinguished from host watchdog");
        check(gap_diagnostics.previous_state == ImuState::tracking && gap_diagnostics.previous_tick == 199500
              && gap_diagnostics.sample_tick == 210000 && gap_diagnostics.tick_delta_us == 10500,
              "first fault tick delta is preserved despite the second sample in the report");
        gap_tracker.watchdog(now+std::chrono::seconds(2));
        check(std::string(gap_tracker.diagnostics().reason) == "sensor_tick_gap",
              "subsequent watchdog must not replace first loss cause");
        ImuReadDiagnostics io; io.reads = 201; io.last_bytes = 64; io.last_read_ms = 1.5; io.max_read_ms = 10.5;
        const auto gap_report = imu_loss_report(gap_tracker,config,now+std::chrono::seconds(2),io);
        check(gap_report["event"] == "imu_capture_fault" && gap_report["reason"] == "sensor_tick_gap"
              && gap_report["sample_tick_delta_us"] == 10500 && gap_report["hid_last_read_ms"] == 1.5,
              "fault JSON carries sensor cause and reader measurements");
        check(gap_report["stale_timeout_ms"] == 100 && gap_report["max_sample_gap_us"] == 5000,
              "fault report quotes the active thresholds");
        const auto stamp = tracker.snapshot().sampled_at;
        tracker.packet(last.data(),last.size(),now+std::chrono::milliseconds(50));
        check(tracker.snapshot().sampled_at == stamp,"duplicate report cannot refresh pose");
        check(tracker.snapshot().state == ImuState::tracking,"duplicate must not cause artificial tick gap");
        check(tracker.diagnostics().reports_duplicate == 1,"duplicate reports counted independently of sample duplicates");
        auto delayed = tracker;
        const auto next = packet(200000,200);
        delayed.packet(next.data(),next.size(),now+std::chrono::milliseconds(101));
        check(!driver_pose(delayed.snapshot(),now+std::chrono::milliseconds(101)).poseIsValid,
              "buffered contiguous report after expiry cannot resurrect a pose");
        check(std::string(delayed.diagnostics().reason) == "watchdog_no_advancing_samples"
              && delayed.diagnostics().failure_progress_age_ms == 101
              && delayed.diagnostics().failure_timeout_ms == 100
              && delayed.diagnostics().reports_blocked_by_watchdog == 1,
              "buffered packet rejected by host watchdog has a frozen age and threshold");
        tracker.packet(last.data(),12,now+std::chrono::milliseconds(60));
        check(tracker.snapshot().sampled_at == stamp,"malformed report cannot refresh pose");
        check(tracker.diagnostics().reports_malformed == 1,"malformed report diagnostic counter");
        check(tracker.watchdog(now+std::chrono::milliseconds(100)),"watchdog threshold boundary remains inclusive");
        check(!tracker.watchdog(now+std::chrono::milliseconds(101)),"stale stream watchdog");
        check(!tracker.recenter(),"lost pose cannot recenter");
        {
            // Lost/stale poses must not carry prediction velocity.
            const auto dead = driver_pose(tracker.snapshot(),now+std::chrono::milliseconds(101));
            check(!dead.poseIsValid && dead.vecAngularVelocity[0]==0
                  && dead.vecAngularVelocity[1]==0 && dead.vecAngularVelocity[2]==0,
                  "invalid pose publishes zero angular velocity");
        }
        tracker.recalibrate(now);
        check(!driver_pose(tracker.snapshot(),now).poseIsValid,"recalibrate clears old pose");
        check(tracker.snapshot().angular_velocity.x==0 && tracker.snapshot().angular_velocity.y==0
              && tracker.snapshot().angular_velocity.z==0,"recalibrate clears prediction velocity");
        const auto reset = tracker.diagnostics();
        check(std::string(reset.reason) == "none" && reset.reports_received == 0 && reset.samples_seen == 0
              && reset.samples_rejected == 0 && reset.samples_advanced == 0 && !reset.have_report,
              "recalibration resets diagnostic epoch including fusion lifetime counters");
        DriverTracker no_data(config,origin);
        check(!no_data.watchdog(origin+std::chrono::milliseconds(3001)),"startup timeout");
        check(std::string(no_data.diagnostics().reason) == "watchdog_no_reports"
              && no_data.diagnostics().failure_timeout_ms == 3000,"startup no-report reason");
        const auto no_data_report = imu_loss_report(no_data,config,origin+std::chrono::milliseconds(3001),{});
        check(no_data_report["last_report_sequence"].is_null() && no_data_report["sample_tick_delta_us"].is_null()
              && no_data_report["last_sample_age_ms"].is_null(),"unavailable diagnostic values are null, not invented zeroes");
        io.last_bytes = -1;
        const auto hid_error_report = imu_loss_report(tracker,config,now,io,"hid_read_error");
        check(hid_error_report["reason"] == "hid_read_error" && hid_error_report["hid_last_bytes"] == -1
              && hid_error_report["failure_elapsed_ms"].is_null(),"HID error remains distinct from tracker watchdog failure");
        DriverTracker malformed(config,origin);
        malformed.packet(last.data(),12,origin);
        check(!malformed.watchdog(origin+std::chrono::milliseconds(1001)),"pretracking no-progress timeout");
        check(std::string(malformed.diagnostics().reason) == "watchdog_no_advancing_samples"
              && malformed.diagnostics().failure_timeout_ms == 1000,"pretracking malformed stream has its own timeout");
        auto bad_calibration_config = config;
        bad_calibration_config.calibration_gyro_mean_dps = 0;
        DriverTracker bad_calibration(bad_calibration_config,origin);
        for (unsigned int i=0; i<120 && bad_calibration.snapshot().state != ImuState::calibration_failed; ++i) {
            const auto report = packet(i*1000,i,1000);
            bad_calibration.packet(report.data(),report.size(),origin+std::chrono::milliseconds(i));
        }
        const auto calibration_report = imu_loss_report(bad_calibration,bad_calibration_config,origin+std::chrono::milliseconds(120),{});
        check(calibration_report["reason"] == "calibration_failed"
              && calibration_report["previous_state"] == "calibrating"
              && !calibration_report["calibration_error"].get<std::string>().empty(),"calibration failure diagnostics preserve the specific fusion error");
        // Offline ownership contract on an isolated per-process mutex name.
        // Production default (Local\PSVR-PC-DeviceSession) is unchanged and still
        // held by any live SteamVR/driver; this test never contends with it.
        // A second owner (different thread, hence non-recursive mutex acquire)
        // must fail; enumeration is not part of this ownership guard.
        {
            const auto isolated = test_session_name();
            DeviceSession owner(isolated);
            bool rejected = false;
            std::thread competing([&] {
                try { DeviceSession second(isolated); } catch (const std::runtime_error&) { rejected = true; }
            });
            competing.join(); check(rejected,"concurrent PSVR owner must be rejected");
            bool distinct_ok = false;
            try { DeviceSession other(isolated + L"-other"); distinct_ok = true; }
            catch (...) { distinct_ok = false; }
            check(distinct_ok,"distinct session names must not contend");
        }
        { DeviceSession restarted(test_session_name()); }
        check(argc == 2,"DLL path required for lifecycle test");
        const auto module = LoadLibraryA(argv[1]);
        check(module != nullptr,"Windows DLL load failed");
        using Factory = void* (*)(const char*,int*);
        const auto factory = reinterpret_cast<Factory>(GetProcAddress(module,"HmdDriverFactory"));
        check(factory != nullptr,"factory export missing");
        int error = -1;
        auto* provider = static_cast<vr::IServerTrackedDeviceProvider*>(factory(vr::IServerTrackedDeviceProvider_Version,&error));
        check(provider && error == vr::VRInitError_None,"provider factory");
        check(provider->GetInterfaceVersions() != nullptr,"interface versions");
        check(!provider->ShouldBlockStandbyMode(),"no forced standby blocking");
        check(provider->Init(nullptr) == vr::VRInitError_Init_InterfaceNotFound,"null context handled");
        struct MissingContext : vr::IVRDriverContext {
            void* GetGenericInterface(const char*,vr::EVRInitError* e) override {
                if (e) *e=vr::VRInitError_Init_InterfaceNotFound;
                return nullptr;
            }
            vr::DriverHandle_t GetDriverHandle() override { return 0; }
        } context;
        check(provider->Init(&context) == vr::VRInitError_Init_InterfaceNotFound,"missing host handled");
        provider->RunFrame(); provider->EnterStandby(); provider->LeaveStandby();
        provider->Cleanup(); provider->Cleanup();
        FreeLibrary(module);
        std::cout << "PASS OpenVR pose contract, packet adapter, duplicate/stale/startup/recalibration gates\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
