// SPDX-License-Identifier: MIT
#pragma once
// Test-only, fixed-layout C-ABI contract shared by the two boundary-harness
// translation units:
//   tests/virtual_display_boundary_fixture.cpp  (builds a test-only DLL)
//   tests/virtual_display_boundary_tests.cpp    (builds an EXE, the caller)
// They are compiled as separate translation units with normal per-TU builds
// and no LTO, so every fact that must agree across the DLL boundary is a
// macro constant, a plain fixed-layout POD, or a static_assert visible in
// both units. Nothing here includes openvr_driver.h or <windows.h>: the SDK
// interface stays inside the DLL; the caller hand-writes Windows x64 vtable
// slot signatures from the pinned interface shape.
//
// This harness proves nothing about real hardware, SteamVR, scanout, or
// vsync: every timing value flowing through these structs is a synthetic
// fixture output, explicit test data. It is never linked into production.
//
// Windows x64 only, on purpose. MSVC may compile both units; the current
// machine has no MSVC/Windows SDK, so only MinGW-w64 GCC was executed and
// no cross-compiler equivalence is claimed.
#if !defined(_M_X64) && !defined(__x86_64__)
#error "virtual_display_boundary_api is Windows x64 only"
#endif
#include <cstddef>
#include <cstdint>

// ---------------------------------------------------------------------------
// Export names. extern "C" on x64 yields exactly these undecorated names for
// both MSVC and MinGW-w64, so GetProcAddress is deterministic.
// ---------------------------------------------------------------------------
#define PSVR_VDB_ABI_PROBE_NAME "psvr_vdb_abi_probe"
#define PSVR_VDB_CREATE_NAME    "psvr_vdb_create"
#define PSVR_VDB_TRACE_READ_NAME "psvr_vdb_trace_read"
#define PSVR_VDB_DESTROY_NAME   "psvr_vdb_destroy"

// Version pin: create() only accepts the pinned interface id (002).
#define PSVR_VDB_INTERFACE_VERSION_002 2u
#define PSVR_VDB_INTERFACE_VERSION_STRING "IVRVirtualDisplay_002"

// Export ABI revision of this harness (bump when POD layout changes).
#define PSVR_VDB_EXPORT_ABI_VERSION 1u

// Magic tags so a stale/mismatched struct is rejected before use.
#define PSVR_VDB_PROBE_MAGIC 0x41544232u // 'A''T''B''2'
#define PSVR_VDB_TRACE_MAGIC 0x56544232u // 'V''T''B''2'

// Return values of the four exports. Every non-OK status means: nothing was
// written through any out-pointer of that call; only the status is output.
#define PSVR_VDB_STATUS_OK           0u
#define PSVR_VDB_STATUS_NULL_OUTPUT  1u // out_handle or out_trace was null
#define PSVR_VDB_STATUS_BAD_VERSION  2u // requested interface version is not 002
#define PSVR_VDB_STATUS_BAD_CONFIG   3u // config carried unknown bits
#define PSVR_VDB_STATUS_NULL_HANDLE  4u // opaque handle pointer was null
#define PSVR_VDB_STATUS_ALLOC_FAILED 5u // fixture allocation failed

// create() config. Bit 0 set: GetTimeSinceLastVsync returns true with
// explicitly synthetic values (debug fixture mode). Bit 0 clear: the honest
// no-hardware answer - return false, write zeros. Any other bit is rejected.
#define PSVR_VDB_CFG_TIMING_SYNTHETIC 1u

// Slot ids of the pinned IVRVirtualDisplay_002 vtable. The interface declares
// no destructor and exactly three pure virtuals in this order, so THIS build's
// vtable is read as [Present, WaitForPresent, GetTimeSinceLastVsync] at
// indices 0/1/2. The harness demonstrates callable-slot compatibility purely
// behaviorally: the probe POD pins the claims, the caller reads only three
// distinct slot entries, and every invocation is confirmed through the
// fixture's own journal. No equivalence of some other compiler's vtable
// layout is assumed or claimed anywhere: in particular, MinGW-w64's
// -mabi=ms is a calling-convention switch, not a statement of full MSVC C++
// ABI agreement, and this harness never infers an ABI from __GNUC__ versions.
// The caller verifies every value against the probe and refuses to invoke
// anything on disagreement.
#define PSVR_VDB_SLOT_PRESENT  0u
#define PSVR_VDB_SLOT_WAIT     1u
#define PSVR_VDB_SLOT_GET_TIME 2u
#define PSVR_VDB_SLOT_COUNT    3u
#define PSVR_VDB_SLOT_NONE     0xFFFFFFFFu
#define PSVR_VDB_SLOT_RING_SIZE 8u

