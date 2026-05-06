import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from database import get_db, init_db
from agents.extractor import ExtractorAgent
from agents.writer import WriterAgent

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")


def require_api_key(x_api_key: Optional[str] = Header(None)):
    if not API_SECRET_KEY:
        raise HTTPException(status_code=500, detail="API_SECRET_KEY not configured")
    if x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Kairex Memory API", lifespan=lifespan)


# ── Request models ────────────────────────────────────────────────────────────

class ExtractSessionRequest(BaseModel):
    org_id: str
    transcript: str
    session_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def row_to_dict(row) -> dict:
    return dict(row) if row else {}


def fact_rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/extract-session", dependencies=[Depends(require_api_key)])
def extract_session(body: ExtractSessionRequest):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    now = datetime.utcnow().isoformat()

    if body.session_id:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (body.session_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session '{body.session_id}' not found")
        session_id = body.session_id
    else:
        session_id = str(uuid.uuid4())
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sessions (id, org_id, transcript, status, created_at) VALUES (?, ?, ?, 'processing', ?)",
                (session_id, body.org_id, body.transcript, now),
            )

    agent = ExtractorAgent(api_key=OPENAI_API_KEY)
    raw_facts = agent.extract(
        transcript=body.transcript,
        session_id=session_id,
        org_id=body.org_id,
    )

    committed_facts = []
    for idx, raw_fact in enumerate(raw_facts):
        with get_db() as conn:
            existing = conn.execute(
                "SELECT fact_data FROM checkpoints WHERE session_id = ? AND fact_index = ?",
                (session_id, idx),
            ).fetchone()

            if existing:
                fact = json.loads(existing["fact_data"])
                # Re-insert in case fact row was lost after checkpoint was written
                conn.execute(
                    """INSERT OR IGNORE INTO facts
                       (id, session_id, org_id, entity_type, entity_value,
                        attribute, value, confidence, status, source_snippet, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        fact["id"], fact["session_id"], fact["org_id"],
                        fact["entity_type"], fact["entity_value"],
                        fact["attribute"], fact["value"], fact["confidence"],
                        fact["source_snippet"], fact["created_at"],
                    ),
                )
                committed_facts.append(fact)
                continue

            fact = {
                "id": str(uuid.uuid4()),
                "session_id": session_id,
                "org_id": body.org_id,
                "entity_type": raw_fact["entity_type"],
                "entity_value": raw_fact["entity_value"],
                "attribute": raw_fact["attribute"],
                "value": raw_fact["value"],
                "confidence": raw_fact["confidence"],
                "status": "pending",
                "source_snippet": raw_fact["source_snippet"],
                "created_at": now,
            }

            # Checkpoint first (write-ahead): if crash after this but before fact INSERT,
            # re-run finds the checkpoint and re-inserts the fact from stored data.
            conn.execute(
                """INSERT OR IGNORE INTO checkpoints
                   (id, session_id, fact_index, fact_data, status, created_at)
                   VALUES (?, ?, ?, ?, 'done', ?)""",
                (str(uuid.uuid4()), session_id, idx, json.dumps(fact), now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO facts
                   (id, session_id, org_id, entity_type, entity_value,
                    attribute, value, confidence, status, source_snippet, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    fact["id"], session_id, body.org_id,
                    fact["entity_type"], fact["entity_value"],
                    fact["attribute"], fact["value"], fact["confidence"],
                    fact["source_snippet"], now,
                ),
            )

        committed_facts.append(fact)

    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'done' WHERE id = ?",
            (session_id,),
        )

    return {
        "session_id": session_id,
        "org_id": body.org_id,
        "facts_extracted": len(committed_facts),
        "facts": committed_facts,
    }


@app.get("/inject-context", dependencies=[Depends(require_api_key)])
def inject_context(org_id: str = Query(..., description="Organisation ID")):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM facts WHERE org_id = ? AND status = 'approved'",
            (org_id,),
        ).fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No approved facts found for org_id '{org_id}'. Approve some facts first via POST /facts/{{id}}/approve.",
        )

    facts = fact_rows_to_list(rows)
    agent = WriterAgent(api_key=OPENAI_API_KEY)
    context_block = agent.generate_context(org_id=org_id, facts=facts)

    return {
        "org_id": org_id,
        "context_block": context_block,
        "facts_used": len(facts),
    }


@app.get("/facts/pending", dependencies=[Depends(require_api_key)])
def list_pending_facts(org_id: Optional[str] = Query(None)):
    with get_db() as conn:
        if org_id:
            rows = conn.execute(
                "SELECT * FROM facts WHERE status = 'pending' AND org_id = ? ORDER BY created_at DESC",
                (org_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM facts WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()

    return {"pending_facts": fact_rows_to_list(rows), "count": len(rows)}


@app.post("/facts/{fact_id}/approve", dependencies=[Depends(require_api_key)])
def approve_fact(fact_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Fact '{fact_id}' not found")

        conn.execute("UPDATE facts SET status = 'approved' WHERE id = ?", (fact_id,))

        updated = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()

    return {"message": "Fact approved", "fact": row_to_dict(updated)}


@app.post("/facts/{fact_id}/reject", dependencies=[Depends(require_api_key)])
def reject_fact(fact_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Fact '{fact_id}' not found")

        conn.execute("UPDATE facts SET status = 'rejected' WHERE id = ?", (fact_id,))

        updated = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()

    return {"message": "Fact rejected", "fact": row_to_dict(updated)}


@app.get("/health")
def health():
    return {"status": "ok"}
