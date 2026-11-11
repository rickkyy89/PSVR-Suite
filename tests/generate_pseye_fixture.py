"""Extract pinned, patched production bodies for device-free C++ tests."""
import sys
from pathlib import Path

source, output = map(Path, sys.argv[1:3])
text = (source / "src/ps3eye.cpp").read_text(encoding="utf-8-sig")

def extract(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end] + "\n"

output.mkdir(parents=True, exist_ok=True)
(output / "transfer_methods.inc").write_text("\n".join(extract(text, sig) for sig in
    ("void cancel_all()", "void close_transfers()", "void transfer_canceled()")), encoding="utf-8")
(output / "transfer_callback.inc").write_text(extract(text,
    "static void LIBUSB_CALL transfer_completed_callback(struct libusb_transfer *xfr)\n{"), encoding="utf-8")
text = (source / "src/ps3eye_capi.cpp").read_text(encoding="utf-8-sig")
(output / "inventory_context.inc").write_text(extract(text,"struct ps3eye_context_t {")+";\n", encoding="utf-8")
(output / "inventory_camera.inc").write_text(extract(text,"struct ps3eye_t {")+";\n", encoding="utf-8")
(output / "inventory_calls.inc").write_text("\n".join(extract(text, sig) for sig in (
    "static void refresh_devices_if_idle()", "void\nps3eye_init()", "void\nps3eye_uninit()",
    "int\nps3eye_count_connected()", "ps3eye_t *\nps3eye_open(", "void\nps3eye_close(")), encoding="utf-8")
