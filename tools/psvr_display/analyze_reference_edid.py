# SPDX-License-Identifier: MIT
"""Read-only, bounded inspection of the historical PSVR EDID ZIP (stdlib only)."""
import argparse
import hashlib
import io
import json
import re
import urllib.request
import zipfile

REFERENCE_URL = "https://github.com/gusmanb/PSVRFramework/files/605425/psvr_120.zip"
WIKI_URL = "https://github.com/gusmanb/PSVRFramework/wiki/Video-routing-and-EDIDs"
LIMIT = 2 * 1024 * 1024


def timing(d, source):
    clock = int.from_bytes(d[:2], "little") * 10000
    if not clock:
        return None
    ha, hb = d[2] | ((d[4] & 240) << 4), d[3] | ((d[4] & 15) << 8)
    va, vb = d[5] | ((d[7] & 240) << 4), d[6] | ((d[7] & 15) << 8)
    hs = d[8] | ((d[11] & 192) << 2)
    hw = d[9] | ((d[11] & 48) << 4)
    vs = (d[10] >> 4) | ((d[11] & 12) << 2)
    vw = (d[10] & 15) | ((d[11] & 3) << 4)
    total = (ha + hb) * (va + vb)
    hz = clock / total if total else None
    interlace = bool(d[17] & 128)
    return dict(source=source, pixel_clock_hz=clock, hactive=ha, hblank=hb,
                vactive=va, vblank=vb, htotal=ha + hb, vtotal=va + vb,
                hsync_offset=hs, hsync_width=hw, vsync_offset=vs, vsync_width=vw,
                hsync_start=ha + hs, hsync_end=ha + hs + hw,
                vsync_start=va + vs, vsync_end=va + vs + vw,
                flags_hex=f"{d[17]:02x}", interlaced=interlace,
                sync_type=(d[17] >> 3) & 3,
                separate_hsync_positive=bool(d[17] & 2) if d[17] & 24 == 24 else None,
                separate_vsync_positive=bool(d[17] & 4) if d[17] & 24 == 24 else None,
                refresh_hz=hz, field_rate_hz=hz * 2 if hz and interlace else hz,
                horizontal_khz=clock / (ha + hb) / 1000 if ha + hb else None)


