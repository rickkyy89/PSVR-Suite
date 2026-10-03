// SPDX-License-Identifier: MIT
#include "diag_core.hpp"
#include "sensor_packet.hpp"
#include "pnp_windows.hpp"
#include <windows.h>
#include <hidsdi.h>
#include <hidapi.h>
#include <array>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>

namespace {
using Clock = std::chrono::steady_clock;
std::atomic<bool> interrupted{false};
BOOL WINAPI console_control(DWORD event) {
    if (event != CTRL_C_EVENT && event != CTRL_BREAK_EVENT) return FALSE;
    interrupted.store(true);
    return TRUE;
}

std::string timestamp(bool filename = false) {
    SYSTEMTIME t{};
    GetSystemTime(&t);
    std::ostringstream s;
    s << std::setfill('0') << std::setw(4) << t.wYear
      << (filename ? "" : "-") << std::setw(2) << t.wMonth
      << (filename ? "" : "-") << std::setw(2) << t.wDay << 'T'
      << std::setw(2) << t.wHour << (filename ? "" : ":")
      << std::setw(2) << t.wMinute << (filename ? "" : ":")
      << std::setw(2) << t.wSecond << '.' << std::setw(3) << t.wMilliseconds << 'Z';
    return s.str();
}

class Logger {
    std::ofstream file_;
public:
    explicit Logger(const std::string& directory) {
        const auto dir = std::filesystem::u8path(directory);
        std::filesystem::create_directories(dir);
        const auto path = dir / ("psvr_diag_" + timestamp(true) + "_" + std::to_string(GetCurrentProcessId()) + ".log");
        file_.open(path, std::ios::out | std::ios::trunc);
        if (!file_) throw std::runtime_error("cannot create log: " + path.u8string());
        write("INFO", "log_file=" + std::filesystem::absolute(path).u8string());
    }
    void write(const char* level, const std::string& message) {
        const auto line = timestamp() + " [" + level + "] " + message;
        std::cout << line << '\n';
        file_ << line << '\n';
        file_.flush();
        if (!file_) throw std::runtime_error("log write failed");
    }
};

struct HidCloser { void operator()(hid_device* device) const { hid_close(device); } };
struct EnumerationCloser { void operator()(hid_device_info* list) const { hid_free_enumeration(list); } };
using HidHandle = std::unique_ptr<hid_device, HidCloser>;
struct HidSession {
    HidSession() {
        if (hid_init() != 0) throw std::runtime_error("hid_init failed: " + psvr::utf8(hid_error(nullptr)));
    }
    ~HidSession() { hid_exit(); }
};

void help() {
    std::cout << "Passive PSVR1 USB/HID diagnostic (Windows x64), milestone 1\n"
        "Usage: psvr_diag [--config file.json] [--duration-ms 100..60000]\n"
        "                 [--read-timeout-ms 1..1000] [--raw-packets 0..100]\n"
        "                 [--log-dir directory] [--enumerate-only] [--all-devices]\n"
        "No power, mode, feature/output report or firmware commands are sent.\n"
        "Config is explicit; CLI options override it regardless of argument order.\n"
        "Exit: 0=PASS, 1=usage/config/internal, 2=absent, 3=interface/I/O,\n"
        "      4=no valid sensor report, 130=interrupted.\n";
}

psvr::Options parse_options(int argc, char** argv) {
    psvr::Options options;
    std::string config;
    // First validate the whole command line and find explicit config; no hardware access.
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--enumerate-only" || arg == "--all-devices") continue;
        if (arg != "--config" && arg != "--duration-ms" && arg != "--read-timeout-ms" &&
            arg != "--raw-packets" && arg != "--log-dir")
            throw std::runtime_error("unknown option: " + arg);
        if (++i >= argc) throw std::runtime_error("missing value for " + arg);
        if (arg == "--config") {
            if (!config.empty()) throw std::runtime_error("--config may be specified only once");
            config = argv[i];
        }
    }
    if (!config.empty()) {
        std::ifstream input(std::filesystem::u8path(config));
        if (!input) throw std::runtime_error("cannot open config: " + config);
        nlohmann::json j;
        input >> j;
        options = psvr::config_options(j);
    }
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--enumerate-only") { options.enumerate_only = true; continue; }
        if (arg == "--all-devices") { options.all_devices = true; continue; }
        const std::string value = argv[++i];
        if (arg == "--duration-ms") options.duration_ms = psvr::bounded_integer(value, 100, 60000);
        if (arg == "--read-timeout-ms") options.read_timeout_ms = psvr::bounded_integer(value, 1, 1000);
        if (arg == "--raw-packets") options.raw_packets = psvr::bounded_integer(value, 0, 100);
        if (arg == "--log-dir") options.log_dir = value;
    }
    psvr::validate_options(options);
    return options;
}

