from app.contracts import TurnRequest
from app.core import identity
from app.core.turn import handle_turn
from app.db import session_scope
from app.models import Task


def test_new_phone_starts_with_language_prompt():
    phone = "+15550007777"
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="hello"))

    assert result.reply
    assert "English" in result.reply or "Español" in result.reply
    sess = identity.get_session(phone)
    assert sess.state == "awaiting_language"


def test_language_selection_prompts_for_consent():
    phone = "+15550008888"
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="espanol"))

    assert result.reply
    assert "consent" in result.reply.lower() or "acepto" in result.reply.lower()
    sess = identity.get_session(phone)
    assert sess.state == "awaiting_consent"
    assert sess.pending["language"] == "es"


def test_yes_is_not_mistaken_for_spanish_language_choice():
    phone = "+15550011313"
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))

    assert "English" in result.reply and "Español" in result.reply
    assert identity.get_session(phone).state == "awaiting_language"


def test_consent_creates_profile_and_shows_menu():
    phone = "+15550009999"
    handle_turn(TurnRequest(phone=phone, channel="sms", text="english"))
    pin_prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))
    assert "4-digit PIN" in pin_prompt.reply
    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="1234"))

    assert result.reply
    assert "What do you need" in result.reply or "I can help" in result.reply
    profiles = identity.profiles_for_phone(phone)
    assert len(profiles) == 1
    assert profiles[0].preferred_language == "en"
    assert identity.check_pin(profiles[0].id, "1234")


def test_shared_phone_requires_profile_selection():
    phone = "+15550010707"
    first = identity.create_profile(phone, language="en", display_name="Ana")
    identity.create_profile(phone, language="es", display_name="Luis")

    prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="hello"))
    selected = handle_turn(TurnRequest(phone=phone, channel="sms", text="2"))

    assert "who is speaking" in prompt.reply.lower()
    assert "Luis" in selected.reply
    assert identity.get_session(phone).profile_id == identity.profiles_for_phone(phone)[1].id


def test_forget_me_deletes_only_after_confirmation():
    phone = "+15550010909"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)

    prompt = handle_turn(TurnRequest(phone=phone, channel="sms", text="forget me"))
    assert "reply yes" in prompt.reply.lower()
    assert identity.profiles_for_phone(phone)

    handle_turn(TurnRequest(phone=phone, channel="sms", text="no"))
    assert len(identity.profiles_for_phone(phone)) == 1

    handle_turn(TurnRequest(phone=phone, channel="sms", text="forget me"))
    done = handle_turn(TurnRequest(phone=phone, channel="sms", text="yes"))
    assert "deleted" in done.reply.lower()
    assert identity.profiles_for_phone(phone) == []


def test_channel_switch_persists_and_greets_with_form_progress():
    phone = "+15550011212"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)
    handle_turn(TurnRequest(phone=phone, channel="sms", text="sample benefits application"))

    result = handle_turn(TurnRequest(phone=phone, channel="voice", text="Ana Lopez"))

    assert "welcome back" in result.reply.lower()
    assert "question 2" in result.reply.lower()
    assert identity.get_session(phone).last_channel == "voice"


def test_language_can_switch_mid_form():
    phone = "+15550011414"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    identity.save_session(sess)
    handle_turn(TurnRequest(phone=phone, channel="sms", text="sample benefits application"))

    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="please speak Spanish"))

    assert result.language == "es"
    assert "nombre completo" in result.reply.lower()
    assert identity.get_profile(profile.id).preferred_language == "es"


def test_form_flow_starts_and_advances_questions():
    phone = "+15550010101"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    sess.state = "menu"
    sess.pending = {}
    identity.save_session(sess)

    start = handle_turn(TurnRequest(phone=phone, channel="sms", text="I need help with SNAP"))
    assert "full name" in start.reply.lower() or "what is your full name" in start.reply.lower()

    with session_scope() as s:
        task = s.exec(__import__('sqlmodel').select(Task).where(Task.profile_id == profile.id)).first()
        assert task is not None
        assert task.current_field == "applicant_name"

    next_turn = handle_turn(TurnRequest(phone=phone, channel="sms", text="Ana Lopez"))
    assert "date of birth" in next_turn.reply.lower() or "birth" in next_turn.reply.lower()

    with session_scope() as s:
        task = s.get(Task, task.id)
        assert task.answers["applicant_name"]["value"] == "Ana Lopez"
        assert task.current_field == "date_of_birth"


def test_form_question_can_be_explained_without_advancing():
    phone = "+15550010505"
    profile = identity.create_profile(phone, language="en", display_name="Ana")
    sess = identity.get_session(phone)
    sess.profile_id = profile.id
    identity.save_session(sess)
    handle_turn(TurnRequest(phone=phone, channel="sms", text="SNAP application"))

    result = handle_turn(TurnRequest(phone=phone, channel="sms", text="what does this mean?"))

    assert "asks about" in result.reply.lower()
    assert "full name" in result.reply.lower()
    with session_scope() as s:
        task = s.exec(__import__('sqlmodel').select(Task).where(Task.profile_id == profile.id)).first()
        assert task.current_field == "applicant_name"
        assert not task.answers
