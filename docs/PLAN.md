# Master build plan

Four lanes build in parallel across four stages. Each stage ends at a **checkpoint** where the team merges, runs the checkpoint demo together, and fixes anything broken before moving on. Phases inside a stage are ordered so that anything another lane needs is delivered first.

Each lane's full brief: [Lane A](lanes/LANE_A.md) · [Lane B](lanes/LANE_B.md) · [Lane C](lanes/LANE_C.md) · [Lane D](lanes/LANE_D.md)

| Lane | Owner | Focus |
|---|---|---|
| A | @aryavsaigal | Conversation brain: `handle_turn`, form-filling conversation, memory use, run-back |
| B | @Edoubek1024 | Channels + identity: Twilio SMS/MMS/voice, PIN, shared phones, reminders |
| C | @luisNava111 | Forms, PDFs, documents: form library, fill/verify, document explanations, ingestion |
| D | @veed2005 | Memory, dashboard, metrics, demo; integrator for shared files and checkpoints |

## Timeline

Hours are approximate for a ~30-hour build. If a stage runs long, cut scope inside the stage rather than skipping the checkpoint.

### Stage 1 (hours 0–8): one form, end to end, over real SMS

| Order | A | B | C | D |
|---|---|---|---|---|
| 1st | **A1** Onboarding + router | **B1** Twilio number, ngrok, webhooks live *(first hour)* | **C1** PDF toolkit: inspect, fill, verify hardening | **D1** Canonical profile keys + seed/reset scripts *(first hour: C and A need the key list)* |
| 2nd | **A2** Form engine on `sample_benefits` | **B2** Signature validation, MMS download, robust webhook | **C2** Choose the 3 real demo PDFs; hand-write the SNAP schema | **D2** Dashboard v1: people, live transcript, fields by source |
| 3rd | **A3** Completion: fill → verify → receipt | | | |

**Checkpoint M1:** someone texts the real number from their own phone, completes `sample_benefits`, and receives a receipt. The dashboard shows the transcript and the fields filling in, color-coded by source. A verified PDF downloads.

### Stage 2 (hours 8–16): real forms, documents, memory

| Order | A | B | C | D |
|---|---|---|---|---|
| 1st | **A4** Memory: prefill, batch confirm, stale re-ask, PIN gate, status, "forget me" | **B3** Identity: shared phones, PIN lockout and reset, channel-switch events | **C3** Document engine (vision) + fake sample letters | **D3** Memory hardening + profile view |
| 2nd | **A5** Document flow: explain → reminders → offer related form | **B4** Reminders: scheduler + send now | **C4** Ingestion: `ingest_pdf`, schemas for all 3 forms | **D4** Document view, verification, form library view + upload |

**Checkpoint M2:** a returning persona texts a photo of a Medicaid renewal letter, gets it explained with its deadline, accepts a reminder, and fills the renewal mostly from memory. The dashboard shows "from memory" fields and the document next to its explanation.

### Stage 3 (hours 16–24): voice and metrics

| Order | A | B | C | D |
|---|---|---|---|---|
| 1st | **A6** Voice-ready brain, channel-switch greetings, scenario tests | **B5** Voice via ConversationRelay | **C5** Upload-to-callable in about a minute; schema review tooling | **D5** Metrics panel + demo controls |

**Checkpoint M3:** call from a phone, start the SNAP application in Spanish, hang up partway, finish by text. The metrics panel shows first-form vs. later-form time.

### Stage 4 (hours 24–end): demo hardening (everyone)

| A | B | C | D |
|---|---|---|---|
| **A7** Latency, LLM-failure fallbacks, conversation polish | **B6** Telephony fallbacks, demo phones, call recording for backup | **C6** Pre-generated schemas, all 3 forms fill perfectly with seed personas | **D6** DEMO_SCRIPT.md, PRIVACY.md, backup video, rehearsal |

**Final:** two full rehearsals of the on-stage flow in Section 15 of the brief, plus one run of the backup plan.

## Cross-lane handoffs

The phase that delivers each dependency comes before the phase that needs it. Until then, the consumer builds against the existing stub.

| Needed by | What | Delivered by |
|---|---|---|
| A2, C2, C4 | Canonical profile key list (`FRESHNESS_POLICY` in `app/memory/profile.py`) | **D1** |
| A3 | Hardened `fill_pdf` / `verify_pdf` with truncation and checkbox handling | **C1** |
| A3, M1 | Real SMS in and out | **B1, B2** |
| A4 | PIN and shared-phone storage that works | already working; **B3** hardens it |
| A5 | Real `explain_document` | **C3** |
| A5 | `reminders.create_reminder` | already working; **B4** adds the scheduler |
| A2 (stage 2) | Real SNAP / Medicaid / school-meals schemas | **C2** (SNAP by hand), **C4** (all three) |
| B5 | `TurnResult` reply text short and voice-styled | **A6** |
| D2, D4 | `Task.answers`, `field_filled`, `document_explained`, and `verification` events published | **A2, A3, A5** |
| D4 | `ingest_pdf` working | **C4** |
| D5 | Metrics events logged with the names in `docs/TEAM.md` | **A2–A5, B3, B5** |

## Rules for every phase

1. Branch from fresh `main`: `git checkout main && git pull && git checkout -b <lane>/<phase>-<what>`.
2. Build, then add tests in your lane's test files.
3. `uv run pytest` passes → push → `gh pr create --fill` → CI green → squash-merge.
4. Post a 2–3 line summary in the team chat: what merged and how to try it.
5. Design decisions go in `docs/TRADEOFFS.md`. Add your entry at the end of the file to avoid conflicts.
