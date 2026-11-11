// SPDX-License-Identifier: MIT
// Original synthetic tests: no hardware, OpenVR, or calibrated PSVR coefficients.
#include "../driver/distortion_math.hpp"
#include "../driver/optics_sdk.hpp"
#include <cstdlib>
#include <iostream>
#include <stdexcept>

namespace {
using namespace psvr::distortion;
unsigned checks = 0;
void check(bool condition, const char* message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
}
void near(double actual, double expected, const char* message, double tolerance = 2e-12) {
    check(std::isfinite(actual) && std::abs(actual-expected) <= tolerance, message);
}
void uv_near(const std::optional<UV>& actual, UV expected, const char* message) {
    check(actual.has_value(), message);
    near(actual->u, expected.u, message); near(actual->v, expected.v, message);
}
void roundtrips(const Mapper& mapper) {
    for (Eye eye : {Eye::left, Eye::right}) {
        for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
            for (int y = 0; y <= 20; ++y) for (int x = 0; x <= 20; ++x) {
                const UV source{x/20.0, y/20.0};
                const auto target = mapper.forward(eye, ch, source);
                check(target.has_value(), "grid forward");
                uv_near(mapper.inverse(eye, ch, *target), source, "grid roundtrip");
            }
            const UV center = mapper.profile().eyes[eye == Eye::left ? 0 : 1].center;
            const auto mapped = mapper.forward(eye, ch, center);
            check(mapped && mapped->u == center.u && mapped->v == center.v, "exact forward center");
            const auto restored = mapper.inverse(eye, ch, center);
            check(restored && restored->u == center.u && restored->v == center.v, "exact inverse center");
        }
    }
}
void identity() {
    const auto mapper = Mapper::create(Profile{});
    check(mapper.has_value(), "explicit synthetic identity valid");
    roundtrips(*mapper);
    for (Eye eye : {Eye::left, Eye::right}) for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
        for (int y = 0; y <= 20; ++y) for (int x = 0; x <= 20; ++x) {
            const UV uv{x/20.0, y/20.0};
            uv_near(mapper->forward(eye, ch, uv), uv, "identity grid/corners");
        }
    }
    // Subnormal offsets around a zero center must not divide by zero.
    Profile p; p.eyes[0].center = {0.0, 0.0};
    const auto zero = Mapper::create(p);
    const double tiny = std::numeric_limits<double>::denorm_min();
    const auto inverse = zero->inverse(Eye::left, Channel::red, {tiny, 0.0});
    check(inverse && inverse->u == tiny && inverse->v == 0.0, "subnormal identity inverse");
}
void synthetic_profiles() {
    Profile p;
    p.eyes[0].center = {0.31, 0.63}; p.eyes[0].scale_x = 1.7; p.eyes[0].scale_y = 0.8;
    p.eyes[1].center = {0.68, 0.42}; p.eyes[1].scale_x = 0.9; p.eyes[1].scale_y = 1.4;
    // Identity remains identity even for asymmetric centers/axis scales.
    auto mapper = Mapper::create(p);
    check(mapper.has_value(), "asymmetric identity valid");
    uv_near(mapper->forward(Eye::right, Channel::blue, {0.1, 0.8}), {0.1, 0.8}, "asymmetric identity");
    roundtrips(*mapper);
    for (auto& eye : p.eyes) {
        eye.a = 0.025; eye.b = 0.05; eye.c = -0.02; eye.d = 0.95;
        eye.rgb = {{1.03, 1.0, 0.97}};
    }
    mapper = Mapper::create(p);
    check(mapper.has_value(), "known mixed-sign polynomial valid");
    roundtrips(*mapper);
    const UV source{0.91, 0.13};
    const auto channels = mapper->forward_rgb(Eye::left, source);
    check(channels.has_value(), "RGB output available");
    const double r = std::hypot((source.u-0.31)*1.7, (source.v-0.63)*0.8);
    // Independent expanded reference guards coefficient ordering and axis convention.
    const double gain = 0.95-0.02*r+0.05*r*r+0.025*r*r*r;
    for (std::size_t ch = 0; ch < 3; ++ch) {
        near((*channels)[ch].u, 0.31+(source.u-0.31)*gain*p.eyes[0].rgb[ch], "RGB radial multiplier U");
        near((*channels)[ch].v, 0.63+(source.v-0.63)*gain*p.eyes[0].rgb[ch], "RGB radial multiplier V");
    }
    check((*channels)[0].u > (*channels)[1].u && (*channels)[1].u > (*channels)[2].u, "distinct RGB");
    p = Profile{}; for (auto& eye : p.eyes) eye.d = 2.0;
    mapper = Mapper::create(p);
    uv_near(mapper->forward(Eye::left, Channel::red, {0.0, 1.0}), {-0.5, 1.5}, "no forward clamp");
    uv_near(mapper->inverse(Eye::left, Channel::red, {-0.5, 1.5}), {0.0, 1.0}, "inverse outside unit target");
    check(!mapper->inverse(Eye::left, Channel::red, {1.6, 0.5}), "disk inside but square outside rejected");
    check(!mapper->inverse(Eye::left, Channel::red, {3.0, 3.0}), "outside supported image radius");
    p = Profile{}; for (auto& eye : p.eyes) eye.d = 0.5;
    mapper = Mapper::create(p);
    check(!mapper->inverse(Eye::left, Channel::red, {0.9, 0.5}), "unit target outside shrunken image rejected");
    const auto t = mapper->forward(Eye::right, Channel::green, {0.12, 0.73});
    const auto first = mapper->inverse(Eye::right, Channel::green, *t);
    for (int i = 0; i < 32; ++i) {
        const auto next = mapper->inverse(Eye::right, Channel::green, *t);
        check(next && next->u == first->u && next->v == first->v, "deterministic bounded inverse");
    }
}
void analytic_validation() {
    Profile p;
    // F'(r)=(r-.351)^2-1e-6: a narrow negative well missed by coarse sampling.
    // Endpoints are positive, quadratic minimum at .351 must be checked.
    p.eyes[0].d = 0.351*0.351-1e-6;
    p.eyes[0].c = -0.351; p.eyes[0].b = 1.0/3.0;
    check(!Mapper::create(p), "analytic linear extrema reject narrow derivative well");
    // Cubic F'=(r-.2)*(r-.4)*(r+1): positive endpoints, negative interior.
    p.eyes[0].a = 0.25; p.eyes[0].b = 0.4/3.0;
    p.eyes[0].c = -0.26; p.eyes[0].d = 0.08;
    check(!Mapper::create(p), "analytic quadratic extrema reject cubic fold");
    p = Profile{}; p.eyes[0].center = {0.0, 0.0};
    // F'=(r-.5)^2: nonsingular requirement rejects stationary radial map.
    p.eyes[0].b = 1.0/3.0; p.eyes[0].c = -0.5; p.eyes[0].d = 0.25;
    check(!Mapper::create(p), "interior zero derivative rejected");
    p = Profile{}; p.eyes[0].d = 0.0;
    check(!Mapper::create(p), "singular center rejected");
    p.eyes[0].d = 1e-12;
    check(!Mapper::create(p), "numerically near-singular center rejected");
    p.eyes[0].d = 2.0*Mapper::min_radial_derivative;
    check(Mapper::create(p).has_value(), "small safely positive derivative accepted");
    p = Profile{}; p.eyes[0].c = -1.0;
    check(!Mapper::create(p), "negative endpoint derivative rejected");
    // Exactly representable repeated quadratic root at .25:
    // F'=(r-.25)^3+.115625 remains positive on [0,R].
    p = Profile{}; p.eyes[0].a = 0.25; p.eyes[0].b = -0.25;
    p.eyes[0].c = 0.09375; p.eyes[0].d = 0.1;
    check(Mapper::create(p).has_value(), "repeated extrema root valid");
    p = Profile{}; p.eyes[0].a = 0.25;
    check(Mapper::create(p).has_value(), "quadratic q zero root at center valid");
    p.eyes[0].c = 0.5;
    check(Mapper::create(p).has_value(), "quadratic no real extrema valid");
    p = Profile{}; p.eyes[0].a = 1e-300; p.eyes[0].b = 0.01;
    check(Mapper::create(p).has_value(), "near linear quadratic avoids huge root");
    p.eyes[0].a = -1e-300;
    check(Mapper::create(p).has_value(), "huge positive irrelevant root ignored");
    p = Profile{}; p.eyes[0].a = -0.01; p.eyes[0].b = 0.02; p.eyes[0].c = 0.1;
    const auto negative_cubic = Mapper::create(p);
    check(negative_cubic.has_value(), "quadratic root outside interval valid");
    roundtrips(*negative_cubic);
    p.eyes[0].d = 0.0;
    check(negative_cubic->profile().eyes[0].d == 1.0, "mapper owns immutable validated copy");
}
void invalid_profiles_and_inputs() {
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    for (std::size_t eye = 0; eye < 2; ++eye) {
        for (double value : {nan, inf, -inf}) {
            for (int field = 0; field < 11; ++field) {
                Profile p; auto& e = p.eyes[eye];
                double* fields[] = {&e.center.u, &e.center.v, &e.scale_x, &e.scale_y,
                                    &e.a, &e.b, &e.c, &e.d, &e.rgb[0], &e.rgb[1], &e.rgb[2]};
                *fields[field] = value;
                check(!Mapper::create(p), "all nonfinite profile fields rejected");
            }
        }
        for (double value : {0.0, -1.0, 1e-4, 101.0}) {
            for (int field = 0; field < 5; ++field) {
                Profile p; auto& e = p.eyes[eye];
                double* fields[] = {&e.scale_x, &e.scale_y, &e.rgb[0], &e.rgb[1], &e.rgb[2]};
                *fields[field] = value;
                check(!Mapper::create(p), "invalid bounded positive field rejected");
            }
        }
        for (double value : {-0.01, 1.01}) for (int axis = 0; axis < 2; ++axis) {
            Profile p;
            (axis == 0 ? p.eyes[eye].center.u : p.eyes[eye].center.v) = value;
            check(!Mapper::create(p), "out-of-square center rejected");
        }
        for (double value : {-101.0, 101.0}) for (int field = 0; field < 4; ++field) {
            Profile p; auto& e = p.eyes[eye];
            double* fields[] = {&e.a, &e.b, &e.c, &e.d}; *fields[field] = value;
            check(!Mapper::create(p), "coefficient bounds rejected");
        }
    }
    auto mapper = Mapper::create(Profile{});
    for (UV uv : {UV{nan, 0.5}, UV{0.5, nan}, UV{inf, 0.5}, UV{0.5, -inf},
                  UV{-0.01, 0.5}, UV{1.01, 0.5}, UV{0.5, -0.01}, UV{0.5, 1.01}}) {
        check(!mapper->forward(Eye::left, Channel::red, uv), "invalid forward input rejected");
        check(!mapper->forward_rgb(Eye::left, uv), "invalid RGB input rejected");
        check(!mapper->inverse(Eye::left, Channel::red, uv), "invalid identity inverse input rejected");
    }
    for (int index : {-1, 3, 100}) {
        const auto channel = static_cast<Channel>(index);
        check(!mapper->forward(Eye::left, channel, {}), "bad forward channel");
        check(!mapper->inverse(Eye::left, channel, {}), "bad inverse channel");
    }
    for (int index : {-1, 2, 100}) {
        const auto eye = static_cast<Eye>(index);
        check(!mapper->forward(eye, Channel::red, {}), "bad forward eye");
        check(!mapper->forward_rgb(eye, {}), "bad RGB eye");
        check(!mapper->inverse(eye, Channel::red, {}), "bad inverse eye");
    }
    // Maximum policy bounds remain finite; boundary center and extreme scales work.
    Profile p;
    for (auto& e : p.eyes) {
        e.center = {0.0, 1.0}; e.scale_x = Mapper::max_scale; e.scale_y = Mapper::min_scale;
        e.a = e.b = e.c = e.d = Mapper::max_coefficient;
        e.rgb = {{Mapper::max_scale, 1.0, Mapper::min_scale}};
    }
    mapper = Mapper::create(p);
    check(mapper.has_value(), "bounded extreme profile valid");
    roundtrips(*mapper);
    check(!mapper->inverse(Eye::left, Channel::red, {std::numeric_limits<double>::max(), 0.5}),
          "overflowing inverse radius rejected");
}
void quantized_target_budget_b10_03() {
    // B10-03: the Jacobian source allowance is a necessary-only coarse
    // filter; acceptance needs forward certification inside the caller
    // budget. Identity with SDK-limit axes .25,4 must reject float targets
    // just outside the edge, while the four shipped radial profiles stay
    // accepted by both math and SDK policy with no acceptance beyond budget.
    constexpr double slope = 64.0*std::numeric_limits<double>::epsilon();
    const auto within_budget = [slope](const Mapper& mapper, Eye eye, Channel channel,
                                  UV target, double budget) {
        const auto recovered = mapper.inverse_with_uncertainty(eye, channel, target, budget);
        if (!recovered) return true; // Rejection never accepts beyond budget.
        const auto image = mapper.forward(eye, channel, *recovered);
        if (!image) return false;
        const double slack_u = slope*std::max({1.0, std::abs(target.u), std::abs(image->u)});
        const double slack_v = slope*std::max({1.0, std::abs(target.v), std::abs(image->v)});
        return std::abs(image->u-target.u) <= budget+slack_u &&
               std::abs(image->v-target.v) <= budget+slack_v;
    };
    Profile anisotropic;
    for (auto& eye : anisotropic.eyes) {
        eye.center = {0.5, 0.5}; eye.scale_x = 0.25; eye.scale_y = 4.0;
    }
    const auto identity = Mapper::create(anisotropic);
    check(identity.has_value(), "B10-03 anisotropic identity valid");
    check(psvr::optics::sdk_profile_error(*identity) == nullptr, "B10-03 anisotropic identity SDK usable");
    for (Eye eye : {Eye::left, Eye::right}) for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
        const double edge_budget = psvr::optics::sdk_uv_rounding_error(1.0f, 0.5f);
        const auto on_edge = identity->inverse_with_uncertainty(eye, ch, {1.0, 0.5}, edge_budget);
        check(on_edge && on_edge->u == 1.0 && on_edge->v == 0.5, "B10-03 exact edge still accepted");
        float step = 1.0f;
        for (int k = 1; k <= 20; ++k) {
            step = std::nextafter(step, 2.0f);
            const double budget = psvr::optics::sdk_uv_rounding_error(step, 0.5f);
            const UV target{static_cast<double>(step), 0.5};
            // Reported defect window: pre-fix accepted k=1..8 with residual
            // up to 16x the budget; all must now be rejected.
            if (k <= 8) check(!identity->inverse_with_uncertainty(eye, ch, target, budget),
                              "B10-03 anisotropic identity rejects ULP outside edge");
            check(within_budget(*identity, eye, ch, target, budget),
                  "B10-03 anisotropic identity never accepts beyond budget");
        }
        // A genuinely quantized interior source must stay accepted.
        const auto interior = identity->forward(eye, ch, {0.75, 0.25});
        check(interior.has_value(), "B10-03 interior forward available");
        const float iu = static_cast<float>(interior->u), iv = static_cast<float>(interior->v);
        const double ibudget = psvr::optics::sdk_uv_rounding_error(iu, iv);
        const auto back = identity->inverse_with_uncertainty(eye, ch, {iu, iv}, ibudget);
        check(back.has_value(), "B10-03 quantized interior still accepted");
        check(within_budget(*identity, eye, ch, {iu, iv}, ibudget),
              "B10-03 quantized interior maps within budget");
    }
    // Coefficients mirror configurations/optics_radial_{mild,medium,
    // stronger}_20261014.json and optics_radial_peripheral.json exactly.
    struct KnownProfile { double a; double b; };
    const KnownProfile shipped[] = {{0.0, 0.2}, {0.0, 0.4}, {0.0, 0.6}, {0.2, 0.4}};
    for (const auto& known : shipped) {
        Profile candidate;
        for (auto& eye : candidate.eyes) {
            eye.center = {0.5, 0.5};
            eye.scale_x = 0.8888888888888888; eye.scale_y = 1.0;
            eye.a = known.a; eye.b = known.b; eye.c = 0.0; eye.d = 1.0;
            eye.rgb = {{1.0, 1.0, 1.0}};
        }
        const auto mapper = Mapper::create(candidate);
        check(mapper.has_value(), "B10-03 shipped profile still valid");
        check(psvr::optics::sdk_profile_error(*mapper) == nullptr, "B10-03 shipped profile still SDK usable");
        for (Eye eye : {Eye::left, Eye::right}) for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
            const auto edge = mapper->forward(eye, ch, {1.0, 0.5});
            check(edge.has_value(), "B10-03 shipped edge image available");
            float step = static_cast<float>(edge->u);
            for (int k = 1; k <= 20; ++k) {
                step = std::nextafter(step, std::numeric_limits<float>::infinity());
                const double budget = psvr::optics::sdk_uv_rounding_error(step, 0.5f);
                check(within_budget(*mapper, eye, ch, {static_cast<double>(step), 0.5}, budget),
                      "B10-03 shipped profile never accepts beyond budget");
            }
        }
    }
}
void strict_edge_roundoff_b10_04() {
    // B10-04: strict inverse(forward(edge)) on ill-conditioned yet valid
    // profiles. The fixed 64-epsilon source allowance cannot cover double
    // forward roundoff amplified by 1/F' (~1e7 for the Finding-2 profile
    // below, deviation ~4e-10 >> 1.4e-14); the strict path now carries a
    // certified edge extension: a conditioning-aware bound from the
    // guaranteed min_radial_derivative floor, plus forward certification of
    // the clamped candidate within double evaluation noise (same 64-epsilon
    // slack as B10-03). The quantized path is untouched. Well-conditioned
    // and shipped profiles keep zero spurious edge rejections; the
    // Finding-2 synthetic roundtrips with certified residuals. SDK policy
    // still rejects that family for the float path (contractual boundary,
    // unchanged): 2997/2997 math-accepted family members SDK-rejected.
    constexpr double slope = 64.0*std::numeric_limits<double>::epsilon();
    const auto strict_certified = [slope](const Mapper& mapper, Eye eye, Channel channel,
                                     UV target, UV recovered) {
        const auto image = mapper.forward(eye, channel, recovered);
        if (!image) return false;
        const double slack_u = slope*std::max({1.0, std::abs(target.u), std::abs(image->u)});
        const double slack_v = slope*std::max({1.0, std::abs(target.v), std::abs(image->v)});
        return std::abs(image->u-target.u) <= slack_u &&
               std::abs(image->v-target.v) <= slack_v;
    };
    // Border ring of the 21x21 grid: the only sources exercising edge roundoff.
    Profile plain;
    Profile anisotropic;
    for (auto& eye : anisotropic.eyes) {
        eye.center = {0.5, 0.5}; eye.scale_x = 0.25; eye.scale_y = 4.0;
    }
    Profile mixed;
    mixed.eyes[0].center = {0.31, 0.63}; mixed.eyes[0].scale_x = 1.7; mixed.eyes[0].scale_y = 0.8;
    mixed.eyes[1].center = {0.68, 0.42}; mixed.eyes[1].scale_x = 0.9; mixed.eyes[1].scale_y = 1.4;
    for (auto& eye : mixed.eyes) {
        eye.a = 0.025; eye.b = 0.05; eye.c = -0.02; eye.d = 0.95;
        eye.rgb = {{1.03, 1.0, 0.97}};
    }
    const Profile* well[] = {&plain, &anisotropic, &mixed};
    for (const Profile* candidate : well) {
        const auto mapper = Mapper::create(*candidate);
        check(mapper.has_value(), "B10-04 well-conditioned profile valid");
        for (Eye eye : {Eye::left, Eye::right}) for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
            for (int k = 0; k <= 20; ++k) {
                const UV border[] = {{k/20.0, 0.0}, {k/20.0, 1.0}, {0.0, k/20.0}, {1.0, k/20.0}};
                for (const UV source : border) {
                    const auto target = mapper->forward(eye, ch, source);
                    check(target.has_value(), "B10-04 well-conditioned edge forward");
                    const auto back = mapper->inverse(eye, ch, *target);
                    check(back.has_value(), "B10-04 well-conditioned edge no spurious rejection");
                    uv_near(back, source, "B10-04 well-conditioned edge roundtrip");
                    check(strict_certified(*mapper, eye, ch, *target, *back),
                          "B10-04 well-conditioned edge certified");
                }
            }
        }
    }
    // Coefficients mirror configurations/optics_radial_{mild,medium,
    // stronger}_20261014.json and optics_radial_peripheral.json exactly.
    struct KnownProfile { double a; double b; };
    const KnownProfile shipped[] = {{0.0, 0.2}, {0.0, 0.4}, {0.0, 0.6}, {0.2, 0.4}};
    for (const auto& known : shipped) {
        Profile candidate;
        for (auto& eye : candidate.eyes) {
            eye.center = {0.5, 0.5};
            eye.scale_x = 0.8888888888888888; eye.scale_y = 1.0;
            eye.a = known.a; eye.b = known.b; eye.c = 0.0; eye.d = 1.0;
            eye.rgb = {{1.0, 1.0, 1.0}};
        }
        const auto mapper = Mapper::create(candidate);
        check(mapper.has_value(), "B10-04 shipped profile still valid");
        check(psvr::optics::sdk_profile_error(*mapper) == nullptr, "B10-04 shipped profile still SDK usable");
        for (Eye eye : {Eye::left, Eye::right}) for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
            for (int k = 0; k <= 20; ++k) {
                const UV border[] = {{k/20.0, 0.0}, {k/20.0, 1.0}, {0.0, k/20.0}, {1.0, k/20.0}};
                for (const UV source : border) {
                    const auto target = mapper->forward(eye, ch, source);
                    check(target.has_value(), "B10-04 shipped edge forward");
                    const auto back = mapper->inverse(eye, ch, *target);
                    check(back.has_value(), "B10-04 shipped edge no spurious rejection");
                    uv_near(back, source, "B10-04 shipped edge roundtrip");
                    check(strict_certified(*mapper, eye, ch, *target, *back),
                          "B10-04 shipped edge certified");
                }
            }
        }
    }
    // Finding-2 synthetic: F'(r)~(r-.1007)^2+1e-7, strictly positive, valid.
    // Pre-fix the strict inverse of forward({1,.5}) returned nullopt.
    Profile ill;
    ill.eyes[0].center = {0.89929999999999999, 0.5};
    ill.eyes[0].scale_x = 1.0; ill.eyes[0].scale_y = 1.0;
    ill.eyes[0].a = 0.0; ill.eyes[0].b = 1.0/3.0;
    ill.eyes[0].c = -0.10070000000000001; ill.eyes[0].d = 0.010140590000000001;
    const auto ill_mapper = Mapper::create(ill);
    check(ill_mapper.has_value(), "B10-04 ill-conditioned profile still valid");
    check(psvr::optics::sdk_profile_error(*ill_mapper) != nullptr,
          "B10-04 ill-conditioned profile still rejected by SDK policy");
    for (Channel ch : {Channel::red, Channel::green, Channel::blue}) {
        const auto target = ill_mapper->forward(Eye::left, ch, {1.0, 0.5});
        check(target.has_value(), "B10-04 ill-conditioned edge forward");
        const auto back = ill_mapper->inverse(Eye::left, ch, *target);
        check(back.has_value(), "B10-04 ill-conditioned edge now accepted");
        near(back->u, 1.0, "B10-04 ill-conditioned edge source U", 1e-9);
        near(back->v, 0.5, "B10-04 ill-conditioned edge source V", 1e-9);
        check(strict_certified(*ill_mapper, Eye::left, ch, *target, *back),
              "B10-04 ill-conditioned edge certified");
        // Full grid on the ill-conditioned eye: no spurious rejections,
        // every acceptance forward-certified, source error within the
        // family scale (pre-fix max 4.36e-9 over the returned subset).
        for (int y = 0; y <= 20; ++y) for (int x = 0; x <= 20; ++x) {
            const UV source{x/20.0, y/20.0};
            const auto grid_target = ill_mapper->forward(Eye::left, ch, source);
            check(grid_target.has_value(), "B10-04 ill-conditioned grid forward");
            const auto grid_back = ill_mapper->inverse(Eye::left, ch, *grid_target);
            check(grid_back.has_value(), "B10-04 ill-conditioned grid no spurious rejection");
            near(grid_back->u, source.u, "B10-04 ill-conditioned grid source U", 1e-8);
            near(grid_back->v, source.v, "B10-04 ill-conditioned grid source V", 1e-8);
            check(strict_certified(*ill_mapper, Eye::left, ch, *grid_target, *grid_back),
                  "B10-04 ill-conditioned grid certified");
        }
    }
}
} // namespace

int main() {
    try {
        identity(); synthetic_profiles(); analytic_validation(); invalid_profiles_and_inputs();
        quantized_target_budget_b10_03(); strict_edge_roundoff_b10_04();
        std::cout << "PASS distortion synthetic tests: " << checks << " checks\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& e) {
        std::cerr << "FAIL after " << checks << " checks: " << e.what() << '\n';
        return EXIT_FAILURE;
    }
}
