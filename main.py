import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import ee
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------
# CONFIGURACION (variables de entorno)
# --------------------------------------------------

PROJECT_ID = os.getenv("GEE_PROJECT_ID")

# Opcional: cuenta de servicio (si no se definen, usa credenciales locales)
SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT")
PRIVATE_KEY_FILE = os.getenv("GEE_PRIVATE_KEY_FILE")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5003"))

# Dias hacia atras que se consultan si no se envian fechas
DEFAULT_DAYS_BACK = int(os.getenv("DEFAULT_DAYS_BACK", "7"))

DATASET = "NASA/SMAP/SPL4SMGP/008"

BANDS = [
    "sm_surface",
    "sm_rootzone",
    "sm_profile",
    "sm_surface_wetness",
    "sm_rootzone_wetness",
    "sm_profile_wetness",
]

BAND_UNITS = {
    "sm_surface": "m3/m3",
    "sm_rootzone": "m3/m3",
    "sm_profile": "m3/m3",
    "sm_surface_wetness": "0-1",
    "sm_rootzone_wetness": "0-1",
    "sm_profile_wetness": "0-1",
}

SCALE_M = 11000  # tamano de pixel de SMAP L4 en Earth Engine


# --------------------------------------------------
# INICIALIZAR EARTH ENGINE
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):

    if not PROJECT_ID:
        raise RuntimeError("Debes configurar la variable GEE_PROJECT_ID")

    try:
        if SERVICE_ACCOUNT and PRIVATE_KEY_FILE:
            credentials = ee.ServiceAccountCredentials(
                SERVICE_ACCOUNT, PRIVATE_KEY_FILE
            )
            ee.Initialize(credentials, project=PROJECT_ID)
        else:
            ee.Initialize(project=PROJECT_ID)

        print("Google Earth Engine inicializado")

    except Exception as error:
        raise RuntimeError(
            f"No se pudo inicializar Earth Engine: {error}"
        ) from error

    yield


