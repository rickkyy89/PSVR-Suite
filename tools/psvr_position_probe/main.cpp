// SPDX-License-Identifier: MIT
// Read-only psvr.position v1 loopback probe. It reuses the driver's strict
// parser and bridge (driver/position_receiver.hpp -> position_protocol.hpp)
// so the tool can never drift from the wire contract shared with
// tools/psvr_camera. Receive-only: no USB, no camera, no driver install, no
// SteamVR/OpenVR calls, no invented samples. Freshness/tracking-loss uses the
// local monotonic clock only; the sender's clocks stay diagnostics.
//
// NOTE (B7-07): this probe is v1-only by design. Bound v2 requires
// bind_driver (driver session/epoch binding), which the probe never performs,
// so a v2-only sender yields "no valid frames" (exit 4): the expected
// diagnosis, not a probe failure.
//
// IMPORTANT: position_receiver.hpp includes <winsock2.h>, which must precede
// <windows.h>.
#include "position_receiver.hpp"
#include <windows.h>

#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>

namespace {
using Json = nlohmann::json;
using Clock = psvr::PositionClock;

constexpr unsigned default_port = 4271;
constexpr unsigned default_duration_ms = 2000;
constexpr unsigned default_timeout_ms = 200;

struct Options {
    unsigned port = default_port;   // 0 = ephemeral, reported in the ready event
    unsigned duration_ms = default_duration_ms;
    unsigned timeout_ms = default_timeout_ms;
    bool json = false;              // JSON lines instead of the human summary
};

// Digits only, value bounded before it can overflow; empty/signed/decimal or
// any trailing junk is rejected.
bool parse_number(const std::wstring& text, unsigned long long maximum, unsigned long long& out) {
    if (text.empty()) return false;
    unsigned long long value = 0;
    for (const wchar_t ch : text) {
        if (ch < L'0' || ch > L'9') return false;
        value = value * 10 + static_cast<unsigned long long>(ch - L'0');
        if (value > maximum) return false;
    }
    out = value;
    return true;
}

// Strict CLI: every option at most once, every value required, no unknown
// tokens. Runs completely before any socket exists.
bool parse(int argc, wchar_t** argv, Options& options) {
    bool port_seen = false, duration_seen = false, timeout_seen = false, json_seen = false;
    for (int i = 1; i < argc; ++i) {
        const std::wstring arg(argv[i]);
        unsigned long long value = 0;
        if (arg == L"--port" && !port_seen && i + 1 < argc) {
            port_seen = true;
            if (!parse_number(argv[++i], 65535, value)) return false;
            options.port = static_cast<unsigned>(value);
        } else if (arg == L"--duration-ms" && !duration_seen && i + 1 < argc) {
            duration_seen = true;
            if (!parse_number(argv[++i], 60000, value)) return false;
            if (value < 100) return false;
            options.duration_ms = static_cast<unsigned>(value);
        } else if (arg == L"--timeout-ms" && !timeout_seen && i + 1 < argc) {
            timeout_seen = true;
            if (!parse_number(argv[++i], 5000, value)) return false;
            if (value < 20) return false;
            options.timeout_ms = static_cast<unsigned>(value);
        } else if (arg == L"--json" && !json_seen) {
            json_seen = true;
            options.json = true;
        } else {
            return false;
        }
    }
    return true;
}

void emit(const Json& event) {
    std::cout << event.dump() << '\n' << std::flush;
    if (!std::cout) throw std::runtime_error("Cannot write probe output");
}

const char* view_state_name(psvr::PositionState state) {
    switch (state) {
    case psvr::PositionState::disabled: return "disabled";
    case psvr::PositionState::waiting: return "waiting";
    case psvr::PositionState::tracking: return "tracking";
    case psvr::PositionState::lost: return "lost";
    }
    return "unknown";
}

Json to_array(const psvr::Vec3& v) { return Json::array({v.x, v.y, v.z}); }

long long ms_between(Clock::time_point from, Clock::time_point to) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(to - from).count();
}

