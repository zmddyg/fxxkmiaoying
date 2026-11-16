"""Plan local class reminders from a dated timetable without registering them."""

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
CLASS_WINDOWS = (
    (time(7, 50), time(8, 10)),
    (time(10, 0), time(10, 20)),
    (time(13, 50), time(14, 10)),
    (time(15, 45), time(16, 5)),
    (time(18, 20), time(18, 40)),
)
STUDY_DAYS = {6, 0, 1, 2, 3}
STUDY_TIMES = (time(18, 30), time(20, 25))


@dataclass(frozen=True)
class Course:
    title: str
    start: datetime
    end: datetime | None
    room: str | None = None
    building: str | None = None


def parse_timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须使用带时区的 ISO 8601 日期时间。")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间必须使用带时区的 ISO 8601 日期时间。")
    return parsed.astimezone(SHANGHAI)


def read_optional_place(row, field, number):
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise ValueError(f"第 {number} 条课表记录的 {field} 须为有效文字。")
    return value.strip() or None


def load_schedule(path):
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("课表 JSON 顶层必须是课程数组。")

    courses = []
    for number, row in enumerate(rows, 1):
        title = row.get("title") if isinstance(row, dict) else None
        if not isinstance(title, str) or not title.strip() or any(
            ord(character) < 32 for character in title
        ):
            raise ValueError(f"第 {number} 条课表记录缺少有效课程名。")
        try:
            start = parse_timestamp(row.get("start"))
            end = parse_timestamp(row["end"]) if "end" in row else None
        except (ValueError, OverflowError) as error:
            raise ValueError(f"第 {number} 条课表记录的时间无效。") from error
        if end is not None and end <= start:
            raise ValueError(f"第 {number} 条课表记录的结束时间须晚于开始时间。")
        room = read_optional_place(row, "room", number)
        building = read_optional_place(row, "building", number)
        courses.append(Course(title.strip(), start, end, room=room, building=building))
    return courses


def join_course_segments(courses):
    by_classroom = defaultdict(list)
    for course in set(courses):
        key = course.start.date(), course.title, course.building, course.room
        by_classroom[key].append(course)

    merged = []
    for segments in by_classroom.values():
        group = []
        for course in sorted(segments, key=lambda row: (row.start, row.end or row.start)):
            if group and (
                course.start == group[-1].start
                or (group[-1].end is not None and course.start <= group[-1].end)
            ):
                previous = group[-1]
                ends = [end for end in (previous.end, course.end) if end is not None]
                group[-1] = replace(previous, end=max(ends) if ends else None)
            else:
                group.append(course)
        merged.extend(group)
    return sorted(merged, key=lambda row: (row.start, row.title, row.building or "", row.room or ""))


def plan_reminders(courses, first_day, last_day, *, self_study=False):
    if last_day < first_day:
        raise ValueError("日期范围的结束日期须不早于开始日期。")

    reminders = {}
    unmatched = 0
    for course in join_course_segments(courses):
        day = course.start.date()
        if not first_day <= day <= last_day:
            continue
        matching_window = next(
            ((opening, closing) for opening, closing in CLASS_WINDOWS
             if opening <= course.start.time() <= closing),
            None,
        )
        if matching_window is None:
            unmatched += 1
            continue
        opening, closing = matching_window
        remind_at = datetime.combine(day, opening, SHANGHAI).isoformat()
        entry = reminders.setdefault(remind_at, {
            "kind": "class",
            "remind_at": remind_at,
            "window_end": datetime.combine(day, closing, SHANGHAI).isoformat(),
            "courses": [],
        })
        detail = {"title": course.title, "start": course.start.isoformat()}
        if course.end is not None:
            detail["end"] = course.end.isoformat()
        if course.room is not None:
            detail["room"] = course.room
        if course.building is not None:
            detail["building"] = course.building
        entry["courses"].append(detail)

    scheduled = list(reminders.values())
    if self_study:
        for offset in range((last_day - first_day).days + 1):
            day = first_day + timedelta(days=offset)
            if day.weekday() in STUDY_DAYS:
                for reminder_time in STUDY_TIMES:
                    scheduled.append({
                        "kind": "self_study",
                        "remind_at": datetime.combine(day, reminder_time, SHANGHAI).isoformat(),
                    })
    return {
        "timezone": "Asia/Shanghai",
        "from": first_day.isoformat(),
        "through": last_day.isoformat(),
        "unmatched_course_starts": unmatched,
        "reminders": sorted(scheduled, key=lambda row: (row["remind_at"], row["kind"])),
    }


