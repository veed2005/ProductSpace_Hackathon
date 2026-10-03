"""handle_turn: the single entry point both channels call. Owner: Lane A.

Contract (do not change the signature without telling Lane B):
    handle_turn(TurnRequest) -> TurnResult
"""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from time import perf_counter

from app.contracts import TurnRequest, TurnResult
from app.core import identity
from app.core.referrals import referral_for
from app.core.router import classify_intent
from app.core.style import style_for
from app.engines import document_engine, form_library
from app.engines.status_engine import recent_activity
from app.engines.form_engine import (
    answer_field,
    reply_key,
    memory_confirmation_text,
    process_memory_confirmation,
    process_readback,
    question_with_stale_hint,
    readback_text,
    start_form,
)
from app.events import log_activity, log_event, log_message, publish
from app.config import get_settings
from app.llm import client as llm
from app.models import Document, Form, Task
from app.db import session_scope
from sqlmodel import select

MENU = "I can help you fill out a form, or explain a letter or document you got. What do you need?"
MENU_ES = "Puedo ayudarte a llenar un formulario o explicar una carta o documento que recibiste. ¿Qué necesitas?"
LANGUAGE_PROMPT = "Welcome! To get started, what language do you prefer? Reply with English or Español."
LANGUAGE_PROMPT_ES = "¡Bienvenido! Para empezar, ¿qué idioma prefiere? Responda con English o Español."
CONSENT_PROMPT = "Thanks. I can help you with forms and letters. Do you agree to keep this conversation secure and use your information to help fill forms? Reply yes or no."
CONSENT_PROMPT_ES = "Gracias. Puedo ayudarle con formularios y cartas. ¿Acepto usar su información para ayudar a completar formularios y mantener esta conversación segura? Responda sí o no."
PIN_SETUP = "Please choose a 4-digit PIN. I’ll ask for it before reusing saved details."
PIN_SETUP_ES = "Elija un PIN de 4 dígitos. Se lo pediré antes de reutilizar datos guardados."


def handle_turn(req: TurnRequest) -> TurnResult:
    started = perf_counter()
    sess = identity.get_session(req.phone)
    switched = identity.note_channel(sess, req.channel)
    identity.save_session(sess)
    log_text = _sensitive_log_text(sess, req.text)
    log_message(req.phone, "in", req.channel, log_text, profile_id=sess.profile_id,
                task_id=sess.active_task_id, media=req.media_paths)

    result = _brain_turn(req, sess)

    log_event("turn", phone=req.phone, profile_id=sess.profile_id, channel=req.channel,
              latency_ms=round((perf_counter() - started) * 1000, 2))
    if switched:
        current = identity.get_session(req.phone)
        if current.state == "form" and current.active_task_id:
            with session_scope() as s:
                task = s.get(Task, current.active_task_id)
            if task and task.form_id:
                schema = form_library.load_schema(task.form_id)
                total = len(schema.fields)
                answered = len(task.answers or {})
                if result.language == "es":
                    result.reply = f"Bienvenido de nuevo. Íbamos en la pregunta {min(answered + 1, total)} de {total}. {result.reply}"
                else:
                    result.reply = f"Welcome back. We were on question {min(answered + 1, total)} of {total}. {result.reply}"

    log_message(req.phone, "out", req.channel, result.reply, profile_id=sess.profile_id,
                task_id=sess.active_task_id)
    return result


def _sensitive_log_text(sess, text: str) -> str:
    if sess.state == "awaiting_pin_setup":  # never store a PIN in the transcript
        return "[PIN]"
    if not sess.active_task_id:
        return text
    with session_scope() as s:
        task = s.get(Task, sess.active_task_id)
    if task is None or not task.form_id:
        return text
    schema = form_library.load_schema(task.form_id)
    if task.status == "readback" and any(field.sensitive for field in schema.fields) and any(c.isdigit() for c in text):
        return "[sensitive correction]"
    field = next((f for f in schema.fields if f.id == task.current_field and f.sensitive), None)
    return "[sensitive answer]" if field else text


