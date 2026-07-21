"""
상업용 부동산 뉴스 클리핑 v3 (강화된 중복 제거)
네이버 뉴스 API + 구글 뉴스 RSS 통합 + 유사도 기반 중복 제거
"""
import io
import re
import time
import html
import datetime as dt
import urllib.parse
from difflib import SequenceMatcher
import requests
import feedparser
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from newspaper import Article
    NEWSPAPER_AVAILABLE = True
except ImportError:
    NEWSPAPER_AVAILABLE = False

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS_AVAILABLE = True
except ImportError:
    BS_AVAILABLE = False

st.set_page_config(page_title="상업용 부동산 뉴스 클리핑", page_icon="📰", layout="wide")

DEFAULT_KEYWORDS = {
    "기존 키워드": [
        "자산운용 매각", "자산운용 매입", "복합개발 -분양", "리테일 상권", "물류센터 매매", "물류센터 공실", "오피스 이전 -영화",
        "매각주관사 빌딩","사옥 매각", "리츠 건물", "오피스 복합개발", "부동산 복합개발", "오피스 매입", "사옥 이전", "사옥 신축", "사무실 이전", "물류센터 매각", "물류센터 투자", "증권 부동산 투자 -분양",
        "오피스 펀드", "오피스 리츠", "공유 오피스", "물류센터 부동산", "데이터센터 개발", "데이터센터 투자", "증권 부동산 투자 해외 -분양", "보험업"
    ],
    "신규 키워드": [
    ],
}

KST = dt.timezone(dt.timedelta(hours=9))
PRESS_PLACEHOLDER = "(언론사 기입 필요)"

