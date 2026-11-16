"""Render the photo supplied to the camera-only attendance form."""

from io import BytesIO
from math import ceil
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from attendance_form import AttendanceError, collects_location, gallery_photo, prepare_location
from attendance_places import SIMULATED_PLACE_PREFIX
from class_reminders import SHANGHAI


WATERMARK_ITEMS = ("name", "photoTime", "photoLocation", "no")


def _watermark_items(value):
    if value is None or value == "":
        return ()
    if not isinstance(value, str):
        raise AttendanceError("activity_watermark_unsupported")
    items = tuple(value.split("-"))
    if not set(items) <= set(WATERMARK_ITEMS) or len(items) != len(set(items)):
        raise AttendanceError("activity_watermark_unsupported")
    return items


def _identity_value(identity, fields, key, label):
    value = identity.get(key)
    if not value:
        value = next((field["value"] for field in fields if field["title"] == label and field["value"]), None)
    value = " ".join(value.split()) if value else ""
    if not value:
        raise AttendanceError(f"watermark_value_missing: {label}")
    return value


def _render_watermark(jpeg, lines):
    with Image.open(BytesIO(jpeg), formats=["JPEG"]) as original:
        photo = original.convert("RGB")
    candidates = [os.environ.get("MIAOYING_FONT_PATH"),
                  str(Path(__file__).resolve().parent.parent / "assets" / "NotoSansCJKsc-Bold.otf"),
                  "C:/Windows/Fonts/msyhbd.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                  "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
                  "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    font_path = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
    if font_path is None:
        raise AttendanceError("chinese_watermark_font_missing")
    width, height = photo.size
    # Scale the screenshot's 600-pixel reference layout while keeping the JPEG dimensions.
    scale = min(width / 600, height / (60 + 30 * len(lines)))
    inset = round(30 * scale)
    shade_height = max(1, round((60 + 30 * len(lines)) * scale))
    mask = Image.new("L", (1, shade_height))
    mask.putdata([round(85 * (row / shade_height) ** 1.6) for row in range(shade_height)])
    photo.paste((84, 76, 75), (0, height - shade_height, width, height), mask.resize((width, shade_height)))

    draw = ImageDraw.Draw(photo)
    for index, line in enumerate(lines, 1):
        units = ceil(24 * min(1, 10 / len(line))) if width < height else 24
        font = ImageFont.truetype(str(font_path), max(1, round(units * scale)))
        available = max(1, width - 2 * inset)
        if font.getlength(line) > available:
            font = font.font_variant(size=max(1, int(font.size * available / font.getlength(line))))
        draw.text((inset, height - round(30 * index * scale)), line, font=font,
                  anchor="ls", fill=(248, 248, 248))
    with BytesIO() as output:
        photo.save(output, format="JPEG", quality=90)
        return output.getvalue(), photo.size


def prepare_photo(config, planned, job, venues, photo_field, fields, identity, prepared_at, gallery_day):
    camera_only = str(photo_field["mediaSourceType"]) == "1"
    items = _watermark_items(photo_field.get("watermarkInfo")) if camera_only else ()
    point = prepare_location(config, planned, job, venues, for_photo=camera_only)
    jpeg, size = gallery_photo(job, gallery_day)
    if camera_only:
        place_name = point["name"].removeprefix(SIMULATED_PLACE_PREFIX)
        lines = []
        for item in items:
            if item == "name":
                label = config.get("nameLabel") or "姓名"
                lines.append(f"{label}  {_identity_value(identity, fields, 'name', label)}")
            elif item == "no":
                label = config.get("noName") or "序号"
                lines.append(f"{label}  {_identity_value(identity, fields, 'noLabel', label)}")
            elif item == "photoTime":
                lines.append(f"{prepared_at.astimezone(SHANGHAI):%Y-%m-%d %H:%M}")
            elif item == "photoLocation":
                lines.append(place_name)
        if "photoLocation" not in items:
            lines.append(place_name)
        jpeg, size = _render_watermark(jpeg, lines)
    return jpeg, size, point if collects_location(config) else None
