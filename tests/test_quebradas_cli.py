from quebradas import __main__ as quebradas_cli


def test_cli_blocks_live_smoke_inside_github_actions(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    exit_code = quebradas_cli.main(
        ["indeci", "live-smoke", "--config", "unused.yaml"]
    )

    assert exit_code == 2
    assert "disabled in GitHub Actions" in capsys.readouterr().err


def test_cli_blocks_discovery_inside_github_actions(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "discover",
            "--config",
            "unused.yaml",
            "--years",
            "2017,2019,2023,2024",
            "--dry-run",
        ]
    )

    assert exit_code == 2
    assert "disabled in GitHub Actions" in capsys.readouterr().err


def test_cli_selects_batch_offline_inside_github_actions(monkeypatch, capsys) -> None:
    calls = []

    def fake_execute(
        discovery_path,
        *,
        config_path,
        output_path,
        max_documents,
        allowed_root,
    ):
        calls.append(
            (
                discovery_path,
                config_path,
                output_path,
                max_documents,
                allowed_root,
            )
        )
        return {
            "batch_id": "indeci-batch-0123456789abcdef",
            "documents_available": 131,
            "documents_selected": 23,
            "selected_by_year": {"2017": 6, "2019": 4, "2023": 7, "2024": 6},
            "selected_by_tier": {"A": 2, "B": 3, "C": 10, "D": 8},
            "selection_output": "metadata/indeci/batch_selection.csv",
        }

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(quebradas_cli, "execute_select_batch", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "select-batch",
            "--discovery",
            "metadata/indeci/discovery_candidates.csv",
            "--max-documents",
            "25",
        ]
    )

    assert exit_code == 0
    assert calls[0][3] == 25
    output = capsys.readouterr().out
    assert "documents_selected=23" in output
    assert "year:2023=7" in output


def test_cli_blocks_batch_ingestion_inside_github_actions(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-batch",
            "--selection",
            "metadata/indeci/batch_selection.csv",
        ]
    )

    assert exit_code == 2
    assert "disabled in GitHub Actions" in capsys.readouterr().err


def test_cli_runs_controlled_batch_ingestion(monkeypatch, capsys) -> None:
    calls = []

    def fake_execute(
        selection_path,
        *,
        config_path,
        allowed_root,
        allow_large_batch,
    ):
        calls.append(
            (selection_path, config_path, allowed_root, allow_large_batch)
        )
        return batch_payload()

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(quebradas_cli, "execute_ingest_batch", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-batch",
            "--selection",
            "metadata/indeci/batch_selection.csv",
        ]
    )

    assert exit_code == 0
    assert calls[0][3] is False
    assert "documents_selected=4" in capsys.readouterr().out