def _brain_turn(req: TurnRequest, sess) -> TurnResult:
    profiles = identity.profiles_for_phone(req.phone)
    if sess.state == "awaiting_pin_setup":
        return _pin_setup_flow(req, sess)
    if sess.state == "awaiting_profile":
        return _profile_selection_flow(req, sess, profiles)
    if sess.state == "awaiting_forget_confirmation":
        return _forget_confirmation_flow(req, sess)
    if sess.state in {"awaiting_reminder_consent", "awaiting_related_form"}:
        return _document_followup_flow(req, sess)
    if sess.profile_id is None and len(profiles) == 1:
        sess.profile_id = profiles[0].id
        identity.save_session(sess)

    if sess.state == "awaiting_language":
        return _language_flow(req, sess)
    if sess.state == "awaiting_consent":
        return _consent_flow(req, sess)
    if sess.state == "form_memory_confirm" and sess.active_task_id:
        answer = reply_key(req.text)
        if answer in {"yes", "y", "sí", "si", "use them", "use it"}:
            accepted = True
        elif answer in {"no", "n", "nope", "don't use them", "do not use them"}:
            accepted = False
        else:
            selected = identity.get_profile(sess.profile_id) if sess.profile_id else None
            return TurnResult(reply="Please reply yes to use these details, or no to answer again.",
                              language=selected.preferred_language if selected else "en")
        task, stale_hints = process_memory_confirmation(sess.active_task_id, accepted)
        selected = identity.get_profile(sess.profile_id) if sess.profile_id else None
        language = selected.preferred_language if selected else "en"
        schema = form_library.load_schema(task.form_id)
        if task.status == "readback":
            return TurnResult(reply=readback_text(task, schema, language=language), language=language)
        return TurnResult(reply=question_with_stale_hint(task, schema, stale_hints, language=language),
                          language=language)

    if not profiles and sess.profile_id is None:
        return _new_phone_flow(req, sess)

    if len(profiles) > 1 and sess.profile_id is None:
        sess.state = "awaiting_profile"
        sess.pending = {}
        identity.save_session(sess)
        choices = " ".join(f"{i + 1}: {p.display_name or 'Profile'}" for i, p in enumerate(profiles))
        return TurnResult(reply=f"Who is speaking? Reply with a number: {choices}.", language="en")

    if sess.profile_id is None:
        sess.profile_id = profiles[0].id
        identity.save_session(sess)

    profile = identity.get_profile(sess.profile_id)
    name = profile.display_name if profile and profile.display_name else "friend"
    intent = classify_intent(req.text, has_media=bool(req.media_paths))
    if sess.state == "form" and sess.active_task_id:
        try:
            with session_scope() as s:
                active_task = s.get(Task, sess.active_task_id)
            language = profile.preferred_language if profile else "en"
            switched_language = _requested_language(req.text)
            if switched_language and switched_language != language and profile:
                identity.update_profile(profile.id, preferred_language=switched_language)
                language = switched_language
                schema = form_library.load_schema(active_task.form_id) if active_task else None
                active_field = next((f for f in schema.fields if f.id == active_task.current_field), None) if schema else None
                question = _localized_question(active_field, language, req.channel) if active_field else "¿En qué puedo ayudarte?" if language == "es" else "How can I help?"
                return TurnResult(reply=question, language=language)
            if active_task is not None and active_task.status == "readback":
                task, outcome = process_readback(sess.active_task_id, req.text)
                if outcome == "confirmed":
                    return _completion_result(task, language, req.phone)
                if outcome == "clarify":
                    prompt = "Tell me what to change, like 'change name to Ana Lopez', or reply yes." if language == "en" else "Dime qué cambiar, por ejemplo 'cambiar nombre a Ana Lopez', o responde sí."
                    return TurnResult(reply=prompt, language=language)
                schema = form_library.load_schema(task.form_id)
                return TurnResult(reply=readback_text(task, schema, language=language), language=language)

            if active_task is not None and _asks_for_explanation(req.text):
                schema = form_library.load_schema(active_task.form_id)
                field = next((f for f in schema.fields if f.id == active_task.current_field), None)
                if field is not None:
                    if language == "es":
                        reply = f"Esta pregunta es sobre {field.label.lower()}. {field.question_hint}"
                    else:
                        reply = f"This asks about {field.label.lower()}. {field.question_hint}"
                    return TurnResult(reply=reply, language=language)

            task = answer_field(sess.active_task_id, req.text, channel=req.channel)
            field = None
            if task.current_field:
                schema = form_library.load_schema(task.form_id)
                field = next((f for f in schema.fields if f.id == task.current_field), None)
            if field is not None:
                schema = form_library.load_schema(task.form_id)
                return TurnResult(reply=question_with_stale_hint(
                    task, schema, sess.pending.get("stale_hints", {}), language=language), language=language)
            schema = form_library.load_schema(task.form_id)
            return TurnResult(reply=readback_text(task, schema, language=language), language=language)
        except ValueError as error:
            guidance = str(error).strip() or "Please answer the current question."
            schema = form_library.load_schema(active_task.form_id) if active_task and active_task.form_id else None
            field = next((f for f in schema.fields if f.id == active_task.current_field), None) if schema else None
            prompt = field.question_hint if field else "Please answer the current question so I can keep going."
            return TurnResult(reply=f"{guidance.capitalize()}. {prompt}", language=profile.preferred_language if profile else "en")
    if intent == "fill_form":
        form_id = form_library.match_form(req.text)
        if form_id:
            task = start_form(profile.id, form_id, phone=req.phone, channel=req.channel)
            schema = form_library.load_schema(form_id)
            updated_session = identity.get_session(req.phone)
            language = profile.preferred_language if profile else "en"
            if updated_session.state == "form_memory_confirm":
                return TurnResult(reply=memory_confirmation_text(task, schema, language=language), language=language)
            if task.status == "readback":
                return TurnResult(reply=readback_text(task, schema, language=language), language=language)
            field = next((f for f in schema.fields if f.id == task.current_field), None)
            question = question_with_stale_hint(task, schema, updated_session.pending.get("stale_hints", {}),
                                                language=language)
            return TurnResult(reply=(f"Hi {name}! I found the {form_library.load_meta(form_id).name} form. " + question), language=language)
        return TurnResult(reply=(f"Hi {name}! I can help you fill a form. Tell me the form name or reply with the name of the form you need."), language=profile.preferred_language if profile else "en")
    if intent == "explain_document" and req.media_paths:
        return _explain_document(req, sess, profile)
    if intent == "explain_document":
        if req.channel == "voice":
            return TurnResult(reply="I’ll text you now. Reply with a clear photo of the letter.",
                              followup_sms=["Please reply to this text with a clear photo of the letter."],
                              language=profile.preferred_language if profile else "en")
        return TurnResult(reply="Please send a clear photo of the letter.", language=profile.preferred_language if profile else "en")
    if intent == "status":
        language = profile.preferred_language if profile else "en"
        if "where was i" in req.text.casefold() and sess.active_task_id:
            with session_scope() as s:
                task = s.get(Task, sess.active_task_id)
            if task and task.form_id:
                schema = form_library.load_schema(task.form_id)
                field = next((f for f in schema.fields if f.id == task.current_field), None)
                question = field.question_hint if field else "We are at the answer review."
                return TurnResult(reply=f"We were working on {form_library.load_meta(task.form_id).name}. {question}",
                                  language=language)
        activity = recent_activity(profile.id)
        if not activity:
            reply = "I have not completed anything for you yet." if language == "en" else "Aún no he completado nada para ti."
        else:
            reply = "Here is what I have done: " + "; ".join(activity) if language == "en" else "Esto es lo que he hecho: " + "; ".join(activity)
        return TurnResult(reply=reply, language=language)
    if intent == "forget_me":
        sess.state = "awaiting_forget_confirmation"
        sess.pending = {"profile_id": profile.id}
        identity.save_session(sess)
        reply = "I can permanently delete your saved details and history. Reply yes to confirm or no to keep them." if profile.preferred_language == "en" else "Puedo borrar permanentemente tus datos guardados e historial. Responde sí para confirmar o no para conservarlos."
        return TurnResult(reply=reply, language=profile.preferred_language)
    menu = MENU_ES if (profile and profile.preferred_language == "es") else MENU
    return TurnResult(reply=f"Hi {name}! {menu}", language=profile.preferred_language if profile else "en")


