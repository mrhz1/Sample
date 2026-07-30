"""
Per-postal-code decision logic. One public function, process_postal_code, produces ONE
wide result per postal code: origin info plus a `locations` list (one entry per
destination). The same function is reused in a loop by the batch endpoint.

Per location, drive/transit/flyover are ALL always attempted, except flyover is skipped
when origin and destination share the same nearest commercial airport (flying doesn't
make sense there).

  drive_distance_km / drive_duration
  transit_distance_km / transit_duration
  flyover_distance_km / flyover_duration   -- sum of the flyover_details breakdown below
  flyover_details:
      origin_to_airport      (postal code -> its airport, driving; None in "direct" mode)
      flight                 (origin airport (or postal code itself) -> destination
                               airport, offline great-circle estimate)
      wait_min                (fixed 3h overhead)
      airport_to_destination (destination's nearest airport -> destination, driving)

Remote-origin fallback (three tiers, tried in order):
  1. Normal: nearest REAL commercial airport (data/airports.csv), if reachable by road.
  2. Fallback airport: if not reachable, live Google Places search for any nearby airport
     (routing_provider.find_nearby_airports) -- broader than our static commercial list,
     since a small local strip Google knows about may have real road access even where the
     nearest commercial one doesn't. The first reachable candidate (nearest first) is used
     for BOTH the ground leg and the flight's starting point.
  3. Direct: if nothing at all is reachable by road (fully isolated community), the ground
     leg is dropped and the flight leg is computed directly from the postal code's own
     coordinates to the destination's airport, instead of returning nothing.
Which tier applied is reported in origin.flyover_mode ("normal" | "fallback_airport" |
"direct") and per-location in flyover_details.

API efficiency: destinations and airports are static, so their place_ids are precomputed
in data/*.csv (see airports.py / load_destinations below) rather than resolved per
request. The common case (tier 1 succeeds) costs 2 Route Matrix calls total: the "postal
code -> each destination" drive and transit legs, plus the "postal code -> its own
airport" drive leg, are combined into a single batched drive call (drive_minutes_batch)
alongside one batched transit call. Tiers 2/3 only add calls for the rare remote-origin
case where tier 1 fails.
"""

import csv

from . import config
from .airports import find_nearest_airport
from .flight_estimate import estimate_flight_minutes
from .geo_utils import haversine_km
from .routing_provider import RoutingProvider

LOCATION_FIELDS = [
    "drive_distance_km",
    "transit_distance_km",
    "flyover_distance_km",
    "drive_duration",
    "transit_duration",
    "flyover_duration",
]


