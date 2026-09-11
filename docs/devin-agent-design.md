# Devin agent support: minimal design

Date: 2026-09-11. Status: DESIGN_READY_FOR_LOCAL_REVIEW. No implementation or new validation is claimed.

## 1. Baseline and boundary

This is Issue #5 / Draft PR #6, separate from the accepted self-service groups work. The inspected baseline is `main@ee57eca173bb7994695d44a9643fb577eb10922a`; the PR branch is `feat/devin-agent-support`, initially at `01943772dc588cf48c2223e13a5cd5aa4b33549f`. The reported 166/166 baseline suite is prior local evidence, not a run performed for this document. [R0] [R1]

The supplied Linux evidence confirms HerdR 0.9.0 accepts `--kind devin` and can report `agent="devin"`. It does not yet prove the bridge's Devin creation flow. Only `codex`, `claude`, and `devin` become explicitly recognized creation kinds. No discovery-based acceptance, additional kinds, aliases, provider setup, plugin registry, new commands, or dependency changes.

[Existing design](design.md) remains authoritative except for the three-kind allowlist and schema upgrade specified here. Preserve fixed `kpi-agg`, the project/user/chat allowlists, dynamic managed-group authorization, independent group/workspace proposals, confirmation ownership, message deduplication, workspace ownership, and exact Pane routing. Do not change Feishu group creation or its SDK boundary.

The live bridge continues from main while development uses an isolated worktree, separate virtual environment, and disposable test databases. No candidate process may open the live database or consume the live bot's events during development. A later local-approved maintenance window is required for migration and controlled acceptance.

## 2. Command and name contract

The new syntax is exactly `/new <project_alias> devin`. Existing `/new <project_alias> codex` and `/new <project_alias> claude` remain valid. Kind matching is case-sensitive; reject `Devin`, other kinds, and extra arguments under the existing error/help behavior. Project aliases remain configuration lookups, never arbitrary directories.

Replace only the two-kind fragment of the current help. The complete `_HELP` value becomes:

    /agents | /bind | /bind <workspace_id> <pane_id> | /read | /new <项目别名> <codex或claude或devin> | /group-new <群名> | /confirm <确认码> | /cancel

Keep existing response codes/templates: valid `/new` returns `creation_pending`, unsupported kinds retain `invalid_agent`, successful verified creation returns `created`. The existing proposal displays `Agent：devin / <generated_name>` together with directory, workspace label, old binding, and the original five-minute confirmation instruction. No automatic workspace or CLI invocation occurs at proposal time.

Reuse `agent_name(kind)` with `secrets.token_hex(6)`: Devin names are exactly `fb-devin-<12 lowercase hexadecimal characters>`, matching `fb-devin-[0-9a-f]{12}` and the existing `[a-z][a-z0-9_-]{0,31}` limit. Generate/persist the name at proposal time. Before starting, inspect live names in the fixed session; on collision regenerate once and persist that replacement. A second preflight collision fails before workspace creation. A racing start conflict never causes another start, name takeover, or another workspace. Keep existing error classification; an unclassified remote error remains unknown.

## 3. Thin adapter and verification

Use the existing fixed allowlists in `BridgeCore._propose`, `HerdrAdapter.start_agent`, and `_agent_info`; extend each to exactly the same three literals. Do not derive acceptance from HerdR help or a runtime catalogue. [R2] [R4]

Protocol remains 22. Keep envelope/result-type validation, mandatory AgentInfo fields, exact resource keys (`workspace.workspace_id`, `tab.tab_id`, `root_pane.pane_id`), stderr error decoding, and plain-text reads unchanged. Preserve the current `/agents` workspace/Tab labels and grouping.

For `_agent_info`, retain the existing recognized-value fallback: inspect `agent`, then `display_agent`, accepting only an exact supported string; otherwise use `unknown`. Thus `agent="devin"` becomes kind `devin` in list/get/start responses. A recognized primary value takes precedence over display metadata; a primary `claude` with display `devin` is still `claude`. Neither names, labels, `argv`, nor substrings establish kind. This follows the existing fallback contract, not a new process-identity guarantee.

