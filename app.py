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

# NAVER API HUB (클라우드 플랫폼). 개발자센터와 주소·헤더가 다르다.
APIHUB_BASE = "https://naverapihub.apigw.ntruss.com"
DATALAB_URL = APIHUB_BASE + "/search-trend/v1/search"
TREND_MONTHS = int(os.getenv("TREND_MONTHS", 36))   # 3년 — 계절성을 보려면 2년으로는 부족
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


def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


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
                "ctr": round(
                    (to_float(item.get("monthlyAvePcCtr"))
                     + to_float(item.get("monthlyAveMobileCtr"))) / 2, 2
                ),
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
    """API HUB 애플리케이션의 Client ID / Secret."""
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
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    if res.status_code in (401, 403):
        raise RuntimeError("검색어 트렌드 인증에 실패했습니다. 운영자에게 알려주세요.")
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


def trend_stats(series):
    """월별 추정치에서 연간 흐름과 계절성을 뽑는다."""
    if len(series) < 13:
        return None
    est = [p["estimate"] for p in series]

    def window(back):
        """back개월 전부터 12개월치 합. 데이터가 모자라면 None."""
        end = len(est) - back
        start = end - 12
        return sum(est[start:end]) if start >= 0 else None

    last12, prev12, prev24 = window(0), window(12), window(24)
    yoy = ((last12 - prev12) / prev12 * 100) if prev12 else None
    yoy_prev = ((prev12 - prev24) / prev24 * 100) if prev24 else None

    # 계절성: 같은 달끼리 평균. 완결된 해가 있어야 의미가 있다.
    by_month = {}
    for p in series:
        m = int(p["month"][5:7])
        by_month.setdefault(m, []).append(p["estimate"])
    avg = {m: sum(v) / len(v) for m, v in by_month.items() if len(v) >= 2}
    peak = low = None
    if len(avg) >= 6:
        peak = max(avg, key=avg.get)
        low = min(avg, key=avg.get)

    # 연도별 합계 (완결 여부 표시)
    years = {}
    for p in series:
        years.setdefault(p["month"][:4], []).append(p["estimate"])
    yearly = [{"year": y, "total": sum(v), "months": len(v), "full": len(v) == 12}
              for y, v in sorted(years.items())]

    return {
        "last12": last12, "prev12": prev12, "prev24": prev24,
        "yoy": round(yoy, 1) if yoy is not None else None,
        "yoy_prev": round(yoy_prev, 1) if yoy_prev is not None else None,
        "peak_month": peak, "low_month": low,
        "peak_avg": round(avg[peak]) if peak else None,
        "low_avg": round(avg[low]) if low else None,
        "yearly": yearly,
    }


def trend_for(keyword, total):
    """월별 추정 검색량. 마지막 달 비율을 현재 검색량에 맞춰 환산한다."""
    if not all(datalab_credentials()):
        return None
    # 기간을 키에 넣어 TREND_MONTHS 를 바꾸면 옛 캐시를 자동으로 버리게 한다
    key = "TREND:{}:".format(TREND_MONTHS) + normalize(keyword).upper()
    series = cache_get(key)
    if not series:
        if not upstream_allowed():
            return None
        groups = fetch_trend([keyword])
        # 응답 title 이 요청 키워드와 다를 수 있어 첫 그룹으로도 받는다
        series = groups.get(keyword) or (list(groups.values())[0] if groups else [])
        if not series:
            return None          # 빈 결과는 캐시하지 않는다
        cache_put(key, series)

    base = series[-1][1] or max((r for _, r in series), default=0)
    if not base:
        return None
    return [
        {"month": month, "ratio": round(ratio, 1),
         "estimate": round(total * ratio / base)}
        for month, ratio in series
    ]


# ------------------------------------------------------------------ 빈틈 키워드
# 문제 해결 의도가 뚜렷한 표현. 단순 '추천/후기'는 탐색형이라 제외한다.
PROBLEM_WORDS = [
    "방법", "없애", "제거", "해결", "원인", "안될", "안돼", "고장", "심할", "너무",
    "대처", "예방", "완화", "줄이", "막는", "왜", "안나", "실패", "부작용", "차이",
    "대신", "직접", "셀프", "잘못", "문제",
]
GAP_MAX_SEED = 3          # 2차 확장에 쓸 씨앗 키워드 수
GAP_MIN_VOLUME = 100      # 이보다 적으면 노이즈로 본다


def is_problem(keyword):
    return any(w in keyword for w in PROBLEM_WORDS)


