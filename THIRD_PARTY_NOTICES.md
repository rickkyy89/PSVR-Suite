# Third-party notices

The project MIT license, Copyright (c) 2026 rickkyy, applies to original
project code. It does **not** replace the licenses of third-party code,
adapted source, external runtimes, or repositories consulted as references.

Original notices are retained in `third_party/licenses/` (file elencati in
`manifest.json`), with source URLs, revisions/versions, categories, byte
counts, and SHA-256 hashes recorded there. Verify them without build caches, installed dependencies,
or network access:

```powershell
python tools/collect_licenses.py --check
```

## Build dependencies and adapted source

| Component | Pinned revision | License / retained notice |
|---|---|---|
| [HIDAPI](https://github.com/libusb/hidapi) 0.15.0 | `d6b2a974608dec3b76fb1e36c189f22b9cf3650c` | BSD-3-Clause option selected; all upstream license-choice files retained as `hidapi-LICENSE*` |
| [nlohmann/json](https://github.com/nlohmann/json) 3.12.0 | `55f93686c01528224f448c19128836e7df245f72` | MIT, `json-MIT.txt` |
| [OpenHMD](https://github.com/OpenHMD/OpenHMD) sensor packet decoder | `85075b0c7e3c723ded2577edb79d00ee11aac339` | Boost Software License 1.0, `BSL-1.0.txt`; Joey Ferwerda copyright retained in `sensor_packet.hpp` |
| [Valve OpenVR SDK](https://github.com/ValveSoftware/openvr) 2.15.6 | `0924064316de3effbcd1acf1e309182a2deb1c05` | BSD-3-Clause, `openvr-LICENSE.txt` |
| [PS3EYEDriver](https://github.com/inspirit/PS3EYEDriver) | `fe03494e648d0bf2aaa77869e5b50965e2e23620` | MIT new code and GPLv2-derived OV534 register tables; `PS3EYEDriver-LICENSE.txt`, `GPL-2.0.txt` |
| [libusb](https://github.com/libusb/libusb) v1.0.27 headers | `d52e355daa09f17ce64819122cb067b8a2ee0d4b` | LGPL-2.1-or-later, `LGPL-2.1.txt`; separately installed runtime may have another version |

The decoder is adapted source, not a linked OpenHMD fusion library. Original
orientation fusion and provider code remain project code. No OpenPSVR or
PSVRFramework source was copied into the implementation.

The PS3 Eye backend is built separately, with its pinned upstream source
and `tools/psvr_pseye/patches/0001-bounded-grab-and-actual-fps.patch`.
Its OV534 tables originate in Linux `drivers/media/usb/gspca/ov534.c`:
the upstream notice credits Antonio Ospite, Jim Paris, Jean-Francois Moine,
Mark Ferrell, Richard Kaswy, and Max Thrun. Dynamic loading does not remove
GPL/LGPL obligations. The MIT license on the original shim does not
relicense the camera backend.

This release artifact contains sources, patches, and notices, **not**
`ps3eye.dll`, libusb binaries, SteamVR DLLs, Python wheels, or installed
runtimes. A future binary camera distribution must also provide the
corresponding sources/build material and applicable licenses; simply
including a URL or an MIT project license is not a substitute.

## Study references — no implementation code included

Original license files are retained under `third_party/licenses/references/`
for provenance, not to claim these projects are incorporated or relicensed.

| Reference | Consulted revision | Notice |
|---|---|---|
| [OpenPSVR](https://github.com/alatnet/OpenPSVR) | `cb86fd7a2fc7cb47733074bb49856b3af3095357` | `OpenPSVR-LICENSE.txt`, AGPL-3.0 |
| [libpsvr](https://github.com/alatnet/libpsvr) | `7858e9cffcd17da5c9e26b301d9d4442a1e6de93` | `libpsvr-LICENSE.txt`, LGPL-3.0 |
| [PSVRFramework](https://github.com/gusmanb/PSVRFramework) | `ccda7ca840f8f820197dd8571bbc869a02563ea4` | `PSVRFramework-LICENSE.txt`, AGPL-3.0 |
| [mSparks43/PSVR-SteamVR-openHMD](https://github.com/mSparks43/PSVR-SteamVR-openHMD) | `3eb8a4d4a725698843976448e1966ed99fbf900a` | No repository-wide LICENSE at this revision; file-specific notices documented in `ARCHITECTURE.md`. No global license invented. |

Monado access was not verified during the original investigation; no Monado
code or runtime is included. Zadig is an external GPLv3 tool used manually
for Windows USB binding; it is neither bundled nor invoked by the build.

## Separately installed runtime and test dependencies

`third_party/licenses/runtime/` retains the original CPython 3.11.9 license,
Tcl/Tk notices, NumPy 2.4.6 notices including component-specific licenses,
OpenCV contrib headless 5.0.0.93 packaging and third-party license bundle,
and pytest 9.1.1's MIT notice. No runtime binaries are included.

- NumPy: BSD-3-Clause and licenses of its bundled components.
- OpenCV Python packaging: MIT; OpenCV library: Apache-2.0 and component
  terms recorded in the original 183 KiB `LICENSE-3RD-PARTY.txt`.
- CPython: PSF and historical licenses in the original license file.
- Tcl/Tk: original `license.terms` notices, retained separately.
- pytest: MIT, development/testing only.

Exact source/version/hash metadata is in the manifest. `collect` can refresh
the build dependency notices from local pinned checkouts and preserves
categorized runtime/reference notices when upstream checkouts are absent.
