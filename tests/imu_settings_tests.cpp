// SPDX-License-Identifier: MIT
#include "imu_settings.hpp"
#include <map>
#include <iostream>

struct Settings {
    std::map<std::string,int> values;
    int GetInt32(const char*,const char* key,vr::EVRSettingsError* error) {
        const auto it=values.find(key);
        *error=it==values.end() ? vr::VRSettingsError_UnsetSettingHasNoDefault : vr::VRSettingsError_None;
        return it==values.end() ? 0 : it->second;
    }
};
int main() {
    const auto check=[](bool ok){ if(!ok) throw std::runtime_error("IMU setting contract failed"); };
    Settings settings;
    psvr::ImuConfig config;
    const auto baseline=config;
    psvr::load_imu_timing_settings(settings,config);
    check(config.calibration_ms==baseline.calibration_ms);
    check(config.stale_timeout_ms==baseline.stale_timeout_ms);
    check(config.max_sample_gap_us==baseline.max_sample_gap_us);
    check(config.gyro_scale==baseline.gyro_scale);
    settings.values={{"imuCalibrationMs",9000},{"imuStaleTimeoutMs",150},{"imuMaxSampleGapUs",6000}};
    psvr::load_imu_timing_settings(settings,config);
    check(config.calibration_ms==9000 && config.stale_timeout_ms==150 && config.max_sample_gap_us==6000);
    settings.values["imuCalibrationMs"]=0;
    bool rejected=false;
    try { psvr::load_imu_timing_settings(settings,config); } catch(const std::runtime_error&) { rejected=true; }
    check(rejected);
    std::cout<<"IMU timing settings: PASS\n";
}
