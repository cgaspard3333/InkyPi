"""Image composition for the Google Photos plugin: framing, photo pairs and captions."""

from PIL import Image, ImageColor, ImageDraw, ImageOps

from plugins.weather.i18n import MONTHS, normalize_language
from utils.app_utils import get_font
from utils.image_utils import pad_image_blur

PAIR_GUTTER = 6

YEARS_AGO = {
    "en": ("1 year ago", "{n} years ago"),
    "fr": ("Il y a 1 an", "Il y a {n} ans"),
    "de": ("Vor 1 Jahr", "Vor {n} Jahren"),
    "es": ("Hace 1 año", "Hace {n} años"),
    "it": ("1 anno fa", "{n} anni fa"),
}


def frame(image, dimensions, fill, background_option="blur", background_color="white"):
    """Fits a photo into the display area: crop to fill, or fit on a blurred/colour background."""
    if fill:
        return ImageOps.fit(image, dimensions, method=Image.LANCZOS)
    if background_option == "blur":
        return pad_image_blur(image, dimensions)
    color = ImageColor.getcolor(background_color or "white", image.mode)
    return ImageOps.pad(image, dimensions, color=color, method=Image.LANCZOS)


def pair_slot_size(dimensions):
    """Size of each half when two photos share the display (along its long side)."""
    width, height = dimensions
    if width >= height:
        return ((width - PAIR_GUTTER) // 2, height)
    return (width, (height - PAIR_GUTTER) // 2)


def compose_pair(first, second, dimensions, gutter_color="white"):
    """Places two photos side by side (landscape display) or stacked (portrait display)."""
    slot = pair_slot_size(dimensions)
    canvas = Image.new("RGB", dimensions, ImageColor.getrgb(gutter_color or "white"))
    a = ImageOps.fit(first.convert("RGB"), slot, method=Image.LANCZOS)
    b = ImageOps.fit(second.convert("RGB"), slot, method=Image.LANCZOS)
    canvas.paste(a, (0, 0))
    if dimensions[0] >= dimensions[1]:
        canvas.paste(b, (dimensions[0] - slot[0], 0))
        regions = [(0, 0, slot[0], slot[1]), (dimensions[0] - slot[0], 0, dimensions[0], slot[1])]
    else:
        canvas.paste(b, (0, dimensions[1] - slot[1]))
        regions = [(0, 0, slot[0], slot[1]), (0, dimensions[1] - slot[1], slot[0], dimensions[1])]
    return canvas, regions


def caption_text(taken, album_title, mode, years_ago, now, language):
    """Builds the caption: '<Month year>', optionally '· <album>', optionally '<n years ago> · ...'."""
    if mode not in ("date", "date_album"):
        return ""
    language = normalize_language(language)
    parts = []
    if taken is not None:
        if years_ago and taken.year < now.year:
            n = now.year - taken.year
            one, many = YEARS_AGO[language]
            parts.append(one if n == 1 else many.format(n=n))
        month = MONTHS[language][taken.month - 1]
        parts.append(f"{month[:1].upper()}{month[1:]} {taken.year}")
    if mode == "date_album" and album_title:
        parts.append(album_title)
    return " · ".join(parts)


def draw_caption(image, text, region, position="bottom_left", scale=1.0):
    """Draws a caption on a light rounded box (readable on e-ink) in a corner of region."""
    if not text:
        return image
    image = image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image
    left, top, right, bottom = region
    region_w, region_h = right - left, bottom - top
    # Same size for a full-screen photo and each photo of a pair (based on height)
    font_size = max(12, int(min(region_h * 0.04, region_w * 0.05) * scale))
    font = get_font("Jost", font_size)
    draw = ImageDraw.Draw(image)

    max_width = region_w - 4 * font_size
    if draw.textlength(text, font=font) > max_width:
        while len(text) > 1 and draw.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        text = text.rstrip() + "…"

    text_w = draw.textlength(text, font=font)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]
    pad_x, pad_y, margin = int(font_size * 0.6), int(font_size * 0.35), int(font_size * 0.6)
    box_w, box_h = int(text_w + 2 * pad_x), int(text_h + 2 * pad_y)
    x = left + margin if position == "bottom_left" else right - margin - box_w
    y = bottom - margin - box_h
    draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=int(font_size * 0.4),
                           fill=(255, 255, 255), outline=(0, 0, 0), width=1)
    draw.text((x + pad_x, y + pad_y - bbox[1]), text, font=font, fill=(0, 0, 0))
    return image
