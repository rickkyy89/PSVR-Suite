#pragma once
// Metadata only. No device names/paths/serials, pixels, properties or payloads.
#include <dvdmedia.h>
#include <array>
#include <cstdio>
#include <cstring>

struct NegotiationDiagnostics {
    static constexpr size_t capacity = 65536;
    std::array<char, capacity> bytes{};
    size_t length = 0;
    unsigned media_rows = 0, omitted_media = 0;
    bool truncated = false;
    const char* current = "session.construct";
    const char* last_result = "none";
    const char* first_failure = "none";
    HRESULT last_hr = S_OK, first_failure_hr = S_OK;
    const char* terminal_stage = "none";
    HRESULT terminal_hr = S_OK;
    int last_index = -1, terminal_index = -1;
    void append(const char* row) noexcept {
        const size_t n = std::strlen(row);
        if (length + n + 1024 >= capacity) { truncated = true; return; }
        std::memcpy(bytes.data() + length, row, n);
        length += n; bytes[length] = '\0';
    }
    void enter(const char* stage) noexcept {
        current = stage;
        char row[160];
        std::snprintf(row, sizeof(row), "{\"event\":\"enter\",\"stage\":\"%s\"}\n", stage);
        append(row);
    }
    void result(const char* stage, HRESULT hr, int index = -1) noexcept {
        last_result = stage; last_hr = hr; last_index = index;
        if (FAILED(hr) && SUCCEEDED(first_failure_hr)) { first_failure = stage; first_failure_hr = hr; }
        char row[224];
        std::snprintf(row, sizeof(row), "{\"event\":\"result\",\"stage\":\"%s\",\"index\":%d,\"hr\":%ld}\n",
            stage, index, static_cast<long>(hr));
        append(row);
    }
    void summary(int total, int visited, int matches, bool read_only, int published_60hz, int validated) noexcept {
        char row[320];
        std::snprintf(row, sizeof(row), "{\"event\":\"caps_summary\",\"total\":%d,\"visited\":%d,\"selectable_candidates\":%d,"
            "\"published_60hz_candidates\":%d,\"validated_60hz_readbacks\":%d,"
            "\"omitted_media\":%u,\"read_only\":%s}\n", total, visited, matches, published_60hz, validated,
            omitted_media, read_only ? "true" : "false");
        append(row);
    }
    void candidate_selection(int index, bool published_60hz, REFERENCE_TIME requested, const char* rejection) noexcept {
        char row[384];
        std::snprintf(row, sizeof(row), "{\"event\":\"candidate_selection\",\"index\":%d,\"published_is_60hz\":%s,"
            "\"selectable_for_60hz_request\":%s,\"requested_interval_100ns\":%lld,\"driver_acceptance\":\"not_tested\",\"reason\":\"%s\"}\n",
            index, published_60hz ? "true" : "false", rejection ? "false" : "true", static_cast<long long>(requested),
            rejection ? rejection : "eligible_pending_SetFormat_GetFormat");
        append(row);
    }
    void caps_info(int count, int size) noexcept {
        char row[128];
        std::snprintf(row, sizeof(row), "{\"event\":\"caps_header\",\"count\":%d,\"struct_bytes\":%d}\n", count, size);
        append(row);
    }
    static void guid(REFGUID id, char (&dst)[37]) noexcept {
        std::snprintf(dst, sizeof(dst), "%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X",
            static_cast<unsigned>(id.Data1), static_cast<unsigned>(id.Data2), static_cast<unsigned>(id.Data3),
            static_cast<unsigned>(id.Data4[0]), static_cast<unsigned>(id.Data4[1]), static_cast<unsigned>(id.Data4[2]),
            static_cast<unsigned>(id.Data4[3]), static_cast<unsigned>(id.Data4[4]), static_cast<unsigned>(id.Data4[5]),
            static_cast<unsigned>(id.Data4[6]), static_cast<unsigned>(id.Data4[7]));
    }
    void media(const char* stage, const AM_MEDIA_TYPE* mt, int index = -1,
        const VIDEO_STREAM_CONFIG_CAPS* caps = nullptr, const char* reason = "not_evaluated", bool force = false) noexcept {
        if (!force && media_rows >= 64) { ++omitted_media; return; }
        ++media_rows;
        char major[37], subtype[37], format[37];
        const GUID empty{};
        guid(mt ? mt->majortype : empty, major);
        guid(mt ? mt->subtype : empty, subtype);
        guid(mt ? mt->formattype : empty, format);
        const BITMAPINFOHEADER* bmi = nullptr;
        REFERENCE_TIME interval = 0;
        RECT source{}, target{};
        if (mt && mt->pbFormat && mt->formattype == FORMAT_VideoInfo && mt->cbFormat >= sizeof(VIDEOINFOHEADER)) {
            const auto* v = reinterpret_cast<const VIDEOINFOHEADER*>(mt->pbFormat);
            bmi = &v->bmiHeader; interval = v->AvgTimePerFrame; source = v->rcSource; target = v->rcTarget;
        } else if (mt && mt->pbFormat && mt->formattype == FORMAT_VideoInfo2 && mt->cbFormat >= sizeof(VIDEOINFOHEADER2)) {
            const auto* v = reinterpret_cast<const VIDEOINFOHEADER2*>(mt->pbFormat);
            bmi = &v->bmiHeader; interval = v->AvgTimePerFrame; source = v->rcSource; target = v->rcTarget;
        }
        const int64_t h = bmi ? bmi->biHeight : 0;
        const int64_t abs_h = h < 0 ? -h : h;
        const int64_t image_stride = bmi && abs_h && bmi->biSizeImage % abs_h == 0 ? bmi->biSizeImage / abs_h : -1;
        char row[1536];
        std::snprintf(row, sizeof(row), "{\"event\":\"media\",\"stage\":\"%s\",\"index\":%d,\"major\":\"%s\",\"subtype\":\"%s\","
            "\"format\":\"%s\",\"cb_format\":%lu,\"has_block\":%s,\"header_parsed\":%s,\"sample_size\":%lu,\"fixed_samples\":%s,"
            "\"bmi_size\":%lu,\"width\":%ld,\"height\":%ld,\"bits\":%u,\"planes\":%u,\"compression\":%lu,\"image_bytes\":%lu,"
            "\"interval_100ns\":%lld,\"image_div_abs_height\":%lld,\"required_yuy2_stride\":3496,"
            "\"rc_source\":[%ld,%ld,%ld,%ld],\"rc_target\":[%ld,%ld,%ld,%ld],"
            "\"caps_min_interval\":%lld,\"caps_max_interval\":%lld,\"caps_min_output\":[%ld,%ld],\"caps_max_output\":[%ld,%ld],"
            "\"caps_output_step\":[%d,%d],\"reject\":\"%s\"}\n",
            stage, index, major, subtype, format, mt ? mt->cbFormat : 0, mt && mt->pbFormat ? "true" : "false", bmi ? "true" : "false",
            mt ? mt->lSampleSize : 0, mt && mt->bFixedSizeSamples ? "true" : "false", bmi ? bmi->biSize : 0,
            bmi ? bmi->biWidth : 0, bmi ? bmi->biHeight : 0,
            static_cast<unsigned>(bmi ? bmi->biBitCount : 0), static_cast<unsigned>(bmi ? bmi->biPlanes : 0),
            bmi ? bmi->biCompression : 0, bmi ? bmi->biSizeImage : 0, static_cast<long long>(interval), static_cast<long long>(image_stride),
            source.left, source.top, source.right, source.bottom, target.left, target.top, target.right, target.bottom,
            static_cast<long long>(caps ? caps->MinFrameInterval : 0), static_cast<long long>(caps ? caps->MaxFrameInterval : 0),
            caps ? caps->MinOutputSize.cx : 0, caps ? caps->MinOutputSize.cy : 0,
            caps ? caps->MaxOutputSize.cx : 0, caps ? caps->MaxOutputSize.cy : 0,
            caps ? caps->OutputGranularityX : 0, caps ? caps->OutputGranularityY : 0, reason ? reason : "accepted");
        append(row);
    }
};
