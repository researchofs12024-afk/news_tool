"""
상업용 부동산 뉴스 클리핑 v4
- 네이버 뉴스 API 단독 수집 (구글 RSS 제거)
- 링크 리다이렉트 해석 + 본문 크롤링 강화
- Gemini REST API 명사형 헤드라인 요약 (병렬 처리)
- 토큰 블로킹 기반 고속 중복 제거
"""
import io
import re
import time
import html
import datetime as dt
import urllib.parse
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import streamlit as st

try:
    from newspaper import Article, Config as NewspaperConfig
    NEWSPAPER_AVAILABLE = True
except ImportError:
    NEWSPAPER_AVAILABLE = False

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from bs4 import BeautifulSoup
    BS_AVAILABLE = True
except ImportError:
    BS_AVAILABLE = False

st.set_page_config(page_title="상업용 부동산 뉴스 클리핑", page_icon="📰", layout="wide")

KST = dt.timezone(dt.timedelta(hours=9))
PRESS_PLACEHOLDER = "(언론사 기입 필요)"

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

DEFAULT_KEYWORDS = {
    "기존 키워드": [
        "자산운용 매각", "자산운용 매입", "복합개발 -분양", "리테일 상권", "물류센터 매매",
        "물류센터 공실", "오피스 이전 -영화", "매각주관사 빌딩", "사옥 매각", "리츠 건물",
        "오피스 복합개발", "부동산 복합개발", "오피스 매입", "사옥 이전", "사옥 신축",
        "사무실 이전", "물류센터 매각", "물류센터 투자", "증권 부동산 투자 -분양",
        "오피스 펀드", "오피스 리츠", "공유 오피스", "물류센터 부동산", "데이터센터 개발",
        "데이터센터 투자", "증권 부동산 투자 해외 -분양", "보험업",
    ],
    "신규 키워드": [],
}


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════
def get_secret(name: str, default: str = "") -> str:
    """secrets.toml이 없는 환경에서도 죽지 않는 안전한 secrets 접근."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def clean(text: str) -> str:
    """HTML 태그·엔티티 제거 후 공백 정리."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


PRESS_DOMAIN_MAP = {
    "hankyung.com": "한국경제", "mk.co.kr": "매일경제", "edaily.co.kr": "이데일리",
    "mt.co.kr": "머니투데이", "sedaily.com": "서울경제", "fnnews.com": "파이낸셜뉴스",
    "chosun.com": "조선일보", "biz.chosun.com": "조선비즈", "donga.com": "동아일보",
    "joongang.co.kr": "중앙일보", "joins.com": "중앙일보", "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문", "seoul.co.kr": "서울신문", "kmib.co.kr": "국민일보",
    "munhwa.com": "문화일보", "hankookilbo.com": "한국일보", "segye.com": "세계일보",
    "asiae.co.kr": "아시아경제", "ajunews.com": "아주경제", "newsis.com": "뉴시스",
    "yna.co.kr": "연합뉴스", "yonhapnews.co.kr": "연합뉴스", "news1.kr": "뉴스1",
    "heraldcorp.com": "헤럴드경제", "etnews.com": "전자신문", "dt.co.kr": "디지털타임스",
    "thebell.co.kr": "더벨", "investchosun.com": "인베스트조선", "dealsite.co.kr": "딜사이트",
    "businesspost.co.kr": "비즈니스포스트", "bizhankook.com": "비즈한국",
    "wowtv.co.kr": "한국경제TV", "moneys.co.kr": "머니S", "ceoscoredaily.com": "CEO스코어데일리",
    "housingnews.co.kr": "하우징헤럴드", "r-e.kr": "부동산일보", "kukinews.com": "쿠키뉴스",
    "newspim.com": "뉴스핌", "ebn.co.kr": "EBN", "tfmedia.co.kr": "조세금융신문",
    "g-enews.com": "글로벌이코노믹", "inews24.com": "아이뉴스24", "zdnet.co.kr": "지디넷코리아",
    "mediapen.com": "미디어펜", "econovill.com": "이코노믹리뷰", "sisajournal-e.com": "시사저널e",
    "theguru.co.kr": "더구루", "pinpointnews.co.kr": "핀포인트뉴스",
    "hankyung.co.kr": "한국경제", "seoulfn.com": "서울파이낸스",
    "financialnews.co.kr": "파이낸셜뉴스", "kbanker.co.kr": "한국금융신문",
    "fntimes.com": "한국금융신문", "insightkorea.co.kr": "인사이트코리아",
    "sisaon.co.kr": "시사오늘", "ekn.kr": "에너지경제", "m-i.kr": "매일일보",
    "smarttoday.co.kr": "스마트투데이", "newsprime.co.kr": "프라임경제",
}


