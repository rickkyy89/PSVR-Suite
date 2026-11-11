// SPDX-License-Identifier: MIT
#pragma once
// API-boundary fakes. Parse the real SDK before redirecting call sites.
#include <windows.h>
#include <setupapi.h>
#include <cfgmgr32.h>
#include <objbase.h>
#include <winusb.h>
#include <algorithm>
#include <cstring>
#include <limits>
#include <string>
#include <vector>
#include <stdexcept>

namespace winfake {
inline void check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
struct Device {
    std::wstring id = L"USB\\VID_054C&PID_09AF&MI_05\\A";
    std::wstring name = L"PSVR control", description = L"Fallback description", service = L"WinUSB";
    ULONG status = 8, problem = 0;
    CONFIGRET status_result = CR_SUCCESS;
};
struct Interface {
    std::wstring id = L"USB\\VID_054C&PID_09AF&MI_05\\A", path = L"\\\\?\\mock-control";
};
struct State {
    std::string failure;
    std::vector<Device> devices{Device{}};
    std::vector<Interface> interfaces{Interface{}};
    std::vector<std::wstring> guids{L"{12345678-1234-1234-1234-123456789ABC}"};
    DWORD registry_type = REG_MULTI_SZ, error = ERROR_ACCESS_DENIED, read_error = 0;
    UCHAR alternate = 0, interface_number = 5;
    std::vector<WINUSB_PIPE_INFORMATION> pipes{
        {UsbdPipeTypeInterrupt, 0x81, 64, 1}, {UsbdPipeTypeInterrupt, 0x02, 64, 1}};
    std::vector<unsigned char> frame{0xf0, 0, 0xaa, 0x10}, packet;
    ULONG write_length = std::numeric_limits<ULONG>::max();
    int sets = 0, destroyed = 0, keys = 0, key_closes = 0;
    int file_attempts = 0, files = 0, file_closes = 0, usb = 0, usb_frees = 0;
    int reads = 0, writes = 0, policies = 0;
};
inline State state;
inline bool fails(const char* operation) { return state.failure == operation; }
inline HANDLE token(int value) { return reinterpret_cast<HANDLE>(static_cast<INT_PTR>(value)); }
inline DWORD last_error() { return state.error; }
inline HDEVINFO class_devs(const GUID* guid, PCWSTR, HWND, DWORD) {
    if (fails(guid ? "interfaces" : "devices")) return INVALID_HANDLE_VALUE;
    ++state.sets; return token(guid ? 2 : 1);
}
inline BOOL destroy(HDEVINFO) { ++state.destroyed; return TRUE; }
inline BOOL enum_device(HDEVINFO, DWORD index, PSP_DEVINFO_DATA device) {
    if (fails("enum_device")) return FALSE;
    if (index >= state.devices.size()) { state.error = ERROR_NO_MORE_ITEMS; return FALSE; }
    device->Reserved = index; device->DevInst = index + 1; return TRUE;
}
inline BOOL instance(HDEVINFO set, PSP_DEVINFO_DATA device, PWSTR buffer, DWORD size, PDWORD needed) {
    if (fails(buffer ? "instance_read" : "instance_size")) return FALSE;
    const auto& id = set == token(2) ? state.interfaces.at(device->Reserved).id : state.devices.at(device->Reserved).id;
    const auto count = static_cast<DWORD>(id.size() + 1);
    if (needed) *needed = count;
    if (!buffer || size < count) { state.error = ERROR_INSUFFICIENT_BUFFER; return FALSE; }
    std::copy(id.c_str(), id.c_str() + count, buffer); return TRUE;
}
inline HKEY open_key(HDEVINFO, PSP_DEVINFO_DATA, DWORD, DWORD, DWORD, REGSAM) {
    if (fails("open_key")) return reinterpret_cast<HKEY>(INVALID_HANDLE_VALUE);
    ++state.keys; return reinterpret_cast<HKEY>(token(3));
}
inline LSTATUS close_key(HKEY) { ++state.key_closes; return ERROR_SUCCESS; }
inline LSTATUS query_key(HKEY, LPCWSTR, LPDWORD, LPDWORD type, LPBYTE buffer, LPDWORD size) {
    if (fails(buffer ? "registry_read" : "registry_size")) return ERROR_ACCESS_DENIED;
    std::vector<wchar_t> data;
    for (const auto& guid : state.guids) {
        data.insert(data.end(), guid.begin(), guid.end()); data.push_back(0);
    }
    data.push_back(0);
    if (data.size() == 1) data.push_back(0);
    *type = state.registry_type;
    const auto bytes = static_cast<DWORD>(data.size() * sizeof(wchar_t));
    if (buffer) {
        if (*size < bytes) return ERROR_MORE_DATA;
        std::memcpy(buffer, data.data(), bytes);
    }
    *size = bytes; return ERROR_SUCCESS;
}
inline BOOL enum_interface(HDEVINFO, PSP_DEVINFO_DATA, const GUID*, DWORD index, PSP_DEVICE_INTERFACE_DATA entry) {
    if (fails("enum_interface")) return FALSE;
    if (index >= state.interfaces.size()) { state.error = ERROR_NO_MORE_ITEMS; return FALSE; }
    entry->Reserved = index; return TRUE;
}
inline BOOL detail(HDEVINFO, PSP_DEVICE_INTERFACE_DATA entry, PSP_DEVICE_INTERFACE_DETAIL_DATA_W output,
                   DWORD size, PDWORD needed, PSP_DEVINFO_DATA device) {
    if (fails(output ? "detail_read" : "detail_size")) return FALSE;
    const auto& path = state.interfaces.at(entry->Reserved).path;
    const auto bytes = static_cast<DWORD>(sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) + path.size() * sizeof(wchar_t));
    if (needed) *needed = bytes;
    if (!output || size < bytes) { state.error = ERROR_INSUFFICIENT_BUFFER; return FALSE; }
    std::copy(path.c_str(), path.c_str() + path.size() + 1, output->DevicePath);
    if (device) device->Reserved = entry->Reserved;
    return TRUE;
}
inline BOOL property(HDEVINFO, PSP_DEVINFO_DATA device, DWORD key, PDWORD type, PBYTE buffer, DWORD size, PDWORD needed) {
    const auto& node = state.devices.at(device->Reserved);
    const auto& text = key == SPDRP_SERVICE ? node.service : key == SPDRP_FRIENDLYNAME ? node.name : node.description;
    const auto bytes = static_cast<DWORD>((text.size() + 1) * sizeof(wchar_t));
    if (needed) *needed = text.empty() ? 0 : bytes;
    if (type) *type = REG_SZ;
    if (!buffer || text.empty() || fails("property_read") || size < bytes) return FALSE;
    std::memcpy(buffer, text.c_str(), bytes); return TRUE;
}
inline CONFIGRET status(PULONG flags, PULONG problem, DEVINST instance_id, ULONG) {
    const auto& device = state.devices.at(instance_id - 1);
    if (device.status_result == CR_SUCCESS) { *flags = device.status; *problem = device.problem; }
    return device.status_result;
}
inline HANDLE open_file(LPCWSTR, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES, DWORD creation, DWORD flags, HANDLE) {
    check(access == (GENERIC_READ | GENERIC_WRITE) && share == (FILE_SHARE_READ | FILE_SHARE_WRITE) &&
          creation == OPEN_EXISTING && flags == FILE_FLAG_OVERLAPPED, "unexpected open flags");
    ++state.file_attempts;
    if (fails("open_file")) return INVALID_HANDLE_VALUE;
    ++state.files; return token(4);
}
inline BOOL close_file(HANDLE file) { check(file == token(4), "unexpected handle closed"); ++state.file_closes; return TRUE; }
inline BOOL initialize(HANDLE file, PWINUSB_INTERFACE_HANDLE usb) {
    check(file == token(4), "unexpected initialize handle");
    if (fails("initialize")) return FALSE;
    ++state.usb; *usb = token(5); return TRUE;
}
inline BOOL free_usb(WINUSB_INTERFACE_HANDLE usb) { check(usb == token(5), "unexpected USB handle"); ++state.usb_frees; return TRUE; }
inline BOOL alternate(WINUSB_INTERFACE_HANDLE, PUCHAR value) {
    if (fails("alternate")) return FALSE;
    *value = state.alternate; return TRUE;
}
inline BOOL descriptor(WINUSB_INTERFACE_HANDLE, UCHAR alt, PUSB_INTERFACE_DESCRIPTOR value) {
    check(alt == 0, "alternate setting changed");
    if (fails("descriptor")) return FALSE;
    value->bInterfaceNumber = state.interface_number;
    value->bNumEndpoints = static_cast<UCHAR>(state.pipes.size()); return TRUE;
}
inline BOOL pipe(WINUSB_INTERFACE_HANDLE, UCHAR alt, UCHAR index, PWINUSB_PIPE_INFORMATION value) {
    check(alt == 0, "pipe alternate setting changed");
    if (fails("pipe")) return FALSE;
    *value = state.pipes.at(index); return TRUE;
}
inline BOOL policy(WINUSB_INTERFACE_HANDLE, UCHAR, ULONG kind, ULONG size, PVOID value) {
    ++state.policies;
    check(kind == PIPE_TRANSFER_TIMEOUT && size == sizeof(ULONG) &&
          *static_cast<ULONG*>(value) >= 1 && *static_cast<ULONG*>(value) <= 1000, "bad timeout policy");
    return fails("policy") ? FALSE : TRUE;
}
inline BOOL read(WINUSB_INTERFACE_HANDLE, UCHAR id, PUCHAR buffer, ULONG size, PULONG received, LPOVERLAPPED overlapped) {
    ++state.reads; check(id == 0x81 && !overlapped && size == 64, "unexpected read");
    if (state.read_error || fails("read")) { if (state.read_error) state.error = state.read_error; return FALSE; }
    check(state.frame.size() <= size, "fake frame too long");
    std::copy(state.frame.begin(), state.frame.end(), buffer);
    *received = static_cast<ULONG>(state.frame.size()); return TRUE;
}
inline BOOL write(WINUSB_INTERFACE_HANDLE, UCHAR id, PUCHAR buffer, ULONG size, PULONG sent, LPOVERLAPPED overlapped) {
    ++state.writes; check(id == 2 && !overlapped, "unexpected write");
    state.packet.assign(buffer, buffer + size);
    if (fails("write")) return FALSE;
    *sent = state.write_length == std::numeric_limits<ULONG>::max() ? size : state.write_length; return TRUE;
}
inline void balanced() {
    check(state.sets == state.destroyed && state.keys == state.key_closes &&
          state.files == state.file_closes && state.usb == state.usb_frees, "backend resource leak/double close");
}
}

#define GetLastError winfake::last_error
#define SetupDiGetClassDevsW winfake::class_devs
#define SetupDiDestroyDeviceInfoList winfake::destroy
#define SetupDiEnumDeviceInfo winfake::enum_device
#define SetupDiGetDeviceInstanceIdW winfake::instance
#define SetupDiOpenDevRegKey winfake::open_key
#define RegQueryValueExW winfake::query_key
#define RegCloseKey winfake::close_key
#define SetupDiEnumDeviceInterfaces winfake::enum_interface
#define SetupDiGetDeviceInterfaceDetailW winfake::detail
#define SetupDiGetDeviceRegistryPropertyW winfake::property
#define CM_Get_DevNode_Status winfake::status
#define CreateFileW winfake::open_file
#define CloseHandle winfake::close_file
#define WinUsb_Initialize winfake::initialize
#define WinUsb_Free winfake::free_usb
#define WinUsb_GetCurrentAlternateSetting winfake::alternate
#define WinUsb_QueryInterfaceSettings winfake::descriptor
#define WinUsb_QueryPipe winfake::pipe
#define WinUsb_SetPipePolicy winfake::policy
#define WinUsb_ReadPipe winfake::read
#define WinUsb_WritePipe winfake::write
