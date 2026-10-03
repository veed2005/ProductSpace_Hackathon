"""Call Formline from the terminal, exactly the way Twilio's ConversationRelay talks to it.

    uv run python scripts/call_sim.py --phone +15550001111 [--server http://localhost:8000]

It POSTs the incoming-call webhook, opens the relay websocket from the returned TwiML, and then
sends what you type as speech. Type `/key 4821` to press keypad digits, `/quit` to hang up.
The server must run with TWILIO_VALIDATE_SIGNATURES=false (local rehearsal only).

Also importable: `PhoneCall` is used by scripts/e2e_golden_path.py.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
import time
from typing import Optional

import httpx
import websockets


class PhoneCall:
    def __init__(self, server: str, phone: str):
        self.server = server.rstrip("/")
        self.phone = phone
        self.ws = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.heard: list[tuple[float, str]] = []
        self._reader: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "PhoneCall":
        async with httpx.AsyncClient() as client:
            r = await client.post(self.server + "/twilio/voice",
                                  data={"From": self.phone, "To": "+15550000000", "CallSid": "CAsim"})
            r.raise_for_status()
        m = re.search(r'url="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("No ConversationRelay in TwiML:\n" + r.text)
        url = html.unescape(m.group(1))
        if self.server.startswith("http://"):
            url = url.replace("wss://", "ws://", 1)
        self.ws = await websockets.connect(url)
        await self.ws.send(json.dumps({"type": "setup", "from": self.phone, "to": "+15550000000",
                                       "callSid": "CAsim"}))
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            await self.ws.close()

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "text":
                    self.heard.append((time.monotonic(), msg["token"]))
                    await self.queue.put(msg["token"])
                elif msg.get("type") == "end":
                    await self.queue.put(None)
        except websockets.ConnectionClosed:
            await self.queue.put(None)

    async def hear(self, timeout: float = 60) -> Optional[str]:
        """The next line Formline speaks (None when the call ends)."""
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def say(self, text: str, lang: str = "en-US") -> None:
        await self.ws.send(json.dumps({"type": "prompt", "voicePrompt": text, "lang": lang, "last": True}))

    async def key(self, digits: str) -> None:
        for d in digits:
            await self.ws.send(json.dumps({"type": "dtmf", "digit": d}))


async def interactive(server: str, phone: str) -> None:
    async with PhoneCall(server, phone) as call:
        async def printer():
            while True:
                line = await call.queue.get()
                if line is None:
                    print("\n[call ended]")
                    return
                print(f"\nFORMLINE: {line}\nYOU> ", end="", flush=True)

        printing = asyncio.create_task(printer())
        loop = asyncio.get_running_loop()
        while not printing.done():
            text = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if text in ("/quit", "/hangup"):
                break
            if text.startswith("/key "):
                await call.key(text.split(None, 1)[1].strip())
            elif text:
                await call.say(text)
        printing.cancel()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--phone", required=True)
    args = p.parse_args()
    asyncio.run(interactive(args.server, args.phone))


if __name__ == "__main__":
    main()
