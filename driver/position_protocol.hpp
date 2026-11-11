// SPDX-License-Identifier: MIT
// M9 opt-in position input: the versioned metric JSON protocol shared with the
// camera sender (tools/psvr_camera/protocol.json, "psvr.position" version 1).
//
// This receiver mirrors the sender's strict validation so the two sides cannot
// drift. Positions are metres in the "psvr_world" frame (+X right, +Y up,
// -Z forward), right-handed. Freshness uses local monotonic receive time and,
// for bound v2, driver-domain IMU source time. Sender host_time_ns / monotonic_s
// are diagnostics only and never trusted for timing. Orientation from the camera is
// validated when present but is not used: the M9 pose owns orientation from the
// IMU, so the camera contributes position only.
//
// No unit guessing, no OpenTrack/AITrack auto-detection, no invented position.
// This header is pure (no sockets) so validation, frame offset, sequence
// handling, recenter and freshness are exercised by CTest without hardware.
#pragma once

#include "orientation_math.hpp"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace psvr {

// Local monotonic clock shared with the IMU bridge (both are steady_clock).
using PositionClock = std::chrono::steady_clock;

inline constexpr const char* position_protocol_name = "psvr.position";
// B7-07 (comments only): this constant is the default sender version, not the
// maximum supported: the strict parser below accepts both v1 and v2.
inline constexpr int position_protocol_version = 1;
inline constexpr const char* position_frame = "psvr_world";
inline constexpr const char* position_units = "m";
inline constexpr double position_quaternion_tolerance = 1e-3;
// Hard protocol limits. The full v1 packet is a few hundred bytes; a datagram
// larger than this is never a valid packet.
inline constexpr std::size_t position_max_packet_bytes = 1024;
inline constexpr std::size_t position_max_depth = 8;

enum class PositionPacketState { tracking, lost, calibrating, ambiguous, error };
enum class PositionSource { aruco, blob_led, none };

inline const char* position_packet_state_name(PositionPacketState state) {
    switch (state) {
    case PositionPacketState::tracking: return "tracking";
    case PositionPacketState::lost: return "lost";
    case PositionPacketState::calibrating: return "calibrating";
    case PositionPacketState::ambiguous: return "ambiguous";
    case PositionPacketState::error: return "error";
    }
    return "unknown";
}

inline std::optional<PositionPacketState> parse_position_packet_state(std::string_view text) {
    if (text == "tracking") return PositionPacketState::tracking;
    if (text == "lost") return PositionPacketState::lost;
    if (text == "calibrating") return PositionPacketState::calibrating;
    if (text == "ambiguous") return PositionPacketState::ambiguous;
    if (text == "error") return PositionPacketState::error;
    return std::nullopt;
}

inline const char* position_source_name(PositionSource source) {
    switch (source) {
    case PositionSource::aruco: return "aruco";
    case PositionSource::blob_led: return "blob_led";
    case PositionSource::none: return "none";
    }
    return "unknown";
}

inline std::optional<PositionSource> parse_position_source(std::string_view text) {
    if (text == "aruco") return PositionSource::aruco;
    if (text == "blob_led") return PositionSource::blob_led;
    if (text == "none") return PositionSource::none;
    return std::nullopt;
}

struct PositionConfig {
    bool enabled = false;
    std::uint16_t port = 4271;
    int timeout_ms = 200;
    int grace_ms = 0; // opt-in last-good hold; zero preserves legacy freshness
    double max_abs_m = 10.0;
    std::uint64_t max_sequence_gap = 1000;
    // Explicit yaw from the camera world frame (psvr_world) to the IMU's
    // initial gravity-aligned frame, in degrees about +Y. Both frames are
    // gravity-aligned, so only yaw is physically meaningful. The default 0 is
    // the documented neutral assumption, NOT a measurement: the camera/IMU
    // mounting yaw is an open physical-calibration gate (see
    // docs/MILESTONE_9_SOFTWARE.md). It is applied to the translation together
    // with the IMU world-yaw reference so a yaw recenter never leaves position
    // and orientation in different frames.
    double camera_yaw_offset_deg = 0.0;
    bool require_bound_v2 = false;
};

