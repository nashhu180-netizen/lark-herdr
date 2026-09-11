# Devin agent support: two-batch implementation plan

Date: 2026-09-11. Status: DESIGN_READY_FOR_LOCAL_REVIEW. This is a plan, not implementation or validation evidence.

## 1. Baseline, isolation and ownership

Use [the Devin design](devin-agent-design.md) as the narrowly scoped extension to the existing design. Baseline: `ee57eca173bb7994695d44a9643fb577eb10922a`; Issue #5 / Draft PR #6; branch `feat/devin-agent-support`, initially `01943772dc588cf48c2223e13a5cd5aa4b33549f`. The prior 166/166 suite is user-reported local baseline evidence; all checks below are not run.

GPT-6 Pro authors implementation and tests. Local Codex applies/pulls, checks scope, runs Linux tests, reviews, and performs any approved narrow fixes with regression evidence. Each batch stops for review. No implementation begins until the design and migration plan are approved.

The live service keeps using main while both batches are developed in an isolated worktree. Use a worktree-local virtual environment and temporary databases; never install the candidate into the live editable environment, use live credentials, start a second bot consumer, or pass the live configuration/database into tests. Do not run candidate `Store` initialization against the live database as a probe.

Check the branch/base before work and preserve any local changes. These are instructions for later local execution, not commands run for this design:

    git status --short
    git branch --show-current
    git rev-parse HEAD
    git merge-base --is-ancestor ee57eca173bb7994695d44a9643fb577eb10922a HEAD

Expected branch: `feat/devin-agent-support`. A moved/divergent baseline needs review; do not force-reset the live checkout. Production rollout and the single controlled live case occur only after both offline batches pass and the local coordinator authorizes a maintenance window.

## 2. Batch D1: atomic schema extension

**Dependency:** approved design; no adapter/core feature changes in this batch.

**Exact paths:**
- Add `tests/fixtures/schema_v1.sql`.
- Modify `feishu_herdr_bridge/store.py`, `tests/test_store.py`, and `tests/test_creation.py`.

The fixture is a frozen copy of the four-table v1 DDL from `ee57eca` with `user_version=1`, no production rows or private values. Test data is synthetic. Do not generate this fixture from the new schema constants or fake v1 by relabelling a v2 database.

Implement fixed v0/v1/v2 recognition, schema/dependency/index checks, and the transactional `create_requests` rebuild from the design. Preserve the old DDL as a distinct validation baseline. Only the kind check gains `devin`; final application tables remain exactly four. Keep recovery and all group/workspace data APIs unchanged.

Adjust existing migration tests to target v2 while still proving real legacy v0/v1 acceptance. In `tests/test_creation.py`, change only the old-database fixture setup needed for pending workspace-confirmation compatibility; do not alter the existing creation flow assertions.

**Red-first tests:** first assert opening a frozen valid v1 database yields `user_version=2` and preserves its records. Against baseline code this must fail on the version assertion, not import/setup. Separately assert a valid Devin proposal row can be inserted after migration while any fourth kind still violates CHECK.

**Migration matrix:**
- Fresh database, historical v0, populated v1, and repeated v2 opens; both fresh and renamed table-rendering shapes pass strict validation.
- All four tables' values, keys, nulls, timestamps, bindings, terminal records, and pending confirmation values survive the migration; include gapped rowids and synthetic nullable legacy fields. Snapshot immediately after `Store`, before recovery.
- Unknown version, wrong kind/status CHECK, missing/extra columns, missing UNIQUE, rogue table/staging table, explicit index, trigger, view or foreign key fail closed. Unexpected v1 rows violating the old CHECK must not become silently accepted by widening it.
- Inject failures after staging creation, copy, DROP, rename, final validation, version update and commit. Reopen with an independent connection; verify the original committed v1 or complete v2, never a half-schema. Include subprocess interruption before commit and after commit.
- Verify PK/UNIQUE index semantics and actual constraint enforcement after recreation. Group done/resource constraints and workspace unique ownership remain enforced.
- Run normal recovery separately after migration: processing becomes unknown under existing rules, done groups remain authorized, old eligible Codex/Claude codes still work, and no external operations are replayed.

**Later local commands:**

    .venv/bin/python -m unittest -v tests.test_store tests.test_creation
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    git diff --check

**Acceptance:** behavior red recorded, tests green, no skipped required tests, independently reopened databases preserve the specified data and schema. No network or real HerdR calls. Unknown database shapes stop before recovery/SDK startup.

**Stop:** `DEVIN_D1_READY_FOR_LOCAL_REVIEW`. Hand off the patch, red/green outputs, schema/index inventories, and rollback/reopen evidence. Do not deploy D1 by itself or begin D2 without approval.

## 3. Batch D2: explicit Devin flow and regression

**Dependency:** D1 accepted and frozen for this batch. Any migration correction returns to D1 review.

**Exact paths to modify:** `feishu_herdr_bridge/core.py`, `feishu_herdr_bridge/herdr.py`, `tests/test_core.py`, `tests/test_creation.py`, `tests/test_herdr.py`, `tests/test_feishu.py`, and `docs/runbook.md`. No new files in this batch.

Core changes are limited to the explicit three-kind allowlist/help and any necessary use of the existing verification guard. Adapter changes are limited to the three-kind decode/start contract and requested-kind verification. Preserve the accepted workspace/Tab list presentation and exact protocol-22 keys/stderr behavior. The runbook change is limited to Devin usage and a v2 migration/rollback note pointing to this design; retain the accepted self-service-group instructions and historical evidence.

Do not modify `store.py`, Feishu production code, configuration, SDK version, systemd, group authorization, proposal namespaces, or the old live-validation documents. Reuse existing test helpers and cloned synthetic envelopes within tests; do not overwrite Codex/Claude fixtures or label them live captures.