std::string hex_bytes(const unsigned char* buffer, int count) {
    std::ostringstream s;
    s << std::hex << std::setfill('0');
    for (int i = 0; i < count; ++i) s << (i ? " " : "") << std::setw(2) << unsigned(buffer[i]);
    return s.str();
}

HidHandle open_interface(const hid_device_info& info, const char* role, Logger& log, bool& supports_input) {
    // HIDAPI can fall back to a metadata-only handle on Windows. Require actual
    // read/write access up front, without sending any report, to avoid false PASS.
    const int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, info.path, -1, nullptr, 0);
    if (!size) throw std::runtime_error("invalid UTF-8 HID path");
    std::vector<wchar_t> path(static_cast<std::size_t>(size));
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, info.path, -1, path.data(), size);
    HANDLE check = CreateFileW(path.data(), GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
    if (check == INVALID_HANDLE_VALUE) {
        log.write("ERROR", std::string(role) + " HID access failed Windows error=" + std::to_string(GetLastError()));
        return {};
    }
    PHIDP_PREPARSED_DATA preparsed = nullptr;
    HIDP_CAPS caps{};
    const bool got_data = HidD_GetPreparsedData(check, &preparsed) != FALSE;
    const bool got_caps = got_data && HidP_GetCaps(preparsed, &caps) == HIDP_STATUS_SUCCESS;
    if (preparsed) HidD_FreePreparsedData(preparsed);
    CloseHandle(check);
    if (!got_caps) {
        log.write("ERROR", std::string(role) + " HID capabilities unavailable");
        return {};
    }
    supports_input = caps.InputReportByteLength != 0;
    log.write("INFO", std::string(role) + " HID capabilities input_bytes=" + std::to_string(caps.InputReportByteLength) +
        " output_bytes=" + std::to_string(caps.OutputReportByteLength) + " feature_bytes=" + std::to_string(caps.FeatureReportByteLength));
    HidHandle handle(hid_open_path(info.path));
    if (!handle) {
        log.write("ERROR", std::string(role) + " hid_open_path failed: " + psvr::utf8(hid_error(nullptr)));
        return {};
    }
    log.write("INFO", std::string(role) + " interface OK interface=" + std::to_string(info.interface_number) +
        " (read/write handle access confirmed; no output reports sent)");
    return handle;
}

