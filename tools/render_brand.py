#!/usr/bin/env python3
"""Render the Echo mark and the admin favicon from assets/brand.

Mark PNGs (mark-64.png … mark-1024.png) come from mark.svg. cairosvg is used when it
is installed; otherwise the same SVG is rasterized with librsvg and Cairo, which is
what ships on the Pi image. Pillow builds admin/web/assets/favicon.ico from the
16 and 32 px favicon PNGs (both sizes stored in the ICO) and copies the mark and
favicon files the admin page serves.

Run from a repo checkout: python3 tools/render_brand.py
"""
import os
import shutil
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "assets", "brand")
ADMIN_ASSETS = os.path.join(ROOT, "admin", "web", "assets")
MARK_SVG = os.path.join(BRAND, "mark.svg")
MARK_SIZES = (64, 128, 256, 512, 1024)


def _render_cairosvg(svg, size):
    import cairosvg
    return cairosvg.svg2png(bytestring=svg, output_width=size, output_height=size)


def _render_librsvg(svg, size):
    """Rasterize SVG bytes to PNG bytes with librsvg + Cairo. Used when cairosvg is absent."""
    import ctypes
    from ctypes import POINTER, Structure, c_char_p, c_double, c_int, c_size_t, c_void_p

    class RsvgRectangle(Structure):
        _fields_ = [("x", c_double), ("y", c_double), ("width", c_double), ("height", c_double)]

    rsvg = ctypes.CDLL("librsvg-2.so.2")
    cairo = ctypes.CDLL("libcairo.so.2")
    gobject = ctypes.CDLL("libgobject-2.0.so.0")
    rsvg.rsvg_handle_new_from_data.argtypes = [c_char_p, c_size_t, c_void_p]
    rsvg.rsvg_handle_new_from_data.restype = c_void_p
    rsvg.rsvg_handle_render_document.argtypes = [c_void_p, c_void_p, POINTER(RsvgRectangle), c_void_p]
    rsvg.rsvg_handle_render_document.restype = c_int
    cairo.cairo_image_surface_create.argtypes = [c_int, c_int, c_int]
    cairo.cairo_image_surface_create.restype = c_void_p
    cairo.cairo_create.argtypes = [c_void_p]
    cairo.cairo_create.restype = c_void_p
    cairo.cairo_destroy.argtypes = [c_void_p]
    cairo.cairo_surface_write_to_png_stream.argtypes = [c_void_p, c_void_p, c_void_p]
    cairo.cairo_surface_write_to_png_stream.restype = c_int
    cairo.cairo_surface_destroy.argtypes = [c_void_p]
    gobject.g_object_unref.argtypes = [c_void_p]

    handle = rsvg.rsvg_handle_new_from_data(svg, len(svg), None)
    if not handle:
        raise RuntimeError("librsvg could not read mark.svg")
    surface = cairo.cairo_image_surface_create(0, size, size)  # CAIRO_FORMAT_ARGB32
    cr = cairo.cairo_create(surface)
    viewport = RsvgRectangle(0, 0, float(size), float(size))
    ok = rsvg.rsvg_handle_render_document(handle, cr, ctypes.byref(viewport), None)
    cairo.cairo_destroy(cr)
    gobject.g_object_unref(handle)
    if not ok:
        cairo.cairo_surface_destroy(surface)
        raise RuntimeError("librsvg failed to render mark.svg")
    chunks = []

    @ctypes.CFUNCTYPE(c_int, c_void_p, ctypes.POINTER(ctypes.c_ubyte), c_int)
    def write(closure, data, length):
        chunks.append(bytes(data[:length]))
        return 0  # CAIRO_STATUS_SUCCESS

    status = cairo.cairo_surface_write_to_png_stream(surface, write, None)
    cairo.cairo_surface_destroy(surface)
    if status != 0:
        raise RuntimeError(f"cairo PNG write failed ({status})")
    return b"".join(chunks)


def render_svg(svg, size):
    try:
        return _render_cairosvg(svg, size)
    except ImportError:
        return _render_librsvg(svg, size)


def render_marks():
    svg = open(MARK_SVG, "rb").read()
    paths = []
    for size in MARK_SIZES:
        raw = render_svg(svg, size)
        path = os.path.join(BRAND, f"mark-{size}.png")
        with open(path, "wb") as f:
            f.write(raw)
        im = Image.open(path)
        if im.size != (size, size):
            raise SystemExit(f"{path} is {im.size}, expected {(size, size)}")
        paths.append(path)
        print(path)
    return paths


def build_ico():
    """Multi-size ICO from the shipped 16 and 32 px PNGs, not a resize of one of them."""
    im16 = Image.open(os.path.join(BRAND, "favicon-16.png")).convert("RGBA")
    im32 = Image.open(os.path.join(BRAND, "favicon-32.png")).convert("RGBA")
    if im16.size != (16, 16) or im32.size != (32, 32):
        raise SystemExit(f"favicon PNGs are {im16.size} and {im32.size}, expected 16 and 32")
    os.makedirs(ADMIN_ASSETS, exist_ok=True)
    dest = os.path.join(ADMIN_ASSETS, "favicon.ico")
    # Pillow skips any size larger than the image passed to save(), so the 32 px
    # PNG is the primary and the 16 px PNG is appended. Both are stored as-is.
    im32.save(dest, format="ICO", sizes=[(16, 16), (32, 32)], append_images=[im16])
    with Image.open(dest) as ico:
        have = set(ico.ico.sizes())
    if have != {(16, 16), (32, 32)}:
        raise SystemExit(f"{dest} sizes are {sorted(have)}, expected 16 and 32")
    for size, src in (((16, 16), im16), ((32, 32), im32)):
        with Image.open(dest) as ico:
            ico.size = size
            frame = ico.convert("RGBA")
        if frame.size != size or frame.tobytes() != src.tobytes():
            raise SystemExit(f"{dest} {size} pixels do not match the source PNG")
    print(dest)
    return dest


def copy_admin_assets():
    names = ("mark.svg", "favicon.svg", "favicon-16.png", "favicon-32.png", "favicon-180.png")
    for name in names:
        src = os.path.join(BRAND, name)
        dst = os.path.join(ADMIN_ASSETS, name)
        shutil.copyfile(src, dst)
        print(dst)


def main():
    render_marks()
    build_ico()
    copy_admin_assets()


if __name__ == "__main__":
    sys.exit(main() or 0)
