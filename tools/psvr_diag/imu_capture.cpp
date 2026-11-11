// SPDX-License-Identifier: MIT
#include "imu_capture.hpp"
#include "pnp_windows.hpp"
#include "imu_runtime.hpp"
#include <windows.h>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <sstream>

namespace psvr {
namespace {
using Clock = std::chrono::steady_clock;
std::string vector_text(Vec3 v) {
    std::ostringstream s;
    s << std::fixed << std::setprecision(6) << '(' << v.x << ',' << v.y << ',' << v.z << ')';
    return s.str();
}
std::wstring event_name(int pid, const wchar_t* action = L"Recenter") {
    return L"Local\\PSVR-PC-"+std::wstring(action)+L"-"+std::to_wstring(pid);
}
struct EventHandle {
    HANDLE value = nullptr;
    ~EventHandle() { if (value) CloseHandle(value); }
};
char console_key(HANDLE input) {
    DWORD mode = 0, count = 0;
    if (!GetConsoleMode(input,&mode) || !GetNumberOfConsoleInputEvents(input,&count) || !count) return 0;
    INPUT_RECORD event{};
    DWORD read = 0;
    if (!ReadConsoleInputW(input,&event,1,&read) || read != 1 || event.EventType != KEY_EVENT || !event.Event.KeyEvent.bKeyDown) return 0;
    return static_cast<char>(event.Event.KeyEvent.uChar.AsciiChar);
}
struct PoseStats {
    unsigned long long count = 0;
    VectorStats gyro{}, accel{};
    double yaw = 0, initial_yaw = 0, previous_yaw = 0, last_time = 0, first_time = 0;
    double min_yaw = 0, max_yaw = 0, min_pitch = 0, max_pitch = 0, min_roll = 0, max_roll = 0;
    Angles initial{};
    void add(const ImuFusion& fusion) {
        const auto a = angles(fusion.orientation());
        if (!count) {
            initial = a; yaw = initial_yaw = previous_yaw = a.yaw;
            min_yaw = max_yaw = yaw; min_pitch = max_pitch = a.pitch; min_roll = max_roll = a.roll;
            first_time = fusion.elapsed_s();
        } else { yaw += angle_difference(a.yaw,previous_yaw); previous_yaw = a.yaw; }
        last_time = fusion.elapsed_s(); ++count;
        min_yaw = std::min(min_yaw,yaw); max_yaw = std::max(max_yaw,yaw);
        min_pitch = std::min(min_pitch,a.pitch); max_pitch = std::max(max_pitch,a.pitch);
        min_roll = std::min(min_roll,a.roll); max_roll = std::max(max_roll,a.roll);
        gyro.add(fusion.corrected_gyro()); accel.add(fusion.accel());
    }
    double seconds() const { return last_time-first_time; }
    double drift() const { return seconds() > 0 ? (yaw-initial_yaw)*60/seconds() : 0; }
};
}

void request_recenter(int pid) {
    EventHandle event{OpenEventW(EVENT_MODIFY_STATE,FALSE,event_name(pid).c_str())};
    if (!event.value || !SetEvent(event.value))
        throw std::runtime_error("recenter target unavailable Windows error="+std::to_string(GetLastError()));
}
void request_recalibrate(int pid) {
    EventHandle event{OpenEventW(EVENT_MODIFY_STATE,FALSE,event_name(pid,L"Calibrate").c_str())};
    if (!event.value || !SetEvent(event.value)) throw std::runtime_error("calibration target unavailable Windows error="+std::to_string(GetLastError()));
}
void request_stop_imu(int pid) {
    EventHandle event{OpenEventW(EVENT_MODIFY_STATE,FALSE,event_name(pid,L"Stop").c_str())};
    if (!event.value || !SetEvent(event.value)) throw std::runtime_error("stop target unavailable Windows error="+std::to_string(GetLastError()));
}

int capture_imu(hid_device* sensor, WinUsbControl* control, const Options& options, const ImuLog& log,
                 std::atomic<bool>& interrupted, const std::filesystem::path& csv_path) {
    return capture_imu(sensor,control,options,log,interrupted,csv_path,Clock::now);
}

int capture_imu(hid_device* sensor, WinUsbControl* control, const Options& options, const ImuLog& log,
                std::atomic<bool>& interrupted, const std::filesystem::path& csv_path, ImuNow now) {
    if (std::filesystem::exists(csv_path)) throw std::runtime_error("IMU CSV already exists: "+csv_path.u8string());
    std::ofstream csv(csv_path);
    if (!csv) throw std::runtime_error("cannot create IMU CSV");
    csv << "host_elapsed_s,device_elapsed_s,tick24,state,valid,yaw_deg,pitch_deg,roll_deg,qw,qx,qy,qz,gx_rad_s,gy_rad_s,gz_rad_s,ax_mps2,ay_mps2,az_mps2,recenter_count,sensor_worn,sensor_status_raw,sensor_ir_raw,sensor_buttons_raw,sensor_volume_raw,sensor_headset_flags_raw\n";
    const auto pid = static_cast<int>(GetCurrentProcessId());
    EventHandle recenter_event{CreateEventW(nullptr,FALSE,FALSE,event_name(pid).c_str())};
    EventHandle calibrate_event{CreateEventW(nullptr,FALSE,FALSE,event_name(pid,L"Calibrate").c_str())};
    EventHandle stop_event{CreateEventW(nullptr,FALSE,FALSE,event_name(pid,L"Stop").c_str())};
    if (!recenter_event.value || !calibrate_event.value || !stop_event.value)
        throw std::runtime_error("cannot create IMU events Windows error="+std::to_string(GetLastError()));
    ImuFusion fusion(options.tracking);
    PoseStats stats;
    unsigned long long reports = 0, malformed = 0, duplicate_reports = 0, sequence_gaps = 0;
    unsigned int previous_sequence = 0, previous_last_tick = 0;
    bool have_sequence = false, scheduled_recenter = false;
    ImuState previous_state = ImuState::warming;
    const auto start = now();
    const auto deadline = options.duration_ms ? start+std::chrono::milliseconds(options.duration_ms) : Clock::time_point::max();
    auto next_output = start;
    auto next_control = start;
    ImuProgressWatchdog progress;
    std::uint32_t latest_tick = 0;
    SensorPacket latest_sensor{};
    bool stopped = false;
    const auto input = GetStdHandle(STD_INPUT_HANDLE);
    CaptureTerminal terminal(options.json_stream,log);
    const auto finish = [&](int code,const char* reason) { terminal.end(reason); return code; };
    const auto host_ms = [&] { return std::chrono::duration_cast<std::chrono::milliseconds>(now()-start).count(); };
    log("INFO","IMU capture: place headset on a stable surface, hands off during calibration; recenter afterwards changes reference, NOT gyro bias");
    log("INFO","IMU CSV="+std::filesystem::absolute(csv_path).u8string());
    log("INFO","IMU recenter: R in this console or psvr_diag --recenter "+std::to_string(pid)+"; C restarts calibration; mode="+options.tracking.recenter_mode);
    if (!options.axis_test.empty()) log("INFO","axis-test="+options.axis_test+": wait for TRACKING (calibration_ms="+
        std::to_string(options.tracking.calibration_ms)+
        "); baseline is captured at calibration completion; manual recenter is disabled; then move 20-40 degrees in requested direction and HOLD");
    const auto state_event = [&](ImuState state, const char* reason) {
        if (options.json_stream) log("DATA",nlohmann::json{{"schema_version",1},{"event","imu_state"},
            {"state",state_name(state)},{"valid",state == ImuState::tracking},{"reason",reason}}.dump());
    };
    const auto vector_json = [](Vec3 v) { return nlohmann::json::array({v.x,v.y,v.z}); };
    state_event(ImuState::warming,"capture-started");
    int result = 0;
    const auto check_progress = [&] {
        const auto failure = progress.failure(host_ms(),fusion.state() == ImuState::tracking,options.tracking.stale_timeout_ms);
        if (failure == ProgressFailure::none) return true;
        if (failure == ProgressFailure::no_reports) {
            log("ERROR","IMU no sensor reports within startup timeout; check headset power");
            state_event(ImuState::lost,"no-sensor-reports"); result = 4;
        } else {
            fusion.mark_lost();
            log("ERROR","IMU stream has no advancing samples: pose invalid, restart capture/calibration required");
            state_event(fusion.state(),"no-advancing-samples"); result = 7;
        }
        return false;
    };
    while (now() < deadline && !interrupted.load()) {
        const auto stop_result = WaitForSingleObject(stop_event.value,0);
        if (stop_result == WAIT_FAILED) throw std::runtime_error("stop event wait failed");
        if (stop_result == WAIT_OBJECT_0) { stopped = true; break; }
        std::array<unsigned char,512> buffer{};
        const auto count = hid_read_timeout(sensor,buffer.data(),buffer.size(),10);
        if (count < 0) { log("ERROR","IMU HID read failed: "+utf8(hid_read_error(sensor))); state_event(ImuState::lost,"sensor-read-failed"); result = 3; break; }
        // Check the PREVIOUS progress before buffered data can refresh it.
        // The configured limits remain inclusive: ordinary jitter and a gap
        // exactly at stale_timeout_ms are still accepted.
        if (!check_progress()) break;
        if (count > 0) {
            ++reports;
            progress.report_received();
            const auto packet = decode_sensor(buffer.data(),static_cast<std::size_t>(count));
            if (!packet) { ++malformed; }
            else {
                bool duplicate = false;
                if (have_sequence) {
                    const auto delta = (unsigned(packet->sequence)-previous_sequence)&0xffu;
                    duplicate = delta == 0 && packet->samples[1].tick == previous_last_tick;
                    if (!duplicate && delta != 1) ++sequence_gaps;
                }
                if (duplicate) ++duplicate_reports;
                else {
                    have_sequence = true; previous_sequence = packet->sequence; previous_last_tick = packet->samples[1].tick;
                    latest_sensor = *packet;
                    for (const auto& sample : packet->samples) {
                        const auto accepted = fusion.update(sample);
                        if (accepted) {
                            progress.advanced(host_ms()); latest_tick = sample.tick&0xffffffu;
                            if (fusion.state() == ImuState::tracking) stats.add(fusion);
                        }
                    }
                }
            }
        }
        // F0 is event-driven. Optional GUI telemetry reads the documented USB IN
        // pipe only; no status-query command and no interpretation of unknown bits.
        if (options.json_stream && control && now() >= next_control) {
            try {
                const auto bytes = control->read(1);
                if (const auto status = decode_status(bytes.data(),bytes.size())) {
                    const auto mode = !status->powered() ? "off" : status->cinematic() && status->vr_active == 0 ? "cinema" :
                                      !status->cinematic() && status->vr_active == 1 ? "vr" : "transition";
                    log("DATA",nlohmann::json{{"schema_version",1},{"event","control_status"},{"power_on",status->powered()},
                        {"worn",status->worn()},{"mode",mode},{"vr_active",status->vr_active},{"status_mask",status->mask}}.dump());
                }
            } catch (const std::exception& error) {
                log("ERROR",std::string("IMU control polling failed: ")+error.what());
                state_event(ImuState::lost,"control-read-failed"); result = 3; break;
            }
            next_control = now()+std::chrono::milliseconds(50);
        }
        if (!check_progress()) break;
        if (fusion.state() != previous_state) {
            log("INFO",std::string("IMU state=")+state_name(fusion.state()));
            state_event(fusion.state(),fusion.state() == ImuState::calibration_failed ? fusion.calibration_error().c_str() : "fusion-transition");
            if (fusion.state() == ImuState::tracking || fusion.state() == ImuState::calibration_failed) {
                log("INFO","IMU calibration samples="+std::to_string(fusion.calibration_gyro().count)+
                    " gyro_mean_dps="+vector_text(fusion.calibration_gyro().mean*degrees)+
                    " gyro_std_dps="+vector_text(fusion.calibration_gyro().deviation()*degrees)+
                    " accel_std_mps2="+vector_text(fusion.calibration_accel().deviation())+
                    " gyro_halves_mean_shift_dps="+vector_text(fusion.calibration_mean_shift())+
                    " accel_norm="+std::to_string(fusion.calibration_accel().mean.norm()));
            }
            previous_state = fusion.state();
        }
        if (fusion.state() == ImuState::calibration_failed || fusion.state() == ImuState::lost) {
            log("ERROR","IMU pose invalid: "+(fusion.state() == ImuState::calibration_failed ? fusion.calibration_error() :
                "clock/stream discontinuity")+"; no assumed orientation");
            result = 7; break;
        }
        const auto elapsed_ms = host_ms();
        const auto key = console_key(input);
        const auto wait_result = WaitForSingleObject(recenter_event.value,0);
        if (wait_result == WAIT_FAILED) throw std::runtime_error("recenter event wait failed");
        const bool recenter = key == 'r' || key == 'R' || wait_result == WAIT_OBJECT_0 ||
            (options.recenter_at_ms && !scheduled_recenter && elapsed_ms >= options.recenter_at_ms);
        if (recenter) {
            if (options.recenter_at_ms && elapsed_ms >= options.recenter_at_ms) scheduled_recenter = true;
            if (options.stationary_test || !options.axis_test.empty()) log("WARN","recenter ignored during measurement profile");
            else if (fusion.recenter()) {
                const auto a = angles(fusion.orientation());
                log("INFO","IMU RECENTER verified yaw_deg="+std::to_string(a.yaw)+" pitch_deg="+std::to_string(a.pitch)+
                    " roll_deg="+std::to_string(a.roll)+" count="+std::to_string(fusion.recenter_count));
                stats = {}; next_output = now();
            } else log("WARN","recenter ignored: calibration not ready");
        }
        const auto calibrate_result = WaitForSingleObject(calibrate_event.value,0);
        if (calibrate_result == WAIT_FAILED) throw std::runtime_error("calibration event wait failed");
        if (key == 'c' || key == 'C' || calibrate_result == WAIT_OBJECT_0) {
            if (options.stationary_test || !options.axis_test.empty()) log("WARN","recalibration ignored during measurement profile");
            else { fusion.restart_calibration(); stats = {}; previous_state = ImuState::warming;
                state_event(ImuState::warming,"operator-recalibration"); log("INFO","IMU calibration restarted by operator"); }
        }
        if (now() >= next_output) {
            const auto q = fusion.output(); const auto a = angles(q);
            const auto gyro = fusion.gyro(), accel = fusion.accel();
            const bool valid = fusion.state() == ImuState::tracking;
            std::ostringstream line;
            line << std::fixed << std::setprecision(4) << "IMU tick24=" << latest_tick << " state=" << state_name(fusion.state()) << " valid=" << valid
                 << " gyro_rad_s=" << vector_text(gyro) << " accel_mps2=" << vector_text(accel)
                 << " yaw_deg=" << a.yaw << " pitch_deg=" << a.pitch << " roll_deg=" << a.roll
                 << " quat_wxyz=(" << q.w << ',' << q.x << ',' << q.y << ',' << q.z << ')';
            log("INFO",line.str());
            if (options.json_stream && have_sequence) {
                const auto sensor_q = fusion.sensor_orientation();
                const auto host_elapsed = std::chrono::duration<double>(now()-start).count();
                log("DATA",nlohmann::json{{"schema_version",1},{"event","imu"},{"host_elapsed_s",host_elapsed},
                    {"device_elapsed_s",fusion.elapsed_s()},{"tick24",latest_tick},{"state",state_name(fusion.state())},{"valid",valid},
                    {"angles_deg",{{"yaw",a.yaw},{"pitch",a.pitch},{"roll",a.roll}}},
                    {"quaternion_wxyz",nlohmann::json::array({q.w,q.x,q.y,q.z})},{"gyro_rad_s",vector_json(gyro)},
                    {"quaternion_sensor_wxyz",nlohmann::json::array({sensor_q.w,sensor_q.x,sensor_q.y,sensor_q.z})},
                    {"corrected_gyro_rad_s",vector_json(fusion.corrected_gyro())},{"accel_mps2",vector_json(accel)},
                    {"recenter_count",fusion.recenter_count},
                    {"sensor",{{"worn",latest_sensor.worn()},{"status_raw",latest_sensor.status_raw},{"ir_raw",latest_sensor.ir_raw},
                               {"buttons_raw",latest_sensor.buttons_raw},{"volume_raw",latest_sensor.volume_raw},
                               {"headset_flags_raw",latest_sensor.headset_flags_raw},
                               {"display_flag_raw",(latest_sensor.status_raw & 2) != 0}}},
                    {"calibration",{{"samples",fusion.calibration_gyro().count},{"target_ms",options.tracking.calibration_ms},
                        {"elapsed_ms",fusion.calibration_elapsed_ms()},{"gyro_mean_dps",vector_json(fusion.calibration_gyro().mean*degrees)},
                        {"gyro_halves_mean_shift_dps",vector_json(fusion.calibration_mean_shift())},
                        {"gyro_std_limit_dps",options.tracking.calibration_gyro_std_dps},{"gyro_mean_shift_limit_dps",options.tracking.calibration_mean_shift_dps},
                        {"gyro_std_dps",vector_json(fusion.calibration_gyro().deviation()*degrees)},
                        {"accel_std_mps2",vector_json(fusion.calibration_accel().deviation())}}},
                    {"rate",{{"reports",reports},{"report_hz",reports/std::max(1e-6,host_elapsed)},{"samples",fusion.samples},
                        {"sample_hz",fusion.integrated_us ? fusion.integrated*1e6/fusion.integrated_us : 0}}}}.dump());
            }
            csv << std::setprecision(12) << elapsed_ms*0.001 << ',' << fusion.elapsed_s() << ',' << latest_tick << ',' << state_name(fusion.state()) << ',' << valid
                << ',' << a.yaw << ',' << a.pitch << ',' << a.roll << ',' << q.w << ',' << q.x << ',' << q.y << ',' << q.z
                << ',' << gyro.x << ',' << gyro.y << ',' << gyro.z << ',' << accel.x << ',' << accel.y << ',' << accel.z << ',' << fusion.recenter_count
                << ',' << latest_sensor.worn() << ',' << unsigned(latest_sensor.status_raw) << ',' << latest_sensor.ir_raw
                << ',' << unsigned(latest_sensor.buttons_raw) << ',' << unsigned(latest_sensor.volume_raw) << ',' << unsigned(latest_sensor.headset_flags_raw) << '\n';
            if (!csv) throw std::runtime_error("IMU CSV write failed");
            next_output = now()+std::chrono::milliseconds(options.tracking.output_interval_ms);
        }
    }
    csv.flush(); if (!csv) throw std::runtime_error("IMU CSV flush failed");
    const auto seconds = std::chrono::duration<double>(now()-start).count();
    std::ostringstream summary;
    summary << std::fixed << std::setprecision(6) << "IMU summary reports=" << reports << " report_hz=" << reports/seconds
            << " samples=" << fusion.samples << " integrated=" << fusion.integrated << " integrated_s=" << fusion.integrated_us*1e-6
            << " sample_hz=" << (fusion.integrated_us ? fusion.integrated*1e6/fusion.integrated_us : 0)
            << " tick_dt_us_min=" << (fusion.integrated ? fusion.min_dt_us : 0) << " tick_dt_us_max=" << fusion.max_dt_us
            << " rollovers=" << fusion.rollovers << " clock_gaps=" << fusion.gaps << " duplicate_samples=" << fusion.duplicates
            << " duplicate_reports=" << duplicate_reports << " malformed=" << malformed << " sequence_discontinuities=" << sequence_gaps
            << " quaternion_max_norm_error=" << fusion.max_quaternion_error << " recenters=" << fusion.recenter_count;
    log("INFO",summary.str());
    log("INFO",std::string(options.stationary_test ? "IMU noise" : "IMU sample variation")+
        " gyro_std_dps="+vector_text(stats.gyro.deviation()*degrees)+" accel_std_mps2="+vector_text(stats.accel.deviation()));
    log("INFO","IMU rotation ranges yaw=("+std::to_string(stats.min_yaw-stats.initial_yaw)+","+std::to_string(stats.max_yaw-stats.initial_yaw)+") pitch=("+
        std::to_string(stats.min_pitch-stats.initial.pitch)+","+std::to_string(stats.max_pitch-stats.initial.pitch)+") roll=("+
        std::to_string(stats.min_roll-stats.initial.roll)+","+std::to_string(stats.max_roll-stats.initial.roll)+") tracking_s="+std::to_string(stats.seconds()));
    log("INFO","IMU yaw_delta_deg="+std::to_string(stats.yaw-stats.initial_yaw)+" drift_dpm="+std::to_string(stats.drift())+
        (options.stationary_test ? " (user-declared stationary test)" : " (NOT a drift measurement unless physically stationary)"));
    if (interrupted.load()) return finish(130,"interrupted");
    if (result) return finish(result,"capture-error");
    if (stopped) { log("INFO","IMU stopped by operator (not a measurement PASS)"); return finish(0,"operator-stop"); }
    if (!reports) return finish(4,"no-data");
    if (fusion.state() != ImuState::tracking || stats.count < 100) { log("ERROR","IMU capture incomplete: calibration/tracking not ready"); return finish(7,"incomplete"); }
    if (options.stationary_test) {
        const bool pass = stats.seconds() >= 30 && max_axis(stats.gyro.deviation()*degrees) <= options.tracking.calibration_gyro_std_dps &&
            max_axis(stats.accel.deviation()) <= options.tracking.calibration_accel_std && std::abs(stats.drift()) <= options.tracking.max_drift_dpm;
        log(pass ? "INFO" : "ERROR",std::string("IMU STATIONARY TEST ")+(pass ? "PASS" : "FAIL")+" (noise, >=30 s tracking, yaw drift threshold)");
        if (!pass) return finish(7,"stationary-quality-failed");
    }
    if (!options.axis_test.empty()) {
        const auto yaw = stats.initial_yaw-stats.min_yaw, pitch = stats.max_pitch-stats.initial.pitch, roll = stats.initial.roll-stats.min_roll;
        const auto yaw_span = stats.max_yaw-stats.min_yaw, pitch_span = stats.max_pitch-stats.min_pitch, roll_span = stats.max_roll-stats.min_roll;
        const auto primary = options.axis_test == "yaw-right" ? yaw : options.axis_test == "pitch-up" ? pitch : roll;
        const auto other = options.axis_test == "yaw-right" ? std::max(pitch_span,roll_span) :
                           options.axis_test == "pitch-up" ? std::max(yaw_span,roll_span) : std::max(yaw_span,pitch_span);
        const bool pass = primary >= 10 && other <= std::max(10.0,primary*0.6);
        log(pass ? "INFO" : "ERROR","IMU AXIS TEST "+options.axis_test+(pass ? " PASS" : " FAIL")+
            " (signed excursion >=10 deg; other-axis span <=max(10 deg,60% of requested excursion))");
        if (!pass) return finish(7,"axis-quality-failed");
    }
    log("INFO","IMU capture OK: finite normalized quaternion, timestamp-based 3DoF; hardware axis/drift profiles remain separate gates");
    return finish(0,"completed");
}
} // namespace psvr
