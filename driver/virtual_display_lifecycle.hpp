// SPDX-License-Identifier: MIT
#pragma once
#include "virtual_display_contract.hpp"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <type_traits>

namespace psvr {
// Offline lifecycle core for the presenter architecture studied in
// driver/virtual_display_contract.hpp. It schedules the *metadata* of accepted
// Present submissions for an external display backend and consumes completion
// and vertical-blank events that the integrator delivers explicitly. This is
// NOT a display backend: nothing here touches GPU resources, shared texture
// memory, scanout timing, DriverLog, WinAPI, or dynamic loading. It reads no
// clock and creates no OS handle; every timestamp and counter it reports
// originates from an event that somebody else delivered. No interface with any
// runtime is registered or even shaped here: PresentLifecycle does not derive
// from vr::IVRVirtualDisplay; the future glue presenter will translate the
// ABI calls into this API. The header is single-threaded by contract: exactly
// one owner thread must call it, or the integrator must serialize all calls
// with an external lock. Every operation is nonblocking and bounded: worst
// case is one scan over at most kMaxCapacity slots, no loops depend on event
// arrival, and the whole object is a fixed-size, heap-free struct.
//
// --- Three distinct frame numbers, never conflated ---
// 1) vr::PresentInfo_t::nFrameId, the compositor's frame id: stored verbatim
//    inside the queued metadata and used as a schedule key by nothing here.
// 2) submission_count(), an internal monotonic counter of accepted submits:
//    counts scheduling events, says nothing about hardware.
// 3) the hardware vsync frame counter and timestamp, exposed only after an
//    externally confirmed notify_vsync_observed() call, always separate from
//    (2). Completion notifications are fence-like signals ("the backend
//    stopped reading the texture"); they are never treated as vsync ticks,
//    and vsync ticks never complete any ticket.
//
// --- Ownership model ---
// The shared-texture handle inside PresentInfo_t is a borrowed integer. The
// lifecycle stores its value, never dereferences it, and never releases,
// destroys, or invents any resource. An in-flight ticket means the borrowed
// handle may still be referenced by the backend; the importer decides texture
// lifetime around that information and this core fails closed: it forgets
// metadata but never claims a resource is free, especially on disconnect or
// backend failure where completions will never arrive.
//
// --- Tickets ---
// A ticket is an opaque 64-bit value combining a slot index and a per-slot
// generation (epoch). Zero is never a valid ticket. Because the generation is
// bumped whenever a slot is recycled or a session is reset, every callback
// naming a ticket from the past is classified as stale instead of silently
// re-activating a slot. Generations use the 56 high bits of the ticket; a
// single slot would need 2^56 reuses to wrap, which is unreachable in
// practice, so wrap-around is a documented limitation, not handled logic.
//
// --- Session token: a second, independent invalidation epoch ---
// Ticket generations and the session token are separate mechanisms: tickets
// validate per-slot backend callbacks, the token validates the vsync stream.
// session_token() names the current backend session and is incremented by
// reset(), disconnect(), and notify_backend_failed(). notify_vsync_observed()
// requires the token the observer subscribed under, so a callback queued by a
// dead backend can never mark the new stream trusted, not even as the first
// event of a fresh session. Tokens are monotonic over the whole object
// lifetime and are never reused or zeroed, and they are counters, not
// clocks: no timestamp is ever synthesized here either.

enum class LifecycleState {
    kDisabled,       // constructed with an invalid capacity: permanently unusable
    kReady,          // accepting submissions
    kDisconnected,   // display gone: no new work trusted, in-flight abandoned
    kBackendFailed,  // backend broke: same fail-closed treatment
};

enum class PresentSubmitResult {
    kAccepted,
    kDetached,            // lifecycle not in kReady (disabled/disconnected/failed)
    kNullSource,          // mapped from the existing byte-gate rules, same order
    kBufferTooSmall,
    kUnknownVSync,
    kZeroSharedHandle,
    kNonFiniteVSyncTime,
    kQueueFull,           // explicit drop-newest backpressure, see submit()
};

enum class CompletionEventResult {
    kCompleted,        // first completion of a live in-flight ticket
    kDuplicate,        // completion for an already completed, unreclaimed slot
    kAlreadyAbandoned, // late completion after disconnect/failure: never revives
    kStaleGeneration,  // ticket names a recycled epoch or a freed slot
    kBadTicket,        // zero ticket or structurally out-of-range ticket
};

enum class VsyncEventResult {
    kAccepted,
    kDetached,           // lifecycle not in kReady
    kNonFiniteTimestamp, // NaN or infinity rejected like the byte gate does
    kNotMonotonic,       // timestamp or counter did not strictly advance
    kStaleSession,       // token does not name the current session (old backend)
};

enum class TicketStatus {
    kUnknownStale, // invalid, bad, or recycled ticket: no information to give
    kInFlight,     // the borrowed handle may still be read by the backend
    kCompleted,    // backend confirmed it stopped reading: terminal
    kAbandoned,    // session ended before confirmation: lifetime UNKNOWN
};

// Ticket value reserved to mean "no scheduled present"; never returned by a
// successful submit, so the integrator can safely zero-initialize.
constexpr std::uint64_t kInvalidTicket = 0;

class PresentLifecycle final {
public:
    // Hard upper bound of the fixed slot array; capacity is set at
    // construction and never reallocated. 0 or greater-than-max requests
    // produce a permanently kDisabled object instead of a silent clamp:
    // a misconfigured owner must fail loudly through every rejection.
    static constexpr std::uint32_t kMaxCapacity = 64;

