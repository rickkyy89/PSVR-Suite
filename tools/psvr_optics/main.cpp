// SPDX-License-Identifier: MIT
#include "optical_profile.hpp"
#include "optics_sdk.hpp"
#include <iostream>

int wmain(int argc,wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--help") {
        std::cout << "Usage: psvr_optics --validate <profile.json> [--json]\n"
            "Offline optical profile validation. No USB, display or SteamVR access.\n"
            "Checks schema, radial math and SDK float usability; NOT optical calibration.\n"
            "Exit: 0 valid math/SDK, 1 bad arguments, 3 invalid/unreadable profile, 4 SDK-unusable profile.\n";
        return 0;
    }
    if ((argc != 3 && argc != 4) || std::wstring(argv[1]) != L"--validate" ||
        (argc == 4 && std::wstring(argv[3]) != L"--json")) {
        std::cerr << "Invalid arguments; use --help.\n";
        return 1;
    }
    try {
        const auto profile = psvr::optics::load_profile(std::filesystem::path(argv[2]));
        const auto* sdk_error = psvr::optics::sdk_profile_error(profile.mapper);
        if (argc == 4) {
            const nlohmann::json summary = {
                {"schema_version",1},{"name",profile.name},{"math_valid",true},
                {"sdk_usable",sdk_error == nullptr},{"sdk_error",sdk_error ? nlohmann::json(sdk_error) : nlohmann::json(nullptr)},
                {"optical_acceptance_verified",false},{"mapping","display-to-render"},
                {"uv_convention","eye-local-top-left"}};
            std::cout << summary.dump(2) << '\n';
        } else {
            std::cout << "Profile: " << profile.name << "\nSchema/radial mathematics: PASS\n"
                << "SDK float usability: " << (sdk_error ? sdk_error : "PASS")
                << "\nOptical acceptance: NOT VERIFIED (requires physical calibration/patterns).\n";
        }
        return std::cout ? (sdk_error ? 4 : 0) : 3;
    } catch (const std::exception& error) {
        std::cerr << "Profile validation failed: " << error.what() << '\n';
        return 3;
    }
}