def test_cli_prints_batch_summary_offline_inside_github_actions(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        quebradas_cli,
        "load_batch_manifest",
        lambda path, *, allowed_root: batch_payload(),
    )

    exit_code = quebradas_cli.main(["indeci", "batch-summary"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents\n---------\nselected: 4" in output
    assert "Event candidates\n----------------\nstrong: 1" in output
    assert "2019: 2" in output


def batch_payload():
    return {
        "documents_selected": 4,
        "documents_downloaded": 3,
        "documents_failed": 0,
        "duplicates": 1,
        "ocr_required": 0,
        "documents_relevant": 2,
        "documents_possible": 1,
        "documents_irrelevant": 1,
        "documents_excluded_geography": 1,
        "event_candidates": 2,
        "strong_candidates": 1,
        "moderate_candidates": 0,
        "weak_candidates": 1,
        "event_clusters": 2,
        "items_pending_review": 5,
        "documents_by_year": {"2017": 1, "2019": 2, "2023": 1, "2024": 0},
        "warnings": [],
        "errors": [],
    }


def test_cli_runs_explicit_discovery_dry_run(monkeypatch, capsys) -> None:
    calls = []

    def fake_execute(
        config_path,
        *,
        years,
        candidates_output,
        run_output,
        allowed_root,
    ):
        calls.append(
            (
                config_path,
                years,
                candidates_output,
                run_output,
                allowed_root,
            )
        )
        return {
            "requests": 4,
            "pages": 4,
            "candidates_raw": 3,
            "candidates_unique": 2,
            "duplicates": 1,
            "candidates_by_year": {"2019": 1, "2023": 1},
            "candidates_by_connector": {
                "archive_emergencias": 1,
                "archive_informes": 1,
                "seed_discovery": 1,
            },
            "golden_rc630_discovered": True,
            "golden_rc630_connectors": ["archive_informes"],
            "golden_ie1496_discovered": False,
            "golden_ie1496_connectors": [],
            "golden_ie1496_available_as_seed": True,
            "connectors_attempted": [
                {"connector": "archive_informes", "status": "success"}
            ],
            "candidates_output": "metadata/indeci/discovery_candidates.csv",
            "run_output": "metadata/indeci/discovery_run.json",
            "warnings": [],
            "errors": [],
        }

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(quebradas_cli, "execute_discovery", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "discover",
            "--config",
            "configs/sources/indeci_cusipata.yaml",
            "--years",
            "2017,2019,2023,2024",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls[0][1] == (2017, 2019, 2023, 2024)
    output = capsys.readouterr().out
    assert "requests\t4" in output
    assert "year:2019\t1" in output
    assert "connector:archive_informes\t1" in output
    assert "golden_rc630_discovered\ttrue" in output


def test_cli_discovery_requires_dry_run(capsys) -> None:
    exit_code = quebradas_cli.main(
        [
            "indeci",
            "discover",
            "--config",
            "unused.yaml",
            "--years",
            "2019",
        ]
    )

    assert exit_code == 2
    assert "--dry-run" in capsys.readouterr().err


def test_cli_runs_live_smoke_only_after_explicit_subcommand(
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_execute(config_path, *, output_path, allowed_root):
        calls.append((config_path, output_path, allowed_root))
        return {
            "golden_2019_found": True,
            "golden_2023_found": False,
            "pages_requested": 2,
            "documents_seen": 4,
            "queries_attempted": [
                {
                    "title": "Chaclacayo",
                    "year": 2019,
                    "requests_made": 2,
                    "pages_parsed": 2,
                }
            ],
            "warnings": [],
            "errors": [],
        }

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(quebradas_cli, "execute_live_smoke", fake_execute)

    exit_code = quebradas_cli.main(
        ["indeci", "live-smoke", "--config", "configs/indeci.yaml"]
    )

    assert exit_code == 0
    assert calls and str(calls[0][0]) == "configs/indeci.yaml"
    output = capsys.readouterr().out
    assert "golden_2019_found=true" in output
    assert "golden_2023_found=false" in output


def ingestion_payload():
    return {
        "documents_requested": 1,
        "documents_downloaded": 1,
        "duplicates": 0,
        "download_errors": 0,
        "documents_extracted": 1,
        "documents_classified": 1,
        "event_candidates": 2,
        "items_pending_review": 3,
        "documents": [],
        "warnings": [],
        "errors": [],
    }


def test_cli_runs_explicit_seed_ingestion(monkeypatch, capsys) -> None:
    calls = []

    def fake_execute(
        config_path,
        seeds_path,
        *,
        output_path,
        candidate_output,
        allowed_root,
    ):
        calls.append(
            (config_path, seeds_path, output_path, candidate_output, allowed_root)
        )
        return ingestion_payload()

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(quebradas_cli, "execute_ingest_seeds", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-seeds",
            "--config",
            "configs/indeci.yaml",
            "--seeds",
            "configs/seeds.yaml",
        ]
    )

    assert exit_code == 0
    assert str(calls[0][0]) == "configs/indeci.yaml"
    assert str(calls[0][1]) == "configs/seeds.yaml"
    output = capsys.readouterr().out
    assert "documents_downloaded=1" in output
    assert "event_candidates=2" in output


def test_cli_runs_explicit_url_ingestion(monkeypatch) -> None:
    calls = []

    def fake_execute(
        config_path,
        url,
        *,
        output_path,
        candidate_output,
        allowed_root,
    ):
        calls.append((config_path, url, output_path, candidate_output, allowed_root))
        return ingestion_payload()

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(quebradas_cli, "execute_ingest_url", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-url",
            "--config",
            "configs/indeci.yaml",
            "--url",
            "https://portal.indeci.gob.pe/wp-content/uploads/report.pdf",
        ]
    )

    assert exit_code == 0
    assert calls[0][1].startswith("https://portal.indeci.gob.pe/")


def test_cli_runs_local_file_ingestion_offline_inside_github_actions(
    monkeypatch,
) -> None:
    calls = []

    def fake_execute(
        config_path,
        file_path,
        *,
        source_url,
        output_path,
        candidate_output,
        allowed_root,
    ):
        calls.append(
            (
                config_path,
                file_path,
                source_url,
                output_path,
                candidate_output,
                allowed_root,
            )
        )
        return ingestion_payload()

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(quebradas_cli, "execute_ingest_file", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-file",
            "--config",
            "configs/indeci.yaml",
            "--file",
            "/tmp/REPORTE-COMPLEMENTARIO-Nº-630-03MAR2019.pdf",
        ]
    )

    assert exit_code == 0
    assert str(calls[0][1]).endswith("03MAR2019.pdf")
    assert calls[0][2] is None


def test_cli_blocks_seed_ingestion_inside_github_actions(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "ingest-seeds",
            "--config",
            "unused.yaml",
            "--seeds",
            "unused.yaml",
        ]
    )

    assert exit_code == 2
    assert "disabled in GitHub Actions" in capsys.readouterr().err


