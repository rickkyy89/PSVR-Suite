// SPDX-License-Identifier: MIT
#pragma once
#include <windows.h>
#include <stdexcept>
#include <string>

namespace psvr {
// Cross-process ownership for our diagnostic and driver. Acquired/released on
// the same thread, before opening hardware. Enumeration remains read-only.
class DeviceSession {
    HANDLE mutex_ = nullptr;
public:
    DeviceSession() : DeviceSession(std::wstring(L"Local\\PSVR-PC-DeviceSession")) {}
    explicit DeviceSession(const std::wstring& name) {
        mutex_ = CreateMutexW(nullptr,FALSE,name.c_str());
        if (!mutex_) throw std::runtime_error("cannot create PSVR ownership mutex");
        const auto result = WaitForSingleObject(mutex_,0);
        if (result != WAIT_OBJECT_0 && result != WAIT_ABANDONED) {
            CloseHandle(mutex_); mutex_ = nullptr;
            throw std::runtime_error("PSVR is in use by another PSVR-PC capture/driver; stop it first");
        }
    }
    ~DeviceSession() { if (mutex_) { ReleaseMutex(mutex_); CloseHandle(mutex_); } }
    DeviceSession(const DeviceSession&) = delete;
    DeviceSession& operator=(const DeviceSession&) = delete;
};
} // namespace psvr
