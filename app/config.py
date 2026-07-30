"""
Tunable constants for the Ontario travel-time estimator. Everything adjustable lives here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = BASE_DIR / "data"
AIRPORTS_CSV = DATA_DIR / "airports.csv"
DESTINATIONS_CSV = DATA_DIR / "destinations.csv"

# --- Routing provider (Google Maps Platform; see routing_provider.py) ---
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAP_API_KEY")
GEOCODING_API_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ROUTE_MATRIX_API_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
PLACES_SEARCH_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Used only as a remote-origin fallback (see pipeline.py) when the nearest REAL commercial
# airport has no road connection at all: search Google's live Places database for any
# nearby airport instead. 50km is the Places API Nearby Search hard maximum radius.
NEARBY_AIRPORT_SEARCH_RADIUS_M = 50000.0
NEARBY_AIRPORT_MAX_RESULTS = 5

# TRANSIT queries with no departureTime default to "right now" server-side, which for a
# sparse intercity route can land right after the last departure of the day and return a
# huge overnight-wait duration -- nothing like what a rider would actually experience or
# what Google Maps' own website shows. Pinning a near-future, fixed departure avoids that.
TRANSIT_DEPARTURE_DAYS_AHEAD = 1
TRANSIT_DEPARTURE_HOUR = 9  # local time, America/Toronto
TRANSIT_TIMEZONE = "America/Toronto"

# --- Offline flight-time estimate (single leg, no stops/layovers modeled) ---
# leg_minutes = FLIGHT_FIXED_OVERHEAD_MIN + (great_circle_km / FLIGHT_CRUISE_KMH) * 60
FLIGHT_CRUISE_KMH = 700.0
FLIGHT_FIXED_OVERHEAD_MIN = 30.0

# Fixed overhead for check-in/security/process, absorbing ALL connection time. A flat
# number, not a calibrated estimate.
FIXED_PROCESS_OVERHEAD_HOURS = 3.0
FIXED_PROCESS_OVERHEAD_MIN = FIXED_PROCESS_OVERHEAD_HOURS * 60
