// SPDX-License-Identifier: MIT
// Independent caller for the test-only boundary fixture DLL (see
// tests/virtual_display_boundary_fixture.cpp). This executable strengthens
// the IVRVirtualDisplay_002 ABI verification by crossing a real DLL boundary:
//   - it NEVER includes openvr_driver.h: interface shape is verified against
//     handwritten Windows x64 vtable slot signatures and the fixed-layout POD
//     journal from tests/virtual_display_boundary_api.hpp. The declarations
//     below being independent of the SDK is intentional; disagreement between
//     the two views fails loudly via the probe, never silently.
//   - it loads the fixture via LoadLibraryA on the FULL SUPPLIED PATH plus
//     GetProcAddress: no import library, no linker knowledge of the DLL, no
//     PATH-based fallback, nothing is registered anywhere.
//   - it invokes slots 0/1/2 as raw function pointers (this as first
//     argument), not via C++ virtual dispatch, and verifies the observed
//     order, this-pointer, bool, counters, and pointer outputs from outside.
//     Slot compatibility is demonstrated behaviorally (probe-pinned ids plus
//     journal-confirmed calls through three distinct live entries); no
//     full-ABI equivalence with any other compiler is assumed or claimed -
//     in particular MinGW's -mabi=ms is only a calling-convention switch.
//   - Present payloads are raw byte buffers with canaries on both sides; the
//     gate decisions come from the same psvr::copy_present_info parser the
//     driver contract pins, running inside the DLL. Source canaries prove
//     the fixture never WRITES past the 32-byte prefix; a Windows guard-page
//     regression (VirtualAlloc two pages, second PAGE_NOACCESS, wire placed
//     flush against the boundary) is what detects a READ past the prefix:
//     a correct call returns normally, any over-read trips the page and
//     crashes this test process, which is itself the regression signal.
//   - the opaque handle is trusted only to originate from psvr_vdb_create;
//     neither side pretends arbitrary pointers are authenticated across the
//     boundary, and the exports only reject nulls with distinct statuses.
// Honest scope: WaitForPresent inside the fixture is a counter only and
// GetTimeSinceLastVsync returns explicitly SYNTHETIC debug-configured test
// values; nothing here measures, emulates, or claims real vsync, scanout, or
// any hardware. No SteamVR, registry, UAC, or GPU contact. This TU is a
// separate, normally compiled translation unit (no LTO) and is never linked
// into production targets.
//
// Usage: psvr_virtual_display_boundary_tests.exe <full path to fixture DLL>
// Verified on: MinGW-w64 GCC 16.2.0 x86_64 only. The sources are written so
// MSVC can compile them later, but no MSVC/Windows SDK exists on this
// machine and no cross-compiler equivalence is claimed by this harness.
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include "virtual_display_boundary_api.hpp"

#include <cmath>
#include <cstdint>
#include <cstring>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

// ---------------------------------------------------------------------------
// Handwritten Windows x64 slot signatures for IVRVirtualDisplay_002. On
// Windows x64 there is a single calling convention and `this` arrives as the
// first integer argument, so a plain function pointer with a leading void*
// reproduces a virtual call. The bool return is read as a raw byte: C++ bool
// is guaranteed 0/1 in AL, upper register bits are never trusted.
// ---------------------------------------------------------------------------
using vdb_present_fn = void (*)(void* self, const void* present_info_bytes, std::uint32_t size_bytes);
using vdb_wait_fn = void (*)(void* self);
using vdb_get_time_fn = std::uint8_t (*)(void* self, float* seconds_since_last_vsync, std::uint64_t* frame_counter);

using vdb_abi_probe_fn = std::uint32_t (*)(psvr_vdb_abi_probe_t*);
using vdb_create_fn = std::uint32_t (*)(std::uint32_t, std::uint32_t, void**, psvr_vdb_trace_t*);
using vdb_trace_read_fn = std::uint32_t (*)(void*, psvr_vdb_trace_t*);
using vdb_destroy_fn = std::uint32_t (*)(void*, psvr_vdb_trace_t*);

static_assert(sizeof(void*) == 8, "caller must be built for x64");

// Canaries and raw storage, same discipline as the contract-test wire: bytes
// decide, padding is never trusted, every out-parameter lives inside guards.
constexpr std::uint8_t kCanarySource = 0x5A; // around compositor bytes
constexpr std::uint8_t kCanaryDest = 0xA5;   // around written destinations
constexpr std::uint8_t kPoison = 0x3C;       // untouched-before-write marker
constexpr std::uint8_t kUnknownTail = 0x77;  // oversized-tail sentinel

struct alignas(8) Wire {
    unsigned char before[16];
    unsigned char body[64]; // declared prefix (32) plus room to over-size it
    unsigned char after[16];
};
static_assert(offsetof(Wire, body) == 16, "wire guard layout changed");

void build_wire(Wire& wire, std::uint64_t handle, std::uint32_t vsync_raw,
                std::uint64_t frame_id, double vsync_time_seconds) {
    std::memset(wire.before, kCanarySource, sizeof wire.before);
    std::memset(wire.after, kCanarySource, sizeof wire.after);
    std::memset(wire.body, 0xCC, sizeof wire.body); // padding/future bytes
    std::memcpy(wire.body + PSVR_VDB_WIRE_OFFSET_HANDLE, &handle, sizeof handle);
    std::memcpy(wire.body + PSVR_VDB_WIRE_OFFSET_VSYNC, &vsync_raw, sizeof vsync_raw);
    std::memcpy(wire.body + PSVR_VDB_WIRE_OFFSET_FRAME, &frame_id, sizeof frame_id);
    std::memcpy(wire.body + PSVR_VDB_WIRE_OFFSET_TIME, &vsync_time_seconds, sizeof vsync_time_seconds);
}

bool wire_guards_intact(const Wire& wire) {
    for (unsigned char byte : wire.before) if (byte != kCanarySource) return false;
    for (unsigned char byte : wire.after) if (byte != kCanarySource) return false;
    return true;
}

