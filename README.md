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
```

`make inventory` ejecuta un flujo offline sobre una fixture sintetica de prueba y
genera `inventory.json`, `inventory.csv` y `manifest.json` bajo `outputs/`.
Esos archivos son salidas regenerables y no se versionan.

`make indeci-live-smoke` es el unico comando con acceso de red habilitado. Realiza
cuatro consultas GET secuenciales y acotadas al portal publico de INDECI, conserva
solo metadatos y escribe `metadata/indeci/live_smoke_results.json`. No sigue los
enlaces de las fichas ni descarga PDFs. La configuracion en
`configs/sources/indeci_cusipata.yaml` usa sintaxis YAML compatible con JSON para
evitar dependencias runtime adicionales.

El mismo flujo puede invocarse explicitamente con:

```bash
python -m quebradas indeci live-smoke \
  --config configs/sources/indeci_cusipata.yaml
```

`historical` y `upload-drive` permanecen fuera de alcance. Cualquier barrido
historico, descarga de PDFs o uso de Google Drive requiere aprobacion separada.
