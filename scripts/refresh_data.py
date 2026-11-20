"""Fetch public data into data/cache/ (raw JSON) and rebuild the SQLite database.

  python -m scripts.refresh_data            # fetch anything missing, rebuild DB
  python -m scripts.refresh_data --force    # re-download everything
  python -m scripts.refresh_data --offline  # only rebuild the DB from the cache
"""
from __future__ import annotations

import argparse

from app import store
from app.sources import fema_nri, open_meteo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    if not args.offline:
        nri_cached = (open_meteo.path("cache_dir") / "nri_hubs.json").exists()
        if args.force or not nri_cached:
            fema_nri.refresh()
        try:
            open_meteo.refresh(force=args.force)
        except open_meteo.QuotaExceeded as e:
            print(f"Open-Meteo quota exceeded ({e}). Hubs already cached are kept; "
                  "re-run later to fill the rest.")

    loaded = store.build_from_cache()
    print(f"SQLite rebuilt: weather for {loaded['weather_hubs'] or 'no hubs'}, "
          f"NRI loaded={loaded['nri']}")


if __name__ == "__main__":
    main()
