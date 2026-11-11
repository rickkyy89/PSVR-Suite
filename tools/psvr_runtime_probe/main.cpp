// SPDX-License-Identifier: MIT
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <tlhelp32.h>
#include <openvr.h>
#include <nlohmann/json.hpp>
#include "../psvr_diag/orientation_math.hpp"
#include <array>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
using Json = nlohmann::json;
constexpr const char* target_serial = "PSVRPC-HMD-0";
struct Options {
    std::wstring dll;
    unsigned duration_ms = 1000;
    std::string request; // Empty by default: no IVRDebug lookup or mutation.
    bool compositor_status = false;
    bool bring_to_front = false;
    bool disable_standby_pause = false;
    std::wstring optics_grid;
    bool grid_near = false;
    std::string dashboard_curvature;
};

bool parse(int argc, wchar_t** argv, Options& options) {
    bool dll_seen = false, duration_seen = false, request_seen = false;
    bool status_seen = false, bring_seen = false, pause_seen = false;
    bool grid_seen = false;
    bool near_seen = false, curvature_seen = false;
    for (int i = 1; i < argc; ++i) {
        const std::wstring arg(argv[i]);
        if (arg == L"--openvr-dll" && !dll_seen && i + 1 < argc) {
            dll_seen = true;
            options.dll = argv[++i];
            // Drive-relative and root-relative paths must never use DLL search.
            if (options.dll.empty() || !std::filesystem::path(options.dll).is_absolute()) return false;
        } else if (arg == L"--duration-ms" && !duration_seen && i + 1 < argc) {
            duration_seen = true;
            const std::wstring value(argv[++i]);
            if (value.empty()) return false;
            unsigned number = 0;
            for (const wchar_t ch : value) {
                if (ch < L'0' || ch > L'9') return false;
                number = number * 10 + static_cast<unsigned>(ch - L'0');
                if (number > 120000) return false; // Grid is the only long-lived mode.
            }
            if (number < 100) return false;
            options.duration_ms = number;
        } else if (arg == L"--request" && !request_seen && i + 1 < argc) {
            request_seen = true;
            const std::wstring command(argv[++i]);
            if (command == L"recenter") options.request = "recenter";
            else if (command == L"recalibrate") options.request = "recalibrate";
            else if (command == L"simulate-worn") options.request = "simulate-worn";
            else if (command == L"simulate-unworn") options.request = "simulate-unworn";
            else if (command == L"tracking-status") options.request = "tracking-status";
            else return false; // No arbitrary commands, including quit.
        } else if (arg == L"--compositor-status" && !status_seen) {
            status_seen = true;
            options.compositor_status = true;
        } else if (arg == L"--bring-to-front" && !bring_seen) {
            bring_seen = true;
            options.bring_to_front = true;
            options.compositor_status = true;
        } else if (arg == L"--disable-standby-pause" && !pause_seen) {
            pause_seen = true;
            options.disable_standby_pause = true;
            options.compositor_status = true;
        } else if (arg == L"--optics-grid" && !grid_seen && i+1<argc) {
            grid_seen = true;
            options.optics_grid = argv[++i];
            if (!std::filesystem::path(options.optics_grid).is_absolute()) return false;
            options.compositor_status = true;
        } else if (arg == L"--grid-near" && !near_seen) {
            near_seen = true; options.grid_near = true;
        } else if (arg == L"--dashboard-curvature" && !curvature_seen && i+1<argc) {
            curvature_seen = true;
            const std::wstring value=argv[++i];
            if(value==L"off") options.dashboard_curvature="off";
            else if(value==L"on") options.dashboard_curvature="on";
            else if(value==L"default") options.dashboard_curvature="default";
            else return false;
            options.compositor_status = true;
        } else return false;
    }
    return dll_seen && !(request_seen && options.compositor_status) && !(pause_seen && bring_seen)
        && (grid_seen || options.duration_ms <= 10000) && !(grid_seen && (bring_seen || pause_seen || curvature_seen))
        && (!near_seen || grid_seen) && !(curvature_seen && (bring_seen || pause_seen));
}

