// SPDX-License-Identifier: MIT
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <tlhelp32.h>
#include <openvr.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <unordered_set>

namespace {
using Json=nlohmann::json;
using Clock=std::chrono::steady_clock;
std::int64_t utc_us() { return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count(); }
struct Options {
    std::filesystem::path dll,output,ready,stop;
    unsigned duration_ms=210000,sample_ms=10;
    bool presence=false;
    std::string dashboard="keep";
};
unsigned number(const std::wstring& s,unsigned lower,unsigned upper) {
    if(s.empty()) throw std::runtime_error("empty number");
    unsigned value=0;
    for(auto c:s) { if(c<L'0'||c>L'9'||value>upper/10) throw std::runtime_error("invalid number"); value=value*10+unsigned(c-L'0'); }
    if(value<lower||value>upper) throw std::runtime_error("number outside limits");
    return value;
}
Options parse(int argc,wchar_t** argv) {
    Options o; std::unordered_set<std::wstring> seen;
    for(int i=1;i<argc;++i) {
        const std::wstring flag=argv[i];
        if(!seen.insert(flag).second) throw std::runtime_error("duplicate option");
        if(flag==L"--synthetic-presence") {o.presence=true;continue;}
        if(i+1>=argc) throw std::runtime_error("missing argument");
        const std::wstring value=argv[++i];
        if(flag==L"--openvr-dll") o.dll=value;
        else if(flag==L"--output") o.output=value;
        else if(flag==L"--ready-file") o.ready=value;
        else if(flag==L"--stop-file") o.stop=value;
        else if(flag==L"--duration-ms") o.duration_ms=number(value,100,720000);
        else if(flag==L"--sample-ms") o.sample_ms=number(value,2,50);
        else if(flag==L"--dashboard") {
            if(value==L"keep") o.dashboard="keep";
            else if(value==L"shown") o.dashboard="shown";
            else if(value==L"hidden") o.dashboard="hidden";
            else throw std::runtime_error("invalid dashboard state");
        } else throw std::runtime_error("unknown option");
    }
    if(!o.dll.is_absolute()||!o.output.is_absolute()) throw std::runtime_error("absolute DLL/output paths required");
    for(const auto& p:{o.output,o.ready,o.stop}) if(!p.empty() && (!p.is_absolute()||!std::filesystem::is_directory(p.parent_path())||std::filesystem::exists(p))) throw std::runtime_error("new absolute files in existing directories required");
    return o;
}
template<class T>T symbol(HMODULE dll,const char* name) {
    T result=nullptr; const auto address=GetProcAddress(dll,name); static_assert(sizeof(result)==sizeof(address)); std::memcpy(&result,&address,sizeof(result));
    if(!result) throw std::runtime_error("missing OpenVR export"); return result;
}
bool server_running() {
    DWORD session=0; if(!ProcessIdToSessionId(GetCurrentProcessId(),&session)) return false;
    const auto handle=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);
    if(handle==INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W entry{}; entry.dwSize=sizeof(entry); bool found=false;
    if(Process32FirstW(handle,&entry)) do { DWORD other=0;
        if(!_wcsicmp(entry.szExeFile,L"vrserver.exe")&&ProcessIdToSessionId(entry.th32ProcessID,&other)&&other==session) {found=true;break;}
    } while(Process32NextW(handle,&entry));
    CloseHandle(handle); return found;
}
struct Runtime {
    HMODULE dll=nullptr;
    using Shutdown=void(VR_CALLTYPE*)(); Shutdown shutdown=nullptr;
    bool initialized=false;
    ~Runtime(){if(initialized&&shutdown) shutdown();if(dll)FreeLibrary(dll);}
};
class Writer {
    HANDLE handle_=INVALID_HANDLE_VALUE; std::string buffer_;
public:
    explicit Writer(const std::filesystem::path& path) {
        handle_=CreateFileW(path.c_str(),GENERIC_WRITE,FILE_SHARE_READ,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(handle_==INVALID_HANDLE_VALUE) throw std::runtime_error("cannot create exclusive telemetry output");
        buffer_.reserve(300000);
    }
    void flush(){
        if(buffer_.empty()) return;
        DWORD wrote=0; if(!WriteFile(handle_,buffer_.data(),static_cast<DWORD>(buffer_.size()),&wrote,nullptr)||wrote!=buffer_.size()) throw std::runtime_error("telemetry write failed");
        buffer_.clear();
    }
    void emit(Json value){buffer_+=value.dump(-1,' ',false,Json::error_handler_t::replace);buffer_+='\n';if(buffer_.size()>250000)flush();}
    ~Writer(){try{flush();}catch(...){} if(handle_!=INVALID_HANDLE_VALUE)CloseHandle(handle_);}
};
vr::TrackedDeviceIndex_t target(vr::IVRSystem& system) {
    for(vr::TrackedDeviceIndex_t i=0;i<vr::k_unMaxTrackedDeviceCount;++i) {
        if(system.GetTrackedDeviceClass(i)!=vr::TrackedDeviceClass_HMD)continue;
        std::array<char,128> serial{};vr::ETrackedPropertyError error;
        system.GetStringTrackedDeviceProperty(i,vr::Prop_SerialNumber_String,serial.data(),static_cast<uint32_t>(serial.size()),&error);
        if(error==vr::TrackedProp_Success&&!std::strcmp(serial.data(),"PSVRPC-HMD-0"))return i;
    }return vr::k_unTrackedDeviceIndexInvalid;
}
Json pose(const vr::TrackedDevicePose_t& p){return {{"valid",p.bPoseIsValid},{"connected",p.bDeviceIsConnected},{"tracking_result",static_cast<int>(p.eTrackingResult)}};}
Json color(const vr::HmdColor_t& c){return {c.r,c.g,c.b,c.a};}
int collect(const Options& o) {
    Runtime r;r.dll=LoadLibraryExW(o.dll.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if(!r.dll)throw std::runtime_error("LoadLibraryExW failed");
    using Init=uint32_t(VR_CALLTYPE*)(vr::EVRInitError*,vr::EVRApplicationType,const char*);
    using Get=void*(VR_CALLTYPE*)(const char*,vr::EVRInitError*);
    const auto init=symbol<Init>(r.dll,"VR_InitInternal2");const auto get=symbol<Get>(r.dll,"VR_GetGenericInterface");
    r.shutdown=symbol<Runtime::Shutdown>(r.dll,"VR_ShutdownInternal");
    if(!server_running())throw std::runtime_error("existing vrserver in this session required");
    vr::EVRInitError error=vr::VRInitError_None;init(&error,vr::VRApplication_Background,nullptr);
    if(error!=vr::VRInitError_None)throw std::runtime_error("Background initialization failed");r.initialized=true;
    auto required=[&](const char* name){auto* p=get(name,&error);if(!p||error!=vr::VRInitError_None)throw std::runtime_error(name);return p;};
    auto& system=*static_cast<vr::IVRSystem*>(required(vr::IVRSystem_Version));
    auto& compositor=*static_cast<vr::IVRCompositor*>(required(vr::IVRCompositor_Version));
    auto& overlay=*static_cast<vr::IVROverlay*>(required(vr::IVROverlay_Version));
    auto& debug=*static_cast<vr::IVRDebug*>(required(vr::IVRDebug_Version));
    auto index=target(system);if(index==vr::k_unTrackedDeviceIndexInvalid)throw std::runtime_error("exact PSVR HMD missing");
    Writer out(o.output);const auto start=Clock::now();
    auto stamp=[&](Json j){j["utc_unix_us"]=utc_us();j["steady_elapsed_us"]=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-start).count();out.emit(std::move(j));};
    auto request=[&](const char* command,const char* expected){
        if(target(system)!=index)throw std::runtime_error("HMD changed before request");
        std::array<char,1024> response{};const auto length=debug.DriverDebugRequest(index,command,response.data(),static_cast<uint32_t>(response.size()));
        const bool queued=length==std::strlen(expected)+1 && !std::strcmp(response.data(),expected);
        stamp({{"event","diagnostic_request"},{"command",command},{"response",response.data()},{"queued",queued}});
        if(!queued)throw std::runtime_error("diagnostic request not acknowledged");
    };
    bool presence_sent=false;
    auto release=[&](){if(presence_sent){try{request("simulate-unworn","simulated unworn queued");}catch(...){}presence_sent=false;}};
    try {
        stamp({{"event","telemetry_start"},{"sample_ms",o.sample_ms},{"synthetic_presence",o.presence},{"dashboard_requested",o.dashboard},
            {"pid",GetCurrentProcessId()},{"application_type","Background"},{"single_client",true},
            {"frame_pose_semantics","m_HmdPose is app render pose, not proof of current driver pose; unavailable/stale app timing is recorded as such"}});
        if(o.presence){request("simulate-worn","simulated worn queued; expires after 180 seconds");presence_sent=true;}
        if(o.dashboard=="shown"&&!overlay.IsDashboardVisible())overlay.ShowDashboard("");
        // Hidden trial is selected before SteamVR startup by the orchestration script.
        // Do not inject system-button events or claim a hidden dashboard without readback.
        if(o.dashboard!="keep") {
            const auto until=Clock::now()+std::chrono::seconds(5);
            while(overlay.IsDashboardVisible()!=(o.dashboard=="shown")&&Clock::now()<until)std::this_thread::sleep_for(std::chrono::milliseconds(20));
            if(overlay.IsDashboardVisible()!=(o.dashboard=="shown"))throw std::runtime_error("requested dashboard state not reached");
        }
        out.flush();
        if(!o.ready.empty()){std::ofstream ready(o.ready);ready<<Json{{"ready",true},{"utc_unix_us",utc_us()},{"dashboard_visible",overlay.IsDashboardVisible()}}.dump();ready.close();if(!ready)throw std::runtime_error("ready signal failed");}
        const auto deadline=start+std::chrono::milliseconds(o.duration_ms);auto next=Clock::now(),renew=start+std::chrono::seconds(120),flush=Clock::now(),identify=Clock::now();
        auto previous_poll=Clock::now();std::unordered_set<uint32_t> frames;
        std::uint64_t samples=0,invalid=0,timings=0,empty_timings=0;double max_gap_ms=0;
        while(Clock::now()<deadline && (o.stop.empty()||!std::filesystem::exists(o.stop))) {
            const auto now=Clock::now();const double gap=std::chrono::duration<double,std::milli>(now-previous_poll).count();previous_poll=now;max_gap_ms=std::max(max_gap_ms,gap);
            if(now>=identify){if(target(system)!=index)throw std::runtime_error("HMD identity changed");identify=now+std::chrono::seconds(1);}
            if(o.presence&&now>=renew){request("simulate-worn","simulated worn queued; expires after 180 seconds");renew=now+std::chrono::seconds(120);}
            std::array<vr::TrackedDevicePose_t,vr::k_unMaxTrackedDeviceCount> raw{},render{};
            system.GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseRawAndUncalibrated,0,raw.data(),static_cast<uint32_t>(raw.size()));
            const auto last_error=compositor.GetLastPoses(render.data(),static_cast<uint32_t>(render.size()),nullptr,0);
            if(!raw[index].bPoseIsValid)++invalid;++samples;
            DWORD foreground=0;GetWindowThreadProcessId(GetForegroundWindow(),&foreground);
            stamp({{"event","sample"},{"sample",samples},{"poll_gap_ms",gap},{"raw_pose",pose(raw[index])},
                {"compositor_pose",last_error==vr::VRCompositorError_None ? pose(render[index]) : Json(nullptr)},
                {"get_last_poses_error",static_cast<int>(last_error)},{"fade_rgba",color(compositor.GetCurrentFadeColor(false))},
                {"background_fade_rgba",color(compositor.GetCurrentFadeColor(true))},{"grid_alpha",compositor.GetCurrentGridAlpha()},
                {"fullscreen",compositor.IsFullscreen()},{"can_render_scene",compositor.CanRenderScene()},
                {"dashboard_visible",overlay.IsDashboardVisible()},{"activity_level",static_cast<int>(system.GetTrackedDeviceActivityLevel(index))},
                {"scene_focus_pid",compositor.GetCurrentSceneFocusProcess()},{"foreground_pid",foreground}});
            std::array<vr::Compositor_FrameTiming,64> history{};for(auto& f:history)f.m_nSize=sizeof(f);
            const auto count=compositor.GetFrameTimings(history.data(),static_cast<uint32_t>(history.size()));
            if(!count)++empty_timings;
            for(uint32_t i=0;i<std::min(count,static_cast<uint32_t>(history.size()));++i) {
                const auto& f=history[i];if(!frames.insert(f.m_nFrameIndex).second)continue;++timings;
                stamp({{"event","frame_timing"},{"frame_index",f.m_nFrameIndex},{"system_time_s",f.m_flSystemTimeInSeconds},
                    {"presents",f.m_nNumFramePresents},{"mispresents",f.m_nNumMisPresented},{"dropped",f.m_nNumDroppedFrames},
                    {"reprojection_flags",f.m_nReprojectionFlags},{"total_gpu_ms",f.m_flTotalRenderGpuMs},
                    {"compositor_gpu_ms",f.m_flCompositorRenderGpuMs},{"compositor_cpu_ms",f.m_flCompositorRenderCpuMs},
                    {"present_cpu_ms",f.m_flPresentCallCpuMs},{"wait_present_cpu_ms",f.m_flWaitForPresentCpuMs},{"hmd_app_pose",pose(f.m_HmdPose)}});
            }
            vr::VREvent_t event{};unsigned polled=0;
            while(polled++<64&&system.PollNextEvent(&event,sizeof(event)))stamp({{"event","vr_event"},{"type",event.eventType},
                {"name",system.GetEventTypeNameFromEnum(static_cast<vr::EVREventType>(event.eventType))},
                {"device_index",event.trackedDeviceIndex},{"age_seconds",event.eventAgeSeconds}});
            if(now>=flush){out.flush();flush=now+std::chrono::seconds(1);}
            next+=std::chrono::milliseconds(o.sample_ms);if(next<Clock::now())next=Clock::now();std::this_thread::sleep_until(next);
        }
        release();stamp({{"event","telemetry_summary"},{"samples",samples},{"raw_invalid",invalid},{"unique_frame_timings",timings},
            {"empty_timing_polls",empty_timings},{"max_poll_gap_ms",max_gap_ms},{"elapsed_ms",std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-start).count()}});out.flush();
    } catch(...){release();throw;}
    return 0;
}
}
int wmain(int argc,wchar_t** argv) {
    if(argc==2&&!std::wcscmp(argv[1],L"--help")){std::cout<<"PSVR continuous telemetry: existing runtime, Background single client, MSVC ABI.\n--openvr-dll <absolute> --output <new absolute JSONL> [--duration-ms 100..720000] [--sample-ms 2..50]\n[--ready-file <new absolute>] [--stop-file <new absolute>] [--synthetic-presence] [--dashboard keep|shown|hidden]\nDefault read-only; explicit presence/dashboard options issue diagnostic requests.\n";return 0;}
    Options options;
    try{options=parse(argc,argv);}catch(const std::exception& e){std::cerr<<"CLI: "<<e.what()<<'\n';return 2;}
    try{return collect(options);}catch(const std::exception& e){std::cerr<<"Telemetry: "<<e.what()<<'\n';return 3;}
}
