"""
상업용 부동산 뉴스 클리핑 v2 (네이버 뉴스 API + 구글 뉴스 RSS 통합)
"""

import io
import re
import time
import html
import datetime as dt
import urllib.parse

import requests
import feedparser
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

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
    text = re.sub(r"<[^>]+>", "", text)  # 모든 태그 제거
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ── 네이버 뉴스 검색 API ──────────────────────────────────────
def fetch_naver(keyword, category, cid, csecret, hours_limit, max_pages=10, diag=None):
    """diag: dict를 넘기면 진단 정보를 채워줌 (상태코드, 원본건수, 최신기사시각 등)."""
    rows = []
    now = dt.datetime.now(KST)
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csecret}
    raw_count = 0
    newest_pub = None
    for page in range(max_pages):
        start = page * 100 + 1
        if start > 1000:  # API 상한
            break
        params = {"query": keyword, "display": 100, "start": start, "sort": "date"}
        try:
            r = requests.get("https://openapi.naver.com/v1/search/news.json",
                             headers=headers, params=params, timeout=10)
            if diag is not None:
                diag["status"] = r.status_code
            if r.status_code != 200:
                # 401: 인증실패(키오류) / 403: 권한없음(검색API 미등록) / 429: 쿼터초과
                return rows, f"네이버 API 오류 {r.status_code}: {r.text[:150]}"
            items = r.json().get("items", [])
        except Exception as e:
            return rows, f"네이버 요청 실패: {e}"
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
            # 최신순 정렬이므로 24시간 초과가 나오면 이후는 볼 필요 없음
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
        time.sleep(0.1)
    if diag is not None:
        diag["raw_count"] = raw_count
        diag["newest"] = newest_pub.strftime("%Y-%m-%d %H:%M") if newest_pub else "없음"
        diag["kept"] = len(rows)
    return rows, None


# 링크 도메인 → 언론사명 매핑 (네이버 API가 언론사명을 안 주므로 링크에서 유추)
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
    """기사 링크 도메인에서 언론사명 유추. 못 찾으면 플레이스홀더 반환."""
    if not url:
        return PRESS_PLACEHOLDER
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        host = host.replace("www.", "")
        # 정확 매칭 우선, 없으면 부분 매칭
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
    q = urllib.parse.quote(f"{keyword} when:{within_days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url)
    now = dt.datetime.now(KST)
    rows = []
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc).astimezone(KST)
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


def dedup(df):
    if df.empty:
        return df
    df = df.copy()
    df["_key"] = df["제목"].str.replace(r"\s+", "", regex=True).str[:40]
    # 네이버 우선 유지 (originallink가 더 깔끔)
    df["_p"] = (df["출처"] == "네이버").astype(int)
    df = df.sort_values("_p", ascending=False).drop_duplicates(subset="_key", keep="first")
    return df.drop(columns=["_key", "_p"]).reset_index(drop=True)


def to_excel_bytes(df):
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
st.caption("네이버 뉴스 API + 구글 뉴스 RSS 통합 · 최근 24시간 · 누락 최소화")

