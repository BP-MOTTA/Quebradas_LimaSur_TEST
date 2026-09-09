# Quebradas Lima Este

Pipeline reproducible para la adquisición, preparación e integración de
imágenes satelitales y variables geoespaciales orientadas al estudio
probabilístico de activación de huaycos en quebradas de Lima Este.

## Piloto inicial

- Quebrada: Cusipata / San Bartolomé
- Distrito: Chaclacayo
- Evento inicial: 16 de marzo de 2023
- CRS de trabajo: EPSG:32718

## Desarrollo

```bash
make setup
make lint
make test
make inventory
make indeci-live-smoke
make indeci-live-smoke-emergency
make indeci-ingest-seeds-live
```

`make inventory` ejecuta un flujo offline sobre una fixture sintetica de prueba y
genera `inventory.json`, `inventory.csv` y `manifest.json` bajo `outputs/`.
Esos archivos son salidas regenerables y no se versionan.

Los dos objetivos `indeci-live-smoke` realizan comprobaciones de metadatos con
acceso de red. `make indeci-live-smoke` realiza cuatro consultas GET secuenciales al
archivo de reportes preliminares/complementarios. El objetivo
`make indeci-live-smoke-emergency` realiza una sola consulta `1496`/`2023` al
archivo de informes de emergencia. Ambos conservan solo metadatos y escriben la
ejecucion mas reciente en `metadata/indeci/live_smoke_results.json`; nunca siguen
los enlaces de las fichas ni descargan PDFs.

`make indeci-ingest-seeds-live` es un flujo distinto y explicito. Descarga solo
las URLs publicas enumeradas en `configs/sources/indeci_seed_documents.yaml`,
valida dominio, respuesta y firma PDF, calcula SHA-256, extrae texto con
PyMuPDF y genera clasificaciones y candidatos que siempre requieren revision
humana. Tambien exporta los candidatos originales a
`metadata/indeci/events_cusipata_candidates.csv`. No descubre URLs, no pagina y
esta bloqueado en GitHub Actions.

Los PDFs se conservan sin sobrescritura bajo `data/raw/indeci/`; el texto
regenerable se escribe bajo `data/interim/indeci/`. Ambos, el registro local y
`metadata/indeci/ingestion_run.json` estan ignorados por Git. La distribucion o
despliegue de PyMuPDF debe revisar su licencia AGPL/comercial por separado.

Las configuraciones bajo `configs/sources/` usan sintaxis YAML compatible con
JSON para evitar dependencias runtime adicionales.

El mismo flujo puede invocarse explicitamente con:

```bash
python -m quebradas indeci live-smoke \
  --config configs/sources/indeci_cusipata.yaml
```

Para comprobar unicamente el informe de emergencia 1496:

```bash
python -m quebradas indeci live-smoke \
  --config configs/sources/indeci_emergency_1496.yaml
```

Para ingerir una URL oficial conocida sin depender de discovery:

```bash
python -m quebradas indeci ingest-url \
  --config configs/sources/indeci_cusipata.yaml \
  --url "<URL>"
```

Para ingerir exclusivamente los seed documents aprobados:

```bash
python -m quebradas indeci ingest-seeds \
  --config configs/sources/indeci_cusipata.yaml \
  --seeds configs/sources/indeci_seed_documents.yaml
```

Para auditar candidatos ya extraidos sin red ni cambios al CSV original:

```bash
python -m quebradas indeci audit-candidates \
  --input metadata/indeci/events_cusipata_candidates.csv
```

La politica de proximidad y exclusiones vive en
`configs/sources/indeci_candidate_quality.yaml`. El comando muestra cada
candidato con su fuerza y razon de revision, escribe una copia enriquecida en
`metadata/indeci/events_cusipata_audit.csv` y la consolidacion no destructiva en
`metadata/indeci/events_cusipata_consolidated.csv`. Los tres CSV reales estan
ignorados por Git. Ningun candidato o cluster cambia automaticamente de
`pending_review` a validado.

`historical` y `upload-drive` permanecen fuera de alcance. Cualquier barrido
historico, URL real adicional o uso de Google Drive requiere aprobacion
separada.