struct Handle {
    HANDLE value;
    ~Handle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
};

bool server_running() {
    DWORD our_session = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &our_session))
        throw std::runtime_error("Cannot determine current Windows session");
    Handle snapshot{CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)};
    if (snapshot.value == INVALID_HANDLE_VALUE)
        throw std::runtime_error("Cannot enumerate processes for runtime preflight");
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    if (!Process32FirstW(snapshot.value, &entry))
        throw std::runtime_error("Cannot read process snapshot for runtime preflight");
    do {
        DWORD session = 0;
        if (_wcsicmp(entry.szExeFile, L"vrserver.exe") == 0 &&
            ProcessIdToSessionId(entry.th32ProcessID, &session) && session == our_session)
            return true;
    } while (Process32NextW(snapshot.value, &entry));
    if (GetLastError() != ERROR_NO_MORE_FILES)
        throw std::runtime_error("Incomplete process snapshot for runtime preflight");
    return false;
}

using Init = uint32_t (VR_CALLTYPE *)(vr::EVRInitError*, vr::EVRApplicationType, const char*);
using GetInterface = void* (VR_CALLTYPE *)(const char*, vr::EVRInitError*);
using Shutdown = void (VR_CALLTYPE *)();
using DescribeError = const char* (VR_CALLTYPE *)(vr::EVRInitError);

struct Runtime {
    HMODULE dll = nullptr;
    Shutdown shutdown = nullptr;
    bool initialized = false;
    Runtime() = default;
    Runtime(const Runtime&) = delete;
    Runtime& operator=(const Runtime&) = delete;
    ~Runtime() {
        if (initialized && shutdown) shutdown();
        if (dll) FreeLibrary(dll);
    }
};

template<class T> T symbol(HMODULE dll, const char* name) {
    // Windows guarantees GetProcAddress is usable as the matching function pointer.
    const auto address = GetProcAddress(dll, name);
    T function = nullptr;
    static_assert(sizeof(function) == sizeof(address), "Function pointer size mismatch");
    std::memcpy(&function, &address, sizeof(function));
    return function;
}

void emit(const Json& event) {
    // A driver debug response may contain malformed UTF-8; still emit valid JSON.
    std::cout << event.dump(-1, ' ', false, Json::error_handler_t::replace) << '\n' << std::flush;
    if (!std::cout) throw std::runtime_error("Cannot write JSON output");
}

std::string api_error(const char* stage, vr::EVRInitError error, DescribeError describe) {
    std::string message = std::string(stage) + ": OpenVR error " + std::to_string(static_cast<int>(error));
    if (describe) {
        if (const char* text = describe(error)) message += std::string(" (") + text + ")";
    }
    return message;
}

vr::TrackedDeviceIndex_t find_target(vr::IVRSystem& system) {
    for (vr::TrackedDeviceIndex_t i = 0; i < vr::k_unMaxTrackedDeviceCount; ++i) {
        if (system.GetTrackedDeviceClass(i) != vr::TrackedDeviceClass_HMD) continue;
        std::array<char, 128> serial{};
        vr::ETrackedPropertyError error = vr::TrackedProp_Success;
        const auto length = system.GetStringTrackedDeviceProperty(
            i, vr::Prop_SerialNumber_String, serial.data(), static_cast<uint32_t>(serial.size()), &error);
        if (error == vr::TrackedProp_Success && length == std::strlen(target_serial) + 1 &&
            std::memcmp(serial.data(), target_serial, length) == 0) return i;
    }
    return vr::k_unTrackedDeviceIndexInvalid;
}

