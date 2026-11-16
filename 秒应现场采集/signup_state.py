"""Keep only the metadata needed to reconcile a write after an interruption."""

import json
import os
from pathlib import Path
import tempfile

from miaoying_session import STATE_DIR, session_lock


class StateError(RuntimeError):
    pass


def state_directory(activity_id):
    return STATE_DIR / "test-signups" / activity_id


def load_attempt(directory: Path):
    try:
        data = (directory / "attempt.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    record = json.loads(data)
    if not isinstance(record, dict) or record.get("version") != 1:
        raise StateError("invalid_attempt_state")
    if record.get("phase") not in {"uploading", "registering", "submitting", "verified"}:
        raise StateError("invalid_attempt_state")
    for key in ("account", "photo_field", "reference", "location_digest"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise StateError("invalid_attempt_state")
    if record["phase"] in {"submitting", "verified"} and type(record.get("dispatched_at")) is not int:
        raise StateError("invalid_attempt_state")
    return record


def save_attempt(directory: Path, record):
    data = json.dumps({"version": 1, **record}, separators=(",", ":")).encode("utf-8")
    fd, path = tempfile.mkstemp(prefix=".attempt-", dir=directory)
    temporary = Path(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "attempt.json")
    finally:
        temporary.unlink(missing_ok=True)
