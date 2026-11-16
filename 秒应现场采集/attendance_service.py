"""Plan and submit classroom and self-study attendance from a local timetable."""

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import requests

from signup import (
    AccountMismatch, SessionBusy, StateError, SubmissionError, Unavailable, WorkflowError,
    ensure_session, load_album_photo, load_attempt, location_digest, save_attempt, session_lock,
    valid_activity_id,
)
from signup_state import STATE_DIR
from attendance_api import AttendanceAPI
from attendance_form import AttendanceError, occurrence, prepare_form, prepare_identity
from attendance_jobs import ACTIVITY_KINDS, ActivityJob, load_settings
from attendance_photo import prepare_photo
from attendance_places import select_place
from attendance_rules import summarize_activity
from attendance_schedule import planned_slots, resolve_slot
from capture_core import SelectedPoint
from check_activity import has_coordinates
from class_reminders import SHANGHAI, parse_timestamp


PERSISTED_STATES = {"submitted", "record_persisted_review_unconfirmed"}


def activity_id(reference):
    if not reference:
        raise AttendanceError("activity_unbound")
    if valid_activity_id(reference):
        return reference
    try:
        url = urlsplit(reference)
    except ValueError:
        raise AttendanceError("unsupported_activity_link") from None
    if url.scheme == "https" and url.netloc in ("hui51.cn", "www.hui51.cn"):
        if url.path.rstrip("/") == "/l":
            match = re.fullmatch(r"0/([0-9a-f]{24})/?", url.fragment)
        else:
            match = re.fullmatch(r"/api/miniApp/qrcode/Tongji_([0-9a-f]{24})/?", url.path)
        if match:
            return match[1]
    if url.scheme == "https" and url.netloc == "miaoying.hui51.cn" and url.path == "/viewPage":
        query = parse_qs(url.query, keep_blank_values=True)
        identifiers = query.get("id", [])
        if query.get("entity") == ["tongji"] and len(identifiers) == 1 and valid_activity_id(identifiers[0]):
            return identifiers[0]
    raise AttendanceError("unsupported_activity_link")


def now_shanghai():
    return datetime.now(SHANGHAI)


def check_open(config, start, end, now):
    # The service can return null for this optional flag on existing activities.
    # A missing field, an explicit closed value, or an unverified removal state
    # still stops the write; the server remains responsible for accepting it.
    closed = config.get("isClosed", True)
    if config.get("isRemove") is not False or (closed is not False and closed is not None):
        raise AttendanceError("activity_closed_or_unverified")
    if not start <= now < end:
        raise AttendanceError("outside_activity_window")


def record_state(rows):
    if len(rows) > 1:
        return "multiple_records"
    if not rows:
        return "no_record"
    if rows[0].get("isRemove") is True:
        return "previous_record_removed"
    return "submitted" if rows[0].get("isPassSecurity") is True else "record_persisted_review_unconfirmed"


def reconcile(api, day, slot, attempt, directory, *, after_write=False):
    for delay in (0, 0.7, 1.5) if after_write else (0,):
        if delay:
            time.sleep(delay)
        rows = api.records(day, slot)
        if len(rows) != 1 or rows[0].get("isRemove") is True:
            continue
        row = rows[0]
        if attempt.get("record_id") not in (None, row["_id"]):
            continue
        values = row.get("formVal")
        if not isinstance(values, list) or not any(
            isinstance(field, dict) and field.get("id") == attempt["photo_field"]
            and isinstance(field.get("value"), str) and attempt["reference"] in field["value"].split(",")
            for field in values
        ):
            continue
        if attempt["simulated"]:
            position = row.get("locationInfo")
            if not has_coordinates(position) or location_digest(
                position["longtitude"], position["lattitude"]
            ) != attempt["location_digest"]:
                continue
        save_attempt(directory, {**attempt, "phase": "verified", "record_id": row["_id"]})
        return {"status": record_state(rows), "matched_photo": True, "matched_location": attempt["simulated"]}
    return {"status": "submission_outcome_unknown", "next_action": "status"}


