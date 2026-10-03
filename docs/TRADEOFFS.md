# Tradeoffs and design decisions

Format: decision, alternatives considered, why.

## Phase 0

**SQLite + SQLModel, with JSON columns for form answers and document results.**
- Alternatives: Postgres; fully normalized answer tables.
- Why: zero setup for a 24–36h build. Forms are schema-driven, so storing answers as JSON keyed by field id means a new form needs no migration.

**Added `Session` and `Message` tables beyond the brief's model list.**
- Alternatives: keep conversation state in memory; derive the transcript from `Event`.
- Why: `Session` holds per-phone conversation state in the database, so a person can hang up a call and continue by text (channel switching) and state survives server restarts. `Message` gives the dashboard a clean interleaved transcript labeled by channel.

**Form library on disk is the source of truth; the `Form` table is an index.**
- Alternatives: store schemas only in the database.
- Why: schemas are generated and then human-reviewed, so they belong in git where they can be diffed. The table speeds up matching and backs the dashboard.

**Freshness stored per fact (`freshness_days`), defaulted from a per-key policy in code.**
- Alternatives: compute freshness only from a code table.
- Why: the brief wants each value to carry its policy, and storing it per fact lets a document or partner override it for one person.

**Default models: `claude-haiku-4-5` for conversation turns, `claude-opus-5-5` for documents and schema generation.**
- Alternatives: one model for everything; Sonnet for turns.
- Why: voice latency is the tightest constraint, and Haiku is the fastest. Document understanding and schema generation happen less often and benefit most from the strongest model. Both are set through environment variables, so we can swap them after measuring.

**Python pinned to 3.11–3.13.**
- Why: 3.14 is the machine default, but some compiled dependencies (PyMuPDF, uvloop) lag new Python releases. 3.11 is installed and known good.

## Team setup

**Four lanes with frozen contracts and working stubs, instead of splitting by phase.**
- Alternatives: everyone works through the phases in order; split by feature without interfaces.
- Why: four people over ~30 hours. Phases depend on each other (the dashboard needs forms, forms need the brain), so splitting by phase leaves people blocked. Lanes own disjoint files, so merges rarely conflict. Every cross-lane call already has a stub that returns realistic data, so each lane can build and test on its own from hour one.

**A hand-written placeholder form (`forms/sample_benefits`) with a generated fillable PDF.**
- Why: lets Lanes A and C work on the full fill → verify → receipt path before the real PDFs are chosen and ingested.

**Server-side refusal fallback on for Opus/Sonnet 5.5 calls in `llm/client.py`.**
- Why: if the strong model declines a document (for example a medical bill or court notice it misreads as sensitive), the API retries on another model in the same call instead of failing the person's request.

## Lane D

**Profile keys are paths into a small set of top-level facts (`address.city`, `household_members[0].first_name`), and freshness is tracked per top-level fact.**
- Alternatives: one flat fact per form-field-sized value (`address_city`, `member1_first_name`); freshness per sub-field.
- Why: forms split the same data differently (one "address" box vs. street/city/zip boxes; three household rows vs. five), and paths let every form map onto one shape. Re-confirming a whole address when one part changes matches how people answer ("yes, that's still my address").

**`full_name` is a virtual key over `name.{first,middle,last}`.**
- Why: some forms want one name box and others want three. Splitting "Ana Maria de la Cruz" by first/last token is imperfect for multi-word surnames; the read-back gives the person a chance to correct it.

**Demo reset drops and recreates tables instead of deleting the SQLite file.**
- Why: it works while the server is running, so the dashboard's "Reset demo" button can call the same function between rehearsals.

**Seeded persona includes a completed first form 45 days ago, with income and employment deliberately stale.**
- Why: the on-stage memory metric needs a "first form" baseline, and the stale values show the re-ask-with-hint behavior.

