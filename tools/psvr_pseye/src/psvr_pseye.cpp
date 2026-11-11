// SPDX-License-Identifier: MIT
//
// psvr_pseye - stable C ABI over a separately built ps3eye.dll.
//
// No third-party headers or libraries are linked here: ps3eye.dll is loaded at
// run time with LoadLibraryExA(..., LOAD_WITH_ALTERED_SEARCH_PATH) so it can
// find its own libusb-1.0.dll. This shim source stays MIT, but distributing it
// together with ps3eye.dll (MIT + GPLv2 ov534 register tables) is a combined
// distribution that must satisfy the GPLv2 for ps3eye.dll. The stage carries
// the full COPYING.GPL-2.0 / COPYING.LGPL-2.1 texts and NOTICE.txt.
//
// ps3eye.dll is built from the pinned upstream plus
// patches/0001-bounded-grab-and-actual-fps.patch, which adds a bounded grab,
// an abort hook and an actual-frame-rate query. Patches 0002/0003 add safe
// transfer teardown and idle refresh. All are checked before upstream init.

#include "psvr_pseye.h"

#include <windows.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <mutex>

namespace {

const char *kUpstreamId =
    "inspirit/PS3EYEDriver@fe03494e648d0bf2aaa77869e5b50965e2e23620"
    "+patches/0001-bounded-grab-and-actual-fps"
    "+0002-transfer-fault-lifetime+0003-refresh-idle-inventory";
const char *kLicenseNote =
    "psvr_pseye.dll source is MIT. ps3eye.dll is MIT (new code) + GPLv2 (ov534 "
    "register tables ported from the Linux kernel). Shipping the two DLLs "
    "together is a combined distribution subject to the GPLv2 for ps3eye.dll: "
    "the stage ships COPYING.GPL-2.0, the upstream LICENSE and NOTICE.txt. "
    "Dynamic loading does not remove that obligation.";
const char *kLibusbNote =
    "libusb-1.0 (LGPL-2.1-or-later) is required by ps3eye.dll at run time; the "
    "system-provided or source-built libusb-1.0.dll is used as-is. Its full "
    "licence text is staged as COPYING.LGPL-2.1.";

// Upstream C API (ps3eye_capi.h), resolved dynamically.
typedef void (*fn_init)(void);
typedef void (*fn_uninit)(void);
typedef int (*fn_count)(void);
typedef void *(*fn_open)(int, int, int, int, int);
typedef void (*fn_grab)(void *, unsigned char *);
typedef void (*fn_close)(void *);
typedef int (*fn_unique)(void *, char *, int);
// Added by patches/0001-bounded-grab-and-actual-fps.patch.
typedef int (*fn_grab_timeout)(void *, unsigned char *, int);
typedef void (*fn_abort)(void *);
typedef int (*fn_frame_rate)(void *);
typedef int (*fn_set_parameter)(void *, int, int);
typedef int (*fn_get_parameter)(void *, int);

HMODULE g_dll = nullptr;
std::mutex g_load_mutex;
fn_init p_init = nullptr;
fn_uninit p_uninit = nullptr;
fn_count p_count = nullptr;
fn_open p_open = nullptr;
fn_grab p_grab = nullptr;
fn_close p_close = nullptr;
fn_unique p_unique = nullptr;
fn_grab_timeout p_grab_timeout = nullptr;
fn_abort p_abort = nullptr;
fn_frame_rate p_frame_rate = nullptr;
fn_set_parameter p_set_parameter = nullptr;
fn_get_parameter p_get_parameter = nullptr;
fn_count p_patch_level = nullptr;

void set_reason(char *reason, int reason_len, const std::string &text) {
    if (reason == nullptr || reason_len <= 0) {
        return;
    }
    std::snprintf(reason, static_cast<size_t>(reason_len), "%s", text.c_str());
}

std::string last_error_text() {
    DWORD code = GetLastError();
    char *message = nullptr;
    DWORD len = FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
            FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<LPSTR>(&message), 0, nullptr);
    std::string text = "win32 error " + std::to_string(code);
    if (len != 0 && message != nullptr) {
        text += ": ";
        text.append(message, len);
        while (!text.empty() &&
               (text.back() == '\r' || text.back() == '\n' ||
                text.back() == ' ')) {
            text.pop_back();
        }
    }
    if (message != nullptr) {
        LocalFree(message);
    }
    return text;
}

