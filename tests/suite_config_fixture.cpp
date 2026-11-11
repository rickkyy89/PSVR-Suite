// SPDX-License-Identifier: MIT
// Pure native parser fixture: stdin JSON only, no Windows/device APIs.
#include "diag_core.hpp"
#include <iostream>

int main() {
    try {
        nlohmann::json payload;
        std::cin >> payload;
        const auto options = psvr::config_options(payload);
        std::cout << nlohmann::json{
            {"calibration_ms", options.tracking.calibration_ms},
            {"stale_timeout_ms", options.tracking.stale_timeout_ms},
            {"max_sample_gap_us", options.tracking.max_sample_gap_us}}
                         .dump() << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