// Outcome codes stored in psvr_vdb_trace_t::last_present_reason. They mirror
// psvr::PresentInfoCopyResult one-to-one; the fixture static_asserts the
// mapping, the caller never includes the contract header.
#define PSVR_VDB_PRESENT_ACCEPTED          0u
#define PSVR_VDB_PRESENT_NULL_SOURCE       1u
#define PSVR_VDB_PRESENT_BUFFER_TOO_SMALL  2u
#define PSVR_VDB_PRESENT_UNKNOWN_VSYNC     3u
#define PSVR_VDB_PRESENT_ZERO_SHARED_HANDLE 4u
#define PSVR_VDB_PRESENT_NONFINITE_TIME    5u

// Wire layout of vr::PresentInfo_t, restated independently for the caller,
// which has no SDK header. The fixture static_asserts these against the real
// SDK struct, so a drift fails at compile time inside the DLL, not silently.
#define PSVR_VDB_WIRE_BYTES   32u
#define PSVR_VDB_WIRE_OFFSET_HANDLE 0u  // uint64 SharedTextureHandle_t
#define PSVR_VDB_WIRE_OFFSET_VSYNC  8u  // 4-byte EVSync, offsets 12..15 are padding
#define PSVR_VDB_WIRE_OFFSET_FRAME  16u // uint64 nFrameId
#define PSVR_VDB_WIRE_OFFSET_TIME   24u // double flVSyncTimeInSeconds
#define PSVR_VDB_INVALID_SHARED_HANDLE_BYTES 0ull // == INVALID_SHARED_TEXTURE_HANDLE

// Synthetic fixture timing value (debug mode GetTime). Test data only - this
// float never comes from, and must not be described as, display hardware.
#define PSVR_VDB_SYNTHETIC_SECONDS_F (1.0 / 120.0)

