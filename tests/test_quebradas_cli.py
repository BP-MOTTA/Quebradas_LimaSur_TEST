from quebradas import __main__ as quebradas_cli


def test_cli_blocks_live_smoke_inside_github_actions(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    exit_code = quebradas_cli.main(
        ["indeci", "live-smoke", "--config", "unused.yaml"]
    )

    assert exit_code == 2
    assert "disabled in GitHub Actions" in capsys.readouterr().err


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
