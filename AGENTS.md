# AGENTS.md

## Propósito

Este repositorio implementa un flujo reproducible para estimar la probabilidad de
activación de huaycos en quebradas de Lima Este mediante integración de Sentinel-1,
óptico VHR, DEM y lluvia antecedente.

## Reglas de trabajo

1. Lee este archivo antes de modificar el repositorio.
2. Explica brevemente el plan antes de implementar una tarea compleja.
3. Realiza cambios pequeños, verificables y relacionados con una sola tarea.
4. No modifiques ni elimines archivos en `data/raw/`.
5. No inventes datos, fechas, coordenadas, fuentes, escenas o etiquetas.
6. Mantén trazabilidad entre cada valor procesado y su fuente.
7. El CRS métrico maestro es EPSG:32718.
8. Conserva el nombre original de una quebrada y añade un identificador canónico.
9. Usa precisión espacial A-E; D y E no son etiquetas fuertes.
10. No utilices datos posteriores al momento de predicción.
11. No realices divisiones aleatorias que mezclen parches del mismo evento.
12. Mantén separados entrenamiento, validación y prueba por tiempo o quebrada.
13. Implementa primero modelos base; no añadas complejidad sin evidencia.
14. Todo módulo nuevo debe incluir pruebas.
15. Todo comando nuevo debe documentarse en README.md.
16. No añadas dependencias sin justificar su necesidad.
17. Evita notebooks como única implementación; la lógica debe vivir en `src/`.
18. Los notebooks se usan para exploración, validación y comunicación.
19. Usa archivos YAML para configuración y JSON para manifiestos.
20. Las tablas finales deben tener una versión CSV legible.
21. Las geometrías finales deben incluir GeoJSON o GeoPackage.
22. No automatices CAPTCHA, autenticación restringida ni descarga no autorizada.
23. No emitas alertas reales ni conclusiones operativas para la población.
24. No redactes afirmaciones científicas que no estén respaldadas por resultados.

## Herramientas y calidad

- Python 3.11 o superior.
- Formato y lint: Ruff.
- Pruebas: Pytest.
- Tipado: type hints en interfaces públicas.
- Validación de datos: Pydantic o equivalente.
- GIS: GeoPandas, Rasterio, Shapely, PyProj y herramientas compatibles.
- ML: Scikit-learn; PyTorch solo cuando esté justificado.
- Configuración reproducible y semillas aleatorias registradas.

## Comandos esperados

```bash
make setup
make lint
make test
make inventory
make gis
make features
make dataset
make train
make evaluate
make report
```

## Convenciones de datos

### Datos originales

`data/raw/` es inmutable.

### Datos intermedios

`data/interim/` puede regenerarse.

### Datos procesados

`data/processed/` contiene productos estandarizados y listos para análisis.

### Salidas

`outputs/` contiene tablas, figuras, mapas, modelos e informes.

## Requisitos de una tarea terminada

Una tarea se considera terminada únicamente cuando:

1. El código está implementado.
2. Las pruebas pasan.
3. El lint pasa.
4. La documentación está actualizada.
5. Las salidas esperadas fueron generadas.
6. Los supuestos y limitaciones quedaron registrados.
7. No se modificaron datos originales.
8. El resultado puede reproducirse mediante un comando documentado.

## Política científica

- Diferenciar evento observado, peligro potencial y obra de mitigación.
- Conservar incertidumbre documental y espacial.
- No tratar ausencia de reporte como ausencia de evento.
- No aceptar automáticamente coordenadas extraídas de texto.
- Exigir revisión humana para etiquetas y geometrías dudosas.
- Reportar tanto desempeño como calibración e incertidumbre.
- Comparar el modelo de fusión contra modelos de una sola fuente.
