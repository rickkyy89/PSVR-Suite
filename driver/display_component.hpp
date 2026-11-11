// SPDX-License-Identifier: MIT
#pragma once
#include <openvr_driver.h>
#include "distortion_math.hpp"
#include "optics_sdk.hpp"
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <iomanip>
#include <limits>
#include <locale>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

namespace psvr {
enum class OpticalMode { identity, profile };
// Raw tangent bounds, NOT degrees or an optical calibration. SDK top means -Y.
struct ProjectionBounds {
    float left = std::numeric_limits<float>::quiet_NaN();
    float right = std::numeric_limits<float>::quiet_NaN();
    float top = std::numeric_limits<float>::quiet_NaN();
    float bottom = std::numeric_limits<float>::quiet_NaN();
    bool valid() const noexcept {
        const auto negative = [](float v) { return std::isfinite(v) && v <= -0.001f && v >= -10.f; };
        const auto positive = [](float v) { return std::isfinite(v) && v >= 0.001f && v <= 10.f; };
        return negative(left) && positive(right) && negative(top) && positive(bottom);
    }
};

struct DisplayConfig {
    bool enabled = false;
    // Requests SteamVR's GPU-native direct display path, not a driver compositor.
    bool native_direct_mode = false;
    bool virtual_display = false; // Runtime virtual output; Sony remains a desktop monitor.
    std::uint32_t edid_vendor_id = 0, edid_product_id = 0;
    // Actual Windows desktop membership. Absence is NOT proof of direct scanout.
    // For use as absence evidence: false must mean confirmed exact-path absence;
    // present or unknown must return true or throw. The current Windows producer
    // does not satisfy this stronger contract on incomplete target identities
    // (B9-01); its false result must not authorize native display exposure.
    std::function<bool()> desktop_membership;
    // Exact Windows monitor device path; never an inferred model/EDID match.
    std::string monitor_path;
    std::int32_t desktop_x = 0, desktop_y = 0;
    std::uint32_t panel_width = 1920, panel_height = 1080;
    std::uint32_t orientation_degrees = 0; // landscape, unrotated only
    std::uint32_t refresh_numerator = 0, refresh_denominator = 0;
    std::uint32_t render_width = 0, render_height = 0; // per eye
    std::array<ProjectionBounds,2> projection{};
    OpticalMode optical_mode = OpticalMode::identity;
    std::optional<distortion::Mapper> optical_mapper;
    std::string optical_profile_name;
    // Optional best-effort evidence only; never influences validation or mapping.
    std::function<void(const std::string&)> diagnostic_sink;

