"""Migration machinery smoke: upgrade head, downgrade base, upgrade again.

Runs against SQLite for a portable, dependency-free check. CI additionally
runs the same commands against real PostgreSQL (migrations job).
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path}/migrate.db"
    monkeypatch.setenv("SOA_DATABASE_URL", url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config, url


def test_upgrade_downgrade_upgrade_cycle(alembic_config: tuple[Config, str]) -> None:
    config, url = alembic_config

    command.upgrade(config, "head")
    engine = create_engine(url)
    assert "alembic_version" in inspect(engine).get_table_names()

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with engine.connect() as conn:
        from sqlalchemy import text

        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0031"
    engine.dispose()
