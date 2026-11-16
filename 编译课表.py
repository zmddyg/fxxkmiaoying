"""Compile the current classroom timetable and form into a small cloud bundle."""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sys

import requests


ROOT = Path(__file__).resolve().parent
EXISTING = ROOT
sys.path[:0] = [str(EXISTING / "秒应现场采集"), str(EXISTING / "秒应会话工具")]

from attendance_api import AttendanceAPI, _ATTENDANCE_ACTIVITY_QUERY
from attendance_form import occurrence, prepare_form, prepare_identity, prepare_location
from attendance_jobs import load_settings
from attendance_schedule import planned_slots
from attendance_service import activity_id
from class_reminders import SHANGHAI, load_schedule
from miaoying_session import STATE_DIR, load_state


OFFSETS = (-6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 5, 10, 22)
PHOTO_MARKER = "placeholder.jpg"
PHOTO_SOURCE = ROOT / "function" / "black.jpg"
VOLATILE = {"isClosed", "isRemove", "publishResult"}


class OfflineFormSerializer(AttendanceAPI):
    """Reuse the normal payload validation. This instance has no HTTP client."""

    def _gql(self, operation, status, query, variables):
        if operation != "createBaomingByInput":
            raise RuntimeError("unexpected serializer operation")
        self.payload = variables["input"]
        return {operation: {"_id": "offline-serialization"}}, 200


def write_json(path, body, *, readable=False):
    options = {"indent": 2} if readable else {"separators": (",", ":")}
    path.write_text(json.dumps(body, ensure_ascii=False, **options) + "\n", encoding="utf-8")


def cron_config(occurrences, cron_timezone):
    by_year = defaultdict(list)
    for item in occurrences:
        opening = datetime.fromtimestamp(item["opens"] / 1000, cron_timezone)
        for offset in OFFSETS:
            at = opening + timedelta(minutes=offset)
            by_year[at.year].append(at)

    def numbers(values):
        return ",".join(map(str, sorted(values)))

    def cron_lines(times):
        minutes_by_hour = defaultdict(set)
        for at in times:
            minutes_by_hour[at.hour].add(at.minute)
        hours_by_minutes = defaultdict(list)
        for hour, minutes in sorted(minutes_by_hour.items()):
            hours_by_minutes[tuple(sorted(minutes))].append(hour)
        days = {at.date() for at in times}
        day = str(next(iter(days)).day) if len(days) == 1 else "*"
        months, years = {at.month for at in times}, {at.year for at in times}
        return [f"0 {numbers(minutes)} {numbers(hours)} {day} {numbers(months)} * {numbers(years)}"
                for minutes, hours in hours_by_minutes.items()]

    rules = [line for _, times in sorted(by_year.items()) for line in cron_lines(times)]
    if len(rules) > 10:
        rules = cron_lines([at for times in by_year.values() for at in times])
    if len(rules) > 10:
        raise ValueError("CloudBase 定时器超过单函数 10 条的限制。")
    return {"triggers": [
        {"name": f"class_time_{index}", "type": "timer", "config": line}
        for index, line in enumerate(rules, 1)
    ]}


def compile_plan():
    state = load_state(STATE_DIR)
    if not state:
        raise ValueError("当前目录缺少可用的秒应登录会话。")
    settings = load_settings(EXISTING / "签到配置.json")
    job = settings.activities["class"]
    target = activity_id(job.activity)
    rows = load_schedule(settings.timetable)
    first = min(course.start.date() for course in rows)
    last = max(course.start.date() for course in rows)
    selection = _ATTENDANCE_ACTIVITY_QUERY.split("{", 1)[1].rsplit("}", 1)[0]
    selection = " ".join(selection.replace("$_id", "$activity").split())
    query = "query PlanBaseline($activity: String) { me { _id } " + selection + " }"
    with requests.Session() as http:
        api = AttendanceAPI(http, state["token"], state["account"], target)
        snapshot, _ = api._gql("PlanBaseline", "plan_baseline", query, {"activity": target})
    if (snapshot.get("me") or {}).get("_id") != state["account"]:
        raise ValueError("登录账号与已保存的课堂账号不一致。")
    config = snapshot.get("tongji")
    if not isinstance(config, dict) or config.get("_id") != target or config.get("isRemove") is not False:
        raise ValueError("当前课堂活动无法核对。")

    fields, photo = prepare_form(config, job)
    identity = prepare_identity(config, job, fields)
    photo_index, = (i for i, field in enumerate(fields) if field["id"] == photo["id"])
    fields[photo_index]["value"] = PHOTO_MARKER
    serializer = OfflineFormSerializer(None, "", state["account"], target)
    serializer.submit_attendance(None, None, fields, None, identity=identity)
    payload = serializer.payload
    info_index, = (i for i, value in enumerate(payload["infoVal"]) if value == PHOTO_MARKER)
    payload["formVal"][photo_index]["value"] = ""
    payload["infoVal"][info_index] = ""

    occurrences = []
    for item in planned_slots(settings.timetable, first, last):
        if item["kind"] != "class":
            continue
        if len(item["courses"]) != 1:
            raise ValueError(f"同一时段有多节课，需核对课表 {item['remind_at']}。")
        day, slot, opens, closes = occurrence(config, item)
        opens_ms = int(opens.timestamp() * 1000)
        key = sha256(f"{target}|{state['account']}|{day}|{slot}".encode()).hexdigest()[:32]
        suffix = sha256(f"{key}|black-285-v1".encode()).hexdigest()[:12]
        location = prepare_location(config, item, job, settings.venues)
        occurrences.append({
            "day": day, "slot": slot, "opens": opens_ms, "closes": int(closes.timestamp() * 1000),
            "key": key, "photo": f"{opens_ms}-{suffix}.jpg", **({"location": location} if location else {}),
        })
    if not occurrences:
        raise ValueError("课表里没有可用的课堂窗口。")
    return {
        "version": 1,
        "activitySelection": selection,
        "activityRules": {k: v for k, v in config.items() if k not in VOLATILE},
        "payload": payload,
        "photoIndex": photo_index,
        "photoInfoIndex": info_index,
        "offsetMinutes": OFFSETS,
        "requireCloudTimer": True,
        "traceStages": True,
        "occurrences": occurrences,
    }


def main():
    parser = argparse.ArgumentParser(description="从已保存的课表编译 CloudBase 云函数数据。")
    parser.add_argument("--from", dest="first_day", type=date.fromisoformat)
    args = parser.parse_args()
    result = compile_plan()
    if args.first_day is not None:
        result["occurrences"] = [s for s in result["occurrences"] if s["day"] >= args.first_day.isoformat()]
    if not result["occurrences"]:
        raise SystemExit("选定日期范围没有课程。")
    if PHOTO_SOURCE.stat().st_size != 285:
        raise SystemExit("黑点 JPEG 和已通过上传测试的文件大小不一致。")

    target = ROOT / "function"
    write_json(target / "plan.json", result)
    write_json(ROOT / "定时触发器-北京时间.json", cron_config(result["occurrences"], SHANGHAI), readable=True)
    write_json(ROOT / "定时触发器-UTC.json", cron_config(result["occurrences"], timezone.utc), readable=True)
    print(json.dumps({"class_slots": len(result["occurrences"]), "first": result["occurrences"][0]["day"],
                      "last": result["occurrences"][-1]["day"], "jpeg_bytes": 285}, ensure_ascii=False))


if __name__ == "__main__":
    main()