struct alignas(8) ProbeBox {
    unsigned char before[16];
    psvr_vdb_abi_probe_t probe;
    unsigned char after[16];
};
struct alignas(8) TraceBox {
    unsigned char before[16];
    psvr_vdb_trace_t trace;
    unsigned char after[16];
};
// GetTime pointer outputs, guarded so writes can only land on the two fields.
struct alignas(8) TimeOutputs {
    unsigned char before[16];
    float seconds;
    std::uint64_t counter;
    unsigned char after[16];
};

bool guards_intact(const unsigned char* before, const unsigned char* after,
                   std::size_t before_size, std::size_t after_size, std::uint8_t marker) {
    for (std::size_t i = 0; i < before_size; ++i) if (before[i] != marker) return false;
    for (std::size_t i = 0; i < after_size; ++i) if (after[i] != marker) return false;
    return true;
}

template <typename Box, typename Payload>
bool box_poison_untouched(const Box& box, const Payload& payload) {
    if (!guards_intact(box.before, box.after, sizeof box.before, sizeof box.after, kPoison)) return false;
    const auto* bytes = static_cast<const unsigned char*>(static_cast<const void*>(&payload));
    for (std::size_t i = 0; i < sizeof payload; ++i) if (bytes[i] != kPoison) return false;
    return true;
}

bool trace_bytes_untouched(const TraceBox& box) {
    return box_poison_untouched(box, box.trace);
}

// ---------------------------------------------------------------------------
// RAII wrappers, declared in the right order by the test body so fixture
// handles are destroyed through the DLL's OWN destroy export before the
// module is freed. Nothing is ever deleted through a vr::IVRVirtualDisplay*.
// ---------------------------------------------------------------------------
class Library final {
    HMODULE module_ = nullptr;
    std::string path_;
public:
    explicit Library(const std::string& path) : path_(path) {
        // LoadLibraryA takes the full supplied path verbatim: no PATH search,
        // no side-loading. Failure here is a hard, clearly labelled error.
        module_ = LoadLibraryA(path.c_str());
        if (module_ == nullptr) {
            throw std::runtime_error("LoadLibraryA failed for supplied path '" + path +
                                     "', Windows error=" + std::to_string(GetLastError()));
        }
    }
    Library(const Library&) = delete;
    Library& operator=(const Library&) = delete;
    void* address_of(const char* name) {
        FARPROC symbol = GetProcAddress(module_, name);
        if (symbol == nullptr) {
            throw std::runtime_error(std::string("GetProcAddress failed for '") + name +
                                     "', Windows error=" + std::to_string(GetLastError()));
        }
        return reinterpret_cast<void*>(symbol);
    }
    // Raw module handle, only for the deliberate missing-export probe below.
    HMODULE handle() const { return module_; }
    // Explicit, checked unload; the destructor is only a fail-safe net.
    void unload_checked() {
        check(module_ != nullptr, "unload called twice");
        const BOOL freed = FreeLibrary(module_);
        module_ = nullptr;
        check(freed != 0, "FreeLibrary failed for '" + path_ +
                         "', Windows error=" + std::to_string(GetLastError()));
    }
    ~Library() { if (module_ != nullptr) FreeLibrary(module_); }
};

class FixtureGuard final {
    void* handle_ = nullptr;
    vdb_destroy_fn destroy_ = nullptr;
public:
    FixtureGuard(void* handle, vdb_destroy_fn destroy) : handle_(handle), destroy_(destroy) {}
    FixtureGuard(const FixtureGuard&) = delete;
    FixtureGuard& operator=(const FixtureGuard&) = delete;
    void release() { handle_ = nullptr; } // destroyed explicitly by the test
    ~FixtureGuard() {
        if (handle_ != nullptr) {
            psvr_vdb_trace_t sink{};
            if (destroy_(handle_, &sink) != PSVR_VDB_STATUS_OK) {
                std::cerr << "FATAL: fixture destroy failed during unwinding; fail closed\n";
                std::abort();
            }
        }
    }
};

// Two committed pages whose second is PAGE_NOACCESS, host test memory only
// (no GPU, no shared texture): a 32-byte wire placed flush against the
// boundary turns any read past the declared prefix into an access violation
// of THIS test process. Detection model: correct behavior returns normally;
// an over-reading fixture crashes the suite, and a crashing test is the
// failing regression. There is no attempt to catch and resume from an AV.
class GuardedPages final {
    unsigned char* base_ = nullptr;
    std::size_t page_size_ = 0;
public:
    GuardedPages() {
        SYSTEM_INFO info{};
        GetSystemInfo(&info);
        page_size_ = info.dwPageSize;
        check(page_size_ >= 2 * PSVR_VDB_WIRE_BYTES, "page too small for guard layout");
        base_ = static_cast<unsigned char*>(
            VirtualAlloc(nullptr, 2 * page_size_, MEM_COMMIT, PAGE_READWRITE));
        if (base_ == nullptr) {
            throw std::runtime_error("guard test: VirtualAlloc failed, Windows error=" +
                                     std::to_string(GetLastError()));
        }
        DWORD old_protection = 0;
        const BOOL protected_second =
            VirtualProtect(base_ + page_size_, page_size_, PAGE_NOACCESS, &old_protection);
        if (protected_second == 0 || old_protection != PAGE_READWRITE) {
            throw std::runtime_error("guard test: VirtualProtect(PAGE_NOACCESS) failed, Windows error=" +
                                     std::to_string(GetLastError()));
        }
    }
    GuardedPages(const GuardedPages&) = delete;
    GuardedPages& operator=(const GuardedPages&) = delete;
    // Last 32 writable bytes of the readable page, 8-byte aligned, touching
    // the no-access boundary immediately after the prefix.
    unsigned char* flush_prefix32() const { return base_ + page_size_ - PSVR_VDB_WIRE_BYTES; }
    // First byte of the PAGE_NOACCESS page: any read here faults immediately,
    // so calls rejected BEFORE reading return normally while a gate that
    // reads first crashes this process - the regression signal.
    unsigned char* inaccessible() const { return base_ + page_size_; }
    ~GuardedPages() {
        if (base_ != nullptr && VirtualFree(base_, 0, MEM_RELEASE) == 0) {
            std::cerr << "FATAL: VirtualFree of guard pages failed; fail closed\n";
            std::abort();
        }
    }
};