def _reconcile(api, day, slot, attempt, directory, *, after_write=False):
    try:
        return reconcile(api, day, slot, attempt, directory, after_write=after_write)
    except SubmissionError as error:
        return {"status": "submission_outcome_unknown", "readback_status": error.status, "next_action": "status"}


def handle_slot(api, account, target, job, venues, planned, mode, *, clock=None):
    clock = clock or now_shanghai
    config = api.attendance_activity()
    try:
        day, slot, start, end = occurrence(config, planned)
    except AttendanceError:
        if planned["kind"] != "class" or mode not in ("status", "submit"):
            raise
        day = parse_timestamp(planned["remind_at"]).date().isoformat()
        slot = planned["slot_index"]
        directory = STATE_DIR / "attendance" / target / f"{day}-{slot}"
        with session_lock(directory):
            attempt = load_attempt(directory)
            if attempt and attempt["account"] != account:
                raise AttendanceError("attempt_account_mismatch")
            if attempt and attempt["phase"] in ("submitting", "verified"):
                return _reconcile(api, day, slot, attempt, directory)
        raise
    summary = {
        "activity": summarize_activity(config, clock()),
        "slot": {"day": day, "index": slot, "opens": start.isoformat(), "closes": end.isoformat()},
    }
    if mode == "inspect":
        summary["activity"]["identity"] = {
            "roster": config.get("showNameList"), "fixed_number": config.get("fixedNo"),
            "number_label": config.get("noName"), "name_label": config.get("nameLabel"),
            "remove_name": config.get("removeName"),
        }
        return {"status": "activity_matched", **summary}
    directory = STATE_DIR / "attendance" / target / (f"{day}-{slot}" if day is not None else "once")
    with session_lock(directory):
        attempt = load_attempt(directory)
        if attempt and attempt["account"] != account:
            raise AttendanceError("attempt_account_mismatch")
        if attempt and attempt["phase"] in ("submitting", "verified"):
            return _reconcile(api, day, slot, attempt, directory)
        rows = api.records(day, slot)
        if rows or mode == "status":
            return {"status": record_state(rows), **summary}

        now = clock()
        if mode == "submit":
            check_open(config, start, end, now)
        photo_time = now if mode == "submit" else parse_timestamp(planned["remind_at"])
        fields, photo_field = prepare_form(config, job)
        identity = prepare_identity(config, job, fields)
        jpeg, size, location = prepare_photo(
            config, planned, job, venues, photo_field, fields, identity, photo_time, start.date(),
        )
        camera_only = str(photo_field["mediaSourceType"]) == "1"
        photo_id = photo_field["id"]
        if mode == "preview":
            return {
                "status": "inputs_prepared", **summary,
                "photo": {"width": size[0], "height": size[1], "bytes": len(jpeg),
                          "source": "simulated_camera" if camera_only else "gallery"},
                "location_source": "simulated" if location else "none",
                **({"selected_place": select_place(planned, job, venues).summary(),
                    "watermark_time": photo_time.isoformat(timespec="minutes")} if camera_only else {}),
            }

        check_open(config, start, end, clock())
        reference = f"{int(time.time() * 1000)}-{uuid4().hex[:12]}.jpg"
        attempt = {
            "account": account, "phase": "uploading", "photo_field": photo_id, "reference": reference,
            "simulated": location is not None,
            "location_digest": location_digest(location["longtitude"], location["lattitude"]) if location else "none",
        }
        save_attempt(directory, attempt)
        api.upload(jpeg, reference)
        attempt["phase"] = "registering"
        save_attempt(directory, attempt)
        api.register_media(reference, len(jpeg))
        updated_config = api.attendance_activity()
        if updated_config != config:
            raise AttendanceError("activity_changed_after_upload")
        rows = api.records(day, slot)
        if rows:
            return {"status": record_state(rows), **summary}
        dispatched_at = clock()
        check_open(updated_config, start, end, dispatched_at)
        for field in fields:
            if field["id"] == photo_id:
                field["value"] = reference
                break
        attempt.update(phase="submitting", dispatched_at=int(dispatched_at.timestamp() * 1000))
        save_attempt(directory, attempt)
        write_error = None
        try:
            attempt["record_id"] = api.submit_attendance(day, slot, fields, location, identity=identity)
            if attempt["record_id"]:
                save_attempt(directory, attempt)
        except SubmissionError as error:
            write_error = error.status
        result = _reconcile(api, day, slot, attempt, directory, after_write=True)
        if result["status"] == "submission_outcome_unknown" and write_error:
            result["write_status"] = write_error
        return result


