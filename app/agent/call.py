"""One phone conversation that drives a paired browser.

    identify  caller ID -> profiles on that number with a paired browser (ask who's calling if several)
    pin       caller ID is only identification: the PIN must be entered (unless verified in the last
              30 minutes) before Formline touches the browser. 3 wrong tries lock the profile.
    ready     find that person's connected browser, say what page it shows, hand each utterance to
              the BrowserAgent

Channel-agnostic: `say` speaks on a call or sends a text. Voice (app/channels/voice.py) and SMS
(app/channels/messaging.py) create one of these when the caller has a paired browser.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional

from fastapi.concurrency import run_in_threadpool

from app.agent.render import site_name
from app.agent.runner import BrowserAgent, ConnectionPort, Decider
from app.browser import pairing
from app.browser.hub import BrowserConnection, BrowserGone, PageUnavailable, hub
from app.core import identity
from app.events import log_message

log = logging.getLogger(__name__)

_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "won": "1", "two": "2", "to": "2", "too": "2", "three": "3",
    "four": "4", "for": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "ate": "8", "nine": "9",
    "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
    "siete": "7", "ocho": "8", "nueve": "9",
}

TEXT = {
    "en": {
        "who": "Hi, this is Formline. Who's calling? Please say your first name.",
        "who_again": "Sorry, I didn't catch that. Please say your first name: {names}.",
        "pin": "Hi {name}. To use your browser, please say or key in your 4-digit PIN.",
        "pin_wrong": "That PIN didn't match. Please try again. You have {left} {tries} left.",
        "pin_locked": "Your PIN is locked after too many tries. A Formline partner can reset it for you. Goodbye.",
        "pin_none": "You don't have a PIN yet. Open the Formline extension on your computer to set one up. Goodbye.",
        "pin_digits": "Please say or key in the 4 digits of your PIN.",
        "no_browser": "I don't see a connected browser right now. Make sure your computer is on and the Formline "
                      "extension is running. I'll keep listening.",
        "browser_back": "Your browser is connected now.",
        "see": "I can see you have {site} open. What would you like help with?",
        "see_ready": "Thanks, {name}. I can see you have {site} open. What would you like help with?",
        "no_page": "Thanks, {name}. I'm connected to your browser, but I don't see a web page open. Open the "
                   "website you need, then tell me what you'd like to do.",
    },
    "es": {
        "who": "Hola, habla Formline. ¿Quién llama? Diga su nombre, por favor.",
        "who_again": "Perdón, no le entendí. Diga su nombre: {names}.",
        "pin": "Hola {name}. Para usar su navegador, diga o marque su PIN de 4 dígitos.",
        "pin_wrong": "Ese PIN no coincide. Intente de nuevo. Le quedan {left} intentos.",
        "pin_locked": "Su PIN quedó bloqueado. Un socio de Formline puede restablecerlo. Adiós.",
        "pin_none": "Todavía no tiene PIN. Abra la extensión de Formline en su computadora para crear uno. Adiós.",
        "pin_digits": "Diga o marque los 4 dígitos de su PIN.",
        "no_browser": "No veo ningún navegador conectado. Asegúrese de que la computadora esté encendida y la "
                      "extensión de Formline activa. Sigo escuchando.",
        "browser_back": "Su navegador ya está conectado.",
        "see": "Veo que tiene abierto {site}. ¿En qué le ayudo?",
        "see_ready": "Gracias, {name}. Veo que tiene abierto {site}. ¿En qué le ayudo?",
        "no_page": "Gracias, {name}. Estoy conectado a su navegador, pero no veo ninguna página abierta. Abra el "
                   "sitio que necesita y dígame qué quiere hacer.",
    },
}


def spoken_digits(text: str) -> str:
    """'4 8 2 1', 'four eight two one', '4821.' -> '4821'."""
    out = []
    for token in re.findall(r"[a-záéíóú]+|\d", (text or "").casefold()):
        out.append(token if token.isdigit() else _NUMBER_WORDS.get(token, ""))
    return "".join(out)


class CallController:
    def __init__(self, phone: str, *, channel: str, say: Callable[[str], Awaitable[None]],
                 end: Optional[Callable[[], Awaitable[None]]] = None, decide: Optional[Decider] = None):
        self.phone = phone
        self.channel = channel
        self._say_cb = say
        self._end = end
        self._decide = decide
        self.phase = "identify"
        self.profile = None
        self.language = "en"
        self.agent: Optional[BrowserAgent] = None
        self.conn: Optional[BrowserConnection] = None
        self._unsubscribe = hub.on_connect(self._browser_connected)
        self._closed = False

    # ------------------------------------------------------------ public

    @staticmethod
    async def for_caller(phone: str, **kwargs) -> Optional["CallController"]:
        """A controller if this phone has a paired browser, else None (the legacy form brain answers)."""
        if not await run_in_threadpool(pairing.paired_profiles, phone):
            return None
        return CallController(phone, **kwargs)

    async def start(self) -> None:
        profiles = await run_in_threadpool(pairing.paired_profiles, self.phone)
        sess = await run_in_threadpool(identity.get_session, self.phone)
        if len(profiles) == 1:
            await self._select(profiles[0])
        elif sess.profile_id and any(p.id == sess.profile_id for p in profiles) and identity.pin_verified(sess):
            await self._select(next(p for p in profiles if p.id == sess.profile_id))
        else:
            self.phase = "choose"
            await self.say(TEXT["en"]["who"])

    async def on_utterance(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._closed:
            return
        logged = "[PIN]" if self.phase == "pin" else text
        log_message(self.phone, "in", self.channel, logged, profile_id=self.profile.id if self.profile else None)
        if self.phase == "choose":
            await self._choose(text)
        elif self.phase == "pin":
            await self._check_pin(text)
        elif self.phase == "ready":
            if self.conn is None or self.conn.closed:
                if not await self._attach_browser(greet=self.agent is None):
                    return
                if self.agent and not self.agent.task_id:
                    return  # just greeted with the page; this utterance was before we could see it
            await self.agent.handle(text)

    async def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        if self.agent:
            await self.agent.close()

    async def say(self, text: str) -> None:
        if not text or self._closed:
            return
        log_message(self.phone, "out", self.channel, text, profile_id=self.profile.id if self.profile else None)
        await self._say_cb(text)

    # ------------------------------------------------------------ identify and authorize

    def _t(self, key: str, **kw) -> str:
        return TEXT[self.language][key].format(**kw)

    async def _choose(self, text: str) -> None:
        profiles = await run_in_threadpool(pairing.paired_profiles, self.phone)
        match = await run_in_threadpool(identity.match_profile, self.phone, text)
        if match and any(p.id == match.id for p in profiles):
            await self._select(match)
        else:
            names = ", ".join(p.display_name or "Unnamed" for p in profiles)
            await self.say(TEXT["en"]["who_again"].format(names=names))

    async def _select(self, profile) -> None:
        self.profile = profile
        self.language = profile.preferred_language if profile.preferred_language in TEXT else "en"
        sess = await run_in_threadpool(identity.get_session, self.phone)
        await run_in_threadpool(identity.select_profile, sess, profile.id)
        if not profile.pin_hash:
            await self.say(self._t("pin_none"))
            await self._hang_up()
            return
        sess = await run_in_threadpool(identity.get_session, self.phone)
        if identity.pin_verified(sess):
            await self._ready()
        else:
            self.phase = "pin"
            await self.say(self._t("pin", name=profile.display_name or ""))

    async def _check_pin(self, text: str) -> None:
        digits = spoken_digits(text)
        if len(digits) != 4:
            await self.say(self._t("pin_digits"))
            return
        sess = await run_in_threadpool(identity.get_session, self.phone)
        result = await run_in_threadpool(identity.verify_pin, sess, digits)
        if result.ok:
            await self._ready()
        elif result.status == "locked":
            await self.say(self._t("pin_locked"))
            await self._hang_up()
        elif result.status == "no_pin":
            await self.say(self._t("pin_none"))
            await self._hang_up()
        else:
            tries = "try" if result.attempts_left == 1 else "tries"
            await self.say(self._t("pin_wrong", left=result.attempts_left, tries=tries))

    async def _hang_up(self) -> None:
        self.phase = "done"
        if self._end:
            await self._end()

    # ------------------------------------------------------------ the browser

    async def _ready(self) -> None:
        self.phase = "ready"
        await self._attach_browser(greet=True)

    def _pick_browser(self) -> Optional[BrowserConnection]:
        conns = hub.for_profile(self.profile.id)
        return conns[0] if conns else None  # most recently active

    async def _attach_browser(self, *, greet: bool, reconnected: bool = False) -> bool:
        """Point the agent at this person's connected browser (keeping any task in progress). Returns
        False if no browser is connected."""
        conn = self._pick_browser()
        name = self.profile.display_name or ""
        if conn is None:
            await self.say(self._t("no_browser"))
            return False
        self.conn = conn
        port = ConnectionPort(conn)
        if self.agent is None:
            self.agent = BrowserAgent(browser=port, say=self.say, profile_id=self.profile.id,
                                      installation_id=conn.installation_id, phone=self.phone, channel=self.channel,
                                      language=self.language, decide=self._decide)
        else:
            self.agent.browser = port  # same task, new connection
        if not greet:
            return True
        try:
            state = await port.page_state()
            site = site_name(state)
            if reconnected:
                await self.say(self._t("browser_back") + " " + self._t("see", site=site))
            else:
                await self.say(self._t("see_ready", name=name, site=site))
        except (PageUnavailable, BrowserGone):
            await self.say(self._t("no_page", name=name))
        return True

    def _browser_connected(self, conn: BrowserConnection) -> None:
        """A browser came online during the call: if we were waiting for one, greet with the page."""
        if (self._closed or self.phase != "ready" or self.profile is None or conn.profile_id != self.profile.id
                or (self.conn is not None and not self.conn.closed)):
            return
        asyncio.ensure_future(self._attach_browser(greet=True, reconnected=True))
