# Sarthi — Architecture (draft, updated 2026-11-21)

Sarthi watches every job source all day, finds jobs that fit you, messages you on Telegram,
applies when you say yes, and tells you when companies reply.

Built from 13 audited open-source repos (all repos equal weight). Rule for every part:
**if a repo already does it best → copy it. If nobody does it well → we build the better version (+1).**

Language: **Python** (most reusable code is Python). TypeScript/JS ideas are ported.
Storage: **one SQLite file**. Runs on the user's machine in **Docker**. Licence: **AGPL-3.0**.

Release plan: **one free launch (v1)** with everything tagged below, then **Sarthi Pro** (paid, §4.2) built
alongside while the free version is marketed. Tags: **[v1]** = in the free launch · **[Pro]** = paid, after launch.

---

## 1. The whole flow

```
 ┌──────────────────────────── SOURCES (150+) ────────────────────────────┐
 │ Company job systems │ Job boards │ Free job feeds │ Freehire │ Telegram │
 └────────────────────────────────┬────────────────────────────────────────┘
                                  ▼
                      SCHEDULER + TASK QUEUE  (when to check what)
                                  ▼
                 NORMALIZE → one job format (+ country codes)
                                  ▼
                 DEDUPE  (same job from 5 sites = 1 job)
                                  ▼
             QUALITY GATE  (still open? scam? repost? visa ok?)
                                  ▼
          MATCH FUNNEL  (free filter → hard rules → AI score → "why")
                                  ▼
          DECIDE  (hide / instant alert / daily digest / auto-apply)
                                  ▼
 ┌────────────── TELEGRAM BOT  ⇄  USER  (Apply · Skip · Later) ──────────────┐
 └───────────────────────────────────┬──────────────────────────────────────┘
                                     ▼
       DOCUMENTS (tailored resume + cover letter + form answers, truth-checked)
                                     ▼
       APPLY ENGINE (rule-based → replay → AI browser → hand to user)
                                     ▼
       TRACKING (inbox replies → interview / rejection / ghosted → Telegram)
```

Everything is a task in the SQLite queue, so a crash loses nothing and each step retries on its own.

---

## 2. Each part: who does it best, what we copy, what we add

### 2.1 Sources [v1]
| Source group | Best existing code | Copy |
|---|---|---|
| ~100 company job systems (Workday, Greenhouse, Lever, iCIMS, Taleo, SuccessFactors, Darwinbox, Keka…) + 173k-company list | **ATS-Scrapers** | Use as library + its `ats-companies/*.csv` |
| LinkedIn, Indeed, Naukri, Glassdoor, Google Jobs, ZipRecruiter, Seek, Bayt, BDJobs | **JobSpy** | Library |
| Adzuna, Jooble, Wellfound, Dice, Hiring Cafe, The Muse, TrueUp, Handshake, CareerBuilder… | **JobDrop** | Library (extras only) |
| Remotive, Jobicy, Himalayas, Arbeitnow, Working Nomads, RemoteOK, WeWorkRemotely | **StillOpen** `scan.py` (Python, zero-token) | Copy connectors |
| Freehire (one search = ~90 job systems) | **JobRadar** `sources/boards/freehire.py` | Copy, replace its India-only filters with user prefs |
| Hacker News "Who is hiring", any careers page | **ApplyPilot** `hackernews.py`, `smartextract.py` | Copy |
| Telegram job channels (live) | **JobRadar** `sources/telegram.py` | Copy |
| India mass-hiring pages (TCS, Infosys, Wipro, Cognizant) | **JobRadar** `sources/pages/watcher.py` | Copy |

