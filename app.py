#!/usr/bin/env python3
"""
네이버 키워드 검색량 조회 — 공개 웹 서비스
==========================================
누구나 접속해서 키워드를 조회할 수 있는 서버입니다.
비밀키는 서버 안에만 있고, 브라우저로 절대 나가지 않습니다.

보호 장치
---------
- SQLite 캐시: 같은 키워드는 24시간 동안 재호출하지 않음
- IP별 레이트리밋: 10분에 20회
- 일일 상한: 하루 업스트림 호출 총량 제한 (광고 계정 쿼터 보호)
- 입력 제한: 1회 최대 3개 키워드, 키워드당 30자

실행
----
    pip install -r requirements.txt
    cp .env.example .env      # 값 채우기
    python app.py             # 개발용
    gunicorn -w 2 -b 0.0.0.0:8000 app:app   # 배포용
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from datetime import date

import requests
from flask import Flask, jsonify, request, send_from_directory

# ------------------------------------------------------------------ 설정
BASE_URL = "https://api.searchad.naver.com"
URI = "/keywordstool"
METHOD = "GET"

DB_PATH = os.getenv("DB_PATH", "keyword_cache.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", 86400))          # 24시간
RATE_WINDOW = int(os.getenv("RATE_WINDOW", 600))        # 10분
RATE_LIMIT = int(os.getenv("RATE_LIMIT", 20))           # 창당 20회
DAILY_UPSTREAM_CAP = int(os.getenv("DAILY_UPSTREAM_CAP", 3000))
MAX_KEYWORDS = 3
MAX_KEYWORD_LEN = 30

DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
TREND_MONTHS = int(os.getenv("TREND_MONTHS", 12))
TREND_TTL = int(os.getenv("TREND_TTL", 604800))   # 7일 (월간 데이터라 자주 안 변함)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


# ------------------------------------------------------------------ 저장소
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key     TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hits (
                ip TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS hits_ts ON hits(ts);
            CREATE TABLE IF NOT EXISTS upstream (
                day   TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            );
            """
        )


def cache_get(key):
    ttl = TREND_TTL if key.startswith("TREND:") else CACHE_TTL
    with db() as conn:
        row = conn.execute(
            "SELECT payload, created FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row and time.time() - row[1] < ttl:
        return json.loads(row[0])
    return None


def cache_put(key, value):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, payload, created) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else request.remote_addr or "?"


def rate_limited(ip):
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM hits WHERE ts < ?", (now - RATE_WINDOW,))
        count = conn.execute(
            "SELECT COUNT(*) FROM hits WHERE ip = ?", (ip,)
        ).fetchone()[0]
        if count >= RATE_LIMIT:
            return True
        conn.execute("INSERT INTO hits (ip, ts) VALUES (?, ?)", (ip, now))
    return False


def upstream_allowed(n=1):
    """오늘 업스트림 호출이 상한을 넘지 않았는지 확인하고 카운트."""
    day = time.strftime("%Y-%m-%d")
    with db() as conn:
        row = conn.execute(
            "SELECT count FROM upstream WHERE day = ?", (day,)
        ).fetchone()
        used = row[0] if row else 0
        if used + n > DAILY_UPSTREAM_CAP:
            return False
        conn.execute(
            "INSERT INTO upstream (day, count) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET count = count + ?",
            (day, n, n),
        )
    return True


# ------------------------------------------------------------------ 네이버 API
def credentials():
    return (
        os.getenv("NAVER_API_KEY"),
        os.getenv("NAVER_SECRET_KEY"),
        os.getenv("NAVER_CUSTOMER_ID"),
    )


def make_signature(secret_key, timestamp):
    message = "{}.{}.{}".format(timestamp, METHOD, URI)
    digest = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def normalize(keyword):
    return re.sub(r"\s+", "", keyword)


def to_int(value):
    if isinstance(value, (int, float)):
        return round(value)
    if value is None:
        return 0
    text = str(value).strip()
    if text.startswith("<"):
        return 9
    try:
        return round(float(text.replace(",", "")))
    except ValueError:
        return 0


def call_naver(keyword):
    api_key, secret_key, customer_id = credentials()
    timestamp = str(round(time.time() * 1000))
    res = requests.get(
        BASE_URL + URI,
        params={"hintKeywords": normalize(keyword), "showDetail": "1"},
        headers={
            "X-Timestamp": timestamp,
            "X-API-KEY": api_key,
            "X-Customer": str(customer_id),
            "X-Signature": make_signature(secret_key, timestamp),
        },
        timeout=15,
    )
    if res.status_code in (401, 403):
        raise RuntimeError("서버의 API 인증이 만료됐습니다. 운영자에게 알려주세요.")
    if res.status_code == 429:
        raise RuntimeError("조회가 몰리고 있습니다. 1분쯤 뒤에 다시 시도하세요.")
    res.raise_for_status()

    rows = []
    for item in res.json().get("keywordList", []):
        pc = to_int(item.get("monthlyPcQcCnt"))
        mo = to_int(item.get("monthlyMobileQcCnt"))
        rows.append(
            {
                "keyword": item.get("relKeyword", ""),
                "pc": pc,
                "mobile": mo,
                "total": pc + mo,
                "comp": item.get("compIdx", "-"),
                "depth": to_int(item.get("plAvgDepth")),
            }
        )
    return rows


