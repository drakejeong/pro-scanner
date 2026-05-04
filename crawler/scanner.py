"""
PRO Scanner - 하이브리드 쇼핑몰 신상품 크롤러 (v3)
========================================================
v3 개선 사항:
  - 사이트당 최대 처리 시간 60초 제한 (시간 초과 방지)
  - 무신사 사이트 먼저 처리 (Gemini 안 쓰니까 빨리 끝남)
  - 진행 상황 실시간 출력 (타임아웃되어도 어디서 멈췄는지 알 수 있음)

실행 흐름:
  1. Firestore 에서 등록된 사이트 목록 가져오기
  2. 무신사 사이트 먼저, 일반 사이트 나중에 정렬
  3. 각 사이트별로 최대 60초 안에 다음 시도:
     (a) URL 이 musinsa.com/brand/* → 무신사 전용 API
     (b) RSS 피드 → 파싱
     (c) sitemap.xml → 파싱
     (d) 모두 실패 → Gemini LLM (5초 딜레이 + 자동 재시도)
  4. 신상품 추출 → Firestore 업데이트 → 텔레그램 알림
"""

import os
import sys
import json
import time
import re
import signal
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


# ======================================================================
# 환경 변수
# ======================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APP_ID = os.environ.get("APP_ID", "drake130-app").strip()

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

GEMINI_MIN_INTERVAL = 5.0
PER_SITE_TIMEOUT = 60  # 사이트당 최대 처리 시간 (초)
_last_gemini_call_time = 0.0

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

REQUEST_TIMEOUT = 15


# ======================================================================
# 사이트별 타임아웃 (signal 이용)
# ======================================================================
class SiteTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise SiteTimeout()


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
# 무신사 전용 크롤러
# ======================================================================
def is_musinsa(url: str) -> bool:
    return "musinsa.com" in urlparse(url).netloc.lower()


def try_musinsa(site_url: str):
    if not is_musinsa(site_url):
        return None

    parsed = urlparse(site_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2 or path_parts[0] != "brand":
        return None
    brand_id = path_parts[1]

    print(f"  [무신사] 브랜드: {brand_id}")

    musinsa_headers = {
        **HTTP_HEADERS,
        "Accept": "application/json",
        "Referer": site_url,
    }

    api_candidates = [
        f"https://api.musinsa.com/api2/hm/web/v5/brands/goods?brand={brand_id}&size=10&page=1&sort=new&listViewType=small",
        f"https://api.musinsa.com/api2/hm/web/v6/brand/{brand_id}/goods?gf=A&size=10&page=1&sort=new",
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
                print(f"  ✓ 무신사 API 성공")
                return items[:10]
        except Exception:
            continue

    print(f"  무신사 API 실패, HTML 파싱 시도")
    return _try_musinsa_html(site_url, brand_id)


def _extract_musinsa_items(data, brand_id):
    candidates = []

    def find_lists(obj, depth=0):
        if depth > 5:
            return
        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict):
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
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag and next_data_tag.string:
            try:
                next_data = json.loads(next_data_tag.string)
                items = _extract_musinsa_items(next_data, brand_id)
                if items:
                    print(f"  ✓ 무신사 __NEXT_DATA__ 파싱 성공")
                    return items
            except Exception:
                pass

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
            print(f"  ✓ 무신사 HTML 파싱 성공: {len(items)}개")
            return items
    except Exception as e:
        print(f"  무신사 HTML 파싱 실패: {e}")

    return None


# ======================================================================
# RSS 피드
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

        if "/collections/" in site_url:
            candidates.append(site_url.rstrip("/") + ".atom")

        for rss_url in candidates:
            items = _parse_rss(rss_url)
            if items:
                print(f"  ✓ RSS: {rss_url}")
                return items[:10]
    except Exception as e:
        print(f"  RSS 실패: {e}")

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
# sitemap.xml
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
            print(f"  ✓ sitemap")
            return items
    except Exception as e:
        print(f"  sitemap 실패: {e}")

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
# Gemini LLM 폴백 (재시도 횟수 줄임)
# ======================================================================
def try_gemini(site_url: str):
    if not GEMINI_API_KEY:
        return None

    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  페이지 가져오기 실패: HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        page_text = str(soup)[:25000]

        prompt = f"""아래는 쇼핑몰 페이지의 HTML 입니다. 현재 표시된 신상품 또는 최신 상품 10개를 추출해주세요.

규칙:
- 상품명(name)과 절대경로 URL(link)만 반환
- 카테고리·메뉴·배너 링크 제외, 실제 개별 상품만
- 상대경로는 {site_url} 기준으로 절대경로로 변환
- JSON 형식: {{"products": [{{"name": "...", "link": "..."}}]}}

HTML:
{page_text}
"""

        # 재시도는 2회만 (시간 절약)
        for attempt in range(2):
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
                timeout=45,
            )

            if api_resp.status_code == 200:
                break

            if api_resp.status_code in (429, 500, 502, 503, 504) and attempt < 1:
                wait = 20  # 20초만 기다림
                print(f"  Gemini HTTP {api_resp.status_code} → {wait}초 후 재시도")
                time.sleep(wait)
                continue

            print(f"  Gemini API 실패: HTTP {api_resp.status_code}")
            return None
        else:
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
            print(f"  ✓ Gemini: {len(products)}개")
            return products[:10]
    except Exception as e:
        print(f"  Gemini 실패: {e}")

    return None


