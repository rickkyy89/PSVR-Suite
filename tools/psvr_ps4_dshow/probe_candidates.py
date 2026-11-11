"""Parent-reviewed, separately launched read-only selected-PS4 metadata probe.

Never sets the hardware enable gate. No SetFormat, controls, Run or images.
"""
import argparse
import json
import os
from pathlib import Path
import threading

from tools.psvr_camera.ps4_owned_dshow import OwnedDshowCapture, OwnedDshowOpenError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--expected-moniker", required=True,
        help="exact IMoniker::GetDisplayName from prior metadata-only DirectShow enumeration; NOT DevicePath/PnP ID")
    parser.add_argument("--dll", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=4)
    parser.add_argument("--operator-owner-release-attested", action="store_true", required=True,
        help="operator attestation after parent process/owner checks, NOT an OS lock or global exclusivity proof")
    args = parser.parse_args(argv)
    approved = (Path(os.environ["LOCALAPPDATA"]) / "Temp" / "opencode").resolve()
    output = Path(args.output).resolve()
    if not output.is_relative_to(approved) or output.suffix.lower() != ".json":
        parser.error("metadata output must be an explicit .json under LOCALAPPDATA\\Temp\\opencode")
    output.parent.mkdir(parents=True, exist_ok=True)
    source = None
    error = None
    try:
        source = OwnedDshowCapture.inspect_index(args.index, args.dll,
            expected_moniker=args.expected_moniker,
            operator_owner_release_attested=args.operator_owner_release_attested, timeout_s=args.timeout)
    except Exception as exc:
        if isinstance(exc, OwnedDshowOpenError):
            source = exc.source
        error = str(exc)[:400]
    finally:
        if source is not None:
            try:
                source.release(timeout_s=0.5)
            except Exception as exc:
                error = error or str(exc)[:400]
        report = source.report() if source is not None else {}
        diagnostics = report.get("negotiation_diagnostics", [])
        if error is None and (not diagnostics or any(row.get("event") == "diagnostic_copy_failed" for row in diagnostics)):
            error = "probe metadata unavailable; no negotiation diagnosis established"
        report.update(probe="read_only_selected_ps4_candidates", error=error or report.get("error"),
                      identity_kind="IMoniker::GetDisplayName",
                      operator_owner_release_attested=args.operator_owner_release_attested,
                      cross_process_exclusivity_established=False,
                      process_id=os.getpid(), frames_requested=0, settings_controls_requested=False)
        try:
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except Exception as exc:
            print("metadata save failed: " + type(exc).__name__, flush=True)
            error = error or "metadata save failed"
        pending = source is not None and source.cleanup_pending
        print(json.dumps({"metadata_path": str(output), "error": error,
                          "cleanup_pending": pending, "pid": os.getpid()}), flush=True)
        if pending:
            # Keep owner/apartment alive. Parent must not retry, detach or kill.
            print("RETAINED OWNER: no retry/forcekill; owner cleanup still pending", flush=True)
            threading.Event().wait()
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
