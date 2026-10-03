// SPDX-License-Identifier: MIT
#pragma once
#include "control_protocol.hpp"
#include <windows.h>
#include <winusb.h>
#include <string>
#include <vector>

namespace psvr {
class WinUsbControl {
    HANDLE file_ = INVALID_HANDLE_VALUE;
    WINUSB_INTERFACE_HANDLE usb_ = nullptr;
    UCHAR input_ = 0, output_ = 0;
    std::string description_;
    void close() noexcept;
public:
    explicit WinUsbControl(const std::string& instance_id);
    ~WinUsbControl() { close(); }
    WinUsbControl(const WinUsbControl&) = delete;
    WinUsbControl& operator=(const WinUsbControl&) = delete;
    const std::string& description() const { return description_; }
    std::vector<unsigned char> read(int timeout_ms);
    unsigned long send(Action action, int timeout_ms);
};
} // namespace psvr