    explicit PresentLifecycle(std::uint32_t capacity = kMaxCapacity) noexcept {
        if (capacity == 0 || capacity > kMaxCapacity) {
            state_ = LifecycleState::kDisabled; // capacity_ stays 0: unusable
            return;
        }
        capacity_ = capacity;
        state_ = LifecycleState::kReady;
    }

    // Copying would duplicate every live ticket without duplicating any
    // backend reality; the object is a single point of truth by ownership.
    PresentLifecycle(const PresentLifecycle&) = delete;
    PresentLifecycle& operator=(const PresentLifecycle&) = delete;

    // --- Submission: schedule validated Present metadata, get a ticket ---
    // Deterministic rejection order, mirroring the existing byte-gate so the
    // first-listed fault always wins: session usable? then vsync enumerator,
    // then shared handle, then time finiteness (via copy_present_info, which
    // also gates null and undersized buffers), and capacity checked LAST so
    // malformed metadata can never crowd out accounting of real frames.
    // Frame-drop policy is explicit drop-newest: on kQueueFull this call
    // stores nothing, touches nothing, and returns kInvalidTicket; the
    // caller drops the frame while still holding full ownership of its
    // texture, which was never handed to any backend.
    // out_ticket (when not null) is always written: the ticket on success,
    // kInvalidTicket on every rejection.
    PresentSubmitResult submit(const void* present_info_bytes,
                               std::uint32_t size_bytes,
                               std::uint64_t* out_ticket) noexcept {
        if (out_ticket != nullptr) *out_ticket = kInvalidTicket;
        if (state_ != LifecycleState::kReady) {
            ++rejected_detached_submits_;
            return PresentSubmitResult::kDetached;
        }
        vr::PresentInfo_t snapshot{};
        const PresentInfoCopyResult gate =
            copy_present_info(snapshot, present_info_bytes, size_bytes);
        switch (gate) {
            case PresentInfoCopyResult::kNullSource:
                ++rejected_invalid_submits_;
                return PresentSubmitResult::kNullSource;
            case PresentInfoCopyResult::kBufferTooSmall:
                ++rejected_invalid_submits_;
                return PresentSubmitResult::kBufferTooSmall;
            case PresentInfoCopyResult::kUnknownVSync:
                ++rejected_invalid_submits_;
                return PresentSubmitResult::kUnknownVSync;
            case PresentInfoCopyResult::kZeroSharedHandle:
                ++rejected_invalid_submits_;
                return PresentSubmitResult::kZeroSharedHandle;
            case PresentInfoCopyResult::kNonFiniteVSyncTime:
                ++rejected_invalid_submits_;
                return PresentSubmitResult::kNonFiniteVSyncTime;
            case PresentInfoCopyResult::kAccepted:
                break;
        }
        if (occupied_ >= capacity_) {
            ++rejected_full_submits_;
            return PresentSubmitResult::kQueueFull;
        }
        // Bounded O(capacity) scan: occupied_ < capacity_ guarantees a free
        // slot, but the check below keeps the scan itself fail-closed against
        // any future accounting drift instead of writing out of bounds.
        std::uint32_t index = 0;
        for (; index < capacity_; ++index) {
            if (slots_[index].state == SlotState::kFree) break;
        }
        if (index >= capacity_) {
            ++rejected_full_submits_;
            return PresentSubmitResult::kQueueFull;
        }
        Slot& slot = slots_[index];
        slot.state = SlotState::kInFlight;
        slot.info = snapshot;
        ++slot.generation; // first use of a slot moves epoch 0 -> 1
        ++submission_counter_;
        slot.submission_ordinal = submission_counter_;
        ++in_flight_;
        ++occupied_;
        if (out_ticket != nullptr) *out_ticket = make_ticket(index, slot.generation);
        return PresentSubmitResult::kAccepted;
    }