struct PositionSample {
    int version = 1;
    std::string session_id;
    std::uint64_t recenter_epoch = 0, imu_seq = 0;
    double imu_sample_time_s = 0;
    Vec3 position_m{};                 // metres; meaningful only when valid
    bool valid = false;                // sender says this frame is usable
    std::uint64_t seq = 0;
    PositionPacketState state = PositionPacketState::lost;
    PositionSource source = PositionSource::none;
    double monotonic_s = 0.0;          // sender monotonic seconds (diagnostics)
    std::uint64_t host_time_ns = 0;    // sender wall clock (diagnostics)
    bool has_orientation = false;
    Quaternion orientation{};          // validated but unused by the IMU pose
    PositionClock::time_point received_at{};
};

// Seated-frame offset: explicit subtraction, no scaling, no axis remap.
inline Vec3 seated_position(Vec3 raw, Vec3 origin) { return raw - origin; }

// Bring a camera-frame displacement into the recentered IMU world frame.
// `world_rotation` is the IMU world-yaw reference: the rotation the IMU applied
// to its gravity-aligned frame at recenter (ImuFusion::world_rotation()).
// `camera_yaw_offset_deg` is the explicit, physically-uncalibrated camera-to-IMU
// yaw. Applying the world reference here is what keeps the translation in the
// same frame as the recentered orientation: without it a yaw recenter would
// leave position and orientation rotated by the removed yaw. Both inputs are
// pure yaw, so they compose about +Y. A non-finite input degrades to the plain
// subtraction rather than throwing out of an SDK callback.
inline Vec3 aligned_position(Vec3 raw, Vec3 origin, Quaternion world_rotation,
                             double camera_yaw_offset_deg) {
    Quaternion frame = world_rotation * rotation_vector({0, camera_yaw_offset_deg / degrees, 0});
    const auto n = frame.norm();
    if (!std::isfinite(n) || n < 1e-12) return raw - origin;
    frame = {frame.w / n, frame.x / n, frame.y / n, frame.z / n};
    return frame.rotate(raw - origin);
}

