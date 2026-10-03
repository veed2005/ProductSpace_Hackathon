# Demo script

The on-stage flow from Section 15 of the brief: about **4½ minutes**, then the pitch wrap-up. One person plays a new user, **Rosa**, on a basic phone. Everything she says is scripted below so the live run matches the rehearsals.

Formline's exact wording comes from Lane A's prompts, so its lines below are what it **should** say, not a word-for-word script. Update them after the M2 checkpoint run.

## Cast

| Role | Who (suggested) | Does |
|---|---|---|
| **Narrator** | whoever pitches | Talks to the judges. Never touches a device. |
| **Rosa** | Edoubek1024 (knows the phone side best) | Holds the flip phone, speaks and texts Rosa's lines. Sits where the audience can see the phone. |
| **Operator** | veed2005 | Drives the dashboard on the projector: tabs, Download PDF, Metrics, upload. |
| **Backup** | aryavsaigal / luisNava111 | Second laptop with the simulator and the backup video open, ready to take over the screen. |

## Rosa's persona card (fake)

Rosa answers in Spanish. English is here for the team; the Narrator translates the key moments.

| Question | Rosa says | Meaning |
|---|---|---|
| Language | "Español, por favor." | Spanish, please. |
| Consent | "Sí, está bien." | Yes, that's fine. |
| New PIN | "Cuatro, ocho, dos, uno." (or keys 4821) | PIN 4821 |
| What do you need? | "Necesito pedir estampillas de comida." | I need to apply for food stamps. |
| Full name | "Rosa Martínez." | |
| Date of birth | "Veintidós de julio de mil novecientos setenta y nueve." | July 22, 1979 |
| Address | "Setenta y siete Maple Avenue, apartamento tres, Springfield, Illinois, sesenta y dos mil setecientos tres." | 77 Maple Ave Apt 3, Springfield IL 62703 |
| Household | "Somos tres: yo y mis dos hijos, Diego de nueve años y Ana de seis." | Three of us: me and my two kids, Diego (9) and Ana (6). |
| Working? | "Sí, en Lincoln Laundromat." | Yes, at Lincoln Laundromat. |
| Pay | "Como trescientos cincuenta a la semana, pero cambia." | About $350 a week, but it changes. **(Messy answer: Formline should confirm about $1,516/month, varies.)** |
| Rent | "Novecientos al mes." | $900 a month |
| SSN | "Prefiero no darlo." | I'd rather not give it. **(Skipped: "to be provided to caseworker".)** |
| Read-back correction | "La renta es novecientos cincuenta, no novecientos." | Rent is $950, not $900. **(Shows a "Corrected" field.)** |

## Before going on stage

**30 minutes before**
1. Laptop on power, notifications off (Do Not Disturb), browser zoom at 100%, dashboard tab only.
2. `uv run uvicorn app.main:app` (no `--reload` on stage), then `ngrok http 8000` with the reserved domain. Put the URL in `PUBLIC_BASE_URL` and restart the server if it changed.
3. Dashboard → **Demo** → put Rosa's flip phone number in the box → **Reset everything**. (Rosa is a *new* user, so her phone must have no data. Reset clears every number.)
4. Generate and print the letter: `uv run python scripts/make_demo_letter.py`. Put the printout on the table next to Rosa.
5. Run the pre-flight check and fix every ❌:
   ```bash
   uv run python scripts/demo_preflight.py
   ```