    // ABI-shaped convenience overload for an already validated struct; it
    // adds no rules, bytes decide, exactly like the contract header does.
    PresentSubmitResult submit(const vr::PresentInfo_t& present_info, std::uint64_t* out_ticket) noexcept {
        return submit(static_cast<const void*>(&present_info),
                      static_cast<std::uint32_t>(sizeof(present_info)),
                      out_ticket);
    }

    // --- Completion events: delivered by the external backend, never faked ---
    // Completion is classified purely by ticket, in every state, because a
    // late callback must always be answerable truthfully. kAbandoned is a
    // terminal state: a completion arriving after a disconnect or backend
    // failure is counted and ignored; nothing here ever revives an abandoned
    // ticket into "completed", because that would falsely tell the importer
    // the borrowed texture is free to destroy. Out-of-order completion across
    // different tickets is legal: backends may finish differently.
    CompletionEventResult notify_present_completed(std::uint64_t ticket) noexcept {
        std::uint32_t index = 0;
        std::uint64_t generation = 0;
        if (!decode_ticket(ticket, index, generation)) {
            ++stale_completion_events_;
            return CompletionEventResult::kBadTicket;
        }
        Slot& slot = slots_[index];
        if (slot.generation != generation) {
            ++stale_completion_events_;
            return CompletionEventResult::kStaleGeneration;
        }
        switch (slot.state) {
            case SlotState::kInFlight:
                slot.state = SlotState::kCompleted;
                --in_flight_;
                ++completion_events_;
                return CompletionEventResult::kCompleted;
            case SlotState::kCompleted:
                ++duplicate_completion_events_;
                return CompletionEventResult::kDuplicate;
            case SlotState::kAbandoned:
                ++ignored_abandoned_events_;
                return CompletionEventResult::kAlreadyAbandoned;
            case SlotState::kFree:
                break; // a same-epoch ticket for a freed slot: recycled away
        }
        ++stale_completion_events_;
        return CompletionEventResult::kStaleGeneration;
    }

    // --- Ticket queries: in-flight handle lifetime, read-only, any state ---
    // The importer uses this to reason about a borrowed texture:
    // kInFlight => may be read by the backend; kCompleted => confirmed not
    // read; kAbandoned => UNKNOWN lifetime, treat conservatively until the
    // backend session itself is verified dead; kUnknownStale => this core no
    // longer tracks the ticket (reset or reclaim bumped its epoch), and the
    // integrator must apply its own conservative policy.
    TicketStatus ticket_status(std::uint64_t ticket) const noexcept {
        std::uint32_t index = 0;
        std::uint64_t generation = 0;
        if (!decode_ticket(ticket, index, generation)) return TicketStatus::kUnknownStale;
        const Slot& slot = slots_[index];
        if (slot.generation != generation) return TicketStatus::kUnknownStale;
        switch (slot.state) {
            case SlotState::kInFlight: return TicketStatus::kInFlight;
            case SlotState::kCompleted: return TicketStatus::kCompleted;
            case SlotState::kAbandoned: return TicketStatus::kAbandoned;
            case SlotState::kFree: break;
        }
        return TicketStatus::kUnknownStale;
    }

