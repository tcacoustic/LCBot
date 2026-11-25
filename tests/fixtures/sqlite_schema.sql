-- The SQLite schema LC Bot ran before the move (staging d925687, after its startup CREATE/ALTER block).
-- Schema only, no data: the load tests build made-up snapshots from it.

CREATE TABLE agents (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    username TEXT,
    dm_ok    INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1,
    role     TEXT NOT NULL DEFAULT 'agent',
    status   TEXT NOT NULL DEFAULT 'active',
    display_name TEXT,
    requested_at TEXT,
    decided_by   INTEGER,
    decided_at   TEXT
, req_msgs TEXT, support_name TEXT, on_avails INTEGER NOT NULL DEFAULT 1, tag_calls INTEGER NOT NULL DEFAULT 1, salaried INTEGER NOT NULL DEFAULT 0, channels TEXT, country TEXT, store TEXT);

CREATE TABLE appointment_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    appt_id    INTEGER NOT NULL,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE appointments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    country            TEXT NOT NULL,
    specialist_id      INTEGER,
    visit_type         TEXT NOT NULL,
    starts_at          TEXT NOT NULL,
    duration_min       INTEGER NOT NULL DEFAULT 120,
    customer_name      TEXT NOT NULL,
    phone              TEXT,
    email              TEXT,
    address            TEXT NOT NULL,
    postal             TEXT,
    product            TEXT,
    issue              TEXT,
    order_no           TEXT,
    status             TEXT NOT NULL DEFAULT 'SCHEDULED',
    created_by         INTEGER,
    created_at         TEXT NOT NULL,
    proposed_starts_at TEXT,
    proposed_by        INTEGER,
    arrived_at         TEXT,
    left_at            TEXT,
    outcome            TEXT,
    amount_cents       INTEGER,
    currency           TEXT,
    pay_method         TEXT,
    followup_done      INTEGER NOT NULL DEFAULT 0
, urgency TEXT, followup_of INTEGER);

CREATE TABLE cab_claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    appt_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    trip         TEXT NOT NULL,      -- TO, FROM, BOTH
    amount_cents INTEGER NOT NULL,
    currency     TEXT NOT NULL,
    receipt      TEXT,               -- Telegram file_id of the receipt photo
    status       TEXT NOT NULL DEFAULT 'RECEIPT',   -- RECEIPT, PENDING, APPROVED, REJECTED
    reason       TEXT,
    decided_by   INTEGER,
    decided_at   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE case_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL,
    user_id    INTEGER,
    side       TEXT,
    action     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE channel_posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER,
    message_id INTEGER,
    agent_id   INTEGER,
    the_date   TEXT NOT NULL,
    created_at TEXT NOT NULL
, kind TEXT NOT NULL DEFAULT 'handover');

CREATE TABLE confirmations (
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (week_id, user_id)
);

CREATE TABLE days (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id   INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    name      TEXT NOT NULL,
    the_date  TEXT NOT NULL,
    msg_id    INTEGER
);

CREATE TABLE drop_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'drop',
    agent_id     INTEGER NOT NULL,
    slot_id      INTEGER NOT NULL,
    reason       TEXT,
    target_id    INTEGER,
    requested_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    decided_by   INTEGER,
    decided_at   TEXT
, notified TEXT);

CREATE TABLE fixed_slots (
    user_id  INTEGER NOT NULL,
    day_name TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (user_id, day_name, label)
);

CREATE TABLE handovers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  INTEGER NOT NULL,
    the_date  TEXT NOT NULL,
    body      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE ho_cases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    the_date   TEXT NOT NULL,
    section    TEXT NOT NULL DEFAULT 'open',
    prio       TEXT,
    platform   TEXT,
    flag       TEXT,
    store      TEXT,
    username   TEXT,
    body       TEXT,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    closed_by  INTEGER,
    closed_at  TEXT
