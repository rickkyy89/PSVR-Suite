# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import subprocess
import sys
import tempfile

exe, fixture = Path(sys.argv[1]), Path(sys.argv[2])

def run(expected, *args):
    result = subprocess.run([str(exe), *map(str, args)], capture_output=True,
                            text=True, encoding="utf-8", check=False)
    assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
    return result

assert "Offline optical profile validation" in run(0, "--help").stdout
run(1)
run(1, "--validate")
run(1, "--invalid")
run(1, "--validate", fixture, "--bad")
summary = json.loads(run(0, "--validate", fixture, "--json").stdout)
assert summary["math_valid"] and summary["sdk_usable"]
assert summary["optical_acceptance_verified"] is False
assert summary["mapping"] == "display-to-render"
with tempfile.TemporaryDirectory(prefix="psvr_optics_cli_") as temp:
    root = Path(temp)
    run(3, "--validate", root / "missing.json")
    malformed = root / "malformed.json"
    malformed.write_text('{"schema_version":1}', encoding="utf-8")
    run(3, "--validate", malformed)
    unicode_path = root / "profil-é-測試.json"
    unicode_path.write_bytes(fixture.read_bytes())
    run(0, "--validate", unicode_path)
    collapsed = json.loads(fixture.read_text(encoding="utf-8"))
    for eye in collapsed["eyes"].values():
        eye["polynomial"]["d"] = 2e-8
        eye["rgb_scale"] = {"red": .001, "green": .001, "blue": .001}
    bad_sdk = root / "collapsed.json"
    bad_sdk.write_text(json.dumps(collapsed), encoding="utf-8")
    data = json.loads(run(4, "--validate", bad_sdk, "--json").stdout)
    assert data["math_valid"] and not data["sdk_usable"] and data["sdk_error"]
print("PASS offline optical CLI arguments, schema, SDK policy, Unicode and JSON reporting")
