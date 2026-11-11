// SPDX-License-Identifier: MIT
// Winsock before anything that pulls in <windows.h> (hidapi.h below).
#include "position_receiver.hpp"
#include "headset_buttons.hpp"
#ifdef _WIN32
#include "headset_audio.hpp"
#endif
#include "imu_exporter.hpp"
#include "pose_bridge.hpp"
#include "position_continuity.hpp"
#include "imu_settings.hpp"
#include "imu_loss_report.hpp"
#include "pose_transition_trace.hpp"
#if defined(_MSC_VER)
#include "presenter_d3d11.hpp"
#endif
#include "device_session.hpp"
#include "display_component.hpp"
#if defined(__MINGW32__) && !defined(_MSC_VER)
#include "display_windows_abi.hpp"
#endif
#include "display_selection.hpp"
#include "optical_profile.hpp"
#include <hidapi.h>
#include <atomic>
#include <array>
#include <cstring>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <stdexcept>
#include <condition_variable>
#include <type_traits>
#include <utility>

namespace {
void log(const std::string& text) {
    // OpenVR's lazy accessor dereferences the context even when no sink exists.
    if (!vr::VRDriverContext()) return;
    if (auto* sink = vr::VRDriverLog()) sink->Log(("psvrpc: "+text).c_str());
}

// Keep argument construction INSIDE the protected callable (not just Log()).
template<class F> void best_effort(F&& action) noexcept {
    try { action(); } catch (...) {}
}
void log_failure(const char* text) noexcept { best_effort([&] { log(text); }); }

// Allocation-free rollback; declared after the lifecycle lock so it runs first.
template<class F> class ScopeExit {
    F action_;
    bool armed_ = true;
public:
    explicit ScopeExit(F action) noexcept(std::is_nothrow_move_constructible_v<F>)
        : action_(std::move(action)) {}
    ScopeExit(const ScopeExit&) = delete;
    ScopeExit& operator=(const ScopeExit&) = delete;
    ~ScopeExit() noexcept { run(); }
    void run() noexcept { if (armed_) { armed_ = false; best_effort(action_); } }
    void release() noexcept { armed_ = false; }
};

psvr::DisplayConfig load_display_config(vr::IVRSettings& settings) {
    constexpr const char* section = "driver_psvrpc";
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    psvr::DisplayConfig config;
    config.diagnostic_sink = [](const std::string& text) noexcept { log_failure(text.c_str()); };
    config.enabled = settings.GetBool(section,"displayEnabled",&error);
    if (error != vr::VRSettingsError_None) throw std::runtime_error("cannot read displayEnabled");
    if (!config.enabled) return config; // no geometry/projection required in tracking-only mode
    const auto integer = [&](const char* key) {
        const auto value = settings.GetInt32(section,key,&error);
        if (error != vr::VRSettingsError_None) throw std::runtime_error(std::string("missing display setting: ")+key);
        return value;
    };
    const auto positive = [&](const char* key) {
        const auto value = integer(key);
        if (value <= 0) throw std::runtime_error(std::string("display setting must be positive: ")+key);
        return static_cast<uint32_t>(value);
    };
    const auto tangent = [&](const char* key) {
        const auto value = settings.GetFloat(section,key,&error);
        if (error != vr::VRSettingsError_None) throw std::runtime_error(std::string("explicit projection setting required: ")+key);
        return value;
    };
    std::array<char,2048> path{};
    settings.GetString(section,"displayMonitorPath",path.data(),static_cast<uint32_t>(path.size()),&error);
    if (error != vr::VRSettingsError_None || path.back() != '\0') throw std::runtime_error("cannot read exact displayMonitorPath");
    config.monitor_path = path.data();
    config.native_direct_mode = settings.GetBool(section,"nativeDirectMode",&error);
    if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) config.native_direct_mode = false;
    else if (error != vr::VRSettingsError_None) throw std::runtime_error("cannot read nativeDirectMode");
    config.virtual_display = settings.GetBool(section,"virtualDisplayEnabled",&error);
    if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) config.virtual_display=false;
    else if (error != vr::VRSettingsError_None) throw std::runtime_error("cannot read virtualDisplayEnabled");
#if !defined(_MSC_VER)
    if (config.virtual_display) throw std::runtime_error("experimental virtual display requires native MSVC build");
#endif
    if (config.native_direct_mode) {
        config.edid_vendor_id = positive("edidVendorId");
        config.edid_product_id = positive("edidProductId");
        config.desktop_membership = [selected = config.monitor_path] {
            // B9-01: enumeration can retain active targets without a readable
            // monitor_device_path. A false result here is not yet reliable
            // absence evidence; do not use it to authorize the native gate.
            for (const auto& item : psvr::display::enumerate_displays(false))
                if (item.active && item.monitor_device_path && *item.monitor_device_path == selected) return true;
            return false;
        };
    }
    config.desktop_x = integer("displayX"); config.desktop_y = integer("displayY");
    config.panel_width = positive("displayWidth"); config.panel_height = positive("displayHeight");
    const auto orientation = integer("displayOrientationDegrees");
    if (orientation != 0) throw std::runtime_error("displayOrientationDegrees must be 0");
    config.orientation_degrees = 0;
    config.refresh_numerator = positive("displayRefreshNumerator");
    config.refresh_denominator = positive("displayRefreshDenominator");
    config.render_width = positive("renderWidth"); config.render_height = positive("renderHeight");
    config.projection[0] = {tangent("projectionLeftLeft"),tangent("projectionLeftRight"),
                            tangent("projectionLeftTop"),tangent("projectionLeftBottom")};
    config.projection[1] = {tangent("projectionRightLeft"),tangent("projectionRightRight"),
                             tangent("projectionRightTop"),tangent("projectionRightBottom")};
    std::array<char,32> optical_mode{};
    settings.GetString(section,"distortionMode",optical_mode.data(),static_cast<uint32_t>(optical_mode.size()),&error);
    if (error != vr::VRSettingsError_None || optical_mode.back() != '\0')
        throw std::runtime_error("cannot read distortionMode");
    if (std::strcmp(optical_mode.data(),"profile") == 0) {
        std::array<char,4096> profile_path{};
        settings.GetString(section,"distortionProfilePath",profile_path.data(),static_cast<uint32_t>(profile_path.size()),&error);
        if (error != vr::VRSettingsError_None || profile_path.back() != '\0' || !profile_path[0])
            throw std::runtime_error("profile mode requires distortionProfilePath");
        const auto path = std::filesystem::u8path(profile_path.data());
        if (!path.is_absolute()) throw std::runtime_error("distortionProfilePath must be absolute");
        const auto loaded = psvr::optics::load_profile(path);
        config.optical_mode = psvr::OpticalMode::profile;
        config.optical_mapper = loaded.mapper;
        config.optical_profile_name = loaded.name;
        log("optical profile loaded and mathematically validated (physical acceptance pending): "+loaded.name);
    } else if (std::strcmp(optical_mode.data(),"identity") != 0) {
        throw std::runtime_error("distortionMode must be identity or profile");
    }
    if (const auto* reason = config.validation_error()) throw std::runtime_error(reason);
    return config;
}

bool verify_selected_active_display(const psvr::DisplayConfig& config) {
    return psvr::matches_display_config(config,psvr::display::enumerate_displays(false));
}

