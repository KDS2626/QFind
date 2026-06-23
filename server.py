"""
최신 게시물 검색 MCP 서버
- 카카오 검색 API(다음)를 사용해 키워드/날짜로 최신 게시물을 찾아준다.
- 제외 키워드를 지정해 원치 않는 결과를 걸러낼 수 있다.

실행 전:
  1) https://developers.kakao.com 에서 앱 생성 → REST API 키 발급
  2) 환경변수 KAKAO_REST_API_KEY 에 키를 넣는다 (.env 또는 셸 export)
"""

import os
import re
import html
from datetime import datetime, date as date_cls, timedelta

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# host="0.0.0.0" + PORT 환경변수: 클라우드(Render 등)에서 외부 접속을 받기 위함
mcp = FastMCP(
    "recent-search",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)

KAKAO_KEY = os.environ.get("KAKAO_REST_API_KEY", "")

# 카카오 검색 API 엔드포인트 (source 값 → URL)
ENDPOINTS = {
    "blog": "https://dapi.kakao.com/v2/search/blog",
    "cafe": "https://dapi.kakao.com/v2/search/cafe",
    "web": "https://dapi.kakao.com/v2/search/web",
}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    """상태 확인용 엔드포인트. 외부 모니터링이 주기적으로 찔러 서버를 깨어 있게 한다."""
    return PlainTextResponse("ok")


