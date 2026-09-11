"""Safe, bounded downloader for explicitly configured INDECI PDF URLs."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quebradas_limaeste.inventory.ingestion_models import (
    IngestionConfigError,
    IngestionPolicy,
    SeedDocument,
    derive_seed_document,
    validate_pdf_source_url,
)

INGESTION_USER_AGENT = (
    "QuebradasLimaEste-A1-INDECI-PDFIngestion/0.1 "
    "(+research; explicit-seeds-only)"
)
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
PDF_SIGNATURE = b"%PDF-"
CHUNK_SIZE = 64 * 1024


class DownloadError(RuntimeError):
    """Raised when a PDF cannot be safely downloaded or retained."""


@dataclass(frozen=True)
class TransportResult:
    http_status: int
    content_type: str
    final_url: str


@dataclass(frozen=True)
class DownloadResult:
    document_id: str
    source_url: str
    final_url: str
    downloaded_at_utc: datetime
    sha256: str
    file_size: int
    original_filename: str
    local_path: Path
    http_status: int
    content_type: str
    download_status: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "source_url": self.source_url,
            "final_url": self.final_url,
            "downloaded_at_utc": self.downloaded_at_utc.isoformat(),
            "sha256": self.sha256,
            "file_size": self.file_size,
            "original_filename": self.original_filename,
            "local_path": str(self.local_path),
            "http_status": self.http_status,
            "content_type": self.content_type,
            "download_status": self.download_status,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _DownloadSource:
    document_id: str
    source_url: str
    original_filename: str
    storage_year: int


class PDFDownloadTransport(Protocol):
    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout_seconds: float,
        user_agent: str,
        max_bytes: int,
    ) -> TransportResult: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibPDFDownloadTransport:
    """Stream one HTTPS response to a new staging path without redirects."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout_seconds: float,
        user_agent: str,
        max_bytes: int,
    ) -> TransportResult:
        request = Request(
            url,
            headers={
                "Accept": "application/pdf,application/octet-stream;q=0.8",
                "User-Agent": user_agent,
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise OSError("PDF exceeds configured byte limit")
                    except ValueError:
                        pass
                size = 0
                with destination.open("xb") as output:
                    while chunk := response.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > max_bytes:
                            raise OSError("PDF exceeds configured byte limit")
                        output.write(chunk)
                return TransportResult(
                    http_status=response.status,
                    content_type=response.headers.get_content_type(),
                    final_url=response.geturl(),
                )
        except HTTPError as exc:
            content_type = (
                exc.headers.get_content_type() if exc.headers is not None else ""
            )
            return TransportResult(
                http_status=exc.code,
                content_type=content_type,
                final_url=exc.geturl(),
            )
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("PDF request timed out") from exc
            raise OSError(f"PDF request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TimeoutError("PDF request timed out") from exc


def download_seed_pdf(
    seed: SeedDocument,
    *,
    policy: IngestionPolicy,
    raw_root: Path,
    existing_records: Iterable[dict[str, object]] = (),
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DownloadResult:
    """Download one approved seed and atomically publish it after validation."""
    source = _DownloadSource(
        document_id=seed.document_id,
        source_url=seed.source_url,
        original_filename=seed.original_filename,
        storage_year=seed.report_date.year,
    )
    return _download_pdf(
        source,
        policy=policy,
        raw_root=raw_root,
        existing_records=existing_records,
        transport=transport,
        sleep=sleep,
        now=now,
        validate_final_url=lambda value: _validate_seed_final_url(
            value,
            seed=seed,
            policy=policy,
        ),
    )


def download_discovered_pdf(
    *,
    document_id: str,
    source_url: str,
    storage_year: int,
    policy: IngestionPolicy,
    raw_root: Path,
    existing_records: Iterable[dict[str, object]] = (),
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    preserve_original_filename: bool = False,
) -> DownloadResult:
    """Download one selected discovery URL without inventing report metadata."""
    if re.fullmatch(r"[A-Z0-9_]{1,120}", document_id) is None:
        raise DownloadError("document_id has an invalid format")
    if (
        isinstance(storage_year, bool)
        or not isinstance(storage_year, int)
        or not 2010 <= storage_year <= 2100
    ):
        raise DownloadError("storage_year is outside the allowed range")
    try:
        normalized_url, filename = validate_pdf_source_url(source_url, policy=policy)
    except IngestionConfigError as exc:
        raise DownloadError(f"source URL rejected: {exc}") from exc
    source = _DownloadSource(
        document_id=document_id,
        source_url=normalized_url,
        original_filename=filename,
        storage_year=storage_year,
    )
    return _download_pdf(
        source,
        policy=policy,
        raw_root=raw_root,
        existing_records=existing_records,
        transport=transport,
        sleep=sleep,
        now=now,
        preserve_original_filename=preserve_original_filename,
        validate_final_url=lambda value: _validate_discovered_final_url(
            value,
            source=source,
            policy=policy,
        ),
    )


def _download_pdf(
    source: _DownloadSource,
    *,
    policy: IngestionPolicy,
    raw_root: Path,
    existing_records: Iterable[dict[str, object]],
    transport: PDFDownloadTransport | None,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
    validate_final_url: Callable[[str], None],
    preserve_original_filename: bool = False,
) -> DownloadResult:
    timestamp = now()
    if timestamp.tzinfo is None:
        raise DownloadError("download timestamp must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)
    records = tuple(existing_records)
    root, target = _target_path(
        raw_root,
        source,
        preserve_original_filename=preserve_original_filename,
    )

    prior = _find_prior_identity(source, records)
    if prior is not None:
        return _duplicate_from_registry(
            source,
            prior,
            root=root,
            validate_final_url=validate_final_url,
        )
    if target.exists():
        return _duplicate_from_existing_target(source, target, now=timestamp)

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(f"{target.suffix}.part")
    if part.exists():
        raise DownloadError(f"staging path already exists: {part}")

    active_transport = transport or UrllibPDFDownloadTransport()
    warnings: list[str] = []
    response: TransportResult | None = None
    try:
        for attempt in range(1, policy.max_attempts + 1):
            if attempt > 1:
                backoff = policy.retry_backoff_seconds * (2 ** (attempt - 2))
                sleep(max(policy.request_delay_seconds, backoff))
            try:
                response = active_transport.download(
                    source.source_url,
                    part,
                    timeout_seconds=policy.timeout_seconds,
                    user_agent=INGESTION_USER_AGENT,
                    max_bytes=policy.max_pdf_bytes,
                )
            except TimeoutError as exc:
                _remove_part(part)
                if attempt < policy.max_attempts:
                    warnings.append(
                        f"timeout on attempt {attempt}/{policy.max_attempts}; retrying"
                    )
                    continue
                raise DownloadError("PDF request timed out") from exc
            except OSError as exc:
                _remove_part(part)
                if attempt < policy.max_attempts:
                    warnings.append(
                        "network error on attempt "
                        f"{attempt}/{policy.max_attempts}; retrying"
                    )
                    continue
                raise DownloadError(f"PDF download failed: {exc}") from exc

            if response.http_status == 200:
                break
            _remove_part(part)
            if (
                response.http_status in TRANSIENT_STATUSES
                and attempt < policy.max_attempts
            ):
                warnings.append(
                    f"HTTP {response.http_status} on attempt "
                    f"{attempt}/{policy.max_attempts}; retrying"
                )
                response = None
                continue
            raise DownloadError(f"PDF request returned HTTP {response.http_status}")

        if response is None:
            raise DownloadError("PDF request did not return a response")
        if not part.is_file():
            raise DownloadError("PDF transport did not create the staging file")
        size = part.stat().st_size
        if size > policy.max_pdf_bytes:
            raise DownloadError("PDF exceeds configured byte limit")
        with part.open("rb") as downloaded_file:
            signature = downloaded_file.read(len(PDF_SIGNATURE))
        if signature != PDF_SIGNATURE:
            raise DownloadError("downloaded content has no PDF signature")

        validate_final_url(response.final_url)
        content_type = response.content_type.split(";", maxsplit=1)[0].strip().lower()
        if content_type and content_type != "application/pdf":
            warnings.append(f"unexpected Content-Type: {content_type}")
        digest = _file_sha256(part)

        duplicate_path = _find_sha_duplicate(digest, records, root=root)
        if duplicate_path is not None:
            return DownloadResult(
                document_id=source.document_id,
                source_url=source.source_url,
                final_url=response.final_url,
                downloaded_at_utc=timestamp,
                sha256=digest,
                file_size=size,
                original_filename=source.original_filename,
                local_path=duplicate_path,
                http_status=response.http_status,
                content_type=response.content_type,
                download_status="duplicate",
                warnings=tuple(warnings),
            )

        try:
            os.link(part, target)
        except FileExistsError as exc:
            if _file_sha256(target) != digest:
                raise DownloadError(
                    "target already exists with different content"
                ) from exc
            return _duplicate_from_existing_target(source, target, now=timestamp)
        return DownloadResult(
            document_id=source.document_id,
            source_url=source.source_url,
            final_url=response.final_url,
            downloaded_at_utc=timestamp,
            sha256=digest,
            file_size=size,
            original_filename=source.original_filename,
            local_path=target,
            http_status=response.http_status,
            content_type=response.content_type,
            download_status="downloaded",
            warnings=tuple(warnings),
        )
    finally:
        _remove_part(part)


def _target_path(
    raw_root: Path,
    source: _DownloadSource,
    *,
    preserve_original_filename: bool = False,
) -> tuple[Path, Path]:
    root = Path(raw_root).resolve()
    year_root = root / str(source.storage_year)
    filename = f"{source.document_id}.pdf"
    canonical_target = year_root / filename
    if (
        preserve_original_filename
        and not canonical_target.exists()
        and not canonical_target.is_symlink()
        and _portable_original_name(source.original_filename)
    ):
        original_target = year_root / source.original_filename
        if not original_target.exists() and not original_target.is_symlink():
            filename = source.original_filename
    target = (year_root / filename).resolve()
    if root not in target.parents:
        raise DownloadError("download path escapes the configured raw root")
    if target.is_symlink():
        raise DownloadError("download target symlinks are not allowed")
    return root, target


def _portable_original_name(value: str) -> bool:
    return (
        Path(value).name == value
        and not value.startswith(".")
        and value.lower().endswith(".pdf")
        and len(value.encode("utf-8")) <= 220
        and not any(ord(character) < 32 for character in value)
        and not set(value) & set('<>:"/\\|?*')
    )


def _validate_seed_final_url(
    final_url: str,
    *,
    seed: SeedDocument,
    policy: IngestionPolicy,
) -> None:
    try:
        final_seed = derive_seed_document(final_url, policy=policy)
    except IngestionConfigError as exc:
        raise DownloadError(f"final URL rejected: {exc}") from exc
    if final_seed.document_id != seed.document_id:
        raise DownloadError("final URL identifies a different document")


def _validate_discovered_final_url(
    final_url: str,
    *,
    source: _DownloadSource,
    policy: IngestionPolicy,
) -> None:
    try:
        normalized_url, _ = validate_pdf_source_url(final_url, policy=policy)
    except IngestionConfigError as exc:
        raise DownloadError(f"final URL rejected: {exc}") from exc
    if normalized_url != source.source_url:
        raise DownloadError("final URL identifies a different document source")


def _find_prior_identity(
    source: _DownloadSource,
    records: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    for record in records:
        same_id = record.get("document_id") == source.document_id
        same_url = record.get("source_url") == source.source_url
        if same_id != same_url:
            raise DownloadError("registry contains a conflicting URL or document_id")
        if same_id and same_url:
            return record
    return None


def _duplicate_from_registry(
    source: _DownloadSource,
    record: dict[str, object],
    *,
    root: Path,
    validate_final_url: Callable[[str], None],
) -> DownloadResult:
    path = _safe_registry_path(record.get("local_path"), root=root)
    digest = _file_sha256(path)
    if digest != record.get("sha256"):
        raise DownloadError("registry SHA-256 does not match its local file")
    downloaded_at = _registry_timestamp(record.get("downloaded_at_utc"))
    final_url = record.get("final_url")
    if not isinstance(final_url, str):
        raise DownloadError("registry final_url is invalid")
    validate_final_url(final_url)
    http_status = record.get("http_status")
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        raise DownloadError("registry http_status is invalid")
    if http_status not in {0, 200}:
        raise DownloadError("registry http_status is invalid")
    content_type = record.get("content_type")
    if not isinstance(content_type, str) or len(content_type) > 200:
        raise DownloadError("registry content_type is invalid")
    return DownloadResult(
        document_id=source.document_id,
        source_url=source.source_url,
        final_url=final_url,
        downloaded_at_utc=downloaded_at,
        sha256=digest,
        file_size=path.stat().st_size,
        original_filename=source.original_filename,
        local_path=path,
        http_status=http_status,
        content_type=content_type,
        download_status="duplicate",
        warnings=("document_id and source URL already exist",),
    )


def _duplicate_from_existing_target(
    source: _DownloadSource,
    target: Path,
    *,
    now: datetime,
) -> DownloadResult:
    with target.open("rb") as existing_file:
        if existing_file.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
            raise DownloadError("existing target has no PDF signature")
    return DownloadResult(
        document_id=source.document_id,
        source_url=source.source_url,
        final_url=source.source_url,
        downloaded_at_utc=now,
        sha256=_file_sha256(target),
        file_size=target.stat().st_size,
        original_filename=source.original_filename,
        local_path=target.resolve(),
        http_status=0,
        content_type="",
        download_status="duplicate",
        warnings=("document_id target already exists; no request made",),
    )


def _find_sha_duplicate(
    digest: str,
    records: tuple[dict[str, object], ...],
    *,
    root: Path,
) -> Path | None:
    for record in records:
        if record.get("sha256") == digest:
            path = _safe_registry_path(record.get("local_path"), root=root)
            if _file_sha256(path) != digest:
                raise DownloadError("registry SHA-256 does not match its local file")
            return path
    return None


def _safe_registry_path(value: object, *, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise DownloadError("registry local_path is invalid")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise DownloadError("registry local_path is outside the raw root or missing")
    return resolved


def _registry_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise DownloadError("registry downloaded_at_utc is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DownloadError("registry downloaded_at_utc is invalid") from exc
    if timestamp.tzinfo is None:
        raise DownloadError("registry downloaded_at_utc is not timezone-aware")
    return timestamp.astimezone(UTC)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_part(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()