// Opt-in position input. Missing positionEnabled means disabled (3DoF default).
// When enabled, core keys are mandatory; optional grace defaults to legacy 0.
// All supplied values are range-checked: an invalid
// configuration aborts activation instead of silently running without 6DoF.
psvr::PositionConfig load_position_config(vr::IVRSettings& settings) {
    constexpr const char* section = "driver_psvrpc";
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    psvr::PositionConfig config;
    config.enabled = settings.GetBool(section,"positionEnabled",&error);
    if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) { config.enabled = false; return config; }
    if (error != vr::VRSettingsError_None) throw std::runtime_error("cannot read positionEnabled");
    if (!config.enabled) return config;
    const auto integer = [&](const char* key, int minimum, int maximum) {
        const auto value = settings.GetInt32(section,key,&error);
        if (error != vr::VRSettingsError_None) throw std::runtime_error(std::string("missing position setting: ")+key);
        if (value < minimum || value > maximum) throw std::runtime_error(std::string("position setting out of range: ")+key);
        return value;
    };
    config.port = static_cast<std::uint16_t>(integer("positionPort",1,65535));
    config.timeout_ms = integer("positionTimeoutMs",20,5000);
    const auto grace = settings.GetInt32(section,"positionGraceMs",&error);
    if (error == vr::VRSettingsError_None) {
        if (grace < 0 || grace > 200) throw std::runtime_error("positionGraceMs out of range");
        config.grace_ms = grace;
    } else if (error != vr::VRSettingsError_UnsetSettingHasNoDefault) {
        throw std::runtime_error("cannot read positionGraceMs");
    }
    const auto maximum = settings.GetFloat(section,"positionMaxAbsM",&error);
    if (error != vr::VRSettingsError_None) throw std::runtime_error("missing position setting: positionMaxAbsM");
    if (!std::isfinite(maximum) || maximum < 0.05 || maximum > 1000.0)
        throw std::runtime_error("positionMaxAbsM out of range");
    config.max_abs_m = maximum;
    // Optional, explicit and physically uncalibrated: the yaw from the camera
    // world frame to the IMU frame. Absent means the documented neutral 0.
    const auto yaw = settings.GetFloat(section,"positionCameraYawOffsetDeg",&error);
    if (error == vr::VRSettingsError_None) {
        if (!std::isfinite(yaw) || yaw < -180.0 || yaw > 180.0)
            throw std::runtime_error("positionCameraYawOffsetDeg out of range");
        config.camera_yaw_offset_deg = yaw;
    } else if (error != vr::VRSettingsError_UnsetSettingHasNoDefault) {
        throw std::runtime_error("cannot read positionCameraYawOffsetDeg");
    }
    return config;
}

psvr::ImuExportConfig load_imu_export_config(vr::IVRSettings& settings) {
    psvr::ImuExportConfig config;
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    config.enabled = settings.GetBool("driver_psvrpc","imuExportEnabled",&error);
    if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) config.enabled = false;
    else if (error != vr::VRSettingsError_None) throw std::runtime_error("cannot read imuExportEnabled");
    if (!config.enabled) return config;
    const auto integer = [&](const char* key, int fallback, int minimum, int maximum) {
        int value = settings.GetInt32("driver_psvrpc",key,&error);
        if (error == vr::VRSettingsError_UnsetSettingHasNoDefault) value = fallback;
        else if (error != vr::VRSettingsError_None) throw std::runtime_error(std::string("cannot read ")+key);
        if (value < minimum || value > maximum) throw std::runtime_error(std::string("out of range: ")+key);
        return value;
    };
    config.port = static_cast<std::uint16_t>(integer("imuExportPort",4272,1,65535));
    config.hz = integer("imuExportHz",100,1,1000);
    return config;
}

class Hmd final : public vr::ITrackedDeviceServerDriver {
    std::string imu_session_;
    std::unique_ptr<psvr::ImuExporter> imu_exporter_;
    std::uint64_t virtual_adapter_luid_ = 0;
    std::mutex mutex_;
    // Lifecycle calls are serialized independently of the snapshot mutex_.
    // Host calls below may synchronously query GetPose/GetComponent/DebugRequest,
    // but must NOT re-enter Activate/Deactivate (or Provider::Cleanup), directly
    // or by waiting for another thread doing so. Worker/host callbacks must not
    // wait for a lifecycle transition either: teardown drains publishes/joins.
    // RunFrame remains the single, non-reentrant publication owner per the SDK.
    std::mutex transition_mutex_;
    enum class Lifecycle { Inactive, Activating, Active, Stopping };
    std::mutex publish_gate_mutex_;
    std::condition_variable publish_idle_;
    Lifecycle lifecycle_ = Lifecycle::Inactive; // publish_gate_mutex_
    std::size_t publishes_in_flight_ = 0;        // publish_gate_mutex_
    psvr::TrackingSnapshot snapshot_{};
    psvr::ImuConfig config_{};
    std::unique_ptr<psvr::DisplayComponent> display_;
#if defined(__MINGW32__) && !defined(_MSC_VER)
    std::unique_ptr<psvr::WindowsDisplayAbi> display_abi_;
#endif
    std::thread reader_;
    std::thread display_monitor_;
    std::thread position_thread_;
    std::mutex display_wait_mutex_;
    std::condition_variable display_wait_;
    psvr::DisplayTopologyGuard display_topology_; // lifetime of HMD, not reader
    bool restart_requested_ = false; // OpenVR host thread only.
    std::atomic<bool> stop_{false}, calibrate_{false};
    // Shared recenter epoch: DebugRequest increments once. The reader thread
    // applies it to the IMU and stamps the applied epoch (and world-yaw
    // reference) into the stored snapshot; the position thread reads that
    // stamped epoch and applies the SAME one. Carrying the epoch in the
    // snapshot, rather than a separate atomic published before the orientation
    // was stored, removes the window where a new epoch could be observed with
    // an old orientation.
    std::atomic<unsigned> recenter_request_{0};
    unsigned imu_recenter_handled_ = 0;         // reader thread only; trigger bookkeeping
    unsigned imu_recenter_logged_ = 0;          // reader thread only
    unsigned position_epoch_applied_ = 0;       // position thread only
    psvr::PositionConfig position_config_{};
    std::unique_ptr<psvr::PositionBridge> position_;
    // Observes the SAME real packets, with no grace. Separates optical validity
    // from the legacy bridge's valid last-good hold (which otherwise obscures loss).
    std::unique_ptr<psvr::PositionBridge> position_observed_;
    psvr::PositionContinuity continuity_; // mutex_; publish is sole advancing owner
    std::unique_ptr<psvr::PositionReceiver> position_receiver_;
    std::atomic<unsigned> component_trace_calls_{0}; // HMD lifetime cap; no per-version map
    // Written under both lifecycle locks; readers hold the gate, a publication
    // lease, or transition_mutex_ after all publication leases have drained.
    vr::TrackedDeviceIndex_t index_ = vr::k_unTrackedDeviceIndexInvalid;
    // DebugRequest enqueues under publish_gate_mutex_ only while Active.
    // RunFrame consumes; teardown clears after closing/draining the gate.
    std::atomic<int> simulated_worn_request_{0};
    vr::VRInputComponentHandle_t proximity_ = 0;
    bool simulated_worn_ = false, original_proximity_sensor_ = false;
    psvr::DriverClock::time_point simulated_worn_until_{};
    psvr::PoseTransitionTrace pose_trace_;
    psvr::DriverClock::time_point pose_trace_origin_{};
    std::uint64_t pose_trace_dropped_logged_ = 0;
    std::atomic<std::uint64_t> imu_snapshot_advances_{0};
    psvr::DriverClock::time_point publish_metrics_at_{}, previous_publish_at_{};
    std::uint64_t metrics_publishes_=0, metrics_valid_=0, metrics_source_start_=0;
    double metrics_interval_sum_=0, metrics_interval_max_=0, metrics_age_sum_=0, metrics_age_max_=0;

    void observe_publish(const vr::DriverPose_t& pose) noexcept {
        const auto now=psvr::DriverClock::now();
        ++metrics_publishes_;
        const auto interval=std::chrono::duration<double,std::milli>(now-previous_publish_at_).count();
        previous_publish_at_=now;
        metrics_interval_sum_+=interval; metrics_interval_max_=std::max(metrics_interval_max_,interval);
        if(pose.poseIsValid) {
            ++metrics_valid_;
            const auto age=-pose.poseTimeOffset*1000;
            metrics_age_sum_+=age; metrics_age_max_=std::max(metrics_age_max_,age);
        }
        const auto elapsed=std::chrono::duration<double>(now-publish_metrics_at_).count();
        if(elapsed<1) return;
        const auto source=imu_snapshot_advances_.load(std::memory_order_relaxed);
        try {
            log(nlohmann::json{{"event","pose_publish_metrics"},{"schema_version",1},
                {"utc_unix_us",std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count()},
                {"window_seconds",elapsed},{"published",metrics_publishes_},{"valid_published",metrics_valid_},
                {"publish_hz",metrics_publishes_/elapsed},{"imu_snapshot_advances_hz",(source-metrics_source_start_)/elapsed},
                {"publish_interval_mean_ms",metrics_interval_sum_/metrics_publishes_},{"publish_interval_max_ms",metrics_interval_max_},
                {"pose_age_mean_ms",metrics_valid_ ? nlohmann::json(metrics_age_sum_/metrics_valid_) : nlohmann::json(nullptr)},
                {"pose_age_max_ms",metrics_valid_ ? nlohmann::json(metrics_age_max_) : nlohmann::json(nullptr)},
                {"angular_velocity",{pose.vecAngularVelocity[0],pose.vecAngularVelocity[1],pose.vecAngularVelocity[2]}},
                {"publication_owner","RunFrame"}}.dump());
        } catch(...) {}
        publish_metrics_at_=now; metrics_source_start_=source; metrics_publishes_=0; metrics_valid_=0;
        metrics_interval_sum_=0; metrics_interval_max_=0; metrics_age_sum_=0; metrics_age_max_=0;
    }

