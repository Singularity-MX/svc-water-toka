# svc-water-toka

API REST (FastAPI) que consulta la humedad del suelo de **NASA SMAP L4** a través de **Google Earth Engine**, por punto, por área de 4 vértices o por polígono GeoJSON.

## Qué devuelve

Para cada consulta, la API entrega el promedio del periodo y la última imagen disponible dentro del periodo, para estas bandas:

| Banda | Unidad |
|---|---|
| `sm_surface` | m³/m³ |
| `sm_rootzone` | m³/m³ |
| `sm_profile` | m³/m³ |
| `sm_surface_wetness` | 0-1 |
| `sm_rootzone_wetness` | 0-1 |
| `sm_profile_wetness` | 0-1 |

Resolución espacial nominal de 9 km (píxel de 11 km en Earth Engine) y resolución temporal de 3 horas. SMAP L4 tiene unos días de retraso, por lo que las fechas más recientes pueden no tener datos.

## Estructura del proyecto

```
svc-water-toka/
├── main.py
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

## Requisitos

- Docker (o Python 3.12 para correrlo sin contenedor).
- Un proyecto de Google Cloud con la **Earth Engine API** habilitada y registrado en Earth Engine.
- Una **cuenta de servicio** con su llave JSON (ver más abajo).

## Configuración

Se hace con variables de entorno:

| Variable | Obligatoria | Descripción | Por defecto |
|---|---|---|---|
| `GEE_PROJECT_ID` | Sí | ID del proyecto de Google Cloud | — |
| `GEE_SERVICE_ACCOUNT` | No* | Correo de la cuenta de servicio | — |
| `GEE_PRIVATE_KEY_FILE` | No* | Ruta a la llave JSON de la cuenta de servicio | — |
| `HOST` | No | Dirección en la que escucha | `0.0.0.0` |
| `PORT` | No | Puerto en el que escucha | `5003` (en Docker) |
| `DEFAULT_DAYS_BACK` | No | Días hacia atrás si no se envían fechas | `7` |

\* Para correr en Docker son necesarias, porque el contenedor no tiene credenciales locales. Si no se definen, la app intenta usar las credenciales locales de `earthengine authenticate`.

### Crear la cuenta de servicio y la llave JSON

1. En https://console.cloud.google.com ve a **IAM y administración → Cuentas de servicio → Crear cuenta de servicio**.
2. Asígnale los roles **Earth Engine Resource Viewer** y **Service Usage Consumer**.
3. Entra a la cuenta, pestaña **Claves → Agregar clave → Crear clave nueva → JSON**. Se descarga el archivo `.json`.
4. Si hace falta, registra la cuenta de servicio en Earth Engine: https://signup.earthengine.google.com/#!/service_accounts

> La llave JSON es una credencial secreta. No la subas a git ni la copies dentro de la imagen. El `.dockerignore` excluye los `.json` para evitarlo.

## Uso con Docker

Construir la imagen:

```bash
sudo docker build -t svc-water-toka .
```

Correr el contenedor:

```bash
sudo docker run -d --name svc-water-toka --restart unless-stopped \
  -p 5003:5003 \
  -e GEE_PROJECT_ID=mi-proyecto-123456 \
  -e GEE_SERVICE_ACCOUNT=svc-water-toka@mi-proyecto-123456.iam.gserviceaccount.com \
  -e GEE_PRIVATE_KEY_FILE=/secrets/key.json \
  -v /ruta/local/key.json:/secrets/key.json:ro \
  svc-water-toka
```

Verificar:

```bash
sudo docker ps                      # el estado debe pasar a "healthy"
sudo docker logs svc-water-toka     # debe decir "Google Earth Engine inicializado"
curl http://localhost:5003/health
```

### Cambiar el puerto

El más simple es cambiar solo el puerto externo del mapeo. Por ejemplo, para exponerlo en el 8080:

```bash
-p 8080:5003
```

Los puertos de un contenedor existente no se pueden modificar: hay que detenerlo (`docker stop`), borrarlo (`docker rm`) y crearlo de nuevo.

## Uso sin Docker

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export GEE_PROJECT_ID=mi-proyecto-123456
export PORT=5003
python main.py
```

