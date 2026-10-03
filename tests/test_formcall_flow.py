"""Phone-only form filling (app/formcall): end-to-end conversations through handle_turn with a scripted model."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.core import identity
from app.db import session_scope
from app.formcall import email as email_mod
from app.formcall import language as lang_mod
from app.formcall import notices as notices_mod
from app.formcall import receipt as receipt_mod
from app.formcall import submit as submit_mod
from app.memory import profile as memory
from app.models import FormNotice, FormRun, Message, Receipt, Task
from app.pdf.verify import read_fields
from formcall_fakes import Caller, FakeEmail, FakeModel, FakeSubmission

PHONE = "+15550100001"


@pytest.fixture(autouse=True)
def phone_forms(monkeypatch):
    monkeypatch.setenv("FORMLINE_PHONE_FORMS", "true")
    monkeypatch.setenv("FORMLINE_VOICE_AUTODETECT", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    lang_mod._cache.clear()
    yield
    email_mod.set_provider(None)
    submit_mod.set_provider(None)
    lang_mod._cache.clear()


@pytest.fixture
def model(monkeypatch):
    return FakeModel(monkeypatch)


@pytest.fixture
def mail():
    fake = FakeEmail()
    email_mod.set_provider(fake)
    return fake


def task_row() -> Task:
    with session_scope() as s:
        return s.exec(select(Task).order_by(Task.id.desc())).first()


def run_row() -> FormRun:
    with session_scope() as s:
        return s.exec(select(FormRun).order_by(FormRun.task_id.desc())).first()


def onboard_english(c: Caller) -> str:
    c.say("I need help applying for SNAP benefits")
    c.say("yes")
    return c.say("4821")


# Answers for the English form, by text (typed answers need no read-back unless something was converted).
ENGLISH = ["Ana Lopez", "March 14, 1988", "412 Elm St, Springfield, IL 62704", "217-555-0104", "skip", "3", "yes",
           "Sunrise Diner", "300 a week", "yes", "$850"]


def through_questions(c: Caller) -> str:
    onboard_english(c)
    reply = ""
    for line in ENGLISH:
        reply = c.say(line)
    return reply


def through_notices(c: Caller) -> str:
    reply = through_questions(c)
    while "exact wording" in reply:
        reply = c.say("that's fine")
    return reply


def to_prepared(c: Caller) -> str:
    through_notices(c)
    c.say("that's correct")
    return c.say("yes")


# ---------------------------------------------------------------- the whole form


def test_english_form_completed_in_english(model, mail):
    c = Caller(PHONE, "sms")
    reply = to_prepared(c)
    assert "What is your full name?" in c.heard[2]  # the goal said before onboarding started the form
    assert "prepared and checked" in reply and "has not been sent" in reply
    task, run = task_row(), run_row()
    assert run.state == "prepared" and task.status == "completed" and task.verification["ok"]
    pdf = read_fields(task.output_pdf_path)
    assert pdf["applicant_name"] == "Ana Lopez" and pdf["date_of_birth"] == "03/14/1988"
    assert "1299" in pdf["monthly_income"].replace(",", "")  # converted from 300 a week, by code
    assert (run.review or {}).get("approved_by") == "explicit yes"
    assert all(lang == "en" for lang in c.languages)


def spanish_demo(c: Caller, model: FakeModel) -> list[str]:
    """The live-demo script: a Spanish caller on voice, start to finish."""
    model.interps["Me llamo Evan Doubek."] = dict(intent="answer", value="Evan Doubek", spelling_uncertain=True,
                                                  confidence=0.7, language="es")
    model.interps["Trescientos dólares por semana."] = dict(intent="answer", amount=300, period="week", value="1300",
                                                            confidence=0.9, language="es")
    model.interps["¿Por qué necesitan saber la renta?"] = dict(intent="explain", language="es",
                                                               question="¿Por qué necesitan saber la renta?")
    model.explanations["renta"] = dict(
        quote="We ask for your rent because housing costs can increase your monthly benefit",
        plain="Su renta puede aumentar su beneficio mensual.", uncertain=False, consequential=False)
    model.translations["We ask for your rent because housing costs can increase your monthly benefit"] = (
        "Le pedimos su renta porque el costo de la vivienda puede aumentar su beneficio mensual.")
    model.interps["En realidad son trescientos quince por semana, no trescientos."] = dict(
        intent="correction", target_field="monthly_income", amount=315, period="week", value="315",
        confidence=0.9, language="es")
    script = [("", None), ("Quiero solicitar beneficios de SNAP.", "es-US"), ("Sí, está bien.", "es-US"),
              ("4821", None), ("Me llamo Evan Doubek.", "es-US"), ("D, O, U, B, E, K.", None), ("Sí.", "es-US"),
              ("14 de marzo de 1988.", "es-US"), ("Sí.", "es-US"), ("412 Elm Street, Springfield, IL 62704", "en-US"),
              ("Sí.", "es-US"), ("217 555 0104", None), ("Sí.", "es-US"), ("6789", None), ("6789", None),
              ("Somos tres.", "es-US"), ("Sí.", "es-US"), ("Sunrise Diner.", "en-US"), ("Sí.", "es-US"),
              ("Trescientos dólares por semana.", "es-US"), ("Sí.", "es-US"),
              ("¿Por qué necesitan saber la renta?", "es-US"), ("Sí, sigamos.", "es-US"),
              ("Ochocientos cincuenta al mes.", "es-US"), ("Sí.", "es-US"),
              ("Sí, léalo.", "es-US"), ("Está bien, sigamos.", "es-US"), ("Está bien.", "es-US"),
              ("Está bien.", "es-US"), ("Está bien.", "es-US"),
              ("En realidad son trescientos quince por semana, no trescientos.", "es-US"), ("Sí.", "es-US"),
              ("Sí, está todo correcto.", "es-US"), ("Ajá.", "es-US"), ("Sí.", "es-US"), ("Sí.", "es-US"),
              ("evan punto doubek arroba gmail punto com", None), ("Sí.", "es-US")]
    return [c.say(text, hint) for text, hint in script]


def test_english_form_completed_entirely_in_spanish_with_the_live_demo_script(model, mail):
    c = Caller(PHONE, "voice")
    replies = spanish_demo(c, model)
    assert all(lang == "es" for lang in c.languages[1:])  # never switched to English, even for an English address
    assert "apellido" in replies[4]  # unsure of the surname: asked to spell it
    assert "D, O, U, B, E, K" in replies[5] and "Evan Doubek" in replies[5]
    assert "$1,299 al mes" in replies[19]  # weekly pay converted and explained before it's entered
    # The form's English words are read back translated, never as English to a Spanish speaker.
    assert 'El formulario dice, traducido del inglés: "Le pedimos su renta porque' in replies[21]
    assert "We ask for your rent" not in replies[21]
    assert "Necesito un sí o un no claro" in replies[33]  # "ajá" is not consent
    assert "No se ha enviado" in replies[34]
    assert "E, V, A, N, punto, D, O, U, B, E, K" in replies[36]
    task, run = task_row(), run_row()
    assert run.state == "prepared"
    pdf = read_fields(task.output_pdf_path)
    assert pdf["applicant_name"] == "Evan Doubek"  # proper name never translated
    assert pdf["employer"] == "Sunrise Diner"
    assert "1364" in pdf["monthly_income"].replace(",", "")  # the correction (315 a week)
    income = task.answers["monthly_income"]
    assert income["provenance"] == "corrected" and income["history"] and income["verification"] == "confirmed"
    assert income["language"] == "es"
    assert mail.sent and mail.sent[0].to == "evan.doubek@gmail.com"


# ---------------------------------------------------------------- language


def test_language_detected_automatically_without_a_menu(model):
    c = Caller(PHONE, "voice")
    assert "Hola, habla Formline" in c.say("")
    reply = c.say("Quiero solicitar beneficios de SNAP.", "es-US")
    assert "Puedo ayudarle con formularios" in reply and c.languages[-1] == "es"


def test_unclear_language_asks_in_both(model):
    c = Caller(PHONE, "voice")
    c.say("")
    reply = c.say("hmm")
    assert "English" in reply and "Español" in reply


def test_language_changed_midway(model):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    reply = c.say("Háblame en español, por favor")
    assert reply == "¿Cuál es su nombre completo?" and c.languages[-1] == "es"
    assert identity.profiles_for_phone(PHONE)[0].preferred_language == "es"
    reply = c.say("Can you explain that part in English?")
    assert reply == "What is your full name?" and c.languages[-1] == "en"


def test_proper_name_is_not_translated(model):
    model.interps["Me llamo Juan Pérez"] = dict(intent="answer", value="John Perez", language="es")  # model anglicized
    c = Caller(PHONE, "sms")
    onboard_english(c)
    c.say("Me llamo Juan Pérez")
    assert task_row().answers["applicant_name"]["value"] == "Juan Pérez"


# ---------------------------------------------------------------- verification and spelling


def test_ambiguous_name_triggers_spelling_and_letters_rebuild_it(model):
    model.interps["My name is Evan Doubek"] = dict(intent="answer", value="Evan Doubek", spelling_uncertain=True)
    c = Caller(PHONE, "voice")
    onboard_english(c)
    assert "Could you spell it" in c.say("My name is Evan Doubek")
    assert "last name" in c.heard[-1]
    reply = c.say("D as in David, O, U, B, E, K")
    assert reply.startswith("D, O, U, B, E, K. That's Evan Doubek")
    assert task_row().answers.get("applicant_name") is None  # not stored until confirmed
    c.say("yes")
    assert task_row().answers["applicant_name"]["value"] == "Evan Doubek"
    assert task_row().answers["applicant_name"]["verification"] == "confirmed"


def test_confident_name_on_a_call_is_read_back_spelled(model):
    model.interps["Ana Lopez"] = dict(intent="answer", value="Ana Lopez", confidence=0.95)
    c = Caller(PHONE, "voice")
    onboard_english(c)
    assert c.say("Ana Lopez") == "I heard Ana Lopez: A, N, A; L, O, P, E, Z. Is that right?"


def test_ordinary_confident_answer_is_not_read_back(model):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:5]:
        c.say(line)
    reply = c.say("3")
    assert reply == "Does anyone in your home have a job right now?"


def test_dollar_amount_normalization(model):
    c = Caller(PHONE, "sms")
    through_questions(c)
    assert task_row().answers["rent_amount"]["value"] == 850.0


def test_weekly_to_monthly_conversion_is_code_not_model(model):
    model.interps["300 a week"] = dict(intent="answer", value="1300", amount=300, period="week")  # model's math ignored
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:8]:
        c.say(line)
    reply = c.say("300 a week")
    assert "about $1,299 each month" in reply
    c.say("yes")
    entry = task_row().answers["monthly_income"]
    assert entry["value"] == 1299.0 and entry["normalized"] == "$300 each week"


def test_sensitive_digits_are_checked_twice_and_never_echoed(model):
    c = Caller(PHONE, "voice")
    onboard_english(c)
    task = task_row()
    with session_scope() as s:
        t = s.get(Task, task.id)
        t.current_field = "ssn_last4"
        s.add(t)
        s.commit()
    reply = c.say("6789")
    assert "6789" not in reply and "once more" in reply
    assert "didn't match" in c.say("1111")
    c.say("6789")
    reply = c.say("6789")
    entry = task_row().answers["ssn_last4"]
    assert entry["value"] == "6789" and entry["heard"] == "***-**-6789" and entry["verification"] == "confirmed"
    with session_scope() as s:
        lines = [m.text for m in s.exec(select(Message).where(Message.phone == PHONE)).all()]
    assert not any("6789" in line for line in lines)  # sensitive answers never reach the transcript
    assert not any("4821" in line for line in lines)  # nor the PIN


# ---------------------------------------------------------------- corrections, questions, navigation


def test_caller_corrects_an_earlier_answer(model):
    model.interps["Actually, it's 315 a week, not 300."] = dict(intent="correction", target_field="monthly_income",
                                                                value="315", amount=315, period="week")
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:10]:
        c.say(line)
    reply = c.say("Actually, it's 315 a week, not 300.")
    assert "about $1,364 each month" in reply
    reply = c.say("yes")
    assert reply == "How much is your rent each month?"  # back where we were
    entry = task_row().answers["monthly_income"]
    assert entry["value"] == 1364.0 and entry["source"] == "corrected"
    assert entry["history"][0]["value"] == "$1,299"


def test_caller_asks_what_a_question_means_then_resumes(model):
    model.interps["Does my roommate count?"] = dict(intent="explain", question="Does my roommate count?")
    model.explanations["roommate"] = dict(
        quote="A roommate who buys and cooks food separately is not part of your household",
        plain="Only if you buy and cook food together.", uncertain=False, consequential=True)
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:5]:
        c.say(line)
    reply = c.say("Does my roommate count?")
    assert 'The form says, "A roommate who buys and cooks food separately' in reply
    assert "In plain words: Only if you buy and cook food together." in reply
    assert "not legal advice" in reply and reply.endswith("Would you like me to continue with the application?")
    assert c.say("yes") == "Including you, how many people live in your home?"


def test_explanation_quote_that_isnt_in_the_form_is_not_presented_as_the_form(model):
    model.interps["What happens if I leave this blank?"] = dict(intent="explain", question="blank?")
    model.explanations["blank"] = dict(quote="Leaving this blank cancels your application", plain="Nothing bad.",
                                       uncertain=False, consequential=False)
    c = Caller(PHONE, "sms")
    onboard_english(c)
    reply = c.say("What happens if I leave this blank?")
    assert "The form says" not in reply and "doesn't say this directly" in reply


def test_caller_asks_what_formline_is_entering(model):
    model.interps["What are you putting down for my income?"] = dict(intent="what_entered",
                                                                     target_field="monthly_income")
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:10]:
        c.say(line)
    reply = c.say("What are you putting down for my income?")
    assert reply == "For monthly income from work, I'm putting down $1,299. How much is your rent each month?"


def test_go_back_and_skip_and_start_over(model):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    c.say("Ana Lopez")
    assert "You said Ana Lopez" in c.say("go back")
    c.say("Ana Maria Lopez")
    assert task_row().answers["applicant_name"]["value"] == "Ana Maria Lopez"
    assert "start this application over" in c.say("start over")
    assert c.say("yes").endswith("What is your full name?")
    assert task_row().answers == {}


def test_stop_pauses_and_continue_resumes(model):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    assert "I stopped" in c.say("stop")
    assert run_row().state == "paused"
    assert c.say("continue") == "What is your full name?"


# ---------------------------------------------------------------- important information


def test_important_notices_are_presented_with_verified_quotes(model):
    c = Caller(PHONE, "sms")
    reply = through_questions(c)
    assert reply.startswith("I want to point something out before you agree to this.")  # the unusual one first
    assert "malicious" not in reply.lower()
    reply = c.say("yes")
    assert 'Here is the exact wording: "may also be shared with Riverbend Partner Network members' in reply
    c.say("I don't agree to that")
    with session_scope() as s:
        rows = {r.category: r for r in s.exec(select(FormNotice)).all()}
    assert rows["data_sharing"].status == "disagreed" and rows["data_sharing"].page == 2
    assert rows["deadline"].status == "pending"  # low severity: listed, not read out


def test_document_quotes_are_translated_for_the_caller(model):
    quote = "may also be shared with Riverbend Partner Network members"
    model.translations[quote] = "también puede compartirse con los miembros de Riverbend Partner Network"
    spoken = lang_mod.quote("es", "notice_read", quote)
    assert spoken == ('El texto, traducido del inglés, dice: "también puede compartirse con los miembros de '
                      'Riverbend Partner Network".')
    assert lang_mod.quote("en", "notice_read", quote) == f'Here is the exact wording: "{quote}".'


def test_document_quote_says_it_is_english_when_it_cant_be_translated(model):
    spoken = lang_mod.quote("es", "doc_says", "We ask for your rent")  # the model returned it unchanged
    assert spoken == 'El formulario dice, en inglés: "We ask for your rent".'


def test_fake_notice_quotes_are_dropped():
    from app.formcall.notices import Finding
    from app.formcall import doctext

    real = Finding(category="penalty", severity="high", unusual=False, quote="you will have to pay them back",
                   explanation="x", reason="y")
    fake = Finding(category="fee", severity="high", unusual=True, quote="a $50 processing fee is charged monthly",
                   explanation="x", reason="y")
    kept = notices_mod.verify([real, fake], doctext.pages("demo_snap"))
    assert [f.quote for f in kept] == ["you will have to pay them back"]


# ---------------------------------------------------------------- final review and consent


def test_required_field_missing_prevents_completion(model):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    c.say("skip")  # the name is required
    for line in ENGLISH[1:]:
        c.say(line)
    reply = c.say("that's fine")
    while "exact wording" in reply:
        reply = c.say("that's fine")
    assert "Before we finish, I still need full name" in reply
    assert run_row().state == "in_progress"


def test_ambiguous_final_consent_does_not_prepare(model):
    c = Caller(PHONE, "sms")
    through_notices(c)
    c.say("that's correct")
    for unclear in ("uh-huh", "I guess", "hmm", "yes but change the rent"):
        reply = c.say(unclear)
        assert task_row().output_pdf_path is None and run_row().state != "prepared", unclear
    assert "change" in reply.lower() or "yes or no" in reply.lower()


def test_explicit_yes_runs_the_stored_final_action(model):
    c = Caller(PHONE, "sms")
    through_notices(c)
    assert "Should I prepare your application now?" in c.say("that's correct")
    c.say("yes")
    assert run_row().state == "prepared" and task_row().output_pdf_path


def test_changed_answers_after_the_question_invalidate_the_yes(model):
    c = Caller(PHONE, "sms")
    through_notices(c)
    c.say("that's correct")
    task = task_row()
    with session_scope() as s:  # an answer changes between the question and the yes
        t = s.get(Task, task.id)
        t.answers = {**t.answers, "rent_amount": {**t.answers["rent_amount"], "value": 900.0}}
        s.add(t)
        s.commit()
    reply = c.say("yes")
    assert "Something changed since you said yes" in reply
    assert task_row().output_pdf_path is None and run_row().state == "ready_for_review"


# ---------------------------------------------------------------- receipts and submission states


def test_receipt_has_entered_values_masked_secrets_and_no_pin(model, mail):
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:4]:
        c.say(line)
    c.say("6789")  # SSN last four, typed
    for line in ENGLISH[5:]:
        c.say(line)
    reply = c.say("that's fine")
    while "exact wording" in reply:
        reply = c.say("that's fine")
    c.say("that's correct")
    c.say("yes")
    c.say("yes")
    assert "I'll spell that back" in c.say("ana.lopez@example.com")
    c.say("yes")
    sent = mail.sent[0]
    body = sent.text + sent.html
    assert "Ana Lopez" in body and "$1,299" in body and "$850" in body
    assert "***-**-6789" in body and "6789" not in body.replace("***-**-6789", "")
    assert "4821" not in body  # never the PIN
    assert "Prepared, not submitted" in body and "has NOT been sent" in body
    assert sent.attachments and sent.attachments[0][2] == "application/pdf"
    with session_scope() as s:
        row = s.exec(select(Receipt)).one()
    assert row.status == "sent" and row.to_address == "ana.lopez@example.com"


def test_outbox_says_saved_not_emailed(model):
    c = Caller(PHONE, "sms")
    to_prepared(c)
    c.say("yes")
    c.say("ana.lopez@example.com")
    reply = c.say("yes")
    assert "saved" in reply and "emailed you" not in reply


def test_email_failure_is_not_reported_as_delivered(model):
    email_mod.set_provider(FakeEmail(ok=False))
    c = Caller(PHONE, "sms")
    to_prepared(c)
    c.say("yes")
    c.say("ana.lopez@example.com")
    reply = c.say("yes")
    assert "couldn't send the email" in reply
    with session_scope() as s:
        assert s.exec(select(Receipt)).one().status == "failed"


@pytest.mark.parametrize("result, state, phrase, receipt_title", [
    (submit_mod.SubmissionResult(ok=True, verified=True, reference="RB-77"), "submitted", "RB-77", "Submitted"),
    (submit_mod.SubmissionResult(ok=False, verified=False, error="down"), "submission_failed", "couldn't submit",
     "Not submitted"),
    (submit_mod.SubmissionResult(ok=True, verified=False), "submission_unverified", "couldn't verify",
     "Submission not confirmed"),
])
def test_submission_states_are_only_what_was_verified(model, mail, result, state, phrase, receipt_title):
    submit_mod.set_provider(FakeSubmission(result))
    c = Caller(PHONE, "sms")
    through_notices(c)
    assert "Should I submit your application now?" in c.say("that's correct")
    reply = c.say("yes")
    assert phrase in reply and run_row().state == state
    c.say("yes")
    c.say("ana.lopez@example.com")
    c.say("yes")
    assert receipt_title in mail.sent[0].text


def test_receipt_for_a_prepared_form_says_prepared():
    from app.contracts import FormSchema

    schema = FormSchema.model_validate({"form_id": "x", "name": "X", "fields": []})
    task = Task(id=7, profile_id=1, kind="fill_form", form_id="x", answers={}, started_at=datetime.now(timezone.utc))
    out = receipt_mod.build(task=task, run=FormRun(task_id=7, profile_id=1, state="prepared"), schema=schema, notices=[])
    assert "Prepared, not submitted" in out.text and "Submitted" not in out.text.replace("not submitted", "")


# ---------------------------------------------------------------- memory and authorization


def _returning(monkeypatch_model=None):
    p = identity.create_profile(PHONE, language="en", display_name="Ana")
    identity.set_pin(p.id, "4821")
    memory.set_fact(p.id, "name", {"first": "Ana", "middle": "", "last": "Lopez"}, source_type="form")
    memory.set_fact(p.id, "date_of_birth", "1988-03-14", source_type="form")
    memory.set_fact(p.id, "monthly_income", 1100, source_type="form",
                    confirmed_at=datetime.now(timezone.utc) - timedelta(days=60))
    return p


def test_saved_information_needs_the_pin_and_then_is_offered(model):
    _returning()
    c = Caller(PHONE, "sms")
    reply = c.say("I need help applying for SNAP benefits")
    assert "say or key in your 4-digit PIN" in reply
    assert "1 more" not in c.say("0000") and "didn't match" in c.heard[-1]
    reply = c.say("4821")
    assert "I have full name and date of birth from last time. Should I use them?" in reply
    reply = c.say("yes")
    answers = task_row().answers
    assert answers["applicant_name"]["provenance"] == "memory" and answers["date_of_birth"]["value"] == "1988-03-14"
    assert reply == "What is your home address, including the city and ZIP code?"


def test_stale_value_is_reasked_with_the_old_value_as_a_hint(model):
    _returning()
    c = Caller(PHONE, "sms")
    c.say("I need help applying for SNAP benefits")
    c.say("4821")
    c.say("yes")
    for line in ["412 Elm St, Springfield, IL 62704", "217-555-0104", "skip", "3", "yes", "Sunrise Diner"]:
        reply = c.say(line)
    assert "Last time you said $1,100. Is that still right?" in reply


def test_switching_person_on_a_shared_phone_needs_the_pin_again(model):
    james = identity.create_profile(PHONE, display_name="James")
    denise = identity.create_profile(PHONE, display_name="Denise")
    identity.set_pin(james.id, "1111")
    identity.set_pin(denise.id, "2222")
    memory.set_fact(denise.id, "date_of_birth", "1964-02-08", source_type="form")
    sess = identity.get_session(PHONE)
    identity.select_profile(sess, james.id)
    identity.verify_pin(identity.get_session(PHONE), "1111")
    identity.select_profile(identity.get_session(PHONE), denise.id)  # James's PIN doesn't carry over
    c = Caller(PHONE, "sms")
    assert "PIN" in c.say("I need help applying for SNAP benefits")


def test_forget_me_deletes_form_runs_notices_and_receipts(model, mail):
    c = Caller(PHONE, "sms")
    to_prepared(c)
    c.say("yes")
    c.say("ana.lopez@example.com")
    c.say("yes")
    pid = identity.profiles_for_phone(PHONE)[0].id
    memory.forget_profile(pid)
    with session_scope() as s:
        assert not s.exec(select(FormRun)).all()
        assert not s.exec(select(FormNotice)).all()
        assert not s.exec(select(Receipt)).all()


def test_a_business_name_is_spelled_whole_not_just_its_last_word(model):
    model.interps["Sunrise Diner"] = dict(intent="answer", value="Sunrise Diner", spelling_uncertain=True)
    c = Caller(PHONE, "voice")
    onboard_english(c)
    with session_scope() as s:
        t = s.get(Task, task_row().id)
        t.answers = {"employed": {"value": "yes", "source": "asked"}}
        t.current_field = "employer"
        s.add(t)
        s.commit()
    assert "employer exactly right" in c.say("Sunrise Diner")
    reply = c.say("S, U, N, R, I, S, E, space, D, I, N, E, R")
    assert "That's Sunrise Diner" in reply
    c.say("yes")
    assert task_row().answers["employer"]["value"] == "Sunrise Diner"