bool orientation(const vr::HmdMatrix34_t& matrix, Json& quaternion, Json& euler) {
    const auto& m = matrix.m;
    for (const auto& row : m) for (float element : row)
        if (!std::isfinite(element)) return false;
    // Reject zero/uninitialized or malformed rotations rather than manufacture a pose.
    for (int a = 0; a < 3; ++a) for (int b = a; b < 3; ++b) {
        double dot = 0;
        for (int k = 0; k < 3; ++k) dot += double(m[k][a]) * m[k][b];
        if (std::abs(dot - (a == b ? 1.0 : 0.0)) > 0.01) return false;
    }
    const double determinant =
        double(m[0][0]) * (double(m[1][1])*m[2][2] - double(m[1][2])*m[2][1]) -
        double(m[0][1]) * (double(m[1][0])*m[2][2] - double(m[1][2])*m[2][0]) +
        double(m[0][2]) * (double(m[1][0])*m[2][1] - double(m[1][1])*m[2][0]);
    if (std::abs(determinant - 1.0) > 0.02) return false;
    psvr::Quaternion q;
    const double trace = double(m[0][0]) + m[1][1] + m[2][2];
    if (trace > 0) {
        const double s = 2 * std::sqrt(trace + 1);
        q = {s/4, (double(m[2][1])-m[1][2])/s,
             (double(m[0][2])-m[2][0])/s, (double(m[1][0])-m[0][1])/s};
    } else {
        int i = 0;
        if (m[1][1] > m[i][i]) i = 1;
        if (m[2][2] > m[i][i]) i = 2;
        const int j = (i+1)%3, k = (i+2)%3;
        const double s = 2 * std::sqrt(std::max(0.0, 1.0 + m[i][i] - m[j][j] - m[k][k]));
        if (s < 1e-12) return false;
        double xyz[3]{};
        xyz[i] = s/4;
        xyz[j] = (double(m[j][i])+m[i][j])/s;
        xyz[k] = (double(m[k][i])+m[i][k])/s;
        q = {(double(m[k][j])-m[j][k])/s, xyz[0], xyz[1], xyz[2]};
    }
    q = q.normalized();
    if (q.w < 0) q = {-q.w, -q.x, -q.y, -q.z};
    quaternion = {q.w, q.x, q.y, q.z};
    // Y-X-Z: Ry(yaw)*Rx(pitch)*Rz(roll). At gimbal lock choose roll=0.
    const double pitch = std::asin(std::clamp(-double(m[1][2]), -1.0, 1.0));
    const bool singular = std::abs(std::cos(pitch)) < 1e-6;
    const double yaw = singular ? std::atan2(-double(m[2][0]), double(m[0][0]))
                                : std::atan2(double(m[0][2]), double(m[2][2]));
    const double roll = singular ? 0 : std::atan2(double(m[1][0]), double(m[1][1]));
    euler = {{"yaw", yaw*psvr::degrees}, {"pitch", pitch*psvr::degrees},
             {"roll", roll*psvr::degrees}, {"gimbal_lock", singular}};
    return true;
}

