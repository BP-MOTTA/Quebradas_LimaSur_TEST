from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.ingestion_models import (
    derive_seed_document,
    load_ingestion_policy,
)
from quebradas_limaeste.inventory.pdf_download import (
    DownloadError,
    TransportResult,
    download_seed_pdf,
)

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
GOLDEN_URL = (
    "https://portal.indeci.gob.pe/wp-content/uploads/2023/05/"
    "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-"
    "LLUVIAS-INTENSAS-EN-EL-DEPARTAMENTO-DE-LIMA-36-DEE.pdf"
)
PDF_BYTES = b"%PDF-1.4\nsynthetic offline fixture\n%%EOF\n"
NOW = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)


class StubDownloadTransport:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.destinations = []
        self.calls = []

    def download(
        self,
        url,
        destination,
        *,
        timeout_seconds,
        user_agent,
        max_bytes,
    ):
        self.calls.append(url)
        self.destinations.append(destination)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        result, body = outcome
        if body is not None:
            destination.write_bytes(body)
        return result


def make_seed_and_policy():
    policy = load_ingestion_policy(SOURCE_CONFIG)
    return derive_seed_document(GOLDEN_URL, policy=policy), policy


def response(*, status=200, content_type="application/pdf", final_url=GOLDEN_URL):
    return TransportResult(
        http_status=status,
        content_type=content_type,
        final_url=final_url,
    )


def test_download_uses_part_validates_signature_and_records_sha(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    transport = StubDownloadTransport(
        [(response(content_type="text/plain"), PDF_BYTES)]
    )

    result = download_seed_pdf(
        seed,
        policy=policy,
        raw_root=tmp_path / "raw",
        transport=transport,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert result.download_status == "downloaded"
    assert result.http_status == 200
    assert result.file_size == len(PDF_BYTES)
    assert result.sha256 == sha256(PDF_BYTES).hexdigest()
    assert result.local_path.read_bytes() == PDF_BYTES
    assert result.local_path.name == "INDECI_IE1496_20230505.pdf"
    assert transport.destinations[0].name.endswith(".pdf.part")
    assert not transport.destinations[0].exists()
    assert result.warnings == ("unexpected Content-Type: text/plain",)


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([(response(status=404), None)], "HTTP 404"),
        ([TimeoutError("timeout"), TimeoutError("timeout")], "timed out"),
        ([(response(), b"not a PDF")], "PDF signature"),
    ],
)
def test_download_rejects_http_timeout_and_non_pdf(
    tmp_path,
    outcomes,
    expected,
) -> None:
    seed, policy = make_seed_and_policy()
    transport = StubDownloadTransport(outcomes)

    with pytest.raises(DownloadError, match=expected):
        download_seed_pdf(
            seed,
            policy=policy,
            raw_root=tmp_path / "raw",
            transport=transport,
            sleep=lambda _: None,
            now=lambda: NOW,
        )

    assert list((tmp_path / "raw").rglob("*.part")) == []
    assert list((tmp_path / "raw").rglob("*.pdf")) == []


def test_download_rejects_final_url_outside_allowlist(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    transport = StubDownloadTransport(
        [
            (
                response(final_url="https://example.org/redirected.pdf"),
                PDF_BYTES,
            )
        ]
    )

    with pytest.raises(DownloadError, match="final URL"):
        download_seed_pdf(
            seed,
            policy=policy,
            raw_root=tmp_path / "raw",
            transport=transport,
            sleep=lambda _: None,
            now=lambda: NOW,
        )


def test_download_deduplicates_by_sha_without_creating_second_copy(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    existing_path = tmp_path / "raw" / "2022" / "existing.pdf"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(PDF_BYTES)
    existing_records = (
        {
            "document_id": "INDECI_IE0001_20220101",
            "source_url": "https://portal.indeci.gob.pe/wp-content/uploads/2022/01/existing.pdf",
            "sha256": sha256(PDF_BYTES).hexdigest(),
            "local_path": str(existing_path),
            "downloaded_at_utc": "2022-01-01T00:00:00+00:00",
        },
    )

    result = download_seed_pdf(
        seed,
        policy=policy,
        raw_root=tmp_path / "raw",
        existing_records=existing_records,
        transport=StubDownloadTransport([(response(), PDF_BYTES)]),
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert result.download_status == "duplicate"
    assert result.local_path == existing_path.resolve()
    assert not (tmp_path / "raw" / "2023" / f"{seed.document_id}.pdf").exists()


def test_download_never_overwrites_existing_document_id(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    target = tmp_path / "raw" / "2023" / f"{seed.document_id}.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(PDF_BYTES)
    transport = StubDownloadTransport([])

    result = download_seed_pdf(
        seed,
        policy=policy,
        raw_root=tmp_path / "raw",
        transport=transport,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert result.download_status == "duplicate"
    assert result.sha256 == sha256(PDF_BYTES).hexdigest()
    assert target.read_bytes() == PDF_BYTES
    assert transport.calls == []


def test_registered_duplicate_preserves_original_timestamp(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    target = tmp_path / "raw" / "2023" / f"{seed.document_id}.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(PDF_BYTES)
    original_timestamp = "2023-05-05T12:30:00+00:00"
    record = {
        "document_id": seed.document_id,
        "source_url": seed.source_url,
        "final_url": seed.source_url,
        "downloaded_at_utc": original_timestamp,
        "sha256": sha256(PDF_BYTES).hexdigest(),
        "local_path": str(target),
        "http_status": 200,
        "content_type": "application/pdf",
    }

    result = download_seed_pdf(
        seed,
        policy=policy,
        raw_root=tmp_path / "raw",
        existing_records=(record,),
        transport=StubDownloadTransport([]),
        now=lambda: NOW,
    )

    assert result.downloaded_at_utc.isoformat() == original_timestamp


def test_registered_duplicate_revalidates_final_url(tmp_path) -> None:
    seed, policy = make_seed_and_policy()
    target = tmp_path / "raw" / "2023" / f"{seed.document_id}.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(PDF_BYTES)
    record = {
        "document_id": seed.document_id,
        "source_url": seed.source_url,
        "final_url": "https://example.org/untrusted.pdf",
        "downloaded_at_utc": "2023-05-05T12:30:00+00:00",
        "sha256": sha256(PDF_BYTES).hexdigest(),
        "local_path": str(target),
        "http_status": 200,
        "content_type": "application/pdf",
    }

    with pytest.raises(DownloadError, match="final URL"):
        download_seed_pdf(
            seed,
            policy=policy,
            raw_root=tmp_path / "raw",
            existing_records=(record,),
            transport=StubDownloadTransport([]),
            now=lambda: NOW,
        )
