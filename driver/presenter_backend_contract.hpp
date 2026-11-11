// SPDX-License-Identifier: MIT
#pragma once
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>

namespace psvr {
namespace presenter {

struct RefreshRate {
    std::uint32_t numerator = 0;
    std::uint32_t denominator = 0;
};
constexpr bool supported_refresh(RefreshRate rate) noexcept {
    return rate.denominator != 0 &&
        (std::uint64_t{rate.numerator} == 90ull * rate.denominator ||
         std::uint64_t{rate.numerator} == 120ull * rate.denominator);
}

struct BackendConfig {
    std::string monitor_path;
    std::uint32_t width = 1920;
    std::uint32_t height = 1080;
    std::int32_t desktop_x = 0;
    std::int32_t desktop_y = 0;
    RefreshRate refresh;
};
enum class ConfigStatus { kValid, kEmptyPath, kPathMismatch, kInvalidDimensions, kInvalidRefresh };
// selected_monitor_path must come from the caller's exact selected output,
// not an ordinal, size match, primary-display fallback, or EDID guess.
inline ConfigStatus validate_config(const BackendConfig& config,
                                    std::string_view selected_monitor_path) noexcept {
    if (config.monitor_path.empty() || selected_monitor_path.empty()) return ConfigStatus::kEmptyPath;
    if (config.monitor_path != selected_monitor_path) return ConfigStatus::kPathMismatch;
    if (config.width != 1920 || config.height != 1080) return ConfigStatus::kInvalidDimensions;
    if (!supported_refresh(config.refresh)) return ConfigStatus::kInvalidRefresh;
    return ConfigStatus::kValid;
}

// Arithmetic preserves the signed HighPart's bit pattern without pointer
// punning, alignment assumptions, host byte order, or sign extension.
constexpr std::uint64_t pack_adapter_luid(std::uint32_t low, std::int32_t high) noexcept {
    return (std::uint64_t{static_cast<std::uint32_t>(high)} << 32) | low;
}

struct RefreshObservation {
    std::uint32_t sync_refresh_count = 0; // DXGI SyncRefreshCount, NOT PresentCount
    std::int64_t sync_qpc = 0;            // DXGI SyncQPCTime
    std::int64_t observed_qpc = 0;        // QPC sampled after obtaining statistics
    bool disjoint = false;               // also set for output/device/clock changes
};
enum class ObservationStatus {
    kBaseline, kAdvanced, kDuplicate, kInvalidParameters, kDisjoint,
    kNonmonotonicQpc, kCounterReset, kInconsistentTiming, kCounterOverflow
};
enum class TimingStatus { kUnknown, kTrusted, kStale, kInvalidQuery, kNonmonotonicQpc };
struct TimingQuery {
    TimingStatus status = TimingStatus::kUnknown;
    double seconds_since_last_vsync = 0.0;
    std::uint64_t refresh_count = 0;
    explicit operator bool() const noexcept { return status == TimingStatus::kTrusted; }
};

// Single-owner metadata helper; caller provides synchronization. No submit,
// wait, compositor frame ID, or estimated timer can establish scanout timing.
// First sample is only a baseline. A consistent advancing pair earns trust.
// Rejection discards the epoch: recovery needs a new baseline + advancement.
// Query age is measured from SyncQPCTime, never from the latest poll.
class RefreshTimingTrust {
public:
    RefreshTimingTrust(std::int64_t qpc_frequency, RefreshRate refresh) noexcept
        : frequency_(qpc_frequency), refresh_(refresh) {}

    void invalidate() noexcept {
        baseline_ = false;
        trusted_ = false;
        clock_known_ = false;
        extended_ = 0;
    }

    ObservationStatus observe(const RefreshObservation& sample) noexcept {
        if (sample.disjoint) return reject(ObservationStatus::kDisjoint);
        if (frequency_ <= 0 || !supported_refresh(refresh_) ||
            sample.sync_qpc < 0 || sample.observed_qpc < sample.sync_qpc)
            return reject(ObservationStatus::kInvalidParameters);
        if (clock_known_ && sample.observed_qpc < last_clock_)
            return reject(ObservationStatus::kNonmonotonicQpc);
        last_clock_ = sample.observed_qpc;
        clock_known_ = true;
        if (!baseline_) {
            last_ = sample;
            extended_ = sample.sync_refresh_count;
            baseline_ = true;
            return ObservationStatus::kBaseline;
        }
        if (sample.sync_qpc < last_.sync_qpc)
            return reject(ObservationStatus::kNonmonotonicQpc);
        if (sample.sync_refresh_count == last_.sync_refresh_count && sample.sync_qpc == last_.sync_qpc)
            return ObservationStatus::kDuplicate; // does not refresh the age
        if (sample.sync_qpc == last_.sync_qpc)
            return reject(ObservationStatus::kNonmonotonicQpc);

        // Unsigned modular subtraction extends a genuine 32-bit wrap. The
        // half-range bound forbids backward resets and ambiguous long gaps.
        // Rate/QPC consistency additionally rejects reset-like modular jumps.
        const std::uint32_t delta = sample.sync_refresh_count - last_.sync_refresh_count;
        if (delta == 0 || delta >= 0x80000000u)
            return reject(ObservationStatus::kCounterReset);
        const long double elapsed = static_cast<long double>(sample.sync_qpc - last_.sync_qpc) / frequency_;
        const long double expected = elapsed * refresh_.numerator / refresh_.denominator;
        // Two refresh periods allow quantization/jitter, not arbitrary jumps.
        if (expected >= 2147483648.0L || std::fabs(expected - delta) > 2.0L)
            return reject(ObservationStatus::kInconsistentTiming);
        if (extended_ > std::numeric_limits<std::uint64_t>::max() - delta)
            return reject(ObservationStatus::kCounterOverflow);
        extended_ += delta;
        last_ = sample;
        trusted_ = true;
        return ObservationStatus::kAdvanced;
    }

    TimingQuery query(std::int64_t now_qpc, double max_age_seconds) noexcept {
        if (now_qpc < 0 || !std::isfinite(max_age_seconds) || max_age_seconds < 0)
            return {TimingStatus::kInvalidQuery, 0, 0};
        if (clock_known_ && now_qpc < last_clock_) {
            invalidate();
            return {TimingStatus::kNonmonotonicQpc, 0, 0};
        }
        last_clock_ = now_qpc;
        clock_known_ = true;
        if (!trusted_) return {};
        const long double age = static_cast<long double>(now_qpc - last_.sync_qpc) / frequency_;
        if (age > static_cast<long double>(max_age_seconds))
            return {TimingStatus::kStale, static_cast<double>(age), extended_};
        return {TimingStatus::kTrusted, static_cast<double>(age), extended_};
    }

private:
    ObservationStatus reject(ObservationStatus status) noexcept {
        invalidate();
        return status;
    }
    std::int64_t frequency_;
    RefreshRate refresh_;
    RefreshObservation last_{};
    std::int64_t last_clock_ = 0;
    std::uint64_t extended_ = 0;
    bool clock_known_ = false;
    bool baseline_ = false;
    bool trusted_ = false;
};

// No finite sequence can distinguish a reset that exactly mimics a real
// wrap/rate from that wrap. The caller MUST flag disjoint statistics and
// invalidate on output/device changes. Count is epoch-local, not lifetime
// scanouts; polling gaps of >= half the 32-bit range are intentionally rejected.
} // namespace presenter
} // namespace psvr
