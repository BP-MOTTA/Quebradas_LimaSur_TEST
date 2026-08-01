# Quebradas Lima Este

Pipeline reproducible para consultar y seleccionar candidatos satelitales
Sentinel orientados al estudio probabilístico de activación de huaycos en
quebradas de Lima Este.

El primer piloto es la quebrada Cusipata / San Bartolomé, distrito de
Chaclacayo, con evento inicial observado el 16 de marzo de 2023. El CRS de
trabajo geoespacial del proyecto es `EPSG:32718`; las consultas de catálogo
satelital usan geometria en `EPSG:4326`.

## Alcance actual

Esta primera versión implementa solo consulta de catálogo STAC, normalización de
metadatos, cálculo de cobertura del AOI y selección de candidatos preevento y
postevento. No descarga productos satelitales completos ni procesa imagenes.

No incluye autenticación OData, ESA SNAP, preprocesamiento SAR, filtros speckle,
DEM, lluvia antecedente, modelos de Machine Learning ni coordenadas reales del
piloto.

## Instalación

Requisitos:

- Python 3.11 o superior.
- `pip`.

```bash
make setup
```

## Estructura

```text
configs/pilots/                 Configuraciones YAML de pilotos.
data/aoi/                       Ubicación esperada para AOI reales.
metadata/catalogs/              CSV generados por el catálogo.
src/quebradas/acquisition/      Módulo de catálogo satelital.
tests/fixtures/                 Fixtures sintéticos para pruebas offline.
tests/acquisition/              Pruebas unitarias y de CLI.
```

## AOI real

El comando real espera el archivo:

```text
data/aoi/cusipata_satellite_aoi.geojson
```

No se versiona ningún AOI real ni coordenadas ficticias en `data/aoi/`. Si el
archivo falta, el comando real se detiene con un error comprensible antes de
hacer solicitudes al catálogo.

El AOI puede incluir un objeto `crs` GeoJSON de estilo `name`. Si no declara CRS,
se trata como `EPSG:4326`, que es la convención práctica para GeoJSON. Cuando el
CRS de origen difiere del CRS de catálogo, la geometría se reproyecta a
`EPSG:4326` para consultar STAC.

## Comandos

Ejecutar lint:

```bash
make lint
```

Ejecutar pruebas:

```bash
make test
```

Ejecutar catálogo offline con fixtures sintéticos:

```bash
make catalog-offline
```

Ejecutar consulta real al STAC oficial de Copernicus Data Space:

```bash
make catalog
```

También puede llamarse directamente con el entorno creado por `make setup`:

```bash
.venv/bin/python -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml
.venv/bin/python -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml --offline-fixtures
.venv/bin/python -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml --sensor sentinel1
.venv/bin/python -m quebradas catalog --config configs/pilots/cusipata_20230316.yaml --sensor sentinel2
```

## Salidas CSV

El piloto escribe:

- `metadata/catalogs/sentinel1_candidates.csv`
- `metadata/catalogs/sentinel2_candidates.csv`
- `metadata/catalogs/scene_pairs.csv`

Los catálogos conservan escenas aceptadas y rechazadas. Las escenas rechazadas
incluyen `rejected_reason`. Los campos STAC ausentes se registran como valores
nulos en el CSV y no se inventan órbitas, polarizaciones, nubosidad ni fechas.

La cobertura se calcula como:

```text
area(intersección escena, AOI) / area(AOI)
```

usando una proyección UTM métrica estimada desde el centroide del AOI.

## Seguridad

No almacenes secretos en Git. `.env`, tokens y `data/raw/` están ignorados. La
plantilla `.env.example` solo contiene nombres de variables sin valores reales.
La autenticación no está implementada en esta etapa.

## Limitaciones

- La calidad de selección depende de los metadatos disponibles en cada item STAC.
- El emparejamiento implementado es Sentinel-1 y exige compatibilidad cuando hay
  metadatos de órbita, dirección, modo y polarizaciones disponibles.
- No se emiten conclusiones operativas ni alertas para la población.
- El modo offline usa fixtures sintéticos exclusivamente para pruebas.
