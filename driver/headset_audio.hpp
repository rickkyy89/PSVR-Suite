// SPDX-License-Identifier: MIT
#pragma once

// Windows Core Audio forwarding for the PSVR1 headset buttons.
//
// Pure mapping (dispatch_headset_button_action) is OS-independent and covered
// by offline tests with a mocked IHeadsetAudioControl. Windows ownership,
// discovery failures and shared mute logic are tested with injected fakes;
// tests must never touch real COM apartments, PnP or audio endpoints.
//
// Threading/COM choice (documented for review): the forwarding calls run
// synchronously on the existing MI_04 reader thread, which is our own thread
// with no window or message pump. ComScope initialises it once as MTA
// (COINIT_MULTITHREADED) and balances CoUninitialize at thread exit. Rationale:
// MMDeviceEnumerator/IAudioEndpointVolume are free-threaded, so no STA message
// pumping or cross-thread marshaling is needed; MTA is the minimum that is
// safe inside vrserver.exe. Calls are outside driver mutexes, but synchronous
// audio calls can still delay this reader. No dedicated audio thread, no
// queue, no retry loop: resolution is re-attempted only on a new button edge,
// failures log once and stay fail-closed.

#include "headset_buttons.hpp"

#include <functional>
#include <cstdio>
#include <string>
#include <utility>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <mmdeviceapi.h>
#include <endpointvolume.h>
#include <functiondiscoverykeys_devpkey.h>
#include <propidl.h>
#include <setupapi.h>
#include <objbase.h>
#endif

namespace psvr {

struct IHeadsetAudioControl {
    virtual ~IHeadsetAudioControl() = default;
    virtual const char* name() const = 0;
    // Single volume step (IAudioEndpointVolume steps one notch per call).
    virtual bool volume_step(bool up) = 0;
    // Reads current mute state, then applies the opposite.
    virtual bool toggle_mute() = 0;
    // Static diagnostic reason; implementations must not expose device paths.
    virtual const char* last_failure() const noexcept { return "sink_failed_or_exception"; }
};

// Pure action -> sink mapping. Returns the sink result, false on sink failure;
// never throws, so a broken audio path cannot take down the sensor thread.
inline bool dispatch_headset_button_action(HeadsetButtonAction action,
                                           IHeadsetAudioControl& sink) noexcept {
    try {
        switch (action) {
            case HeadsetButtonAction::VolumeUp: return sink.volume_step(true);
            case HeadsetButtonAction::VolumeDown: return sink.volume_step(false);
            case HeadsetButtonAction::MuteToggle: return sink.toggle_mute();
        }
    } catch (...) {
    }
    return false;
}

#ifdef _WIN32

// Minimal COM pointer for the few interfaces used here (no ATL/WRL dependency,
// MinGW-compatible). Move-only; out() hands a fresh slot to COM out-params.
template <class T>
class AudioComPtr {
public:
    AudioComPtr() = default;
    ~AudioComPtr() { reset(); }
    AudioComPtr(const AudioComPtr&) = delete;
    AudioComPtr& operator=(const AudioComPtr&) = delete;
    AudioComPtr(AudioComPtr&& other) noexcept : ptr_(other.ptr_) { other.ptr_ = nullptr; }
    AudioComPtr& operator=(AudioComPtr&& other) noexcept {
        if (this != &other) {
            reset();
            ptr_ = other.ptr_;
            other.ptr_ = nullptr;
        }
        return *this;
    }
    T* get() const { return ptr_; }
    T** out() { return &ptr_; } // caller guarantees an empty slot
    T* operator->() const { return ptr_; }
    explicit operator bool() const { return ptr_ != nullptr; }
    void reset() {
        if (ptr_) {
            ptr_->Release();
            ptr_ = nullptr;
        }
    }

private:
    T* ptr_ = nullptr;
};

class ComScope {
public:
    // Injection is for offline pairing tests; production always uses Win32.
    using Initialize = HRESULT (WINAPI*)(LPVOID, DWORD);
    using Uninitialize = void (WINAPI*)();
    explicit ComScope(Initialize initialize = &::CoInitializeEx,
                      Uninitialize uninitialize = &::CoUninitialize)
        : result_(initialize(nullptr, COINIT_MULTITHREADED)), uninitialize_(uninitialize) {}
    ~ComScope() {
        if (owns_initialization()) uninitialize_();
    }
    ComScope(const ComScope&) = delete;
    ComScope& operator=(const ComScope&) = delete;
    // S_FALSE also increments COM's per-thread init count and must be balanced.
    // RPC_E_CHANGED_MODE means another apartment owns this thread: fail closed,
    // never borrow it, change its mode, or uninitialize its owner's reference.
    bool ok() const noexcept { return SUCCEEDED(result_); }
    bool owns_initialization() const noexcept { return SUCCEEDED(result_); }
    HRESULT result() const noexcept { return result_; }

private:
    HRESULT result_;
    Uninitialize uninitialize_;
};

// Real backend and offline fake endpoint execute the very same toggle logic.
// No SetMute after a failed GetMute; report which API failed to the caller.
template<class Endpoint>
inline bool toggle_headset_endpoint_mute(Endpoint& endpoint, const char*& failure) {
    BOOL muted = FALSE;
    failure = nullptr;
    if (FAILED(endpoint.GetMute(&muted))) {
        failure = "headset button: mute query failed, fail-closed";
        return false;
    }
    if (FAILED(endpoint.SetMute(muted ? FALSE : TRUE, nullptr))) {
        failure = "headset button: mute toggle failed, fail-closed";
        return false;
    }
    return true;
}

class CoreAudioHeadsetControl : public IHeadsetAudioControl {
public:
    using DiagnosticSink = std::function<void(const std::string&)>;
    using EnumeratorFactory = HRESULT (*)(IMMDeviceEnumerator**);
    using InterfacePresent = bool (*)();

