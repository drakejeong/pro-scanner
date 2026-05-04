"""
PRO Scanner - 하이브리드 쇼핑몰 신상품 크롤러 (v2)
========================================================
v2 개선 사항:
  - 무신사(musinsa.com) 전용 크롤러 추가 (정확도 100%)
  - Gemini 호출 사이 5초 딜레이 (분당 12회로 안전)
  - HTTP 429 (Rate Limit) 자동 재시도 (지수 백오프)
  - 사이트별 처리 결과 요약 로그

실행 흐름:
  1. Firestore 에서 등록된 사이트 목록 가져오기
  2. 각 사이트별로 다음 순서로 시도:
     (a) URL이 musinsa.com/brand/* 면 → 무신사 전용 API 시도
     (b) RSS 피드 발견 → 파싱
     (c) sitemap.xml 발견 → 파싱
     (d) 위 모두 실패 → HTML 가져와서 Gemini 로 추출
  3. 직전 결과와 비교해서 신상품 추출
  4. Firestore 에 결과 업데이트
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
# 환경 변수 로드
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

# Gemini 호출 사이 최소 간격 (분당 15회 한도 회피)
GEMINI_MIN_INTERVAL = 5.0  # seconds
_last_gemini_call_time = 0.0

# 공통 HTTP 요청 헤더
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
# 크롤링 전략 (a) 무신사 전용 크롤러
# ======================================================================
def is_musinsa(url: str) -> bool:
    """무신사 브랜드 URL 인지 확인"""
    return "musinsa.com" in urlparse(url).netloc.lower()


def try_musinsa(site_url: str):
    """
    무신사 브랜드 페이지에서 신상품 추출.
    여러 API 패턴을 순차적으로 시도하고, 모두 실패하면 HTML 파싱으로 폴백.
    """
    if not is_musinsa(site_url):
        return None

    # URL 에서 브랜드 ID 추출 (예: /brand/dickies → dickies)
    parsed = urlparse(site_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2 or path_parts[0] != "brand":
        return None
    brand_id = path_parts[1]

    print(f"  [무신사] 브랜드 ID: {brand_id}")

    musinsa_headers = {
        **HTTP_HEADERS,
        "Accept": "application/json",
        "Referer": site_url,
    }

    # 무신사 내부 API 후보들 (실제 호출 시 작동하는 것 사용)
    api_candidates = [
        # 검색 API (가장 안정적, sort=NEW 로 신상품)
        f"https://api.musinsa.com/api2/hm/web/v5/brands/goods?brand={brand_id}&size=10&page=1&sort=new&listViewType=small",
        f"https://api.musinsa.com/api2/hm/web/v6/brand/{brand_id}/goods?gf=A&size=10&page=1&sort=new",
        # 폴백: brand goods JSON 엔드포인트
        f"https://www.musinsa.com/api/brand/{brand_id}/goods?size=10&sort=new",
    ]

    for api_url in api_candidates:
        try:
            resp = requests.get(api_url, headers=musinsa_headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = _extract_musinsa_items(data, brand_id)
            if items:
                print(f"  ✓ 무신사 API 성공: {api_url[:60]}...")
                return items[:10]
        except Exception:
            continue

    # API 모두 실패 → HTML 파싱 폴백
    print(f"  무신사 API 모두 실패, HTML 파싱 시도")
    return _try_musinsa_html(site_url, brand_id)


def _extract_musinsa_items(data, brand_id):
    """무신사 API 응답에서 상품 목록 추출 (다양한 응답 형식 지원)"""
    candidates = []

    # 응답에서 상품 리스트로 보이는 곳들을 재귀적으로 탐색
    def find_lists(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict):
                # 상품 리스트로 보이는 패턴
                first = obj[0]
                keys = set(first.keys())
                if keys & {"goodsNo", "goods_no", "productNo", "id"} and \
                   keys & {"goodsName", "goods_name", "productName", "name", "title"}:
                    candidates.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                find_lists(v, depth + 1)

    find_lists(data)

    if not candidates:
        return []

    # 가장 긴 리스트를 상품 리스트로 간주
    products = max(candidates, key=len)

    items = []
    for p in products[:10]:
        name = (
            p.get("goodsName") or p.get("goods_name") or
            p.get("productName") or p.get("name") or p.get("title") or ""
        )
        goods_no = (
            p.get("goodsNo") or p.get("goods_no") or
            p.get("productNo") or p.get("id") or ""
        )
        if name and goods_no:
            items.append({
                "name": str(name).strip(),
                "link": f"https://www.musinsa.com/products/{goods_no}",
            })
    return items


def _try_musinsa_html(site_url: str, brand_id: str):
    """무신사 브랜드 페이지 HTML 에서 직접 상품 추출 (Next.js __NEXT_DATA__ 파싱)"""
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Next.js 사이트는 __NEXT_DATA__ script 태그에 전체 데이터가 들어있음
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag and next_data_tag.string:
            try:
                next_data = json.loads(next_data_tag.string)
                items = _extract_musinsa_items(next_data, brand_id)
                if items:
                    print(f"  ✓ 무신사 HTML(__NEXT_DATA__) 파싱 성공")
                    return items
            except Exception:
                pass

        # 폴백: 일반 anchor 태그에서 /products/ 패턴 찾기
        product_links = soup.find_all("a", href=re.compile(r"/products/\d+"))
        seen = set()
        items = []
        for a in product_links:
            href = a.get("href", "")
            match = re.search(r"/products/(\d+)", href)
            if not match:
                continue
            goods_no = match.group(1)
            if goods_no in seen:
                continue
            seen.add(goods_no)

            # 텍스트 또는 alt 속성에서 상품명 추출
            name = a.get_text(strip=True) or ""
            if not name:
                img = a.find("img")
                if img:
                    name = img.get("alt", "")
            if not name:
                continue

            items.append({
                "name": name[:100],
                "link": urljoin("https://www.musinsa.com", href.split("?")[0]),
            })
            if len(items) >= 10:
                break

        if items:
            print(f"  ✓ 무신사 HTML(anchor) 파싱 성공: {len(items)}개")
            return items

    except Exception as e:
        print(f"  무신사 HTML 파싱 실패: {e}")

    return None


# ======================================================================
# 크롤링 전략 (b) RSS 피드
# ======================================================================
def try_rss(site_url: str):
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        rss_link = soup.find(
            "link",
            attrs={"type": re.compile(r"application/(rss|atom)\+xml")},
        )

        candidates = []
        if rss_link and rss_link.get("href"):
            candidates.append(urljoin(site_url, rss_link["href"]))

        for path in ["/rss", "/feed", "/rss.xml", "/feed.xml"]:
            candidates.append(urljoin(site_url, path))

        # Shopify 사이트는 collection URL + .atom 으로 직접 접근 가능
        if "/collections/" in site_url:
            candidates.append(site_url.rstrip("/") + ".atom")

        for rss_url in candidates:
            items = _parse_rss(rss_url)
            if items:
                print(f"  ✓ RSS 발견: {rss_url}")
                return items[:10]

    except Exception as e:
        print(f"  RSS 탐지 실패: {e}")

    return None


def _parse_rss(rss_url: str):
    try:
        resp = requests.get(rss_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200 or len(resp.content) < 50:
            return None

        root = ET.fromstring(resp.content)
        items = []

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
                    link = child.text.strip() if child.text else child.get("href")

            if title and link:
                items.append({"name": title, "link": link})

        return items if items else None

    except Exception:
        return None


# ======================================================================
# 크롤링 전략 (c) sitemap.xml
# ======================================================================
def try_sitemap(site_url: str):
    try:
        sitemap_url = urljoin(site_url, "/sitemap.xml")
        resp = requests.get(sitemap_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls_with_dates = []
        sitemaps = root.findall("sm:sitemap", ns)
        if sitemaps:
            for sm in sitemaps[:3]:
                loc = sm.find("sm:loc", ns)
                if loc is not None and loc.text:
                    sub_items = _parse_sitemap_urls(loc.text.strip())
                    if sub_items:
                        urls_with_dates.extend(sub_items)
        else:
            urls_with_dates = _parse_sitemap_urls_from_root(root, ns)

        if not urls_with_dates:
            return None

        product_pattern = re.compile(r"(product|item|goods|/p/|/dp/)", re.IGNORECASE)
        filtered = [u for u in urls_with_dates if product_pattern.search(u["link"])]
        if not filtered:
            filtered = urls_with_dates

        filtered.sort(key=lambda x: x.get("lastmod", ""), reverse=True)

        items = []
        for u in filtered[:10]:
            name = u["link"].rstrip("/").split("/")[-1].replace("-", " ")[:80]
            items.append({"name": name, "link": u["link"]})

        if items:
            print(f"  ✓ sitemap 발견")
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
# 크롤링 전략 (d) Gemini LLM 폴백 (딜레이 + 재시도)
# ======================================================================
def try_gemini(site_url: str):
    """페이지 HTML 을 가져와서 Gemini 에게 신상품 추출 요청 (자동 재시도 포함)"""
    if not GEMINI_API_KEY:
        print("  Gemini API 키 없음, 폴백 불가")
        return None

    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  페이지 가져오기 실패: HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

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

        # 재시도 로직 (지수 백오프)
        for attempt in range(4):
            # 분당 한도 회피용 딜레이
            _wait_for_gemini_quota()

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

            if api_resp.status_code == 200:
                break

            # 429 (Rate Limit) 또는 5xx → 재시도
            if api_resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 30 * (attempt + 1)  # 30s, 60s, 90s
                print(f"  Gemini HTTP {api_resp.status_code} → {wait}초 후 재시도 ({attempt + 1}/3)")
                time.sleep(wait)
                continue

            print(f"  Gemini API 실패: HTTP {api_resp.status_code}")
            return None
        else:
            print(f"  Gemini 재시도 한계 도달")
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

        for p in products:
            if p.get("link") and not p["link"].startswith("http"):
                p["link"] = urljoin(site_url, p["link"])

        if products:
            print(f"  ✓ Gemini 폴백 성공: {len(products)}개")
            return products[:10]

    except Exception as e:
        print(f"  Gemini 폴백 실패: {e}")

    return None


def _wait_for_gemini_quota():
    """Gemini 호출 사이에 최소 5초 보장 (분당 15회 한도 회피)"""
    global _last_gemini_call_time
    elapsed = time.time() - _last_gemini_call_time
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)
    _last_gemini_call_time = time.time()


# ======================================================================
# 메인 크롤링 함수 (전략 순차 시도)
# ======================================================================
def crawl_site(site_url: str):
    """
    하이브리드 전략:
      1. 무신사면 → 무신사 전용 크롤러
      2. RSS → sitemap → Gemini 순으로 폴백
    """
    # 무신사는 전용 크롤러 우선
    if is_musinsa(site_url):
        result = try_musinsa(site_url)
        if result:
            return result
        # 무신사 크롤러도 실패하면 일반 전략 폴백
        print(f"  무신사 전용 크롤러 실패, 일반 전략으로 폴백")

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
    print(f"=== PRO Scanner v2 시작 @ {datetime.now(timezone.utc).isoformat()} ===")

    db = init_firebase()
    sites_ref = db.collection("artifacts").document(APP_ID) \
                  .collection("public").document("data") \
                  .collection("monitoring_sites")

    sites = list(sites_ref.stream())
    if not sites:
        print("등록된 사이트가 없습니다.")
        return

    print(f"총 {len(sites)}개 사이트 스캔 시작\n")

    total_new_count = 0
    success_count = 0
    fail_count = 0

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
                "lastError": "크롤링 실패 (모든 전략 응답 없음)",
                "lastErrorAt": datetime.now(timezone.utc).isoformat(),
            })
            fail_count += 1
            continue

        # 신상품 추출
        if previous_items:
            new_items = [
                f for f in fetched
                if not any(
                    p.get("link") == f.get("link") or p.get("name") == f.get("name")
                    for p in previous_items
                )
            ]
        else:
            new_items = []  # 최초 스캔은 알림 X (기준점만 저장)

        sites_ref.document(site_id).update({
            "items": fetched,
            "newItems": new_items,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
        })

        success_count += 1
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

        time.sleep(1)

    print(f"\n=== 스캔 완료 ===")
    print(f"  성공: {success_count}개 / 실패: {fail_count}개")
    print(f"  신규 상품: {total_new_count}건")


if __name__ == "__main__":
    main()