**Red-first tests:** baseline `/new <test_alias> devin` fails the expected `creation_pending` behavior, and a valid AgentInfo with `agent="devin"` fails the expected decoded kind. Establish at least one actual behavior red before extending the allowlists.

**Required tests:**
- Exact help string, lowercase-only three-kind acceptance, no arbitrary directory/kind, no external action during proposal, persisted `fb-devin-[0-9a-f]{12}` name, one collision regeneration, and no start retry on a racing/unknown failure.
- List/get/start decode `devin`; existing fallback precedence remains explicit, unknown kinds stay unknown, workspace/Tab labels stay intact. A conflicting recognized primary kind cannot be overridden by the display field to pass Devin verification.
- Adapter uses explicit `--session kpi-agg`, `--kind devin`, the returned root Pane and the existing timeout arguments. Wrong response cardinality/Pane/kind and malformed output fail closed. Validate the exact command array through the injected runner/fake boundary.
- Core checks both start and subsequent get for the new workspace/Pane/requested kind. Test start mismatch and get mismatch separately, including known-other-kind and unknown; preserve old binding/resources without repeating any write.
- Same-user/same-chat, expiry, duplicate event, changed binding/project, restart after processing/unknown, partial creation and persistence failure retain existing behavior. Do not make Devin confirmations adopt group-confirmation replay semantics.
- Parameterize successful creation over Codex, Claude and Devin; retain unsupported-kind and existing group tests. Exercise Devin via the existing Feishu event entry with fake HerdR and no real SDK network, including a done dynamic group and one-workspace ownership rejection.

**Later local commands:**

    .venv/bin/python -m unittest -v tests.test_core tests.test_creation tests.test_herdr tests.test_feishu
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m pip check
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m feishu_herdr_bridge --help
    git diff --check

**Acceptance:** targeted/full regression, compileall, pip check and help succeed in the fixed Linux environment, with no required SDK test skipped and no production network. Preserve the prior tests rather than weakening assertions to recover a green count. The full-suite count is recorded when run, not predicted here.

**Stop:** `DEVIN_D2_READY_FOR_LOCAL_REVIEW`. Hand off all actual outputs and remaining risks. No automatic deployment, PR merge or live test. Following local approval, hand over to the controlled validation below.

## 4. One controlled live case, performed only by the local coordinator

This is a single acceptance scenario after the two implementation batches, not another feature batch. The author does not execute it. Existing `kpi-agg` Agent list metadata may be returned by normal list/name-preflight calls, but the test must never bind, get/read, prompt, start in, or focus an existing KPI workspace/Pane.

1. With the accepted main service, use the existing management-group proposal/confirmation flow to create exactly one new disposable Feishu task group T. Keep actual group names/IDs/codes private. Confirm T is managed and unbound; do not create a workspace yet. This uses accepted group behavior without changing it.
2. At an approved idle boundary, stop the single live bridge, take a consistent private v1 backup including T, and locally verify Devin's CLI/login readiness without touching existing KPI workspaces. Deploy only the reviewed candidate under an explicit temporary rollout decision. Never run candidate and main bot consumers concurrently. Confirm migration completes and T remains authorized; do not use another HerdR session or a second database to bypass migration.
3. In T, send `/new <approved_disposable_project_alias> devin`. Check pending-only behavior, the generated name and unchanged binding; then the original requester confirms once in T. Record W_NEW and P_NEW only from this request's created-resource evidence. Verify exactly one new workspace and a Devin Agent in its root Pane, with binding established only after verified success.
4. After verifying T's exact new binding, send one harmless request for a test marker with no file changes, then `/read` in T. `/agents` must show the new Agent as `devin`, retaining its workspace/Tab presentation. No commands may target any pre-existing KPI Pane. A blocked first-run screen, unknown response, missing kind or partial result stops the case; do not force `/bind`, duplicate the confirmation, or create another workspace to manufacture a pass.
5. After that request is idle, perform one authorized normal bridge restart and check only T's binding and P_NEW output. The v2 reopen must not recreate groups, workspaces, or Agents. Keep other groups untouched. Preserve the v2 state; do not restart old main against it or restore a write-before backup after this case's remote writes.

Local rollout duration, promotion and post-test service ownership require an explicit local decision. Development itself leaves main running. No rollback recipe may discard newly consumed confirmations or managed groups; prefer an approved v2-compatible correction when new writes exist.

Publish only redacted evidence under Issue #5 / PR #6; no new acceptance document is required. T/W_NEW/P_NEW and the test marker are symbolic labels. Do not publish actual names, codes, IDs, tokens, private paths or raw responses. A failed scenario is recorded as failed/partial, never retried automatically.

| Check | Initial result | Required evidence |
|---|---|---|
| D1 migration and red/green tests | Not run | Fixed v1 fixture, exact data/index comparison, failure rollback and restart evidence. |
| D2 targeted/full and environment checks | Not run | Actual commands, counts and exit codes; unchanged accepted group behavior. |
| T created and preserved through controlled v2 rollout | Not run | Redacted ownership/authorization evidence and migration result. |
| Devin proposal, single confirmed start and exact new binding | Not run | Request-to-W_NEW/P_NEW correspondence and verified kind; no existing workspace writes. |
| New-Pane marker/read and normal restart | Not run | Redacted output and unchanged new binding/resource counts after restart. |

Known remaining risks: exact local SQLite-rendered DDL after rename; unanticipated manually added schema objects; old-binary incompatibility with v2; Devin first-run/authentication/blocked behavior. Resolve through the specified tests and local review, not permissive schema matching or automatic write retries.

Final design handoff: `DESIGN_READY_FOR_LOCAL_REVIEW`. Wait for local approval before D1.
