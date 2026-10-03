// SPDX-License-Identifier: MIT
#pragma once
#include <string>
#include <vector>

namespace psvr {
struct PnpDevice {
    std::string instance_id;
    std::string name;
    std::string service;
    unsigned long status = 0;
    unsigned long problem = 0;
    unsigned long status_result = 0;
    bool is_psvr = false;
    bool is_composite = false;
};
std::string utf8(const wchar_t* text);
std::vector<PnpDevice> enumerate_usb();
} // namespace psvr