def clean(text: str) -> str:
    """HTML 태그·엔티티 제거 후 공백 정리."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

# ── 네이버 뉴스 검색 API ──────────────────────────────────────
def fetch_naver(keyword, category, cid, csecret, hours_limit, max_pages=10, diag=None):
    """네이버 뉴스 검색 API 호출"""
    rows = []
    now = dt.datetime.now(KST)
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csecret}
    raw_count = 0
    newest_pub = None

    for page in range(max_pages):
        start = page * 100 + 1
        if start > 1000:
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
                pub = dt.datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
                if newest_pub is None or pub > newest_pub:
                    newest_pub = pub
            except Exception:
                pass

            if hours_limit and pub and (now - pub).total_seconds() > hours_limit * 3600:
                stop = True
                break

            rows.append({
                "카테고리": category, "키워드": keyword,
                "제목": clean(it.get("title", "")),
                "언론사": press_from_link(it.get("originallink") or it.get("link", "")),
                "발행시각": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
                "링크": it.get("originallink") or it.get("link", ""),
                "요약초안": clean(it.get("description", "")),
                "출처": "네이버",
            })
        if stop:
            break
        time.sleep(0.05)

    if diag is not None:
        diag["raw_count"] = raw_count
        diag["newest"] = newest_pub.strftime("%Y-%m-%d %H:%M") if newest_pub else "없음"
        diag["kept"] = len(rows)

    return rows, None

# 링크 도메인 → 언론사명 매핑
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
    "newspim.com": "뉴스핌", "ebn.co.kr": "EBN", "ibabo.co.kr": "이바보",
    "tfmedia.co.kr": "조세금융신문", "g-enews.com": "글로벌이코노믹",
}

def press_from_link(url: str) -> str:
    """기사 링크 도메인에서 언론사명 유추"""
    if not url:
        return PRESS_PLACEHOLDER
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        host = host.replace("www.", "")
        if host in PRESS_DOMAIN_MAP:
            return PRESS_DOMAIN_MAP[host]
        for domain, name in PRESS_DOMAIN_MAP.items():
            if domain in host:
                return name
    except Exception:
        pass
    return PRESS_PLACEHOLDER

# ── 구글 뉴스 RSS ────────────────────────────────────────────
def fetch_google(keyword, category, within_days, hours_limit):
    """구글 뉴스 RSS 파싱 (타임아웃 및 에러 처리 강화)"""
    q = urllib.parse.quote(f"{keyword} when:{within_days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    rows = []
    now = dt.datetime.now(KST)

    try:
        feed = feedparser.parse(url, timeout=15)
        if feed.status != 200 and not feed.entries:
            return rows
    except Exception:
        return rows

    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            try:
                pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(KST)
            except Exception:
                pass

        if hours_limit and pub and (now - pub).total_seconds() > hours_limit * 3600:
            continue

        title = e.title
        source = e.get("source", {}).get("title", "")
        if not source and " - " in title:
            title, source = title.rsplit(" - ", 1)
        source = source.strip()
        if not source:
            source = press_from_link(e.link)

        rows.append({
            "카테고리": category, "키워드": keyword,
            "제목": title.strip(), "언론사": source,
            "발행시각": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
            "링크": e.link,
            "요약초안": clean(e.get("summary", "")) if e.get("summary") else "",
            "출처": "구글",
        })

    return rows

# ── 개선된 중복 제거 (유사도 기반) ───────────────────────────────
def calculate_title_similarity(title1: str, title2: str) -> float:
    """제목 간 유사도 계산 (0~1, 1=동일)"""
    return SequenceMatcher(None, title1, title2).ratio()

def calculate_word_similarity(title1: str, title2: str) -> float:
    """제목의 공통 단어 비율 (자카드 유사도)"""
    words1 = set(title1.split())
    words2 = set(title2.split())

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)
    return intersection / union if union > 0 else 0.0

def dedup(df, title_sim_threshold=0.65, word_sim_threshold=0.5, progress_bar=None):
    """
    강화된 중복 제거 (유사도 기반 + 최적화)
    - 제목 유사도 > title_sim_threshold OR 단어 유사도 > word_sim_threshold → 중복 판정
    - 네이버 우선 유지
    - 최신순 정렬
    - 빠른 사전 체크: 길이 차이, 첫 글자 등으로 명백한 비중복 판정 후 유사도 계산
    """
    if df.empty:
        return df

    df = df.copy()

    # 링크 중복 제거 (같은 URL이면 당연히 같은 기사)
    df = df.drop_duplicates(subset=["링크"], keep="first")

    # 네이버 우선, 최신순 정렬
    df["_p"] = (df["출처"] == "네이버").astype(int)
    df = df.sort_values(["_p", "발행시각"], ascending=[False, False]).reset_index(drop=True)

    keep_rows = []
    total = len(df)

    for current_idx, (idx, row) in enumerate(df.iterrows()):
        title = row["제목"]
        title_len = len(title)
        is_dup = False

        # 이미 선택된 기사들과 비교
        for kept_row in keep_rows:
            kept_title = kept_row["제목"]
            kept_len = len(kept_title)

            # 빠른 사전 체크: 길이 차이가 60% 이상 차이나면 비중복
            # (예: 50글자 vs 30글자 → 다른 기사일 가능성 높음)
            if max(title_len, kept_len) > 0:
                len_ratio = min(title_len, kept_len) / max(title_len, kept_len)
                if len_ratio < 0.4:  # 너무 길이 다르면 skip
                    continue

            # 첫 글자 10글자가 완전히 다르면 skip (명백한 다른 기사)
            title_prefix = title[:10]
            kept_prefix = kept_title[:10]
            if len(set(title_prefix) & set(kept_prefix)) < 2:
                continue

            # 이제 비용 큰 유사도 계산
            seq_sim = calculate_title_similarity(title, kept_title)

            # 문자열 유사도 낮으면 단어 유사도도 계산하지 않음
            if seq_sim > title_sim_threshold:
                is_dup = True
                break

            # 문자열 유사도 낮으면 단어 유사도만 확인
            word_sim = calculate_word_similarity(title, kept_title)
            if word_sim > word_sim_threshold:
                is_dup = True
                break

        if not is_dup:
            keep_rows.append(row)

        # 진행률 표시 (current_idx 사용 - 0부터 시작)
        if progress_bar is not None and total > 0:
            progress = min((current_idx + 1) / total, 1.0)  # 1.0 초과 방지
            progress_bar.progress(
                progress,
                text=f"중복 제거 중... ({current_idx + 1}/{total})"
            )

    result_df = pd.DataFrame(keep_rows).reset_index(drop=True)
    result_df = result_df.drop(columns=["_p"], errors="ignore")
    return result_df

def to_excel_bytes(df):
    """Excel 다운로드 바이트 생성"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="클리핑")
        ws = w.sheets["클리핑"]
        for col, width in zip("ABCDEFG", [18, 16, 60, 14, 17, 50, 8]):
            ws.column_dimensions[col].width = width
        link_col = list(df.columns).index("링크") + 1
        for row in range(2, len(df) + 2):
            c = ws.cell(row=row, column=link_col)
            if c.value:
                c.hyperlink = c.value
                c.style = "Hyperlink"
    return buf.getvalue()