Json send_request(const Options& options, vr::IVRSystem& system,
                  GetInterface get_interface, DescribeError describe) {
    Json event = {{"event", "request"}, {"serial", target_serial},
        {"command", options.request}, {"device_index", nullptr}, {"dispatched", false},
        {"response", nullptr}, {"response_bytes_including_nul", nullptr},
        {"response_nul_terminated", nullptr}, {"response_truncated", nullptr},
        {"queued", false}, {"applied_verified", false}, {"verification", "not_dispatched"}};
    const auto index = find_target(system);
    if (index == vr::k_unTrackedDeviceIndexInvalid) {
        event["reason"] = "no_matching_hmd";
        return event; // Do not even resolve IVRDebug without the exact HMD.
    }
    vr::EVRInitError error = vr::VRInitError_None;
    auto* debug = static_cast<vr::IVRDebug*>(get_interface(vr::IVRDebug_Version, &error));
    if (!debug || error != vr::VRInitError_None)
        throw std::runtime_error(api_error("IVRDebug interface", error, describe));
    // Interface lookup may take time: recheck serial and HMD class before dispatch.
    if (find_target(system) != index) {
        event["reason"] = "target_changed_before_dispatch";
        return event;
    }
    std::array<char, 4096> response;
    response.fill('\x7f'); // Detect a missing NUL; never scan beyond this buffer.
    const auto bytes = debug->DriverDebugRequest(index, options.request.c_str(),
        response.data(), static_cast<uint32_t>(response.size()));
    const auto nul = std::find(response.begin(), response.end(), '\0');
    const bool terminated = nul != response.end();
    const bool truncated = !terminated || bytes > response.size();
    const std::string text(response.begin(), nul);
    const std::string expected = options.request == "recenter" ? "recenter queued"
        : options.request == "simulate-worn" ? "simulated worn queued; expires after 180 seconds"
        : options.request == "simulate-unworn" ? "simulated unworn queued"
        : "recalibration queued; keep headset stationary";
    const bool queued = !truncated && bytes == text.size() + 1 && text == expected;
    event["device_index"] = index;
    event["dispatched"] = true;
    event["response"] = text;
    event["response_bytes_including_nul"] = bytes;
    event["response_nul_terminated"] = terminated;
    event["response_truncated"] = truncated;
    if (options.request == "tracking-status") {
        event["queued"] = false;
        event["verification"] = "read_only_query";
        if (!truncated) {
            try {
                const auto status=Json::parse(text);
                if(status.is_object() && status.value("schema",std::string())=="psvr.driver.tracking_status") {
                    event["driver_tracking"]=status;
                    event["status_supported"]=true;
                } else event["status_supported"]=false;
            } catch(const Json::exception&) { event["status_supported"]=false; }
        }
        return event;
    }
    event["queued"] = queued;
    event["verification"] = queued ? "queued_not_applied" : "response_unconfirmed";
    return event; // Never retry a mutating request to recover/truncate its response.
}

Json compositor_snapshot(vr::IVRCompositor& compositor, const char* phase,
                         bool requested, bool dispatched) {
    return {{"event", "compositor_status"}, {"phase", phase},
        {"fullscreen", compositor.IsFullscreen()},
        {"can_render_scene", compositor.CanRenderScene()},
        {"scene_focus_pid", compositor.GetCurrentSceneFocusProcess()},
        {"bring_requested", requested}, {"bring_dispatched", dispatched},
        {"verification", dispatched ? "observed_before_after" :
            (requested ? "not_dispatched" : "not_requested")}};
}

