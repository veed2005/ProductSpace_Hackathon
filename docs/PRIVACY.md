# Privacy and data handling

Formline handles exactly the information people worry about most: income, household, addresses, benefits letters. This page says what the prototype stores and why, how identity works and where it falls short, and what a real deployment would need.

**Prototype rules:** fake personas only, no full Social Security numbers, and nothing is submitted to any government system. Completed forms end as verified PDFs for a partner organization to review.

Status key: **Built** = in the code today. **Planned** = assigned to a phase, not merged yet. **Production** = not in the prototype.

## What we store, and why

Everything lives in one SQLite file and a data folder (`data/`, never committed to git) on the machine running Formline.

| Data | Why we keep it | Where |
|---|---|---|
| Phone number | Recognize a returning person; one phone can belong to several people | `profile`, `session`, `message` |
| Saved details: name, birthday, address, household, job, income, rent, utilities, case numbers | Fill the next form faster by confirming instead of re-asking | `profilefact`, each with its source, when it was last confirmed, and how long it stays fresh |
| PIN (hashed) | Check it's really the person before reading back or reusing their details | `profile.pin_hash` |
| Form answers in progress | Resume after a hang-up or a switch from call to text | `task.answers` |
| Filled PDFs | The finished product, for the partner organization | `data/filled/` |
| Photos of letters + their explanation | Show the partner what the person received; save deadlines and case numbers | `data/media/`, `document` |
| Transcript of calls (as text) and texts | Partner view of what happened; debugging | `message` |
| Activity log | "What have you done for me?" and the run-back receipts | `activity` |
| Metrics events (durations, counts) | Measure whether Formline is working | `event` |
| Reminders | Text the person before a deadline, with their permission | `reminder` |

**Sensitive values.** Only the last 4 digits of an SSN are ever collected, and the person can skip it ("to be provided to caseworker"). Fields marked `sensitive` are masked on the dashboard (`••••`) and must never appear in texts. Receipts mask them (Lane A, Phase A3).

**Freshness.** Details expire on a schedule so Formline re-checks instead of reusing stale data: income and job after 30 days, household after 90, address after 180, birthday never (`CANONICAL_KEYS` in `app/memory/profile.py`).

## Identity and the PIN

Caller ID can be faked, so **knowing someone's phone number is not enough to hear their information.**

- **Built:** a 4-digit PIN is set on first contact and is required before Formline reads back stored details or reuses them on a form. It's stored as a salted PBKDF2-SHA256 hash (100,000 iterations), never in plain text. A correct PIN counts for 30 minutes in that conversation.
- **Built:** shared phones. A phone can hold several profiles, and Formline asks who's calling. A PIN unlocks only that person's profile.
- **Built:** only a partner can reset a PIN, from the dashboard (Demo → Reset PIN). There's no self-service reset, which would hand control to whoever holds the phone.
- **Planned (Lane B, Phase B3):** lock the PIN after 3 wrong attempts.

**Limits, stated plainly.**
- A 4-digit PIN has 10,000 combinations. Without the planned lockout, someone with the phone could eventually guess it. With lockout and a partner-only reset, guessing becomes impractical.
- Anyone holding the person's unlocked phone during those 30 minutes can continue the conversation.
- A family member who knows the PIN can see everything in that profile. On shared phones, separate profiles and PINs are the protection.
- The PIN protects reading and reusing data. It does not encrypt the data.

## "Forget me"

The person can say "forget me" or "delete my info" at any time. Formline confirms, then (**built**, `memory.forget_profile`):

- **deletes** their saved details, forms in progress and completed, filled PDFs, letters and their photos, reminders, activity log, and their transcript;
- **keeps metrics events with identity removed** (no phone number, profile, or form link: only durations and counts), so aggregate numbers stay honest;
- **leaves anyone else on a shared phone untouched.** Transcript lines that were never tied to a specific person stay while someone else still uses that phone.

## Who else sees the data

| Service | What it receives | Notes |
|---|---|---|
| **Twilio** | Text messages, letter photos (MMS), and call audio, which its speech service turns into text | Twilio stores messages and media under its own retention settings. Production should delete media from Twilio after downloading it and turn on message redaction. |
| **Anthropic (Claude API)** | Conversation turns, letter photos, and the text of form PDFs being added to the library | Covered by Anthropic's commercial API terms. Production should confirm the retention terms fit (for example, a zero-data-retention arrangement) and sign a BAA before handling Medicaid health information. |
| **ngrok** (development and demo only) | Proxies Twilio's webhooks to the laptop | Not part of a real deployment. |

