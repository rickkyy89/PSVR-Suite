// No enumeration/open, no CoCreateInstance, no physical hardware. Exercise
// actual production callback + control helpers against Windows COM fixtures.
#include "backend.cpp"
#include <iostream>
#include <future>
#include <stdexcept>
#include <thread>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
class FakeCapture final : public IBaseFilter, public IAMCameraControl, public IAMVideoProcAmp {
    std::atomic<ULONG> refs_{1};
public:
    int writes = 0;
    long value[10]{};
    long flags[10]{};
    HRESULT set_errors[10]{};
    HRESULT get_errors[10]{};
    HRESULT range_errors[10]{};
    bool omit_get_outputs = false;
    long native_steps[10]{};
    long native_caps[10]{};
    std::vector<long> write_ids;
    std::vector<long> read_ids;
    bool mismatch = false;
    long mismatch_id = -1;
    FakeCapture() {
        std::fill_n(native_steps, 10, 1);
        std::fill_n(native_caps, 10, 3);
        value[CameraControl_Exposure] = -5;
        flags[CameraControl_Exposure] = 1;
        value[VideoProcAmp_Gain] = 3;
        flags[VideoProcAmp_Gain] = 2;
        value[VideoProcAmp_Brightness] = 2;
        flags[VideoProcAmp_Brightness] = 2;
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid == IID_IUnknown || iid == IID_IBaseFilter) *out = static_cast<IBaseFilter*>(this);
        else if (iid == IID_IAMCameraControl) *out = static_cast<IAMCameraControl*>(this);
        else if (iid == IID_IAMVideoProcAmp) *out = static_cast<IAMVideoProcAmp*>(this);
        else return E_NOINTERFACE;
        AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { auto n = --refs_; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE GetClassID(CLSID*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE Stop() override { return S_OK; }
    HRESULT STDMETHODCALLTYPE Pause() override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE Run(REFERENCE_TIME) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetState(DWORD, FILTER_STATE*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetSyncSource(IReferenceClock*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetSyncSource(IReferenceClock**) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE EnumPins(IEnumPins**) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE FindPin(LPCWSTR, IPin**) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE QueryFilterInfo(FILTER_INFO*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE JoinFilterGraph(IFilterGraph*, LPCWSTR) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE QueryVendorInfo(LPWSTR*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetRange(long id, long* lo, long* hi, long* step, long* def, long* caps) override {
        if (id != CameraControl_Exposure && id != VideoProcAmp_Gain && id != VideoProcAmp_Brightness) return E_INVALIDARG;
        if (FAILED(range_errors[id])) return range_errors[id];
        *lo = id == CameraControl_Exposure ? -13 : 0;
        *hi = id == CameraControl_Exposure ? -1 : 8;
        *step = native_steps[id]; *def = *lo; *caps = native_caps[id]; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Set(long id, long target, long mode) override {
        if (id < 0 || id >= 10) return E_INVALIDARG;
        ++writes;
        write_ids.push_back(id);
        if (FAILED(set_errors[id])) return set_errors[id];
        value[id] = (mismatch || mismatch_id == id) ? target + 1 : target;
        flags[id] = mode; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Get(long id, long* target, long* mode) override {
        if (id < 0 || id >= 10) return E_INVALIDARG;
        read_ids.push_back(id);
        if (FAILED(get_errors[id])) return get_errors[id];
        if (omit_get_outputs) return S_OK; // intentionally broken COM fixture
        *target = value[id]; *mode = flags[id]; return S_OK;
    }
};
class FakeSample final : public IMediaSample {
    std::atomic<ULONG> refs_{1};
public:
    std::vector<BYTE> bytes = std::vector<BYTE>(kBytes, 37);
    long length = static_cast<long>(kBytes);
    bool dynamic_bad = false;
    int pointer_requests = 0;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != IID_IUnknown && iid != IID_IMediaSample) return E_NOINTERFACE;
        *out = static_cast<IMediaSample*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { auto n = --refs_; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE GetPointer(BYTE** out) override { ++pointer_requests; *out = bytes.data(); return S_OK; }
    long STDMETHODCALLTYPE GetSize() override { return static_cast<long>(bytes.size()); }
    HRESULT STDMETHODCALLTYPE GetTime(REFERENCE_TIME*, REFERENCE_TIME*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetTime(REFERENCE_TIME*, REFERENCE_TIME*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE IsSyncPoint() override { return S_OK; }
    HRESULT STDMETHODCALLTYPE SetSyncPoint(BOOL) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE IsPreroll() override { return S_FALSE; }
    HRESULT STDMETHODCALLTYPE SetPreroll(BOOL) override { return E_NOTIMPL; }
    long STDMETHODCALLTYPE GetActualDataLength() override { return length; }
    HRESULT STDMETHODCALLTYPE SetActualDataLength(long n) override { length = n; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetMediaType(AM_MEDIA_TYPE** out) override {
        *out = nullptr;
        if (!dynamic_bad) return S_FALSE;
        *out = static_cast<AM_MEDIA_TYPE*>(CoTaskMemAlloc(sizeof(AM_MEDIA_TYPE)));
        if (!*out) return E_OUTOFMEMORY;
        **out = {}; // invalid dynamic format must latch an error
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetMediaType(AM_MEDIA_TYPE*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE IsDiscontinuity() override { return S_FALSE; }
    HRESULT STDMETHODCALLTYPE SetDiscontinuity(BOOL) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetMediaTime(LONGLONG*, LONGLONG*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE SetMediaTime(LONGLONG*, LONGLONG*) override { return E_NOTIMPL; }
};
class FakeStreamConfig final : public IAMStreamConfig {
    std::atomic<ULONG> refs_{1};
    static HRESULT make_type(REFERENCE_TIME interval, AM_MEDIA_TYPE** out) {
        *out = static_cast<AM_MEDIA_TYPE*>(CoTaskMemAlloc(sizeof(AM_MEDIA_TYPE)));
        if (!*out) return E_OUTOFMEMORY;
        **out = {};
        auto* video = static_cast<VIDEOINFOHEADER*>(CoTaskMemAlloc(sizeof(VIDEOINFOHEADER)));
        if (!video) { CoTaskMemFree(*out); *out = nullptr; return E_OUTOFMEMORY; }
        *video = {};
        video->AvgTimePerFrame = interval;
        video->bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        video->bmiHeader.biWidth = 1748; video->bmiHeader.biHeight = 408;
        video->bmiHeader.biBitCount = 16; video->bmiHeader.biPlanes = 1;
        video->bmiHeader.biCompression = MAKEFOURCC('Y','U','Y','2');
        video->bmiHeader.biSizeImage = static_cast<DWORD>(kBytes);
        (*out)->majortype = MEDIATYPE_Video; (*out)->subtype = MEDIASUBTYPE_YUY2;
        (*out)->formattype = FORMAT_VideoInfo;
        (*out)->pbFormat = reinterpret_cast<BYTE*>(video); (*out)->cbFormat = sizeof(*video);
        return S_OK;
    }
public:
    struct Candidate { REFERENCE_TIME interval, minimum, maximum; };
    std::vector<Candidate> published;
    REFERENCE_TIME requested = 0;
    REFERENCE_TIME forced_readback = 0;
    int sets = 0, reads = 0;
    int caps_reads = 0;
    std::vector<REFERENCE_TIME> attempted_intervals;
    GUID readback_subtype = MEDIASUBTYPE_YUY2, candidate_subtype = MEDIASUBTYPE_YUY2;
    DWORD readback_image_bytes = static_cast<DWORD>(kBytes), candidate_image_bytes = static_cast<DWORD>(kBytes);
    HRESULT set_error = S_OK, get_error = S_OK, caps_error = S_OK;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != IID_IUnknown && iid != IID_IAMStreamConfig) return E_NOINTERFACE;
        *out = static_cast<IAMStreamConfig*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { auto n = --refs_; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE SetFormat(AM_MEDIA_TYPE* type) override {
        ++sets;
        attempted_intervals.push_back(reinterpret_cast<VIDEOINFOHEADER*>(type->pbFormat)->AvgTimePerFrame);
        if (FAILED(set_error)) return set_error;
        requested = reinterpret_cast<VIDEOINFOHEADER*>(type->pbFormat)->AvgTimePerFrame;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetFormat(AM_MEDIA_TYPE** out) override {
        ++reads;
        *out = nullptr;
        if (FAILED(get_error)) return get_error;
        const HRESULT hr = make_type(forced_readback ? forced_readback : requested, out);
        if (SUCCEEDED(hr)) {
            (*out)->subtype = readback_subtype;
            reinterpret_cast<VIDEOINFOHEADER*>((*out)->pbFormat)->bmiHeader.biSizeImage = readback_image_bytes;
        }
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetNumberOfCapabilities(int* count, int* size) override {
        *count = static_cast<int>(published.size()); *size = sizeof(VIDEO_STREAM_CONFIG_CAPS); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetStreamCaps(int index, AM_MEDIA_TYPE** type, BYTE* raw) override {
        ++caps_reads;
        *type = nullptr;
        if (FAILED(caps_error)) return caps_error;
        if (index < 0 || static_cast<size_t>(index) >= published.size()) return E_INVALIDARG;
        const auto& item = published[static_cast<size_t>(index)];
        auto* caps = reinterpret_cast<VIDEO_STREAM_CONFIG_CAPS*>(raw);
        *caps = {};
        caps->guid = FORMAT_VideoInfo;
        caps->MinFrameInterval = item.minimum; caps->MaxFrameInterval = item.maximum;
        const HRESULT hr = make_type(item.interval, type);
        if (SUCCEEDED(hr)) {
            (*type)->subtype = candidate_subtype;
            reinterpret_cast<VIDEOINFOHEADER*>((*type)->pbFormat)->bmiHeader.biSizeImage = candidate_image_bytes;
        }
        return hr;
    }
};
void test_same_filter_controls() {
    auto* fake = new FakeCapture;
    ComPtr<IBaseFilter> capture;
    capture.Attach(static_cast<IBaseFilter*>(fake));
    ComPtr<IAMCameraControl> camera;
    ComPtr<IAMVideoProcAmp> amp;
    query_controls(capture.Get(), camera, amp);
    ComPtr<IUnknown> capture_identity, camera_identity, amp_identity;
    check(capture.As(&capture_identity));
    check(camera.As(&camera_identity));
    check(amp.As(&amp_identity));
    require(capture_identity.Get() == camera_identity.Get() &&
        capture_identity.Get() == amp_identity.Get(), "capture/control COM identity mismatch");
    psvr_dshow_control range{};
    check(describe(camera.Get(), amp.Get(), 0, range));
    require(range.minimum == -13 && range.value == -5 && range.flags == 1, "wrong native exposure units/range");
    check(write_control(camera.Get(), amp.Get(), 0, -7, 2));
    check(write_control(camera.Get(), amp.Get(), 1, 8, 2));
    check(write_control(camera.Get(), amp.Get(), 2, 1, 2));
    require(fake->writes == 3 && fake->value[4] == -7 && fake->value[9] == 8 && fake->value[0] == 1,
        "writes did not reach exact capture filter");
}
void test_format_contract() {
    VIDEOINFOHEADER video{};
    video.AvgTimePerFrame = kInterval;
    video.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    video.bmiHeader.biWidth = 1748; video.bmiHeader.biHeight = 408;
    video.bmiHeader.biBitCount = 16; video.bmiHeader.biPlanes = 1;
    video.bmiHeader.biCompression = MAKEFOURCC('Y','U','Y','2');
    video.bmiHeader.biSizeImage = static_cast<DWORD>(kBytes);
    AM_MEDIA_TYPE type{};
    type.majortype = MEDIATYPE_Video; type.subtype = MEDIASUBTYPE_YUY2;
    type.formattype = FORMAT_VideoInfo;
    type.pbFormat = reinterpret_cast<BYTE*>(&video); type.cbFormat = sizeof(video);
    require(exact_format(type), "exact PS4 YUY2 format rejected");
    video.AvgTimePerFrame = 166666;
    require(exact_format(type), "60 Hz quantized interval rejected");
    video.AvgTimePerFrame = 333333;
    require(!exact_format(type), "30 Hz fallback accepted");
    video.AvgTimePerFrame = kInterval; video.bmiHeader.biWidth = 1280;
    require(!exact_format(type), "scaled transport accepted");
    video.bmiHeader.biWidth = 1748; video.bmiHeader.biSizeImage += 4;
    require(!exact_format(type), "padded/unknown stride accepted");
    video.bmiHeader.biSizeImage = static_cast<DWORD>(kBytes); type.subtype = MEDIASUBTYPE_RGB24;
    require(!exact_format(type), "RGB substitution accepted");
}
void test_real_transport_selection_body() {
    // Calls the exact production helper invoked by initialize(), not a copy
    // of its predicate/selection algorithm. Only COM responses are fixtures.
    for (const REFERENCE_TIME interval : {REFERENCE_TIME(166666), REFERENCE_TIME(166667)}) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{interval, interval, interval}};
        configure_transport(config.Get());
        require(fake->sets == 1 && fake->reads == 1 && fake->requested == interval,
            "fixed advertised 60 Hz interval was changed/rejected");
    }
    const FakeStreamConfig::Candidate invalid[] = {
        {83333, 83333, 83333}, {333333, 333333, 333333},
        {0, 83333, 1250000}, // inconsistent published metadata remains invalid
        {166666, 166667, 166667}, {166667, 166666, 166666},
        {166666, 0, 166667}, {166667, 166667, 166666}
    };
    for (const auto& candidate : invalid) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {candidate};
        HRESULT result = S_OK;
        try { configure_transport(config.Get()); } catch (HRESULT hr) { result = hr; }
        require(result == VFW_E_INVALIDMEDIATYPE && fake->sets == 0,
             "locked/inconsistent/out-of-limits interval reached SetFormat");
    }
    {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{333333, 333333, 333333}, {166666, 166666, 166666}};
        configure_transport(config.Get());
        require(fake->sets == 1 && fake->requested == 166666,
            "selection did not preserve the valid enumerated entry");
    }
    for (const REFERENCE_TIME clamped : {REFERENCE_TIME(166667), REFERENCE_TIME(333333)}) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{166666, 166666, 166667}};
        fake->forced_readback = clamped;
        HRESULT result = S_OK;
        try { configure_transport(config.Get()); } catch (HRESULT hr) { result = hr; }
        require(result == VFW_E_INVALIDMEDIATYPE && fake->sets == 1 && fake->reads == 1,
            "SetFormat readback silently changed the selected interval");
    }
}
void test_callback_copy_and_lifetime() {
    auto box = std::make_shared<Mailbox>();
    std::weak_ptr<Mailbox> weak = box;
    ComPtr<GrabberCallback> callback;
    callback.Attach(new Callback(box));
    ComPtr<IMediaSample> sample;
    auto* fake = new FakeSample;
    sample.Attach(fake);
    check(callback->SampleCB(123.0, sample.Get()));
    fake->bytes[0] = 99;
    require(box->sequence == 1 && box->bytes[0] == 37, "sample buffer was not copied");
    check(callback->SampleCB(123.1, sample.Get()));
    require(box->sequence == 2 && box->bytes[0] == 99 && box->bytes.size() == kBytes,
        "newest single-slot contract broken");
    box->accepting = false;
    check(callback->SampleCB(123.2, sample.Get()));
    require(box->sequence == 2, "callback published after stop");
    box.reset();
    require(!weak.expired(), "callback lost mailbox during stop");
    check(callback->SampleCB(123.3, sample.Get()));
    callback.Reset();
    require(weak.expired(), "callback leaked mailbox");
}
void test_callback_coordinate_payload_exact_and_padded_sample_guards() {
    constexpr size_t stride = 1748u * 2u;
    std::vector<uint8_t> expected(kBytes);
    for (size_t y = 0; y < 408; ++y) for (size_t pair = 0; pair < 874; ++pair) {
        const size_t offset = y * stride + pair * 4;
        expected[offset] = static_cast<uint8_t>(16 + (y * 17 + pair * 13) % 220);
        expected[offset + 1] = static_cast<uint8_t>(64 + (y * 7 + pair * 3) % 48);
        expected[offset + 2] = static_cast<uint8_t>(16 + (y * 31 + pair * 19 + 67) % 220);
        expected[offset + 3] = static_cast<uint8_t>(144 + (y * 11 + pair * 5) % 48);
    }
    for (int mode = 0; mode < 5; ++mode) {
        auto box = std::make_shared<Mailbox>();
        ComPtr<GrabberCallback> callback; callback.Attach(new Callback(box));
        auto* fake = new FakeSample;
        ComPtr<IMediaSample> sample; sample.Attach(fake);
        fake->bytes = expected;
        if (mode == 1 || mode == 3) fake->bytes.resize(kBytes + 64, 0xa5);
        if (mode == 2) {
            // Actual row padding is NOT transport data. Reject before pointer.
            fake->bytes.assign((stride + 8) * 408, 0xa5);
            for (size_t y = 0; y < 408; ++y)
                std::memcpy(fake->bytes.data() + y * (stride + 8), expected.data() + y * stride, stride);
        }
        if (mode == 2 || mode == 3) fake->length = static_cast<long>(fake->bytes.size());
        if (mode == 4) fake->bytes.resize(kBytes - 1); // false full-length metadata
        check(callback->SampleCB(0, sample.Get())); // production callback, no graph/device
        if (mode <= 1) {
            // Spare allocation capacity is allowed when actual length is exact;
            // confusing allocator capacity with a padded frame is over-strict.
            require(box->error == S_OK && box->sequence == 1 && fake->pointer_requests == 1 && box->bytes == expected,
                "valid exact-length packed payload changed, shifted, flipped or rejected due to spare capacity");
            for (const size_t y : {size_t(0), size_t(399), size_t(400), size_t(407)})
                for (const size_t x : {size_t(47), size_t(48), size_t(687), size_t(688), size_t(1327), size_t(1328)}) {
                    const size_t offset = y * stride + x * 2;
                    require(box->bytes[offset] == expected[offset] && box->bytes[offset + 1] == expected[offset + 1],
                        "raw boundary marker moved during callback copy");
                    fake->bytes[offset] ^= 0xff;
                }
            require(box->bytes == expected, "callback retained/aliased driver-owned coordinate bytes");
        } else {
            require(box->error == VFW_E_BUFFER_OVERFLOW && box->sequence == 0 && fake->pointer_requests == 0,
                "padded/oversized/underallocated sample reached GetPointer/copy");
            require(std::all_of(box->bytes.begin(), box->bytes.end(), [](uint8_t b) { return b == 0; }),
                "invalid sample changed the raw mailbox");
        }
    }
}
void test_bad_samples() {
    for (int mode = 0; mode < 3; ++mode) {
        auto box = std::make_shared<Mailbox>();
        ComPtr<GrabberCallback> callback;
        callback.Attach(new Callback(box));
        auto* fake = new FakeSample;
        ComPtr<IMediaSample> sample;
        sample.Attach(fake);
        if (mode == 0) fake->length -= 2;
        else if (mode == 1) fake->dynamic_bad = true;
        else fake->bytes.resize(16); // claims full actual length, tiny allocation
        check(callback->SampleCB(0, sample.Get()));
        require(FAILED(box->error) && box->sequence == 0, "invalid format/length accepted");
        require(fake->pointer_requests == 0, "invalid sample pointer was obtained before capacity validation");
        require(box->bytes[0] == 0, "malformed sample was read/copied");
        fake->bytes.resize(kBytes, 37);
        fake->length = static_cast<long>(kBytes); fake->dynamic_bad = false;
        check(callback->SampleCB(0, sample.Get()));
        require(box->sequence == 0, "latched invalid stream resumed silently");
    }
}
void test_control_abi_and_restore() {
    // Session apartment is real COM initialization; graph/devices are absent.
    auto* session = new psvr_dshow_session;
    auto* fake = new FakeCapture;
    session->capture.Attach(static_cast<IBaseFilter*>(fake));
    query_controls(session->capture.Get(), session->camera, session->amp);
    check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 0, -7)));
    require(fake->value[4] == -7 && fake->flags[4] == 2, "Manual flags absent");
    const int writes = fake->writes;
    require(FAILED(psvr_dshow_set_manual(session, 0, -99)) && fake->writes == writes, "range guard wrote invalid value");
    HRESULT foreign = S_OK;
    std::thread wrong_owner([&] { psvr_dshow_control out{}; foreign = psvr_dshow_get_control(session, 0, &out); });
    wrong_owner.join();
    require(foreign == RPC_E_WRONG_THREAD, "foreign-thread COM call allowed");
    // Keep fake alive past close to observe restoration.
    ComPtr<IBaseFilter> observation = session->capture;
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
    require(fake->value[4] == -5 && fake->flags[4] == 1, "original auto state not restored");
}
void test_unconfirmed_write_fails_closed() {
    auto* session = new psvr_dshow_session;
    auto* fake = new FakeCapture;
    session->capture.Attach(static_cast<IBaseFilter*>(fake));
    query_controls(session->capture.Get(), session->camera, session->amp);
    fake->mismatch = true;
    require(FAILED(psvr_dshow_set_manual(session, 1, 8)) && session->faulted,
        "mismatching readback not terminal");
    require(FAILED(psvr_dshow_close(session)), "failed restore was silently accepted");
    fake->mismatch = false;
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
}
void test_best_effort_three_property_restore() {
    const long ids[] = {CameraControl_Exposure, VideoProcAmp_Gain, VideoProcAmp_Brightness};
    for (uint32_t failed = 0; failed < 3; ++failed) {
        auto* session = new psvr_dshow_session;
        auto* fake = new FakeCapture;
        session->capture.Attach(static_cast<IBaseFilter*>(fake));
        query_controls(session->capture.Get(), session->camera, session->amp);
        check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 0, -7)));
        check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 1, 8)));
        check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 2, 1)));
        fake->write_ids.clear(); fake->read_ids.clear();
        fake->set_errors[ids[failed]] = E_ACCESSDENIED;
        require(psvr_dshow_close(session) == E_ACCESSDENIED, "restore lost the first HRESULT");
        require(fake->write_ids == std::vector<long>({ids[0], ids[1], ids[2]}),
            "one failed restore skipped another property's write");
        require(fake->read_ids == std::vector<long>({ids[0], ids[1], ids[2]}),
            "best-effort restore skipped a readback");
        for (uint32_t prop = 0; prop < 3; ++prop)
            require(session->changed[prop] == (prop == failed), "confirmed restore remained pending or failure was cleared");
        psvr_dshow_control out{};
        require(FAILED(psvr_dshow_get_control(session, 0, &out)), "cleanup quarantine allowed control calls");
        ComPtr<IBaseFilter> observation = session->capture;
        fake->set_errors[ids[failed]] = S_OK;
        fake->write_ids.clear();
        check(static_cast<HRESULT>(psvr_dshow_close(session)));
        require(fake->write_ids == std::vector<long>({ids[failed]}), "retry rewrote already-confirmed properties");
        require(fake->value[4] == -5 && fake->flags[4] == 1 && fake->value[9] == 3 && fake->value[0] == 2,
            "best-effort retry did not restore all originals");
    }
}
void test_restore_first_error_survives_later_mismatch() {
    auto* session = new psvr_dshow_session;
    auto* fake = new FakeCapture;
    session->capture.Attach(static_cast<IBaseFilter*>(fake));
    query_controls(session->capture.Get(), session->camera, session->amp);
    check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 0, -7)));
    check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 1, 8)));
    check(static_cast<HRESULT>(psvr_dshow_set_manual(session, 2, 1)));
    fake->set_errors[CameraControl_Exposure] = E_ACCESSDENIED;
    fake->mismatch_id = VideoProcAmp_Gain;
    fake->write_ids.clear();
    require(psvr_dshow_close(session) == E_ACCESSDENIED, "later readback mismatch replaced first restore error");
    require(fake->write_ids == std::vector<long>({4, 9, 0}) && session->changed[0] &&
        session->changed[1] && !session->changed[2], "partial failure did not retain exactly the unconfirmed properties");
    fake->set_errors[CameraControl_Exposure] = S_OK; fake->mismatch_id = -1;
    fake->write_ids.clear();
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
}
void test_batch_preflight_zero_writes() {
    for (int invalid = 0; invalid < 4; ++invalid) {
        auto* session = new psvr_dshow_session;
        auto* fake = new FakeCapture;
        session->capture.Attach(static_cast<IBaseFilter*>(fake));
        query_controls(session->capture.Get(), session->camera, session->amp);
        int32_t targets[3] = {-7, 8, 1};
        if (invalid == 0) targets[2] = 99;
        else if (invalid == 1) { fake->native_steps[9] = 3; targets[1] = 8; }
        else if (invalid == 2) fake->native_caps[0] = 1;
        else fake->flags[0] = 0;
        uint64_t sequence = 0;
        require(FAILED(psvr_dshow_begin_controls(session, 7, targets, &sequence)), "invalid batch accepted");
        require(fake->writes == 0 && !session->transaction_active, "batch mutated before ALL validation");
        check(static_cast<HRESULT>(psvr_dshow_close(session)));
    }
}
void test_batch_acknowledgement_and_persistence() {
    auto* session = new psvr_dshow_session;
    auto* fake = new FakeCapture;
    session->capture.Attach(static_cast<IBaseFilter*>(fake));
    query_controls(session->capture.Get(), session->camera, session->amp);
    const int32_t targets[3] = {-7, 8, 1};
    uint64_t sequence = 0;
    check(static_cast<HRESULT>(psvr_dshow_begin_controls(session, 7, targets, &sequence)));
    require(fake->write_ids == std::vector<long>({4, 9, 0}), "batch used a different property route");
    require(FAILED(psvr_dshow_finish_controls(session, 1)) && session->transaction_active,
        "finish bypassed native readback verification");
    check(static_cast<HRESULT>(psvr_dshow_commit_controls(session, &sequence)));
    require(session->transaction_active, "driver-readback phase prematurely lost rollback snapshot");
    check(static_cast<HRESULT>(psvr_dshow_finish_controls(session, 1)));
    require(!session->transaction_active && !session->changed[0] && !session->changed[1] && !session->changed[2],
        "explicit persistent acknowledgement did not clear lifetime restoration");
    ComPtr<IBaseFilter> observation = session->capture;
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
    require(fake->writes == 3 && fake->value[4] == -7 && fake->value[9] == 8 && fake->value[0] == 1,
        "normal Suite close silently undid the explicitly persistent profile");
}
void test_batch_simultaneous_failure_and_retry() {
    auto* session = new psvr_dshow_session;
    auto* fake = new FakeCapture;
    session->capture.Attach(static_cast<IBaseFilter*>(fake));
    query_controls(session->capture.Get(), session->camera, session->amp);
    const int32_t targets[3] = {-7, 8, 1};
    fake->set_errors[9] = E_ABORT;
    uint64_t sequence = 0;
    require(psvr_dshow_begin_controls(session, 7, targets, &sequence) == E_ABORT,
        "partial batch did not report failed setter");
    fake->set_errors[4] = E_ACCESSDENIED;
    fake->write_ids.clear(); fake->read_ids.clear();
    uint32_t confirmed = 0;
    require(psvr_dshow_rollback_controls(session, &confirmed) == E_ACCESSDENIED,
        "rollback failed to retain first simultaneous error");
    require(fake->write_ids == std::vector<long>({4, 9}) && fake->read_ids == std::vector<long>({4, 9, 0}),
        "partial batch rollback skipped verification of an untouched property");
    require(confirmed == 4 && session->transaction_active && session->faulted,
        "failed rollback lost quarantine or falsely confirmed a failed property");
    require(FAILED(psvr_dshow_close(session)), "pending failed rollback released ownership");
    fake->set_errors[4] = S_OK; fake->set_errors[9] = S_OK;
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
}
void test_describe_output_initialization_and_error_paths() {
    const long ids[] = {CameraControl_Exposure, VideoProcAmp_Gain, VideoProcAmp_Brightness};
    for (uint32_t prop = 0; prop < 3; ++prop) {
        auto* session = new psvr_dshow_session;
        auto* fake = new FakeCapture;
        session->capture.Attach(static_cast<IBaseFilter*>(fake));
        query_controls(session->capture.Get(), session->camera, session->amp);
        const psvr_dshow_control untouched = {11, 22, 33, 44, 55, 66, 77};
        auto out = untouched;
        fake->range_errors[ids[prop]] = E_ACCESSDENIED;
        require(describe(session->camera.Get(), session->amp.Get(), prop, out) == E_ACCESSDENIED,
            "describe changed GetRange failure HRESULT");
        require(std::memcmp(&out, &untouched, sizeof(out)) == 0 && fake->read_ids.empty(),
            "GetRange failure published output or called Get");
        fake->range_errors[ids[prop]] = S_OK;
        fake->get_errors[ids[prop]] = E_ABORT;
        require(describe(session->camera.Get(), session->amp.Get(), prop, out) == E_ABORT &&
            std::memcmp(&out, &untouched, sizeof(out)) == 0,
            "Get failure published temporary outputs or changed its HRESULT");
        fake->get_errors[ids[prop]] = S_OK;
        fake->omit_get_outputs = true;
        require(describe(session->camera.Get(), session->amp.Get(), prop, out) == S_OK &&
            out.value == 0 && out.flags == 0,
            "successful Get omitting outputs left value/flags indeterminate");
        const int32_t targets[3] = {-7, 8, 1};
        uint64_t sequence = 0;
        require(FAILED(psvr_dshow_begin_controls(session, 7, targets, &sequence)) && fake->writes == 0,
            "missing Get flags were interpreted as a valid control mode");
        check(static_cast<HRESULT>(psvr_dshow_close(session)));
    }
}
void test_startup_originals_survive_until_delivered_readiness_ack() {
    for (int mode = 0; mode < 3; ++mode) {
        auto* session = new psvr_dshow_session;
        auto* fake = new FakeCapture;
        session->capture.Attach(static_cast<IBaseFilter*>(fake));
        query_controls(session->capture.Get(), session->camera, session->amp);
        const int32_t targets[3] = {-7, 8, 1};
        uint64_t sequence = 0;
        check(static_cast<HRESULT>(psvr_dshow_begin_controls(session, 7, targets, &sequence)));
        check(static_cast<HRESULT>(psvr_dshow_commit_controls(session, &sequence)));
        check(static_cast<HRESULT>(psvr_dshow_finish_controls(session, 0)));
        require(session->changed[0] && session->changed[1] && session->changed[2],
            "startup finish(false) discarded original controls before readiness");
        require(FAILED(psvr_dshow_accept_persistence(session, sequence)) &&
            FAILED(psvr_dshow_accept_persistence(session, sequence + 1)),
            "native accepted a stale or not-yet-delivered readiness sequence");
        ComPtr<IBaseFilter> observation = session->capture;
        if (mode == 1) {
            // Fixture models a completed native read's owner metadata. We test
            // the production ACK body, not a fake algorithm or real graph.
            session->delivered = sequence + 1;
            check(static_cast<HRESULT>(psvr_dshow_accept_persistence(session, sequence + 1)));
        } else if (mode == 2) {
            fake->set_errors[VideoProcAmp_Gain] = E_ACCESSDENIED;
            require(psvr_dshow_close(session) == E_ACCESSDENIED && session->changed[1] &&
                !session->changed[0] && !session->changed[2],
                "unaccepted-startup restore failure did not retain exactly the unresolved control");
            fake->set_errors[VideoProcAmp_Gain] = S_OK;
        }
        check(static_cast<HRESULT>(psvr_dshow_close(session)));
        if (mode == 1) require(fake->value[4] == -7 && fake->value[9] == 8 && fake->value[0] == 1,
            "accepted readiness did not preserve the explicit Suite profile");
        else require(fake->value[4] == -5 && fake->flags[4] == 1 && fake->value[9] == 3 && fake->value[0] == 2,
            "startup without accepted readiness did not restore all originals/flags");
    }
}
void test_atomic_commit_completion_and_cancel_races() {
    // Actual production decision/ACK bodies; no Python notifier is involved.
    for (int mode = 0; mode < 20; ++mode) {
        auto* session = new psvr_dshow_session;
        auto* fake = new FakeCapture;
        session->capture.Attach(static_cast<IBaseFilter*>(fake));
        query_controls(session->capture.Get(), session->camera, session->amp);
        const int32_t targets[3] = {-7, 8, 1};
        uint64_t boundary = 0;
        check(static_cast<HRESULT>(psvr_dshow_begin_controls(session, 7, targets, &boundary)));
        check(static_cast<HRESULT>(psvr_dshow_commit_controls(session, &boundary)));
        alignas(8) unsigned char storage[16];
        check(static_cast<HRESULT>(psvr_dshow_decision_init(storage, sizeof(storage))));
        auto* decision = reinterpret_cast<psvr_dshow_decision*>(storage);
        int32_t error = 99;
        require(psvr_dshow_decision_status(decision, &error) == 0 && error == 0,
            "decision did not initialize pending");
        HRESULT result = S_OK;
        if (mode < 2) {
            // Runtime finish uses the SAME atomic completion protocol.
            if (mode == 0) require(psvr_dshow_decision_cancel(decision) == 2, "cancel did not win pending");
            result = psvr_dshow_finish_controls_decided(session, 1, decision);
        } else {
            check(static_cast<HRESULT>(psvr_dshow_finish_controls(session, 0)));
            session->delivered = boundary + 1; // fixture completed-read metadata
            if (mode == 2) {
                // Invalid ACK publishes a single immutable failure HRESULT.
                result = psvr_dshow_accept_persistence_decided(session, boundary, decision);
                require(psvr_dshow_decision_status(decision, &error) == 3 && error == E_UNEXPECTED,
                    "failed ACK did not atomically publish its HRESULT");
                require(psvr_dshow_decision_cancel(decision) == 3, "cancel overwrote a completed failure");
                require(decide_failure(decision, E_ACCESSDENIED) == E_ACCESSDENIED &&
                    psvr_dshow_decision_status(decision, &error) == 3 && error == E_UNEXPECTED,
                    "later failure replaced the original completion HRESULT");
            } else if (mode == 3) {
                psvr_dshow_decision_cancel(decision);
                result = psvr_dshow_accept_persistence_decided(session, boundary + 1, decision);
            } else {
                std::promise<void> go;
                auto signal = go.get_future();
                std::thread canceller([&] { signal.wait(); psvr_dshow_decision_cancel(decision); });
                go.set_value();
                result = psvr_dshow_accept_persistence_decided(session, boundary + 1, decision);
                canceller.join(); // foreign thread used ONLY atomic metadata, never COM/session APIs
            }
        }
        const auto phase = psvr_dshow_decision_status(decision, &error);
        require(phase == 1 || phase == 2 || phase == 3, "decision remained undecided after operation");
        require((phase == 1) == SUCCEEDED(result), "native result and completion phase disagree");
        if (phase == 1) require(psvr_dshow_decision_cancel(decision) == 1 && error == 0,
            "late timeout/cancel retroactively revoked committed success");
        ComPtr<IBaseFilter> observation = session->capture;
        check(static_cast<HRESULT>(psvr_dshow_close(session)));
        if (phase == 1) require(fake->value[4] == -7 && fake->value[9] == 8 && fake->value[0] == 1,
            "committed decision did not preserve explicit profile");
        else require(fake->value[4] == -5 && fake->flags[4] == 1 && fake->value[9] == 3 && fake->value[0] == 2,
            "cancelled/failed decision lost rollback originals");
    }
}
void test_negotiation_failure_stage_not_inferred_from_hresult() {
    const char* stages[] = {"stream.GetStreamCaps", "stream.SetFormat", "stream.GetFormat.after_set",
                           "validate.GetFormat.after_set", "select.no_exact_candidate"};
    for (int mode = 0; mode < 5; ++mode) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{166666, 166666, 166667}};
        if (mode == 0) fake->caps_error = VFW_E_INVALIDMEDIATYPE;
        if (mode == 1) fake->set_error = VFW_E_INVALIDMEDIATYPE;
        if (mode == 2) fake->get_error = VFW_E_INVALIDMEDIATYPE;
        if (mode == 3) fake->forced_readback = 333333;
        if (mode == 4) fake->published = {{83333, 83333, 83333}};
        NegotiationDiagnostics trace;
        HRESULT hr = S_OK;
        try { configure_transport(config.Get(), &trace); } catch (HRESULT failure) { hr = failure; }
        require(hr == VFW_E_INVALIDMEDIATYPE && std::strcmp(trace.current, stages[mode]) == 0,
            "same HRESULT was assigned to the wrong negotiation stage");
        require(std::strstr(trace.bytes.data(), stages[mode]) != nullptr, "precise failure stage was not recorded");
        if (mode == 3) require(std::strstr(trace.bytes.data(), "interval_not_60hz_quantized") != nullptr,
            "GetFormat success with a wrong actual interval lacked raw metadata reason");
    }
    NegotiationDiagnostics connection;
    HRESULT hr = S_OK;
    try { negotiation_step(connection, "graph.ConnectDirect.capture_to_grabber", [] { return VFW_E_INVALIDMEDIATYPE; }); }
    catch (HRESULT failure) { hr = failure; }
    require(hr == VFW_E_INVALIDMEDIATYPE && std::strcmp(connection.current, "graph.ConnectDirect.capture_to_grabber") == 0,
        "actual production graph-step wrapper lost ConnectDirect stage");
}
void test_readonly_candidates_metadata_bounds_and_format_reasons() {
    auto* fake = new FakeStreamConfig;
    ComPtr<IAMStreamConfig> config; config.Attach(fake);
    // Range membership permits a 60 Hz request, NOT driver acceptance.
    // The read-only path must report eligibility without mutating the nominal
    // 120 Hz header or calling SetFormat/GetFormat, even with a later 60 Hz entry.
    fake->published = {{83333, 83333, 333333}, {166666, 166666, 166667}};
    NegotiationDiagnostics trace;
    configure_transport(config.Get(), &trace, true);
    require(fake->sets == 0 && fake->reads == 0 && std::strstr(trace.bytes.data(), "\"read_only\":true"),
        "read-only production enumeration changed or read back a selected mode");
    require(std::strstr(trace.bytes.data(), "\"interval_100ns\":83333") &&
        std::strstr(trace.bytes.data(), "\"published_is_60hz\":false,\"selectable_for_60hz_request\":true") &&
        std::strstr(trace.bytes.data(), "\"requested_interval_100ns\":166667") &&
        std::strstr(trace.bytes.data(), "\"driver_acceptance\":\"not_tested\"") &&
        std::strstr(trace.bytes.data(), "\"selectable_candidates\":2,\"published_60hz_candidates\":1,\"validated_60hz_readbacks\":0") &&
        std::strstr(trace.bytes.data(), "32595559-0000-0010-8000-00AA00389B71"), "candidate GUID/interval diagnostics missing");
    VIDEOINFOHEADER header{};
    header.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    header.bmiHeader.biWidth = 1748; header.bmiHeader.biHeight = -408;
    header.bmiHeader.biBitCount = 16; header.bmiHeader.biPlanes = 1;
    header.bmiHeader.biCompression = MAKEFOURCC('Y','U','Y','2');
    header.bmiHeader.biSizeImage = static_cast<DWORD>(kBytes); header.AvgTimePerFrame = 166666;
    AM_MEDIA_TYPE type{};
    type.majortype = MEDIATYPE_Video; type.subtype = MEDIASUBTYPE_YUY2; type.formattype = FORMAT_VideoInfo;
    type.pbFormat = reinterpret_cast<BYTE*>(&header); type.cbFormat = sizeof(header);
    require(!exact_format(type) && std::strcmp(transport_rejection(type), "height_not_positive_408") == 0,
        "diagnostics weakened the positive-height Sample Grabber requirement");
    trace.media("fixture.negative_height", &type, -1, nullptr, transport_rejection(type), true);
    header.bmiHeader.biHeight = 408; header.bmiHeader.biSizeImage = 0;
    require(!exact_format(type) && std::strcmp(transport_rejection(type), "image_bytes_not_exact") == 0,
        "diagnostics silently guessed unknown YUY2 allocation/stride");
    trace.media("fixture.zero_image_bytes", &type, -1, nullptr, transport_rejection(type), true);
    VIDEOINFOHEADER2 second{};
    second.bmiHeader = header.bmiHeader; second.AvgTimePerFrame = 166666;
    type.pbFormat = reinterpret_cast<BYTE*>(&second); type.cbFormat = sizeof(second); type.formattype = FORMAT_VideoInfo2;
    require(!exact_format(type), "VideoInfo2 was silently accepted for the legacy Sample Grabber");
    trace.media("fixture.videoinfo2", &type, -1, nullptr, transport_rejection(type), true);
    require(std::strstr(trace.bytes.data(), "\"height\":-408") && std::strstr(trace.bytes.data(), "\"image_bytes\":0"),
        "diagnostics normalized away the actual raw header fields");
    for (int i = 0; i < 1000; ++i) trace.media("fixture.cap", &type);
    require(trace.omitted_media > 0 && trace.length < NegotiationDiagnostics::capacity - 1024,
        "capability logging is not bounded at source");
    auto* session = new psvr_dshow_session;
    session->diagnostics = trace;
    session->diagnostics.terminal_stage = "graph.ConnectDirect.capture_to_grabber";
    session->diagnostics.terminal_hr = VFW_E_INVALIDMEDIATYPE;
    std::array<char, NegotiationDiagnostics::capacity> output{};
    check(static_cast<HRESULT>(psvr_dshow_diagnostics(session, output.data(), static_cast<uint32_t>(output.size()))));
    require(std::strstr(output.data(), "\"terminal_stage\":\"graph.ConnectDirect.capture_to_grabber\"") &&
        !std::strstr(output.data(), "vid_") && !std::strstr(output.data(), "device_path"),
        "bounded terminal-stage output missing or leaked device identity");
    check(static_cast<HRESULT>(psvr_dshow_close(session)));
}
void test_nominal_120hz_candidate_requests_and_verifies_60hz() {
    for (const REFERENCE_TIME requested : {REFERENCE_TIME(166667), REFERENCE_TIME(166666)}) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        // Real probe candidate 2, with a second fixture excluding only 166667.
        fake->published = {{83333, 83333, requested == 166667 ? 1250000 : 166666}};
        NegotiationDiagnostics trace;
        configure_transport(config.Get(), &trace);
        require(fake->caps_reads == 1 && fake->sets == 1 && fake->reads == 1 && fake->requested == requested,
            "nominal 120 Hz candidate did not request and read back exact 60 Hz");
        require(fake->attempted_intervals.size() == 1 && fake->attempted_intervals[0] == requested &&
            fake->published[0].interval == 83333, "nominal metadata was used as requested fps or fixture was mutated");
        require(std::strstr(trace.bytes.data(), "\"interval_100ns\":83333") &&
            std::strstr(trace.bytes.data(), "\"stage\":\"stream.requested\"") &&
            std::strstr(trace.bytes.data(), "validated_exact_60hz_readback") &&
            std::strstr(trace.bytes.data(), "\"published_60hz_candidates\":0,\"validated_60hz_readbacks\":1"),
            "diagnostics conflated nominal 120 Hz, requested 60 Hz and validated actual fps");
    }
}
void test_nominal_120hz_setformat_failure_never_retries_or_scans_on() {
    auto* fake = new FakeStreamConfig;
    ComPtr<IAMStreamConfig> config; config.Attach(fake);
    fake->published = {{83333, 83333, 1250000}, {166666, 166666, 166666}};
    fake->set_error = E_FAIL; // may have changed device state despite failure
    NegotiationDiagnostics trace;
    HRESULT hr = S_OK;
    try { configure_transport(config.Get(), &trace); } catch (HRESULT failure) { hr = failure; }
    require(hr == E_FAIL && fake->sets == 1 && fake->caps_reads == 1 && fake->reads == 0 &&
        fake->attempted_intervals.size() == 1 && fake->attempted_intervals[0] == 166667,
        "failed 60 Hz request retried 166666, scanned another mode or used stale readback");
    require(std::strcmp(trace.current, "stream.SetFormat") == 0, "SetFormat failure stage lost");
}
void test_nominal_120hz_ignored_or_changed_request_is_rejected() {
    for (const REFERENCE_TIME actual : {REFERENCE_TIME(83333), REFERENCE_TIME(166666)}) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{83333, 83333, 1250000}, {166666, 166666, 166666}};
        fake->forced_readback = actual;
        NegotiationDiagnostics trace;
        HRESULT hr = S_OK;
        try { configure_transport(config.Get(), &trace); } catch (HRESULT failure) { hr = failure; }
        require(hr == VFW_E_INVALIDMEDIATYPE && fake->sets == 1 && fake->reads == 1 && fake->caps_reads == 1,
            "driver ignored/changed 60 Hz request but was accepted or another candidate was tried");
        const char* reason = actual == 83333 ? "interval_not_60hz_quantized" : "actual_interval_differs_from_request";
        require(std::strstr(trace.bytes.data(), reason) && std::strcmp(trace.current, "validate.GetFormat.after_set") == 0,
            "actual fps mismatch diagnostics lost");
    }
}
void test_requested_60hz_actual_layout_and_locked_caps_remain_strict() {
    for (int mode = 0; mode < 5; ++mode) {
        auto* fake = new FakeStreamConfig;
        ComPtr<IAMStreamConfig> config; config.Attach(fake);
        fake->published = {{83333, 83333, 1250000}};
        if (mode == 0) fake->readback_subtype = MEDIASUBTYPE_RGB24;
        if (mode == 1) fake->readback_image_bytes = static_cast<DWORD>(kBytes + 408 * 4);
        if (mode == 2) fake->candidate_subtype = MEDIASUBTYPE_RGB24;
        if (mode == 3) fake->candidate_image_bytes = static_cast<DWORD>(kBytes + 408 * 4);
        if (mode == 4) fake->published = {{83333, 83333, 83333}};
        NegotiationDiagnostics trace;
        HRESULT hr = S_OK;
        try { configure_transport(config.Get(), &trace); } catch (HRESULT failure) { hr = failure; }
        const int expected_sets = mode < 2 ? 1 : 0;
        require(hr == VFW_E_INVALIDMEDIATYPE && fake->sets == expected_sets && fake->reads == expected_sets,
            "requesting 60 Hz weakened subtype/stride/locked-120-Hz gates");
        const char* reason = mode == 4 ? "caps_exclude_60hz" : (mode % 2 == 0 ? "subtype_not_yuy2" : "image_bytes_not_exact");
        require(std::strstr(trace.bytes.data(), reason) != nullptr, "strict rejection reason missing");
    }
}
void test_probe_exact_moniker_guard_missing_mismatch_never_binds() {
    const wchar_t* expected = L"@device:pnp:usb#vid_05a9&pid_058a#unit-a";
    const wchar_t* other = L"@device:pnp:usb#vid_05a9&pid_058a#unit-b";
    int reads = 0, binds = 0;
    auto bind = [&]() -> HRESULT { ++binds; return S_OK; };
    auto read = [&]() -> std::wstring { ++reads; return expected; };
    // Exercise the production index+identity selector, not only the final bind
    // guard: an earlier/later exact name must not override a stale index.
    require(!moniker_selection_matches(expected, expected, 1, 0) &&
        !moniker_selection_matches(expected, expected, 1, 2), "identity overrode selected index after reorder");
    HRESULT selection_hr = S_OK;
    try { moniker_selection_matches(other, expected, 1, 1); }
    catch (HRESULT failure) { selection_hr = failure; }
    require(selection_hr == E_INVALIDARG && binds == 0, "selected second PS4 bypassed identity mismatch");
    require(moniker_selection_matches(expected, expected, 1, 1), "matching index and identity were rejected");
    const wchar_t* missing[] = {nullptr, L"", L"not-a-ps4"};
    for (const auto* identity : missing) {
        HRESULT hr = S_OK;
        try { probe_identity_then_bind(identity, read, bind); } catch (HRESULT failure) { hr = failure; }
        require(hr == E_INVALIDARG && reads == 0 && binds == 0, "missing identity reached metadata/bind");
    }
    // Another identical VID/PID device, case changes, and a DevicePath-like
    // value are NOT the previously acquired exact GetDisplayName.
    const wchar_t* mismatches[] = {other, L"@DEVICE:pnp:usb#vid_05a9&pid_058a#unit-a",
        L"usb#vid_05a9&pid_058a#unit-a", L""};
    for (const auto* actual : mismatches) {
        HRESULT hr = S_OK;
        try { probe_identity_then_bind(expected, [&]() -> std::wstring { return actual; }, bind); }
        catch (HRESULT failure) { hr = failure; }
        require(hr == E_INVALIDARG && binds == 0, "mismatched/other-kind identity bound a filter");
    }
    HRESULT hr = S_OK;
    try { probe_identity_then_bind(expected, []() -> std::wstring { throw HRESULT(E_FAIL); }, bind); }
    catch (HRESULT failure) { hr = failure; }
    require(hr == E_FAIL && binds == 0, "failed identity read still bound a filter");
    probe_identity_then_bind(expected, read, bind);
    require(reads == 1 && binds == 1, "exact identity did not bind once");
    psvr_dshow_session* session = nullptr;
    require(psvr_dshow_inspect_index(0, &session) == E_INVALIDARG && !session,
        "retired index-only probe did not fail closed");
    require(psvr_dshow_inspect_index_exact_moniker(0, nullptr, &session) == E_INVALIDARG && !session,
        "missing expected identity allocated/initialized a session");
}
} // namespace
int main() {
    try {
        test_same_filter_controls(); test_format_contract(); test_real_transport_selection_body();
        test_callback_copy_and_lifetime();
        test_callback_coordinate_payload_exact_and_padded_sample_guards();
        test_bad_samples(); test_control_abi_and_restore();
        test_unconfirmed_write_fails_closed();
        test_best_effort_three_property_restore(); test_restore_first_error_survives_later_mismatch();
        test_batch_preflight_zero_writes(); test_batch_acknowledgement_and_persistence();
        test_batch_simultaneous_failure_and_retry();
        test_describe_output_initialization_and_error_paths();
        test_startup_originals_survive_until_delivered_readiness_ack();
        test_atomic_commit_completion_and_cancel_races();
        test_negotiation_failure_stage_not_inferred_from_hresult();
        test_readonly_candidates_metadata_bounds_and_format_reasons();
        test_probe_exact_moniker_guard_missing_mismatch_never_binds();
        test_nominal_120hz_candidate_requests_and_verifies_60hz();
        test_nominal_120hz_setformat_failure_never_retries_or_scans_on();
        test_nominal_120hz_ignored_or_changed_request_is_rejected();
        test_requested_60hz_actual_layout_and_locked_caps_remain_strict();
        std::cout << "23 offline COM fixture groups passed; no devices enumerated/opened\n";
        return 0;
    } catch (HRESULT hr) {
        std::cerr << "HRESULT failure: " << std::hex << static_cast<unsigned long>(hr) << '\n';
    } catch (const std::exception& ex) { std::cerr << ex.what() << '\n'; }
    return 1;
}
