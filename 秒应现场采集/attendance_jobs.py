"""Load settings for the classroom and study attendance activities."""

from dataclasses import dataclass, field
import json
from pathlib import Path
from urllib.parse import urlsplit

from attendance_places import MapPlace
from capture_core import SelectedPoint


DEFAULT_TIMETABLE = Path(__file__).resolve().with_name("课表.json")
ACTIVITY_KINDS = ("class", "self_study")


@dataclass(frozen=True)
class ActivityJob:
    activity: str | None = field(default=None, repr=False)
    photo_file: Path | None = field(default=None, repr=False)
    photo_directory: Path | None = field(default=None, repr=False)
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    location: str = "none"
    point: SelectedPoint | None = field(default=None, repr=False)
    geofence_datum: str | None = None
    place: str | None = None
    map_place: str | None = None


@dataclass(frozen=True)
class Settings:
    timetable: Path
    activities: dict[str, ActivityJob]
    venues: dict[str, SelectedPoint | MapPlace]


def _valid_text(value):
    return isinstance(value, str) and bool(value.strip()) and all(ord(char) >= 32 for char in value)


def _optional_path(value, parent, key):
    if value is None:
        return None
    if not _valid_text(value):
        raise ValueError(f"{key} 须为文件或目录路径。")
    return parent / Path(value)


def _check_object(value, keys, key):
    if not isinstance(value, dict):
        raise ValueError(f"{key} 须为 JSON 对象。")
    if value.keys() - keys:
        raise ValueError(f"{key} 包含未识别的配置项。")


def _point(value, key):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{key} 须为 WGS84 坐标 [纬度, 经度]。")
    latitude, longitude = value
    if (type(latitude) not in (int, float) or not -90 <= latitude <= 90
            or type(longitude) not in (int, float) or not -180 <= longitude <= 180):
        raise ValueError(f"{key} 的 WGS84 经纬度超出有效范围。")
    return SelectedPoint(latitude, longitude)


def _load_job(config, parent, key):
    _check_object(config, {"activity", "photo_file", "photo_directory", "fields", "location", "point",
                           "geofence_datum", "place", "map_place"}, key)
    activity = config.get("activity")
    if activity is not None and not _valid_text(activity):
        raise ValueError(f"{key}.activity 须为活动分享链接或标识。")

    photo_file = _optional_path(config.get("photo_file"), parent, f"{key}.photo_file")
    photo_directory = _optional_path(config.get("photo_directory"), parent, f"{key}.photo_directory")
    if photo_file is not None and photo_directory is not None:
        raise ValueError(f"{key} 只能设置一个照片文件或一个图库目录。")

    fields = config.get("fields", {})
    if not isinstance(fields, dict) or any(
        not _valid_text(name) or not isinstance(value, str) for name, value in fields.items()
    ):
        raise ValueError(f"{key}.fields 须以字段标题或 ID 为键，以填写内容为值。")

    location = config.get("location", "none")
    if location not in ("none", "simulated"):
        raise ValueError(f"{key}.location 须为 none 或 simulated。")
    point = config.get("point")
    place = config.get("place")
    map_place = config.get("map_place")
    if place is not None and (not _valid_text(place) or point is None):
        raise ValueError(f"{key}.place 须与该地点的 point 一起填写。")
    if map_place is not None and (not _valid_text(map_place) or point is not None or place is not None):
        raise ValueError(f"{key}.map_place 须指定一个地图地点，且不能同时填写 point 或 place。")
    datum = config.get("geofence_datum")
    if datum not in (None, "WGS84", "GCJ-02"):
        raise ValueError(f"{key}.geofence_datum 须为 WGS84 或 GCJ-02。")
    return ActivityJob(
        activity=activity, photo_file=photo_file, photo_directory=photo_directory,
        fields=fields, location=location, point=None if point is None else _point(point, f"{key}.point"),
        geofence_datum=datum, place=place.strip() if place else None, map_place=map_place,
    )


def _load_venue(name, value):
    key = f"venues.{name}"
    if isinstance(value, list):
        return _point(value, key)
    _check_object(value, {"name", "point", "source_url", "precision"}, key)
    label, source, precision = value.get("name"), value.get("source_url"), value.get("precision")
    if not _valid_text(label) or not _valid_text(source):
        raise ValueError(f"{key} 须填写地图名称和来源。")
    parsed = urlsplit(source)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{key}.source_url 须为 HTTPS 地图来源。")
    if precision not in ("room", "building", "venue", "reference"):
        raise ValueError(f"{key}.precision 须为 room、building、venue 或 reference。")
    return MapPlace(label.strip(), _point(value.get("point"), key), "configured", source, precision)


def load_settings(path: Path) -> Settings:
    path = path.absolute()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    _check_object(config, {"timetable", "activities", "venues"}, "配置")
    timetable = _optional_path(config.get("timetable"), path.parent, "timetable") or DEFAULT_TIMETABLE

    activities = config.get("activities", {})
    _check_object(activities, ACTIVITY_KINDS, "activities")
    jobs = {
        kind: _load_job(activities.get(kind, {}), path.parent, f"activities.{kind}")
        for kind in ACTIVITY_KINDS
    }

    venues = config.get("venues", {})
    if not isinstance(venues, dict) or any(not _valid_text(name) for name in venues):
        raise ValueError("venues 须以课表上的教学楼名为键，以 WGS84 坐标为值。")
    return Settings(timetable, jobs, {name: _load_venue(name, value) for name, value in venues.items()})