def _clean(text: str) -> str:
    """카카오가 돌려주는 <b> 태그 등 HTML을 제거하고 엔티티를 푼다."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _parse_dt(s: str) -> datetime | None:
    """ISO8601 문자열(예: 2024-06-22T15:59:30.000+09:00)을 datetime으로."""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


@mcp.tool(
    annotations=ToolAnnotations(
        title="QFind 최신 게시물 검색",
        readOnlyHint=True,       # 데이터를 읽기만 함 (생성/수정/삭제 없음)
        destructiveHint=False,   # 파괴적 동작 없음
        idempotentHint=True,     # 같은 입력이면 부작용 없이 동일하게 동작
        openWorldHint=True,      # 외부(카카오 검색 API)와 통신함
    )
)
async def search_recent(
    query: str,
    date: str | None = None,
    days: int = 0,
    exclude: list[str] | None = None,
    source: str = "blog",
    size: int = 10,
) -> str:
    """QFind — 키워드로 최신 게시물(블로그/카페/웹)을 검색한다.

    (QFind는 카카오 검색 API 기반의 최신 게시물 검색 도구입니다.)

    예) query="성수동 OO카페", date="2026-06-22", days=3, exclude=["광고", "협찬"]
        → 6/19~6/22 사이에 올라온 글 중 광고·협찬 글을 뺀 최신 결과.

    날짜 다루는 방법:
    - date만 주고 days=0  → 딱 그 날짜 하루치 (글이 없을 수 있음)
    - date + days=N       → 그 날짜 포함 최근 N일 (date-N일 ~ date)
    - date 없이 days=N    → 오늘 기준 최근 N일
    - 둘 다 없음          → 그냥 최신순

    Args:
        query: 검색어 (가게 이름, 주제 등).
        date: "YYYY-MM-DD" 형식. 기준 날짜. 생략 가능.
        days: 기준 날짜로부터 거슬러 포함할 일수 (예: 3 = 최근 3일). 기본 0.
        exclude: 제외할 키워드 목록. 제목·본문에 이 단어가 있으면 제외.
        source: "blog"(기본) | "cafe" | "web".
        size: 반환할 결과 개수 (기본 10, 최대 30).
    """
    if not KAKAO_KEY:
        return "⚠️ KAKAO_REST_API_KEY 환경변수가 설정되지 않았습니다. 카카오 REST API 키를 발급받아 설정해 주세요."

    source = source if source in ENDPOINTS else "blog"
    size = max(1, min(size, 30))
    days = max(0, days)
    exclude = exclude or []

    # 카카오 쿼리 자체에서도 제외어를 빼주면 더 깔끔하다 (-단어)
    api_query = query + "".join(f" -{w}" for w in exclude if w)

    # 날짜 범위 [start_date, end_date] 계산
    start_date: date_cls | None = None
    end_date: date_cls | None = None
    if date:
        try:
            end_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return f"⚠️ 날짜 형식이 올바르지 않습니다: '{date}' (예: 2026-06-22)"
        start_date = end_date - timedelta(days=days)
    elif days > 0:
        end_date = date_cls.today()
        start_date = end_date - timedelta(days=days)

    has_range = start_date is not None

    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    collected: list[dict] = []

    # 날짜 범위가 있으면 여러 페이지를 훑어 해당 기간 글을 모은다.
    max_pages = 5 if has_range else 1

    async with httpx.AsyncClient(timeout=10) as client:
        for page in range(1, max_pages + 1):
            params = {"query": api_query, "sort": "recency", "size": 50, "page": page}
            try:
                resp = await client.get(ENDPOINTS[source], headers=headers, params=params)
            except httpx.RequestError as e:
                return f"⚠️ 네트워크 오류: {e}"

            if resp.status_code == 401:
                return "⚠️ 인증 실패(401). REST API 키가 올바른지 확인해 주세요."
            if resp.status_code != 200:
                return f"⚠️ 카카오 API 오류 {resp.status_code}: {resp.text[:200]}"

            data = resp.json()
            docs = data.get("documents", [])
            if not docs:
                break

            stop = False
            for d in docs:
                dt = _parse_dt(d.get("datetime", ""))
                # 날짜 범위 필터
                if has_range:
                    if dt is None:
                        continue
                    dd = dt.date()
                    if dd > end_date:
                        continue  # 아직 기준일보다 최신 → 더 내려가야 함
                    if dd < start_date:
                        # recency 정렬이라 이 아래는 전부 범위 밖(과거) → 종료
                        stop = True
                        break

                title = _clean(d.get("title", ""))
                contents = _clean(d.get("contents", ""))
                # 제외 키워드 후처리 (제목/본문)
                blob = f"{title} {contents}"
                if any(w and w in blob for w in exclude):
                    continue

                collected.append({
                    "title": title,
                    "contents": contents,
                    "url": d.get("url", ""),
                    "datetime": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                    "name": d.get("blogname") or d.get("cafename") or "",
                })
                if len(collected) >= size:
                    stop = True
                    break

            if stop or data.get("meta", {}).get("is_end"):
                break

    # 기간 표시용 문자열
    if has_range and start_date != end_date:
        period = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
    elif has_range:
        period = end_date.isoformat()
    else:
        period = ""

    if not collected:
        when = f" ({period})" if period else ""
        return f"'{query}'{when}에 대한 최신 게시물을 찾지 못했습니다."

    # 사람이 읽기 좋은 형태로 정리해서 반환 (AI가 그대로 보여주거나 요약)
    lines = [f"🔎 '{query}' 최신 게시물 {len(collected)}건" + (f" — {period}" if period else "")]
    if exclude:
        lines[0] += f" (제외: {', '.join(exclude)})"
    lines.append("")
    for i, it in enumerate(collected, 1):
        lines.append(f"{i}. {it['title']}")
        if it["datetime"]:
            lines.append(f"   🕒 {it['datetime']}  ·  {it['name']}")
        if it["contents"]:
            snippet = it["contents"][:120]
            lines.append(f"   {snippet}{'…' if len(it['contents']) > 120 else ''}")
        lines.append(f"   🔗 {it['url']}")
        lines.append("")

    return "\n".join(lines).strip()


if __name__ == "__main__":
    # 로컬 테스트: stdio 방식 (Claude Desktop 등)
    # 원격 배포(PlayMCP): 환경변수 MCP_TRANSPORT=streamable-http 로 실행
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