# ── UI ───────────────────────────────────────────────────────
st.title("📰 상업용 부동산 뉴스 클리핑")
st.caption("네이버 뉴스 API + 구글 뉴스 RSS 통합 · 강화된 중복 제거 · 최근 24시간")

with st.sidebar:
    st.header("⚙️ 설정")
    use_naver = st.checkbox("네이버 API 사용", value=True)
    use_google = st.checkbox("구글 RSS 사용", value=True)
    cid = csecret = ""
    if use_naver:
        try:
            cid = st.secrets.get("NAVER_CLIENT_ID", "")
            csecret = st.secrets.get("NAVER_CLIENT_SECRET", "")
        except Exception:
            cid = csecret = ""
        if not (cid and csecret):
            cid = st.text_input("네이버 Client ID", type="password")
            csecret = st.text_input("네이버 Client Secret", type="password")

    within = st.radio("수집 기간", [1, 2, 3], format_func=lambda x: f"최근 {x}일", index=0)
    strict24 = st.checkbox("정확히 24시간 이내만", value=True)

    st.divider()
    st.write("**중복 제거 민감도**")
    sim_threshold = st.slider("제목 유사도 임계값", 0.4, 0.6, 0.5, 0.05,
                              help="낮을수록 더 많이 제거 (0.5 기본, 0.4 적극 제거)")

    st.divider()
    st.write("**AI 요약 설정 (선택사항)**")

    # Streamlit Secrets에서 API 키 읽기
    gemini_key = st.secrets.get("GEMINI_API_KEY", "")

    if gemini_key:
        use_gemini = st.checkbox("Google Gemini AI로 요약", value=True,
                                help="✓ API 키 설정됨")
        st.caption("✓ Gemini API 준비 완료")
    else:
        use_gemini = False
        st.warning("⚠️ GEMINI_API_KEY가 설정되지 않았습니다.\n`.streamlit/secrets.toml`에 키를 추가하세요.")

    st.divider()
    st.write("**카테고리 선택**")
    selected = {c: st.checkbox(c, value=True) for c in DEFAULT_KEYWORDS}

st.subheader("키워드 편집")
edited = {}
cols = st.columns(2)
for i, (cat, kws) in enumerate(DEFAULT_KEYWORDS.items()):
    if selected.get(cat):
        with cols[i % 2]:
            txt = st.text_area(cat, value="\n".join(kws), height=110, key=f"kw_{cat}")
            edited[cat] = [k.strip() for k in txt.splitlines() if k.strip()]

st.divider()