with st.sidebar:
    st.header("⚙️ 설정")
    use_naver = st.checkbox("네이버 API 사용", value=True)
    use_google = st.checkbox("구글 RSS 사용", value=True)

    cid = csecret = ""
    if use_naver:
        # Streamlit Secrets에서 안전하게 로드
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
        st.error("네이버 API를 사용하려면 Client ID/Secret을 입력하세요. (또는 네이버 체크 해제)")
        st.stop()

    hours_limit = 24 if strict24 else None
    all_rows, errors, diags = [], [], []
    kw_order = []  # 검색한 키워드 순서 기록
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

    # 네이버 진단 패널 — 왜 0건인지 원인 파악용
    if use_naver and diags:
        naver_total = sum(1 for row in all_rows if row.get("출처") == "네이버")
        with st.expander("🔎 네이버 수집 진단 (0건일 때 원인 확인)", expanded=(naver_total == 0)):
            dd = pd.DataFrame(diags)
            cols_order = [c for c in ["키워드", "status", "raw_count", "kept", "newest"] if c in dd.columns]
            dd = dd[cols_order].rename(columns={
                "status": "HTTP상태", "raw_count": "네이버원본건수",
                "kept": "24h내채택", "newest": "최신기사시각"})
            st.dataframe(dd, hide_index=True, use_container_width=True)
            st.caption("HTTP상태 200=정상 / 401=키오류 / 403=검색API미등록 / 429=쿼터초과. "
                       "원본건수는 있는데 24h내채택이 0이면 → 24시간 필터 때문. '정확히 24시간 이내만'을 끄거나 기간을 늘리세요.")

    df = dedup(pd.DataFrame(all_rows))
    if df.empty:
        st.warning("수집된 기사가 없습니다. 키워드/기간/API 키를 확인하세요.")
        st.session_state.pop("collected", None)
    else:
        # 검색한 키워드 순서대로 정렬 (같은 키워드 내에서는 최신순)
        kw_rank = {kw: i for i, kw in enumerate(kw_order)}
        df["_kw_rank"] = df["키워드"].map(kw_rank).fillna(len(kw_order)).astype(int)
        df = df.sort_values(["_kw_rank", "발행시각"], ascending=[True, False])
        df = df.drop(columns="_kw_rank").reset_index(drop=True)
        st.session_state["collected"] = df  # 배포 편집에서 사용

        st.success(f"총 {len(df)}건 (중복 제거 후) · "
                   f"네이버 {sum(df['출처']=='네이버')} / 구글 {sum(df['출처']=='구글')}")
        st.dataframe(df["키워드"].value_counts().rename_axis("키워드").reset_index(name="건수"),
                     hide_index=True)
        fname = f"뉴스클리핑_{dt.datetime.now(KST).strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button("📥 엑셀 다운로드", to_excel_bytes(df), file_name=fname,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)


# ═══════════════════════════════════════════════════════════════
# 배포 편집 섹션 — 기사 선택 → 카테고리 분류 → 요약 → 메일 HTML 생성
# ═══════════════════════════════════════════════════════════════
MAIL_CATEGORIES = ["개발계획", "매입매각", "이전동향", "업계동향", "시장동향", "정책"]

# 키워드/제목에 아래 단어가 있으면 해당 카테고리로 초기 추천 (위에서부터 우선)
CATEGORY_RULES = [
    ("정책",   ["정책", "규제", "법", "제도", "정부", "국토부", "세제", "금리", "완화", "개정"]),
    ("이전동향", ["이전", "사옥", "본사", "입주", "임차", "리모델링"]),
    ("개발계획", ["개발", "복합개발", "신축", "착공", "준공", "분양", "인허가", "부지"]),
    ("매입매각", ["매각", "매입", "매매", "인수", "거래", "딜", "클로징", "펀드", "리츠", "투자"]),
    ("시장동향", ["시장", "전망", "공실", "임대료", "수익률", "가격", "지수", "동향"]),
    ("업계동향", ["운용", "증권", "보험", "건설", "업계", "협회", "인사", "조직"]),
]


def suggest_category(keyword: str, title: str) -> str:
    """키워드+제목 텍스트로 카테고리 초기 추천."""
    text = f"{keyword} {title}"
    for cat, words in CATEGORY_RULES:
        if any(w in text for w in words):
            return cat
    return "업계동향"  # 어디에도 안 걸리면 기본


