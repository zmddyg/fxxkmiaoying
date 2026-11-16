"""Transport for the signed-in participant's Miaoying attendance records."""

from datetime import date
import sys

from check_activity import SESSION_TOOL

sys.path.insert(0, str(SESSION_TOOL))
from submission_api import SubmissionAPI, SubmissionError, _SUBMIT_MUTATION


_ATTENDANCE_ACTIVITY_QUERY = """
query AttendanceActivity($_id: String) {
  tongji(_id: $_id) {
    _id createdBy title isRemove isClosed isRepeat needLocation needSubmitLocation
    needImages needSignature needPay needWifi fixedNo showNameList needCheckMode
    removeName nameLabel noName hideNameListNo groupLabelName
    openLocationInfo needApprovalMode needExamMode publishResult
    needTimeLimit startTime endTime allowBaomingCount userAllowBaomingCount totalAllow
    banUpdate restrictGroupMember orgId groupId needVideo needAudio openBookTime
    repeatDays repeatDates repeatDaysType repeatStartDate repeatEndDate dayRepeatCount
    allowBuka allowBukaCount
    locations { longtitude latitude name distance }
    allowSubmitTimeRules { _id startTime endTime }
    mustReadNotice { enabled }
    infoForms {
      id type title required isRemove mediaSourceType watermarkInfo minSelect maxSelect maxRows
      options order isInfoOption
      restrictPhoto { isRestrict }
    }
  }
}
"""

_ATTENDANCE_RECORDS_QUERY = """
query AttendanceRecords($activity: String, $user: String, $day: String, $slot: Int) {
  baomingsWithCount(
    tongjiId: $activity, userId: $user, submitDayStr: $day, submitDayIndex: $slot, limit: "2"
  ) {
    total
    baomings {
      _id tongjiId createdBy isRemove isPassSecurity submitDayStr submitDayIndex createdAt no noLabel
      infoKey infoVal
      formVal { id type title value }
      locationInfo { longtitude lattitude name }
    }
  }
}
"""


def _check_occurrence(day: str | None, slot: int | None) -> None:
    if day is None and slot is None:
        return
    if not isinstance(day, str) or type(slot) is not int or not 0 <= slot < 2**31:
        raise SubmissionError("invalid_attendance_occurrence")
    try:
        canonical_day = date.fromisoformat(day).isoformat()
    except ValueError:
        raise SubmissionError("invalid_attendance_occurrence") from None
    if day != canonical_day:
        raise SubmissionError("invalid_attendance_occurrence")


class AttendanceAPI(SubmissionAPI):
    """Read and submit attendance as the current account, using the shared media transport."""

    def attendance_activity(self) -> dict:
        data, http = self._gql(
            "AttendanceActivity", "activity", _ATTENDANCE_ACTIVITY_QUERY, {"_id": self._activity_id},
        )
        activity = data.get("tongji")
        if not isinstance(activity, dict) or activity.get("_id") != self._activity_id:
            raise SubmissionError("activity_invalid_response", http)
        return activity

    def records(self, day: str | None, slot: int | None) -> list[dict]:
        _check_occurrence(day, slot)
        data, http = self._gql(
            "AttendanceRecords", "attendance_records", _ATTENDANCE_RECORDS_QUERY,
            {"activity": self._activity_id, "user": self._account, "day": day, "slot": slot},
        )
        ledger = data.get("baomingsWithCount")
        if not isinstance(ledger, dict):
            raise SubmissionError("attendance_records_invalid_response", http)
        total = ledger.get("total")
        rows = ledger.get("baomings")
        if type(total) is not int or total < 0 or not isinstance(rows, list) or len(rows) != min(2, total):
            raise SubmissionError("attendance_records_invalid_response", http)
        for row in rows:
            if (
                not isinstance(row, dict) or not isinstance(row.get("_id"), str) or not row["_id"]
                or row.get("createdBy") != self._account or row.get("tongjiId") != self._activity_id
                or (day is not None and (row.get("submitDayStr"), row.get("submitDayIndex")) != (day, slot))
            ):
                raise SubmissionError("attendance_records_invalid_response", http)
        return rows

    def submit_attendance(
        self, day: str | None, slot: int | None, fields: list[dict], location: dict | None,
        *, identity: dict | None = None,
    ) -> str | None:
        _check_occurrence(day, slot)
        if not isinstance(fields, list) or not fields:
            raise SubmissionError("invalid_attendance_fields")
        field_ids = set()
        for field in fields:
            if (
                not isinstance(field, dict) or field.keys() != {"id", "type", "title", "value"}
                or any(not isinstance(field[key], str) for key in field)
                or any(not field[key].strip() for key in ("id", "type", "title"))
                or field["id"] in field_ids
            ):
                raise SubmissionError("invalid_attendance_fields")
            field_ids.add(field["id"])
        payload = {
            "tongjiId": self._activity_id,
            "userId": self._account,
            "signUrl": "",
            "formVal": fields,
            "infoKey": [field["title"] for field in fields],
            "infoVal": [field["value"] for field in fields],
        }
        if day is not None:
            payload.update(submitDayStr=day, submitDayIndex=slot)
        if identity is not None:
            if not isinstance(identity, dict) or identity.keys() - {"no", "noLabel", "name_label", "name"}:
                raise SubmissionError("invalid_attendance_identity")
            if "no" in identity:
                if type(identity["no"]) is not int or not 0 <= identity["no"] < 2**31:
                    raise SubmissionError("invalid_attendance_identity")
                payload["no"] = identity["no"]
            if "noLabel" in identity:
                if not isinstance(identity["noLabel"], str) or not identity["noLabel"].strip():
                    raise SubmissionError("invalid_attendance_identity")
                payload["noLabel"] = identity["noLabel"]
            if "name_label" in identity or "name" in identity:
                label, name = identity.get("name_label"), identity.get("name")
                if any(not isinstance(value, str) or not value.strip() for value in (label, name)):
                    raise SubmissionError("invalid_attendance_identity")
                payload["infoKey"].insert(0, label)
                payload["infoVal"].insert(0, name)
        if location is not None:
            if not isinstance(location, dict) or location.keys() != {"longtitude", "lattitude", "name"}:
                raise SubmissionError("invalid_attendance_location")
            longitude, latitude, name = location["longtitude"], location["lattitude"], location["name"]
            if (
                type(longitude) not in (int, float) or not -180 <= longitude <= 180
                or type(latitude) not in (int, float) or not -90 <= latitude <= 90
                or not isinstance(name, str) or not name.strip()
            ):
                raise SubmissionError("invalid_attendance_location")
            payload["locationInfo"] = location
        data, http = self._gql(
            "createBaomingByInput", "submit", _SUBMIT_MUTATION, {"input": payload},
        )
        if "createBaomingByInput" not in data:
            raise SubmissionError("submit_invalid_response", http)
        result = data["createBaomingByInput"]
        if result is None:
            return None
        if not isinstance(result, dict) or "_id" not in result:
            raise SubmissionError("submit_invalid_response", http)
        receipt_id = result["_id"]
        if receipt_id is not None and (not isinstance(receipt_id, str) or not receipt_id):
            raise SubmissionError("submit_invalid_response", http)
        return receipt_id
