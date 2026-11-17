"""Dessine les icônes de l'application installable (PWA), sans aucune dépendance.

    python tools/make_icons.py

Le logo de la page : un « H » blanc sur un carré vert arrondi, dégradé à 135°
de #12B76A à #0E7C66 (cf. `.brand-mark` dans app/templates/index.html).
Écrit dans app/static/icons/ :

- icon-192.png, icon-512.png   carré arrondi sur fond transparent (« any ») ;
- icon-maskable-512.png        fond plein, le « H » dans la zone sûre (cercle
                               de 80 %) : Android découpe la forme qu'il veut ;
- apple-touch-icon.png         180 px, fond plein (iOS arrondit lui-même) ;
- favicon-32.png               l'onglet du navigateur.

Rendu : couverture analytique en x, 8 sous-lignes en y (anticrénelage), PNG
RGBA écrit avec zlib + struct.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
TOP_LEFT = (0x12, 0xB7, 0x6A)
BOTTOM_RIGHT = (0x0E, 0x7C, 0x66)
SUB = 8  # sous-lignes par pixel


def _rounded_rect_span(y: float, x0: float, y0: float, x1: float, y1: float, r: float):
    """Intervalle [a, b] couvert par un rectangle arrondi sur la ligne y (ou None)."""
    if y < y0 or y > y1:
        return None
    if r <= 0:
        return (x0, x1)
    dy = 0.0
    if y < y0 + r:
        dy = y0 + r - y
    elif y > y1 - r:
        dy = y - (y1 - r)
    inset = r - math.sqrt(max(0.0, r * r - dy * dy)) if dy else 0.0
    return (x0 + inset, x1 - inset)


def _h_spans(y: float, size: int, h_ratio: float, w_ratio: float, stem_ratio: float, bar_ratio: float):
    """Intervalles couverts par le « H » (deux fûts et une traverse) sur la ligne y."""
    h, w = h_ratio * size, w_ratio * size
    stem, bar = stem_ratio * size, bar_ratio * size
    top, left = (size - h) / 2, (size - w) / 2
    if y < top or y > top + h:
        return []
    mid = top + h / 2
    if mid - bar / 2 <= y <= mid + bar / 2:
        return [(left, left + w)]
    return [(left, left + stem), (left + w - stem, left + w)]


def _overlap(px: int, spans) -> float:
    total = 0.0
    for a, b in spans:
        lo, hi = max(px, a), min(px + 1, b)
        if hi > lo:
            total += hi - lo
    return min(1.0, total)


def draw(size: int, *, rounded: bool, h_ratio: float, w_ratio: float, stem_ratio: float, bar_ratio: float) -> bytes:
    radius = size * 8 / 28 if rounded else 0.0  # même arrondi que .brand-mark (8 px sur 28)
    rows = []
    for py in range(size):
        cover_bg = [0.0] * size
        cover_h = [0.0] * size
        for s in range(SUB):
            y = py + (s + 0.5) / SUB
            span = _rounded_rect_span(y, 0, 0, size, size, radius)
            hs = _h_spans(y, size, h_ratio, w_ratio, stem_ratio, bar_ratio)
            for px in range(size):
                if span:
                    cover_bg[px] += _overlap(px, [span])
                if hs:
                    cover_h[px] += _overlap(px, hs)
        row = bytearray([0])  # filtre PNG « aucun »
        for px in range(size):
            bg = cover_bg[px] / SUB
            hv = min(cover_h[px] / SUB, bg) if rounded else cover_h[px] / SUB
            # Dégradé CSS à 135° sur un carré : t = (x + y) / (2 × côté).
            t = (px + 0.5 + py + 0.5) / (2 * size)
            base = [TOP_LEFT[i] + (BOTTOM_RIGHT[i] - TOP_LEFT[i]) * t for i in range(3)]
            if bg <= 0:
                row += b"\x00\x00\x00\x00"
                continue
            # Blanc par-dessus le vert, puis l'alpha du fond (bords arrondis).
            rgb = [round(c * (1 - hv / bg) + 255 * (hv / bg)) if bg else 0 for c in base]
            row += bytes(rgb) + bytes([round(255 * bg)])
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8 bits, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# Proportions du « H » : celles du logo (lettre très grasse, ~42 % du carré).
ANY = dict(h_ratio=0.42, w_ratio=0.37, stem_ratio=0.12, bar_ratio=0.105)
# Icône « maskable » : le H tient dans le cercle de sécurité (80 % du côté).
MASKABLE = dict(h_ratio=0.34, w_ratio=0.30, stem_ratio=0.097, bar_ratio=0.085)

ICONS = {
    "icon-192.png": (192, True, ANY),
    "icon-512.png": (512, True, ANY),
    "icon-maskable-512.png": (512, False, MASKABLE),
    "apple-touch-icon.png": (180, False, ANY),
    "favicon-32.png": (32, True, ANY),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (size, rounded, ratios) in ICONS.items():
        (OUT / name).write_bytes(draw(size, rounded=rounded, **ratios))
        print(f"{name} ({size} px)")


if __name__ == "__main__":
    main()
