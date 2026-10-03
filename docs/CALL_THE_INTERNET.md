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

**PDFs:** open any PDF in Chrome (try http://localhost:8000/demo/testbench/lease.pdf) and ask about it: "I don't understand this contract", "Can I have a dog?", "How do I get out of my lease early?". Formline reads the whole document, every page, and each answer is backed by a quote from it (shown on the dashboard). It keeps a private copy of a PDF in that tab the moment it opens, so document portals' links that expire after a minute (S3 and similar, often marked no-store) still work later in the call. If a PDF was opened before Formline could keep a copy and its link has expired, it reads only what's on screen and says so; opening the document again from the website gives it the whole thing. PDFs saved on the computer (`file://`) need one switch: chrome://extensions → Formline → Details → **Allow access to file URLs**. Limits: 10 MB per PDF; about 250,000 characters of text (roughly 150 pages) are read; scanned PDFs with no text layer are read as pictures of their first 8 pages.

**Asking about what's on screen:** "Who's Peter New?" on a film page, "How much is my rent?" on a statement. When you ask something, the agent gets a screenshot of what you see along with the page text, so it can answer from what's actually on your screen (tables, images, layout the text misses). Answers quote the page; a quote that can't be confirmed is flagged on the dashboard but the answer is still given, never a dead end. Each new request starts fresh; "try again", "keep going" or "OK, I logged in" continue the last one.

**Searching sites:** "Look up Arrival" is one `search` step, done by code in the extension the same careful way every time: pick the site's main search box (not a filter for one person's reviews, not a box in a dialog), open it if it's hidden behind a search icon and wait until it has finished opening, type key by key, wait for suggestions, then submit with Enter, the form, or the search button. It reports what happened (results page, or the suggestions to click). http://localhost:8000/demo/reelbox/profile.html is a Letterboxd-like test page with an animated hidden search and decoy search boxes. The agent can't type web addresses, and a new request never inherits details (like which movie) from an earlier one; it asks.

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

