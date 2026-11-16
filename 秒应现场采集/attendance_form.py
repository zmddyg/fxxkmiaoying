"""Resolve the actual activity window and turn configured inputs into form fields."""

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from math import asin, cos, isfinite, radians, sin, sqrt
import re

from attendance_places import AttendanceError, SIMULATED_PLACE_PREFIX, select_place
from attendance_rules import summarize_activity
from class_reminders import SHANGHAI, parse_timestamp
from coordinate_transform import wgs84_to_gcj02
from signup import load_album_photo, select_album_photo


def _activity_date(value):
    if type(value) is not int or value <= 0:
        raise AttendanceError("repeat_date_unverified")
    try:
        return datetime.fromtimestamp(value / 1000, SHANGHAI).date()
    except (OSError, OverflowError, ValueError):
        raise AttendanceError("repeat_date_unverified") from None


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise AttendanceError("unsupported_activity_window")
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise AttendanceError("unsupported_activity_window") from None


def _repeat_day(config, day):
    if not _activity_date(config.get("repeatStartDate")) <= day <= _activity_date(config.get("repeatEndDate")):
        return False
    mode = config.get("repeatDaysType")
    if mode in (None, 0, "0"):
        days = config.get("repeatDays")
        if not isinstance(days, list) or any(str(item) not in "0123456" or len(str(item)) != 1 for item in days):
            raise AttendanceError("repeat_days_unverified")
        return str((day.weekday() + 1) % 7) in [str(item) for item in days]
    if mode in (1, "1", 2, "2"):
        return day.day % 2 == (int(mode) % 2)
    if mode in (3, "3"):
        dates = config.get("repeatDates")
        if not isinstance(dates, list) or any(not isinstance(item, str) for item in dates):
            raise AttendanceError("repeat_days_unverified")
        return day.isoformat() in dates
    raise AttendanceError("unsupported_repeat_days_type")


def occurrence(config, planned):
    """Return the date/index and live window that the organizer configured."""
    anchor = parse_timestamp(planned["remind_at"])
    if planned["kind"] == "class" and (
        config.get("title") != planned["title"] or config.get("isRepeat") is not True
    ):
        raise AttendanceError("class_activity_mismatch")
    if config.get("isRepeat") is False:
        timing = summarize_activity(config, anchor)["window"]
        if not timing["start"] or not timing["end"] or timing["has_time_limit"] is not True:
            raise AttendanceError("one_time_activity_window_unverified")
        start, end = parse_timestamp(timing["start"]), parse_timestamp(timing["end"])
        if not start <= anchor < end:
            raise AttendanceError("reminder_outside_activity_window")
        return None, None, start, end
    if config.get("isRepeat") is not True or not _repeat_day(config, anchor.date()):
        raise AttendanceError("activity_does_not_repeat_that_day")

    rules = config.get("allowSubmitTimeRules")
    if not isinstance(rules, list) or not rules or any(not isinstance(rule, dict) for rule in rules):
        raise AttendanceError("activity_windows_unverified")
    matches = []
    for index, rule in enumerate(rules):
        opening = _clock(rule.get("startTime"))
        closing = time.max if rule.get("endTime") == "24:00" else _clock(rule.get("endTime"))
        if closing <= opening:
            raise AttendanceError("unsupported_activity_window")
        start = datetime.combine(anchor.date(), opening, SHANGHAI)
        end = datetime.combine(anchor.date(), closing, SHANGHAI)
        if start <= anchor < end:
            matches.append((anchor.date().isoformat(), index, start, end))
    if len(matches) != 1:
        raise AttendanceError("activity_window_not_matched")
    match = matches[0]
    if planned["kind"] == "class" and (
        match[1] != planned["slot_index"] or match[2] != anchor
        or match[3] != parse_timestamp(planned["window_end"])
    ):
        raise AttendanceError("class_window_changed")
    return match


