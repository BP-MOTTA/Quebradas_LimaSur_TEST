# INDECI portal notes

Inspection date: 2026-09-07.

Authoritative page:
https://portal.indeci.gob.pe/informe/reportes-preliminares-complementarios-emergencias/

The portal HTML is external, untrusted data. These notes record only structural
metadata needed by the collector; no complete HTML response or PDF is stored in
the repository.

## Filter contract

The result filter is a regular HTML form, not an observed AJAX endpoint:

- Method: `GET`.
- Action: the authoritative page above, rendered without the trailing slash.
- Title input: `name="title"`; free text.
- Event type select: `name="tipo_alerta"`; values are portal taxonomy slugs,
  such as `activacion-de-quebradas`.
- Year select: `name="anos_alertas"`; integer values observed from 2012 through
  2026 at inspection time.
- The type filter describes the event taxonomy, not the document class
  (`Reporte Complementario`, `Reporte Preliminar`, or `Informe de Emergencia`).

Live-smoke leaves `tipo_alerta` empty because the two golden cases belong to
different event wording and document classes. It uses narrow title/year pairs
and does not invent undocumented parameters.

## Result structure

The result count appears as `Alertas encontradas: N` in an `h4`. Cards are
contained in `section.list-news.alerts-archive`, with one `article` per result:

- The displayed title is the text of `h3`, normally containing an `a` link.
- `a.btn-download` exposes the PDF URL and label `DESCARGAR`.
- `a.lnk-view` exposes the detail URL and label `Ver más`.
- The collector records these URLs as metadata only and never requests them.
- Missing download or detail links are retained as `null` with explicit
  warnings rather than hiding or inventing values.

The visible title is not always sufficient for document identity. In the
observed 2019 golden card it omits report number 630, while the PDF filename
contains the number and compact date `03MAR2019`. Metadata extraction therefore
checks the displayed title and URL text, without opening the PDF.

## Pagination

The unfiltered page displayed 20 result cards and pagination links using paths
of the form `/page/N/`. The next link uses classes `next page-numbers`.
Live-smoke parses that link but configuration caps each query at one page and
the client validates the same HTTPS host, archive path, and known query keys
before any request.

## Empty and error behavior

A deliberately unmatched title/year query returned HTTP 200 with
`Alertas encontradas: 0` and an empty `section.list-news.alerts-archive`; there
was no separate no-results message. This is treated as a valid empty result.

The runtime reports HTTP 404, terminal HTTP 429, timeout, empty responses,
unexpected content types, invalid UTF-8, and unrecognized HTML in `errors`.
HTTP 429 and selected server errors are retried sequentially with bounded
backoff; redirects are refused.

## Controlled observation

The inspection query `title=Chaclacayo`, empty `tipo_alerta`, and
`anos_alertas=2019` returned three cards and exposed the 2019 golden report.
This observation establishes discoverability only; it is not a scientific
event label, and no PDF was requested.

## Live-smoke result

The single approved live-smoke run on 2026-09-07 completed four sequential
single-page requests with no HTTP or parsing errors:

- Pages requested: 4.
- Documents seen before deduplication: 9.
- `golden_2019_found=true`: Reporte Complementario 630, dated 2019-03-03,
  was identified from the displayed card and PDF URL metadata.
- `golden_2023_found=false`: no candidate for Informe de Emergencia 1496,
  dated 2023-05-05, appeared in the configured archive queries.

The candidate set and request totals indicate that the `1496`/2023 query did
not add a result. The inspected navigation exposes a separate archive at
`/informe/informe-de-emergencia/`, so the best-supported diagnosis is that this
golden case is indexed under a different archive rather than hidden behind
pagination in the configured report archive. This is an inference from the
observed archive structure and bounded result set. No follow-up request,
alternate-archive query, or broad crawl was performed.
