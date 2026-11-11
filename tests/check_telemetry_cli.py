# SPDX-License-Identifier: MIT
"""CLI tests: missing DLL only, never initialize the real runtime."""
import subprocess
import sys
import tempfile
from pathlib import Path

exe = Path(sys.argv[1]).resolve(strict=True)
with tempfile.TemporaryDirectory() as temp:
    parent = Path(temp)
    dll = parent / 'missing_openvr_api.dll'
    output = parent / 'telemetry.jsonl'
    base = ['--openvr-dll', str(dll), '--output', str(output)]
    def run(expected, args):
        result = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, (args, result.returncode, result.stderr)
        assert not output.exists(), 'offline CLI tests must never create telemetry files'
    run(0, ['--help'])
    run(2, [])
    run(2, ['--openvr-dll', 'relative.dll', '--output', str(output)])
    run(2, base + ['--sample-ms', '1'])
    run(2, base + ['--duration-ms', '720001'])
    run(2, base + ['--sample-ms', '10', '--sample-ms', '10'])
    run(2, base + ['--dashboard', 'unknown'])
    run(2, base + ['--synthetic-presence', '--synthetic-presence'])
    run(3, base)
    run(3, base + ['--synthetic-presence', '--dashboard', 'hidden'])
print('PASS continuous telemetry CLI: offline, bounded options, no runtime initialization')
