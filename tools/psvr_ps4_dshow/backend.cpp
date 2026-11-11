// Isolated, opt-in DirectShow PoC. Not linked to the application default.
#include "abi.h"
#include <windows.h>
#include <dshow.h>
#include <wrl/client.h>
#include "negotiation_diagnostics.hpp"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <cwctype>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <type_traits>
#include <vector>

using Microsoft::WRL::ComPtr;
struct alignas(8) psvr_dshow_decision {
    // Low word: phase; high word: failure HRESULT. One immutable CAS result.
    std::atomic<uint64_t> outcome{0};
    uint64_t reserved = 0;
};
static_assert(sizeof(psvr_dshow_decision) == 16, "decision ABI storage changed");
static_assert(std::atomic<uint64_t>::is_always_lock_free,
    "decision completion must not wait on a hidden mutex");
static_assert(std::is_trivially_destructible_v<psvr_dshow_decision>, "ctypes storage needs trivial lifetime cleanup");
namespace {
HRESULT decide_success(psvr_dshow_decision* decision) noexcept {
    if (!decision) return E_POINTER;
    uint64_t pending = 0;
    return decision->outcome.compare_exchange_strong(pending, 1,
        std::memory_order_acq_rel, std::memory_order_acquire) ? S_OK : E_ABORT;
}
int32_t decide_failure(psvr_dshow_decision* decision, int32_t hr) noexcept {
    if (decision && hr < 0) {
        uint64_t pending = 0;
        const uint64_t failed = (uint64_t(static_cast<uint32_t>(hr)) << 32) | 3u;
        decision->outcome.compare_exchange_strong(pending, failed, std::memory_order_release, std::memory_order_relaxed);
    }
    return hr;
}
constexpr size_t kBytes = 1748u * 408u * 2u;
constexpr REFERENCE_TIME kInterval = 166667; // nearest integer 100ns to 60 Hz
// qedit.h is absent from modern Windows SDKs. These are the documented legacy
// interfaces, not a replacement/registration of the system Sample Grabber.
const CLSID kGrabber = {0xc1f400a0,0x3f08,0x11d3,{0x9f,0x0b,0x00,0x60,0x08,0x03,0x9e,0x37}};
const CLSID kNullRenderer = {0xc1f400a4,0x3f08,0x11d3,{0x9f,0x0b,0x00,0x60,0x08,0x03,0x9e,0x37}};
MIDL_INTERFACE("0579154A-2B53-4994-B0D0-E773148EFF85") GrabberCallback : IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SampleCB(double, IMediaSample*) = 0;
    virtual HRESULT STDMETHODCALLTYPE BufferCB(double, BYTE*, long) = 0;
};
MIDL_INTERFACE("6B652FFF-11FE-4FCE-92AD-0266B5D7C78F") Grabber : IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SetOneShot(BOOL) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetMediaType(const AM_MEDIA_TYPE*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetConnectedMediaType(AM_MEDIA_TYPE*) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetBufferSamples(BOOL) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCurrentBuffer(long*, long*) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCurrentSample(IMediaSample**) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetCallback(GrabberCallback*, long) = 0;
};
void check(HRESULT hr) { if (FAILED(hr)) throw hr; }
void free_type(AM_MEDIA_TYPE& mt) noexcept {
    CoTaskMemFree(mt.pbFormat);
    if (mt.pUnk) mt.pUnk->Release();
    mt = {};
}
struct MediaType {
    AM_MEDIA_TYPE mt{};
    ~MediaType() { free_type(mt); }
};
struct AllocatedType {
    AM_MEDIA_TYPE* mt = nullptr;
    ~AllocatedType() { if (mt) { free_type(*mt); CoTaskMemFree(mt); } }
};
// Layout eligibility is independent of the nominal interval in GetStreamCaps.
// Actual/connected/dynamic media types still use the full strict 60 Hz gate.
REFERENCE_TIME reported_interval(const AM_MEDIA_TYPE* mt) {
    if (!mt || !mt->pbFormat) return 0;
    if (mt->formattype == FORMAT_VideoInfo && mt->cbFormat >= sizeof(VIDEOINFOHEADER))
        return reinterpret_cast<const VIDEOINFOHEADER*>(mt->pbFormat)->AvgTimePerFrame;
    if (mt->formattype == FORMAT_VideoInfo2 && mt->cbFormat >= sizeof(VIDEOINFOHEADER2))
        return reinterpret_cast<const VIDEOINFOHEADER2*>(mt->pbFormat)->AvgTimePerFrame;
    return 0; // metadata only; parsing does NOT make VideoInfo2 eligible
}
const char* transport_layout_rejection(const AM_MEDIA_TYPE& mt) {
    if (mt.majortype != MEDIATYPE_Video) return "major_not_video";
    if (mt.subtype != MEDIASUBTYPE_YUY2) return "subtype_not_yuy2";
    if (mt.formattype != FORMAT_VideoInfo) return "format_not_videoinfo";
    if (!mt.pbFormat || mt.cbFormat < sizeof(VIDEOINFOHEADER)) return "missing_or_short_format_block";
    const auto& v = *reinterpret_cast<const VIDEOINFOHEADER*>(mt.pbFormat);
    if (v.bmiHeader.biSize < sizeof(BITMAPINFOHEADER)) return "short_bitmap_header";
    if (v.bmiHeader.biWidth != 1748) return "width_not_1748";
    if (v.bmiHeader.biHeight != 408) return "height_not_positive_408";
    if (v.bmiHeader.biBitCount != 16 || v.bmiHeader.biPlanes != 1) return "bits_or_planes";
    if (v.bmiHeader.biCompression != MAKEFOURCC('Y','U','Y','2')) return "compression_not_yuy2";
    if (v.bmiHeader.biSizeImage != kBytes) return "image_bytes_not_exact";
    return nullptr;
}
bool is_60hz_interval(REFERENCE_TIME interval) { return interval == 166666 || interval == kInterval; }
const char* transport_rejection(const AM_MEDIA_TYPE& mt) {
    if (const char* reason = transport_layout_rejection(mt)) return reason;
    if (!is_60hz_interval(reinterpret_cast<const VIDEOINFOHEADER*>(mt.pbFormat)->AvgTimePerFrame))
        return "interval_not_60hz_quantized";
    return nullptr;
}
bool exact_format(const AM_MEDIA_TYPE& mt) { return transport_rejection(mt) == nullptr; }
bool interval_in_caps(REFERENCE_TIME interval, const VIDEO_STREAM_CONFIG_CAPS& caps) {
    return caps.MinFrameInterval > 0 && caps.MaxFrameInterval >= caps.MinFrameInterval &&
        interval >= caps.MinFrameInterval && interval <= caps.MaxFrameInterval;
}
bool advertised_transport(const AM_MEDIA_TYPE& mt, const VIDEO_STREAM_CONFIG_CAPS& caps) {
    return exact_format(mt) && interval_in_caps(reinterpret_cast<const VIDEOINFOHEADER*>(mt.pbFormat)->AvgTimePerFrame, caps);
}
const char* select_transport_interval(const AM_MEDIA_TYPE& mt, const VIDEO_STREAM_CONFIG_CAPS& caps,
    REFERENCE_TIME& requested) {
    requested = 0;
    if (const char* reason = transport_layout_rejection(mt)) return reason;
    if (caps.MinFrameInterval <= 0 || caps.MaxFrameInterval < caps.MinFrameInterval) return "invalid_caps_interval_limits";
    const auto published = reinterpret_cast<const VIDEOINFOHEADER*>(mt.pbFormat)->AvgTimePerFrame;
    if (!interval_in_caps(published, caps)) return "published_interval_outside_caps";
    if (is_60hz_interval(published)) requested = published; // preserve exact published 60 Hz
    else if (interval_in_caps(kInterval, caps)) requested = kInterval;
    else if (interval_in_caps(166666, caps)) requested = 166666;
    else return "caps_exclude_60hz";
    // Range membership permits a request, NOT proof of discrete driver support.
    // Only successful SetFormat + exact GetFormat readback can validate it.
    return nullptr;
}
template<class F> void negotiation_step(NegotiationDiagnostics& trace, const char* stage, F&& operation) {
    trace.enter(stage);
    const HRESULT hr = operation();
    trace.result(stage, hr);
    check(hr);
}
void configure_transport(IAMStreamConfig* config, NegotiationDiagnostics* trace = nullptr, bool read_only = false) {
    int count = 0, size = 0;
    if (trace) trace->enter("stream.GetNumberOfCapabilities");
    HRESULT hr = config->GetNumberOfCapabilities(&count, &size);
    if (trace) { trace->result("stream.GetNumberOfCapabilities", hr); trace->caps_info(count, size); }
    check(hr);
    if (count <= 0 || count > 4096 || size != sizeof(VIDEO_STREAM_CONFIG_CAPS)) throw HRESULT(E_UNEXPECTED);
    int matches = 0, published_60hz = 0;
    for (int i = 0; i < count; ++i) {
        AllocatedType type;
        VIDEO_STREAM_CONFIG_CAPS caps{};
        if (trace) { if (i < 64) trace->enter("stream.GetStreamCaps"); else trace->current = "stream.GetStreamCaps"; }
        hr = config->GetStreamCaps(i, &type.mt, reinterpret_cast<BYTE*>(&caps));
        if (trace && (i < 64 || FAILED(hr))) trace->result("stream.GetStreamCaps", hr, i);
        check(hr);
        REFERENCE_TIME interval = 0;
        const char* rejection = type.mt ? select_transport_interval(*type.mt, caps, interval) : "null_media_type";
        const bool published_is_60hz = is_60hz_interval(reported_interval(type.mt));
        if (trace) {
            trace->media("stream.candidate", type.mt, i, &caps, rejection ? rejection : "eligible_unverified_60hz_request");
            if (i < 64) trace->candidate_selection(i, published_is_60hz, interval, rejection);
        }
        if (rejection) continue;
        ++matches;
        if (published_is_60hz) ++published_60hz;
        if (read_only) continue; // NO SetFormat, graph connection, Run or controls
        // Change ONLY AvgTimePerFrame of the owned GetStreamCaps allocation.
        // One request on the first eligible candidate. A failed SetFormat may
        // mutate driver state: propagate to owner cleanup, no retry/next mode.
        if (trace) trace->media("stream.selected", type.mt, i, &caps, "selected_not_yet_validated", true);
        reinterpret_cast<VIDEOINFOHEADER*>(type.mt->pbFormat)->AvgTimePerFrame = interval;
        if (trace) { trace->media("stream.requested", type.mt, i, &caps, "pending_SetFormat_GetFormat", true); trace->enter("stream.SetFormat"); }
        hr = config->SetFormat(type.mt);
        if (trace) trace->result("stream.SetFormat", hr, i);
        check(hr);
        AllocatedType actual;
        if (trace) trace->enter("stream.GetFormat.after_set");
        hr = config->GetFormat(&actual.mt);
        const char* actual_rejection = actual.mt ? transport_rejection(*actual.mt) : "null_media_type";
        if (!actual_rejection && !advertised_transport(*actual.mt, caps)) actual_rejection = "actual_interval_outside_caps";
        if (!actual_rejection && reinterpret_cast<const VIDEOINFOHEADER*>(actual.mt->pbFormat)->AvgTimePerFrame != interval)
            actual_rejection = "actual_interval_differs_from_request";
        if (trace) { trace->result("stream.GetFormat.after_set", hr); trace->media("stream.actual", actual.mt, i, &caps,
            actual_rejection ? actual_rejection : (FAILED(hr) ? "GetFormat_failed" : "validated_exact_60hz_readback"), true); }
        check(hr);
        if (trace) trace->enter("validate.GetFormat.after_set");
        if (actual_rejection)
            { if (trace) trace->result("validate.GetFormat.after_set", VFW_E_INVALIDMEDIATYPE); throw HRESULT(VFW_E_INVALIDMEDIATYPE); }
        if (trace) { trace->result("validate.GetFormat.after_set", S_OK); trace->summary(count, i + 1, matches, false, published_60hz, 1); }
        return;
    }
    if (trace) trace->summary(count, count, matches, read_only, published_60hz, 0);
    if (read_only) return;
    if (trace) { trace->enter("select.no_exact_candidate"); trace->result("select.no_exact_candidate", VFW_E_INVALIDMEDIATYPE); }
    throw HRESULT(VFW_E_INVALIDMEDIATYPE);
}
bool is_ps4(const std::wstring& name) {
    std::wstring lower = name;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](wchar_t c) { return static_cast<wchar_t>(std::towlower(c)); });
    return lower.find(L"vid_05a9&pid_058a") != std::wstring::npos ||
        lower.find(L"vid_05a9&pid_058b") != std::wstring::npos;
}
// The probe accepts ONLY an exact IMoniker::GetDisplayName, never a
// IPropertyBag DevicePath/PnP instance ID. Keep this guard immediately before
// BindToObject; missing/mismatch must not activate any device.
template<class ReadDisplay, class Bind> void probe_identity_then_bind(
    const wchar_t* expected_moniker, ReadDisplay&& read_display, Bind&& bind) {
    if (!expected_moniker || !*expected_moniker || !is_ps4(expected_moniker))
        throw HRESULT(E_INVALIDARG);
    const std::wstring actual = read_display();
    if (actual != expected_moniker || !is_ps4(actual)) throw HRESULT(E_INVALIDARG);
    check(bind());
}
struct Mailbox {
    std::mutex mutex;
    std::condition_variable changed;
    std::vector<uint8_t> bytes = std::vector<uint8_t>(kBytes);
    uint64_t sequence = 0;
    HRESULT error = S_OK;
    bool accepting = true;
};
class Callback final : public GrabberCallback {
    std::atomic<ULONG> refs_{1};
    std::shared_ptr<Mailbox> box_;
public:
    explicit Callback(std::shared_ptr<Mailbox> box) : box_(std::move(box)) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != IID_IUnknown && iid != __uuidof(GrabberCallback)) return E_NOINTERFACE;
        *out = static_cast<GrabberCallback*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { auto n = --refs_; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE SampleCB(double, IMediaSample* sample) override {
        try {
            // Never retain a driver-owned sample or issue COM control calls here.
            HRESULT hr = S_OK;
            BYTE* data = nullptr;
            if (!sample) hr = E_POINTER;
            else {
                AllocatedType dynamic_type;
                hr = sample->GetMediaType(&dynamic_type.mt);
                if (SUCCEEDED(hr) && hr != S_FALSE &&
                    (!dynamic_type.mt || !exact_format(*dynamic_type.mt))) hr = VFW_E_INVALIDMEDIATYPE;
                // ActualDataLength alone is driver metadata, not allocation
                // capacity. Reject before even obtaining/reading its pointer.
                if (SUCCEEDED(hr) && (sample->GetSize() < static_cast<long>(kBytes) ||
                    sample->GetActualDataLength() != static_cast<long>(kBytes))) hr = VFW_E_BUFFER_OVERFLOW;
                if (SUCCEEDED(hr)) hr = sample->GetPointer(&data);
                if (SUCCEEDED(hr) && !data) hr = E_POINTER;
            }
            {
                std::lock_guard<std::mutex> lock(box_->mutex);
                if (!box_->accepting) return S_OK;
                if (FAILED(hr)) box_->error = hr;
                else if (SUCCEEDED(box_->error)) {
                    std::memcpy(box_->bytes.data(), data, kBytes);
                    ++box_->sequence;
                }
            }
            box_->changed.notify_all();
            return S_OK; // owner read receives latched errors; don't throw on stream thread
        } catch (...) {
            return E_FAIL; // never propagate a C++ exception across a COM callback
        }
    }
    HRESULT STDMETHODCALLTYPE BufferCB(double, BYTE*, long) override { return E_NOTIMPL; }
};
void query_controls(IBaseFilter* capture, ComPtr<IAMCameraControl>& camera, ComPtr<IAMVideoProcAmp>& amp,
    NegotiationDiagnostics* trace = nullptr) {
    // The very same IBaseFilter that is added and connected to the graph.
    if (trace) trace->enter("controls.QueryInterface.CameraControl");
    HRESULT hr = capture->QueryInterface(IID_PPV_ARGS(camera.ReleaseAndGetAddressOf()));
    if (trace) trace->result("controls.QueryInterface.CameraControl", hr);
    check(hr);
    if (trace) trace->enter("controls.QueryInterface.VideoProcAmp");
    hr = capture->QueryInterface(IID_PPV_ARGS(amp.ReleaseAndGetAddressOf()));
    if (trace) trace->result("controls.QueryInterface.VideoProcAmp", hr);
    check(hr);
}
HRESULT describe(IAMCameraControl* camera, IAMVideoProcAmp* amp, uint32_t prop, psvr_dshow_control& out) {
    if (prop > 2) return E_INVALIDARG;
    // COM success normally supplies every output, but do not read indeterminate
    // storage if a driver reports success without completing it (MSVC C4701).
    // Zero flags/capabilities/step cannot falsely confirm a Manual observation.
    long lo = 0, hi = 0, step = 0, def = 0, caps = 0, value = 0, flags = 0;
    HRESULT hr;
    const long id = prop == 1 ? VideoProcAmp_Gain : VideoProcAmp_Brightness;
    if (prop == 0) {
        hr = camera->GetRange(CameraControl_Exposure, &lo, &hi, &step, &def, &caps);
        if (SUCCEEDED(hr)) hr = camera->Get(CameraControl_Exposure, &value, &flags);
    } else {
        hr = amp->GetRange(id, &lo, &hi, &step, &def, &caps);
        if (SUCCEEDED(hr)) hr = amp->Get(id, &value, &flags);
    }
    if (SUCCEEDED(hr)) out = {lo, hi, step, def, caps, value, flags};
    return hr;
}
HRESULT write_control(IAMCameraControl* camera, IAMVideoProcAmp* amp, uint32_t prop, long value, long flags) {
    if (prop > 2) return E_INVALIDARG;
    return prop == 0 ? camera->Set(CameraControl_Exposure, value, flags) :
        amp->Set(prop == 1 ? VideoProcAmp_Gain : VideoProcAmp_Brightness, value, flags);
}
ComPtr<IPin> first_pin(IBaseFilter* filter, PIN_DIRECTION direction) {
    ComPtr<IEnumPins> pins;
    check(filter->EnumPins(&pins));
    ComPtr<IPin> pin;
    while (pins->Next(1, pin.ReleaseAndGetAddressOf(), nullptr) == S_OK) {
        PIN_DIRECTION current;
        check(pin->QueryDirection(&current));
        if (current == direction) return pin;
    }
    throw HRESULT(VFW_E_NOT_FOUND);
}
// Enumerates monikers, never binds non-selected devices. Device identity is
// selection metadata only: actual capture/control identity is COM object reuse.
bool moniker_selection_matches(const std::wstring& name, const wchar_t* wanted,
    int wanted_index, int current_index) {
    if (wanted_index >= 0) {
        if (wanted_index != current_index) return false;
        if (!is_ps4(name) || (wanted && name != wanted)) throw HRESULT(E_INVALIDARG);
        return true;
    }
    return wanted && name == wanted;
}
ComPtr<IMoniker> select_moniker(const wchar_t* wanted, psvr_dshow_enum_cb callback = nullptr,
    void* context = nullptr, int wanted_index = -1) {
    ComPtr<ICreateDevEnum> dev;
    check(CoCreateInstance(CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&dev)));
    ComPtr<IEnumMoniker> items;
    HRESULT hr = dev->CreateClassEnumerator(CLSID_VideoInputDeviceCategory, &items, 0);
    check(hr);
    if (hr != S_OK) throw HRESULT(VFW_E_NOT_FOUND);
    ComPtr<IBindCtx> bind;
    check(CreateBindCtx(0, &bind));
    ComPtr<IMoniker> item;
    int index = 0;
    while ((hr = items->Next(1, item.ReleaseAndGetAddressOf(), nullptr)) == S_OK) {
        const bool selected_index = wanted_index >= 0 && index == wanted_index;
        ++index;
        LPOLESTR raw = nullptr;
        check(item->GetDisplayName(bind.Get(), nullptr, &raw));
        std::unique_ptr<wchar_t, decltype(&CoTaskMemFree)> allocated(raw, &CoTaskMemFree);
        const std::wstring name(raw ? raw : L"");
        if (!is_ps4(name)) {
            if (selected_index) throw HRESULT(E_INVALIDARG);
            continue;
        }
        if (callback) callback(name.c_str(), context);
        if (moniker_selection_matches(name, wanted, wanted_index, index - 1)) return item;
    }
    check(hr);
    if (wanted || wanted_index >= 0) throw HRESULT(VFW_E_NOT_FOUND);
    return {};
}
struct Apartment {
    Apartment() { check(CoInitializeEx(nullptr, COINIT_MULTITHREADED)); }
    ~Apartment() { CoUninitialize(); }
};
template<class F> int32_t boundary(F&& fn) noexcept {
    try { return static_cast<int32_t>(fn()); }
    catch (HRESULT hr) { return static_cast<int32_t>(hr); }
    catch (const std::bad_alloc&) { return static_cast<int32_t>(E_OUTOFMEMORY); }
    catch (...) { return static_cast<int32_t>(E_FAIL); }
}
} // namespace

