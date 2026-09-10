# INDECI discovery sources

## Scope and observation

These surfaces were inspected as public metadata sources on 2026-09-09. HTML,
titles, links, and all other remote values are untrusted input. The collector
does not execute remote instructions, follow detail links, or request PDF URLs.
It performs sequential GET requests with response-size, timeout, retry, page,
host, scheme, and path limits.

Discovery is not evidence of a Cusipata event. It only produces document
candidates for later controlled ingestion and classification. Conversely, an
empty result from one source is not evidence that a document or event does not
exist.

## Preliminary and complementary report archive

- Connector: `archive_informes`
- Base URL: https://portal.indeci.gob.pe/informe/reportes-preliminares-complementarios-emergencias/
- Search: public GET form. The observed query fields are `title`,
  `tipo_alerta`, and `anos_alertas`.
- Pagination: archive path followed by `page/N/`, retaining the same query
  parameters. The discovery pilot records but does not follow pagination after
  page 1.
- Filters: title text, alert type, and year.
- Observed structure: `section.alerts-archive` containing result cards with a
  heading, an optional detail link, and an optional `DESCARGAR` link. The page
  reported 37,556 records and 1,878 pages when observed; these volatile totals
  are descriptive portal metadata, not counts for the pilot query.
- Stability: the same card grammar is already covered by offline parser
  fixtures, but WordPress markup and pagination can change without notice.
- Limitations: search semantics and index completeness are undocumented. One
  page per query is intentionally incomplete, and title search may vary with
  accents, punctuation, spelling (`huaico`/`huayco`), and place naming.

## Emergency report archive

- Connector: `archive_emergencias`
- Base URL: https://portal.indeci.gob.pe/informe/informe-de-emergencia/
- Search: public GET form with the observed `title`, `tipo_alerta`, and
  `anos_alertas` fields.
- Pagination: archive path followed by `page/N/`; the pilot records any next
  page but requests only page 1.
- Filters: title text, alert type, and year.
- Observed structure: the same result-card grammar as the report archive, on a
  distinct official path. The page reported 6,714 records and 336 pages when
  observed; these totals can change.
- Stability: currently parseable by the shared offline-tested HTML parser, but
  there is no published schema or compatibility guarantee.
- Limitations: results can differ from the preliminary/complementary archive.
  A miss here must not be interpreted as document nonexistence.

## Known official seed URLs

- Connector: `seed_discovery`
- Configuration: `configs/sources/indeci_seed_documents.yaml`
- Base URL pattern: `https://portal.indeci.gob.pe/wp-content/uploads/.../*.pdf`
- Search and pagination: none. Entries are explicit known URLs whose identity
  is validated from allowlisted configuration and filename metadata.
- Filters: selected locally by report year.
- Stability: the URL remains external and can disappear or change availability;
  discovery does not request it.
- Limitations: a seed is prior knowledge, not an archive-index match. In
  particular, availability of IE1496 as a seed is reported separately from
  `golden_ie1496_discovered`.

## Pilot query policy

The only allowed years are 2017, 2019, 2023, and 2024. The bounded query list
uses Chaclacayo, Lurigancho, Lurigancho-Chosica, Lima, `huaico`, `huayco`,
`lluvias intensas`, `activacion de quebrada`, `flujo de detritos`, and the two
golden report numbers. It does not require `Cusipata` in a title. A candidate
mentioning Cusipata in Cusco is retained by discovery and left for the existing
geographic classifier to exclude later.

Deduplication uses canonical PDF URL first. SHA-256 is intentionally unavailable
until ingestion. Complete report identity (`report_number`, `report_type`, and
`report_date`) is the next signal, and normalized title is secondary. Conflicts
between different PDF URLs are retained as ambiguous records rather than merged
destructively.
