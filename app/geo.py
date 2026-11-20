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
    lam = math.radians(lon + _ROTATE_LAMBDA)
    if lam > math.pi:  # d3's rotation wraps longitude into [-pi, pi]
        lam -= 2 * math.pi
    px, py = _conic_equal_area(lam, math.radians(lat))
    return _DX + _SCALE * px, _DY - _SCALE * py
