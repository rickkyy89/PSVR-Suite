// SPDX-License-Identifier: MIT
// Hardware-free DLL integration test. NEVER call ITrackedDeviceServerDriver::Activate:
// it starts the HID reader. Never supply an enabled, valid display configuration.
// Build with C++17 and the SAME openvr_driver.h used to build the driver DLL.
// No driver implementation is compiled into this executable; kernel32 is the
// only explicitly required Windows library. Usage: executable <driver DLL>.
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <openvr_driver.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

// Fail closed on unexpected runtime operations, rather than let the driver
// catch a mock exception and continue toward a hardware operation.
[[noreturn]] void unexpected(const char* method) {
    std::cerr << "UNSAFE/UNEXPECTED mock call: " << method << '\n';
    std::abort();
}

class Settings final : public vr::IVRSettings {
public:
    using Value = std::variant<bool, int32_t, float, std::string>;
    std::map<std::string, Value> values;
    std::vector<std::string> reads;
    std::map<std::string, vr::EVRSettingsError> errors;

    template<class T> T read(const char* section, const char* key, vr::EVRSettingsError* error) {
        if (!section || std::strcmp(section, "driver_psvrpc") || !key)
            unexpected("settings section/key");
        reads.emplace_back(key);
        vr::EVRSettingsError result = vr::VRSettingsError_UnsetSettingHasNoDefault;
        T value{};
        const auto found = values.find(key);
        if (found != values.end()) {
            if (const auto* typed = std::get_if<T>(&found->second)) {
                value = *typed;
                result = vr::VRSettingsError_None;
            } else result = vr::VRSettingsError_JsonParseFailed;
        }
        const auto forced = errors.find(key);
        if (forced != errors.end()) result = forced->second;
        if (error) *error = result;
        return value;
    }
    const char* GetSettingsErrorNameFromEnum(vr::EVRSettingsError) override { return "mock settings error"; }
    void SetBool(const char*, const char*, bool, vr::EVRSettingsError*) override { unexpected("SetBool"); }
    void SetInt32(const char*, const char*, int32_t, vr::EVRSettingsError*) override { unexpected("SetInt32"); }
    void SetFloat(const char*, const char*, float, vr::EVRSettingsError*) override { unexpected("SetFloat"); }
    void SetString(const char*, const char*, const char*, vr::EVRSettingsError*) override { unexpected("SetString"); }
    bool GetBool(const char* s, const char* k, vr::EVRSettingsError* e) override { return read<bool>(s,k,e); }
    int32_t GetInt32(const char* s, const char* k, vr::EVRSettingsError* e) override { return read<int32_t>(s,k,e); }
    float GetFloat(const char* s, const char* k, vr::EVRSettingsError* e) override { return read<float>(s,k,e); }
    void GetString(const char* s, const char* k, char* buffer, uint32_t size, vr::EVRSettingsError* e) override {
        const auto value = read<std::string>(s,k,e);
        if (buffer && size) {
            const auto n = std::min(value.size(), static_cast<std::size_t>(size-1));
            std::memcpy(buffer, value.data(), n);
            buffer[n] = '\0';
            if (value.size() >= size && e) *e = vr::VRSettingsError_ReadFailed;
        }
    }
    void RemoveSection(const char*, vr::EVRSettingsError*) override { unexpected("RemoveSection"); }
    void RemoveKeyInSection(const char*, const char*, vr::EVRSettingsError*) override { unexpected("RemoveKeyInSection"); }
};

class Properties final : public vr::IVRProperties {
public:
    vr::ETrackedPropertyError ReadPropertyBatch(vr::PropertyContainerHandle_t, vr::PropertyRead_t*, uint32_t) override { unexpected("ReadPropertyBatch"); }
    vr::ETrackedPropertyError WritePropertyBatch(vr::PropertyContainerHandle_t, vr::PropertyWrite_t*, uint32_t) override { unexpected("WritePropertyBatch (activation forbidden)"); }
    const char* GetPropErrorNameFromEnum(vr::ETrackedPropertyError) override { unexpected("GetPropErrorNameFromEnum"); }
    vr::PropertyContainerHandle_t TrackedDeviceToPropertyContainer(vr::TrackedDeviceIndex_t) override { unexpected("TrackedDeviceToPropertyContainer (activation forbidden)"); }
};

class DriverLog final : public vr::IVRDriverLog {
public:
    std::vector<std::string> messages;
    void Log(const char* message) override {
        if (!message) unexpected("Log(null)");
        messages.emplace_back(message);
    }
    bool contains(const char* text) const {
        return std::any_of(messages.begin(), messages.end(), [text](const auto& m) {
            return m.find(text) != std::string::npos;
        });
    }
};

