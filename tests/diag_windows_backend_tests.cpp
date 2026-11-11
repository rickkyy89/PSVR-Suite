// SPDX-License-Identifier: MIT
#include "diag_windows_fakes.hpp"
#include "control_windows.hpp"
#include "pnp_windows.hpp"
#include <iostream>

namespace {
constexpr const char* target = "USB\\VID_054C&PID_09AF&MI_05\\A";
int cases = 0;
template<class Function> void rejects(Function function) {
    bool rejected = false;
    try { function(); } catch (const std::runtime_error&) { rejected = true; }
    winfake::check(rejected, "expected explicit backend failure");
}
template<class Function> void scenario(const char* name, Function function) {
    winfake::state = winfake::State{};
    try { function(); winfake::balanced(); ++cases; }
    catch (const std::exception& error) { throw std::runtime_error(std::string(name) + ": " + error.what()); }
}
}
int main() {
    using namespace winfake;
    try {
        scenario("PnP metadata and filter", [] {
            auto composite = Device{}; composite.id = L"USB\\VID_054C&PID_09AF\\C";
            composite.name.clear(); composite.description = L"Composite fallback";
            composite.service = L"usbccgp";
            auto foreign = Device{}; foreign.id = L"PCI\\VEN_1234";
            auto other = Device{}; other.id = L"USB\\VID_0001&PID_0002\\D";
            state.devices = {Device{}, composite, foreign, other};
            const auto result = psvr::enumerate_usb();
            check(result.size() == 3 && result[0].is_psvr && !result[0].is_composite &&
                result[0].service == "WinUSB" && result[0].status == 8 && result[0].problem == 0 &&
                result[1].is_composite && result[1].name == "Composite fallback" && !result[2].is_psvr,
                "PnP inventory lost metadata, fallback or USB filter");
            check(state.file_attempts == 0 && state.usb == 0 && state.writes == 0, "PnP opened a device");
        });
        for (const auto failure : {"devices", "enum_device", "instance_size", "instance_read"})
            scenario(failure, [=] { state.failure = failure; rejects([] { psvr::enumerate_usb(); }); });
        scenario("PnP empty", [] { state.devices.clear(); check(psvr::enumerate_usb().empty(), "empty inventory wrong"); });
        scenario("PnP unavailable property", [] {
            state.failure = "property_read";
            const auto result = psvr::enumerate_usb();
            check(result.front().name == "<unavailable>" && result.front().service == "<unavailable>", "property failure hidden");
        });
        scenario("PnP status failure", [] {
            state.devices.front().status_result = CR_FAILURE;
            check(psvr::enumerate_usb().front().status_result == CR_FAILURE, "status error hidden");
        });
        scenario("PnP problem", [] {
            state.devices.front().problem = 28;
            check(psvr::enumerate_usb().front().problem == 28, "PnP problem lost");
        });
        scenario("target whitelist", [] {
            rejects([] { psvr::WinUsbControl control("USB\\VID_054C&PID_09AF&MI_04\\A"); });
            check(state.sets == 0 && state.file_attempts == 0, "wrong interface reached discovery");
        });
        for (const auto failure : {"devices", "enum_device", "instance_size", "instance_read", "open_key",
                "registry_size", "registry_read", "interfaces", "enum_interface", "detail_size", "detail_read"})
            scenario(failure, [=] {
                state.failure = failure;
                rejects([] { psvr::WinUsbControl control(target); });
                check(state.file_attempts == 0, "discovery failure opened a device");
            });
        scenario("device disappeared", [] {
            state.devices.clear(); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("registry wrong type", [] {
            state.registry_type = REG_SZ; rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("registry no GUIDs", [] {
            state.guids.clear(); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("registry malformed GUID", [] {
            state.guids = {L"not-a-GUID"}; rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("no path", [] {
            state.interfaces.clear(); rejects([] { psvr::WinUsbControl control(target); });
            check(state.file_attempts == 0, "missing path opened a device");
        });
        scenario("foreign path excluded", [] {
            state.interfaces.front().id = L"USB\\VID_054C&PID_09AF&MI_05\\OTHER";
            rejects([] { psvr::WinUsbControl control(target); });
            check(state.file_attempts == 0, "foreign instance selected");
        });
        scenario("multiple paths", [] {
            auto extra = Interface{}; extra.path = L"\\\\?\\other-control";
            state.interfaces.push_back(extra);
            rejects([] { psvr::WinUsbControl control(target); });
            check(state.file_attempts == 0, "ambiguous path selected");
        });
        scenario("case-insensitive duplicate paths and multiple GUIDs", [] {
            state.guids.push_back(L"{87654321-4321-4321-4321-CBA987654321}");
            auto duplicate = Interface{}; duplicate.path = L"\\\\?\\MOCK-CONTROL";
            duplicate.id = L"usb\\vid_054c&pid_09af&mi_05\\a";
            state.interfaces.push_back(duplicate);
            { psvr::WinUsbControl control(target); check(!control.description().empty(), "missing description"); }
            check(state.files == 1 && state.writes == 0 && state.reads == 0, "open had side effects or duplicated path");
        });
        for (const auto failure : {"open_file", "initialize", "alternate", "descriptor", "pipe"})
            scenario(failure, [=] {
                state.failure = failure; rejects([] { psvr::WinUsbControl control(target); });
                check(state.writes == 0 && state.reads == 0, "constructor sent/read a command");
            });
        scenario("wrong alternate", [] {
            state.alternate = 1; rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("wrong interface", [] {
            state.interface_number = 4; rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("unsupported pipe", [] {
            state.pipes.front().PipeType = UsbdPipeTypeIsochronous;
            rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("zero endpoints", [] {
            state.pipes.clear(); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("missing input", [] {
            state.pipes.erase(state.pipes.begin()); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("missing output", [] {
            state.pipes.pop_back(); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("multiple inputs", [] {
            state.pipes.push_back(state.pipes.front()); rejects([] { psvr::WinUsbControl control(target); });
        });
        scenario("bulk accepted", [] {
            for (auto& pipe : state.pipes) pipe.PipeType = UsbdPipeTypeBulk;
            psvr::WinUsbControl control(target);
            check(state.writes == 0 && state.reads == 0, "bulk open performed I/O");
        });
        for (const int timeout : {0, 1001})
            scenario("bad timeout", [=] {
                psvr::WinUsbControl control(target);
                rejects([&] { control.read(timeout); });
                rejects([&] { control.send(psvr::Action::vr_mode, timeout); });
                check(state.policies == 0 && state.reads == 0 && state.writes == 0, "invalid timeout reached API");
            });
        for (const int timeout : {1, 1000})
            scenario("timeout boundaries", [=] {
                psvr::WinUsbControl control(target);
                check(control.read(timeout) == state.frame, "read data changed");
                check(control.send(psvr::Action::vr_mode, timeout) == 8 &&
                      state.packet == psvr::command_packet(psvr::Action::vr_mode), "wire command changed");
            });
        for (const DWORD timeout : {static_cast<DWORD>(ERROR_SEM_TIMEOUT), static_cast<DWORD>(ERROR_TIMEOUT)})
            scenario("read timeout not error", [=] {
                state.read_error = timeout;
                psvr::WinUsbControl control(target);
                check(control.read(100).empty(), "read timeout must yield no frame");
            });
        scenario("read error", [] {
            state.read_error = ERROR_DEVICE_NOT_CONNECTED;
            psvr::WinUsbControl control(target); rejects([&] { control.read(100); });
            check(state.reads == 1, "read error retried");
        });
        scenario("policy failure", [] {
            state.failure = "policy";
            psvr::WinUsbControl control(target);
            rejects([&] { control.read(100); });
            rejects([&] { control.send(psvr::Action::power_on, 100); });
            check(state.reads == 0 && state.writes == 0, "failed policy reached transfer");
        });
        scenario("write failure", [] {
            state.failure = "write";
            psvr::WinUsbControl control(target); rejects([&] { control.send(psvr::Action::power_on, 100); });
            check(state.writes == 1, "failed write retried");
        });
        scenario("partial write", [] {
            state.write_length = 3;
            psvr::WinUsbControl control(target); rejects([&] { control.send(psvr::Action::power_on, 100); });
            check(state.writes == 1, "partial write retried");
        });
        scenario("write whitelist", [] {
            psvr::WinUsbControl control(target);
            for (const auto action : {psvr::Action::none, psvr::Action::status,
                    psvr::Action::control_info, static_cast<psvr::Action>(999)})
                rejects([&] { control.send(action, 100); });
            check(state.writes == 0 && state.policies == 0, "non-command reached USB write");
        });
        std::cout << "PASS Windows backends: " << cases << " fault/contract cases; balanced resources; all device API calls fake\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL Windows backends: " << error.what() << '\n'; return 1;
    }
}
