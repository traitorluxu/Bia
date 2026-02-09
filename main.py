import os
import hmac
from typing import Optional, List, Dict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# Optional DB support (falls back to in-memory if DATABASE_URL isn't set or psycopg missing)
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

# In-memory fallback (won't persist across redeploys)
_mem_history: Dict[str, List[Dict[str, str]]] = {}
_mem_notes: Dict[str, List[str]] = {}


def get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Don’t crash the whole server on import/startup, only fail when /chat is called
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")
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
            cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY,
                ses