    // Scheduling view for the backend dispatcher: the oldest still in-flight
    // submission by internal ordinal (never by compositor nFrameId). Bounded
    // O(capacity) scan; outputs are zeroed and false when nothing is queued.
    // This is read-only inspection, NOT a dequeue: repeated calls keep
    // returning the same head until its ticket's state changes elsewhere. The
    // core deliberately offers no consume step, so the owner (the future
    // dispatching presenter) must separately prevent dispatching the same
    // submission twice by tracking dispatched tickets itself. Completing a
    // ticket requires actual confirmation that the backend stopped reading
    // the source, NOT merely handing it to the backend. What the core
    // tracks are scheduled submissions and their borrowed metadata, never GPU
    // or texture ownership of any kind.
    bool peek_next_in_flight(std::uint64_t* out_ticket, vr::PresentInfo_t* out_info) const noexcept {
        if (out_ticket != nullptr) *out_ticket = kInvalidTicket;
        if (out_info != nullptr) *out_info = vr::PresentInfo_t{};
        const Slot* best = nullptr;
        std::uint32_t best_index = 0;
        for (std::uint32_t index = 0; index < capacity_; ++index) {
            const Slot& slot = slots_[index];
            if (slot.state != SlotState::kInFlight) continue;
            if (best == nullptr || slot.submission_ordinal < best->submission_ordinal) {
                best = &slot;
                best_index = index;
            }
        }
        if (best == nullptr) return false;
        if (out_ticket != nullptr) *out_ticket = make_ticket(best_index, best->generation);
        if (out_info != nullptr) *out_info = best->info;
        return true;
    }

    // --- Explicit, bounded reclamation ---
    // Terminal slots (kCompleted/kAbandoned) are retained until the owner
    // reclaims them, so ticket queries stay truthful for a bounded session.
    // Reclaim bumps each freed slot's generation: any later callback naming
    // an old ticket of that slot is then classified kStaleGeneration. O(1)
    // per slot, one pass, returns how many slots became free.
    std::uint32_t reclaim_terminal() noexcept {
        std::uint32_t reclaimed = 0;
        for (std::uint32_t index = 0; index < capacity_; ++index) {
            Slot& slot = slots_[index];
            if (slot.state != SlotState::kCompleted && slot.state != SlotState::kAbandoned) continue;
            slot.state = SlotState::kFree;
            ++slot.generation;
            --occupied_;
            ++reclaimed;
        }
        return reclaimed;
    }

    // --- Session transitions: fail-closed, bounded, resource-silent ---
    // Each of these abandons every in-flight slot (confirmations will never
    // arrive), clears vsync trust (the hardware observation stream is gone),
    // bumps the session token (queued callbacks of the old backend are then
    // kStaleSession, even against an empty new stream), and returns how many
    // tickets were abandoned. None of them destroys, closes, or clears any
    // texture: handle values are simply no longer advanced by this core, and
    // the importer keeps ownership decisions.
    std::uint32_t disconnect() noexcept {
        const std::uint32_t abandoned = abandon_all_in_flight();
        vsync_valid_ = false;
        ++session_token_;
        if (state_ != LifecycleState::kDisabled) state_ = LifecycleState::kDisconnected;
        return abandoned;
    }

    std::uint32_t notify_backend_failed() noexcept {
        const std::uint32_t abandoned = abandon_all_in_flight();
        vsync_valid_ = false;
        ++session_token_;
        if (state_ != LifecycleState::kDisabled) state_ = LifecycleState::kBackendFailed;
        return abandoned;
    }

    // New session, same configured capacity: clears the queue (bumping the
    // generation of every live slot so old tickets resolve as stale), bumps
    // the session token so old vsync callbacks die as kStaleSession, zeroes
    // all session counters, and clears vsync validity. The token itself is
    // never zeroed or reused: it is lifetime monotonic on purpose, otherwise
    // an old callback could match a later session of the same value. A
    // kDisabled object stays kDisabled: capacity is chosen at construction
    // only.
    void reset() noexcept {
        for (std::uint32_t index = 0; index < capacity_; ++index) {
            Slot& slot = slots_[index];
            if (slot.state == SlotState::kFree) continue;
            slot.state = SlotState::kFree;
            ++slot.generation;
        }
        occupied_ = 0;
        in_flight_ = 0;
        submission_counter_ = 0;
        rejected_invalid_submits_ = 0;
        rejected_full_submits_ = 0;
        rejected_detached_submits_ = 0;
        completion_events_ = 0;
        duplicate_completion_events_ = 0;
        stale_completion_events_ = 0;
        ignored_abandoned_events_ = 0;
        abandoned_slots_lifetime_ = 0;
        vsync_observations_ = 0;
        vsync_valid_ = false;
        last_vsync_seconds_ = 0.0;
        last_vsync_counter_ = 0;
        ++session_token_; // invalidates queued callbacks; never zeroed back
        if (state_ != LifecycleState::kDisabled) state_ = LifecycleState::kReady;
    }

