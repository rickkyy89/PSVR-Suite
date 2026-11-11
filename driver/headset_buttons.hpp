// SPDX-License-Identifier: MIT
#pragma once

// Headset button edge detection for the PSVR1 sensor report.
//
// Byte 0 of the 64-byte MI_04 HID report carries a button bitmask documented in
// docs/HEADSET_BUTTONS_20261018.md (source: PSMoveService MorpheusHMD.cpp):
//   VolumePlus = 2, VolumeMinus = 4, MicrophoneMute = 8.
// The mask constants below intentionally reuse that exact bit semantics; do not
// redefine divergent values. Live arrival of these bits is still unvalidated
// (the PU may consume them internally), so all of this stays fail-closed.
//
// This header is pure logic with no OS dependencies so the edge/debounce
// behaviour is unit-testable offline with synthetic timestamps. The Core Audio
// side lives behind IHeadsetAudioControl (driver/headset_audio.hpp) and is
// mocked in tests; tests never touch real audio endpoints.

#include <cstdint>
#include <vector>

namespace psvr {

// Sensor report byte 0 button bits (see docs/HEADSET_BUTTONS_20261018.md).
constexpr std::uint8_t kHeadsetVolumeUpMask = 0x02;
constexpr std::uint8_t kHeadsetVolumeDownMask = 0x04;
constexpr std::uint8_t kHeadsetMuteMask = 0x08;
constexpr std::uint8_t kHeadsetButtonMaskAll =
    static_cast<std::uint8_t>(kHeadsetVolumeUpMask | kHeadsetVolumeDownMask | kHeadsetMuteMask);

// Bounce guard: re-fires of the same button sooner than this after its last
// accepted edge are suppressed. The IMU loop polls every ~10 ms, so 150 ms
// covers contact bounce and USB duplicate reports without making deliberate
// repeated presses feel stuck.
constexpr std::uint64_t kHeadsetButtonDebounceMs = 150;

enum class HeadsetButtonAction { VolumeUp, VolumeDown, MuteToggle };

inline const char* headset_button_action_name(HeadsetButtonAction action) noexcept {
    switch (action) {
        case HeadsetButtonAction::VolumeUp: return "volume_up";
        case HeadsetButtonAction::VolumeDown: return "volume_down";
        case HeadsetButtonAction::MuteToggle: return "mute_toggle";
    }
    return "unknown";
}

// Reader-local diagnostics only; never affects edge state or dispatch. Log
// initial baseline / changed bytes, at most 16 records per second and 256 per
// activation. Suppressed transitions are counted, not queued or replayed.
struct HeadsetButtonDiagnostics {
    bool initialized = false;
    std::uint8_t buttons = 0, volume = 0, flags = 0;
    std::uint64_t window_ms = 0, suppressed = 0;
    unsigned in_window = 0, emitted = 0;
    bool limit_notice_emitted = false;

    bool observe(std::uint8_t b, std::uint8_t v, std::uint8_t f) noexcept {
        const bool changed = !initialized || buttons != b || volume != v || flags != f;
        initialized = true; buttons = b; volume = v; flags = f;
        return changed;
    }
    bool allow_log(std::uint64_t now_ms) noexcept {
        if (now_ms - window_ms >= 1000) { window_ms = now_ms; in_window = 0; }
        if (emitted >= 256 || in_window >= 16) { ++suppressed; return false; }
        ++emitted; ++in_window;
        return true;
    }
    bool take_limit_notice() noexcept {
        if (emitted < 256 || limit_notice_emitted) return false;
        limit_notice_emitted = true;
        return true;
    }
};

struct HeadsetButtonEdgeState {
    std::uint8_t previous = 0;
    bool initialized = false;
    // Per-button (index 0=Up, 1=Down, 2=Mute) last accepted edge time.
    std::uint64_t last_fire_ms[3] = {0, 0, 0};
    bool fired_once[3] = {false, false, false};
};

// Returns one action per 0->1 transition of a known button bit, in bit order
// Up, Down, Mute. A held button (bit stays 1 across reports) fires exactly
// once; a rising edge inside the debounce window of the same button's last
// accepted edge is suppressed. Unknown bits are ignored. The first observed
// report only arms the detector (no action), so a button already held at
// driver activation does not emit a spurious volume/mute step.
inline std::vector<HeadsetButtonAction> poll_headset_button_edges(
    std::uint8_t byte0, std::uint64_t now_ms, HeadsetButtonEdgeState& state) {
    std::vector<HeadsetButtonAction> actions;
    if (!state.initialized) {
        state.previous = byte0;
        state.initialized = true;
        return actions;
    }
    const std::uint8_t risen =
        static_cast<std::uint8_t>((byte0 & ~state.previous) & kHeadsetButtonMaskAll);
    state.previous = byte0;
    const std::uint8_t masks[3] = {kHeadsetVolumeUpMask, kHeadsetVolumeDownMask, kHeadsetMuteMask};
    const HeadsetButtonAction ids[3] = {HeadsetButtonAction::VolumeUp,
                                        HeadsetButtonAction::VolumeDown,
                                        HeadsetButtonAction::MuteToggle};
    for (int i = 0; i < 3; ++i) {
        if ((risen & masks[i]) == 0) continue;
        if (state.fired_once[i] && now_ms - state.last_fire_ms[i] < kHeadsetButtonDebounceMs)
            continue; // bounce/duplicate inside the guard window
        state.last_fire_ms[i] = now_ms;
        state.fired_once[i] = true;
        actions.push_back(ids[i]);
    }
    return actions;
}

} // namespace psvr
