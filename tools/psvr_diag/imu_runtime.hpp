// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>
#include <functional>
#include <string>
#include <nlohmann/json.hpp>

namespace psvr {
enum class ProgressFailure { none, no_reports, no_advancing_samples };
class ImuProgressWatchdog {
    bool saw_report_ = false;
    std::int64_t last_advance_ms_ = 0;
public:
    void report_received() { saw_report_ = true; }
    void advanced(std::int64_t elapsed_ms) { last_advance_ms_ = elapsed_ms; }
    ProgressFailure failure(std::int64_t elapsed_ms, bool tracking, int tracking_timeout_ms) const {
        if (!saw_report_) return elapsed_ms > 3000 ? ProgressFailure::no_reports : ProgressFailure::none;
        const auto limit = tracking ? tracking_timeout_ms : 1000;
        return elapsed_ms-last_advance_ms_ > limit ? ProgressFailure::no_advancing_samples : ProgressFailure::none;
    }
};

// All capture exits attempt exactly one terminal invalidation. A broken log/
// output sink cannot guarantee delivery; its exception must not mask the original.
class CaptureTerminal {
    bool enabled_, finalized_ = false;
    const std::function<void(const char*,const std::string&)>& log_;
public:
    CaptureTerminal(bool enabled, const std::function<void(const char*,const std::string&)>& log)
        : enabled_(enabled), log_(log) {}
    void end(const char* reason) {
        if (finalized_) return;
        finalized_ = true;
        if (enabled_) log_("DATA",nlohmann::json{{"schema_version",1},{"event","imu_state"},{"state","stopped"},
            {"valid",false},{"reason",reason}}.dump());
    }
    ~CaptureTerminal() noexcept {
        if (!finalized_) { try { end("capture-exception"); } catch (...) {} }
    }
};
} // namespace psvr
