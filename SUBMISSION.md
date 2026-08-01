# SUBMISSION.md

## Architecture

A FastAPI service with four logical layers:

- **API layer** (`main.py`) — routes, bearer auth (`require_auth` dependency), a
  custom `HTTPException` handler and a `RequestValidationError` handler that
  both normalize every error into the `{"error": {"code", "message"}}`
  envelope, and a sliding-window rate limiter (deque of timestamps per token)
  applied only to `POST /v1/reviews`.
- **Job store** — an in-memory dict (`JOBS`) keyed by `jobId`, holding status,
  findings, usage, and a per-job event log used for SSE replay. Two lookup
  tables (`CACHE`, `IDEMPOTENCY`) sit alongside it for content-hash caching
  and `Idempotency-Key` handling.
- **Worker pool** — `POST /v1/reviews` creates a job in `"queued"` state and
  dispatches it with `asyncio.create_task`, returning `202` immediately. An
  `asyncio.Semaphore(4)` bounds concurrent processing; a 5th job waits rather
  than failing. Each job transitions `queued → running → done/failed`,
  wrapped in a single `try/except` that is the one place graceful degradation
  happens for both providers.
- **Providers** — `rules.py` (mock) and `llm_provider.py` (Gemini) both return
  the identical `(findings, total, chunks)` shape, so the worker doesn't need
  to know which one it called.
- **SSE broadcaster** — `GET /v1/reviews/{id}/stream` replays a job's event
  log from index 0 on every connection. Live streaming and replay are the
  same code path: a live connection just catches up to a list that keeps
  growing; a replay connection catches up to a list that's already finished.

Diff parsing (`parser.py`) and chunking (`chunker.py`) are separate,
independently testable modules from the rule engine, so each piece could be
unit-tested without spinning up the server.

## Provider design

Both providers share one contract: `review_fn(diff_text, max_findings) ->
(findings, total_before_truncation, chunks_used)`. `review_diff` (mock) is
deterministic — it chunks the diff on file boundaries, runs the 9 fixed rules
per chunk, then merges, dedupes by `id`, sorts by `(path, line, ruleId)`, and
truncates once across the whole result set (never per chunk), so chunking is
invisible to the caller. `llm_review_diff` sends the raw diff to Gemini with a
prompt constraining output to a JSON array in the same finding shape, then
runs it through the same dedupe/sort/truncate logic so both providers are
interchangeable from the worker's perspective. The `llm` path does not chunk
large diffs in this version — noted below under next steps.

## How I verified the cross-cutting behaviors

- **Chunking**: generated an ~82KB synthetic diff across 3 files, each with a
  planted finding. Confirmed it split into 2 chunks and all 3 findings came
  back correctly, with no duplicates or losses. Separately verified a single
  file larger than 64KiB becomes its own chunk rather than being split.
- **Concurrency**: added a temporary 2-second delay in the worker, fired 6
  jobs simultaneously, and polled their statuses every 0.5s. Observed exactly
  4 jobs `running` and 2 `queued` for the first ~2 seconds, then the
  remaining 2 starting only once slots freed — total time ~4.6s, confirming
  the semaphore genuinely bounds concurrency rather than either serializing
  everything or running unbounded.
- **SSE replay**: connected to a finished job's `/stream` endpoint twice.
  Both connections returned the identical full event sequence
  (status→finding→finding→status→done), proving replay doesn't depend on
  when the client connects.
- **Caching & idempotency**: verified same `Idempotency-Key` + same body
  returns the same `jobId`; same key + different body returns `409`; and a
  byte-identical body with no key at all still hits the cache and the job's
  `usage.cacheHit` flips to `true`.
- **Rate limiting**: fired 35 rapid submissions; the first 30 succeeded, the
  next 5 returned `429` with a `Retry-After` header.
- **Injection inertness**: built an adversarial diff mixing 3 different
  injection phrases with 3 genuine rule violations. All 6 findings came back
  correctly — the injection content triggered `MOCK-INJ` findings like any
  other pattern match and did not suppress or alter the other rules' output.
- **Error taxonomy**: explicitly tested malformed JSON (`400`/`invalid_json`,
  requiring a dedicated `RequestValidationError` handler since FastAPI's
  default behavior doesn't match the contract), an empty diff, a diff missing
  hunk headers, oversized payloads, and unknown job IDs.
- **`llm` graceful degradation**: proven under three distinct real failure
  conditions — missing API key, an invalid model name, and Gemini free-tier
  quota exhaustion (reproduced on two different models) — all producing a
  clean `"failed"` job with a structured error, never a crash. Reproduced the
  same behavior against the deployed Fly.io instance, not just locally.

## AI tools used

Built with Claude (Anthropic) as a pair-programming/planning partner
throughout — architecture sketch before writing code, incremental
implementation phase by phase, and debugging via pasted error output and
terminal logs at each step.

## An AI suggestion I rejected

Claude's first version of the `llm` provider used the `google.generativeai`
Python package. Running it surfaced a runtime deprecation warning: Google has
ended all support for that package in favor of `google.genai`. I rejected
building further on a dead package and had it migrate the implementation
(client construction, method signatures, and config all differ between the
two) before continuing — better to fix that immediately than carry a
known-deprecated dependency into a service meant to be defended in an
interview.

## What I'd do next with more time

- Chunk large diffs for the `llm` provider the same way the mock provider
  does, for the same context-window reasons.
- Replace the empty-catch-block heuristic (`MOCK-004`) with a real
  brace-aware parser — the current line-adjacent regex will miss blocks with
  blank lines or comments between `{` and `}`.
- Tighten unified-diff validation beyond "contains `---`/`+++`/`@@`" toward
  actually attempting a full structural parse and rejecting anything that
  doesn't produce a consistent result.
- Move the in-memory job store to Redis or a small database so state
  survives a restart and could be shared across multiple server instances.
- Replace the SSE generator's 0.2s polling loop with a proper pub/sub or
  condition-variable wakeup instead of polling.
- Resolve the Gemini free-tier quota/billing configuration to get a full
  successful `llm` run end to end, not just verified graceful failure.
