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
```

`make inventory` ejecuta un flujo offline sobre una fixture sintetica de prueba y
genera `inventory.json`, `inventory.csv` y `manifest.json` bajo `outputs/`.
Esos archivos son salidas regenerables y no se versionan.

Los dos objetivos `indeci-live-smoke` son los unicos comandos con acceso de red
habilitado. `make indeci-live-smoke` realiza cuatro consultas GET secuenciales al
archivo de reportes preliminares/complementarios. El objetivo
`make indeci-live-smoke-emergency` realiza una sola consulta `1496`/`2023` al
archivo de informes de emergencia. Ambos conservan solo metadatos y escriben la
ejecucion mas reciente en `metadata/indeci/live_smoke_results.json`; nunca siguen
los enlaces de las fichas ni descargan PDFs.

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

`historical` y `upload-drive` permanecen fuera de alcance. Cualquier barrido
historico, descarga de PDFs o uso de Google Drive requiere aprobacion separada.
