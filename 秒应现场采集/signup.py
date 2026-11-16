"""Submit one photo/location entry to a private, self-owned Miaoying test activity."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat
import requests

from capture_core import CaptureRejected, SelectedPoint, capture_evidence
from check_activity import SESSION_TOOL, has_coordinates, valid_activity_id
from coordinate_transform import wgs84_to_gcj02
from windows_capture import CaptureUnavailable, capture_location, capture_photo, windows_capture_apartment

if not (SESSION_TOOL / "miaoying_session.py").is_file():
    print(json.dumps({"status": "session_tool_missing"}), flush=True)
    raise SystemExit(2)
sys.path.insert(0, str(SESSION_TOOL))

from miaoying_session import AccountMismatch, SessionBusy, Unavailable, ensure_session
from signup_state import StateError, load_attempt, save_attempt, session_lock, state_directory
from submission_api import DISPLAY_NAME, SIMULATED_LOCATION_LABEL, SubmissionAPI, SubmissionError


class WorkflowError(RuntimeError):
    pass


DISALLOWED_OPTIONS = (
    "isClosed", "isRemove", "isRepeat", "needImages", "needAudio", "needVideo",
    "needSignature", "needPay", "needWifi", "fixedNo", "showNameList", "needCheckMode",
    "removeName", "openLocationInfo", "publishResult", "needApprovalMode", "needExamMode",
    "openBookTime", "orgId", "groupId", "allowSubmitTimeRules",
)
PHOTO_FORMAT = {"type": "4", "required": True}
CAMERA_WATERMARK = "name-photoTime-photoLocation"
MAX_LOCAL_IMAGE_BYTES = 30 * 1024 * 1024
ALBUM_EXTENSIONS = {".jpeg", ".jpg", ".png"}
SCHEDULE_GRACE = timedelta(minutes=1)
SIMULATION_TITLE_PREFIX = "自用测试·模拟定位"


def check_configuration(config, account, activity_id, *, use_file=False):
    title = config.get("title")
    if config.get("_id") != activity_id or config.get("createdBy") != account:
        raise WorkflowError("activity_owner_unverified")
    if not isinstance(title, str) or not title.startswith("自用测试"):
        raise WorkflowError("test_activity_title_required")
    if config.get("needLocation") is True or config.get("locations"):
        raise WorkflowError("geofence_unsupported")
    if config.get("locations") != []:
        raise WorkflowError("geofence_unverified")
    notice = config.get("mustReadNotice")
    if any(config.get(key) for key in DISALLOWED_OPTIONS) or (
        notice is not None and (not isinstance(notice, dict) or notice.get("enabled") is not False)
    ):
        raise WorkflowError("activity_has_unsupported_rules")
    if config.get("needSubmitLocation") is not True or config.get("allowBaomingCount") != 1:
        raise WorkflowError("expected_single_entry_with_live_location")
    now = int(time.time() * 1000)
    start, end = config.get("startTime"), config.get("endTime")
    if config.get("needTimeLimit") and not (
        type(start) is int and type(end) is int and start <= now < end
    ):
        raise WorkflowError("outside_activity_window")
    forms = config.get("infoForms")
    if not isinstance(forms, list) or any(not isinstance(item, dict) for item in forms):
        raise WorkflowError("unexpected_photo_field")
    active = [item for item in forms if not item.get("isRemove")]
    if len(active) != 1:
        raise WorkflowError("unexpected_photo_field")
    photo = active[0]
    maximum = photo.get("maxSelect")
    restrictions = photo.get("restrictPhoto")
    if (any(photo.get(key) != value for key, value in PHOTO_FORMAT.items()) or
        not all(isinstance(photo.get(key), str) and photo[key] for key in ("id", "title")) or
        photo.get("minSelect") not in (None, 0, 1) or
        (maximum not in (None, 0) and (type(maximum) is not int or maximum < 1)) or
        photo.get("maxRows") not in (None, 0)):
        raise WorkflowError("unexpected_photo_field")
    if restrictions is not None and (
        not isinstance(restrictions, dict) or restrictions.get("isRestrict") is not False
    ):
        raise WorkflowError("photo_constraints_unsupported")
    source = photo.get("mediaSourceType")
    watermark = photo.get("watermarkInfo")
    if use_file:
        if source != "0" or watermark:
            raise WorkflowError("activity_does_not_allow_album_photo")
    elif source != "1" or watermark != CAMERA_WATERMARK:
        raise WorkflowError("expected_camera_only_watermark_photo")
    return photo


def render_test_photo(jpeg, location, photo_at, selected):
    with Image.open(BytesIO(jpeg), formats=["JPEG"]) as image:
        frame = ImageOps.exif_transpose(image).convert("RGB")
    luma = frame.convert("L")
    histogram = luma.histogram()
    if ImageStat.Stat(luma).mean[0] > 12 or sum(histogram[26:]) / sum(histogram) > 0.001:
        raise CaptureRejected("自建测试的镜头需要盖住。此次画面未通过暗帧检查。")

    point = selected or location
    longitude, latitude = wgs84_to_gcj02(point.longitude, point.latitude)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(font_path, 23) if font_path.is_file() else ImageFont.load_default(size=23)
    text = "\n".join((
        DISPLAY_NAME if font_path.is_file() else "Personal test",
        photo_at.astimezone().isoformat(timespec="seconds"),
        f"{'Nearby selected point' if selected else 'Windows live location'} · "
        f"GCJ-02 {latitude:.2f}, {longitude:.2f}",
    ))
    canvas = Image.new("RGB", (frame.width, frame.height + 116), "black")
    canvas.paste(frame, (0, 0))
    ImageDraw.Draw(canvas).multiline_text((14, frame.height + 9), text, font=font, fill="white", spacing=4)
    with BytesIO() as memory:
        canvas.save(memory, "JPEG", quality=88)
        return memory.getvalue(), canvas.size


def normalize_album_photo(path):
    # Re-encode with no EXIF, so old GPS and timestamps cannot be mistaken for the new fix.
    with path.open("rb") as handle:
        source = handle.read(MAX_LOCAL_IMAGE_BYTES + 1)
    if len(source) > MAX_LOCAL_IMAGE_BYTES:
        raise WorkflowError("photo_file_too_large")
    with Image.open(BytesIO(source), formats=["JPEG", "PNG"]) as image:
        if getattr(image, "is_animated", False):
            raise WorkflowError("animated_photo_unsupported")
        frame = ImageOps.exif_transpose(image).convert("RGB")
    with BytesIO() as output:
        frame.save(output, "JPEG", quality=90)
        return output.getvalue(), frame.size


def select_album_photo(directory, *, on_day=None):
    if not directory.is_dir():
        raise WorkflowError("photo_directory_unavailable")
    try:
        photos = sorted(
            (
                path for path in directory.iterdir()
                if not path.name.startswith(".") and path.suffix.lower() in ALBUM_EXTENSIONS
                and not path.is_symlink() and path.is_file()
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as error:
        raise WorkflowError("photo_directory_unavailable") from error
    if not photos:
        raise WorkflowError("photo_directory_empty")
    day = on_day if on_day is not None else datetime.now().astimezone().date()
    return photos[day.toordinal() % len(photos)]


def load_album_photo(photo_file):
    try:
        return normalize_album_photo(photo_file)
    except (OSError, Image.DecompressionBombError) as error:
        raise WorkflowError("photo_file_invalid_or_unreadable") from error


def acquire(photo_file=None, selected=None):
    take_photo, render = capture_photo, render_test_photo
    if photo_file is not None:
        photo, size = load_album_photo(photo_file)

        async def take_photo():
            return photo

        def render(_jpeg, _location, _time, _selected):
            return photo, size
    try:
        with windows_capture_apartment():
            return asyncio.run(capture_evidence(
                take_photo, capture_location, selected=selected, render=render,
            ))
    except* (CaptureRejected, CaptureUnavailable, ImportError, OSError) as failures:
        raise WorkflowError("camera_or_location_unavailable") from failures


def location_digest(longitude, latitude):
    return hashlib.sha256(f"{longitude:.5f},{latitude:.5f}".encode("ascii")).hexdigest()


def result_matches(row, attempt, account, activity_id, *, participant=False):
    if not isinstance(row, dict) or row.get("createdBy") != account:
        return False
    if participant and row.get("tongjiId") != activity_id:
        return False
    position = row.get("locationInfo")
    if not has_coordinates(position) or location_digest(
        position["longtitude"], position["lattitude"]
    ) != attempt["location_digest"]:
        return False
    if attempt.get("simulated"):
        names, values = row.get("infoKey"), row.get("infoVal")
        if not isinstance(names, list) or not isinstance(values, list) or not any(
            name == attempt.get("name_label", "姓名") and value == SIMULATED_LOCATION_LABEL
            for name, value in zip(names, values)
        ):
            return False
    fields = row.get("formVal")
    return isinstance(fields, list) and any(
        isinstance(field, dict) and field.get("id") == attempt["photo_field"]
        and isinstance(field.get("value"), str)
        and attempt["reference"] in field["value"].split(",")
        for field in fields
    )


def reconcile(api, account, activity_id, attempt, directory, *, attempts=1):
    for delay in (0, 0.7, 1.5)[:attempts]:
        if delay:
            time.sleep(delay)
        own, ledger = api.participant(), api.organizer()
        if not isinstance(own, dict) or ledger["total"] != 1 or len(ledger["baomings"]) != 1:
            continue
        row = ledger["baomings"][0]
        record_id = own.get("_id")
        if (not record_id or row.get("_id") != record_id or
            attempt.get("record_id") not in (None, record_id)):
            continue
        created_at = own.get("createdAt")
        if type(created_at) is not int or not (
            attempt["dispatched_at"] - 120_000 <= created_at <= time.time() * 1000 + 120_000
        ):
            continue
        if not (result_matches(own, attempt, account, activity_id, participant=True) and
                result_matches(row, attempt, account, activity_id)):
            continue
        approved = own.get("isPassSecurity") is True and row.get("isPassSecurity") is True
        save_attempt(directory, {**attempt, "phase": "verified", "record_id": record_id})
        result = {"status": "submitted" if approved else "record_persisted_review_unconfirmed",
                  "records": 1, "matched_photo": True, "matched_location": True}
        if attempt.get("simulated"):
            result["location_source"] = "simulated"
            result["matched_simulation_label"] = True
            result["location_label_preserved"] = all(
                item["locationInfo"].get("name") == SIMULATED_LOCATION_LABEL for item in (own, row)
            )
        return result, 0
    return {"status": "submission_outcome_unknown", "next_action": "status_only"}, 1


def activity_is_empty(api):
    if api.participant() is not None:
        return False
    ledger = api.organizer()
    return ledger["total"] == 0 and not ledger["baomings"]


def run(api, account, activity_id, *, submit, retry_before_signup=False, photo_file=None,
        photo_directory=None, selected=None, simulated_point=None, scheduled_at=None,
        expected_title=None):
    directory = state_directory(activity_id)
    with session_lock(directory):
        previous = load_attempt(directory)
        config = api.activity()
        if config.get("_id") != activity_id or config.get("createdBy") != account:
            raise WorkflowError("activity_owner_unverified")
        if expected_title is not None and config.get("title") != expected_title:
            raise WorkflowError("test_activity_title_changed")
        if previous:
            if previous.get("account") != account:
                raise WorkflowError("attempt_account_mismatch")
            if previous["phase"] in {"submitting", "verified"}:
                try:
                    return reconcile(api, account, activity_id, previous, directory)
                except SubmissionError as error:
                    return {"status": "submission_outcome_unknown", "readback_status": error.status,
                            "next_action": "status_only"}, 1
            if not retry_before_signup:
                return {"status": "interrupted_before_signup", "phase": previous["phase"],
                        "media_may_remain": True}, 1

        simulated = simulated_point is not None
        if simulated and selected is not None:
            raise WorkflowError("location_modes_conflict")
        if simulated and not str(config.get("title", "")).startswith(SIMULATION_TITLE_PREFIX):
            raise WorkflowError("simulation_test_activity_required")
        if simulated and photo_file is None and photo_directory is None:
            photo_file = Path(__file__).with_name("黑屏测试图.png")
        use_album = photo_file is not None or photo_directory is not None
        photo_field = check_configuration(config, account, activity_id, use_file=use_album)
        if not activity_is_empty(api):
            return {"status": "already_present_or_activity_has_records"}, 0 if not submit else 1
        if photo_directory is not None:
            photo_file = select_album_photo(photo_directory)
        if not submit:
            if use_album:
                load_album_photo(photo_file)
            return {"status": "ready", "requires_live_location": not simulated,
                    "photo_source": "file" if use_album else "camera",
                    "location_source": "simulated" if simulated else "live"}, 0

        if scheduled_at is not None and datetime.now(timezone.utc) > scheduled_at + SCHEDULE_GRACE:
            raise WorkflowError("scheduled_time_missed")
        prepared_at = None
        if simulated:
            jpeg, _ = load_album_photo(photo_file)
            point = simulated_point
        else:
            evidence = acquire(photo_file, selected)
            jpeg, prepared_at = evidence.jpeg, evidence.photo_at
            point = selected or evidence.location
        longitude, latitude = wgs84_to_gcj02(point.longitude, point.latitude)
        if api.activity() != config or not activity_is_empty(api):
            raise WorkflowError("activity_or_records_changed")
        if scheduled_at is not None and datetime.now(timezone.utc) > scheduled_at + SCHEDULE_GRACE:
            raise WorkflowError("scheduled_time_missed")
        reference = f"{int(time.time() * 1000)}-{uuid4().hex[:12]}.jpg"
        name_label = config.get("nameLabel") or "姓名"
        attempt = {
            "account": account, "phase": "uploading", "photo_field": photo_field["id"],
            "reference": reference, "location_digest": location_digest(longitude, latitude),
            "simulated": simulated, "name_label": name_label,
        }
        save_attempt(directory, attempt)
        api.upload(jpeg, reference)
        attempt["phase"] = "registering"
        save_attempt(directory, attempt)
        api.register_media(reference, len(jpeg))
        if api.activity() != config or not activity_is_empty(api):
            raise WorkflowError("activity_or_records_changed_after_upload")
        if prepared_at is not None and (datetime.now(timezone.utc) - prepared_at).total_seconds() > 60:
            raise WorkflowError("capture_expired_after_upload")
        if scheduled_at is not None and datetime.now(timezone.utc) > scheduled_at + SCHEDULE_GRACE:
            raise WorkflowError("scheduled_time_missed")
        attempt["phase"] = "submitting"
        attempt["dispatched_at"] = int(time.time() * 1000)
        save_attempt(directory, attempt)
        write_error = None
        try:
            attempt["record_id"] = api.submit(
                name_label, photo_field["title"], reference, longitude, latitude,
                selected=selected is not None, simulated=simulated,
            )
            if attempt["record_id"]:
                save_attempt(directory, attempt)
        except SubmissionError as error:
            write_error = error.status
        try:
            report, code = reconcile(api, account, activity_id, attempt, directory, attempts=3)
        except SubmissionError as error:
            report, code = {"status": "submission_outcome_unknown",
                            "readback_status": error.status, "next_action": "status_only"}, 1
        if code and write_error:
            report["write_status"] = write_error
        return report, code


def parse_scheduled_time(value):
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() is None:
            raise ValueError("timezone required")
        return timestamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("请输入带时区的 ISO 时间，如 2026-11-13T19:30:00+08:00。") from None


def wait_for_schedule(timestamp):
    while True:
        remaining = (timestamp - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            if remaining < -SCHEDULE_GRACE.total_seconds():
                raise WorkflowError("scheduled_time_missed")
            return
        time.sleep(min(remaining, 30))


def run_with_session(activity_id, *, scheduled_at=None, **options):
    code = 1
    try:
        if scheduled_at is not None:
            wait_for_schedule(scheduled_at)
        session, token, account = ensure_session()
        if not token:
            return {"status": session}, 2
        with requests.Session() as client:
            api = SubmissionAPI(client, token, account, activity_id)
            return run(api, account, activity_id, scheduled_at=scheduled_at, **options)
    except SubmissionError as error:
        result = {"status": error.status}
        if error.http is not None:
            result["http"] = error.http
        if error.status == "reauthorization_required":
            code = 2
    except AccountMismatch:
        result, code = {"status": "account_mismatch"}, 2
    except SessionBusy:
        result = {"status": "session_or_activity_busy"}
    except Unavailable:
        result = {"status": "session_check_unavailable"}
    except (WorkflowError, StateError) as error:
        result = {"status": str(error)}
    except (OSError, ValueError, TypeError):
        result = {"status": "local_state_or_capture_error"}
    except KeyboardInterrupt:
        result = {"status": "interrupted", "next_action": "status_only"}
    return result, code


def main():
    parser = argparse.ArgumentParser(description="核对或提交本人自建的秒应水印拍照定位测试活动。")
    parser.add_argument("activity_id", help="本人自建测试活动的 24 位 ID。")
    parser.add_argument("--submit", action="store_true", help="采集当次照片和位置并发出一次提交。")
    photo_source = parser.add_mutually_exclusive_group()
    photo_source.add_argument("--photo-file", type=Path, metavar="PATH",
                              help="仅用于允许相册的自建测试活动，读取指定的 JPEG 或 PNG。")
    photo_source.add_argument("--photo-directory", type=Path, metavar="PATH",
                              help="仅用于允许相册的自建测试活动，从指定图库按本机日期轮换图片。")
    location_source = parser.add_mutually_exclusive_group()
    location_source.add_argument("--selected-point", type=float, nargs=2, metavar=("LAT", "LON"),
                                 help="选取距 Windows 实测位置 150 米内的 WGS84 点。")
    location_source.add_argument("--simulate-location", type=float, nargs=2, metavar=("LAT", "LON"),
                                 help="仅在允许相册的自建测试活动使用标记为模拟的 WGS84 坐标。")
    parser.add_argument("--at", type=parse_scheduled_time, metavar="ISO-TIME",
                        help="指定带时区的本次执行时间。保持电脑在线且当前用户已登录。")
    parser.add_argument("--retry-before-signup", action="store_true",
                        help="先前在上传或媒体登记阶段中断时，用当次采集重试。")
    args = parser.parse_args()
    if not valid_activity_id(args.activity_id):
        parser.error("请输入自建活动的 24 位标识。")
    if args.retry_before_signup and not args.submit:
        parser.error("--retry-before-signup 需要与 --submit 一起使用。")
    if args.at is not None and not args.submit:
        parser.error("--at 需要与 --submit 一起使用。")
    point = None
    if args.selected_point or args.simulate_location:
        latitude, longitude = args.selected_point or args.simulate_location
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            parser.error("请输入有效的 WGS84 经纬度。")
        point = SelectedPoint(latitude, longitude)

    result, code = run_with_session(
        args.activity_id, submit=args.submit, retry_before_signup=args.retry_before_signup,
        photo_file=args.photo_file, photo_directory=args.photo_directory,
        selected=point if args.selected_point else None,
        simulated_point=point if args.simulate_location else None, scheduled_at=args.at,
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
