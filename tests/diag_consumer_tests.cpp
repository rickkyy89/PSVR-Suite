// SPDX-License-Identifier: MIT
// Exercise the real CLI/orchestrator with fake transports. No device APIs.
#include <windows.h>
#include <hidsdi.h>
#include <winusb.h>
#include <deque>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace offline {
int ownership = 0, releases = 0, raw_opens = 0;
HANDLE mutex(LPSECURITY_ATTRIBUTES, BOOL, LPCWSTR) { ++ownership; return reinterpret_cast<HANDLE>(1); }
DWORD wait(HANDLE, DWORD) { return WAIT_OBJECT_0; }
BOOL release(HANDLE) { ++releases; return TRUE; }
BOOL close(HANDLE) { return TRUE; }
HANDLE open(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE) {
    ++raw_opens; return INVALID_HANDLE_VALUE;
}
BOOLEAN preparsed(HANDLE, PHIDP_PREPARSED_DATA*) { return FALSE; }
BOOLEAN free_preparsed(PHIDP_PREPARSED_DATA) { return TRUE; }
NTSTATUS caps(PHIDP_PREPARSED_DATA, PHIDP_CAPS) { return 0; }
}
// Headers are already parsed: redirect call sites, never SDK declarations.
#define CreateMutexW offline::mutex
#define WaitForSingleObject offline::wait
#define ReleaseMutex offline::release
#define CloseHandle offline::close
#define CreateFileW offline::open
#define HidD_GetPreparsedData offline::preparsed
#define HidD_FreePreparsedData offline::free_preparsed
#define HidP_GetCaps offline::caps
#define main diag_entry
#include "../tools/psvr_diag/main.cpp"
#undef main

namespace offline {
std::vector<psvr::PnpDevice> devices;
hid_device_info sensor{};
hid_device_info* hids = nullptr;
std::deque<std::vector<unsigned char>> frames;
int sends = 0, opens = 0, closes = 0, hid_opens = 0, unexpected = 0, cases = 0;
int enumerations = 0;
bool fail_read = false, fail_send = false, fail_open = false, fail_after_send = false;
void check(bool ok, const char* text) { if (!ok) throw std::runtime_error(text); }
psvr::PnpDevice node(const char* suffix) {
    psvr::PnpDevice device;
    device.instance_id = std::string("USB\\VID_054C&PID_09AF&MI_05\\") + suffix;
    device.name = "mock control"; device.service = "WinUSB"; device.is_psvr = true;
    return device;
}
void reset() {
    devices = {node("A")}; hids = nullptr; frames.clear();
    sends = opens = closes = hid_opens = unexpected = raw_opens = ownership = releases = 0;
    enumerations = 0; fail_read = fail_send = fail_open = fail_after_send = false; interrupted.store(false);
    sensor = {}; sensor.vendor_id = psvr::vendor_id; sensor.product_id = psvr::product_id;
    sensor.interface_number = 4; sensor.path = const_cast<char*>("offline-sensor");
}
std::vector<unsigned char> ack(unsigned char command, unsigned char result = 0) {
    return {0xa0, 0, 0xaa, 4, command, result, 0, 0};
}
std::vector<unsigned char> vr() {
    std::vector<unsigned char> frame(20, 0);
    frame[0] = 0xf0; frame[2] = 0xaa; frame[3] = 16; frame[4] = 1; frame[11] = 1;
    return frame;
}
}
namespace psvr {
std::vector<PnpDevice> enumerate_usb() { ++offline::enumerations; return offline::devices; }
std::string utf8(const wchar_t* value) {
    if (!value) return "<not provided>";
    std::string result;
    while (*value) result += static_cast<char>(*value++);
    return result;
}
WinUsbControl::WinUsbControl(const std::string&) {
    if (offline::fail_open) throw std::runtime_error("mock open failure");
    ++offline::opens;
}
void WinUsbControl::close() noexcept { ++offline::closes; }
std::vector<unsigned char> WinUsbControl::read(int) {
    if (offline::fail_read || (offline::fail_after_send && offline::sends))
        throw std::runtime_error("mock read failure");
    if (offline::frames.empty()) { Sleep(1); return {}; }
    auto frame = offline::frames.front(); offline::frames.pop_front(); return frame;
}
unsigned long WinUsbControl::send(Action action, int) {
    ++offline::sends;
    if (offline::fail_send) throw std::runtime_error("mock send failure");
    return static_cast<unsigned long>(command_packet(action).size());
}
int capture_imu(hid_device*, WinUsbControl*, const Options&, const ImuLog&,
                std::atomic<bool>&, const std::filesystem::path&) {
    ++offline::unexpected; throw std::runtime_error("unexpected capture");
}
void request_recenter(int) { ++offline::unexpected; }
void request_recalibrate(int) { ++offline::unexpected; }
void request_stop_imu(int) { ++offline::unexpected; }
}
extern "C" {
int hid_init() { return 0; }
int hid_exit() { return 0; }
hid_device_info* hid_enumerate(unsigned short, unsigned short) { return offline::hids; }
void hid_free_enumeration(hid_device_info*) {}
const wchar_t* hid_error(hid_device*) { return L"offline"; }
const wchar_t* hid_read_error(hid_device*) { return L"offline"; }
const char* hid_version_str() { return "offline"; }
hid_device* hid_open_path(const char*) { ++offline::hid_opens; return nullptr; }
void hid_close(hid_device*) { ++offline::unexpected; }
int hid_read_timeout(hid_device*, unsigned char*, size_t, int) { ++offline::unexpected; return -1; }
}