def decode_edid(data):
    warnings, timings, ranges, descriptors, extensions = [], [], [], [], []
    if len(data) < 128:
        raise ValueError("EDID shorter than base block")
    if len(data) % 128:
        warnings.append("Length is not a multiple of 128 bytes")
    blocks = [data[i:i + 128] for i in range(0, len(data) - 127, 128)]
    header = data[:8] == bytes.fromhex("00ffffffffffff00")
    if not header:
        warnings.append("Invalid EDID header")
    checks = [dict(index=i, checksum_valid=sum(b) % 256 == 0) for i, b in enumerate(blocks)]
    if not all(b["checksum_valid"] for b in checks):
        warnings.append("Invalid block checksum")
    if len(blocks) != data[126] + 1:
        warnings.append("Declared extension count differs from supplied blocks")
    manufacturer = int.from_bytes(data[8:10], "big")
    m = "".join(chr(64 + ((manufacturer >> shift) & 31)) for shift in (10, 5, 0))
    product = int.from_bytes(data[10:12], "little")
    identity = f"{m}{product:04X}"
    if identity != "SNY6A04":
        warnings.append(f"Reference identity {identity} differs from current SNY6A04; full binary import substitutes reference EDID identity")
    for offset in range(54, 126, 18):
        d = data[offset:offset + 18]
        t = timing(d, f"base:{offset}")
        if t:
            timings.append(t)
        elif d[:3] == b"\0\0\0":
            descriptors.append(dict(tag_hex=f"{d[3]:02x}", raw_hex=d.hex()))
            if d[3] == 253:
                ranges.append(dict(min_vertical_hz=d[5], max_vertical_hz=d[6],
                                   min_horizontal_khz=d[7], max_horizontal_khz=d[8],
                                   max_pixel_clock_mhz=d[9] * 10,
                                   formula_tag=d[10], raw_hex=d.hex()))
                if data[18:20] != b"\x01\x03":
                    warnings.append("Range fields shown raw; EDID 1.4 offsets/extended clock precision are not interpreted")
    # VIC labels are explicit CTA-defined modes, not timings inferred from a name.
    vic_labels = {1: "640x480p60", 2: "720x480p60", 3: "720x480p60", 4: "1280x720p60",
                  5: "1920x1080i60", 16: "1920x1080p60", 17: "720x576p50",
                  18: "720x576p50", 19: "1280x720p50", 20: "1920x1080i50",
                  31: "1920x1080p50", 32: "1920x1080p24", 63: "1920x1080p120",
                  64: "1920x1080p100"}
    for i, b in enumerate(blocks[1:], 1):
        ext = dict(index=i, tag=b[0], raw_hex=b.hex(), data_blocks=[])
        extensions.append(ext)
        if b[0] != 2:
            warnings.append(f"Unknown/unparsed extension tag {b[0]} at block {i}")
            continue
        ext.update(revision=b[1], dtd_offset=b[2], flags_hex=f"{b[3]:02x}")
        end = b[2]
        if end == 0:
            continue
        if not 4 <= end <= 127:
            warnings.append(f"Invalid CTA DTD offset at block {i}")
            continue
        pos = 4
        while pos < end:
            tag, length = b[pos] >> 5, b[pos] & 31
            if pos + 1 + length > end:
                warnings.append(f"Truncated CTA data block at {i}:{pos}")
                break
            payload = b[pos + 1:pos + 1 + length]
            entry = dict(tag=tag, payload_hex=payload.hex())
            if tag == 2:
                entry["video_modes"] = [dict(vic=x & 127, native=bool(x & 128),
                                                 label=vic_labels.get(x & 127)) for x in payload]
                for x in payload:
                    if x & 127 not in vic_labels:
                        warnings.append(f"Unparsed CTA VIC {x & 127}")
            elif tag == 3 and len(payload) >= 3 and payload[:3] == b"\x03\x0c\x00":
                entry["vendor"] = "HDMI Licensing (OUI 000C03)"
                entry["max_tmds_clock_mhz"] = payload[6] * 5 if len(payload) >= 7 else None
                warnings.append("HDMI vendor block only partially decoded; raw payload retained")
            else:
                warnings.append(f"CTA data block tag {tag} retained raw, not semantically decoded")
            ext["data_blocks"].append(entry)
            pos += 1 + length
        for offset in range(end, 110, 18):
            t = timing(b[offset:offset + 18], f"CTA:{i}:{offset}")
            if t:
                timings.append(t)
    candidates = [t for t in timings if not t["interlaced"] and t["refresh_hz"] is not None
                  and any(abs(t["refresh_hz"] - hz) < 0.5 for hz in (90, 120))]
    if not ranges:
        warnings.append("No FD monitor range descriptor; absence is not evidence of unlimited hardware capability")
    standard = []
    for offset in range(38, 54, 2):
        a, b = data[offset:offset + 2]
        if (a, b) == (1, 1):
            continue
        width = (a + 31) * 8
        ratios = ((1, 1) if data[18:20] < b"\x01\x03" else (16, 10), (4, 3), (5, 4), (16, 9))
        num, den = ratios[b >> 6]
        standard.append(dict(hactive=width, vactive=width * den // num,
                             refresh_hz=(b & 63) + 60, source=f"standard:{offset}"))
    for t in candidates:
        for r in ranges:
            if (t["refresh_hz"] > r["max_vertical_hz"] or
                    t["horizontal_khz"] > r["max_horizontal_khz"] or
                    t["pixel_clock_hz"] > r["max_pixel_clock_mhz"] * 1000000):
                warnings.append(f"Candidate {t['source']} exceeds reference FD range/clock limits")
    return dict(size=len(data), sha256=hashlib.sha256(data).hexdigest(), header_valid=header,
                blocks=checks, declared_extensions=data[126], identity=identity,
                manufacturer=m, product_hex=f"{product:04X}",
                serial_number=int.from_bytes(data[12:16], "little"),
                manufacture_week=data[16], manufacture_year=1990 + data[17],
                version=f"{data[18]}.{data[19]}", ranges=ranges, descriptors=descriptors,
                timings=timings, refresh_candidates_90_120=candidates,
                established_timings_raw=data[35:38].hex(), standard_timings_raw=data[38:54].hex(),
                standard_timings=standard,
                extensions=extensions, warnings=warnings)


