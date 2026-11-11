# psvr_pseye — native PS3 Eye capture (Windows, libusb-win32)

A small native module that captures real frames from a Sony PS3 Eye
(`VID_1415&PID_2000`) through the **libusb-win32** driver that is already bound
to the camera's video interface. It does **not** install, replace, rebind or
reflash any USB driver and does not require administrator rights.

It exists because DirectShow/OpenCV does not expose the PS3 Eye's native video
interface when interface 0 is bound to libusb-win32. The PS3 Eye delivers video
over a **bulk** endpoint (`0x81`, 512-byte packets); the upstream
`inspirit/PS3EYEDriver` already implements the exact OV534/OV772x register
initialisation and UVC-payload frame reassembly, so this module reuses it
instead of guessing register writes.

## Artifacts and licences

| Artifact | Licence | Role |
|---|---|---|
| `psvr_pseye.dll` | **MIT** (this repo) | Stable C ABI shim; loads `ps3eye.dll` at run time |
| `ps3eye.dll` | **MIT + GPLv2** | Upstream `inspirit/PS3EYEDriver` capture library |
| `libusb-1.0.dll` | **LGPL-2.1-or-later** | USB backend used by `ps3eye.dll` |

The shim uses `LoadLibraryExA(..., LOAD_WITH_ALTERED_SEARCH_PATH)` and
`GetProcAddress`, so it has **no link-time third-party dependency**. The shim
source is MIT. However, shipping `psvr_pseye.dll` together with `ps3eye.dll` is
a **combined distribution** and the GPLv2 conditions that cover `ps3eye.dll`
(the OV534 register tables ported from the Linux kernel) apply to it. Dynamic
loading does not remove that obligation. The stage therefore ships:

* `COPYING.GPL-2.0` — the complete GNU General Public License version 2,
* `COPYING.LGPL-2.1` — the complete GNU Lesser General Public License 2.1,
* `PS3EYEDriver-LICENSE.txt` — the upstream licence statement,
* `psvr_pseye-MIT.txt` — this repository's MIT licence,
* `NOTICE.txt` — provenance and the corresponding-source location,
* `ps3eye-bounded-grab.patch` — the in-repo patch applied to the upstream.

`psvr_pseye_upstream_id()` / `psvr_pseye_license_note()` /
`psvr_pseye_libusb_note()` report the pins and licence at run time.

### Pinned provenance

* `inspirit/PS3EYEDriver` commit `fe03494e648d0bf2aaa77869e5b50965e2e23620`
  (2017-06-16), plus the in-repo patch
  `patches/0001-bounded-grab-and-actual-fps.patch`. Licence: MIT for newly
  written code; GPLv2 for the register tables ported from the Linux `ov534`
  gspca driver. See its `LICENSE` and `COPYING.GPL-2.0`.
* `libusb/libusb` tag `v1.0.27` commit `d52e355daa09f17ce64819122cb067b8a2ee0d4b`
  (headers and, with `-BuildLibusb`, the patched runtime). Legacy builds may
  explicitly supply a system runtime, but ABI compatibility alone does not
  establish safe behaviour with libusb0/libusbK; see the regression below.

### The bounded-grab patch

`ps3eye.dll` is built from the pin **plus the tracked patch series**,
applied by CMake at configure
time to a copy of the upstream in the build tree (the fetched checkout is never
edited). The patch adds:

* `ps3eye_grab_frame_timeout()` / `PS3EYECam::tryGetFrame()` — a grab with a
  real timeout, so an unplugged or stalled device can never block forever,
* `ps3eye_abort_frame()` / `PS3EYECam::abortFrameWait()` — wakes a blocked
  grab, so `close`/`release` can stop a reader instead of abandoning the handle,
* `ps3eye_get_frame_rate()` — the frame rate the sensor actually negotiated,
  so the report never states a snapped request as if it were real.

`0002-transfer-fault-lifetime.patch` retires failed re-submissions exactly
once and prevents submission after the shutdown cancellation sweep. The owner
stays alive until the callback's final use.