def prepare_form(config, job):
    """Return serialized fields and the photo field configuration."""
    if any(config.get(key) for key in (
        "needWifi", "needPay", "needSignature", "needExamMode", "needCheckMode",
        "needImages", "needAudio", "needVideo",
    )):
        raise AttendanceError("unsupported_activity_requirements")
    notice = config.get("mustReadNotice")
    if isinstance(notice, dict) and notice.get("enabled") is True:
        raise AttendanceError("activity_notice_requires_action")
    forms = config.get("infoForms")
    if not isinstance(forms, list) or any(not isinstance(field, dict) for field in forms):
        raise AttendanceError("activity_fields_unverified")

    active = [field for field in forms if not field.get("isRemove")]
    field_titles = [field.get("title") for field in active]
    field_ids = set()
    fields, photo_field = [], None
    for field in active:
        field_id, title, kind = field.get("id"), field.get("title"), str(field.get("type"))
        if not all(isinstance(value, str) and value.strip() for value in (field_id, title)):
            raise AttendanceError("activity_fields_unverified")
        if field_id in field_ids or type(field.get("required")) is not bool:
            raise AttendanceError("activity_fields_unverified")
        field_ids.add(field_id)
        value = job.fields.get(field_id)
        if value is None and title in job.fields:
            if field_titles.count(title) != 1:
                raise AttendanceError(f"ambiguous_field_title: {title}")
            value = job.fields[title]
        if kind == "4" and (field.get("required") or job.photo_file or job.photo_directory):
            if photo_field is not None:
                raise AttendanceError("multiple_photo_fields_unsupported")
            source = str(field.get("mediaSourceType"))
            restrictions = field.get("restrictPhoto")
            if source not in ("0", "1") or (source == "0" and field.get("watermarkInfo")) or (
                restrictions is not None and (not isinstance(restrictions, dict) or restrictions.get("isRestrict") is not False)
            ):
                raise AttendanceError("activity_photo_rules_unsupported")
            if field.get("minSelect") not in (None, 0, 1, "0", "1"):
                raise AttendanceError("multiple_photos_required")
            maximum = field.get("maxSelect")
            if maximum is not None and (type(maximum) not in (str, int) or not str(maximum).isdigit()):
                raise AttendanceError("photo_limit_unverified")
            value, photo_field = "", field
        elif field.get("required") and (value is None or not value.strip()):
            raise AttendanceError(f"missing_required_field: {title}")
        elif value is not None:
            if kind not in ("0", "1", "7", "9", "39"):
                raise AttendanceError(f"unsupported_field_type: {title}")
            options = field.get("options")
            if kind in ("1", "39") and (not isinstance(options, list) or value not in options):
                raise AttendanceError(f"unrecognized_option: {title}")
            if kind == "9":
                try:
                    if not Decimal(value).is_finite():
                        raise InvalidOperation
                except InvalidOperation:
                    raise AttendanceError(f"invalid_number: {title}") from None
        if value is not None:
            fields.append({"id": field_id, "type": kind, "title": title, "value": value})
    if photo_field is None:
        raise AttendanceError("activity_has_no_matching_photo_field")
    if job.photo_directory is None and job.photo_file is None:
        raise AttendanceError("gallery_photo_unconfigured")
    return fields, photo_field


def prepare_identity(config, job, fields):
    identity = {}
    if config.get("showNameList") is True or config.get("fixedNo") is True:
        number_label = config.get("noName") or "学号"
        number = job.fields.get(number_label)
        if not number or not number.strip():
            raise AttendanceError(f"missing_required_field: {number_label}")
        identity["noLabel"] = number
        roster_number = job.fields.get("@名单序号", "" if config.get("showNameList") is True else number)
        if not re.fullmatch(r"[0-9]{1,10}", roster_number) or not 0 <= int(roster_number) < 2**31:
            raise AttendanceError("roster_index_unverified")
        identity["no"] = int(roster_number)
    if config.get("removeName") is not True:
        name_label = config.get("nameLabel") or "姓名"
        if not any(field["title"] == name_label and field["value"].strip() for field in fields):
            name = job.fields.get(name_label)
            if not name or not name.strip():
                raise AttendanceError(f"missing_required_field: {name_label}")
            identity.update(name_label=name_label, name=name)
    return identity


def gallery_photo(job, on_day):
    if job.photo_directory is not None:
        return load_album_photo(select_album_photo(job.photo_directory, on_day=on_day))
    return load_album_photo(job.photo_file)


def _distance(lat1, lon1, lat2, lon2):
    latitude_delta, longitude_delta = radians(lat2 - lat1), radians(lon2 - lon1)
    angle = sin(latitude_delta / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(longitude_delta / 2) ** 2
    return 12_742_017.6 * asin(sqrt(min(1, angle)))


def collects_location(config):
    return config.get("needSubmitLocation") is True or config.get("needLocation") is True or bool(config.get("locations"))


def prepare_location(config, planned, job, venues, *, for_photo=False):
    fences = config.get("locations")
    if (
        type(config.get("needLocation")) is not bool or type(config.get("needSubmitLocation")) is not bool
        or (fences is not None and not isinstance(fences, list))
    ):
        raise AttendanceError("activity_location_rules_unverified")
    if not collects_location(config) and not for_photo:
        return None
    if job.location != "simulated":
        raise AttendanceError("activity_requires_location")
    place = select_place(planned, job, venues)
    point = place.point
    longitude, latitude = wgs84_to_gcj02(point.longitude, point.latitude)
    if config.get("needLocation") is True or fences:
        if not isinstance(fences, list) or not fences:
            raise AttendanceError("activity_geofence_unverified")
        if job.geofence_datum is None:
            raise AttendanceError("activity_geofence_datum_unverified")
        compare_lat, compare_lon = latitude, longitude
        if job.geofence_datum == "WGS84":
            compare_lat, compare_lon = point.latitude, point.longitude
        within = False
        for fence in fences:
            if not isinstance(fence, dict):
                raise AttendanceError("activity_geofence_unverified")
            lat, lon, radius = (fence.get(key) for key in ("latitude", "longtitude", "distance"))
            if any(type(value) not in (int, float) or not isfinite(value) for value in (lat, lon, radius)):
                raise AttendanceError("activity_geofence_unverified")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and radius > 0):
                raise AttendanceError("activity_geofence_unverified")
            within |= _distance(compare_lat, compare_lon, lat, lon) <= radius
        if not within:
            raise AttendanceError("scheduled_venue_outside_activity_geofence")
    return {"longtitude": longitude, "lattitude": latitude, "name": SIMULATED_PLACE_PREFIX + place.name}
