// SPDX-License-Identifier: MIT
#pragma once
#include "distortion_math.hpp"

namespace psvr::optics {
// Float-SDK usability policy ONLY, not optics calibration or loader validity.
// Scales in [.25,4]; on the furthest-corner disk, both Jacobian eigenvalues
// (tangential RGB*P and radial RGB*F') must be conservatively in [.1,8].
// Absolute derivative contributions dominate tangential contributions too.
// This bounds inverse UV sensitivity by 16/.1 = 160 (scale ratio/lower
// eigenvalue) and avoids float collapse/overflow.
inline const char* sdk_profile_error(const distortion::Mapper& mapper) noexcept {
    for (const auto& p : mapper.profile().eyes) {
        if (p.scale_x < .25 || p.scale_x > 4.0 || p.scale_y < .25 || p.scale_y > 4.0)
            return "SDK numerical validation: axis scales must be .25..4";
        const double R = std::hypot(std::max(p.center.u, 1.0-p.center.u)*p.scale_x,
                                    std::max(p.center.v, 1.0-p.center.v)*p.scale_y);
        const double contribution = 2.0*std::abs(p.c)*R + 3.0*std::abs(p.b)*R*R +
                                    4.0*std::abs(p.a)*R*R*R;
        for (double rgb : p.rgb) {
            if (rgb*(p.d-contribution) < .1 || rgb*(p.d+contribution) > 8.0)
                return "SDK numerical validation: per-RGB Jacobian bounds must be .1..8";
        }
    }
    return nullptr;
}

// Half the larger adjacent float spacing bounds round-to-nearest error,
// including binade boundaries, zero and subnormals. Measured at the actual
// input magnitude, not at unit UV: expanded mappings may leave [0,1].
inline double sdk_uv_rounding_error(float u, float v) noexcept {
    const auto error = [](float x) {
        const double up = std::nextafter(x, std::numeric_limits<float>::infinity());
        const double down = std::nextafter(x, -std::numeric_limits<float>::infinity());
        return .5*std::max(up-static_cast<double>(x), static_cast<double>(x)-down);
    };
    return std::max(error(u), error(v));
}
} // namespace psvr::optics
