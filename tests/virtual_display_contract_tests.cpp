// SPDX-License-Identifier: MIT
// Hardware-free ABI/contract harness for vr::IVRVirtualDisplay_002, pinned to
// SteamVR SDK 2.15.6 openvr_driver.h. It is NOT a fake display backend: the
// test-only presenter stub below is the compile-shape proof required by the
// contract, with zero GPU, timing, DriverLog, WinAPI, threading, or
// dynamic-load behaviour, and is never registered anywhere.
// No SteamVR runtime, VRInit, driver factory, or component registration is
// invoked; this executable links nothing beyond the C++ runtime.
// Not registered in CMakeLists.txt by design; CMake/ctest integration is a
// deliberate later step. Build manually against the SAME openvr_driver.h the
// driver uses (C++17, MinGW-w64 example):
//   g++ -std=c++17 -Wall -Wextra -Wpedantic -isystem <sdk-header-dir> -Idriver
//       -o virtual_display_contract_tests.exe tests/virtual_display_contract_tests.cpp
#include "virtual_display_contract.hpp"
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>

namespace {
void check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

using psvr::copy_present_info;
using psvr::PresentInfoCopyResult;

bool status_is(PresentInfoCopyResult got, PresentInfoCopyResult want) { return got == want; }

// Source as the compositor sends it: raw bytes with guards on both sides so
// any write outside the declared body becomes observable.
struct alignas(vr::PresentInfo_t) Wire {
    unsigned char before[16];
    unsigned char body[64]; // struct (32) + room for a future-larger prefix
    unsigned char after[16];
};
static_assert(offsetof(Wire, body) == 16, "wire layout changed");

// Destination guarded by canary bytes: a raw scan proves only the 32-byte
// object was written and that every rejection leaves it untouched.
struct alignas(vr::PresentInfo_t) Destination {
    unsigned char before[16];
    vr::PresentInfo_t info;
    unsigned char after[16];
};
static_assert(offsetof(Destination, info) == 16, "destination guard layout changed");

void build_wire(Wire& wire, std::uint64_t handle, std::uint32_t vsync_raw,
                std::uint64_t frame_id, double vsync_time) {
    std::memset(wire.before, 0x5A, sizeof wire.before);
    std::memset(wire.after, 0x5A, sizeof wire.after);
    std::memset(wire.body, 0xCC, sizeof wire.body); // unknown/padding/future bytes
    std::memcpy(wire.body + offsetof(vr::PresentInfo_t, backbufferTextureHandle), &handle, sizeof handle);
    std::memcpy(wire.body + offsetof(vr::PresentInfo_t, vsync), &vsync_raw, sizeof vsync_raw);
    std::memcpy(wire.body + offsetof(vr::PresentInfo_t, nFrameId), &frame_id, sizeof frame_id);
    std::memcpy(wire.body + offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds), &vsync_time, sizeof vsync_time);
}

void poison(Destination& destination) {
    std::memset(destination.before, 0xA5, sizeof destination.before);
    std::memset(destination.after, 0xA5, sizeof destination.after);
    std::memset(static_cast<unsigned char*>(static_cast<void*>(&destination.info)), 0x3C, sizeof destination.info);
}

bool destination_untouched(const Destination& destination) {
    for (unsigned char byte : destination.before) if (byte != 0xA5) return false;
    for (unsigned char byte : destination.after) if (byte != 0xA5) return false;
    const unsigned char* info = static_cast<const unsigned char*>(static_cast<const void*>(&destination.info));
    for (std::size_t i = 0; i < sizeof destination.info; ++i) if (info[i] != 0x3C) return false;
    return true;
}

bool destination_guards_intact(const Destination& destination) {
    for (unsigned char byte : destination.before) if (byte != 0xA5) return false;
    for (unsigned char byte : destination.after) if (byte != 0xA5) return false;
    return true;
}

bool wire_guards_intact(const Wire& wire) {
    for (unsigned char byte : wire.before) if (byte != 0x5A) return false;
    for (unsigned char byte : wire.after) if (byte != 0x5A) return false;
    return true;
}

// One gate attempt: poison, copy with the declared size, classify, rescan.
PresentInfoCopyResult attempt(Destination& destination, const Wire& wire, std::uint32_t size,
                              const std::string& label) {
    poison(destination);
    const auto result = copy_present_info(destination.info, static_cast<const void*>(wire.body), size);
    check(wire_guards_intact(wire), "wire guards damaged: " + label);
    return result;
}

const std::uint64_t kGoodHandle = 0x1122334455667788ull;
const std::uint32_t kGoodVSync = 1; // VSync_WaitRender
const std::uint64_t kGoodFrame = 42;
const double kGoodTime = 0.008333333333333333; // 1/120, finite

