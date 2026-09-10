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
make indeci-discovery-dry-run
make indeci-batch-select
make indeci-batch-summary
make indeci-review-batch
make indeci-review-summary
make indeci-full-ingest
make indeci-build-review-package
make indeci-filter-limaeste
make indeci-build-limaeste-review-package
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

`make indeci-discovery-dry-run` consulta secuencialmente una sola pagina por
consulta en los dos archivos publicos documentados de INDECI y agrega las URLs
seed como un tercer mecanismo separado. Se limita a 2017, 2019, 2023 y 2024;
solo escribe metadatos en `metadata/indeci/discovery_candidates.csv` y
`metadata/indeci/discovery_run.json`. No sigue enlaces, no descarga PDFs y no
ejecuta ingestion. `make indeci-discovery-live` ejecuta el mismo dry-run de red
de forma explicita y permanece fuera de CI. Las fuentes y sus limitaciones se
describen en `docs/indeci_discovery_sources.md`.

`make indeci-batch-select` es offline: ordena los candidatos descubiertos por
evidencia preliminar en el titulo, aplica una cuota de hasta seis documentos
por ano e incluye los controles RC630 e IE1496. El tier solo establece orden de
revision; no determina relevancia cientifica y no excluye definitivamente
ningun candidato. `make indeci-batch-live` descarga como maximo 25 seleccionados
de forma secuencial, continua ante fallos documentales y permanece fuera de CI.

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

El discovery controlado puede invocarse con:

```bash
python -m quebradas indeci discover \
  --config configs/sources/indeci_cusipata.yaml \
  --years 2017,2019,2023,2024 \
  --dry-run
```

La ausencia de un documento en uno o ambos indices no demuestra que el
documento no exista. El CSV conserva fuentes intentadas, fuentes coincidentes y
estado de deduplicacion para revision posterior.

La muestra reproducible se crea sin red con:

```bash
python -m quebradas indeci select-batch \
  --discovery metadata/indeci/discovery_candidates.csv \
  --max-documents 25
```

La seleccion completa se registra en
`metadata/indeci/batch_selection.csv`. Solo las filas `selected=true` se
procesan mediante el comando de red explicitamente aprobado:

```bash
python -m quebradas indeci ingest-batch \
  --selection metadata/indeci/batch_selection.csv
```

El batch reutiliza descarga segura, SHA-256, extraccion, clasificacion,
extraccion de candidatos, auditoria y consolidacion. Escribe
`batch_documents.csv`, `batch_event_candidates.csv`,
`batch_event_clusters.csv`, `review_queue.csv` y `batch_run.json` bajo
`metadata/indeci/`; todas estas salidas y los PDFs permanecen ignorados por
Git. Un PDF sin texto se marca `ocr_required=true`, pero este flujo no ejecuta
OCR. `--allow-large-batch` existe como anulacion explicita y no forma parte de
la ejecucion A1.14.

El resumen local se consulta sin red con:

```bash
python -m quebradas indeci batch-summary
```

`relevance_status=relevant` no significa `event_confirmed=true`, y
`candidate_strength=strong` no significa `ground_truth=1`. Documentos,
candidatos y clusters siguen pendientes de revision humana; el batch no valida
evidencia automaticamente.

La revision cientifica local se organiza primero por documento y luego por sus
clusters y fragmentos de evidencia:

```bash
python -m quebradas indeci review-batch
```

Cada documento completado se guarda en
`metadata/indeci/document_review.csv`; cada decision de cluster se guarda en
`metadata/indeci/human_review.csv`. Ambos archivos son estado curado local,
estan ignorados por Git y deben respaldarse fuera del repositorio. Una sesion
interrumpida continua con el siguiente documento pendiente y nunca reemplaza
una decision existente. Si cambia la evidencia automatica, la siguiente sesion
interactiva conserva las decisiones y marca `review_stale=true`.

El packet compacto muestra solo metadatos, candidatos y fragmentos acotados ya
presentes en los CSV; nunca abre el PDF ni muestra su texto completo. Los
controles RC630 e IE1496 aparecen como `golden_control=true`, pero no entran en
la carga de nueva validacion. `human_inventory_decision=include` es una decision
de inventario y no crea `training_label` ni ground truth.