class Host final : public vr::IVRServerDriverHost {
public:
    bool accept = true;
    unsigned additions = 0;
    std::string serial;
    vr::ETrackedDeviceClass device_class = vr::TrackedDeviceClass_Invalid;
    vr::ITrackedDeviceServerDriver* device = nullptr; // borrowed until Cleanup ONLY
    bool TrackedDeviceAdded(const char* number, vr::ETrackedDeviceClass kind,
                            vr::ITrackedDeviceServerDriver* driver) override {
        ++additions;
        if (!number || !driver) unexpected("TrackedDeviceAdded(null)");
        serial = number;
        device_class = kind;
        // A real server would Activate here. The mock intentionally does NOT.
        // Do not retain the rejected pointer: the provider deletes it on failure.
        if (accept) device = driver;
        return accept;
    }
    void TrackedDevicePoseUpdated(uint32_t, const vr::DriverPose_t&, uint32_t) override { unexpected("TrackedDevicePoseUpdated before activation"); }
    void VsyncEvent(double) override { unexpected("VsyncEvent"); }
    void VendorSpecificEvent(uint32_t, vr::EVREventType, const vr::VREvent_Data_t&, double) override { unexpected("VendorSpecificEvent"); }
    bool IsExiting() override { unexpected("IsExiting"); }
    bool PollNextEvent(vr::VREvent_t*, uint32_t) override { unexpected("PollNextEvent"); }
    void GetRawTrackedDevicePoses(float, vr::TrackedDevicePose_t*, uint32_t) override { unexpected("GetRawTrackedDevicePoses"); }
    void RequestRestart(const char*, const char*, const char*, const char*) override { unexpected("RequestRestart"); }
    uint32_t GetFrameTimings(vr::Compositor_FrameTiming*, uint32_t) override { unexpected("GetFrameTimings"); }
    void SetDisplayEyeToHead(uint32_t, const vr::HmdMatrix34_t&, const vr::HmdMatrix34_t&) override { unexpected("SetDisplayEyeToHead"); }
    void SetDisplayProjectionRaw(uint32_t, const vr::HmdRect2_t&, const vr::HmdRect2_t&) override { unexpected("SetDisplayProjectionRaw"); }
    void SetRecommendedRenderTargetSize(uint32_t, uint32_t, uint32_t) override { unexpected("SetRecommendedRenderTargetSize"); }
};

class Manager final : public vr::IVRDriverManager {
public:
    uint32_t GetDriverCount() const override { unexpected("GetDriverCount"); }
    uint32_t GetDriverName(vr::DriverId_t, char*, uint32_t) override { unexpected("GetDriverName"); }
    vr::DriverHandle_t GetDriverHandle(const char*) override { unexpected("Manager::GetDriverHandle"); }
    bool IsEnabled(vr::DriverId_t) const override { unexpected("IsEnabled"); }
};

class Resources final : public vr::IVRResources {
public:
    uint32_t LoadSharedResource(const char*, char*, uint32_t) override { unexpected("LoadSharedResource"); }
    uint32_t GetResourceFullPath(const char*, const char*, char*, uint32_t) override { unexpected("GetResourceFullPath"); }
};

const std::array<const char*,6> required_interfaces{{
    vr::IVRServerDriverHost_Version, vr::IVRSettings_Version, vr::IVRProperties_Version,
    vr::IVRDriverLog_Version, vr::IVRDriverManager_Version, vr::IVRResources_Version
}};