// Test-only compile-shape proof for IVRVirtualDisplay_002. The name and the
// anonymous namespace keep it out of reach of any production backend, and
// nothing here is ever registered with SteamVR or another runtime. Single
// public inheritance, no other bases, all three pure virtuals overridden.
// Only in-process virtual dispatch is exercised: this harness neither claims
// nor tests compatibility with a host built by a different compiler or
// standard library. All retained state is bounded fixed-size metadata;
// payloads only reach psvr::copy_present_info, which decodes raw bytes and
// never follows the handle. The compositor frame id in the validated
// snapshot and the internal accepted-present counter stay distinct; neither
// is derived from or overwritten by the other, and no timing or scanout
// claim is ever invented.
class TestOnlyPresenterStub final : public vr::IVRVirtualDisplay {
    vr::PresentInfo_t last_presented_{};
    bool has_presented_ = false;
    std::uint64_t accepted_presents_ = 0; // internal virtual-frame stream
    std::uint64_t rejected_presents_ = 0;
    std::uint64_t wait_calls_ = 0;
public:
    // ABI returns void, so malformed submits are counted, never thrown.
    // The pointer is only viewed as bytes; fields are not read directly.
    void Present(const vr::PresentInfo_t* pPresentInfo, std::uint32_t unPresentInfoSize) override {
        vr::PresentInfo_t snapshot{};
        if (copy_present_info(snapshot, pPresentInfo, unPresentInfoSize) == PresentInfoCopyResult::kAccepted) {
            last_presented_ = snapshot;
            has_presented_ = true;
            ++accepted_presents_;
        } else {
            ++rejected_presents_;
        }
    }
    // Nothing ever scans out here: blocking would deadlock the harness and
    // returning "success" would claim scanout. Immediate no-op is the honest
    // non-functional answer; a real backend must implement its own.
    void WaitForPresent() override { ++wait_calls_; }
    bool GetTimeSinceLastVsync(float* pfSecondsSinceLastVsync, std::uint64_t* pulFrameCounter) override {
        if (pfSecondsSinceLastVsync) *pfSecondsSinceLastVsync = 0.0f;
        if (pulFrameCounter) *pulFrameCounter = 0;
        return false; // no hardware, therefore no vsync time and no frame counter
    }
    // Test-only introspection of the bounded metadata.
    bool has_presented() const noexcept { return has_presented_; }
    const vr::PresentInfo_t& last_presented() const noexcept { return last_presented_; }
    std::uint64_t accepted_presents() const noexcept { return accepted_presents_; }
    std::uint64_t rejected_presents() const noexcept { return rejected_presents_; }
    std::uint64_t wait_calls() const noexcept { return wait_calls_; }
};
static_assert(std::is_base_of<vr::IVRVirtualDisplay, TestOnlyPresenterStub>::value,
              "test-only stub must derive from the SDK interface");
static_assert(!std::is_abstract<TestOnlyPresenterStub>::value,
              "all three IVRVirtualDisplay_002 pure virtual methods must be overridden");
} // namespace