def _asks_for_explanation(text: str) -> bool:
    lowered = text.casefold()
    return any(phrase in lowered for phrase in (
        "what does this mean", "what does that mean", "explain this", "explain the question",
        "why do you need", "no entiendo", "que significa", "qué significa",
    ))


def _requested_language(text: str) -> str | None:
    lowered = text.casefold()
    if any(phrase in lowered for phrase in ("speak spanish", "in spanish", "habla español", "en español", "español por favor")):
        return "es"
    if any(phrase in lowered for phrase in ("speak english", "in english", "habla inglés", "en inglés", "english please")):
        return "en"
    return None


def _localized_question(field, language: str, channel: str) -> str:
    if language == "en":
        return field.question_hint
    spanish = {
        "applicant_name": "¿Cuál es su nombre completo?",
        "date_of_birth": "¿Cuál es su fecha de nacimiento?",
        "address": "¿Cuál es su domicilio?",
        "household_size": "¿Cuántas personas viven con usted, incluyéndole?",
        "employed": "¿Está trabajando actualmente?",
        "employer": "¿Para quién trabaja?",
        "monthly_income": "¿Cuánto dinero recibe su hogar al mes, antes de impuestos?",
        "ssn_last4": "¿Cuáles son los últimos cuatro dígitos de su Seguro Social? Puede omitirlo.",
    }
    if field.id in spanish:
        return spanish[field.id]
    if get_settings().anthropic_api_key:
        try:
            return llm.text(
                system=("Translate the form question into concise Spanish without adding facts. "
                        + style_for(channel)),
                messages=[{"role": "user", "content": field.question_hint}],
                model=llm.fast_model(), max_tokens=180,
            )
        except Exception:
            pass
    return f"Por favor responda sobre {field.label.lower()}."