def _wait_for_gemini_quota():
    global _last_gemini_call_time
    elapsed = time.time() - _last_gemini_call_time
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)
    _last_gemini_call_time = time.time()


# ======================================================================
# 메인 크롤링 (사이트당 타임아웃 60초)
# ======================================================================
def crawl_site_with_timeout(site_url: str):
    """사이트당 최대 60초 안에 결과 반환"""
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(PER_SITE_TIMEOUT)

    try:
        if is_musinsa(site_url):
            result = try_musinsa(site_url)
            if result:
                return result
            print(f"  무신사 전용 실패, 일반 전략 폴백")

        for strategy in (try_rss, try_sitemap, try_gemini):
            result = strategy(site_url)
            if result:
                return result
            time.sleep(0.3)
        return None
    except SiteTimeout:
        print(f"  ⏱ 60초 초과 → 다음 사이트로 이동")
        return None
    finally:
        signal.alarm(0)  # 타이머 해제


# ======================================================================
# 텔레그램
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
    start_time = time.time()
    print(f"=== PRO Scanner v3 시작 @ {datetime.now(timezone.utc).isoformat()} ===")

    db = init_firebase()
    sites_ref = db.collection("artifacts").document(APP_ID) \
                  .collection("public").document("data") \
                  .collection("monitoring_sites")

    sites = list(sites_ref.stream())
    if not sites:
        print("등록된 사이트가 없습니다.")
        return

    # ⭐ 무신사 사이트 먼저 처리 (Gemini 안 쓰니까 빨리 끝남)
    sites_with_data = [(s, s.to_dict()) for s in sites]
    sites_sorted = sorted(
        sites_with_data,
        key=lambda x: 0 if is_musinsa(x[1].get("url", "")) else 1
    )

    print(f"총 {len(sites_sorted)}개 사이트 스캔 시작 (무신사 우선)\n")

    total_new_count = 0
    success_count = 0
    fail_count = 0

    for site_doc, site_data in sites_sorted:
        elapsed_total = time.time() - start_time
        if elapsed_total > 1500:  # 25분 경과 시 강제 종료
            print(f"\n⚠ 전체 25분 경과 → 남은 사이트 스킵")
            break

        site_id = site_doc.id
        name = site_data.get("name", "(이름 없음)")
        url = site_data.get("url", "")
        previous_items = site_data.get("items", []) or []

        print(f"[{name}] {url}")

        if not url:
            print("  URL 비어있음")
            continue

        site_start = time.time()
        fetched = crawl_site_with_timeout(url)
        site_elapsed = time.time() - site_start

        if not fetched:
            print(f"  ✗ 크롤링 실패 ({site_elapsed:.1f}s)")
            sites_ref.document(site_id).update({
                "lastError": "크롤링 실패",
                "lastErrorAt": datetime.now(timezone.utc).isoformat(),
            })
            fail_count += 1
            continue

        if previous_items:
            new_items = [
                f for f in fetched
                if not any(
                    p.get("link") == f.get("link") or p.get("name") == f.get("name")
                    for p in previous_items
                )
            ]
        else:
            new_items = []

        sites_ref.document(site_id).update({
            "items": fetched,
            "newItems": new_items,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
        })

        success_count += 1
        print(f"  → 전체 {len(fetched)}개 / 신규 {len(new_items)}개 ({site_elapsed:.1f}s)")

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

    total_elapsed = time.time() - start_time
    print(f"\n=== 스캔 완료 ({total_elapsed:.1f}s) ===")
    print(f"  성공: {success_count}개 / 실패: {fail_count}개 / 신규: {total_new_count}건")


if __name__ == "__main__":
    main()
