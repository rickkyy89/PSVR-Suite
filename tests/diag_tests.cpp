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
        const auto packet = psvr::decode_sensor(bytes.data(), bytes.size());
        check(packet.has_value(), "valid packet rejected");
        check(packet->samples[0].tick == 0xfffffe && packet->samples[1].tick == 498, "timestamps/endian wrong");
        check(packet->samples[0].gyro[0] == -32768 && packet->samples[0].gyro[1] == 32767 &&
            packet->samples[0].gyro[2] == -1, "signed gyro wrong");
        check(packet->samples[0].accel[0] == 0x1234 && packet->samples[1].accel[0] == -16384, "sample/accel layout wrong");
        check(packet->sequence == 0xa5, "sequence wrong");
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
        std::cout << "PASS decoder: binary layout, signed values, malformed packets\n"
                  << "PASS selection: absent, incomplete, normal, ambiguous\n"
                  << "PASS config: ranges, type, overflow, unknown fields\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL " << e.what() << '\n';
        return 1;
    }
}
