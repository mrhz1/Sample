# Ontario Travel-Time Estimator

A FastAPI service that estimates travel time from an Ontario postal code to 5 fixed
destinations, by drive, public transit, and a "flyover" (drive to nearest airport → flight
→ drive from destination's nearest airport, plus a fixed 3h overhead).

## Endpoints

- `GET /estimate/{postal_code}` — drive/transit/flyover estimate for one postal code, as JSON.
- `POST /batch/excel` — upload a `.csv`/`.xlsx` of postal codes; returns the results as an `.xlsx`.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Google Maps Platform API key
uvicorn app.main:app --reload
```

Requires a Google Maps Platform API key (`.env`, `GOOGLE_MAP_API_KEY`) with the
**Geocoding API**, **Routes API**, and **Places API (New)** enabled.

## How it works

- **Drive / transit**: Google Routes API (`computeRouteMatrix`), batched per request.
- **Geocoding**: Google Geocoding API. Postal codes covering large rural areas are
  refined by re-geocoding the resolved locality name, since the raw postal-code centroid
  can land tens of km from where anyone actually lives.
- **Nearest airport**: offline, haversine distance against `app/data/airports.csv` (real
  Ontario commercial airports). If that airport has no road connection at all, falls back
  to a live Google Places "Nearby Search" for the nearest airport of any kind.
- **Flight leg**: offline estimate (great-circle distance between airports, fixed cruise
  speed and overhead) — no flight-schedule API is used.

See `app/` for the implementation; each module has a short docstring explaining its role.