The model answers with one of: `act` (1–3 steps), `ask_user` (one question), `confirm` (the one final step, with a spoken summary), `done` (with quoted evidence), `answer` (a reply about the page or document, with quoted evidence; the conversation continues), or `blocked` (the person must do something at the computer). Actions: `search` (the site's own search, run end to end by the extension), `click, type, clear, select, check, uncheck, press_enter, scroll, go_back, focus`. Each look at the page marks what's new since the last look with `+`, so the agent can see what its action opened. Typing is key by key; the extension waits for animations and late-arriving suggestions before the next look; a text box counts as visible only if it's really open on screen. **There is no "run JavaScript" action, and no typing of web addresses.**

**PDFs:** Chrome's PDF viewer is sealed off from other extensions (its frames can't be scripted and its text commands can't be reached), so the file itself is read. When a PDF tab opens, the content script immediately keeps a copy in that tab's memory, fetched from the page itself (same origin, with the person's own session; it leaves the browser only when the person asks about the document). The server extracts every page with PyMuPDF (`app/browser/pdf.py`), masking SSN- and card-shaped numbers. If there's no copy and the link has expired, the visible part of the viewer is captured instead and the agent is told that's all it can see. The whole text goes into the model's view of the page, first in the prompt so follow-up questions about the same document can reuse the provider's prompt cache.

## Safety and privacy

- **Identification ≠ authorization.** Caller ID finds the paired browser; the PIN (spoken or keyed, hashed, 3 tries then locked until a partner resets it) is required before Formline touches the browser, unless entered in the last 30 minutes.
- **Pairing** proves the phone: a 6-digit code by text or voice call (landlines), 10-minute expiry, 5 tries, 3 codes per number per 10 minutes. The extension then holds a random token; only its SHA-256 is stored. The phone number is never the credential.
- **Consequential actions need a spoken yes.** The model marks them, and code also catches final buttons on its own (book, submit, send, pay, delete, cancel <thing>, final buttons on review pages, agreeing to terms). The exact step is stored server-side with a fingerprint of the page (URL, document, the element, every value and text). "Yes" runs exactly that step, and only if the page is unchanged. "No" cancels; anything else is treated as a new instruction.
- **Success needs evidence.** `done` must quote text that is on a fresh snapshot of the page, and if a final step was confirmed it must have actually run.
- **Stop** cancels the loop immediately, even mid-model-call.
- **What leaves the browser:** a sanitized snapshot only. Password fields, hidden inputs, and fields that look like card numbers, SSNs, PINs or one-time codes are reported without values; SSN- and card-shaped numbers are masked in all text; query strings are dropped from URLs; cookies and storage are never read. The server scrubs again. Typing into secret fields is refused in the extension and in the policy.
- **Page text is data.** The prompt says so, and the demo portal's inbox contains an injection attempt to test it.
- **No security bypass.** CAPTCHAs, logins, MFA and verification codes are handed back to the person at the computer.
- **Screenshots** of the visible tab go to the model when the caller asks a question, after a failed step, or when a page has almost no text, so it can see what the person sees. They can't be redacted the way page text is (an SSN visible on screen would be in the picture), so `FORMLINE_SCREENSHOTS=false` turns them off.

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
| PDF can't be read | Too big, password-protected, or a local file without "Allow access to file URLs": the agent says which, and how to fix it. |
| PDF link expired | Read from the copy kept when the tab opened; without one, only the visible part, and the agent says so. |
| The person switches tabs mid-task | The agent follows them to the new tab and is told it changed. |
| A search box hidden behind an icon | The search routine opens it once and waits for it to finish opening (clicking again would close it). |
| Enter doesn't submit (no form, or the site ignores it) | The search routine submits the form or clicks the search/Go button. |
| Suggestions instead of results | Reported with their ids; the agent clicks the right one. |
| The same click keeps not working | The third identical click on an unchanged page is refused; the agent must try something else or ask. |
| A bot check ("Just a moment...") | The greeting names the site from its address, and the agent asks the person to complete the check. |
| An answer quotes text that isn't there | Rejected and retried; a summary may stitch several real quotes, but every piece must be in the page or document. |
| Model error or cut-off output | One automatic retry; then "Sorry, something went wrong…". |
| Stuck after 3 errors, or 16 decisions | Says so and asks the caller how to proceed. |
| Caller hangs up | The task is marked interrupted; nothing else runs. |

## Testing

```bash
uv run pytest                                              # 395 offline tests (fake browser, scripted model)
uv run python scripts/extension_smoke.py                   # real Chromium: pairing, actions, stale ids, privacy, screenshot
uv run python scripts/e2e_golden_path.py --runs 3          # real model + Chromium + call simulator, golden demo
uv run python scripts/e2e_golden_path.py --scenario library
uv run python scripts/e2e_golden_path.py --scenario pdf             # 4 questions about a 6-page lease
uv run python scripts/e2e_golden_path.py --scenario expiring_pdf    # the same PDF from an expired no-store link
uv run python scripts/e2e_golden_path.py --scenario reelbox         # "Look up Avengers" must use the search box
uv run python scripts/e2e_golden_path.py --scenario reelbox_vague   # "Look up a movie" must ask which one
uv run python scripts/e2e_golden_path.py --scenario reelbox_profile # Letterboxd-like: hidden animated search, decoys
uv run python scripts/e2e_golden_path.py --scenario reelbox_explore # a search with no form and no Enter
uv run python scripts/e2e_golden_path.py --scenario reelbox_film_qa # search, questions about the film, then an action
uv run python scripts/e2e_golden_path.py --scenario statement_pdf   # a rent table PDF behind an expired /original link
uv run python scripts/agent_bench.py trace.jsonl --models gpt-5.4-mini,gpt-4.1-mini   # compare models on recorded prompts
```

`FORMLINE_AGENT_TRACE=trace.jsonl` records every agent prompt and decision (local debugging only: it contains page text and what the caller said).

Latest live results (2026-10-03, `gpt-5.4-mini`, simulated caller): golden demo 3/3 (19–22 s per call, 10 browser steps, 12–13 model calls); library renewal 3/3 (about 10 s, 4 browser steps); PDF questions 3/3 (every answer correct and quoted, about 1 s each); site search 3/3 and vague lookup 3/3. Real letterboxd.com blocks automated browsers with a bot check, so it's covered by the Reelbox test site and by hand. Each model decision takes about 0.8–1 s; the longest wait for the caller is the first turn (about 7 s, three pages deep), covered by "One moment" and a short status line.

## Known limits and next steps

- Formline acts on the active tab of the most recently used browser; with several paired browsers it picks the most recently active one rather than asking.
- Custom widgets with no accessible name and no visible text can't be targeted yet; the screenshot fallback helps the model understand them but there is no click-by-coordinates action (deliberately).
- Iframes (including cross-origin) aren't read.
- PDFs over 10 MB, or past about 250,000 characters of text, are cut off; the agent says so.
- Twilio ConversationRelay plays queued status lines in order; a long status can delay a question by a second or two.
- The real phone path is the same code as the simulator, but it still needs one live run with the team's Twilio number and ngrok.