def build_mail_html(sel_df):
    """사내 배포 포맷대로 메일용 HTML 생성. 카테고리별 그룹핑.

    메일 클라이언트(Knox/Outlook)는 <p>의 margin을 무시/축소하는 경우가 많아,
    간격을 CSS margin이 아니라 '실제 빈 줄 문단'(&nbsp;)으로 만든다.
    모든 문단의 margin은 0으로 통일 → 클라이언트가 재계산해도 균일 유지.
    """
    FF = "'맑은 고딕','Malgun Gothic',sans-serif"
    # 모든 텍스트 줄에 공통 적용되는 문단 스타일 (margin 0, 줄높이 통일)
    P = f'margin:0;padding:0;line-height:1.8;font-family:{FF};'
    BLANK = f'<p style="{P}font-size:13pt;">&nbsp;</p>'  # 균일 간격용 빈 줄

    parts = [f'<div style="font-family:{FF};color:#000;">']
    for ci, cat in enumerate(MAIL_CATEGORIES):
        group = sel_df[sel_df["메일카테고리"] == cat]
        if group.empty:
            continue
        # 카테고리 사이 구분 빈 줄 (첫 카테고리 앞에는 불필요)
        if ci > 0 and len(parts) > 1:
            parts.append(BLANK)
        # 카테고리 헤더: 12pt 볼드
        parts.append(
            f'<p style="{P}font-size:12pt;font-weight:bold;color:#000;">'
            + html.escape(cat) + '</p>'
        )
        parts.append(BLANK)  # 헤더와 첫 기사 사이
        for _, row in group.iterrows():
            title = html.escape(row["제목"])
            link = html.escape(row["링크"], quote=True)
            summary = html.escape(row.get("요약", "") or "")
            press = html.escape(row.get("언론사", "") or "")
            # 언론사가 비어있으면 플레이스홀더로 대체
            if not press.strip():
                press = html.escape(PRESS_PLACEHOLDER)

            # 제목 줄
            parts.append(
                f'<p style="{P}">'
                f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
                f'style="font-family:{FF};font-size:10pt;font-weight:bold;'
                'color:#0000FF;text-decoration:underline;">'
                f'{title}</a></p>'
            )
            # 요약 줄 (여러 줄이면 각각 독립 문단으로 → 줄 간격 균일)
            if summary:
                for ln in summary.split("\n"):
                    ln = ln.strip()
                    if ln:
                        parts.append(
                            f'<p style="{P}font-size:10pt;font-weight:normal;'
                            f'color:#000;">{ln}</p>'
                        )
            # 언론사 줄 — 플레이스홀더면 빨간색으로 눈에 띄게
            press_color = "#000" if press == html.escape(PRESS_PLACEHOLDER) else "#000"
            parts.append(
                f'<p style="{P}font-size:8pt;color:{press_color};">{press}</p>'
            )
            # 기사 사이 균일 간격
            parts.append(BLANK)
    parts.append("</div>")
    return "".join(parts)