def load_destinations() -> list[dict]:
    destinations = []
    with open(config.DESTINATIONS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            destinations.append({
                "name": row["name"],
                "city": row["city"],
                "address": row["address"],
                "lat": float(row["latitude"]),
                "lng": float(row["longitude"]),
                "place_id": row.get("place_id") or None,
            })
    return destinations


DESTINATIONS = load_destinations()


def _empty_location(name: str, error: str = "") -> dict:
    return {"name": name, **{f: None for f in LOCATION_FIELDS}, "flyover_details": None, "error": error}


def process_postal_code(postal_code: str, provider: RoutingProvider) -> dict:
    geocode = provider.geocode(postal_code)
    if not geocode["available"]:
        return {
            "postal_code": postal_code,
            "error": f"geocoding failed: {geocode['reason']}",
            "origin": None,
            "locations": [_empty_location(d["name"]) for d in DESTINATIONS],
        }

    origin_lat, origin_lng = geocode["lat"], geocode["lng"]
    origin_point = {"lat": origin_lat, "lng": origin_lng, "place_id": geocode.get("place_id")}
    # Route against place_id (not the bare coordinate) wherever available: a raw lat/lng
    # can snap to a spot with no direct road access (e.g. Billy Bishop Airport's coordinate
    # landing somewhere without a driveway), producing a wildly inflated short-hop time.
    commercial_airport = find_nearest_airport(origin_lat, origin_lng)
    commercial_point = {"lat": commercial_airport["lat"], "lng": commercial_airport["lng"], "place_id": commercial_airport["place_id"]}

    dest_points = [{"lat": d["lat"], "lng": d["lng"], "place_id": d["place_id"]} for d in DESTINATIONS]
    dest_airports = [find_nearest_airport(d["lat"], d["lng"]) for d in DESTINATIONS]

    # One batched call each for drive and transit: postal code -> every destination, plus
    # (for drive) postal code -> its own nearest commercial airport, as one extra "destination".
    drive_results = provider.drive_minutes_batch(origin_point, [*dest_points, commercial_point])
    transit_results = provider.transit_minutes_batch(origin_point, dest_points)
    drive_results, origin_to_airport = drive_results[:-1], drive_results[-1]

    origin_airport, origin_airport_point, flyover_mode = commercial_airport, commercial_point, "normal"

    if not origin_to_airport["available"]:
        # Tier 2: nearest real commercial airport has no road at all -- search Google's
        # live airport database (broader than our static commercial-only list) and try
        # each candidate, nearest first, until one has a real road.
        for candidate in provider.find_nearby_airports(
            origin_point, config.NEARBY_AIRPORT_SEARCH_RADIUS_M, config.NEARBY_AIRPORT_MAX_RESULTS
        ):
            candidate_point = {"lat": candidate["lat"], "lng": candidate["lng"], "place_id": candidate["place_id"]}
            candidate_drive = provider.drive_minutes(origin_point, candidate_point)
            if candidate_drive["available"]:
                distance_km = round(haversine_km(origin_lat, origin_lng, candidate["lat"], candidate["lng"]), 2)
                origin_airport = {**candidate, "iata": None, "distance_from_point_km": distance_km}
                origin_airport_point = candidate_point
                origin_to_airport = candidate_drive
                flyover_mode = "fallback_airport"
                break
        else:
            # Tier 3: nothing reachable by road at all.
            flyover_mode = "direct"

    origin_info = {
        "formatted_address": geocode.get("formatted_address", ""),
        "lat": origin_lat,
        "lng": origin_lng,
        "nearest_airport_iata": origin_airport["iata"],
        "nearest_airport_name": origin_airport["name"],
        "nearest_airport_distance_km": origin_airport.get("distance_from_point_km"),
        "flyover_mode": flyover_mode,
    }

    locations = []
    for dest, dest_point, dest_airport, drive, transit in zip(DESTINATIONS, dest_points, dest_airports, drive_results, transit_results):
        loc = _empty_location(dest["name"])
        errors = []

        if drive["available"]:
            loc["drive_distance_km"] = drive["km"]
            loc["drive_duration"] = drive["minutes"]
        else:
            errors.append(f"driving unavailable: {drive['reason']}")
        if transit["available"]:
            loc["transit_distance_km"] = transit["km"]
            loc["transit_duration"] = transit["minutes"]
        else:
            errors.append(f"transit unavailable: {transit['reason']}")

        if commercial_airport["iata"] == dest_airport["iata"]:
            # Same nearest commercial airport on both ends -- flying doesn't make sense here.
            errors.append(
                f"flyover not applicable: origin and destination share the same nearest "
                f"commercial airport ({commercial_airport['iata']})"
            )
        else:
            dest_airport_point = {"lat": dest_airport["lat"], "lng": dest_airport["lng"], "place_id": dest_airport["place_id"]}
            airport_to_dest = provider.drive_minutes(dest_airport_point, dest_point)

            if flyover_mode == "direct":
                # Nothing reachable by road at all -- skip the ground leg and estimate the
                # flight directly from the postal code's own coordinates.
                flight = estimate_flight_minutes({"lat": origin_lat, "lng": origin_lng}, dest_airport)
                origin_leg = None
            else:
                flight = estimate_flight_minutes(origin_airport, dest_airport)
                origin_leg = (
                    {"distance_km": origin_to_airport["km"], "duration_min": origin_to_airport["minutes"]}
                    if origin_to_airport["available"] else None
                )

            loc["flyover_details"] = {
                "flyover_mode": flyover_mode,
                "origin_airport": origin_airport["iata"] or origin_airport["name"],
                "destination_airport": dest_airport["iata"],
                "origin_to_airport": origin_leg,
                "flight": {"distance_km": flight["great_circle_km"], "duration_min": flight["minutes"]},
                "wait_min": config.FIXED_PROCESS_OVERHEAD_MIN,
                "airport_to_destination": (
                    {"distance_km": airport_to_dest["km"], "duration_min": airport_to_dest["minutes"]}
                    if airport_to_dest["available"] else None
                ),
            }

            if flyover_mode == "fallback_airport":
                errors.append(
                    f"flyover uses fallback airport {origin_airport['name']} found via live search "
                    "(no scheduled service; nearest real commercial airport has no road at all)"
                )
            elif flyover_mode == "direct":
                errors.append(
                    "flyover is a direct estimate (no reachable airport, commercial or otherwise, "
                    "found by road); flight computed straight from the postal code's location"
                )
            if not airport_to_dest["available"]:
                errors.append(f"flyover airport-to-destination leg unavailable: {airport_to_dest['reason']}")

            ground_km = origin_leg["distance_km"] if origin_leg else 0.0
            ground_min = origin_leg["duration_min"] if origin_leg else 0.0
            if (flyover_mode == "direct" or origin_leg is not None) and airport_to_dest["available"]:
                loc["flyover_distance_km"] = round(ground_km + flight["great_circle_km"] + airport_to_dest["km"], 2)
                loc["flyover_duration"] = round(
                    ground_min + flight["minutes"] + config.FIXED_PROCESS_OVERHEAD_MIN + airport_to_dest["minutes"], 1
                )

        loc["error"] = "; ".join(errors)
        locations.append(loc)

    return {
        "postal_code": postal_code,
        "error": "",
        "origin": origin_info,
        "locations": locations,
    }
