// SPDX-License-Identifier: MIT
// Real capture loop, synthetic HID and clock; never opens a device.
#include "imu_capture.hpp"
#include "pnp_windows.hpp"
#include <array>
#include <cstring>
#include <iostream>
#include <stdexcept>

namespace offline {
using Clock = std::chrono::steady_clock;
Clock::time_point clock_time{};
unsigned reads = 0;
int gap_ms = 1, inject_at = 650;
enum class Feed { normal, silent, malformed, duplicate, control_delay,
                  single_duplicate, single_malformed, brief_silence, jitter };
Feed feed = Feed::normal;
bool injected = false, request_recenter = false;
std::array<unsigned char,64> last{};
void check(bool ok,const char* message) { if (!ok) throw std::runtime_error(message); }
Clock::time_point now() { return clock_time; }
std::array<unsigned char,64> report(unsigned index) {
    std::array<unsigned char,64> b{};
    b[63] = static_cast<unsigned char>(index);
    for (unsigned i=0; i<2; ++i) {
        const auto tick = (index*1000+i*500)&0xffffffu;
        const auto offset = 16+16*i;
        for (unsigned j=0; j<4; ++j) b[offset+j] = static_cast<unsigned char>(tick>>(8*j));
        b[offset+11] = 64; // mapped +Y gravity = 16384
    }
    return b;
}
}

extern "C" int HID_API_CALL hid_read_timeout(hid_device*,unsigned char* bytes,size_t size,int) {
    using namespace offline;
    check(reads<5000 && size>=64,"unbounded capture or unexpected buffer");
    clock_time += std::chrono::milliseconds(1);
    if (feed==Feed::jitter) clock_time += std::chrono::milliseconds(reads%17);
    if (feed!=Feed::control_delay && static_cast<int>(reads)==inject_at) {
        clock_time += std::chrono::milliseconds(gap_ms-1); injected = true;
    }
    const bool repeat = (feed==Feed::duplicate && reads>=static_cast<unsigned>(inject_at)) ||
        (feed==Feed::single_duplicate && reads==static_cast<unsigned>(inject_at));
    const auto b = repeat ? last : report(reads);
    std::memcpy(bytes,b.data(),b.size()); last = b;
    ++reads;
    if (request_recenter && reads==650) psvr::request_recenter(static_cast<int>(GetCurrentProcessId()));
    if (reads==800) psvr::request_stop_imu(static_cast<int>(GetCurrentProcessId()));
    if (feed==Feed::silent) return 0;
    if (feed==Feed::malformed && reads>static_cast<unsigned>(inject_at)) return 12;
    if (feed==Feed::single_malformed && reads==static_cast<unsigned>(inject_at+1)) return 12;
    if (feed==Feed::brief_silence && (reads==static_cast<unsigned>(inject_at+1) ||
                                     reads==static_cast<unsigned>(inject_at+2))) return 0;
    return 64;
}
extern "C" const wchar_t* HID_API_CALL hid_read_error(hid_device*) { return L"offline error"; }
namespace psvr {
std::string utf8(const wchar_t*) { return "offline error"; }
WinUsbControl::WinUsbControl(const std::string&) {}
void WinUsbControl::close() noexcept {}
std::vector<unsigned char> WinUsbControl::read(int) {
    if (offline::feed==offline::Feed::control_delay && offline::reads>=650 && !offline::injected) {
        offline::clock_time += std::chrono::milliseconds(offline::gap_ms); offline::injected = true;
    }
    return {};
}
}