    // --- Hardware vsync: adopt only externally confirmed observations ---
    // The integrator (a future D3DKMT wait-vblank wrapper or equivalent)
    // delivers what it actually observed, together with the session token it
    // subscribed under. Rules, in fixed order: a token that does not name the
    // current session is rejected first, even if it would be the session's
    // very first event, so a callback queued by an old backend before a
    // reset/disconnect/failure cannot trust-poison the new stream; then
    // non-kReady is rejected; non-finite timestamps are rejected like the byte
    // gate does; after the first trusted observation the timestamp AND the
    // counter must both strictly advance, otherwise the event is rejected and
    // the last trusted pair stays intact. No positivity is invented: a finite
    // negative epoch-style timestamp is legal, matching "no invented rules" in
    // the contract header. Completions never call this and this never
    // completes anything: fence != vblank.
    VsyncEventResult notify_vsync_observed(std::uint64_t expected_session_token,
                                           std::uint64_t hardware_frame_counter,
                                           double timestamp_seconds) noexcept {
        if (expected_session_token != session_token_) return VsyncEventResult::kStaleSession;
        if (state_ != LifecycleState::kReady) return VsyncEventResult::kDetached;
        if (!std::isfinite(timestamp_seconds)) return VsyncEventResult::kNonFiniteTimestamp;
        if (vsync_valid_ && (timestamp_seconds <= last_vsync_seconds_ ||
                             hardware_frame_counter <= last_vsync_counter_)) {
            return VsyncEventResult::kNotMonotonic;
        }
        last_vsync_seconds_ = timestamp_seconds;
        last_vsync_counter_ = hardware_frame_counter;
        vsync_valid_ = true;
        ++vsync_observations_;
        return VsyncEventResult::kAccepted;
    }

    // Mirror of the IVRVirtualDisplay query shape WITHOUT a hidden clock:
    // the caller supplies its own observation of "now" in the same timestamp
    // domain it used for notify_vsync_observed. Returns false, outputs zeroed
    // and not invented, before any trusted event, after any reset/disconnect/
    // failure, for a non-finite now, or when now lies below the last trusted
    // vsync (a backward caller clock is not exposed as a negative delta).
    // Even two finite endpoints can defeat a naive subtraction: now - last
    // overflows double near the range ends, and a huge but finite double
    // would cast to +inf float. The delta itself is therefore checked to be
    // finite and within float's representable range before anything is
    // written; rejections leave the zeroed outputs untouched.
    bool get_time_since_last_vsync(double observed_now_seconds,
                                   float* seconds_since_last_vsync,
                                   std::uint64_t* hardware_frame_counter) const noexcept {
        if (seconds_since_last_vsync != nullptr) *seconds_since_last_vsync = 0.0f;
        if (hardware_frame_counter != nullptr) *hardware_frame_counter = 0;
        if (!vsync_valid_) return false;
        if (!std::isfinite(observed_now_seconds)) return false;
        if (observed_now_seconds < last_vsync_seconds_) return false;
        const double delta_seconds = observed_now_seconds - last_vsync_seconds_;
        if (!std::isfinite(delta_seconds) ||
            delta_seconds > static_cast<double>(std::numeric_limits<float>::max())) {
            return false; // finite endpoints, unrepresentable result: fail closed
        }
        if (seconds_since_last_vsync != nullptr) {
            *seconds_since_last_vsync = static_cast<float>(delta_seconds);
        }
        if (hardware_frame_counter != nullptr) *hardware_frame_counter = last_vsync_counter_;
        return true;
    }

    // --- State and bounded counters (all O(1) reads) ---
    LifecycleState state() const noexcept { return state_; }
    std::uint32_t configured_capacity() const noexcept { return capacity_; }
    std::uint32_t occupied_count() const noexcept { return occupied_; }
    std::uint32_t in_flight_count() const noexcept { return in_flight_; }
    // Submission counter: accepted submits in this session. Distinct by
    // construction from the compositor nFrameId and from the hardware vsync
    // frame counter; none is derived from another and none resets the other.
    std::uint64_t submission_count() const noexcept { return submission_counter_; }
    bool vsync_confirmed() const noexcept { return vsync_valid_; }
    std::uint64_t vsync_observations() const noexcept { return vsync_observations_; }
    // Token the integrator must pass as expected_session_token to
    // notify_vsync_observed; read it when subscribing and hand it back with
    // every delivered tick. Lifetime monotonic: never reset, never reused.
    std::uint64_t session_token() const noexcept { return session_token_; }

