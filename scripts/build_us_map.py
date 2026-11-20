"""Build app/static/us-states.json (state outlines as SVG paths) from us-atlas.

  python -m scripts.build_us_map            # download us-atlas@3.0.1 from the npm registry
  python -m scripts.build_us_map --tgz FILE # use an already-downloaded tarball

Source: us-atlas 3.0.1, states-albers-10m.json (ISC licence, (c) Michael Bostock), derived from
the US Census Bureau's 2017 cartographic state boundaries (public domain). The geometry is
already projected with d3.geoAlbersUsa().scale(1300).translate([487.5, 305]) into a 975x610
viewport; app/geo.py places hubs with the same projection. The tarball is checked against the
integrity hash the npm registry publishes for that version.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "us-states.json"
TARBALL = "https://registry.npmjs.org/us-atlas/-/us-atlas-3.0.1.tgz"
INTEGRITY = ("sha512-wEIZCq0ImPvGblTd8gZMqNNCPkQshugMUG/8nkSWXb02+XqNFREg9atHOKP9w6prLZTpqcLh"
             "SvdBW81MkV3/0Q==")
MEMBER = "package/states-albers-10m.json"
ATTRIBUTION = ("State outlines: us-atlas 3.0.1 (ISC, (c) Michael Bostock), from US Census Bureau "
               "2017 cartographic boundaries.")


def check_integrity(data: bytes) -> None:
    digest = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != INTEGRITY:
        raise SystemExit(f"us-atlas tarball integrity mismatch: {digest}")


def decode_arcs(topo: dict) -> list[list[tuple[float, float]]]:
    """Delta-decode the quantized arcs and apply the topology transform."""
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)
    return arcs


def ring_points(ring: list[int], arcs) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i, idx in enumerate(ring):
        arc = arcs[idx] if idx >= 0 else arcs[~idx][::-1]
        pts.extend(arc if i == 0 else arc[1:])  # consecutive arcs share their end points
    return pts


def ring_path(pts) -> str:
    coords = [f"{x:.1f} {y:.1f}" for x, y in pts]
    deduped = [c for i, c in enumerate(coords) if i == 0 or c != coords[i - 1]]
    return "M" + "L".join(deduped) + "Z"


def build(topo: dict) -> dict:
    arcs = decode_arcs(topo)
    states = []
    for geom in topo["objects"]["states"]["geometries"]:
        polygons = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
        d = "".join(ring_path(ring_points(ring, arcs)) for poly in polygons for ring in poly)
        states.append({"name": geom["properties"]["name"], "d": d})
    states.sort(key=lambda s: s["name"])
    return {"viewBox": "0 0 975 610", "attribution": ATTRIBUTION, "states": states}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tgz", help="path to us-atlas-3.0.1.tgz (otherwise downloaded)")
    args = ap.parse_args()
    data = Path(args.tgz).read_bytes() if args.tgz else requests.get(TARBALL, timeout=60).content
    check_integrity(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        topo = json.load(tar.extractfile(MEMBER))
    out = build(topo)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(out['states'])} states, "
          f"{OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