int main(int argc,char** argv) {
    try {
        using namespace offline;
        check(argc==2,"test log directory required");
        const auto root = std::filesystem::path(argv[1]) /
            ("run-"+std::to_string(GetCurrentProcessId())+"-"+std::to_string(GetTickCount64()));
        std::filesystem::create_directories(root);
        unsigned cases = 0;
        const auto run = [&](const char* name,Feed mode,int gap,int at,int stale,int expected,bool axis=false) {
            clock_time = {}; reads = 0; last = {}; injected = false;
            feed = mode; gap_ms = gap; inject_at = at; request_recenter = axis;
            psvr::Options options; options.imu = true; options.duration_ms = 0; options.json_stream = true;
            options.tracking.startup_discard_ms = 0; options.tracking.calibration_ms = 500;
            options.tracking.output_interval_ms = 20; options.tracking.stale_timeout_ms = stale;
            if (axis) options.axis_test = "yaw-right";
            bool saw_valid = false, valid_after_injection = false, lost = false, valid_after_loss = false;
            bool ignored_recenter = false, axis_baseline = false;
            unsigned terminals = 0;
            const psvr::ImuLog log = [&](const char* level,const std::string& text) {
                if (text=="recenter ignored during measurement profile") ignored_recenter = true;
                if (text.find("baseline is captured at calibration completion")!=std::string::npos) axis_baseline = true;
                if (std::strcmp(level,"DATA")!=0) return;
                const auto j = nlohmann::json::parse(text);
                const auto event = j.value("event","");
                const bool valid = j.value("valid",false);
                if (event=="imu" && valid) {
                    saw_valid = true;
                    if (injected) valid_after_injection = true;
                    if (lost) valid_after_loss = true;
                }
                if (event=="imu_state" && j.value("state","")=="tracking-lost") lost = true;
                if (event=="imu_state" && j.value("state","")=="stopped") ++terminals;
            };
            std::atomic<bool> interrupted{false};
            psvr::WinUsbControl control("offline");
            const auto code = psvr::capture_imu(nullptr,mode==Feed::control_delay ? &control : nullptr,
                options,log,interrupted,root/(std::string(name)+".csv"),now);
            if (code!=expected) throw std::runtime_error(std::string(name)+": exit="+std::to_string(code));
            check(terminals==1 && !valid_after_loss,"terminal invalidation or post-loss pose");
            if (at>=650 && mode!=Feed::silent) check(saw_valid,"tracking was never reached before injection");
            if (expected==0) check(valid_after_injection,"allowed delay falsely invalidated tracking");
            if (expected!=0 && mode!=Feed::duplicate && mode!=Feed::malformed)
                check(!valid_after_injection,"expired data refreshed a pose");
            if (axis) check(ignored_recenter && axis_baseline,"axis baseline/recenter contract");
            ++cases;
            std::cout<<"PASS "<<name<<'\n';
        };
        run("ordinary_jitter",Feed::normal,20,650,100,0);
        run("just_under_tracking_limit",Feed::normal,99,650,100,0);
        run("at_tracking_limit",Feed::normal,100,650,100,0);
        run("over_tracking_limit",Feed::normal,101,650,100,7);
        run("buffered_after_180ms",Feed::normal,180,650,100,7);
        run("configured_250ms_accepts_180ms",Feed::normal,180,650,250,0);
        run("configured_250ms_boundary",Feed::normal,250,650,250,0);
        run("configured_250ms_expiry",Feed::normal,251,650,250,7);
        run("pretracking_1000ms_boundary",Feed::normal,1000,100,100,0);
        run("pretracking_expired",Feed::normal,1001,100,100,7);
        run("first_report_3000ms_boundary",Feed::normal,3000,0,100,0);
        run("first_report_expired",Feed::normal,3001,0,100,4);
        // Silence must run beyond 3000ms, without reaching the normal test stop.
        // Inject the entire startup wait on its first read.
        run("silent_startup",Feed::silent,3001,0,100,4);
        run("duplicate_reports_do_not_refresh",Feed::duplicate,1,650,100,7);
        run("malformed_reports_do_not_refresh",Feed::malformed,1,650,100,7);
        run("one_duplicate_is_tolerated",Feed::single_duplicate,1,650,100,0);
        run("one_malformed_is_tolerated",Feed::single_malformed,1,650,100,0);
        run("two_empty_reads_are_tolerated",Feed::brief_silence,1,650,100,0);
        run("repeated_host_jitter_is_tolerated",Feed::jitter,1,650,100,0);
        run("slow_control_poll",Feed::control_delay,101,650,100,7);
        run("axis_baseline_and_recenter_guard",Feed::normal,1,650,100,0,true);
        std::cout<<"PASS capture scenarios="<<cases<<'\n';
        return 0;
    } catch (const std::exception& e) { std::cerr<<"FAIL "<<e.what()<<'\n'; return 1; }
}