`0003-refresh-idle-inventory.patch` serializes context open/count/close and
refreshes both device snapshots when no camera handles are open. With live
handles the inventory is deliberately the active-index snapshot; it does not
renumber or replace a running camera. Once all handles close, count or a new
explicit open refreshes the USB inventory. This is not auto-reconnect.

The shim requires `ps3eye_psvr_patch_level()==3` before calling init, so an
older DLL is rejected with a rebuild message without enumerating hardware.
Rebuild/stage the shim and upstream together. ABI 2 of the Python-facing shim
is unchanged. CMake recreates the known generated source inputs before
applying the series, so changing a patch also works in an existing build.

The shim requires those exports and rejects an outdated `ps3eye.dll` at load
time. This keeps the build reproducible: the only change to upstream is the
tracked patch series, and it is applied to a build-local copy.

The Python adapter (`tools/psvr_camera/pseye_backend.py`) also accepts any other
`libusb-1.0.dll` already present on the machine.

### libusb0 control-transfer regression (2026-11-01)

On this machine the existing libusbK 3.0.7 userspace backend can return immediate
success without initializing the overlapped control request's output length.
Both the Steam libusb 1.0.29 and an unpatched source-built 1.0.27 reproduced it:
a one-byte OV534 register request was published as 499998300 bytes, while its
real IOCP completion reported one byte. The synchronous IN copy then overran
the native buffer during `open`, before Python allocated an image buffer.
Replugging or changing the audio interface does not fix this software defect.

`patches/libusb-0001-control-overlapped-length.patch` queries the completed
OVERLAPPED result for libusb0, leaves incomplete requests to IOCP, and bounds
the synchronous copy against `wLength`. No kernel driver is changed.
`build_libusb.ps1` builds the pinned libusb source plus this patch in a separate
build-local copy. To reproduce with the installed MSVC 2022 Build Tools:

```powershell
tools/psvr_pseye/build.ps1 -BuildLibusb -Generator 'Visual Studio 17 2022'
```

The optional `PSVR_PSEYE_PATCHED_LIBUSB_SOURCE` CMake path enables native
device-free tests extracted from the actual patched dependency bodies. These
cover garbage lengths, completed/pending/failed queries, unaffected backends,
and synchronous copy bounds. Do not replace the patched runtime with the Steam
DLL on this setup. For redistribution, include the libusb patch and the pinned
corresponding source as well as its LGPL notice.

## Build

Prerequisites (all source-built from the pins above; no prebuilt binaries are
downloaded):

* A C/C++ toolchain (MSVC or MinGW-w64) and CMake ≥ 3.24.
* `git` for the pinned source fetch and to apply the patch.
* A `libusb-1.0.dll` to link `ps3eye.dll` against (system or source-built).
* With MSVC, the matching import library `libusb-1.0.lib` as well (MSVC cannot
  link a `.dll` directly; pass `-DPSVR_PSEYE_LIBUSB_LIB=...` or place the
  `.lib` next to the `.dll`).

```powershell
# Fetches the pinned sources into tools/psvr_pseye/.deps and builds:
#   .deps/PS3EYEDriver, .deps/libusb
powershell -NoProfile -ExecutionPolicy Bypass -File tools/psvr_pseye/build.ps1 `
    -ToolchainBin "C:\path\to\mingw\bin"

# Output is staged into out/pseye/:
#   out/pseye/psvr_pseye.dll
#   out/pseye/ps3eye.dll
#   out/pseye/libusb-1.0.dll
#   out/pseye/NOTICE.txt, COPYING.GPL-2.0, COPYING.LGPL-2.1, ...
```

If you already have libusb, skip the fetch and point at it:

```powershell
powershell -File tools/psvr_pseye/build.ps1 `
    -LibusbDll "C:\Program Files (x86)\Steam\libusb-1.0.dll" `
    -LibusbInclude "C:\deps\libusb\libusb"
