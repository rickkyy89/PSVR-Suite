// SPDX-License-Identifier: MIT
#include "control_windows.hpp"
#include "pnp_windows.hpp"
#include <setupapi.h>
#include <objbase.h>
#include <memory>
#include <sstream>
#include <iomanip>
#include <stdexcept>

namespace psvr {
namespace {
[[noreturn]] void win_error(const char* operation) {
    const auto code = GetLastError();
    throw std::runtime_error(std::string(operation) + " Windows error=" + std::to_string(code));
}
std::wstring wide(const std::string& s) {
    const int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(), -1, nullptr, 0);
    if (!size) win_error("UTF-8 conversion");
    std::wstring result(static_cast<std::size_t>(size), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(), -1, result.data(), size)) win_error("UTF-8 conversion");
    result.pop_back();
    return result;
}
struct DeviceSetCloser { void operator()(void* value) const { SetupDiDestroyDeviceInfoList(value); } };
using DeviceSet = std::unique_ptr<void, DeviceSetCloser>;
struct RegistryKey {
    HKEY key;
    ~RegistryKey() { if (key != INVALID_HANDLE_VALUE) RegCloseKey(key); }
};
std::wstring instance(HDEVINFO set, SP_DEVINFO_DATA& dev) {
    DWORD required = 0;
    SetupDiGetDeviceInstanceIdW(set, &dev, nullptr, 0, &required);
    if (!required) win_error("SetupDiGetDeviceInstanceId length");
    std::wstring result(required, L'\0');
    if (!SetupDiGetDeviceInstanceIdW(set, &dev, result.data(), required, nullptr)) win_error("SetupDiGetDeviceInstanceId");
    result.pop_back();
    return result;
}
std::vector<GUID> device_guids(const std::wstring& wanted) {
    auto set = SetupDiGetClassDevsW(nullptr, L"USB", nullptr, DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (set == INVALID_HANDLE_VALUE) win_error("USB device enumeration");
    DeviceSet guard(set);
    for (DWORD index = 0;; ++index) {
        SP_DEVINFO_DATA dev{};
        dev.cbSize = sizeof(dev);
        if (!SetupDiEnumDeviceInfo(set, index, &dev)) {
            if (GetLastError() == ERROR_NO_MORE_ITEMS) break;
            win_error("USB device entry");
        }
        if (_wcsicmp(instance(set, dev).c_str(), wanted.c_str()) != 0) continue;
        RegistryKey registry{SetupDiOpenDevRegKey(set, &dev, DICS_FLAG_GLOBAL, 0, DIREG_DEV, KEY_QUERY_VALUE)};
        if (registry.key == INVALID_HANDLE_VALUE) win_error("WinUSB device registry key");
        DWORD size = 0, type = 0;
        auto error = RegQueryValueExW(registry.key, L"DeviceInterfaceGUIDs", nullptr, &type, nullptr, &size);
        if (error != ERROR_SUCCESS || type != REG_MULTI_SZ || size < sizeof(wchar_t))
            throw std::runtime_error("MI_05 WinUSB DeviceInterfaceGUIDs missing/invalid; verify Zadig installation");
        std::vector<wchar_t> data(size / sizeof(wchar_t) + 2, L'\0');
        error = RegQueryValueExW(registry.key, L"DeviceInterfaceGUIDs", nullptr, &type,
            reinterpret_cast<BYTE*>(data.data()), &size);
        if (error != ERROR_SUCCESS) throw std::runtime_error("WinUSB GUID registry read error=" + std::to_string(error));
        std::vector<GUID> guids;
        for (const wchar_t* p = data.data(); *p; p += wcslen(p) + 1) {
            GUID guid{};
            if (FAILED(CLSIDFromString(p, &guid))) throw std::runtime_error("invalid WinUSB interface GUID");
            guids.push_back(guid);
        }
        if (guids.empty()) throw std::runtime_error("empty WinUSB interface GUID list");
        return guids;
    }
    throw std::runtime_error("MI_05 disappeared before WinUSB discovery");
}
std::wstring interface_path(const std::string& id) {
    const auto wanted = wide(id);
    if (id.find("USB\\VID_054C&PID_09AF&MI_05\\") != 0)
        throw std::runtime_error("WinUSB target must be PSVR 054C:09AF MI_05");
    std::vector<std::wstring> paths;
    for (const auto& guid : device_guids(wanted)) {
        auto set = SetupDiGetClassDevsW(&guid, nullptr, nullptr, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
        if (set == INVALID_HANDLE_VALUE) win_error("WinUSB interface enumeration");
        DeviceSet guard(set);
        for (DWORD index = 0;; ++index) {
            SP_DEVICE_INTERFACE_DATA entry{};
            entry.cbSize = sizeof(entry);
            if (!SetupDiEnumDeviceInterfaces(set, nullptr, &guid, index, &entry)) {
                if (GetLastError() == ERROR_NO_MORE_ITEMS) break;
                win_error("WinUSB interface entry");
            }
            DWORD size = 0;
            SetupDiGetDeviceInterfaceDetailW(set, &entry, nullptr, 0, &size, nullptr);
            if (size < sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)) win_error("WinUSB interface detail length");
            // malloc-backed storage is suitably aligned for the variable-size structure.
            std::vector<unsigned char> buffer(size);
            auto* detail = reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA_W*>(buffer.data());
            detail->cbSize = sizeof(*detail);
            SP_DEVINFO_DATA dev{};
            dev.cbSize = sizeof(dev);
            if (!SetupDiGetDeviceInterfaceDetailW(set, &entry, detail, size, nullptr, &dev)) win_error("WinUSB interface detail");
            if (_wcsicmp(instance(set, dev).c_str(), wanted.c_str()) == 0) {
                const std::wstring path = detail->DevicePath;
                bool duplicate = false;
                for (const auto& existing : paths) if (_wcsicmp(existing.c_str(), path.c_str()) == 0) duplicate = true;
                if (!duplicate) paths.push_back(path);
            }
        }
    }
    if (paths.size() != 1) throw std::runtime_error("expected exactly one MI_05 WinUSB path, found " + std::to_string(paths.size()));
    return paths.front();
}
}

