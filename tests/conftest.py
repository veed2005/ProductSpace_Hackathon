import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app import config, db

    config.get_settings.cache_clear()
    db._engine = None
    db.init_db()
    yield
    db._engine = None
    config.get_settings.cache_clear()