## Endpoints

Documentación interactiva (Swagger) en `http://localhost:5003/docs`.

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado del servicio |
| GET | `/api/v1/smap/point` | Humedad en un punto (`lat`, `lon`) |
| POST | `/api/v1/smap/point` | Igual, con cuerpo JSON |
| GET | `/api/v1/smap/area` | Área de 4 vértices (`lat1..lat4`, `lon1..lon4`) |
| POST | `/api/v1/smap/area` | Igual, con cuerpo JSON |
| POST | `/api/v1/clima-historico` | Área de 4 vértices en el campo `poligono` |
| POST | `/api/v1/smap/mean` | Polígono o MultiPolygon GeoJSON |

En todos los endpoints, `start_date` y `end_date` (formato `YYYY-MM-DD`) son opcionales, excepto en `/smap/mean`, donde son obligatorios. Si se omiten, se usan los últimos `DEFAULT_DAYS_BACK` días.

Los 4 vértices se ordenan automáticamente alrededor de su centro, así que el orden en que se envíen no importa, pero deben ser 4 puntos distintos.

### Ejemplos

Punto (GET):

```bash
curl "http://localhost:5003/api/v1/smap/point?lat=20.72&lon=-101.95"
```

Punto con fechas (POST):

```bash
curl -X POST http://localhost:5003/api/v1/smap/point \
  -H "Content-Type: application/json" \
  -d '{"lat": 20.72, "lon": -101.95, "start_date": "2026-11-01", "end_date": "2026-11-10"}'
```

Área de 4 vértices (GET):

```bash
curl "http://localhost:5003/api/v1/smap/area?lat1=20.8&lon1=-102&lat2=20.8&lon2=-101.8&lat3=20.6&lon3=-101.8&lat4=20.6&lon4=-102"
```

Clima histórico (POST):

```bash
curl -X POST http://localhost:5003/api/v1/clima-historico \
  -H "Content-Type: application/json" \
  -d '{
    "poligono": [
      {"lat": 20.9215, "lon": -101.4230},
      {"lat": 20.9230, "lon": -101.4210},
      {"lat": 20.9205, "lon": -101.4195},
      {"lat": 20.9195, "lon": -101.4220}
    ]
  }'
```

Polígono GeoJSON (POST):

```bash
curl -X POST http://localhost:5003/api/v1/smap/mean \
  -H "Content-Type: application/json" \
  -d '{
    "geometry": {
      "type": "Polygon",
      "coordinates": [[[-102,20.6],[-101.8,20.6],[-101.8,20.8],[-102,20.8],[-102,20.6]]]
    },
    "start_date": "2026-11-01",
    "end_date": "2026-11-10"
  }'
```

### Respuesta

```json
{
  "area_km2": 0.0,
  "source": "NASA SMAP L4",
  "dataset": "NASA/SMAP/SPL4SMGP/008",
  "period": { "start_date": "...", "end_date": "...", "image_count": 0 },
  "latest_observation_utc": "...",
  "period_mean": { "sm_surface": 0.0, "...": 0.0 },
  "latest_image_in_period": { "sm_surface": 0.0, "...": 0.0 },
  "units": { "sm_surface": "m3/m3", "...": "..." },
  "spatial_resolution": { "nominal_km": 9, "earth_engine_pixel_size_m": 11000 },
  "temporal_resolution": "3 hours"
}
```

Los endpoints de punto agregan `location`, y los de área, `vertices`. Si un área es menor que un píxel y no devuelve datos, se usa el píxel del centro del área y se incluye un campo `note`.

### Códigos de error

| Código | Causa |
|---|---|
| 404 | No hay imágenes SMAP para las fechas pedidas |
| 422 | Parámetros inválidos (fecha final anterior a la inicial, vértices repetidos, geometría inválida) |
| 502 | Error al consultar Google Earth Engine |