**Dashboard: vanilla JS + SSE-triggered refetch, plus a 4-second poll.**
- Alternatives: a React/Vue build; pushing full state over the event stream.
- Why: no build step for a 30-hour project, and nothing loaded from a CDN, because venue Wi-Fi is unreliable. Events only say "something changed" and the page refetches, so missing an event can't leave the screen wrong. The poll covers anything published from another process (e.g. the simulator run in-process).

**Light theme, large type, phone numbers masked to the last 4 digits.**
- Why: projectors wash out dark themes, judges read from the back of the room, and team phones appear on screen.

**Transcript hides inbound replies that are just 4 digits.**
- Alternatives: a `sensitive` flag on each logged message, set by the brain.
- Why: PINs and SSN last-4 are typed as bare digits, and this needs no cross-lane change. It misses longer messages like "my pin is 1234". A per-message flag from Lane A would be exact; we can add it later.

**"Forget me" hard-deletes the person's data but keeps anonymized metrics events.**
- Alternatives: soft delete (a `deleted_at` flag); delete metrics events too.
- Why: a deletion the person asked for should be real, including their filled PDFs and letter photos. Metrics events keep only numbers (durations, counts) once identity is stripped, so the aggregate metrics stay honest. On a shared phone, transcript lines that were never tied to anyone are kept while another profile remains, because they may belong to that person.

## Lane B

**Twilio webhooks are set by a script (`python -m app.channels.twilio_setup`), not by hand in the console.**
- Alternatives: click through the console every time; a reserved ngrok domain only.
- Why: a free ngrok URL changes on every restart, and a stale webhook silently breaks the demo. The script can read the live URL from the local ngrok agent, repoints both webhooks in one step, and reports missing capabilities and (on trial accounts) unverified phones.
**Form upload runs ingestion inside the request (no job queue), with a live elapsed-time counter in the dialog.**
- Alternatives: background job + polling.
- Why: ingestion targets under a minute and happens a handful of times, mostly on stage. A synchronous request is one moving part instead of three. A failed ingestion removes the half-written form folder so the upload can be retried.

**The form library flags problems instead of blocking: PDF fields that don't exist, unknown memory keys, broken conditions, SSN fields not marked sensitive.**
- Why: generated schemas are drafts. The reviewer sees exactly what to fix before pressing "Mark reviewed".

**The dashboard and its API only answer requests made on the machine running Formline.**
- Alternatives: a password; leave it open.
- Why: during the demo ngrok exposes the whole server so Twilio can reach the webhooks, which would also put personal data and the "Reset everything" button on a public URL. ngrok forwards from localhost but adds `X-Forwarded-For`, so the check rejects any request that's non-local or carries proxy headers. `/twilio/*` is unaffected. `FORMLINE_DASHBOARD_REMOTE=true` turns it off for a trusted network. A password would be the production answer.

**Metrics come from the `events` table; the per-form "x% faster" banner comes from task timestamps.**
- Why: metrics follow the brief's event contract and keep working after "forget me" strips identity. The live banner only needs this person's previous task, which the tasks table answers directly. Both use the same definition: time from form start to completion.

**Transcript hides a 4-digit reply only when Formline's previous message asked for a PIN or SSN digits.**
- Why: the first version hid every bare 4-digit reply, which also hid income answers like "1450" during testing.

**PDF fill: checkbox and radio values map to the widget's real on-state; unrecognized values are left unset.**
- Alternatives: write the schema's `pdf_values` string as-is; treat any non-empty value as "checked".
- Why: real forms use "On", "1", "Y" or custom names instead of "Yes", and writing the wrong name leaves the box blank in most viewers. Mapping yes/true/on/x and the on-state itself covers schemas written without opening the PDF. A value like "sometimes" isn't guessed: it stays unchecked and `verify_pdf` reports a mismatch, so the brain re-asks instead of shipping a wrong box.

