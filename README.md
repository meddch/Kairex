# Organizational Memory API

Organizational memory pipeline for extracting structured facts from conversation transcripts, routing them through a human approval gate, and injecting approved context into downstream LLM prompts — in French.

---

## Architecture

The system is built around three hard requirements. Each has a specific, verifiable implementation.

### Requirement 1 — Human Approval Gate

Facts extracted from a transcript are never automatically committed. Every fact enters the system with `status = pending` and stays there until a human decision is made. No LLM output touches downstream prompts without a human in the loop.

```
POST /extract-session
        │
        ▼
  facts (status=pending)
        │
   ┌────┴────┐
   ▼         ▼
approve    reject
   │
   ▼
GET /inject-context  ← only approved facts reach the WriterAgent
```

The `pending → approved/rejected` transition is explicit: two separate endpoints, one action each. `GET /inject-context` queries `WHERE status = 'approved'` — rejected and pending facts are structurally excluded.

### Requirement 2 — Idempotent Checkpoint Pipeline

Every fact is checkpointed individually, immediately after it is written. If the server crashes mid-extraction, re-submitting the same `session_id` resumes from the last committed checkpoint — no facts are lost, no facts are duplicated.

**Write order:**

```
1. INSERT OR IGNORE INTO checkpoints  ← written first (write-ahead)
2. INSERT OR IGNORE INTO facts        ← written second
```

If a crash occurs between steps 1 and 2, the checkpoint exists but the fact row is missing. On re-run, the checkpoint is found, and the fact is re-inserted from the stored `fact_data` JSON using `INSERT OR IGNORE` (same UUID — idempotent). If both rows already exist, both inserts are no-ops.

The `UNIQUE(session_id, fact_index)` constraint on `checkpoints` makes duplicate checkpoint writes impossible at the DB level, independent of application logic.

SQLite WAL mode ensures checkpoint durability across restarts without requiring a separate process or external store.

### Requirement 3 — Two Independent Agents

The agents are isolated by contract, not by convention.

| Agent | Input | Output | DB access |
|---|---|---|---|
| `ExtractorAgent` | transcript `str` | `list[dict]` | None |
| `WriterAgent` | `list[dict]` | context block `str` | None |

`ExtractorAgent` calls the OpenAI API and returns raw parsed facts. It has zero imports from `database.py`. It does not know facts have IDs, sessions, or statuses.

`WriterAgent` receives a list of already-approved fact dicts and synthesizes a French context block. It does not know where those facts came from or how they were stored.

All database wiring — session creation, checkpoint writes, fact inserts, status queries — lives exclusively in `main.py`. The agents can be swapped, tested, or replaced without touching each other or the DB layer.

---

## Stack

**Python + FastAPI** — synchronous endpoints on a threadpool. No async complexity needed for this workload.

**SQLite + WAL mode on Railway Volume** — chosen over Postgres because there is no replication requirement, no concurrent writers from multiple processes, and no ops overhead to justify it. WAL mode gives durable, concurrent reads without locking the writer. A persistent Railway volume survives deploys. For a single-tenant memory pipeline this is the right call.

**Plain OpenAI SDK, `gpt-4o-mini`** — no LangChain, no LangGraph. Both agents fit in a single `chat.completions.create` call. Adding an orchestration framework would introduce abstraction without solving any actual problem here. `gpt-4o-mini` is fast and cheap for structured extraction and short synthesis tasks.

**Railway** — zero-config deploys from a `railway.json`. Persistent volume mounted at the DB path. `ON_FAILURE` restart policy covers transient crashes.

**API key authentication** — `X-API-Key` header validated against `API_SECRET_KEY` env var on all endpoints except `/health`. Simple, stateless, no session management needed.

---

## Endpoints

### `POST /extract-session`

Extracts structured facts from a transcript and stores them in `pending` state.

**Request body:**
```json
{
  "org_id": "acme",
  "transcript": "...",
  "session_id": "optional-uuid-for-resumption"
}
```

If `session_id` is omitted, a new session is created. If provided, the endpoint resumes from the last checkpoint — facts already processed are skipped.

**Response:**
```json
{
  "session_id": "uuid",
  "org_id": "acme",
  "facts_extracted": 6,
  "facts": [
    {
      "id": "uuid",
      "entity_type": "person",
      "entity_value": "Sara",
      "attribute": "position",
      "value": "CTO",
      "confidence": 0.95,
      "status": "pending",
      "source_snippet": "Sara, our CTO"
    }
  ]
}
```

