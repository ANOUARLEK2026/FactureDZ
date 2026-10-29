# -*- coding: utf-8 -*-
"""Developer helper: draw ``pdf_invoice.build_layout`` ops to a PNG with Pillow (no Qt needed).

    python -m tests.layout_preview out.png [items]

Text is shaped with Raqm when available, so Arabic looks right. This is a *preview of the layout*
(geometry, wrapping, overflow); the real PDF is painted by Qt in ``pdf_invoice.render_invoice_pdf``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFont

from facturedz import pdf_invoice as pi

_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_cache: Dict[tuple, ImageFont.FreeTypeFont] = {}


def _font(size: float, bold: bool):
    key = (round(size * 4), bold)
    if key not in _cache:
        path = _FONTS[1 if bold else 0]
        try:
            _cache[key] = ImageFont.truetype(path, max(int(round(size * 4)), 4), layout_engine=ImageFont.Layout.RAQM)
        except Exception:  # noqa: BLE001 - basic layout still lets geometry be checked
            _cache[key] = ImageFont.truetype(path, max(int(round(size * 4)), 4))
    return _cache[key]


def _width(text: str, size: float, bold: bool, rtl: bool = True) -> float:
    f = _font(size, bold)
    try:
        return f.getlength(text, direction="rtl" if rtl else "ltr") / 4.0
    except Exception:  # noqa: BLE001
        return f.getlength(text) / 4.0


def _wrap(text: str, size: float, bold: bool, width: float) -> List[str]:
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if cur and _width(trial, size, bold) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    return lines


class PilMeasurer:
    LINE = 1.22

    def wrapped_height(self, text, size, bold, width, title_font=False):
        return len(_wrap(text or " ", size, bold, width)) * size * self.LINE

    def text_width(self, text, size, bold, title_font=False):
        return _width(text, size, bold)


def render(layout: pi.Layout, out: Path, ppp: float = 1.6) -> Path:
    W, H = int(pi.PAGE_W * ppp), int(pi.PAGE_H * ppp)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img, "RGBA")
    s = layout.scale

    def pt(x, y):
        ax, ay = layout.point(x, y)
        return ax * ppp, ay * ppp

    for op in layout.ops:
        if isinstance(op, pi.Card):
            x, y = pt(op.x, op.y)
            w, h = op.w * s * ppp, op.h * s * ppp
            d.rounded_rectangle((x + 2, y + 3, x + w + 2, y + h + 3), 12 * s * ppp, fill=(0, 0, 0, 40))
            d.rounded_rectangle((x, y, x + w, y + h), 12 * s * ppp, fill=(238, 238, 238), outline="black", width=1)
            d.rounded_rectangle((x + 6, y + 6, x + w - 6, y + h - 6), 8 * s * ppp, fill="white")
        elif isinstance(op, pi.Rect):
            x, y = pt(op.x, op.y)
            d.rectangle((x, y, x + op.w * s * ppp, y + op.h * s * ppp), fill=op.fill, outline=op.stroke)
        elif isinstance(op, pi.Line):
            d.line((*pt(op.x1, op.y1), *pt(op.x2, op.y2)), fill=op.color, width=max(1, int(op.width * s * ppp)))
        elif isinstance(op, pi.Flag):
            x, y = pt(op.x, op.y)
            d.ellipse((x, y, x + op.w * s * ppp, y + op.h * s * ppp), fill=(200, 60, 60), outline="black")
        elif isinstance(op, pi.Text):
            size = op.size
            lines = _wrap(op.text, size, op.bold, op.w) if op.wrap else [op.text]
            lh = size * PilMeasurer.LINE
            block = lh * len(lines)
            y0 = op.y + (0 if op.valign == "top" else (op.h - block) / 2)
            for i, line in enumerate(lines):
                tw = _width(line, size, op.bold, rtl=not op.ltr)
                x0 = {"right": op.x + op.w - tw, "left": op.x, "center": op.x + (op.w - tw) / 2}[op.align]
                ax, ay = pt(x0, y0 + i * lh)
                d.text((ax, ay), line, font=_scaled(size * s * ppp, op.bold), fill="black",
                       direction=None if op.ltr else "rtl")
                if op.underline:
                    uy = ay + lh * s * ppp * 0.92
                    d.line((ax, uy, ax + tw * s * ppp, uy), fill="black", width=1)
    img.save(out)
    return out


_scaled_cache: Dict[tuple, ImageFont.FreeTypeFont] = {}


def _scaled(px_size: float, bold: bool):
    key = (round(px_size * 2), bold)
    if key not in _scaled_cache:
        path = _FONTS[1 if bold else 0]
        try:
            _scaled_cache[key] = ImageFont.truetype(path, max(int(round(px_size)), 4), layout_engine=ImageFont.Layout.RAQM)
        except Exception:  # noqa: BLE001
            _scaled_cache[key] = ImageFont.truetype(path, max(int(round(px_size)), 4))
    return _scaled_cache[key]


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    out = Path(argv[0] if argv else "preview.png")
    n = int(argv[1]) if len(argv) > 1 else 3
    from tests.sample_data import make_invoice
    layout = pi.build_layout(make_invoice(n), PilMeasurer())
    render(layout, out)
    print(f"{out}  scale={layout.scale:.3f}  bottom={layout.bottom:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
