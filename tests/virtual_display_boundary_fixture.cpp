// SPDX-License-Identifier: MIT
// Test-only boundary fixture DLL for vr::IVRVirtualDisplay_002, pinned to the
// generated openvr_driver.h of SteamVR SDK 2.15.6 (see driver/virtual_display_
// contract.hpp). This translation unit is the ONLY place in the harness that
// knows the SDK C++ types: it implements the abstract three-method interface
// concretely so the interface, its vtable, and its bytes never leave the DLL.
// The sibling tests/virtual_display_boundary_tests.cpp loads the built DLL by
// full supplied path and calls vtable slots through handwritten Windows x64
// signatures - never through the SDK's C++ dispatch.
//
// What this fixture is NOT: it is not a display backend and it is never
// linked into or registered with any production target (driver_psvrpc does
// not reference it). WaitForPresent is a counter only - no scanout, no
// blocking, no hardware. GetTimeSinceLastVsync returns explicitly SYNTHETIC
// debug-configured test values (a fixed float and a private incrementing
// stream); they are not, and must never be described as, real vsync timing.
// Nothing here touches GPU resources, shared texture memory, DriverLog, the
// registry, UAC, SteamVR, or dynamic loading of other modules. Single-thread
// by construction: one caller, sequential calls, therefore no locks.
//
// Present() bytes are gated by psvr::copy_present_info, the SAME parser the
// driver contract uses: null/short sources are rejected before any read, an
// oversized source is accepted as a forward-compatible prefix and its tail
// is never touched, and every rejection leaves the accepted snapshot and
// counters byte-for-byte unchanged (observable via the trace out-parameter).
//
// Exports are extern "C" with deterministic undecorated x64 names. Handles
// are opaque void*; the interface has no virtual destructor (contract
// static_assert), so psvr_vdb_destroy casts back to the CONCRETE type and
// deletes there - deleting through a vr::IVRVirtualDisplay* would be UB and
// is deliberately impossible from outside. No destructor is guessed or
// invoked via vtable; this build's vtable carries only the three declared
// slots, and the caller proves that behaviorally by journal-checked calls
// through exactly three slot entries.
//
// Build is a separate translation unit, normal compilation, no LTO (MinGW
// example, run from the repository root):
//   g++ -std=c++17 -Wall -Wextra -Wpedantic -O1 -c `
//       -isystem C:\Users\rickk\AppData\Local\Temp\opencode\psvr-openvr\headers `
//       -I driver -I tests `
//       -o <temp>\virtual_display_boundary_fixture.obj `
//       tests\virtual_display_boundary_fixture.cpp
//   g++ -shared -o <temp>\psvr_virtual_display_boundary_fixture.dll `
//       <temp>\virtual_display_boundary_fixture.obj `
//       -static -static-libgcc -static-libstdc++
// All build outputs go to a temporary directory only.
#define PSVR_VDB_BUILD_DLL
#include "virtual_display_boundary_api.hpp"
#include "virtual_display_contract.hpp"

#include <cmath>
#include <cstring>
#include <new>
#include <type_traits>

namespace {

// Cross-checks between the shared boundary ABI and the pinned SDK as seen
// from inside the DLL. A drift breaks the build, never the running test.
static_assert(sizeof(vr::PresentInfo_t) == PSVR_VDB_WIRE_BYTES, "wire byte count drifted from SDK");
static_assert(offsetof(vr::PresentInfo_t, backbufferTextureHandle) == PSVR_VDB_WIRE_OFFSET_HANDLE,
              "wire handle offset drifted from SDK");
static_assert(offsetof(vr::PresentInfo_t, vsync) == PSVR_VDB_WIRE_OFFSET_VSYNC,
              "wire vsync offset drifted from SDK");
static_assert(offsetof(vr::PresentInfo_t, nFrameId) == PSVR_VDB_WIRE_OFFSET_FRAME,
              "wire frame id offset drifted from SDK");
static_assert(offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds) == PSVR_VDB_WIRE_OFFSET_TIME,
              "wire vsync time offset drifted from SDK");