class Context final : public vr::IVRDriverContext {
public:
    Settings settings;
    Properties properties;
    Host host;
    DriverLog log;
    Manager manager;
    Resources resources;
    std::vector<std::string> lookups;
    std::string missing_interface;
    Context() {
        settings.values = {{"enable", true}, {"displayEnabled", false},
                           {"recenterMode", std::string("full")}};
    }
    void* GetGenericInterface(const char* version, vr::EVRInitError* error) override {
        if (!version) unexpected("GetGenericInterface(null)");
        lookups.emplace_back(version);
        void* result = nullptr;
        // Cast to the actual SDK base interface, never fabricate dummy vtables.
        if (missing_interface != version) {
            if (!std::strcmp(version, vr::IVRSettings_Version)) result = static_cast<vr::IVRSettings*>(&settings);
            else if (!std::strcmp(version, vr::IVRProperties_Version)) result = static_cast<vr::IVRProperties*>(&properties);
            else if (!std::strcmp(version, vr::IVRServerDriverHost_Version)) result = static_cast<vr::IVRServerDriverHost*>(&host);
            else if (!std::strcmp(version, vr::IVRDriverLog_Version)) result = static_cast<vr::IVRDriverLog*>(&log);
            else if (!std::strcmp(version, vr::IVRDriverManager_Version)) result = static_cast<vr::IVRDriverManager*>(&manager);
            else if (!std::strcmp(version, vr::IVRResources_Version)) result = static_cast<vr::IVRResources*>(&resources);
            else unexpected("unknown context interface");
        }
        if (error) *error = result ? vr::VRInitError_None : vr::VRInitError_Init_InterfaceNotFound;
        return result;
    }
    vr::DriverHandle_t GetDriverHandle() override { unexpected("Context::GetDriverHandle"); }
    void check_interfaces() const {
        for (const auto* version : required_interfaces)
            require(std::count(lookups.begin(), lookups.end(), version) == 1,
                    std::string("mandatory interface not looked up exactly once: ")+version);
        require(lookups.size() == required_interfaces.size(), "unexpected interface lookup count");
    }
};

