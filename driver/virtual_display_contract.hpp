// SPDX-License-Identifier: MIT
#pragma once
#include <openvr_driver.h>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <type_traits>

namespace psvr {
// Hardware-free ABI contract for vr::IVRVirtualDisplay_002, pinned to the
// generated openvr_driver.h of SteamVR SDK 2.15.6. This header proves the
// PresentInfo_t layout and the abstract three-method shape at compile time
// and gates incoming Present() bytes. It is NOT a display backend: nothing
// here touches GPU resources, shared texture memory, scanout timing,
// DriverLog, WinAPI, threads, or dynamic loading. The concrete test-only
// presenter lives in tests/virtual_display_contract_tests.cpp, never here,
// and nothing in this repository registers one with any runtime.
//
// The SDK header wraps vrtypes.h in #pragma pack(push, 8), so MSVC and
// MinGW GCC agree on every offset asserted here.
//
// --- PresentInfo_t ABI: each fact pinned independently ---
static_assert(sizeof(vr::PresentInfo_t) == 32, "PresentInfo_t size changed");
static_assert(std::is_standard_layout<vr::PresentInfo_t>::value, "offsetof needs standard layout");
static_assert(offsetof(vr::PresentInfo_t, backbufferTextureHandle) == 0, "backbufferTextureHandle offset changed");
static_assert(offsetof(vr::PresentInfo_t, vsync) == 8, "vsync offset changed");
static_assert(offsetof(vr::PresentInfo_t, nFrameId) == 16, "nFrameId offset changed");
static_assert(offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds) == 24, "flVSyncTimeInSeconds offset changed");
static_assert(sizeof(vr::SharedTextureHandle_t) == 8, "shared handle width changed");
static_assert(std::is_same<vr::SharedTextureHandle_t, std::uint64_t>::value, "shared handle is not uint64");
static_assert(sizeof(vr::EVSync) == 4, "EVSync storage width changed");
static_assert(vr::VSync_None == 0 && vr::VSync_WaitRender == 1 && vr::VSync_NoWaitRender == 2,
              "EVSync enumerator values changed");

// --- IVRVirtualDisplay_002 abstract shape and exact signatures ---
static_assert(std::is_abstract<vr::IVRVirtualDisplay>::value, "IVRVirtualDisplay must stay abstract");
static_assert(!std::has_virtual_destructor<vr::IVRVirtualDisplay>::value,
              "SDK interface changed: no virtual destructor, so never delete through a vr::IVRVirtualDisplay*");
static_assert(std::is_same<decltype(&vr::IVRVirtualDisplay::Present),
                           void (vr::IVRVirtualDisplay::*)(const vr::PresentInfo_t*, std::uint32_t)>::value,
              "Present signature changed");
static_assert(std::is_same<decltype(&vr::IVRVirtualDisplay::WaitForPresent),
                           void (vr::IVRVirtualDisplay::*)()>::value,
              "WaitForPresent signature changed");
static_assert(std::is_same<decltype(&vr::IVRVirtualDisplay::GetTimeSinceLastVsync),
                           bool (vr::IVRVirtualDisplay::*)(float*, std::uint64_t*)>::value,
              "GetTimeSinceLastVsync signature changed");

// Typed outcome of the raw-bytes gate, so tests assert exact reasons.
enum class PresentInfoCopyResult {
    kAccepted,
    kNullSource,
    kBufferTooSmall,
    kUnknownVSync,
    kZeroSharedHandle,
    kNonFiniteVSyncTime,
};

// Validate-and-copy compositor bytes into a typed PresentInfo_t.
// Null and undersized buffers are rejected before any read of the source
// object representation. A size larger than the struct is accepted as a
// forward-compatible prefix: exactly the first sizeof(PresentInfo_t) bytes
// are copied and the tail is never touched. Fields are decoded from raw
// bytes via memcpy, so out-of-range EVSync values never enter the enum type
// (no UB reading the enumerator). Only when every rule passes does this
// write the destination; every rejection leaves it byte-for-byte unmodified.
// Windows x64 only: doubles here have no trap representations.
inline PresentInfoCopyResult copy_present_info(vr::PresentInfo_t& destination,
                                               const void* source,
                                               std::uint32_t source_size_bytes) noexcept {
    if (source == nullptr) return PresentInfoCopyResult::kNullSource;
    if (source_size_bytes < sizeof(vr::PresentInfo_t)) return PresentInfoCopyResult::kBufferTooSmall;
    unsigned char prefix[sizeof(vr::PresentInfo_t)];
    std::memcpy(prefix, source, sizeof prefix);
    vr::SharedTextureHandle_t handle = 0;
    std::uint32_t vsync_raw = 0;
    std::uint64_t frame_id = 0;
    double vsync_time = 0.0;
    std::memcpy(&handle, prefix + offsetof(vr::PresentInfo_t, backbufferTextureHandle), sizeof handle);
    std::memcpy(&vsync_raw, prefix + offsetof(vr::PresentInfo_t, vsync), sizeof vsync_raw);
    std::memcpy(&frame_id, prefix + offsetof(vr::PresentInfo_t, nFrameId), sizeof frame_id);
    std::memcpy(&vsync_time, prefix + offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds), sizeof vsync_time);
    // Enumerators are 0..2; a zero-extended uint32 leaves no negative blind spot.
    if (vsync_raw > static_cast<std::uint32_t>(vr::VSync_NoWaitRender)) return PresentInfoCopyResult::kUnknownVSync;
    if (handle == INVALID_SHARED_TEXTURE_HANDLE) return PresentInfoCopyResult::kZeroSharedHandle;
    if (!std::isfinite(vsync_time)) return PresentInfoCopyResult::kNonFiniteVSyncTime;
    destination.backbufferTextureHandle = handle;
    destination.vsync = static_cast<vr::EVSync>(vsync_raw);
    destination.nFrameId = frame_id;
    destination.flVSyncTimeInSeconds = vsync_time;
    return PresentInfoCopyResult::kAccepted;
}

// ABI-shaped convenience overload for the vr::PresentInfo_t* parameter the
// compositor hands to Present(). It adds no rules: bytes decide.
inline PresentInfoCopyResult copy_present_info(vr::PresentInfo_t& destination,
                                               const vr::PresentInfo_t* source,
                                               std::uint32_t source_size_bytes) noexcept {
    return copy_present_info(destination, static_cast<const void*>(source), source_size_bytes);
}
} // namespace psvr