    // Render endpoint ID observed on this machine class for
    // "Altoparlanti (PS VR Audio)". Kept as the configured default; the
    // capture endpoint is never hardcoded and is resolved dynamically.
    static constexpr const wchar_t* kRenderEndpointId =
        L"{0.0.0.00000000}.{cdf7602b-1685-4d54-ad1b-2637ee88879f}";

    // Readiness is mandatory: no endpoint access after COM initialization fails.
    explicit CoreAudioHeadsetControl(bool com_ready, DiagnosticSink sink = {},
                                    EnumeratorFactory create = &create_enumerator,
                                    InterfacePresent present = &pnp_mi01_present)
        : sink_(std::move(sink)), com_ready_(com_ready),
          create_enumerator_(create), interface_present_(present) {}

    const char* name() const override { return "coreaudio"; }
    const char* last_failure() const noexcept override {
        return last_failure_ ? last_failure_ : "backend_exception_or_failure";
    }

    bool volume_step(bool up) override {
        last_failure_ = nullptr;
        if (!require_com()) return false;
        if (!ensure_render()) return false;
        const HRESULT hr = up ? render_volume_->VolumeStepUp(nullptr)
                              : render_volume_->VolumeStepDown(nullptr);
        if (FAILED(hr)) {
            log_once(render_step_logged_, "headset button: volume step failed, fail-closed");
            return false;
        }
        return true;
    }

    bool toggle_mute() override {
        last_failure_ = nullptr;
        if (!require_com()) return false;
        if (!ensure_capture()) return false;
        const char* failure = nullptr;
        if (!toggle_headset_endpoint_mute(*capture_volume_.get(), failure)) {
            log_once(capture_step_logged_, failure);
            return false;
        }
        return true;
    }

private:
    static HRESULT create_enumerator(IMMDeviceEnumerator** output) {
        return ::CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
            __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(output));
    }
    bool require_com() {
        if (com_ready_) return true;
        log_once(com_logged_, "headset button: COM initialization unavailable, fail-closed");
        return false;
    }
    void log_once(bool& flag, const char* text) {
        last_failure_ = text; // Keep each action's reason even after once-only logging.
        if (flag || !sink_) return;
        flag = true;
        try {
            sink_(text);
        } catch (...) {
        }
    }