int probe(const Options& options) {
    Runtime runtime;
    runtime.dll = LoadLibraryExW(options.dll.c_str(), nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!runtime.dll) throw std::runtime_error("LoadLibraryExW failed: Windows error " + std::to_string(GetLastError()));
    const auto init = symbol<Init>(runtime.dll, "VR_InitInternal2");
    const auto get_interface = symbol<GetInterface>(runtime.dll, "VR_GetGenericInterface");
    runtime.shutdown = symbol<Shutdown>(runtime.dll, "VR_ShutdownInternal");
    const auto describe = symbol<DescribeError>(runtime.dll, "VR_GetVRInitErrorAsEnglishDescription");
    if (!init || !get_interface || !runtime.shutdown)
        throw std::runtime_error("DLL is missing required OpenVR exports");
    // Utility does not load drivers; Background alone has an explicit no-start promise
    // in this SDK. Require the existing server and NEVER fall back to Scene init.
    if (!server_running()) throw std::runtime_error("No vrserver.exe in current session; refusing OpenVR initialization");
    vr::EVRInitError error = vr::VRInitError_None;
    // Utility intentionally has no IVRCompositor access (runtime error 123).
    // Only explicit compositor flags select Background; never take Scene focus
    // or silently retry initialization with another application type.
    init(&error, options.compositor_status ? vr::VRApplication_Background : vr::VRApplication_Utility, nullptr);
    if (error != vr::VRInitError_None) throw std::runtime_error(api_error(options.compositor_status ? "Background initialization" : "Utility initialization", error, describe));
    runtime.initialized = true;
    auto* system = static_cast<vr::IVRSystem*>(get_interface(vr::IVRSystem_Version, &error));
    if (!system || error != vr::VRInitError_None)
        throw std::runtime_error(api_error("IVRSystem interface", error, describe));

    if (options.disable_standby_pause) {
        const auto index = find_target(*system);
        if (index == vr::k_unTrackedDeviceIndexInvalid)
            throw std::runtime_error("No target HMD: standby policy not changed");
        auto* settings = static_cast<vr::IVRSettings*>(get_interface(vr::IVRSettings_Version,&error));
        if (!settings || error != vr::VRInitError_None)
            throw std::runtime_error(api_error("IVRSettings interface",error,describe));
        vr::EVRSettingsError setting_error = vr::VRSettingsError_None;
        const bool before = settings->GetBool("power","pauseCompositorOnStandby",&setting_error);
        if (setting_error != vr::VRSettingsError_None || find_target(*system) != index)
            throw std::runtime_error("Cannot verify standby policy/target: no change applied");
        settings->SetBool("power","pauseCompositorOnStandby",false,&setting_error);
        if (setting_error != vr::VRSettingsError_None)
            throw std::runtime_error("Standby pause setting write failed");
        const bool after = settings->GetBool("power","pauseCompositorOnStandby",&setting_error);
        if (setting_error != vr::VRSettingsError_None || after)
            throw std::runtime_error("Standby pause setting readback failed");
        emit({{"event","standby_policy"},{"serial",target_serial},
            {"key","power.pauseCompositorOnStandby"},{"before",before},{"after",after},
            {"verification","setting_readback_only"},{"fullscreen_fix_verified",false}});
    }

    Json request_event = nullptr;
    if (!options.request.empty()) {
        request_event = send_request(options, *system, get_interface, describe);
        emit(request_event);
    }
    if (!options.dashboard_curvature.empty()) {
        if (find_target(*system)==vr::k_unTrackedDeviceIndexInvalid) throw std::runtime_error("Dashboard curvature requires verified PSVR HMD");
        auto* settings=static_cast<vr::IVRSettings*>(get_interface(vr::IVRSettings_Version,&error));
        if(!settings||error!=vr::VRInitError_None) throw std::runtime_error("Dashboard settings unavailable");
        vr::EVRSettingsError se=vr::VRSettingsError_None;
        const bool before=settings->GetBool("dashboard","allowCurvature",&se);
        if(se!=vr::VRSettingsError_None) throw std::runtime_error("Cannot read dashboard allowCurvature");
        if(options.dashboard_curvature=="default") settings->RemoveKeyInSection("dashboard","allowCurvature",&se);
        else settings->SetBool("dashboard","allowCurvature",options.dashboard_curvature=="on",&se);
        if(se!=vr::VRSettingsError_None) throw std::runtime_error("Cannot change dashboard allowCurvature");
        const bool after=settings->GetBool("dashboard","allowCurvature",&se);
        if(se!=vr::VRSettingsError_None) throw std::runtime_error("Cannot verify dashboard allowCurvature");
        emit({{"event","dashboard_curvature"},{"before",before},{"after",after},{"requested",options.dashboard_curvature},
            {"verification","setting_readback_only"},{"optical_projection_changed",false}});
    }

    struct GridOverlay {
        vr::IVROverlay* api = nullptr;
        vr::VROverlayHandle_t handle = vr::k_ulOverlayHandleInvalid;
        ~GridOverlay() { if (api && handle != vr::k_ulOverlayHandleInvalid) { api->HideOverlay(handle); api->DestroyOverlay(handle); } }
    } grid;
    if (!options.optics_grid.empty()) {
        if (!std::filesystem::is_regular_file(options.optics_grid)) throw std::runtime_error("Optics grid PNG missing");
        const auto index = find_target(*system);
        if (index == vr::k_unTrackedDeviceIndexInvalid) throw std::runtime_error("Optics grid requires verified PSVR HMD");
        std::array<vr::TrackedDevicePose_t,vr::k_unMaxTrackedDeviceCount> current_poses{};
        system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseRawAndUncalibrated,0,current_poses.data(),static_cast<uint32_t>(current_poses.size()));
        if (!current_poses[index].bPoseIsValid || !current_poses[index].bDeviceIsConnected)
            throw std::runtime_error("Optics grid requires a connected, valid calibrated pose");
        auto* display_status = static_cast<vr::IVRCompositor*>(get_interface(vr::IVRCompositor_Version,&error));
        if (!display_status || error != vr::VRInitError_None || !display_status->IsFullscreen())
            throw std::runtime_error("Optics grid requires active compositor fullscreen");
        error = vr::VRInitError_None;
        grid.api = static_cast<vr::IVROverlay*>(get_interface(vr::IVROverlay_Version,&error));
        if (!grid.api || error != vr::VRInitError_None) throw std::runtime_error("Optics grid overlay interface unavailable");
        const auto require = [&](vr::EVROverlayError result) {
            if (result != vr::VROverlayError_None) throw std::runtime_error(std::string("Optics grid: ")+grid.api->GetOverlayErrorNameFromEnum(result));
        };
        const auto key = "psvrpc.optics.grid."+std::to_string(GetCurrentProcessId());
        require(grid.api->CreateOverlay(key.c_str(),"PSVR-PC flat optical grid",&grid.handle));
        require(grid.api->SetOverlayFromFile(grid.handle,std::filesystem::path(options.optics_grid).u8string().c_str()));
        const float distance=options.grid_near ? .6f : 1.5f;
        const float width=3.4f*distance/1.5f; // Keep angular size constant while changing depth.
        require(grid.api->SetOverlayWidthInMeters(grid.handle,width));
        require(grid.api->SetOverlaySortOrder(grid.handle,10000));
        require(grid.api->SetOverlayFlag(grid.handle,vr::VROverlayFlags_VisibleInDashboard,true));
        vr::HmdMatrix34_t transform{}; transform.m[0][0]=1;transform.m[1][1]=1;transform.m[2][2]=1;transform.m[2][3]=-distance;
        require(grid.api->SetOverlayTransformTrackedDeviceRelative(grid.handle,index,&transform));
        require(grid.api->ShowOverlay(grid.handle));
        emit({{"event","optics_grid"},{"visible",grid.api->IsOverlayVisible(grid.handle)},
            {"surface","flat plane, HMD-relative"},{"distance_m",distance},{"width_m",width},
            {"duration_ms",options.duration_ms},{"optical_calibration_verified",false}});
    }

    vr::IVRCompositor* compositor = nullptr;
    Json compositor_before = nullptr, compositor_after = nullptr, compositor_final = nullptr;
    bool bring_dispatched = false;
    std::string bring_reason = options.bring_to_front ? "no_matching_hmd" : "not_requested";
    if (options.compositor_status) {
        error = vr::VRInitError_None;
        compositor = static_cast<vr::IVRCompositor*>(get_interface(vr::IVRCompositor_Version, &error));
        if (!compositor || error != vr::VRInitError_None)
            throw std::runtime_error(api_error("IVRCompositor interface (no Scene fallback)", error, describe));
        compositor_before = compositor_snapshot(*compositor, "before", options.bring_to_front, false);
        emit(compositor_before);
        if (options.bring_to_front) {
            const auto index = find_target(*system);
            if (index != vr::k_unTrackedDeviceIndexInvalid) {
                // The before observation and interface lookup precede the final identity check.
                if (find_target(*system) == index) {
                    // Void API: focus request only, not proof of focus or fullscreen success.
                    compositor->CompositorBringToFront();
                    bring_dispatched = true; // Exactly one dispatch; never retry.
                    bring_reason = "focus_request_only_no_fullscreen_guarantee";
                } else bring_reason = "target_changed_before_dispatch";
            }
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        compositor_after = compositor_snapshot(*compositor, "after_100ms", options.bring_to_front, bring_dispatched);
        compositor_after["reason"] = bring_reason;
        emit(compositor_after);
    }

    using Clock = std::chrono::steady_clock;
    const auto start = Clock::now();
    const auto deadline = start + std::chrono::milliseconds(options.duration_ms);
    auto next = start;
    unsigned samples = 0, target_samples = 0, valid_count = 0;
    bool ever_connected = false;
    while (Clock::now() < deadline) {
        Json event = {{"event", "snapshot"}, {"sample", samples++},
            {"elapsed_ms", std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-start).count()},
            {"serial", target_serial}, {"tracking_universe", "raw_and_uncalibrated"},
            {"prediction_seconds", 0}, {"target_found", false}, {"device_index", nullptr},
            {"valid", false}, {"connected", false}, {"tracking_result", nullptr},
            {"accepted", false}, {"position_m", nullptr}, {"matrix34", nullptr},
            {"quaternion_wxyz", nullptr}, {"euler_yxz_degrees", nullptr}};
        const auto index = find_target(*system); // Recheck serial/class on every sample.
        if (index != vr::k_unTrackedDeviceIndexInvalid) {
            std::array<vr::TrackedDevicePose_t, vr::k_unMaxTrackedDeviceCount> poses{};
            system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseRawAndUncalibrated,
                0.0f, poses.data(), static_cast<uint32_t>(poses.size()));
            // Do not accept another device if hotplug changed the index during the read.
            if (find_target(*system) == index) {
                ++target_samples;
                const auto& pose = poses[index];
                const auto& m = pose.mDeviceToAbsoluteTracking.m;
                event["target_found"] = true;
                event["device_index"] = index;
                event["valid"] = pose.bPoseIsValid;
                event["connected"] = pose.bDeviceIsConnected;
                event["tracking_result"] = static_cast<int>(pose.eTrackingResult);
                event["position_m"] = {m[0][3], m[1][3], m[2][3]};
                event["matrix34"] = {{m[0][0],m[0][1],m[0][2],m[0][3]},
                    {m[1][0],m[1][1],m[1][2],m[1][3]}, {m[2][0],m[2][1],m[2][2],m[2][3]}};
                ever_connected = ever_connected || pose.bDeviceIsConnected;
                if (pose.bPoseIsValid && pose.bDeviceIsConnected &&
                    orientation(pose.mDeviceToAbsoluteTracking, event["quaternion_wxyz"], event["euler_yxz_degrees"])) {
                    event["accepted"] = true;
                    ++valid_count;
                }
            }
        }
        emit(event);
        next += std::chrono::milliseconds(100);
        if (next < Clock::now()) next = Clock::now(); // Never burst to catch up.
        std::this_thread::sleep_until(std::min(next, deadline));
    }
    const bool queried_status = !request_event.is_null() && request_event.value("status_supported",false);
    const int exit_code = valid_count || queried_status ? 0 : 4;
    Json summary = {{"event", "summary"}, {"serial", target_serial}, {"duration_ms", options.duration_ms},
        {"elapsed_ms", std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-start).count()},
        {"sample_count", samples}, {"target_sample_count", target_samples}, {"valid_count", valid_count},
        {"ever_connected", ever_connected}, {"exit_code", exit_code},
        {"request_command", options.request.empty() ? Json(nullptr) : Json(options.request)},
        {"request_dispatched", !request_event.is_null() && request_event["dispatched"].get<bool>()},
        {"request_verification", request_event.is_null() ? Json("not_requested") : request_event["verification"]},
        {"reason", valid_count ? "valid_target_pose_collected" : (target_samples ? "invalid_only" : "no_matching_hmd")}};
    if (compositor) {
        compositor_final = compositor_snapshot(*compositor, "final", options.bring_to_front, bring_dispatched);
        compositor_final["reason"] = bring_reason;
        emit(compositor_final);
        summary["compositor"] = {{"fullscreen", compositor_final["fullscreen"]},
            {"can_render_scene", compositor_final["can_render_scene"]},
            {"scene_focus_pid", compositor_final["scene_focus_pid"]},
            {"bring_requested", options.bring_to_front}, {"bring_dispatched", bring_dispatched},
            {"verification", compositor_final["verification"]}, {"reason", bring_reason},
            {"before", compositor_before}, {"after_100ms", compositor_after}, {"final", compositor_final}};
    }
    emit(summary);
    return exit_code;
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--help") {
        std::cout << "Usage: psvr_runtime_probe --openvr-dll <absolute DLL path> [--duration-ms 100..10000]\n"
            "                          [--request recenter|recalibrate|simulate-worn|simulate-unworn]\n"
            "                          [--compositor-status] [--bring-to-front]\n"
            "                          [--optics-grid <absolute PNG> --duration-ms 100..120000]\n"
            "                          [--grid-near] [--dashboard-curvature off|on|default]\n"
            "OpenVR client pose diagnostic for PSVRPC-HMD-0 (HMD class); read-only by default.\n"
            "Requires already-running vrserver.exe in this session; Utility init only, no Scene fallback.\n"
            "JSON lines at approximately 10 Hz, raw/uncalibrated universe, zero prediction.\n"
             "Default duration: 1000 ms. Default path: no USB, registration or settings changes.\n"
            "Optional --request dispatches once via IVRDebug only after exact serial/HMD verification.\n"
            "Request JSON reports queued acknowledgment, NOT applied verification; snapshots follow.\n"
            "No target: request skipped, never retried. Recalibrate: keep headset stationary.\n"
             "--compositor-status: explicit Background client, read-only fullscreen/CanRenderScene/scene focus PID.\n"
            "--bring-to-front implies status: exact serial/HMD required, focus request dispatched once.\n"
            "Focus only: no guaranteed fullscreen; void API verification is observed_before_after, NOT success.\n"
            "Status observations: before, after 100 ms, and after pose duration; no Scene fallback.\n"
            "No matching HMD: no focus action. Missing compositor API: exit 3.\n"
             "Compositor flags cannot combine with --request; duplicate flags are invalid.\n"
             "--optics-grid: temporary flat reference overlay, verified HMD; implies status. Removed automatically on exit.\n"
             "--grid-near keeps angular size but moves the reference to 0.6m.\n"
             "--dashboard-curvature explicitly changes dashboard.allowCurvature only; readback is not visual proof.\n"
             "--disable-standby-pause: explicit one-key SteamVR setting change, verified target required; implies status.\n"
             "Does not prove a fullscreen fix; cannot combine with --request or --bring-to-front.\n"
            "Exit: 0 valid target samples, 2 invalid CLI, 3 DLL/API/init failure, 4 no target/invalid only.\n";
        return 0;
    }
    try {
        Options options;
        if (!parse(argc, argv, options)) {
            emit({{"event", "error"}, {"exit_code", 2}, {"message", "Invalid arguments; use --help. Explicit absolute --openvr-dll required."}});
            return 2;
        }
        return probe(options);
    } catch (const std::exception& error) {
        std::cerr << "Runtime probe failed: " << error.what() << '\n';
        try { emit({{"event", "error"}, {"exit_code", 3}, {"message", error.what()}}); } catch (...) {}
        return 3;
    }
}
