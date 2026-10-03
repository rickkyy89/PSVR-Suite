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
    return result;
}
} // namespace psvr
