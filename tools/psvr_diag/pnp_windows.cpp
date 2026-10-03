// SPDX-License-Identifier: MIT
#include "pnp_windows.hpp"
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <algorithm>
#include <memory>
#include <stdexcept>

namespace psvr {
std::string utf8(const wchar_t* text) {
    if (!text || !*text) return "<not provided>";
    const int size = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
    if (size <= 0) return "<conversion error>";
    std::string result(static_cast<std::size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, -1, result.data(), size, nullptr, nullptr);
    result.pop_back();
    return result;
}

namespace {
struct DeviceSetCloser {
    void operator()(void* set) const { SetupDiDestroyDeviceInfoList(set); }
};
std::string property(HDEVINFO set, SP_DEVINFO_DATA& device, DWORD key) {
    DWORD type = 0, size = 0;
    SetupDiGetDeviceRegistryPropertyW(set, &device, key, &type, nullptr, 0, &size);
    if (size == 0) return "<not provided>";
    std::vector<wchar_t> buffer(size / sizeof(wchar_t) + 2, 0);
    if (!SetupDiGetDeviceRegistryPropertyW(set, &device, key, &type,
            reinterpret_cast<PBYTE>(buffer.data()), size, nullptr)) return "<unavailable>";
    return utf8(buffer.data());
}
}

std::vector<PnpDevice> enumerate_usb() {
    auto set = SetupDiGetClassDevsW(nullptr, nullptr, nullptr, DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (set == INVALID_HANDLE_VALUE)
        throw std::runtime_error("SetupDiGetClassDevs failed Windows error=" + std::to_string(GetLastError()));
    std::unique_ptr<void, DeviceSetCloser> guard(set);
    std::vector<PnpDevice> result;
    for (DWORD index = 0;; ++index) {
        SP_DEVINFO_DATA device{};
        device.cbSize = sizeof(device);
        if (!SetupDiEnumDeviceInfo(set, index, &device)) {
            const auto error = GetLastError();
            if (error == ERROR_NO_MORE_ITEMS) break;
            throw std::runtime_error("SetupDiEnumDeviceInfo failed Windows error=" + std::to_string(error));
        }
        DWORD needed = 0;
        SetupDiGetDeviceInstanceIdW(set, &device, nullptr, 0, &needed);
        if (!needed) throw std::runtime_error("PnP instance ID length unavailable");
        std::vector<wchar_t> id(needed, 0);
        if (!SetupDiGetDeviceInstanceIdW(set, &device, id.data(), needed, nullptr))
            throw std::runtime_error("PnP instance ID unavailable Windows error=" + std::to_string(GetLastError()));
        const auto instance = utf8(id.data());
        if (instance.rfind("USB\\", 0) != 0) continue;
        PnpDevice entry;
        entry.instance_id = instance;
        entry.name = property(set, device, SPDRP_FRIENDLYNAME);
        if (entry.name == "<not provided>") entry.name = property(set, device, SPDRP_DEVICEDESC);
        entry.service = property(set, device, SPDRP_SERVICE);
        entry.status_result = CM_Get_DevNode_Status(&entry.status, &entry.problem, device.DevInst, 0);
        std::string upper = instance;
        std::transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) {
            return static_cast<char>(c >= 'a' && c <= 'z' ? c - 'a' + 'A' : c);
        });
        entry.is_psvr = upper.find("VID_054C&PID_09AF") != std::string::npos;
        entry.is_composite = entry.is_psvr && upper.find("&MI_") == std::string::npos;
        result.push_back(std::move(entry));
    }
    return result;
}
} // namespace psvr
