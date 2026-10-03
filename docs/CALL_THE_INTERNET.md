# Formline: call the internet

Someone has a website open on their computer. They call the Formline number from any phone (smartphone, flip phone, or landline), say what they want to do, and Formline operates the website for them while they watch. It asks them on the phone when the site needs something only they know, and it asks permission before anything final.

The phone is the conversation. The Chrome extension is Formline's eyes and hands. The backend connects them.

```
phone ──Twilio ConversationRelay──► Formline backend ◄──websocket──► Chrome extension ──► the open web page
            (speech ⇄ text)          CallController                    page snapshot ▲   │ one checked action
                                     BrowserAgent ⇄ model                            └───┘ at a time
```

## Try it in 5 minutes (no phone needed)

```bash
uv sync
uv run playwright install chromium        # only for the automated end-to-end runs
cp .env.example .env                      # set OPENAI_API_KEY (or ANTHROPIC_API_KEY)
uv run uvicorn app.main:app               # http://localhost:8000
```

1. **Load the extension:** Chrome → `chrome://extensions` → turn on Developer mode → **Load unpacked** → choose the repo's `extension/` folder. Pin the Formline icon.
2. **Pair it:** click the icon, enter the phone number you'll call from, choose **Text me** (or **Call me** for a landline), type the 6-digit code, then your first name and a 4-digit PIN. Without Twilio set up, the code is printed in the server console (and shown in the popup if `FORMLINE_DEV_ENDPOINTS=true`). The popup shows **CONNECTED** and the badge turns green.
3. **Open the demo portal:** http://localhost:8000/demo/riverbend/
4. **Watch:** http://localhost:8000/dashboard (the **Agent** tab).
5. **Call without a phone:** restart the server with `TWILIO_VALIDATE_SIGNATURES=false`, then
   ```bash
   uv run python scripts/call_sim.py --phone +1YOURNUMBER
   ```
   Type `/key 4821` for the PIN, then "I need to make an appointment with Dr. Smith." Never run with signatures off while ngrok is exposing the server.

## The real phone

1. Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` in `.env`.
2. `ngrok http 8000`, put the https URL in `PUBLIC_BASE_URL`, restart the server.
3. `uv run python -m app.channels.twilio_setup` points the number's voice and SMS webhooks at it.
4. Pair the extension with the phone you'll call from (the code now arrives by text, or by voice call for landlines).
5. Call the number. `uv run python scripts/demo_preflight.py` checks all of this before a demo.

## The golden demo (about 90 seconds)

| Step | Caller says / does | Formline / screen |
|---|---|---|
| 1 | Has the Riverbend portal open; popup shows CONNECTED. Calls Formline. | "Hi Margaret. To use your browser, please say or key in your 4-digit PIN." |
| 2 | Keys **4821** | "Thanks, Margaret. I can see you have Riverbend Health patient portal open. What would you like help with?" |
| 3 | "I need to make an appointment with Dr. Smith." | "Okay, opening your visits." The browser moves: Visits → Schedule → picks Dr. Smith → Next. A blue outline flashes on each control it uses. |
| 4 | | "What would you like to see Dr. Smith about?" |
| 5 | "My knee has been hurting." | The text appears in the reason box; Next. |
| 6 | | "Dr. Smith has Tuesday, October 6th at 10:30 AM, or Thursday, October 8th at 2 PM or 3:30 PM. Which would you like?" |
| 7 | "Thursday at 2." | Picks the slot, reaches **Review your appointment**. Dashboard shows ⏸ Waiting for confirmation. |
| 8 | | "I'm ready to schedule Dr. Alan Smith at Riverbend Main Clinic on Thursday, October 8th at 2 PM for your knee pain. Should I go ahead?" |
| 9 | "Yes." | Clicks **Schedule appointment**; the success page appears. |
| 10 | | "Your appointment is scheduled… confirmation number RB-4xxxx." Dashboard: 🏁 Success verified on the page. |

Also try: "**Stop**" mid-task (halts immediately), "**No**" at the confirmation (nothing is submitted), or the second site, http://localhost:8000/demo/library/index.html: "Can you renew my library book, The Overstory?"

Reset between rehearsals: the portal's footer has **Reset demo data**; the dashboard's **Demo → Reset** wipes Formline's side.

**If something fails on stage:** text the number instead (texting drives the browser the same way); or run `scripts/call_sim.py` on screen next to the dashboard.

## How it works

| Piece | Where | What it does |
|---|---|---|
| Extension | `extension/` | Service worker keeps an authenticated websocket (keepalive, reconnect). The content script builds a **semantic snapshot**: controls with temporary ids (`e12`), headings, short text, no raw HTML. It runs **one action at a time** from a fixed vocabulary and reports what changed. |
| Hub + pairing | `app/browser/` | Live connections, request/response commands, page-snapshot cache, pairing codes, credentials. |
| Agent loop | `app/agent/runner.py` | Goal + conversation + recent steps + current page → model → structured `Decision` → validate → execute → look again. |
| Policy | `app/agent/policy.py` | Step validation, consequential-action backstop, confirmation fingerprint, yes/no/stop. |
| Call controller | `app/agent/call.py` | Caller ID → profile → PIN → find the browser → page-aware greeting → hand speech to the agent. Same for SMS (`app/agent/sms.py`). |
| Voice | `app/channels/voice.py` | Unchanged transport; callers with a paired browser go to the controller, everyone else to the original form assistant. |
| Dashboard | `app/dashboard/agent_api.py`, `static/agent.js` | Agent tab: browsers, live action feed, pending confirmation, call transcript, "what Formline sees". |
| Demo sites | `demo_sites/` | Riverbend Health portal (5-step scheduling wizard), Maple County Library (multi-page renewals), privacy testbench. Fake data only. |

The model answers with one of: `act` (1–3 steps), `ask_user` (one question), `confirm` (the one final step, with a spoken summary), `done` (with quoted evidence), or `blocked` (the person must do something at the computer). Actions: `click, type, clear, select, check, uncheck, press_enter, scroll, go_back, navigate (same site only), focus`. **There is no "run JavaScript" action.**

## Safety and privacy

- **Identification ≠ authorization.** Caller ID finds the paired browser; the PIN (spoken or keyed, hashed, 3 tries then locked until a partner resets it) is required before Formline touches the browser, unless entered in the last 30 minutes.
- **Pairing** proves the phone: a 6-digit code by text or voice call (landlines), 10-minute expiry, 5 tries, 3 codes per number per 10 minutes. The extension then holds a random token; only its SHA-256 is stored. The phone number is never the credential.
- **Consequential actions need a spoken yes.** The model marks them, and code also catches final buttons on its own (book, submit, send, pay, delete, cancel <thing>, final buttons on review pages, agreeing to terms). The exact step is stored server-side with a fingerprint of the page (URL, document, the element, every value and text). "Yes" runs exactly that step, and only if the page is unchanged. "No" cancels; anything else is treated as a new instruction.
- **Success needs evidence.** `done` must quote text that is on a fresh snapshot of the page, and if a final step was confirmed it must have actually run.
- **Stop** cancels the loop immediately, even mid-model-call.
- **What leaves the browser:** a sanitized snapshot only. Password fields, hidden inputs, and fields that look like card numbers, SSNs, PINs or one-time codes are reported without values; SSN- and card-shaped numbers are masked in all text; query strings are dropped from URLs; cookies and storage are never read. The server scrubs again. Typing into secret fields is refused in the extension and in the policy.
- **Page text is data.** The prompt says so, and the demo portal's inbox contains an injection attempt to test it.
- **No security bypass.** CAPTCHAs, logins, MFA and verification codes are handed back to the person at the computer.
- **Screenshots** (`FORMLINE_VISION_FALLBACK=true`) are off by default because they skip the redaction; when on, they're only sent for pages whose snapshot has almost no text.

## Failure handling

| Situation | What happens |
|---|---|
| No browser connected | "I don't see a connected browser right now…" and it keeps listening; when the browser connects mid-call it says so and describes the page. |
| Browser disconnects mid-task | "I lost the connection to your browser…"; on reconnect the same task continues. |
| Stale or invented element id | Refused before reaching the page; the reason is fed back to the model. |
| Click covered by something | The extension reports what's covering it; the model handles it (e.g. closes the dialog). |
| Page still loading | The agent waits for loading text, `aria-busy`, or "Scheduling…"-style buttons to clear (up to ~6 s). |
| Form validation error | Shown in the snapshot (`INVALID`, alert text); the model fixes it. |
| Link opens a new tab | The extension follows it and the task continues there. |
| Model error or cut-off output | One automatic retry; then "Sorry, something went wrong…". |
| Stuck after 3 errors, or 16 decisions | Says so and asks the caller how to proceed. |
| Caller hangs up | The task is marked interrupted; nothing else runs. |

## Testing

```bash
uv run pytest                                              # 369 offline tests (fake browser, scripted model)
uv run python scripts/extension_smoke.py                   # real Chromium: pairing, actions, stale ids, privacy, screenshot
uv run python scripts/e2e_golden_path.py --runs 3          # real model + Chromium + call simulator, golden demo
uv run python scripts/e2e_golden_path.py --scenario library
uv run python scripts/agent_bench.py trace.jsonl --models gpt-5.4-mini,gpt-4.1-mini   # compare models on recorded prompts
```

`FORMLINE_AGENT_TRACE=trace.jsonl` records every agent prompt and decision (local debugging only: it contains page text and what the caller said).

Latest live results (2026-10-03, `gpt-5.4-mini`, simulated caller): golden demo 11 consecutive runs passed, 19–26 s per call end to end, 10 browser steps and 12 model calls each; library renewal 4 consecutive runs passed, 7–10 s, 4 model calls. Each model decision takes about 0.8–1 s; the longest wait for the caller is the first turn (about 7 s, three pages deep), covered by "One moment" and a short status line.

## Known limits and next steps

- Formline acts on the active tab of the most recently used browser; with several paired browsers it picks the most recently active one rather than asking.
- Custom widgets with no accessible name and no visible text can't be targeted yet; the screenshot fallback helps the model understand them but there is no click-by-coordinates action (deliberately).
- Iframes (including cross-origin) aren't read.
- Twilio ConversationRelay plays queued status lines in order; a long status can delay a question by a second or two.
- The real phone path is the same code as the simulator, but it still needs one live run with the team's Twilio number and ngrok.
