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
```

`make inventory` ejecuta un flujo offline sobre una fixture sintetica de prueba y
genera `inventory.json`, `inventory.csv` y `manifest.json` bajo `outputs/`.
Esos archivos son salidas regenerables y no se versionan.

Los modos `historical`, `live-smoke` y `upload-drive` permanecen bloqueados.
Cualquier consulta real a INDECI/COEN, descarga de PDFs o uso de Google Drive
requiere aprobacion separada.
