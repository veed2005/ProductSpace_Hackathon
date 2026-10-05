from datetime import datetime, timedelta, timezone

import pytest

from app.core import identity
from app.engines import form_library
from app.memory import profile as memory


@pytest.fixture
def pid():
    return identity.create_profile("+12025550199").id


@pytest.mark.parametrize("path", [
    "date_of_birth", "full_name", "name.first", "address", "address.zip", "household_members",
    "household_members[2].first_name", "employment.pay_frequency", "case_numbers.snap", "ssn_last4",
])
def test_valid_paths(path):
    assert memory.validate_profile_key(path) is None


@pytest.mark.parametrize("path", [
    "favorite_color", "address.country", "household_members.first_name", "date_of_birth.year",
    "full_name.first", "employment[0].employer", "address..city", "",
])
def test_invalid_paths(path):
    assert memory.validate_profile_key(path) is not None


def test_every_form_schema_uses_canonical_profile_keys():
    """Form schemas must map to keys memory understands."""
    for meta in form_library.list_forms():
        for field in form_library.load_schema(meta.form_id).fields:
            if field.profile_key:
                problem = memory.validate_profile_key(field.profile_key)
                assert problem is None, f"{meta.form_id}.{field.id}: {problem}"


def test_nested_set_and_get(pid):
    memory.set_value(pid, "address.city", "Springfield", source_type="conversation")
    memory.set_value(pid, "address.zip", "62704", source_type="conversation")
    memory.set_value(pid, "household_members[1].first_name", "Sofia", source_type="form")
    assert memory.get_fact(pid, "address").value == {"city": "Springfield", "zip": "62704"}
    members = memory.get_fact(pid, "household_members").value
    assert members == [{}, {"first_name": "Sofia"}]
    assert memory.get_value(pid, "household_members[1].first_name").value == "Sofia"
    assert memory.get_value(pid, "household_members[0].first_name") is None
    assert memory.get_value(pid, "household_members[5].first_name") is None


def test_full_name_is_virtual(pid):
    memory.set_value(pid, "full_name", "Ana Maria de la Cruz", source_type="conversation")
    assert memory.get_fact(pid, "name").value == {"first": "Ana", "middle": "Maria de la", "last": "Cruz"}
    assert memory.get_value(pid, "full_name").value == "Ana Maria de la Cruz"
    assert memory.get_value(pid, "name.last").value == "Cruz"


def test_set_value_rejects_unknown_keys(pid):
    with pytest.raises(ValueError):
        memory.set_value(pid, "favorite_color", "blue", source_type="conversation")


def test_freshness_and_confirm(pid):
    old = datetime.now(timezone.utc) - timedelta(days=45)
    memory.set_fact(pid, "monthly_income", 1300, source_type="form", confirmed_at=old)
    memory.set_fact(pid, "date_of_birth", "1988-03-14", source_type="form", confirmed_at=old)
    memory.set_fact(pid, "address", {"city": "Springfield"}, source_type="form", confirmed_at=old)
    assert not memory.get_value(pid, "monthly_income").fresh  # 30-day policy
    assert memory.get_value(pid, "date_of_birth").fresh  # never stale
    assert memory.get_value(pid, "address.city").fresh  # 180-day policy
    memory.confirm_fact(pid, "monthly_income")
    assert memory.get_value(pid, "monthly_income").fresh


def test_sensitive_keys_are_flagged(pid):
    memory.set_value(pid, "ssn_last4", "6789", source_type="conversation")
    assert memory.get_value(pid, "ssn_last4").sensitive
    assert "6789" not in memory.mask("6789")


def test_format_value():
    addr = {"street": "412 Elm St", "apt": "2B", "city": "Springfield", "state": "IL", "zip": "62704"}
    assert memory.format_value("address", addr) == "412 Elm St, Apt 2B, Springfield, IL 62704"
    assert memory.format_value("name", {"first": "Ana", "middle": "", "last": "Lopez"}) == "Ana Lopez"
    assert memory.format_value("disability_in_household", False) == "No"
    assert memory.format_value("monthly_income", 1300.0) == "1300"
