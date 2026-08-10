from __future__ import annotations

import html
import csv
import io
import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from pypdf import PdfReader

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


class TextAnalyzeRequest(BaseModel):
    text: str


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


MAX_IMPORT_BYTES = 20 * 1024 * 1024

HEADER_ALIASES = {
    "word": {"word","단어","일본어","한자","표기","語彙","単語","漢字"},
    "reading": {"reading","읽기","요미가나","발음","かな","よみ","読み","ふりがな","フリガナ"},
    "meaning": {"meaning","뜻","의미","한국어","번역","意味","뜻/의미"},
    "level": {"level","레벨","jlpt","급수","분류"},
    "example": {"example","예문","例文"},
}

def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-./]+", "", (value or "").strip().lower())

def canonical_header(value: str) -> str | None:
    n = normalize_header(value)
    for key, aliases in HEADER_ALIASES.items():
        if n in {normalize_header(a) for a in aliases}:
            return key
    return None

def normalize_level(value: str | None) -> str:
    v = (value or "").strip().upper().replace(" ", "")
    if v in {"N5","N4","N3","N2","N1"}:
        return v
    if v in {"BASIC","기초","N5/N4","N4/N5","기초일본어단어(N5/N4)"}:
        return "basic"  # legacy / 미분류
    return ""

def candidate(word="", reading="", meaning="", level="", example=""):
    return {
        "word": str(word or "").strip(),
        "reading": str(reading or "").strip(),
        "meaning": str(meaning or "").strip(),
        "level": normalize_level(level),
        "example": str(example or "").strip(),
    }

def is_useful_candidate(c: dict[str, str]) -> bool:
    return bool(c["word"] and c["reading"] and c["meaning"])