int main(int argc, char** argv) {
    std::ostringstream output;
    auto* previous = std::cout.rdbuf(output.rdbuf());
    try {
        offline::check(argc == 2, "test log directory required");
        Logger log(argv[1]);
        psvr::Options options; options.control_timeout_ms = 100; options.raw_packets = 0;
        // B2-01: both PnP entries and both enumeration summaries must survive.
        offline::reset(); options.enumerate_only = true;
        offline::devices.push_back(offline::node("B"));
        offline::check(diagnose(options, log) == 0, "multiple controls break read-only inventory");
        offline::check(output.str().find("MI_05\\B") != std::string::npos &&
            output.str().find("USB enumeration present_nodes=2 psvr_nodes=2") != std::string::npos &&
            output.str().find("HID enumeration total=0") != std::string::npos,
            "read-only inventory was truncated");
        offline::check(offline::opens == 0 && offline::ownership == 0 && offline::raw_opens == 0,
                       "enumeration acquired operational ownership/opened a device");
        ++offline::cases;
        options.enumerate_only = false;
        for (const auto action : {psvr::Action::none, psvr::Action::control_info, psvr::Action::vr_mode}) {
            options.action = action;
            offline::check(diagnose(options, log) == 3, "ambiguous operational selection must return 3");
            offline::check(offline::opens == 0 && offline::sends == 0 && offline::ownership == 0,
                           "ambiguous operational selection reached ownership or transport");
            ++offline::cases;
        }
        // M2 is independent of the sensor; capture still requires MI_04.
        for (const auto action : {psvr::Action::control_info, psvr::Action::status,
                psvr::Action::power_on, psvr::Action::power_off, psvr::Action::vr_mode,
                psvr::Action::cinema_mode, psvr::Action::leds_on}) {
            offline::reset(); options.action = action;
            auto status = offline::vr();
            if (action == psvr::Action::power_off) { status[4] = 0; status[11] = 0; }
            if (action == psvr::Action::cinema_mode) { status[4] = 5; status[11] = 0; }
            const bool writing = psvr::writes_command(action);
            if (writing) offline::frames = {{}, offline::ack(psvr::command_packet(action)[0]), status};
            else offline::frames = {status};
            offline::check(diagnose(options, log) == 0, "M2 unnecessarily requires MI_04 sensor");
            offline::check(offline::opens == 1 && offline::closes == 1 &&
                offline::ownership == 1 && offline::releases == 1 &&
                offline::sends == (writing ? 1 : 0) && offline::raw_opens == 0 && offline::hid_opens == 0,
                "M2 violated ownership, sensor independence or write count");
            ++offline::cases;
        }
        for (const bool imu : {false, true}) {
            offline::reset(); options.action = psvr::Action::none; options.imu = imu;
            offline::check(diagnose(options, log) == 3 && offline::ownership == 0 && offline::opens == 0,
                           "sensor-less capture must fail before opening control");
            ++offline::cases;
        }
        options.imu = false; options.action = psvr::Action::control_info;
        // Ambiguity and unhealthy control must remain failures even for M2.
        offline::reset();
        auto second_sensor = offline::sensor; second_sensor.next = nullptr;
        offline::sensor.next = &second_sensor; offline::hids = &offline::sensor;
        offline::check(diagnose(options, log) == 3 && offline::ownership == 0,
                       "M2 must still reject multiple sensors/processor units");
        ++offline::cases;
        for (int failure = 0; failure < 4; ++failure) {
            offline::reset();
            if (failure == 0) offline::devices.front().problem = 28;
            if (failure == 1) offline::devices.front().status_result = 1;
            if (failure == 2) offline::devices.front().service = "HidUsb";
            if (failure == 3) offline::fail_open = true;
            offline::check(diagnose(options, log) == (failure == 2 ? 5 : 3), "bad MI_05 accepted");
            offline::check(offline::opens == 0 && offline::sends == 0 && offline::raw_opens == 0 &&
                offline::ownership == offline::releases, "bad MI_05 leaked ownership or sent a command");
            ++offline::cases;
        }
        // Real control_action implementation, deterministic fake input streams.
        auto run = [&](psvr::Action action, std::deque<std::vector<unsigned char>> frames,
                       int result, int sends) {
            offline::reset(); options.action = action; offline::frames = std::move(frames);
            { psvr::WinUsbControl control("mock");
              offline::check(control_action(control, options, log) == result, "wrong state-machine result"); }
            offline::check(offline::sends == sends && offline::opens == offline::closes,
                           "wrong write count or unclosed fake transport");
            ++offline::cases;
        };
        run(psvr::Action::vr_mode, {{}, offline::ack(0x23), offline::vr()}, 0, 1);
        run(psvr::Action::vr_mode, {{}, offline::vr(), offline::ack(0x23)}, 0, 1);
        run(psvr::Action::vr_mode, {{}, offline::ack(0x23)}, 6, 1);
        run(psvr::Action::vr_mode, {{}, offline::vr()}, 6, 1);
        run(psvr::Action::vr_mode, {{}, offline::ack(0x23, 1)}, 3, 1);
        run(psvr::Action::vr_mode, {{}, offline::ack(0x17), offline::vr()}, 6, 1);
        run(psvr::Action::status, {offline::vr()}, 0, 0);
        run(psvr::Action::leds_on, {{}, offline::ack(0x15)}, 0, 1);
        run(psvr::Action::vr_mode, std::deque<std::vector<unsigned char>>(32, offline::vr()), 6, 0);
        // Stale pre-write success must not verify the subsequent command.
        run(psvr::Action::vr_mode, {offline::ack(0x23), offline::vr(), {}}, 6, 1);
        auto invalid_status = offline::vr(); invalid_status[2] = 0;
        run(psvr::Action::vr_mode, {{}, offline::ack(0x23), invalid_status}, 6, 1);
        auto wrong_status = offline::vr(); wrong_status[4] = 5; wrong_status[11] = 0;
        run(psvr::Action::vr_mode, {{}, offline::ack(0x23), wrong_status}, 6, 1);
        run(psvr::Action::status, {invalid_status}, 6, 0);
        for (int failure = 0; failure < 3; ++failure) {
            offline::reset(); options.action = psvr::Action::vr_mode;
            offline::fail_read = failure == 0;
            offline::fail_send = failure == 1;
            offline::fail_after_send = failure == 2;
            offline::check(diagnose(options, log) == 3, "I/O error not returned as exit 3");
            offline::check(offline::opens == 1 && offline::closes == 1 && offline::releases == 1 &&
                offline::sends == (failure == 0 ? 0 : 1), "I/O failure leaked handles or retried a write");
            ++offline::cases;
        }
        offline::reset(); options.action = psvr::Action::vr_mode; interrupted.store(true);
        offline::check(diagnose(options, log) == 130 && offline::sends == 0 &&
            offline::opens == offline::closes && offline::ownership == offline::releases,
            "interruption sent a command or leaked ownership");
        ++offline::cases;
        offline::reset(); offline::devices.clear(); options.action = psvr::Action::none;
        options.enumerate_only = true;
        offline::check(diagnose(options, log) == 2 && offline::ownership == 0, "absent inventory must return 2");
        ++offline::cases;
        // The real CLI must reject conflicts/dry-run before any discovery/open.
        auto cli = [&](std::vector<std::string> flags, int expected) {
            offline::reset();
            std::vector<std::string> words{"psvr_diag", "--log-dir", std::string(argv[1]) + "/cli"};
            words.insert(words.end(), flags.begin(), flags.end());
            std::vector<char*> pointers;
            for (auto& word : words) pointers.push_back(word.data());
            std::ostringstream errors;
            auto* error_output = std::cerr.rdbuf(errors.rdbuf());
            const auto result = diag_entry(static_cast<int>(pointers.size()), pointers.data());
            std::cerr.rdbuf(error_output);
            offline::check(result == expected && offline::enumerations == 0 && offline::ownership == 0 &&
                offline::opens == 0 && offline::sends == 0 && offline::raw_opens == 0, "CLI guard accessed hardware");
            ++offline::cases;
        };
        cli({"--vr-mode", "--dry-run"}, 0);
        cli({"--vr-mode", "--cinema-mode"}, 1);
        cli({"--enumerate-only", "--vr-mode"}, 1);
        cli({"--status", "--dry-run"}, 1);
        cli({"--not-a-real-flag"}, 1);
        offline::check(offline::unexpected == 0 && offline::raw_opens == 0, "unexpected hardware path");
        std::cout.rdbuf(previous);
        std::cout << "PASS diag consumer: " << offline::cases << " cases; hardware access intercepted\n";
        return 0;
    } catch (const std::exception& error) {
        std::cout.rdbuf(previous);
        std::cerr << "FAIL diag consumer: " << error.what() << '\n';
        return 1;
    }
}
