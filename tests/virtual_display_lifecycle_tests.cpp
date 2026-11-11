// SPDX-License-Identifier: MIT
// Hardware-free, offline harness for psvr::PresentLifecycle, the metadata
// scheduling core for the IVRVirtualDisplay_002 presenter architecture studied
// by tests/virtual_display_contract_tests.cpp. This executable links nothing
// beyond the C++ runtime: no SteamVR, no VRInit, no driver factory, no GPU,
// no OS timing, no threads. PresentLifecycle never derives from any SDK
// interface and is never registered anywhere; every completion and vsync
// "event" below is a hand-delivered test value, never a synthetic hardware
// claim. Not registered in CMakeLists.txt by design; CMake/ctest integration
// is a deliberate later step (a snippet matching the contract test block was
// provided with this file's report). Build manually against the SAME
// openvr_driver.h the driver uses (C++17, MinGW-w64 example):
//   g++ -std=c++17 -Wall -Wextra -Wpedantic -isystem <sdk-header-dir> -Idriver
//       -o virtual_display_lifecycle_tests.exe tests/virtual_display_lifecycle_tests.cpp
#include "virtual_display_lifecycle.hpp"

#include <cmath>
#include <cstdint>
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

using psvr::CompletionEventResult;
using psvr::kInvalidTicket;
using psvr::LifecycleState;
using psvr::PresentLifecycle;
using psvr::PresentSubmitResult;
using psvr::TicketStatus;
using psvr::VsyncEventResult;

const std::uint64_t kHandle1 = 0x1000000000000001ull;
const std::uint64_t kHandle2 = 0x2000000000000002ull;
const std::uint64_t kHandle3 = 0x3000000000000003ull;
const std::uint64_t kHandle4 = 0x4000000000000004ull;

// Well-formed metadata for the typed path. The compositor frame ids used in
// scheduling tests (9001..) are deliberately far from the submission counter
// and from the hardware vsync counters (7, 10, 500..) so the three streams
// can never be confused by accident.
vr::PresentInfo_t make_info(std::uint64_t handle,
                            vr::EVSync vsync,
                            std::uint64_t compositor_frame_id,
                            double vsync_time_seconds) {
    vr::PresentInfo_t info{};
    info.backbufferTextureHandle = handle;
    info.vsync = vsync;
    info.nFrameId = compositor_frame_id;
    info.flVSyncTimeInSeconds = vsync_time_seconds;
    return info;
}

// Raw 32-byte body exactly like the compositor's Present bytes, fields packed
// at their pinned offsets. Reused for every submit() call through the void*
// overload so the parser rules stay under test end to end.
void build_wire(unsigned char* wire,
                std::uint64_t handle,
                std::uint32_t vsync_raw,
                std::uint64_t frame_id,
                double vsync_time) {
    std::memset(wire, 0xCC, 32); // padding/unknown bytes, never interpreted
    std::memcpy(wire + offsetof(vr::PresentInfo_t, backbufferTextureHandle), &handle, sizeof handle);
    std::memcpy(wire + offsetof(vr::PresentInfo_t, vsync), &vsync_raw, sizeof vsync_raw);
    std::memcpy(wire + offsetof(vr::PresentInfo_t, nFrameId), &frame_id, sizeof frame_id);
    std::memcpy(wire + offsetof(vr::PresentInfo_t, flVSyncTimeInSeconds), &vsync_time, sizeof vsync_time);
}

// Submit through the typed overload and require acceptance, or fail with the
// given label. Returns the ticket.
std::uint64_t submit_ok(PresentLifecycle& lifecycle, const vr::PresentInfo_t& info, const std::string& label) {
    std::uint64_t ticket = kInvalidTicket;
    check(lifecycle.submit(info, &ticket) == PresentSubmitResult::kAccepted, "submit accepted: " + label);
    check(ticket != kInvalidTicket, "submit produced a live ticket: " + label);
    return ticket;
}

bool close_seconds(float got, float want) { return std::fabs(got - want) < 1e-5f; }

const double kNan = std::numeric_limits<double>::quiet_NaN();
const double kPosInf = std::numeric_limits<double>::infinity();
const double kNegInf = -std::numeric_limits<double>::infinity();
} // namespace

