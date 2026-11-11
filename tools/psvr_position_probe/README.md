# psvr_position_probe — read-only psvr.position listener

Windows x64 console probe that observes the versioned M9 position stream on
loopback UDP (127.0.0.1), for interoperability checks between
`tools/psvr_camera` senders and the driver's position input. It reuses the
driver code directly — `parse_position_packet`, `PositionReceiver` and
`PositionBridge` from `driver/position_receiver.hpp` / `position_protocol.hpp`
— so the tool's parsing, seated origin, recenter-epoch and freshness behaviour
cannot drift from the driver's. Other tools and tests share this same
protocol; the camera sender is `tools/psvr_camera/udp_sender.py`.

## Scope and safety

* **Receive-only.** No USB, no camera, no device enumeration, no driver
  registration and no SteamVR/OpenVR calls. The probe never sends a datagram.
* **Nothing invented.** Every reported number comes from a datagram actually
  received inside the bounded `--duration-ms` window. The first valid frame
  fixes the seated origin, so raw camera coordinates are never reported as a
  position; displacement is metres relative to that origin, in `psvr_world`.
* **Local timing only.** Tracking loss uses the receiver's monotonic clock and
  `--timeout-ms` (the sender's `host_time_ns`/`monotonic_s` are validated and
  kept for diagnostics but never trusted), exactly like the driver.
* **v1-only (B7-07).** Bound v2 requires `bind_driver`, which the probe never
  performs; with a v2-only sender expect no valid frames (exit 4).

## Usage

```powershell
.\psvr_position_probe.exe --help
.\psvr_position_probe.exe                          # 127.0.0.1:4271, 2 s, human summary
.\psvr_position_probe.exe --port 0 --duration-ms 5000 --timeout-ms 300 --json
```

| Option | Values | Meaning |
|---|---|---|
| `--port` | `0..65535`, default `4271` | loopback port to bind; `0` = ephemeral, the bound port is reported in the `ready` event |
| `--duration-ms` | `100..60000`, default `2000` | bounded observation window |
| `--timeout-ms` | `20..5000`, default `200` | local monotonic freshness timeout that turns a silent sender into tracking loss |
| `--json` | flag | emit JSON lines instead of the human summary |

Malformed or duplicate options are rejected **before any socket exists**.

## Stdout protocol

The first stdout line is always a machine-readable JSON `ready` handshake
written after the bind succeeds; automation waits for it instead of guessing
when the socket is up (deterministic, no flaky startup retries). With `--json`
the rest of stdout is one JSON object per line: `packet` (one per parsed
datagram), `state` (one per view transition, with the `last_seq` at the time
of the transition, which distinguishes local-timeout loss from sender-declared
loss) and a final `summary` with receiver counters (`datagrams`, `parsed`,
`malformed`, `oversized`), bridge counters (`accepted`, `invalid`,
`duplicates`, `out_of_order`, `gaps`, `resyncs`), `valid_snapshot_observations`,
`max_displacement_m`/`final_displacement_m` relative to the first seated
origin, `tracking_loss_observed` and `tracking_recovery_count`. Without
`--json` a human summary follows the `ready` line.

Exit codes: `0` valid position observed, `2` invalid CLI (no socket opened),
`3` bind failure or fatal socket error, `4` finished with no usable position.

## Build (integrated in the root `CMakeLists.txt`)

No OpenVR is needed: the receiver uses Winsock, `orientation_math.hpp` and
nlohmann/json only. The standalone target and `position_probe_interop` test
are included in the main build; the package is staged to `out/position_probe/`:

```cmake
add_executable(psvr_position_probe tools/psvr_position_probe/main.cpp)
target_include_directories(psvr_position_probe PRIVATE driver)
target_link_libraries(psvr_position_probe PRIVATE psvr_diag_core ws2_32)
if(MSVC)
    target_compile_options(psvr_position_probe PRIVATE /W4 /permissive- /utf-8)
else()
    target_compile_options(psvr_position_probe PRIVATE -Wall -Wextra -Wpedantic)
    target_link_options(psvr_position_probe PRIVATE -municode -static -static-libgcc -static-libstdc++)
endif()
if(BUILD_TESTING AND Python3_FOUND)
    add_test(NAME position_probe_interop COMMAND ${Python3_EXECUTABLE} -B
        "${CMAKE_SOURCE_DIR}/tests/check_position_probe.py" "$<TARGET_FILE:psvr_position_probe>")
endif()
```

## Test

```powershell
python -B tests\check_position_probe.py build\mingw\psvr_position_probe.exe
```

The test first checks CLI validation (exactly one JSON `error` event, never a
`ready` line, including a held-port bind failure with exit 3), then launches
the real process on an ephemeral port, waits for the `ready` handshake and
drives the interop through `tools/psvr_camera/udp_sender`: seated origin,
+0.1 m frames, silence longer than `--timeout-ms` (local-timeout loss), one
recovery frame, one `valid:false` frame and one malformed datagram. It asserts
the exact `packet`/`state` event sequence, all counters and the 0.1 m relative
displacement end to end. No hardware, no SteamVR, no shared fixed port.

Protocol contract: `tools/psvr_camera/PROTOCOL.md`,
`docs/MILESTONE_9_SOFTWARE.md`.