**Letters are data, never instructions.** Text inside a photographed letter can't change what Formline does. A letter that says "ignore previous instructions" is explained like any other letter (Lane C, Phase C3, with a test).

## The partner dashboard

- **Built:** the dashboard and its API only answer requests made on the computer running Formline. Requests through ngrok are refused, even though ngrok must expose the server so Twilio can reach it. Twilio's webhooks are unaffected.
- **Built:** projector-safe display. Phone numbers show only their last 4 digits, sensitive fields are masked, and a 4-digit reply right after a PIN or SSN question is hidden in the transcript.
- **Production:** real sign-in for partner staff, per-organization access (a food bank sees only its own clients), and an audit log of who viewed or downloaded what.

## Known gaps in the prototype

- **No encryption at rest.** The SQLite file, filled PDFs, and photos sit unencrypted in `data/`.
- **No retention limit.** Nothing is deleted automatically; data stays until "forget me" or a demo reset.
- **Logs contain personal data.** Server access logs include phone numbers in dashboard URLs. In offline mode (no Twilio credentials), outgoing texts are written to the log.
- **Webhook signature validation** is configured on by default, but its check is still being built (Lane B, Phase B2). Until then, requests to `/twilio/messaging` aren't authenticated.
- **Transcript masking is best-effort.** A PIN typed inside a sentence ("my pin is 1234") isn't hidden. Exact masking needs the brain to flag sensitive messages when it logs them.

## What production would need

1. **Encryption at rest** for the database and files, with keys managed outside the app (e.g. a cloud KMS).
2. **Retention limits.** Delete transcripts and photos after a set period (for example, 90 days after the last contact), and filled PDFs once the partner confirms submission. Purge Twilio media right after download.
3. **A reviewed consent script** in every supported language, played or texted on first contact and stored with a timestamp (`profile.consent_at` exists for this).
4. **Partner accounts with audit access:** sign-in, roles, per-organization data separation, and a log of every view, download, and PIN reset.
5. **PIN hardening:** lockout and rate limiting (planned), optional longer PINs, and alerts on repeated failures.
6. **Agreements and compliance:** confirmed data terms with Twilio and Anthropic (zero retention where available), BAAs where Medicaid health information is involved, US A2P 10DLC or toll-free registration for texting, and a privacy notice reviewed by counsel.
7. **Redacted logs:** no phone numbers in URLs or logs, and structured logging that masks sensitive fields.
8. **Incident response:** a named owner, a breach-notification plan, and regular backups that are encrypted too.

## Draft consent script

To be finalized with legal review (production item 3). Short enough to speak on a call or fit in two texts.

> **English:** "Formline helps you fill out forms and understand letters. We'll save what you tell us so next time is faster, and protect it with a 4-digit PIN you choose. We don't send anything to the government. A helper organization reviews your forms. Say 'forget me' any time to delete your information. Is that OK?"
>
> **Español:** "Formline le ayuda a llenar formularios y entender cartas. Guardaremos lo que nos diga para que la próxima vez sea más rápido, protegido con un PIN de 4 dígitos que usted elige. No enviamos nada al gobierno. Una organización de ayuda revisa sus formularios. Diga 'olvídame' cuando quiera para borrar su información. ¿Está bien?"

## The browser agent ("call the internet")

**Built.** The Chrome extension can see whatever page the person has open, so it sends as little as possible:

- **What leaves the browser:** a text snapshot of the current tab: control names and states, headings, short text. Password fields, hidden inputs, and fields that look like card numbers, SSNs, PINs or one-time codes are listed without their values; SSN- and card-shaped numbers are masked in all text; query strings are removed from addresses. Cookies, storage, and passwords are never read. The server scrubs every snapshot a second time.
- **What Formline stores:** each browser task's goal, status, and action log (which button, what was typed into which field; values typed into secret fields are refused, never stored), plus the paired browser (a hashed token, a label like "Chrome on Windows"). "Forget me" deletes all of it.
- **Screenshots:** off by default (`FORMLINE_VISION_FALLBACK`), because a screenshot can't be redacted.
- **Who can drive the browser:** only a caller from the paired number who also enters the PIN (or did within 30 minutes). Caller ID alone is never enough.
- **Debug trace:** `FORMLINE_AGENT_TRACE` writes prompts (page text and what the caller said) to a local file. Only for development; never on a shared machine.

**Production would need:** consent text in the extension that names what is read, per-site allow lists chosen by the person, retention limits for task logs, and an audit view for the person themselves.
