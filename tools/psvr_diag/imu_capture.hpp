// SPDX-License-Identifier: MIT
#pragma once
#include "diag_core.hpp"
#include "control_windows.hpp"
#include <hidapi.h>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <functional>

namespace psvr {
using ImuLog = std::function<void(const char*,const std::string&)>;
int capture_imu(hid_device* sensor, WinUsbControl* control, const Options& options, const ImuLog& log,
                 std::atomic<bool>& interrupted, const std::filesystem::path& csv_path);
// Same capture loop with a monotonic clock supplied for deterministic offline tests.
using ImuNow = std::chrono::steady_clock::time_point (*)();
int capture_imu(hid_device* sensor, WinUsbControl* control, const Options& options, const ImuLog& log,
                std::atomic<bool>& interrupted, const std::filesystem::path& csv_path, ImuNow now);
void request_recenter(int pid);
void request_recalibrate(int pid);
void request_stop_imu(int pid);
} // namespace psvr
