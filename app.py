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
    if key.startswith("TREND:"):
        ttl = TREND_TTL
    elif key.startswith("UNMET"):
        ttl = SEARCH_TTL
    else:
        ttl = CACHE_TTL
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


# ------------------------------------------------------------------ 미해결 신호 (지식iN·카페·블로그)
SEARCH_SOURCES = [("kin", "지식iN"), ("cafearticle", "카페"), ("blog", "블로그")]
SEARCH_TTL = int(os.getenv("SEARCH_TTL", 259200))   # 3일

# 생활용품에서 반복되는 불만 축.
# 글 자체가 이미 그 제품 얘기이므로, 표현은 불만을 가리키는 것만 담는다.
COMPLAINT_AXES = [
    ("효과 없음",   ["효과가 없", "효과없", "효과 별로", "소용없", "소용 없", "안없어",
                     "안 없어", "여전히", "변화가 없", "차이가 없", "안 되네", "안되네",
                     "실패", "무용지물", "체감이 안"]),
    ("금방 돌아옴", ["다시 나", "또 나", "금방 다시", "하루 만", "며칠 만", "일시적",
                     "얼마 안 가", "오래 안 가", "지속이 안", "잠깐뿐"]),
    ("근본 원인",   ["근본적", "근본 원인", "왜 그런", "왜 자꾸", "재발", "반복해서",
                     "원인이 뭐", "원인을 모르"]),
    ("성분 걱정",   ["유해", "독성", "아기한테", "아이한테", "임산부", "반려동물",
                     "강아지한테", "고양이한테", "알레르기", "피부에 안", "화학 성분",
                     "성분이 걱정", "안전한지", "무해한"]),
    ("사용 번거로움", ["번거", "귀찮", "매번 뿌", "일일이", "손이 많이", "불편해",
                       "오래 걸려"]),
    ("가격 부담",   ["너무 비싸", "가격이 부담", "가성비가 안", "돈만 나가", "돈 아깝",
                     "돈만 버", "괜히 샀"]),
]
# 시판 제품이 못 채워서 사람들이 직접 만들어 쓰는 신호
DIY_WORDS = ["베이킹소다", "베이킹 소다", "식초", "신문지", "녹차 티백", "숯을",
             "직접 만들", "만들어 쓰", "집에서 만", "홈메이드", "대용으로", "자작"]
# 질문이 아직 안 풀렸다는 신호
UNSOLVED_WORDS = ["방법 없", "어떻게 해야", "도와주", "알려주세요", "해결 방법",
                  "다들 어떻게", "제발", "답답", "미치겠"]

TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text):
    return TAG_RE.sub("", text or "").replace("&quot;", '"').replace("&amp;", "&") \
        .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")


def search_docs(source, query, display=100):
    """API HUB 검색 API. 제목+본문 요약을 돌려준다."""
    client_id, client_secret = datalab_credentials()
    res = requests.get(
        APIHUB_BASE + "/search/v1/" + source,
        params={"query": query, "display": display, "sort": "sim"},
        headers={
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
        },
        timeout=15,
    )
    if res.status_code in (401, 403):
        raise RuntimeError("검색 API 인증에 실패했습니다. 운영자에게 알려주세요.")
    if res.status_code == 429:
        raise RuntimeError("검색 호출 한도를 넘었습니다. 잠시 뒤 다시 시도하세요.")
    res.raise_for_status()
    body = res.json()
    docs = []
    for item in body.get("items", []):
        docs.append({
            "title": strip_tags(item.get("title", "")),
            "text": strip_tags(item.get("description", "")),
            "link": item.get("link", ""),
        })
    return docs, body.get("total", 0)


def relevance_tokens(keyword):
    """키워드를 토막 내 관련성 검사에 쓸 조각을 만든다."""
    parts = [p for p in re.split(r"\s+", keyword.strip()) if p]
    return parts or [keyword]


def is_relevant(doc, tokens):
    """글이 정말 그 제품 얘기인지 본다.
    핵심 명사(마지막 토막)가 제목이나 본문에 있으면 통과.
    '신발 탈취제'로 검색해도 '운동화 냄새' 글이 나오므로 앞 단어는 강제하지 않는다."""
    return tokens[-1] in doc["title"] + " " + doc["text"]


def analyze_complaints(keyword):
    """키워드로 모은 글 중, 관련 있는 것만 골라 불만 축을 센다."""
    if not all(datalab_credentials()):
        return None

    key = "UNMET3:" + normalize(keyword).upper()
    cached = cache_get(key)
    if cached:
        return cached
    if not upstream_allowed(len(SEARCH_SOURCES)):
        raise RuntimeError("오늘 조회 한도를 모두 썼습니다. 내일 다시 이용해 주세요.")

    tokens = relevance_tokens(keyword)
    docs, totals, dropped, seen_links = [], {}, 0, set()
    for source, label in SEARCH_SOURCES:
        try:
            found, total = search_docs(source, keyword, display=100)
        except requests.RequestException:
            continue
        totals[label] = total
        for d in found:
            if not is_relevant(d, tokens):
                dropped += 1
                continue
            if d["link"] in seen_links:
                continue
            seen_links.add(d["link"])
            d["source"] = label
            docs.append(d)

    if not docs:
        return None

    axes = []
    for name, words in COMPLAINT_AXES:
        hits = []
        for d in docs:
            blob = d["title"] + " " + d["text"]
            hit = next((w for w in words if w in blob), None)
            if not hit:
                continue
            # 제목에 불만 표현이 있으면 더 확실한 근거다
            weight = (2 if hit in d["title"] else 0) + (1 if tokens[-1] in d["title"] else 0)
            hits.append({"source": d["source"], "title": d["title"],
                         "link": d["link"], "matched": hit, "_w": weight})
        hits.sort(key=lambda h: h["_w"], reverse=True)
        for h in hits:
            h.pop("_w", None)
        axes.append({"name": name, "count": len(hits), "samples": hits[:5]})
    axes.sort(key=lambda a: a["count"], reverse=True)

    def count_any(words):
        return sum(1 for d in docs if any(w in d["title"] + " " + d["text"] for w in words))

    diy, unsolved = count_any(DIY_WORDS), count_any(UNSOLVED_WORDS)

    result = {
        "keyword": keyword,
        "doc_count": len(docs),
        "dropped": dropped,
        "totals": totals,
        "axes": axes,
        "diy": diy,
        "unsolved": unsolved,
        "diy_ratio": round(diy / len(docs) * 100),
        "unsolved_ratio": round(unsolved / len(docs) * 100),
    }
    cache_put(key, result)
    return result


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


@app.post("/api/unmet")
def api_unmet():
    if not all(datalab_credentials()):
        return jsonify({"error": "검색 API가 아직 설정되지 않았습니다. 운영자에게 알려주세요."}), 503
    if rate_limited(client_ip()):
        return jsonify({"error": "잠시 뒤에 다시 시도해 주세요. 짧은 시간에 너무 많이 요청했습니다."}), 429

    body = request.get_json(silent=True) or {}
    keyword = str(body.get("keyword", "")).strip()[:MAX_KEYWORD_LEN]
    if not keyword:
        return jsonify({"error": "키워드를 입력해 주세요."}), 400

    try:
        result = analyze_complaints(keyword)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 429
    except requests.RequestException:
        return jsonify({"error": "네이버 서버와 통신하지 못했습니다. 잠시 뒤 다시 시도해 주세요."}), 502

    if not result:
        return jsonify({"error": "관련 글을 찾지 못했습니다. 다른 키워드로 시도해 보세요."}), 404
    return jsonify(result)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
