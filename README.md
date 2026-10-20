# SDLC factory

A standalone HTTP service that scopes a task, plans a change, builds it, runs configured checks, repairs failures, independently reviews the candidate, and conditionally merges it. Model providers are adapters; each stage can use a different provider and model.

This is a working single-worker foundation, implemented with Python's standard library and Git. It requires Python 3.11+ and Git. No package installation or model credentials are needed for the demo.

## Run the complete offline demonstration

```powershell
python -m factory demo
```

The demo creates a disposable calculator repository under `.factory/`, scopes a known task, makes an intentionally incorrect implementation, runs real unit tests, repairs the failure, reviews the passing candidate, and merges it into the disposable bare repository. It writes full results to the printed `result.json` path. The scripted provider is a deterministic fixture and does not implement arbitrary tasks.

## Try the service without API keys

Prepare a fresh fixture and start the server:

```powershell
python -m factory demo --prepare-only --directory .factory/api-demo
python -m factory serve --config .factory/api-demo/config.json --state .factory/api-demo/service
```

In another terminal, submit the exact demo task:

```powershell
$taskBody = @{
    repository = 'calculator'
    task = 'Fix calculator.add so it adds two numbers correctly.'
} | ConvertTo-Json
$run = Invoke-RestMethod http://127.0.0.1:8765/api/tasks -Method Post -ContentType application/json -Body $taskBody
Invoke-RestMethod "http://127.0.0.1:8765/api/tasks/$($run.id)"
Invoke-RestMethod "http://127.0.0.1:8765/api/tasks/$($run.id)/events"
```

The worker progresses automatically. Repeating the inspection command shows the current stage and evidence. Stop with Ctrl+C; restart with the same config and state directory to resume queued work and recover expired worker leases. An interrupted worker lease expires after 90 seconds.

## Connect models and a repository

Copy `examples/config.json` to `factory.local.json`. Set a real Git source, target branch, model names, allowed paths, and check commands. Both local bare repositories and remote Git sources can be used. Git access uses the worker's available Git/SSH credentials. A local target branch must not be checked out in another working directory; a bare repository is suitable for this prototype.

Set the credential environment variables referenced by your configuration, then run:

```powershell
python -m factory serve --config factory.local.json --state .factory/service
```

Credentials are resolved at runtime from named environment variables, never from task payloads. Avoid credentials in source URLs. Each task snapshots its configuration; changing configuration applies to new tasks after a service restart.

Supported adapters:

| Kind | Endpoint | Configuration |
| --- | --- | --- |
| `openai-compatible` | `<base_url>/chat/completions` | Explicit model and optional `api_key_env`; works with compatible hosted or local endpoints |
| `anthropic` | `<base_url>/messages` | Explicit model and `api_key_env` |
| `scripted` | None | Offline calculator demonstration only |

Each model request is stateless and includes stage instructions, the task, the available repository snapshot, and relevant evidence. Review receives requirements, code, diff, and executed check results in a fresh request. You may route review to another model or provider. Adapters validate returned JSON before applying changes or advancing stages. An incompatible response fails the stage with a recorded error; there is no silent provider fallback.

OpenAI-compatible servers differ in parameter support. Set `token_parameter` to `max_tokens` when required by your endpoint; the default is `max_completion_tokens`. Configured model names are never automatically replaced. Real provider calls have not been validated against paid endpoints; wire contracts are tested with local mock servers.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service health |
| GET | `/api/repositories` | Configured repository names, checks, and merge modes |
| POST | `/api/tasks` | Submit `{"repository":"name","task":"request"}` |
| GET | `/api/tasks` | Latest 200 tasks |
| GET | `/api/tasks/{id}` | State, artifacts, check logs, usage, candidate commit |
| GET | `/api/tasks/{id}/events` | Ordered audit events |
| POST | `/api/tasks/{id}/answer` | Answer blocking scope questions with `{"answer":"..."}` |
| POST | `/api/tasks/{id}/approve` | Authorize manual merge with `{"sha":"current-candidate"}` |
| POST | `/api/tasks/{id}/resume` | Retry a failed stage with `{}` and the existing budgets |
| POST | `/api/tasks/{id}/cancel` | Cancel before merge has been requested, with `{}` |