def dedupe_candidates(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out, seen = [], set()
    for c in items:
        if not is_useful_candidate(c):
            continue
        key = (c["word"], c["reading"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

def decode_text_bytes(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8", "cp949", "shift_jis"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    raise HTTPException(status_code=422, detail="CSV 문자 인코딩을 읽을 수 없습니다. UTF-8 CSV로 저장해서 다시 시도해주세요.")

def parse_csv_candidates(text: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    sample = text[:10000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel

    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return [], {"has_header": False, "columns": []}

    header_map: dict[int, str] = {}
    for idx, cell in enumerate(rows[0]):
        key = canonical_header(cell)
        if key:
            header_map[idx] = key

    has_header = {"word","reading","meaning"}.issubset(set(header_map.values()))
    data_rows = rows[1:] if has_header else rows
    items: list[dict[str, str]] = []

    if has_header:
        for row in data_rows:
            vals = {"word":"","reading":"","meaning":"","level":"","example":""}
            for idx, key in header_map.items():
                if idx < len(row):
                    vals[key] = row[idx]
            items.append(candidate(**vals))
    else:
        # Headerless CSV: first three columns = word / reading / meaning, optional 4th=level, 5th=example
        for row in data_rows:
            if len(row) < 3:
                continue
            items.append(candidate(
                word=row[0],
                reading=row[1],
                meaning=row[2],
                level=row[3] if len(row) > 3 else "",
                example=row[4] if len(row) > 4 else "",
            ))

    return dedupe_candidates([autofill_kana_reading(c) for c in items]), {
        "has_header": has_header,
        "columns": rows[0] if has_header else [],
        "row_count": len(data_rows),
    }


KANA_ONLY_RE = re.compile(r"^[ぁ-ゖァ-ヺー・]+$")

def is_kana_only(value: str) -> bool:
    return bool(KANA_ONLY_RE.fullmatch((value or "").strip()))

def autofill_kana_reading(c: dict[str, str]) -> dict[str, str]:
    # If the written form is entirely kana, its reading is the same string.
    if c.get("word") and not c.get("reading") and is_kana_only(c["word"]):
        c["reading"] = c["word"]
    return c

def extract_vocab_candidates_from_text(text: str) -> list[dict[str, str]]:
    """
    Supported examples:
      経験（けいけん） 경험, 체험
      経験 けいけん 경험, 체험
      お母さん おかあさん 어머니
      お菓子 おかし 과자
      お金 おかね 돈
      食べる たべる 먹다
      新しい あたらしい 새롭다
      上げる あげる(무엇을) 올리다
      乗る のる（탈것에）타다
      あまり 별로, 그다지
      ホテル 호텔
      3-line: word / reading / meaning
      kana-only 2-line: word / meaning
    """
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    found: list[dict[str, str]] = []

    # Japanese surface form may freely mix kanji / hiragana / katakana.
    jp_word = r"(?:[-－~〜～]?[一-龯々ぁ-ゖァ-ヺー・]+)"
    kana = r"(?:[-－~〜～]?[ぁ-ゖァ-ヺー・]+)"

    # 1) word(reading) meaning
    p_paren_reading = re.compile(
        rf"^({jp_word})\s*[（(]({kana})[）)]\s*[-–—:：]?\s*(.+)$"
    )

    # 2) mixed-Japanese headword + explicit kana reading + meaning
    # Crucial fix: first field no longer has to begin with kanji.
    p_explicit_reading = re.compile(
        rf"^({jp_word})\s+({kana})(?=\s|[（(\[【〔〈《「『])\s*(.+)$"
    )

    # 3) pipe/tab separated
    p_sep = re.compile(
        rf"^({jp_word})\s*[\t|｜]\s*({kana})\s*[\t|｜]\s*(.+)$"
    )

    # 4) kana-only word + meaning
    p_kana_only = re.compile(
        rf"^({kana})(?=\s|[（(\[【〔〈《「『])\s*(.+)$"
    )

    for line in lines:
        clean = re.sub(r"^\s*(?:\d+[.)]\s*|[•▪︎●■□★☆◆◇▶▷※○◎✓✔]\s*|[-－]\s+)", "", line)

        matched = False
        for pat in (p_paren_reading, p_explicit_reading, p_sep):
            mm = pat.search(clean)
            if mm:
                meaning = mm.group(3).strip()
                if re.search(r"[가-힣]", meaning):
                    found.append(candidate(mm.group(1), mm.group(2), meaning))
                matched = True
                break
        if matched:
            continue

        mm = p_kana_only.search(clean)
        if mm:
            word = mm.group(1).strip()
            meaning = mm.group(2).strip()

            # Handle legacy duplicated kana input like: ホテル ホテル 호텔
            dup = re.match(rf"^({kana})\s+(.+)$", meaning)
            if dup and dup.group(1) == word and re.search(r"[가-힣]", dup.group(2)):
                meaning = dup.group(2).strip()

            if re.search(r"[가-힣]", meaning):
                found.append(candidate(word, word, meaning))

    # 3-line format: word / reading / meaning
    for i in range(len(lines) - 2):
        a, b, c = lines[i:i+3]
        if re.fullmatch(jp_word, a) and re.fullmatch(kana, b) and re.search(r"[가-힣]", c):
            found.append(candidate(a, b, c))

    # kana-only 2-line format: word / meaning
    for i in range(len(lines) - 1):
        a, b = lines[i:i+2]
        if re.fullmatch(kana, a) and re.search(r"[가-힣]", b):
            found.append(candidate(a, a, b))

    return dedupe_candidates([autofill_kana_reading(c) for c in found])


@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="CSV 파일은 20MB 이하만 업로드할 수 있습니다.")
    if not data:
        raise HTTPException(status_code=422, detail="빈 CSV 파일입니다.")

    text, encoding = decode_text_bytes(data)
    items, meta = parse_csv_candidates(text)
    return {
        "filename": file.filename,
        "encoding": encoding,
        "candidates": items,
        "candidate_count": len(items),
        **meta,
    }




def diagnose_vocab_lines(text: str) -> list[dict[str, Any]]:
    raw_lines = text.splitlines()
    results: list[dict[str, Any]] = []

    # Use the same parser per-line where possible. Also support grouped 2/3-line formats.
    nonempty = [(i + 1, line.strip()) for i, line in enumerate(raw_lines) if line.strip()]
    consumed: set[int] = set()

    # First pass: one-line formats.
    for line_no, line in nonempty:
        parsed = extract_vocab_candidates_from_text(line)
        if parsed:
            c = parsed[0]
            results.append({
                "line_no": line_no,
                "text": line,
                "status": "recognized",
                "word": c.get("word",""),
                "reading": c.get("reading",""),
                "meaning": c.get("meaning",""),
            })
            consumed.add(line_no)

    # Second pass: grouped formats across adjacent non-empty lines.
    for idx in range(len(nonempty)):
        line_no, a = nonempty[idx]
        if line_no in consumed:
            continue

        # 3-line: word / reading / meaning
        if idx + 2 < len(nonempty):
            n2, b = nonempty[idx + 1]
            n3, ctext = nonempty[idx + 2]
            if n2 not in consumed and n3 not in consumed:
                joined = f"{a}\n{b}\n{ctext}"
                parsed = extract_vocab_candidates_from_text(joined)
                if parsed:
                    c = parsed[0]
                    results.append({
                        "line_no": line_no,
                        "line_end": n3,
                        "text": joined,
                        "status": "recognized_group",
                        "word": c.get("word",""),
                        "reading": c.get("reading",""),
                        "meaning": c.get("meaning",""),
                    })
                    consumed.update({line_no, n2, n3})
                    continue

        # 2-line kana-only: word / meaning
        if idx + 1 < len(nonempty):
            n2, b = nonempty[idx + 1]
            if n2 not in consumed:
                joined = f"{a}\n{b}"
                parsed = extract_vocab_candidates_from_text(joined)
                if parsed:
                    c = parsed[0]
                    results.append({
                        "line_no": line_no,
                        "line_end": n2,
                        "text": joined,
                        "status": "recognized_group",
                        "word": c.get("word",""),
                        "reading": c.get("reading",""),
                        "meaning": c.get("meaning",""),
                    })
                    consumed.update({line_no, n2})

    # Any remaining non-empty lines are unrecognized.
    for line_no, line in nonempty:
        if line_no not in consumed:
            results.append({
                "line_no": line_no,
                "text": line,
                "status": "unrecognized",
                "word": "",
                "reading": "",
                "meaning": "",
            })

    results.sort(key=lambda x: x["line_no"])
    return results


@app.post("/api/import/diagnose")
def diagnose_import_text(req: TextAnalyzeRequest):
    text = (req.text or "").strip("\n")
    if not text.strip():
        return {"lines": [], "recognized": 0, "unrecognized": 0}
    lines = diagnose_vocab_lines(text)
    recognized = sum(1 for x in lines if x["status"].startswith("recognized"))
    unrecognized = sum(1 for x in lines if x["status"] == "unrecognized")
    return {
        "lines": lines,
        "recognized": recognized,
        "unrecognized": unrecognized,
    }



@app.post("/api/import/reanalyze")
def reanalyze_import_text(req: TextAnalyzeRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="분석할 텍스트가 없습니다.")
    items = extract_vocab_candidates_from_text(text)
    return {
        "candidate_count": len(items),
        "candidates": items,
    }



@app.post("/api/import/pdf")
async def import_pdf(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="PDF 파일은 20MB 이하만 업로드할 수 있습니다.")
    if not data:
        raise HTTPException(status_code=422, detail="빈 PDF 파일입니다.")

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"PDF를 열 수 없습니다: {e}")

    page_texts = []
    for page in reader.pages:
        try:
            page_texts.append(page.extract_text() or "")
        except Exception:
            page_texts.append("")

    full_text = "\n".join(page_texts).strip()
    if not full_text:
        raise HTTPException(
            status_code=422,
            detail="이 PDF에서는 텍스트를 추출할 수 없습니다. 스캔 이미지형 PDF일 가능성이 큽니다. 텍스트 선택이 가능한 PDF를 사용해주세요."
        )

    items = extract_vocab_candidates_from_text(full_text)
    return {
        "filename": file.filename,
        "page_count": len(reader.pages),
        "text_char_count": len(full_text),
        "candidate_count": len(items),
        "candidates": items,
        # 사용자가 자동 추출 실패 시 확인/복사할 수 있도록 일부가 아니라 전체 텍스트 반환.
        "extracted_text": full_text,
        "message": "자동 후보가 적거나 없으면 추출 텍스트를 확인해 직접 수정 후 등록할 수 있습니다."
    }


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
        if not re.fullmatch(r"[ぁ-ゖァ-ヺー・]+", reading): return
        if not re.search(r"[가-힣]", meaning): return
        key = (word, reading)
        if key in seen: return
        seen.add(key)
        found.append({"word": word, "reading": reading, "meaning": meaning})

    p1 = re.compile(r"([一-龯々ぁ-んァ-ヶー]+)\s*[（(]([ぁ-ゖァ-ヺー・]+)[）)]\s*[-–—:：]?\s*([가-힣].*)$")
    p2 = re.compile(r"([一-龯々]+[ぁ-んァ-ヶー]*)\s+([ぁ-ゖァ-ヺー・]+)\s+([가-힣].*)$")
    for line in lines:
        clean = re.sub(r"^\s*(?:\d+[.)]|[-•▪︎●])\s*", "", line)
        m = p1.search(clean) or p2.search(clean)
        if m:
            add(m.group(1), m.group(2), m.group(3))
    for i in range(len(lines)-2):
        a,b,c = lines[i:i+3]
        if re.fullmatch(r"[一-龯々ぁ-んァ-ヶー]+", a) and re.search(r"[一-龯々]", a):
            if re.fullmatch(r"[ぁ-ゖァ-ヺー・]+", b) and re.search(r"[가-힣]", c):
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
