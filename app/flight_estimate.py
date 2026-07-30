"""
Offline flight-time estimate (protocol point 4). No API call, no stops/layovers modeled --
the fixed 3h process overhead (config.FIXED_PROCESS_OVERHEAD_MIN) is what absorbs all of
that, applied separately by pipeline.py. This module only estimates the single flight leg
itself: origin's nearest airport -> destination's nearest airport.
"""

from . import config
from .geo_utils import haversine_km


def estimate_flight_minutes(origin_airport: dict, destination_airport: dict) -> dict:
    great_circle_km = haversine_km(
        origin_airport["lat"], origin_airport["lng"],
        destination_airport["lat"], destination_airport["lng"],
    )
    minutes = config.FLIGHT_FIXED_OVERHEAD_MIN + (great_circle_km / config.FLIGHT_CRUISE_KMH) * 60
    return {
        "minutes": round(minutes, 1),
        "great_circle_km": round(great_circle_km, 2),
    }