if st.button("🔍 뉴스 수집 시작", type="primary", use_container_width=True):
    if use_naver and (not cid or not csecret):
        st.error("네이버 API를 사용하려면 Client ID/Secret을 입력하세요.")
        st.stop()

    hours_limit = 24 if strict24 else None
    all_rows, errors, diags = [], [], []
    kw_order = []
    total = sum(len(v) for v in edited.values())
    prog = st.progress(0.0, text="수집 중...")
    done = 0

    for cat, kws in edited.items():
        for kw in kws:
            if kw not in kw_order:
                kw_order.append(kw)
            if use_naver:
                d = {}
                r, err = fetch_naver(kw, cat, cid, csecret, hours_limit, diag=d)
                all_rows.extend(r)
                d["키워드"] = kw
                diags.append(d)
                if err:
                    errors.append(err)
            if use_google:
                all_rows.extend(fetch_google(kw, cat, within, hours_limit))
            done += 1
            prog.progress(done / max(total, 1), text=f"수집 중... ({kw})")

    prog.empty()

    if errors:
        st.error("네이버 API 오류:\n\n" + "\n\n".join(set(errors)))

    if use_naver and diags:
        naver_total = sum(1 for row in all_rows if row.get("출처") == "네이버")
        with st.expander("🔎 네이버 수집 진단", expanded=(naver_total == 0)):
            dd = pd.DataFrame(diags)
            cols_order = [c for c in ["키워드", "status", "raw_count", "kept", "newest"] if c in dd.columns]
            dd = dd[cols_order].rename(columns={
                "status": "HTTP상태", "raw_count": "네이버원본건수",
                "kept": "24h내채택", "newest": "최신기사시각"})
            st.dataframe(dd, hide_index=True, use_container_width=True)

    # 중복 제거 실행 (진행률 표시)
    if all_rows:
        dedup_prog = st.progress(0.0, text="중복 제거 중... (0/0)")
        df = dedup(pd.DataFrame(all_rows), title_sim_threshold=sim_threshold,
                   word_sim_threshold=0.5, progress_bar=dedup_prog)
        dedup_prog.empty()
    else:
        df = pd.DataFrame()

    if df.empty:
        st.warning("수집된 기사가 없습니다.")
        st.session_state.pop("collected", None)
    else:
        kw_rank = {kw: i for i, kw in enumerate(kw_order)}
        df["_kw_rank"] = df["키워드"].map(kw_rank).fillna(len(kw_order)).astype(int)
        df = df.sort_values(["_kw_rank", "발행시각"], ascending=[True, False])
        df = df.drop(columns="_kw_rank").reset_index(drop=True)
        st.session_state["collected"] = df

        st.success(f"✅ 총 {len(df)}건 (중복 제거 후) · "
                   f"네이버 {sum(df['출처']=='네이버')} / 구글 {sum(df['출처']=='구글')}")

        st.dataframe(df["키워드"].value_counts().rename_axis("키워드").reset_index(name="건수"),
                     hide_index=True)

        fname = f"뉴스클리핑_{dt.datetime.now(KST).strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button("📥 엑셀 다운로드", to_excel_bytes(df), file_name=fname,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

# ═══════════════════════════════════════════════════════════════
# 배포 편집 섹션
# ═══════════════════════════════════════════════════════════════
MAIL_CATEGORIES = ["개발계획", "매입매각", "이전동향", "업계동향", "시장동향", "정책"]

CATEGORY_RULES = [
    ("정책",   ["정책", "규제", "법", "제도", "정부", "국토부", "세제", "금리", "완화", "개정"]),
    ("이전동향", ["이전", "사옥", "본사", "입주", "임차", "리모델링"]),
    ("개발계획", ["개발", "복합개발", "신축", "착공", "준공", "분양", "인허가", "부지"]),
    ("매입매각", ["매각", "매입", "매매", "인수", "거래", "딜", "클로징", "펀드", "리츠", "투자"]),
    ("시장동향", ["시장", "전망", "공실", "임대료", "수익률", "가격", "지수", "동향"]),
    ("업계동향", ["운용", "증권", "보험", "건설", "업계", "협회", "인사", "조직"]),
]

def suggest_category(keyword: str, title: str) -> str:
    """키워드+제목으로 카테고리 추천"""
    text = f"{keyword} {title}"
    for cat, words in CATEGORY_RULES:
        if any(w in text for w in words):
            return cat
    return "업계동향"