    std::uint64_t rejected_invalid_submits() const noexcept { return rejected_invalid_submits_; }
    std::uint64_t rejected_full_submits() const noexcept { return rejected_full_submits_; }
    std::uint64_t rejected_detached_submits() const noexcept { return rejected_detached_submits_; }
    std::uint64_t completion_events() const noexcept { return completion_events_; }
    std::uint64_t duplicate_completion_events() const noexcept { return duplicate_completion_events_; }
    std::uint64_t stale_completion_events() const noexcept { return stale_completion_events_; }
    std::uint64_t ignored_abandoned_events() const noexcept { return ignored_abandoned_events_; }
    std::uint64_t abandoned_slots_lifetime() const noexcept { return abandoned_slots_lifetime_; }

private:
    static constexpr std::uint32_t kTicketSlotBits = 8;
    static_assert(kMaxCapacity <= (1u << kTicketSlotBits), "ticket slot field must hold max capacity");

    enum class SlotState : std::uint8_t { kFree, kInFlight, kCompleted, kAbandoned };

    struct Slot {
        vr::PresentInfo_t info{};
        std::uint64_t generation = 0; // ticket epoch; 0 means "never used"
        std::uint64_t submission_ordinal = 0;
        SlotState state = SlotState::kFree;
    };

    static std::uint64_t make_ticket(std::uint32_t index, std::uint64_t generation) noexcept {
        return (generation << kTicketSlotBits) | static_cast<std::uint64_t>(index);
    }

    bool decode_ticket(std::uint64_t ticket,
                       std::uint32_t& index_out,
                       std::uint64_t& generation_out) const noexcept {
        if (ticket == kInvalidTicket) return false;
        const std::uint32_t index = static_cast<std::uint32_t>(ticket & ((1ull << kTicketSlotBits) - 1ull));
        const std::uint64_t generation = ticket >> kTicketSlotBits;
        if (generation == 0 || index >= capacity_) return false;
        index_out = index;
        generation_out = generation;
        return true;
    }

    // Shared fail-closed body of disconnect()/notify_backend_failed():
    // in-flight slots become kAbandoned and stay occupied with their metadata
    // intact until the owner reclaims them; nothing is dereferenced or freed.
    std::uint32_t abandon_all_in_flight() noexcept {
        std::uint32_t abandoned = 0;
        for (std::uint32_t index = 0; index < capacity_; ++index) {
            Slot& slot = slots_[index];
            if (slot.state != SlotState::kInFlight) continue;
            slot.state = SlotState::kAbandoned;
            ++abandoned;
            ++abandoned_slots_lifetime_;
        }
        in_flight_ = 0;
        return abandoned;
    }

    static_assert(sizeof(vr::PresentInfo_t) == 32, "lifecycle slots assume the pinned ABI");
    static_assert(std::is_trivially_copyable<vr::PresentInfo_t>::value,
                  "metadata is stored by value into fixed slots");

    std::uint32_t capacity_ = 0; // 0 <=> object is kDisabled
    std::uint32_t occupied_ = 0;
    std::uint32_t in_flight_ = 0;
    LifecycleState state_ = LifecycleState::kDisabled;
    bool vsync_valid_ = false;
    double last_vsync_seconds_ = 0.0;
    std::uint64_t last_vsync_counter_ = 0;
    std::uint64_t submission_counter_ = 0;
    std::uint64_t rejected_invalid_submits_ = 0;
    std::uint64_t rejected_full_submits_ = 0;
    std::uint64_t rejected_detached_submits_ = 0;
    std::uint64_t completion_events_ = 0;
    std::uint64_t duplicate_completion_events_ = 0;
    std::uint64_t stale_completion_events_ = 0;
    std::uint64_t ignored_abandoned_events_ = 0;
    std::uint64_t abandoned_slots_lifetime_ = 0;
    std::uint64_t vsync_observations_ = 0;
    std::uint64_t session_token_ = 1; // start live; bumps on invalidating transitions
    Slot slots_[kMaxCapacity];
};

static_assert(std::is_final<PresentLifecycle>::value, "lifecycle is a leaf, never an ABI interface");
static_assert(sizeof(PresentLifecycle) <= 8192, "fixed footprint: no heap, no growth, bounded state");
static_assert(!std::is_base_of<vr::IVRVirtualDisplay, PresentLifecycle>::value,
              "this core must not derive from the SDK interface; registration is a separate, later step");
} // namespace psvr