if "collected" in st.session_state and not st.session_state["collected"].empty:
    st.divider()
    st.header("✉️ 메일 배포용 정리")
    st.caption("배포할 기사를 선택하고, 카테고리를 지정한 뒤 요약을 다듬으세요. "
               "요약 초안은 기사 원문 일부에서 자동으로 채워집니다. "
               f"언론사가 '{PRESS_PLACEHOLDER}'로 표시된 기사는 직접 입력해 주세요.")

    base = st.session_state["collected"].copy()

    # 편집용 표 준비: 선택 체크박스, 메일카테고리, 요약(초안 자동 채움)
    if "editor_df" not in st.session_state or \
            len(st.session_state.get("editor_df", [])) != len(base):
        edit = base.copy()
        edit.insert(0, "선택", False)
        edit["메일카테고리"] = edit.apply(
            lambda r: suggest_category(str(r.get("키워드", "")), str(r.get("제목", ""))), axis=1)
        # 언론사 비어있으면 플레이스홀더로 채움
        edit["언론사"] = edit["언론사"].fillna("").apply(
            lambda s: s if str(s).strip() else PRESS_PLACEHOLDER)
        # 요약 초안: 원문 앞부분 다듬어 초안으로
        edit["요약"] = edit["요약초안"].fillna("").apply(lambda s: s[:120])
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
            "요약초안": None,  # 숨김
            "카테고리": None, "출처": None,
        },
        disabled=["제목", "키워드", "발행시각", "링크"],
        key="editor",
    )

    sel = edited[edited["선택"] == True].copy()
    st.write(f"선택된 기사: **{len(sel)}건**")
    # 언론사 미기입 경고
    if not sel.empty:
        need_press = sel[sel["언론사"].astype(str).str.strip().isin(["", PRESS_PLACEHOLDER])]
        if not need_press.empty:
            st.warning(f"⚠️ 선택한 기사 중 {len(need_press)}건은 언론사가 비어 있습니다. "
                       "표의 '언론사' 칸을 직접 채우면 메일에 반영됩니다.")

    if st.button("📋 메일 본문 생성", type="primary", use_container_width=True,
                 disabled=sel.empty):
        # 카테고리 순 → 발행시각 순 정렬
        sel["_c"] = sel["메일카테고리"].map({c: i for i, c in enumerate(MAIL_CATEGORIES)})
        sel = sel.sort_values(["_c", "발행시각"], ascending=[True, False])
        mail_html = build_mail_html(sel)
        st.session_state["mail_html"] = mail_html

    if "mail_html" in st.session_state:
        st.subheader("메일 본문")
        st.caption("아래 [메일 본문 복사] 버튼을 누르면 서식이 그대로 클립보드에 담깁니다. "
                   "메일 작성창에 붙여넣기(Ctrl+V)만 하면 됩니다. "
                   "붙여넣은 뒤 빈 줄이 남으면 그 줄에서 Backspace 한 번으로 정리돼요.")

        mail_html = st.session_state["mail_html"]
    # ☆ 수정된 부분: 버튼을 메일 본문과 분리 ☆
    # 버튼과 메시지를 별도 행에 배치 (고정 위치)
    btn_col1, btn_col2 = st.columns([4, 1])

    with btn_col1:
        copy_btn = st.button(
            "📋 메일 본문 복사 (서식 유지)",
            use_container_width=True,
            key="copy_mail_btn"
        )

    msg_placeholder = btn_col2.empty()

    if copy_btn:
        st.session_state["copy_success"] = True
        msg_placeholder.success("✓ 복사됨!")

    st.divider()  # 분리선 추가

    # 메일 본문 미리보기만 렌더링 (스크롤 가능)
    st.components.v1.html(preview_html, height=500, scrolling=True)


        # 백업용 HTML 파일 다운로드도 유지
        full_html = ("<!doctype html><html><head><meta charset='utf-8'></head>"
                     "<body>" + mail_html + "</body></html>")
        st.download_button(
            "📥 (백업) 메일 HTML 파일 다운로드", data=full_html.encode("utf-8"),
            file_name=f"뉴스클리핑_메일_{dt.datetime.now(KST).strftime('%Y%m%d')}.html",
            mime="text/html", use_container_width=True)

with st.expander("ℹ️ 네이버 API 키 발급 & 배포 방법"):
    st.markdown("""
**1. 네이버 API 키 발급 (무료, 1회)**
- developers.naver.com 로그인 → 상단 Application → 애플리케이션 등록
- 사용 API: **검색** 선택 / 환경: WEB 설정 (URL은 배포 후 Streamlit 주소)
- 발급된 **Client ID / Client Secret** 확보 (하루 25,000회 무료)

**2. Streamlit Cloud 배포**
- GitHub에 이 파일 + requirements.txt 업로드 → share.streamlit.io에서 Deploy
- 키를 매번 입력하기 싫으면 앱 Settings → **Secrets**에 아래 저장:
```
NAVER_CLIENT_ID = "발급받은_ID"
NAVER_CLIENT_SECRET = "발급받은_SECRET"
```

**requirements.txt**
```
streamlit
requests
feedparser
pandas
openpyxl
```

**참고**
- 네이버 API는 키워드당 최대 1,000건까지. 부동산 키워드는 24시간 내 이 상한을 거의 안 넘어 사실상 전수 수집.
- 네이버 API는 언론사명을 안 줘서 링크 도메인으로 유추합니다. 못 찾으면 '(언론사 기입 필요)'로 표시되니 표에서 직접 채우세요.
""")
