-- Frozen DDL from ee57eca / schema v1. No application rows or private data.

CREATE TABLE IF NOT EXISTS bindings (
    chat_id TEXT PRIMARY KEY,
    herdr_session TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    agent_name TEXT,
    valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision > 0),
    bound_at REAL NOT NULL,
    bound_by TEXT NOT NULL,
    UNIQUE (herdr_session, workspace_id)
);
CREATE TABLE IF NOT EXISTS requests (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    binding_revision INTEGER,
    herdr_session TEXT,
    workspace_id TEXT,
    pane_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('processing','done','failed','unknown')),
    result_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS create_requests (
    request_id TEXT PRIMARY KEY,
    confirmation_code TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    project_path TEXT NOT NULL,
    workspace_label TEXT NOT NULL,
    agent_kind TEXT NOT NULL CHECK (agent_kind IN ('codex','claude')),
    agent_name TEXT NOT NULL,
    original_revision INTEGER,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending','processing','done','failed','unknown')
    ),
    workspace_id TEXT,
    tab_id TEXT,
    pane_id TEXT,
    result_code TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);


CREATE TABLE group_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    confirmation_code TEXT NOT NULL UNIQUE,
    source_chat_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    group_name TEXT NOT NULL,
    create_uuid TEXT NOT NULL UNIQUE,
    herdr_session TEXT NOT NULL CHECK (herdr_session = 'kpi-agg'),
    bot_open_id TEXT NOT NULL,
    created_chat_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending','processing','done','failed','unknown')),
    result_code TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status <> 'done' OR (created_chat_id IS NOT NULL AND length(created_chat_id) > 0))
);

PRAGMA user_version=1;
