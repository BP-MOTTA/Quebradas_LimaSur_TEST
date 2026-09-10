"""Metadata-only connector for explicitly configured official seed URLs."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from quebradas_limaeste.inventory.discovery_models import (
    ConnectorResult,
    DiscoveryCandidate,
)
from quebradas_limaeste.inventory.ingestion_models import SeedDocument


class SeedDiscoveryConnector:
    """Expose known seeds as a source distinct from archive discovery."""

    name = "seed_discovery"

    def __init__(self, seeds: tuple[SeedDocument, ...]) -> None:
        self.seeds = seeds

    def discover(
        self,
        *,
        years: tuple[int, ...],
        discovered_at_utc: datetime,
    ) -> ConnectorResult:
        candidates = []
        for seed in self.seeds:
            if seed.report_date.year not in years:
                continue
            filename = unquote(PurePosixPath(urlparse(seed.source_url).path).name)
            title = re.sub(r"[-_]+", " ", filename.removesuffix(".pdf"))
            candidates.append(
                DiscoveryCandidate.create(
                    source_connector=self.name,
                    title=title,
                    detail_url=None,
                    pdf_url=seed.source_url,
                    report_type=seed.report_type,
                    report_number=seed.report_number,
                    report_date=seed.report_date,
                    year=seed.report_date.year,
                    discovered_at_utc=discovered_at_utc,
                    query_context={
                        "mechanism": "known_official_seed",
                        "seed_document_id": seed.document_id,
                    },
                    raw_metadata={
                        "document_id": seed.document_id,
                        "original_filename": seed.original_filename,
                        "source_domain": seed.source_domain,
                    },
                )
            )
        return ConnectorResult(
            connector=self.name,
            candidates=tuple(candidates),
            requests=0,
            pages=0,
        )