The start command remains exactly:

    <absolute_herdr_executable> --session kpi-agg agent start <generated_name> --kind devin --pane <new_root_pane_id> --timeout <existing_timeout_ms>

No additional agent flags, focus changes, permission bypass, or provider credentials. Keep the existing name and Pane argument checks. The adapter must receive exactly one Agent with the requested Pane; additionally require `returned.kind == requested_kind`. A kind mismatch or `unknown` raises `start_unverified` with an uncertain write result; retain the current uncertain `wrong_target` behavior for wrong Pane/cardinality.

The core retains its second boundary: both the start result and subsequent `agent get <new_root_pane_id>` must match the just-created workspace ID, root Pane ID, and requested kind. Never substitute the most recent/focused Pane. Only then call the existing atomic `complete_creation` to bind. An adapter-only success or matching generated name is insufficient.

## 4. SQLite v1 to v2

### Recognized schemas

The baseline has four application tables and `user_version=1`. The only final DDL difference is the `create_requests.agent_kind` check:

    v1: CHECK (agent_kind IN ('codex','claude'))
    v2: CHECK (agent_kind IN ('codex','claude','devin'))

Do not add permanent tables, columns, kinds to group records, or new persistent indexes. Keep all 17 `create_requests` columns, their order, affinities, nullability, defaults, primary/unique constraints and status check exactly as v1. In particular, do not silently strengthen legacy primary-key nullability or refresh timestamps. [R3]

The ordered copy columns are:

    request_id, confirmation_code, chat_id, requested_by, project_path,
    workspace_label, agent_kind, agent_name, original_revision, expires_at,
    status, workspace_id, tab_id, pane_id, result_code, created_at, updated_at

Retain known version-0 compatibility: only an empty new database or the exact historical three-table shape is eligible. A populated v0 database follows the existing v0-to-v1 step and then this change in one outer transaction; a new database ends with the same v2 schema. A v2 database is validated, never rebuilt again. Unknown `user_version`, version/DDL disagreement, unexpected columns/constraints or leftover staging objects fail closed before recovery or SDK/event startup. Do not reinterpret a four-table unversioned database as v1.

Freeze the baseline v1 DDL independently of the new DDL. Validation must recognize both fresh-v2 and SQLite-renamed-v2 table rendering, including SQLite's quotation of the renamed table identifier, without broadly relaxing CHECK expressions, defaults, or literal values.

### Indexes, constraints, dependencies

Inspect the schema and index metadata before any destructive statement. The accepted application schema has no user-created indexes, triggers, views, or declared foreign keys. Reject unexpected ones before migration; do not silently drop or copy unknown executable schema. This is a fixed-schema migration, not a generic migration engine.

Required implicit PK/UNIQUE index key sets, with existing ordering/collation and no partial/expression indexes, are:

| Table | Required keys |
|---|---|
| `bindings` | Primary key `chat_id`; unique `(herdr_session, workspace_id)`. |
| `requests` | Primary key `message_id`. |
| `create_requests` | Primary key `request_id`; unique `confirmation_code`. |
| `group_requests` | Primary key `request_id`; unique `confirmation_code`, `create_uuid`, and `created_chat_id` separately. |

Recreating `create_requests` with its full declared constraints recreates its two implicit indexes. Verify their origin, uniqueness and ordered columns using index metadata, not hard-coded `sqlite_autoindex` names. Other tables and their constraints remain unchanged, including group `done` requiring a nonempty resource ID and the fixed-session check. SQLite-managed metadata is not an application table.

There are no expected triggers/views to recreate. Any unexpected dependency or user-created index needs explicit local review rather than automatic preservation. Never turn off constraints, edit `sqlite_schema` through `writable_schema`, or use a schema-version reset to force acceptance.

### Atomic sequence and recovery

