"""Test doubles for the phone-form workflow: a scripted model, and fake email / submission providers."""

from __future__ import annotations

import ast
from typing import Optional

from app.contracts import TurnRequest
from app.core.turn import handle_turn
from app.formcall import email as email_mod
from app.formcall import interpret as interpret_mod
from app.formcall import submit as submit_mod
from app.formcall.explain import Explanation
from app.formcall.interpret import TurnInterpretation
from app.llm import client as llm


class FakeModel:
    """Scripted proposals by exactly what the caller said; anything unscripted gets the keyword fallback, so the
    deterministic code paths are what's being tested."""

    def __init__(self, monkeypatch):
        self.interps: dict[str, dict] = {}
        self.explanations: dict[str, dict] = {}
        self.translations: dict[str, str] = {}
        self.calls: list[str] = []
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "structured", self.structured)
        monkeypatch.setattr(llm, "text", self.text)

    def structured(self, output, *, system, messages, model=None, max_tokens=None, **kw):
        content = messages[-1]["content"]
        self.calls.append(output.__name__)
        if output is TurnInterpretation:
            said = ast.literal_eval(content.rsplit("The caller said: ", 1)[1])
            base = interpret_mod.fallback(said, schema=None, field=None)
            return base.model_copy(update=self.interps.get(said, {}))
        if output is Explanation:
            for key, value in self.explanations.items():
                if key in content:
                    return Explanation(**value)
            return Explanation(quote=None, plain="It's asking about that.", uncertain=False, consequential=False)
        raise AssertionError(f"unexpected model call for {output.__name__}")

    def text(self, *, system, messages, model=None, max_tokens=None):
        said = messages[-1]["content"]
        return self.translations.get(said, said)


class FakeEmail:
    name = "fake"

    def __init__(self, *, ok=True, delivered=True):
        self.ok, self.delivered = ok, delivered
        self.sent: list[email_mod.Email] = []

    def send(self, email):
        self.sent.append(email)
        if not self.ok:
            return email_mod.SendResult(ok=False, delivered=False, error="mail server said no")
        return email_mod.SendResult(ok=True, delivered=self.delivered, ref="msg-1")


class FakeSubmission:
    name = "fake-agency"

    def __init__(self, result: submit_mod.SubmissionResult):
        self.result = result
        self.calls = 0

    def submit(self, *, form_id, pdf_path, task_id):
        self.calls += 1
        return self.result


class Caller:
    def __init__(self, phone: str, channel: str = "voice"):
        self.phone, self.channel = phone, channel
        self.heard: list[str] = []
        self.languages: list[str] = []

    def say(self, text: str, hint: Optional[str] = None) -> str:
        result = handle_turn(TurnRequest(phone=self.phone, channel=self.channel, text=text, language_hint=hint))
        self.heard.append(result.reply)
        self.languages.append(result.language)
        return result.reply
