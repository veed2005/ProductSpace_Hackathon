# Demo script: call the internet

About **3½ minutes** on stage, then the pitch. One person plays **Margaret Ellis**, who has a website open on her computer and doesn't want to fight with it. She calls Formline from an ordinary phone, says what she wants, and Formline operates the site in front of everyone, asking her on the phone before anything final.

This is the flow the team has tested live: the golden demo passed 3 of 3 runs on 2026-10-03 (see `docs/CALL_THE_INTERNET.md`). The original paperwork flow (Rosa) isn't in the stage demo for now; see [the appendix](#appendix-the-paperwork-demo-on-hold).

## Cast

| Role | Does |
|---|---|
| **Narrator** | Talks to the judges. Never touches a device. |
| **Margaret** | Holds the phone on speaker, says the lines below. Sits beside the laptop so the audience sees both. |
| **Operator** | Runs the laptop: the window layout, resets, and the backup if needed. Doesn't touch the browser during the run; Formline does. |

## The screen

One laptop runs everything and is mirrored to the projector:
- **Left half:** Chrome with the **Riverbend Health** portal, at http://localhost:8000/demo/riverbend/
- **Right half:** the dashboard's **Agent** tab, at http://localhost:8000/dashboard, which shows the live action feed, the call, and the pending confirmation.

Set the browser zoom so the portal's buttons are readable from the back of the room (110–125%).

## Margaret's lines

| When | Margaret says |
|---|---|
| Formline asks for the PIN | Keys **4821** on the phone (or says "four eight two one") |
| "What would you like help with?" | "I need to make an appointment with Dr. Smith." |
| "What would you like to see Dr. Smith about?" | "My knee has been hurting." |
| It offers times | "Thursday at 2." |
| "Should I go ahead?" | "Yes." |
| (Act 2) on the lease PDF | "I don't understand this lease. Can I have a dog?" |
| (Act 2) follow-up | "How do I get out of my lease early?" |
| (Act 3, optional) on the library site | "Can you renew my library book, The Overstory?" |

## One-time setup on the demo laptop

Do this once, at least an hour before the first rehearsal. `docs/CALL_THE_INTERNET.md` has more detail.

1. **`.env`:** copy `.env.example` to `.env`. Put in `OPENAI_API_KEY` (the agent defaults to `gpt-5.4-mini`) or `ANTHROPIC_API_KEY`, plus `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` and `PUBLIC_BASE_URL`. Get these from the team privately, never in a group chat.
2. **ngrok:** `ngrok config add-authtoken <token>` using the token from the ngrok account that owns the team's reserved domain. Only one computer can run that tunnel at a time, so stop it anywhere else first.
3. **Chrome extension:** `chrome://extensions` → Developer mode on → **Load unpacked** → the repo's `extension/` folder. Pin the Formline icon.
4. **Reset first, then pair.** Dashboard → **Demo → Reset everything** deletes the browser pairing too, so do any reset *before* pairing:
   - Start the server and ngrok (see "Before going on stage").
   - Click the extension icon, enter Margaret's phone number (the phone you'll call from on stage), and choose **Call me**. Outgoing texts don't deliver until the A2P registration is approved.
   - Type the 6-digit code you hear, then first name **Margaret** and PIN **4821**.
   - The popup shows **CONNECTED** and the badge turns green.
   - If "Call me" fails too (Twilio voice geo permissions not enabled yet), comment out `TWILIO_ACCOUNT_SID` in `.env`, restart the server, and pair: the code prints in the server console. Then restore the line and restart. The pairing is kept.

## Before going on stage

**30 minutes before**
1. Laptop on power, Do Not Disturb on, every other app closed.
2. Terminal 1: `uv run uvicorn app.main:app` (no `--reload` on stage).
3. Terminal 2: `ngrok http --url=<the team's reserved domain> 8000`.
4. If the ngrok URL changed: put it in `PUBLIC_BASE_URL`, restart the server, then `uv run python -m app.channels.twilio_setup`.
5. Run the pre-flight check and fix every ❌:
   ```bash
   uv run python scripts/demo_preflight.py
   ```
6. **Dry run.** Margaret calls and books an appointment end to end. Then reset only the website: the portal's footer → **Reset demo data**. **Don't** use the dashboard's Reset everything now; it would delete the pairing.
7. Open the lease PDF in a second tab, http://localhost:8000/demo/testbench/lease.pdf, then switch back to the Riverbend tab.

**5 minutes before**
- Riverbend tab in front, on its home page. Popup says **CONNECTED**. Dashboard on the **Agent** tab.
- Margaret's phone: volume up, on speaker, the Formline number ready to dial, battery above 50%.
- Everyone else silences their phones.

## The run

### Act 1: Book an appointment by phone (≈90 s)

