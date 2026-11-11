/* SPDX-License-Identifier: MIT
 *
 * psvr_pseye - stable C ABI over the upstream PS3 Eye capture library.
 *
 * The shim source itself is MIT. It dynamically loads a separately built
 * ``ps3eye.dll`` (PS3EYEDriver, pinned commit below), which is MIT for newly
 * written code and GPLv2 for the ov534 register tables ported from the Linux
 * kernel. Dynamic loading does NOT make a distribution of the two artifacts
 * MIT-only: shipping psvr_pseye.dll together with ps3eye.dll is a combined
 * distribution that must satisfy the GPLv2 conditions for ps3eye.dll (complete
 * corresponding source and the full licence text). The stage directory carries
 * COPYING.GPL-2.0, COPYING.LGPL-2.1, the upstream LICENSE and NOTICE.txt for
 * that purpose. See tools/psvr_pseye/README.md.
 *
 * ps3eye.dll is built from the pinned upstream plus the in-repo patch
 * patches/0001-bounded-grab-and-actual-fps.patch, which adds a bounded grab,
 * an abort hook and an actual-frame-rate query. The shim requires those
 * exports plus the transfer-lifetime/idle-refresh patches (0002/0003).
 * The patch-level handshake rejects an outdated ps3eye.dll before init.
 *
 * The ABI is versioned; callers must check psvr_pseye_abi_version().
 */
#ifndef PSVR_PSEYE_H
#define PSVR_PSEYE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PSVR_PSEYE_ABI_VERSION 2

/* Output pixel formats. Values match upstream PS3EYECam::EOutputFormat. */
typedef enum {
    PSVR_PSEYE_FORMAT_BAYER = 0, /* 1 byte/pixel  */
    PSVR_PSEYE_FORMAT_BGR = 1,   /* 3 bytes/pixel */
    PSVR_PSEYE_FORMAT_RGB = 2,   /* 3 bytes/pixel */
    PSVR_PSEYE_FORMAT_GRAY = 3   /* 1 byte/pixel  */
} psvr_pseye_format;

typedef struct psvr_pseye_camera psvr_pseye_camera;

/* Return the ABI version compiled into the shim. */
int psvr_pseye_abi_version(void);

/* Load the upstream capture library.
 *
 * dll_dir: directory holding ps3eye.dll and its libusb-1.0.dll dependency, or
 *          NULL to let the OS search PATH. LoadLibraryEx with
 *          LOAD_WITH_ALTERED_SEARCH_PATH is used so ps3eye.dll resolves its
 *          own libusb-1.0.dll from the same directory.
 * reason : optional buffer receiving a human-readable failure reason.
 *
 * Returns 0 on success, negative on failure. Idempotent. */
int psvr_pseye_load(const char *dll_dir, char *reason, int reason_len);

/* Camera count, or -1 if not loaded. Refreshes the USB inventory while idle;
 * with live handles, preserves the active-index snapshot until they close. */
int psvr_pseye_count(void);

/* Open camera ``index`` at the requested mode. width/height of 0 select
 * 640x480; the upstream library snaps to 640x480 or 320x240. fps is a target
 * (see upstream rate tables). Returns NULL on failure. */
psvr_pseye_camera *psvr_pseye_open(int index, int width, int height, int fps,
                                   int format);

void psvr_pseye_close(psvr_pseye_camera *cam);

/* Grab the next frame into ``out`` (must hold width*height*bpp bytes), waiting
 * at most the camera's configured grab timeout (default 1000 ms). Returns the
 * number of bytes written, or -1 on timeout/error. The wait is always bounded:
 * a stalled or unplugged device can never block the caller forever. */
int psvr_pseye_grab(psvr_pseye_camera *cam, unsigned char *out, int out_len);

/* As psvr_pseye_grab, but waits at most ``timeout_ms`` for this call.
 * timeout_ms < 0 waits indefinitely (only for callers that manage their own
 * cancellation). Returns bytes written, or -1 on timeout/error. */
int psvr_pseye_grab_timeout(psvr_pseye_camera *cam, unsigned char *out,
                            int out_len, int timeout_ms);

/* Change the default wait used by psvr_pseye_grab. */
void psvr_pseye_set_grab_timeout(psvr_pseye_camera *cam, int timeout_ms);

/* Wake a thread blocked in psvr_pseye_grab/psvr_pseye_grab_timeout so it
 * returns -1. Safe to call from another thread; used by release() so a stalled
 * device does not force the handle to be abandoned. */
void psvr_pseye_abort(psvr_pseye_camera *cam);

int psvr_pseye_width(const psvr_pseye_camera *cam);
int psvr_pseye_height(const psvr_pseye_camera *cam);
/* The frame rate the device actually negotiated. The requested fps is snapped
 * to the nearest rate the sensor supports, so this may differ from the value
 * passed to psvr_pseye_open. Returns 0 if unknown. */
int psvr_pseye_fps(const psvr_pseye_camera *cam);
int psvr_pseye_bytes_per_pixel(const psvr_pseye_camera *cam);

/* Additive ABI 2 optional controls, matching pinned upstream ps3eye_parameter:
 * 0 auto gain (also auto exposure), 1 gain [0,63], 2 auto white balance,
 * 3 exposure [0,255]. Returns -2 if upstream exports are unavailable.
 * Caller must serialize with grab and close, preferably on the reader thread. */
int psvr_pseye_set_parameter(psvr_pseye_camera *cam, int parameter, int value);
int psvr_pseye_get_parameter(psvr_pseye_camera *cam, int parameter);

/* Unique per-port identifier (bus + port path). Returns 0 on success, -1 on
 * failure. */
int psvr_pseye_unique_id(psvr_pseye_camera *cam, char *out, int out_len);

/* Provenance strings; never NULL. */
const char *psvr_pseye_upstream_id(void);
const char *psvr_pseye_license_note(void);
const char *psvr_pseye_libusb_note(void);

#ifdef __cplusplus
}
#endif

#endif /* PSVR_PSEYE_H */
