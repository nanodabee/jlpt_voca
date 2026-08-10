from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL

try:
    import psycopg
except ImportError:
    psycopg = None

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "jlpt_vocab.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="JLPT Vocabulary Private App")




class StateRequest(BaseModel):
    state: dict[str, Any]


class YouTubeRequest(BaseModel):
    url: str


def using_postgres() -> bool:
    return bool(DATABASE_URL)


def ensure_storage():
    if using_postgres():
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is configured.")
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_state (
                    id INTEGER PRIMARY KEY,
                    payload JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            conn.commit()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id=1),
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()


def load_state():
    ensure_storage()
    if using_postgres():
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
            return row[0] if row else None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def save_state(state: dict[str, Any]):
    ensure_storage()
    if using_postgres():
        payload = json.dumps(state, ensure_ascii=False)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("""
                INSERT INTO app_state(id, payload, updated_at)
                VALUES(1, %s::jsonb, NOW())
                ON CONFLICT(id) DO UPDATE
                SET payload=EXCLUDED.payload, updated_at=NOW()
            """, (payload,))
            conn.commit()
        return
    payload = json.dumps(state, ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO app_state(id,payload,updated_at)
        VALUES(1,?,CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP
    """, (payload,))
    conn.commit()
    conn.close()


@app.get("/")
def index():
    return FileResponse(APP_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "database": "postgresql" if using_postgres() else "sqlite"
    }


@app.get("/api/state")
def get_state():
    return {"state": load_state()}


@app.put("/api/state")
def put_state(req: StateRequest):
    save_state(req.state)
    return {"ok": True}

def choose_subtitle(info: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def pick_from(group: dict[str, Any], kind: str):
        if not group:
            return None
        preferred = ["ja", "ja-JP", "ko", "ko-KR"]
        keys = list(group.keys())
        ordered = preferred + [k for k in keys if k not in preferred and not k.startswith("live_chat")]
        for lang in ordered:
            formats = group.get(lang)
            if not formats:
                continue
            for ext in ("json3", "srv3", "vtt", "ttml"):
                for f in formats:
                    if f.get("ext") == ext and f.get("url"):
                        return lang, f, kind
            for f in formats:
                if f.get("url"):
                    return lang, f, kind
        return None

    picked = pick_from(manual, "수동 자막") or pick_from(auto, "자동 생성 자막")
    if not picked:
        raise HTTPException(status_code=422, detail="가져올 수 있는 자막을 찾지 못했습니다.")
    return picked


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_json3(raw: str) -> str:
    data = json.loads(raw)
    lines = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).replace("\n", " ").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def strip_vtt_or_xml(raw: str) -> str:
    raw = re.sub(r"WEBVTT.*?\n", "", raw, flags=re.S)
    raw = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d+\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d+.*", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    out, last = [], None
    for line in raw.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.isdigit() or line == last:
            continue
        out.append(line)
        last = line
    return "\n".join(out)


def extract_candidates(transcript: str) -> list[dict[str, str]]:
    lines = [re.sub(r"\s+", " ", x).strip() for x in transcript.splitlines() if x.strip()]
    found, seen = [], set()

    def add(word: str, reading: str, meaning: str):
        word, reading, meaning = word.strip(), reading.strip(), meaning.strip()
        if not re.search(r"[一-龯々]", word): return
        if not re.fullmatch(r"[ぁ-んァ-ヶー]+", reading): return
        if not re.search(r"[가-힣]", meaning): return
        key = (word, reading)
        if key in seen: return
        seen.add(key)
        found.append({"word": word, "reading": reading, "meaning": meaning})

    p1 = re.compile(r"([一-龯々ぁ-んァ-ヶー]+)\s*[（(]([ぁ-んァ-ヶー]+)[）)]\s*[-–—:：]?\s*([가-힣].*)$")
    p2 = re.compile(r"([一-龯々]+[ぁ-んァ-ヶー]*)\s+([ぁ-んァ-ヶー]+)\s+([가-힣].*)$")
    for line in lines:
        clean = re.sub(r"^\s*(?:\d+[.)]|[-•▪︎●])\s*", "", line)
        m = p1.search(clean) or p2.search(clean)
        if m:
            add(m.group(1), m.group(2), m.group(3))
    for i in range(len(lines)-2):
        a,b,c = lines[i:i+3]
        if re.fullmatch(r"[一-龯々ぁ-んァ-ヶー]+", a) and re.search(r"[一-龯々]", a):
            if re.fullmatch(r"[ぁ-んァ-ヶー]+", b) and re.search(r"[가-힣]", c):
                add(a,b,c)
    return found


@app.post("/api/youtube/import")
def import_youtube(req: YouTubeRequest):
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"YouTube 정보를 가져오지 못했습니다: {e}")

    if not isinstance(info, dict):
        raise HTTPException(status_code=422, detail="영상 정보를 읽을 수 없습니다.")

    lang, fmt, kind = choose_subtitle(info)
    try:
        raw = fetch_text(fmt["url"])
        transcript = parse_json3(raw) if fmt.get("ext") == "json3" else strip_vtt_or_xml(raw)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"자막을 읽지 못했습니다: {e}")

    return {
        "title": info.get("title"),
        "video_id": info.get("id"),
        "language": lang,
        "subtitle_type": kind,
        "transcript": transcript,
        "candidates": extract_candidates(transcript),
    }