app = FastAPI(
    title="API de humedad SMAP",
    description="Consulta de humedad del suelo con NASA SMAP por coordenadas",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------
# MODELOS DE ENTRADA
# --------------------------------------------------

class PointRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitud")
    lon: float = Field(..., ge=-180, le=180, description="Longitud")
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class Coord(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitud")
    lon: float = Field(..., ge=-180, le=180, description="Longitud")


class QuadRequest(BaseModel):
    points: List[Coord] = Field(
        ..., min_length=4, max_length=4, description="4 vértices (lat, lon)"
    )
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class ClimaHistoricoRequest(BaseModel):
    poligono: List[Coord] = Field(
        ..., min_length=4, max_length=4, description="4 vértices (lat, lon)"
    )
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class PolygonRequest(BaseModel):
    geometry: Dict[str, Any]
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_request(self):
        if self.end_date < self.start_date:
            raise ValueError(
                "La fecha final no puede ser anterior a la inicial"
            )

        if self.geometry.get("type") not in ["Polygon", "MultiPolygon"]:
            raise ValueError(
                "La geometría debe ser Polygon o MultiPolygon GeoJSON"
            )

        if "coordinates" not in self.geometry:
            raise ValueError("La geometría debe incluir coordinates")

        return self


# --------------------------------------------------
# FUNCIONES AUXILIARES
# --------------------------------------------------

def clean_values(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Redondea los valores numericos. Los None (sin dato) se conservan."""
    clean = {}

    for key, value in (values or {}).items():
        if isinstance(value, (int, float)):
            clean[key] = round(float(value), 6)
        else:
            clean[key] = value

    return clean


def resolve_dates(start: Optional[date], end: Optional[date]):
    """Si no se envian fechas, usa los ultimos DEFAULT_DAYS_BACK dias."""
    today = datetime.now(timezone.utc).date()

    end = end or today
    start = start or (end - timedelta(days=DEFAULT_DAYS_BACK))

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="La fecha final no puede ser anterior a la inicial",
        )

    return start, end


def build_quad(points: List[tuple]) -> ee.Geometry:
    """
    Construye un poligono de 4 vertices a partir de (lat, lon).
    Los vertices se ordenan alrededor del centro para que el cuadro
    no quede cruzado, sin importar el orden en que se envien.
    Los lados son lineas rectas en lat/lon (geodesic=False).
    """
    if len(set(points)) != 4:
        raise HTTPException(
            status_code=422, detail="Los 4 puntos deben ser distintos"
        )

    c_lat = sum(p[0] for p in points) / 4
    c_lon = sum(p[1] for p in points) / 4

    ordered = sorted(
        points, key=lambda p: math.atan2(p[0] - c_lat, p[1] - c_lon)
    )

    ring = [[lon, lat] for lat, lon in ordered]
    ring.append(ring[0])

    return ee.Geometry(
        {
            "type": "Polygon",
            "coordinates": [ring],
            "geodesic": False,
        }
    )


def query_smap(
    geometry: ee.Geometry,
    start: date,
    end: date,
    allow_fallback: bool = True,
) -> Dict[str, Any]:
    """Promedio del periodo + ultima observacion para una geometria."""

    # Earth Engine usa fecha final exclusiva
    end_exclusive = end + timedelta(days=1)

    collection = (
        ee.ImageCollection(DATASET)
        .filterBounds(geometry)
        .filterDate(start.isoformat(), end_exclusive.isoformat())
        .select(BANDS)
    )

    image_count = collection.size().getInfo()

    if image_count == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                "No hay imagenes SMAP para las fechas solicitadas "
                "(SMAP L4 tiene unos dias de retraso)"
            ),
        )

    def reduce(image):
        return image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geometry,
            scale=SCALE_M,
            maxPixels=1e13,
            tileScale=4,
        )

    mean_image = collection.mean()
    latest_image = ee.Image(collection.sort("system:time_start", False).first())

    # Una sola llamada a Earth Engine para todo
    result = ee.Dictionary(
        {
            "mean": reduce(mean_image),
            "latest": reduce(latest_image),
            "latest_time": latest_image.get("system:time_start"),
            "area_km2": geometry.area(1).divide(1e6),
        }
    ).getInfo()

    area_km2 = round(float(result["area_km2"]), 3)
    note = None

    # Si el area es menor a un pixel y no devolvio datos,
    # se usa el centro del area.
    mean_values = result["mean"] or {}
    has_data = any(v is not None for v in mean_values.values())

    if not has_data and allow_fallback and area_km2 > 0:
        data = query_smap(
            geometry.centroid(1), start, end, allow_fallback=False
        )
        data["area_km2"] = area_km2
        data["note"] = (
            "El area no devolvio datos; se uso el pixel del centro del area."
        )
        return data

    latest_date = datetime.fromtimestamp(
        result["latest_time"] / 1000, tz=timezone.utc
    ).isoformat()

    return {
        "area_km2": area_km2,
        "source": "NASA SMAP L4",
        "dataset": DATASET,
        "period": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "image_count": image_count,
        },
        "latest_observation_utc": latest_date,
        "period_mean": clean_values(result["mean"]),
        "latest_image_in_period": clean_values(result["latest"]),
        "units": BAND_UNITS,
        "spatial_resolution": {
            "nominal_km": 9,
            "earth_engine_pixel_size_m": SCALE_M,
        },
        "temporal_resolution": "3 hours",
    }


def run_query(geometry: ee.Geometry, start: date, end: date):
    try:
        return query_smap(geometry, start, end)

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Error consultando Google Earth Engine: {str(error)}",
        )


# --------------------------------------------------
# ENDPOINTS POR COORDENADAS
# --------------------------------------------------

@app.get("/api/v1/smap/point")
def get_point_get(
    lat: float = Query(..., ge=-90, le=90, description="Latitud"),
    lon: float = Query(..., ge=-180, le=180, description="Longitud"),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD (opcional)"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD (opcional)"),
):
    """Ejemplo: /api/v1/smap/point?lat=20.72&lon=-101.95"""
    start, end = resolve_dates(start_date, end_date)
    geometry = ee.Geometry.Point([lon, lat])

    data = run_query(geometry, start, end)
    data["location"] = {"lat": lat, "lon": lon}
    return data


@app.post("/api/v1/smap/point")
def get_point_post(request: PointRequest):
    """Body: {"lat": 20.72, "lon": -101.95}"""
    start, end = resolve_dates(request.start_date, request.end_date)
    geometry = ee.Geometry.Point([request.lon, request.lat])

    data = run_query(geometry, start, end)
    data["location"] = {"lat": request.lat, "lon": request.lon}
    return data


# --------------------------------------------------
# ENDPOINTS POR 4 COORDENADAS (CUADRO)
# --------------------------------------------------

def quad_response(points: List[tuple], start, end):
    start, end = resolve_dates(start, end)
    geometry = build_quad(points)

    data = run_query(geometry, start, end)
    data["vertices"] = [{"lat": lat, "lon": lon} for lat, lon in points]
    return data


@app.get("/api/v1/smap/area")
def get_area_get(
    lat1: float = Query(..., ge=-90, le=90),
    lon1: float = Query(..., ge=-180, le=180),
    lat2: float = Query(..., ge=-90, le=90),
    lon2: float = Query(..., ge=-180, le=180),
    lat3: float = Query(..., ge=-90, le=90),
    lon3: float = Query(..., ge=-180, le=180),
    lat4: float = Query(..., ge=-90, le=90),
    lon4: float = Query(..., ge=-180, le=180),
    start_date: Optional[date] = Query(None, description="YYYY-MM-DD (opcional)"),
    end_date: Optional[date] = Query(None, description="YYYY-MM-DD (opcional)"),
):
    """Ejemplo: /api/v1/smap/area?lat1=20.8&lon1=-102&lat2=20.8&lon2=-101.8&lat3=20.6&lon3=-101.8&lat4=20.6&lon4=-102"""
    points = [(lat1, lon1), (lat2, lon2), (lat3, lon3), (lat4, lon4)]
    return quad_response(points, start_date, end_date)


@app.post("/api/v1/smap/area")
def get_area_post(request: QuadRequest):
    points = [(p.lat, p.lon) for p in request.points]
    return quad_response(points, request.start_date, request.end_date)


@app.post("/api/v1/clima-historico")
def clima_historico(request: ClimaHistoricoRequest):
    """
    Body:
    {
      "poligono": [
        {"lat": 20.9215, "lon": -101.4230},
        {"lat": 20.9230, "lon": -101.4210},
        {"lat": 20.9205, "lon": -101.4195},
        {"lat": 20.9195, "lon": -101.4220}
      ]
    }
    (start_date y end_date son opcionales)
    """
    points = [(p.lat, p.lon) for p in request.poligono]
    return quad_response(points, request.start_date, request.end_date)


# --------------------------------------------------
# ENDPOINT POR POLIGONO (el original)
# --------------------------------------------------

@app.post("/api/v1/smap/mean")
def get_smap_polygon(request: PolygonRequest):
    geometry = ee.Geometry(request.geometry)
    return run_query(geometry, request.start_date, request.end_date)


# --------------------------------------------------
# ENDPOINT DE SALUD
# --------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "SMAP API", "port": PORT}


# --------------------------------------------------
# ARRANQUE: python main.py
# --------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