def run_slot(settings, planned, mode):
    job = settings.activities[planned["kind"]]
    try:
        target = activity_id(job.activity)
        session, token, account = ensure_session()
        if not token:
            return {"status": session}
        with requests.Session() as client:
            return handle_slot(AttendanceAPI(client, token, account, target), account, target,
                               job, settings.venues, planned, mode)
    except (AttendanceError, WorkflowError, StateError) as error:
        return {"status": str(error)}
    except SubmissionError as error:
        return {"status": error.status, "http": error.http}
    except AccountMismatch:
        return {"status": "account_mismatch"}
    except SessionBusy:
        return {"status": "session_or_slot_busy"}
    except Unavailable:
        return {"status": "session_check_unavailable"}
    except (OSError, ValueError):
        return {"status": "local_state_or_photo_error"}


def show(result):
    print(json.dumps(result, ensure_ascii=False), flush=True)


def wait_for_slot(anchor):
    while (seconds := (anchor - now_shanghai()).total_seconds()) > 0:
        time.sleep(min(30, seconds))


def write_photo_preview(planned, job, venues, output, photo_time):
    field = {"id": "test-photo", "title": "自习照片", "type": "4", "required": True,
             "mediaSourceType": "1", "watermarkInfo": "photoTime-photoLocation"}
    config = {"needLocation": False, "needSubmitLocation": True, "infoForms": [field], "removeName": True}
    place = select_place(planned, job, venues)
    fields, photo_field = prepare_form(config, job)
    jpeg, size, _ = prepare_photo(
        config, planned, job, venues, photo_field, fields, {}, photo_time, photo_time.date(),
    )
    with output.open("xb") as handle:
        handle.write(jpeg)
    show({"status": "simulated_camera_photo_valid", "width": size[0], "height": size[1],
          "bytes": len(jpeg), "output": str(output.resolve()), "selected_place": place.summary(),
          "watermark_time": photo_time.isoformat(timespec="minutes")})