def calendar_text(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def fold_calendar_line(line):
    parts = []
    current = []
    size = 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            parts.append("".join(current))
            current, size = [" "], 1
        current.append(character)
        size += width
    parts.append("".join(current))
    return "\r\n".join(parts)


def describe_place(building, room):
    if room and building and building in room:
        return room
    return " ".join(dict.fromkeys(part for part in (building, room) if part))


def render_calendar(plan, *, generated_at=None):
    """Return an importable UTF-8 iCalendar with display-only alerts."""
    generated_at = generated_at or datetime.now(timezone.utc)
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Class Reminder Plan//ZH-CN",
             "CALSCALE:GREGORIAN"]
    for entry in plan["reminders"]:
        begins = parse_timestamp(entry["remind_at"])
        kind = entry["kind"]
        uid = hashlib.sha256(f"{kind}|{entry['remind_at']}".encode("utf-8")).hexdigest()[:32]
        event_location = ""
        if kind == "class":
            courses = entry["courses"]
            titles = list(dict.fromkeys(course["title"] for course in courses))
            summary = "课堂提醒 · " + "、".join(titles)
            ends = parse_timestamp(entry["window_end"])
            description = f"提醒时段 {begins:%H:%M}–{ends:%H:%M}。"
            places = {(course.get("building"), course.get("room")) for course in courses}
            if len(places) == 1:
                building, room = next(iter(places))
                event_location = describe_place(building, room)
            elif places:
                details = []
                for course in courses:
                    place = describe_place(course.get("building"), course.get("room"))
                    details.append(f"{course['title']}（{place or '课表未提供教室'}）")
                description += "\n" + "\n".join(dict.fromkeys(details))
        else:
            summary = "晚自习提醒"
            ends = begins + timedelta(minutes=1)
            description = "按晚自习安排提醒。"
        lines.extend((
            "BEGIN:VEVENT",
            f"UID:{uid}@class-reminders.local",
            f"DTSTAMP:{stamp}",
            "DTSTART:" + begins.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "DTEND:" + ends.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + calendar_text(summary),
            "DESCRIPTION:" + calendar_text(description),
        ))
        if event_location:
            lines.append("LOCATION:" + calendar_text(event_location))
        lines.extend((
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "TRIGGER:PT0M",
            "DESCRIPTION:" + calendar_text(summary),
            "END:VALARM",
            "END:VEVENT",
        ))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_calendar_line(line) for line in lines) + "\r\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="从日期化课表生成课堂提醒清单。")
    parser.add_argument("schedule", type=Path,
                        help="包含 title、start 及可选 end、room、building 的 JSON 数组。")
    parser.add_argument("--from", dest="first_day", type=date.fromisoformat, required=True,
                        metavar="YYYY-MM-DD", help="起始日期，含当天。")
    parser.add_argument("--through", type=date.fromisoformat, required=True,
                        metavar="YYYY-MM-DD", help="截止日期，含当天。")
    parser.add_argument("--self-study", action="store_true",
                        help="同时加入周日到周四 18:30 和 20:25 的晚自习提醒。")
    parser.add_argument("--format", choices=("json", "ics"), default="json", help="输出格式。")
    args = parser.parse_args(argv)
    try:
        courses = load_schedule(args.schedule)
        plan = plan_reminders(courses, args.first_day, args.through, self_study=args.self_study)
    except (OSError, ValueError, OverflowError) as error:
        parser.error(str(error))
    if args.format == "ics":
        sys.stdout.buffer.write(render_calendar(plan).encode("utf-8"))
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