def _completion_result(task: Task, language: str, phone: str) -> TurnResult:
    sess = identity.get_session(phone)
    sess.state = "menu"
    sess.pending = {}
    sess.active_task_id = None
    identity.save_session(sess)
    if task.status == "completed":
        form_name = form_library.load_meta(task.form_id).name
        count = sum(1 for answer in (task.answers or {}).values() if answer.get("value") is not None)
        if language == "es":
            return TurnResult(
                reply=f"Listo. Completé y verifiqué {count} respuestas de {form_name}.",
                followup_sms=[f"Formline: Se completó y verificó {form_name} con {count} respuestas."],
                language=language,
            )
        return TurnResult(
            reply=f"Done. I filled and verified {count} answers for {form_name}.",
            followup_sms=[f"Formline: Your {form_name} form was filled and verified with {count} answers."],
            language=language,
        )
    schema = form_library.load_schema(task.form_id)
    pdf_fields = {field.pdf_field: field.label for field in schema.fields if field.pdf_field}
    verification = task.verification or {}
    problems = [pdf_fields.get(item.get("pdf_field"), item.get("pdf_field", ""))
                for item in verification.get("mismatches", [])]
    problems.extend(verification.get("missing_required", []))
    problems.extend(verification.get("truncated", []))
    detail = f" Needs attention: {', '.join(dict.fromkeys(filter(None, problems)))}." if problems else ""
    message = (("No pude verificar el formulario. Necesita atención antes de considerarlo completo." + detail)
               if language == "es" else
               ("I couldn't verify the form. It needs attention before it can be considered complete." + detail))
    return TurnResult(reply=message, language=language)


