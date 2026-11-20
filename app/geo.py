"""Place hubs on the US map (app/static/us-states.json).

The map comes from us-atlas `states-albers-10m.json`, which is pre-projected with
d3.geoAlbersUsa().scale(1300).translate([487.5, 305]) into a 975x610 viewport. This module
reproduces that projection's lower-48 branch exactly (d3.geoAlbers: conic equal-area,
parallels 29.5/45.5, rotate [96, 0], center [-0.6, 38.7]) so hub markers line up with the
state shapes. All hubs are in the contiguous US; Alaska and Hawaii (map insets) are not
supported here.
"""
from __future__ import annotations

import math

VIEWBOX = (975, 610)
_SCALE = 1300.0
_TRANSLATE = (487.5, 305.0)
_ROTATE_LAMBDA = 96.0
_CENTER = (-0.6, 38.7)
_PARALLELS = (29.5, 45.5)

_phi0, _phi1 = (math.radians(p) for p in _PARALLELS)
_sy0 = math.sin(_phi0)
_N = (_sy0 + math.sin(_phi1)) / 2
_C = 1 + _sy0 * (2 * _N - _sy0)
_R0 = math.sqrt(_C) / _N


def _conic_equal_area(lam: float, phi: float) -> tuple[float, float]:
    """d3.geoConicEqualAreaRaw (radians in, unit-scale coordinates out)."""
    r = math.sqrt(_C - 2 * _N * math.sin(phi)) / _N
    return r * math.sin(lam * _N), _R0 - r * math.cos(lam * _N)


def _projected_center() -> tuple[float, float]:
    cx, cy = _conic_equal_area(math.radians(_CENTER[0]), math.radians(_CENTER[1]))
    return _TRANSLATE[0] - _SCALE * cx, _TRANSLATE[1] + _SCALE * cy


_DX, _DY = _projected_center()


def in_lower48(lon: float, lat: float) -> bool:
    return -125.0 <= lon <= -66.0 and 24.0 <= lat <= 50.0


def albers_usa(lon: float, lat: float) -> tuple[float, float]:
    """(lon, lat) in degrees -> (x, y) in the map's 975x610 SVG coordinates."""
    if not in_lower48(lon, lat):
        raise ValueError(f"({lon}, {lat}) is outside the contiguous US")
    return _project(lon, lat)


def _project(lon: float, lat: float) -> tuple[float, float]:
    lam = math.radians(lon + _ROTATE_LAMBDA)
    if lam > math.pi:  # d3's rotation wraps longitude into [-pi, pi]
        lam -= 2 * math.pi
    px, py = _conic_equal_area(lam, math.radians(lat))
    return _DX + _SCALE * px, _DY - _SCALE * py


# USPS code -> full name, for "Houston, Texas" style display.
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def graticule(step: float = 5.0, sample: float = 0.5) -> list[str]:
    """Meridians and parallels every `step` degrees across the contiguous US, as SVG paths in
    map coordinates (the UI clips them to the lower-48 land for an atlas-style grid)."""
    def line(points):
        return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in points)
    paths = []
    lon = -125.0
    while lon <= -65.0:
        pts = [_project(lon, 24.0 + i * sample) for i in range(int((50 - 24) / sample) + 1)]
        paths.append(line(pts))
        lon += step
    lat = 25.0
    while lat <= 50.0:
        pts = [_project(-125.0 + i * sample, lat) for i in range(int((125 - 65) / sample) + 1)]
        paths.append(line(pts))
        lat += step
    return paths