struct psvr_dshow_session {
    // Apartment first: destroyed last, after every COM smart pointer.
    Apartment apartment;
    const DWORD owner = GetCurrentThreadId();
    NegotiationDiagnostics diagnostics;
    std::shared_ptr<Mailbox> box = std::make_shared<Mailbox>();
    ComPtr<IGraphBuilder> graph;
    ComPtr<ICaptureGraphBuilder2> builder;
    ComPtr<IBaseFilter> capture, grabber_filter, renderer;
    ComPtr<IAMCameraControl> camera;
    ComPtr<IAMVideoProcAmp> amp;
    ComPtr<Grabber> grabber;
    ComPtr<GrabberCallback> callback;
    ComPtr<IMediaControl> media;
    ComPtr<IMediaEvent> events;
    uint64_t consumed = 0;
    uint64_t delivered = 0;
    uint32_t persistence_mask = 0;
    uint64_t persistence_boundary = 0;
    psvr_dshow_control original[3]{};
    bool changed[3]{};
    bool transaction_active = false;
    bool transaction_verified = false;
    uint32_t transaction_mask = 0;
    psvr_dshow_control transaction_before[3]{};
    int32_t transaction_targets[3]{};
    bool transaction_touched[3]{};
    bool running = false;
    bool closing = false;
    bool faulted = false;
    void owner_only() const { if (owner != GetCurrentThreadId()) throw HRESULT(RPC_E_WRONG_THREAD); }
    void stop() {
        { std::lock_guard<std::mutex> lock(box->mutex); box->accepting = false; }
        box->changed.notify_all();
        // Driver Stop may block. Python quarantines this owner and never frees
        // the session on a different thread; all callback storage stays alive.
        if (media) {
            diagnostics.enter("cleanup.MediaControl.Stop");
            const HRESULT hr = media->Stop();
            diagnostics.result("cleanup.MediaControl.Stop", hr);
            check(hr);
        }
        running = false;
        if (grabber) check(grabber->SetCallback(nullptr, 0));
    }
    uint64_t barrier() {
        std::lock_guard<std::mutex> lock(box->mutex);
        check(box->error);
        if (!box->accepting) throw HRESULT(E_ABORT);
        return consumed = box->sequence;
    }
    void initialize(const wchar_t* display, int index = -1, bool inspect_only = false) try {
        auto step = [&](const char* stage, auto operation) {
            negotiation_step(diagnostics, stage, operation);
        };
        if (inspect_only && (!display || !*display || !is_ps4(display))) throw HRESULT(E_INVALIDARG);
        diagnostics.enter("device.select_ps4_moniker");
        auto moniker = select_moniker(display, nullptr, nullptr, index);
        diagnostics.result("device.select_ps4_moniker", S_OK);
        if (inspect_only) {
            diagnostics.enter("probe.validate_exact_moniker_before_bind");
            probe_identity_then_bind(display, [&] {
                ComPtr<IBindCtx> bind;
                check(CreateBindCtx(0, &bind));
                LPOLESTR raw = nullptr;
                const HRESULT hr = moniker->GetDisplayName(bind.Get(), nullptr, &raw);
                std::unique_ptr<wchar_t, decltype(&CoTaskMemFree)> allocated(raw, &CoTaskMemFree);
                check(hr);
                return std::wstring(raw ? raw : L"");
            }, [&] {
                diagnostics.result("probe.validate_exact_moniker_before_bind", S_OK);
                diagnostics.enter("capture.BindToObject");
                const HRESULT hr = moniker->BindToObject(nullptr, nullptr, IID_PPV_ARGS(&capture));
                diagnostics.result("capture.BindToObject", hr);
                return hr;
            });
        } else {
            step("capture.BindToObject", [&] { return moniker->BindToObject(nullptr, nullptr, IID_PPV_ARGS(&capture)); });
        }
        if (!inspect_only) query_controls(capture.Get(), camera, amp, &diagnostics);
        step("graph.CoCreateInstance", [&] { return CoCreateInstance(CLSID_FilterGraph, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&graph)); });
        step("builder.CoCreateInstance", [&] { return CoCreateInstance(CLSID_CaptureGraphBuilder2, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&builder)); });
        step("builder.SetFiltergraph", [&] { return builder->SetFiltergraph(graph.Get()); });
        step("graph.AddCapture", [&] { return graph->AddFilter(capture.Get(), L"PS4 owned capture"); });
        ComPtr<IPin> capture_pin;
        step("capture.FindCapturePin", [&] { return builder->FindPin(capture.Get(), PINDIR_OUTPUT, &PIN_CATEGORY_CAPTURE,
            &MEDIATYPE_Video, FALSE, 0, &capture_pin); });
        ComPtr<IAMStreamConfig> config;
        step("capture.QueryStreamConfig", [&] { return capture_pin.As(&config); });
        // Best-effort observation only. A failed diagnostic GetFormat does not
        // itself change selection policy or replace a later actual failure.
        AllocatedType initial;
        diagnostics.enter("stream.GetFormat.initial");
        const HRESULT initial_hr = config->GetFormat(&initial.mt);
        diagnostics.result("stream.GetFormat.initial", initial_hr);
        if (SUCCEEDED(initial_hr)) diagnostics.media("stream.initial", initial.mt, -1, nullptr,
            initial.mt ? transport_rejection(*initial.mt) : "null_media_type", true);
        configure_transport(config.Get(), &diagnostics, inspect_only);
        if (inspect_only) {
            diagnostics.enter("probe.enumeration_complete_no_setformat_no_controls_no_run");
            diagnostics.result(diagnostics.current, S_OK);
            return;
        }
        // System filter optional: missing/unregistered means fail closed, no install.
        step("grabber.CoCreateInstance", [&] { return CoCreateInstance(kGrabber, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&grabber_filter)); });
        step("grabber.QueryInterface", [&] { return grabber_filter.As(&grabber); });
        step("renderer.CoCreateInstance", [&] { return CoCreateInstance(kNullRenderer, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&renderer)); });
        step("graph.AddGrabber", [&] { return graph->AddFilter(grabber_filter.Get(), L"PS4 YUY2 copy"); });
        step("graph.AddRenderer", [&] { return graph->AddFilter(renderer.Get(), L"PS4 discard"); });
        AM_MEDIA_TYPE wanted{};
        wanted.majortype = MEDIATYPE_Video; wanted.subtype = MEDIASUBTYPE_YUY2; wanted.formattype = FORMAT_VideoInfo;
        diagnostics.media("grabber.requested_partial_type", &wanted, -1, nullptr, "partial_type_guids_only", true);
        step("grabber.SetMediaType.YUY2", [&] { return grabber->SetMediaType(&wanted); });
        step("grabber.SetOneShot.false", [&] { return grabber->SetOneShot(FALSE); });
        step("grabber.SetBufferSamples.false", [&] { return grabber->SetBufferSamples(FALSE); });
        diagnostics.enter("grabber.FindInputPin");
        auto input = first_pin(grabber_filter.Get(), PINDIR_INPUT);
        // Direct connections forbid silently inserted format converters.
        step("graph.ConnectDirect.capture_to_grabber", [&] { return graph->ConnectDirect(capture_pin.Get(), input.Get(), nullptr); });
        diagnostics.enter("grabber.FindOutputPin");
        auto output = first_pin(grabber_filter.Get(), PINDIR_OUTPUT);
        diagnostics.enter("renderer.FindInputPin");
        auto sink = first_pin(renderer.Get(), PINDIR_INPUT);
        step("graph.ConnectDirect.grabber_to_renderer", [&] { return graph->ConnectDirect(output.Get(), sink.Get(), nullptr); });
        MediaType connected;
        step("grabber.GetConnectedMediaType", [&] { return grabber->GetConnectedMediaType(&connected.mt); });
        diagnostics.media("grabber.connected", &connected.mt, -1, nullptr, transport_rejection(connected.mt), true);
        diagnostics.enter("validate.grabber.connected");
        if (!exact_format(connected.mt)) throw HRESULT(VFW_E_INVALIDMEDIATYPE);
        // Verify the source pin too, not merely the grabber's requested type.
        MediaType source_type;
        step("capture.ConnectionMediaType", [&] { return capture_pin->ConnectionMediaType(&source_type.mt); });
        diagnostics.media("capture.connected_raw", &source_type.mt, -1, nullptr, transport_rejection(source_type.mt), true);
        diagnostics.enter("validate.capture.connected_raw");
        if (!exact_format(source_type.mt)) throw HRESULT(VFW_E_INVALIDMEDIATYPE);
        callback.Attach(new Callback(box));
        step("grabber.SetCallback.SampleCB", [&] { return grabber->SetCallback(callback.Get(), 0); });
        step("graph.QueryMediaControl", [&] { return graph.As(&media); });
        step("graph.QueryMediaEvent", [&] { return graph.As(&events); });
        step("graph.MediaControl.Run", [&] { return media->Run(); });
        running = true;
        diagnostics.result("open.complete", S_OK);
    } catch (HRESULT hr) {
        diagnostics.terminal_stage = diagnostics.current;
        diagnostics.terminal_hr = hr;
        diagnostics.terminal_index = std::strcmp(diagnostics.last_result, diagnostics.current) == 0 && diagnostics.last_hr == hr
            ? diagnostics.last_index : -1;
        diagnostics.result(diagnostics.current, hr, diagnostics.terminal_index);
        throw;
    }
};