def _new_phone_flow(req: TurnRequest, sess) -> TurnResult:
    text = reply_key(req.text)
    if _language_choice(text) == "en":
        sess.pending = {"language": "en"}
        sess.state = "awaiting_consent"
        identity.save_session(sess)
        return TurnResult(reply=CONSENT_PROMPT, language="en")
    if _language_choice(text) == "es":
        sess.pending = {"language": "es"}
        sess.state = "awaiting_consent"
        identity.save_session(sess)
        return TurnResult(reply=CONSENT_PROMPT_ES, language="es")

    sess.state = "awaiting_language"
    sess.pending = {}
    identity.save_session(sess)
    return TurnResult(reply=LANGUAGE_PROMPT, language="en")


def _language_flow(req: TurnRequest, sess) -> TurnResult:
    text = reply_key(req.text)
    lang = _language_choice(text)
    if lang is None:
        reply = LANGUAGE_PROMPT_ES if sess.pending.get("language") == "es" else LANGUAGE_PROMPT
        return TurnResult(reply=reply, language=sess.pending.get("language", "en"))

    sess.pending = {"language": lang}
    sess.state = "awaiting_consent"
    identity.save_session(sess)
    consent = CONSENT_PROMPT_ES if lang == "es" else CONSENT_PROMPT
    return TurnResult(reply=consent, language=lang)


def _language_choice(text: str) -> str | None:
    """'en español por favor' is Spanish: check the language names before the bare codes,
    and accept a bare 'en'/'es' only as the whole reply."""
    if re.search(r"\b(?:español|espanol|spanish|castellano)\b", text):
        return "es"
    if re.search(r"\b(?:english|inglés|ingles)\b", text):
        return "en"
    return {"en": "en", "es": "es"}.get(text.strip())


def _consent_flow(req: TurnRequest, sess) -> TurnResult:
    text = reply_key(req.text)
    lang = sess.pending.get("language", "en")
    if text in {"yes", "sí", "si", "y", "ok", "okay", "accept", "acepto"}:
        profile = identity.create_profile(req.phone, language=lang)
        sess.profile_id = profile.id
        sess.state = "awaiting_pin_setup"
        sess.pending = {"language": lang}
        identity.save_session(sess)
        return TurnResult(reply=PIN_SETUP_ES if lang == "es" else PIN_SETUP, language=lang)
    if text in {"no", "nope", "not now", "cancel", "cancelar", "n"}:
        sess.state = "awaiting_language"
        sess.pending = {}
        identity.save_session(sess)
        return TurnResult(reply=LANGUAGE_PROMPT_ES if lang == "es" else LANGUAGE_PROMPT, language=lang)
    consent = CONSENT_PROMPT_ES if lang == "es" else CONSENT_PROMPT
    return TurnResult(reply=consent, language=lang)


def _pin_setup_flow(req: TurnRequest, sess) -> TurnResult:
    lang = sess.pending.get("language", "en")
    pin = re.sub(r"[\s.,-]", "", reply_key(req.text))  # "1 2 3 4." from speech -> "1234"
    if len(pin) != 4 or not pin.isdigit():
        prompt = "Please enter exactly 4 digits for your PIN." if lang == "en" else "Ingrese exactamente 4 dígitos para su PIN."
        return TurnResult(reply=prompt, language=lang)
    identity.set_pin(sess.profile_id, pin)
    sess.pin_verified_at = datetime.now(timezone.utc)
    sess.state = "menu"
    sess.pending = {}
    identity.save_session(sess)
    return TurnResult(reply=MENU_ES if lang == "es" else MENU, language=lang)


