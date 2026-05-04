"""
PRO Scanner - 하이브리드 쇼핑몰 신상품 크롤러
========================================================
실행 흐름:
  1. Firestore 에서 등록된 사이트 목록 가져오기
  2. 각 사이트별로 다음 순서로 시도:
     (a) RSS 피드 발견 → 파싱
     (b) sitemap.xml 발견 → 파싱
     (c) 위 둘 다 실패 → HTML 가져와서 Gemini로 추출
  3. 직전 결과와 비교해서 신상품 추출
  4. Firestore에 결과 업데이트
  5. 신상품이 있으면 텔레그램으로 알림 전송
"""

import os
import sys
import json
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


# ======================================================================
# 환경 변수 로드 (GitHub Secrets 또는 로컬 .env)
# ======================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APP_ID = os.environ.get("APP_ID", "drake130-app").strip()

# Gemini 모델 (안정 버전)
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# 공통 HTTP 요청 헤더 (봇 차단 회피용)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

REQUEST_TIMEOUT = 20  # seconds


# ======================================================================
# Firebase 초기화
# ======================================================================
def init_firebase():
    """서비스 계정 JSON 으로 Firebase Admin SDK 초기화"""
    if not FIREBASE_CREDENTIALS_JSON:
        sys.exit("❌ FIREBASE_CREDENTIALS_JSON 환경변수가 설정되지 않았습니다.")

    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
    except json.JSONDecodeError as e:
        sys.exit(f"❌ FIREBASE_CREDENTIALS_JSON 파싱 실패: {e}")

    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()


# ======================================================================
# 크롤링 전략 (a) RSS 피드 탐지 & 파싱
# ======================================================================
def try_rss(site_url: str):
    """
    홈페이지에서 RSS 링크를 찾아서 파싱.
    성공하면 [{name, link}, ...] 반환, 실패하면 None.
    """
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # <link rel="alternate" type="application/rss+xml"> 태그 찾기
        rss_link = soup.find(
            "link",
            attrs={"type": re.compile(r"application/(rss|atom)\+xml")},
        )

        candidates = []
        if rss_link and rss_link.get("href"):
            candidates.append(urljoin(site_url, rss_link["href"]))

        # 흔한 경로 추가 시도
        for path in ["/rss", "/feed", "/rss.xml", "/feed.xml"]:
            candidates.append(urljoin(site_url, path))

        for rss_url in candidates:
            items = _parse_rss(rss_url)
            if items:
                print(f"  ✓ RSS 발견: {rss_url}")
                return items[:10]

    except Exception as e:
        print(f"  RSS 탐지 실패: {e}")

    return None


