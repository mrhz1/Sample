"""
Single swappable interface for every external routing/geocoding call.

This is the ONLY module allowed to talk to a routing/geocoding vendor. If the
organization needs to swap providers (e.g. to a self-hosted OSRM/Nominatim stack to
avoid sending postal codes to a commercial US vendor), implement RoutingProvider and
point pipeline.py at the new class -- nothing else in the codebase changes.

geocode(postal_code) -> {"available": True, "lat", "lng", "formatted_address", "place_id"}
                       | {"available": False, "reason"}
drive_minutes(origin, destination) -> {"available": True, "minutes", "km"}
                                     | {"available": False, "reason"}
drive_minutes_batch(origin, destinations) -> list of the above, one per destination, same
                                              order -- ONE Route Matrix call for all of them
                                              instead of one call each.
transit_minutes_batch(origin, destinations) -> same as drive_minutes_batch, TRANSIT mode.
  origin/destination(s) are {"lat": float, "lng": float, "place_id": str | None (optional)}.
  When place_id is present, routing snaps to that exact place instead of the bare
  coordinate -- short hops especially can otherwise land on a spot with no direct road
  access (e.g. a bare Billy Bishop Airport coordinate vs. its actual terminal entrance),
  producing an inflated detour. Airport and destination place_ids are precomputed and
  stored in data/airports.csv and data/destinations.csv (they never change), so no
  resolve-by-address call is needed at request time -- only the input postal code itself
  is geocoded live.

DRIVE and TRANSIT cannot be combined into one call -- travelMode is a single field per
Route Matrix request, not settable per origin/destination pair.

find_nearby_airports(origin, radius_m, max_results) -> [{"name", "lat", "lng", "place_id"}, ...]
  Live search (Places API Nearby Search) for airports near a point, nearest first. Used
  only as a remote-origin fallback in pipeline.py when the nearest real commercial airport
  (data/airports.csv) has no road connection at all -- Google's own airport database is
  broader than our static commercial-airport list. No IATA code is available from this API,
  only a name.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import config
from .geo_utils import haversine_km


def _next_transit_departure_iso() -> str:
    """See config.TRANSIT_DEPARTURE_* -- a fixed, documented near-future departure time
    (not literal 'now') so TRANSIT results reflect a normal day's service."""
    tz = ZoneInfo(config.TRANSIT_TIMEZONE)
    target = (datetime.now(tz) + timedelta(days=config.TRANSIT_DEPARTURE_DAYS_AHEAD)).replace(
        hour=config.TRANSIT_DEPARTURE_HOUR, minute=0, second=0, microsecond=0
    )
    return target.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


class RoutingProvider(ABC):
    @abstractmethod
    def geocode(self, postal_code: str) -> dict: ...

    @abstractmethod
    def drive_minutes(self, origin: dict, destination: dict) -> dict: ...

    @abstractmethod
    def drive_minutes_batch(self, origin: dict, destinations: list[dict]) -> list[dict]: ...

    @abstractmethod
    def transit_minutes_batch(self, origin: dict, destinations: list[dict]) -> list[dict]: ...

    @abstractmethod
    def find_nearby_airports(self, origin: dict, radius_m: float, max_results: int) -> list[dict]: ...


