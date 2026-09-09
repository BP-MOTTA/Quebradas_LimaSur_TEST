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

    def fake_execute(config_path, seeds_path, *, allowed_root):
        calls.append((config_path, seeds_path, allowed_root))
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

    def fake_execute(config_path, url, *, allowed_root):
        calls.append((config_path, url, allowed_root))
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