static_assert(static_cast<vr::SharedTextureHandle_t>(PSVR_VDB_INVALID_SHARED_HANDLE_BYTES) ==
                  INVALID_SHARED_TEXTURE_HANDLE,
              "invalid shared handle value drifted from SDK");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kAccepted) ==
                  PSVR_VDB_PRESENT_ACCEPTED, "gate code map: kAccepted");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kNullSource) ==
                  PSVR_VDB_PRESENT_NULL_SOURCE, "gate code map: kNullSource");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kBufferTooSmall) ==
                  PSVR_VDB_PRESENT_BUFFER_TOO_SMALL, "gate code map: kBufferTooSmall");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kUnknownVSync) ==
                  PSVR_VDB_PRESENT_UNKNOWN_VSYNC, "gate code map: kUnknownVSync");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kZeroSharedHandle) ==
                  PSVR_VDB_PRESENT_ZERO_SHARED_HANDLE, "gate code map: kZeroSharedHandle");
static_assert(static_cast<std::uint32_t>(psvr::PresentInfoCopyResult::kNonFiniteVSyncTime) ==
                  PSVR_VDB_PRESENT_NONFINITE_TIME, "gate code map: kNonFiniteVSyncTime");
static_assert(!std::has_virtual_destructor<vr::IVRVirtualDisplay>::value,
              "interface gained a virtual destructor: slot layout claim must be re-verified");