def press_from_link(url: str) -> str:
    """기사 링크 도메인에서 언론사명 유추."""
    if not url:
        return PRESS_PLACEHOLDER
    try:
        host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
        if host in PRESS_DOMAIN_MAP:
            return PRESS_DOMAIN_MAP[host]
        for domain, name in PRESS_DOMAIN_MAP.items():
            if host.endswith(domain):
                return name
    except Exception:
        pass
    return PRESS_PLACEHOLDER


# ══════════════════════════════════════════════════════════════
# 링크 해석 (리다이렉트 / 단축 URL / 네이버 뉴스)
# ══════════════════════════════════════════════════════════════
NAVER_NEWS_HOSTS = ("n.news.naver.com", "news.naver.com", "m.news.naver.com")


def resolve_final_url(url: str, timeout: int = 8) -> str:
    """
    리다이렉트를 따라가 최종 기사 URL을 반환.
    HEAD를 거부하는 언론사가 많아 실패 시 GET(stream)으로 폴백.
    """
    if not url:
        return url
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=UA_HEADERS)
        if r.status_code < 400 and r.url:
            return r.url
    except Exception:
        pass
    try:
        r = requests.get(url, allow_redirects=True, timeout=timeout,
                         headers=UA_HEADERS, stream=True)
        final = r.url or url
        r.close()
        return final
    except Exception:
        return url


def extract_origin_from_naver(url: str) -> str:
    """네이버 뉴스 페이지에서 원문 링크(og:url / 기사원문 링크)를 추출."""
    if not BS_AVAILABLE:
        return url
    try:
        r = requests.get(url, timeout=8, headers=UA_HEADERS)
        if r.status_code != 200:
            return url
        soup = BeautifulSoup(r.content, "html.parser")
        for sel, attr in [
            ('a.media_end_head_origin_link', 'href'),
            ('a[class*="origin_link"]', 'href'),
            ('meta[property="og:url"]', 'content'),
        ]:
            tag = soup.select_one(sel)
            if tag and tag.get(attr):
                cand = tag.get(attr)
                if cand and not any(h in cand for h in NAVER_NEWS_HOSTS):
                    return cand
    except Exception:
        pass
    return url


def normalize_article_url(url: str) -> str:
    """수집 링크를 실제 언론사 기사 URL로 정규화."""
    if not url:
        return url
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in NAVER_NEWS_HOSTS):
        origin = extract_origin_from_naver(url)
        if origin and origin != url:
            return origin
        return url
    # 단축/리다이렉트 링크만 선별적으로 해석 (일반 기사 URL은 그대로)
    if any(k in host for k in ("bit.ly", "buly.kr", "goo.gl", "url.kr", "link.")):
        return resolve_final_url(url)
    return url