void WinUsbControl::close() noexcept {
    if (usb_) { WinUsb_Free(usb_); usb_ = nullptr; }
    if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
}
WinUsbControl::WinUsbControl(const std::string& id) {
    try {
        const auto path = interface_path(id);
        file_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
            nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) win_error("MI_05 CreateFile");
        if (!WinUsb_Initialize(file_, &usb_)) win_error("MI_05 WinUsb_Initialize");
        USB_INTERFACE_DESCRIPTOR descriptor{};
        UCHAR alternate = 0;
        if (!WinUsb_GetCurrentAlternateSetting(usb_, &alternate)) win_error("WinUSB alternate setting");
        if (alternate != 0) throw std::runtime_error("MI_05 alternate setting is not 0; refusing to change it");
        if (!WinUsb_QueryInterfaceSettings(usb_, alternate, &descriptor)) win_error("WinUSB interface descriptor");
        if (descriptor.bInterfaceNumber != 5) throw std::runtime_error("WinUSB descriptor is not interface 5");
        std::ostringstream text;
        text << "WinUSB interface=5 alternate=0 class=" << unsigned(descriptor.bInterfaceClass)
             << " endpoints=" << unsigned(descriptor.bNumEndpoints) << " path=" << utf8(path.c_str());
        unsigned inputs = 0, outputs = 0;
        for (UCHAR index = 0; index < descriptor.bNumEndpoints; ++index) {
            WINUSB_PIPE_INFORMATION pipe{};
            if (!WinUsb_QueryPipe(usb_, alternate, index, &pipe)) win_error("WinUSB pipe descriptor");
            text << " [endpoint=0x" << std::hex << unsigned(pipe.PipeId) << std::dec
                 << " type=" << (pipe.PipeType == UsbdPipeTypeInterrupt ? "interrupt" :
                                   pipe.PipeType == UsbdPipeTypeBulk ? "bulk" : "unsupported")
                 << " max_packet=" << pipe.MaximumPacketSize << "]";
            if (pipe.PipeType != UsbdPipeTypeInterrupt && pipe.PipeType != UsbdPipeTypeBulk)
                throw std::runtime_error("MI_05 has unsupported pipe type");
            if ((pipe.PipeId & 0x80) != 0) { input_ = pipe.PipeId; ++inputs; }
            else { output_ = pipe.PipeId; ++outputs; }
        }
        if (inputs != 1 || outputs != 1) throw std::runtime_error("MI_05 requires one USB IN and one USB OUT pipe");
        description_ = text.str();
    } catch (...) { close(); throw; }
}
std::vector<unsigned char> WinUsbControl::read(int timeout_ms) {
    if (timeout_ms < 1 || timeout_ms > 1000) throw std::runtime_error("WinUSB read timeout outside 1..1000 ms");
    ULONG timeout = static_cast<ULONG>(timeout_ms), received = 0;
    if (!WinUsb_SetPipePolicy(usb_, input_, PIPE_TRANSFER_TIMEOUT, sizeof(timeout), &timeout)) win_error("WinUSB read timeout policy");
    std::vector<unsigned char> bytes(64);
    if (!WinUsb_ReadPipe(usb_, input_, bytes.data(), static_cast<ULONG>(bytes.size()), &received, nullptr)) {
        const auto error = GetLastError();
        if (error == ERROR_SEM_TIMEOUT || error == ERROR_TIMEOUT) return {};
        throw std::runtime_error("WinUSB control read Windows error=" + std::to_string(error));
    }
    bytes.resize(received);
    return bytes;
}
unsigned long WinUsbControl::send(Action action, int timeout_ms) {
    const auto packet = command_packet(action); // Strict whitelist; no raw-write API.
    if (timeout_ms < 1 || timeout_ms > 1000) throw std::runtime_error("WinUSB write timeout outside 1..1000 ms");
    ULONG timeout = static_cast<ULONG>(timeout_ms), sent = 0;
    if (!WinUsb_SetPipePolicy(usb_, output_, PIPE_TRANSFER_TIMEOUT, sizeof(timeout), &timeout)) win_error("WinUSB write timeout policy");
    if (!WinUsb_WritePipe(usb_, output_, const_cast<unsigned char*>(packet.data()), static_cast<ULONG>(packet.size()), &sent, nullptr))
        win_error("WinUSB control write");
    if (sent != packet.size()) throw std::runtime_error("partial WinUSB control write sent=" + std::to_string(sent));
    return sent;
}
} // namespace psvr
