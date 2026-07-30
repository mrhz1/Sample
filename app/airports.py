"""
Offline nearest-commercial-airport lookup. Fully offline: haversine against a static,
user-editable table (data/airports.csv: real, large/medium, scheduled-service Ontario
airports), which also carries each airport's Google place_id (resolved once, ahead of
time -- it never changes -- see routing_provider.py for why place_id matters).

Rows with a non-empty `notes` column are unverified/flagged (see the starter file's YRO
entry -- a general-aviation-only field OurAirports mistakenly lists as scheduled) and are
excluded until the note is cleared.

For remote origins where even this nearest commercial airport has no road connection at
all, pipeline.py falls back to a live Google Places search (routing_provider.py's
find_nearby_airports) rather than a second static table here.
"""

import csv

from . import config
from .geo_utils import haversine_km


def _load_airports() -> list[dict]:
    airports = []
    with open(config.AIRPORTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("notes", "").strip():
                continue
            airports.append({
                "iata": row["iata"],
                "name": row["name"],
                "city": row["city"],
                "lat": float(row["latitude"]),
                "lng": float(row["longitude"]),
                "place_id": row.get("place_id") or None,
            })
    return airports


AIRPORTS = _load_airports()


def find_nearest_airport(lat: float, lng: float) -> dict:
    nearest = min(AIRPORTS, key=lambda a: haversine_km(lat, lng, a["lat"], a["lng"]))
    distance_km = haversine_km(lat, lng, nearest["lat"], nearest["lng"])
    return {**nearest, "distance_from_point_km": round(distance_km, 2)}
