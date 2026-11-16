"""Match the scheduled classroom or study venue to a configured map point."""

from dataclasses import dataclass

from capture_core import SelectedPoint
from class_reminders import describe_place


class AttendanceError(RuntimeError):
    pass


SIMULATED_PLACE_PREFIX = "模拟定位 · "


@dataclass(frozen=True)
class MapPlace:
    name: str
    point: SelectedPoint
    matched_by: str
    source_url: str | None = None
    precision: str | None = None

    def summary(self):
        latitude, longitude = self.point.latitude, self.point.longitude
        return {
            "name": self.name, "matched_by": self.matched_by, "precision": self.precision,
            "latitude": latitude, "longitude": longitude, "datum": "WGS84",
            "source_url": self.source_url,
            "map_url": (
                f"https://www.openstreetmap.org/?mlat={latitude:.7f}&mlon={longitude:.7f}"
                f"#map=19/{latitude:.7f}/{longitude:.7f}"
            ),
        }


def _catalog_place(name, value, matched_by):
    if isinstance(value, MapPlace):
        return MapPlace(value.name, value.point, matched_by, value.source_url, value.precision)
    return MapPlace(name, value, matched_by)


def select_place(planned, job, venues):
    if planned["kind"] == "class":
        if len(planned["courses"]) != 1:
            raise AttendanceError("class_slot_ambiguous")
        course = planned["courses"][0]
        room, building = course.get("room"), course.get("building")
        for key, matched_by in ((room, "room"), (building, "building")):
            if key is not None and key in venues:
                value = venues[key]
                if isinstance(value, SelectedPoint):
                    return MapPlace(describe_place(building, room), value, matched_by)
                return _catalog_place(key, value, matched_by)
        raise AttendanceError("scheduled_venue_coordinates_missing")
    if job.map_place:
        if job.map_place not in venues:
            raise AttendanceError("self_study_map_place_not_found")
        return _catalog_place(job.map_place, venues[job.map_place], "configured")
    if job.point is None or not job.place:
        raise AttendanceError("self_study_place_unconfigured")
    return MapPlace(job.place, job.point, "configured")