Los modos de solo lectura, sin red ni escritura, son:

```bash
python -m quebradas indeci review-batch --summary
python -m quebradas indeci review-summary
```

La ingesta completa A1.16 consume exclusivamente el snapshot local ya aprobado
de 131 candidatos. No ejecuta discovery y queda deshabilitada en GitHub Actions:

```bash
python -m quebradas indeci ingest-discovery \
  --discovery metadata/indeci/discovery_candidates.csv
```

El flujo reutiliza el registro y los raw existentes, descarga solo faltantes de
forma secuencial y procesa cada documento con el pipeline validado. Los raw no se
sobrescriben ni se renombran; las descargas nuevas conservan el basename oficial
cuando es portable y no colisiona. Los resultados completos se escriben en
`all_documents.csv`, `all_event_candidates.csv`, `all_event_clusters.csv`,
`duplicate_review.csv` y `full_ingestion_run.json` bajo `metadata/indeci/`.

Una vez disponibles los PDFs, el paquete para el coautor se construye offline:

```bash
python -m quebradas indeci build-review-package
```

El directorio `review_packages/indeci_all/` contiene una sola copia física de
cada PDF bajo P1-P4, un mapa verificable raw-copia y los índices CSV/XLSX. El
constructor se niega a reemplazar un paquete existente para proteger cualquier
avance humano. P1-P4 solo ordena revisión: no valida eventos, no excluye
documentos y no crea `training_label`. La fecha de evento solo procede de
evidencia extraída; nunca se sustituye por la fecha del reporte. El contrato
completo se documenta en `docs/indeci_full_ingestion.md`.

El filtro geográfico y temático A1.16 funciona exclusivamente sobre esos
archivos y textos locales:

```bash
python -m quebradas indeci filter-limaeste
python -m quebradas indeci build-limaeste-review-package
```

El primer comando conserva las 131 filas y añade variables derivadas separadas
para cercanía espacial, pertinencia del evento, relación explícita con lluvia y
prioridad P1-PX. Escribe dos CSV filtrados y los resúmenes geográficos bajo
`metadata/indeci/`. El segundo copia únicamente PDFs ya disponibles a
`review_packages/cusipata_limaeste_filtered/` y crea índices CSV/XLSX con las
decisiones humanas vacías. Ninguno descarga, ejecuta OCR, modifica raw ni crea
ground truth. `PX` significa fuera de revisión prioritaria, no evento falso. El
contrato se detalla en `docs/indeci_limaeste_filter.md`.

Las opciones de ruta de ambos comandos permiten revisar un batch aislado dentro
del workspace. El contrato de campos y las salvaguardas se detallan en
`docs/indeci_human_review.md`.

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

Las salidas de una ingestion pueden aislarse con `--output` y
`--candidate-output`. Para procesar un PDF local explicito sin copiarlo a
`data/raw/`:

```bash
python -m quebradas indeci ingest-file \
  --config configs/sources/indeci_cusipata.yaml \
  --file "/ruta/al/documento.pdf"
```

`ingest-file` deriva la identidad del nombre original, limita tamano y firma,
rechaza symlinks y registra ruta original, SHA-256, tamano, paginas y hora UTC.
`--source-url` es opcional y solo debe usarse cuando la URL oficial ya es
conocida; nunca se infiere una URL desde el contenido local.

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

Dos auditorias de control ya separadas pueden resumirse sin conservar sus
fragmentos documentales:

```bash
python -m quebradas indeci compare-golden-controls \
  --negative-audit metadata/indeci/events_cusipata_audit.csv \
  --positive-audit metadata/indeci/events_cusipata_positive_audit.csv
```

El resultado runtime `metadata/indeci/golden_controls.json` contiene unicamente
el `document_id` y los conteos `strong`, `moderate` y `weak` de cada control.

`historical` y `upload-drive` permanecen fuera de alcance. Cualquier barrido
historico, URL real adicional o uso de Google Drive requiere aprobacion
separada.
