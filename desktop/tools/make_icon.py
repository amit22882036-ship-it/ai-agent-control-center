"""Draws the application icon (the Lumen mark) and writes assets/icon.png and assets/icon.ico.

Pure standard library: the mark is rendered with 4x supersampling and saved as PNG images, which
the .ico file embeds at 16 to 256 pixels. Run it again only to change the icon."""
import math
from pathlib import Path
import struct
import zlib

INK = (14, 19, 32)
LIGHT = (247, 248, 251)
AMBER = (255, 181, 71)


def render(size):
    scale = 4
    big = size * scale
    pixels = [[(0, 0, 0, 0)] * big for _ in range(big)]
    radius = big * 0.27
    margin = big * 0.04

    def inside_round_rect(x, y):
        left, top, right, bottom = margin, margin, big - margin, big - margin
        cx = min(max(x, left + radius), right - radius)
        cy = min(max(y, top + radius), bottom - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 and left <= x <= right and top <= y <= bottom

    unit = big / 28
    dots = [((14, 8.6), 2.6, LIGHT), ((9.2, 19.4), 2.4, LIGHT), ((18.8, 19.4), 2.5, AMBER)]
    threads = [((14, 10.4), (14, 13.6)), ((14, 13.6), (9.2, 17.4)), ((14, 13.6), (18.8, 17.4))]
    glow = (18.8 * unit, 19.4 * unit)
    for y in range(big):
        for x in range(big):
            if not inside_round_rect(x + .5, y + .5):
                continue
            r, g, b = INK
            # The amber light pool.
            distance = math.hypot(x - glow[0], y - glow[1]) / (9.5 * unit)
            if distance < 1:
                weight = (1 - distance) ** 2 * .75
                r, g, b = (r + (AMBER[0] - r) * weight, g + (AMBER[1] - g) * weight, b + (AMBER[2] - b) * weight)
            for (x1, y1), (x2, y2) in threads:
                px, py = x / unit, y / unit
                dx, dy = x2 - x1, y2 - y1
                t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
                if math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)) < .8:
                    r, g, b = (r * .45 + LIGHT[0] * .55, g * .45 + LIGHT[1] * .55, b * .45 + LIGHT[2] * .55)
            for (cx, cy), dot_radius, color in dots:
                if math.hypot(x / unit - cx, y / unit - cy) <= dot_radius:
                    r, g, b = color
            pixels[y][x] = (int(r), int(g), int(b), 255)
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            total = [0, 0, 0, 0]
            for sy in range(scale):
                for sx in range(scale):
                    pr, pg, pb, pa = pixels[y * scale + sy][x * scale + sx]
                    total[0] += pr * pa
                    total[1] += pg * pa
                    total[2] += pb * pa
                    total[3] += pa
            alpha = total[3] / (scale * scale)
            if total[3]:
                row += bytes((int(total[0] / total[3]), int(total[1] / total[3]), int(total[2] / total[3]), int(alpha)))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    return png(size, b''.join(rows))


def png(size, raw):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


def ico(images):
    header = struct.pack('<HHH', 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b'', b''
    for size, image in images:
        entries += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return header + entries + data


if __name__ == '__main__':
    out = Path(__file__).resolve().parents[1] / 'assets'
    out.mkdir(exist_ok=True)
    images = [(size, render(size)) for size in (16, 24, 32, 48, 64, 128, 256)]
    (out / 'icon.png').write_bytes(dict(images)[256])
    (out / 'icon.ico').write_bytes(ico(images))
    print('wrote', out / 'icon.png', out / 'icon.ico')