def _parse_rss(rss_url: str):
    """RSS/Atom 피드를 파싱해서 [{name, link}, ...] 반환"""
    try:
        resp = requests.get(rss_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200 or len(resp.content) < 50:
            return None

        root = ET.fromstring(resp.content)
        items = []

        # RSS 2.0 (<item> 태그)
        for item in root.iter():
            tag = item.tag.split("}")[-1].lower()
            if tag != "item" and tag != "entry":
                continue

            title = None
            link = None
            for child in item:
                ctag = child.tag.split("}")[-1].lower()
                if ctag == "title" and child.text:
                    title = child.text.strip()
                elif ctag == "link":
                    # RSS 는 text, Atom 은 href 속성
                    link = child.text.strip() if child.text else child.get("href")

            if title and link:
                items.append({"name": title, "link": link})

        return items if items else None

    except Exception:
        return None


# ======================================================================
# 크롤링 전략 (b) sitemap.xml 파싱
# ======================================================================
def try_sitemap(site_url: str):
    """
    /sitemap.xml 에서 가장 최근 URL 들을 가져옴.
    상품 URL 패턴 (product, item, goods 등)이 포함된 것만 필터링.
    """
    try:
        sitemap_url = urljoin(site_url, "/sitemap.xml")
        resp = requests.get(sitemap_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls_with_dates = []

        # sitemap index 형식이면 첫 번째 sub-sitemap 만 따라가기
        sitemaps = root.findall("sm:sitemap", ns)
        if sitemaps:
            for sm in sitemaps[:3]:  # 최대 3개만 시도
                loc = sm.find("sm:loc", ns)
                if loc is not None and loc.text:
                    sub_items = _parse_sitemap_urls(loc.text.strip())
                    if sub_items:
                        urls_with_dates.extend(sub_items)
        else:
            urls_with_dates = _parse_sitemap_urls_from_root(root, ns)

        if not urls_with_dates:
            return None

        # 상품 URL 패턴 필터
        product_pattern = re.compile(r"(product|item|goods|/p/|/dp/)", re.IGNORECASE)
        filtered = [
            u for u in urls_with_dates
            if product_pattern.search(u["link"])
        ]
        if not filtered:
            filtered = urls_with_dates  # 패턴 못 찾으면 전체 사용

        # 최근 수정 순으로 정렬
        filtered.sort(key=lambda x: x.get("lastmod", ""), reverse=True)

        items = []
        for u in filtered[:10]:
            # URL 마지막 슬러그를 임시 이름으로 사용
            name = u["link"].rstrip("/").split("/")[-1].replace("-", " ")[:80]
            items.append({"name": name, "link": u["link"]})

        if items:
            print(f"  ✓ sitemap 발견: {sitemap_url}")
            return items

    except Exception as e:
        print(f"  sitemap 탐지 실패: {e}")

    return None


def _parse_sitemap_urls(sitemap_url: str):
    try:
        resp = requests.get(sitemap_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        return _parse_sitemap_urls_from_root(root, ns)
    except Exception:
        return []


def _parse_sitemap_urls_from_root(root, ns):
    results = []
    for url_el in root.findall("sm:url", ns):
        loc = url_el.find("sm:loc", ns)
        lastmod = url_el.find("sm:lastmod", ns)
        if loc is not None and loc.text:
            results.append({
                "link": loc.text.strip(),
                "lastmod": lastmod.text.strip() if lastmod is not None and lastmod.text else "",
            })
    return results


# ======================================================================
# 크롤링 전략 (c) Gemini LLM 폴백
# ======================================================================
def try_gemini(site_url: str):
    """
    페이지 HTML 을 가져와서 Gemini 에게 신상품 추출 요청.
    """
    if not GEMINI_API_KEY:
        print("  Gemini API 키 없음, 폴백 불가")
        return None

    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  페이지 가져오기 실패: HTTP {resp.status_code}")
            return None

        # HTML 정제 (script/style 제거 후 본문만)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # 토큰 절약을 위해 30,000 자로 제한
        page_text = str(soup)[:30000]

        prompt = f"""아래는 쇼핑몰 페이지의 HTML 입니다. 현재 표시된 신상품 또는 최신 상품 10개를 추출해주세요.

규칙:
- 상품명(name)과 절대경로 URL(link)만 반환
- 카테고리 페이지·메뉴·배너 링크는 제외, 실제 개별 상품만
- 상대경로는 {site_url} 기준으로 절대경로로 변환
- JSON 형식으로만 응답: {{"products": [{{"name": "...", "link": "..."}}]}}

HTML:
{page_text}
"""

        api_resp = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1,
                },
            },
            timeout=60,
        )

        if api_resp.status_code != 200:
            print(f"  Gemini API 실패: HTTP {api_resp.status_code} - {api_resp.text[:200]}")
            return None

        result = api_resp.json()
        text = (
            result.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        if not text:
            return None

        data = json.loads(text)
        products = data.get("products", [])

        # link 가 상대경로면 절대경로로 변환
        for p in products:
            if p.get("link") and not p["link"].startswith("http"):
                p["link"] = urljoin(site_url, p["link"])

        if products:
            print(f"  ✓ Gemini 폴백 성공: {len(products)}개")
            return products[:10]

    except Exception as e:
        print(f"  Gemini 폴백 실패: {e}")

    return None


# ======================================================================
# 메인 크롤링 함수 (전략 순차 시도)
# ======================================================================
def crawl_site(site_url: str):
    """
    하이브리드 전략: RSS → sitemap → Gemini 순으로 시도.
    성공한 첫 번째 결과를 반환.
    """
    for strategy in (try_rss, try_sitemap, try_gemini):
        result = strategy(site_url)
        if result:
            return result
        time.sleep(0.5)
    return None


# ======================================================================
# 텔레그램 알림
# ======================================================================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"  텔레그램 전송 실패: {e}")


# ======================================================================
# 메인 실행
# ======================================================================
def main():
    print(f"=== PRO Scanner 시작 @ {datetime.now(timezone.utc).isoformat()} ===")

    db = init_firebase()
    sites_ref = db.collection("artifacts").document(APP_ID) \
                  .collection("public").document("data") \
                  .collection("monitoring_sites")

    sites = list(sites_ref.stream())
    if not sites:
        print("등록된 사이트가 없습니다. 대시보드에서 먼저 사이트를 추가해주세요.")
        return

    print(f"총 {len(sites)}개 사이트 스캔 시작\n")

    total_new_count = 0

    for site_doc in sites:
        site_data = site_doc.to_dict()
        site_id = site_doc.id
        name = site_data.get("name", "(이름 없음)")
        url = site_data.get("url", "")
        previous_items = site_data.get("items", []) or []

        print(f"[{name}] {url}")

        if not url:
            print("  URL 비어있음, 건너뜀")
            continue

        fetched = crawl_site(url)

        if not fetched:
            print(f"  ✗ 모든 크롤링 전략 실패")
            sites_ref.document(site_id).update({
                "lastError": "크롤링 실패 (RSS/sitemap/LLM 모두 응답 없음)",
                "lastErrorAt": datetime.now(timezone.utc).isoformat(),
            })
            continue

        # 신상품 추출 (link 또는 name 기준 비교)
        if previous_items:
            previous_keys = {
                (p.get("link"), p.get("name")) for p in previous_items
            }
            new_items = [
                f for f in fetched
                if (f.get("link"), f.get("name")) not in previous_keys
                and not any(
                    p.get("link") == f.get("link") or p.get("name") == f.get("name")
                    for p in previous_items
                )
            ]
        else:
            new_items = []  # 최초 스캔 시에는 알림 보내지 않음 (전부 다 신상품으로 잡힘 방지)

        # Firestore 업데이트
        sites_ref.document(site_id).update({
            "items": fetched,
            "newItems": new_items,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
        })

        print(f"  → 전체 {len(fetched)}개 / 신규 {len(new_items)}개")

        # 텔레그램 알림
        if new_items:
            total_new_count += len(new_items)
            msg_lines = [f"🆕 <b>{name}</b> 신상품 {len(new_items)}건"]
            for item in new_items[:5]:
                msg_lines.append(
                    f"• <a href='{item.get('link', '#')}'>{item.get('name', '')}</a>"
                )
            if len(new_items) > 5:
                msg_lines.append(f"…외 {len(new_items) - 5}건")
            send_telegram("\n".join(msg_lines))

        time.sleep(1)  # 사이트별 텀

    print(f"\n=== 스캔 완료. 총 신규 {total_new_count}건 ===")


if __name__ == "__main__":
    main()
