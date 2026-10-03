from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import session_scope
from app.models import Activity, Profile, ProfileFact, Task


def test_health():
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/dashboard").status_code == 200


def test_models_round_trip():
    with session_scope() as s:
        p = Profile(phone="+15550001111", display_name="Ana", preferred_language="es")
        s.add(p)
        s.commit()
        s.add(ProfileFact(profile_id=p.id, key="address", value={"street": "412 Elm St"},
                          source_type="seed", freshness_days=180))
        s.add(Task(profile_id=p.id, kind="fill_form", answers={"name": {"value": "Ana", "source": "memory"}}))
        s.add(Activity(profile_id=p.id, kind="form_started", description="Started SNAP application"))
        s.commit()

    with session_scope() as s:
        fact = s.exec(select(ProfileFact)).one()
        assert fact.value == {"street": "412 Elm St"}
        task = s.exec(select(Task)).one()
        assert task.answers["name"]["source"] == "memory"
