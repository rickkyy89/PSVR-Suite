// SPDX-License-Identifier: MIT
// Independent implementation of documented PSVR wire values. References:
// OpenHMD drv_psvr/psvr.h (85075b0) and alatnet/libpsvr ReportID.h,
// Control_Frame.h, Device_Status.h (7858e9c); PSVRFramework wiki Report-0xF0
// and PSVR.cs (ccda7ca) for the full 16-byte status payload. No unknown commands.
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace psvr {
enum class Action { none, control_info, status, power_on, power_off, vr_mode, cinema_mode, leds_on };
inline const char* action_name(Action action) {
    switch (action) {
    case Action::none: return "passive";
    case Action::control_info: return "control-info";
    case Action::status: return "status";
    case Action::power_on: return "power-on";
    case Action::power_off: return "power-off";
    case Action::vr_mode: return "vr-mode";
    case Action::cinema_mode: return "cinema-mode";
    case Action::leds_on: return "leds-on";
    }
    throw std::runtime_error("invalid action");
}
inline std::optional<Action> parse_action(const std::string& flag) {
    if (flag == "--control-info") return Action::control_info;
    if (flag == "--status") return Action::status;
    if (flag == "--power-on") return Action::power_on;
    if (flag == "--power-off") return Action::power_off;
    if (flag == "--vr-mode") return Action::vr_mode;
    if (flag == "--cinema-mode") return Action::cinema_mode;
    if (flag == "--leds-on") return Action::leds_on;
    return std::nullopt;
}
inline bool writes_command(Action a) {
    return a == Action::power_on || a == Action::power_off || a == Action::vr_mode || a == Action::cinema_mode || a == Action::leds_on;
}
inline std::vector<unsigned char> command_packet(Action action) {
    if (!writes_command(action)) throw std::runtime_error("action has no documented command packet");
    if (action == Action::leds_on) {
        // PSVRFramework Report-0x15---LED-Brightness: LE16 mask, nine
        // intensities 0..100, five reserved bytes. Explicitly select all LEDs.
        std::vector<unsigned char> packet(20, 0);
        packet[0] = 0x15; packet[2] = 0xaa; packet[3] = 16;
        packet[4] = 0xff; packet[5] = 0x01;
        for (std::size_t i = 6; i < 15; ++i) packet[i] = 100;
        return packet;
    }
    const bool power = action == Action::power_on || action == Action::power_off;
    const bool enabled = action == Action::power_on || action == Action::vr_mode;
    // libpsvr gp_id=0; full Sony header + LE32 boolean payload, no HID prefix.
    return {static_cast<unsigned char>(power ? 0x17 : 0x23), 0x00, 0xaa, 0x04,
             static_cast<unsigned char>(enabled ? 1 : 0), 0x00, 0x00, 0x00};
}
struct ControlStatus {
    std::uint8_t mask;
    std::uint8_t vr_active;
    bool powered() const { return (mask & 0x01) != 0; }
    bool worn() const { return (mask & 0x02) != 0; }
    bool cinematic() const { return (mask & 0x04) != 0; }
};
inline std::optional<ControlStatus> decode_status(const unsigned char* bytes, std::size_t size) {
    // F0 has 16 payload bytes (20-byte frame, optionally USB-padded to 64).
    // libpsvr's status struct only describes the first 8 payload bytes.
    if (!bytes || size < 20 || size > 64 || bytes[0] != 0xf0 || bytes[1] != 0 || bytes[2] != 0xaa || bytes[3] != 16)
        return std::nullopt;
    return ControlStatus{bytes[4], bytes[11]};
}
struct CommandResponse { std::uint8_t command_id, result_code; };
inline std::optional<CommandResponse> decode_response(const unsigned char* b, std::size_t size) {
    if (!b || size < 6 || size > 64 || b[0] != 0xa0 || b[1] != 0 || b[2] != 0xaa ||
        b[3] < 2 || b[3] > 60 || size < std::size_t(b[3]) + 4) return std::nullopt;
    return CommandResponse{b[4], b[5]};
}
inline bool status_matches(Action action, const ControlStatus& status) {
    switch (action) {
    case Action::power_on: return status.powered();
    case Action::power_off: return !status.powered();
    case Action::vr_mode: return status.powered() && !status.cinematic() && status.vr_active == 1;
    case Action::cinema_mode: return status.powered() && status.cinematic() && status.vr_active == 0;
    default: return false;
    }
}
inline bool action_verified(Action action, const std::optional<ControlStatus>& fresh_status, bool acknowledged) {
    return acknowledged && fresh_status && status_matches(action, *fresh_status);
}
} // namespace psvr