def inspect_archive(raw):
    if len(raw) > LIMIT:
        raise ValueError("Archive exceeds 2 MiB limit")
    report = dict(archive_size=len(raw), archive_sha256=hashlib.sha256(raw).hexdigest(), members=[])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 128 or sum(i.file_size for i in infos) > LIMIT:
            raise ValueError("Archive member count or uncompressed size exceeds safety limit")
        for info in infos:
            member = dict(name=info.filename, size=info.file_size, compressed_size=info.compress_size)
            report["members"].append(member)
            suffix = info.filename.lower().rsplit(".", 1)[-1]
            if info.is_dir() or suffix not in ("bin", "inf"):
                member["inspection"] = "Skipped; never extracted or executed"
                continue
            with archive.open(info) as stream:
                data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("Expanded member exceeds safety limit")
            member["sha256"] = hashlib.sha256(data).hexdigest()
            if suffix == "bin":
                member["edid"] = decode_edid(data)
            else:
                text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
                member["inert_text"] = text
                overrides = {}
                for line in text.replace("\\\r\n", "").replace("\\\n", "").splitlines():
                    match = re.match(r'^\s*HKR\s*,\s*"?EDID_OVERRIDE"?\s*,\s*"?(\d+)"?\s*,\s*(?:0x)?0*1\s*,(.*)$', line, re.I)
                    if match:
                        tokens = [t.strip() for t in match[2].split(";", 1)[0].split(",")]
                        if all(re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{2}", t) for t in tokens):
                            overrides[int(match[1])] = bytes(int(t, 16) for t in tokens)
                member["override_block_indices"] = sorted(overrides)
                if overrides and sorted(overrides) == list(range(len(overrides))):
                    member["override_edid"] = decode_edid(b"".join(overrides[i] for i in sorted(overrides)))
                else:
                    member["warning"] = "No complete contiguous EDID override decoded; inspect inert text"
    return report


def fetch_reference(url):
    if not url.lower().startswith("https://"):
        raise ValueError("HTTPS reference URL required")
    request = urllib.request.Request(url, headers={"User-Agent": "PSVR-read-only-EDID-analysis/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.geturl().lower().startswith("https://"):
            raise ValueError("Non-HTTPS final URL rejected")
        length = response.headers.get("Content-Length")
        if length and int(length) > LIMIT:
            raise ValueError("Download exceeds 2 MiB limit")
        # Read one byte past the limit to distinguish an exact-LIMIT file from overflow.
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Download reaches 2 MiB safety limit")
        return raw, response.geturl()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-url", nargs="?", const=REFERENCE_URL,
                        help="Explicit network opt-in; omit value to use the official wiki ZIP")
    args = parser.parse_args()
    if args.reference_url is None:
        parser.error("No network access without explicit --reference-url")
    try:
        raw, final_url = fetch_reference(args.reference_url)
        report = inspect_archive(raw)
        report.update(reference_url=args.reference_url, final_url=final_url, wiki_url=WIKI_URL,
                      safety="In-memory inert analysis only; no profile applied or files extracted")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        print(json.dumps(dict(error=str(error), reference_url=args.reference_url)))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