def _profile_selection_flow(req: TurnRequest, sess, profiles) -> TurnResult:
    text = reply_key(req.text)
    selected = None
    if text.isdigit() and 1 <= int(text) <= len(profiles):
        selected = profiles[int(text) - 1]
    else:
        selected = next((p for p in profiles if p.display_name and p.display_name.casefold() in text), None)
    if selected is None:
        choices = " ".join(f"{i + 1}: {p.display_name or 'Profile'}" for i, p in enumerate(profiles))
        return TurnResult(reply=f"Please choose one of these profiles: {choices}.", language="en")
    sess.profile_id = selected.id
    sess.state = "menu"
    sess.pending = {}
    identity.save_session(sess)
    menu = MENU_ES if selected.preferred_language == "es" else MENU
    return TurnResult(reply=f"Hi {selected.display_name or 'friend'}! {menu}", language=selected.preferred_language)


def _forget_confirmation_flow(req: TurnRequest, sess) -> TurnResult:
    text = reply_key(req.text)
    profile_id = sess.pending.get("profile_id")
    profile = identity.get_profile(profile_id) if profile_id else None
    language = profile.preferred_language if profile else "en"
    if text in {"yes", "y", "sí", "si", "confirm", "confirmo"}:
        from app.memory.profile import forget_profile

        forget_profile(profile_id)
        remaining = identity.profiles_for_phone(req.phone)
        if remaining:
            sess.profile_id = None
            sess.active_task_id = None
            sess.state = "awaiting_profile"
            sess.pending = {}
            identity.save_session(sess)
        reply = "Your saved profile and history have been deleted." if language == "en" else "Tu perfil guardado y tu historial fueron eliminados."
        return TurnResult(reply=reply, language=language)
    if text in {"no", "n", "nope", "cancel", "cancelar"}:
        sess.state = "menu"
        sess.pending = {}
        identity.save_session(sess)
        reply = "Okay. I kept your saved details." if language == "en" else "De acuerdo. Conservé tus datos guardados."
        return TurnResult(reply=reply, language=language)
    prompt = "Reply yes to delete everything, or no to keep it." if language == "en" else "Responde sí para borrar todo o no para conservarlo."
    return TurnResult(reply=prompt, language=language)


def _explain_document(req: TurnRequest, sess, profile) -> TurnResult:
    language = profile.preferred_language if profile else "en"
    try:
        explanation = document_engine.explain_document(req.media_paths, language=language)
    except Exception:
        message = "I couldn't read that photo just now. Please try sending it again." if language == "en" else "No pude leer esa foto. Intenta enviarla de nuevo."
        return TurnResult(reply=message, language=language)

    with session_scope() as s:
        if explanation.related_form_id and s.get(Form, explanation.related_form_id) is None:
            meta = form_library.load_meta(explanation.related_form_id)
            schema = form_library.load_schema(explanation.related_form_id)
            s.add(Form(id=meta.form_id, name=meta.name, aliases=meta.aliases, agency=meta.agency,
                       description=meta.description, reviewed=schema.reviewed, field_count=len(schema.fields)))
            s.flush()
        document = Document(
            profile_id=profile.id,
            media_paths=req.media_paths,
            document_type=explanation.document_type,
            result=explanation.model_dump(mode="json"),
            related_form_id=explanation.related_form_id,
            confidence=explanation.confidence,
        )
        s.add(document)
        s.commit()
        s.refresh(document)
        document_id = document.id

    log_activity("document_explained", f"Explained {explanation.document_type}",
                 profile_id=profile.id)
    log_event("document_explained", profile_id=profile.id,
              document_type=explanation.document_type, related_form_id=explanation.related_form_id)
    publish("document_explained", document_id=document_id, profile_id=profile.id,
            explanation=explanation.model_dump(mode="json"), media_paths=req.media_paths)

    for reference in explanation.reference_numbers:
        try:
            from app.memory import profile as memory

            memory.set_value(profile.id, "case_numbers.other", reference,
                             source_type="document", source_ref=str(document_id))
        except Exception:
            pass

    uncertain = explanation.confidence < 0.5 or bool(explanation.unreadable_parts)
    if uncertain:
        reply = "I couldn't read this clearly enough to explain it safely. Please send a clearer, well-lit photo." if language == "en" else "No pude leerlo con claridad suficiente. Envía una foto más nítida y bien iluminada."
    else:
        reply = explanation.plain_summary
    if explanation.high_stakes:
        reply += " " + referral_for(language)

    if not uncertain and explanation.deadlines and explanation.deadlines[0].date:
        deadline = explanation.deadlines[0]
        sess.state = "awaiting_reminder_consent"
        sess.pending = {
            "document_id": document_id,
            "due_at": deadline.date.isoformat(),
            "reminder_message": deadline.description,
            "related_form_id": explanation.related_form_id,
        }
        identity.save_session(sess)
        question = (f" Would you like a reminder before {deadline.date.isoformat()}? Reply yes or no."
                    if language == "en" else f" ¿Quieres un recordatorio antes del {deadline.date.isoformat()}? Responde sí o no.")
        return TurnResult(reply=reply + question, language=language)
    if not uncertain and explanation.related_form_id:
        sess.state = "awaiting_related_form"
        sess.pending = {"document_id": document_id, "related_form_id": explanation.related_form_id}
        identity.save_session(sess)
        offer = " Would you like help filling out the related form?" if language == "en" else " ¿Quieres ayuda para completar el formulario relacionado?"
        return TurnResult(reply=reply + offer, language=language)
    return TurnResult(reply=reply, language=language)


