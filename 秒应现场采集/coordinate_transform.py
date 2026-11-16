"""Offline WGS84-to-GCJ-02 conversion adapted from coordTransform_py.

Reference revision: 39e54c64cbc61086ea0389368d4909e52aadd2e6
https://github.com/wandergis/coordTransform_py/blob/39e54c64cbc61086ea0389368d4909e52aadd2e6/coordTransform_utils.py
"""

# The MIT License (MIT)
#
# Copyright (c) 2015 WangMing
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from math import cos, pi, sin, sqrt

_SEMI_MAJOR_AXIS_M = 6378245.0
_ECCENTRICITY_SQUARED = 0.00669342162296594323


def _latitude_offset(x: float, y: float) -> float:
    base = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * sqrt(abs(x))
    base += (20.0 * sin(6.0 * x * pi) + 20.0 * sin(2.0 * x * pi)) * 2.0 / 3.0
    base += (20.0 * sin(y * pi) + 40.0 * sin(y / 3.0 * pi)) * 2.0 / 3.0
    base += (160.0 * sin(y / 12.0 * pi) + 320.0 * sin(y * pi / 30.0)) * 2.0 / 3.0
    return base


def _longitude_offset(x: float, y: float) -> float:
    base = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * sqrt(abs(x))
    base += (20.0 * sin(6.0 * x * pi) + 20.0 * sin(2.0 * x * pi)) * 2.0 / 3.0
    base += (20.0 * sin(x * pi) + 40.0 * sin(x / 3.0 * pi)) * 2.0 / 3.0
    base += (150.0 * sin(x / 12.0 * pi) + 300.0 * sin(x / 30.0 * pi)) * 2.0 / 3.0
    return base


def wgs84_to_gcj02(longitude: float, latitude: float) -> tuple[float, float]:
    """Convert decimal degrees, preserving longitude-first order.

    The upstream transform leaves points outside its rectangular bounds unchanged.
    """
    if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
        raise ValueError("Invalid WGS84 coordinate")
    if not (73.66 < longitude < 135.05 and 3.86 < latitude < 53.55):
        return longitude, latitude

    x = longitude - 105.0
    y = latitude - 35.0
    latitude_radians = latitude / 180.0 * pi
    sine_latitude = sin(latitude_radians)
    scale = 1.0 - _ECCENTRICITY_SQUARED * sine_latitude * sine_latitude
    scale_root = sqrt(scale)

    latitude_radius = _SEMI_MAJOR_AXIS_M * (1.0 - _ECCENTRICITY_SQUARED) / (scale * scale_root)
    longitude_radius = _SEMI_MAJOR_AXIS_M / scale_root * cos(latitude_radians)
    delta_latitude = _latitude_offset(x, y) * 180.0 / (latitude_radius * pi)
    delta_longitude = _longitude_offset(x, y) * 180.0 / (longitude_radius * pi)
    return longitude + delta_longitude, latitude + delta_latitude
