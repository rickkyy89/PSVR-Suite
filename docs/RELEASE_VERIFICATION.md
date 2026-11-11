# Local release verification — 0.3.0-alpha

Copyright (c) 2026 rickkyy. The project remains a prerelease.

## Scope

This source snapshot consolidates the driver, Suite GUI, camera tools,
tracking continuity, optical/projection trial tools, launcher/icon, and
the current user-tested defaults. It adds live Italian/English selection,
persisted separately from driver preferences, without restarting tracking.
Existing saved preferences remain respected.

MIT applies to original project code. Third-party licenses and attribution
remain intact; 36 original notice files are verified through their manifest.
The PayPal donation URL is intentionally not configured yet.

## Performed checks

| Check | Result |
|---|---|
| Windows x64 / VS2022 RelWithDebInfo local configure and build | PASS |
| Local complete CTest | 63/63 PASS |
| Affected license, packaging, EDID, refresh and Suite CTest rerun | 6/6 PASS |
| Final Python from isolated source extraction, before native compilation | 912 passed, 8 skipped, 287 subtests passed |
| Source-extraction headless Suite imports/schema | PASS |
| Source-extraction license verification without build caches/network | 36/36 PASS |
| Isolated source configure with pinned dependency source cache | PASS |
| Isolated source native build, not deployed into installed SteamVR driver | PASS |
| Isolated source complete CTest after native build | 63/63 PASS |
| Real isolated Tk IT→EN→IT switch with mocked backends | PASS |
| Final Suite CTest after aligning the initial setup utility with identity | 1/1 PASS |

The real Tk check preserved unsaved values and widget identities, persisted
only a temporary UI-language file, and made zero hardware calls. The source
Python run skips the native optics-validator test before compilation because
no native executable is bundled, alongside the applicable optional-path skips.

The isolated MSVC build emits MSB8029 because its output directory is under
the temporary directory. Build and CTest succeed; this is not a claim of a
warning-free compilation in temporary storage.

## Clean-checkout issues fixed

- EDID/refresh Python tests previously loaded captures from ignored `logs/`.
  They now use small, synthetic versioned fixtures without machine-specific
  registry paths or captured user logs.
- Offline ownership tests use an isolated per-process mutex name, while
  production keeps its original exclusive device-session mutex.
- Projection script tests mock runtime detection for temporary fixtures and
  explicitly verify the real production guard remains enforced.
- Driver-script tests handle expected child PowerShell stderr in a source
  checkout without generated driver packages.
- Source export retains OpenVR manifests/default settings and original
  notices; it excludes binaries, generated outputs, user preferences, venv,
  logs, caches, and Drive staging directories at every depth.

## Artifacts and reproduction

```powershell
.\.venv-camera\Scripts\python.exe tools/prepare_release.py
.\.venv-camera\Scripts\python.exe tools/verify_release.py dist/psvr-suite-0.3.0-alpha-source.zip
```

The first command produces a source ZIP plus SHA-256 sidecar. The archive
contains `RELEASE_MANIFEST.json` with every source file's size/hash. The second
verifies those hashes and runs license, headless, and Python checks from a
fresh temporary extraction. Use `--cmake <cmake.exe> --dependency-cache
<existing-build/_deps> --build` for the additional isolated native build and
CTest, as done during verification. Dependency checkouts are the pinned
HIDAPI, JSON, and OpenVR revisions documented in the notices and CMake file.

Release candidates used for verification remain under ignored `dist/candidate*`;
the artifact intended for hand-off is the ZIP directly under `dist/`.

No Git commit, tag, remote push, GitHub publication, or GitHub-hosted CI run
was performed. Hardware/gameplay findings and the recurring dashboard resize
-202 remain documented limitations; offline checks do not certify their fix.
