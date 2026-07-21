# build_mail_html() 교체용 코드
# 기존 app.py에서 build_mail_html 함수 전체를 아래 코드로 바꿔주세요.
#
# 변경 이유:
# 붙여넣어주신 실제 메일 HTML을 보면 모든 <p>가
#   line-height:1.8 / 폰트 '맑은 고딕','Malgun Gothic',sans-serif / color:#000000
#   margin-left/right/top/bottom:0px, padding-left/right/top/bottom:0px (개별 지정)
#   orphans:2
# 을 공통으로 갖고, font-size/font-weight만 요소별로 다릅니다.
#   - 카테고리 제목: 12pt, bold
#   - 제목(링크): 10pt, 문단은 400 / 링크 <a>만 bold, 파란색(#0000ff) 밑줄
#   - 요약: 10pt, 400
#   - 언론사: 8pt, 400
#   - 빈 줄(스페이서): 13pt, 400
#
# 기존 코드는 margin:0;padding:0; 축약형만 쓰고 orphans, longhand margin/padding이
# 빠져 있어서, 메일 클라이언트(삼성 웹메일 등)에 붙여넣을 때 일부 문단에
# line-height가 적용되지 않거나 간격이 들쭉날쭉해지는 원인이 됩니다.
# 아래 버전은 모든 문단에 동일한 규칙을 명시적으로 박아 넣어 이 문제를 없앱니다.

def build_mail_html(sel_df):
    """메일용 HTML 생성 (줄간격/폰트/색상/크기 통일 버전)"""
    FF = "'맑은 고딕', 'Malgun Gothic', sans-serif"

    def style(font_size, bold=False):
        weight = "bold" if bold else "400"
        return (
            "line-height:1.8;"
            f"font-family:{FF};"
            "color:#000000;"
            "orphans:2;"
            f"font-size:{font_size};"
            f"font-weight:{weight};"
            "margin-left:0px;margin-bottom:0px;margin-right:0px;margin-top:0px;"
            "padding-left:0px;padding-bottom:0px;padding-right:0px;padding-top:0px;"
        )

    CATEGORY_STYLE = style("12pt", bold=True)
    BODY_STYLE = style("10pt")
    PRESS_STYLE = style("8pt")
    SPACER_STYLE = style("13pt")
    LINK_STYLE = (
        f"font-family:{FF};font-size:10pt;font-weight:bold;"
        "color:#0000ff;text-decoration:underline;"
    )

    BLANK = f'<p style="{SPACER_STYLE}">&nbsp;</p>'

    parts = [f'<div style="font-family:{FF};color:#000;">']
    for ci, cat in enumerate(MAIL_CATEGORIES):
        group = sel_df[sel_df["메일카테고리"] == cat]
        if group.empty:
            continue
        if ci > 0 and len(parts) > 1:
            parts.append(BLANK)
        parts.append(f'<p style="{CATEGORY_STYLE}">' + html.escape(cat) + "</p>")
        parts.append(BLANK)

        for _, row in group.iterrows():
            title = html.escape(row["제목"])
            link = html.escape(row["링크"], quote=True)
            summary = html.escape(row.get("요약", "") or "")
            press = html.escape(row.get("언론사", "") or "")
            if not press.strip():
                press = html.escape(PRESS_PLACEHOLDER)

            parts.append(
                f'<p style="{BODY_STYLE}">'
                f'<a href="{link}" target="_blank" rel="noopener noreferrer" '
                f'style="{LINK_STYLE}">{title}</a></p>'
            )
            if summary:
                for ln in summary.split("\n"):
                    ln = ln.strip()
                    if ln:
                        parts.append(f'<p style="{BODY_STYLE}">{ln}</p>')
            parts.append(f'<p style="{PRESS_STYLE}">{press}</p>')
            parts.append(BLANK)

    parts.append("</div>")
    return "".join(parts)
