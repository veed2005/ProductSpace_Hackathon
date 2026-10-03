"""handle_turn: the single entry point both channels call. Owner: Lane A.

Contract (do not change the signature without telling Lane B):
    handle_turn(TurnRequest) -> TurnResult

Everything below the logging is a placeholder that proves the wiring end to end.
Lane A replaces it with the real state machine (identity -> router -> engines -> run-back).
"""

from app.contracts import TurnRequest, TurnResult
from app.core import identity
from app.core.router import classify_intent
from app.engines import document_engine, form_library
from app.events import log_message

MENU = "I can help you fill out a form, or explain a letter or document you got. What do you need?"


def handle_turn(req: TurnRequest) -> TurnResult:
    sess = identity.get_session(req.phone)
    sess.last_channel = req.channel
    identity.save_session(sess)
    log_message(req.phone, "in", req.channel, req.text, profile_id=sess.profile_id,
                task_id=sess.active_task_id, media=req.media_paths)

    result = _placeholder_brain(req)

    log_message(req.phone, "out", req.channel, result.reply, profile_id=sess.profile_id,
                task_id=sess.active_task_id)
    return result


def _placeholder_brain(req: TurnRequest) -> TurnResult:
    intent = classify_intent(req.text, has_media=bool(req.media_paths))
    if intent == "fill_form":
        form_id = form_library.match_form(req.text)
        if form_id:
            return TurnResult(reply=f"(placeholder) I found the form '{form_library.load_meta(form_id).name}'.")
        return TurnResult(reply="(placeholder) I couldn't find that form yet.")
    if intent == "explain_document" and req.media_paths:
        return TurnResult(reply=document_engine.explain_document(req.media_paths).plain_summary)
    if intent == "explain_document":
        return TurnResult(reply="Please text me a photo of the letter.")
    return TurnResult(reply=MENU)
