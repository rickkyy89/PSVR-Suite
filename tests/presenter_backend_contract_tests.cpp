// SPDX-License-Identifier: MIT
// Standalone C++17; no SDK, hardware, runtime, or platform libraries required.
#include "presenter_backend_contract.hpp"
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <type_traits>

namespace {
using namespace psvr::presenter;
void check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
void configuration_tests() {
    static_assert(std::is_signed<decltype(BackendConfig::desktop_x)>::value, "signed X");
    static_assert(std::is_signed<decltype(BackendConfig::desktop_y)>::value, "signed Y");
    BackendConfig c{"exact-monitor-path", 1920, 1080, -1920, -1080, {90000, 1000}};
    check(validate_config(c, "exact-monitor-path") == ConfigStatus::kValid, "negative origins and rational 90");
    c.refresh = {120000, 1000};
    check(validate_config(c, c.monitor_path) == ConfigStatus::kValid, "rational 120");
    c.desktop_x = std::numeric_limits<std::int32_t>::min();
    c.desktop_y = std::numeric_limits<std::int32_t>::max();
    check(validate_config(c, c.monitor_path) == ConfigStatus::kValid, "full signed origin range");
    check(validate_config(c, "Exact-monitor-path") == ConfigStatus::kPathMismatch, "exact case-sensitive path");
    check(validate_config(c, "") == ConfigStatus::kEmptyPath, "empty selected path");
    c.monitor_path.clear();
    check(validate_config(c, "path") == ConfigStatus::kEmptyPath, "empty configured path");
    c.monitor_path = "path";
    c.width = 1919;
    check(validate_config(c, "path") == ConfigStatus::kInvalidDimensions, "bad width");
    c.width = 1920;
    c.height = 1081;
    check(validate_config(c, "path") == ConfigStatus::kInvalidDimensions, "bad height");
    c.height = 1080;
    for (RefreshRate bad : {RefreshRate{0, 0}, {120, 0}, {0, 1}, {60, 1}, {119999, 1000},
                            {std::numeric_limits<std::uint32_t>::max(), 1}}) {
        c.refresh = bad;
        check(validate_config(c, "path") == ConfigStatus::kInvalidRefresh, "invalid rational refresh");
    }
    static_assert(pack_adapter_luid(0x89abcdefu, 0x12345678) == 0x1234567889abcdefull, "both halves");
    static_assert(pack_adapter_luid(0x89abcdefu, -1) == 0xffffffff89abcdefull, "signed high bits");
    static_assert(pack_adapter_luid(0, std::numeric_limits<std::int32_t>::min()) == 0x8000000000000000ull,
                  "minimum signed high");
    check(pack_adapter_luid(1, 2) != pack_adapter_luid(1, 3), "high half matters");
    check(pack_adapter_luid(1, 2) != pack_adapter_luid(2, 2), "low half matters");
}

void timing_tests() {
    using O = ObservationStatus;
    using T = TimingStatus;
    RefreshTimingTrust t(120000, {120, 1});
    check(t.query(0, 1).status == T::kUnknown, "startup unknown without observations");
    check(t.observe({10, 1000, 1100}) == O::kBaseline, "first sample only baseline");
    check(t.query(1100, 1).status == T::kUnknown, "baseline not trusted");
    check(t.observe({11, 2000, 2100}) == O::kAdvanced, "consistent advancing sample");
    auto q = t.query(2600, 0.01);
    check(q && q.refresh_count == 11 && std::fabs(q.seconds_since_last_vsync - 0.005) < 1e-12,
          "measured QPC age and external count");
    check(t.query(3200, 0.01).status == T::kTrusted, "maxAge inclusive boundary");
    check(t.query(3201, 0.01).status == T::kStale, "maxAge stale boundary");
    check(t.observe({11, 2000, 4000}) == O::kDuplicate, "duplicate accepted without age renewal");
    check(t.query(4000, 0.01).status == T::kStale, "poll does not invent vsync");
    check(t.observe({12, 3000, 4100}) == O::kAdvanced, "new external refresh recovers freshness");
    check(t.query(4100, 0.01).status == T::kTrusted, "fresh again");
    check(t.observe({0, 4000, 4200}) == O::kCounterReset, "backward counter reset");
    check(t.query(4200, 1).status == T::kUnknown, "reset revokes trust");
    check(t.observe({1, 5000, 5100}) == O::kBaseline, "reset starts new epoch");
    check(t.observe({2, 6000, 6100}) == O::kAdvanced, "epoch revalidated");
    check(t.observe({3, 7000, 7100, true}) == O::kDisjoint, "explicit disjoint");
    check(t.query(7100, 1).status == T::kUnknown, "disjoint unknown");

    RefreshTimingTrust wrap(120000, {120, 1});
    check(wrap.observe({0xfffffffeu, 1000, 1000}) == O::kBaseline, "wrap baseline");
    check(wrap.observe({1, 4000, 4000}) == O::kAdvanced, "real wrap skips three refreshes");
    check(wrap.query(4000, 0).refresh_count == 0x100000001ull, "wrap extended to 64 bits");
    check(wrap.observe({0, 5000, 5000}) == O::kCounterReset, "small backward step is not wrap");
    RefreshTimingTrust fake_wrap(120000, {120, 1});
    fake_wrap.observe({0xffff0000u, 1000, 1000});
    check(fake_wrap.observe({0, 2000, 2000}) == O::kInconsistentTiming, "reset-like wrap rejected by QPC rate");

    RefreshTimingTrust clock(120000, {120, 1});
    clock.observe({1, 1000, 1100});
    check(clock.observe({2, 999, 1200}) == O::kNonmonotonicQpc, "backward sync QPC");
    clock.observe({1, 1000, 1100});
    check(clock.observe({2, 2000, 1999}) == O::kInvalidParameters, "sync QPC cannot be in future");
    clock.observe({1, 1000, 1100});
    check(clock.observe({2, 1000, 1200}) == O::kNonmonotonicQpc, "changed count with equal sync QPC");
    clock.observe({1, 1000, 1100});
    check(clock.observe({1, 2000, 2100}) == O::kCounterReset, "changed timestamp without refresh");
    clock.observe({1, 1000, 1100});
    clock.observe({2, 2000, 2100});
    clock.query(2200, 1);
    check(clock.query(2199, 1).status == T::kNonmonotonicQpc, "query clock regression");
    check(clock.query(2200, 1).status == T::kUnknown, "query regression revokes trust");
    clock.invalidate();
    clock.observe({1, 1000, 1100});
    clock.query(3000, 1);
    check(clock.observe({2, 2000, 2900}) == O::kNonmonotonicQpc, "observation clock regression");

    RefreshTimingTrust invalid(0, {120, 1});
    check(invalid.observe({1, 0, 0}) == O::kInvalidParameters, "zero QPC frequency");
    RefreshTimingTrust bad_rate(120000, {60, 1});
    check(bad_rate.observe({1, 0, 0}) == O::kInvalidParameters, "unsupported timing rate");
    check(clock.observe({1, -1, 0}) == O::kInvalidParameters, "negative sync QPC");
    for (double age : {-1.0, std::numeric_limits<double>::infinity(), std::numeric_limits<double>::quiet_NaN()})
        check(clock.query(0, age).status == T::kInvalidQuery, "invalid maxAge");
    check(clock.query(-1, 1).status == T::kInvalidQuery, "negative query QPC");
    RefreshTimingTrust ninety(90000, {90000, 1000});
    ninety.observe({0, 0, 0});
    check(ninety.observe({90, 90000, 90000}) == O::kAdvanced, "90Hz one-second observation gap");
    check(ninety.query(90000, 0).refresh_count == 90, "count zero is valid baseline");
    ninety.invalidate();
    check(ninety.query(90000, 1).status == T::kUnknown, "manual output-change invalidation");
    RefreshTimingTrust gap(120000, {120, 1});
    gap.observe({0, 0, 0});
    check(gap.observe({1, 2147483648000ll, 2147483648000ll}) == O::kInconsistentTiming,
          "half-range time gap ambiguous even with plausible modular delta");
    RefreshTimingTrust extremes(120000, {120, 1});
    extremes.observe({0, 0, 0});
    check(extremes.observe({1, std::numeric_limits<std::int64_t>::max(),
                           std::numeric_limits<std::int64_t>::max()}) == O::kInconsistentTiming,
          "maximum QPC handled without signed overflow");
}
} // namespace

int main() {
    try {
        configuration_tests();
        timing_tests();
        std::cout << "PASS presenter backend config, LUID, external refresh timing trust\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