def lookup(keyword):
    """캐시 우선 조회. 캐시에 없으면 네이버 호출."""
    key = normalize(keyword).upper()
    cached = cache_get(key)
    if cached is not None:
        return cached, True
    if not upstream_allowed():
        raise RuntimeError("오늘 조회 한도를 모두 썼습니다. 내일 다시 이용해 주세요.")
    rows = call_naver(keyword)
    cache_put(key, rows)
    return rows, False


# ------------------------------------------------------------------ 월간 추이 (데이터랩)
def datalab_credentials():
    return (os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET"))


def month_window():
    """지난달 말일까지, TREND_MONTHS 개월치 구간을 만든다.
    이번 달은 아직 안 끝나서 값이 낮게 나오므로 제외한다."""
    today = date.today()
    end = date(today.year, today.month, 1) - __import__("datetime").timedelta(days=1)
    y, m = end.year, end.month - (TREND_MONTHS - 1)
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1).isoformat(), end.isoformat()


def fetch_trend(keywords):
    """데이터랩에서 월별 상대 비율을 가져온다. {키워드: [(월, 비율)]}"""
    client_id, client_secret = datalab_credentials()
    start, end = month_window()
    body = {
        "startDate": start,
        "endDate": end,
        "timeUnit": "month",
        "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords[:5]],
    }
    res = requests.post(
        DATALAB_URL,
        json=body,
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    if res.status_code in (401, 403):
        raise RuntimeError("데이터랩 인증이 만료됐습니다. 운영자에게 알려주세요.")
    if res.status_code == 429:
        raise RuntimeError("추이 조회가 몰리고 있습니다. 잠시 뒤 다시 시도하세요.")
    res.raise_for_status()

    out = {}
    for group in res.json().get("results", []):
        out[group.get("title", "")] = [
            (p.get("period", "")[:7], float(p.get("ratio", 0)))
            for p in group.get("data", [])
        ]
    return out


def trend_for(keyword, total):
    """월별 추정 검색량. 마지막 달 비율을 현재 검색량에 맞춰 환산한다."""
    if not all(datalab_credentials()):
        return None
    key = "TREND:" + normalize(keyword).upper()
    series = cache_get(key)
    if series is None:
        if not upstream_allowed():
            return None
        series = fetch_trend([keyword]).get(keyword, [])
        cache_put(key, series)
    if not series:
        return None

    base = series[-1][1] or max((r for _, r in series), default=0)
    if not base:
        return None
    return [
        {"month": month, "ratio": round(ratio, 1),
         "estimate": round(total * ratio / base)}
        for month, ratio in series
    ]


# ------------------------------------------------------------------ 라우트
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": all(credentials()), "trend": all(datalab_credentials())})


@app.post("/api/search")
def api_search():
    if not all(credentials()):
        return jsonify({"error": "서버가 아직 설정되지 않았습니다. 운영자에게 알려주세요."}), 503

    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"error": "잠시 뒤에 다시 조회해 주세요. 쇧은 시간에 너무 많이 요청했습니다."}), 429

    body = request.get_json(silent=True) or {}
    raw = body.get("keywords", [])
    if isinstance(raw, str):
        raw = raw.split(",")

    keywords, seen = [], set()
    for item in raw:
        text = str(item).strip()[:MAX_KEYWORD_LEN]
        norm = normalize(text).upper()
        if text and norm not in seen:
            seen.add(norm)
            keywords.append(text)
    keywords = keywords[:MAX_KEYWORDS]

    if not keywords:
        return jsonify({"error": "키워드를 입력해 주세요."}), 400

    include_related = bool(body.get("related"))
    wanted = {normalize(k).upper() for k in keywords}

    rows, from_cache = [], True
    try:
        for keyword in keywords:
            found, cached = lookup(keyword)
            rows.extend(found)
            from_cache = from_cache and cached
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 429
    except requests.RequestException:
        return jsonify({"error": "네이버 서버와 통신하지 못했습니다. 잠시 뒤 다시 시도해 주세요."}), 502

    if not include_related:
        rows = [r for r in rows if normalize(r["keyword"]).upper() in wanted]

    unique, seen_kw = [], set()
    for r in sorted(rows, key=lambda x: x["total"], reverse=True):
        if r["keyword"] not in seen_kw:
            seen_kw.add(r["keyword"])
            unique.append(r)

    trends = {}
    for keyword in keywords:
        norm = normalize(keyword).upper()
        match = next((r for r in unique if normalize(r["keyword"]).upper() == norm), None)
        if not match:
            continue
        try:
            series = trend_for(keyword, match["total"])
        except (RuntimeError, requests.RequestException):
            series = None
        if series:
            trends[match["keyword"]] = series

    return jsonify({"rows": unique[:200], "cached": from_cache, "trends": trends})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
