"""Check whether an owned Miaoying test activity requires live photo and location."""

import argparse
import json
from pathlib import Path
import re
import sys

SESSION_TOOL = Path(__file__).resolve().parent.parent / "秒应会话工具"
ACTIVITY_QUERY = """
query getTongji($_id: String) {
  tongji(_id: $_id) {
    createdBy
    needLocation
    needSubmitLocation
    infoForms { id type required isRemove mediaSourceType watermarkInfo }
  }
}
"""
RECEIPT_QUERY = """
query getBaomings($tongjiId: String, $userId: String) {
  baomingsWithCount(tongjiId: $tongjiId, userId: $userId, limit: "2", isPassSecurity: true) {
    total
    baomings { createdBy locationInfo { longtitude lattitude } formVal { id value } }
  }
}
"""


def valid_activity_id(value):
    return bool(re.fullmatch(r"[0-9a-f]{24}", value))


def is_location_watermark(field):
    value = field.get("watermarkInfo")
    return isinstance(value, str) and "photoLocation" in value.split("-")


def has_coordinates(position):
    if not isinstance(position, dict):
        return False
    for name, bound in (("lattitude", 90), ("longtitude", 180)):
        value = position.get(name)
        if type(value) not in (float, int) or not -bound <= value <= bound:
            return False
    return True


def summarize_receipt(data, account, photo_field_ids):
    result = data.get("baomingsWithCount")
    if not isinstance(result, dict):
        return {"status": "receipt_not_accessible"}, 1
    count = result.get("total")
    records = result.get("baomings")
    if type(count) is not int or not isinstance(records, list):
        return {"status": "unrecognized_receipt"}, 1
    if count == 0 and not records:
        return {"status": "no_approved_record"}, 0
    if count > 1 and records:
        return {"status": "receipt_ambiguous", "approved_record_count": count}, 1
    if count != 1 or len(records) != 1:
        return {"status": "unrecognized_receipt"}, 1
    row = records[0]
    if not isinstance(row, dict) or row.get("createdBy") != account:
        return {"status": "receipt_account_unverified"}, 1
    fields = row.get("formVal")
    has_photo = isinstance(fields, list) and any(
        isinstance(field, dict)
        and isinstance(field.get("id"), str)
        and field["id"] in photo_field_ids
        and bool(field.get("value"))
        for field in fields
    )
    return {
        "status": "record_found",
        "has_photo_field_value": has_photo,
        "has_structured_location": has_coordinates(row.get("locationInfo")),
    }, 0


def check_activity(activity_id, *, receipt=False):
    if not (SESSION_TOOL / "miaoying_session.py").is_file():
        return {"status": "session_tool_missing"}, 1
    sys.path.insert(0, str(SESSION_TOOL))
    from miaoying_session import (
        AccountMismatch, NATIVE, SessionBusy, Unavailable,
        authentication_rejected, ensure_session, request_json,
    )

    try:
        session, token, account = ensure_session()
        if not token:
            return {"status": session}, 2

        http, response = request_json(
            NATIVE,
            "POST",
            payload={
                "query": ACTIVITY_QUERY,
                "operationName": "getTongji",
                "variables": {"_id": activity_id},
            },
            token=token,
        )
    except AccountMismatch:
        return {"status": "account_mismatch"}, 2
    except SessionBusy:
        return {"status": "session_busy"}, 2
    except (Unavailable, OSError, ValueError):
        return {"status": "activity_check_failed"}, 1

    if authentication_rejected(http, response):
        return {"status": "reauthorization_required"}, 2
    if http == 403:
        return {"status": "activity_not_accessible"}, 1
    if http != 200 or not response or response.get("errors"):
        return {"status": "activity_check_failed", "http": http}, 1

    data = response.get("data")
    activity = data.get("tongji") if isinstance(data, dict) else None
    if not isinstance(activity, dict):
        return {"status": "activity_not_accessible"}, 1
    if not account or activity.get("createdBy") != account:
        return {"status": "activity_creator_unverified"}, 1

    fields = activity.get("infoForms")
    if fields is not None and not isinstance(fields, list):
        return {"status": "unrecognized_activity_configuration"}, 1
    camera_fields = [
        field
        for field in fields or []
        if isinstance(field, dict)
        and str(field.get("type")) == "4"
        and field.get("required") is True
        and field.get("isRemove") is not True
        and str(field.get("mediaSourceType")) == "1"
    ]
    report = {
        "status": "activity_read",
        "session": session,
        "requires_camera_only_photo": bool(camera_fields),
        "requires_photo_location_watermark": any(
            is_location_watermark(field) for field in camera_fields
        ),
        "collects_live_location": activity.get("needSubmitLocation") is True,
        "has_geofence": activity.get("needLocation") is True,
        "external_submission": "unverified",
    }
    if not receipt:
        return report, 0
    try:
        http, response = request_json(
            NATIVE, "POST", payload={
                "operationName": "getBaomings",
                "query": RECEIPT_QUERY,
                "variables": {"tongjiId": activity_id, "userId": account},
            }, token=token,
        )
    except (Unavailable, OSError, ValueError):
        return {"status": "receipt_check_failed"}, 1
    if authentication_rejected(http, response):
        return {"status": "reauthorization_required"}, 2
    data = response.get("data") if http == 200 and response and not response.get("errors") else None
    if not isinstance(data, dict):
        return {"status": "receipt_check_failed", "http": http}, 1
    photo_field_ids = {
        field["id"] for field in camera_fields
        if isinstance(field.get("id"), str) and is_location_watermark(field)
    }
    summary, exit_code = summarize_receipt(data, account, photo_field_ids)
    report["receipt"] = summary
    return report, exit_code


def main():
    parser = argparse.ArgumentParser(description="只读检查自建秒应活动的采集要求。")
    parser.add_argument("activity_id", help="自建测试活动的 ID。")
    parser.add_argument("--receipt", action="store_true", help="核对本账号的既有审核通过记录。")
    args = parser.parse_args()
    if not valid_activity_id(args.activity_id):
        parser.error("请输入自建活动的 24 位标识。")
    report, exit_code = check_activity(args.activity_id, receipt=args.receipt)
    print(json.dumps(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
