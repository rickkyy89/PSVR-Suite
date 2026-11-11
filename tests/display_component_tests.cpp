// SPDX-License-Identifier: MIT
#include "display_component.hpp"
#include <iostream>
#include <type_traits>
#include <vector>

namespace {
void check(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
template<class F> void rejected(F action, const char* message) {
    bool threw = false;
    try { action(); } catch (const std::exception&) { threw = true; }
    check(threw,message);
}
psvr::DisplayConfig fixture() {
    // Synthetic test data, not a real selected monitor or calibrated projection.
    psvr::DisplayConfig c;
    c.enabled = true; c.monitor_path = "synthetic-exact-monitor-path";
    c.desktop_x = -1920; c.desktop_y = -120;
    c.refresh_numerator = 120000; c.refresh_denominator = 1001;
    c.render_width = 1100; c.render_height = 1200;
    c.projection[0] = {-1.2f,0.9f,-0.8f,1.1f};
    c.projection[1] = {-0.9f,1.2f,-1.1f,0.8f};
    return c;
}
void diagnostic_tests() {
    using namespace psvr;
    check(!fixture().diagnostic_sink,"pure tests default to no diagnostic sink");
    std::vector<std::string> lines;
    auto c = fixture();
    c.diagnostic_sink = [&](const std::string& line) { lines.push_back(line); };
    const auto count = [&](const char* text) {
        std::size_t n = 0;
        for (const auto& line : lines) if (line.find(text) != std::string::npos) ++n;
        return n;
    };
    {
        DisplayComponent traced(c);
        traced.diagnostic_activation();
        vr::HmdVector2_t inverse{{42,43}};
        // Exhaust normal inverse samples before first failure: a separate global
        // failure budget must still capture raw channel 4, without accepting it.
        for (int i=0; i<8; ++i)
            check(traced.ComputeInverseDistortion(&inverse,vr::Eye_Right,2,0.25f,-0.f),"traced valid identity inverse");
        inverse = {{42,43}};
        check(!traced.ComputeInverseDistortion(&inverse,vr::Eye_Left,4,0.25f,0.75f),"raw channel 4 still rejected");
        check(inverse.v[0] == 42 && inverse.v[1] == 43,"traced failure leaves output unchanged");
        check(lines.back().find("eye=0 channel=4 pointer_null=0") != std::string::npos &&
              lines.back().find("success=0 reason=invalid_channel output=unchanged") != std::string::npos,
              "diagnostic preserves raw invalid channel and failure reason");
        check(lines.back().find("u=0.25/0x") != std::string::npos &&
              lines.back().find("/bits=0x3e800000") != std::string::npos,
              "UV logged in decimal, hexfloat and exact float bits");
        check(lines[1].find("v=-0/-0x0") != std::string::npos &&
              lines[1].find("/bits=0x80000000") != std::string::npos,"negative zero bits retained");
        check(!traced.ComputeInverseDistortion(nullptr,vr::Eye_Left,0,0,0),"traced null output rejected");
        check(!traced.ComputeInverseDistortion(&inverse,static_cast<vr::EVREye>(7),0,0,0),"traced raw invalid eye rejected");
        check(!traced.ComputeInverseDistortion(&inverse,vr::Eye_Left,0,std::numeric_limits<float>::quiet_NaN(),0),
              "traced nonfinite rejected");
        for (int i=0; i<1000; ++i) {
            const auto forward = traced.ComputeDistortion(vr::Eye_Right,-0.1f,1.1f);
            check(forward.rfRed[0] == -0.1f && forward.rfGreen[1] == 1.1f && forward.rfBlue[1] == 1.1f,
                  "tracing does not clamp identity forward");
            check(traced.ComputeInverseDistortion(&inverse,vr::Eye_Left,1,-0.1f,1.1f) &&
                  inverse.v[0] == -0.1f && inverse.v[1] == 1.1f,"tracing preserves inverse outputs");
            float l=0, r=0, t=0, b=0;
            traced.GetProjectionRaw(vr::Eye_Right,&l,&r,&t,&b);
            check(l == c.projection[1].left && b == c.projection[1].bottom,"tracing preserves projection");
            check(!traced.ComputeInverseDistortion(&inverse,vr::Eye_Right,4u+static_cast<uint32_t>(i),0,0),
                  "varying invalid channels cannot create unbounded per-value budgets");
        }
        check(count("ComputeDistortion ") == 8 && count("GetProjectionRaw ") == 8 &&
              count("ComputeInverseDistortion ") == 16,"global first-eight call/failure caps; no repeated-call spam");
        check(count("reason=null_output") == 1 && count("eye=7 channel=0") == 1 && count("reason=nonfinite_uv") == 1,
              "failure diagnostics cover original validation reasons");
    }
    check(lines.size() == 34 && count("summary forward=1000 inverse=2012 projection=1000 inverse_failures=1004") == 1,
          "one bounded destructor summary reports total counts");
    check(count("SDK=2.15.6 frozen bounds=-1920,-120,1920,1080 render_per_eye=1100x1200") == 1 &&
          count("identity=1") == 1 && count("projection_eye=1") == 1,"activation fingerprint and frozen config evidence");

    c = fixture();
    c.diagnostic_sink = [](const std::string&) { throw std::runtime_error("synthetic sink failure"); };
    {
        DisplayComponent throwing(c);
        throwing.diagnostic_activation();
        const auto f = throwing.ComputeDistortion(vr::Eye_Left,0.25f,0.75f);
        check(f.rfRed[0] == 0.25f && f.rfGreen[1] == 0.75f && f.rfBlue[0] == 0.25f,"throwing sink preserves forward");
        vr::HmdVector2_t inv{{42,43}};
        check(throwing.ComputeInverseDistortion(&inv,vr::Eye_Right,2,0.25f,0.75f) &&
              inv.v[0] == 0.25f && inv.v[1] == 0.75f,"throwing sink preserves inverse success");
        check(!throwing.ComputeInverseDistortion(&inv,vr::Eye_Left,4,0,0) && inv.v[0] == 0.25f,
              "throwing sink preserves inverse failure");
        float l=0,r=0,t=0,b=0;
        throwing.GetProjectionRaw(vr::Eye_Left,&l,&r,&t,&b);
        check(l == c.projection[0].left && b == c.projection[0].bottom,"throwing sink preserves projection");
        throwing.GetProjectionRaw(static_cast<vr::EVREye>(9),nullptr,&r,nullptr,&b);
        check(r == 0 && b == 0,"throwing sink preserves invalid projection and nullable outputs");
        const auto invalid = throwing.ComputeDistortion(static_cast<vr::EVREye>(9),0,0);
        check(invalid.rfGreen[0] == -1,"throwing sink preserves forward failure sentinel");
    } // Destructor sink failure must also be contained.

    // Nonidentity synthetic profile: compare traced and untraced SDK channel
    // outputs, then mutate the source config to check copied mapper ownership.
    distortion::Profile profile;
    profile.eyes[0].rgb = {{1.01,1.02,1.03}};
    profile.eyes[1].rgb = {{1.04,1.05,1.06}};
    c = fixture(); c.optical_mode = OpticalMode::profile;
    c.optical_profile_name = "synthetic"; c.optical_mapper = distortion::Mapper::create(profile);
    DisplayComponent reference(c);
    c.diagnostic_sink = [](const std::string&) { throw 1; };
    DisplayComponent copied(c);
    c.optical_mapper.reset(); c.optical_mode = OpticalMode::identity; c.projection[0] = {};
    for (const auto eye : {vr::Eye_Left,vr::Eye_Right}) {
        const auto expected = reference.ComputeDistortion(eye,0.25f,0.75f);
        const auto actual = copied.ComputeDistortion(eye,0.25f,0.75f);
        for (int i=0; i<2; ++i)
            check(actual.rfRed[i] == expected.rfRed[i] && actual.rfGreen[i] == expected.rfGreen[i] &&
                  actual.rfBlue[i] == expected.rfBlue[i],"copied config forward mapping unchanged");
        for (uint32_t channel=0; channel<3; ++channel) {
            vr::HmdVector2_t a{}, b{};
            check(reference.ComputeInverseDistortion(&a,eye,channel,0.25f,0.75f) &&
                  copied.ComputeInverseDistortion(&b,eye,channel,0.25f,0.75f) &&
                  a.v[0] == b.v[0] && a.v[1] == b.v[1],"copied config SDK channel mapping unchanged");
        }
    }
}
}
int main() {
    try {
        using namespace psvr;
        static_assert(!std::is_abstract<DisplayComponent>::value,"implement every SDK _003 virtual");
        diagnostic_tests();
        const auto good = fixture();
        check(!good.validation_error(),"valid explicit config");
        check(std::abs(good.refresh_hz()-119.88011988011988) < 1e-10,"refresh derived from rational, not truncated");
        DisplayConfig defaults;
        check(!defaults.enabled && defaults.validation_error(),"defaults disabled and intentionally incomplete");
        int calls = 0;
        const DisplayVerifier accept = [&](const DisplayConfig& c) {
            ++calls; check(c.monitor_path == good.monitor_path,"verifier receives exact path");
            check(c.desktop_x == -1920 && c.refresh_denominator == 1001,"verifier receives exact mode");
            return true; // synthetic verifier only; never used in production
        };
        check(!make_verified_display(defaults,accept) && calls == 0,"disabled must not invoke verifier");
        rejected([&] { make_verified_display(good,{}); },"missing verifier fails closed");
        rejected([&] { make_verified_display(good,[](const DisplayConfig&) { return false; }); },"rejected hardware fails closed");
        rejected([&] { make_verified_display(good,[](const DisplayConfig&) -> bool { throw std::runtime_error("inventory failure"); }); },"inventory exception fails closed");
        auto display = make_verified_display(good,accept);
        check(display && calls == 1,"valid opt-in and verification constructs display");
        check(display->IsDisplayOnDesktop() && display->IsDisplayRealDisplay(),"extended real display");
        auto native = good;
        native.native_direct_mode = true;
        rejected([&] { DisplayComponent invalid(native); },"native mode cannot omit real IDs/query");
        native.edid_vendor_id = 0xd94d; native.edid_product_id = 0x6a04;
        bool desktop_present = true;
        native.desktop_membership = [&] { return desktop_present; };
        DisplayComponent direct(native);
        check(direct.IsDisplayOnDesktop(),"request cannot hide a still-visible desktop display");
        desktop_present = false;
        check(!direct.IsDisplayOnDesktop() && direct.IsDisplayRealDisplay(),"actual desktop removal reflected");
        native.desktop_membership = []() -> bool { throw std::runtime_error("query failed"); };
        DisplayComponent failed_query(native);
        check(failed_query.IsDisplayOnDesktop(),"query failure cannot falsely report desktop removal");
        int32_t x = 0, y = 0; uint32_t w = 0, h = 0;
        display->GetWindowBounds(&x,&y,&w,&h);
        check(x == -1920 && y == -120 && w == 1920 && h == 1080,"signed desktop bounds");
        display->GetRecommendedRenderTargetSize(&w,&h);
        check(w == 1100 && h == 1200,"render size is per-eye, independent of viewport");
        uint32_t vx = 0, vy = 1;
        display->GetEyeOutputViewport(vr::Eye_Left,&vx,&vy,&w,&h);
        check(vx == 0 && vy == 0 && w == 960 && h == 1080,"left half viewport");
        display->GetEyeOutputViewport(vr::Eye_Right,&vx,&vy,&w,&h);
        check(vx == 960 && vy == 0 && w == 960 && h == 1080,"right half viewport");
        const auto bad_eye = static_cast<vr::EVREye>(2);
        display->GetEyeOutputViewport(bad_eye,&vx,&vy,&w,&h);
        check(w == 0 && h == 0,"invalid eye does not become right eye");
        for (const auto eye : {vr::Eye_Left,vr::Eye_Right}) {
            float l = 0, r = 0, t = 0, b = 0;
            display->GetProjectionRaw(eye,&l,&r,&t,&b);
            const auto& p = good.projection[eye == vr::Eye_Left ? 0 : 1];
            check(l == p.left && r == p.right && t == p.top && b == p.bottom,"asymmetric eye projection unchanged, SDK top/bottom signs");
            for (const auto uv : {std::array<float,2>{0,0}, {1,1}, {0.25f,0.75f}, {-0.1f,1.1f}}) {
                const auto forward = display->ComputeDistortion(eye,uv[0],uv[1]);
                check(forward.rfRed[0] == uv[0] && forward.rfRed[1] == uv[1] &&
                      forward.rfGreen[0] == uv[0] && forward.rfGreen[1] == uv[1] &&
                      forward.rfBlue[0] == uv[0] && forward.rfBlue[1] == uv[1],"diagnostic identity all channels");
                for (uint32_t channel = 0; channel < 3; ++channel) {
                    vr::HmdVector2_t inverse{};
                    check(display->ComputeInverseDistortion(&inverse,eye,channel,uv[0],uv[1]),"explicit identity inverse succeeds");
                    check(inverse.v[0] == uv[0] && inverse.v[1] == uv[1],"inverse matches forward without clamping");
                }
            }
        }
        vr::HmdVector2_t inverse{{42,43}};
        const auto nan = std::numeric_limits<float>::quiet_NaN();
        const auto inf = std::numeric_limits<float>::infinity();
        check(!display->ComputeInverseDistortion(nullptr,vr::Eye_Left,0,0,0),"null inverse output");
        check(!display->ComputeInverseDistortion(&inverse,bad_eye,0,0,0),"invalid inverse eye");
        check(!display->ComputeInverseDistortion(&inverse,vr::Eye_Left,3,0,0),"invalid inverse channel");
        check(!display->ComputeInverseDistortion(&inverse,vr::Eye_Left,0,nan,0),"NaN inverse U");
        check(!display->ComputeInverseDistortion(&inverse,vr::Eye_Left,0,0,inf),"infinite inverse V");
        check(inverse.v[0] == 42 && inverse.v[1] == 43,"inverse failure leaves output untouched");
        display->GetWindowBounds(nullptr,nullptr,nullptr,nullptr);
        display->GetRecommendedRenderTargetSize(nullptr,nullptr);
        display->GetEyeOutputViewport(vr::Eye_Left,nullptr,nullptr,nullptr,nullptr);
        display->GetProjectionRaw(vr::Eye_Left,nullptr,nullptr,nullptr,nullptr);
        float l = 9, r = 9, t = 9, b = 9;
        display->GetProjectionRaw(bad_eye,&l,&r,&t,&b);
        check(l == 0 && r == 0 && t == 0 && b == 0,"invalid projection eye");

        int invalid_cases = 0;
        const auto invalid = [&](DisplayConfig c) {
            ++invalid_cases;
            check(c.validation_error(),"invalid config rejected by pure validator");
            rejected([&] { DisplayComponent component(c); },"invalid immutable component rejected");
            rejected([&] { make_verified_display(c,accept); },"invalid config rejected before verification");
            check(calls == 1,"invalid config must not reach hardware verifier");
        };
        auto c = good; c.monitor_path.clear(); invalid(c);
        c = good; c.monitor_path.assign(2048,'x'); invalid(c);
        c = good; c.monitor_path = std::string("a\0b",3); invalid(c);
        for (const auto width : {0u,1919u,1080u,3840u}) { c = good; c.panel_width = width; invalid(c); }
        c = good; c.panel_height = 1920; invalid(c);
        for (const auto rotation : {90u,180u,270u}) { c = good; c.orientation_degrees = rotation; invalid(c); }
        for (const auto pos : {std::numeric_limits<int32_t>::min(),-131073,131073,std::numeric_limits<int32_t>::max()}) {
            c = good; c.desktop_x = pos; invalid(c); c = good; c.desktop_y = pos; invalid(c);
        }
        for (const auto size : {0u,8193u,std::numeric_limits<uint32_t>::max()}) {
            c = good; c.render_width = size; invalid(c); c = good; c.render_height = size; invalid(c);
        }
        c = good; c.refresh_numerator = 0; invalid(c);
        c = good; c.refresh_denominator = 0; invalid(c);
        c = good; c.refresh_numerator = 29; c.refresh_denominator = 1; invalid(c);
        c = good; c.refresh_numerator = 241; c.refresh_denominator = 1; invalid(c);
        for (int eye = 0; eye < 2; ++eye) {
            c = good; c.projection[eye] = {}; invalid(c); // no invented FOV default
            for (int bound = 0; bound < 4; ++bound) {
                for (float value : {nan,inf,-inf,0.f,0.0001f,-0.0001f,11.f,-11.f}) {
                    c = good;
                    auto& p = c.projection[eye];
                    float* fields[] = {&p.left,&p.right,&p.top,&p.bottom};
                    *fields[bound] = value; invalid(c);
                }
                c = good; auto& p = c.projection[eye];
                float* fields[] = {&p.left,&p.right,&p.top,&p.bottom};
                *fields[bound] *= -1; invalid(c);
            }
        }
        c = good; c.enabled = false;
        rejected([&] { DisplayComponent component(c); },"component requires opt-in");
        c = good; c.desktop_x = 131072; c.desktop_y = -131072;
        c.render_width = 8192; c.render_height = 1;
        c.refresh_numerator = 30; c.refresh_denominator = 1;
        check(!c.validation_error(),"inclusive safety boundaries");
        c.refresh_numerator = 240; check(!c.validation_error(),"maximum refresh boundary");
        auto mutable_source = good;
        DisplayComponent frozen(mutable_source); mutable_source.desktop_x = 123;
        frozen.GetWindowBounds(&x,&y,&w,&h); check(x == good.desktop_x,"component owns immutable snapshot");
        std::cout << "display_component_tests: PASS (" << invalid_cases << " invalid configurations; pure SDK _003 outputs and fail-closed verification)\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "display_component_tests: FAIL: " << error.what() << '\n'; return 1;
    }
}