    void flush_pose_trace() noexcept {
        try {
            std::array<psvr::PoseTransition,128> events;
            const auto count = pose_trace_.drain(events);
            for (std::size_t i=0; i<count; ++i) {
                const auto& e = events[i];
                log(nlohmann::json{{"event","pose_validity_transition"},{"schema_version",1},
                    {"sequence",e.sequence},{"utc_unix_us",e.utc_unix_us},{"steady_elapsed_us",e.steady_elapsed_us},
                    {"valid",e.valid},{"connected",e.connected},{"imu_state",psvr::state_name(e.imu_state)},
                    {"tracking_result",e.tracking_result},{"reason",psvr::pose_reason_name(e.reason)},
                    {"mode",psvr::tracking_mode_name(e.mode)},{"orientation_valid",e.orientation_valid},
                    {"position_valid",e.position_valid},{"recenter_epoch",e.recenter_epoch},
                    {"recovery_offset_m",{e.recovery_offset.x,e.recovery_offset.y,e.recovery_offset.z}},
                    {"recovery_duration_ms",e.recovery_duration_ms},{"recovery_elapsed_ms",e.recovery_elapsed_ms},
                    {"sample_age_ms",e.has_sample ? nlohmann::json(e.sample_age_ms) : nlohmann::json(nullptr)},
                    {"sample_age_after_copy_ms",e.has_sample ? nlohmann::json(e.sample_age_after_copy_ms) : nlohmann::json(nullptr)},
                    {"query_to_copy_ms",e.query_to_copy_ms},{"stale_timeout_ms",config_.stale_timeout_ms}}.dump());
            }
            const auto dropped = pose_trace_.dropped();
            if (dropped != pose_trace_dropped_logged_) {
                log(nlohmann::json{{"event","pose_trace_dropped"},{"count",dropped}}.dump());
                pose_trace_dropped_logged_ = dropped;
            }
        } catch (...) {} // Best effort diagnostics, outside GetPose.
    }

    // Called by the publication owner, or by teardown after publications drain.
    // Restoration must not pass through the overwritable request mailbox.
    void restore_simulated_worn() {
        if (!simulated_worn_) return;
        const auto container = vr::VRProperties()->TrackedDeviceToPropertyContainer(index_);
        const auto input_error = vr::VRDriverInput()->UpdateBooleanComponent(proximity_,false,0);
        const auto property_error = vr::VRProperties()->SetBoolProperty(container,vr::Prop_ContainsProximitySensor_Bool,original_proximity_sensor_);
        if (input_error == vr::VRInputError_None && property_error == vr::TrackedProp_Success) {
            simulated_worn_ = false;
            log("diagnostic simulated-worn ended: /proximity=false; proximity property restored");
        }
    }

    void update_simulated_worn() {
        const int request = simulated_worn_request_.exchange(0);
        if (request == 1) {
            const auto container = vr::VRProperties()->TrackedDeviceToPropertyContainer(index_);
            auto* input = vr::VRDriverInput();
            if (!input) { log("diagnostic simulated-worn failed: IVRDriverInput unavailable"); return; }
            if (!proximity_ && input->CreateBooleanComponent(container,"/proximity",&proximity_) != vr::VRInputError_None) {
                log("diagnostic simulated-worn failed: cannot create /proximity"); return;
            }
            if (!simulated_worn_) original_proximity_sensor_ = vr::VRProperties()->GetBoolProperty(container,vr::Prop_ContainsProximitySensor_Bool);
            if (vr::VRProperties()->SetBoolProperty(container,vr::Prop_ContainsProximitySensor_Bool,true) != vr::TrackedProp_Success) {
                log("diagnostic simulated-worn failed: cannot enable proximity property"); return;
            }
            if (input->UpdateBooleanComponent(proximity_,true,0) != vr::VRInputError_None) {
                vr::VRProperties()->SetBoolProperty(container,vr::Prop_ContainsProximitySensor_Bool,original_proximity_sensor_);
                log("diagnostic simulated-worn failed: cannot publish /proximity=true"); return;
            }
            simulated_worn_ = true;
            simulated_worn_until_ = psvr::DriverClock::now() + std::chrono::seconds(180);
            log("diagnostic simulated-worn applied: /proximity=true; lease=180s; synthetic presence, real IMU poses");
        }
        if (simulated_worn_ && (request == -1 || psvr::DriverClock::now() >= simulated_worn_until_)) {
            restore_simulated_worn();
        }
    }

    void trace_component(const char* version, bool returned) noexcept {
        if (!display_ || !display_->config().diagnostic_sink) return;
        auto calls = component_trace_calls_.load(std::memory_order_relaxed);
        do {
            if (calls >= 8) return;
        } while (!component_trace_calls_.compare_exchange_weak(calls,calls+1,std::memory_order_relaxed));
        try {
            // No addresses; cap the string length as well as the call count.
            std::size_t length = 0;
            if (version) while (length < 128 && version[length]) ++length;
            display_->config().diagnostic_sink("display diagnostic GetComponent requested_version=" +
                (version ? std::string(version,length) : std::string("<null>")) +
                " returned=" + (returned ? "1" : "0"));
        } catch (...) {} // Diagnostics must not escape an SDK callback.
    }