6. Send one test text **and one test photo** from Rosa's phone, confirm both replies, then **Reset everything** again. Many flip phones can't send MMS photos; if Rosa's can't, use a basic smartphone as "Rosa's phone" for the whole demo (memory is tied to the phone number, so don't switch phones mid-demo).
7. Backup laptop: video file open and paused at 0:00; a terminal ready with the simulator command from Plan C.

**5 minutes before**
- Dashboard on the **Live** page, **Follow live** checked, Form tab visible.
- Rosa's phone: volume up, on speaker, the Formline number on speed dial, battery above 50%.
- Everyone silences their own phones.

## The run

### Step 1: A new caller starts SNAP in Spanish, then switches to text (≈90 s)

| | |
|---|---|
| **Rosa does** | Calls the Formline number on speaker. Answers language, consent, PIN, "estampillas de comida", then name, birthday, and address from the persona card. Then: *"Perdón, tengo que colgar. Le sigo por mensaje."* ("Sorry, I have to hang up. I'll continue by text.") **Hangs up.** Texts: *"Hola, sigo con la solicitud."* |
| **Formline should** | Ask the language, give a short consent line, set the PIN, identify the SNAP application, and ask one question at a time. After the switch to text: *"Welcome back, we were on your SNAP application"* and continue with the next question, not from the start. |
| **Operator** | Nothing: Follow live jumps to Rosa. Point at the **VOICE → SMS** labels in the transcript as the switch happens. |
| **Narrator** | "Rosa has a flip phone. No app, no internet, and she's more comfortable in Spanish. She just calls. … She has to hang up, so she texts, and Formline picks up exactly where she left off." |
| **If it fails** | The call drops or voice lags: Rosa says *"Le escribo"* and does the whole step by text. The story still works. |

Rosa keeps texting the rest of the persona card: household, work, messy pay, rent, skip SSN. At read-back she sends the **rent correction**.

### Step 2: Verified PDF and receipt (≈20 s)

| | |
|---|---|
| **Formline should** | Fill the official SNAP PDF, re-read it, and only then text a receipt: what was filled, how many answers, what's missing (SSN for the caseworker), no sensitive values. |
| **Operator** | Point at the **Verified** banner, click **Download PDF**, and show the filled first page for 3 seconds. Close it. |
| **Narrator** | "Before it says 'done', the backend opens the PDF it just filled and checks every field. Then Rosa gets a receipt she can show her caseworker." |
| **If it fails** | Verification shows **Needs attention**: say so. "It caught a problem instead of pretending." That's the feature working. |

### Step 3: Rosa texts a photo of a letter (≈45 s)

| | |
|---|---|
| **Rosa does** | Photographs the printed letter and texts it with no words. Then, when offered: *"Sí, recuérdame."* ("Yes, remind me.") and *"Sí, llénala ahora."* ("Yes, fill it now.") |
| **Formline should** | Explain in Spanish: a Medicaid renewal notice, return the form by the deadline (30 days from today), coverage for her children could end. Offer a reminder, then offer to fill the renewal now, noting it already has most of her information. |
| **Operator** | The **Letter** tab opens by itself. Point at the photo next to the explanation, the **deadline countdown**, and **Related form: Medicaid renewal**. |
| **Narrator** | "She got a letter she can't easily read. She texts a photo, and Formline tells her what it is, what it wants, and the deadline, in Spanish. And it noticed the letter needs a form it can fill." |
| **If it fails** | The photo is blurry: Formline should ask for a retake, which is correct behavior; retake once. If MMS doesn't arrive within 20 seconds, the Backup sends the PNG with the simulator (Plan C) and the Operator stays on the dashboard. |

### Step 4: The renewal fills from memory (≈45 s)

| | |
|---|---|
| **Rosa does** | Says *"Sí"* to the batch confirmation ("I have your name, address, your kids Diego and Ana, and your job from before. Should I use those?") and answers whatever is still missing. |
| **Formline should** | Confirm the saved answers in one or two batches, ask only what the renewal needs beyond SNAP, read back, fill, verify, send a receipt. |
| **Operator** | Form tab: point at the **green "From memory"** fields lighting up, then the **"x% faster"** banner. Switch to **Metrics** for the big "less time" number and the two bars. |
| **Narrator** | "Second form. Formline already knows her, so instead of asking everything again it confirms. Green is memory. The first form took this long; the second took this long. Every form after the first gets faster." |
| **If it fails** | If memory reuse breaks, switch the dashboard to **Maria** (seeded returning user, Memory tab) and explain what Rosa would have seen. Plan B below. |

### Step 5 (optional finale): A brand-new form, live (≈45 s)

Only if C4/C5 are merged and rehearsed twice. Otherwise skip it and say one line about it in the pitch.

| | |
|---|---|
| **Operator** | **Forms → + Add a new form**, upload the LIHEAP energy-assistance PDF (Lane C keeps a known-good copy), name it, aliases "calefacción, heating help". Watch the seconds counter. Open it, glance at the questions, **Mark reviewed**. |
| **Rosa does** | Texts: *"Necesito ayuda con la factura de la calefacción."* ("I need help with my heating bill.") |
| **Formline should** | Match the new form and start it, prefilled from memory. |
| **Narrator** | "That form didn't exist in Formline a minute ago. Any partner can add the forms their community needs." |

## Backup plans

Decide fast: if a step hasn't worked after **one retry**, switch to the next plan without apologizing at length.

| Plan | When | What |
|---|---|---|
| **B: same phone, text only** | Voice fails or lags | Rosa does everything by SMS. Steps 1–5 still work. |
| **C: simulator on screen** | Twilio, ngrok, or the venue network fails | Backup laptop takes the projector. Start the server with `FORMLINE_DEV_ENDPOINTS=true`, open the dashboard, and in a second window run `uv run python scripts/simulate.py --server http://localhost:8000 --phone +12025550104`. Type Rosa's lines; send the letter with `/image data/demo/medicaid_renewal_letter.png`. The dashboard behaves exactly the same. |
| **D: video** | Nothing live works | Play the backup video from the final rehearsal. Narrate over it with the same script. |
| **Seeded user** | Memory reuse fails live | Show **Maria** (Demo → Seed personas): Memory tab with stale income, first-form baseline on Metrics. |

## Recording the backup video

Record at the last full rehearsal that goes cleanly.

1. On the dashboard laptop: QuickTime → File → New Screen Recording → record the full screen with the dashboard on **Live**.
2. Film Rosa's phone at the same time with a second phone on a stand (screen and speaker visible). Or skip the phone video: the dashboard transcript shows every message.
3. Run steps 1–4 exactly as scripted. Then open Metrics for 5 seconds.
4. Trim to under 4 minutes and put the phone footage picture-in-picture in a corner. Save it to the backup laptop **and** a USB stick.

## Readiness: what each step needs

| Step | Needs merged | Checkpoint |
|---|---|---|
| 1 | A1–A2 (onboarding, form engine), B2 (SMS); B5 (voice) for the call | M1 (SMS), M3 (voice) |
| 2 | A3 (fill, verify, receipt), C1 (verify), C2 (real SNAP PDF) | M1 |
| 3 | B2 (MMS download), C3 (document engine), A5 (document flow), B4 (reminders) | M2 |
| 4 | A4 (memory), C4 (Medicaid renewal schema), D2–D5 | M2 |
| 5 | C4 + C5 (fast ingestion), D4 (upload) | M3 |

If voice (B5) isn't solid by the final rehearsal, open with Rosa **texting**, and mention calling in one sentence.

## Rehearsal log

| # | Date / time | Plan used | Total time | What broke | Fixed by |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