def extract_text_with_bs4(url: str) -> str:
    """
    BeautifulSoup으로 HTML에서 텍스트 추출 (한국 뉴스 최적화)
    """
    if not BS_AVAILABLE:
        return ""

    try:
        response = requests.get(url, timeout=8, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0'
        })
        if response.status_code != 200:
            return ""

        soup = BeautifulSoup(response.content, 'html.parser')

        # 스크립트, 스타일 제거
        for script in soup(["script", "style"]):
            script.decompose()

        # 주요 텍스트 컨테이너 찾기 (한국 뉴스사이트)
        article_body = None
        for selector in ['article', '.article-body', '.news-body', '#article-view-content-div',
                         '.article_content', '.content', 'main']:
            article_body = soup.select_one(selector)
            if article_body:
                break

        if article_body:
            text = article_body.get_text()
        else:
            text = soup.get_text()

        # 공백 정리
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:2000]  # 처음 2000글자만

    except Exception:
        return ""


def generate_summary_with_gemini(article_text: str, gemini_key: str) -> str:
    """
    Gemini API를 사용해서 기사 텍스트를 한 문장으로 요약
    """
    if not GEMINI_AVAILABLE or not gemini_key or not article_text:
        return ""

    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-1.5-flash')

        response = model.generate_content(
            f"다음 뉴스 기사를 한 문장 (최대 100글자)으로 정확하게 요약해줘:\n\n{article_text[:1500]}"
        )

        summary = response.text.strip()
        if summary and len(summary) > 10:
            return summary[:150]
        return ""

    except Exception:
        return ""


def extract_article_summary(url: str, max_chars: int = 150) -> str:
    """
    기사 URL에서 본문 추출 후 요약
    - newspaper3k 먼저 시도 (한국 뉴스 최적)
    - 실패하면 trafilatura 시도
    - 모두 실패 시 빈 문자열 반환
    """
    extracted_text = None

    # 방법 1: newspaper3k (한국 뉴스에 강함)
    if NEWSPAPER_AVAILABLE:
        try:
            article = Article(url, language='ko')
            article.download()
            article.parse()
            extracted_text = article.text

            if extracted_text and len(extracted_text.strip()) >= 50:
                # 충분한 텍스트 추출됨
                pass
            else:
                extracted_text = None
        except Exception:
            extracted_text = None

    # 방법 2: trafilatura (폴백)
    if not extracted_text and trafilatura:
        try:
            response = requests.get(url, timeout=8, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0'
            })
            if response.status_code == 200:
                extracted_text = trafilatura.extract(response.text, include_comments=False)

                if extracted_text and len(extracted_text.strip()) >= 50:
                    pass
                else:
                    extracted_text = None
        except Exception:
            extracted_text = None

    # 추출 실패
    if not extracted_text:
        return ""

    # 첫 문장 또는 일정 길이 추출
    extracted_text = extracted_text.strip()

    # 첫 문장 찾기 (마침표 기준)
    match = re.search(r'[^.!?\n]*[.!?]', extracted_text)
    if match:
        summary = match.group(0)[:max_chars]
    else:
        # 문장 구분이 없으면 앞부분 + 마침표
        summary = extracted_text[:max_chars].rstrip() + "."

    return summary.strip()

def build_mail_html(sel_df):
    """메일용 HTML 생성"""
    FF = "'맑은 고딕','Malgun Gothic',sans-serif"
    P = f'margin:0;padding:0;line-height:1.8;font-family:{FF};'
    BLANK = f'<p style="{P}font-size:13pt;">&nbsp;</p>'
    parts = [f'<div style="font-family:{FF};color:#000;">']

    for ci, cat in enumerate(MAIL_CATEGORIES):
        group = sel_df[sel_df["메일카테고리"] == cat]
        if group.empty:
            continue
        if ci > 0 and len(parts) > 1:
            parts.append(BLANK)
        parts.append(
            f'<p style="{P}font-size:12pt;font-weight:bold;color:#000;">'
            + html.escape(cat) + '</p>'
        )
        parts.append(BLANK)
        for _, row in group.iterrows():
            title = html.escape(row["제목"])
            link = html.escape(row["링크"], quote=True)
            summary = html.escape(row.get("요약", "") or "")
            press = html.escape(row.get("언론사", "") or "")
            if not press.strip():
                press = html.escape(PRESS_PLACEHOLDER)

            parts.append(
                f'<p style="{P}">'
                f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
                f'style="font-family:{FF};font-size:10pt;font-weight:bold;'
                'color:#0000FF;text-decoration:underline;">'
                f'{title}</a></p>'
            )
            if summary:
                for ln in summary.split("\n"):
                    ln = ln.strip()
                    if ln:
                        parts.append(
                            f'<p style="{P}font-size:10pt;font-weight:normal;'
                            f'color:#000;">{ln}</p>'
                        )
            parts.append(
                f'<p style="{P}font-size:8pt;color:#000;">{press}</p>'
            )
            parts.append(BLANK)

    parts.append("</div>")
    return "".join(parts)