    double refresh_hz() const noexcept {
        return refresh_denominator ? static_cast<double>(refresh_numerator)/refresh_denominator : 0.;
    }
    // Pure validation. These safety limits are policy, not measured optics.
    const char* validation_error() const noexcept {
        if (virtual_display && native_direct_mode) return "virtual display and native direct mode are mutually exclusive";
        if (native_direct_mode && (!edid_vendor_id || edid_vendor_id > 65535 ||
            !edid_product_id || edid_product_id > 65535 || !desktop_membership))
            return "native direct mode requires real EDID IDs and a desktop membership query";
        if (monitor_path.empty() || monitor_path.size() >= 2048 || monitor_path.find('\0') != std::string::npos)
            return "displayMonitorPath must be an explicit exact monitor path";
        if (panel_width != 1920 || panel_height != 1080 || panel_width % 2 || orientation_degrees != 0)
            return "display requires unrotated 1920x1080 panel with equal half viewports";
        if (desktop_x < -131072 || desktop_x > 131072 || desktop_y < -131072 || desktop_y > 131072)
            return "display desktop coordinates outside safety bounds";
        if (!render_width || !render_height || render_width > 8192 || render_height > 8192)
            return "display render dimensions must be 1..8192 per eye";
        if (!refresh_numerator || !refresh_denominator || !std::isfinite(refresh_hz()) ||
            refresh_hz() < 30. || refresh_hz() > 240.)
            return "display refresh rational must yield 30..240 Hz";
        if (!projection[0].valid() || !projection[1].valid())
            return "explicit finite per-eye projection tangents required (negative left/top, positive right/bottom)";
        if (optical_mode == OpticalMode::profile) {
            if (!optical_mapper || optical_profile_name.empty())
                return "profile mode requires a validated explicit optical profile";
            if (const auto* error = optics::sdk_profile_error(*optical_mapper)) return error;
        } else if (optical_mode != OpticalMode::identity || optical_mapper || !optical_profile_name.empty()) {
            return "inconsistent or unknown optical mode";
        }
        return nullptr;
    }
};

class DisplayComponent final : public vr::IVRDisplayComponent {
    const DisplayConfig config_; // frozen for the component lifetime
    static constexpr std::uint64_t trace_limit = 8;
    std::atomic<std::uint64_t> forward_calls_{0}, inverse_calls_{0}, projection_calls_{0}, inverse_failures_{0}, activation_calls_{0};
    // Caps are component-wide, not keyed by eye/channel/UV. Relaxed ordering is
    // sufficient: counters do not publish state or participate in mapping.
    bool sample(std::atomic<std::uint64_t>& count) noexcept {
        if (!config_.diagnostic_sink) return false;
        auto value = count.load(std::memory_order_relaxed);
        // Saturation prevents even counter wraparound from reopening a budget.
        while (value != std::numeric_limits<std::uint64_t>::max()) {
            if (count.compare_exchange_weak(value,value+1,std::memory_order_relaxed)) return value < trace_limit;
        }
        return false;
    }
    template<class F> void trace(F format) const noexcept {
        if (!config_.diagnostic_sink) return;
        try {
            std::ostringstream out;
            out.imbue(std::locale::classic());
            out << "display diagnostic ";
            format(out);
            config_.diagnostic_sink(out.str());
        } catch (...) {} // Includes allocation/formatting and sink exceptions.
    }
    static void number(std::ostream& out, float value) {
        std::uint32_t bits = 0;
        static_assert(sizeof(bits) == sizeof(value),"32-bit SDK float");
        std::memcpy(&bits,&value,sizeof(bits));
        out << std::defaultfloat << std::setprecision(std::numeric_limits<float>::max_digits10) << value
            << "/" << std::hexfloat << value << "/bits=0x" << std::hex << std::setw(8)
            << std::setfill('0') << bits << std::dec << std::setfill(' ') << std::defaultfloat;
    }
    static void uv(std::ostream& out, float u, float v) {
        out << "u="; number(out,u); out << " v="; number(out,v);
    }
    void trace_forward(bool sampled, vr::EVREye eye, float u, float v,
                       const char* reason, const vr::DistortionCoordinates_t& result) const noexcept {
        if (!sampled) return;
        trace([&](std::ostream& out) {
            out << "ComputeDistortion eye=" << static_cast<int>(eye) << " channel=n/a pointer_null=n/a ";
            uv(out,u,v); out << " success=" << (std::strcmp(reason,"ok") == 0) << " reason=" << reason;
            const float* channels[] = {result.rfRed,result.rfGreen,result.rfBlue};
            const char* names[] = {" red "," green "," blue "};
            for (std::size_t i=0; i<3; ++i) { out << names[i]; uv(out,channels[i][0],channels[i][1]); }
        });
    }
    bool trace_inverse(bool sampled, vr::HmdVector2_t* result, vr::EVREye eye,
                       uint32_t channel, float u, float v, const char* reason) noexcept {
        const bool success = std::strcmp(reason,"ok") == 0;
        const bool failure_sample = !success && sample(inverse_failures_);
        if (sampled || failure_sample) trace([&](std::ostream& out) {
            out << "ComputeInverseDistortion eye=" << static_cast<int>(eye) << " channel=" << channel
                << " pointer_null=" << (result == nullptr) << ' ';
            uv(out,u,v); out << " success=" << success << " reason=" << reason << " output=";
            // Failure leaves caller storage untouched; do not read potentially
            // uninitialized storage just to log it.
            if (success) uv(out,result->v[0],result->v[1]);
            else out << "unchanged";
        });
        return success;
    }
    static bool valid_eye(vr::EVREye eye) noexcept { return eye == vr::Eye_Left || eye == vr::Eye_Right; }
    template<class T> static void write(T* out, T value) noexcept { if (out) *out = value; }
public:
    explicit DisplayComponent(DisplayConfig config) : config_(std::move(config)) {
        if (!config_.enabled) throw std::invalid_argument("display is not opted in");
        if (const auto* error = config_.validation_error()) throw std::invalid_argument(error);
    }
    ~DisplayComponent() {
        trace([&](std::ostream& out) {
            out << "summary forward=" << forward_calls_.load(std::memory_order_relaxed)
                << " inverse=" << inverse_calls_.load(std::memory_order_relaxed)
                << " projection=" << projection_calls_.load(std::memory_order_relaxed)
                << " inverse_failures=" << inverse_failures_.load(std::memory_order_relaxed);
        });
    }
    void diagnostic_activation() noexcept {
        if (!sample(activation_calls_)) return;
        trace([&](std::ostream& out) {
            out << "Activate build=" << __DATE__ << ' ' << __TIME__ << " SDK=2.15.6 frozen bounds="
                << config_.desktop_x << ',' << config_.desktop_y << ',' << config_.panel_width << ',' << config_.panel_height
                << " render_per_eye=" << config_.render_width << 'x' << config_.render_height
                << " refresh=" << config_.refresh_numerator << '/' << config_.refresh_denominator
                << " hz=" << std::setprecision(17) << config_.refresh_hz()
                << " identity=" << (config_.optical_mode == OpticalMode::identity)
                << " native_direct_requested=" << config_.native_direct_mode
                << " edid_vendor=" << config_.edid_vendor_id << " edid_product=" << config_.edid_product_id;
            if (config_.native_direct_mode) out << " desktop_observed=" << config_.desktop_membership();
            for (std::size_t i=0; i<2; ++i) {
                const auto& p = config_.projection[i];
                out << " projection_eye=" << i << " left="; number(out,p.left);
                out << " right="; number(out,p.right); out << " top="; number(out,p.top);
                out << " bottom="; number(out,p.bottom);
            }
        });
    }
    const DisplayConfig& config() const noexcept { return config_; }
    void GetWindowBounds(int32_t* x, int32_t* y, uint32_t* width, uint32_t* height) override {
        write(x,config_.desktop_x); write(y,config_.desktop_y);
        write(width,config_.panel_width); write(height,config_.panel_height);
    }
    bool IsDisplayOnDesktop() override {
        if (config_.virtual_display) return false;
        if (!config_.native_direct_mode) return true;
        try { return config_.desktop_membership(); }
        catch (...) { return true; } // Do not hide a still-active/unknown desktop display.
    }
    bool IsDisplayRealDisplay() override { return !config_.virtual_display; }
    void GetRecommendedRenderTargetSize(uint32_t* width, uint32_t* height) override {
        write(width,config_.render_width); write(height,config_.render_height);
    }
    void GetEyeOutputViewport(vr::EVREye eye, uint32_t* x, uint32_t* y, uint32_t* width, uint32_t* height) override {
        write(x,eye == vr::Eye_Right ? config_.panel_width/2 : 0u); write(y,0u);
        write(width,valid_eye(eye) ? config_.panel_width/2 : 0u);
        write(height,valid_eye(eye) ? config_.panel_height : 0u);
    }
    void GetProjectionRaw(vr::EVREye eye, float* left, float* right, float* top, float* bottom) override {
        const bool sampled = sample(projection_calls_);
        const auto p = valid_eye(eye) ? config_.projection[eye == vr::Eye_Left ? 0 : 1] : ProjectionBounds{0,0,0,0};
        write(left,p.left); write(right,p.right); write(top,p.top); write(bottom,p.bottom);
        if (sampled) trace([&](std::ostream& out) {
            out << "GetProjectionRaw eye=" << static_cast<int>(eye) << " channel=n/a pointer_null="
                << (left == nullptr) << ',' << (right == nullptr) << ',' << (top == nullptr) << ',' << (bottom == nullptr)
                << " success=" << valid_eye(eye) << " reason=" << (valid_eye(eye) ? "ok" : "invalid_eye")
                << " output left="; number(out,p.left); out << " right="; number(out,p.right);
            out << " top="; number(out,p.top); out << " bottom="; number(out,p.bottom);
        });
    }
    // OpenVR receives display/output UV and requests render sampling UV. The
    // schema declares display-to-render: call forward, never inverse or Y flip.
    vr::DistortionCoordinates_t ComputeDistortion(vr::EVREye eye, float u, float v) override {
        const bool sampled = sample(forward_calls_);
        vr::DistortionCoordinates_t result{{-1,-1},{-1,-1},{-1,-1}};
        if (!valid_eye(eye) || !std::isfinite(u) || !std::isfinite(v)) {
            trace_forward(sampled,eye,u,v,!valid_eye(eye) ? "invalid_eye" : "nonfinite_uv",result);
            return result;
        }
        if (config_.optical_mode == OpticalMode::identity) {
            result.rfRed[0] = result.rfGreen[0] = result.rfBlue[0] = u;
            result.rfRed[1] = result.rfGreen[1] = result.rfBlue[1] = v;
            trace_forward(sampled,eye,u,v,"ok",result);
            return result;
        }
        const auto mapped = config_.optical_mapper->forward_rgb(
            eye == vr::Eye_Left ? distortion::Eye::left : distortion::Eye::right,{u,v});
        // No boolean failure channel exists in this SDK method. Invalid-domain
        // input returns finite out-of-texture coordinates, never identity fallback.
        if (!mapped) { trace_forward(sampled,eye,u,v,"mapping_failed",result); return result; }
        float* channels[] = {result.rfRed,result.rfGreen,result.rfBlue};
        for (std::size_t i=0; i<3; ++i) {
            channels[i][0] = static_cast<float>((*mapped)[i].u);
            channels[i][1] = static_cast<float>((*mapped)[i].v);
        }
        trace_forward(sampled,eye,u,v,"ok",result);
        return result;
    }
    // SDK _003: true supplies the inverse; false asks the runtime to estimate it.
    // SDK channels 0/1/2 = red/blue/green; mapper order is red/green/blue.
    bool ComputeInverseDistortion(vr::HmdVector2_t* result, vr::EVREye eye, uint32_t channel, float u, float v) override {
        const bool sampled = sample(inverse_calls_);
        if (!result || !valid_eye(eye) || channel > 2 || !std::isfinite(u) || !std::isfinite(v))
            return trace_inverse(sampled,result,eye,channel,u,v,!result ? "null_output" :
                !valid_eye(eye) ? "invalid_eye" : channel > 2 ? "invalid_channel" : "nonfinite_uv");
        if (config_.optical_mode == OpticalMode::identity) {
            result->v[0] = u; result->v[1] = v;
            return trace_inverse(sampled,result,eye,channel,u,v,"ok");
        }
        const distortion::Channel channels[] = {distortion::Channel::red,distortion::Channel::blue,distortion::Channel::green};
        const auto mapped = config_.optical_mapper->inverse_with_uncertainty(
            eye == vr::Eye_Left ? distortion::Eye::left : distortion::Eye::right,channels[channel],{u,v},
            optics::sdk_uv_rounding_error(u,v));
        if (!mapped) return trace_inverse(sampled,result,eye,channel,u,v,"mapping_failed");
        result->v[0] = static_cast<float>(mapped->u);
        result->v[1] = static_cast<float>(mapped->v);
        return trace_inverse(sampled,result,eye,channel,u,v,"ok");
    }
};

// The parent supplies read-only Windows inventory validation. In extended mode,
// it must prove that
// monitor_path selects exactly one active, non-cloned extended-desktop target and
// matches x/y, 1920x1080, orientation and the refresh rational. Do not enable a
// fallback to the primary monitor, first EDID match, or configuration alone.
// Native mode's current snapshot verifier rejects contradictions but does not
// prove desktop absence or direct scanout. B9-01 remains pending a trustworthy
// membership producer before adding an absence prerequisite; see display_selection.hpp.
using DisplayVerifier = std::function<bool(const DisplayConfig&)>;
inline std::unique_ptr<DisplayComponent> make_verified_display(const DisplayConfig& config,
                                                              const DisplayVerifier& verify) {
    if (!config.enabled) return nullptr;
    if (const auto* error = config.validation_error()) throw std::invalid_argument(error);
    if (!verify || !verify(config)) throw std::runtime_error("selected active display is not verified; display exposure refused");
    return std::make_unique<DisplayComponent>(config);
}
} // namespace psvr