def camera_photo_test(path, output, point, place):
    latitude, longitude = point
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise AttendanceError("invalid_simulated_coordinates")
    job = ActivityJob(photo_file=path, location="simulated", point=SelectedPoint(latitude, longitude), place=place)
    write_photo_preview({"kind": "self_study"}, job, {}, output, now_shanghai())


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="按课表匹配真实秒应课堂签到和自习签到。")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "签到配置.json")
    actions = parser.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan", help="离线列出课堂时段和自习提醒。")
    plan.add_argument("--from", dest="first", type=date.fromisoformat, default=now_shanghai().date())
    plan.add_argument("--through", type=date.fromisoformat)
    photo = actions.add_parser("photo-test", help="只在本机验证一张图库照片。")
    photo.add_argument("path", type=Path)
    camera = actions.add_parser("camera-test", help="在本机合成仅限拍照表单的模拟水印照片。")
    camera.add_argument("path", type=Path)
    camera.add_argument("output", type=Path)
    camera.add_argument("--point", type=float, nargs=2, required=True, metavar=("LAT", "LON"),
                        help="模拟 WGS84 纬度和经度。")
    camera.add_argument("--place", default="自习", help="与坐标对应的地图地点名称。")
    slot_photo = actions.add_parser("slot-photo", help="按课表选点和签到时段生成本地照片预览。")
    slot_photo.add_argument("kind", choices=ACTIVITY_KINDS)
    slot_photo.add_argument("at", type=parse_timestamp, help="带时区的课表提醒时间。")
    slot_photo.add_argument("output", type=Path)
    slot_photo.add_argument("--map-place", help="仅在自习预览中指定配置里的地图地点。")
    slot_photo.add_argument("--photo-file", type=Path, help="本次预览的图库照片。")
    for action, help_text in (
        ("inspect", "读取活动及对应时段的真实要求。"),
        ("preview", "读取活动并在本机预处理资料。"),
        ("status", "核对当前账号在对应时段的回执。"),
        ("submit", "仅在活动开放时向秒应提交一次。"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("kind", choices=ACTIVITY_KINDS)
        command.add_argument("at", type=parse_timestamp, help="带时区的课表提醒时间。")
    run = actions.add_parser("run", help="保持服务运行，按已绑定的活动自动提交。")
    run.add_argument("--through", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    try:
        if args.action == "photo-test":
            jpeg, size = load_album_photo(args.path)
            show({"status": "photo_valid", "width": size[0], "height": size[1], "bytes": len(jpeg)})
            return 0
        if args.action == "camera-test":
            place = args.place.strip()
            if not place or not all(character.isprintable() for character in place):
                raise AttendanceError("invalid_place_name")
            camera_photo_test(args.path, args.output, args.point, place)
            return 0
        settings = load_settings(args.config)
        if args.action == "plan":
            for slot in planned_slots(settings.timetable, args.first, args.through or args.first):
                job = settings.activities[slot["kind"]]
                try:
                    map_result = {"selected_place": select_place(slot, job, settings.venues).summary()}
                except AttendanceError as error:
                    map_result = {"place_status": str(error)}
                show({**slot, "activity_bound": bool(job.activity), **map_result})
            return 0
        if args.action == "slot-photo":
            planned = resolve_slot(settings.timetable, args.kind, args.at)
            job = settings.activities[args.kind]
            if args.map_place:
                if args.kind != "self_study":
                    raise AttendanceError("class_location_comes_from_timetable")
                job = replace(job, map_place=args.map_place, point=None, place=None)
            if args.photo_file:
                job = replace(job, photo_file=args.photo_file, photo_directory=None)
            write_photo_preview(planned, job, settings.venues, args.output, args.at)
            return 0
        if args.action == "run":
            for job in settings.activities.values():
                if job.activity:
                    activity_id(job.activity)
            if not any(job.activity for job in settings.activities.values()):
                raise AttendanceError("activity_unbound")
            started = now_shanghai()
            show({"status": "service_started", "started_at": started.isoformat(),
                  "through": args.through.isoformat()})
            failed = False
            for slot in planned_slots(settings.timetable, started.date(), args.through):
                if not settings.activities[slot["kind"]].activity:
                    continue
                anchor = parse_timestamp(slot["remind_at"])
                if slot["kind"] == "class" and started >= parse_timestamp(slot["window_end"]):
                    continue
                wait_for_slot(anchor + timedelta(seconds=1))
                if slot["kind"] == "class" and len(slot["courses"]) != 1:
                    show({**slot, "status": "class_slot_ambiguous"})
                    failed = True
                    continue
                result = run_slot(settings, slot, "submit")
                if anchor < started and result["status"] == "outside_activity_window":
                    continue
                show({**slot, **result})
                if result["status"] in ("account_mismatch", "reauthorization_required"):
                    return 1
                failed |= result["status"] not in PERSISTED_STATES
            return int(failed)
        planned = resolve_slot(settings.timetable, args.kind, args.at)
        result = run_slot(settings, planned, args.action)
        show({**planned, **result})
        success = PERSISTED_STATES | {"activity_matched", "inputs_prepared"}
        return 0 if result["status"] in success or (args.action == "status" and result["status"] == "no_record") else 1
    except (AttendanceError, OSError, ValueError, WorkflowError) as error:
        parser.exit(2, f"{error}\n")
    except KeyboardInterrupt:
        show({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