**PDF fill strips the XFA layer and sets NeedAppearances.**
- Why: many government PDFs are XFA hybrids, and Acrobat shows the (empty) XFA data instead of the AcroForm values we wrote. Regenerated appearances plus NeedAppearances make values show in Preview, Chrome and Acrobat. The cost is that XFA-only scripting (dynamic sections, calculations) no longer runs, which we don't use.

**Truncation is measured, not guessed: base-14 font widths against the widget rect.**
- Alternatives: rely on `max_length` only; render and OCR the result.
- Why: most boxes have no MaxLen, and long names and addresses are the realistic failure. Unknown embedded fonts are measured as Helvetica, which can be off by a few percent; auto-size fields count as truncated only when the text would shrink below 6pt. Multiline uses greedy word wrap at 1.15 line height.

**Required-field check skips checkboxes and conditional fields whose condition can't be read from the PDF.**
- Why: unchecked means "no", which is a valid answer; and flagging a conditional field we can't evaluate would block completion on a false alarm. A caller that wants strict checks can pass an explicit list of pdf fields instead of the schema.
**The demo uses a live new user (Rosa) whose memory is built on stage, with the seeded Maria only as a fallback.**
- Alternatives: demo memory reuse with a pre-seeded returning user.
- Why: judges see the profile being built in step 1 and reused in step 4, so the "x% faster" number is measured live, not staged. Maria stays seeded in case memory reuse fails.