// Test-only concrete implementation of the pinned SDK interface. final and
// single public inheritance keep the base pointer equal to the object
// address; no user-declared destructor exists, none is assumed. All retained
// state is bounded plain data mirroring psvr_vdb_trace_t; the handle value
// from Present bytes is stored as an integer and NEVER followed as a pointer.
class BoundaryFixtureDisplay final : public vr::IVRVirtualDisplay {
    const bool timing_synthetic_;        // debug configuration, from create()
    std::uint32_t config_flags_;
    std::uint32_t last_present_reason_ = PSVR_VDB_PRESENT_ACCEPTED;
    std::uint64_t object_address_ = 0;   // raw 'this' as observed by slot calls
    std::uint64_t accepted_presents_ = 0;
    std::uint64_t rejected_presents_ = 0;
    std::uint64_t wait_calls_ = 0;
    std::uint64_t gettime_calls_ = 0;
    std::uint64_t last_compositor_frame_id_ = 0;
    std::uint64_t last_shared_handle_ = 0;
    double last_vsync_time_seconds_ = 0.0;
    std::uint32_t last_vsync_raw_ = 0;
    std::uint32_t call_count_ = 0;
    std::uint32_t slot_ring_[PSVR_VDB_SLOT_RING_SIZE];
    std::uint32_t gettime_return_raw_ = 0;
    float gettime_seconds_ = 0.0f;
    std::uint64_t gettime_frame_counter_ = 0;
    std::uint64_t synthetic_vsync_stream_ = 0; // fixture-private, never a compositor id

public:
    explicit BoundaryFixtureDisplay(std::uint32_t config_flags) noexcept
        : timing_synthetic_((config_flags & PSVR_VDB_CFG_TIMING_SYNTHETIC) != 0u),
          config_flags_(config_flags) {
        object_address_ = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(this));
        for (std::uint32_t& entry : slot_ring_) entry = PSVR_VDB_SLOT_NONE;
    }

    // ABI returns void, so malformed submits are counted, never thrown. The
    // pointer is viewed as raw bytes only; psvr::copy_present_info decides.
    void Present(const vr::PresentInfo_t* pPresentInfo, std::uint32_t unPresentInfoSize) override {
        note_slot(PSVR_VDB_SLOT_PRESENT);
        vr::PresentInfo_t snapshot{};
        const psvr::PresentInfoCopyResult result =
            psvr::copy_present_info(snapshot, pPresentInfo, unPresentInfoSize);
        last_present_reason_ = static_cast<std::uint32_t>(result);
        if (result == psvr::PresentInfoCopyResult::kAccepted) {
            last_shared_handle_ = snapshot.backbufferTextureHandle;
            last_vsync_raw_ = static_cast<std::uint32_t>(snapshot.vsync);
            last_compositor_frame_id_ = snapshot.nFrameId;
            last_vsync_time_seconds_ = snapshot.flVSyncTimeInSeconds;
            ++accepted_presents_;
        } else {
            // Rejection keeps every accepted snapshot field untouched.
            ++rejected_presents_;
        }
    }

    // Counter only. Nothing here blocks or scans out; a real backend must
    // implement its own wait, and this fixture claims nothing about it.
    void WaitForPresent() override {
        note_slot(PSVR_VDB_SLOT_WAIT);
        ++wait_calls_;
    }

    // SYNTHETIC TEST DATA ONLY. Debug-configured fixture behaviour, returned
    // verbatim to the caller so raw bool/pointer crossing can be verified:
    // with PSVR_VDB_CFG_TIMING_SYNTHETIC it returns true plus a fixed float
    // and a private per-call stream (never the compositor frame id); without
    // it, the honest no-hardware answer: false with zeroed outputs.
    bool GetTimeSinceLastVsync(float* pfSecondsSinceLastVsync, std::uint64_t* pulFrameCounter) override {
        note_slot(PSVR_VDB_SLOT_GET_TIME);
        ++gettime_calls_;
        if (timing_synthetic_) {
            ++synthetic_vsync_stream_;
            gettime_return_raw_ = 1u;
            gettime_seconds_ = static_cast<float>(PSVR_VDB_SYNTHETIC_SECONDS_F);
            gettime_frame_counter_ = synthetic_vsync_stream_;
        } else {
            gettime_return_raw_ = 0u;
            gettime_seconds_ = 0.0f;
            gettime_frame_counter_ = 0u;
        }
        if (pfSecondsSinceLastVsync) *pfSecondsSinceLastVsync = gettime_seconds_;
        if (pulFrameCounter) *pulFrameCounter = gettime_frame_counter_;
        return gettime_return_raw_ != 0u;
    }

    // Bounded journal snapshot into the fixed-layout out-parameter.
    void fill_trace(psvr_vdb_trace_t& out) const noexcept {
        out = psvr_vdb_trace_t{};
        out.struct_size = PSVR_VDB_TRACE_BYTES;
        out.magic = PSVR_VDB_TRACE_MAGIC;
        out.config_flags = config_flags_;
        out.last_present_reason = last_present_reason_;
        out.object_address = object_address_;
        out.accepted_presents = accepted_presents_;
        out.rejected_presents = rejected_presents_;
        out.wait_calls = wait_calls_;
        out.gettime_calls = gettime_calls_;
        out.last_compositor_frame_id = last_compositor_frame_id_;
        out.last_shared_handle = last_shared_handle_;
        out.last_vsync_time_seconds = last_vsync_time_seconds_;
        out.last_vsync_raw = last_vsync_raw_;
        out.call_count = call_count_;
        // slot_ring_ is stored physically (call k lives at position k % 8).
        // The exported journal is reconstructed in CHRONOLOGICAL order: with
        // N calls, out.slot_ring[i] = slot of call (N - window + i) for
        // i < window = min(N, 8), and SLOT_NONE after the window. Copying
        // the raw ring verbatim would expose wrap position instead of order.
        const std::uint32_t window =
            call_count_ < PSVR_VDB_SLOT_RING_SIZE ? call_count_ : PSVR_VDB_SLOT_RING_SIZE;
        for (std::uint32_t i = 0; i < PSVR_VDB_SLOT_RING_SIZE; ++i) {
            if (i < window) {
                const std::uint32_t sequence = call_count_ - window + i;
                out.slot_ring[i] = slot_ring_[sequence % PSVR_VDB_SLOT_RING_SIZE];
            } else {
                out.slot_ring[i] = PSVR_VDB_SLOT_NONE;
            }
        }
        out.gettime_return_raw = gettime_return_raw_;
        out.gettime_seconds = gettime_seconds_;
        out.gettime_frame_counter = gettime_frame_counter_;
    }

