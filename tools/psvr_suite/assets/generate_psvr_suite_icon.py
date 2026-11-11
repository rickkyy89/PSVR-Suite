"""Genera icona PSVR Suite (PNG + ICO) in modo riproducibile, solo stdlib.

Sorgente vettoriale di riferimento: psvr_suite.svg (stesso disegno,
ricostruito qui proceduralmente). Nessuna dipendenza Pillow/cv2.
Emette solo riepiloghi brevi; non stampa binari.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

QUI = Path(__file__).resolve().parent
SVG = QUI / "psvr_suite.svg"
PNG_OUT = QUI / "psvr_suite.png"
ICO_OUT = QUI / "psvr_suite.ico"

# Colori (R, G, B, A).
BLU_SFONDO = (11, 30, 51, 255)
BLU_VISORE = (18, 58, 92, 255)
CIANO = (56, 225, 255, 255)
MAGENTA = (255, 63, 212, 255)
VERDE = (61, 255, 138, 255)


def _rettangolo_arrotondato(buf, size, x0, y0, w, h, raggio, colore):
    """Riempie un rettangolo arrotondato nel buffer RGBA."""
    for y in range(max(0, y0), min(size, y0 + h)):
        for x in range(max(0, x0), min(size, x0 + w)):
            dx = min(x - x0, x0 + w - 1 - x)
            dy = min(y - y0, y0 + h - 1 - y)
            dentro = dx >= raggio or dy >= raggio or (dx * dx + dy * dy <= raggio * raggio)
            if dentro:
                buf[y * size + x] = colore


def _bordo_arrotondato(buf, size, x0, y0, w, h, raggio, spessore, colore):
    """Bordo di rettangolo arrotondato (disegno esterno meno interno)."""
    esterno = bytearray(size * size)
    interno = bytearray(size * size)
    for y in range(size):
        for x in range(size):
            in_ext = x0 <= x < x0 + w and y0 <= y < y0 + h
            if in_ext:
                dx = min(x - x0, x0 + w - 1 - x)
                dy = min(y - y0, y0 + h - 1 - y)
                if dx >= raggio or dy >= raggio or (dx * dx + dy * dy <= raggio * raggio):
                    esterno[y * size + x] = 1
            ix0, iy0 = x0 + spessore, y0 + spessore
            iw, ih = w - 2 * spessore, h - 2 * spessore
            ri = max(0, raggio - spessore)
            in_int = ix0 <= x < ix0 + iw and iy0 <= y < iy0 + ih
            if in_int:
                dx = min(x - ix0, ix0 + iw - 1 - x)
                dy = min(y - iy0, iy0 + ih - 1 - y)
                if dx >= ri or dy >= ri or (dx * dx + dy * dy <= ri * ri):
                    interno[y * size + x] = 1
    for i in range(size * size):
        if esterno[i] and not interno[i]:
            buf[i] = colore


def _disco(buf, size, cx, cy, r, colore):
    for y in range(max(0, cy - r), min(size, cy + r + 1)):
        for x in range(max(0, cx - r), min(size, cx + r + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                buf[y * size + x] = colore


def _disegna_base(size):
    """Disegna l'icona a lato `size` (solo 256 e' il master dettagliato)."""
    buf = [(0, 0, 0, 0)] * (size * size)
    scala = size / 256.0

    def s(v):
        return int(round(v * scala))

    # Sfondo arrotondato.
    _rettangolo_arrotondato(buf, size, s(8), s(8), s(240), s(240), s(52), BLU_SFONDO)
    _bordo_arrotondato(buf, size, s(8), s(8), s(240), s(240), s(52), max(1, s(6)), CIANO)
    # Visore.
    _rettangolo_arrotondato(buf, size, s(44), s(96), s(168), s(64), s(30), BLU_VISORE)
    _bordo_arrotondato(buf, size, s(44), s(96), s(168), s(64), s(30), max(1, s(8)), CIANO)
    # LED magenta.
    for cx in (76, 104, 128, 152, 180):
        _disco(buf, size, s(cx), s(128), max(1, s(9)), MAGENTA)
    # Arco guida verde: quadratica M64,176 Q128,200 192,176.
    r = max(1, s(4))
    t = 0.0
    while t <= 1.0:
        x = (1 - t) ** 2 * 64 + 2 * (1 - t) * t * 128 + t * t * 192
        y = (1 - t) ** 2 * 176 + 2 * (1 - t) * t * 200 + t * t * 176
        _disco(buf, size, s(x), s(y), r, VERDE)
        t += 0.01
    return buf


def _ridimensiona_box(buf_src, src_size, dst_size):
    """Downscale a blocchi (media); serve per 16/32/48 dall'originale 256."""
    buf = [(0, 0, 0, 0)] * (dst_size * dst_size)
    blocco = src_size // dst_size
    for y in range(dst_size):
        for x in range(dst_size):
            rs = gs = bs = a_s = 0
            n = 0
            for yy in range(y * blocco, (y + 1) * blocco):
                for xx in range(x * blocco, (x + 1) * blocco):
                    r, g, b, a = buf_src[yy * src_size + xx]
                    rs += r
                    gs += g
                    bs += b
                    a_s += a
                    n += 1
            buf[y * dst_size + x] = (rs // n, gs // n, bs // n, a_s // n)
    return buf


def _chunk(tipo: bytes, dati: bytes) -> bytes:
    crc = zlib.crc32(tipo)
    crc = zlib.crc32(dati, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", crc)


def _png_rgba(buf, size: int) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filtro None per ogni riga
        for x in range(size):
            r, g, b, a = buf[y * size + x]
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    firma = b"\x89PNG\r\n\x1a\n"
    return firma + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def _ico_png(png_per_taglia: dict[int, bytes]) -> bytes:
    taglie = sorted(png_per_taglia)
    testa = struct.pack("<HHH", 0, 1, len(taglie))
    offset = 6 + 16 * len(taglie)
    voci = bytearray()
    dati = bytearray()
    for taglia in taglie:
        png = png_per_taglia[taglia]
        lato = 0 if taglia >= 256 else taglia
        voci += struct.pack("<BBBBHHII", lato, lato, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        dati += png
    return testa + bytes(voci) + bytes(dati)


def main() -> int:
    master = _disegna_base(256)
    png_256 = _png_rgba(master, 256)
    PNG_OUT.write_bytes(png_256)
    per_ico = {256: png_256}
    for taglia in (16, 32, 48):
        buf = _ridimensiona_box(master, 256, taglia)
        per_ico[taglia] = _png_rgba(buf, taglia)
    ICO_OUT.write_bytes(_ico_png(per_ico))
    print(f"SVG sorgente: {SVG.name}")
    print(f"PNG: {PNG_OUT.name} 256x256 RGBA {len(png_256)} byte")
    print(f"ICO: {ICO_OUT.name} taglie {sorted(per_ico)} {len(ICO_OUT.read_bytes())} byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
