# -*- coding: utf-8 -*-
"""
서울과학기술대학교 공지사항 크롤러 + 디스코드 알림 봇

- '대학공지사항', '학사공지(전체)' 두 게시판을 확인
- 이전 실행 때 못 본 새 글이 있으면 디스코드 웹훅으로 알림 전송
- 이미 본 글 목록은 seen_notices.json 에 저장 (GitHub Actions가 자동 커밋)
"""

import json
import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# 설정: 확인할 게시판 목록
# webhook_env : 이 게시판 알림을 보낼 디스코드 웹훅 URL이 들어있는 환경변수 이름
# layout      : "table" = 표 형태 게시판, "list" = 카드/리스트 형태 게시판
# ----------------------------------------------------------------------
BOARDS = [
    {
        "name": "대학공지사항",
        "url": "https://www.seoultech.ac.kr/service/info/notice?do=list",
        "base": "https://www.seoultech.ac.kr/service/info/notice",
        "webhook_env": "DISCORD_WEBHOOK_URL_NOTICE",
        "layout": "table",
    },
    {
        "name": "학사공지",
        "url": "https://www.seoultech.ac.kr/service/info/matters?do=list",
        "base": "https://www.seoultech.ac.kr/service/info/matters",
        "webhook_env": "DISCORD_WEBHOOK_URL_MATTERS",
        "layout": "table",
    },
    {
        "name": "생활관 공지",
        "url": "https://housing.seoultech.ac.kr/community/notice?boardFilter=58605",
        "base": "https://housing.seoultech.ac.kr/community/notice",
        "webhook_env": "DISCORD_WEBHOOK_URL_HOUSING",
        "layout": "list",
    },
]

SEEN_FILE = "seen_notices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def load_seen():
    """이전에 알림을 보낸 게시글 bidx 목록을 불러옴"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def _find_date_container(a_tag, max_levels=6):
    """a 태그에서 위로 올라가며 '등록날짜'가 포함된 부모 블록을 찾음 (list 레이아웃용)"""
    node = a_tag
    for _ in range(max_levels):
        if node.parent is None:
            break
        node = node.parent
        if "등록날짜" in node.get_text(" ", strip=True):
            return node
    return a_tag.parent


def fetch_notices(board):
    """게시판 목록 페이지에서 (bidx, 제목, 작성자, 날짜, 링크) 리스트를 뽑아냄"""
    resp = requests.get(board["url"], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    notices = []
    seen_bidx_on_page = set()

    layout = board.get("layout", "table")

    # 제목 링크는 두 레이아웃 모두 'do=commonview' 와 'bidx=' 를 포함함
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "do=commonview" not in href or "bidx=" not in href:
            continue

        match = re.search(r"bidx=(\d+)", href)
        if not match:
            continue
        bidx = match.group(1)

        if bidx in seen_bidx_on_page:
            continue

        title = a.get_text(strip=True)
        if not title:
            continue
        # "No.1012 제목" 형태에서 앞의 번호 제거
        title = re.sub(r"^No\.\d+\s*", "", title)

        seen_bidx_on_page.add(bidx)
        author, date = "", ""

        if layout == "table":
            # 표 형태: 같은 <tr> 안의 다른 <td>에서 작성자/날짜 추출
            tr = a.find_parent("tr")
            if tr:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                date_candidates = [t for t in tds if re.match(r"^\d{4}-\d{2}-\d{2}$", t)]
                if date_candidates:
                    date = date_candidates[0]
                    idx = tds.index(date)
                    if idx - 1 >= 0:
                        author = tds[idx - 1]
        else:
            # 리스트/카드 형태: '등록날짜 : YYYY-MM-DD' 텍스트에서 날짜 추출
            container = _find_date_container(a)
            text = container.get_text(" ", strip=True)
            date_match = re.search(r"등록날짜\s*[:：]\s*(\d{4}-\d{2}-\d{2})", text)
            if date_match:
                date = date_match.group(1)

        full_link = urljoin(board["base"], href)

        notices.append(
            {
                "bidx": bidx,
                "title": title,
                "author": author,
                "date": date,
                "link": full_link,
            }
        )

    return notices


def send_discord_message(webhook_url, board_name, notice):
    if not webhook_url:
        print(f"[경고] '{board_name}' 담당 웹훅 환경변수가 설정되어 있지 않습니다.")
        return

    lines = [f"**[{board_name}]** {notice['title']}"]
    meta = []
    if notice.get("author"):
        meta.append(notice["author"])
    if notice.get("date"):
        meta.append(notice["date"])
    if meta:
        lines.append(" · ".join(meta))
    lines.append(notice["link"])

    payload = {"content": "\n".join(lines)}
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code >= 300:
        print(f"[경고] 디스코드 전송 실패 ({resp.status_code}): {resp.text}")


def main():
    seen = load_seen()
    total_new = 0

    for board in BOARDS:
        name = board["name"]
        webhook_url = os.environ.get(board["webhook_env"])
        seen.setdefault(name, [])
        already_seen = set(seen[name])

        try:
            notices = fetch_notices(board)
        except Exception as e:
            print(f"[오류] '{name}' 크롤링 실패: {e}")
            continue

        # 최신 글이 위에 있으므로, 알림은 오래된 것부터 순서대로 보내도록 뒤집음
        new_notices = [n for n in notices if n["bidx"] not in already_seen]
        new_notices.reverse()

        for notice in new_notices:
            print(f"[새 공지] ({name}) {notice['title']}")
            send_discord_message(webhook_url, name, notice)
            seen[name].append(notice["bidx"])
            total_new += 1

        # 목록이 너무 커지지 않도록 최근 300개만 유지
        seen[name] = seen[name][-300:]

    save_seen(seen)
    print(f"완료: 새 공지 {total_new}건 처리")


if __name__ == "__main__":
    sys.exit(main())