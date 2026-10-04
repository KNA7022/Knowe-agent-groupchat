# Read-only design: durable ownership for feedback on pending Agent cards

## Problem

The baseline triage identifies `_cmd_feedback_instruction` → `adjust_instruction` → `_run_feedback_flight` as an aux-model control-plane path. Legacy Gate pending records lack a durable backend-owned Task Runtime owner run. Passing renderer/model IDs or fabricating a broad background run is forbidden. Existing behavior must remain fail-closed until a sound integration exists. A baseline implementer is fixing unrelated tests and reproducing this denial; do not edit its work.

## Required design outcome

- Inspect existing card creation/persistence/recovery, Task Runtime run/approval state machines, Provider Gateway authority, credential leases and cancellation. Name concrete existing seams; do not build a parallel identity/approval registry if existing tables can express ownership.
- Propose the smallest safe implementation that binds new cards to canonical group/task/run and card generation before exposing them to the renderer, verifies ownership at feedback/approval time, and never grants authority to old unowned cards by guessing.
- Clarify which parent run states can support user-requested read-only aux feedback, how bounded side/control work is budgeted/scheduled, what can be inherited, and what is not inherited. Do not solve the problem by globally allowing waiting/terminal runs to use providers.
- Include stale-card, completed/cancelled task, source/group deletion, replay/restart, duplicate request and cancellation semantics. Preserve DLP, purpose-bound short-lived credentials, scope isolation and no secret/content audit logging.
- Explain safe migration/display behavior for old cards with no owner binding. Reapproval alone must not grant arbitrary effects or replay prior high-risk calls.
- Test matrix must reach the actual authenticated command plus inert real Gateway path and observe both authorized success and no-egress/no-effect denial. Avoid source-string-only or mock-before-policy tests.
- Return a bounded implementation sequence, files/seams, alternatives rejected with reason, and concrete unresolved questions only if existing code cannot decide them. No external calls, code changes, branch/index mutations or subagents. UTF-8.

Read `backend-regression-triage.md` in this directory for initial evidence, then verify relevant source. Output only this requested durable design report `card-authority-design.md` in the same directory. Do not read the whole historical plan/ledger or make changes. This is preparation, not approval to start dependent implementation before the current baseline gate.