def _document_followup_flow(req: TurnRequest, sess) -> TurnResult:
    text = reply_key(req.text)
    yes = text in {"yes", "y", "sí", "si", "sure", "okay", "ok"}
    no = text in {"no", "n", "nope", "not now"}
    profile = identity.get_profile(sess.profile_id) if sess.profile_id else None
    language = profile.preferred_language if profile else "en"
    was_reminder = sess.state == "awaiting_reminder_consent"
    if not yes and not no:
        question = "Reply yes or no." if language == "en" else "Responde sí o no."
        return TurnResult(reply=question, language=language)

    if was_reminder and yes:
        from app.reminders import create_reminder

        due_date = datetime.fromisoformat(sess.pending["due_at"]).date()
        due_at = datetime.combine(due_date, time.min, tzinfo=timezone.utc)
        create_reminder(profile.id, due_at, sess.pending["reminder_message"],
                        document_id=sess.pending["document_id"])

    related_form_id = sess.pending.get("related_form_id")
    document_id = sess.pending.get("document_id")
    if was_reminder and related_form_id:
        sess.state = "awaiting_related_form"
        sess.pending = {"document_id": document_id, "related_form_id": related_form_id}
        identity.save_session(sess)
        offer = "Would you like help filling out the related form?" if language == "en" else "¿Quieres ayuda para completar el formulario relacionado?"
        if yes:
            offer = "I set the reminder. " + offer if language == "en" else "Programé el recordatorio. " + offer
        return TurnResult(reply=offer, language=language)

    if not was_reminder and yes and related_form_id:
        task = start_form(profile.id, related_form_id, phone=req.phone, channel=req.channel,
                          from_document_id=document_id)
        schema = form_library.load_schema(related_form_id)
        field = next((f for f in schema.fields if f.id == task.current_field), None)
        updated_session = identity.get_session(req.phone)
        sess.state = updated_session.state
        sess.pending = updated_session.pending
        sess.active_task_id = updated_session.active_task_id
        return TurnResult(reply=field.question_hint if field else "Let's review the form answers.", language=language)

    sess.state = "menu"
    sess.pending = {}
    identity.save_session(sess)
    reply = "Okay." if language == "en" else "De acuerdo."
    return TurnResult(reply=reply, language=language)
