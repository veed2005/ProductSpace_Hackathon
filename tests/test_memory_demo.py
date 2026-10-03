from sqlmodel import select

from app.core import identity
from app.dashboard.demo import DemoPhones, reset_demo, seed_demo
from app.db import session_scope
from app.memory import profile as memory
from app.models import Event, Form, Message


def test_seed_creates_personas_and_is_repeatable():
    first = seed_demo()
    second = seed_demo()
    phones = DemoPhones()
    assert len(identity.profiles_for_phone(phones.returning)) == 1
    assert len(identity.profiles_for_phone(phones.shared)) == 2
    assert identity.profiles_for_phone(phones.new) == []
    assert first["returning"]["name"] == second["returning"]["name"] == "Maria Garcia"

    maria = identity.profiles_for_phone(phones.returning)[0]
    assert identity.check_pin(maria.id, "1234")
    assert memory.get_value(maria.id, "full_name").value == "Maria Elena Garcia"
    assert not memory.get_value(maria.id, "monthly_income").fresh  # stale on purpose
    assert memory.get_value(maria.id, "address").fresh


def test_seed_records_first_form_baseline():
    seed_demo()
    with session_scope() as s:
        completed = s.exec(select(Event).where(Event.type == "form_completed")).all()
        assert len(completed) == 1 and completed[0].data["fields_from_memory"] == 0
        assert s.get(Form, "sample_benefits") is not None


def test_seed_clears_the_new_phone():
    phones = DemoPhones()
    identity.create_profile(phones.new)
    from app.events import log_message
    log_message(phones.new, "in", "sms", "hello")
    seed_demo()
    assert identity.profiles_for_phone(phones.new) == []
    with session_scope() as s:
        assert s.exec(select(Message).where(Message.phone == phones.new)).all() == []


def test_reset_wipes_everything(tmp_path):
    seed_demo()
    identity.create_profile("+12025550177")
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "photo.jpg").write_bytes(b"x")
    reset_demo(seed=False)
    assert identity.profiles_for_phone("+12025550177") == []
    assert identity.profiles_for_phone(DemoPhones().returning) == []
    assert not (tmp_path / "media").exists()