# ══════════════════════════════════════════════════════════════
# 네이버 뉴스 검색 API
# ══════════════════════════════════════════════════════════════
def fetch_naver(keyword, category, cid, csecret, hours_limit, max_pages=10, diag=None):
    rows = []
    now = dt.datetime.now(KST)
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csecret}
    raw_count = 0
    newest_pub = None

    for page in range(max_pages):
        start = page * 100 + 1
        if start > 900:  # 네이버 제한: start + display <= 1000
            break
        params = {"query": keyword, "display": 100, "start": start, "sort": "date"}
        try:
            r = requests.get("https://openapi.naver.com/v1/search/news.json",
                             headers=headers, params=params, timeout=20)
            if diag is not None:
                diag["status"] = r.status_code
            if r.status_code != 200:
                return rows, f"네이버 API 오류 {r.status_code}: {r.text[:150]}"
            items = r.json().get("items", [])
        except requests.exceptions.Timeout:
            return rows, f"네이버 API 타임아웃 (키워드: {keyword})"
        except Exception as e:
            return rows, f"네이버 요청 실패 ({keyword}): {str(e)[:100]}"

        if not items:
            break
        raw_count += len(items)

        stop = False
        for it in items:
            pub = None
            try:
                pub = dt.datetime.strptime(
                    it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                if newest_pub is None or pub > newest_pub:
                    newest_pub = pub
            except Exception:
                pass
            if hours_limit and pub and (now - pub).total_seconds() > hours_limit * 3600:
                stop = True
                break
            link = it.get("originallink") or it.get("link", "")
            rows.append({
                "카테고리": category,
                "키워드": keyword,
                "제목": clean(it.get("title", "")),
                "언론사": press_from_link(link),
                "발행시각": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
                "링크": link,
                "네이버링크": it.get("link", ""),
                "요약초안": clean(it.get("description", "")),
            })
        if stop:
            break
        time.sleep(0.05)

    if diag is not None:
        diag["raw_count"] = raw_count
        diag["newest"] = newest_pub.strftime("%Y-%m-%d %H:%M") if newest_pub else "없음"
        diag["kept"] = len(rows)
    return rows, None


# ══════════════════════════════════════════════════════════════
# 중복 제거 (토큰 블로킹 + 유사도)
# ══════════════════════════════════════════════════════════════
BRACKET_RE = re.compile(r"[\[\(【〔<][^\]\)】〕>]{0,12}[\]\)】〕>]")
NONWORD_RE = re.compile(r"[^가-힣A-Za-z0-9 ]")


def normalize_title(title: str) -> str:
    """[단독], (종합) 등 말머리와 특수문자 제거."""
    t = BRACKET_RE.sub(" ", title or "")
    t = NONWORD_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens_of(title: str) -> set:
    return {w for w in normalize_title(title).split() if len(w) >= 2}


def dedup(df, title_sim_threshold=0.65, word_sim_threshold=0.6, progress_bar=None):
    """
    링크 중복 제거 → 토큰 인덱스로 후보군 좁힘 → 유사도 비교.
    전수 비교(O(n²)) 대신 공통 토큰이 있는 기사끼리만 비교하여 대폭 가속.
    """
    if df.empty:
        return df

    df = df.copy()
    df = df.drop_duplicates(subset=["링크"], keep="first")
    df = df.sort_values("발행시각", ascending=False).reset_index(drop=True)

    kept_rows = []
    kept_norm = []
    kept_tokens = []
    token_index = {}  # token -> [kept 인덱스]
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows()):
        title = str(row["제목"])
        norm = normalize_title(title)
        toks = tokens_of(title)

        # 공통 토큰을 가진 기존 기사만 후보로
        candidates = set()
        for t in toks:
            candidates.update(token_index.get(t, ()))

        is_dup = False
        for ci in candidates:
            kt = kept_tokens[ci]
            union = len(toks | kt)
            if union == 0:
                continue
            word_sim = len(toks & kt) / union
            if word_sim >= word_sim_threshold:
                is_dup = True
                break
            # 단어 유사도가 어느 정도 있을 때만 비용 큰 문자열 비교
            if word_sim >= 0.3:
                if SequenceMatcher(None, norm, kept_norm[ci]).ratio() >= title_sim_threshold:
                    is_dup = True
                    break

        if not is_dup:
            idx = len(kept_rows)
            kept_rows.append(row)
            kept_norm.append(norm)
            kept_tokens.append(toks)
            for t in toks:
                token_index.setdefault(t, []).append(idx)

        if progress_bar is not None and total > 0 and (i % 20 == 0 or i == total - 1):
            progress_bar.progress(min((i + 1) / total, 1.0),
                                  text=f"중복 제거 중... ({i + 1}/{total})")

    return pd.DataFrame(kept_rows).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
# 본문 추출
# ══════════════════════════════════════════════════════════════
CAPTION_RE = re.compile(
    r"^\s*(?:[▲◀▶★●○□■▼△▽◇◆※☞]"
    r"|\[?\s*(?:사진|영상|자료|출처|제공|그래픽|표|이미지)\s*[=:\]]"
    r"|\(\s*(?:사진|영상|자료|출처|제공)\s*[=:]?)"
)
REPORTER_RE = re.compile(r"(기자\s*[=:]|무단\s*전재|재배포\s*금지|저작권자|ⓒ|Copyright|@[\w.]+\.(?:co\.kr|com|kr))")

ARTICLE_SELECTORS = [
    "#dic_area", "#newsct_article", "#articleBodyContents", "#articeBody",  # 네이버
    "#article-view-content-div", ".article-body", ".article_body", ".articleBody",
    ".news-body", ".article_content", ".article-content", ".articleText",
    ".news-content", ".entry-content", "#articleBody", "#news_body_area",
    "#CmAdContent", "article", "#content", "main",
]

DROP_SELECTORS = [
    "script", "style", "nav", "footer", "header", "aside", "iframe",
    ".nav", ".menu", ".ad", ".advertisement", ".comment", ".related",
    ".sidebar", ".social", "figure", "figcaption", ".caption",
    ".photo-caption", ".reporter", ".copyright", ".byline",
]


def _clean_paragraphs(lines):
    out = []
    for line in lines:
        line = line.strip()
        if len(line) < 15:
            continue
        if CAPTION_RE.match(line):
            continue
        if REPORTER_RE.search(line) and len(line) < 80:
            continue
        out.append(line)
    return out


def extract_text_with_bs4(url: str, timeout: int = 8) -> str:
    """BeautifulSoup으로 본문 추출 (캡션·메뉴·저작권 제거)."""
    if not BS_AVAILABLE:
        return ""
    try:
        r = requests.get(url, timeout=timeout, headers=UA_HEADERS)
        if r.status_code != 200:
            return ""
        r.encoding = r.apparent_encoding or r.encoding
        soup = BeautifulSoup(r.text, "html.parser")

        for selector in DROP_SELECTORS:
            for el in soup.select(selector):
                el.decompose()

        body = None
        for selector in ARTICLE_SELECTORS:
            body = soup.select_one(selector)
            if body:
                break

        target = body if body is not None else soup
        paragraphs = target.find_all("p")
        if paragraphs and len(" ".join(p.get_text() for p in paragraphs)) > 200:
            lines = [p.get_text() for p in paragraphs]
        else:
            lines = target.get_text("\n").split("\n")

        text = " ".join(_clean_paragraphs(lines))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2500] if len(text) >= 150 else ""
    except Exception:
        return ""


