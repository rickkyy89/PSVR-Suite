// SPDX-License-Identifier: MIT
// Original software-only distortion foundation; NOT a PSVR optical calibration.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <optional>

namespace psvr::distortion {

struct UV { double u = 0.5; double v = 0.5; };
enum class Eye { left, right };
enum class Channel { red, green, blue };

// Eye-local UV: u increases rightwards, v downwards, origin at top left.
// No Y flip, panel/SBS conversion, aspect correction or optical units implied.
// x=(u-center.u)*scale_x, y=(v-center.v)*scale_y, r=hypot(x,y).
// r'=rgb[channel]*r*(d+c*r+b*r^2+a*r^3).
// Output UV = center + (x,y)*(r'/r)/(scale_x,scale_y).
struct EyeProfile {
    UV center{};
    double scale_x = 1.0;
    double scale_y = 1.0;
    double a = 0.0, b = 0.0, c = 0.0, d = 1.0;
    std::array<double, 3> rgb{{1.0, 1.0, 1.0}};
};
struct Profile {
    std::array<EyeProfile, 2> eyes{};
};

// Default Profile is useful synthetic identity ONLY. Call create explicitly;
// there is no unvalidated/default-constructed mapper or mutable profile access.
class Mapper {
public:
    // Deliberately bounded numerical policy, not hardware calibration limits.
    static constexpr double min_scale = 1e-3;
    static constexpr double max_scale = 100.0;
    static constexpr double max_coefficient = 100.0;
    static constexpr double min_radial_derivative = 1e-8;
    static constexpr unsigned inverse_iterations = 80;

    static std::optional<Mapper> create(const Profile& profile) noexcept {
        std::array<double, 2> radii{};
        for (std::size_t i = 0; i < profile.eyes.size(); ++i) {
            const auto& p = profile.eyes[i];
            if (!unit_uv(p.center) || !positive_bounded(p.scale_x) ||
                !positive_bounded(p.scale_y)) return std::nullopt;
            for (double coefficient : {p.a, p.b, p.c, p.d}) {
                if (!std::isfinite(coefficient) ||
                    std::abs(coefficient) > max_coefficient) return std::nullopt;
            }
            for (double factor : p.rgb) {
                if (!positive_bounded(factor)) return std::nullopt;
            }
            const double r = std::hypot(std::max(p.center.u, 1.0-p.center.u)*p.scale_x,
                                        std::max(p.center.v, 1.0-p.center.v)*p.scale_y);
            // Validate the entire enclosing disk, not just UV corners/grid.
            // F'(r)=d+2*c*r+3*b*r^2+4*a*r^3. Its extrema solve
            // F''(r)=2*c+6*b*r+12*a*r^2=0 analytically.
            if (!regular(p, 0.0) || !regular(p, r)) return std::nullopt;
            // Normalize r=R*t and coefficients before discriminant calculation.
            double A = 12.0*p.a*r*r*r;
            double B = 6.0*p.b*r*r;
            double C = 2.0*p.c*r;
            const double magnitude = std::max({std::abs(A), std::abs(B), std::abs(C)});
            if (magnitude != 0.0) {
                A /= magnitude; B /= magnitude; C /= magnitude;
                const auto check_root = [&](double numerator, double denominator) {
                    // Only divide for roots in [0,1]; avoids enormous roots
                    // when a quadratic or linear coefficient is near zero.
                    if (denominator == 0.0 ||
                        (numerator != 0.0 && std::signbit(numerator) != std::signbit(denominator)) ||
                        std::abs(numerator) > std::abs(denominator)) return true;
                    return regular(p, r*(numerator/denominator));
                };
                if (A == 0.0) {
                    if (!check_root(-C, B)) return std::nullopt;
                } else {
                    const double discriminant = std::fma(-4.0*A, C, B*B);
                    if (discriminant >= 0.0) {
                        // Stable quadratic formula, avoiding cancellation.
                        const double q = -0.5*(B + std::copysign(std::sqrt(discriminant), B));
                        if (q == 0.0) {
                            if (!check_root(-B, 2.0*A)) return std::nullopt;
                        } else if (!check_root(q, A) || !check_root(C, q)) {
                            return std::nullopt;
                        }
                    }
                }
            }
            radii[i] = r;
        }
        return Mapper(profile, radii);
    }

    const Profile& profile() const noexcept { return profile_; }