namespace position_detail {
using Json = nlohmann::json;

inline bool finite_number(const Json& value) {
    return value.is_number() && std::isfinite(value.get<double>());
}

inline bool finite_vec(Vec3 value) {
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

inline bool finite_vector(const Json& value, std::size_t size) {
    if (!value.is_array() || value.size() != size) return false;
    for (const auto& element : value)
        if (!finite_number(element)) return false;
    return true;
}
} // namespace position_detail

// Strict parser mirroring tools/psvr_camera/protocol.py::validate_packet.
// Unknown keys are ignored for forward compatibility; duplicate keys, wrong
// types, unknown enum values, non-finite numbers, out-of-bounds positions and
// non-unit quaternions are rejected. Returns nullopt on any violation.
inline std::optional<PositionSample> parse_position_packet(std::string_view text,
        const PositionConfig& config, PositionClock::time_point received_at) {
    if (text.empty() || text.size() > position_max_packet_bytes) return std::nullopt;
    if (text.find('\0') != std::string_view::npos) return std::nullopt;
    try {
        // Reject duplicate keys and unbounded nesting before the DOM collapses
        // them. Unknown keys are tolerated, matching the reference validator.
        std::vector<std::set<std::string>> keys;
        bool rejected = false;
        const auto callback = [&](int, position_detail::Json::parse_event_t event,
                                  position_detail::Json& value) {
            using Event = position_detail::Json::parse_event_t;
            if (event == Event::object_start || event == Event::array_start) {
                if (keys.size() >= position_max_depth) { rejected = true; return false; }
                keys.emplace_back();
            } else if (event == Event::object_end || event == Event::array_end) {
                if (!keys.empty()) keys.pop_back();
            } else if (event == Event::key) {
                const auto key = value.get<std::string>();
                if (!keys.empty() && !keys.back().insert(key).second) { rejected = true; return false; }
            }
            return true;
        };
        const auto document = position_detail::Json::parse(text.begin(), text.end(), callback);
        if (rejected || !document.is_object()) return std::nullopt;

        if (!document.contains("protocol") || !document["protocol"].is_string() ||
            document["protocol"].get<std::string>() != position_protocol_name) return std::nullopt;
        if (!document.contains("version") || !document["version"].is_number_integer() ||
            (document["version"] != 1 && document["version"] != 2)) return std::nullopt;
        const int version = document["version"].get<int>();
        if (config.require_bound_v2 && version != 2) return std::nullopt;
        if (!document.contains("frame") || !document["frame"].is_string() ||
            document["frame"].get<std::string>() != (version == 2 ? "psvr_imu_unrecentered" : position_frame)) return std::nullopt;
        if (!document.contains("units") || !document["units"].is_string() ||
            document["units"].get<std::string>() != position_units) return std::nullopt;

        std::uint64_t seq = 0;
        if (!document.contains("seq")) return std::nullopt;
        if (document["seq"].is_number_unsigned()) seq = document["seq"].get<std::uint64_t>();
        else if (document["seq"].is_number_integer()) {
            const auto value = document["seq"].get<std::int64_t>();
            if (value < 0) return std::nullopt;
            seq = static_cast<std::uint64_t>(value);
        } else return std::nullopt;

        if (!document.contains("monotonic_s") || !position_detail::finite_number(document["monotonic_s"]) ||
            document["monotonic_s"].get<double>() < 0.0) return std::nullopt;
        const double monotonic_s = document["monotonic_s"].get<double>();

        if (!document.contains("host_time_ns")) return std::nullopt;
        std::uint64_t host_time_ns = 0;
        if (document["host_time_ns"].is_number_unsigned()) host_time_ns = document["host_time_ns"].get<std::uint64_t>();
        else if (document["host_time_ns"].is_number_integer()) {
            const auto value = document["host_time_ns"].get<std::int64_t>();
            if (value < 0) return std::nullopt;
            host_time_ns = static_cast<std::uint64_t>(value);
        } else return std::nullopt;

        if (!document.contains("valid") || !document["valid"].is_boolean()) return std::nullopt;
        const bool valid = document["valid"].get<bool>();

        if (!document.contains("state") || !document["state"].is_string()) return std::nullopt;
        const auto state = parse_position_packet_state(document["state"].get<std::string>());
        if (!state) return std::nullopt;
        if (!document.contains("source") || !document["source"].is_string()) return std::nullopt;
        const auto source = parse_position_source(document["source"].get<std::string>());
        if (!source) return std::nullopt;
        // Cross-field consistency (driver-side, stricter than the reference
        // sender validator): a usable frame must be `tracking`. The protocol
        // documents lost/error/ambiguous as valid:false, and a valid:true frame
        // with a non-tracking state is contradictory and must never be trusted
        // as a position. `tracking` with valid:false is allowed (degraded).
        if (valid && *state != PositionPacketState::tracking) return std::nullopt;

        Vec3 position;
        if (document.contains("position_m")) {
            const auto& position_value = document["position_m"];
            if (valid) {
                if (!position_detail::finite_vector(position_value, 3)) return std::nullopt;
            } else if (!position_value.is_null() && !position_detail::finite_vector(position_value, 3)) {
                return std::nullopt;
            }
            if (position_detail::finite_vector(position_value, 3)) {
                double* components[3] = {&position.x, &position.y, &position.z};
                for (std::size_t i = 0; i < 3; ++i) {
                    const double component = position_value[i].get<double>();
                    if (std::abs(component) > config.max_abs_m) return std::nullopt;
                    *components[i] = component;
                }
            }
        } else if (valid) {
            return std::nullopt;
        }

        bool has_orientation = false;
        Quaternion orientation;
        if (document.contains("orientation_wxyz") && !document["orientation_wxyz"].is_null()) {
            if (!position_detail::finite_vector(document["orientation_wxyz"], 4)) return std::nullopt;
            orientation = {document["orientation_wxyz"][0].get<double>(),
                           document["orientation_wxyz"][1].get<double>(),
                           document["orientation_wxyz"][2].get<double>(),
                           document["orientation_wxyz"][3].get<double>()};
            if (std::abs(orientation.norm() - 1.0) > position_quaternion_tolerance) return std::nullopt;
            has_orientation = true;
        }
        for (const char* key : {"reprojection_px", "ambiguity_ratio"}) {
            if (document.contains(key) && !document[key].is_null()) {
                if (!position_detail::finite_number(document[key]) || document[key].get<double>() < 0.0)
                    return std::nullopt;
            }
        }
        if (document.contains("inliers") && !document["inliers"].is_null()) {
            // Unsigned is accepted directly: a large uint64 is non-negative by
            // type and must not go through a signed conversion (no throw/catch
            // as control flow). Signed values are checked for negativity.
            if (document["inliers"].is_number_unsigned()) {
            } else if (document["inliers"].is_number_integer()) {
                if (document["inliers"].get<std::int64_t>() < 0) return std::nullopt;
            } else return std::nullopt;
        }
        if (document.contains("note") && !document["note"].is_null() && !document["note"].is_string())
            return std::nullopt;

        PositionSample sample;
        sample.version = version;
        if (version == 2) {
            if (!document.contains("session_id") || !document["session_id"].is_string()) return std::nullopt;
            sample.session_id = document["session_id"].get<std::string>();
            if (sample.session_id.empty() || sample.session_id.size() > 128 ||
                sample.session_id.find('\0') != std::string::npos) return std::nullopt;
            for (const char* key : {"recenter_epoch", "imu_seq"}) {
                if (!document.contains(key) || !document[key].is_number_integer() ||
                    (!document[key].is_number_unsigned() && document[key].get<std::int64_t>() < 0)) return std::nullopt;
            }
            if (!document.contains("imu_sample_time_s") ||
                !position_detail::finite_number(document["imu_sample_time_s"]) ||
                document["imu_sample_time_s"].get<double>() < 0) return std::nullopt;
            sample.recenter_epoch = document["recenter_epoch"].get<std::uint64_t>();
            sample.imu_seq = document["imu_seq"].get<std::uint64_t>();
            sample.imu_sample_time_s = document["imu_sample_time_s"].get<double>();
        }
        sample.position_m = position;
        sample.valid = valid;
        sample.seq = seq;
        sample.state = *state;
        sample.source = *source;
        sample.monotonic_s = monotonic_s;
        sample.host_time_ns = host_time_ns;
        sample.has_orientation = has_orientation;
        sample.orientation = orientation;
        sample.received_at = received_at;
        return sample;
    } catch (...) {
        return std::nullopt;
    }
}

enum class PositionState { disabled, waiting, tracking, lost };

// Thread-safe state machine combining sequence handling, local monotonic
// freshness, seated-frame offset and recenter epoch. It never invents a
// position: when disabled, invalid or not fresh, `view().valid` is false.
class PositionBridge {
public:
    struct View {
        bool enabled = false;
        bool valid = false;       // fresh, valid and coherent with the IMU epoch
        bool holding = false;     // last-good position, never a predicted translation
        bool coherent = true;     // position origin epoch matches the IMU epoch
        PositionState state = PositionState::disabled;
        Vec3 position{};          // seated frame, aligned to the recentered IMU frame
        PositionClock::time_point received_at{};
        unsigned epoch = 0;
    };