```

CMake options:

* `PSVR_PSEYE_BUILD_UPSTREAM` (default `ON`) — build `ps3eye.dll` from the pin.
  Set `OFF` to build only the MIT shim and supply `ps3eye.dll` yourself.
* `PSVR_PSEYE_UPSTREAM_TAG` — override the pinned upstream commit.
* `PSVR_PSEYE_LIBUSB_DLL` / `PSVR_PSEYE_LIBUSB_INCLUDE_DIR` — link inputs.
  If unset, CMake searches the known Steam/FreeCAD locations and
  `PSVR_PSEYE_LIBUSB_DIR`.
* `PSVR_PSEYE_LIBUSB_LIB` — the MSVC import library (`libusb-1.0.lib`).
* `PSVR_PSEYE_STAGE` — staging directory (default `<repo>/out/pseye`).

With `BUILD_TESTING=ON` (default), Python 3.11+ generates small fixtures from
the actual patched source. `psvr_pseye_transfer_tests` and
`psvr_pseye_inventory_tests` exercise those native bodies with device-free
libusb/enumeration doubles, even if libusb link inputs are not configured.
`pseye_upstream_patch_guard` additionally verifies the real shim loader rejects
a legacy test DLL before its init is called, without enumerating devices.
Use `ctest --test-dir <build> -C Release --output-on-failure`. A DLL-only build
can set `BUILD_TESTING=OFF`. MSVC exports the upstream C API explicitly via
`WINDOWS_EXPORT_ALL_SYMBOLS`.

The parent build can `add_subdirectory(tools/psvr_pseye)`; the module also
builds standalone. The root `CMakeLists.txt` is owned by the parent and is not
modified by this work.

## Runtime layout

```
out/pseye/
  psvr_pseye.dll
  ps3eye.dll
  libusb-1.0.dll
  NOTICE.txt
  COPYING.GPL-2.0
  COPYING.LGPL-2.1
  PS3EYEDriver-LICENSE.txt
  psvr_pseye-MIT.txt
  ps3eye-bounded-grab.patch
  0001-bounded-grab-and-actual-fps.patch
  0002-transfer-fault-lifetime.patch
  0003-refresh-idle-inventory.patch
```

The Python adapter finds the shim in this order:

1. `PSVR_PSEYE_DLL` (authoritative if set),
2. `PSVR_PSEYE_DIR`,
3. `<repo>/out/pseye/psvr_pseye.dll`,
4. `tools/psvr_pseye/build/psvr_pseye.dll`,
5. `<repo>/build/pseye/psvr_pseye.dll`.

## Use

```powershell
# Read-only inventory (DLL provenance + connected count, no capture):
python -m psvr_camera pseye inventory

# Bounded capture of one explicit index; JSON report with achieved fps:
python -m psvr_camera pseye probe --index 0 --width 640 --height 480 --fps 60 --frames 120
python -m psvr_camera pseye probe --index 0 --width 320 --height 240 --fps 187 --frames 240
```

The report uses the existing `psvr.camera.capture` schema (negotiated mode,
frames captured/dropped, timeouts, real duration, achieved fps, errors). The
`negotiated.fps` value is the rate the sensor actually locked to, which may be
lower than the requested value (e.g. 640x480 @ 45 snaps to 40). Every grab is
bounded, so the probe honours `--max-seconds` even if the camera stops
delivering frames.

## Verified hardware result

Measured on the target machine, PS3 Eye interface 0 bound to libusb-win32,
through the Python adapter (`python -m psvr_camera pseye probe`), steady-state
capture loop (`duration_s` excludes open/init/warmup):

| Requested | Negotiated | Captured | Achieved fps |
|---|---|---|---|
| 640x480 @ 60 | 60 | 120 / 120 | 58.3 |
| 640x480 @ 75 | 75 | 120 / 120 | 72.3 |
| 320x240 @ 60 | 60 | 120 / 120 | 58.2 |
| 320x240 @ 187 | 187 | 240 / 240 | 178.3 |

These are real frames: a 640x480 gray grab returned 307200 bytes with 306760
non-zero bytes and a mean of 17.4. The upstream C loop measured directly is
slightly higher (59.6 / 74.6 / 59.6 / 187.2 fps); the difference is Python/ctypes
per-frame overhead.