An optional `Idempotency-Key` header on submission returns the existing task for a repeated identical request. Reusing the key for a different request returns HTTP 409.

Set `FACTORY_API_TOKEN` to require `Authorization: Bearer <token>` on every request. Non-loopback binding requires this token. For shared deployment, put TLS and access controls in front of the service. All authenticated callers share one operator role in this version.

## Workflow and merge rules

`scope → plan → build → verify → review → merge`

Failed checks or blocking review findings return to `repair`, then verification and review repeat. Scope questions wait in `awaiting_input`. Manual policy waits in `awaiting_approval`. Budgets bound model calls, repair iterations, and elapsed execution time; external operations also have timeouts. Dollar accounting and hard process-resource limits are not implemented.

Only application code evaluates merge eligibility. Required checks must pass for the current candidate and base; review must approve that same pair without blocking findings. Manual approval is tied to the candidate commit. If the target moves, the worker rebases, invalidates approval and evidence, and repeats checks and review. Conflicting rebases fail for operator resolution in the task's isolated workspace.

Merge uses a fast-forward candidate plus an atomic expected-base condition. The conditional push is confirmed against the target. A merge intent is saved before the external operation, allowing restart or resume to reconcile a successful merge whose response was lost. Cancellation is rejected after that intent because the external operation may have taken effect.

This repository adapter merges through Git push. It does **not** create hosted pull requests, collect GitHub/GitLab approvals, or poll hosted CI. Branch rules that require those workflows reject direct pushes; a hosted pull request adapter is a next milestone. Add every required check to the trusted configuration for the current adapter.

## Boundaries and next milestones

- Intended for trusted small repositories and an operator-controlled worker. Repository checks run on the worker host. Git checkout isolation is not a process sandbox; containerized execution is required before accepting untrusted repositories or users.
- Model edits are full UTF-8 file replacements, limited to 50 files / 500 KB per response. Private file paths, path traversal, symlink edits, and configured protected paths are rejected before any write.
- Model context contains tracked text files, limited to 24 KB per file and 120 KB in total. Omitted files are listed; `context_exclude` accepts additional glob patterns to exclude from model context. Diffs over 64 KB fail rather than reaching review with truncated evidence. This is not yet a tool-using coding harness with arbitrary repository search.
- SQLite stores stage checkpoints, artifacts, usage reports, leases, and audit events. Workspace locks prevent an expired lease from enabling concurrent edits. Orphaned interrupted clone directories are retained for inspection; automatic retention/cleanup is not implemented.
- Waiting for scope answers or approval does not consume the active-time budget. Time between a worker crash and recovery is conservatively charged. Budgets are checked between operations; an in-flight Git operation can run up to its command timeout.
- This version uses one worker per service and has no dashboard, distributed scheduler, dollar-cost ledger, hosted CI integration, deployment, or post-merge rollback automation.

Next: add container workers and a tool-using agent loop, a GitHub/GitLab pull request adapter, then shared-service authentication/authorization and a task dashboard. Keep the existing stage contracts and deterministic merge gate.

See `SDLC_FACTORY.md` for the broader architecture and milestone contracts.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The suite exercises real temporary Git repositories and check commands, failure-driven repair, independent review rejection, commit-specific approval, target movement, interrupted stage recovery, uncertain merge outcomes, idempotency, cancellation, edit boundaries, HTTP authentication, and provider request/response formats. It makes no external model calls.

Provider references: [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat), [Claude Messages API](https://platform.claude.com/docs/en/api/messages/create), [Claude API authentication](https://platform.claude.com/docs/en/api/overview).
