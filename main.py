import os
import hmac
from typing import Optional, List, Dict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# Optional: local dev support (.env). Harmless on Railway.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Optional DB support (falls back to in-memory if DATABASE_URL isn't set)
try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
BIA_API_TOKEN = os.getenv("BIA_API_TOKEN", "")
BIA_BASE_PROMPT = os.getenv("BIA_BASE_PROMPT", "You are Bia. Stay in voice.")
DATABASE_URL = os.getenv("DATABASE_URL", "")

app = FastAPI(title="Bia Gateway")

# In-memory fallback (works immediately, but won't persist across redeploys)
_mem_history: Dict[str, List[Dict[str, str]]] = {}
_mem_notes: Dict[str, List[str]] = {}


def get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set on server")
    return OpenAI(api_key=api_key)


def require_auth(auth_header: Optional[str]) -> None:
    if not BIA_API_TOKEN:
        raise HTTPException(status_code=500, detail="Server missing BIA_API_TOKEN")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, BIA_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")


def db_enabled() -> bool:
    return bool(DATABASE_URL) and psycopg is not None


def db_conn():
    if not db_enabled():
        raise RuntimeError("DB not enabled")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    if not db_enabled():
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_messages ("
                "id BIGSERIAL PRIMARY KEY, "
                "session_id TEXT NOT NULL, "
                "role TEXT NOT NULL, "
                "content TEXT NOT NULL, "
                "created_at TIMESTAMPTZ DEFAULT now()"
                ");"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS memory_notes ("
                "id BIGSERIAL PRIMARY KEY, "
                "session_id TEXT NOT NULL, "
                "note TEXT NOT NULL, "
                "created_at TIMESTAMPTZ DEFAULT now()"
                ");"
            )
        conn.commit()


@app.on_event("startup")
def on_startup():
    init_db()


class ChatIn(BaseModel):
    session_id: str
    message: str
    max_history: int = 20


class ChatOut(BaseModel):
    session_id: str
    reply: str


def fetch_history(session_id: str, max_history: int) -> List[Dict[str, str]]:
    if db_enabled():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role, content FROM chat_messages "
                    "WHERE session_id=%s ORDER BY created_at DESC LIMIT %s",
                    (session_id, max_history),
                )
                rows = cur.fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    hist = _mem_history.get(session_id, [])
    return hist[-max_history:]


def add_message(session_id: str, role: str, content: str) -> None:
    if db_enabled():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)",
                    (session_id, role, content),
                )
            conn.commit()
        return

    _mem_history.setdefault(session_id, []).append({"role": role, "content": content})


def add_note(session_id: str, note: str) -> None:
    if db_enabled():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO memory_notes (session_id, note) VALUES (%s, %s)",
                    (session_id, note),
                )
            conn.commit()
        return

    _mem_notes.setdefault(session_id, []).append(note)


def fetch_notes(session_id: str, limit: int = 50) -> List[str]:
    if db_enabled():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT note FROM memory_notes WHERE session_id=%s ORDER BY created_at DESC LIMIT %s",
                    (session_id, limit),
                )
                notes = [r["note"] for r in cur.fetchall()]
        notes.reverse()
        return notes

    notes = _mem_notes.get(session_id, [])
    return notes[-limit:]


@app.get("/health")
def health():
    return {"ok": True, "db": db_enabled(), "model": OPENAI_MODEL}


@app.post("/chat", response_model=ChatOut)
def chat(payload: ChatIn, authorization: Optional[str] = Header(default=None)):
    require_auth(authorization)

    session_id = payload.session_id.strip()
    msg = payload.message.strip()

    # Command: /remember ...
    if msg.lower().startswith("/remember "):
        note = msg[len("/remember "):].strip()
        if note:
            add_note(session_id, note)
            return ChatOut(session_id=session_id, reply="Noted.")
        return ChatOut(session_id=session_id, reply="Nothing to remember.")

    add_message(session_id, "user", msg)

    notes = fetch_notes(session_id)
    instructions = BIA_BASE_PROMPT
    if notes:
        instructions += "\n\nLong-term memory notes:\n" + "\n".join(f"- {n}" for n in notes)

    history = fetch_history(session_id, payload.max_history)
    if not history:
        history = [{"role": "user", "content": msg}]

    client = get_client()

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=history,
            store=False,
        )
    except Exception as e:
        # This will show up in Railway logs and helps you debug quickly
        raise HTTPException(status_code=500, detail=f"OpenAI call failed: {type(e).__name__}: {str(e)[:300]}")

    reply = (resp.output_text or "").strip() or "Got a blank response. Try again."
    add_message(session_id, "assistant", reply)
    return ChatOut(session_id=session_id, reply=reply)
