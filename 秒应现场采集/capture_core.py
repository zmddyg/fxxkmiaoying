"""Pair a fresh on-site position with a camera frame and label the photo in memory."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
import math

from PIL import Image, ImageDraw, ImageFont, ImageOps

EARTH_MEAN_RADIUS_M = 6_371_008.8
MAX_POINT_ADJUSTMENT_M = 150


class CaptureRejected(RuntimeError):
    """The acquired evidence could not satisfy the requested limits."""


@dataclass(frozen=True)
class Location:
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)
    accuracy_m: float
    observed_at: datetime
    source: str
    coordinate_system: str = "WGS84"


@dataclass(frozen=True)
class SelectedPoint:
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)


@dataclass(frozen=True)
class Evidence:
    jpeg: bytes = field(repr=False)
    location: Location = field(repr=False)
    photo_at: datetime
    photo_size: tuple[int, int]
    selection_distance_m: float | None


def check_selected_point(location, selected):
    if selected is None:
        return None
    if not (-90 <= selected.latitude <= 90 and -180 <= selected.longitude <= 180):
        raise CaptureRejected("手选点超出了经纬度的有效范围。")
    latitude = math.radians(location.latitude)
    selected_latitude = math.radians(selected.latitude)
    longitude_delta = math.radians(selected.longitude - location.longitude)
    angle = (
        math.sin((selected_latitude - latitude) / 2) ** 2
        + math.cos(latitude) * math.cos(selected_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    distance = 2 * EARTH_MEAN_RADIUS_M * math.asin(math.sqrt(min(1.0, angle)))
    if distance > MAX_POINT_ADJUSTMENT_M:
        raise CaptureRejected("手选点距实测位置超过 150 米。")
    return distance


def check_location(location, photo_at, max_age_seconds, max_accuracy_m):
    if location.coordinate_system != "WGS84":
        raise CaptureRejected("Windows 未返回 WGS84 坐标。")
    if location.source not in {"wifi", "cellular", "satellite"}:
        raise CaptureRejected("Windows 未从 Wi-Fi、蜂窝网络或卫星获取位置。")
    if not (-90 <= location.latitude <= 90 and -180 <= location.longitude <= 180):
        raise CaptureRejected("Windows 返回了无效坐标。")
    if not math.isfinite(location.accuracy_m) or location.accuracy_m < 0:
        raise CaptureRejected("Windows 未返回有效的定位精度。")
    if location.observed_at.utcoffset() is None:
        raise CaptureRejected("Windows 未返回带时区的定位时间。")
    if abs((photo_at - location.observed_at).total_seconds()) > max_age_seconds:
        raise CaptureRejected("定位时间与拍照时间相差太久。")
    if max_accuracy_m is not None and location.accuracy_m > max_accuracy_m:
        raise CaptureRejected("定位误差估计超过了指定上限。")


def apply_watermark(jpeg, location, photo_at, selected=None):
    lines = [
        "Windows local capture",
        f"Photo UTC  {photo_at.isoformat(timespec='seconds')}",
        f"Measured {location.coordinate_system}  {location.latitude:.5f}, {location.longitude:.5f}",
        f"Position UTC  {location.observed_at.isoformat(timespec='seconds')}",
        f"Source {location.source}  |  Accuracy {location.accuracy_m:.0f} m",
    ]
    if selected is not None:
        lines.append(f"Selected point  {selected.latitude:.5f}, {selected.longitude:.5f}")
    with Image.open(BytesIO(jpeg), formats=["JPEG"]) as original:
        photo = ImageOps.exif_transpose(original).convert("RGB")
    padding = 10
    font = ImageFont.load_default(size=max(12, min(26, photo.width // 42)))
    label_width = math.ceil(max(font.getlength(line) for line in lines)) + padding * 2
    text = "\n".join(lines)
    draw = ImageDraw.Draw(photo)
    label_height = draw.multiline_textbbox((0, 0), text, font=font, spacing=3)[3]
    size = (max(photo.width, label_width), photo.height + label_height + padding * 2)
    canvas = Image.new("RGB", size, "black")
    canvas.paste(photo, (0, 0))
    ImageDraw.Draw(canvas).multiline_text(
        (padding, photo.height + padding), text, font=font, fill="white", spacing=3
    )
    with BytesIO() as output:
        canvas.save(output, format="JPEG", quality=90)
        return output.getvalue(), canvas.size


async def capture_evidence(camera, geolocator, *, max_age_seconds=15, max_accuracy_m=None,
                           selected=None, render=apply_watermark):
    async def take_photo():
        jpeg = await camera()
        return jpeg, datetime.now(timezone.utc)

    async with asyncio.TaskGroup() as group:
        photo_task = group.create_task(take_photo())
        location_task = group.create_task(geolocator())
    photo, photo_at = photo_task.result()
    location = location_task.result()
    check_location(location, photo_at, max_age_seconds, max_accuracy_m)
    distance = check_selected_point(location, selected)
    jpeg, size = render(photo, location, photo_at, selected)
    return Evidence(
        jpeg=jpeg, location=location, photo_at=photo_at, photo_size=size,
        selection_distance_m=distance,
    )
