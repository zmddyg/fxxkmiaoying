"""Summarize the signup requirements returned for a Miaoying activity."""

from datetime import datetime
import math

from class_reminders import SHANGHAI


BOOLEAN_RULES = (
    "isRepeat", "needImages", "needAudio", "needVideo", "needSignature", "needPay",
    "needWifi", "fixedNo", "needCheckMode", "needApprovalMode", "needExamMode",
    "restrictGroupMember", "banUpdate",
)
PHOTO_SOURCES = {"0": "album_allowed", "1": "camera_only"}


def _boolean(value):
    return value if type(value) is bool else None


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _activity_time(value):
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def _summarize_window(config, now):
    start = _activity_time(config.get("startTime"))
    end = _activity_time(config.get("endTime"))
    has_time_limit = _boolean(config.get("needTimeLimit"))
    removed = _boolean(config.get("isRemove"))
    closed = _boolean(config.get("isClosed"))
    if removed is True:
        state = "removed"
    elif closed is True:
        state = "closed"
    elif has_time_limit is False:
        state = "unbounded"
    elif has_time_limit is None or start is None or end is None or start >= end:
        state = "unknown"
    elif now < start:
        state = "not_started"
    elif now >= end:
        state = "ended"
    else:
        state = "within_window"
    return {
        "state": state,
        "has_time_limit": has_time_limit,
        "closed": closed,
        "removed": removed,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


def _required_fields(fields):
    if not isinstance(fields, list):
        return None
    required_fields = []
    for field in fields:
        if not isinstance(field, dict):
            return None
        if field.get("isRemove") is True:
            continue
        required = _boolean(field.get("required"))
        if required is None:
            return None
        if not required:
            continue
        raw_type = field.get("type")
        code = str(raw_type) if type(raw_type) in (str, int) else None
        result = {"id": _text(field.get("id")), "type": code, "title": _text(field.get("title"))}
        if code == "4":
            source = field.get("mediaSourceType")
            restrictions = field.get("restrictPhoto")
            result.update({
                "photo_source": PHOTO_SOURCES.get(str(source)) if type(source) in (str, int) else None,
                "watermark": _text(field.get("watermarkInfo")),
                "restricted": _boolean(restrictions.get("isRestrict")) if isinstance(restrictions, dict) else None,
            })
        required_fields.append(result)
    return required_fields


def _summarize_location(config):
    locations = config.get("locations")
    has_geofence = _boolean(config.get("needLocation"))
    distances = None
    if isinstance(locations, list):
        if locations:
            has_geofence = True
        if all(isinstance(location, dict) for location in locations):
            radii = [location.get("distance") for location in locations]
            if all(type(radius) in (int, float) and 0 <= radius < math.inf for radius in radii):
                distances = radii
    return {
        "collects_live_location": _boolean(config.get("needSubmitLocation")),
        "has_geofence": has_geofence,
        "geofence_radii": distances,
    }


def _summarize_other_rules(config):
    flags = {key: _boolean(config.get(key)) for key in BOOLEAN_RULES}
    notice = config.get("mustReadNotice")
    flags["mustReadNotice"] = _boolean(notice.get("enabled")) if isinstance(notice, dict) else None
    time_rules = config.get("allowSubmitTimeRules")
    flags["allowSubmitTimeRules"] = bool(time_rules) if isinstance(time_rules, list) else None
    return {
        "enabled": [key for key, enabled in flags.items() if enabled is True],
        "unknown": [key for key, enabled in flags.items() if enabled is None],
    }


def summarize_activity(config, now):
    """Return observed rules. None indicates an unrecognized or absent value."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must include a time zone")
    return {
        "title": _text(config.get("title")),
        "window": _summarize_window(config, now),
        "required_fields": _required_fields(config.get("infoForms")),
        "location": _summarize_location(config),
        "additional_rules": _summarize_other_rules(config),
    }