int main() {
    try {
        // 1. ABI: eight independent typed layout facts, each checked separately.
        check(sizeof(vr::PresentInfo_t) == 32, "ABI: PresentInfo_t is 32 bytes");
        check(offsetof(vr::PresentInfo_t, backbufferTextureHandle) == 0, "ABI: handle at offset 0");
        check(offsetof(vr::PresentInfo_t, vsync) == 8, "ABI: vsync at offset 8");
        check(offsetof(vr::PresentInfo_t, nFrameId) == 16, "ABI: frame id at offset 16");
        check(offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds) == 24, "ABI: vsync time at offset 24");
        check(sizeof(vr::SharedTextureHandle_t) == 8, "ABI: shared handle width 8");
        check(std::is_same<vr::SharedTextureHandle_t, std::uint64_t>::value, "ABI: shared handle typedef uint64");
        check(sizeof(vr::EVSync) == 4, "ABI: EVSync stored in 4 bytes");
        check(vr::VSync_None == 0 && vr::VSync_WaitRender == 1 && vr::VSync_NoWaitRender == 2,
              "ABI: EVSync enumerators 0/1/2");
        check(std::strcmp(vr::IVRVirtualDisplay_Version, "IVRVirtualDisplay_002") == 0,
              "ABI: interface version string is IVRVirtualDisplay_002");

        // 2. Class shape: the test-only stub compiles against the abstract
        // base with single inheritance; the base shares the object address.
        TestOnlyPresenterStub concrete;
        vr::IVRVirtualDisplay* presenter = &concrete;
        check(static_cast<void*>(presenter) == static_cast<void*>(&concrete),
              "shape: single-inheritance base shares object address");

        Wire wire;
        Destination destination;

        // 3. Gate rejects before touching memory: a null read would have
        // crashed, so simply classifying proves the guard order.
        poison(destination);
        check(status_is(copy_present_info(destination.info, static_cast<const void*>(nullptr), 32),
                        PresentInfoCopyResult::kNullSource),
              "gate: null source with full size rejected");
        check(destination_untouched(destination), "gate: null source left destination bytes untouched");
        check(status_is(copy_present_info(destination.info, static_cast<const void*>(nullptr), 0),
                        PresentInfoCopyResult::kNullSource),
              "gate: null source with zero size rejected");
        check(destination_untouched(destination), "gate: null source (size 0) left destination bytes untouched");

        // 4. Truncated sizes never reach content validation.
        build_wire(wire, kGoodHandle, kGoodVSync, kGoodFrame, kGoodTime);
        check(status_is(attempt(destination, wire, 0, "size 0 valid content"),
                        PresentInfoCopyResult::kBufferTooSmall),
              "gate: size 0 with valid body rejected as too small");
        check(destination_untouched(destination), "gate: truncated size committed nothing");
        check(status_is(attempt(destination, wire, static_cast<std::uint32_t>(sizeof(vr::PresentInfo_t)) - 1,
                                "size 31 valid content"),
                        PresentInfoCopyResult::kBufferTooSmall),
              "gate: size 31 with valid body rejected as too small");
        check(destination_untouched(destination), "gate: size 31 committed nothing");

        // 5. Exact-size and future-larger prefixes are accepted; the tail is ignored.
        for (std::uint32_t size : {32u, 33u, 64u}) {
            const std::string label = "size " + std::to_string(size);
            check(status_is(attempt(destination, wire, size, label), PresentInfoCopyResult::kAccepted),
                  "gate: valid body accepted with size " + label);
            check(destination.info.backbufferTextureHandle == kGoodHandle &&
                  destination.info.vsync == vr::VSync_WaitRender &&
                  destination.info.nFrameId == kGoodFrame &&
                  destination.info.flVSyncTimeInSeconds == kGoodTime,
                  "gate: prefix fields copied for " + label);
            check(destination_guards_intact(destination), "gate: destination canaries intact for " + label);
        }

        // 6. Invalid data is rejected with the exact rule, destination untouched.
        const auto reject = [&](const Wire& bad, std::uint32_t size, PresentInfoCopyResult want,
                                const std::string& label) {
            check(status_is(attempt(destination, bad, size, label), want), "gate: expected rejection " + label);
            check(destination_untouched(destination), "gate: rejection left destination untouched: " + label);
        };
        build_wire(wire, kGoodHandle, 3, kGoodFrame, kGoodTime);
        reject(wire, 32, PresentInfoCopyResult::kUnknownVSync, "vsync enumerator 3");
        build_wire(wire, kGoodHandle, 99, kGoodFrame, kGoodTime);
        reject(wire, 64, PresentInfoCopyResult::kUnknownVSync, "vsync enumerator 99 (future prefix)");
        build_wire(wire, kGoodHandle, 0xFFFFFFFFu, kGoodFrame, kGoodTime);
        reject(wire, 32, PresentInfoCopyResult::kUnknownVSync, "vsync bytes 0xFFFFFFFF (negative int)");
        build_wire(wire, INVALID_SHARED_TEXTURE_HANDLE, kGoodVSync, kGoodFrame, kGoodTime);
        reject(wire, 32, PresentInfoCopyResult::kZeroSharedHandle, "zero shared handle");
        const double nan_value = std::numeric_limits<double>::quiet_NaN();
        const double positive_infinity = std::numeric_limits<double>::infinity();
        const double negative_infinity = -std::numeric_limits<double>::infinity();
        for (const double bad_time : {nan_value, positive_infinity, negative_infinity}) {
            build_wire(wire, kGoodHandle, kGoodVSync, kGoodFrame, bad_time);
            reject(wire, 32, PresentInfoCopyResult::kNonFiniteVSyncTime, "non-finite vsync time");
        }

        // 7. Rule order is deterministic: the first listed fault wins.
        build_wire(wire, 0, 7, kGoodFrame, nan_value);
        reject(wire, 32, PresentInfoCopyResult::kUnknownVSync, "vsync checked before handle and time");
        build_wire(wire, 0, kGoodVSync, kGoodFrame, nan_value);
        reject(wire, 32, PresentInfoCopyResult::kZeroSharedHandle, "handle checked before vsync time");

        // 8. No invented rules: every finite/zero-but-legal combination passes.
        build_wire(wire, kGoodHandle, 0, 0, 0.0); // VSync_None, frame id 0 (first frame), time 0
        check(status_is(attempt(destination, wire, 32, "all-zero-legal"), PresentInfoCopyResult::kAccepted),
              "gate: frame id 0 and vsync time 0.0 are legal and accepted");
        check(destination.info.nFrameId == 0 && destination.info.flVSyncTimeInSeconds == 0.0 &&
              destination.info.vsync == vr::VSync_None,
              "gate: first-frame snapshot copied exactly");
        for (std::uint32_t vsync : {0u, 1u, 2u}) {
            build_wire(wire, kGoodHandle, vsync, kGoodFrame, -1.5);
            check(status_is(attempt(destination, wire, 32, "negative time"), PresentInfoCopyResult::kAccepted),
                  "gate: finite negative vsync time with enumerator " + std::to_string(vsync) + " accepted");
        }

        // 9. Typed ABI-shaped overload accepts a real struct object.
        vr::PresentInfo_t typed{};
        typed.backbufferTextureHandle = kGoodHandle;
        typed.vsync = vr::VSync_NoWaitRender;
        typed.nFrameId = 7;
        typed.flVSyncTimeInSeconds = 0.0166666;
        poison(destination);
        check(status_is(copy_present_info(destination.info, &typed, static_cast<std::uint32_t>(sizeof(typed))),
                        PresentInfoCopyResult::kAccepted),
              "gate: typed overload accepts a well-formed struct");
        check(destination.info.nFrameId == 7 && destination.info.vsync == vr::VSync_NoWaitRender,
              "gate: typed overload copied fields");

        // 10. Presenter: honest non-functional runtime. Timing reads validate
        // only: nothing is fabricated, counters stay distinct from frame ids.
        float seconds_since_last_vsync = 123.0f;
        std::uint64_t vsync_frame_counter = 999;
        check(!presenter->GetTimeSinceLastVsync(&seconds_since_last_vsync, &vsync_frame_counter),
              "presenter: GetTimeSinceLastVsync reports no hardware timing");
        check(seconds_since_last_vsync == 0.0f && vsync_frame_counter == 0,
              "presenter: timing outputs zeroed, not invented");

        presenter->Present(&typed, static_cast<std::uint32_t>(sizeof(typed))); // frame id 7, enumerator 2
        check(concrete.accepted_presents() == 1 && concrete.rejected_presents() == 0 && concrete.has_presented(),
              "presenter: valid submit accepted once");
        check(concrete.last_presented().nFrameId == 7 &&
              concrete.last_presented().backbufferTextureHandle == kGoodHandle &&
              concrete.last_presented().vsync == vr::VSync_NoWaitRender,
              "presenter: snapshot preserved compositor fields");

        presenter->WaitForPresent();
        check(concrete.wait_calls() == 1, "presenter: WaitForPresent returned immediately, never claimed scanout");
        check(!presenter->GetTimeSinceLastVsync(&seconds_since_last_vsync, &vsync_frame_counter) &&
              seconds_since_last_vsync == 0.0f && vsync_frame_counter == 0,
              "presenter: waiting did not invent timing or a vsync counter");

        // Malformed submit through the ABI pointer: bytes only, state unchanged.
        build_wire(wire, 0, kGoodVSync, 1234, kGoodTime); // zero shared handle
        presenter->Present(reinterpret_cast<const vr::PresentInfo_t*>(wire.body), 32);
        check(concrete.accepted_presents() == 1 && concrete.rejected_presents() == 1,
              "presenter: malformed submit counted, not thrown across the ABI");
        check(concrete.last_presented().nFrameId == 7,
              "presenter: rejection did not overwrite the last good snapshot");
        presenter->Present(nullptr, 0);
        check(concrete.rejected_presents() == 2, "presenter: null submit survived and counted");

        // Compositor frame ids stream in independently from the internal
        // virtual-frame counter; neither one is derived from the other.
        vr::PresentInfo_t stream = typed;
        stream.nFrameId = 100000;
        presenter->Present(&stream, static_cast<std::uint32_t>(sizeof(stream)));
        stream.nFrameId = 200000;
        presenter->Present(&stream, static_cast<std::uint32_t>(sizeof(stream)));
        check(concrete.accepted_presents() == 3 && concrete.last_presented().nFrameId == 200000,
              "presenter: counter is submits-seen, frame id is the compositor value");
        check(concrete.accepted_presents() != concrete.last_presented().nFrameId,
              "presenter: internal counter never equals or replaces the frame id");

        std::cout << "PASS IVRVirtualDisplay_002 PresentInfo ABI, byte-gate, and non-functional presenter shape\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
