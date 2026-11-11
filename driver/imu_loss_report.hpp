// SPDX-License-Identifier: MIT
#pragma once
#include "pose_bridge.hpp"
#include <nlohmann/json.hpp>

namespace psvr {
struct ImuReadDiagnostics {
    std::uint64_t reads = 0, empty_reads = 0;
    int last_bytes = 0;
    double last_read_ms = 0, max_read_ms = 0, last_loop_interval_ms = 0;
};

// Called only on a terminal capture fault; never serializes each sensor packet.
inline nlohmann::json imu_loss_report(const DriverTracker& tracker, const ImuConfig& config,
                                     DriverClock::time_point now, const ImuReadDiagnostics& read,
                                     const char* override_reason = nullptr) {
    const auto d = tracker.diagnostics();
    const auto snapshot = tracker.snapshot();
    const bool calibration_failed = snapshot.state == ImuState::calibration_failed;
    const bool failure_recorded = std::strcmp(d.reason,"none") != 0;
    return {{"event","imu_capture_fault"},{"schema_version",1},
        {"reason",override_reason ? override_reason : (calibration_failed ? "calibration_failed" : d.reason)},
        {"previous_state",state_name(override_reason ? snapshot.state : d.previous_state)}, {"state",state_name(snapshot.state)},
        {"failure_elapsed_ms",failure_recorded ? nlohmann::json(d.failure_elapsed_ms) : nlohmann::json(nullptr)},
        {"failure_progress_age_ms",failure_recorded ? nlohmann::json(d.failure_progress_age_ms) : nlohmann::json(nullptr)},
        {"progress_age_ms",tracker.progress_age_ms(now)}, {"failure_timeout_ms",d.failure_timeout_ms},
        {"last_sample_age_ms",snapshot.has_sample ? nlohmann::json(std::chrono::duration<double,std::milli>(now-snapshot.sampled_at).count()) : nlohmann::json(nullptr)},
        {"stale_timeout_ms",config.stale_timeout_ms},{"max_sample_gap_us",config.max_sample_gap_us},
        {"hid_timeout_ms",10},{"hid_reads",read.reads},{"hid_empty_reads",read.empty_reads},
        {"hid_last_bytes",read.last_bytes},{"hid_last_read_ms",read.last_read_ms},
        {"hid_max_read_ms",read.max_read_ms},{"reader_last_loop_interval_ms",read.last_loop_interval_ms},
        {"reports_received",d.reports_received},{"reports_decoded",d.reports_decoded},
        {"reports_malformed",d.reports_malformed},{"reports_duplicate",d.reports_duplicate},
        {"reports_blocked_by_watchdog",d.reports_blocked_by_watchdog},
        {"last_report_sequence",d.have_report ? nlohmann::json(d.last_sequence) : nlohmann::json(nullptr)},
        {"last_report_tick",d.have_report ? nlohmann::json(d.last_report_tick) : nlohmann::json(nullptr)},
        {"previous_sample_tick",d.have_tick_delta ? nlohmann::json(d.previous_tick) : nlohmann::json(nullptr)},
        {"sample_tick",d.have_tick_delta ? nlohmann::json(d.sample_tick) : nlohmann::json(nullptr)},
        {"sample_tick_delta_us",d.have_tick_delta ? nlohmann::json(d.tick_delta_us) : nlohmann::json(nullptr)},
        {"samples_seen",d.samples_seen},{"samples_advanced",d.samples_advanced},
        {"samples_rejected",d.samples_rejected},{"sample_duplicates",d.sample_duplicates},
        {"sample_gaps",d.sample_gaps},{"calibration_error",tracker.calibration_error()},
        {"interpretation","Observed software fault, not proof of physical USB disconnect. HID timings use the host steady clock; tick deltas use the sensor 24-bit clock."}};
}
} // namespace psvr
