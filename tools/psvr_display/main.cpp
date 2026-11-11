// SPDX-License-Identifier: MIT
#include "display_inventory.hpp"
#include <nlohmann/json.hpp>
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    bool json = false, help = false;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--json" && !json) json = true;
        else if (arg == "--help" && !help) help = true;
        else {
            std::cerr << "Invalid argument: " << arg << "\nUse --help.\n";
            return 1;
        }
    }
    if (help && json) {
        std::cerr << "--help and --json cannot be combined.\n";
        return 1;
    }
    if (help) {
        std::cout << "Usage: psvr_display [--json | --help]\n"
            "Read-only active Windows display inventory. No writes or automatic selection.\n"
            "--json  Emit schema-versioned JSON to stdout.\n"
            "--help  Show help without enumerating displays.\n"
            "Exit codes: 0 success, 1 invalid arguments, 3 enumeration/output failure.\n";
        return std::cout ? 0 : 3;
    }
    try {
        const auto displays = psvr::display::enumerate_displays();
        if (json) std::cout << psvr::display::inventory_json(displays).dump(2) << '\n';
        else {
            std::cout << "Read-only active display inventory: " << displays.size() << " path(s)\n";
            for (const auto& d : displays) {
                std::cout << d.friendly_name.value_or("<name unavailable>") << '\n'
                    << "  monitorDevicePath: " << d.monitor_device_path.value_or("<unavailable>") << '\n'
                    << "  GDI source: " << d.source_gdi_name.value_or("<unavailable>") << '\n';
                if (d.source) std::cout << "  desktop: " << d.source->width << 'x' << d.source->height
                    << " at (" << d.source->x << ',' << d.source->y << ")\n";
                if (d.target) std::cout << "  scanout: " << d.target->width << 'x' << d.target->height << '\n';
                std::cout << "  refresh: " << d.refresh.numerator << '/' << d.refresh.denominator
                    << " Hz; rotation=" << d.rotation << "; scanline=" << d.scanline_ordering << '\n';
                for (const auto& warning : d.warnings) std::cout << "  warning: " << warning << '\n';
            }
            std::cout << "Use --json for all metadata and available modes. Nothing selected or changed.\n";
        }
        return std::cout ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "Display inventory failed: " << error.what() << '\n';
        return 3;
    }
}