int capture(hid_device* sensor, hid_device* control, bool control_supports_input, const psvr::Options& options, Logger& log) {
    unsigned long long reports = 0, valid = 0, invalid = 0, control_reports = 0;
    unsigned long long sequence_gaps = 0;
    unsigned int previous_sequence = 0;
    bool have_sequence = false;
    bool io_failed = false;
    const auto start = Clock::now();
    const auto deadline = start + std::chrono::milliseconds(options.duration_ms);
    log.write("INFO", "capture started duration_ms=" + std::to_string(options.duration_ms) +
        " read_timeout_ms=" + std::to_string(options.read_timeout_ms));
    while (Clock::now() < deadline && !interrupted.load()) {
        std::array<unsigned char, 512> buffer{};
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        const int timeout = static_cast<int>(std::max<long long>(1, std::min<long long>(options.read_timeout_ms, remaining)));
        const int count = hid_read_timeout(sensor, buffer.data(), buffer.size(), timeout);
        if (count < 0) {
            log.write("ERROR", "sensor read failed: " + psvr::utf8(hid_read_error(sensor)));
            io_failed = true;
            break;
        }
        if (count > 0) {
            ++reports;
            const auto packet = psvr::decode_sensor(buffer.data(), static_cast<std::size_t>(count));
            if (packet) {
                ++valid;
                if (have_sequence) {
                    const auto delta = (unsigned(packet->sequence) - previous_sequence) & 0xffu;
                    if (delta != 1) ++sequence_gaps;
                }
                previous_sequence = packet->sequence;
                have_sequence = true;
                if (valid == 1) {
                    log.write("INFO", "sensor packet OK length=64 tick0=" + std::to_string(packet->samples[0].tick) +
                        " tick1=" + std::to_string(packet->samples[1].tick) + " seq=" + std::to_string(packet->sequence));
                    if (options.verbosity == "debug")
                        log.write("DEBUG", "sensor first intra-packet tick delta (24bit)=" +
                            std::to_string((packet->samples[1].tick - packet->samples[0].tick) & 0xffffffu));
                }
            } else {
                ++invalid;
                if (invalid <= 3) log.write("WARN", "sensor packet rejected length=" + std::to_string(count) + " expected=64");
            }
            if (reports <= static_cast<unsigned>(options.raw_packets))
                log.write("INFO", "sensor raw length=" + std::to_string(count) + " hex=" + hex_bytes(buffer.data(), count));
        }
        // Passive input only. Silence on control is normal; never request device info.
        const int control_count = control_supports_input ? hid_read_timeout(control, buffer.data(), buffer.size(), 0) : 0;
        if (control_count < 0) {
            log.write("ERROR", "control read failed: " + psvr::utf8(hid_read_error(control)));
            io_failed = true;
            break;
        }
        if (control_count > 0) {
            ++control_reports;
            if (control_reports <= static_cast<unsigned>(options.raw_packets))
                log.write("INFO", "control raw length=" + std::to_string(control_count) + " hex=" + hex_bytes(buffer.data(), control_count));
        }
    }
    const double seconds = std::chrono::duration<double>(Clock::now() - start).count();
    std::ostringstream summary;
    summary << std::fixed << std::setprecision(2) << "sensor summary reports=" << reports
        << " valid64=" << valid << " rejected=" << invalid << " sequence_discontinuities=" << sequence_gaps
        << " elapsed_s=" << seconds << " report_hz=" << reports / seconds
        << " samples_hz_estimate=" << (2 * valid) / seconds;
    log.write("INFO", summary.str());
    log.write("INFO", "control summary reports=" + std::to_string(control_reports) + " (passive timeout is allowed)");
    if (interrupted.load()) return 130;
    if (io_failed) return 3;
    if (valid == 0) {
        log.write("WARN", "MILESTONE 1 INCOMPLETE: no valid 64-byte sensor reports; check physical power/cables and repeat");
        return 4;
    }
    log.write("INFO", "MILESTONE 1 PASS: HID 4/5 open and sensor reports received; power/mode/orientation/display untested");
    return 0;
}