extern "C" {
int32_t psvr_dshow_decision_init(void* storage, uint32_t size) {
    if (!storage || size != sizeof(psvr_dshow_decision) ||
        reinterpret_cast<uintptr_t>(storage) % alignof(psvr_dshow_decision)) return E_INVALIDARG;
    new (storage) psvr_dshow_decision;
    return S_OK;
}
uint32_t psvr_dshow_decision_status(psvr_dshow_decision* decision, int32_t* error) {
    if (!decision || !error) { if (error) *error = E_POINTER; return 3; }
    const uint64_t outcome = decision->outcome.load(std::memory_order_acquire);
    const auto phase = static_cast<uint32_t>(outcome);
    *error = phase == 3 ? static_cast<int32_t>(outcome >> 32) : 0;
    return phase;
}
uint32_t psvr_dshow_decision_cancel(psvr_dshow_decision* decision) {
    if (!decision) return 3;
    uint64_t pending = 0;
    if (decision->outcome.compare_exchange_strong(pending, 2,
        std::memory_order_acq_rel, std::memory_order_acquire)) return 2;
    return static_cast<uint32_t>(pending);
}
uint32_t psvr_dshow_abi(void) { return 2; }
int32_t psvr_dshow_enumerate(psvr_dshow_enum_cb cb, void* context) {
    return boundary([&]() -> HRESULT {
        if (!cb) return E_POINTER;
        Apartment apartment;
        select_moniker(nullptr, cb, context);
        return S_OK;
    });
}
int32_t psvr_dshow_open(const wchar_t* display, psvr_dshow_session** out) {
    return boundary([&]() -> HRESULT {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (!display || !is_ps4(display)) return E_INVALIDARG;
        // Publish before initialize: even failed startup can retain a graph whose
        // Stop is stuck. Python MUST close a non-null handle on failure.
        *out = new psvr_dshow_session;
        (*out)->initialize(display);
        return S_OK;
    });
}
int32_t psvr_dshow_open_index(int32_t index, psvr_dshow_session** out) {
    return boundary([&]() -> HRESULT {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (index < 0) return E_INVALIDARG;
        *out = new psvr_dshow_session;
        (*out)->initialize(nullptr, index);
        return S_OK;
    });
}
int32_t psvr_dshow_inspect_index(int32_t /*index*/, psvr_dshow_session** out) {
    // Retired signature: an old caller must fail closed, not silently omit
    // identity or reinterpret arguments after an ABI signature change.
    if (!out) return E_POINTER;
    *out = nullptr;
    return E_INVALIDARG;
}
int32_t psvr_dshow_inspect_index_exact_moniker(int32_t index, const wchar_t* expected_moniker,
    psvr_dshow_session** out) {
    return boundary([&]() -> HRESULT {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (index < 0 || !expected_moniker || !*expected_moniker || !is_ps4(expected_moniker)) return E_INVALIDARG;
        *out = new psvr_dshow_session;
        (*out)->initialize(expected_moniker, index, true);
        return S_OK;
    });
}
int32_t psvr_dshow_diagnostics(psvr_dshow_session* s, char* dst, uint32_t size) {
    return boundary([&]() -> HRESULT {
        if (!s || !dst || size < NegotiationDiagnostics::capacity) return E_INVALIDARG;
        s->owner_only();
        const auto& trace = s->diagnostics;
        std::memcpy(dst, trace.bytes.data(), trace.length);
        std::snprintf(dst + trace.length, size - trace.length,
            "{\"event\":\"diagnostic_end\",\"current_stage\":\"%s\",\"last_result_stage\":\"%s\",\"last_hr\":%ld,"
            "\"terminal_stage\":\"%s\",\"terminal_hr\":%ld,\"terminal_index\":%d,\"first_failed_observation\":\"%s\",\"first_observation_hr\":%ld,"
            "\"truncated\":%s,\"omitted_media\":%u}\n", trace.current, trace.last_result, static_cast<long>(trace.last_hr),
            trace.terminal_stage, static_cast<long>(trace.terminal_hr), trace.terminal_index,
            trace.first_failure, static_cast<long>(trace.first_failure_hr),
            trace.truncated ? "true" : "false", trace.omitted_media);
        return S_OK;
    });
}
int32_t psvr_dshow_read(psvr_dshow_session* s, uint8_t* dst, uint32_t capacity,
    uint32_t timeout_ms, uint64_t* sequence) {
    return boundary([&]() -> HRESULT {
        if (!s || !dst || !sequence || capacity != kBytes || timeout_ms > 1000) return E_INVALIDARG;
        s->owner_only();
        if (s->closing || s->faulted || !s->running) return E_UNEXPECTED;
        long code; LONG_PTR p1, p2;
        for (int i = 0; i < 64 && s->events->GetEvent(&code, &p1, &p2, 0) == S_OK; ++i) {
            s->events->FreeEventParams(code, p1, p2);
            if (code == EC_ERRORABORT || code == EC_DEVICE_LOST || code == EC_COMPLETE) {
                s->faulted = true; return VFW_E_RUNTIME_ERROR;
            }
        }
        std::unique_lock<std::mutex> lock(s->box->mutex);
        if (!s->box->changed.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] {
            return s->box->sequence > s->consumed || FAILED(s->box->error) || !s->box->accepting;
        })) return S_FALSE;
        if (FAILED(s->box->error)) { s->faulted = true; return s->box->error; }
        if (!s->box->accepting) return E_ABORT;
        std::memcpy(dst, s->box->bytes.data(), kBytes);
        *sequence = s->consumed = s->box->sequence;
        s->delivered = *sequence;
        return S_OK;
    });
}
int32_t psvr_dshow_get_control(psvr_dshow_session* s, uint32_t prop, psvr_dshow_control* out) {
    return boundary([&]() -> HRESULT {
        if (!s || !out) return E_POINTER;
        s->owner_only();
        if (s->closing || s->faulted || !s->camera || !s->amp) return E_UNEXPECTED;
        return describe(s->camera.Get(), s->amp.Get(), prop, *out);
    });
}
int32_t psvr_dshow_set_manual(psvr_dshow_session* s, uint32_t prop, int32_t target) {
    return boundary([&]() -> HRESULT {
        if (!s || prop > 2) return E_INVALIDARG;
        s->owner_only();
        if (s->closing || s->faulted || s->transaction_active || !s->camera || !s->amp) return E_UNEXPECTED;
        psvr_dshow_control before{};
        check(describe(s->camera.Get(), s->amp.Get(), prop, before));
        const int64_t offset = int64_t(target) - before.minimum;
        if (!(before.capabilities & 2) || before.step <= 0 || target < before.minimum ||
            target > before.maximum || offset % before.step) return E_INVALIDARG;
        if (!s->changed[prop]) s->original[prop] = before;
        // Save before attempting Set: even a failed Set can mutate a driver.
        s->changed[prop] = true;
        HRESULT hr = write_control(s->camera.Get(), s->amp.Get(), prop, target, 2);
        psvr_dshow_control actual{};
        if (SUCCEEDED(hr)) hr = describe(s->camera.Get(), s->amp.Get(), prop, actual);
        if (SUCCEEDED(hr) && (actual.value != target || actual.flags != 2)) hr = E_FAIL;
        if (FAILED(hr)) s->faulted = true; // no frames after unconfirmed writes
        return hr;
    });
}
int32_t psvr_dshow_close(psvr_dshow_session* s) {
    return boundary([&]() -> HRESULT {
        if (!s) return S_OK;
        s->owner_only();
        s->closing = true;
        s->stop();
        if (s->transaction_active) {
            uint32_t confirmed = 0;
            check(static_cast<HRESULT>(psvr_dshow_rollback_controls(s, &confirmed)));
        }
        // Isolated PoC never silently persists changes, including failed Set.
        // Restore original auto/manual flags exactly, and verify readback.
        HRESULT first_error = S_OK;
        for (uint32_t prop = 0; prop < 3; ++prop) if (s->changed[prop]) {
            const auto& original = s->original[prop];
            HRESULT hr = write_control(s->camera.Get(), s->amp.Get(), prop, original.value, original.flags);
            psvr_dshow_control actual{};
            const HRESULT read_hr = describe(s->camera.Get(), s->amp.Get(), prop, actual);
            if (SUCCEEDED(hr)) hr = read_hr;
            if (SUCCEEDED(hr) && (actual.flags != original.flags ||
                ((original.flags & 2) && actual.value != original.value))) hr = E_FAIL;
            if (FAILED(hr)) {
                if (SUCCEEDED(first_error)) first_error = hr;
            } else s->changed[prop] = false;
        }
        // Retain the session/owner for explicit retry of ONLY unconfirmed
        // properties. One failed restore must not skip all remaining ones.
        if (FAILED(first_error)) return first_error;
        delete s;
        return S_OK;
    });
}
int32_t psvr_dshow_begin_controls(psvr_dshow_session* s, uint32_t mask,
    const int32_t* targets, uint64_t* sequence) {
    return boundary([&]() -> HRESULT {
        if (!s || !targets || !sequence || !mask || (mask & ~7u)) return E_INVALIDARG;
        s->owner_only();
        if (s->closing || s->faulted || s->transaction_active || !s->camera || !s->amp) return E_UNEXPECTED;
        psvr_dshow_control before[3]{};
        for (uint32_t prop = 0; prop < 3; ++prop) if (mask & (1u << prop)) {
            check(describe(s->camera.Get(), s->amp.Get(), prop, before[prop]));
            const auto& info = before[prop];
            const int64_t offset = int64_t(targets[prop]) - info.minimum;
            if (!(info.capabilities & 2) || info.step <= 0 || info.minimum > info.maximum ||
                targets[prop] < info.minimum || targets[prop] > info.maximum || offset % info.step)
                return E_INVALIDARG;
            if (info.flags != 1 && info.flags != 2) return E_UNEXPECTED;
        }
        // No writes or transaction mutation precede complete validation.
        s->transaction_mask = mask;
        s->transaction_active = true;
        s->transaction_verified = false;
        for (uint32_t prop = 0; prop < 3; ++prop) {
            s->transaction_before[prop] = before[prop];
            s->transaction_targets[prop] = targets[prop];
            s->transaction_touched[prop] = false;
        }
        for (uint32_t prop = 0; prop < 3; ++prop) if (mask & (1u << prop)) {
            if (!s->changed[prop]) s->original[prop] = before[prop];
            s->changed[prop] = true;
            s->transaction_touched[prop] = true; // failed Set may also mutate
            check(write_control(s->camera.Get(), s->amp.Get(), prop, targets[prop], 2));
        }
        *sequence = s->barrier();
        return S_OK;
    });
}
int32_t psvr_dshow_commit_controls(psvr_dshow_session* s, uint64_t* sequence) {
    return boundary([&]() -> HRESULT {
        if (!s || !sequence) return E_INVALIDARG;
        s->owner_only();
        if (!s->transaction_active || s->closing || s->faulted) return E_UNEXPECTED;
        s->transaction_verified = false;
        HRESULT first = S_OK;
        for (uint32_t prop = 0; prop < 3; ++prop) if (s->transaction_mask & (1u << prop)) {
            psvr_dshow_control actual{};
            HRESULT hr = describe(s->camera.Get(), s->amp.Get(), prop, actual);
            if (SUCCEEDED(hr) && (actual.value != s->transaction_targets[prop] || actual.flags != 2)) hr = E_FAIL;
            if (FAILED(hr) && SUCCEEDED(first)) first = hr;
        }
        check(first);
        *sequence = s->barrier();
        s->transaction_verified = true;
        return S_OK;
    });
}
int32_t psvr_dshow_finish_controls(psvr_dshow_session* s, uint32_t persist) {
    return boundary([&]() -> HRESULT {
        if (!s || persist > 1) return E_INVALIDARG;
        s->owner_only();
        if (!s->transaction_active || !s->transaction_verified || s->closing || s->faulted) return E_UNEXPECTED;
        if (persist) for (uint32_t prop = 0; prop < 3; ++prop)
            if (s->transaction_mask & (1u << prop)) s->changed[prop] = false;
        s->persistence_mask = persist ? 0 : s->transaction_mask;
        s->persistence_boundary = s->consumed;
        s->transaction_active = false;
        return S_OK;
    });
}
int32_t psvr_dshow_accept_persistence(psvr_dshow_session* s, uint64_t sequence) {
    return boundary([&]() -> HRESULT {
        if (!s) return E_INVALIDARG;
        s->owner_only();
        if (s->closing || s->faulted || s->transaction_active || !s->persistence_mask ||
            sequence <= s->persistence_boundary || sequence > s->delivered) return E_UNEXPECTED;
        // Pure owner state transition. No COM, driver calls, callback locks or
        // allocations: Python linearizes it with shared cancellation/pump ACK.
        for (uint32_t prop = 0; prop < 3; ++prop)
            if (s->persistence_mask & (1u << prop)) s->changed[prop] = false;
        s->persistence_mask = 0;
        return S_OK;
    });
}
int32_t psvr_dshow_accept_persistence_decided(psvr_dshow_session* s, uint64_t sequence,
    psvr_dshow_decision* decision) {
    return psvr_dshow_accept_readiness_decided(s, sequence, 1, decision);
}
int32_t psvr_dshow_accept_readiness_decided(psvr_dshow_session* s, uint64_t sequence,
    uint32_t persist, psvr_dshow_decision* decision) {
    const auto hr = boundary([&]() -> HRESULT {
        if (!s || !decision || persist > 1) return E_INVALIDARG;
        s->owner_only();
        if (s->closing || s->faulted || s->transaction_active || !s->persistence_mask ||
            sequence <= s->persistence_boundary || sequence > s->delivered) return E_UNEXPECTED;
        check(decide_success(decision));
        // Completion is ALREADY authoritatively published. Nothing can fail,
        // allocate, call COM or acquire a lock after this CAS.
        if (persist) for (uint32_t prop = 0; prop < 3; ++prop)
            if (s->persistence_mask & (1u << prop)) s->changed[prop] = false;
        s->persistence_mask = 0;
        return S_OK;
    });
    return decide_failure(decision, hr);
}
int32_t psvr_dshow_finish_controls_decided(psvr_dshow_session* s, uint32_t persist,
    psvr_dshow_decision* decision) {
    const auto hr = boundary([&]() -> HRESULT {
        if (!s || !decision || persist > 1) return E_INVALIDARG;
        s->owner_only();
        if (!s->transaction_active || !s->transaction_verified || s->closing || s->faulted) return E_UNEXPECTED;
        check(decide_success(decision));
        if (persist) for (uint32_t prop = 0; prop < 3; ++prop)
            if (s->transaction_mask & (1u << prop)) s->changed[prop] = false;
        s->persistence_mask = persist ? 0 : s->transaction_mask;
        s->persistence_boundary = s->consumed;
        s->transaction_active = false;
        return S_OK;
    });
    return decide_failure(decision, hr);
}
int32_t psvr_dshow_rollback_controls(psvr_dshow_session* s, uint32_t* confirmed_mask) {
    return boundary([&]() -> HRESULT {
        if (!s || !confirmed_mask) return E_INVALIDARG;
        s->owner_only();
        *confirmed_mask = 0;
        if (!s->transaction_active) return S_FALSE;
        s->faulted = true; // terminal even when rollback succeeds
        s->transaction_verified = false;
        HRESULT first = S_OK;
        for (uint32_t prop = 0; prop < 3; ++prop) if (s->transaction_mask & (1u << prop)) {
            const auto& before = s->transaction_before[prop];
            HRESULT hr = S_OK;
            if (s->transaction_touched[prop])
                hr = write_control(s->camera.Get(), s->amp.Get(), prop, before.value, before.flags);
            psvr_dshow_control actual{};
            const HRESULT read_hr = describe(s->camera.Get(), s->amp.Get(), prop, actual);
            if (SUCCEEDED(hr)) hr = read_hr;
            if (SUCCEEDED(hr) && (actual.flags != before.flags ||
                ((before.flags & 2) && actual.value != before.value))) hr = E_FAIL;
            if (FAILED(hr)) { if (SUCCEEDED(first)) first = hr; }
            else { s->transaction_touched[prop] = false; *confirmed_mask |= 1u << prop; }
        }
        if (SUCCEEDED(first)) s->transaction_active = false;
        return first;
    });
}
} // extern C