| | |
|---|---|
| **Margaret** | Calls Formline on speaker. Keys **4821**. "I need to make an appointment with Dr. Smith." |
| **Formline** | "Hi Margaret… please say or key in your PIN." Then "I can see you have Riverbend Health patient portal open. What would you like help with?" and "Okay, opening your visits." |
| **Screen** | The portal moves by itself: Visits → Schedule → Dr. Smith → Next. A blue outline flashes on each control it uses. The dashboard feed lists each step. |
| **Margaret** | "My knee has been hurting." Then, when offered times: "Thursday at 2." |
| **Formline** | Reads out the available times, picks Thursday 2 PM, and reaches **Review your appointment**. The dashboard shows **⏸ Waiting for confirmation**. "I'm ready to schedule Dr. Alan Smith … Thursday at 2 PM for your knee pain. Should I go ahead?" |
| **Margaret** | "Yes." |
| **Screen** | It clicks **Schedule appointment**. The success page appears, and the dashboard shows **🏁 Success verified on the page** with the confirmation number. |
| **Narrator** | "Margaret has the portal open but can't make sense of it. She calls Formline from any phone (it works on a landline) and just says what she wants. Formline is driving her actual browser. … Watch: before anything final, it stops and asks her out loud. Nothing gets booked, sent, or paid without a spoken yes, and 'done' only counts when the confirmation is really on the page." |
| **If it fails** | It stalls on a step: Margaret says "try again". The call drops: call back (the task continues). Still stuck: go to Plan B. |

### Act 2: Understand a document (≈45 s)

| | |
|---|---|
| **Operator** | Switches Chrome to the **lease PDF** tab. |
| **Margaret** | "I don't understand this lease. Can I have a dog?" Then: "How do I get out of my lease early?" |
| **Formline** | Answers in plain words from the actual lease. The dashboard shows the quote from the PDF that backs each answer. |
| **Narrator** | "Same call. Now it's a six-page lease. Formline reads the whole document, every page, and every answer is backed by a quote from it, so it can't make things up." |

### Act 3 (optional, only if you're under time): A second site (≈30 s)

| | |
|---|---|
| **Operator** | Opens http://localhost:8000/demo/library/index.html in the active tab. |
| **Margaret** | "Can you renew my library book, The Overstory?" |
| **Narrator** | "Any site, not a hard-coded one. It's a library this time, and there's no code specific to this site." |

### Close (≈15 s)

**Narrator:** "No app on the phone, no new website to learn. Her phone becomes the way she uses the internet. The same number also fills out benefits forms and explains letters for people without a computer at all."

## What to say about safety (if asked)

- The **PIN** is required before Formline touches the browser (once per 30 minutes), because caller ID only identifies the browser. After 3 wrong PINs it locks until a partner resets it.
- **Passwords, card numbers, SSNs and hidden fields never leave the browser.** The page is sent as a redacted text snapshot.
- **The agent can't run code or type web addresses.** It has a fixed set of actions: search, click, type, select…
- **Logins, CAPTCHAs and verification codes** are handed back to the person at the computer.
- Text on a web page is treated as **data, never instructions**. The demo portal's inbox includes an injection attempt as a test.
- Saying **"Stop"** halts it immediately.

## Backup plans

If a step hasn't worked after **one retry**, switch to the next plan.

| Plan | When | What |
|---|---|---|
| **A: retry** | One step stalls | Margaret: "try again". |
| **B: call simulator on screen** | The phone call, Twilio, or the venue network fails | 1. **Stop ngrok first** (Ctrl+C in terminal 2): never turn signature checks off while the server is exposed. 2. Restart the server with `TWILIO_VALIDATE_SIGNATURES=false uv run uvicorn app.main:app`. 3. In a third window next to the dashboard: `uv run python scripts/call_sim.py --phone <Margaret's paired number>`, then type `/key 4821` and her lines. The browser and dashboard behave exactly the same. |
| **C: video** | Nothing live works | Play the backup video from the final rehearsal and narrate over it. |

**Texting is not a fallback yet.** Twilio accepts texts but won't deliver replies until the A2P registration is approved. Check `A2P_RESUBMISSION.md` / the Twilio console before the demo.

## Recording the backup video

At the last clean rehearsal:
1. QuickTime → File → New Screen Recording, full screen with the split view.
2. For call audio, set `FORMLINE_RECORD_CALLS=true` in `.env` and restart. The greeting then says the call is recorded, so team phones only. Or film the phone on speaker with a second phone.
3. Run Acts 1–2 exactly as scripted. Trim to under 3 minutes. Save it to the laptop **and** a USB stick.

## Between rehearsals

- **Website:** Riverbend footer → **Reset demo data**. This undoes the booked appointment.
- **Formline:** leave it alone. **Demo → Reset everything** deletes the pairing; if you use it, pair again (setup step 4).
- After any code change: restart the server and run the pre-flight check again.

## Rehearsal log

| # | Date / time | Plan used | Total time | What broke | Fixed by |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

## Appendix: the paperwork demo (on hold)

The original flow, where Rosa fills out SNAP by phone in Spanish, texts a photo of a Medicaid letter, and fills the renewal from memory, is still in the product. But `uv run python scripts/demo_replay.py` shows it isn't stage-ready yet. Spanish answers are misread or answered in English, and a PDF field overflows so verification fails (details in `docs/REVIEW_DEMO_PATH.md`). It also depends on texting, which waits on the A2P registration.

If Lane A fixes it and the replay runs clean, it can return as a short Act 4. The full Rosa script, persona card and printable letter (`scripts/make_demo_letter.py`) are in this file's previous version: `git show f189498:docs/DEMO_SCRIPT.md`.
