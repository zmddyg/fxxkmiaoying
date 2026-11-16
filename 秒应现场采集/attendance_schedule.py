"""Match timetable courses and study reminders to known attendance times."""

from datetime import date, datetime
from pathlib import Path

from class_reminders import CLASS_WINDOWS, SHANGHAI, load_schedule, parse_timestamp, plan_reminders


CLASS_ACTIVITY_TITLE = "课堂签到"
CLASS_FIRST_DAY = date.min
CLASS_LAST_DAY = date.max
CLASS_SLOT_INDICES = {opening: index for index, (opening, _) in enumerate(CLASS_WINDOWS)}


def planned_slots(timetable: Path, first_day: date, last_day: date) -> list[dict]:
    plan = plan_reminders(load_schedule(timetable), first_day, last_day, self_study=True)
    slots = []
    for entry in plan["reminders"]:
        if entry["kind"] == "self_study":
            slots.append(entry)
            continue
        opening = parse_timestamp(entry["remind_at"])
        if CLASS_FIRST_DAY <= opening.date() <= CLASS_LAST_DAY:
            slots.append({
                **entry,
                "title": CLASS_ACTIVITY_TITLE,
                "slot_index": CLASS_SLOT_INDICES[opening.time()],
            })
    return slots


def resolve_slot(timetable: Path, kind: str, at: datetime) -> dict:
    if kind not in ("class", "self_study"):
        raise ValueError("签到类型须为课堂或自习。")
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError("签到时间须包含时区。")
    local = at.astimezone(SHANGHAI)
    anchor = local.isoformat()
    matches = [
        slot for slot in planned_slots(timetable, local.date(), local.date())
        if slot["kind"] == kind and slot["remind_at"] == anchor
    ]
    if not matches:
        raise ValueError("该时间未匹配课表中的课堂或自习提醒。")
    if len(matches) > 1 or (kind == "class" and len(matches[0]["courses"]) != 1):
        raise ValueError("同一时段匹配多条安排，无法确定签到对象。")
    return matches[0]