int probe(const Options& options) {
    psvr::PositionConfig config;
    config.enabled = true;  // The bridge only reports position when enabled.
    config.port = static_cast<std::uint16_t>(options.port);
    config.timeout_ms = static_cast<int>(options.timeout_ms);
    psvr::PositionBridge bridge(config);

    // The sink runs on this same thread inside poll(), so `pending` is the one
    // parsed datagram this iteration consumed, if any.
    std::optional<psvr::PositionSample> pending;
    psvr::PositionReceiver receiver(config, [&](const psvr::PositionSample& sample) {
        bridge.ingest(sample);
        pending = sample;
    });

    // Deterministic startup handshake: automation waits for this exact line
    // before it sends anything, so there is no bind/send race and no flaky
    // "retry until the port answers" loop.
    emit(Json{{"event", "ready"},
              {"tool", "psvr_position_probe"},
              {"protocol", psvr::position_protocol_name},
              {"version", psvr::position_protocol_version},
              {"frame", psvr::position_frame},
              {"units", psvr::position_units},
              {"transport", "udp-loopback-receive-only"},
              {"port_option", options.port},
              {"bound_port", receiver.local_port()},
              {"duration_ms", options.duration_ms},
              {"timeout_ms", options.timeout_ms},
              {"json_lines", options.json},
              {"pid", static_cast<unsigned>(GetCurrentProcessId())}});

    const auto start = Clock::now();
    const auto deadline = start + std::chrono::milliseconds(options.duration_ms);

    bool fatal = false;
    bool have_sequence = false;
    std::uint64_t first_sequence = 0, last_sequence = 0;
    unsigned long long valid_observations = 0;   // polls whose view was valid
    bool ever_tracking = false;                  // view reached tracking at least once
    bool loss_observed = false;                  // tracking -> non-tracking after that
    unsigned recoveries = 0;                     // non-tracking -> tracking re-entries
    bool have_valid = false;
    long long first_valid_ms = 0;
    bool have_displacement = false;
    double max_displacement = 0.0, final_displacement = 0.0;
    std::string previous_state = view_state_name(bridge.view(start, 0).state);

    for (;;) {
        const auto now_before = Clock::now();
        if (now_before >= deadline) break;
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                                   deadline - now_before).count();
        const int slice = static_cast<int>(std::min<long long>(50, remaining));
        if (receiver.poll(slice) < 0) { fatal = true; break; }

        const auto now = Clock::now();
        const auto elapsed_ms = ms_between(start, now);
        if (pending) {
            if (!have_sequence) { first_sequence = pending->seq; have_sequence = true; }
            last_sequence = pending->seq;
            if (options.json) {
                emit(Json{{"event", "packet"},
                          {"elapsed_ms", elapsed_ms},
                          {"seq", pending->seq},
                          {"valid", pending->valid},
                          {"state", psvr::position_packet_state_name(pending->state)},
                          {"source", psvr::position_source_name(pending->source)},
                          {"position_m", pending->valid ? to_array(pending->position_m)
                                                        : Json(nullptr)},
                          {"monotonic_s", pending->monotonic_s},
                          {"has_orientation", pending->has_orientation}});
            }
            pending.reset();
        }

        // Freshness is re-evaluated on every poll even when no datagram
        // arrived: that is what turns a silent sender into a local timeout.
        const auto view = bridge.view(now, 0);
        const std::string current_state = view_state_name(view.state);
        if (view.valid) {
            ++valid_observations;
            const double displacement = view.position.norm();  // seated-origin frame
            if (!have_valid) { have_valid = true; first_valid_ms = elapsed_ms; }
            if (!have_displacement || displacement > max_displacement) max_displacement = displacement;
            final_displacement = displacement;
            have_displacement = true;
        }
        if (current_state != previous_state) {
            if (ever_tracking) {
                if (previous_state == "tracking" && current_state != "tracking") loss_observed = true;
                if (current_state == "tracking") ++recoveries;
            }
            if (current_state == "tracking") ever_tracking = true;
            if (options.json) {
                emit(Json{{"event", "state"},
                          {"elapsed_ms", elapsed_ms},
                          {"from", previous_state},
                          {"to", current_state},
                          {"last_seq", have_sequence ? Json(last_sequence) : Json(nullptr)},
                          {"position_rel_m", to_array(view.position)},
                          {"age_ms", view.received_at == Clock::time_point{}
                                         ? Json(-1) : Json(ms_between(view.received_at, now))}});
            }
            previous_state = current_state;
        }
    }

    const auto end = Clock::now();
    const auto receiver_counters = receiver.counters();
    const auto bridge_counters = bridge.counters();
    const auto final_view = bridge.view(end, 0);

    int exit_code = 0;
    std::string reason;
    if (fatal) { exit_code = 3; reason = "receiver_fatal"; }
    else if (bridge_counters.accepted > 0) { exit_code = 0; reason = "valid_position_observed"; }
    else if (receiver_counters.datagrams > 0) { exit_code = 4; reason = "no_valid_frames"; }
    else { exit_code = 4; reason = "no_datagrams"; }

    Json summary{{"event", "summary"},
        {"tool", "psvr_position_probe"},
        {"protocol", psvr::position_protocol_name},
        {"version", psvr::position_protocol_version},
        {"frame", psvr::position_frame},
        {"units", psvr::position_units},
        {"source", "loopback UDP receive-only; no USB, no SteamVR"},
        {"port_option", options.port},
        {"bound_port", receiver.local_port()},
        {"duration_ms", options.duration_ms},
        {"timeout_ms", options.timeout_ms},
        {"elapsed_ms", ms_between(start, end)},
        {"receiver", {{"datagrams", receiver_counters.datagrams},
                      {"parsed", receiver_counters.parsed},
                      {"malformed", receiver_counters.malformed},
                      {"oversized", receiver_counters.oversized},
                      {"errors", receiver_counters.errors},
                      {"fatal", receiver_counters.fatal}}},
        {"bridge", {{"accepted", bridge_counters.accepted},
                    {"invalid", bridge_counters.invalid},
                    {"duplicates", bridge_counters.duplicates},
                    {"out_of_order", bridge_counters.out_of_order},
                    {"gaps", bridge_counters.gaps},
                    {"resyncs", bridge_counters.resyncs},
                    {"rejected", bridge_counters.rejected}}},
        {"first_sequence", have_sequence ? Json(first_sequence) : Json(nullptr)},
        {"last_sequence", have_sequence ? Json(last_sequence) : Json(nullptr)},
        {"valid_frames_ingested", bridge_counters.accepted},
        {"valid_snapshot_observations", valid_observations},
        {"first_valid_elapsed_ms", have_valid ? Json(first_valid_ms) : Json(nullptr)},
        {"max_displacement_m", have_displacement ? Json(max_displacement) : Json(nullptr)},
        {"final_displacement_m", have_displacement ? Json(final_displacement) : Json(nullptr)},
        {"tracking_loss_observed", loss_observed},
        {"tracking_recovery_count", recoveries},
        {"final_view", {{"state", view_state_name(final_view.state)},
                        {"valid", final_view.valid},
                        {"position_rel_m", to_array(final_view.position)},
                        {"age_ms", final_view.received_at == Clock::time_point{}
                                       ? Json(-1) : Json(ms_between(final_view.received_at, end))}}},
        {"exit_code", exit_code},
        {"reason", reason}};

    if (options.json) {
        emit(summary);
    } else {
        std::cout << "psvr_position_probe 127.0.0.1:" << receiver.local_port()
                  << " for " << options.duration_ms << " ms (freshness "
                  << options.timeout_ms << " ms, local monotonic)\n"
                  << "receiver: datagrams=" << receiver_counters.datagrams
                  << " parsed=" << receiver_counters.parsed
                  << " malformed=" << receiver_counters.malformed
                  << " oversized=" << receiver_counters.oversized
                  << " errors=" << receiver_counters.errors
                  << " fatal=" << receiver_counters.fatal << '\n'
                  << "bridge: accepted=" << bridge_counters.accepted
                  << " invalid=" << bridge_counters.invalid
                  << " duplicates=" << bridge_counters.duplicates
                  << " out_of_order=" << bridge_counters.out_of_order
                  << " gaps=" << bridge_counters.gaps
                  << " resyncs=" << bridge_counters.resyncs
                  << " rejected=" << bridge_counters.rejected << '\n'
                  << "valid frames ingested=" << bridge_counters.accepted
                  << " valid snapshot observations=" << valid_observations
                  << " (first at " << (have_valid ? std::to_string(first_valid_ms) : std::string("never"))
                  << " ms)\n"
                  << "displacement from first seated origin: "
                  << "max=" << (have_displacement ? std::to_string(max_displacement) : std::string("n/a"))
                  << " m final=" << (have_displacement ? std::to_string(final_displacement) : std::string("n/a"))
                  << " m\n"
                  << "tracking loss observed=" << (loss_observed ? "yes" : "no")
                  << " recoveries=" << recoveries << '\n'
                  << "result: " << reason << " (exit " << exit_code << ")\n" << std::flush;
    }
    return exit_code;
}

