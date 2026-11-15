"""Scaffold smoke test: the repository layout and toolchain are in place.

Replaced/extended by real domain tests from FASE 2 onward.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DIRS = [
    "apps/api",
    "apps/market_collector",
    "apps/research_worker",
    "apps/risk_engine",
    "apps/execution_gateway",
    "apps/journal_service",
    "apps/portfolio_ledger",
    "apps/alerting",
    "packages/aegis_core",
    "packages/aegis_exchange",
    "packages/aegis_strategy",
    "packages/aegis_backtest",
    "packages/aegis_risk",
    "packages/aegis_metrics",
    "packages/aegis_db",
    "packages/aegis_config",
    "packages/aegis_observability",
    "configs",
    "data/migrations",
    "data/seeds",
    "infra/compose",
    "infra/prometheus",
    "infra/grafana",
    "tests/unit",
    "tests/integration",
    "tests/fixtures",
    "docs/architecture",
    "docs/adr",
    "docs/runbooks",
    "scripts",
]

REQUIRED_FILES = [
    "pyproject.toml",
    "Makefile",
    ".env.example",
    "README.md",
    "LICENSE",
    ".github/workflows/ci.yml",
    ".gitignore",
]


def test_repository_layout_exists() -> None:
    missing = [d for d in REQUIRED_DIRS if not (ROOT / d).is_dir()]
    assert not missing, f"missing directories: {missing}"

    missing_files = [f for f in REQUIRED_FILES if not (ROOT / f).is_file()]
    assert not missing_files, f"missing files: {missing_files}"


def test_pyproject_targets_python_312_and_pydantic_v2() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    assert project["requires-python"].startswith(">=3.12")
    deps = project["dependencies"]
    assert any(d.startswith("pydantic>=") for d in deps)
    assert not any(d.startswith("pydantic<2") for d in deps)


def test_env_example_contains_no_real_looking_secrets() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    placeholders = {"", "change-me", "false", "true", '""', "0"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        _, value = stripped.split("=", 1)
        assert value.strip() not in {"sk_live", "AKIA"}, f"non-placeholder secret: {stripped}"
        # Every credential-ish key must stay empty until the operator fills it in.
        key = stripped.split("=", 1)[0]
        if key.endswith(("_KEY", "_SECRET", "_TOKEN", "_WEBHOOK_URL", "CHAT_ID", "_PASSWORD")):
            assert value.strip() in placeholders or value.strip() == "change-me", (
                f"{key} must be a placeholder in .env.example"
            )
