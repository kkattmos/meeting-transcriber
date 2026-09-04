#!/usr/bin/env python3
"""
Crop a recorded frame down to the part worth printing.

A raw 1920x1080 frame from a Meet or Zoom recording is mostly not slide: dark
UI chrome around the edges, a participant filmstrip down one side, a toolbar at
the bottom. Dropped into a PDF at full width, the actual slide ends up a
postage stamp in the middle of a grey page.

Two modes, in increasing ambition:

  border  — trim uniform borders (letterbox/pillarbox and flat dark chrome).
            Purely mechanical: a row or column is border if it is both dark
            and nearly uniform. This is the safe mode and the fallback.

  slide   — additionally look for the shared-screen/slide region: the largest
            bright rectangle in the frame. Slides are overwhelmingly light on
            dark UI, which makes them findable without any real vision model.
            Every acceptance test below has to pass or this mode falls back to
            `border`, because a confidently wrong crop (half a slide, or one
            participant's face) is worse than an untrimmed frame.

Acceptance tests for a slide candidate:
  * covers at least MIN_AREA_FRACTION of the frame
  * is at least MIN_SIDE_PX on both sides, at full resolution
  * has an aspect ratio in ASPECT_RANGE (4:3 through ultrawide)
  * is actually brighter than the frame it was cut from

Analysis runs on a downscaled grayscale copy (ANALYSIS_WIDTH px wide), so the
cost is a few milliseconds per frame regardless of source resolution.

Pillow is required for cropping. Without it, crop_frame() copies the source
through unchanged and says so once — a PDF with uncropped frames still beats
no PDF.
"""
import os
import shutil
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - exercised on boxes without Pillow
    Image = None

ANALYSIS_WIDTH = 200

# Below this mean brightness (0-255) a pixel is "chrome", not content.
BORDER_LEVEL = 40
# A slide pixel is at least this bright.
BRIGHT_LEVEL = 150

MIN_AREA_FRACTION = 0.18
MIN_SIDE_PX = 240
ASPECT_RANGE = (0.9, 3.2)

_warned_no_pillow = False


def _warn_once():
    global _warned_no_pillow
    if not _warned_no_pillow:
        _warned_no_pillow = True
        print("  note: Pillow is not installed — frames go into the PDF "
              "uncropped (pip install pillow)", file=sys.stderr)


def _profiles(gray, width, height):
    """Row and column mean/max brightness for a small grayscale image."""
    pixels = gray.load()
    col_mean = [0.0] * width
    col_max = [0] * width
    row_mean = [0.0] * height
    row_max = [0] * height
    for y in range(height):
        row_total = 0
        row_peak = 0
        for x in range(width):
            v = pixels[x, y]
            row_total += v
            if v > row_peak:
                row_peak = v
            col_mean[x] += v
            if v > col_max[x]:
                col_max[x] = v
        row_mean[y] = row_total / width
        row_max[y] = row_peak
    for x in range(width):
        col_mean[x] /= height
    return row_mean, row_max, col_mean, col_max


def _span(values, keep):
    """First and last index (inclusive) where keep(value) holds, or None."""
    first = last = None
    for i, v in enumerate(values):
        if keep(v):
            if first is None:
                first = i
            last = i
    if first is None:
        return None
    return first, last


def _border_box(row_mean, row_max, col_mean, col_max):
    """Bounding box of everything that isn't flat dark border."""
    rows = _span(list(zip(row_mean, row_max)),
                 lambda mm: mm[0] > BORDER_LEVEL or mm[1] > BORDER_LEVEL * 2)
    cols = _span(list(zip(col_mean, col_max)),
                 lambda mm: mm[0] > BORDER_LEVEL or mm[1] > BORDER_LEVEL * 2)
    if not rows or not cols:
        return None
    return cols[0], rows[0], cols[1] + 1, rows[1] + 1


def _bright_box(gray, width, height):
    """Bounding box of the bright (slide-like) pixels."""
    pixels = gray.load()
    left, top = width, height
    right = bottom = -1
    for y in range(height):
        for x in range(width):
            if pixels[x, y] >= BRIGHT_LEVEL:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0:
        return None
    return left, top, right + 1, bottom + 1


def _scale_box(box, factor, size):
    """Scale an analysis-resolution box back up to the real image."""
    w, h = size
    left = max(0, int(box[0] * factor))
    top = max(0, int(box[1] * factor))
    right = min(w, int(round(box[2] * factor)))
    bottom = min(h, int(round(box[3] * factor)))
    if right - left < 2 or bottom - top < 2:
        return None
    return left, top, right, bottom


def _mean_brightness(gray, box):
    pixels = gray.load()
    left, top, right, bottom = box
    total = 0
    count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            total += pixels[x, y]
            count += 1
    return (total / count) if count else 0.0