    bool ensure_render() {
        if (render_volume_) return true;
        AudioComPtr<IMMDeviceEnumerator> enumerator;
        if (FAILED(create_enumerator_(enumerator.out())) || !enumerator) {
            log_once(render_logged_, "headset button: audio enumerator unavailable, fail-closed");
            return false;
        }
        AudioComPtr<IMMDevice> device;
        if (FAILED(enumerator->GetDevice(kRenderEndpointId, device.out()))) {
            log_once(render_logged_,
                     "headset button: render endpoint not found, fail-closed (no retry loop)");
            return false;
        }
        AudioComPtr<IAudioEndpointVolume> volume;
        if (FAILED(device->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr,
                                    reinterpret_cast<void**>(volume.out())))) {
            log_once(render_logged_, "headset button: render volume unavailable, fail-closed");
            return false;
        }
        render_device_ = std::move(device); // keep the device alive with its volume
        render_volume_ = std::move(volume);
        return true;
    }

    static bool instance_id_matches_mi01(const wchar_t* id) {
        // Dynamic discovery: USB composite interface 1 of 054C:09AF, matched by
        // PnP instance prefix. No MMDevice ID is ever hardcoded here.
        static constexpr const wchar_t* kPrefix = L"USB\\VID_054C&PID_09AF&MI_01";
        if (id == nullptr) return false;
        for (const wchar_t* a = id, *b = kPrefix; *b != L'\0'; ++a, ++b) {
            wchar_t ca = *a, cb = *b;
            if (ca >= L'a' && ca <= L'z') ca = static_cast<wchar_t>(ca - L'a' + L'A');
            if (cb >= L'a' && cb <= L'z') cb = static_cast<wchar_t>(cb - L'a' + L'A');
            if (ca != cb) return false;
        }
        return true;
    }

    static bool pnp_mi01_present() {
        ::HDEVINFO set =
            ::SetupDiGetClassDevsW(nullptr, L"USB", nullptr, DIGCF_PRESENT | DIGCF_ALLCLASSES);
        if (set == INVALID_HANDLE_VALUE) return false;
        bool found = false;
        ::SP_DEVINFO_DATA info{};
        info.cbSize = sizeof(info);
        for (DWORD index = 0; ::SetupDiEnumDeviceInfo(set, index, &info); ++index) {
            constexpr DWORD kInstanceChars = 256;
            wchar_t instance[kInstanceChars] = {};
            if (!::SetupDiGetDeviceInstanceIdW(set, &info, instance, kInstanceChars, nullptr))
                continue;
            if (instance_id_matches_mi01(instance)) {
                found = true;
                break;
            }
        }
        ::SetupDiDestroyDeviceInfoList(set);
        return found;
    }

    // Uppercase-normalised friendly-name match: strong on "(PS VR Audio)",
    // weak fallback on a bare PS VR/PSVR token. The caller keeps the weak
    // device only when no strong candidate exists.
    static bool friendly_name_matches(const std::wstring& upper, bool& strong) {
        if (upper.find(L"PS VR AUDIO") != std::wstring::npos) {
            strong = true;
            return true;
        }
        if (upper.find(L"PS VR") != std::wstring::npos ||
            upper.find(L"PSVR") != std::wstring::npos) {
            strong = false;
            return true;
        }
        return false;
    }

    bool ensure_capture() {
        if (capture_volume_) return true;
        // Gate 1: the PSVR USB-audio interface must be present in PnP. Absent
        // means no forwarding target exists: fail closed, no endpoint touched.
        if (!interface_present_()) {
            log_once(capture_logged_,
                     "headset button: PSVR audio interface MI_01 not in PnP, mute fail-closed");
            return false;
        }
        // Gate 2: resolve the capture MMDevice dynamically by friendly name.
        AudioComPtr<IMMDeviceEnumerator> enumerator;
        if (FAILED(create_enumerator_(enumerator.out())) || !enumerator) {
            log_once(capture_logged_, "headset button: audio enumerator unavailable, fail-closed");
            return false;
        }
        AudioComPtr<IMMDeviceCollection> collection;
        if (FAILED(enumerator->EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE,
                                                   collection.out()))) {
            log_once(capture_logged_,
                     "headset button: capture endpoint enumeration failed, fail-closed");
            return false;
        }
        UINT count = 0;
        if (FAILED(collection->GetCount(&count))) {
            log_once(capture_logged_, "headset button: capture endpoint count failed, fail-closed");
            return false;
        }
        AudioComPtr<IMMDevice> weak;
        for (UINT i = 0; i < count; ++i) {
            AudioComPtr<IMMDevice> device;
            if (FAILED(collection->Item(i, device.out()))) continue;
            AudioComPtr<IPropertyStore> store;
            if (FAILED(device->OpenPropertyStore(STGM_READ, store.out()))) continue;
            PROPVARIANT value{};
            ::PropVariantInit(&value);
            bool matched = false, strong = false;
            if (SUCCEEDED(store->GetValue(PKEY_Device_FriendlyName, &value)) &&
                value.vt == VT_LPWSTR && value.pwszVal != nullptr) {
                std::wstring upper(value.pwszVal);
                for (auto& c : upper)
                    if (c >= L'a' && c <= L'z') c = static_cast<wchar_t>(c - L'a' + L'A');
                matched = friendly_name_matches(upper, strong);
            }
            ::PropVariantClear(&value);
            if (!matched) continue;
            if (strong) {
                AudioComPtr<IAudioEndpointVolume> volume;
                if (FAILED(device->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr,
                                            reinterpret_cast<void**>(volume.out())))) {
                    log_once(capture_logged_,
                             "headset button: capture volume unavailable, fail-closed");
                    return false;
                }
                capture_device_ = std::move(device);
                capture_volume_ = std::move(volume);
                return true;
            }
            if (!weak) weak = std::move(device);
        }
        if (!weak) {
            log_once(capture_logged_,
                     "headset button: PS VR capture endpoint not found, mute fail-closed");
            return false;
        }
        AudioComPtr<IAudioEndpointVolume> volume;
        if (FAILED(weak->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr,
                                  reinterpret_cast<void**>(volume.out())))) {
            log_once(capture_logged_, "headset button: capture volume unavailable, fail-closed");
            return false;
        }
        capture_device_ = std::move(weak);
        capture_volume_ = std::move(volume);
        return true;
    }

    DiagnosticSink sink_;
    bool com_ready_;
    EnumeratorFactory create_enumerator_;
    InterfacePresent interface_present_;
    const char* last_failure_ = nullptr;
    bool com_logged_ = false;
    AudioComPtr<IMMDevice> render_device_; // kept alive with its volume
    AudioComPtr<IAudioEndpointVolume> render_volume_;
    AudioComPtr<IMMDevice> capture_device_; // kept alive with its volume
    AudioComPtr<IAudioEndpointVolume> capture_volume_;
    bool render_logged_ = false;
    bool render_step_logged_ = false;
    bool capture_logged_ = false;
    bool capture_step_logged_ = false;
};

#endif // _WIN32

} // namespace psvr