private:
    void note_slot(std::uint32_t slot_id) noexcept {
        slot_ring_[call_count_ % PSVR_VDB_SLOT_RING_SIZE] = slot_id;
        ++call_count_;
        // The raw 'this' seen on every call; a shifted base would change it.
        object_address_ = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(this));
    }
};

static_assert(!std::is_abstract<BoundaryFixtureDisplay>::value,
              "fixture must override all three IVRVirtualDisplay_002 pure virtuals");

} // namespace

// ---------------------------------------------------------------------------
// extern "C" exports. Every non-OK status writes nothing through any
// out-pointer of that call: the caller can poison its destinations and
// require them untouched, exactly like the Present gate rule on the driver
// side. On OK, out_trace (when required) receives a fresh journal snapshot.
// ---------------------------------------------------------------------------

// Fixed-layout ABI probe. Must be the first export the caller invokes: it
// pins sizes, offsets, slot numbering, enum range, and the version string.
PSVR_VDB_EXPORT std::uint32_t psvr_vdb_abi_probe(psvr_vdb_abi_probe_t* out_probe) {
    if (out_probe == nullptr) return PSVR_VDB_STATUS_NULL_OUTPUT;
    psvr_vdb_abi_probe_t probe{};
    probe.struct_size = PSVR_VDB_PROBE_BYTES;
    probe.magic = PSVR_VDB_PROBE_MAGIC;
    probe.export_abi_version = PSVR_VDB_EXPORT_ABI_VERSION;
    probe.sizeof_pointer = static_cast<std::uint32_t>(sizeof(void*));
    probe.sizeof_trace = static_cast<std::uint32_t>(sizeof(psvr_vdb_trace_t));
    probe.sizeof_probe = static_cast<std::uint32_t>(sizeof(psvr_vdb_abi_probe_t));
    probe.sizeof_present_info = static_cast<std::uint32_t>(sizeof(vr::PresentInfo_t));
    probe.sizeof_vsync_enum = static_cast<std::uint32_t>(sizeof(vr::EVSync));
    probe.slot_count = PSVR_VDB_SLOT_COUNT;
    probe.slot_present = PSVR_VDB_SLOT_PRESENT;
    probe.slot_wait = PSVR_VDB_SLOT_WAIT;
    probe.slot_get_time = PSVR_VDB_SLOT_GET_TIME;
    probe.has_virtual_destructor = std::has_virtual_destructor<vr::IVRVirtualDisplay>::value ? 1u : 0u;
    probe.offset_handle = PSVR_VDB_WIRE_OFFSET_HANDLE;
    probe.offset_vsync = PSVR_VDB_WIRE_OFFSET_VSYNC;
    probe.offset_frame = PSVR_VDB_WIRE_OFFSET_FRAME;
    probe.offset_time = PSVR_VDB_WIRE_OFFSET_TIME;
    probe.evsync_min = static_cast<std::uint32_t>(vr::VSync_None);
    probe.evsync_max = static_cast<std::uint32_t>(vr::VSync_NoWaitRender);
    probe.build_is_msvc =
#if defined(_MSC_VER)
        1u;
#else
        0u;
#endif
    // Honest diagnostic, set ONLY from _MSC_VER: full MSVC C++ vtable ABI
    // can be claimed only when MSVC itself compiled this DLL. MinGW-w64's
    // -mabi=ms is a calling-convention switch, not a claim of MSVC C++ ABI
    // equivalence, so nothing here is ever inferred from __GNUC__ versions.
    // Callable-slot compatibility is demonstrated behaviorally instead:
    // probe-pinned slot ids + the journal echoing three distinct live slots.
    probe.build_msvc_cpp_abi =
#if defined(_MSC_VER)
        1u;
#else
        0u;
#endif
    probe.invalid_shared_handle = static_cast<std::uint64_t>(INVALID_SHARED_TEXTURE_HANDLE);
    const std::size_t version_length = std::strlen(vr::IVRVirtualDisplay_Version);
    if (version_length + 1u <= sizeof probe.interface_version) {
        std::memcpy(probe.interface_version, vr::IVRVirtualDisplay_Version, version_length + 1u);
    }
    *out_probe = probe;
    return PSVR_VDB_STATUS_OK;
}