Common plug-in shape (JobRadar's design): every source is one module with `fetch() -> list[RawJob]`.

**Implemented:** `sources/ats.py` (ATS-Scrapers, one source per company board), `sources/boards.py` (JobSpy
public `scrape_jobs`; Naukri added for India, Seek for Australia), `sources/ingest.py` (store, quiet first-run
baseline, alert-priority `match` tasks). ATS-Scrapers is installed **without** its `[scrapers]` extra: that extra
downloads a stealth Chromium binary; without it ~93 of 99 hiring systems work and Meta/Tesla boards are unavailable.
`sources/feeds.py` (StillOpen connectors: Remotive, RemoteOK, We Work Remotely, Himalayas, Jobicy, Arbeitnow, Working
Nomads; one request each, no key), `sources/hackernews.py` ("Who is hiring?" threads via the Algolia HN API),
`sources/freehire.py` (JobRadar port: one search per role over ~90 hiring systems, filtered by the user's countries and
remote preference). A remote job is "open worldwide" only when its location names no restriction; a time zone or a
city Sarthi cannot place is shown as "check it includes you" instead of being read as anywhere.

**+1 (nobody does this):**
- **Smart polling:** each source gets its own speed. Telegram live, company job systems every 5–30 min, LinkedIn/Naukri slower to avoid blocks. Sources that often post new jobs get checked more.
- **Focus list:** from 173k companies, check often only the ones matching the user's roles + countries; rest covered by Freehire + board searches.
- **Source health:** a broken source is auto-detected and paused, and the user is told (idea from Career-Ops `dead-boards`).
- **Paste any careers link** → Sarthi auto-detects its job system (ATS-Scrapers `get_scraper_for_url`) and starts watching. Safe-URL check from StillOpen `netguard.py`.

### 2.2 Scheduler + task queue [v1]
- **Copy JobRadar** `queue/tasks.py` + `queue/worker.py`: SQLite task table, retries with backoff (30s → 2m → 10m → 1h), never runs a step twice.
- **Copy JobRadar** board watcher rule: first check of a new source = save only, no alerts (no 500-message flood).
- **+1:** priority lanes — "new job alert" tasks always run before slow tasks (tailoring, applying), so alerts are never stuck behind work.

### 2.3 One job format [v1]
- **Base model: ATS-Scrapers `Job`** (pydantic): has `country_iso` per location, salary with ISO currency, skills, education.
- **Add StillOpen `geo.py`** (copy as-is, no internet needed): turns "Bengaluru", "Remote (EU)", "London or remote UK" into country codes.
- **Add fields:** `remote_type` (onsite/hybrid/remote), `remote_countries` (empty = anywhere), `visa_sponsorship`, `first_seen_at`, `sources[]`.
- **Copy OpenApply "lean index":** store a 700-char snippet per job, fetch the full text only for jobs that pass matching → DB stays small (150k jobs ≈ 300 MB).

### 2.4 Dedupe [v1]
- **Copy JobRadar:** clean-URL hash + company/role/location fingerprint; extra sources attach to the same job.
- **Copy Openings `duplicates.py`** (no dependencies): requisition id + title/company similarity signals.
- **Copy OpenApply `prior.ts` logic (port):** "you already applied to this company for a similar role" guard.

### 2.5 Fill missing details [v1]
- **Copy JobRadar layered extraction:** page's built-in job data (JSON-LD) → labelled lines → AI only if role/company still missing. Saves most AI calls.
- **ApplyPilot `enrichment/detail.py`** for full description pages.

### 2.6 Quality gate [v1]
| Check | Best code | Copy |
|---|---|---|
| Still open? | **JobRadar** `liveness.py` (asks Greenhouse/Lever/SmartRecruiters/Workday directly) | Copy |
| Scam? | **JobRadar** `scam.py` (no deps) | Copy as-is |
| Repost / ghost job? | **Career-Ops** `detect-reposts.mjs` | Port |

### 2.7 Match funnel [v1] — cheapest first
| Layer | Cost | Best code |
|---|---|---|
| L0 Fast filter: title, keywords, location, salary, excluded companies | Free | **OpenApply** `match.ts` (port) |
| L1 Hard rules: country/visa eligible, years of experience, seniority | Free | **JobRadar** `match.py` hard rules + our `geo.py` |
| L2 AI score: set questions (eligible? role fit? skills? seniority? location?) → score 0–100 computed in code | 1 small AI call | **JobRadar** design (but local/any LLM instead of its paid Jev service) |
| L3 "Why it fits": each requirement → where your resume proves it | Free | **StillOpen** `matchcard.py` (port logic) |

ApplyPilot (AI 1–10 score for every job) is the most expensive approach — we only use AI at L2, for jobs that already passed L0+L1.

**Implemented:** `matching/rules.py` (L1, runs first: country / remote / visa from ISO codes, job type,
years required, exclusions, JobRadar scam check), `matching/prefilter.py` (L0, OpenApply port; seniority inferred
from years when unset), `matching/funnel.py` (AI only for jobs that pass both; `AiScorer` slot for L2),
`matching/decide.py`, `matching/handler.py` (`match` task → `job_match` row → `alert` task). Checked on 1,705 real
jobs for an Indian backend profile: 1,651 hidden (mostly outside the user's countries), 38 alerts, 16 digest.
The L3 evidence card (StillOpen matchcard) is not ported yet: L0 + rule notes supply the "why" lines for now.

**+1:** learns from your taps. Every Apply/Skip adjusts your alert threshold (JobRadar has the idea as a suggestion; we make it automatic).

**Relevance rules (implemented, from a real session where a 1-year developer was sent "SDE III", "Senior" and
".NET" jobs):** the level a title states ("Senior", "Sr.", "III", "SDE-3", "L5", "Staff", "Leader") must fit the
user's years (senior 4, lead 6, principal 8, with 1 year of slack early in a career, 2 later); years asked in the
title ("8-10 yrs") or text count the same way, and the AI also reads the required years from the full posting, with
code applying the rule to its answer. A title built around a technology the user lacks (Java, .NET, PHP, SAP...)
is hidden; so are other job families (sales, support, recruiting, teaching). Sharing only "engineer" is no title
match, synonyms are unified ("AI Developer" = "AI Engineer", "ML" = "Machine Learning"), a job needs a title match
or 4+ of the user's skills to be shown at all, and only a title match can be an instant alert. Alerts are re-checked
before sending: a match made with an older profile or older matching logic (``LOGIC_VERSION``) is never sent.

### 2.8 Decide [v1]
**Copy JobRadar `decide.py`** rule table, extended:
not eligible / scam → hide · strong + sure → **instant Telegram alert** · medium → **daily digest** · above auto-apply score + auto mode on → **apply directly** [v1].

### 2.9 Telegram bot — built by us (no repo has one) [v1]
- **Onboarding chat:** upload resume, pick roles, countries, remote, visa, salary. (Preference shape from **Openings** `preferences.py`: `looking_for`, `ruled_out`, plus structured fields.)
- **Job alert:** title, company, place, salary, match %, 3 "why" lines, buttons **Apply · Skip · Later · Details**.
- **Before submit:** screenshot of the filled form + **Confirm** button.
- **Questions:** "This form asks your notice period — what should I answer?" → answer saved forever.
- **Commands:** `/status`, `/pause`, `/today`, `/prefs`, `/limit`.
- One `Channel` interface → **WhatsApp** plugs into the same code [Pro].

**Implemented (2026-11-20):** Bot API over plain `httpx` (`chat/telegram/api.py`, no bot library); one-time
pairing code from `sarthi pair` (hash stored, 15-minute expiry, cancelled after 5 wrong guesses; strangers get no
reply); job cards with Apply / Skip / Later / Details (job-site text HTML-escaped; first answer wins; Apply queues
an `apply` task); `/today`, `/status`, `/pause`, `/resume`, `/help`; daily digest at `SARTHI_DIGEST_HOUR` local
time; `app.py` is the composition root.
**Setup:** `/setup` conversation (`chat/setup.py`): resume upload (PDF/.docx/text, read locally by
`profile/resume.py`: size cap, type from bytes, no XML parser), AI suggestions from the scrubbed resume
(`ai/resume.py`), questions parsed by `profile/answers.py` (countries, remote, years, salary such as "12 LPA"),
summary with Save; saving re-matches jobs from the last 7 days. `/profile`, `/cancel`.
Lists (roles, skills) follow the usual chatbot pattern: **tap-to-remove buttons** for suggestions
(✅/❌ per item, then Done), and free text for edits. Plain lists and "ok" use fast rules; anything else goes to
the AI (`ai/answers.py`), which must answer which *current items* to keep (exact text, so "ML engineer" can
match "Machine Learning Engineer") and what to add; code builds the list and checks new items are names.
Rules never override the AI (an earlier rule "safety net" undid correct AI answers). Rules alone are used only
when no AI answers. Groq first (about 2 s, 11/11 real phrasings), local model as fallback. Every answeris echoed back ("✓ Roles: …") with a Change button.
**Conversation:** every non-command message goes through `chat/inbox.py` (quick bursts grouped into one;
one batch at a time with a single "still reading" note; polling never waits) and `chat/understand.py`, which
decides answer / correction / question / small talk / off-topic (rules first, `ai/intent.py` for unclear
messages, fixed choices only). Corrections work any time ("add UAE to my countries"): during setup they update
the draft and that question is skipped; afterwards they update the saved profile with an Undo button. While
the user is waiting on a reply, background match scoring defers so the local model answers the chat first.
**`sarthi run`:** bot + worker + `sources/schedule.py`: a 1-minute tick queues due sources (boards hourly
with the profile's roles/countries, company boards every 30 min); each check looks back to its last success
(catch-up after the computer was off, §2.18); failing sources back off up to 8x. Every new job is matched
(JobRadar's quiet first run is not used: matching decides what alerts, and the look-back window drops old jobs).

### 2.10 Documents [v1]
| Need | Best code | Copy |
|---|---|---|
| Tailored resume + PDF | **ApplyPilot** `scoring/tailor.py`, `pdf.py`, `json_resume.py` | Copy |
| Cover letter | **ApplyPilot** `scoring/cover_letter.py` | Copy |
| No invented facts | **OpenApply** `truth.ts` (port) + **Resume-Matcher** `resume_preservation.py` | Combine |
| ATS can read the PDF | **Resume-Matcher** `ats_parse/checks.py` | Copy [v1] |

**Implemented:** `profile/master.py` (structured master resume; contact details read locally, never from or to
the AI; entries kept only when the resume text confirms them), `ai/documents.py` (read into sections, tailor,
cover letter, claim reviewer), `documents/build.py` (code copies every fact; each AI line is used only if it adds
no number or skill; strict second try for clichés), `documents/truth.py` (OpenApply truth check + ApplyPilot lists),
`documents/render.py` + `templates/*.typ` (Typst PDFs; data passed as JSON, never as markup; read back to check an
ATS sees name, email, sections, employers, titles, schools), `documents/keywords.py` (on resume / you have but not
shown / missing), `documents/handler.py` (waits for a rate-limited AI instead of settling for the plain fallback,
except on the last try). Tapping Apply or "📄 Tailored resume" sends both PDFs and the keyword report on Telegram.

**+1:** documents are made only for jobs you tap Apply on (or auto-apply jobs), not every match → far fewer AI calls.

### 2.11 Form answers [v1]
- **Copy OpenApply `questions.ts` (port):** read the form's real questions from Greenhouse/Ashby/Lever **before** opening a browser.
- **Copy ApplyPilot `qa_knowledge` table:** every answer saved and reused, with outcome.
- **Implemented:** `answers/questions.py` (Greenhouse and Ashby APIs, Lever's apply page; any link of the job, so a
  LinkedIn job with a Greenhouse form is read too), `jobs/forms.py` (OpenApply field mapping: options, dates,
  place-bound relocation questions, decline for demographic questions; work authorisation and sponsorship per job
  country, and per home country when a question asks about the country of residence; "are you a human or an AI"
  questions are only ever answered by the user), `profile/applicant.py`, `db/answers.py` (answer bank + pending
  questions), `chat/questions.py` (one question at a time, buttons or typed, answers checked against the options).
- **+1:** unknown questions are asked on Telegram **together with the job alert**, so when you tap Apply nothing blocks the application.

### 2.12 Apply engine [v1] — four-step ladder
| Step | What | Best code |
|---|---|---|
| 1. Rule-based fill | Known job systems filled with plain code, no AI | **OpenApply** `apply/adapters/*` + `fieldmap.ts` (port to Python) |
| 2. Replay | Repeat the click path that worked last time on the same job system | **ApplyPilot** `successful_paths.py` |
| 3. AI browser | AI fills any unknown form, any website | **AIHawk** `apply.py` (works with Ollama or any OpenAI-style API) |
| 4. Hand to user | CAPTCHA / login wall → send link + resume + answers on Telegram | **OpenApply** `captcha.ts` detection |

Plus, copied:
- **Approval tied to one fill** (OpenApply): submit only works with the code from that exact preview.
- **Passwords encrypted** (AIHawk `secret.py`) — never in AI prompts (ApplyPilot's flaw removed).
- **Site logins remembered** per site in a saved browser profile (ApplyPilot accounts + AIHawk session).
- **Proof:** screenshot of the confirmation page (AIHawk).
- **Daily limit per site** (user-set) to protect the user's LinkedIn/Naukri accounts.

**Implemented (rule-based step + hand-off):** `apply/adapters.py` + `scan.js` + `captcha.js` (OpenApply port:
Greenhouse incl. embedded iframes, Lever, Ashby, any plain form; waits until React dropdowns are live before
typing), `apply/engine.py` (headless fill, files first, form screenshot, one-time code; submit only with that code
on the same open page, never with blanks, never past a CAPTCHA; "submitted" only on the hiring system's own success
mark), `apply/handler.py` (one application per role, daily per-site limit, Workday / login-only boards / CAPTCHA /
unfillable fields handed to the user with the link and an "I applied" button), `chat/applying.py` (screenshot with
Submit / Cancel). Verified on a live Greenhouse form (dry run, never submitted): 17 fields filled in 3 of 3 runs.
LinkedIn Easy Apply (`apply/linkedin.py`, adapted from LinkedIn-AI-Job-Applier-Ultimate, MIT) and Naukri one-click
apply (`apply/naukri.py`) use the user's own login, saved once with `sarthi login linkedin|naukri` in a local browser
profile (the password is typed into the site, never seen by Sarthi); both stop before the final submit for approval.
Remaining for v1: replay of saved paths, AI browser for unknown forms, Workday.

**+1:** steps 1–2 cost zero AI; AI is only used when the cheap ways fail, and every AI success is saved as a replay path → each job system gets cheaper over time.

### 2.13 Tracking replies [v1]
- **Copy ApplyPilot `tracking/*`:** Gmail reading, AI sorting (confirmation / rejection / interview / offer), people + dates, ghosted after N days.
- **Copy JobRadar `company_replies.py` + `followups.py`:** follow-up reminder with a draft [v1].
- **+1:** IMAP option with app password (works with any email, not only Gmail OAuth); every reply → Telegram message.

**Implemented:** `tracking/mail.py` (IMAP, read-only EXAMINE, works with Gmail app passwords and any mailbox; first
read looks back 30 days), `tracking/triage.py` + `matcher.py` (ApplyPilot ports: rules sort most emails, an email
counts only when its signals add up to one of the user's applications; the job board is never taken for the
employer), `ai/replies.py` (interviews, offers, tests and unclear emails only), `tracking/followup.py` (draft from
the user's own facts, never an AI; at most 2, from 10 days, none after 60 days or once the employer wrote),
`tracking/handler.py`; every reply goes to Telegram with dates and what to do.

### 2.14 AI layer [v1]
- **Plain `httpx` against OpenAI-compatible endpoints** (decided 2026-11-20, replacing LiteLLM): Ollama, Gemini,
  OpenAI, Anthropic, Groq, OpenRouter and DeepSeek all speak that API, so one small client covers them with far
  fewer dependencies in a tool that holds resumes and keys. Users list `<provider>/<model>` in the order to try.
- **Copy ApplyPilot** fallback chain (provider down → next one).
- **Copy JobRadar:** cache AI answers per job, daily AI budget, job text passed only as data (prompt-injection safe), only a compact profile sent (no name/email/phone).
- Two model slots: **fast** (matching) and **quality** (resume, cover letter, forms).
- Daily budget counts **tokens** of paid providers (local Ollama is exempt); full job text is stored compressed
  (`job_text`, 12k chars) so the AI judges the whole posting, not the snippet.
- Matching never uses a local model, even when one is in the fast list: scoring thousands of jobs on a laptop
  CPU keeps it at full load. It uses only the cloud fast models, and the free score when none answers.
- **Implemented:** `ai/providers.py`, `ai/client.py`, `ai/prompts.py` (fenced untrusted data),
  `ai/privacy.py` (emails, phones, links removed from the resume), `ai/scoring.py` (L2 of the match funnel).

### 2.15 Storage [v1]
One SQLite file. Tables: `sources`, `jobs`, `job_sources`, `matches`, `applications`, `answers` (Q&A bank), `accounts` (encrypted), `apply_paths`, `emails`, `tasks`, `llm_cache`, `events`.
**Copy Career-Ops rule:** user data (resume, prefs, DB) separated from app files → updates never touch user data.

### 2.16 Run & install [v1]
`docker compose up` → Sarthi + Playwright browser (+ optional Ollama). Setup finishes inside Telegram.

### 2.17 Dashboard [v1]
Small local web page: pipeline, applied, replies, stats. (Patterns from OpenApply `ui` / JobScan.)

### 2.18 Catch-up mode — for users who can't run Sarthi all day [v1]
User picks a time window: **last 24 hours · 2 days · 5 days · 7 days**. Sarthi searches every source for jobs posted in that window, runs the full pipeline (dedupe → quality gate → match), and shows the results as one batch.

How each source searches by date (verified in code):
| Source | Date filter |
|---|---|
| LinkedIn, Indeed, Naukri, Glassdoor… | **JobSpy** `hours_old` parameter |
| Freehire | API `open_within_days` |
| Company job systems | **ATS-Scrapers** returns all open jobs with `posted_at` → filter by date |
| Free feeds, HN, Telegram channels | posted date in the data / message history catch-up (JobRadar Telethon catch-up) |
| Jobs with no posted date | `first_seen_at` (when Sarthi first saw it) |

Same idea exists in **AIHawk** `library.py` (search "looks back N days", default 15) — we copy the concept.

After the batch is ready, user chooses on Telegram:
1. **Apply for me (batch)** — Sarthi applies one by one with safe pacing:
   - per-site daily limit (e.g. LinkedIn 15/day, Naukri 20/day — user can change);
   - random gaps between applications (minutes, not seconds);
   - leftover jobs carry over to the next run, best matches first;
   - one summary message at the end: applied / needs you / failed.
2. **Give me the list (I'll apply myself)** — Sarthi sends:
   - an Excel/CSV file: company, role, location, salary, match %, why it fits, apply link;
   - tailored resume PDF + cover letter + ready form answers for each job (zip), for the top matches;
   - user taps "Applied" on each so tracking still works.
3. **Mix** — auto-apply on easy sites, list for the rest.

**Scheduled catch-up:** "every day at 9 PM, check last 24 hours" — Sarthi starts, does the batch, sends results. Works with a computer that is on only part of the day. If the computer was off for 3 days, the next start automatically catches up on those 3 days.

**Implemented:** `/catchup` (24 h, 2, 5, 7 days) → `catchup` task reads every source over the window →
`catchup_report` waits for matching, then sends a summary, the best 10 as cards and every match as a CSV
(formula-injection safe); single alerts are held meanwhile. Batch apply is added with the apply engine.

**+1:** no repo combines a time-window search over all sources with batch apply + safe pacing + a do-it-yourself pack.

---

## 3. Where Sarthi beats every repo
| Thing | Best repo today | Sarthi |
|---|---|---|
| Sources | ATS-Scrapers ~100 / JobDrop 38 | All combined: 150+ plus Telegram + India drives |
| Speed | JobRadar 20–30 min fixed | Per-source smart polling, live Telegram |
| Matching | One method each | 4-layer funnel, AI only where needed, learns from taps |
| Applying | Each repo has 1 method | 4-step ladder, gets cheaper over time |
| Talking to user | Email / web page / terminal | Telegram bot (+ WhatsApp in Pro), apply from the phone |
| Form questions | Asked mid-application or guessed | Asked up front with the alert, saved forever |
| Passwords | ApplyPilot plaintext in prompts | Encrypted, never sent to AI |
| Cost to user | Paid AI plans in several | Free with Ollama, or own key |
| Not running all day | Mostly unsupported / jobs missed | Catch-up mode: last 24h–7 days, batch apply with safe pacing, or a ready-to-apply pack |

## 4. Release plan

### 4.1 Free launch (v1) — open source, self-hosted, every feature in §2

**Done (2026-11-21):**
- Sources: ~95 company hiring systems (ATS-Scrapers), LinkedIn/Indeed/Naukri/Glassdoor/Seek… (JobSpy), seven free
  remote feeds, Hacker News "Who is hiring", Freehire; smart polling with back-off; first-run baseline
- Job model, country detection, dedupe and reposts, scam check, full-text storage
- Match funnel: hard rules (country, remote, visa, level vs experience, years asked, tech stack, job family,
  internships), free score, AI score with code-checked years and level; versioned matching logic
- Telegram bot: pairing, alerts with Apply/Skip/Later/Details, daily digest, /today /status /pause /resume,
  natural-language chat with corrections and Undo, message batching
- Profile setup in chat: resume reading, years computed from dates, tap-to-remove buttons, AI list edits
- AI layer: any OpenAI-compatible provider, fallback chain, cache, token budget, local-model gate
- Catch-up mode in chat: /catchup 24 h–7 days → one batch with cards and a CSV

Dropped (2026-11-21): the UK licensed-sponsor register check: it only helps people who need a UK visa; the
job's own "we sponsor / no sponsorship" text and the user's visa answers already decide eligibility.

**Remaining for the launch:**
1. Sources: Telegram job channels (live), India mass-hiring pages, "paste any careers link", focus list from the
   173k-company file, Adzuna/Jooble (free keys), source-health message to the user
2. Catch-up: `sarthi catchup --days N` command, scheduled daily catch-up, batch apply from the results
3. Documents: tailored resume PDF, cover letter, no-invented-facts check, ATS keyword-gap report per job,
   ATS-readable PDF check
4. Form answers: read a form's questions before applying, ask unknown ones with the alert, answer bank
5. Apply engine: rule-based Greenhouse/Lever/Ashby → replay saved paths → AI browser for any form → hand to user;
   approval tied to one preview, encrypted site logins, screenshot proof, daily per-site limits; adapters for
   LinkedIn Easy Apply, Workday, Naukri; full auto-apply mode (user opt-in)
6. Tracking: Gmail and IMAP reading, reply sorting (confirmation / rejection / interview / offer), ghosted after
   N days, every reply on Telegram, follow-up drafts
7. Learning from Apply/Skip taps
8. Local web dashboard (pipeline, applied, replies, stats)
9. Install: `docker compose up`, README, first-run guide

WhatsApp moves to Pro (the official WhatsApp API needs a business account and per-message fees).

### 4.2 Sarthi Pro [Pro] — paid, built after launch

Rule: the free version stays complete and genuinely useful. Paid features are only things that need Sarthi's
servers, shared data or a running cost, so they cannot simply be copied from the open code. They are written
from scratch in a separate private service (no AGPL third-party code inside), which the open app calls over an
API; outside contributions to the open repo need a CLA so Sarthi can license its own code.

| # | Feature | What the user gets | Why it is paid |
|---|---|---|---|
| 1 | **Sarthi Cloud + Autopilot** | Runs 24/7 without a laptop; applies within the user's limits while they sleep; maintained apply bots for hard sites (Workday, LinkedIn) | Servers and ongoing maintenance |
| 2 | **First-to-know feed** | One central crawler checks 173k company career pages every few minutes: apply in the first hour | One laptop cannot poll that much without being blocked |
| 3 | **Crowd intelligence** | Reply rate per company, ghost/repost jobs, typical reply time, real salary ranges | Built from all users' anonymous, opt-in outcomes |
| 4 | **WhatsApp** | Alerts and Apply/Skip buttons on WhatsApp, no setup | Official API, business number, per-message fees |
| 5 | **Interview prep + mock interviews** | Per-job prep pack (likely questions, matching stories, questions to ask); text mock interview, then voice (Whisper speech-to-text + text-to-speech) with feedback | AI cost per session, curated question banks |
| 6 | **Referral finder** (only once revenue allows) | 2–3 people at the company in the user's field, with a short referral request drafted | People-data providers charge per lookup |

Pricing idea (job searches are short and urgent; India and global prices): weekly ~₹199 / $9, monthly ~₹599 /
$25, 3 months ~₹1,499 / $59; a reminder before every renewal; optional "pay after you get an offer".
Payments: Razorpay (India), Stripe or Lemon Squeezy (global). Crowd data is anonymous and opt-in; the free
self-hosted version stays 100 % local.

## 5. Folder structure

Pattern: **one folder per pipeline step + plug-in folders** (JobRadar / ApplyPilot style; repos audited use
step folders, per-site plug-ins, or web-app layers — no shared standard; MVC fits websites, Sarthi is a worker + bot).

```
sarthi/
├── src/sarthi/
│   ├── sources/        # 2.1  one module per source group (base.py = Source protocol) + liveness check
│   ├── queue/          # 2.2, 2.18  task queue, workers, scheduler, catch-up
│   ├── jobs/           # 2.3–2.6  pure job model, geo, dedupe, normalise, scam/repost checks (no I/O)
│   ├── matching/       # 2.7–2.8  match funnel + decide
│   ├── documents/      # 2.10 resume, cover letter, truth check, PDF
│   ├── answers/        # 2.11 form questions + answer bank
│   ├── apply/          # 2.12 apply ladder
│   │   └── adapters/       # rule-based fillers per job system
│   ├── tracking/       # 2.13 replies, ghosting, follow-ups
│   ├── chat/           # 2.9  Channel protocol
│   │   ├── telegram/
│   │   └── whatsapp/       # Pro
│   ├── ai/             # 2.14 LiteLLM, fallback, cache, budget, prompts
│   ├── db/             # 2.15 schema + data access (only place with SQL)
│   ├── profile/        # resume + preferences
│   ├── config.py  log.py  cli.py
├── tests/              # mirrors src/sarthi/
├── data/               # user data, git-ignored, never touched by updates
├── docker/  docs/  .github/workflows/
├── LICENSE  THIRD_PARTY_NOTICES.md  SECURITY.md  pyproject.toml
```

## 6. Rules the code follows

Full list in [STANDARDS.md](STANDARDS.md). The architecture-level ones, enforced by `import-linter` in CI:
- Pipeline steps (`sources`, `matching`, `documents`, `answers`, `apply`, `tracking`, `chat`) never import each other; they talk only through `db` + `queue`.
- Layers import downwards only: `cli` → steps → `queue / ai` → `profile` → `db` → `jobs` (pure job model, no I/O) → `clock / config / log`.
- Only `db` writes SQL; only `ai` calls LLMs.

## 7. Not yet proven
Everything above is from reading code. Before building, each copied piece must pass its own tests and a live run.