def extract_text_with_trafilatura(url: str, timeout: int = 8) -> str:
    if not trafilatura:
        return ""
    try:
        r = requests.get(url, timeout=timeout, headers=UA_HEADERS)
        if r.status_code != 200:
            return ""
        r.encoding = r.apparent_encoding or r.encoding
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
        if not text:
            return ""
        text = " ".join(_clean_paragraphs(text.split("\n")))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2500] if len(text) >= 150 else ""
    except Exception:
        return ""


def extract_text_with_newspaper(url: str, timeout: int = 8) -> str:
    if not NEWSPAPER_AVAILABLE:
        return ""
    try:
        cfg = NewspaperConfig()
        cfg.browser_user_agent = UA_HEADERS["User-Agent"]
        cfg.request_timeout = timeout
        cfg.fetch_images = False
        cfg.memoize_articles = False
        article = Article(url, language="ko", config=cfg)
        article.download()
        article.parse()
        text = " ".join(_clean_paragraphs((article.text or "").split("\n")))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2500] if len(text) >= 150 else ""
    except Exception:
        return ""


def fetch_article_text(url: str):
    """
    링크 정규화 → 3단계 폴백으로 본문 추출.
    반환: (본문, 사용된추출기, 최종URL)
    """
    final_url = normalize_article_url(url)
    for name, fn in (("bs4", extract_text_with_bs4),
                     ("trafilatura", extract_text_with_trafilatura),
                     ("newspaper", extract_text_with_newspaper)):
        text = fn(final_url)
        if text:
            return text, name, final_url
    # 원본 URL로 한 번 더 시도 (정규화가 오히려 실패한 경우)
    if final_url != url:
        text = extract_text_with_bs4(url) or extract_text_with_trafilatura(url)
        if text:
            return text, "bs4(원본)", url
    return "", "실패", final_url


def first_sentence(text: str, max_chars: int = 150) -> str:
    if not text:
        return ""
    m = re.search(r"[^.!?\n]*[.!?]", text)
    return (m.group(0) if m else text[:max_chars].rstrip() + ".")[:max_chars].strip()


# ══════════════════════════════════════════════════════════════
# Gemini (REST 직접 호출)
# ══════════════════════════════════════════════════════════════
GEMINI_PROMPT = """뉴스 기사 헤드라인 작성 (명사형 필수)

반드시 지킬 규칙:
1. 명사형 종결 (추진, 확정, 결정, 완료, 진행, 개시 등)
2. "~한다" "~했다" 동사형 금지
3. 기업/기관명 + 핵심 내용
4. 최대 100글자
5. 헤드라인만 출력

기사:
{text}

헤드라인:"""

BAD_MODEL_TOKENS = ("embedding", "aqa", "vision", "imagen", "tts", "live",
                    "gemma", "image", "veo", "learnlm")