def detect_crop(path, mode="slide"):
    """Return (left, top, right, bottom) in source pixels, or None.

    None means "use the frame as-is" — either cropping is off, the image can't
    be analysed, or nothing passed the acceptance tests.
    """
    if mode in (None, "", "none") or Image is None:
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            full_w, full_h = img.size
            if full_w < 8 or full_h < 8:
                return None
            factor = max(full_w / ANALYSIS_WIDTH, 1.0)
            small = img.convert("L").resize(
                (max(1, int(full_w / factor)), max(1, int(full_h / factor)))
            )
    except OSError:
        return None

    w, h = small.size
    row_mean, row_max, col_mean, col_max = _profiles(small, w, h)

    border = _border_box(row_mean, row_max, col_mean, col_max)
    border_full = _scale_box(border, factor, (full_w, full_h)) if border else None
    if border_full and (_is_full_frame(border_full, (full_w, full_h))
                        or not _acceptable(border_full, (full_w, full_h),
                                           check_aspect=False)):
        # Either nothing to trim, or the "content" is a speck — a cursor
        # highlight on an otherwise dark screen. Leave the frame alone.
        border_full = None

    if mode != "slide":
        return border_full

    bright = _bright_box(small, w, h)
    if bright:
        candidate = _scale_box(bright, factor, (full_w, full_h))
        if (candidate and _acceptable(candidate, (full_w, full_h))
                and not _is_full_frame(candidate, (full_w, full_h))):
            # A "slide" that is no brighter than the whole frame is just the
            # frame — usually a full-screen camera shot with no slide at all.
            if _mean_brightness(small, bright) > _mean_brightness(
                    small, (0, 0, w, h)) + 10:
                return candidate

    # Fall back to the mechanical border trim.
    return border_full


def _acceptable(box, size, check_aspect=True):
    """Is this box worth cropping to?

    The size and area floors matter more than they look: without them a white
    logo, a mouse cursor or one bright caption line becomes "the slide", and
    the PDF gets a 30-pixel thumbnail instead of the lecture. A border trim
    has no expected shape (letterbox is very wide, a pillarboxed phone
    recording very tall), so aspect is only checked for slide candidates.
    """
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    full_w, full_h = size
    if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
        return False
    if (width * height) < MIN_AREA_FRACTION * full_w * full_h:
        return False
    if not check_aspect:
        return True
    aspect = width / height if height else 0
    return ASPECT_RANGE[0] <= aspect <= ASPECT_RANGE[1]


def _is_full_frame(box, size, tolerance=0.03):
    """True when the box is the whole frame, give or take rounding.

    The analysis pass runs on a downscaled copy, so "no border at all" comes
    back as e.g. (0, 0, 960, 538) on a 960x540 frame. Scaling that back up and
    calling it a crop would re-encode every frame for two lost pixels.
    """
    left, top, right, bottom = box
    full_w, full_h = size
    return ((right - left) >= full_w * (1 - tolerance)
            and (bottom - top) >= full_h * (1 - tolerance))


def crop_frame(src, dst, mode="slide", max_width=1280):
    """Write a cropped, downscaled copy of `src` to `dst`. Returns dst.

    Never raises: a frame that can't be processed is copied through, because
    the PDF is worth more than the crop.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if Image is None:
        _warn_once()
        shutil.copyfile(src, dst)
        return dst

    try:
        box = detect_crop(src, mode=mode)
        with Image.open(src) as img:
            img = img.convert("RGB")
            if box:
                img = img.crop(box)
            if max_width and img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, max(1, int(img.height * ratio))))
            img.save(dst, "JPEG", quality=82)
    except (OSError, ValueError) as exc:
        print(f"  note: could not crop {src.name} ({exc}) — using it as-is",
              file=sys.stderr)
        try:
            shutil.copyfile(src, dst)
        except OSError:
            return src
    return dst


def crop_mode_from_env():
    """PDF_FRAME_CROP: slide (default), border, or none."""
    value = (os.environ.get("PDF_FRAME_CROP") or "slide").strip().lower()
    if value not in ("slide", "border", "none"):
        print(f"  warning: PDF_FRAME_CROP={value!r} — expected slide, border "
              f"or none; using slide", file=sys.stderr)
        return "slide"
    return value


def max_width_from_env():
    try:
        return int(os.environ.get("PDF_FRAME_MAX_WIDTH", "1280"))
    except ValueError:
        return 1280


def _main(argv):
    """CLI: `framecrop.py <image> [out.jpg] [--mode slide|border|none]`."""
    args = [a for a in argv[1:] if not a.startswith("--")]
    mode = "slide"
    for a in argv[1:]:
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
    if not args:
        print("Usage: framecrop.py <image> [out.jpg] [--mode=slide|border|none]",
              file=sys.stderr)
        return 2
    src = args[0]
    box = detect_crop(src, mode=mode)
    print(f"crop box: {box}")
    if len(args) > 1:
        print(crop_frame(src, args[1], mode=mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