template <typename T>
bool resolve(HMODULE dll, const char *name, T &out) {
    out = reinterpret_cast<T>(GetProcAddress(dll, name));
    return out != nullptr;
}

} // namespace

// Default bounded wait for psvr_pseye_grab. Long enough for the slowest
// supported mode (2 fps -> 500 ms) with margin, short enough that a stalled or
// unplugged device cannot hang a caller.
constexpr int kDefaultGrabTimeoutMs = 1000;

struct psvr_pseye_camera {
    void *eye = nullptr;
    int width = 0;
    int height = 0;
    int fps = 0;
    int format = PSVR_PSEYE_FORMAT_BGR;
    int bytes_per_pixel = 3;
    int grab_timeout_ms = kDefaultGrabTimeoutMs;
};

extern "C" {

int psvr_pseye_abi_version(void) { return PSVR_PSEYE_ABI_VERSION; }

const char *psvr_pseye_upstream_id(void) { return kUpstreamId; }
const char *psvr_pseye_license_note(void) { return kLicenseNote; }
const char *psvr_pseye_libusb_note(void) { return kLibusbNote; }

int psvr_pseye_load(const char *dll_dir, char *reason, int reason_len) {
    std::lock_guard<std::mutex> lock(g_load_mutex);
    if (g_dll != nullptr) {
        return 0;
    }

    std::string path = "ps3eye.dll";
    DWORD flags = 0;
    if (dll_dir != nullptr && dll_dir[0] != '\0') {
        path.assign(dll_dir);
        if (path.back() != '\\' && path.back() != '/') {
            path.push_back('\\');
        }
        path.append("ps3eye.dll");
        flags = LOAD_WITH_ALTERED_SEARCH_PATH;
    }

    HMODULE dll = LoadLibraryExA(path.c_str(), nullptr, flags);
    if (dll == nullptr) {
        set_reason(reason, reason_len,
                   "cannot load " + path + " (" + last_error_text() +
                       "). Build ps3eye.dll (tools/psvr_pseye/build.ps1) or "
                       "point PSVR_PSEYE_DLL at it.");
        return -1;
    }

    bool ok = resolve(dll, "ps3eye_init", p_init) &&
              resolve(dll, "ps3eye_uninit", p_uninit) &&
              resolve(dll, "ps3eye_count_connected", p_count) &&
              resolve(dll, "ps3eye_open", p_open) &&
              resolve(dll, "ps3eye_grab_frame", p_grab) &&
              resolve(dll, "ps3eye_close", p_close) &&
              resolve(dll, "ps3eye_grab_frame_timeout", p_grab_timeout) &&
              resolve(dll, "ps3eye_abort_frame", p_abort) &&
              resolve(dll, "ps3eye_get_frame_rate", p_frame_rate) &&
              resolve(dll, "ps3eye_psvr_patch_level", p_patch_level) &&
              p_patch_level() == 3;
    if (!ok) {
        FreeLibrary(dll);
        set_reason(reason, reason_len,
                   "ps3eye.dll lacks the required PSVR patch set (level 3); "
                   "build it with tools/psvr_pseye/build.ps1");
        return -2;
    }

    // Optional: unique identifier is only available on newer builds.
    resolve(dll, "ps3eye_get_unique_identifier", p_unique);
    resolve(dll, "ps3eye_set_parameter", p_set_parameter);
    resolve(dll, "ps3eye_get_parameter", p_get_parameter);

    g_dll = dll;
    p_init();
    return 0;
}

int psvr_pseye_count(void) {
    std::lock_guard<std::mutex> lock(g_load_mutex);
    if (g_dll == nullptr || p_count == nullptr) {
        return -1;
    }
    return p_count();
}

psvr_pseye_camera *psvr_pseye_open(int index, int width, int height, int fps,
                                   int format) {
    std::lock_guard<std::mutex> lock(g_load_mutex);
    if (g_dll == nullptr || p_open == nullptr) {
        return nullptr;
    }
    void *eye = p_open(index, width, height, fps, format);
    if (eye == nullptr) {
        return nullptr;
    }

    // Mirror PS3EYECam::init's mode selection exactly.
    int resolved_w;
    int resolved_h;
    if ((width == 0 && height == 0) || width > 320 || height > 240) {
        resolved_w = 640;
        resolved_h = 480;
    } else {
        resolved_w = 320;
        resolved_h = 240;
    }
    int bpp = (format == PSVR_PSEYE_FORMAT_BAYER ||
               format == PSVR_PSEYE_FORMAT_GRAY)
                  ? 1
                  : 3;

    // Report the frame rate the device actually negotiated, not the requested
    // value: upstream snaps the request to the nearest rate it supports.
    int actual_fps = p_frame_rate(eye);
    if (actual_fps <= 0) {
        actual_fps = fps;
    }

    psvr_pseye_camera *cam = new psvr_pseye_camera();
    cam->eye = eye;
    cam->width = resolved_w;
    cam->height = resolved_h;
    cam->fps = actual_fps;
    cam->format = format;
    cam->bytes_per_pixel = bpp;
    return cam;
}

void psvr_pseye_close(psvr_pseye_camera *cam) {
    if (cam == nullptr) {
        return;
    }
    if (cam->eye != nullptr) {
        // Wake a concurrent grab before freeing the handle so close() cannot
        // leave a reader blocked inside the DLL.
        if (p_abort != nullptr) {
            p_abort(cam->eye);
        }
        if (p_close != nullptr) {
            p_close(cam->eye);
        }
    }
    delete cam;
}

int psvr_pseye_set_parameter(psvr_pseye_camera *cam, int parameter, int value) {
    if (!cam || !cam->eye || parameter < 0 || parameter > 3) return -1;
    const int maximum = parameter == 1 ? 63 : parameter == 3 ? 255 : 1;
    if (value < 0 || value > maximum) return -1;
    return p_set_parameter ? p_set_parameter(cam->eye, parameter, value) : -2;
}

int psvr_pseye_get_parameter(psvr_pseye_camera *cam, int parameter) {
    if (!cam || !cam->eye || parameter < 0 || parameter > 3) return -1;
    return p_get_parameter ? p_get_parameter(cam->eye, parameter) : -2;
}

int psvr_pseye_grab_timeout(psvr_pseye_camera *cam, unsigned char *out,
                            int out_len, int timeout_ms) {
    if (cam == nullptr || cam->eye == nullptr || p_grab_timeout == nullptr ||
        out == nullptr) {
        return -1;
    }
    const int needed = cam->width * cam->height * cam->bytes_per_pixel;
    if (out_len < needed) {
        return -1;
    }
    int got = p_grab_timeout(cam->eye, out, timeout_ms);
    if (got != 1) {
        // 0 = timed out / stopped, -1 = invalid handle.
        return -1;
    }
    return needed;
}

int psvr_pseye_grab(psvr_pseye_camera *cam, unsigned char *out, int out_len) {
    if (cam == nullptr) {
        return -1;
    }
    return psvr_pseye_grab_timeout(cam, out, out_len, cam->grab_timeout_ms);
}

void psvr_pseye_set_grab_timeout(psvr_pseye_camera *cam, int timeout_ms) {
    if (cam == nullptr) {
        return;
    }
    cam->grab_timeout_ms = timeout_ms;
}

void psvr_pseye_abort(psvr_pseye_camera *cam) {
    if (cam == nullptr || cam->eye == nullptr || p_abort == nullptr) {
        return;
    }
    p_abort(cam->eye);
}

int psvr_pseye_width(const psvr_pseye_camera *cam) {
    return cam != nullptr ? cam->width : 0;
}
int psvr_pseye_height(const psvr_pseye_camera *cam) {
    return cam != nullptr ? cam->height : 0;
}
int psvr_pseye_fps(const psvr_pseye_camera *cam) {
    return cam != nullptr ? cam->fps : 0;
}
int psvr_pseye_bytes_per_pixel(const psvr_pseye_camera *cam) {
    return cam != nullptr ? cam->bytes_per_pixel : 0;
}

int psvr_pseye_unique_id(psvr_pseye_camera *cam, char *out, int out_len) {
    if (cam == nullptr || cam->eye == nullptr || p_unique == nullptr ||
        out == nullptr || out_len <= 0) {
        return -1;
    }
    return p_unique(cam->eye, out, out_len);
}

} // extern "C"
