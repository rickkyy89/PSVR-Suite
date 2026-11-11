// SPDX-License-Identifier: MIT
#pragma once
#include "imu_fusion.hpp"
#include <openvr_driver.h>
#include <stdexcept>
#include <string>

namespace psvr {
// Exposes existing timing parameters, not a different orientation algorithm.
// Missing properties preserve the exact baseline ImuConfig.
template<class Settings>
void load_imu_timing_settings(Settings& settings, ImuConfig& config) {
    vr::EVRSettingsError error=vr::VRSettingsError_None;
    const auto optional=[&](const char* key,int baseline,int lo,int hi) {
        const auto value=settings.GetInt32("driver_psvrpc",key,&error);
        if(error==vr::VRSettingsError_UnsetSettingHasNoDefault) return baseline;
        if(error!=vr::VRSettingsError_None || value<lo || value>hi)
            throw std::runtime_error(std::string("invalid IMU timing setting: ")+key);
        return value;
    };
    config.calibration_ms=optional("imuCalibrationMs",config.calibration_ms,500,10000);
    config.stale_timeout_ms=optional("imuStaleTimeoutMs",config.stale_timeout_ms,20,1000);
    config.max_sample_gap_us=optional("imuMaxSampleGapUs",config.max_sample_gap_us,1000,50000);
}
}