// Journal window check against the full scripted call list. The exported
// slot_ring is the fixture's CHRONOLOGICAL view: with N calls it holds
// script[N-window+i] for i < window = min(N, 8), then SLOT_NONE padding.
// The caller recomputes the same window here; if the fixture ever leaked
// raw wrap positions instead of order, mixed-slot scripts with N > 8 (the
// tail of script_a ends ...W,G) fail loudly.
void check_ring(const psvr_vdb_trace_t& trace, const std::vector<std::uint32_t>& script,
                const std::string& label) {
    check(trace.call_count == static_cast<std::uint32_t>(script.size()),
          "call_count mismatch after " + label);
    const std::size_t total = script.size();
    const std::size_t window = total <= PSVR_VDB_SLOT_RING_SIZE ? total : PSVR_VDB_SLOT_RING_SIZE;
    const std::size_t start = total - window;
    for (std::size_t i = 0; i < PSVR_VDB_SLOT_RING_SIZE; ++i) {
        const std::uint32_t want = (i < window) ? script[start + i] : PSVR_VDB_SLOT_NONE;
        check(trace.slot_ring[i] == want, "slot_ring[" + std::to_string(i) + "] wrong after " + label);
    }
}

// One explicit journal read through the export, guarded and validated.
psvr_vdb_trace_t read_trace(vdb_trace_read_fn trace_read, void* handle, const std::string& label) {
    TraceBox box;
    std::memset(box.before, kPoison, sizeof box.before);
    std::memset(box.after, kPoison, sizeof box.after);
    std::memset(static_cast<unsigned char*>(static_cast<void*>(&box.trace)), kPoison, sizeof box.trace);
    const std::uint32_t status = trace_read(handle, &box.trace);
    check(status == PSVR_VDB_STATUS_OK, "trace_read failed after " + label);
    check(guards_intact(box.before, box.after, sizeof box.before, sizeof box.after, kPoison),
          "trace write escaped its guards after " + label);
    check(box.trace.struct_size == PSVR_VDB_TRACE_BYTES, "trace struct_size wrong after " + label);
    check(box.trace.magic == PSVR_VDB_TRACE_MAGIC, "trace magic wrong after " + label);
    return box.trace;
}