def test_cli_audits_candidates_offline_inside_github_actions(
    monkeypatch,
    capsys,
) -> None:
    calls = []

    def fake_execute(
        input_path,
        *,
        config_path,
        audit_output,
        consolidated_output,
        allowed_root,
    ):
        calls.append(
            (input_path, config_path, audit_output, consolidated_output, allowed_root)
        )
        return {
            "candidates_original": 1,
            "candidates_excluded": 0,
            "strong": 1,
            "moderate": 0,
            "weak": 0,
            "clusters_consolidated": 1,
            "pages_audited": [3],
            "pages_relevant": [3],
            "strong_cusipata": True,
            "audit_output": "metadata/indeci/audit.csv",
            "consolidated_output": "metadata/indeci/consolidated.csv",
            "audited_candidates": [
                {
                    "candidate_id": "indeci-candidate-test",
                    "source_page": 3,
                    "event_date": "2023-03-14",
                    "reported_quebrada": "Cusipata",
                    "event_type": "activacion_quebrada",
                    "evidence_snippet": "Synthetic\x1b[31m evidence.",
                    "matched_terms": ["Cusipata"],
                    "candidate_strength": "strong",
                    "review_reason": "site_event_location_date_same_sentence",
                }
            ],
        }

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(quebradas_cli, "execute_candidate_audit", fake_execute)

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "audit-candidates",
            "--input",
            "metadata/indeci/events_cusipata_candidates.csv",
        ]
    )

    assert exit_code == 0
    assert calls
    output = capsys.readouterr().out
    assert "candidate_id=indeci-candidate-test" in output
    assert "candidate_strength=strong" in output
    assert "strong_cusipata=true" in output
    assert "\x1b" not in output
    assert "\\x1b" in output


def test_cli_compares_golden_controls_offline(monkeypatch, capsys) -> None:
    calls = []

    def fake_execute(negative_audit, positive_audit, *, output_path, allowed_root):
        calls.append((negative_audit, positive_audit, output_path, allowed_root))
        return {
            "negative_control": {
                "document_id": "INDECI_IE1496_20230505",
                "strong": 0,
                "moderate": 0,
                "weak": 16,
            },
            "positive_control": {
                "document_id": "INDECI_RC630_20190303",
                "strong": 1,
                "moderate": 0,
                "weak": 0,
            },
        }

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        quebradas_cli,
        "execute_golden_control_comparison",
        fake_execute,
    )

    exit_code = quebradas_cli.main(
        [
            "indeci",
            "compare-golden-controls",
            "--negative-audit",
            "metadata/indeci/negative.csv",
            "--positive-audit",
            "metadata/indeci/positive.csv",
        ]
    )

    assert exit_code == 0
    assert calls
    output = capsys.readouterr().out
    assert (
        "negative_control=INDECI_IE1496_20230505,"
        "strong=0,moderate=0,weak=16" in output
    )
    assert (
        "positive_control=INDECI_RC630_20190303,"
        "strong=1,moderate=0,weak=0" in output
    )