const char* const usage_text = R"(Usage: psvr_position_probe [--port 0..65535] [--duration-ms 100..60000]
                           [--timeout-ms 20..5000] [--json]

Read-only, receive-only loopback UDP probe for the psvr.position v1 protocol
shared with tools/psvr_camera and driver/position_receiver.hpp. It never
sends, never touches USB or camera devices, never installs the driver and
never talks to the SteamVR runtime. Nothing is invented: every reported
number comes from a datagram actually received in the bounded window.

--port 0..65535   loopback port to bind (default 4271; 0 = ephemeral, the
                  bound port is reported in the ready event)
--duration-ms     observation window, 100..60000 ms (default 2000)
--timeout-ms      local monotonic freshness timeout that turns a silent
                  sender into tracking loss, 20..5000 ms (default 200);
                  the sender's own clocks are validated but never trusted
--json            emit JSON lines: ready, packet, state, summary

Stdout protocol: the first line is always the machine-readable 'ready' JSON
event, written after the socket is bound, so automation can wait for it
deterministically before sending. Without --json a human summary follows.

Exit: 0 valid position observed, 2 invalid CLI (validated before any socket),
3 bind failure or fatal socket error, 4 finished with no usable position.
)";
} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--help") {
        std::cout << usage_text;
        return 0;
    }
    try {
        Options options;
        if (!parse(argc, argv, options)) {
            emit({{"event", "error"},
                  {"tool", "psvr_position_probe"},
                  {"exit_code", 2},
                  {"message", "Invalid arguments; run --help. Options: --port 0..65535, "
                              "--duration-ms 100..60000, --timeout-ms 20..5000, --json."}});
            return 2;
        }
        return probe(options);
    } catch (const std::exception& error) {
        std::cerr << "Position probe failed: " << error.what() << '\n';
        try {
            emit({{"event", "error"}, {"tool", "psvr_position_probe"},
                  {"exit_code", 3}, {"message", error.what()}});
        } catch (...) {}
        return 3;
    }
}