    // Forward accepts only finite source UV in the closed unit square.
    // Finite output may leave that square: NEVER clamp compositor coordinates.
    std::optional<UV> forward(Eye eye, Channel channel, UV source) const noexcept {
        const auto e = eye_index(eye), ch = channel_index(channel);
        if (!e || !ch || !unit_uv(source)) return std::nullopt;
        const auto& p = profile_.eyes[*e];
        const double du = source.u-p.center.u, dv = source.v-p.center.v;
        if (du == 0.0 && dv == 0.0) return p.center;
        const double r = std::hypot(du*p.scale_x, dv*p.scale_y);
        const double gain = polynomial(p, r)*p.rgb[*ch];
        UV result{p.center.u+du*gain, p.center.v+dv*gain};
        return finite_uv(result) ? std::optional<UV>(result) : std::nullopt;
    }

    std::optional<std::array<UV, 3>> forward_rgb(Eye eye, UV source) const noexcept {
        const auto red = forward(eye, Channel::red, source);
        const auto green = forward(eye, Channel::green, source);
        const auto blue = forward(eye, Channel::blue, source);
        if (!red || !green || !blue) return std::nullopt;
        return std::array<UV, 3>{{*red, *green, *blue}};
    }

    // Inverse accepts finite target UV (including outside [0,1]) only in the
    // channel's forward image of the source square. First reject outside the
    // supported image disk F([0,R])*rgb, then reject recovered source outside
    // the square. The enclosing disk alone is NOT the valid inverse domain.
    // A 64-epsilon relative radius/boundary allowance covers roundoff at corners;
    // only inverse boundary roundoff is snapped to [0,1]. No extrapolation.
    std::optional<UV> inverse(Eye eye, Channel channel, UV target) const noexcept {
        return inverse_with_uncertainty(eye, channel, target, 0.0);
    }