    void store(psvr::TrackingSnapshot value) {
        std::lock_guard<std::mutex> lock(mutex_);
        snapshot_ = value;
        if (position_) position_->bind_driver(imu_session_,value.recenter_epoch,
            psvr::driver_seconds(value.sampled_at),value.has_sample,psvr::DriverClock::now());
        if (position_observed_) position_observed_->bind_driver(imu_session_,value.recenter_epoch,
            psvr::driver_seconds(value.sampled_at),value.has_sample,psvr::DriverClock::now());
    }
    void monitor_display() noexcept {
        try {
            while (!stop_.load()) {
                if (!display_topology_.update(verify_selected_active_display(display_->config()))) {
                    // Coordinate the stop predicate with display_wait_mutex_
                    // before waking the waiter; join order below is unchanged.
                    { std::lock_guard<std::mutex> lock(display_wait_mutex_); stop_ = true; }
                    display_wait_.notify_all();
                    log("display topology/timing changed; pose invalidated, runtime restart required");
                    return;
                }
                std::unique_lock<std::mutex> lock(display_wait_mutex_);
                display_wait_.wait_for(lock,std::chrono::seconds(1),[this] { return stop_.load(); });
            }
        } catch (const std::exception& error) {
            display_topology_.update(false);
            { std::lock_guard<std::mutex> lock(display_wait_mutex_); stop_ = true; }
            display_wait_.notify_all();
            best_effort([&] { log(std::string("display verification failed: ")+error.what()); });
        } catch (...) {
            display_topology_.update(false);
            { std::lock_guard<std::mutex> lock(display_wait_mutex_); stop_ = true; }
            display_wait_.notify_all();
            log_failure("display verification exception");
        }
    }
    void read_sensor() noexcept try {
            ScopeExit disconnect([this] {
                // Retain the last real timestamp/epoch even after capture stops.
                psvr::TrackingSnapshot disconnected;
                { std::lock_guard<std::mutex> lock(mutex_); disconnected = snapshot_; }
                disconnected.connected = false; disconnected.state = psvr::ImuState::lost;
                disconnected.angular_velocity = {};
                store(disconnected);
            });
            psvr::DeviceSession ownership;
            // Ownership stays on this thread, including open, read and close.
            if (hid_init() != 0) throw std::runtime_error("HID initialization failed");
            struct HidLifetime { ~HidLifetime() { hid_exit(); } } lifetime;
            std::unique_ptr<hid_device_info,decltype(&hid_free_enumeration)> devices(
                hid_enumerate(0x054c,0x09af),hid_free_enumeration);
            std::string path;
            for (auto* d = devices.get(); d; d = d->next) {
                if (d->interface_number != 4) continue;
                if (!path.empty()) throw std::runtime_error("multiple PSVR sensors; selection is ambiguous");
                if (d->path) path = d->path;
            }
            if (path.empty()) throw std::runtime_error("PSVR sensor interface 4 not found");
            std::unique_ptr<hid_device,decltype(&hid_close)> sensor(hid_open_path(path.c_str()),hid_close);
            if (!sensor) throw std::runtime_error("cannot open PSVR sensor interface 4");
            log("sensor opened; hands off on table for 8 s calibration; no power/mode commands");
            psvr::DriverTracker tracker(config_,psvr::DriverClock::now());
#ifdef _WIN32
            // Button forwarding runs on this same reader thread: our own thread
            // with no message pump, so MTA init here is the minimum safe COM
            // setup inside vrserver.exe (see headset_audio.hpp). Balanced at
            // thread exit; audio calls below never hold a driver mutex.
            psvr::ComScope com_audio;
            best_effort([&] {
                char text[192];
                std::snprintf(text,sizeof(text),
                    "headset button: COM init hr=0x%08lX ready=%d owned=%d requested_apartment=MTA policy=fail_closed",
                    static_cast<unsigned long>(static_cast<std::uint32_t>(com_audio.result())),
                    com_audio.ok() ? 1 : 0,com_audio.owns_initialization() ? 1 : 0);
                log(text);
            });
            psvr::CoreAudioHeadsetControl headset_audio(
                com_audio.ok(),
                [](const std::string& text) { log(text); });
            psvr::HeadsetButtonEdgeState button_state;
            psvr::HeadsetButtonDiagnostics button_diagnostics;
            bool button_invalid_report_logged = false;
            const auto button_epoch = psvr::DriverClock::now();
#endif
            store(tracker.snapshot());
            auto previous = tracker.snapshot().state;
            psvr::DriverClock::time_point previous_sample_at{};
            psvr::ImuReadDiagnostics read_diagnostics;
            auto previous_read_end = psvr::DriverClock::now();
            const auto report_fault = [&](const char* reason = nullptr) noexcept {
                try { log(psvr::imu_loss_report(tracker,config_,psvr::DriverClock::now(),read_diagnostics,reason).dump()); }
                catch (...) {} // Diagnostic failures must not mask the capture fault.
            };
            while (!stop_.load()) {
                if (calibrate_.exchange(false)) {
                    tracker.recalibrate(psvr::DriverClock::now());
                    read_diagnostics = {};
                    previous_read_end = psvr::DriverClock::now();
                }
                const auto request = recenter_request_.load(std::memory_order_relaxed);
                if (request != imu_recenter_handled_) {
                    const bool ready = tracker.watchdog(psvr::DriverClock::now());
                    if (ready && tracker.recenter()) {
                        imu_recenter_handled_ = request;
                        log("recenter applied");
                    } else if (request != imu_recenter_logged_) {
                        imu_recenter_logged_ = request;
                        log("recenter pending: pose not ready");
                    }
                }
                std::array<unsigned char,512> bytes{};
                const auto read_start = psvr::DriverClock::now();
                const int count = hid_read_timeout(sensor.get(),bytes.data(),bytes.size(),10);
                const auto now = psvr::DriverClock::now();
                ++read_diagnostics.reads;
                if (count == 0) ++read_diagnostics.empty_reads;
                read_diagnostics.last_bytes = count;
                read_diagnostics.last_read_ms = std::chrono::duration<double,std::milli>(now-read_start).count();
                read_diagnostics.max_read_ms = std::max(read_diagnostics.max_read_ms,read_diagnostics.last_read_ms);
                read_diagnostics.last_loop_interval_ms = std::chrono::duration<double,std::milli>(now-previous_read_end).count();
                previous_read_end = now;
                if (count < 0) {
                    report_fault("hid_read_error");
                    throw std::runtime_error("sensor HID read failed; tracking disconnected");
                }
                if (count > 0) {
                    tracker.packet(bytes.data(),static_cast<std::size_t>(count),now);
#ifdef _WIN32
                    // Leave tracker.packet/gates unchanged. Only validated sensor
                    // reports may arm or advance the independent button detector.
                    const auto button_report = psvr::decode_sensor(
                        bytes.data(),static_cast<std::size_t>(count));
                    const auto now_ms = static_cast<std::uint64_t>(
                        std::chrono::duration_cast<std::chrono::milliseconds>(
                            now-button_epoch).count());
                    if (button_report) {
                        const auto actions = psvr::poll_headset_button_edges(
                            button_report->buttons_raw,now_ms,button_state);
                        std::array<bool,3> results{};
                        std::array<const char*,3> reasons{};
                        for (std::size_t i=0;i<actions.size();++i) {
                            results[i] = psvr::dispatch_headset_button_action(actions[i],headset_audio);
                            reasons[i] = results[i] ? "ok" : headset_audio.last_failure();
                        }
                        if (button_diagnostics.observe(button_report->buttons_raw,
                                button_report->volume_raw,button_report->headset_flags_raw)) {
                            if (button_diagnostics.allow_log(now_ms)) best_effort([&] {
                                char raw[192];
                                std::snprintf(raw,sizeof(raw),
                                    "headset button: transition t_ms=%llu raw=0x%02X volume=%u flags=0x%02X suppressed=%llu",
                                    static_cast<unsigned long long>(now_ms),
                                    static_cast<unsigned>(button_report->buttons_raw),
                                    static_cast<unsigned>(button_report->volume_raw),
                                    static_cast<unsigned>(button_report->headset_flags_raw),
                                    static_cast<unsigned long long>(button_diagnostics.suppressed));
                                std::string text(raw);
                                if (actions.empty()) text += " action=none (baseline/release/debounce/unknown-bit)";
                                for (std::size_t i=0;i<actions.size();++i) {
                                    text += " action="; text += psvr::headset_button_action_name(actions[i]);
                                    text += actions[i] == psvr::HeadsetButtonAction::MuteToggle
                                        ? " endpoint=psvr_capture_dynamic" : " endpoint=psvr_render_configured";
                                    text += results[i] ? " result=success reason=" : " result=failure reason=";
                                    text += reasons[i];
                                }
                                log(text);
                            });
                            else if (button_diagnostics.take_limit_notice())
                                log_failure("headset button: transition log limit=256 reached; forwarding unchanged");
                        }
                    } else if (!button_invalid_report_logged) {
                        button_invalid_report_logged = true;
                        log_failure("headset button: invalid report rejected; expected decoded 64-byte sensor report");
                    }
#endif
                }
                tracker.watchdog(now);
                const auto value = tracker.snapshot();
                if(value.has_sample && value.sampled_at!=previous_sample_at) {
                    imu_snapshot_advances_.fetch_add(1,std::memory_order_relaxed);
                    previous_sample_at=value.sampled_at;
                }
                // The tracker already stamped the world epoch and reference that
                // belong to this orientation, so no separate atomic is needed.
                store(value);
                if (value.state != previous) {
                    log(std::string("IMU state=")+psvr::state_name(value.state)); previous = value.state;
                }
                if (value.state == psvr::ImuState::lost || value.state == psvr::ImuState::calibration_failed) {
                    report_fault();
                    log("capture invalid; deactivate/reactivate to restart"); break;
                }
            }
        } catch (const std::exception& error) { log_failure(error.what()); }
          catch (...) { log_failure("unexpected sensor exception; tracking disconnected"); }
    void fault_position() noexcept {
        best_effort([&] { if (position_) position_->mark_faulted(); });
        best_effort([&] { if (position_observed_) position_observed_->mark_faulted(); });
    }
    void read_position() noexcept {
        try {
            while (!stop_.load()) {
                // Apply exactly the epoch the reader stamped into the stored
                // snapshot, so origin and world-yaw reference are the same pair
                // the orientation was published with.
                unsigned epoch = 0;
                { std::lock_guard<std::mutex> lock(mutex_); epoch = snapshot_.recenter_epoch; }
                if (!position_config_.require_bound_v2 && epoch != position_epoch_applied_ && position_) {
                    if (position_observed_) position_observed_->apply_recenter(psvr::PositionClock::now(),epoch);
                    if (position_->apply_recenter(psvr::PositionClock::now(),epoch)) {
                        position_epoch_applied_ = epoch;
                        log("position recenter applied");
                    }
                }
                if (position_receiver_->poll(50) < 0) {
                    fault_position();
                    log_failure("position receiver fatal socket error");
                    return;
                }
            }
        } catch (const std::exception& error) {
            fault_position();
            best_effort([&] { log(std::string("position receiver failed: ")+error.what()); });
        } catch (...) {
            fault_position();
            log_failure("position receiver unexpected exception");
        }
    }
public:
    explicit Hmd(psvr::ImuConfig config, std::unique_ptr<psvr::DisplayComponent> display, std::uint64_t virtual_adapter=0)
        : virtual_adapter_luid_(virtual_adapter), config_(config), display_(std::move(display)) {
#if defined(__MINGW32__) && !defined(_MSC_VER)
        if (display_) display_abi_ = std::make_unique<psvr::WindowsDisplayAbi>(*display_);
#endif
    }
    ~Hmd() { Deactivate(); }
    vr::EVRInitError Activate(uint32_t index) override try {
        std::lock_guard<std::mutex> transition(transition_mutex_);
        {
            std::lock_guard<std::mutex> gate(publish_gate_mutex_);
            if (lifecycle_ != Lifecycle::Inactive) return vr::VRInitError_Driver_Failed;
            lifecycle_ = Lifecycle::Activating;
            index_ = index;
        }
        bool verifying_display = false;
        ScopeExit rollback([&] {
            // Only verification failures latch; unrelated failures can retry.
            if (verifying_display) display_topology_.update(false);
            deactivate_locked(); // transition_mutex_ is already owned
        });
            if (display_ && !display_topology_.valid())
                throw std::runtime_error("display invalidation is latched; runtime restart required");
            // Revalidate between provider registration and activation; names and
            // geometry may have changed during the runtime's startup.
            if (display_) {
                verifying_display = true;
                if (!display_topology_.update(verify_selected_active_display(display_->config())))
                    throw std::runtime_error("selected display changed before activation");
                verifying_display = false;
            }
            if (display_) display_->diagnostic_activation();
            const auto container = vr::VRProperties()->TrackedDeviceToPropertyContainer(index);
            auto* props = vr::VRProperties();
            const auto require = [](vr::ETrackedPropertyError error) {
                if (error != vr::TrackedProp_Success) throw std::runtime_error("OpenVR property write failed");
            };
            require(props->SetStringProperty(container,vr::Prop_TrackingSystemName_String,"psvrpc"));
            require(props->SetStringProperty(container,vr::Prop_ModelNumber_String,"PSVR1 3DoF development"));
            require(props->SetStringProperty(container,vr::Prop_ManufacturerName_String,"Sony"));
            require(props->SetStringProperty(container,vr::Prop_SerialNumber_String,"PSVRPC-HMD-0"));
            require(props->SetUint64Property(container,vr::Prop_CurrentUniverseId_Uint64,2));
            require(props->SetBoolProperty(container,vr::Prop_WillDriftInYaw_Bool,true));
            require(props->SetBoolProperty(container,vr::Prop_HasDisplayComponent_Bool,display_ != nullptr));
            require(props->SetBoolProperty(container,vr::Prop_HasDriverDirectModeComponent_Bool,false));
            require(props->SetBoolProperty(container,vr::Prop_HasVirtualDisplayComponent_Bool,false));
            if (display_ && display_->config().virtual_display) {
                require(props->SetBoolProperty(container,vr::Prop_IsOnDesktop_Bool,false));
                require(props->SetUint64Property(container,vr::Prop_GraphicsAdapterLuid_Uint64,virtual_adapter_luid_));
                log("experimental HMD virtual output: separate DisplayRedirect owns physical presentation");
            }
            if (display_) require(props->SetFloatProperty(container,vr::Prop_DisplayFrequency_Float,
                static_cast<float>(display_->config().refresh_hz())));
            if (display_ && display_->config().native_direct_mode) {
                const bool desktop_present = display_->IsDisplayOnDesktop();
                require(props->SetBoolProperty(container,vr::Prop_IsOnDesktop_Bool,desktop_present));
                log(std::string("native display desktop property from actual inventory=") + (desktop_present ? "1" : "0"));
                require(props->SetInt32Property(container,vr::Prop_EdidVendorID_Int32,
                    static_cast<int32_t>(display_->config().edid_vendor_id)));
                require(props->SetInt32Property(container,vr::Prop_EdidProductID_Int32,
                    static_cast<int32_t>(display_->config().edid_product_id)));
                log("native direct display requested via SteamVR; GPU acquisition/refresh must be verified in compositor log");
            }
            // Extended-desktop (display without virtual or native direct mode):
            // Prop_IsOnDesktop is intentionally not written here, so the
            // runtime default is preserved. Writing an explicit default is a
            // design decision left to the owner (B8-06).
            // Reset the monitor stop predicate under its mutex before any
            // worker starts waiting; join order in teardown is unchanged.
            { std::lock_guard<std::mutex> lock(display_wait_mutex_); stop_ = false; }
            calibrate_ = false; store({});
            simulated_worn_request_ = 0; proximity_ = 0; simulated_worn_ = false;
            pose_trace_.reset(); pose_trace_origin_ = psvr::DriverClock::now(); pose_trace_dropped_logged_ = 0;
            imu_snapshot_advances_=0; publish_metrics_at_=pose_trace_origin_; previous_publish_at_=pose_trace_origin_;
            metrics_publishes_=0; metrics_valid_=0; metrics_source_start_=0;
            metrics_interval_sum_=0; metrics_interval_max_=0; metrics_age_sum_=0; metrics_age_max_=0;
            recenter_request_ = 0;
            imu_recenter_handled_ = 0; imu_recenter_logged_ = 0; position_epoch_applied_ = 0;
            restart_requested_ = false;
            // Read the opt-in position settings only when the runtime actually
            // activates the HMD. Bind before starting threads so a bad port or
            // an invalid configuration fails activation instead of degrading
            // silently to 3DoF.
            position_config_ = load_position_config(*vr::VRSettings());
            const auto continuity_config = psvr::load_position_continuity_config(*vr::VRSettings());
            const auto export_config = load_imu_export_config(*vr::VRSettings());
            { std::lock_guard<std::mutex> lock(mutex_);
              imu_session_ = psvr::new_imu_session();
              continuity_ = psvr::PositionContinuity(continuity_config); }
            position_config_.require_bound_v2 = export_config.enabled && position_config_.enabled;
            if (position_config_.enabled) {
                auto bridge = std::make_unique<psvr::PositionBridge>(position_config_);
                bridge->bind_driver(imu_session_,0,0,false,psvr::DriverClock::now());
                auto observed_config = position_config_; observed_config.grace_ms = 0;
                auto observed = std::make_unique<psvr::PositionBridge>(observed_config);
                observed->bind_driver(imu_session_,0,0,false,psvr::DriverClock::now());
                auto receiver = std::make_unique<psvr::PositionReceiver>(position_config_,
                    [this](const psvr::PositionSample& sample) {
                        std::lock_guard<std::mutex> lock(mutex_);
                        if (position_) position_->ingest(sample);
                        if (position_observed_) position_observed_->ingest(sample);
                    });
                log("position input enabled; loopback UDP 127.0.0.1:" +
                    std::to_string(receiver->local_port()));
                std::lock_guard<std::mutex> lock(mutex_);
                position_ = std::move(bridge);
                position_observed_ = std::move(observed);
                position_receiver_ = std::move(receiver);
            }
            reader_ = std::thread(&Hmd::read_sensor,this);
            if (export_config.enabled) {
                try {
                    imu_exporter_ = std::make_unique<psvr::ImuExporter>(export_config,imu_session_,[this] {
                        std::lock_guard<std::mutex> lock(mutex_); return snapshot_;
                    },config_.stale_timeout_ms);
                } catch (const std::exception& error) {
                    best_effort([&] { log(std::string("IMU export unavailable (orientation unaffected): ")+error.what()); });
                } catch (...) {
                    log_failure("IMU export unavailable (orientation unaffected): unexpected exception");
                }
            }
            // Desktop removal is the intended native-direct transition, not a
            // topology fault. SteamVR's GPU backend owns video disconnect events.
            if (display_ && !display_->config().native_direct_mode)
                display_monitor_ = std::thread(&Hmd::monitor_display,this);
            if (position_config_.enabled) position_thread_ = std::thread(&Hmd::read_position,this);
            if (display_ && display_->config().native_direct_mode)
                log("HMD activated; SteamVR-native direct mode requested, NOT yet proof of working scanout");
            else if (display_) log(display_->config().optical_mode == psvr::OpticalMode::profile
                ? "HMD activated; extended-desktop, explicit profile distortion (optical acceptance pending)"
                : "HMD activated; extended-desktop, diagnostic identity distortion (NOT optical calibration)");
            else log("HMD activated; tracking-only, display disabled");
            { std::lock_guard<std::mutex> gate(publish_gate_mutex_); lifecycle_ = Lifecycle::Active; }
            rollback.release();
            return vr::VRInitError_None;
        } catch (const std::exception& error) {
            // Rollback has already joined workers, before any fallible logging.
            log_failure(error.what());
            return vr::VRInitError_Driver_Failed;
        } catch (...) {
            log_failure("unexpected activation exception");
            return vr::VRInitError_Driver_Failed;
        }
private:
    // Caller owns transition_mutex_, including failed-Activate rollback. Never
    // take mutex_ while waiting: admitted publishers may need it or query poses
    // synchronously from a host callback. The gate is not held across host calls.
    void deactivate_locked() noexcept try {
        {
            std::unique_lock<std::mutex> gate(publish_gate_mutex_);
            lifecycle_ = Lifecycle::Stopping; // no new publication leases
            publish_idle_.wait(gate,[this] { return publishes_in_flight_ == 0; });
        }
        // No producer can enqueue after Stopping; admitted publications have
        // finished consuming. Discard pending diagnostics before direct restore.
        simulated_worn_request_ = 0;
        if (index_ != vr::k_unTrackedDeviceIndexInvalid) flush_pose_trace();
        if (simulated_worn_ && index_ != vr::k_unTrackedDeviceIndexInvalid) {
            best_effort([&] { restore_simulated_worn(); });
        }
        // Publish the stop predicate under display_wait_mutex_, then notify.
        // Join order below is unchanged: display monitor, sensor, position.
        { std::lock_guard<std::mutex> lock(display_wait_mutex_); stop_ = true; }
        display_wait_.notify_all();
        if (display_monitor_.joinable()) display_monitor_.join();
        if (reader_.joinable()) reader_.join();
        if (position_thread_.joinable()) position_thread_.join();
        imu_exporter_.reset(); // joins exporter before releasing snapshot/IPC state
        std::unique_ptr<psvr::PositionReceiver> receiver;
        std::unique_ptr<psvr::PositionBridge> bridge;
        {
            // All users are joined; guard against a concurrent GetPose on the
            // runtime thread before releasing the receiver and bridge.
            std::lock_guard<std::mutex> lock(mutex_);
            receiver = std::move(position_receiver_);
            bridge = std::move(position_);
            position_observed_.reset();
            continuity_.reset();
        }
        store({});
        {
            std::lock_guard<std::mutex> gate(publish_gate_mutex_);
            index_ = vr::k_unTrackedDeviceIndexInvalid;
            lifecycle_ = Lifecycle::Inactive;
        }
    } catch (...) { log_failure("unexpected HMD teardown exception"); }
public:
    void Deactivate() noexcept override try {
        std::lock_guard<std::mutex> transition(transition_mutex_);
        deactivate_locked();
    } catch (...) { log_failure("unexpected HMD deactivation exception"); }
    void EnterStandby() override {} // Passive: does not power the headset down.
    void* GetComponent(const char* version) override {
        const bool returned = display_ && version && std::strcmp(version,vr::IVRDisplayComponent_Version) == 0;
        trace_component(version,returned);
        if (returned) {
#if defined(__MINGW32__) && !defined(_MSC_VER)
            return display_abi_->runtime_interface();
#else
            return static_cast<vr::IVRDisplayComponent*>(display_.get());
#endif
        }
        return nullptr;
    }
    void DebugRequest(const char* request, char* response, uint32_t size) override try {
        const char* result = "supported: recenter, recalibrate, tracking-status";
        std::string status;
        if (request && std::strcmp(request,"tracking-status") == 0) {
            psvr::DriverClock::time_point now;
            std::string session;
            psvr::PositionContinuityConfig settings;
            const auto s = continuity_snapshot(false,now,&session,&settings);
            const bool display_valid = !display_ || display_topology_.valid();
            const auto pose = psvr::driver_pose(display_valid ? s : psvr::TrackingSnapshot{},now,config_.stale_timeout_ms);
            status = nlohmann::json{{"schema","psvr.driver.tracking_status"},{"version",1},
                {"mode",display_valid ? psvr::tracking_mode_name(s.tracking_mode) : "invalid"},
                {"pose_valid",pose.poseIsValid},{"orientation_valid",psvr::fresh_orientation(s,now,config_.stale_timeout_ms)},
                {"position_valid",s.optical_position_valid && display_valid},
                {"position_enabled",s.position_enabled},{"display_valid",display_valid},
                {"angular_centering",s.angular_centering},{"angular_center_weight",s.angular_center_weight},
                {"session_id",session},{"recenter_epoch",s.recenter_epoch},
                {"imu_sample_age_ms",s.has_sample ? nlohmann::json(std::chrono::duration<double,std::milli>(now-s.sampled_at).count()) : nlohmann::json(nullptr)},
                {"recovery_offset_m",{s.recovery_offset.x,s.recovery_offset.y,s.recovery_offset.z}},
                {"recovery_remaining_m",s.recovery_offset.norm()},
                {"recovery_duration_ms",s.recovery_duration_ms},{"recovery_elapsed_ms",s.recovery_elapsed_ms},
                {"position_m",{pose.vecPosition[0],pose.vecPosition[1],pose.vecPosition[2]}},
                 {"settings",{{"positionFallback3DofEnabled",settings.fallback_3dof_enabled},
                    {"positionAngularCenterEnabled",settings.angular_center_enabled},
                    {"positionReacquireBlendMs",settings.reacquire_blend_ms},
                    {"positionReacquireMaxSpeedMps",settings.reacquire_max_speed_mps},
                    {"positionInitialOffsetX",settings.initial_offset.x},
                    {"positionInitialOffsetY",settings.initial_offset.y},
                    {"positionInitialOffsetZ",settings.initial_offset.z}}},
                {"publication_owner","RunFrame"}}.dump();
            if (status.size() >= size) status = "{\"error\":\"response_buffer_too_small\"}";
            result = status.c_str();
        }
        if (request && std::strcmp(request,"recenter") == 0) {
            recenter_request_.fetch_add(1,std::memory_order_relaxed);
            result = "recenter queued";
        }
        if (request && std::strcmp(request,"recalibrate") == 0) { calibrate_ = true; result = "recalibration queued; keep headset stationary"; }
        if (request && (std::strcmp(request,"simulate-worn") == 0 || std::strcmp(request,"simulate-unworn") == 0)) {
            // Acceptance and enqueue are one operation relative to Stopping.
            // Release the gate before any runtime calls (including restoration).
            std::lock_guard<std::mutex> gate(publish_gate_mutex_);
            if (lifecycle_ != Lifecycle::Active || index_ == vr::k_unTrackedDeviceIndexInvalid)
                result = "simulation rejected: HMD not active";
            else {
                const bool worn = std::strcmp(request,"simulate-worn") == 0;
                simulated_worn_request_ = worn ? 1 : -1;
                result = worn ? "simulated worn queued; expires after 180 seconds" : "simulated unworn queued";
            }
        }
        if (response && size) {
            const auto n = std::min(std::strlen(result),static_cast<std::size_t>(size-1));
            std::memcpy(response,result,n); response[n] = '\0';
        }
    } catch (...) {
        if (response && size) response[0] = '\0';
        log_failure("unexpected debug request exception");
    }
    psvr::TrackingSnapshot continuity_snapshot(bool advance, psvr::DriverClock::time_point& now,
                                             std::string* session = nullptr,
                                             psvr::PositionContinuityConfig* settings = nullptr) {
        psvr::TrackingSnapshot snapshot;
        bool optical_valid = false, holding = false;
        {
            // One lock for the snapshot copy and the position view: the epoch
            // and world reference come from the same stored snapshot, and the
            // unique_ptr cannot be reset underneath a concurrent Deactivate.
            std::lock_guard<std::mutex> lock(mutex_);
            snapshot = snapshot_;
            // Sample age must be measured AFTER copying under the reader lock.
            // Sampling before the lock let a newer IMU snapshot appear "future",
            // invalidating a healthy pose for one publish and triggering a fade.
            now = psvr::DriverClock::now();
            if (session) *session = imu_session_;
            if (settings) *settings = continuity_.config();
            if (position_ && position_->enabled()) {
                const auto position = position_->view(now,snapshot.recenter_epoch,snapshot.world_rotation);
                const auto observed = position_observed_->view(now,snapshot.recenter_epoch,snapshot.world_rotation);
                optical_valid = observed.valid;
                holding = position.valid && !observed.valid;
                const auto& selected = continuity_.config().fallback_3dof_enabled ? observed : position;
                snapshot.position_enabled = true;
                snapshot.has_position = selected.valid;
                snapshot.position_coherent = selected.coherent;
                // An absent/rejected/faulted optical view supplies NO position.
                // The fallback frame is the authoritative atomic IMU epoch,
                // not an old optical origin. The controller independently keys
                // its held point by this activation + epoch (or uses zero).
                // A valid measurement still requires bridge coherence; strict
                // mode keeps the bridge's original coherence/gray semantics.
                if ((continuity_.config().fallback_3dof_enabled || continuity_.config().angular_center_enabled) && !selected.valid)
                    snapshot.position_coherent = true;
                snapshot.position = selected.position;
                snapshot.position_at = selected.received_at;
            }
            const bool display_valid = !display_ || display_topology_.valid();
            return advance ? continuity_.update(snapshot,imu_session_,now,config_.stale_timeout_ms,
                                                display_valid,optical_valid,holding)
                           : continuity_.preview(snapshot,imu_session_,now,config_.stale_timeout_ms,
                                                 display_valid,optical_valid,holding);
        }
    }
    vr::DriverPose_t get_pose(bool advance) {
        psvr::DriverClock::time_point now;
        const auto snapshot = continuity_snapshot(advance,now);
        const auto copied = psvr::DriverClock::now();
        const bool display_valid = !display_ || display_topology_.valid();
        const auto effective = display_valid ? snapshot : psvr::TrackingSnapshot{};
        const auto pose = psvr::driver_pose(effective,now,config_.stale_timeout_ms);
        pose_trace_.observe(snapshot,pose,now,copied,pose_trace_origin_,config_.stale_timeout_ms,display_valid);
        return pose;
    }
    vr::DriverPose_t GetPose() override { return get_pose(false); }
    void publish() {
        {
            std::lock_guard<std::mutex> gate(publish_gate_mutex_);
            if (lifecycle_ != Lifecycle::Active || index_ == vr::k_unTrackedDeviceIndexInvalid) return;
            ++publishes_in_flight_;
        }
        struct PublicationExit {
            Hmd& hmd;
            ~PublicationExit() {
                std::lock_guard<std::mutex> gate(hmd.publish_gate_mutex_);
                if (--hmd.publishes_in_flight_ == 0) hmd.publish_idle_.notify_all();
            }
        } publication{*this}; // releases the lease on normal AND exceptional exits
        {
            update_simulated_worn();
            const auto pose=get_pose(true);
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(index_,pose,sizeof(vr::DriverPose_t));
            observe_publish(pose);
            flush_pose_trace();
            if (display_ && !display_topology_.valid() && !restart_requested_) {
                restart_requested_ = true;
                // SDK restart request, not proof of synchronous compositor stop.
                vr::VRServerDriverHost()->RequestRestart(
                    "PSVR-PC: display topology changed; verify display settings before restarting.",
                    nullptr,nullptr,nullptr);
            }
        }
    }
};