if "collected" in st.session_state and not st.session_state["collected"].empty:
    st.divider()
    st.header("✉️ 메일 배포용 정리")
    st.caption("배포할 기사를 선택하고 카테고리를 지정한 뒤 요약을 다듬으세요.")

    base = st.session_state["collected"].copy()

    if "editor_df" not in st.session_state or len(st.session_state.get("editor_df", [])) != len(base):
        edit = base.copy()
        edit.insert(0, "선택", False)
        edit["메일카테고리"] = edit.apply(
            lambda r: suggest_category(str(r.get("키워드", "")), str(r.get("제목", ""))), axis=1)
        edit["언론사"] = edit["언론사"].fillna("").apply(
            lambda s: s if str(s).strip() else PRESS_PLACEHOLDER)

        # 요약: 첫 문장 또는 60글자 (간결하게)
        def get_summary(text):
            if not text:
                return ""
            # 첫 마침표까지만 추출 (없으면 60글자)
            match = re.search(r'[^.!?\n]*[.!?]', text)
            if match:
                return match.group(0)[:70]
            return text[:70]

        edit["요약"] = edit["요약초안"].fillna("").apply(get_summary)
        st.session_state["editor_df"] = edit

    edited = st.data_editor(
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
            "요약초안": None,
            "카테고리": None, "출처": None,
        },
        disabled=["제목", "키워드", "발행시각", "링크"],
        key="editor",
    )

    sel = edited[edited["선택"] == True].copy()
    st.write(f"선택된 기사: **{len(sel)}건**")

    if not sel.empty:
        need_press = sel[sel["언론사"].astype(str).str.strip().isin(["", PRESS_PLACEHOLDER])]
        if not need_press.empty:
            st.warning(f"⚠️ 선택한 기사 중 {len(need_press)}건은 언론사가 비어 있습니다.")

    if st.button("📋 메일 본문 생성", type="primary", use_container_width=True,
                 disabled=sel.empty):

        use_ai = use_gemini and gemini_key

        if use_ai:
            st.write("**AI 요약 설정:**")
            st.write("✓ Google Gemini API 활성화")

        with st.spinner("기사 본문 크롤링 및 요약 생성 중..."):
            prog = st.progress(0, text="처리 중... (0/0)")
            updated_count = 0
            failed_urls = []
            error_logs = []

            # sel을 copy해서 인덱스 리셋
            sel_copy = sel.copy().reset_index(drop=True)

            for idx, row in sel_copy.iterrows():
                prog.progress(
                    (idx + 1) / len(sel_copy),
                    text=f"처리 중... ({idx + 1}/{len(sel_copy)})"
                )
                url = row.get("링크", "")
                if url:
                    article_text = None

                    if use_ai:
                        # Gemini AI 요약 (크롤링 + AI)
                        # 1순위: BeautifulSoup (한국 뉴스 최적)
                        article_text = extract_text_with_bs4(url)

                        # 2순위: newspaper3k (폴백)
                        if not article_text or len(article_text.strip()) < 50:
                            try:
                                article = Article(url, language='ko')
                                article.download(timeout=8)
                                article.parse()
                                article_text = article.text
                            except Exception:
                                article_text = None

                        # 3순위: trafilatura (마지막 폴백)
                        if not article_text or len(article_text.strip()) < 50:
                            article_text = extract_article_summary(url)

                        # Gemini로 요약
                        if article_text and len(article_text.strip()) >= 50:
                            try:
                                summary = generate_summary_with_gemini(article_text, gemini_key)

                                # Gemini 응답 없으면 BeautifulSoup 텍스트에서 직접 추출 (폴백)
                                if not summary:
                                    # 첫 문장 추출
                                    match = re.search(r'[^.!?\n]*[.!?]', article_text)
                                    if match:
                                        summary = match.group(0)[:150]
                                    else:
                                        summary = article_text[:150].rstrip() + "."

                                if summary:
                                    sel_copy.loc[idx, "요약"] = summary
                                    updated_count += 1
                                else:
                                    failed_urls.append(url[:50])
                                    error_logs.append(f"요약 추출 불가: {url[:50]}")
                            except Exception as e:
                                failed_urls.append(url[:50])
                                error_logs.append(f"Gemini API 오류: {str(e)[:100]}")
                        else:
                            failed_urls.append(url[:50])
                            error_logs.append(f"크롤링 실패/텍스트 부족: {url[:50]}")
                    else:
                        # 기존 방식 (자동 크롤링)
                        summary = extract_article_summary(url)
                        if summary:
                            sel_copy.loc[idx, "요약"] = summary
                            updated_count += 1
                        else:
                            failed_urls.append(url[:50])

                time.sleep(0.2)  # 서버 부하 방지

            prog.empty()

            # 결과 표시
            st.write(f"**결과:** ✓ {updated_count}/{len(sel_copy)}개 기사 요약 완료")
            if failed_urls:
                st.warning(f"⚠️ {len(failed_urls)}개 기사는 요약 실패")
                with st.expander("🔍 실패 원인 확인"):
                    for log in error_logs[:5]:
                        st.text(f"• {log}")

            sel = sel_copy

        sel["_c"] = sel["메일카테고리"].map({c: i for i, c in enumerate(MAIL_CATEGORIES)})
        sel = sel.sort_values(["_c", "발행시각"], ascending=[True, False])
        mail_html = build_mail_html(sel)
        st.session_state["mail_html"] = mail_html
        st.success("✅ 메일 본문이 생성되었습니다.")

    if "mail_html" in st.session_state:
        st.subheader("메일 본문")
        st.caption("아래 [메일 본문 복사] 버튼을 누르면 서식이 클립보드에 담깁니다.")

        mail_html = st.session_state["mail_html"]
        import json as _json
        html_js = _json.dumps(mail_html)

        # 메일 복사 버튼 (붉은색, 큰 타이틀 바로 아래)
        copy_widget = f"""
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
          try {{
            const blob = new Blob([htmlStr], {{type: 'text/html'}});
            const textBlob = new Blob([document.getElementById('preview').innerText],
                                      {{type: 'text/plain'}});
            await navigator.clipboard.write([
              new ClipboardItem({{'text/html': blob, 'text/plain': textBlob}})
            ]);
            document.getElementById('copyMsg').innerText = '✓ 복사됨! 메일에 붙여넣으세요';
          }} catch (e) {{
            const range = document.createRange();
            range.selectNode(document.getElementById('preview'));
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

        # 메일 본문 미리보기
        with st.expander("📨 메일 본문 미리보기", expanded=True):
            preview_widget = f"""
            <div id="preview" style="font-family:'맑은 고딕',sans-serif;">{mail_html}</div>
            """
            st.components.v1.html(preview_widget, height=500, scrolling=True)

        # HTML 다운로드
        full_html = ("<!doctype html><html><head><meta charset='utf-8'></head>"
                     "<body>" + mail_html + "</body></html>")
        st.download_button(
            "📥 (백업) 메일 HTML 파일 다운로드", data=full_html.encode("utf-8"),
            file_name=f"뉴스클리핑_메일_{dt.datetime.now(KST).strftime('%Y%m%d')}.html",
            mime="text/html", use_container_width=True)