// Test values. Handles/frames carry the high bit so a silent 32-bit truncation
// anywhere across the boundary becomes an inequality the tests immediately see.
const std::uint64_t kGoodHandle1 = 0x8122334455667788ull; // high-bit 64-bit handle
const std::uint64_t kGoodHandle2 = 0xFEDCBA9887654321ull; // high-bit, distinct
const std::uint32_t kGoodVSync1 = 1u;                     // VSync_WaitRender
const std::uint32_t kGoodVSync2 = 2u;                     // VSync_NoWaitRender
const std::uint64_t kGoodFrame1 = 0x8000000000001234ull;  // high-bit 64-bit compositor frame id
const std::uint64_t kGoodFrame2 = 4243ull;
const double kGoodTime1 = 1.0 / 120.0;
const double kGoodTime2 = 1.0 / 60.0;

} // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: psvr_virtual_display_boundary_tests.exe <full path to fixture DLL>\n";
        return 2;
    }
    const std::string dll_path = argv[1];
    try {
        // 1. Wrong-path load must fail with its own clear, distinct error,
        // before any real module is involved.
        const std::string wrong_path = dll_path + ".definitely-not-a-module";
        HMODULE stray = LoadLibraryA(wrong_path.c_str());
        const DWORD stray_error = GetLastError();
        check(stray == nullptr, "wrong-path LoadLibraryA unexpectedly succeeded: " + wrong_path);
        check(stray_error == ERROR_MOD_NOT_FOUND,
              "wrong-path LoadLibraryA must fail with ERROR_MOD_NOT_FOUND(126), got " +
                  std::to_string(stray_error));
        std::cout << "info: wrong-path LoadLibraryA rejected with error 126 ERROR_MOD_NOT_FOUND\n";

        // 2. Load the fixture DLL from the full supplied path. RAII: freed
        // only after every fixture handle has gone through the destroy export.
        Library library(dll_path);
        const auto probe_fn = reinterpret_cast<vdb_abi_probe_fn>(library.address_of(PSVR_VDB_ABI_PROBE_NAME));
        const auto create_fn = reinterpret_cast<vdb_create_fn>(library.address_of(PSVR_VDB_CREATE_NAME));
        const auto trace_read_fn =
            reinterpret_cast<vdb_trace_read_fn>(library.address_of(PSVR_VDB_TRACE_READ_NAME));
        const auto destroy_fn = reinterpret_cast<vdb_destroy_fn>(library.address_of(PSVR_VDB_DESTROY_NAME));

        // A bogus export name fails with its own clear error too: nothing is
        // ever silently substituted, and PATH is not consulted at all.
        check(GetProcAddress(library.handle(), "psvr_vdb_definitely_not_exported") == nullptr,
              "missing export unexpectedly resolved");
        check(GetLastError() == ERROR_PROC_NOT_FOUND,
              "missing export must report ERROR_PROC_NOT_FOUND(127), got " + std::to_string(GetLastError()));
        std::cout << "info: missing-export probe rejected with error 127 ERROR_PROC_NOT_FOUND\n";

        // 3. Probe the fixed-layout ABI POD before touching anything else.
        ProbeBox probe_box;
        std::memset(probe_box.before, kPoison, sizeof probe_box.before);
        std::memset(probe_box.after, kPoison, sizeof probe_box.after);
        std::memset(static_cast<unsigned char*>(static_cast<void*>(&probe_box.probe)), kPoison,
                    sizeof probe_box.probe);
        check(probe_fn(&probe_box.probe) == PSVR_VDB_STATUS_OK, "abi_probe call failed");
        check(guards_intact(probe_box.before, probe_box.after, sizeof probe_box.before,
                            sizeof probe_box.after, kPoison),
              "abi_probe wrote outside its fixed-layout struct");
        const psvr_vdb_abi_probe_t& abi = probe_box.probe;
        check(abi.struct_size == PSVR_VDB_PROBE_BYTES &&
                  abi.struct_size == static_cast<std::uint32_t>(sizeof(psvr_vdb_abi_probe_t)),
              "ABI: probe struct_size disagrees with both sides");
        check(abi.magic == PSVR_VDB_PROBE_MAGIC, "ABI: probe magic");
        check(abi.export_abi_version == PSVR_VDB_EXPORT_ABI_VERSION, "ABI: export ABI revision pinned");
        check(abi.sizeof_pointer == 8u && abi.sizeof_pointer == static_cast<std::uint32_t>(sizeof(void*)),
              "ABI: pointer width 8");
        check(abi.sizeof_trace == sizeof(psvr_vdb_trace_t), "ABI: trace POD size agrees across DLL");
        check(abi.sizeof_probe == sizeof(psvr_vdb_abi_probe_t), "ABI: probe POD size agrees across DLL");
        check(abi.sizeof_present_info == PSVR_VDB_WIRE_BYTES, "ABI: PresentInfo_t is 32 bytes across DLL");
        check(abi.sizeof_vsync_enum == 4u, "ABI: EVSync stored in 4 bytes");
        check(abi.slot_count == PSVR_VDB_SLOT_COUNT && abi.slot_present == 0u && abi.slot_wait == 1u &&
                  abi.slot_get_time == 2u,
              "ABI: three slots, order Present/WaitForPresent/GetTimeSinceLastVsync");
        check(abi.has_virtual_destructor == 0u,
              "ABI: pinned interface has no virtual destructor (slot base would shift)");
        check(abi.offset_handle == 0u && abi.offset_vsync == 8u && abi.offset_frame == 16u &&
                  abi.offset_time == 24u,
              "ABI: wire offsets 0/8/16/24 agree with the handwritten view");
        check(abi.evsync_min == 0u && abi.evsync_max == 2u, "ABI: EVSync enumerator range 0..2");
        check(abi.invalid_shared_handle == PSVR_VDB_INVALID_SHARED_HANDLE_BYTES,
              "ABI: INVALID_SHARED_TEXTURE_HANDLE is 0 across the boundary");
        check(std::strcmp(abi.interface_version, PSVR_VDB_INTERFACE_VERSION_STRING) == 0,
              "ABI: SDK-pinned version string equals IVRVirtualDisplay_002");
        check(abi.build_is_msvc <= 1u && abi.build_msvc_cpp_abi <= 1u, "ABI: build tags are 0/1");
        std::cout << "info: fixture build tags msvc=" << abi.build_is_msvc
                  << " msvc_cpp_abi=" << abi.build_msvc_cpp_abi
                  << " (set only from _MSC_VER; slot compatibility here is demonstrated"
                     " behaviorally, never inferred from compiler versions)\n";

        // 4. Fixture creation rejects malformed requests without writing
        // anywhere: poisoned destinations must stay byte-for-byte poison.
        for (const std::uint32_t bad_version : {0u, 1u, 3u, 0xFFFFFFFFu}) {
            TraceBox box;
            void* out_handle = reinterpret_cast<void*>(0xDEADBEEFDEADBEEFull);
            std::memset(box.before, kPoison, sizeof box.before);
            std::memset(box.after, kPoison, sizeof box.after);
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&box.trace)), kPoison, sizeof box.trace);
            check(create_fn(bad_version, 0u, &out_handle, &box.trace) == PSVR_VDB_STATUS_BAD_VERSION,
                  "create rejects version " + std::to_string(bad_version));
            check(out_handle == reinterpret_cast<void*>(0xDEADBEEFDEADBEEFull),
                  "rejected create wrote through out_handle");
            check(trace_bytes_untouched(box), "rejected create wrote through out_trace");
        }
        for (const std::uint32_t bad_config : {2u, 0x80000000u, 0xFFFFFFFFu}) {
            TraceBox box;
            void* out_handle = reinterpret_cast<void*>(0xDEADBEEFDEADBEEFull);
            std::memset(box.before, kPoison, sizeof box.before);
            std::memset(box.after, kPoison, sizeof box.after);
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&box.trace)), kPoison, sizeof box.trace);
            check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, bad_config, &out_handle, &box.trace) ==
                      PSVR_VDB_STATUS_BAD_CONFIG,
                  "create rejects config bits " + std::to_string(bad_config));
            check(out_handle == reinterpret_cast<void*>(0xDEADBEEFDEADBEEFull),
                  "config-rejected create wrote through out_handle");
            check(trace_bytes_untouched(box), "config-rejected create wrote through out_trace");
        }
        {
            TraceBox box;
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&box.trace)), kPoison, sizeof box.trace);
            void* sink_handle = nullptr;
            check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, 1u, nullptr, &box.trace) ==
                      PSVR_VDB_STATUS_NULL_OUTPUT,
                  "create rejects a null out_handle");
            check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, 1u, &sink_handle, nullptr) ==
                      PSVR_VDB_STATUS_NULL_OUTPUT,
                  "create rejects a null out_trace");
            check(sink_handle == nullptr, "null-out_trace rejection still allocated a fixture");
        }

        // 5. Create the synthetic-timing fixture (config bit 0 set).
        void* handle_a = nullptr;
        TraceBox created;
        std::memset(created.before, kPoison, sizeof created.before);
        std::memset(created.after, kPoison, sizeof created.after);
        std::memset(static_cast<unsigned char*>(static_cast<void*>(&created.trace)), kPoison,
                    sizeof created.trace);
        check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, PSVR_VDB_CFG_TIMING_SYNTHETIC, &handle_a,
                         &created.trace) == PSVR_VDB_STATUS_OK,
              "create accepts version 002 with synthetic timing");
        check(handle_a != nullptr, "accepted create returned a null handle");
        // Take ownership BEFORE any further assert or read: every failing
        // check on the lines below still unwinds through the DLL's own
        // destroy export (never via a vr::IVRVirtualDisplay*, never via a
        // guessed vtable destructor slot), and guard_a is declared after
        // `library`, so it is destroyed before the module is freed.
        FixtureGuard guard_a(handle_a, destroy_fn);
        check(guards_intact(created.before, created.after, sizeof created.before, sizeof created.after,
                            kPoison),
              "accepted create wrote outside its trace guards");
        check(created.trace.object_address == static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(handle_a)),
              "create journal records the object address as the raw this value");
        check(created.trace.call_count == 0u && created.trace.accepted_presents == 0u &&
                  created.trace.rejected_presents == 0u && created.trace.wait_calls == 0u &&
                  created.trace.gettime_calls == 0u,
              "fresh fixture has zeroed counters");
        for (std::uint32_t i = 0; i < PSVR_VDB_SLOT_RING_SIZE; ++i)
            check(created.trace.slot_ring[i] == PSVR_VDB_SLOT_NONE, "fresh journal ring is empty");

        // Explicit journal read through the export before any slot call.
        psvr_vdb_trace_t trace = read_trace(trace_read_fn, handle_a, "create");
        check(trace.config_flags == PSVR_VDB_CFG_TIMING_SYNTHETIC, "journal echoes synthetic timing config");

        // 6. Read exactly three vtable slot entries - one word of the object
        // (the vptr) and the three pointers behind it - without copying any
        // vtable and without reading beyond index 2 (no destructor or extra
        // slot is guessed). Why these indices are trusted is behavioral, not
        // an ABI claim: the probe pinned slot ids 0/1/2 and slot_count 3, the
        // entries must be three distinct live pointers, and each call below
        // is confirmed through the fixture's own journal, which records the
        // slot id the DLL-side method observed.
        void** object_vtable = *reinterpret_cast<void***>(handle_a);
        void* slot0 = object_vtable[PSVR_VDB_SLOT_PRESENT];
        void* slot1 = object_vtable[PSVR_VDB_SLOT_WAIT];
        void* slot2 = object_vtable[PSVR_VDB_SLOT_GET_TIME];
        check(slot0 != nullptr && slot1 != nullptr && slot2 != nullptr, "vtable slots must be real");
        check(slot0 != slot1 && slot1 != slot2 && slot0 != slot2,
              "the three pinned slots must be three distinct entries (no aliasing, no purecall)");
        const auto present_slot = reinterpret_cast<vdb_present_fn>(slot0);
        const auto wait_slot = reinterpret_cast<vdb_wait_fn>(slot1);
        const auto get_time_slot = reinterpret_cast<vdb_get_time_fn>(slot2);

        Wire wire;

        // 7. Three-slot order. Present (slot 0), then WaitForPresent (slot
        // 1), then GetTimeSinceLastVsync (slot 2), verified journal-wise.
        build_wire(wire, kGoodHandle1, kGoodVSync1, kGoodFrame1, kGoodTime1);
        present_slot(handle_a, wire.body, PSVR_VDB_WIRE_BYTES);
        check(wire_guards_intact(wire), "Present damaged the source canaries");
        trace = read_trace(trace_read_fn, handle_a, "first present");
        check(trace.accepted_presents == 1u && trace.rejected_presents == 0u, "first present accepted");
        check(trace.last_shared_handle == kGoodHandle1, "high-bit 64-bit handle crossed intact");
        check(trace.last_compositor_frame_id == kGoodFrame1, "high-bit 64-bit frame id crossed intact");
        check(trace.last_vsync_raw == kGoodVSync1, "EVSync enumerator crossed as raw value 1");
        check(trace.last_vsync_time_seconds == kGoodTime1 && std::isfinite(trace.last_vsync_time_seconds),
              "finite double crossed bit-exact");
        check(trace.last_present_reason == PSVR_VDB_PRESENT_ACCEPTED, "reason codes ACCEPTED");
        check(trace.object_address == static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(handle_a)),
              "slot 0 received the untouched this pointer");
        check_ring(trace, {0}, "slot 0 invocation");

        wait_slot(handle_a);
        trace = read_trace(trace_read_fn, handle_a, "first wait");
        check(trace.wait_calls == 1u, "WaitForPresent is counter-only: 1");
        check_ring(trace, {0, 1}, "slot 1 invocation");

        TimeOutputs outputs;
        std::memset(outputs.before, kCanaryDest, sizeof outputs.before);
        std::memset(outputs.after, kCanaryDest, sizeof outputs.after);
        outputs.seconds = 123.0f;
        outputs.counter = 999u;
        const std::uint8_t ret1 = get_time_slot(handle_a, &outputs.seconds, &outputs.counter);
        check(ret1 == 1u, "debug-configured GetTime returns raw true (1)");
        check(guards_intact(outputs.before, outputs.after, sizeof outputs.before, sizeof outputs.after,
                            kCanaryDest),
              "GetTime wrote outside its two output pointers");
        check(outputs.seconds == static_cast<float>(PSVR_VDB_SYNTHETIC_SECONDS_F),
              "GetTime seconds are the SYNTHETIC test constant, nothing real");
        check(outputs.counter == 1u, "GetTime counter is the fixture-private stream, not a compositor id");
        check(outputs.counter != kGoodFrame1, "synthetic stream never mirrors the compositor frame id");
        trace = read_trace(trace_read_fn, handle_a, "first gettime");
        check(trace.gettime_calls == 1u && trace.gettime_return_raw == 1u, "journal saw the true bool");
        check_ring(trace, {0, 1, 2}, "three-slot order Present/Wait/GetTime verified");

        // 8. Repeated calls: waits accumulate, the synthetic stream advances,
        // and nothing else moves.
        wait_slot(handle_a);
        wait_slot(handle_a);
        outputs.seconds = 123.0f;
        outputs.counter = 999u;
        const std::uint8_t ret2 = get_time_slot(handle_a, &outputs.seconds, &outputs.counter);
        check(ret2 == 1u && outputs.counter == 2u, "repeated GetTime advances only the synthetic stream");
        trace = read_trace(trace_read_fn, handle_a, "repeated calls");
        check(trace.wait_calls == 3u && trace.gettime_calls == 2u, "repeated-call counters exact");
        check(trace.accepted_presents == 1u && trace.rejected_presents == 0u,
              "repeat path left present counters alone");
        check_ring(trace, {0, 1, 2, 1, 1, 2}, "journal order after repeats");

        // 9. Null / short / oversized sources through the SAME parser.
        present_slot(handle_a, nullptr, PSVR_VDB_WIRE_BYTES);
        trace = read_trace(trace_read_fn, handle_a, "null present");
        check(trace.rejected_presents == 1u && trace.accepted_presents == 1u, "null source rejected");
        check(trace.last_present_reason == PSVR_VDB_PRESENT_NULL_SOURCE, "reason NULL_SOURCE");
        check(trace.last_shared_handle == kGoodHandle1 && trace.last_compositor_frame_id == kGoodFrame1,
              "null-source rejection preserved the accepted snapshot");

        present_slot(handle_a, wire.body, 16u);
        trace = read_trace(trace_read_fn, handle_a, "16-byte present");
        check(trace.rejected_presents == 2u &&
                  trace.last_present_reason == PSVR_VDB_PRESENT_BUFFER_TOO_SMALL,
              "short source rejected as too small");
        present_slot(handle_a, wire.body, PSVR_VDB_WIRE_BYTES - 1u);
        trace = read_trace(trace_read_fn, handle_a, "31-byte present");
        check(trace.rejected_presents == 3u &&
                  trace.last_present_reason == PSVR_VDB_PRESENT_BUFFER_TOO_SMALL,
              "size 31 rejected as too small (exact prefix rule)");
        check(trace.accepted_presents == 1u && trace.last_shared_handle == kGoodHandle1 &&
                  trace.last_compositor_frame_id == kGoodFrame1,
              "short rejections preserved the accepted snapshot and counters");

        // Oversized: 64 declared bytes, distinct good prefix, 0x77 sentinel
        // tail. The sentinel scan proves the fixture never WRITES the source
        // past the 32-byte prefix; whether anything is READ past it cannot be
        // seen from here. That half is covered by the dedicated guard-page
        // regression (section 11b), where a read past the prefix would fault
        // against PAGE_NOACCESS memory.
        build_wire(wire, kGoodHandle2, kGoodVSync2, kGoodFrame2, kGoodTime2);
        std::memset(wire.body + PSVR_VDB_WIRE_BYTES, kUnknownTail,
                    sizeof wire.body - PSVR_VDB_WIRE_BYTES);
        present_slot(handle_a, wire.body, static_cast<std::uint32_t>(sizeof wire.body));
        check(wire_guards_intact(wire), "oversized present damaged source guards");
        for (std::size_t i = PSVR_VDB_WIRE_BYTES; i < sizeof wire.body; ++i)
            check(wire.body[i] == kUnknownTail, "oversized tail was rewritten by the fixture");
        trace = read_trace(trace_read_fn, handle_a, "oversized present");
        check(trace.accepted_presents == 2u && trace.last_present_reason == PSVR_VDB_PRESENT_ACCEPTED,
              "oversized source accepted as forward-compatible prefix");
        check(trace.last_shared_handle == kGoodHandle2 && trace.last_compositor_frame_id == kGoodFrame2 &&
                  trace.last_vsync_raw == kGoodVSync2 && trace.last_vsync_time_seconds == kGoodTime2,
              "only the first 32 bytes were consumed by the gate");

        // 10. Invalid raw enum / non-finite / zero handle rejections must
        // preserve the accepted snapshot (the oversized prefix above).
        const double nan_value = std::numeric_limits<double>::quiet_NaN();
        const double positive_infinity = std::numeric_limits<double>::infinity();
        const double negative_infinity = -std::numeric_limits<double>::infinity();
        struct BadCase {
            std::uint64_t handle;
            std::uint32_t vsync_raw;
            double time_value;
            std::uint32_t want_reason;
            const char* label;
        };
        const std::uint64_t good_frame = kGoodFrame2;
        const BadCase bad_cases[] = {
            {kGoodHandle2, 3u, kGoodTime2, PSVR_VDB_PRESENT_UNKNOWN_VSYNC, "EVSync raw 3"},
            {kGoodHandle2, 0xFFFFFFFFu, kGoodTime2, PSVR_VDB_PRESENT_UNKNOWN_VSYNC, "EVSync raw 0xFFFFFFFF"},
            {PSVR_VDB_INVALID_SHARED_HANDLE_BYTES, kGoodVSync2, kGoodTime2,
             PSVR_VDB_PRESENT_ZERO_SHARED_HANDLE, "zero shared handle"},
            {kGoodHandle2, kGoodVSync2, nan_value, PSVR_VDB_PRESENT_NONFINITE_TIME, "NaN vsync time"},
            {kGoodHandle2, kGoodVSync2, positive_infinity, PSVR_VDB_PRESENT_NONFINITE_TIME, "+Inf vsync time"},
            {kGoodHandle2, kGoodVSync2, negative_infinity, PSVR_VDB_PRESENT_NONFINITE_TIME, "-Inf vsync time"},
        };
        for (const BadCase& bad : bad_cases) {
            build_wire(wire, bad.handle, bad.vsync_raw, good_frame, bad.time_value);
            present_slot(handle_a, wire.body, PSVR_VDB_WIRE_BYTES);
            check(wire_guards_intact(wire), std::string("guards damaged by ") + bad.label);
            trace = read_trace(trace_read_fn, handle_a, bad.label);
            check(trace.last_present_reason == bad.want_reason,
                  std::string("wrong reason for ") + bad.label);
            check(trace.accepted_presents == 2u, std::string("accepted count moved on ") + bad.label);
            check(trace.last_shared_handle == kGoodHandle2 &&
                      trace.last_compositor_frame_id == kGoodFrame2 &&
                      trace.last_vsync_raw == kGoodVSync2 &&
                      trace.last_vsync_time_seconds == kGoodTime2,
                  std::string("rejection overwrote the accepted snapshot: ") + bad.label);
        }
        // Mixed-slot regression across the wrap: with 16 calls the window is
        // eight identical Present ids, so a raw wrap-position dump would
        // still match by luck. Calls 17 (Wait) and 18 (GetTime) put different
        // slot values at ring positions 0 and 1, exactly where physical
        // modulo-8 storage would place them FIRST if fill_trace ever leaked
        // wrap order instead of reconstructing chronology.
        wait_slot(handle_a);
        outputs.seconds = 123.0f;
        outputs.counter = 999u;
        const std::uint8_t ret_wrap = get_time_slot(handle_a, &outputs.seconds, &outputs.counter);
        check(ret_wrap == 1u && outputs.counter == 3u,
              "wrap-regression GetTime advanced only the synthetic stream to 3");
        trace = read_trace(trace_read_fn, handle_a, "full script on A");
        check(trace.rejected_presents == 9u, "nine gate rejections total on fixture A");
        check(trace.wait_calls == 4u && trace.gettime_calls == 3u && trace.gettime_return_raw == 1u,
              "A counters after full script");
        check(trace.gettime_seconds == static_cast<float>(PSVR_VDB_SYNTHETIC_SECONDS_F) &&
                  trace.gettime_frame_counter == 3u,
              "journal echoes the SYNTHETIC GetTime outputs (test data, not vsync)");
        const std::vector<std::uint32_t> script_a = {
            0, 1, 2, 1, 1, 2,            // present, wait, gettime, wait, wait, gettime
            0, 0, 0, 0,                  // null, 16-byte, 31-byte, oversized(prefix)
            0, 0, 0, 0, 0, 0,            // six gate rejections
            1, 2,                        // mixed wrap regression: wait, gettime
        };
        // Chronological window for 18 calls is script[10..17]; under a raw
        // wrap dump it would be [1,2,0,0,0,0,0,0] and this check fails.
        check_ring(trace, script_a, "full A script");
        const psvr_vdb_trace_t trace_a_before_b = trace;

        // 11. Second fixture with timing OFF: raw bool FALSE path and zeroed
        // outputs across the boundary; per-handle state stays isolated.
        void* handle_b = nullptr;
        TraceBox created_b;
        std::memset(static_cast<unsigned char*>(static_cast<void*>(&created_b.trace)), kPoison,
                    sizeof created_b.trace);
        check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, 0u, &handle_b, &created_b.trace) ==
                      PSVR_VDB_STATUS_OK,
              "create accepts version 002 with timing disabled (config 0)");
        check(handle_b != nullptr && handle_b != handle_a, "second fixture is its own object");
        FixtureGuard guard_b(handle_b, destroy_fn);
        const std::uint8_t ret_null = get_time_slot(handle_b, nullptr, nullptr);
        check(ret_null == 0u, "timing-off GetTime returns raw false (0) even with null outputs");
        outputs.seconds = 123.0f;
        outputs.counter = 999u;
        const std::uint8_t ret_off = get_time_slot(handle_b, &outputs.seconds, &outputs.counter);
        check(ret_off == 0u, "timing-off GetTime returns false: the honest no-hardware answer");
        check(outputs.seconds == 0.0f && outputs.counter == 0u,
              "timing-off outputs are zeroed, never invented");
        build_wire(wire, kGoodHandle1, kGoodVSync1, kGoodFrame1, kGoodTime1);
        present_slot(handle_b, wire.body, PSVR_VDB_WIRE_BYTES);
        psvr_vdb_trace_t trace_b = read_trace(trace_read_fn, handle_b, "fixture B");
        check(trace_b.config_flags == 0u, "B journal echoes timing-off config");
        check(trace_b.accepted_presents == 1u && trace_b.rejected_presents == 0u, "B accepted its own present");
        check(trace_b.gettime_calls == 2u && trace_b.wait_calls == 0u, "B saw its own calls only");
        check_ring(trace_b, {2, 2, 0}, "B journal ring");
        const psvr_vdb_trace_t trace_a_after_b = read_trace(trace_read_fn, handle_a, "isolation recheck");
        check(std::memcmp(&trace_a_before_b, &trace_a_after_b, sizeof(psvr_vdb_trace_t)) == 0,
              "calls on fixture B never touched fixture A's journal");

        // 11b. Guard-page read regressions, on a dedicated fixture C so every
        // counter elsewhere stays as scripted. Host test memory only - no
        // GPU, no shared texture, no driver memory. Two distinct claims:
        //   (a) REJECT-BEFORE-READ: sources pointing at PAGE_NOACCESS memory
        //       combined with undersized declarations (0/16/31). The shared
        //       parser checks null and size before touching source bytes, so
        //       a correct call returns a clean BUFFER_TOO_SMALL; a gate that
        //       read first would fault and crash this process immediately.
        //   (b) NO-READ-PAST-PREFIX: a real 32-byte wire laid flush against
        //       the no-access boundary, declared with size 64. Reading only
        //       the struct's own 32 bytes never faults; reading any byte the
        //       caller did not own inside the prefix would touch the guard.
        // Detection model is deliberate: success means the call RETURNED and
        // the journal accepted/rejected exactly as expected; a fault is the
        // regression, with no attempt to catch an AV and keep going.
        {
            GuardedPages pages;
            void* handle_c = nullptr;
            TraceBox created_c;
            std::memset(created_c.before, kPoison, sizeof created_c.before);
            std::memset(created_c.after, kPoison, sizeof created_c.after);
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&created_c.trace)), kPoison,
                        sizeof created_c.trace);
            check(create_fn(PSVR_VDB_INTERFACE_VERSION_002, PSVR_VDB_CFG_TIMING_SYNTHETIC, &handle_c,
                             &created_c.trace) == PSVR_VDB_STATUS_OK,
                  "guard fixture C created");
            check(handle_c != nullptr && handle_c != handle_a && handle_c != handle_b,
                  "fixture C is its own object");
            FixtureGuard guard_c(handle_c, destroy_fn);

            // (a) reject-before-read with truly unreadable sources.
            const void* fault_pointer = pages.inaccessible();
            std::uint64_t expected_rejected = 0;
            for (const std::uint32_t short_size : {0u, 16u, PSVR_VDB_WIRE_BYTES - 1u}) {
                present_slot(handle_c, fault_pointer, short_size);
                ++expected_rejected;
                const psvr_vdb_trace_t trace_c =
                    read_trace(trace_read_fn, handle_c, "guard-page short-size reject");
                check(trace_c.last_present_reason == PSVR_VDB_PRESENT_BUFFER_TOO_SMALL &&
                          trace_c.rejected_presents == expected_rejected &&
                          trace_c.accepted_presents == 0u,
                      "unreadable source with size " + std::to_string(short_size) +
                          " was rejected before any byte was read");
            }

            // (b) no-read-past-prefix: readable 32-byte prefix flush against
            // the guard page, oversized declaration. If this call returns,
            // bytes 32..63 were provably never read.
            unsigned char* flush_wire = pages.flush_prefix32();
            std::memset(flush_wire, 0xCC, PSVR_VDB_WIRE_BYTES);
            std::memcpy(flush_wire + PSVR_VDB_WIRE_OFFSET_HANDLE, &kGoodHandle2, sizeof kGoodHandle2);
            std::memcpy(flush_wire + PSVR_VDB_WIRE_OFFSET_VSYNC, &kGoodVSync2, sizeof kGoodVSync2);
            std::memcpy(flush_wire + PSVR_VDB_WIRE_OFFSET_FRAME, &kGoodFrame2, sizeof kGoodFrame2);
            std::memcpy(flush_wire + PSVR_VDB_WIRE_OFFSET_TIME, &kGoodTime2, sizeof kGoodTime2);
            present_slot(handle_c, flush_wire, 64u); // declared size exceeds the readable prefix
            present_slot(handle_c, flush_wire, PSVR_VDB_WIRE_BYTES); // exact-size, same flush wire
            const psvr_vdb_trace_t trace_c =
                read_trace(trace_read_fn, handle_c, "guard-page oversized accepted");
            check(trace_c.accepted_presents == 2u && trace_c.rejected_presents == 3u,
                  "guard-page fixture saw 2 accepted prefix reads and 3 pre-read rejections");
            check(trace_c.last_shared_handle == kGoodHandle2 &&
                      trace_c.last_compositor_frame_id == kGoodFrame2 &&
                      trace_c.last_vsync_raw == kGoodVSync2 &&
                      trace_c.last_vsync_time_seconds == kGoodTime2,
                  "flush-against-guard prefix decoded field-exact");
            check_ring(trace_c, {0, 0, 0, 0, 0}, "guard fixture C script");

            TraceBox final_c;
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&final_c.trace)), kPoison,
                        sizeof final_c.trace);
            check(destroy_fn(handle_c, &final_c.trace) == PSVR_VDB_STATUS_OK, "explicit destroy of C");
            guard_c.release();
            check(final_c.trace.magic == PSVR_VDB_TRACE_MAGIC && final_c.trace.accepted_presents == 2u,
                  "C final journal copied out before teardown");
            // handle_c dangles; pages release both VirtualAlloc pages on scope exit.
        }
        // A's journal is untouched by the guard section: re-read and compare
        // against the saved full-script snapshot.
        const psvr_vdb_trace_t trace_a_after_c = read_trace(trace_read_fn, handle_a, "post-guard recheck");
        check(std::memcmp(&trace_a_before_b, &trace_a_after_c, sizeof(psvr_vdb_trace_t)) == 0,
              "fixture C calls never touched fixture A's journal");

        // 12. Zero-handle rejections on the read and destroy exports; the
        // destination stays poison, and no object is disturbed.
        {
            TraceBox box;
            std::memset(box.before, kPoison, sizeof box.before);
            std::memset(box.after, kPoison, sizeof box.after);
            std::memset(static_cast<unsigned char*>(static_cast<void*>(&box.trace)), kPoison, sizeof box.trace);
            check(trace_read_fn(nullptr, &box.trace) == PSVR_VDB_STATUS_NULL_HANDLE,
                  "trace_read rejects a zero handle");
            check(trace_bytes_untouched(box), "zero-handle trace_read wrote its destination");
            check(destroy_fn(nullptr, &box.trace) == PSVR_VDB_STATUS_NULL_HANDLE,
                  "destroy rejects a zero handle");
            check(trace_bytes_untouched(box), "zero-handle destroy wrote its destination");
            check(destroy_fn(handle_b, nullptr) == PSVR_VDB_STATUS_NULL_OUTPUT,
                  "destroy rejects a null out_trace WITHOUT deleting");
        }

        // 13. Teardown in the mandated order: every fixture goes through the
        // DLL's own destroy export (never a base-pointer delete, never a raw
        // vtable destructor guess), and only then is the module unloaded.
        TraceBox final_b;
        std::memset(static_cast<unsigned char*>(static_cast<void*>(&final_b.trace)), kPoison,
                    sizeof final_b.trace);
        check(destroy_fn(handle_b, &final_b.trace) == PSVR_VDB_STATUS_OK, "explicit destroy of B");
        guard_b.release();
        check(final_b.trace.magic == PSVR_VDB_TRACE_MAGIC && final_b.trace.accepted_presents == 1u,
              "final journal copied out before the object died");
        // handle_b is dangling from here on; it is never touched again.

        std::cout << "PASS IVRVirtualDisplay_002 cross-DLL boundary harness: vtable slot order 0/1/2 "
                     "demonstrated behaviorally, this-pointer preservation, raw bool true/false, "
                     "guarded 32-byte Present wire, null/short/oversized handling by the shared "
                     "parser, guard-page regressions for reject-before-read and no-read-past-the-"
                     "32-byte-prefix (an over-read would fault this process), chronological "
                     "slot_ring verified across the 8-call wrap at 17/18 mixed calls, rejections "
                     "preserving the accepted trace, and RAII unload after the fixture's own "
                     "destroy export. GetTime/Wait outputs are SYNTHETIC TEST DATA; no hardware or "
                     "real vsync claim. Verified only with MinGW-w64 GCC; MSVC was not run.\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL virtual_display_boundary: " << error.what() << '\n';
        return 1;
    }
}