int main() {
    try {
        // 1. Shape and initial state: bounded, empty, and above all NOT an
        // SDK interface (static_asserts in the header already enforce this).
        static_assert(std::is_final<PresentLifecycle>::value, "lifecycle is a leaf type");
        static_assert(!std::is_base_of<vr::IVRVirtualDisplay, PresentLifecycle>::value,
                      "no production interface registration: the core must not derive from IVRVirtualDisplay");
        static_assert(!std::is_copy_constructible<PresentLifecycle>::value,
                      "tickets are tied to exactly one live object");
        {
            PresentLifecycle lifecycle;
            check(lifecycle.state() == LifecycleState::kReady, "init: default-constructed lifecycle is ready");
            check(lifecycle.configured_capacity() == PresentLifecycle::kMaxCapacity, "init: default capacity is max");
            check(lifecycle.session_token() != 0, "init: session token is live and nonzero");
            check(lifecycle.session_token() == lifecycle.session_token(),
                  "init: session token getter is a pure read, bumping nothing");
            check(lifecycle.submission_count() == 0, "init: submission counter starts at zero");
            check(lifecycle.occupied_count() == 0 && lifecycle.in_flight_count() == 0, "init: queue empty");
            check(!lifecycle.vsync_confirmed(), "init: no hardware vsync is claimed without an observation");
            float seconds = 123.0f;
            std::uint64_t counter = 999;
            check(!lifecycle.get_time_since_last_vsync(1000000.0, &seconds, &counter),
                  "init: GetTimeSinceLastVsync unavailable before any trusted event");
            check(seconds == 0.0f && counter == 0, "init: timing outputs zeroed, not invented");
            check(lifecycle.peek_next_in_flight(nullptr, nullptr) == false, "init: peek with null outputs is safe");
            check(lifecycle.ticket_status(kInvalidTicket) == TicketStatus::kUnknownStale,
                  "init: the zero ticket is never a live present");
            std::uint64_t ticket = 777; // caller garbage is always overwritten
            check(lifecycle.submit(nullptr, 0, &ticket) == PresentSubmitResult::kNullSource &&
                      ticket == kInvalidTicket,
                  "init: rejected submit writes the invalid ticket into a garbage output");
        }

        // 2. Misconfigured capacity fails closed and permanently: no silent
        // clamp, no partial usability.
        {
            PresentLifecycle zero_capacity(0);
            PresentLifecycle oversized(PresentLifecycle::kMaxCapacity + 1);
            for (PresentLifecycle* broken : {&zero_capacity, &oversized}) {
                check(broken->state() == LifecycleState::kDisabled, "misconfig: object is permanently disabled");
                std::uint64_t ticket = 555;
                check(broken->submit(make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), &ticket) ==
                          PresentSubmitResult::kDetached,
                      "misconfig: valid submit rejected, not clamped into a smaller queue");
                check(ticket == kInvalidTicket, "misconfig: rejection produced no ticket");
                check(broken->notify_vsync_observed(broken->session_token(), 1, 10.0) == VsyncEventResult::kDetached,
                      "misconfig: vsync events rejected on a disabled lifecycle");
                check(broken->notify_vsync_observed(broken->session_token() + 999, 1, 10.0) ==
                          VsyncEventResult::kStaleSession,
                      "misconfig: a foreign token is a stale session before any state talk");
                check(broken->notify_present_completed(256) == CompletionEventResult::kBadTicket,
                      "misconfig: no ticket can ever be live on a disabled lifecycle");
                broken->reset();
                check(broken->state() == LifecycleState::kDisabled, "misconfig: reset cannot invent a capacity");
                check(broken->disconnect() == 0 && broken->notify_backend_failed() == 0,
                      "misconfig: session transitions report nothing abandoned");
            }
        }

        // 3. Submit boundary reuses the existing byte-gate rules verbatim:
        // same reasons, same deterministic first-fault order, nothing stored
        // on rejection.
        {
            PresentLifecycle lifecycle(4);
            alignas(vr::PresentInfo_t) unsigned char wire[32];
            std::uint64_t ticket = 0;

            const auto reject_raw = [&](const std::string& label,
                                        PresentSubmitResult want,
                                        const void* bytes,
                                        std::uint32_t size) {
                ticket = 12345;
                const auto result = lifecycle.submit(bytes, size, &ticket);
                check(result == want, "submit gate: " + label);
                check(ticket == kInvalidTicket, "submit gate: rejection returned no ticket: " + label);
                check(lifecycle.submission_count() == 0 && lifecycle.occupied_count() == 0 &&
                          lifecycle.in_flight_count() == 0,
                      "submit gate: rejection stored nothing anywhere: " + label);
            };

            reject_raw("null source, full size", PresentSubmitResult::kNullSource, nullptr, 32);
            reject_raw("null source, size 0", PresentSubmitResult::kNullSource, nullptr, 0);
            build_wire(wire, kHandle1, 1, 9001, 0.008);
            reject_raw("size 0 with valid body", PresentSubmitResult::kBufferTooSmall, wire, 0);
            reject_raw("size 31 with valid body", PresentSubmitResult::kBufferTooSmall, wire, 31);

            build_wire(wire, kHandle1, 3, 9001, 0.008); // unknown EVSync enumerator
            reject_raw("vsync enumerator 3", PresentSubmitResult::kUnknownVSync, wire, 32);
            build_wire(wire, kHandle1, 0xFFFFFFFFu, 9001, 0.008);
            reject_raw("vsync bytes 0xFFFFFFFF", PresentSubmitResult::kUnknownVSync, wire, 32);
            build_wire(wire, INVALID_SHARED_TEXTURE_HANDLE, 1, 9001, 0.008); // zero handle rejected
            reject_raw("zero shared handle", PresentSubmitResult::kZeroSharedHandle, wire, 32);
            for (const double bad_time : {kNan, kPosInf, kNegInf}) {
                build_wire(wire, kHandle1, 1, 9001, bad_time);
                reject_raw("non-finite vsync time", PresentSubmitResult::kNonFiniteVSyncTime, wire, 32);
            }

            // First listed fault wins, exactly like the contract gate:
            build_wire(wire, 0, 7, 9001, kNan);
            reject_raw("vsync checked before handle and time", PresentSubmitResult::kUnknownVSync, wire, 32);
            build_wire(wire, 0, 1, 9001, kNan);
            reject_raw("handle checked before time", PresentSubmitResult::kZeroSharedHandle, wire, 32);
            check(lifecycle.rejected_invalid_submits() == 12, "submit gate: every invalid attempt counted once");

            // No invented rules: legal boundary metadata is accepted, and the
            // compositor frame id / vsync time are stored verbatim.
            build_wire(wire, kHandle1, 0, 0, 0.0); // VSync_None, first frame, zero time
            ticket = kInvalidTicket;
            check(lifecycle.submit(wire, 32, &ticket) == PresentSubmitResult::kAccepted &&
                      ticket != kInvalidTicket,
                  "submit gate: legal all-zero-optional body accepted through the raw path");
            std::uint64_t peeked_ticket = kInvalidTicket;
            vr::PresentInfo_t next_in_flight{};
            check(lifecycle.peek_next_in_flight(&peeked_ticket, &next_in_flight) && peeked_ticket == ticket &&
                      next_in_flight.backbufferTextureHandle == kHandle1 && next_in_flight.nFrameId == 0 &&
                      next_in_flight.vsync == vr::VSync_None && next_in_flight.flVSyncTimeInSeconds == 0.0,
                  "submit gate: raw metadata stored verbatim and scheduled first");
            check(lifecycle.submit(make_info(kHandle2, vr::VSync_NoWaitRender, 1, -1.5), &ticket) ==
                      PresentSubmitResult::kAccepted,
                  "submit gate: finite negative vsync time still legal, like the contract says");
            check(lifecycle.submission_count() == 2, "submit gate: submission counter counts accepted only");
        }

        // 4. Ticket scheduling, out-of-order completions, duplicates, and
        // explicit bounded reclamation that turns old tickets stale.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t ticket_a =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), "A");
            const std::uint64_t ticket_b =
                submit_ok(lifecycle, make_info(kHandle2, vr::VSync_None, 9002, 0.016), "B");
            const std::uint64_t ticket_c =
                submit_ok(lifecycle, make_info(kHandle3, vr::VSync_NoWaitRender, 9003, 0.024), "C");
            check(ticket_a != ticket_b && ticket_b != ticket_c && ticket_a != ticket_c,
                  "tickets: distinct submits get distinct opaque tickets");
            check(lifecycle.ticket_status(ticket_a) == TicketStatus::kInFlight, "tickets: fresh ticket in flight");

            std::uint64_t peeked = kInvalidTicket;
            vr::PresentInfo_t info{};
            check(lifecycle.peek_next_in_flight(&peeked, &info) && peeked == ticket_a,
                  "schedule: oldest in-flight is first by submission ordinal");
            check(lifecycle.notify_present_completed(ticket_b) == CompletionEventResult::kCompleted,
                  "schedule: completions may arrive out of order (B before A)");
            check(lifecycle.peek_next_in_flight(&peeked, &info) && peeked == ticket_a,
                  "schedule: completed B left the head untouched");
            check(lifecycle.notify_present_completed(ticket_a) == CompletionEventResult::kCompleted,
                  "schedule: A completes after B even though it was submitted first");
            check(lifecycle.peek_next_in_flight(&peeked, &info) && peeked == ticket_c &&
                      info.backbufferTextureHandle == kHandle3 && info.nFrameId == 9003,
                  "schedule: head moved to the only remaining in-flight present");
            check(lifecycle.peek_next_in_flight(&peeked, &info) && peeked == ticket_c &&
                      lifecycle.in_flight_count() == 1 && lifecycle.occupied_count() == 3,
                  "peek: read-only inspection, not a dequeue; repeated calls keep the same head and state");
            check(lifecycle.in_flight_count() == 1 && lifecycle.occupied_count() == 3,
                  "schedule: terminal slots stay occupied (bounded retention) until reclaim");

            check(lifecycle.notify_present_completed(ticket_a) == CompletionEventResult::kDuplicate &&
                      lifecycle.notify_present_completed(ticket_a) == CompletionEventResult::kDuplicate,
                  "callback hygiene: duplicate completions detected and idempotent");
            check(lifecycle.duplicate_completion_events() == 2 && lifecycle.completion_events() == 2,
                  "callback hygiene: duplicates never double-credit the completion counter");
            check(lifecycle.notify_present_completed(kInvalidTicket) == CompletionEventResult::kBadTicket,
                  "callback hygiene: the zero ticket is a bad ticket, not a secret live slot");

            const std::uint64_t ticket_d =
                submit_ok(lifecycle, make_info(kHandle4, vr::VSync_WaitRender, 9004, 0.032), "D");
            check(lifecycle.occupied_count() == 4, "tickets: A..D now occupy the whole capacity");
            check(lifecycle.reclaim_terminal() == 2, "reclaim: exactly the two terminal slots came back");
            check(lifecycle.ticket_status(ticket_a) == TicketStatus::kUnknownStale &&
                      lifecycle.ticket_status(ticket_b) == TicketStatus::kUnknownStale,
                  "reclaim: reclaimed slots' tickets are stale, carrying no information");
            check(lifecycle.notify_present_completed(ticket_a) == CompletionEventResult::kStaleGeneration,
                  "callback hygiene: post-reclaim completion is a stale generation, never a revival");
            check(lifecycle.stale_completion_events() == 2, "callback hygiene: bad and stale tickets counted together");
            check(lifecycle.ticket_status(ticket_c) == TicketStatus::kInFlight &&
                      lifecycle.ticket_status(ticket_d) == TicketStatus::kInFlight,
                  "reclaim: live tickets of other slots are unaffected");

            std::uint64_t ticket_a2 = kInvalidTicket;
            check(lifecycle.submit(make_info(kHandle1, vr::VSync_WaitRender, 9005, 0.008), &ticket_a2) ==
                      PresentSubmitResult::kAccepted,
                  "reclaim: freed slot capacity is reusable");
            check(ticket_a2 != ticket_a && ticket_a2 != ticket_b,
                  "generation epoch: reused slot never re-issues an old ticket value");
            check(lifecycle.ticket_status(ticket_a2) == TicketStatus::kInFlight, "generation epoch: new ticket live");
        }

        // 5. Backpressure: the policy is explicitly drop-NEWEST. A rejected
        // submit stores nothing, the oldest schedule entry is never evicted,
        // and terminal retention honestly counts against capacity until
        // reclaim.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t ticket_1 =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), "full-1");
            submit_ok(lifecycle, make_info(kHandle2, vr::VSync_WaitRender, 9002, 0.008), "full-2");
            submit_ok(lifecycle, make_info(kHandle3, vr::VSync_WaitRender, 9003, 0.008), "full-3");
            submit_ok(lifecycle, make_info(kHandle4, vr::VSync_WaitRender, 9004, 0.008), "full-4");

            std::uint64_t ticket_5 = 777;
            check(lifecycle.submit(make_info(kHandle1, vr::VSync_WaitRender, 9005, 0.008), &ticket_5) ==
                      PresentSubmitResult::kQueueFull,
                  "backpressure: submit at capacity rejected, never blocks, never evicts");
            check(ticket_5 == kInvalidTicket, "backpressure: dropped frame hands back the invalid ticket");
            check(lifecycle.ticket_status(ticket_1) == TicketStatus::kInFlight &&
                      lifecycle.in_flight_count() == 4 && lifecycle.submission_count() == 4,
                  "backpressure: drop-newest kept the oldest four fully intact");
            check(lifecycle.rejected_full_submits() == 1, "backpressure: full rejections counted");

            // A parse failure on a full queue still reports the real reason:
            // capacity is checked last, so invalid metadata never hides.
            std::uint64_t another_ticket = 3;
            check(lifecycle.submit(nullptr, 32, &another_ticket) == PresentSubmitResult::kNullSource,
                  "backpressure: state order puts parse rules before capacity");

            // Completion alone does NOT free capacity: terminal retention is
            // bounded by reclaim, not by hardware assumptions.
            check(lifecycle.notify_present_completed(ticket_1) == CompletionEventResult::kCompleted,
                  "backpressure: one present completed");
            check(lifecycle.submit(make_info(kHandle2, vr::VSync_WaitRender, 9006, 0.008), &another_ticket) ==
                      PresentSubmitResult::kQueueFull,
                  "backpressure: completed-but-unreclaimed slot still counts against capacity");
            check(lifecycle.reclaim_terminal() == 1, "backpressure: owner reclaims the terminal slot");
            check(lifecycle.submit(make_info(kHandle2, vr::VSync_WaitRender, 9006, 0.008), &another_ticket) ==
                      PresentSubmitResult::kAccepted,
                  "backpressure: capacity returns only through the explicit, bounded reclaim step");
        }

        // 6. Fence != vblank: completions never act as vsync ticks, vsync
        // ticks never complete anything, and the three frame streams stay
        // separate: compositor nFrameId, submission counter, hardware vsync.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t ticket_1 =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 5001, 0.008), "fence-1");
            const std::uint64_t ticket_2 =
                submit_ok(lifecycle, make_info(kHandle2, vr::VSync_WaitRender, 5002, 0.016), "fence-2");
            check(lifecycle.notify_present_completed(ticket_1) == CompletionEventResult::kCompleted &&
                      lifecycle.notify_present_completed(ticket_2) == CompletionEventResult::kCompleted,
                  "fence: both presents completed normally");
            check(!lifecycle.vsync_confirmed(), "fence: completions alone never fabricate a hardware vsync");
            float seconds = 42.0f;
            std::uint64_t counter = 42;
            check(!lifecycle.get_time_since_last_vsync(1000000.0, &seconds, &counter) && seconds == 0.0f &&
                      counter == 0,
                  "fence: without an observed vblank there is no time and no hardware counter");
            check(lifecycle.submission_count() == 2 && lifecycle.completion_events() == 2,
                  "fence: submission counter counts scheduling, completion counter counts fences");
            check(lifecycle.submission_count() != 5001 && lifecycle.submission_count() != 5002,
                  "fence: submission counter is not the compositor frame id stream");

            check(lifecycle.notify_vsync_observed(lifecycle.session_token(), 500, 100.0) == VsyncEventResult::kAccepted,
                  "vblank: one externally delivered tick is enough to become trusted");
            check(lifecycle.completion_events() == 2 && lifecycle.duplicate_completion_events() == 0 &&
                      lifecycle.in_flight_count() == 0,
                  "vblank: a tick never completes or re-opens any present");
            check(lifecycle.get_time_since_last_vsync(100.25, &seconds, &counter) && seconds == 0.25f &&
                      counter == 500,
                  "vblank: hardware counter 500 differs from submissions (2) and nFrameIds (5001/5002)");
        }

        // 7. Vsync stream hygiene: non-finite and non-monotonic observations
        // are rejected without moving the last trusted pair; reset/disconnect
        // clear validity and bump the session token; positivity is never
        // invented; and range-end subtractions fail closed instead of
        // producing inf out-params.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t token = lifecycle.session_token();
            const std::uint64_t ticket =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), "vsync-1");
            float seconds = 1.0f;
            std::uint64_t counter = 1;
            for (const double bad_time : {kNan, kPosInf, kNegInf}) {
                check(lifecycle.notify_vsync_observed(token, 10, bad_time) == VsyncEventResult::kNonFiniteTimestamp,
                      "vblank: non-finite timestamp rejected");
            }
            check(!lifecycle.vsync_confirmed() && lifecycle.vsync_observations() == 0,
                  "vblank: rejected observations never mark the stream trusted");
            check(lifecycle.notify_vsync_observed(token, 10, 100.5) == VsyncEventResult::kAccepted &&
                      lifecycle.ticket_status(ticket) == TicketStatus::kInFlight,
                  "vblank: adoption as first trusted tick leaves in-flight tickets alone");
            check(lifecycle.get_time_since_last_vsync(100.5, &seconds, &counter) && seconds == 0.0f && counter == 10,
                  "vblank: zero delta at exactly the trusted timestamp");
            check(!lifecycle.get_time_since_last_vsync(99.0, &seconds, &counter) && seconds == 0.0f && counter == 0,
                  "vblank: a caller clock behind the tick is not exposed as a negative delta");
            check(!lifecycle.get_time_since_last_vsync(kNan, &seconds, &counter),
                  "vblank: non-finite now yields no timing");

            check(lifecycle.notify_vsync_observed(token, 11, 100.5) == VsyncEventResult::kNotMonotonic,
                  "vblank: equal timestamp is not a new tick (no duplicate vblank credit)");
            check(lifecycle.notify_vsync_observed(token, 10, 100.75) == VsyncEventResult::kNotMonotonic,
                  "vblank: regressing hardware counter rejected");
            check(lifecycle.notify_vsync_observed(token, 11, 100.25) == VsyncEventResult::kNotMonotonic,
                  "vblank: regressing timestamp rejected");
            check(lifecycle.vsync_observations() == 1 &&
                      lifecycle.get_time_since_last_vsync(100.75, &seconds, &counter) && seconds == 0.25f &&
                      counter == 10,
                  "vblank: rejections keep the last trusted pair exactly as it was");
            check(lifecycle.notify_vsync_observed(token, 11, 100.75) == VsyncEventResult::kAccepted &&
                      lifecycle.vsync_observations() == 2,
                  "vblank: a properly advancing tick is adopted");

            lifecycle.reset();
            const std::uint64_t fresh_token = lifecycle.session_token();
            check(fresh_token != token, "reset: session token advanced past the old session");
            check(!lifecycle.vsync_confirmed(), "reset: vsync validity cleared with the session");
            check(!lifecycle.get_time_since_last_vsync(1000000.0, &seconds, &counter) && seconds == 0.0f &&
                      counter == 0,
                  "reset: timing unavailable again, no memory of the previous session");
            check(lifecycle.ticket_status(ticket) == TicketStatus::kUnknownStale,
                  "reset: every pre-reset ticket lost its epoch");
            check(lifecycle.notify_vsync_observed(token, 7, -5.0) == VsyncEventResult::kStaleSession &&
                      !lifecycle.vsync_confirmed(),
                  "reset: the pre-reset token cannot open the new stream, even as first event");
            check(lifecycle.notify_vsync_observed(fresh_token, 7, -5.0) == VsyncEventResult::kAccepted,
                  "reset: a fresh stream may start at any finite value, positivity never invented");
            check(lifecycle.notify_vsync_observed(fresh_token, 8, -4.5) == VsyncEventResult::kAccepted &&
                      lifecycle.notify_vsync_observed(fresh_token, 8, -4.0) == VsyncEventResult::kNotMonotonic,
                  "vblank: monotonicity holds in negative domains too");
            check(!lifecycle.get_time_since_last_vsync(-4.6, &seconds, &counter) &&
                      lifecycle.get_time_since_last_vsync(-4.2, &seconds, &counter) &&
                      close_seconds(seconds, 0.3f) && counter == 8,
                  "vblank: negative timestamps work in the caller's own clock domain");
        }

        // 7b. Subtraction range: two finite endpoints can still defeat a
        // naive now - last. The delta must be finite in double AND
        // representable in float before anything is written; rejections keep
        // the outputs zeroed and the trusted pair intact.
        {
            PresentLifecycle extremes(4);
            const double kMaxDouble = std::numeric_limits<double>::max();
            float seconds = 1.0f;
            std::uint64_t counter = 1;
            const std::uint64_t token_a = extremes.session_token();
            check(extremes.notify_vsync_observed(token_a, 3, -kMaxDouble) == VsyncEventResult::kAccepted,
                  "overflow: -DBL_MAX is finite, therefore a legal first tick");
            check(!extremes.get_time_since_last_vsync(kMaxDouble, &seconds, &counter) && seconds == 0.0f &&
                      counter == 0,
                  "overflow: -DBL_MAX to +DBL_MAX overflows double; rejected, outputs stay zero");
            check(extremes.get_time_since_last_vsync(-kMaxDouble, &seconds, &counter) && seconds == 0.0f &&
                      counter == 3,
                  "overflow: the rejected now did not move the trusted pair");
            extremes.reset();
            const std::uint64_t token_b = extremes.session_token();
            check(extremes.notify_vsync_observed(token_b, 4, -1e100) == VsyncEventResult::kAccepted,
                  "overflow: a 1e100-magnitude finite timestamp is legal metadata-domain input");
            check(!extremes.get_time_since_last_vsync(0.0, &seconds, &counter) && seconds == 0.0f && counter == 0,
                  "overflow: 1e100 delta exceeds float max; never cast into an +inf float output");
            check(extremes.get_time_since_last_vsync(-1e100, &seconds, &counter) && seconds == 0.0f && counter == 4,
                  "overflow: after the range rejection the stream stays usable and trusted");
        }

        // 8. Delayed old-callback regression: a vblank callback queued by a
        // dead backend session must never mark the new stream trusted. The
        // session token gate kills it even against an empty stream, and even
        // when its numbers would otherwise pass the monotonic rule. Ticket
        // generations and the session token stay separate mechanisms.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t old_token = lifecycle.session_token();
            lifecycle.reset();
            const std::uint64_t new_token = lifecycle.session_token();
            check(new_token != old_token, "session: every reset bumps the invalidation token");
            check(lifecycle.notify_vsync_observed(old_token, 6, 201.0) == VsyncEventResult::kStaleSession &&
                      !lifecycle.vsync_confirmed(),
                  "session: delayed old callback rejected even as the first event of the new stream");
            check(lifecycle.notify_vsync_observed(new_token, 5, 200.0) == VsyncEventResult::kAccepted,
                  "session: the current token opens the new stream normally");
            float seconds = 1.0f;
            std::uint64_t counter = 1;
            // The old backend's queued callback arrives now, and its raw
            // numbers (counter 6 > 5, timestamp 201.0 > 200.0) would sail
            // through monotonicity: only the token can identify the session.
            check(lifecycle.notify_vsync_observed(old_token, 6, 201.0) == VsyncEventResult::kStaleSession,
                  "session: monotonic-looking old numbers remain a stale session");
            check(lifecycle.get_time_since_last_vsync(200.25, &seconds, &counter) && seconds == 0.25f &&
                      counter == 5 && lifecycle.vsync_observations() == 1,
                  "session: the trusted new tick is exactly unchanged by the old callback");
            const std::uint64_t ticket =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), "session-ticket");
            check(lifecycle.notify_vsync_observed(old_token, 8, 202.0) == VsyncEventResult::kStaleSession &&
                      lifecycle.ticket_status(ticket) == TicketStatus::kInFlight &&
                      lifecycle.in_flight_count() == 1,
                  "session: stale-session events touch no ticket: the two epoch systems are separate");
            check(lifecycle.notify_vsync_observed(new_token, 7, 199.0) == VsyncEventResult::kNotMonotonic &&
                      lifecycle.vsync_observations() == 1 &&
                      lifecycle.get_time_since_last_vsync(200.25, &seconds, &counter) && seconds == 0.25f &&
                      counter == 5,
                  "session: current-session events still obey the unchanged monotonic rule");
        }

        // 9. Disconnect and backend failure: bounded, fail-closed cleanup.
        // Abandoned is terminal; nothing here revives, destroys, or forgets
        // that the borrowed handle's lifetime became unknown.
        {
            PresentLifecycle lifecycle(4);
            const std::uint64_t old_token = lifecycle.session_token();
            const std::uint64_t ticket_x =
                submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9001, 0.008), "disconnect-x");
            const std::uint64_t ticket_y =
                submit_ok(lifecycle, make_info(kHandle2, vr::VSync_WaitRender, 9002, 0.008), "disconnect-y");
            check(lifecycle.notify_vsync_observed(old_token, 7, 50.0) == VsyncEventResult::kAccepted,
                  "disconnect: vsync stream established before the session dies");

            check(lifecycle.disconnect() == 2, "disconnect: reports exactly the abandoned in-flight count");
            check(lifecycle.state() == LifecycleState::kDisconnected, "disconnect: session enters disconnected");
            check(!lifecycle.vsync_confirmed(), "disconnect: hardware trust cleared with the stream");
            check(lifecycle.notify_vsync_observed(old_token, 8, 50.5) == VsyncEventResult::kStaleSession,
                  "disconnect: the pre-disconnect token is a stale session, not merely detached");
            check(lifecycle.ticket_status(ticket_x) == TicketStatus::kAbandoned &&
                      lifecycle.ticket_status(ticket_y) == TicketStatus::kAbandoned,
                  "disconnect: in-flight tickets become abandoned, never silently freed");
            check(lifecycle.peek_next_in_flight(nullptr, nullptr) == false && lifecycle.in_flight_count() == 0 &&
                      lifecycle.occupied_count() == 2,
                  "disconnect: nothing schedulable, terminal retention still bounded by reclaim");

            check(lifecycle.notify_present_completed(ticket_x) == CompletionEventResult::kAlreadyAbandoned &&
                      lifecycle.notify_present_completed(ticket_x) == CompletionEventResult::kAlreadyAbandoned,
                  "disconnect: late completions are classified, never a revival into kCompleted");
            check(lifecycle.ticket_status(ticket_x) == TicketStatus::kAbandoned &&
                      lifecycle.completion_events() == 0 && lifecycle.ignored_abandoned_events() == 2,
                  "disconnect: abandoned stays terminal even if the backend eventually replies");
            std::uint64_t ticket = 888;
            check(lifecycle.submit(make_info(kHandle3, vr::VSync_WaitRender, 9003, 0.008), &ticket) ==
                          PresentSubmitResult::kDetached &&
                      ticket == kInvalidTicket,
                  "disconnect: no new work scheduled on a dead session");
            check(lifecycle.notify_vsync_observed(lifecycle.session_token(), 9, 51.0) == VsyncEventResult::kDetached,
                  "disconnect: no vsync observations belong to a dead session");
            check(lifecycle.abandoned_slots_lifetime() == 2 && lifecycle.rejected_detached_submits() == 1,
                  "disconnect: abandon and detach counters agree with the events delivered");

            check(lifecycle.notify_backend_failed() == 0, "backend failure after disconnect: nothing left to abandon");
            check(lifecycle.state() == LifecycleState::kBackendFailed, "backend failure recorded in the state machine");

            lifecycle.reset();
            check(lifecycle.state() == LifecycleState::kReady && lifecycle.occupied_count() == 0 &&
                      lifecycle.submission_count() == 0 && lifecycle.abandoned_slots_lifetime() == 0 &&
                      lifecycle.rejected_detached_submits() == 0 && lifecycle.ignored_abandoned_events() == 0,
                  "reset: new session starts with cleared queue, counters, and hardware trust");
            check(lifecycle.ticket_status(ticket_x) == TicketStatus::kUnknownStale &&
                      lifecycle.notify_present_completed(ticket_x) == CompletionEventResult::kStaleGeneration,
                  "reset: old-epoch tickets rejected as stale generations after the epoch bump");
            submit_ok(lifecycle, make_info(kHandle1, vr::VSync_WaitRender, 9101, 0.008), "reset-new");
            check(lifecycle.submission_count() == 1, "reset: fresh submission stream, unrelated to old ids");
        }

        // 10. Bounded stress: 100 submits against capacity 4 with no reclaim:
        // exactly capacity accepted, the rest rejected as full, and every
        // counter is consistent in O(capacity) steps with no event loop.
        {
            PresentLifecycle lifecycle(4);
            std::uint64_t tickets[100];
            std::uint32_t accepted = 0;
            for (std::uint32_t i = 0; i < 100; ++i) {
                std::uint64_t ticket = kInvalidTicket;
                const auto result =
                    lifecycle.submit(make_info(kHandle1 + i, vr::VSync_WaitRender, 10000 + i, 0.008), &ticket);
                if (result == PresentSubmitResult::kAccepted) {
                    tickets[accepted++] = ticket;
                } else {
                    check(result == PresentSubmitResult::kQueueFull, "stress: non-accept is kQueueFull, nothing else");
                    check(ticket == kInvalidTicket, "stress: full reject hands back the invalid ticket");
                }
                check(lifecycle.occupied_count() <= lifecycle.configured_capacity(),
                      "stress: occupied never exceeds configured capacity");
                check(lifecycle.in_flight_count() <= lifecycle.occupied_count(),
                      "stress: in-flight is always a subset of occupied");
            }
            check(accepted == 4 && lifecycle.rejected_full_submits() == 96,
                  "stress: drop-newest policy is exact: 4 scheduled, 96 dropped");
            check(lifecycle.notify_present_completed(tickets[0]) == CompletionEventResult::kCompleted &&
                      lifecycle.notify_present_completed(tickets[1]) == CompletionEventResult::kCompleted,
                  "stress: two oldest completed out of order is fine");
            check(lifecycle.reclaim_terminal() == 2 && lifecycle.occupied_count() == 2,
                  "stress: reclaimed 2, occupied back to the two still in flight");
            submit_ok(lifecycle, make_info(kHandle3, vr::VSync_WaitRender, 10004, 0.008), "stress-refill-1");
            submit_ok(lifecycle, make_info(kHandle4, vr::VSync_WaitRender, 10005, 0.008), "stress-refill-2");
            check(lifecycle.submission_count() == 6 && lifecycle.occupied_count() == 4 &&
                      lifecycle.in_flight_count() == 4,
                  "stress: refill after reclaim uses freed epochs, never duplicate tickets");
        }

        std::cout << "PASS virtual display lifecycle: bounded queue, tickets, drop-newest backpressure, "
                     "stale/duplicate/out-of-order events, disconnect cleanup, fence-vs-vblank, monotonic "
                     "vsync, stale-session vblank rejection, representable-delta guard\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
