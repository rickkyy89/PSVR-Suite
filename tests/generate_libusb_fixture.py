"""Extract tested bodies from the actual patched libusb source, not copies."""
from pathlib import Path
import sys


def block(text, start):
    begin = text.index(start)
    brace = text.index("{", begin)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[begin:end] + "\n"


source, output = map(Path, sys.argv[1:])
output.mkdir(parents=True, exist_ok=True)
windows = (source / "os/windows_winusb.c").read_text(encoding="utf-8")
start = windows.index("/* libusbK 3.0.7's libusb0 backend")
end = windows.index("windows_force_sync_completion(itransfer, transferred);", start)
end += len("windows_force_sync_completion(itransfer, transferred);")
(output / "control_result.inc").write_text(windows[start:end], encoding="utf-8")
sync = (source / "sync.c").read_text(encoding="utf-8")
(output / "control_copy_guard.inc").write_text(
    block(sync, "if (transfer->actual_length < 0 || transfer->actual_length > wLength)"),
    encoding="utf-8")