Run in the existing `Store` startup boundary, after the process lock and before `BridgeCore.recover_incomplete` or any external action. Use the existing explicit transaction with individual `execute` calls, not `executescript`. [S1][S2]

1. Begin `IMMEDIATE`; read/validate the source version, fixed DDL, dependencies and indexes. Reject pre-existing constraint/integrity violations rather than cleaning data. For v1, preserve an unchanged source until its copy has been verified.
2. Create a normal staging table `create_requests_v2_migration` in the same database using the full v2 definition. A pre-existing staging name is an error. It is transaction-local work, not a fifth persistent application table.
3. Copy `rowid` and all 17 explicit columns with a single `INSERT ... SELECT`. No `OR IGNORE`, `REPLACE`, filtering, regenerated keys, or new defaults. Verify equal counts and bidirectional row/value equality, including rowid, before dropping the original.
4. Drop the old `create_requests`, rename the verified staging table to `create_requests`, then validate the final v2 DDL/index inventory and database integrity. Preserve every row in `bindings`, `requests`, and `group_requests` without rewriting those tables.
5. Set `PRAGMA user_version=2` last and commit once. Any exception, busy/locked database, failed copy/check, rename or version update aborts startup and rolls back any open migration transaction. Never retry a partially committed migration step in place.

A process exit before commit leaves the original committed version recoverable; after commit, reopening recognizes v2 without recopying. Tests must cover failure after DROP/rename as well as before them. A commit whose acknowledgement is lost is resolved by validating the actual version on next startup, never by replaying remote work.

Migration preserves all stored statuses and confirmation codes. Normal startup recovery remains a separate existing step: unfinished operations become unknown according to current rules; done group authorization and bindings persist. Migration must not call creator, HerdR, or recovery itself. Old pending Codex/Claude confirmations remain usable if still otherwise eligible; migration does not extend expiry or resurrect terminal requests.

Stop the live bridge and take a consistent private backup only during the later approved rollout. Old main rejects v2, so do not restart `ee57eca` against an upgraded database. No automatic downgrade is added. Restoring a pre-migration backup after new remote writes could discard consumed confirmations; preserve the latest state and require local review instead.

## 5. Preserved operation safety

Devin follows the same proposal, same-user/same-chat confirmation, expiry, project revalidation, binding revision check, name preflight, workspace create, start, get, and bind sequence. Workspace/group confirmation namespaces and cancellation ownership remain untouched.

Known created resources stay recorded after failure; the old binding is not replaced. Timeout, malformed output, stopped/interrupted write, wrong workspace/Pane/kind, and persistence uncertainty never automatically retry create/start or resubmit a consumed confirmation. No cleanup/delete, fallback to Shell input, new fingerprint, or automatic recovery workflow.

Tests must demonstrate these rules for Devin without weakening Codex/Claude or accepted self-service-group tests. See [implementation plan](devin-agent-plan.md) for exact scope and the one disposable live-flow boundary.

## 6. Evidence and open checks

HerdR support/detection is supplied live evidence; end-to-end Devin creation, local Devin initialization, Linux migration/restart behavior and the new tests remain pending. First-run/login/trust prompts must be resolved by the authorized local operator, never by bridge permission bypass. A blocked or uncertain trial is not a passed creation test.

Only public repository references and symbolic runtime placeholders are recorded here. No real group name, user/chat ID, token, confirmation value or private path belongs in documentation, fixtures or evidence.

[R0]: https://github.com/nashhu180-netizen/lark-herdr/pull/6
[R1]: https://github.com/nashhu180-netizen/lark-herdr/issues/5
[R2]: https://github.com/nashhu180-netizen/lark-herdr/blob/ee57eca/feishu_herdr_bridge/herdr.py
[R3]: https://github.com/nashhu180-netizen/lark-herdr/blob/ee57eca/feishu_herdr_bridge/store.py
[R4]: https://github.com/nashhu180-netizen/lark-herdr/blob/ee57eca/feishu_herdr_bridge/core.py
[S1]: https://www.sqlite.org/lang_altertable.html
[S2]: https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Cursor.executescript