```bash
curl -X POST https://memory-api.example.com/extract-session \
  -H "X-API-Key: test-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "acme",
    "transcript": "Meeting with Sara, our CTO. We use Salesforce and are evaluating HubSpot. 45 employees. Budget 200,000 MAD."
  }'
```

---

### `GET /inject-context`

Reads all approved facts for an org and returns a French context block ready for injection into an LLM prompt.

Returns `404` if no approved facts exist for the org.

**Query params:** `org_id` (required)

**Response:**
```json
{
  "org_id": "acme",
  "context_block": "## Contexte organisationnel\n\nL'organisation Acme est dirigée par Sara...",
  "facts_used": 3
}
```

```bash
curl "https://memory-api.example.com/inject-context?org_id=acme" \
  -H "X-API-Key: test-api-key"
```

---

### `GET /facts/pending`

Lists facts awaiting a human decision.

**Query params:** `org_id` (optional — omit to list all orgs)

**Response:**
```json
{
  "pending_facts": [...],
  "count": 5
}
```

```bash
curl "https://memory-api.example.com/facts/pending?org_id=acme" \
  -H "X-API-Key: test-api-key"
```

---

### `POST /facts/{id}/approve`

Promotes a fact from `pending` to `approved`. Approved facts are included in `/inject-context`.

```bash
curl -X POST "https://memory-api.example.com/facts/FACT-UUID/approve" \
  -H "X-API-Key: test-api-key"
```

**Response:**
```json
{
  "message": "Fact approved",
  "fact": { "id": "...", "status": "approved", ... }
}
```

---

### `POST /facts/{id}/reject`

Sets a fact to `rejected`. Rejected facts are permanently excluded from context injection.

```bash
curl -X POST "https://memory-api.example.com/facts/FACT-UUID/reject" \
  -H "X-API-Key: test-api-key"
```

---

### `GET /health`

Unauthenticated liveness check.

```bash
curl https://memory-api.example.com/health
# {"status": "ok"}
```

---

## Full Flow Walkthrough

**Step 1 — Extract from a transcript**

```bash
curl -X POST https://memory-api.example.com/extract-session \
  -H "X-API-Key: test-api-key" \
  -H "Content-Type: application/json" \
  -d '{"org_id": "acme", "transcript": "Sara is our CTO. We use Salesforce. 45 employees."}'
```

Returns `session_id` and 3–6 pending facts. Save the `session_id`.

**Step 2 — Review pending facts**

```bash
curl "https://memory-api.example.com/facts/pending?org_id=acme" \
  -H "X-API-Key: test-api-key"
```

**Step 3 — Approve and reject**

```bash
curl -X POST https://memory-api.example.com/facts/FACT-ID-1/approve \
  -H "X-API-Key: test-api-key"

curl -X POST https://memory-api.example.com/facts/FACT-ID-2/reject \
  -H "X-API-Key: test-api-key"
```

**Step 4 — Inject approved context**

```bash
curl "https://memory-api.example.com/inject-context?org_id=acme" \
  -H "X-API-Key: test-api-key"
```

Returns a French context block containing only approved facts.

**Step 5 — Resume a crashed session**

If the extraction was interrupted (server restart, timeout, network drop), re-submit with the original `session_id`:

```bash
curl -X POST https://memory-api.example.com/extract-session \
  -H "X-API-Key: test-api-key" \
  -H "Content-Type: application/json" \
  -d '{"org_id": "acme", "transcript": "...", "session_id": "ORIGINAL-SESSION-ID"}'
```

Already-checkpointed facts are restored from the DB, not re-extracted. No duplicates. Approved/rejected statuses on existing facts are preserved.

---

## What Was Cut and Why

**Rate limiting** — would add value in a multi-tenant production deployment. Not needed for a single-tenant deployment. Can be added as FastAPI middleware in one file.

**Semantic deduplication** — detecting that "Sara is CTO" and "The CTO is Sara" are the same fact requires embedding comparison. Omitted because the human approval gate already catches this: a reviewer rejects the duplicate. Adding vector similarity before the gate is the right next step, not a prerequisite.

**Async task queue** — extraction is synchronous and blocks the request. For long transcripts or high concurrency, this should be a background job (Celery, ARQ, or Railway's job service) with a status-polling endpoint. Excluded to keep the deployment surface minimal for this scope.

**Memory validation UI** — the approve/reject endpoints are the interface. A thin frontend over `GET /facts/pending` and the two mutation endpoints is a natural next layer. Not built here because the API contract is the focus.