    explicit PositionBridge(PositionConfig config) : config_(config) {}

    bool enabled() const { return config_.enabled; }
    const PositionConfig& config() const { return config_; }

    // Caller supplies the current atomic tracker snapshot, never camera clocks.
    // Identity changes discard the old origin; no old packet may rebaseline it.
    void bind_driver(const std::string& session, unsigned epoch, double sample_time_s,
                     bool has_sample, PositionClock::time_point now) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (session != session_ || epoch != bound_epoch_) {
            session_ = session; bound_epoch_ = epoch;
            have_good_ = false;
            // Identity changes always discard origin/stream state (fail-closed):
            // no old packet may rebaseline or publish a pose with a stale
            // origin, including legacy v1 without grace.
            pending_epoch_ = epoch;
            have_ = false; have_origin_ = false; last_valid_ = false;
            epoch_floor_s_ = sample_time_s;
        }
        driver_sample_s_ = sample_time_s;
        driver_has_sample_ = has_sample;
        driver_now_s_ = std::chrono::duration<double>(now.time_since_epoch()).count();
    }

    // Accept a strictly validated packet. Returns true when it advances the
    // sequence. A valid=false packet advances the sequence and marks the stream
    // invalid without touching the last good position. Duplicates and in-window
    // reordering are rejected; a strictly older seq arriving after the stream
    // went stale is accepted as a fresh baseline (sender restart / recovery).
    bool ingest(const PositionSample& sample) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (faulted_) return false;
        if ((config_.require_bound_v2 && sample.version != 2) ||
            (sample.version != 1 && sample.version != 2)) { ++rejected_; return false; }
        if (sample.version == 2) {
            const double receive_s = std::chrono::duration<double>(sample.received_at.time_since_epoch()).count();
            if (session_.empty() || sample.session_id != session_ || sample.recenter_epoch != bound_epoch_ ||
                !std::isfinite(sample.imu_sample_time_s) || sample.imu_sample_time_s < 0 ||
                (sample.valid && (!driver_has_sample_ || sample.imu_sample_time_s < epoch_floor_s_ ||
                    sample.imu_sample_time_s > driver_sample_s_ || sample.imu_sample_time_s > receive_s ||
                    receive_s - sample.imu_sample_time_s > config_.timeout_ms*.001 ||
                    driver_now_s_ - sample.imu_sample_time_s > config_.timeout_ms*.001))) {
                ++rejected_; return false;
            }
        }
        // Defence in depth: the wire parser already rejects this, but a
        // directly-constructed sample must not smuggle a lost/error/ambiguous
        // frame in as a usable position.
        if (sample.valid && sample.state != PositionPacketState::tracking) { ++rejected_; return false; }
        if (sample.valid && !position_detail::finite_vec(sample.position_m)) { ++rejected_; return false; }
        if (sample.version != last_version_) {
            have_good_ = false;
            have_ = false; have_origin_ = false; last_valid_ = false;
        }
        if (sample.version == 2) pending_epoch_ = bound_epoch_;
        if (have_) {
            if (sample.seq == last_sequence_) { ++duplicates_; ++rejected_; return false; }
            if (sample.seq < last_sequence_) {
                const auto silence = std::chrono::duration<double>(sample.received_at - received_at_).count();
                if (silence <= config_.timeout_ms * 0.001) { ++out_of_order_; ++rejected_; return false; }
                ++resyncs_; // stream considered dead; accept as a new baseline
            } else if (sample.seq - last_sequence_ > config_.max_sequence_gap) {
                ++gaps_;
            }
        }
        last_sequence_ = sample.seq;
        received_at_ = sample.received_at;
        imu_sample_time_s_ = sample.imu_sample_time_s;
        last_valid_ = sample.valid;
        last_state_ = sample.state;
        have_ = true;
        last_version_ = sample.version;
        if (sample.valid) {
            good_raw_ = sample.position_m;
            good_received_at_ = sample.received_at;
            good_source_time_s_ = sample.imu_sample_time_s;
            have_good_ = true;
            // The first valid sample, and every completed recenter, fixes the
            // seated origin from the camera frame. Until then no position is
            // published, so the raw camera coordinates (whose origin is the
            // camera, not the seated head) can never leak out as a pose.
            if (!have_origin_ || pending_epoch_ != applied_epoch_) {
                origin_ = good_raw_;
                applied_epoch_ = pending_epoch_;
                have_origin_ = true;
            }
            ++accepted_;
        } else {
            // Terminal states must not be followed by lost packets that revive
            // an old position. Only a new good frame can arm the hold again.
            if (sample.state == PositionPacketState::error ||
                sample.state == PositionPacketState::calibrating)
                have_good_ = false;
            ++invalid_;
        }
        return true;
    }

    // Shared-epoch recenter. Captures the current raw position as the seated
    // origin only when the latest packet is valid and fresh; otherwise the
    // request stays pending and completes on the next valid sample.
    bool apply_recenter(PositionClock::time_point now, unsigned epoch) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (last_version_ == 2 && pending_epoch_ == epoch)
            return have_origin_ && applied_epoch_ == epoch;
        pending_epoch_ = epoch;
        have_good_ = false; // never carry a hold across an origin change
        if (config_.require_bound_v2 || last_version_ == 2) {
            have_ = false; have_origin_ = false; last_valid_ = false;
            return false;
        }
        if (!have_ || faulted_ || !last_valid_) return false;
        const auto age = std::chrono::duration<double>(now - received_at_).count();
        if (age < 0 || age > config_.timeout_ms * 0.001) return false;
        origin_ = good_raw_;
        applied_epoch_ = epoch;
        have_origin_ = true;
        return true;
    }

    void mark_faulted() {
        std::lock_guard<std::mutex> lock(mutex_);
        faulted_ = true;
    }

    // `imu_epoch` is the recenter epoch the IMU has applied and
    // `world_rotation` is the IMU world-yaw reference for that epoch. Position
    // is only reported when this bridge has applied the same epoch, so a
    // recenter can never mix an old origin with a new orientation. The
    // translation is rotated by the same world reference (plus the explicit
    // camera yaw offset) so both channels live in one frame.
    View view(PositionClock::time_point now, unsigned imu_epoch,
              Quaternion world_rotation = {}) const {
        std::lock_guard<std::mutex> lock(mutex_);
        View result;
        result.enabled = config_.enabled;
        result.epoch = applied_epoch_;
        if (!config_.enabled) { result.state = PositionState::disabled; return result; }
        result.coherent = applied_epoch_ == imu_epoch;
        if (faulted_) { result.state = PositionState::lost; result.coherent = false; return result; }
        if (!have_ || !have_origin_) { result.state = PositionState::waiting; return result; }
        result.received_at = received_at_;
        // B7-07 (comments only): the camera yaw offset applies to v1 only;
        // v2 frames already arrive in the IMU frame, so no offset is added.
        result.position = aligned_position(good_raw_, origin_, world_rotation,
            last_version_ == 2 ? 0.0 : config_.camera_yaw_offset_deg);
        if (config_.grace_ms > 0) {
            // Bound silence and transient loss by the SAME last-good receive
            // clock. Invalid traffic updates received_at_, not this deadline.
            const auto good_age = std::chrono::duration<double>(now - good_received_at_).count();
            const double hard_limit = std::min(config_.timeout_ms, 200) * .001;
            const double grace = std::min(config_.grace_ms, 200) * .001;
            const double source_age = std::chrono::duration<double>(now.time_since_epoch()).count()
                - good_source_time_s_;
            const bool transient = last_valid_ || last_state_ == PositionPacketState::lost ||
                last_state_ == PositionPacketState::ambiguous;
            result.received_at = good_received_at_;
            if (!result.coherent || pending_epoch_ != applied_epoch_) {
                result.state = PositionState::waiting;
                return result;
            }
            if (!have_good_ || !transient || good_age < 0 || good_age > grace ||
                good_age > hard_limit || (last_version_ == 2 &&
                (!std::isfinite(source_age) || source_age < 0 || source_age > hard_limit))) {
                result.state = last_state_ == PositionPacketState::calibrating
                    ? PositionState::waiting : PositionState::lost;
                return result;
            }
            result.valid = true;
            result.holding = !last_valid_ || good_age > 0;
            result.state = PositionState::tracking;
            return result;
        }
        const auto age = std::chrono::duration<double>(now - received_at_).count();
        const bool fresh = age >= 0 && age <= config_.timeout_ms * 0.001;
        if (!fresh) { result.state = PositionState::lost; return result; }
        if (!last_valid_) {
            result.state = last_state_ == PositionPacketState::calibrating ? PositionState::waiting
                                                                            : PositionState::lost;
            return result;
        }
        // A recent receive cannot extend the lifetime of an already-old source
        // frame. Re-evaluate driver-domain source age on every pose query, not
        // only at ingest. Invalid bound packets deliberately bypass this check.
        if (last_version_ == 2) {
            const double source_age = std::chrono::duration<double>(now.time_since_epoch()).count()
                - imu_sample_time_s_;
            if (!std::isfinite(source_age) || source_age < 0 || source_age > config_.timeout_ms*.001) {
                result.state = PositionState::lost;
                return result;
            }
        }
        if (!result.coherent) { result.state = PositionState::waiting; return result; }
        result.state = PositionState::tracking;
        result.valid = true;
        return result;
    }

    std::uint64_t last_sequence() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return last_sequence_;
    }

    // Diagnostics only; counters are monotonic and never reset.
    struct Counters {
        std::uint64_t accepted = 0, invalid = 0, duplicates = 0, out_of_order = 0,
                      gaps = 0, resyncs = 0, rejected = 0;
    };
    Counters counters() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return {accepted_, invalid_, duplicates_, out_of_order_, gaps_, resyncs_, rejected_};
    }

private:
    PositionConfig config_;
    std::string session_;
    double driver_sample_s_ = 0, driver_now_s_ = 0, epoch_floor_s_ = 0;
    double imu_sample_time_s_ = 0; // last accepted packet's driver-domain source time
    bool driver_has_sample_ = false;
    int last_version_ = 1;
    mutable std::mutex mutex_;
    bool have_ = false;
    bool have_origin_ = false;
    bool have_good_ = false;
    bool faulted_ = false;
    bool last_valid_ = false;
    PositionPacketState last_state_ = PositionPacketState::lost;
    std::uint64_t last_sequence_ = 0;
    Vec3 good_raw_{}, origin_{}; // invalid frames never overwrite last-good raw
    PositionClock::time_point received_at_{};
    PositionClock::time_point good_received_at_{};
    double good_source_time_s_ = 0;
    unsigned pending_epoch_ = 0, applied_epoch_ = 0, bound_epoch_ = 0;
    std::uint64_t accepted_ = 0, invalid_ = 0, duplicates_ = 0, out_of_order_ = 0,
                  gaps_ = 0, resyncs_ = 0, rejected_ = 0;
};

} // namespace psvr
