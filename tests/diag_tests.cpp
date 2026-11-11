// SPDX-License-Identifier: MIT
#include "diag_core.hpp"
#include "sensor_packet.hpp"
#include <array>
#include <iostream>

namespace {
void check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
template<class Function> void rejects(Function f, const char* message) {
    bool rejected = false;
    try { f(); } catch (const std::exception&) { rejected = true; }
    check(rejected, message);
}
}
int main() {
    try {
        // Fixed protocol fixture: little endian timestamps, signed extrema,
        // distinct samples, and seq. No alignment/struct packing assumptions.
        std::array<unsigned char, 64> bytes{};
        bytes[16] = 0xfe; bytes[17] = 0xff; bytes[18] = 0xff;
        bytes[32] = 0xf2; bytes[33] = 0x01;
        bytes[20] = 0x00; bytes[21] = 0x80;
        bytes[22] = 0xff; bytes[23] = 0x7f;
        bytes[24] = 0xff; bytes[25] = 0xff;
        bytes[26] = 0x34; bytes[27] = 0x12;
        bytes[42] = 0x00; bytes[43] = 0xc0;
        bytes[63] = 0xa5;
        bytes[8] = 0x91; bytes[55] = 0xff; bytes[56] = 3;
        const auto packet = psvr::decode_sensor(bytes.data(), bytes.size());
        check(packet.has_value(), "valid packet rejected");
        check(packet->samples[0].tick == 0xfffffe && packet->samples[1].tick == 498, "timestamps/endian wrong");
        check(packet->samples[0].gyro[0] == -32768 && packet->samples[0].gyro[1] == 32767 &&
            packet->samples[0].gyro[2] == -1, "signed gyro wrong");
        check(packet->samples[0].accel[0] == 0x1234 && packet->samples[1].accel[0] == -16384, "sample/accel layout wrong");
        check(packet->sequence == 0xa5, "sequence wrong");
        check(packet->worn() && packet->status_raw == 0x91 && packet->ir_raw == 1023,"sensor presence/raw IR offsets wrong");
        // MI_04 headset input (read-only): byte0 buttons, byte2 volume, byte8 flags.
        // Untouched fixture above has no buttons pressed: defaults must be neutral.
        check(packet->buttons_raw == 0 && packet->volume_raw == 0, "default input bytes not zero");
        check(packet->headset_flags_raw == 0x91, "headset flags must mirror byte8");
        check(!packet->volume_up() && !packet->volume_down() && !packet->mic_mute_pressed(), "phantom button press");
        check(!packet->mic_muted() && packet->headphones_present(), "byte8 bit3/bit4 helpers wrong for 0x91");
        // Synthetic frames: each button bit in isolation, then combined.
        for (const auto bit : {0x02, 0x04, 0x08}) {
            auto frame = bytes;
            frame[0] = static_cast<unsigned char>(bit);
            const auto pressed = psvr::decode_sensor(frame.data(), frame.size());
            check(pressed.has_value(), "button frame rejected");
            check(pressed->volume_up() == (bit == 0x02), "volume_up bitmask wrong");
            check(pressed->volume_down() == (bit == 0x04), "volume_down bitmask wrong");
            check(pressed->mic_mute_pressed() == (bit == 0x08), "mic_mute bitmask wrong");
        }
        {
            auto frame = bytes;
            frame[0] = 0x0e; frame[2] = 0x2a; frame[8] = 0x19;
            const auto pressed = psvr::decode_sensor(frame.data(), frame.size());
            check(pressed.has_value(), "combined input frame rejected");
            check(pressed->buttons_raw == 0x0e && pressed->volume_raw == 0x2a, "input raw bytes wrong");
            check(pressed->headset_flags_raw == 0x19 && pressed->status_raw == 0x19, "flags/status raw diverged");
            check(pressed->volume_up() && pressed->volume_down() && pressed->mic_mute_pressed(), "combined buttons wrong");
            check(pressed->worn() && pressed->mic_muted() && pressed->headphones_present(), "combined byte8 flags wrong");
            frame[8] = 0x00;
            const auto bare = psvr::decode_sensor(frame.data(), frame.size());
            check(bare && !bare->worn() && !bare->mic_muted() && !bare->headphones_present(), "cleared byte8 still flagged");
        }
        // IMU regression: identical IMU/seq/IR bytes with input bytes set must
        // decode to identical samples/sequence/IR (offsets untouched).
        {
            auto frame = bytes;
            frame[0] = 0x0e; frame[2] = 0x2a;
            const auto with_input = psvr::decode_sensor(frame.data(), frame.size());
            check(with_input.has_value(), "regression frame rejected");
            for (std::size_t s = 0; s < 2; ++s) {
                check(with_input->samples[s].tick == packet->samples[s].tick, "tick offset shifted by input bytes");
                for (std::size_t a = 0; a < 3; ++a)
                    check(with_input->samples[s].gyro[a] == packet->samples[s].gyro[a] &&
                        with_input->samples[s].accel[a] == packet->samples[s].accel[a], "IMU axis offset shifted by input bytes");
            }
            check(with_input->sequence == packet->sequence && with_input->ir_raw == packet->ir_raw, "seq/IR offset shifted by input bytes");
        }
        for (const auto size : {0u, 1u, 63u, 65u, 512u})
            check(!psvr::decode_sensor(bytes.data(), size), "malformed packet accepted");
        check(!psvr::decode_sensor(nullptr, 64), "null packet accepted");
        check(psvr::select_interfaces({}) == psvr::Selection::absent, "absent selection wrong");
        check(psvr::select_interfaces({8, 8, 8}) == psvr::Selection::incomplete, "auxiliary HID treated as sensor");
        check(psvr::select_interfaces({4}) == psvr::Selection::incomplete, "control missing not handled");
        check(psvr::select_interfaces({4, 5, 8, 8, 8}) == psvr::Selection::ready, "normal PSVR collections rejected");
        check(psvr::select_interfaces({4, 4, 5}) == psvr::Selection::ambiguous, "multiple sensors accepted");
        check(psvr::select_interfaces({4, 5, 5}) == psvr::Selection::ambiguous, "multiple controls accepted");
        auto j = nlohmann::json::parse(R"({"schema_version":1,"diagnostic":{"duration_ms":5000}})");
        check(psvr::config_options(j).duration_ms == 5000, "config not applied");
        j["diagnostic"]["duration_ms"] = 1.5;
        rejects([&] { psvr::config_options(j); }, "fractional duration accepted");
        j["diagnostic"]["duration_ms"] = 18446744073709551615ull;
        rejects([&] { psvr::config_options(j); }, "overflow accepted");
        j["diagnostic"]["duration_ms"] = -1;
        rejects([&] { psvr::config_options(j); }, "negative duration accepted");
        j["diagnostic"]["duration_ms"] = 2000;
        j["diagnostic"]["power_on"] = true;
        rejects([&] { psvr::config_options(j); }, "unknown config accepted");
        rejects([] { psvr::bounded_integer("100ms", 100, 60000); }, "CLI junk accepted");
        rejects([] { psvr::bounded_integer("-100", 100, 60000); }, "CLI negative accepted");
        const std::vector<unsigned char> power_on{0x17, 0, 0xaa, 4, 1, 0, 0, 0};
        const std::vector<unsigned char> power_off{0x17, 0, 0xaa, 4, 0, 0, 0, 0};
        const std::vector<unsigned char> vr{0x23, 0, 0xaa, 4, 1, 0, 0, 0};
        const std::vector<unsigned char> cinema{0x23, 0, 0xaa, 4, 0, 0, 0, 0};
        check(psvr::command_packet(psvr::Action::power_on) == power_on, "wrong power-on wire packet");
        check(psvr::command_packet(psvr::Action::power_off) == power_off, "wrong power-off wire packet");
        check(psvr::command_packet(psvr::Action::vr_mode) == vr, "wrong VR wire packet");
        check(psvr::command_packet(psvr::Action::cinema_mode) == cinema, "wrong cinema wire packet");
        const std::vector<unsigned char> leds{0x15, 0, 0xaa, 16, 0xff, 1,
            100, 100, 100, 100, 100, 100, 100, 100, 100, 0, 0, 0, 0, 0};
        check(psvr::command_packet(psvr::Action::leds_on) == leds, "wrong LED mask/intensity/reserved bytes");
        check(psvr::parse_action("--leds-on") == psvr::Action::leds_on, "LED action not parsed");
        check(!psvr::status_matches(psvr::Action::leds_on, psvr::ControlStatus{1,1}),
              "F0 cannot verify LED brightness");
        rejects([] { psvr::command_packet(psvr::Action::status); }, "status action produces write");
        rejects([] { psvr::command_packet(static_cast<psvr::Action>(999)); }, "unknown action produces write");
        std::array<unsigned char, 64> f0{};
        f0[0] = 0xf0; f0[2] = 0xaa; f0[3] = 16; f0[4] = 0x05;
        const auto state = psvr::decode_status(f0.data(), 20);
        check(state && state->powered() && state->cinematic() && !state->worn(), "F0 flags wrong");
        check(psvr::status_matches(psvr::Action::cinema_mode, *state) &&
              !psvr::status_matches(psvr::Action::vr_mode, *state), "mode verification wrong");
        f0[4] = 0;
        const auto off = psvr::decode_status(f0.data(), 64);
        check(off && psvr::status_matches(psvr::Action::power_off, *off) &&
              !psvr::status_matches(psvr::Action::vr_mode, *off), "powered-off is falsely VR verified");
        check(!psvr::decode_status(nullptr, 64) && !psvr::decode_status(f0.data(), 19), "short status accepted");
        f0[4] = 1; f0[11] = 1;
        check(psvr::status_matches(psvr::Action::vr_mode, *psvr::decode_status(f0.data(), 20)), "active VR not verified");
        f0[11] = 3;
        check(!psvr::status_matches(psvr::Action::vr_mode, *psvr::decode_status(f0.data(), 20)), "unknown VR state verified");
        f0[2] = 0;
        check(!psvr::decode_status(f0.data(), 64), "bad magic accepted");
        f0[2] = 0xaa; f0[3] = 60;
        check(!psvr::decode_status(f0.data(), 64), "wrong F0 length accepted");
        const std::array<unsigned char, 20> hardware_status{{0xf0,0,0xaa,0x10,0x55,0x22,0,0,0,0xff,0,0,2,0,0,0,0,0,0,0}};
        const auto actual = psvr::decode_status(hardware_status.data(), hardware_status.size());
        check(actual && actual->powered() && actual->cinematic() && actual->vr_active == 0, "real F0 capture not parsed correctly");
        check(!psvr::action_verified(psvr::Action::cinema_mode, actual, false), "matching F0 alone verified command without ACK");
        check(!psvr::action_verified(psvr::Action::cinema_mode, std::nullopt, true), "ACK alone falsely verified state");
        check(psvr::action_verified(psvr::Action::cinema_mode, actual, true), "ACK + state rejected");
        const std::array<unsigned char, 8> ack{{0xa0,0,0xaa,4,0x17,0,0,0}};
        const auto response = psvr::decode_response(ack.data(), ack.size());
        check(response && response->command_id == 0x17 && response->result_code == 0, "A0 response wrong");
        check(!psvr::decode_response(ack.data(), 5), "short A0 accepted");
        check(!psvr::parse_action("--flash-firmware"), "unknown command recognized");
        auto cfg2 = nlohmann::json::parse(R"({"schema_version":2,"diagnostic":{},"tracking":{"calibration_ms":3000,"recenter_mode":"yaw"}})");
        check(psvr::config_options(cfg2).tracking.calibration_ms == 3000,"tracking config not applied");
        cfg2["tracking"]["smoothing_ms"] = -1;
        rejects([&] { psvr::config_options(cfg2); },"negative smoothing accepted");
        psvr::Options continuous; continuous.imu = true; continuous.duration_ms = 0;
        psvr::validate_options(continuous);
        continuous.stationary_test = true;
        rejects([&] { psvr::validate_options(continuous); },"unbounded measurement profile accepted");
        std::cout << "PASS decoder: binary layout, signed values, malformed packets\n"
                  << "PASS selection: absent, incomplete, normal, ambiguous\n"
                  << "PASS config: ranges, type, overflow, unknown fields\n"
                  << "PASS control: documented packets, F0 envelope, power/mode verification\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL " << e.what() << '\n';
        return 1;
    }
}
