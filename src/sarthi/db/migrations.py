"""Versioned schema changes, applied in order on every start.

The applied version is kept in SQLite's ``user_version``. Never edit a released migration: append a new one.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[str, ...] = (
    # 1: jobs, the boards each job was seen on, and the task queue
    """
    CREATE TABLE job (
        id                   TEXT PRIMARY KEY,          -- sha1 of the canonical URL first seen
        role_key             TEXT,                      -- company|role, joins reposts across boards
        canonical_url        TEXT NOT NULL UNIQUE,
        source               TEXT NOT NULL,             -- source of the first sighting
        title                TEXT NOT NULL,
        company              TEXT NOT NULL,
        country_codes        TEXT NOT NULL DEFAULT '',  -- space-separated ISO codes, e.g. ' IN GB '
        remote_type          TEXT NOT NULL,
        posting              TEXT NOT NULL,             -- the full JobPosting as JSON
        posted_at            TEXT,
        first_seen_at        TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL
    ) STRICT;
    CREATE INDEX job_role_key ON job (role_key, first_seen_at);
    CREATE INDEX job_first_seen ON job (first_seen_at);

    CREATE TABLE job_source (
        job_id               TEXT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
        source               TEXT NOT NULL,
        canonical_url        TEXT NOT NULL UNIQUE,
        first_seen_at        TEXT NOT NULL,
        last_seen_at         TEXT NOT NULL,
        PRIMARY KEY (job_id, canonical_url)
    ) STRICT;

    CREATE TABLE task (
        id                   INTEGER PRIMARY KEY,
        type                 TEXT NOT NULL,
        key                  TEXT NOT NULL,             -- idempotency key, e.g. a job id
        payload              TEXT NOT NULL DEFAULT '{}',
        status               TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'running', 'done', 'failed')),
        attempts             INTEGER NOT NULL DEFAULT 0,
        priority             INTEGER NOT NULL DEFAULT 0, -- higher runs first
        run_after            TEXT NOT NULL,
        started_at           TEXT,                      -- set on claim; finds tasks stuck after a crash
        rerun                INTEGER NOT NULL DEFAULT 0, -- re-enqueued while running: run once more after
        last_error           TEXT,
        UNIQUE (type, key)
    ) STRICT;
    CREATE INDEX task_ready ON task (status, priority DESC, run_after);
    """,
    # 2: per-source state: first-run baseline (no alert flood) and health
    """
    CREATE TABLE source_state (
        name                 TEXT PRIMARY KEY,
        baseline_done        INTEGER NOT NULL DEFAULT 0, -- first successful read only records what is there
        last_checked_at      TEXT,
        last_ok_at           TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error           TEXT
    ) STRICT;
    """,
    # 3: match results, one per job, re-run when the profile changes
    """
    CREATE TABLE job_match (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        profile_version      TEXT NOT NULL,
        score                INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
        decision             TEXT NOT NULL CHECK (decision IN ('hide', 'digest', 'alert')),
        rule                 TEXT NOT NULL,
        hidden_because       TEXT,
        reasons              TEXT NOT NULL DEFAULT '[]',  -- JSON list of short lines
        notes                TEXT NOT NULL DEFAULT '[]',
        ai_used              INTEGER NOT NULL DEFAULT 0,
        matched_at           TEXT NOT NULL
    ) STRICT;
    CREATE INDEX job_match_decision ON job_match (decision, matched_at);
    """,
    # 4: full job text for the AI (compressed), AI answer cache, AI token usage for the daily budget
    """
    CREATE TABLE job_text (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        text_zlib            BLOB NOT NULL
    ) STRICT;

    CREATE TABLE llm_cache (
        key                  TEXT PRIMARY KEY,          -- sha256 of model, prompt version and input
        value                TEXT NOT NULL,
        created_at           TEXT NOT NULL
    ) STRICT;

    CREATE TABLE llm_usage (
        id                   INTEGER PRIMARY KEY,
        provider             TEXT NOT NULL,
        model                TEXT NOT NULL,
        prompt_tokens        INTEGER NOT NULL DEFAULT 0,
        completion_tokens    INTEGER NOT NULL DEFAULT 0,
        at                   TEXT NOT NULL
    ) STRICT;
    CREATE INDEX llm_usage_at ON llm_usage (at);
    """,
    # 5: small app state (paired chat, pause switch) and what the user did with each job
    """
    CREATE TABLE app_state (
        key                  TEXT PRIMARY KEY,
        value                TEXT NOT NULL
    ) STRICT;

    CREATE TABLE job_user (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        status               TEXT NOT NULL
                             CHECK (status IN
                                 ('alerted', 'digested', 'apply', 'skipped', 'later', 'applied')),
        message_id           INTEGER,                   -- the chat message showing this job
        updated_at           TEXT NOT NULL
    ) STRICT;
    CREATE INDEX job_user_status ON job_user (status, updated_at);
    """,
    # 6: tailored documents per job
    """
    CREATE TABLE job_document (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        profile_version      TEXT NOT NULL,             -- the profile the documents were made for
        resume_file          TEXT NOT NULL,             -- path inside the data directory
        letter_file          TEXT NOT NULL,
        report               TEXT NOT NULL,             -- JSON: keywords, warnings, what was kept from the AI
        made_at              TEXT NOT NULL
    ) STRICT;
    """,
    # 7: the answer bank (every form answer the user gave, reused) and questions waiting for the user
    """
    CREATE TABLE saved_answer (
        question             TEXT PRIMARY KEY,          -- the form's label, as asked
        answer               TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    ) STRICT;

    CREATE TABLE pending_question (
        job_id               TEXT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
        field_key            TEXT NOT NULL,
        label                TEXT NOT NULL,
        field_type           TEXT NOT NULL,
        options              TEXT NOT NULL DEFAULT '[]', -- JSON list
        position             INTEGER NOT NULL,          -- form order
        PRIMARY KEY (job_id, field_key)
    ) STRICT;
    """,
    # 8: applications: how each one went (also what reply tracking matches emails against)
    """
    CREATE TABLE application (
        job_id               TEXT PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
        status               TEXT NOT NULL
                             CHECK (status IN
                                 ('filled', 'submitted', 'needs_human', 'failed', 'applied_by_user')),
        site                 TEXT NOT NULL DEFAULT '',  -- the form's host, for daily per-site limits
        detail               TEXT NOT NULL DEFAULT '',
        screenshot           TEXT NOT NULL DEFAULT '',  -- path inside the data directory
        updated_at           TEXT NOT NULL,
        submitted_at         TEXT
    ) STRICT;
    CREATE INDEX application_site ON application (site, submitted_at);
    """,
    # 9: replies from employers (read from the user's mailbox) and follow-up reminders
    """
    CREATE TABLE reply (
        message_id           TEXT PRIMARY KEY,          -- the email's Message-ID: each email is read once
        job_id               TEXT REFERENCES job (id) ON DELETE SET NULL,
        kind                 TEXT NOT NULL,             -- confirmation, rejection, interview, ...
        sender               TEXT NOT NULL DEFAULT '',
        subject              TEXT NOT NULL DEFAULT '',
        summary              TEXT NOT NULL DEFAULT '',
        details              TEXT NOT NULL DEFAULT '{}', -- JSON: dates, actions, match signals
        received_at          TEXT,
        read_at              TEXT NOT NULL
    ) STRICT;
    CREATE INDEX reply_job ON reply (job_id, received_at);
    ALTER TABLE application ADD COLUMN reminders INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE application ADD COLUMN reminded_at TEXT;
    """,
    # 10: pages and career links the user asked Sarthi to watch, and what each watched page last said
    """
    CREATE TABLE watched (
        url                  TEXT PRIMARY KEY,
        kind                 TEXT NOT NULL CHECK (kind IN ('board', 'page')),
        system               TEXT NOT NULL DEFAULT '',  -- for boards: the hiring system (greenhouse, ...)
        slug                 TEXT NOT NULL DEFAULT '',  -- for boards: the company on that system
        name                 TEXT NOT NULL DEFAULT '',
        added_at             TEXT NOT NULL
    ) STRICT;

    CREATE TABLE page_text (
        url                  TEXT PRIMARY KEY,
        text_zlib            BLOB NOT NULL,
        read_at              TEXT NOT NULL
    ) STRICT;
    """,
)


def schema_version(conn: sqlite3.Connection) -> int:
    """The number of migrations applied to this database."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration, each in its own transaction. Returns the new version.

    Raises ``RuntimeError`` when the database is newer than this code (a downgrade), to avoid corrupting it.
    """
    current = schema_version(conn)
    if current > len(MIGRATIONS):
        raise RuntimeError(f"database schema v{current} is newer than this Sarthi (v{len(MIGRATIONS)})")
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript commits any open transaction first, so the BEGIN/COMMIT live inside the script.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)
