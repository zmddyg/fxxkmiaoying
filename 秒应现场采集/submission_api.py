"""HTTP transport for the owned photo-and-location test activity."""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit
import uuid

import requests
from miaoying_session import NATIVE, authentication_rejected


DISPLAY_NAME = "自用测试"
LOCATION_LABEL = "Windows 实时定位（自用测试）"
SIMULATED_LOCATION_LABEL = "模拟定位（自用测试）"

_SIGNER = "https://miaoying.hui51.cn/api/file/getOssSign"
_STORAGE_HOST = "hui51.oss-cn-beijing.aliyuncs.com"
_SIGNING_FIELDS = ("policy", "signature", "OSSAccessKeyId")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.jpg")

_ACTIVITY_QUERY = """
query getTongji($_id: String) {
  tongji(_id: $_id) {
    _id createdBy title isRemove isClosed isRepeat needLocation needSubmitLocation
    needImages needSignature needPay needWifi fixedNo showNameList needCheckMode
    removeName nameLabel openLocationInfo needApprovalMode needExamMode publishResult
    needTimeLimit startTime endTime allowBaomingCount userAllowBaomingCount totalAllow
    banUpdate restrictGroupMember orgId groupId needVideo needAudio openBookTime
    locations { distance }
    allowSubmitTimeRules { _id }
    mustReadNotice { enabled }
    infoForms {
      id type title required isRemove mediaSourceType watermarkInfo minSelect maxSelect maxRows
      restrictPhoto { isRestrict }
    }
  }
}
"""

_PARTICIPANT_QUERY = """
query getBaoming($_id: String, $uid: String) {
  baoming(tongjiId: $_id, userId: $uid) {
    _id tongjiId createdAt createdBy isPassSecurity
    infoKey infoVal
    formVal { id value }
    locationInfo { longtitude lattitude name }
  }
}
"""

_ORGANIZER_QUERY = """
query getBaomings($tid: String) {
  baomingsWithCount(tongjiId: $tid, limit: "2") {
    total
    baomings {
      _id createdBy isPassSecurity
      infoKey infoVal
      formVal { id value }
      locationInfo { longtitude lattitude name }
    }
  }
}
"""

_REGISTER_MUTATION = """
mutation createMediaFileByInput($input: createMediaFileInput!) {
  createMediaFileByInput(input: $input) { _id }
}
"""

_SUBMIT_MUTATION = """
mutation createBaomingByInput($input: createBaomingInput!) {
  createBaomingByInput(input: $input) { _id isPassSecurity }
}
"""


class SubmissionError(RuntimeError):
    """A transport, response, or input failure with a safe status code."""

    def __init__(self, status: str, http: int | None = None):
        super().__init__(status)
        self.status = status
        self.http = http


def _storage_auth(prepared_request):
    # OSS authenticates this request with the signer fields in the form body.
    prepared_request.headers.pop("Authorization", None)
    prepared_request.headers.pop("Cookie", None)
    return prepared_request


def _reference_path(reference: str) -> str:
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise SubmissionError("invalid_reference")
    return "uploads/" + reference