// Opaque-handle factory. Rejects wrong interface versions and unknown config
// bits before allocating: a rejected call leaves out_handle and out_trace
// untouched, so malformed requests cannot even create a half-built object.
PSVR_VDB_EXPORT std::uint32_t psvr_vdb_create(std::uint32_t interface_version,
                                              std::uint32_t config_flags,
                                              void** out_handle,
                                              psvr_vdb_trace_t* out_trace) {
    if (out_handle == nullptr || out_trace == nullptr) return PSVR_VDB_STATUS_NULL_OUTPUT;
    if (interface_version != PSVR_VDB_INTERFACE_VERSION_002) return PSVR_VDB_STATUS_BAD_VERSION;
    if ((config_flags & ~PSVR_VDB_CFG_TIMING_SYNTHETIC) != 0u) return PSVR_VDB_STATUS_BAD_CONFIG;
    BoundaryFixtureDisplay* fixture = new (std::nothrow) BoundaryFixtureDisplay(config_flags);
    if (fixture == nullptr) return PSVR_VDB_STATUS_ALLOC_FAILED;
    *out_handle = static_cast<void*>(fixture); // object address; no SDK type escapes
    fixture->fill_trace(*out_trace);
    return PSVR_VDB_STATUS_OK;
}

// Explicit journal read. Unknown/null handles are rejected without writing.
// NOTE: the handle is opaque and trusted to come from psvr_vdb_create; this
// fixture does not pretend to authenticate pointers across the boundary.
PSVR_VDB_EXPORT std::uint32_t psvr_vdb_trace_read(void* handle, psvr_vdb_trace_t* out_trace) {
    if (handle == nullptr) return PSVR_VDB_STATUS_NULL_HANDLE;
    if (out_trace == nullptr) return PSVR_VDB_STATUS_NULL_OUTPUT;
    // Single-inheritance final class: the object address IS the interface
    // address; no vtable is read here, only static casts of the same value.
    const auto* fixture = static_cast<const BoundaryFixtureDisplay*>(handle);
    fixture->fill_trace(*out_trace);
    return PSVR_VDB_STATUS_OK;
}

// Teardown. Deletes through the CONCRETE pointer on purpose: the interface
// has no virtual destructor, so deleting via vr::IVRVirtualDisplay* would be
// undefined behaviour and is never done. The final journal is copied out
// before the object ceases to exist; after return, the handle is dangling
// and must never be used again (the caller's RAII guard enforces that).
PSVR_VDB_EXPORT std::uint32_t psvr_vdb_destroy(void* handle, psvr_vdb_trace_t* out_trace) {
    if (handle == nullptr) return PSVR_VDB_STATUS_NULL_HANDLE;
    if (out_trace == nullptr) return PSVR_VDB_STATUS_NULL_OUTPUT;
    auto* fixture = static_cast<BoundaryFixtureDisplay*>(handle);
    fixture->fill_trace(*out_trace);
    delete fixture;
    return PSVR_VDB_STATUS_OK;
}
