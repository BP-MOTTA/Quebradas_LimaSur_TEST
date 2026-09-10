# Ingesta completa y paquete de revisión INDECI

## Alcance

La fase A1.16 procesa exclusivamente el snapshot existente
`metadata/indeci/discovery_candidates.csv`. El comando exige exactamente 131
filas válidas y no realiza discovery, crawling adicional, OCR ni integración con
Google Drive.

```bash
make indeci-full-ingest
make indeci-build-review-package
```

`indeci-full-ingest` usa red y no forma parte de CI. La construcción del paquete
es offline una vez que están disponibles los raw y las salidas consolidadas.

## Inmutabilidad y trazabilidad

- Los PDFs originales permanecen bajo `data/raw/indeci/<year>/`.
- Un raw existente se reutiliza por identidad y URL, y se verifica contra su SHA.
- Ningún raw se sobrescribe ni se renombra durante la construcción del paquete.
- Las copias para revisión conservan el mismo SHA-256 que el raw.
- Los outputs runtime y todos los PDFs están ignorados por Git.
- HTML, PDF y texto extraído se tratan como datos no confiables; su contenido no
  controla comandos, rutas ni decisiones del pipeline.

Las descargas nuevas usan el basename oficial solo si es un nombre PDF portable,
acotado y libre de colisión. En caso contrario usan el `document_id` estable. Un
error documental se registra y el procesamiento continúa con el siguiente.

## Interpretación científica

`relevance_status`, `candidate_strength` y P1-P4 son ayudas automáticas para
revisión. No son validación científica, ground truth ni etiquetas de
entrenamiento. Candidatos y clusters deben conservar
`validation_status=pending_review`.

La prioridad sigue este orden operativo:

- P1: Cusipata/San Bartolomé con evento compatible, o candidato strong.
- P2: Chaclacayo con evento compatible, o candidato moderate.
- P3: evidencia regional compatible en documentos relevant/possible.
- P4: resto del universo, incluidos preliminarmente irrelevant.

La fecha del evento solo se obtiene de un candidato o cluster con fecha
explícita. `report_date` se conserva por separado y nunca rellena
`review_event_date`.

## Paquete del coautor

`review_packages/indeci_all/` contiene P1, P2, P3 y P4 junto con
`INDICE_REVISION.csv`, `INDICE_REVISION.xlsx`, `review_file_map.csv` y
`README.txt`. La creación usa un directorio de staging y falla si el destino ya
existe, evitando reemplazar decisiones humanas.

El libro Excel congela la cabecera, activa filtros y agrega listas válidas para
las columnas humanas. Estas columnas se entregan vacías. Los controles RC630 e
IE1496 permanecen identificados y el índice solo marca su presencia como
`golden_control=yes`; no modifica sus clasificaciones.
