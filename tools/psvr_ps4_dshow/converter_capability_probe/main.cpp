// Discovery ONLY. No graph, source/sink, sample conversion, device enumeration,
// capture, production DLL, preferences, registration or installation.
// QueryAccept on an unconnected pin is NOT negotiated-format evidence.
#include <windows.h>
#include <dshow.h>
#include <wrl/client.h>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

using Microsoft::WRL::ComPtr;

namespace {
std::string hex_hr(HRESULT hr) {
    std::ostringstream text;
    text << "0x" << std::hex << std::setw(8) << std::setfill('0')
         << static_cast<unsigned long>(hr);
    return text.str();
}

std::string guid_text(REFGUID guid) {
    wchar_t wide[40]{};
    if (!StringFromGUID2(guid, wide, 40)) return "guid-format-error";
    std::string result;
    for (const wchar_t value : wide) {
        if (!value) break;
        result.push_back(static_cast<char>(value)); // GUID alphabet is ASCII
    }
    return result;
}

struct ProposedType {
    VIDEOINFOHEADER video{};
    AM_MEDIA_TYPE type{};
    explicit ProposedType(bool yuy2) {
        video.AvgTimePerFrame = 166667;
        video.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        video.bmiHeader.biWidth = 1748;
        video.bmiHeader.biHeight = 408;
        video.bmiHeader.biPlanes = 1;
        video.bmiHeader.biBitCount = yuy2 ? 16 : 24;
        video.bmiHeader.biCompression = yuy2 ? MAKEFOURCC('Y', 'U', 'Y', '2') : BI_RGB;
        video.bmiHeader.biSizeImage = static_cast<DWORD>(1748 * 408 * (yuy2 ? 2 : 3));
        type.majortype = MEDIATYPE_Video;
        type.subtype = yuy2 ? MEDIASUBTYPE_YUY2 : MEDIASUBTYPE_RGB24;
        type.bFixedSizeSamples = TRUE;
        type.lSampleSize = video.bmiHeader.biSizeImage;
        type.formattype = FORMAT_VideoInfo;
        type.cbFormat = sizeof(video);
        type.pbFormat = reinterpret_cast<BYTE*>(&video);
    }
    ProposedType(const ProposedType&) = delete;
    ProposedType& operator=(const ProposedType&) = delete;
};

bool probe(const char* name, REFCLSID requested) {
    std::cout << "{\"candidate\":\"" << name << "\",\"stage\":\"before_create\"}\n";
    ComPtr<IBaseFilter> filter;
    HRESULT hr = CoCreateInstance(requested, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(filter.GetAddressOf()));
    std::cout << "{\"candidate\":\"" << name << "\",\"requested_clsid\":\""
              << guid_text(requested) << "\",\"create_hresult\":\"" << hex_hr(hr) << "\"";
    if (FAILED(hr) || !filter) {
        const bool absent = hr == REGDB_E_CLASSNOTREG || hr == CLASS_E_CLASSNOTAVAILABLE;
        std::cout << ",\"status\":\"" << (absent ? "unavailable" : "probe_error") << "\"}\n";
        return absent;
    }
    CLSID actual{};
    hr = filter->GetClassID(&actual);
    if (FAILED(hr)) {
        std::cout << ",\"status\":\"probe_error\",\"get_class_id_hresult\":\""
                  << hex_hr(hr) << "\"}\n";
        return false;
    }
    std::cout << ",\"reported_clsid\":\"" << guid_text(actual)
              << "\",\"status\":\"instantiated_not_connected\"}\n";
    if (actual != requested) return false;

    ComPtr<IEnumPins> pins;
    hr = filter->EnumPins(pins.GetAddressOf());
    if (FAILED(hr) || !pins) {
        std::cout << "{\"candidate\":\"" << name << "\",\"enum_pins_hresult\":\""
                  << hex_hr(hr) << "\",\"status\":\"probe_error\"}\n";
        return false;
    }
    ProposedType input(true), output(false);
    // Bound enumeration even for a broken third-party/system COM implementation.
    for (unsigned index = 0; index != 16; ++index) {
        ComPtr<IPin> pin;
        ULONG fetched = 0;
        hr = pins->Next(1, pin.GetAddressOf(), &fetched);
        if (hr == S_FALSE && !fetched && !pin) return index != 0;
        if (hr != S_OK || fetched != 1 || !pin) {
            std::cout << "{\"candidate\":\"" << name << "\",\"next_hresult\":\""
                      << hex_hr(hr) << "\",\"status\":\"probe_error\"}\n";
            return false;
        }
        PIN_DIRECTION direction{};
        hr = pin->QueryDirection(&direction);
        if (FAILED(hr) || (direction != PINDIR_INPUT && direction != PINDIR_OUTPUT)) {
            std::cout << "{\"candidate\":\"" << name << "\",\"direction_hresult\":\""
                      << hex_hr(hr) << "\",\"status\":\"probe_error\"}\n";
            return false;
        }
        const bool is_input = direction == PINDIR_INPUT;
        hr = pin->QueryAccept(is_input ? &input.type : &output.type);
        std::cout << "{\"candidate\":\"" << name << "\",\"pin_index\":" << index
                  << ",\"direction\":\"" << (is_input ? "input" : "output")
                  << "\",\"proposed_subtype\":\"" << (is_input ? "YUY2" : "RGB24")
                  << "\",\"unconnected_query_accept_hresult\":\"" << hex_hr(hr)
                  << "\",\"declares_acceptance_only\":" << (hr == S_OK ? "true" : "false")
                  << "}\n";
        // Rejection is a capability observation, not a successful conversion.
        if (hr != S_OK && hr != S_FALSE && hr != VFW_E_TYPE_NOT_ACCEPTED) return false;
    }
    std::cout << "{\"candidate\":\"" << name << "\",\"status\":\"pin_limit_exceeded\"}\n";
    return false;
}
} // namespace

int main() {
    // Flush stage records before any potentially blocking COM call/release.
    std::cout << std::unitbuf;
    std::cout << "{\"scope\":\"capability_probe_only_no_conversion\",\"graph_created\":false,"
                 "\"width\":1748,\"height\":408,\"proposed_format\":\"VideoInfo\","
                 "\"proposed_avg_time_per_frame_100ns\":166667,"
                 "\"proposed_yuy2_row_bytes\":3496,\"proposed_rgb24_row_bytes\":5244,"
                 "\"colorimetry\":\"unspecified_not_negotiated\"}\n";
    const HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) {
        std::cout << "{\"status\":\"com_init_error\",\"hresult\":\"" << hex_hr(hr) << "\"}\n";
        return 1;
    }
    bool ok = false;
    try {
        const bool colour = probe("CLSID_Colour", CLSID_Colour);
        const bool avi = probe("CLSID_AVIDec", CLSID_AVIDec);
        ok = colour && avi;
    } catch (...) {
        std::cout << "{\"status\":\"probe_exception\"}\n";
    }
    // All filter/enumerator/pin refs have been released on this owner thread.
    CoUninitialize();
    std::cout << "{\"probe_completed\":" << (ok ? "true" : "false")
              << ",\"sample_conversion_performed\":false,\"parity_test_performed\":false}\n";
    return ok ? 0 : 1;
}