**A pre-flight script checks the demo setup instead of a printed checklist alone.**
- Why: the failures that sink live demos are configuration (stale ngrok URL, Twilio webhook pointing at yesterday's tunnel, dev endpoint left on, key missing). Each is a few lines to check automatically, and the script prints the fix.

**Demo forms are all Illinois: IDHS IL444-0683 (SNAP), HFS 2378H (Medicaid), ISBE 68-06 (school meals).**
- Alternatives: the combined IL444-2378B (cash + medical + SNAP, 20 pages); USDA's prototype school meals form; other states' Medicaid renewals.
- Why: same state means the same household, income and address questions, so memory reuse is obvious on stage. All three are fillable AcroForms (two are XFA hybrids, which `fill_pdf` handles), and SNAP-only 0683 is half the length of 2378B. Illinois has no blank Medicaid renewal form (renewals are mailed pre-filled, and DC/Ohio renewals we found are flat scans), so the HFS 2378H medical benefits application stands in for "Medicaid renewal"; its aliases include "Medicaid renewal".

**The SNAP schema is 27 hand-picked fields, not the form's 367.**
- Why: a phone conversation can't ask 367 questions. We ask what decides eligibility and benefit size (household, income, rent) plus contact details, and leave the rest (immigration table, race, signature) for the caseworker. Yes/No answers map to on-states by button position, because this form names them inconsistently ("0" is Yes on most rows and No on others).

**Gaps in the SNAP schema we accepted for now.**
- The applicant's name is written once (page 1), not again in row 1 of the household table, because a schema field maps to one PDF field. An additive `also_pdf_fields` on `FormField` would fix it.
- The household member's name box isn't mapped to memory: it wants "Last, First" in one box, and memory stores first/last separately with no formatter for a list item's full name. Asked Lane D for one.
- The SSN box gets whatever Lane A writes for the last 4 (e.g. "XXX-XX-1234"); full SSNs are never collected.

**`match_form` matches whole words, longest alias first, accents ignored.**
- Why: substring matching made "EBT" match "medical debt" and "SNAP" match "snapshot". Longest-first lets a specific alias ("school lunch") beat a generic one, and accent folding lets "almuerzo gratis" match "almuerzo gratís" typed on a phone.

**`fill_pdf` writes button states as raw PDF names.**
- Why: pymupdf decodes escaped names when writing, so the school meals form's "Hispanic#2FLatino" became /Hispanic/Latino and the radio showed blank in every viewer. We set /AS and /V ourselves after pymupdf's update.

**Document engine: one strong-model call over all photos, then deterministic checks on the result.**
- Alternatives: OCR first and send text; one call per page; let the model's output stand.
- Why: vision reads layout (which date is the deadline, which number is the case number) better than OCR text, and one call keeps a multi-page letter coherent. Code then enforces what the model must not decide alone: `related_form_id` must exist in the library, Social Security numbers are dropped from reference numbers, confidence is clamped, and a keyword backstop sets `high_stakes` for eviction/court/immigration even if the model misses it (it never downgrades the model's `true`).

**Letter text is only ever inside the image; the system prompt says photos are data.**
- Why: prompt injection in a mailed letter ("ignore previous instructions") is a real risk for a tool that reads strangers' mail. Nothing from the photo is copied into a text block, so the only instructions the model sees come from us. A live test sends a letter with an injection attempt.

**Photos are downscaled to 2000 px and re-encoded as JPEG when needed; PDFs are rendered (first 5 pages).**
- Why: phone photos are often 4000 px and several MB, which is slower and costlier with no gain in readability, and MMS can deliver PDFs or formats the API doesn't take. Anything we can't open (e.g. HEIC) is reported in `unreadable_parts` so the brain can ask for a retake; with nothing readable, we skip the model call entirely.

**`explain_document` raises on API failure instead of returning a low-confidence result.**
- Why: a low-confidence result makes the brain say "the photo is blurry, please retake it", which is wrong when the real problem is the network. Lane A catches the error and apologizes instead.
**Ingestion: the model picks and words the questions; code supplies and checks everything it can read from the PDF.**
- Alternatives: one question per PDF field; let the model's draft stand.
- Why: a 580-field form can't become 580 phone questions, so choosing 12-40 is the model's job. But field names, Yes/No states, max lengths and SSN handling are facts in the PDF, so code fills them in (Yes/No from the printed "Yes"/"No" labels, then the model's guess, then layout) and checks the draft (fields exist, no box used twice, conditions point backwards, memory keys are canonical). It retries once with the problems listed, then repairs what's left (drops invented boxes, renames duplicate ids, clears bad keys) so a stage upload never fails on a bad draft. Output is always `reviewed: false`.

**The model sees each field's tooltip or printed label, not just its name.**
- Why: government PDFs name fields `TextField1[3]` or (really) `breastcancer[2]` for "wages/self-employment". Tooltips are usually descriptive; when they're the authoring tool's default, we use the words printed to the left of (or above) the box. Page text is trimmed to the start of each page to keep a 23-page form fast.

**Medicaid and school meals schemas were hand-written, not generated.**
- Why: no API key was available on the Lane C machine, and these two are demo-critical. They were built against the PDFs with the same checks ingestion uses, fill-and-verify tested, and are marked reviewed. Ingestion itself is covered by mocked tests and one live test.

**`fill_pdf` shrinks text to fit a tight box (down to 6pt) before reporting truncation.**
- Why: the Medicaid date-of-birth boxes are 47pt wide, so "03/14/1988" lost its last digit at the form's 10pt. Real forms are full of boxes like that. Shrinking keeps the full value readable; anything that won't fit at 6pt is still reported by `verify_pdf`.

**`match_form` falls back to the fast model only when no alias matches.**
- Why: aliases cover what people usually say and cost nothing; "help paying for groceries" needs language understanding. The model may only return a real form_id, and any error means "no match", so the brain asks instead of crashing.

**Form library files are read and written as UTF-8.**
- Why: Python on Windows defaults to cp1252, which garbled the Spanish aliases ("seguro médico").

**Upload-to-callable speed: send less, call the model once when possible, and make the model configurable.**
- Measured without an API key (model time is still unmeasured): reading a PDF went from 1.9s to 0.24s for the 23-page Medicaid form (one pass over each page instead of one per field), and the prompt shrank 31-64% per form (SNAP 42k -> 23k chars, Medicaid 77k -> 53k, school meals 15k -> 5k).
- How: the model sees short handles ("F12") instead of XFA names like `form1[0].#subform[6].TextField4[0]` (code maps them back, which also stops typos), and only the first two rows of a repeating table (fields whose labels differ only by "#3" or "third"). A field whose label has no row number is never hidden.
- A second model call happens only when repairing the draft would drop more than a quarter of its questions; otherwise the instant repair wins, since a retry doubles the wait on stage.
- `FORMLINE_INGEST_MODEL` (shared config, additive) picks a faster model for drafting without touching the strong model used for letters. Default stays the strong model until someone times both.
- Not done: vision-based filling of flat (non-fillable) PDFs, the C5 stretch goal. Flat, XFA-only and password-protected PDFs get a specific error instead.

**Twilio retries are deduplicated in memory by `MessageSid`, not in the database.**
- Alternatives: a `processed_sid` table or a column on `Message`.
- Why: retries arrive seconds apart, so a bounded in-memory set catches them without a shared `models.py` change. A restart between a message and its retry could double-process it; that's acceptable for a demo.

**A bad or missing Twilio signature gets a bare 403; every other failure gets a 200 with an apology text.**
- Why: a forged request shouldn't trigger a reply or reveal anything, while a real person must never be left with silence because the brain or a media download failed. A failed photo download is skipped (the turn still runs) rather than failing the whole message.

**Tests run with Twilio credentials blanked and signature validation off (`tests/conftest.py`).**
- Why: real credentials in a developer's `.env` must never make a test send a real text, and other lanes' tests post unsigned webhooks. `tests/test_channels_messaging.py` turns validation back on to test it.
**Demo rehearsal fills all three forms for the seeded persona, from memory, in the test suite.**
- Why: it's the on-stage path end to end (Lane D's seed -> memory -> schema `profile_key` -> PDF -> verify) and it caught a real bug: the seeded SNAP case number `IL-SNAP-448120` is 14 characters, but the school meals box holds 9, so it would print cut off. The seed now uses `448120917`, and the schema validates the case number (up to 9 letters or digits) so a longer one is re-asked. `FORMLINE_KEEP_DEMO_PDFS=1` keeps the filled PDFs in `data/demo/filled/` for eyeballing.

**`app/pdf/format.py`: `pdf_value(field, answer)` turns stored values into what paper forms expect.**
- Why: memory stores ISO dates and E.164 phones, so the Medicaid PDF showed `+12025550101` and dates as `1988-03-14`. US forms want `(202) 555-0101` and `03/14/1988`, money without "$", and the form's own checkbox states. Lane A's completion step can call it per field.

**Option labels are read from whichever side the form prints them.**
- Why: the SNAP and Medicaid forms print "[ ] Yes [ ] No"; the SNAP renewal prints "Yes [ ] No [ ]". Reading only the word right of a button mapped the second layout backwards. If the word just left of a group's leftmost button is Yes/No/Sí, labels are on the left.

**The finale form is the 3-page IDHS SNAP Redetermination (IL444-1893), kept in `forms/_new_form_demo/` without a meta.json.**
- Alternatives: a county LIHEAP application (12 pages, a scanned page with boxes laid over it).
- Why: it's clean, short (about a 3k-token prompt), Illinois, and overlaps the SNAP application, so the newly added form fills mostly from memory on stage. Without meta.json the library ignores it until it's uploaded.

**Voice: the relay websocket is authorized with a one-time token issued by the signed `/twilio/voice` webhook.**
- Alternatives: validate `X-Twilio-Signature` on the websocket upgrade; leave the websocket open.
- Why: the call webhook is already signature-checked, and a random single-use token (2-minute expiry) in the relay URL ties each websocket to a real call without depending on undocumented upgrade-signing details.

**Voice: the brain gets an empty-text turn when a call connects; Twilio speaks a short fixed greeting first.**
- Alternatives: only a static `welcomeGreeting`; wait for the caller to speak.
- Why: the greeting plays instantly while the brain thinks, and the brain still owns the real opening ("welcome back, we were on question 6"). Callers with a Spanish profile get the Spanish greeting and Spanish speech recognition from the first second.

**Voice: to hang up after the goodbye, wait about as long as the reply takes to say, then send `end`.**
- Why: ConversationRelay's `end` cuts speech off immediately. ~150 words a minute, capped at 15 s.

**Voice: keypad digits are buffered and sent as one turn on `#`, at 4 digits, or after a 2-second pause.**
- Why: a PIN typed on the keypad arrives as four separate `dtmf` messages; the brain should see "1234", and a single "1" for a menu choice still goes through after the pause.

**PIN lockout: 3 wrong attempts in a row lock the profile until a partner resets it; no timed unlock.**
- Alternatives: a 15-minute cool-off; lock per session instead of per profile.
- Why: with only 10,000 PINs, a timed unlock still lets someone holding the phone keep guessing. Per profile (not per session), so switching from a call to a text doesn't reset the count. A correct PIN clears the count. Risk: a person who fumbles the PIN three times is stuck until a partner clicks Reset PIN, which is the partner-only reset the brief asks for.

**Lockout counts live in a new `PinGuard` table with no foreign key, not in new `Profile` columns.**
- Why: `create_all` adds new tables to existing databases but never adds columns, so new columns would have forced every teammate to delete `data/formline.db`. Without a foreign key, "forget me" and demo reset keep working unchanged; the table holds only counts and timestamps.

**Switching the person on a shared phone (`select_profile`) drops the PIN verification and active task.**
- Why: James's verified PIN must never unlock Denise's details, and James's half-filled form isn't Denise's. The conversation `state`/`pending` stay with the brain, which decides what to say next.
**Reminders: an in-process APScheduler job checks every minute; sending first claims the row (`pending` → `sending`).**
- Alternatives: a separate worker process; Twilio's own scheduled messages.
- Why: one process is simplest for a demo laptop, and a minute's precision is plenty for deadline reminders. The atomic claim means the scheduler and the dashboard's "Send now" can never text the same reminder twice. The first check runs a minute after startup, which also keeps the job from firing inside short-lived test apps.

**A reminder that fails to send is marked `failed`, not retried.**
- Why: if Twilio rejects a number (opted out, unregistered sender), retrying every minute would hammer it. The failure is logged as activity so a partner sees it, and "Send now" can't resend it by accident.

**Reminder texts are scrubbed of anything shaped like a full SSN, both when saved and when sent.**
- Why: reminder wording can come from a letter the person photographed. Case numbers and dates stay, since the person needs them.

**Voice: say "One moment." when the brain takes over 2.5 s, and apologize at 25 s instead of waiting.**
- Why: on a call, silence feels like a dropped line and people hang up. The `voice_latency` metric still measures the time to the real answer, not to the filler, so it stays honest.

**Call recording for the backup video is opt-in (`FORMLINE_RECORD_CALLS`), and the greeting announces it.**
- Why: Illinois requires every party's consent to record a call, and recordings hold voices and personal details. Only team phones should be on a call while it's on.

**When the laptop or ngrok is down, Twilio's fallback URL points at a Twilio-hosted TwiML Bin.**
- Why: our own fallback (`/twilio/voice/status`) can't help if our server is unreachable. A TwiML Bin lives on Twilio, so callers still hear "Formline is offline, try again in a few minutes" instead of Twilio's generic error.

## Pivot: call the internet

**Formline operates the person's own browser through a Chrome extension, instead of filling a fixed set of PDFs.**
- Alternatives: keep the form-schema product; a server-side headless browser that logs in for the person.
- Why: the extension acts in the browser where the person is already signed in, so Formline never sees passwords or cookies, works on any site, and the person watches it happen. The form/PDF assistant stays as-is for callers without a paired browser.

**The model sees a semantic text snapshot of the page, not screenshots or HTML.**
- Alternatives: screenshots + vision (click by coordinates); raw HTML.
- Why: a snapshot of controls with accessible names is small (fast and cheap per decision), works with any language model, and lets the extension redact secrets before anything leaves the browser. Screenshots can't be redacted, so they're an opt-in fallback for nearly empty pages only (`FORMLINE_VISION_FALLBACK`).

**Actions name temporary element ids from the snapshot; the model never writes JavaScript or selectors.**
- Why: a fixed vocabulary (click, type, select, check, …) is checkable. Ids carry a per-document `doc_id`, so an id from a previous page is refused rather than hitting a different element that happens to share it.

**Consequential steps are gated three ways: the model's `confirm`, a deterministic backstop, and a server-stored pending action with a page fingerprint.**
- Alternatives: trust the model to ask; a fixed list of selectors per site.
- Why: models forget to ask. The backstop catches final verbs (book, send, pay, delete, cancel <thing>) and any button on a review page, while letting "Schedule an appointment" (which starts a flow) through. The fingerprint covers page text too, so a "yes" given to one summary can't book a different one; a site with live-updating text just gets asked again.

**Success is only reported with quoted evidence found on a fresh snapshot, and only after a confirmed final step actually ran.**
- Why: "the model asked to click Book" is not "the appointment is booked". Matching ignores case, punctuation, and snapshot markup, so a quote spanning a heading and the line below it counts.

**The agent loop runs in the background of the call, with an inbox for speech that arrives mid-task.**
- Why: "stop" must work while the agent is mid-step. A reply to a question the agent just asked is treated as the answer, not as mid-task chatter.

**Agent model: gpt-5.4-mini (low reasoning) when using OpenAI.**
- Alternatives: gpt-4.1-mini, gpt-4.1, gpt-5-mini.
- Why: measured on the recorded prompts that failed (scripts/agent_bench.py): gpt-5.4-mini chose correctly every time at ~0.8–1 s; gpt-4.1-mini asked ahead and mangled ids, gpt-4.1 used the wrong action for a radio button, gpt-5-mini was 2–7 s. OpenAI support was added to `llm.client` because this machine had only an OpenAI key; the provider is picked by which key is set, so Anthropic users see no change.

**Pairing proves the phone with a code sent by text or by voice call, then issues a random browser token (stored hashed). The PIN is still required on each call.**
- Why: landlines can't receive texts but can answer a call. Caller ID only identifies the browser; it can be spoofed, so the PIN authorizes control (reusing the existing 30-minute verification and 3-strike lockout).

**The agent waits out loading indicators before deciding.**
- Why: in the first live run the model saw a "Loading…" placeholder and gave up. Waiting on `aria-busy`, loading text, and disabled "…ing…" buttons (up to ~6 s) is generic and fixed it.

**Texts from a paired phone drive the browser too, with replies sent as separate texts.**
- Why: deaf and hard-of-hearing callers, and the demo's text-only backup. Browser work can outlast Twilio's webhook timeout, so replies go out by REST, not in the webhook response.

**End-to-end tests drive Playwright's Chromium with a model playing the caller.**
- Alternatives: keyword-matched caller replies; Chrome stable.
- Why: Chrome 137+ ignores `--load-extension`, so automated runs use Chromium. Keyword replies broke whenever the agent phrased a question differently; a persona-driven caller answers like a person would, and the run passes only if the site's own state shows the goal was done.

**Browser tables (pairing, installations, tasks, actions) have no foreign keys to Profile.**
- Why: same reason as PinGuard: existing databases pick up new tables without a reset, and "forget me" deletes these rows itself.

**PDFs are read whole: the extension downloads the file from the tab and the server extracts every page.**
- Alternatives: read Chrome's PDF viewer like a page (it's opaque to snapshots); screenshots of the visible page; have the server fetch the URL.
- Why: in a live call the agent saw nothing of a lease and couldn't answer. Fetching from the page itself carries the person's session (PDFs behind a login work) without the server ever seeing cookies, and text extraction gives every page, not just the visible one. Scanned PDFs (no text layer) fall back to pictures of their first 8 pages. The document goes first in the prompt so follow-up questions reuse the provider's prompt cache (answers took about 1 s).

**A new decision kind, `answer`, for questions about the page or document, with quoted evidence.**
- Alternatives: reuse `done` (ends the task); answer without evidence.
- Why: questions come in a series ("can I have a dog?", "how do I get out early?"), so the conversation must stay open. Requiring a quote that code finds in the page keeps answers grounded in the person's own document. A summary may stitch several quotes; every piece of 3+ words must be real.

**The agent can no longer type web addresses (`navigate` removed from its vocabulary).**
- Why: in a live call, "look up a movie" jumped straight to the previous request's film by URL: nothing visible happened and nothing was searched. Using the site's own search and links is what the person expects to see. Requests for the person's own things (appointments, loans, orders) go to the account area instead of search, which kept the library flow at 4 steps.

**A new request starts with a note not to reuse details from earlier requests.**
- Why: the conversation is kept for context ("that one", "the other time"), but a vague new request ("look up a movie") must be clarified, not filled in from an old one.

**Evidence is checked against exactly the text the model was shown.**
- Why: the model quoted the page title as it appears in the snapshot ("Title: …", with an invisible direction mark) and was rejected three times on a page that did prove success.

**Icon-only buttons get a guessed name from class names, ids and icon references, marked "(icon)".**
- Why: a magnifying-glass button with no text or label was invisible to the model, so it couldn't open a hidden search box.

**Site names skip bot-check titles and prefer the part of the title that matches the address.**
- Why: a Cloudflare check made the greeting say "I can see you have Just a moment... open".

**PDFs are copied when the tab opens, because Chrome's viewer can't be read and portal links expire.**
- Alternatives tried (all measured in Chromium): re-downloading on demand (an AppFolio/S3 link returned 403 minutes later); Chrome's HTTP cache (works only when the file has no Cache-Control; no-cache and no-store, which portals use, get 403); the viewer's select-all/get-text commands (its embed is in a closed shadow root and its frames belong to another extension, so they can't be reached); printing the tab through the DevTools protocol (prints the toolbar, one page); jumping pages with #page=N for screenshots (the viewer ignored it).
- Why: a copy fetched in the first second, while the link is fresh, makes every later question work. It stays in that tab's memory and leaves the browser only when the person asks about the document. Without a copy (a PDF opened before the extension loaded), the visible part is captured and the agent says that's all it can see.

**The agent follows the person when they switch tabs.**
- Why: in a live call the person moved from a PDF to Letterboxd and asked "can you see Letterboxd?"; the agent was pinned to the PDF tab. The extension already reports the active tab (excluding Formline's own dashboard), so that is the page.

**Names for controls with no text come from tooltip attributes, a neighbouring image, or the link's address; "icon" guesses only for small elements.**
- Why: Letterboxd's film posters are text-less links with a `has-menu` class next to an image; the agent saw "menu (icon)" and clicked a film thinking it was a menu. Poster tooltips (`data-original-title`) and `/film/the-ritual-2017/` say what they are.

**Search boxes are recognised by name, id, class, placeholder or form, and a hidden one is mentioned.**
- Why: many sites use a plain text input named `q`, often hidden until a magnifying glass is clicked. The agent clicked "More..." seven times looking for one.

**The same click on an unchanged page is refused the third time.**
- Why: that "More..." loop. "Unchanged" means the same address and headings, so a wizard's "Next" on each new step is never refused (the first version of this guard keyed on the label alone and broke the booking flow).