, channel TEXT, order_no TEXT, product TEXT, happened TEXT, done TEXT, need TEXT, sh_closed INTEGER NOT NULL DEFAULT 0, sh_closed_by INTEGER, sh_closed_at TEXT, on_closed INTEGER NOT NULL DEFAULT 0, on_closed_by INTEGER, on_closed_at TEXT, chat_posted INTEGER NOT NULL DEFAULT 0, post_id INTEGER, handed_back INTEGER NOT NULL DEFAULT 0, pm_notified INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL DEFAULT 'sh');

CREATE TABLE memberships (
    user_id    INTEGER NOT NULL,
    dept       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, dept)
);

CREATE TABLE onsite_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    appt_id     INTEGER NOT NULL,
    country     TEXT NOT NULL,
    need        TEXT NOT NULL,       -- REPLACEMENT, PART, OTHER
    reason      TEXT NOT NULL,
    warranty    TEXT NOT NULL,       -- IN, OUT
    price_cents INTEGER,             -- NULL = to confirm
    currency    TEXT,
    pays_by     TEXT NOT NULL,       -- LINK, PAYNOW, NONE
    status      TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN, TAKEN, DONE
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    taken_by    INTEGER,
    taken_at    TEXT,
    done_by     INTEGER,
    done_at     TEXT,
    note        TEXT,
    chat_id     INTEGER,
    msg_id      INTEGER
);

CREATE TABLE ot_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL,
    agent_id    INTEGER NOT NULL,
    claimed_out TEXT NOT NULL,
    ot_minutes  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    asked_at    TEXT NOT NULL,
    decided_by  INTEGER,
    decided_at  TEXT,
    notified    TEXT
);

CREATE TABLE pay_rates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       INTEGER,
    cents          INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    created_by     INTEGER,
    created_at     TEXT NOT NULL
);

CREATE TABLE presets (
    name   TEXT PRIMARY KEY,
    config TEXT NOT NULL
);

CREATE TABLE reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    the_date   TEXT NOT NULL,
    photo_id   TEXT,
    note       TEXT,
    added_by   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE signups (
    slot_id  INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    ts       TEXT NOT NULL,
    PRIMARY KEY (slot_id, user_id)
);

CREATE TABLE slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id     INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    label      TEXT NOT NULL,
    start_min  INTEGER NOT NULL,
    capacity   INTEGER
);

CREATE TABLE special_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL,
    country         TEXT,
    category        TEXT NOT NULL,
    details         TEXT NOT NULL,
    delivery_date   TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    timeframe_other TEXT,
    created_by      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    after_cutoff    INTEGER NOT NULL DEFAULT 0,   -- next working day's delivery, asked after 2pm
    urgent          INTEGER NOT NULL DEFAULT 0,   -- same-day delivery
    status          TEXT NOT NULL DEFAULT 'OPEN',
    reason          TEXT,
    taken_by        INTEGER,
    taken_at        TEXT,
    ops_note        TEXT,
    handled_by      INTEGER,
    handled_at      TEXT,
    ops_chat_id     INTEGER,
    ops_msg_id      INTEGER,
    sheet_dirty     INTEGER NOT NULL DEFAULT 1,
    sheet_row       INTEGER
, source TEXT NOT NULL DEFAULT 'bot', submitted_name TEXT);

CREATE TABLE specialist_blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    starts_at  TEXT NOT NULL,
    ends_at    TEXT NOT NULL,
    note       TEXT,
    part       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE time_edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL,
    changed_by INTEGER NOT NULL,
    before     TEXT,
    after      TEXT,
    reason     TEXT,
    changed_at TEXT NOT NULL
);

CREATE TABLE time_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL,
    slot_id    INTEGER,
    the_date   TEXT NOT NULL,
    clock_in   TEXT NOT NULL,
    clock_out  TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    source     TEXT NOT NULL DEFAULT 'agent',
    note       TEXT,
    shift_label TEXT,
    opening_posted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE weeks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    key_events    TEXT,
    deadline      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    header_msg_id INTEGER
);
