// Copyright 2016, Joey Ferwerda.
// Copyright 2026, PSVR-PC contributors.
// SPDX-License-Identifier: BSL-1.0
// Adapted from OpenHMD src/drv_psvr/packet.c and psvr.h, commit
// 85075b0c7e3c723ded2577edb79d00ee11aac339. See third_party/BSL-1.0.txt.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

namespace psvr {
struct SensorSample {
    std::uint32_t tick;
    std::array<std::int16_t, 3> gyro;
    std::array<std::int16_t, 3> accel;
};
struct SensorPacket {
    std::array<SensorSample, 2> samples;
    std::uint8_t sequence;
    std::uint8_t status_raw;
    std::uint16_t ir_raw;
    // MI_04 headset-input bytes (read-only parsing, never a USB command):
    // byte0 = button bitmask, byte2 = volume level, byte8 = headset flags.
    // Ground truth PSMoveService MorpheusHMD.cpp: byte0 VolumePlus=0x02,
    // VolumeMinus=0x04, MicrophoneMute=0x08; byte8 bit0=worn, bit3=mic muted,
    // bit4=headphones present. headset_flags_raw mirrors status_raw (byte8);
    // kept as a named alias so call sites read intent, not offsets.
    std::uint8_t buttons_raw = 0;
    std::uint8_t volume_raw = 0;
    std::uint8_t headset_flags_raw = 0;
    bool worn() const { return (status_raw & 1) != 0; }
    bool volume_up() const { return (buttons_raw & 0x02) != 0; }
    bool volume_down() const { return (buttons_raw & 0x04) != 0; }
    bool mic_mute_pressed() const { return (buttons_raw & 0x08) != 0; }
    bool mic_muted() const { return (headset_flags_raw & 0x08) != 0; }
    bool headphones_present() const { return (headset_flags_raw & 0x10) != 0; }
};

inline std::uint32_t le32(const unsigned char* b) {
    return std::uint32_t(b[0]) | (std::uint32_t(b[1]) << 8) |
        (std::uint32_t(b[2]) << 16) | (std::uint32_t(b[3]) << 24);
}
inline std::int16_t le_signed16(const unsigned char* b) {
    const int value = int(b[0]) | (int(b[1]) << 8);
    return static_cast<std::int16_t>(value >= 32768 ? value - 65536 : value);
}

inline std::optional<SensorPacket> decode_sensor(const unsigned char* b, std::size_t length) {
    if (b == nullptr || length != 64) return std::nullopt;
    SensorPacket result{};
    for (std::size_t sample = 0; sample < 2; ++sample) {
        const auto offset = 16 + sample * 16;
        result.samples[sample].tick = le32(b + offset);
        for (std::size_t axis = 0; axis < 3; ++axis) {
            result.samples[sample].gyro[axis] = le_signed16(b + offset + 4 + axis * 2);
            result.samples[sample].accel[axis] = le_signed16(b + offset + 10 + axis * 2);
        }
    }
    result.sequence = b[63];
    // PSVRFramework PSVR.cs ccda7ca: worn = byte8 bit0, raw IR = LE16 at 55.
    // Byte8 bit1 display semantics conflict between references; expose raw only.
    // IMU framing (samples at 16/32, seq at 63, IR at 55) is live-validated:
    // do NOT shift it; the headset-input bytes below are parsed independently.
    result.status_raw = b[8];
    result.ir_raw = static_cast<std::uint16_t>(unsigned(b[55]) | (unsigned(b[56]) << 8));
    // Read-only headset input (PSMoveService MorpheusHMD.cpp ground truth).
    // Live gate: which bits actually toggle on press needs the user at return.
    result.buttons_raw = b[0];
    result.volume_raw = b[2];
    result.headset_flags_raw = b[8];
    return result;
}
} // namespace psvr