def list_gemini_models(gemini_key: str):
    """이 키로 generateContent 가능한 모델 목록 조회. 반환: (모델리스트, 에러)"""
    if not gemini_key:
        return [], "GEMINI_API_KEY 없음"
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": gemini_key}, timeout=15)
    except Exception as e:
        return [], f"모델목록 조회 오류: {str(e)[:120]}"

    if r.status_code != 200:
        return [], f"모델목록 조회 실패 HTTP {r.status_code}: {r.text[:150]}"

    usable = []
    for m in r.json().get("models", []):
        name = m.get("name", "").replace("models/", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if any(bad in name.lower() for bad in BAD_MODEL_TOKENS):
            continue
        usable.append(name)
    if not usable:
        return [], "generateContent 지원 모델 없음"

    def score(n):
        s = 0
        if "flash" in n:
            s += 20
        if "lite" in n:
            s += 3          # flash-lite: 빠르고 저렴
        if "2.5" in n:
            s += 8
        elif "2.0" in n:
            s += 6
        elif "1.5" in n:
            s -= 5
        if "latest" in n:
            s += 2
        if "exp" in n or "preview" in n or "thinking" in n:
            s -= 6
        return s

    usable.sort(key=score, reverse=True)
    return usable, None


def resolve_gemini_model(gemini_key: str):
    """성공 결과만 세션에 캐시 (실패를 1시간 캐싱하던 문제 해결)."""
    cached = st.session_state.get("_gemini_model")
    if cached:
        return cached, None
    models, err = list_gemini_models(gemini_key)
    if not models:
        return "", err
    st.session_state["_gemini_model"] = models[0]
    st.session_state["_gemini_model_list"] = models
    return models[0], None


def generate_summary_with_gemini(article_text: str, gemini_key: str, model_name: str = ""):
    """명사형 헤드라인 생성. 반환: (요약, 에러)"""
    if not gemini_key:
        return "", "GEMINI_API_KEY 없음"
    if not article_text:
        return "", "본문 없음"

    if not model_name:
        model_name, err = resolve_gemini_model(gemini_key)
        if not model_name:
            return "", f"모델 선택 실패: {err}"

    payload = {
        "contents": [{"parts": [{"text": GEMINI_PROMPT.format(text=article_text[:2500])}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.3},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": gemini_key}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    last_err = ""
    for attempt in range(2):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=20)
        except Exception as e:
            last_err = f"{model_name}: 요청 오류 {str(e)[:100]}"
            time.sleep(1.5)
            continue

        if r.status_code == 200:
            data = r.json()
            candidates = data.get("candidates", [])
            if not candidates:
                fb = data.get("promptFeedback", {})
                return "", f"{model_name}: 후보 없음 (blockReason={fb.get('blockReason', '?')})"
            cand = candidates[0]
            parts = cand.get("content", {}).get("parts", [])
            summary = "".join(p.get("text", "") for p in parts).strip()
            if summary and len(summary) > 5:
                return re.sub(r"\s+", " ", summary)[:150], None
            return "", f"{model_name}: 빈 응답 (finishReason={cand.get('finishReason', '?')})"

        if r.status_code in (429, 500, 503):
            last_err = f"{model_name} HTTP {r.status_code} (재시도)"
            time.sleep(2.0)
            continue

        return "", f"{model_name} HTTP {r.status_code}: {r.text[:150]}"

    return "", last_err or "알 수 없는 오류"


def summarize_one(row_idx, url, gemini_key, model_name, use_ai):
    """워커: 본문 추출 + 요약 생성. 반환: (idx, 요약, 로그)"""
    text, extractor, final_url = fetch_article_text(url)
    if not text:
        return row_idx, "", f"본문 추출 실패 → {final_url[:70]}"

    if not use_ai:
        return row_idx, first_sentence(text), None

    summary, err = generate_summary_with_gemini(text, gemini_key, model_name)
    if summary:
        return row_idx, summary, None
    return row_idx, first_sentence(text), f"[Gemini실패/{extractor}] {err}"


# ══════════════════════════════════════════════════════════════
# 엑셀 출력
# ══════════════════════════════════════════════════════════════
COL_WIDTHS = {
    "카테고리": 16, "키워드": 18, "제목": 60, "언론사": 14,
    "발행시각": 17, "링크": 50, "네이버링크": 40, "요약초안": 55,
    "요약": 55, "메일카테고리": 14, "선택": 8,
}


def to_excel_bytes(df):
    from openpyxl.utils import get_column_letter
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="클리핑")
        ws = w.sheets["클리핑"]
        for i, col in enumerate(df.columns, start=1):
            ws.column_dimensions[get_column_letter(i)].width = COL_WIDTHS.get(col, 20)
        if "링크" in df.columns:
            link_col = list(df.columns).index("링크") + 1
            for row in range(2, len(df) + 2):
                c = ws.cell(row=row, column=link_col)
                if c.value:
                    c.hyperlink = c.value
                    c.style = "Hyperlink"
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 사이드바
# ══════════════════════════════════════════════════════════════
st.title("📰 상업용 부동산 뉴스 클리핑")
st.caption("네이버 뉴스 API 수집 · 본문 크롤링 + Gemini 명사형 요약")

with st.sidebar:
    st.header("⚙️ 설정")

    cid = get_secret("NAVER_CLIENT_ID")
    csecret = get_secret("NAVER_CLIENT_SECRET")
    if not (cid and csecret):
        cid = st.text_input("네이버 Client ID", type="password")
        csecret = st.text_input("네이버 Client Secret", type="password")
    else:
        st.caption("✓ 네이버 API 키 로드됨")

    hours = st.radio("수집 기간", [24, 48, 72],
                     format_func=lambda x: f"최근 {x}시간", index=0)

    st.divider()
    st.write("**중복 제거 민감도**")
    sim_threshold = st.slider("제목 유사도 임계값", 0.5, 0.9, 0.65, 0.05,
                              help="낮을수록 더 많이 제거")
    word_threshold = st.slider("단어 중복 임계값", 0.4, 0.9, 0.6, 0.05,
                               help="공통 단어 비율(자카드). 낮을수록 더 많이 제거")

    st.divider()
    st.write("**AI 요약 (Gemini)**")
    gemini_key = get_secret("GEMINI_API_KEY")
    if gemini_key:
        use_gemini = st.checkbox("Gemini로 요약", value=True)
        if st.button("🔌 Gemini 연결 테스트", use_container_width=True):
            models, err = list_gemini_models(gemini_key)
            if models:
                st.session_state["_gemini_model"] = models[0]
                st.success(f"✓ 연결 성공 · 사용 모델: **{models[0]}**")
                with st.expander("사용 가능 모델 전체"):
                    st.write(models[:15])
            else:
                st.error(f"✗ {err}")
        active = st.session_state.get("_gemini_model")
        if active:
            st.caption(f"모델: `{active}`")
    else:
        use_gemini = False
        st.warning("⚠️ GEMINI_API_KEY 미설정\n\n"
                   "앱 Settings → Secrets에 추가하세요:\n\n"
                   '`GEMINI_API_KEY = "AIza..."`')

    st.divider()
    max_workers = st.slider("크롤링 동시 처리 수", 1, 8, 5,
                            help="높을수록 빠르지만 차단 위험 증가")

    st.divider()
    st.write("**카테고리 선택**")
    selected = {c: st.checkbox(c, value=True) for c in DEFAULT_KEYWORDS}


# ══════════════════════════════════════════════════════════════
# 키워드 편집 + 수집
# ══════════════════════════════════════════════════════════════
st.subheader("키워드 편집")
keyword_map = {}
cols = st.columns(2)
for i, (cat, kws) in enumerate(DEFAULT_KEYWORDS.items()):
    if selected.get(cat):
        with cols[i % 2]:
            txt = st.text_area(cat, value="\n".join(kws), height=110, key=f"kw_{cat}")
            keyword_map[cat] = [k.strip() for k in txt.splitlines() if k.strip()]

st.divider()

if st.button("🔍 뉴스 수집 시작", type="primary", use_container_width=True):
    if not cid or not csecret:
        st.error("네이버 Client ID/Secret을 입력하세요.")
        st.stop()

    all_rows, errors, diags, kw_order = [], [], [], []
    total_kw = sum(len(v) for v in keyword_map.values())
    prog = st.progress(0.0, text="수집 중...")
    done = 0

    for cat, kws in keyword_map.items():
        for kw in kws:
            if kw not in kw_order:
                kw_order.append(kw)
            d = {"키워드": kw}
            rows, err = fetch_naver(kw, cat, cid, csecret, hours, diag=d)
            all_rows.extend(rows)
            diags.append(d)
            if err:
                errors.append(err)
            done += 1
            prog.progress(done / max(total_kw, 1), text=f"수집 중... ({kw})")
    prog.empty()

    if errors:
        st.error("네이버 API 오류:\n\n" + "\n\n".join(sorted(set(errors))))

    if diags:
        with st.expander("🔎 네이버 수집 진단", expanded=(len(all_rows) == 0)):
            dd = pd.DataFrame(diags)
            order = [c for c in ["키워드", "status", "raw_count", "kept", "newest"]
                     if c in dd.columns]
            st.dataframe(dd[order].rename(columns={
                "status": "HTTP상태", "raw_count": "원본건수",
                "kept": "기간내채택", "newest": "최신기사시각"}),
                hide_index=True, use_container_width=True)

    if all_rows:
        dprog = st.progress(0.0, text="중복 제거 중...")
        df = dedup(pd.DataFrame(all_rows),
                   title_sim_threshold=sim_threshold,
                   word_sim_threshold=word_threshold,
                   progress_bar=dprog)
        dprog.empty()
    else:
        df = pd.DataFrame()

    # 재수집 시 하위 상태 초기화
    for k in ("editor_df", "mail_html", "collected"):
        st.session_state.pop(k, None)

    if df.empty:
        st.warning("수집된 기사가 없습니다.")
    else:
        rank = {kw: i for i, kw in enumerate(kw_order)}
        df["_r"] = df["키워드"].map(rank).fillna(len(kw_order)).astype(int)
        df = df.sort_values(["_r", "발행시각"], ascending=[True, False])
        df = df.drop(columns="_r").reset_index(drop=True)

        st.session_state["collected"] = df
        st.session_state["collect_token"] = dt.datetime.now(KST).isoformat()
        st.success(f"✅ 총 {len(df)}건 (중복 제거 후 / 원본 {len(all_rows)}건)")
        st.dataframe(
            df["키워드"].value_counts().rename_axis("키워드").reset_index(name="건수"),
            hide_index=True)

        fname = f"뉴스클리핑_{dt.datetime.now(KST).strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button(
            "📥 엑셀 다운로드", to_excel_bytes(df), file_name=fname,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True)


# ══════════════════════════════════════════════════════════════
# 배포 편집
# ══════════════════════════════════════════════════════════════
MAIL_CATEGORIES = ["개발계획", "매입매각", "이전동향", "업계동향", "시장동향", "정책"]

CATEGORY_RULES = [
    ("매입매각", ["매각", "매입", "매매", "인수", "거래", "딜 ", "클로징", "매각주관", "우선협상"]),
    ("개발계획", ["개발", "복합개발", "신축", "착공", "준공", "인허가", "부지", "설계", "기공"]),
    ("이전동향", ["이전", "사옥", "본사", "입주", "임차", "리모델링", "사무실"]),
    ("시장동향", ["시장", "전망", "공실", "임대료", "수익률", "지수", "동향", "거래량"]),
    ("정책", ["정책", "규제", "제도", "정부", "국토부", "세제", "금리", "개정", "법안"]),
    ("업계동향", ["운용사", "증권", "보험", "건설", "업계", "협회", "인사", "조직", "펀드", "리츠"]),
]


def suggest_category(keyword: str, title: str) -> str:
    """매칭 개수 기반 스코어링 (첫 매칭 방식의 오분류 개선)."""
    text = f"{keyword} {title}"
    best, best_score = "업계동향", 0
    for cat, words in CATEGORY_RULES:
        score = sum(2 if w in title else 1 for w in words if w in text)
        if score > best_score:
            best, best_score = cat, score
    return best


def build_mail_html(sel_df):
    FF = "'맑은 고딕', 'Malgun Gothic', sans-serif"

    def style(size, bold=False):
        return (f"line-height:1.8;font-family:{FF};color:#000000;orphans:2;"
                f"font-size:{size};font-weight:{'bold' if bold else '400'};"
                "margin:0px;padding:0px;")

    CATEGORY_STYLE = style("12pt", bold=True)
    BODY_STYLE = style("10pt")
    PRESS_STYLE = style("8pt")
    SPACER_STYLE = style("13pt")
    LINK_STYLE = (f"font-family:{FF};font-size:10pt;font-weight:bold;"
                  "color:#0000ff;text-decoration:underline;")
    BLANK = f'<p style="{SPACER_STYLE}">&nbsp;</p>'

    parts = [f'<div style="font-family:{FF};color:#000;">']
    for ci, cat in enumerate(MAIL_CATEGORIES):
        group = sel_df[sel_df["메일카테고리"] == cat]
        if group.empty:
            continue
        if ci > 0 and len(parts) > 1:
            parts.append(BLANK)
        parts.append(f'<p style="{CATEGORY_STYLE}">{html.escape(cat)}</p>')
        parts.append(BLANK)
        for _, row in group.iterrows():
            title = html.escape(str(row["제목"]))
            link = html.escape(str(row["링크"]), quote=True)
            summary = html.escape(str(row.get("요약", "") or ""))
            press = html.escape(str(row.get("언론사", "") or "").strip()) or html.escape(PRESS_PLACEHOLDER)
            parts.append(
                f'<p style="{BODY_STYLE}"><a href="{link}" target="_blank" '
                f'rel="noopener noreferrer" style="{LINK_STYLE}">{title}</a></p>')
            for ln in summary.split("\n"):
                if ln.strip():
                    parts.append(f'<p style="{BODY_STYLE}">{ln.strip()}</p>')
            parts.append(f'<p style="{PRESS_STYLE}">{press}</p>')
            parts.append(BLANK)
    parts.append("</div>")
    return "".join(parts)


if "collected" in st.session_state and not st.session_state["collected"].empty:
    st.divider()
    st.header("✉️ 메일 배포용 정리")
    st.caption("배포할 기사를 선택하고 카테고리를 지정한 뒤 요약을 다듬으세요.")

    base = st.session_state["collected"].copy()
    token = st.session_state.get("collect_token", "")

    if st.session_state.get("editor_token") != token:
        edit = base.copy()
        edit.insert(0, "선택", False)
        edit["메일카테고리"] = edit.apply(
            lambda r: suggest_category(str(r.get("키워드", "")), str(r.get("제목", ""))), axis=1)
        edit["언론사"] = edit["언론사"].fillna("").apply(
            lambda s: s if str(s).strip() else PRESS_PLACEHOLDER)
        edit["요약"] = edit["요약초안"].fillna("").apply(lambda t: first_sentence(t, 70))
        st.session_state["editor_df"] = edit
        st.session_state["editor_token"] = token

    edited_df = st.data_editor(
        st.session_state["editor_df"],
        hide_index=True, use_container_width=True, height=430,
        column_order=["선택", "키워드", "메일카테고리", "제목", "요약", "언론사", "발행시각", "링크"],
        column_config={
            "선택": st.column_config.CheckboxColumn("선택", width="small"),
            "키워드": st.column_config.TextColumn("키워드", width="small"),
            "메일카테고리": st.column_config.SelectboxColumn(
                "메일 카테고리", options=MAIL_CATEGORIES, width="small"),
            "제목": st.column_config.TextColumn("제목", width="large"),
            "요약": st.column_config.TextColumn("요약 (직접 수정)", width="large"),
            "언론사": st.column_config.TextColumn("언론사 (직접 수정)", width="small"),
            "링크": st.column_config.LinkColumn("링크", display_text="열기"),
            "요약초안": None, "카테고리": None, "네이버링크": None,
        },
        disabled=["제목", "키워드", "발행시각", "링크"],
        key="editor",
    )

    sel = edited_df[edited_df["선택"] == True].copy()
    st.write(f"선택된 기사: **{len(sel)}건**")

    if not sel.empty:
        need_press = sel[sel["언론사"].astype(str).str.strip().isin(["", PRESS_PLACEHOLDER])]
        if not need_press.empty:
            st.warning(f"⚠️ 선택한 기사 중 {len(need_press)}건은 언론사가 비어 있습니다.")

    if st.button("📋 메일 본문 생성", type="primary", use_container_width=True,
                 disabled=sel.empty):
        use_ai = bool(use_gemini and gemini_key)
        model_name = ""
        if use_ai:
            model_name, merr = resolve_gemini_model(gemini_key)
            if not model_name:
                st.error(f"Gemini 모델 확인 실패 → 첫 문장 요약으로 대체합니다. ({merr})")
                use_ai = False
            else:
                st.info(f"Gemini 모델: `{model_name}`")

        sel_copy = sel.reset_index(drop=True)
        prog = st.progress(0.0, text="본문 크롤링 및 요약 생성 중...")
        logs, ok = [], 0
        total_n = len(sel_copy)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(summarize_one, i, str(r.get("링크", "")),
                          gemini_key, model_name, use_ai): i
                for i, r in sel_copy.iterrows() if str(r.get("링크", ""))
            }
            for n, fut in enumerate(as_completed(futures), start=1):
                try:
                    idx, summary, log = fut.result()
                    if summary:
                        sel_copy.loc[idx, "요약"] = summary
                        if log is None:
                            ok += 1
                    if log:
                        logs.append(log)
                except Exception as e:
                    logs.append(f"워커 오류: {str(e)[:100]}")
                prog.progress(n / max(total_n, 1), text=f"처리 중... ({n}/{total_n})")
        prog.empty()

        label = "Gemini 요약" if use_ai else "첫 문장 요약"
        st.write(f"**결과:** ✓ {ok}/{total_n}건 {label} 성공")
        if logs:
            st.warning(f"⚠️ {len(logs)}건 문제 발생 (첫 문장으로 대체됨)")
            with st.expander("🔍 실패 원인 상세"):
                for log in logs[:20]:
                    st.text(f"• {log}")

        sel_copy["_c"] = sel_copy["메일카테고리"].map(
            {c: i for i, c in enumerate(MAIL_CATEGORIES)})
        sel_copy = sel_copy.sort_values(["_c", "발행시각"], ascending=[True, False])
        st.session_state["mail_html"] = build_mail_html(sel_copy)
        st.success("✅ 메일 본문이 생성되었습니다.")

    if "mail_html" in st.session_state:
        st.subheader("메일 본문")
        st.caption("[메일 본문 복사] 버튼을 누르면 서식이 클립보드에 담깁니다.")
        mail_html = st.session_state["mail_html"]

        import json as _json
        html_js = _json.dumps(mail_html)
        copy_widget = f"""
        <div id="src" style="position:absolute;left:-9999px;top:-9999px;">{mail_html}</div>
        <div style="font-family:'맑은 고딕',sans-serif;">
          <button id="copyBtn" style="padding:10px 18px;font-size:14px;
              background:#ff4b4b;color:#fff;border:none;border-radius:6px;
              cursor:pointer;width:100%;font-weight:bold;">
            📋 메일 본문 복사 (서식 유지)
          </button>
          <span id="copyMsg" style="margin-left:10px;color:#0a0;font-weight:bold;"></span>
        </div>
        <script>
        const htmlStr = {html_js};
        document.getElementById('copyBtn').addEventListener('click', async () => {{
          const src = document.getElementById('src');
          try {{
            await navigator.clipboard.write([new ClipboardItem({{
              'text/html': new Blob([htmlStr], {{type: 'text/html'}}),
              'text/plain': new Blob([src.innerText], {{type: 'text/plain'}})
            }})]);
            document.getElementById('copyMsg').innerText = '✓ 복사됨! 메일에 붙여넣으세요';
          }} catch (e) {{
            const range = document.createRange();
            range.selectNode(src);
            window.getSelection().removeAllRanges();
            window.getSelection().addRange(range);
            document.execCommand('copy');
            window.getSelection().removeAllRanges();
            document.getElementById('copyMsg').innerText = '✓ 복사됨 (폴백)';
          }}
        }});
        </script>
        """
        st.components.v1.html(copy_widget, height=100, scrolling=False)

        with st.expander("📨 메일 본문 미리보기", expanded=True):
            st.components.v1.html(
                f"<div style=\"font-family:'맑은 고딕',sans-serif;\">{mail_html}</div>",
                height=500, scrolling=True)

        full_html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
                     + mail_html + "</body></html>")
        st.download_button(
            "📥 (백업) 메일 HTML 파일 다운로드", data=full_html.encode("utf-8"),
            file_name=f"뉴스클리핑_메일_{dt.datetime.now(KST).strftime('%Y%m%d')}.html",
            mime="text/html", use_container_width=True)