int diagnose(const psvr::Options& options, Logger& log) {
    HidSession session;
    log.write("INFO", std::string("psvr_diag 0.1.0 x64 HIDAPI=") + hid_version_str() + " passive=true");
    log.write("INFO", "USB detection started");
    const auto usb = psvr::enumerate_usb();
    unsigned int matches = 0, composite_devices = 0;
    for (const auto& device : usb) {
        if (device.is_psvr) ++matches;
        if (device.is_composite) ++composite_devices;
        if (device.is_psvr || options.all_devices) {
            log.write(device.is_psvr && device.problem ? "WARN" : "INFO",
                "USB PnP id=" + device.instance_id + " name=" + device.name + " service=" + device.service +
                " problem_code=" + std::to_string(device.problem) + " status=" + std::to_string(device.status) +
                " status_query_result=" + std::to_string(device.status_result));
        }
    }
    log.write("INFO", "USB enumeration present_nodes=" + std::to_string(usb.size()) + " psvr_nodes=" + std::to_string(matches));

    std::unique_ptr<hid_device_info, EnumerationCloser> hid_list(hid_enumerate(0, 0));
    // HIDAPI documents nullptr + hid_error as an enumeration error, including no
    // matching HID devices. USB PnP still distinguishes PSVR absent from inaccessible.
    if (!hid_list) log.write("WARN", "HID enumeration returned no devices: " + psvr::utf8(hid_error(nullptr)));
    unsigned int total = 0;
    std::vector<int> interfaces;
    const hid_device_info* sensor_info = nullptr;
    const hid_device_info* control_info = nullptr;
    for (auto* info = hid_list.get(); info; info = info->next) {
        ++total;
        const bool match = info->vendor_id == psvr::vendor_id && info->product_id == psvr::product_id;
        if (match) {
            interfaces.push_back(info->interface_number);
            if (info->interface_number == psvr::sensor_interface) sensor_info = info;
            if (info->interface_number == psvr::control_interface) control_info = info;
        }
        if (match || options.all_devices) {
            std::ostringstream metadata;
            metadata << "HID VID=" << std::hex << std::setfill('0') << std::setw(4) << info->vendor_id
                << " PID=" << std::setw(4) << info->product_id << " release=" << std::setw(4) << info->release_number
                << " usage_page=" << std::setw(4) << info->usage_page << " usage=" << std::setw(4) << info->usage
                << std::dec << " interface=" << info->interface_number << " product=" << psvr::utf8(info->product_string)
                << " manufacturer=" << psvr::utf8(info->manufacturer_string) << " serial=" << psvr::utf8(info->serial_number)
                << " path=" << (info->path ? info->path : "<missing>");
            log.write("INFO", metadata.str());
        }
    }
    log.write("INFO", "HID enumeration total=" + std::to_string(total) + " psvr_collections=" + std::to_string(interfaces.size()));
    if (matches == 0 && interfaces.empty()) {
        log.write("WARN", "PSVR not detected VID=054c PID=09af (no crash; check USB data cable and processor power)");
        return 2;
    }
    log.write("INFO", "PSVR detected VID=054c PID=09af (PSVR1 family; exact CUH revision not inferred)");
    if (composite_devices) log.write("INFO", "USB interface OK (PnP enumeration; no raw USB claim)");
    else log.write("WARN", "USB composite node not confirmed; HID presence alone is not raw USB validation");
    if (options.enumerate_only) {
        log.write("INFO", "enumerate-only complete; interfaces not opened, milestone 1 not validated");
        return 0;
    }
    const auto selection = psvr::select_interfaces(interfaces);
    if (composite_devices > 1 || selection == psvr::Selection::ambiguous) {
        log.write("ERROR", "ambiguous PSVR sensor/control interfaces; connect one processor unit only");
        return 3;
    }
    if (selection != psvr::Selection::ready || !sensor_info || !control_info) {
        log.write("ERROR", "required HID interfaces missing: sensor=4 control=5; check Windows HID bindings/service and competing drivers");
        return 3;
    }
    if (!sensor_info->path || !control_info->path) throw std::runtime_error("selected HID path missing");
    log.write("INFO", "HID open started (only PSVR interfaces 4/5)");
    bool sensor_supports_input = false, control_supports_input = false;
    auto sensor = open_interface(*sensor_info, "sensor", log, sensor_supports_input);
    auto control = open_interface(*control_info, "control", log, control_supports_input);
    if (!sensor || !control) return 3;
    if (!sensor_supports_input) {
        log.write("ERROR", "sensor HID descriptor has no input reports");
        return 3;
    }
    if (!control_supports_input) log.write("INFO", "control descriptor exposes no input reports; passive control read skipped");
    const auto result = capture(sensor.get(), control.get(), control_supports_input, options, log);
    sensor.reset();
    control.reset();
    log.write("INFO", "HID handles closed (no mode/power changes)");
    return result;
}
} // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--help") { help(); return 0; }
    std::unique_ptr<Logger> log;
    try {
        const auto options = parse_options(argc, argv);
        SetConsoleOutputCP(CP_UTF8);
        log = std::make_unique<Logger>(options.log_dir);
        log->write("INFO", "config effective duration_ms=" + std::to_string(options.duration_ms) +
            " read_timeout_ms=" + std::to_string(options.read_timeout_ms) + " raw_packets=" +
            std::to_string(options.raw_packets) + " verbosity=" + options.verbosity);
        if (!SetConsoleCtrlHandler(console_control, TRUE)) throw std::runtime_error("cannot register Ctrl+C handler");
        const int result = diagnose(options, *log);
        SetConsoleCtrlHandler(console_control, FALSE);
        log->write("INFO", "shutdown exit_code=" + std::to_string(result));
        return result;
    } catch (const std::exception& error) {
        SetConsoleCtrlHandler(console_control, FALSE);
        if (log) {
            try { log->write("ERROR", std::string(error.what()) + "; shutdown exit_code=1"); }
            catch (...) { std::cerr << "ERROR: logging also failed\n"; }
        }
        std::cerr << "ERROR: " << error.what() << "\nUse psvr_diag --help. No power/mode commands were sent.\n";
        return 1;
    }
}