    // Explicit opt-in for quantized callers: absolute error per target UV axis.
    // Zero preserves the strict double API. Positive error uses a conservative
    // lower Jacobian bound over the enclosing disk, never a global epsilon.
    std::optional<UV> inverse_with_uncertainty(Eye eye, Channel channel, UV target,
                                              double abs_uv_error) const noexcept {
        const auto e = eye_index(eye), ch = channel_index(channel);
        if (!e || !ch || !finite_uv(target) || !std::isfinite(abs_uv_error) || abs_uv_error < 0.0)
            return std::nullopt;
        const auto& p = profile_.eyes[*e];
        const double du = target.u-p.center.u, dv = target.v-p.center.v;
        if (du == 0.0 && dv == 0.0) return p.center;
        const double image_r = std::hypot(du*p.scale_x, dv*p.scale_y);
        const double R = radii_[*e];
        const double maximum = R*polynomial(p, R)*p.rgb[*ch];
        constexpr double tolerance = 64.0*std::numeric_limits<double>::epsilon();
        const double radius_error = abs_uv_error*std::hypot(p.scale_x, p.scale_y);
        UV source_error{0.0, 0.0};
        if (abs_uv_error > 0.0) {
            const double lower = p.rgb[*ch]*(p.d - 2.0*std::abs(p.c)*R -
                3.0*std::abs(p.b)*R*R - 4.0*std::abs(p.a)*R*R*R);
            if (!(lower > 0.0) || !std::isfinite(radius_error)) return std::nullopt;
            source_error = {radius_error/(lower*p.scale_x), radius_error/(lower*p.scale_y)};
            if (!finite_uv(source_error)) return std::nullopt;
        }
        if (!std::isfinite(image_r) || image_r > maximum*(1.0+tolerance)+radius_error)
            return std::nullopt;
        // Project quantized points back onto the image disk before inversion.
        // Disk projection is nonexpansive. The inverse is 1/lower Lipschitz
        // in scaled coordinates, hence the source-square allowance above.
        const double projection = abs_uv_error > 0.0 && image_r > maximum ? maximum/image_r : 1.0;
        const double solve_r = image_r*projection;
        double lo = 0.0, hi = R;
        for (unsigned i = 0; i < inverse_iterations; ++i) {
            const double mid = lo+(hi-lo)*0.5;
            if (mid*polynomial(p, mid)*p.rgb[*ch] < solve_r) lo = mid;
            else hi = mid;
        }
        const double r = lo+(hi-lo)*0.5;
        // Divide by gain, not image_r: handles tiny radii without 0/0.
        const double gain = polynomial(p, r)*p.rgb[*ch];
        UV result{p.center.u+du*projection/gain, p.center.v+dv*projection/gain};
        const double allowance_u = tolerance+source_error.u, allowance_v = tolerance+source_error.v;
        const bool inside_allowance = finite_uv(result) && result.u >= -allowance_u &&
            result.u <= 1.0+allowance_u && result.v >= -allowance_v && result.v <= 1.0+allowance_v;
        if (!inside_allowance) {
            // Outside the fixed source allowance: the quantized path keeps
            // its B10-03 behaviour (reject; the certified search below only
            // runs for candidates that passed this filter). The strict path
            // gets a B10-04 certified edge extension instead, see below.
            if (abs_uv_error != 0.0 || !finite_uv(result)) return std::nullopt;
            // B10-04: strict-only certified edge extension for ill-conditioned
            // yet valid profiles. Double forward roundoff (~tolerance*|target|)
            // divided by a tiny radial sensitivity pushes the recovered source
            // outside the fixed 64-epsilon allowance even when the true source
            // is exactly on the edge (Finding 2: F'(r)~(r-.1007)^2+1e-7,
            // deviation ~4e-10 >> 1.4e-14). create() guarantees both Jacobian
            // eigenvalues (times positive RGB) stay above
            // min_radial_derivative everywhere, so target noise maps to a
            // bounded source deviation: only candidates inside that
            // conditioning-aware bound are even considered (beyond it the
            // profile is contractually too ill-conditioned for a double
            // inverse at that distance: explicit rejection, no extrapolation).
            // Acceptance still needs forward certification of the clamped
            // candidate within double evaluation noise (same 64-epsilon slack
            // as B10-03), so genuinely outside targets keep being rejected;
            // the extension can only convert spurious rejections into
            // certified acceptances, never loosen the domain.
            const double floor = min_radial_derivative*p.rgb[*ch];
            const double target_mag = std::max({1.0, std::abs(target.u), std::abs(target.v)});
            const double noise_radius = tolerance*target_mag*std::hypot(p.scale_x, p.scale_y);
            if (!(floor > 0.0) || !std::isfinite(floor) || !std::isfinite(noise_radius))
                return std::nullopt;
            const double extended_u = noise_radius/(floor*p.scale_x);
            const double extended_v = noise_radius/(floor*p.scale_y);
            if (!std::isfinite(extended_u) || !std::isfinite(extended_v) ||
                result.u < -extended_u || result.u > 1.0+extended_u ||
                result.v < -extended_v || result.v > 1.0+extended_v)
                return std::nullopt;
            const UV clamped{std::clamp(result.u, 0.0, 1.0), std::clamp(result.v, 0.0, 1.0)};
            const auto image = forward(eye, channel, clamped);
            if (!image) return std::nullopt;
            const double slack_u = tolerance*std::max({1.0, std::abs(target.u), std::abs(image->u)});
            const double slack_v = tolerance*std::max({1.0, std::abs(target.v), std::abs(image->v)});
            if (std::abs(image->u-target.u) <= slack_u && std::abs(image->v-target.v) <= slack_v)
                return clamped;
            return std::nullopt;
        }
        result.u = std::clamp(result.u, 0.0, 1.0);
        result.v = std::clamp(result.v, 0.0, 1.0);
        if (abs_uv_error == 0.0) return result;
        // B10-03: the Jacobian source allowance above is a necessary-only
        // coarse filter; a sensitivity overestimate must not dilate the
        // accepted target domain. Certify in the target domain that the
        // clamped in-square candidate re-maps within the caller budget, plus
        // only justified double evaluation noise (the existing 64-epsilon
        // scale times the magnitudes involved, ~1e-14 here: six orders below
        // float budgets). Per-axis clamp need not be the closest feasible
        // point for coupled axes, so a failing candidate gets a bounded
        // constrained search on the square before rejection. Only
        // forward-certified points are ever returned, so the search cannot
        // over-accept; at worst it conservatively rejects. The strict
        // (zero-uncertainty) path above is untouched.
        const auto certify = [&](UV candidate, double& out_du, double& out_dv) -> bool {
            const auto image = forward(eye, channel, candidate);
            if (!image) {
                out_du = out_dv = std::numeric_limits<double>::infinity();
                return false;
            }
            out_du = std::abs(image->u-target.u);
            out_dv = std::abs(image->v-target.v);
            const double slack_u = tolerance*std::max({1.0, std::abs(target.u), std::abs(image->u)});
            const double slack_v = tolerance*std::max({1.0, std::abs(target.v), std::abs(image->v)});
            return out_du <= abs_uv_error+slack_u && out_dv <= abs_uv_error+slack_v;
        };
        double res_u = 0.0, res_v = 0.0;
        if (certify(result, res_u, res_v)) return result;
        // Any in-square source explaining the target within budget lies
        // within ~source_error of the clamped point (1/lower Lipschitz in
        // scaled coordinates, then nonexpansive clamp), and source_error
        // overestimates that distance because lower underestimates the true
        // sensitivity. Seed steps from the allowance with a double-noise
        // floor and shrink on no improvement. Bounded, deterministic and
        // allocation-free; forward evaluations only.
        UV best = result;
        double best_excess = std::max(res_u-abs_uv_error, res_v-abs_uv_error);
        double step_u = std::max(source_error.u, tolerance);
        double step_v = std::max(source_error.v, tolerance);
        for (unsigned iter = 0; iter < 48; ++iter) {
            bool improved = false;
            for (int dir = 0; dir < 4; ++dir) {
                UV probe = best;
                if (dir == 0) probe.u = std::clamp(best.u-step_u, 0.0, 1.0);
                else if (dir == 1) probe.u = std::clamp(best.u+step_u, 0.0, 1.0);
                else if (dir == 2) probe.v = std::clamp(best.v-step_v, 0.0, 1.0);
                else probe.v = std::clamp(best.v+step_v, 0.0, 1.0);
                if (probe.u == best.u && probe.v == best.v) continue;
                double probe_u = 0.0, probe_v = 0.0;
                if (certify(probe, probe_u, probe_v)) return probe;
                if (std::isfinite(probe_u)) {
                    const double excess = std::max(probe_u-abs_uv_error, probe_v-abs_uv_error);
                    if (excess < best_excess) { best = probe; best_excess = excess; improved = true; }
                }
            }
            if (!improved) {
                step_u *= 0.5; step_v *= 0.5;
                if (step_u+step_v < tolerance) break;
            }
        }
        return std::nullopt;
    }

private:
    Profile profile_;
    std::array<double, 2> radii_;
    Mapper(const Profile& p, std::array<double, 2> r) noexcept : profile_(p), radii_(r) {}
    static bool finite_uv(UV uv) noexcept { return std::isfinite(uv.u) && std::isfinite(uv.v); }
    static bool unit_uv(UV uv) noexcept {
        return finite_uv(uv) && uv.u >= 0.0 && uv.u <= 1.0 && uv.v >= 0.0 && uv.v <= 1.0;
    }
    static bool positive_bounded(double x) noexcept {
        return std::isfinite(x) && x >= min_scale && x <= max_scale;
    }
    static double polynomial(const EyeProfile& p, double r) noexcept {
        return ((p.a*r+p.b)*r+p.c)*r+p.d;
    }
    static bool regular(const EyeProfile& p, double r) noexcept {
        const double derivative = ((4.0*p.a*r+3.0*p.b)*r+2.0*p.c)*r+p.d;
        // Conservatively reject minima too close to floating-point cancellation;
        // an absolute floor alone would be unsafe for large opposing terms.
        const double terms = std::abs(p.d) + std::abs(2.0*p.c*r) +
                             std::abs(3.0*p.b*r*r) + std::abs(4.0*p.a*r*r*r);
        const double roundoff = 32.0*std::numeric_limits<double>::epsilon()*terms;
        // F(0)=0 and F'>=floor imply F(r)/r>=floor, including its
        // limit d at zero. Thus both radial and tangential Jacobian
        // eigenvalues (times positive RGB) are nonsingular everywhere.
        return std::isfinite(derivative) && derivative >= min_radial_derivative+roundoff;
    }
    static std::optional<std::size_t> eye_index(Eye eye) noexcept {
        switch (eye) { case Eye::left: return 0; case Eye::right: return 1; }
        return std::nullopt;
    }
    static std::optional<std::size_t> channel_index(Channel channel) noexcept {
        switch (channel) {
        case Channel::red: return 0; case Channel::green: return 1; case Channel::blue: return 2;
        }
        return std::nullopt;
    }
};

} // namespace psvr::distortion