// ---------------------------------------------------------------------------
// Probe POD: returned by psvr_vdb_abi_probe before anything else is invoked.
// It pins sizes, offsets, slot numbering, enum range, version string and the
// no-virtual-destructor fact across the boundary. Plain fixed-layout POD,
// natural alignment only, both units assert every offset and the total size.
// ---------------------------------------------------------------------------
struct psvr_vdb_abi_probe_t {
    std::uint32_t struct_size;              // 0
    std::uint32_t magic;                    // 4
    std::uint32_t export_abi_version;       // 8
    std::uint32_t sizeof_pointer;           // 12
    std::uint32_t sizeof_trace;             // 16
    std::uint32_t sizeof_probe;             // 20
    std::uint32_t sizeof_present_info;      // 24
    std::uint32_t sizeof_vsync_enum;        // 28
    std::uint32_t slot_count;               // 32
    std::uint32_t slot_present;             // 36
    std::uint32_t slot_wait;                // 40
    std::uint32_t slot_get_time;            // 44
    std::uint32_t has_virtual_destructor;   // 48
    std::uint32_t offset_handle;            // 52
    std::uint32_t offset_vsync;             // 56
    std::uint32_t offset_frame;             // 60
    std::uint32_t offset_time;              // 64
    std::uint32_t evsync_min;               // 68
    std::uint32_t evsync_max;               // 72
    std::uint32_t build_is_msvc;            // 76
    std::uint32_t build_msvc_cpp_abi;       // 80  1 only when MSVC itself compiled the DLL
    std::uint32_t reserved0;                // 84
    std::uint64_t invalid_shared_handle;    // 88
    std::uint32_t reserved1;                // 96
    std::uint32_t reserved2;                // 100
    char interface_version[32];             // 104, NUL-terminated
};
#define PSVR_VDB_PROBE_BYTES 136u
static_assert(sizeof(psvr_vdb_abi_probe_t) == PSVR_VDB_PROBE_BYTES, "probe POD size changed");
static_assert(alignof(psvr_vdb_abi_probe_t) == 8, "probe POD alignment changed");
static_assert(offsetof(psvr_vdb_abi_probe_t, struct_size) == 0, "probe layout: struct_size");
static_assert(offsetof(psvr_vdb_abi_probe_t, magic) == 4, "probe layout: magic");
static_assert(offsetof(psvr_vdb_abi_probe_t, export_abi_version) == 8, "probe layout: export_abi_version");
static_assert(offsetof(psvr_vdb_abi_probe_t, sizeof_pointer) == 12, "probe layout: sizeof_pointer");
static_assert(offsetof(psvr_vdb_abi_probe_t, sizeof_trace) == 16, "probe layout: sizeof_trace");
static_assert(offsetof(psvr_vdb_abi_probe_t, sizeof_probe) == 20, "probe layout: sizeof_probe");
static_assert(offsetof(psvr_vdb_abi_probe_t, sizeof_present_info) == 24, "probe layout: sizeof_present_info");
static_assert(offsetof(psvr_vdb_abi_probe_t, sizeof_vsync_enum) == 28, "probe layout: sizeof_vsync_enum");
static_assert(offsetof(psvr_vdb_abi_probe_t, slot_count) == 32, "probe layout: slot_count");
static_assert(offsetof(psvr_vdb_abi_probe_t, slot_present) == 36, "probe layout: slot_present");
static_assert(offsetof(psvr_vdb_abi_probe_t, slot_wait) == 40, "probe layout: slot_wait");
static_assert(offsetof(psvr_vdb_abi_probe_t, slot_get_time) == 44, "probe layout: slot_get_time");
static_assert(offsetof(psvr_vdb_abi_probe_t, has_virtual_destructor) == 48, "probe layout: has_virt_dtor");
static_assert(offsetof(psvr_vdb_abi_probe_t, offset_handle) == 52, "probe layout: offset_handle");
static_assert(offsetof(psvr_vdb_abi_probe_t, offset_vsync) == 56, "probe layout: offset_vsync");
static_assert(offsetof(psvr_vdb_abi_probe_t, offset_frame) == 60, "probe layout: offset_frame");
static_assert(offsetof(psvr_vdb_abi_probe_t, offset_time) == 64, "probe layout: offset_time");
static_assert(offsetof(psvr_vdb_abi_probe_t, evsync_min) == 68, "probe layout: evsync_min");
static_assert(offsetof(psvr_vdb_abi_probe_t, evsync_max) == 72, "probe layout: evsync_max");
static_assert(offsetof(psvr_vdb_abi_probe_t, build_is_msvc) == 76, "probe layout: build_is_msvc");
static_assert(offsetof(psvr_vdb_abi_probe_t, build_msvc_cpp_abi) == 80, "probe layout: build_msvc_cpp_abi");
static_assert(offsetof(psvr_vdb_abi_probe_t, invalid_shared_handle) == 88, "probe layout: invalid_handle");
static_assert(offsetof(psvr_vdb_abi_probe_t, interface_version) == 104, "probe layout: interface_version");

