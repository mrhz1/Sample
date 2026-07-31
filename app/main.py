"""
Ontario Travel-Time Estimator API.

Endpoints:
  GET  /estimate/{postal_code}  Drive, transit, and flyover estimates from one postal code
                                 to the 5 fixed destinations (data/destinations.csv).
  POST /batch/excel             Same logic, looped over every postal code in an uploaded
                                 .csv/.xlsx; returns the results as an .xlsx.

Both endpoints call the same process_postal_code() (pipeline.py) and share one
process-wide RoutingProvider -- one calculation, reused per row for batch.

Run with: uvicorn app.main:app --reload
"""

import io

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from .excel_io import build_workbook_bytes, read_input_rows_from_buffer
from .pipeline import process_postal_code
from .routing_provider import GoogleMapsProvider

app = FastAPI(
    title="Ontario Travel-Time Estimator API",
    description=(
        "Drive, transit, and flyover travel-time estimates from an Ontario postal code "
        "to 5 fixed destinations."
    ),
    version="1.0.0",
)

_provider = GoogleMapsProvider()


@app.get("/")
def root():
    return {
        "name": "Ontario Travel-Time Estimator API",
        "endpoints": {
            "GET /estimate/{postal_code}": "Drive/transit/flyover estimate for one postal code, as JSON",
            "POST /batch/excel": "Upload a .csv/.xlsx of postal codes; returns the results as an .xlsx",
        },
    }


@app.get("/estimate/{postal_code}")
def estimate(postal_code: str):
    return process_postal_code(postal_code, _provider)


@app.post("/batch/excel")
def batch_excel(file: UploadFile = File(...)):
    content = file.file.read()
    try:
        input_rows = read_input_rows_from_buffer(io.BytesIO(content), file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not input_rows:
        raise HTTPException(status_code=400, detail="No postal codes found in the uploaded file.")

    rows = []
    for input_row in input_rows:
        row = process_postal_code(input_row["postal_code"], _provider)
        row["temporary_id"] = input_row["temporary_id"]
        rows.append(row)

    xlsx_bytes = build_workbook_bytes(rows)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=results.xlsx"},
    )