def gap_score(row):
    """수요는 있는데 아무도 안 붙은 정도. 0~100."""
    import math
    # 수요 (0~40): 100회=0, 10000회=40
    demand = 40 * min(1.0, math.log10(max(row["total"], 100) / 100) / 2)
    # 미개척 (0~35): 광고가 적을수록 높다
    unserved = 35 * max(0.0, 1 - row["depth"] / 10)
    # 진입 여지 (0~15): 경쟁강도
    entry = {"낮음": 15, "중간": 8, "높음": 2}.get(row["comp"], 8)
    # 문제형 표현 (0~10)
    problem = 10 if is_problem(row["keyword"]) else 0
    return round(demand + unserved + entry + problem)


def find_gaps(keyword, expand=True):
    """레드오션 키워드에서 아직 안 풀린 지점을 뽑는다."""
    rows, _ = lookup(keyword)
    norm = normalize(keyword).upper()
    core = next((r for r in rows if normalize(r["keyword"]).upper() == norm), None)
    if not core:
        return None

    pool = {}
    for r in rows:
        if normalize(r["keyword"]).upper() != norm and r["total"] >= GAP_MIN_VOLUME:
            pool[r["keyword"]] = r

    # 1차에서 유망한 문제형 키워드를 씨앗으로 2차 확장
    seeds = []
    if expand:
        cands = [r for r in pool.values() if is_problem(r["keyword"])]
        cands.sort(key=gap_score, reverse=True)
        for r in cands[:GAP_MAX_SEED]:
            try:
                more, _ = lookup(r["keyword"])
            except (RuntimeError, requests.RequestException):
                break
            seeds.append(r["keyword"])
            for m in more:
                if m["total"] >= GAP_MIN_VOLUME and m["keyword"] not in pool \
                        and normalize(m["keyword"]).upper() != norm:
                    pool[m["keyword"]] = m

    gaps = []
    for r in pool.values():
        gaps.append({
            "keyword": r["keyword"], "total": r["total"],
            "comp": r["comp"], "depth": r["depth"],
            "problem": is_problem(r["keyword"]),
            "score": gap_score(r),
        })
    gaps.sort(key=lambda g: g["score"], reverse=True)

    unserved = [g for g in gaps if g["depth"] <= 2 and g["comp"] != "높음"]

    return {
        "core": {"keyword": core["keyword"], "total": core["total"],
                 "comp": core["comp"], "depth": core["depth"]},
        "seeds": seeds,
        "scanned": len(pool),
        "gaps": gaps[:40],
        "unserved_count": len(unserved),
    }


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
        return jsonify({"error": "잠시 뒤에 다시 조회해 주세요. 짧은 시간에 너무 많이 요청했습니다."}), 429

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

    trends, stats, trend_error = {}, {}, None
    for keyword in keywords:
        norm = normalize(keyword).upper()
        match = next((r for r in unique if normalize(r["keyword"]).upper() == norm), None)
        if not match:
            continue
        try:
            series = trend_for(keyword, match["total"])
        except RuntimeError as exc:
            series, trend_error = None, str(exc)
        except requests.HTTPError as exc:
            series = None
            code = exc.response.status_code if exc.response is not None else "?"
            detail = (exc.response.text[:200] if exc.response is not None else "")
            trend_error = "데이터랩 응답 {}: {}".format(code, detail)
        except requests.RequestException as exc:
            series, trend_error = None, "데이터랩 연결 실패: {}".format(type(exc).__name__)
        if series:
            trends[match["keyword"]] = series
            st = trend_stats(series)
            if st:
                stats[match["keyword"]] = st

    payload = {"rows": unique[:200], "cached": from_cache, "trends": trends, "trend_stats": stats}
    if trend_error:
        payload["trend_error"] = trend_error
    return jsonify(payload)


@app.post("/api/gaps")
def api_gaps():
    if not all(credentials()):
        return jsonify({"error": "서버가 아직 설정되지 않았습니다. 운영자에게 알려주세요."}), 503
    if rate_limited(client_ip()):
        return jsonify({"error": "잠시 뒤에 다시 시도해 주세요. 짧은 시간에 너무 많이 요청했습니다."}), 429

    body = request.get_json(silent=True) or {}
    keyword = str(body.get("keyword", "")).strip()[:MAX_KEYWORD_LEN]
    if not keyword:
        return jsonify({"error": "키워드를 입력해 주세요."}), 400

    try:
        result = find_gaps(keyword, expand=bool(body.get("expand", True)))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 429
    except requests.RequestException:
        return jsonify({"error": "네이버 서버와 통신하지 못했습니다. 잠시 뒤 다시 시도해 주세요."}), 502

    if not result:
        return jsonify({"error": "검색량 데이터가 없는 키워드입니다. 철자를 확인해 주세요."}), 404
    return jsonify(result)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