class Provider final : public vr::IServerTrackedDeviceProvider {
    // Init/Cleanup are serialized separately from frame admission. Lock order:
    // provider_transition_mutex_ -> frame_gate_mutex_; release the frame gate
    // before device/host calls (in particular Hmd's transition_mutex_). RunFrame
    // owns only a counted lease across those calls, never either provider lock.
    // As with Hmd's lifecycle contract, host/worker callbacks must not re-enter
    // Init/Cleanup, or wait for a thread doing so: Cleanup drains frame leases.
    // The SDK still owns serialization/non-reentrancy of RunFrame itself, and
    // calls Cleanup before re-Init and before releasing the provider instance.
    std::mutex provider_transition_mutex_;
    std::mutex frame_gate_mutex_;
    std::condition_variable frame_idle_;
    bool frames_enabled_ = false;       // frame_gate_mutex_
    std::size_t frames_in_flight_ = 0;  // frame_gate_mutex_
    std::unique_ptr<Hmd> hmd_;
#if defined(_MSC_VER)
    class Redirect final : public vr::ITrackedDeviceServerDriver, public vr::IVRVirtualDisplay {
        std::unique_ptr<psvr::presenter::D3D11Presenter> backend_;
        psvr::DisplayConfig config_;
        std::atomic<unsigned> present_calls_{0},time_calls_{0};
        // Same lifecycle contract as Hmd: serialize transitions, but never hold
        // the notification gate across backend/host calls. Host callbacks may
        // query the device, not re-enter/wait for Activate/Deactivate/Cleanup;
        // Deactivate drains admitted polls before invalidating the index.
        std::mutex transition_mutex_;
        enum class Lifecycle { Inactive, Activating, Active, Stopping };
        std::mutex publish_gate_mutex_;
        std::condition_variable publish_idle_;
        Lifecycle lifecycle_=Lifecycle::Inactive; // publish_gate_mutex_
        std::size_t publishes_in_flight_=0;        // publish_gate_mutex_
        // Writers own both lifecycle locks; polls hold a notification lease.
        vr::TrackedDeviceIndex_t index_=vr::k_unTrackedDeviceIndexInvalid;
        bool failure_reported_=false; // publish_gate_mutex_; lifetime latch
    public:
        explicit Redirect(const psvr::DisplayConfig& config):config_(config) {
            backend_=std::make_unique<psvr::presenter::D3D11Presenter>(config,[](const std::string& text) noexcept {log_failure(text.c_str());});
        }
        std::uint64_t adapter() const noexcept {return backend_->adapter_luid();}
        vr::EVRInitError Activate(uint32_t index) override try {
            std::lock_guard<std::mutex> transition(transition_mutex_);
            {
                std::lock_guard<std::mutex> gate(publish_gate_mutex_);
                if(lifecycle_!=Lifecycle::Inactive)return vr::VRInitError_Driver_Failed;
                lifecycle_=Lifecycle::Activating;
            }
            ScopeExit activation([this] {
                std::lock_guard<std::mutex> gate(publish_gate_mutex_);
                // Only a fully configured device has a published index; all
                // property failures leave activation inactive and fail closed.
                lifecycle_=index_==vr::k_unTrackedDeviceIndexInvalid ? Lifecycle::Inactive : Lifecycle::Active;
            });
            try {
                if(!backend_->healthy())return vr::VRInitError_Driver_Failed;
                const auto container=vr::VRProperties()->TrackedDeviceToPropertyContainer(index);
                auto* p=vr::VRProperties();
                if(p->SetStringProperty(container,vr::Prop_ModelNumber_String,"PSVR-PC experimental display redirect")!=vr::TrackedProp_Success ||
                   p->SetStringProperty(container,vr::Prop_SerialNumber_String,"PSVRPC-DISPLAY-REDIRECT-0")!=vr::TrackedProp_Success ||
                   p->SetUint64Property(container,vr::Prop_GraphicsAdapterLuid_Uint64,adapter())!=vr::TrackedProp_Success ||
                   p->SetFloatProperty(container,vr::Prop_DisplayFrequency_Float,static_cast<float>(config_.refresh_hz()))!=vr::TrackedProp_Success ||
                   p->SetFloatProperty(container,vr::Prop_SecondsFromVsyncToPhotons_Float,0)!=vr::TrackedProp_Success)
                    return vr::VRInitError_Driver_Failed;
                { std::lock_guard<std::mutex> gate(publish_gate_mutex_); index_=index; }
                log_failure("experimental DisplayRedirect activated; IVRVirtualDisplay_002 exposed");return vr::VRInitError_None;
            } catch (...) {
                { std::lock_guard<std::mutex> gate(publish_gate_mutex_); index_=vr::k_unTrackedDeviceIndexInvalid; }
                throw;
            }
        } catch (...) {
            log_failure("unexpected DisplayRedirect activation exception");
            return vr::VRInitError_Driver_Failed;
        }
        void Deactivate() override {
            std::lock_guard<std::mutex> transition(transition_mutex_);
            {
                std::unique_lock<std::mutex> gate(publish_gate_mutex_);
                lifecycle_=Lifecycle::Stopping;
                publish_idle_.wait(gate,[this] {return publishes_in_flight_==0;});
                index_=vr::k_unTrackedDeviceIndexInvalid;
            }
            if(backend_)backend_->shutdown();
            { std::lock_guard<std::mutex> gate(publish_gate_mutex_); lifecycle_=Lifecycle::Inactive; }
        }
        void poll_health() {
            {
                std::lock_guard<std::mutex> gate(publish_gate_mutex_);
                if(lifecycle_!=Lifecycle::Active||index_==vr::k_unTrackedDeviceIndexInvalid||failure_reported_)return;
                ++publishes_in_flight_;
            }
            ScopeExit publication([this] {
                std::lock_guard<std::mutex> gate(publish_gate_mutex_);
                if(--publishes_in_flight_==0)publish_idle_.notify_all();
            }); // includes healthy(), GetPose() and both exceptional host exits
            if(backend_->healthy())return;
            {
                std::lock_guard<std::mutex> gate(publish_gate_mutex_);
                if(failure_reported_)return;
                failure_reported_=true;
            }
            const auto pose=GetPose();
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(index_,pose,sizeof(pose));
            vr::VRServerDriverHost()->RequestRestart("PSVR-PC experimental presenter failed; inspect presenter_failure log before restart.",nullptr,nullptr,nullptr);
        }
        void EnterStandby() override {}
        void* GetComponent(const char* version) override {
            if(version&&!std::strcmp(version,vr::IVRVirtualDisplay_Version)) {
                log_failure("DisplayRedirect GetComponent IVRVirtualDisplay_002 returned=1");
                return static_cast<vr::IVRVirtualDisplay*>(this);
            }return nullptr;
        }
        void DebugRequest(const char*,char* output,uint32_t size) override {if(output&&size)output[0]='\0';}
        vr::DriverPose_t GetPose() override {
            vr::DriverPose_t p{};p.qRotation.w=p.qWorldFromDriverRotation.w=p.qDriverFromHeadRotation.w=1;
            p.deviceIsConnected=p.poseIsValid=backend_->healthy();p.result=p.poseIsValid ? vr::TrackingResult_Running_OK : vr::TrackingResult_Uninitialized;return p;
        }
        void Present(const vr::PresentInfo_t* info,uint32_t size) override {
            const bool accepted=backend_->submit(info,size);
            if(present_calls_.fetch_add(1)<4)best_effort([&] { log(nlohmann::json{{"event","virtual_display_present"},{"accepted",accepted},{"size",size}}.dump()); });
        }
        void WaitForPresent() override {backend_->wait_for_present();}
        bool GetTimeSinceLastVsync(float* seconds,uint64_t* counter) override {
            const bool result=backend_->time_since_vsync(seconds,counter);
            if(time_calls_.fetch_add(1)<4)best_effort([&] { log(nlohmann::json{{"event","virtual_display_timing"},{"valid",result}}.dump()); });return result;
        }
    };
    std::unique_ptr<Redirect> redirect_;
#endif
public:
    vr::EVRInitError Init(vr::IVRDriverContext* context) override try {
        if (!context) return vr::VRInitError_Init_InterfaceNotFound;
        std::lock_guard<std::mutex> transition(provider_transition_mutex_);
        ScopeExit context_cleanup([] { vr::CleanupDriverContext(); });
        const auto error = vr::InitServerDriverContext(context);
        if (error != vr::VRInitError_None) return error;
        try {
            ScopeExit rollback([&] {
                hmd_.reset();
#if defined(_MSC_VER)
                redirect_.reset();
#endif
            });
#if defined(_MSC_VER)
            log("build compiler=MSVC version=" + std::to_string(_MSC_VER) + " display_abi=native");
#elif defined(__MINGW32__)
            log("build compiler=MinGW version=" __VERSION__ " display_abi=windows-sret-bridge");
#else
            log("build compiler=other display_abi=native");
#endif
            vr::EVRSettingsError settings_error = vr::VRSettingsError_None;
            const bool enabled = vr::VRSettings()->GetBool("driver_psvrpc","enable",&settings_error);
            if (settings_error != vr::VRSettingsError_None || !enabled) {
                log("disabled by default; enable driver_psvrpc only for explicit runtime tests");
                { std::lock_guard<std::mutex> gate(frame_gate_mutex_); frames_enabled_ = true; }
                rollback.release();
                context_cleanup.release();
                return vr::VRInitError_None;
            }
            psvr::ImuConfig config;
            std::array<char,32> mode{};
            vr::VRSettings()->GetString("driver_psvrpc","recenterMode",mode.data(),mode.size(),&settings_error);
            if (settings_error != vr::VRSettingsError_None || (std::strcmp(mode.data(),"full") && std::strcmp(mode.data(),"yaw")))
                throw std::runtime_error("recenterMode must be full or yaw");
            config.recenter_mode = mode.data();
            psvr::load_imu_timing_settings(*vr::VRSettings(),config);
            auto display = psvr::make_verified_display(load_display_config(*vr::VRSettings()),verify_selected_active_display);
            std::uint64_t virtual_adapter=0;
#if defined(_MSC_VER)
            if(display && display->config().virtual_display) {
                // Explicit package gate: registration metadata is part of the runtime contract.
                const auto install=vr::VRProperties()->GetStringProperty(vr::VRDriverHandle(),vr::Prop_InstallPath_String);
                std::ifstream manifest(std::filesystem::u8path(install)/"driver.vrdrivermanifest");
                const auto data=nlohmann::json::parse(manifest);
                if(!data.value("redirectsDisplay",false))throw std::runtime_error("virtualDisplayEnabled requires experimental redirectsDisplay package");
                redirect_=std::make_unique<Redirect>(display->config());virtual_adapter=redirect_->adapter();
                if(!vr::VRServerDriverHost()->TrackedDeviceAdded("PSVRPC-DISPLAY-REDIRECT-0",vr::TrackedDeviceClass_DisplayRedirect,redirect_.get()))
                    throw std::runtime_error("DisplayRedirect registration rejected");
            }
#endif
            hmd_ = std::make_unique<Hmd>(config,std::move(display),virtual_adapter);
            if (!vr::VRServerDriverHost()->TrackedDeviceAdded("PSVRPC-HMD-0",vr::TrackedDeviceClass_HMD,hmd_.get()))
                throw std::runtime_error("TrackedDeviceAdded rejected HMD");
            log("HMD registered, OpenVR SDK 2.15.6");
            { std::lock_guard<std::mutex> gate(frame_gate_mutex_); frames_enabled_ = true; }
            rollback.release();
            context_cleanup.release();
            return vr::VRInitError_None;
        } catch (const std::exception& error) {
            log_failure(error.what());
            return vr::VRInitError_Driver_Failed;
        } catch (...) {
            log_failure("unexpected provider initialization exception");
            return vr::VRInitError_Driver_Failed;
        }
    } catch (...) {
        log_failure("unexpected driver context initialization exception");
        return vr::VRInitError_Driver_Failed;
    }
    void Cleanup() noexcept override try {
        std::lock_guard<std::mutex> transition(provider_transition_mutex_);
        {
            std::unique_lock<std::mutex> gate(frame_gate_mutex_);
            frames_enabled_ = false;
            frame_idle_.wait(gate,[this] { return frames_in_flight_ == 0; });
        }
        // No admitted frame can access either unique_ptr or the runtime context.
        // Keep the transition lock through context cleanup, including when two
        // Cleanup calls overlap; never wait for Hmd while holding the frame gate.
        hmd_.reset();
#if defined(_MSC_VER)
        redirect_.reset();
#endif
        best_effort([] { vr::CleanupDriverContext(); });
    } catch (...) { log_failure("unexpected provider cleanup exception"); }
    const char* const* GetInterfaceVersions() override { return vr::k_InterfaceVersions; }
    void RunFrame() override try {
        {
            std::lock_guard<std::mutex> gate(frame_gate_mutex_);
            if (!frames_enabled_) return;
            ++frames_in_flight_;
        }
        struct FrameExit {
            Provider& provider;
            ~FrameExit() {
                std::lock_guard<std::mutex> gate(provider.frame_gate_mutex_);
                if (--provider.frames_in_flight_ == 0) provider.frame_idle_.notify_all();
            }
        } frame{*this}; // includes redirect polling and exceptional callback exits
        if (hmd_) hmd_->publish();
#if defined(_MSC_VER)
        if(redirect_)redirect_->poll_health();
#endif
    } catch (...) { log_failure("unexpected runtime frame exception"); }
    bool ShouldBlockStandbyMode() override { return false; }
    void EnterStandby() override {}
    void LeaveStandby() override {}
};
Provider provider;
}

extern "C" __declspec(dllexport) void* HmdDriverFactory(const char* version, int* return_code) {
    if (version && std::strcmp(version,vr::IServerTrackedDeviceProvider_Version) == 0) {
        if (return_code) *return_code = vr::VRInitError_None;
        return &provider;
    }
    if (return_code) *return_code = vr::VRInitError_Init_InterfaceNotFound;
    return nullptr;
}