// ---------------------------------------------------------------------------
// Trace POD: the fixture's bounded, plain-data call journal. It is copied
// out only through fixed-layout out-parameters (create/trace_read/destroy),
// never by leaking pointers to live state, so the caller can guard it with
// canaries and rescan raw bytes. Counters are fixture-side facts; the
// compositor frame id and the internal synthetic vsync stream stay distinct.
// ---------------------------------------------------------------------------
struct psvr_vdb_trace_t {
    std::uint32_t struct_size;              // 0
    std::uint32_t magic;                    // 4
    std::uint32_t config_flags;             // 8   config accepted at create
    std::uint32_t last_present_reason;      // 12  PSVR_VDB_PRESENT_*
    std::uint64_t object_address;           // 16  raw 'this' observed by the last slot call
    std::uint64_t accepted_presents;        // 24  gate-accepted Present invocations
    std::uint64_t rejected_presents;        // 32  gate-rejected Present invocations
    std::uint64_t wait_calls;               // 40  WaitForPresent invocations (counter only)
    std::uint64_t gettime_calls;            // 48  GetTimeSinceLastVsync invocations
    std::uint64_t last_compositor_frame_id; // 56  nFrameId of last accepted Present
    std::uint64_t last_shared_handle;       // 64  handle bytes of last accepted Present
    double last_vsync_time_seconds;         // 72  finite, from last accepted Present
    std::uint32_t last_vsync_raw;           // 80  EVSync enumerator 0..2, decoded from bytes
    std::uint32_t call_count;               // 84  total slot invocations on this fixture
    std::uint32_t slot_ring[PSVR_VDB_SLOT_RING_SIZE]; // 88 chronological last-8 slot ids,
                                                      //    SLOT_NONE-padded; rebuilt by the
                                                      //    fixture on export, never a raw
                                                      //    wrap-position dump
    std::uint32_t gettime_return_raw;       // 120 last bool returned, raw 0/1
    std::uint32_t reserved0;                // 124
    float gettime_seconds;                  // 128 last synthetic/zero seconds written
    std::uint32_t reserved1;                // 132
    std::uint64_t gettime_frame_counter;    // 136 last internal synthetic vsync stream value
};
#define PSVR_VDB_TRACE_BYTES 144u
static_assert(sizeof(psvr_vdb_trace_t) == PSVR_VDB_TRACE_BYTES, "trace POD size changed");
static_assert(alignof(psvr_vdb_trace_t) == 8, "trace POD alignment changed");
static_assert(offsetof(psvr_vdb_trace_t, struct_size) == 0, "trace layout: struct_size");
static_assert(offsetof(psvr_vdb_trace_t, magic) == 4, "trace layout: magic");
static_assert(offsetof(psvr_vdb_trace_t, config_flags) == 8, "trace layout: config_flags");
static_assert(offsetof(psvr_vdb_trace_t, last_present_reason) == 12, "trace layout: last_present_reason");
static_assert(offsetof(psvr_vdb_trace_t, object_address) == 16, "trace layout: object_address");
static_assert(offsetof(psvr_vdb_trace_t, accepted_presents) == 24, "trace layout: accepted_presents");
static_assert(offsetof(psvr_vdb_trace_t, rejected_presents) == 32, "trace layout: rejected_presents");
static_assert(offsetof(psvr_vdb_trace_t, wait_calls) == 40, "trace layout: wait_calls");
static_assert(offsetof(psvr_vdb_trace_t, gettime_calls) == 48, "trace layout: gettime_calls");
static_assert(offsetof(psvr_vdb_trace_t, last_compositor_frame_id) == 56, "trace layout: last_frame_id");
static_assert(offsetof(psvr_vdb_trace_t, last_shared_handle) == 64, "trace layout: last_shared_handle");
static_assert(offsetof(psvr_vdb_trace_t, last_vsync_time_seconds) == 72, "trace layout: last_vsync_time");
static_assert(offsetof(psvr_vdb_trace_t, last_vsync_raw) == 80, "trace layout: last_vsync_raw");
static_assert(offsetof(psvr_vdb_trace_t, call_count) == 84, "trace layout: call_count");
static_assert(offsetof(psvr_vdb_trace_t, slot_ring) == 88, "trace layout: slot_ring");
static_assert(offsetof(psvr_vdb_trace_t, gettime_return_raw) == 120, "trace layout: gettime_return_raw");
static_assert(offsetof(psvr_vdb_trace_t, gettime_seconds) == 128, "trace layout: gettime_seconds");
static_assert(offsetof(psvr_vdb_trace_t, gettime_frame_counter) == 136, "trace layout: gettime_frame_counter");

// Export declaration macro, used by the fixture translation unit only.
// dllexport attribute works on both MSVC and MinGW-w64 and keeps extern "C"
// names undecorated on x64.
#if defined(PSVR_VDB_BUILD_DLL)
#  if defined(_MSC_VER)
#    define PSVR_VDB_EXPORT extern "C" __declspec(dllexport)
#  else
#    define PSVR_VDB_EXPORT extern "C" __attribute__((dllexport))
#  endif
#else
#  define PSVR_VDB_EXPORT extern "C"
#endif
