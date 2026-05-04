"""
PRO Scanner - 순수 크롤링 버전 (v4, Gemini 제거)
========================================================
v4 변경 사항:
  - Gemini API 완전 제거 (429 문제 근본 해결)
  - 무신사 제거
  - 사이트별 전용 파서 추가 (나이키, 카시나)
  - 범용 HTML 파서 강화 (JSON-LD, 상품 링크 패턴)

크롤링 전략 순서:
  1. RSS 피드    (Shopify 계열 → .atom, 일반 RSS/feed.xml)
  2. sitemap.xml (상품 URL 패턴 필터링)
  3. JSON-LD     (페이지에 내장된 구조화 데이터)
  4. 사이트별 전용 파서 (나이키, 카시나 등)
  5. 범용 HTML 파서 (상품 링크 패턴 추출)

실패 처리:
  - 파페치 등 봇 차단 사이트 → 빠르게 실패 처리 (시간 낭비 X)
  - 사이트당 최대 45초 제한
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
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
APP_ID = os.environ.get("APP_ID", "drake130-app").strip()

# Cloudflare Workers 프록시 URL (IP 차단 우회용)
# 예: https://rss-proxy.YOUR_SUBDOMAIN.workers.dev
CF_PROXY_URL = os.environ.get("CF_PROXY_URL", "").strip()

PER_SITE_TIMEOUT = 45  # 사이트당 최대 처리 시간 (초)
REQUEST_TIMEOUT = 12

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# 봇 차단이 강해서 현실적으로 크롤링 불가능한 도메인
BLOCKED_DOMAINS = [
    "farfetch.com",      # 봇 차단 최강
    "ssense.com",        # 봇 차단 강함
]


# ======================================================================
# 사이트별 타임아웃
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
# 봇 차단 사이트 빠른 확인
# ======================================================================
def is_blocked_domain(url: str) -> bool:
    hostname = urlparse(url).netloc.lower()
    return any(blocked in hostname for blocked in BLOCKED_DOMAINS)


# ======================================================================
# 전략 1: RSS / Atom 피드
# ======================================================================
def try_rss(site_url: str):
    try:
        candidates = []

        # Shopify /collections/ URL → .atom 직접 시도
        if "/collections/" in site_url:
            atom_url = site_url.rstrip("/").split("?")[0] + ".atom"
            candidates.append(atom_url)
            # new-arrivals 계열 컬렉션도 추가 시도
            base = urljoin(site_url, "/collections/")
            for col in ["new-arrivals", "new-arrivals-all", "new", "all"]:
                candidates.append(f"{base}{col}.atom")

        # 루트 경로의 흔한 RSS 경로들
        for path in ["/rss", "/feed", "/rss.xml", "/feed.xml", "/atom.xml"]:
            candidates.append(urljoin(site_url, path))

        # 페이지 HTML 에서 RSS 링크 태그 찾기
        try:
            resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                rss_link = soup.find("link", attrs={"type": re.compile(r"application/(rss|atom)\+xml")})
                if rss_link and rss_link.get("href"):
                    href = urljoin(site_url, rss_link["href"])
                    if href not in candidates:
                        candidates.insert(0, href)
        except Exception:
            pass

        # 중복 제거
        seen = set()
        unique_candidates = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique_candidates.append(c)

        for rss_url in unique_candidates:
            # 일반 헤더로 먼저 시도
            items = _parse_feed(rss_url)
            if items:
                print(f"  ✓ RSS: {rss_url}")
                return items[:10]

            # 실패 시 모바일 User-Agent로 재시도 (일부 사이트 IP 차단 우회)
            items = _parse_feed(rss_url, use_mobile_ua=True)
            if items:
                print(f"  ✓ RSS (mobile-ua): {rss_url}")
                return items[:10]

    except Exception as e:
        print(f"  RSS 실패: {e}")

    return None


def _parse_feed(feed_url: str, use_mobile_ua: bool = False):
    try:
        headers = dict(HTTP_HEADERS)
        if use_mobile_ua:
            headers["User-Agent"] = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
            headers["Accept"] = "application/atom+xml, application/rss+xml, application/xml, */*"

        # 1차: 직접 요청
        resp = requests.get(feed_url, headers=headers, timeout=REQUEST_TIMEOUT)

        # 직접 요청 실패(403/429 등)하고 프록시 설정 있으면 → 프록시로 재시도
        if resp.status_code != 200 and CF_PROXY_URL:
            proxy_url = f"{CF_PROXY_URL}/?url={feed_url}"
            resp = requests.get(proxy_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                print(f"    (프록시 우회 성공)")

        if resp.status_code != 200 or len(resp.content) < 100:
            return None

        root = ET.fromstring(resp.content)
        items = []

        for node in root.iter():
            tag = node.tag.split("}")[-1].lower()
            if tag not in ("item", "entry"):
                continue

            title = link = None
            for child in node:
                ctag = child.tag.split("}")[-1].lower()
                if ctag == "title" and child.text:
                    title = child.text.strip()
                elif ctag == "link":
                    link = child.text.strip() if child.text else child.get("href", "")

            if title and link:
                items.append({"name": title, "link": link})

        return items if items else None
    except Exception:
        return None


# ======================================================================
# 전략 2: sitemap.xml
# ======================================================================
def try_sitemap(site_url: str):
    try:
        sitemap_url = urljoin(site_url, "/sitemap.xml")
        resp = requests.get(sitemap_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            # sitemap이 XML이 아닌 HTML 등 깨진 형식인 경우 조용히 스킵
            return None
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        all_urls = []

        # sitemap index 형식 처리
        sitemaps = root.findall("sm:sitemap", ns)
        if sitemaps:
            for sm in sitemaps[:5]:
                loc = sm.find("sm:loc", ns)
                if loc is not None and loc.text:
                    all_urls.extend(_fetch_sitemap_urls(loc.text.strip()))
        else:
            all_urls = _parse_sitemap_root(root, ns)

        if not all_urls:
            return None

        # 상품 URL 패턴 필터
        product_re = re.compile(
            r"(/product|/goods|/item|/p/|/pd/|/dp/|cate_no|category|list\.html)",
            re.IGNORECASE
        )
        filtered = [u for u in all_urls if product_re.search(u["link"])]
        if not filtered:
            filtered = all_urls

        # 최신순 정렬
        filtered.sort(key=lambda x: x.get("lastmod", ""), reverse=True)

        items = []
        for u in filtered[:10]:
            raw_name = u["link"].rstrip("/").split("/")[-1].split("?")[0]
            name = re.sub(r"[-_]", " ", raw_name)[:80] or u["link"]
            items.append({"name": name, "link": u["link"]})

        if items:
            print(f"  ✓ sitemap")
            return items

    except Exception as e:
        print(f"  sitemap 실패: {e}")

    return None


def _fetch_sitemap_urls(url: str):
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        return _parse_sitemap_root(root, ns)
    except Exception:
        return []


def _parse_sitemap_root(root, ns):
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
# 전략 3: JSON-LD (페이지에 내장된 구조화 데이터)
# ======================================================================
def try_jsonld(site_url: str):
    """
    많은 쇼핑몰이 SEO 용으로 JSON-LD 형식의 상품 데이터를 HTML 에 내장함.
    script type="application/ld+json" 에서 ItemList 또는 Product 타입 추출.
    """
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        scripts = soup.find_all("script", {"type": "application/ld+json"})

        items = []
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                # 리스트로 감싸진 경우도 처리
                if isinstance(data, list):
                    for d in data:
                        items.extend(_extract_jsonld_products(d, site_url))
                else:
                    items.extend(_extract_jsonld_products(data, site_url))
            except Exception:
                continue

        if items:
            print(f"  ✓ JSON-LD: {len(items)}개")
            return items[:10]

    except Exception as e:
        print(f"  JSON-LD 실패: {e}")

    return None


def _extract_jsonld_products(data: dict, base_url: str):
    items = []
    dtype = data.get("@type", "")

    # ItemList 타입: 상품 목록
    if dtype == "ItemList":
        for element in data.get("itemListElement", []):
            item = element.get("item", element)
            name = item.get("name", "")
            url = item.get("url", "")
            if name and url:
                if not url.startswith("http"):
                    url = urljoin(base_url, url)
                items.append({"name": name, "link": url})

    # Product 타입: 개별 상품 (목록 페이지에서 여러 개 나올 수도 있음)
    elif dtype == "Product":
        name = data.get("name", "")
        url = data.get("url", base_url)
        if name:
            items.append({"name": name, "link": url})

    # @graph 패턴
    for node in data.get("@graph", []):
        items.extend(_extract_jsonld_products(node, base_url))

    return items


# ======================================================================
# 전략 4: 사이트별 전용 파서
# ======================================================================
def try_site_specific(site_url: str):
    """등록된 사이트별 전용 파서 시도"""
    hostname = urlparse(site_url).netloc.lower()

    if "nike.com" in hostname:
        return _parse_nike(site_url)
    if "kasina.co.kr" in hostname:
        return _parse_kasina(site_url)
    if "salomon.co.kr" in hostname:
        return _parse_salomon(site_url)
    if "arcteryx.co.kr" in hostname:
        return _parse_arcteryx(site_url)
    if "worksout.co.kr" in hostname:
        return _parse_worksout(site_url)

    return None


def _parse_worksout(url: str):
    """
    웍스아웃 전용 파서.
    자체 쇼핑몰 (Shopify 아님) → sitemap에서 /products/ 패턴 URL 추출.
    """
    try:
        # sitemap에서 상품 URL 직접 추출
        sitemap_url = "https://www.worksout.co.kr/sitemap.xml"
        resp = requests.get(sitemap_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            try:
                root = ET.fromstring(resp.content)
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

                all_urls = []
                # sitemap index 처리
                sitemaps = root.findall("sm:sitemap", ns)
                if sitemaps:
                    for sm in sitemaps[:5]:
                        loc = sm.find("sm:loc", ns)
                        if loc is not None and loc.text:
                            all_urls.extend(_fetch_sitemap_urls(loc.text.strip()))
                else:
                    all_urls = _parse_sitemap_root(root, ns)

                # 상품 URL 패턴 필터 (웍스아웃: /products/숫자 또는 /goods/ 패턴)
                product_re = re.compile(r"/(products?|goods)/\d+", re.IGNORECASE)
                products = [u for u in all_urls if product_re.search(u["link"])]

                # 최신순 정렬
                products.sort(key=lambda x: x.get("lastmod", ""), reverse=True)

                if products:
                    items = []
                    for u in products[:10]:
                        raw = u["link"].rstrip("/").split("/")[-1].split("?")[0]
                        name = re.sub(r"[-_]", " ", raw)[:80] or u["link"]
                        items.append({"name": name, "link": u["link"]})
                    print(f"  ✓ 웍스아웃 sitemap: {len(items)}개")
                    return items
            except ET.ParseError:
                pass

        # sitemap 실패 시 카테고리 페이지 HTML 직접 파싱
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        product_re = re.compile(r"/(products?|goods)/\d+", re.IGNORECASE)
        links = soup.find_all("a", href=product_re)

        seen = set()
        items = []
        for a in links:
            href = a.get("href", "")
            link = urljoin("https://www.worksout.co.kr", href.split("?")[0])
            if link in seen:
                continue
            seen.add(link)
            name = ""
            img = a.find("img")
            if img:
                name = img.get("alt", "").strip()
            if not name:
                name = a.get_text(strip=True)[:80]
            if name and len(name) > 2:
                items.append({"name": name, "link": link})
            if len(items) >= 10:
                break

        if items:
            print(f"  ✓ 웍스아웃 HTML: {len(items)}개")
            return items

    except Exception as e:
        print(f"  웍스아웃 파서 실패: {e}")
    return None


def _parse_salomon(url: str):
    """살로몬 코리아 - Shopify 계열, .atom 직접 시도"""
    try:
        # /collections/ URL 이면 .atom 직접 시도
        if "/collections/" in url:
            atom_url = url.rstrip("/").split("?")[0] + ".atom"
            items = _parse_feed(atom_url)
            if items:
                print(f"  ✓ 살로몬 atom: {len(items)}개")
                return items

        # 신상품 컬렉션 URL 직접 시도
        for collection in ["new-arrivals", "new-arrivals-all", "gnb-new-arrivals-all", "new"]:
            atom_url = f"https://www.salomon.co.kr/collections/{collection}.atom"
            items = _parse_feed(atom_url)
            if items:
                print(f"  ✓ 살로몬 atom({collection}): {len(items)}개")
                return items

        # HTML 파싱 폴백
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Shopify 상품 링크 패턴: /products/상품명
            links = soup.find_all("a", href=re.compile(r"/products/[a-z0-9-]+"))
            seen = set()
            items = []
            for a in links:
                href = a.get("href", "")
                link = urljoin("https://www.salomon.co.kr", href.split("?")[0])
                if link in seen:
                    continue
                seen.add(link)
                name = ""
                img = a.find("img")
                if img:
                    name = img.get("alt", "").strip()
                if not name:
                    name = a.get_text(strip=True)[:80]
                if name and len(name) > 2:
                    items.append({"name": name, "link": link})
                if len(items) >= 10:
                    break
            if items:
                print(f"  ✓ 살로몬 HTML: {len(items)}개")
                return items
    except Exception as e:
        print(f"  살로몬 파서 실패: {e}")
    return None


def _parse_arcteryx(url: str):
    """아크테릭스 코리아 전용 파서"""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # JSON-LD 먼저 시도
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for script in scripts:
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                items = _extract_jsonld_products(data, url)
                if items:
                    print(f"  ✓ 아크테릭스 JSON-LD: {len(items)}개")
                    return items
            except Exception:
                continue

        # HTML 파싱: /products/category/ 하위 상품 링크
        product_re = re.compile(r"/products?/(?!category)[a-z0-9-/]+", re.IGNORECASE)
        links = soup.find_all("a", href=product_re)
        seen = set()
        items = []
        for a in links:
            href = a.get("href", "")
            link = urljoin("https://arcteryx.co.kr", href.split("?")[0])
            if link in seen:
                continue
            seen.add(link)
            name = ""
            img = a.find("img")
            if img:
                name = img.get("alt", "").strip()
            if not name:
                name = a.get_text(strip=True)[:80]
            if name and len(name) > 2:
                items.append({"name": name, "link": link})
            if len(items) >= 10:
                break

        if items:
            print(f"  ✓ 아크테릭스 HTML: {len(items)}개")
            return items

    except Exception as e:
        print(f"  아크테릭스 파서 실패: {e}")
    return None


def _parse_nike(url: str):
    """
    나이키는 내부 API로 상품 데이터를 가져옴.
    Wall 페이지 URL 에서 thread ID 추출 → API 호출.
    """
    try:
        # 나이키 공개 검색 API (로그인 불필요)
        # URL 에서 카테고리 코드 추출 시도
        # 예: /kr/w/new-releases-men-3n82yznik1 → 3n82yznik1

        wall_match = re.search(r'/w/[^/]+-([a-z0-9]+)$', url)
        if wall_match:
            wall_id = wall_match.group(1)
            api_url = (
                f"https://api.nike.com/cics/browse/v2"
                f"?queryid=products&anonymousId=0&country=kr&channel=NIKE"
                f"&language=ko&localizedRangeStr={{minRange}}%20~%20{{maxRange}}"
                f"&consumer=wall&subType=facets&facets=true"
                f"&filter=wall({wall_id})&filter=marketplace(KR)"
                f"&sort=newest&fields=active,id,title,product_type,pdp_url,images"
                f"&count=10"
            )
            nike_headers = {
                **HTTP_HEADERS,
                "Accept": "application/json",
                "Referer": "https://www.nike.com/",
            }
            resp = requests.get(api_url, headers=nike_headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("data", {}).get("products", {}).get("products", [])
                if products:
                    items = []
                    for p in products[:10]:
                        name = p.get("title", "")
                        pdp = p.get("pdp_url", "")
                        if name and pdp:
                            link = f"https://www.nike.com{pdp}" if pdp.startswith("/") else pdp
                            items.append({"name": name, "link": link})
                    if items:
                        print(f"  ✓ 나이키 API: {len(items)}개")
                        return items

        # API 실패 시 HTML에서 JSON 데이터 추출 (Next.js __NEXT_DATA__)
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            next_data = soup.find("script", id="__NEXT_DATA__")
            if next_data and next_data.string:
                data = json.loads(next_data.string)
                # 재귀적으로 products 배열 찾기
                items = _deep_find_products(data, url)
                if items:
                    print(f"  ✓ 나이키 HTML 파싱: {len(items)}개")
                    return items

    except Exception as e:
        print(f"  나이키 파서 실패: {e}")

    return None


def _parse_kasina(url: str):
    """카시나 전용 파서"""
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        items = []
        seen = set()

        # 카시나 상품 링크 패턴: /goods/숫자 또는 /product/숫자
        product_links = soup.find_all("a", href=re.compile(r"/(goods|product)/\d+"))

        for a in product_links:
            href = a.get("href", "")
            if not href:
                continue

            link = urljoin("https://www.kasina.co.kr", href.split("?")[0])
            if link in seen:
                continue
            seen.add(link)

            # 상품명: img alt 또는 텍스트
            name = ""
            img = a.find("img")
            if img:
                name = img.get("alt", "").strip()
            if not name:
                name = a.get_text(separator=" ", strip=True)[:80]
            if not name:
                continue

            items.append({"name": name, "link": link})
            if len(items) >= 10:
                break

        if items:
            print(f"  ✓ 카시나 HTML: {len(items)}개")
            return items

    except Exception as e:
        print(f"  카시나 파서 실패: {e}")

    return None


def _deep_find_products(data, base_url, depth=0):
    """JSON 구조에서 상품 배열을 재귀적으로 탐색"""
    if depth > 8:
        return []
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            keys = set(data[0].keys())
            if keys & {"title", "name", "productName"} and keys & {"url", "pdp_url", "href", "link"}:
                items = []
                for p in data[:10]:
                    name = p.get("title") or p.get("name") or p.get("productName") or ""
                    link = p.get("url") or p.get("pdp_url") or p.get("href") or p.get("link") or ""
                    if name and link:
                        if not link.startswith("http"):
                            link = urljoin(base_url, link)
                        items.append({"name": str(name).strip(), "link": link})
                if items:
                    return items
        for item in data:
            result = _deep_find_products(item, base_url, depth + 1)
            if result:
                return result
    elif isinstance(data, dict):
        for v in data.values():
            result = _deep_find_products(v, base_url, depth + 1)
            if result:
                return result
    return []


# ======================================================================
# 전략 5: 범용 HTML 파서
# ======================================================================
def try_generic_html(site_url: str):
    """
    상품 링크 패턴을 찾아서 이름+링크 추출.
    대부분의 쇼핑몰에서 동작하는 범용 방식.
    """
    try:
        resp = requests.get(site_url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"  HTML 가져오기 실패: HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 스크립트/스타일 제거
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        domain = urlparse(site_url).netloc
        items = []
        seen_links = set()
        seen_names = set()

        # 상품 링크 패턴
        product_re = re.compile(
            r"/(product|goods|item|pd|shop|buy|detail|view)[s]?/|"
            r"cate_no=|goods_no=|product_no=|item_no=|"
            r"/p/[a-z0-9-]{4,}|"
            r"list\.html\?",
            re.IGNORECASE
        )

        all_links = soup.find_all("a", href=True)

        for a in all_links:
            href = a.get("href", "")
            if not href or href.startswith(("#", "javascript", "mailto", "tel")):
                continue

            # 절대경로 변환
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin(site_url, href)
            elif not href.startswith("http"):
                href = urljoin(site_url, href)

            # 같은 도메인만
            if domain not in urlparse(href).netloc:
                continue

            # 상품 패턴 확인
            if not product_re.search(href):
                continue

            clean_link = href.split("?")[0].rstrip("/")
            if clean_link in seen_links:
                continue
            seen_links.add(clean_link)

            # 상품명 추출 시도
            name = ""

            # 1. img alt
            img = a.find("img")
            if img:
                name = img.get("alt", "").strip()

            # 2. 텍스트
            if not name or len(name) < 2:
                name = a.get_text(separator=" ", strip=True)

            # 3. title 속성
            if not name or len(name) < 2:
                name = a.get("title", "").strip()

            # 이름 정제
            name = re.sub(r'\s+', ' ', name).strip()[:100]

            # 너무 짧거나, 메뉴/카테고리 같은 이름은 제외
            skip_words = re.compile(r'^(홈|HOME|MENU|메뉴|전체|ALL|더보기|BACK|이전|다음|장바구니|로그인)$', re.I)
            if not name or len(name) < 3 or skip_words.match(name):
                continue

            if name in seen_names:
                continue
            seen_names.add(name)

            items.append({"name": name, "link": href})
            if len(items) >= 10:
                break

        if items:
            print(f"  ✓ HTML 범용 파서: {len(items)}개")
            return items

    except Exception as e:
        print(f"  HTML 범용 파서 실패: {e}")

    return None


# ======================================================================
# 메인 크롤링 함수
# ======================================================================
def crawl_site(site_url: str):
    """
    전략 순서:
      RSS → sitemap → JSON-LD → 사이트별 전용 → 범용 HTML
    """
    # 봇 차단 도메인은 즉시 건너뜀
    if is_blocked_domain(site_url):
        print(f"  ⚠ 봇 차단 도메인 (크롤링 불가) - 대시보드에서 수동 확인 권장")
        return None

    # Shopify /collections/ URL 은 RSS 가 확실 → RSS 만 먼저 시도
    if "/collections/" in site_url or "/products/category" in site_url:
        result = try_rss(site_url)
        if result:
            return result

    # 전략 순서대로 시도
    strategies = [
        ("RSS", try_rss),
        ("sitemap", try_sitemap),
        ("JSON-LD", try_jsonld),
        ("사이트별 전용", try_site_specific),
        ("범용 HTML", try_generic_html),
    ]

    for strategy_name, strategy_fn in strategies:
        try:
            result = strategy_fn(site_url)
            if result:
                return result
        except Exception as e:
            print(f"  {strategy_name} 예외: {e}")
        time.sleep(0.3)

    return None


def crawl_with_timeout(site_url: str):
    """사이트당 최대 45초"""
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(PER_SITE_TIMEOUT)
    try:
        return crawl_site(site_url)
    except SiteTimeout:
        print(f"  ⏱ {PER_SITE_TIMEOUT}초 초과 → 다음 사이트로")
        return None
    finally:
        signal.alarm(0)


# ======================================================================
# 텔레그램
# ======================================================================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
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
# 메인
# ======================================================================
def main():
    start_time = time.time()
    print(f"=== PRO Scanner v4 시작 @ {datetime.now(timezone.utc).isoformat()} ===")
    print(f"    Gemini 없음 | 순수 크롤링 모드\n")

    db = init_firebase()
    sites_ref = (
        db.collection("artifacts")
        .document(APP_ID)
        .collection("public")
        .document("data")
        .collection("monitoring_sites")
    )

    raw_sites = list(sites_ref.stream())
    if not raw_sites:
        print("등록된 사이트가 없습니다.")
        return

    # 무신사 제외, 봇 차단 도메인은 맨 뒤로
    def sort_key(doc):
        url = doc.to_dict().get("url", "")
        if "musinsa.com" in url:
            return 2  # 무신사: 맨 뒤 (사실상 스킵)
        if is_blocked_domain(url):
            return 1  # 봇 차단: 뒤쪽
        return 0

    sites = sorted(raw_sites, key=sort_key)

    print(f"총 {len(sites)}개 사이트 스캔 시작\n")

    success_count = 0
    fail_count = 0
    skip_count = 0
    total_new = 0

    for site_doc in sites:
        elapsed = time.time() - start_time
        if elapsed > 22 * 60:  # 22분 초과 시 중단
            print(f"\n⚠ 22분 경과 → 조기 종료")
            break

        data = site_doc.to_dict()
        site_id = site_doc.id
        name = data.get("name", "(이름 없음)")
        url = data.get("url", "")
        prev_items = data.get("items", []) or []

        print(f"[{name}] {url}")

        if not url:
            skip_count += 1
            continue

        # 무신사는 스킵
        if "musinsa.com" in url:
            print(f"  → 무신사 제외 (직접 확인 권장)")
            skip_count += 1
            continue

        t0 = time.time()
        fetched = crawl_with_timeout(url)
        elapsed_site = time.time() - t0

        if not fetched:
            print(f"  ✗ 실패 ({elapsed_site:.1f}s)")
            sites_ref.document(site_id).update({
                "lastError": "크롤링 실패 (모든 전략 응답 없음)",
                "lastErrorAt": datetime.now(timezone.utc).isoformat(),
            })
            fail_count += 1
            continue

        # 신상품 비교
        new_items = []
        if prev_items:
            prev_keys = {(p.get("link", ""), p.get("name", "")) for p in prev_items}
            new_items = [
                f for f in fetched
                if not any(
                    p.get("link") == f.get("link") or p.get("name") == f.get("name")
                    for p in prev_items
                )
            ]

        sites_ref.document(site_id).update({
            "items": fetched,
            "newItems": new_items,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
        })

        success_count += 1
        total_new += len(new_items)
        print(f"  → 전체 {len(fetched)}개 / 신규 {len(new_items)}개 ({elapsed_site:.1f}s)")

        if new_items:
            msg = [f"🆕 <b>{name}</b> 신상품 {len(new_items)}건"]
            for item in new_items[:5]:
                msg.append(f"• <a href='{item.get('link','#')}'>{item.get('name','')}</a>")
            if len(new_items) > 5:
                msg.append(f"…외 {len(new_items)-5}건")
            send_telegram("\n".join(msg))

        time.sleep(0.5)

    total_elapsed = time.time() - start_time
    print(f"\n=== 스캔 완료 ({total_elapsed:.1f}s) ===")
    print(f"  성공: {success_count} / 실패: {fail_count} / 스킵: {skip_count} / 신규: {total_new}건")


if __name__ == "__main__":
    main()