class Library {
    HMODULE module_ = nullptr;
public:
    using Factory = void* (__cdecl *)(const char*, int*);
    Factory factory = nullptr;
    explicit Library(const char* path) {
        // Resolve explicitly, so a DLL basename cannot silently select a different
        // installed driver through the normal DLL search path.
        std::array<char,32768> absolute{};
        const auto n = GetFullPathNameA(path, static_cast<DWORD>(absolute.size()), absolute.data(), nullptr);
        require(n && n < absolute.size(), "cannot resolve DLL path");
        module_ = LoadLibraryExA(absolute.data(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        require(module_ != nullptr, "LoadLibraryEx failed, Windows error="+std::to_string(GetLastError()));
        const auto address = GetProcAddress(module_, "HmdDriverFactory");
        // Avoid incompatible function-pointer cast warnings under MinGW.
        static_assert(sizeof(factory) == sizeof(address), "Windows function pointer size");
        std::memcpy(&factory, &address, sizeof(factory));
        if (!factory) {
            FreeLibrary(module_); module_ = nullptr;
            throw std::runtime_error("DLL has no HmdDriverFactory export");
        }
        std::cout << "Loaded actual DLL: " << absolute.data() << '\n';
    }
    ~Library() { if (module_) FreeLibrary(module_); }
    Library(const Library&) = delete;
    Library& operator=(const Library&) = delete;
};

// Must be declared AFTER Context: Cleanup runs while its mock interfaces live,
// including on assertion failure. Never dereference a captured HMD afterward.
struct Session {
    vr::IServerTrackedDeviceProvider& provider;
    Context& context;
    bool cleaned = false;
    void cleanup() {
        provider.Cleanup();
        context.host.device = nullptr;
        cleaned = true;
    }
    ~Session() { if (!cleaned) cleanup(); }
};

void check_pose(vr::ITrackedDeviceServerDriver& hmd) {
    const auto pose = hmd.GetPose();
    require(!pose.poseIsValid && !pose.deviceIsConnected, "sensorless pose must be invalid/disconnected");
    require(pose.result == vr::TrackingResult_Running_OutOfRange, "sensorless pose tracking result");
    require(pose.willDriftInYaw && !pose.shouldApplyHeadModel, "3DoF pose flags");
    require(pose.qRotation.w == 1 && pose.qRotation.x == 0 &&
            pose.qRotation.y == 0 && pose.qRotation.z == 0, "sensorless identity rotation");
    for (unsigned i = 0; i < 3; ++i)
        require(std::isfinite(pose.vecPosition[i]) && pose.vecPosition[i] == 0 &&
                pose.vecVelocity[i] == 0 && pose.vecAngularVelocity[i] == 0, "no invented motion");
}

void check_debug(vr::ITrackedDeviceServerDriver& hmd) {
    struct Request { const char* input; const char* expected; };
    const Request requests[] = {{"recenter", "recenter queued"},
        {"recalibrate", "recalibration queued; keep headset stationary"},
        {"simulate-worn", "simulation rejected: HMD not active"},
        {"simulate-unworn", "simulation rejected: HMD not active"},
        {"unknown", "supported: recenter, recalibrate, tracking-status"},
        {"", "supported: recenter, recalibrate, tracking-status"}, {nullptr, "supported: recenter, recalibrate, tracking-status"}};
    for (const auto& request : requests) {
        for (uint32_t size : {0u, 1u, 2u, 8u, 64u}) {
            std::array<char,66> guarded;
            guarded.fill('#');
            hmd.DebugRequest(request.input, guarded.data()+1, size);
            require(guarded[0] == '#' && guarded[size+1] == '#', "DebugRequest buffer overrun");
            if (size) {
                const auto n = std::min(std::strlen(request.expected), static_cast<std::size_t>(size-1));
                require(guarded[n+1] == '\0', "DebugRequest missing NUL");
                require(std::string(guarded.data()+1) == std::string(request.expected,n), "DebugRequest response/truncation");
                for (std::size_t i = n+2; i < guarded.size(); ++i)
                    require(guarded[i] == '#', "DebugRequest wrote beyond terminator");
            } else require(std::all_of(guarded.begin(), guarded.end(), [](char c) { return c == '#'; }), "zero-size response touched");
        }
        hmd.DebugRequest(request.input, nullptr, 0);
        hmd.DebugRequest(request.input, nullptr, 64);
    }
    std::array<char,2048> status{};
    hmd.DebugRequest("tracking-status",status.data(),static_cast<uint32_t>(status.size()));
    const std::string diagnostic(status.data());
    require(diagnostic.find("\"schema\":\"psvr.driver.tracking_status\"") != std::string::npos &&
            diagnostic.find("\"mode\":\"invalid\"") != std::string::npos &&
            diagnostic.find("\"pose_valid\":false") != std::string::npos &&
            diagnostic.find("\"position_valid\":false") != std::string::npos,
            "tracking-status must not fabricate tracking before activation");
    check_pose(hmd); // queued commands do not synthesize a sensor sample
}

void exercise(vr::IServerTrackedDeviceProvider& provider, Context& context,
              vr::EVRInitError expected, unsigned additions, const char* log_text,
              bool inspect_hmd = false) {
    Session session{provider, context};
    require(provider.Init(&context) == expected, "unexpected provider Init result");
    context.check_interfaces();
    require(context.host.additions == additions, "unexpected HMD registration count");
    require(context.log.contains(log_text), std::string("missing log: ")+log_text);
    if (additions) {
        require(context.host.serial == "PSVRPC-HMD-0", "HMD serial");
        require(context.host.device_class == vr::TrackedDeviceClass_HMD, "registered device class");
    }
    if (inspect_hmd) {
        auto* hmd = context.host.device;
        require(hmd != nullptr, "successful registration has no HMD");
        for (const char* version : {vr::IVRDisplayComponent_Version, vr::IVRDriverDirectModeComponent_Version,
                                   vr::IVRVirtualDisplay_Version, "unsupported_component", "", static_cast<const char*>(nullptr)})
            require(hmd->GetComponent(version) == nullptr, "tracking-only component must be null");
        check_pose(*hmd);
        check_debug(*hmd);
        hmd->EnterStandby();
        hmd->Deactivate(); // safe without activation; must remain idempotent
        hmd->Deactivate();
        check_pose(*hmd);
    } else require(context.host.device == nullptr, "unexpected retained HMD");
    require(!provider.ShouldBlockStandbyMode(), "provider blocks standby");
    provider.EnterStandby(); provider.LeaveStandby();
    provider.RunFrame(); // unactivated HMD must not publish anything
    session.cleanup();
    provider.RunFrame(); // catches residual HMD ownership after Cleanup
    provider.Cleanup(); // idempotent, mocks still alive
}

DWORD handles() {
    DWORD count = 0;
    require(GetProcessHandleCount(GetCurrentProcess(), &count) != 0, "GetProcessHandleCount failed");
    return count;
}
} // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "Usage: driver_runtime_mock_tests <actual-build-driver_psvrpc.dll>\n";
        return 2;
    }
    try {
        Library library(argv[1]);
        int code = -1;
        require(library.factory("unsupported_provider", &code) == nullptr &&
                code == vr::VRInitError_Init_InterfaceNotFound, "unsupported factory interface");
        require(library.factory(nullptr, nullptr) == nullptr, "null factory interface");
        auto* provider = static_cast<vr::IServerTrackedDeviceProvider*>(
            library.factory(vr::IServerTrackedDeviceProvider_Version, &code));
        require(provider && code == vr::VRInitError_None, "provider factory failed");
        require(library.factory(vr::IServerTrackedDeviceProvider_Version, nullptr) == provider, "provider singleton");
        require(provider->GetInterfaceVersions() != nullptr, "missing SDK interface versions");
        require(provider->Init(nullptr) == vr::VRInitError_Init_InterfaceNotFound, "null context Init");
        provider->Cleanup();

        unsigned cases = 0;
        const auto run = [&](const char* name, Context& c, vr::EVRInitError expected,
                             unsigned additions, const char* text, bool inspect = false) {
            exercise(*provider,c,expected,additions,text,inspect);
            ++cases;
            std::cout << "PASS " << name << '\n';
        };
        {
            Context c; c.settings.values["enable"] = false;
            run("enable=false",c,vr::VRInitError_None,0,"disabled by default");
            require(c.settings.reads == std::vector<std::string>{"enable"}, "disabled driver reads extra settings");
        }
        for (const char* mode : {"full", "yaw"}) {
            Context c; c.settings.values["recenterMode"] = std::string(mode);
            run(mode,c,vr::VRInitError_None,1,"HMD registered",true);
            require(c.settings.reads == std::vector<std::string>{"enable","recenterMode","imuCalibrationMs",
                "imuStaleTimeoutMs","imuMaxSampleGapUs","displayEnabled"},
                    "tracking-only Init queried display geometry/projection");
            require(!c.log.contains("sensor opened") && !c.log.contains("activated"), "hardware reader unexpectedly started");
        }
        for (const char* key : {"enable", "recenterMode", "displayEnabled"}) {
            for (bool wrong_type : {false, true}) {
                Context c;
                if (wrong_type) c.settings.values[key] = int32_t{42};
                else c.settings.values.erase(key);
                run((std::string(wrong_type ? "wrong-type " : "missing ")+key).c_str(), c,
                    !std::strcmp(key,"enable") ? vr::VRInitError_None : vr::VRInitError_Driver_Failed,
                    0, !std::strcmp(key,"enable") ? "disabled by default" :
                       (!std::strcmp(key,"recenterMode") ? "recenterMode must be full or yaw" : "cannot read displayEnabled"));
            }
        }
        {
            Context c; c.settings.errors["enable"] = vr::VRSettingsError_ReadFailed;
            run("enable read failure (true value ignored)",c,vr::VRInitError_None,0,"disabled by default");
        }
        for (const char* mode : {"", "invalid", "FULL"}) {
            Context c; c.settings.values["recenterMode"] = std::string(mode);
            run("invalid recenterMode",c,vr::VRInitError_Driver_Failed,0,"recenterMode must be full or yaw");
        }
        {
            Context c; c.host.accept = false;
            run("host rejects registration",c,vr::VRInitError_Driver_Failed,1,"TrackedDeviceAdded rejected HMD");
        }
        for (const auto* version : required_interfaces) {
            // SDK 2.15.6 InitServer tests the always-nonnull property HELPER,
            // not VRPropertiesRaw(). Missing properties therefore is not an
            // interface-init failure in this SDK; normal contexts supply it.
            if (!std::strcmp(version,vr::IVRProperties_Version)) continue;
            Context c; c.missing_interface = version;
            Session session{*provider,c};
            require(provider->Init(&c) == vr::VRInitError_Init_InterfaceNotFound, "missing mandatory interface accepted");
            require(c.host.additions == 0 && c.settings.reads.empty() && c.log.messages.empty(),
                    "failed context initialization continued into driver");
            require(std::count(c.lookups.begin(),c.lookups.end(),version) == 1, "missing interface not requested");
            session.cleanup(); provider->RunFrame();
            ++cases;
            std::cout << "PASS missing interface " << version << '\n';
        }
        // Fresh contexts across repeated success/rejection/disabled cycles catch
        // stale SDK caches and residual HMD state; handle counts detect leaked
        // OS handles. This is NOT a claim of heap-allocation leak instrumentation.
        const DWORD before = handles();
        for (unsigned i = 0; i < 64; ++i) {
            {
                Context c; c.host.accept = false;
                exercise(*provider,c,vr::VRInitError_Driver_Failed,1,"TrackedDeviceAdded rejected HMD");
            }
            {
                Context c; c.settings.values["recenterMode"] = std::string(i%2 ? "yaw" : "full");
                exercise(*provider,c,vr::VRInitError_None,1,"HMD registered",true);
            }
            {
                Context c; c.settings.values["enable"] = false;
                exercise(*provider,c,vr::VRInitError_None,0,"disabled by default");
                require(c.settings.reads == std::vector<std::string>{"enable"}, "stale enabled state after cleanup");
            }
        }
        const DWORD after = handles();
        require(after == before, "process handle count changed: "+std::to_string(before)+" -> "+std::to_string(after));
        std::cout << "PASS 64 rejection/success/disabled lifecycle cycles; handles " << before << " -> " << after << '\n'
                  << "PASS " << cases << " named cases + 192 lifecycle sessions; no Activate, no valid enabled display config\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