class GoogleMapsProvider(RoutingProvider):
    """Google Maps Platform: Geocoding API + Routes API (computeRouteMatrix)."""

    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        self.api_key = api_key or config.GOOGLE_MAPS_API_KEY
        if not self.api_key:
            raise RuntimeError("GOOGLE_MAP_API_KEY is not set in .env")
        self.timeout = timeout

    def _geocode_raw(self, query: str) -> dict:
        params = {
            "address": query,
            "components": "country:CA|administrative_area:ON",
            "key": self.api_key,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(config.GEOCODING_API_URL, params=params)
        try:
            data = resp.json()
        except ValueError:
            return {"available": False, "reason": f"Geocoding API returned a non-JSON response (HTTP {resp.status_code})."}
        if data.get("status") != "OK" or not data.get("results"):
            return {"available": False, "reason": f"Geocoding failed: {data.get('status')}"}
        result = data["results"][0]
        location = result["geometry"]["location"]
        return {
            "available": True,
            "lat": location["lat"],
            "lng": location["lng"],
            "formatted_address": result.get("formatted_address"),
            "place_id": result.get("place_id"),
            "viewport_span_km": self._viewport_span_km(result["geometry"].get("viewport")),
            "locality": self._extract_locality(result.get("address_components", [])),
        }

    @staticmethod
    def _viewport_span_km(viewport: dict | None) -> float:
        """How large an area this geocode result actually covers. `location_type` alone
        can't tell a genuinely vast rural postal code (100+ km across) apart from an
        ordinary tight urban one -- Google marks both APPROXIMATE. A North York postal
        code's viewport is ~0.4 km across; a remote far-north FSA's can be 150+ km."""
        if not viewport:
            return 0.0
        ne, sw = viewport.get("northeast", {}), viewport.get("southwest", {})
        if not ne or not sw:
            return 0.0
        width_km = haversine_km(ne["lat"], sw["lng"], ne["lat"], ne["lng"])
        height_km = haversine_km(ne["lat"], sw["lng"], sw["lat"], sw["lng"])
        return max(width_km, height_km)

    @staticmethod
    def _extract_locality(address_components: list[dict]) -> str | None:
        for comp in address_components:
            if "locality" in comp.get("types", []):
                return comp["long_name"]
        for comp in address_components:
            types = comp.get("types", [])
            if "sublocality" in types or "sublocality_level_1" in types:
                return comp["long_name"]
        return None

    def geocode(self, postal_code: str) -> dict:
        result = self._geocode_raw(postal_code)
        if not result["available"]:
            return result
        # Postal codes covering vast rural areas can geocode to a centroid tens of km from
        # where anyone actually lives (e.g. off in the bush near a remote community) -- if
        # the covered area is large and Google told us which town/community this postal
        # code belongs to, re-geocode that name directly for a far more accurate point to
        # search for roads/airports around. An ordinary tight urban postal code (covering
        # well under a km) is left alone even though Google also marks it "APPROXIMATE".
        if result["viewport_span_km"] > config.GEOCODE_REFINE_VIEWPORT_KM and result.get("locality"):
            refined = self._geocode_raw(f"{result['locality']}, Ontario, Canada")
            if refined["available"] and refined["viewport_span_km"] < result["viewport_span_km"]:
                return refined
        return result

    @staticmethod
    def _waypoint(point: dict) -> dict:
        place_id = point.get("place_id")
        if place_id:
            return {"waypoint": {"placeId": place_id}}
        return {"waypoint": {"location": {"latLng": {"latitude": point["lat"], "longitude": point["lng"]}}}}

    def _route_matrix(self, origin: dict, destinations: list[dict], travel_mode: str) -> list[dict]:
        body = {
            "origins": [self._waypoint(origin)],
            "destinations": [self._waypoint(d) for d in destinations],
            "travelMode": travel_mode,
        }
        if travel_mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"
        elif travel_mode == "TRANSIT":
            body["departureTime"] = _next_transit_departure_iso()
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,distanceMeters,duration",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(config.ROUTE_MATRIX_API_URL, json=body, headers=headers)
        try:
            data = resp.json()
        except ValueError:
            reason = f"Route Matrix API returned a non-JSON response (HTTP {resp.status_code})."
            return [{"available": False, "reason": reason}] * len(destinations)

        if not isinstance(data, list):
            reason = data.get("error", {}).get("message") if isinstance(data, dict) else "Empty response from Route Matrix API."
            return [{"available": False, "reason": reason or "Empty response from Route Matrix API."}] * len(destinations)

        # The matrix API returns one element per (originIndex, destinationIndex) pair, not
        # necessarily in request order -- index results by destinationIndex to be safe.
        results: list[dict | None] = [None] * len(destinations)
        for element in data:
            idx = element.get("destinationIndex", 0)
            status = element.get("status") or {}
            condition = element.get("condition")
            if status.get("code") or condition == "ROUTE_NOT_FOUND":
                results[idx] = {"available": False, "reason": status.get("message") or condition or "No route found."}
            elif "distanceMeters" not in element or "duration" not in element:
                results[idx] = {"available": False, "reason": "Route Matrix API returned no distance/duration."}
            else:
                duration_seconds = float(str(element["duration"]).rstrip("s"))
                results[idx] = {
                    "available": True,
                    "minutes": round(duration_seconds / 60, 1),
                    "km": round(element["distanceMeters"] / 1000, 2),
                }

        return [r or {"available": False, "reason": "No result returned for this destination."} for r in results]

    def drive_minutes(self, origin: dict, destination: dict) -> dict:
        return self._route_matrix(origin, [destination], "DRIVE")[0]

    def drive_minutes_batch(self, origin: dict, destinations: list[dict]) -> list[dict]:
        return self._route_matrix(origin, destinations, "DRIVE")

    def transit_minutes_batch(self, origin: dict, destinations: list[dict]) -> list[dict]:
        return self._route_matrix(origin, destinations, "TRANSIT")

    def find_nearby_airports(self, origin: dict, radius_m: float, max_results: int) -> list[dict]:
        body = {
            "includedTypes": ["airport"],
            "maxResultCount": max_results,
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": origin["lat"], "longitude": origin["lng"]},
                    "radius": min(radius_m, 50000.0),  # Places API hard maximum
                }
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "places.id,places.displayName,places.location",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(config.PLACES_SEARCH_NEARBY_URL, json=body, headers=headers)
        try:
            data = resp.json()
        except ValueError:
            return []
        return [
            {
                "name": p.get("displayName", {}).get("text", ""),
                "lat": p["location"]["latitude"],
                "lng": p["location"]["longitude"],
                "place_id": p["id"],
            }
            for p in data.get("places", [])
        ]