class SubmissionAPI:
    """Single-attempt API calls using a normal, non-retrying requests session."""

    def __init__(self, client: requests.Session, token: str, account: str, activity_id: str):
        self._client = client
        self._token = token
        self._account = account
        self._activity_id = activity_id

    def _gql(self, operation: str, status: str, query: str, variables: dict) -> tuple[dict, int]:
        try:
            with self._client.post(
                NATIVE,
                json={"operationName": operation, "query": query, "variables": variables},
                headers={"Authorization": "Bearer " + self._token},
                timeout=8,
                allow_redirects=False,
            ) as response:
                http = response.status_code
                if http == 401:
                    raise SubmissionError("reauthorization_required", http)
                if http != 200:
                    raise SubmissionError(f"{status}_rejected", http)
                try:
                    body = response.json()
                except ValueError:
                    raise SubmissionError(f"{status}_invalid_response", http) from None
        except requests.RequestException:
            raise SubmissionError(f"{status}_unavailable") from None

        if not isinstance(body, dict):
            raise SubmissionError(f"{status}_invalid_response", http)
        errors = body.get("errors")
        if errors:
            if authentication_rejected(http, body):
                raise SubmissionError("reauthorization_required", http)
            raise SubmissionError(f"{status}_rejected", http)
        data = body.get("data")
        if not isinstance(data, dict):
            raise SubmissionError(f"{status}_invalid_response", http)
        return data, http

    def activity(self) -> dict:
        data, http = self._gql(
            "getTongji", "activity", _ACTIVITY_QUERY, {"_id": self._activity_id},
        )
        activity = data.get("tongji")
        if not isinstance(activity, dict):
            raise SubmissionError("activity_invalid_response", http)
        return activity

    def participant(self) -> dict | None:
        data, http = self._gql(
            "getBaoming", "participant", _PARTICIPANT_QUERY,
            {"_id": self._activity_id, "uid": self._account},
        )
        if "baoming" not in data or (data["baoming"] is not None and not isinstance(data["baoming"], dict)):
            raise SubmissionError("participant_invalid_response", http)
        return data["baoming"]

    def organizer(self) -> dict:
        data, http = self._gql(
            "getBaomings", "organizer", _ORGANIZER_QUERY, {"tid": self._activity_id},
        )
        ledger = data.get("baomingsWithCount")
        if not isinstance(ledger, dict):
            raise SubmissionError("organizer_invalid_response", http)
        total = ledger.get("total")
        rows = ledger.get("baomings")
        if (
            type(total) is not int or total < 0 or not isinstance(rows, list)
            or len(rows) > min(2, total) or any(not isinstance(row, dict) for row in rows)
        ):
            raise SubmissionError("organizer_invalid_response", http)
        return ledger

    def _sign_upload(self) -> tuple[str, dict, int | float]:
        boundary = uuid.uuid4().hex
        try:
            with self._client.post(
                _SIGNER,
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                },
                data=f"--{boundary}--\r\n".encode("ascii"),
                timeout=8,
                allow_redirects=False,
            ) as response:
                http = response.status_code
                if http == 401:
                    raise SubmissionError("reauthorization_required", http)
                if http != 200:
                    raise SubmissionError("upload_not_authorized", http)
                try:
                    envelope = response.json()
                except ValueError:
                    raise SubmissionError("upload_not_authorized", http) from None
        except requests.RequestException:
            raise SubmissionError("signer_unavailable") from None

        if not isinstance(envelope, dict):
            raise SubmissionError("upload_not_authorized", http)
        if envelope.get("statusCode") == 401:
            raise SubmissionError("reauthorization_required", http)
        signing = envelope.get("data")
        if envelope.get("statusCode") != 200 or not isinstance(signing, dict):
            raise SubmissionError("upload_not_authorized", http)

        url = signing.get("uploadImageUrl")
        try:
            target = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            target = None
        if (
            target is None or target.scheme != "https" or target.netloc != _STORAGE_HOST
            or target.path not in ("", "/") or target.query or target.fragment
        ):
            raise SubmissionError("unverified_upload_destination", http)

        fields = {field: signing.get(field) for field in _SIGNING_FIELDS}
        max_size = signing.get("maxSize")
        if (
            any(not isinstance(value, str) or not value for value in fields.values())
            or type(max_size) not in (int, float) or max_size <= 0
            or (isinstance(max_size, float) and not math.isfinite(max_size))
        ):
            raise SubmissionError("unverified_upload_policy", http)
        return f"https://{_STORAGE_HOST}/", fields, max_size

    def upload(self, jpeg: bytes, reference: str) -> None:
        key = _reference_path(reference)
        if not isinstance(jpeg, bytes) or not jpeg:
            raise SubmissionError("invalid_photo")
        destination, form, max_size = self._sign_upload()
        if len(jpeg) > max_size * 1024 * 1024:
            raise SubmissionError("photo_size")
        form.update({"key": key, "success_action_status": "200"})
        try:
            with self._client.post(
                destination,
                auth=_storage_auth,
                data=form,
                files={"file": (reference, jpeg, "image/jpeg")},
                timeout=10,
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise SubmissionError("storage_rejected", response.status_code)
        except requests.RequestException:
            raise SubmissionError("storage_unavailable") from None

    def register_media(self, reference: str, photo_size: int) -> None:
        key = _reference_path(reference)
        if type(photo_size) is not int or photo_size <= 0:
            raise SubmissionError("photo_size")
        data, http = self._gql(
            "createMediaFileByInput", "media_registration", _REGISTER_MUTATION,
            {"input": {
                "ossPath": key,
                "bucket": "hui51",
                "fileSize": photo_size,
                "entity": "tongji",
                "entityId": self._activity_id,
            }},
        )
        media = data.get("createMediaFileByInput")
        if not isinstance(media, dict) or not isinstance(media.get("_id"), str) or not media["_id"]:
            raise SubmissionError("media_registration_not_confirmed", http)

    def submit(
        self, name_label: str, photo_title: str, reference: str, longitude: float, latitude: float,
        *, selected: bool = False, simulated: bool = False,
    ) -> str | None:
        _reference_path(reference)
        if (
            any(not isinstance(label, str) or not label.strip() for label in (name_label, photo_title))
            or type(longitude) not in (int, float) or not -180 <= longitude <= 180
            or type(latitude) not in (int, float) or not -90 <= latitude <= 90
            or selected and simulated
        ):
            raise SubmissionError("invalid_submission_input")
        location_name = LOCATION_LABEL
        if simulated:
            location_name = SIMULATED_LOCATION_LABEL
        elif selected:
            location_name = "Windows 实时定位附近手选点（自用测试）"
        data, http = self._gql(
            "createBaomingByInput", "submit", _SUBMIT_MUTATION,
            {"input": {
                "tongjiId": self._activity_id,
                "userId": self._account,
                "signUrl": "",
                "infoKey": [name_label, photo_title],
                "infoVal": [SIMULATED_LOCATION_LABEL if simulated else DISPLAY_NAME, reference],
                "locationInfo": {
                    "longtitude": longitude,
                    "lattitude": latitude,
                    "name": location_name,
                },
            }},
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
