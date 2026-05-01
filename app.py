"""
求人給与ランキング（ローカル専用・高精度版）
起動: python3 app.py  →  http://localhost:8080

・Indeed: ヘッド付きブラウザ＋永続プロファイルで初回ログイン保存、以降自動ログイン
・全サイト最大10ページ取得
・雇用形態（派遣/バイト/パートetc）・勤務日数（週3/単発etc）を表示
・TOP10ランキング
"""

import asyncio
import json
import os
import queue
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request, stream_with_context
from playwright.async_api import async_playwright

try:
    from curl_cffi import requests as cffi_req
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

app = Flask(__name__)

MAX_PAGES      = 10    # 各サイト最大ページ数
TOP_N          = 10    # ランキング表示件数
LOGIN_WAIT     = 30    # Indeedログイン待機秒数
INDEED_PROFILE = os.path.expanduser("~/.kyujin_indeed_profile")

SHOKUSHU_LIST = [
    "営業職", "事務職", "エンジニア", "販売・接客",
    "製造・工場", "介護・福祉", "ドライバー", "軽作業",
    "医療・看護", "デザイナー",
]

# ─────────────────────────────────────────
# HTTP セッション
# ─────────────────────────────────────────

_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})


# ─────────────────────────────────────────
# 給与パース（範囲対応・max値使用）
# ─────────────────────────────────────────

def parse_salary(text: str) -> dict:
    result = {
        "raw": text, "jikyu": None, "jikyu_min": None,
        "monthly": None, "annual": None, "jikyu_normalized": None,
    }
    if not text:
        return result
    t = text.replace(",", "").replace("，", "").replace(" ", "").replace("　", "")

    # 時給（範囲 例: 時給1900〜2500円）→ max使用
    m = re.search(r"時給[^\d]*(\d{3,6})(?:円)?[〜~\-－～]+(\d{3,6})", t)
    if m:
        mn, mx = int(m.group(1)), int(m.group(2))
        result["jikyu"] = mx
        result["jikyu_min"] = mn
        result["jikyu_normalized"] = mx
        return result

    # 時給（単一）
    m = re.search(r"時給[^\d]*(\d{3,6})", t)
    if m:
        v = int(m.group(1))
        result["jikyu"] = v
        result["jikyu_normalized"] = v
        return result

    # 日給（8時間換算）
    m = re.search(r"日給[^\d]*(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(1))
        if m.group(2) == "万":
            val *= 10000
        result["jikyu_normalized"] = int(val / 8)
        return result

    # 月給（範囲 → max）
    m = re.search(r"月[給収][^\d]*(\d+(?:\.\d+)?)(万)?[〜~\-－～]+(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(3))
        if m.group(4) == "万":
            val *= 10000
        result["monthly"] = int(val)
        result["jikyu_normalized"] = int(val / 160)
        return result

    # 月給（単一）
    m = re.search(r"月[給収][^\d]*(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(1))
        if m.group(2) == "万":
            val *= 10000
        result["monthly"] = int(val)
        result["jikyu_normalized"] = int(val / 160)
        return result

    # 年収（範囲 → max）
    m = re.search(r"年[収給][^\d]*(\d+(?:\.\d+)?)(万)?[〜~\-－～]+(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(3))
        if m.group(4) == "万":
            val *= 10000
        result["annual"] = int(val)
        result["jikyu_normalized"] = int(val / (12 * 160))
        return result

    # 年収（単一）
    m = re.search(r"年[収給][^\d]*(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(1))
        if m.group(2) == "万":
            val *= 10000
        result["annual"] = int(val)
        result["jikyu_normalized"] = int(val / (12 * 160))
        return result

    # NN万円（月給扱い）
    m = re.search(r"(\d+(?:\.\d+)?)万円?", t)
    if m:
        val = float(m.group(1)) * 10000
        result["monthly"] = int(val)
        result["jikyu_normalized"] = int(val / 160)
    return result


def salary_display(parsed: dict) -> str:
    if parsed.get("jikyu"):
        if parsed.get("jikyu_min") and parsed["jikyu_min"] != parsed["jikyu"]:
            return f"時給 {parsed['jikyu_min']:,}〜{parsed['jikyu']:,}円"
        return f"時給 {parsed['jikyu']:,}円"
    if parsed.get("monthly"):
        return f"月給 {parsed['monthly']:,}円"
    if parsed.get("annual"):
        return f"年収 {parsed['annual']:,}円"
    return parsed.get("raw") or "—"


# ─────────────────────────────────────────
# 雇用形態・勤務体制パース
# ─────────────────────────────────────────

def extract_employment_type(text: str) -> str:
    for pattern, label in [
        (r"派遣社員|派遣スタッフ|人材派遣|派遣", "派遣"),
        (r"アルバイト|バイト", "アルバイト"),
        (r"パートタイム|パート", "パート"),
        (r"正社員", "正社員"),
        (r"契約社員", "契約社員"),
        (r"業務委託|フリーランス", "業務委託"),
    ]:
        if re.search(pattern, text):
            return label
    return ""


def extract_work_schedule(text: str) -> str:
    # 週N〜M日
    m = re.search(r"週\s*([1-7])\s*[〜~～]\s*([1-7])\s*日", text)
    if m:
        return f"週{m.group(1)}〜{m.group(2)}日"
    # 週N日
    m = re.search(r"週\s*([1-7])\s*日", text)
    if m:
        return f"週{m.group(1)}日"
    # 週N回
    m = re.search(r"週\s*([1-7])\s*回", text)
    if m:
        return f"週{m.group(1)}回"
    # 週N日以上
    m = re.search(r"週\s*([1-7])\s*日以上", text)
    if m:
        return f"週{m.group(1)}日〜"
    if re.search(r"単発", text):
        return "単発"
    if re.search(r"短期", text):
        return "短期"
    if re.search(r"フルタイム", text):
        return "フルタイム"
    if re.search(r"土日(?:祝)?のみ|週末のみ", text):
        return "土日のみ"
    if re.search(r"平日のみ", text):
        return "平日のみ"
    if re.search(r"シフト自由", text):
        return "シフト自由"
    if re.search(r"シフト制", text):
        return "シフト制"
    return ""


# ─────────────────────────────────────────
# テキスト汎用抽出（フォールバック）
# ─────────────────────────────────────────

NOISE = re.compile(
    r"^(検索|絞り込み|ログイン|新規登録|会員|エリア|こだわり|条件|特集|新着|おすすめ|"
    r"アルバイト|バイト|正社員|派遣|パート|契約社員|求人|仕事|サイト|ページ|一覧|"
    r"トップ|ホーム|ナビ|メニュー|詳細|確認|\d+件|\d+ページ)$"
)

def extract_jobs_from_text(text: str, site_name: str) -> list:
    jobs = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    seen: set = set()
    for i, line in enumerate(lines):
        if not re.search(r"(時給|月給|日給|年収)[^\d]*\d", line):
            continue
        title, company = "", ""
        for j in range(i - 1, max(0, i - 16), -1):
            ln = lines[j]
            if len(ln) < 4 or NOISE.match(ln):
                continue
            if re.search(r"\d+万|\d+円|〜|以上|応相談|※|【|】|\d+件", ln):
                continue
            if not title:
                title = ln[:80]
            elif not company:
                company = ln[:50]
                break
        if title and title not in seen:
            seen.add(title)
            ctx = " ".join(lines[max(0, i - 8):i + 4])
            jobs.append({
                "site": site_name, "title": title, "company": company,
                "location": "", "salary_text": line,
                "employment_type": extract_employment_type(ctx),
                "work_schedule": extract_work_schedule(ctx),
            })
    return jobs


# ─────────────────────────────────────────
# HTTP フェッチ
# ─────────────────────────────────────────

def _fetch(url: str, referer: str = "https://www.google.co.jp/") -> BeautifulSoup | None:
    try:
        resp = _http.get(url, headers={"Referer": referer}, timeout=20, allow_redirects=True)
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


# ─────────────────────────────────────────
# マイナビ転職（HTTP・並列ページ取得）
# ─────────────────────────────────────────

def _extract_mynavi_t(soup: BeautifulSoup) -> list:
    jobs = []
    cards = soup.select(".cassetteRecruit, [class*='cassetteRecruit']")
    if not cards:
        cards = soup.select("article, [class*='recruit'], [class*='Recruit']")
    for card in cards:
        he = card.select_one(".cassetteRecruit__heading, [class*='heading'], [class*='Heading']")
        title, company = "", ""
        if he:
            hl = [t for t in he.get_text("\n", strip=True).split("\n") if t.strip()]
            if hl:
                company = hl[0].split("|")[0].strip()
            if len(hl) > 1:
                title = hl[1]
        if not title:
            h = card.find(["h2", "h3"])
            if h:
                title = h.get_text(strip=True)
        card_text = card.get_text("\n", strip=True)
        salary = ""
        for ptn in [r"給与\s*[\t\n]\s*([^\n]+)",
                    r"月給[^\d]*(\d[\d,万円〜\-～以上]+)",
                    r"年収[^\d]*(\d[\d,万円〜\-～以上]+)"]:
            m = re.search(ptn, card_text)
            if m:
                salary = m.group(1).strip()
                break
        if title:
            jobs.append({
                "site": "マイナビ転職", "title": title, "company": company,
                "location": "", "salary_text": salary,
                "employment_type": extract_employment_type(card_text),
                "work_schedule": extract_work_schedule(card_text),
            })
    return jobs


def _fetch_mynavi_t_page(keyword: str, page: int) -> list:
    url = (f"https://tenshoku.mynavi.jp/list/?keyword={urllib.parse.quote(keyword)}"
           if page == 1 else
           f"https://tenshoku.mynavi.jp/list/?keyword={urllib.parse.quote(keyword)}&p={page}")
    soup = _fetch(url, referer="https://tenshoku.mynavi.jp/")
    if not soup:
        return []
    jobs = _extract_mynavi_t(soup)
    if not jobs and page == 1:
        jobs = extract_jobs_from_text(soup.get_text("\n", strip=True), "マイナビ転職")
    return jobs


def http_scrape_mynavi_tenshoku(keyword: str) -> list:
    with ThreadPoolExecutor(max_workers=MAX_PAGES) as ex:
        pages = list(ex.map(partial(_fetch_mynavi_t_page, keyword), range(1, MAX_PAGES + 1)))
    seen, all_jobs = set(), []
    for jobs in pages:
        for j in jobs:
            if j["title"] not in seen:
                seen.add(j["title"])
                all_jobs.append(j)
    return all_jobs


# ─────────────────────────────────────────
# エン転職（HTTP・並列ページ取得）
# ─────────────────────────────────────────

def _extract_en_tenshoku(soup: BeautifulSoup) -> list:
    jobs = []
    cards = soup.select(".jobSearchListUnit, [class*='jobSearchList'], [class*='job-unit'], article, li[class*='item']")
    for card in cards:
        te = card.select_one(".jobNameText, [class*='jobName'], h2 a, h3 a")
        ce = card.select_one(".company, [class*='companyName'], [class*='company']")
        title = te.get_text(strip=True) if te else ""
        company = ce.get_text(strip=True) if ce else ""
        card_text = card.get_text("\n", strip=True)
        salary = ""
        for ptn in [r"給与\s*\n\s*([^\n]+)",
                    r"月給[^\d]*(\d[\d,万円〜\-～以上]+)",
                    r"年収[^\d]*(\d[\d,万円〜\-～以上]+)",
                    r"時給[^\d]*(\d[\d,]+(?:[〜~]\d[\d,]+)?)"]:
            m = re.search(ptn, card_text)
            if m:
                salary = m.group(1).strip()
                break
        if title:
            jobs.append({
                "site": "エン転職", "title": title, "company": company,
                "location": "", "salary_text": salary,
                "employment_type": extract_employment_type(card_text),
                "work_schedule": extract_work_schedule(card_text),
            })
    return jobs


def _fetch_en_tenshoku_page(keyword: str, page: int) -> list:
    url = (f"https://employment.en-japan.com/keyword/{urllib.parse.quote(keyword, safe='')}/"
           if page == 1 else
           f"https://employment.en-japan.com/keyword/{urllib.parse.quote(keyword, safe='')}/{page}/")
    soup = _fetch(url, referer="https://employment.en-japan.com/")
    if not soup:
        return []
    jobs = _extract_en_tenshoku(soup)
    if not jobs and page == 1:
        jobs = extract_jobs_from_text(soup.get_text("\n", strip=True), "エン転職")
    return jobs


def http_scrape_en_tenshoku(keyword: str) -> list:
    with ThreadPoolExecutor(max_workers=MAX_PAGES) as ex:
        pages = list(ex.map(partial(_fetch_en_tenshoku_page, keyword), range(1, MAX_PAGES + 1)))
    seen, all_jobs = set(), []
    for jobs in pages:
        for j in jobs:
            if j["title"] not in seen:
                seen.add(j["title"])
                all_jobs.append(j)
    return all_jobs


# ─────────────────────────────────────────
# バイトル（HTTP・並列ページ取得）
# ─────────────────────────────────────────

def _extract_baitoru(soup: BeautifulSoup) -> list:
    jobs = []
    cards = soup.select(
        ".cassette, .cassetteJobs, .jobList__item, "
        "[class*='jobList'], [class*='job-list'], "
        "[class*='job-unit'], [class*='jobUnit']"
    )
    if not cards:
        return extract_jobs_from_text(soup.get_text("\n", strip=True), "バイトル")
    for card in cards:
        te = card.select_one(
            ".jobName, .cassetteJobs__name, [class*='jobName'], "
            "[class*='jobTitle'], [class*='title'], h2, h3"
        )
        ce = card.select_one(
            ".shopName, .storeName, [class*='shopName'], [class*='storeName'], "
            "[class*='company'], [class*='Company']"
        )
        title = te.get_text(strip=True) if te else ""
        company = ce.get_text(strip=True) if ce else ""
        card_text = card.get_text("\n", strip=True)
        salary = ""
        m = re.search(r"(時給|日給|月給)[^\d]*(\d[\d,]+(?:[〜~\-]\d[\d,]+)?)", card_text)
        if m:
            salary = m.group(0)
        if title:
            jobs.append({
                "site": "バイトル", "title": title, "company": company,
                "location": "", "salary_text": salary,
                "employment_type": extract_employment_type(card_text),
                "work_schedule": extract_work_schedule(card_text),
            })
    return jobs


def _fetch_baitoru_page(keyword: str, page: int) -> list:
    base = f"https://www.baitoru.com/op/list/?keyword={urllib.parse.quote(keyword, safe='')}"
    url = base if page == 1 else f"{base}&pageNo={page}"
    soup = _fetch(url, referer="https://www.baitoru.com/")
    if not soup:
        return []
    jobs = _extract_baitoru(soup)
    if not jobs and page == 1:
        jobs = extract_jobs_from_text(soup.get_text("\n", strip=True), "バイトル")
    return jobs


def http_scrape_baitoru(keyword: str) -> list:
    with ThreadPoolExecutor(max_workers=MAX_PAGES) as ex:
        pages = list(ex.map(partial(_fetch_baitoru_page, keyword), range(1, MAX_PAGES + 1)))
    seen, all_jobs = set(), []
    for jobs in pages:
        for j in jobs:
            if j["title"] not in seen:
                seen.add(j["title"])
                all_jobs.append(j)
    return all_jobs


# ─────────────────────────────────────────
# Playwright: Indeed（ヘッド付き＋永続プロファイル）
# ─────────────────────────────────────────

async def playwright_scrape_indeed(keyword: str, q: queue.Queue) -> list:
    """
    永続プロファイル（~/.kyujin_indeed_profile）でログイン状態を保存。
    初回のみログインが必要（ブラウザが開くので手動でログイン）。
    以降は自動ログイン済み。
    """
    all_jobs = []
    seen: set = set()
    q_enc = urllib.parse.quote(keyword)

    try:
        async with async_playwright() as p:
            os.makedirs(INDEED_PROFILE, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=INDEED_PROFILE,
                headless=False,   # ← ヘッド付き（ログイン可能、ブロック回避）
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,800",
                    "--start-maximized",
                ],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                ignore_https_errors=True,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja','en-US']});"
            )

            page = await context.new_page()

            # Indeedホームへ移動してログイン状態を確認
            try:
                await page.goto("https://jp.indeed.com/", timeout=20000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
            except Exception:
                pass

            # ログイン状態チェック
            is_logged_in = bool(await page.query_selector(
                '[data-testid="UserProfileMenuButton"], .gnav-UserInfo__email, '
                '[class*="AccountMenuButton"], [class*="userAccount"], [aria-label*="アカウント"]'
            ))

            if not is_logged_in:
                q.put({"type": "progress", "site": "Indeed",
                       "status": "login_wait",
                       "msg": "ブラウザが開きました。Indeedにログインしてください。"})
                for sec in range(LOGIN_WAIT, 0, -1):
                    q.put({"type": "progress", "site": "Indeed",
                           "status": "countdown", "seconds": sec})
                    await asyncio.sleep(1)
                    try:
                        is_logged_in = bool(await page.query_selector(
                            '[data-testid="UserProfileMenuButton"], .gnav-UserInfo__email, '
                            '[class*="AccountMenuButton"]'
                        ))
                        if is_logged_in:
                            break
                    except Exception:
                        pass

            q.put({"type": "progress", "site": "Indeed", "status": "scraping"})

            # 全ページスクレイピング
            for pg in range(1, MAX_PAGES + 1):
                url = (f"https://jp.indeed.com/jobs?q={q_enc}&sort=date&limit=20"
                       if pg == 1 else
                       f"https://jp.indeed.com/jobs?q={q_enc}&sort=date&limit=20&start={20*(pg-1)}")
                try:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)
                except Exception:
                    break

                title_text = await page.title()
                body_text = await page.inner_text("body")
                if any(w in title_text + body_text
                       for w in ["Security Check", "CAPTCHA", "robot", "403 Forbidden"]):
                    break

                cards = await page.query_selector_all("div.job_seen_beacon, div[data-jk]")
                page_jobs = []

                for card in cards:
                    try:
                        te = await card.query_selector(
                            "h2 a span[title], h2.jobTitle span, h2 a span")
                        ce = await card.query_selector(
                            "[data-testid='company-name'], .companyName")
                        se = await card.query_selector(
                            "[data-testid='attribute_snippet_testid'], "
                            "[class*='salary-snippet'], [class*='salaryText']")
                        title = (await te.inner_text()).strip() if te else ""
                        if not title:
                            continue
                        card_text = (await card.inner_text()).strip()
                        salary = (await se.inner_text()).strip() if se else ""
                        if not salary:
                            m2 = re.search(
                                r"(時給|月給|日給|年収)[^\d]*\d[\d,万円〜\-～]+", card_text)
                            if m2:
                                salary = m2.group(0)
                        if title not in seen:
                            seen.add(title)
                            page_jobs.append({
                                "site": "Indeed",
                                "title": title,
                                "company": (await ce.inner_text()).strip() if ce else "",
                                "location": "",
                                "salary_text": salary,
                                "employment_type": extract_employment_type(card_text),
                                "work_schedule": extract_work_schedule(card_text),
                            })
                    except Exception:
                        continue

                if not page_jobs and pg == 1:
                    fallback = extract_jobs_from_text(body_text, "Indeed")
                    for j in fallback:
                        if j["title"] not in seen:
                            seen.add(j["title"])
                            all_jobs.append(j)
                    break

                all_jobs.extend(page_jobs)
                if not page_jobs:
                    break

            try:
                await context.close()
            except Exception:
                pass

    except Exception as e:
        q.put({"type": "progress", "site": "Indeed",
               "status": "error", "msg": f"ブラウザエラー: {e}"})

    return all_jobs


# ─────────────────────────────────────────
# Playwright: エンゲージ（全ページ）
# ─────────────────────────────────────────

async def playwright_scrape_engage(page, keyword: str) -> list:
    all_jobs = []
    seen: set = set()
    try:
        for pg in range(1, MAX_PAGES + 1):
            url = (f"https://en-gage.net/user/search/?searchWord={urllib.parse.quote(keyword)}"
                   if pg == 1 else
                   f"https://en-gage.net/user/search/?searchWord={urllib.parse.quote(keyword)}&page={pg}")
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            cards = await page.query_selector_all(
                "article, [class*='jobCard'], [class*='job-card'], [class*='JobCard'],"
                "[class*='search-result'], [class*='SearchResult']"
            )
            page_jobs = []
            for card in cards:
                try:
                    te = await card.query_selector("h2,h3,[class*='title'],[class*='Title']")
                    title = (await te.inner_text()).strip() if te else ""
                    card_text = (await card.inner_text()).strip()
                    salary = ""
                    m = re.search(r"(時給|月給|日給|年収)[^\d]*\d[\d,万円〜\-～]+", card_text)
                    if m:
                        salary = m.group(0)
                    if title and title not in seen:
                        seen.add(title)
                        page_jobs.append({
                            "site": "エンゲージ", "title": title,
                            "company": "", "location": "", "salary_text": salary,
                            "employment_type": extract_employment_type(card_text),
                            "work_schedule": extract_work_schedule(card_text),
                        })
                except Exception:
                    continue

            if not page_jobs and pg == 1:
                body = await page.inner_text("body")
                fallback = extract_jobs_from_text(body, "エンゲージ")
                for j in fallback:
                    if j["title"] not in seen:
                        seen.add(j["title"])
                        all_jobs.append(j)
                break

            all_jobs.extend(page_jobs)
            if not page_jobs:
                break
    except Exception:
        pass
    return all_jobs


# ─────────────────────────────────────────
# Playwright: マイナビバイト（全ページ）
# ─────────────────────────────────────────

async def playwright_scrape_mynavi_baito(page, keyword: str) -> list:
    all_jobs = []
    seen: set = set()
    try:
        word = keyword.replace("職", "").replace("・", "")
        base_url = f"https://baito.mynavi.jp/ai/word_{word}/"

        for pg in range(1, MAX_PAGES + 1):
            url = base_url if pg == 1 else f"{base_url}?p={pg}"
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            cards = await page.query_selector_all(".tabJobOfferCard")
            page_jobs = []
            for card in cards:
                try:
                    lines = [l.strip() for l in (await card.inner_text()).split("\n") if l.strip()]
                    if len(lines) < 2:
                        continue
                    company = re.sub(r"/\d+$", "", lines[0]).strip()
                    title = lines[1]
                    card_text = "\n".join(lines)
                    salary_text = ""
                    for i, line in enumerate(lines):
                        if line == "給与" and i + 1 < len(lines):
                            salary_text = lines[i + 1]
                            break
                    if not salary_text:
                        m = re.search(r"(時給|月給)[^\d]*\d[\d,]+", card_text)
                        if m:
                            salary_text = m.group(0)
                    if title and title not in seen:
                        seen.add(title)
                        page_jobs.append({
                            "site": "マイナビ", "title": title, "company": company,
                            "location": "", "salary_text": salary_text,
                            "employment_type": extract_employment_type(card_text),
                            "work_schedule": extract_work_schedule(card_text),
                        })
                except Exception:
                    continue

            if not page_jobs and pg == 1:
                body = await page.inner_text("body")
                fallback = extract_jobs_from_text(body, "マイナビ")
                for j in fallback:
                    if j["title"] not in seen:
                        seen.add(j["title"])
                        all_jobs.append(j)
                break

            all_jobs.extend(page_jobs)
            if not page_jobs:
                break
    except Exception:
        pass
    return all_jobs


# ─────────────────────────────────────────
# HTTPスクレイパー並列ラッパー
# ─────────────────────────────────────────

async def _run_http(site_name: str, fn, keyword: str, q: queue.Queue) -> list:
    q.put({"type": "progress", "site": site_name, "status": "searching"})
    try:
        jobs = await asyncio.to_thread(fn, keyword)
        sc = sum(1 for j in jobs if j.get("salary_text"))
        q.put({"type": "progress", "site": site_name,
               "status": "done", "count": len(jobs), "salary_count": sc})
        return jobs
    except Exception as e:
        q.put({"type": "progress", "site": site_name, "status": "error", "msg": str(e)})
        return []


# ─────────────────────────────────────────
# メインスクレイパー
# ─────────────────────────────────────────

async def run_scraper(keyword: str, q: queue.Queue):
    all_jobs = []

    # ── Phase 1: HTTP スクレイパー（並列） ──
    results = await asyncio.gather(
        _run_http("マイナビ転職", http_scrape_mynavi_tenshoku, keyword, q),
        _run_http("エン転職",     http_scrape_en_tenshoku,     keyword, q),
        _run_http("バイトル",     http_scrape_baitoru,         keyword, q),
    )
    for jobs in results:
        all_jobs.extend(jobs)

    # ── Phase 2: Indeed（ヘッド付きPlaywright・永続プロファイル） ──
    q.put({"type": "progress", "site": "Indeed", "status": "searching"})
    indeed_jobs = await playwright_scrape_indeed(keyword, q)
    all_jobs.extend(indeed_jobs)
    sc = sum(1 for j in indeed_jobs if j.get("salary_text"))
    q.put({"type": "progress", "site": "Indeed",
           "status": "done", "count": len(indeed_jobs), "salary_count": sc})

    # ── Phase 3: エンゲージ＋マイナビバイト（ヘッドレスPlaywright） ──
    q.put({"type": "progress", "site": "マイナビ", "status": "searching"})
    q.put({"type": "progress", "site": "エンゲージ", "status": "searching"})

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu", "--disable-setuid-sandbox",
                "--window-size=1280,800",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )

        # マイナビバイト
        page1 = await context.new_page()
        try:
            jobs = await playwright_scrape_mynavi_baito(page1, keyword)
            all_jobs.extend(jobs)
            sc = sum(1 for j in jobs if j.get("salary_text"))
            q.put({"type": "progress", "site": "マイナビ",
                   "status": "done", "count": len(jobs), "salary_count": sc})
        except Exception as e:
            q.put({"type": "progress", "site": "マイナビ",
                   "status": "error", "msg": str(e)})
        finally:
            await page1.close()

        # エンゲージ
        page2 = await context.new_page()
        try:
            jobs = await playwright_scrape_engage(page2, keyword)
            all_jobs.extend(jobs)
            sc = sum(1 for j in jobs if j.get("salary_text"))
            q.put({"type": "progress", "site": "エンゲージ",
                   "status": "done", "count": len(jobs), "salary_count": sc})
        except Exception as e:
            q.put({"type": "progress", "site": "エンゲージ",
                   "status": "error", "msg": str(e)})
        finally:
            await page2.close()

        await browser.close()

    # ── ランキング計算 ──
    parsed_jobs = []
    seen_titles: set = set()
    for job in all_jobs:
        # サイト間の重複除去
        key = f"{job['title']}_{job.get('company','')}"
        if key in seen_titles:
            continue
        seen_titles.add(key)
        parsed = parse_salary(job.get("salary_text", ""))
        if parsed["jikyu_normalized"]:
            parsed_jobs.append({**job, **parsed})

    jikyu_ranking = sorted(
        [j for j in parsed_jobs if j.get("jikyu")],
        key=lambda x: x["jikyu"], reverse=True,
    )[:TOP_N]

    salary_ranking = sorted(
        parsed_jobs, key=lambda x: x["jikyu_normalized"], reverse=True,
    )[:TOP_N]

    def fmt(jobs):
        return [{
            "site":    j["site"],
            "title":   j["title"],
            "company": j["company"],
            "salary":  salary_display(j),
            "raw":     j.get("salary_text", ""),
            "emp":     j.get("employment_type", ""),
            "sched":   j.get("work_schedule", ""),
        } for j in jobs]

    q.put({
        "type": "result",
        "total": len(all_jobs),
        "timestamp": datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        "jikyu_ranking":  fmt(jikyu_ranking),
        "salary_ranking": fmt(salary_ranking),
    })
    q.put(None)


# ─────────────────────────────────────────
# Flask ルート
# ─────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/search")
def search():
    keyword = request.args.get("keyword", "")
    if not keyword:
        return Response("keyword required", status=400)

    q = queue.Queue()

    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_scraper(keyword, q))
        loop.close()

    threading.Thread(target=run_in_thread, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                yield 'data: {"type":"end"}\n\n'
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────
# HTML テンプレート
# ─────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>求人給与ランキング</title>
<style>
  :root {
    --bg:#f0f4f8;--surface:#fff;--header:#1a3654;
    --primary:#1d4ed8;--primary-light:#eff6ff;--primary-border:#bfdbfe;
    --success:#15803d;--success-light:#f0fdf4;--success-border:#86efac;
    --warning:#b45309;--warning-light:#fffbeb;--warning-border:#fcd34d;
    --error:#dc2626;--error-light:#fef2f2;--error-border:#fca5a5;
    --login:#7c3aed;--login-light:#f5f3ff;--login-border:#c4b5fd;
    --text:#111827;--text-sub:#374151;--text-muted:#6b7280;
    --border:#e5e7eb;--border-mid:#d1d5db;
    --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.06);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic UI',sans-serif;
    background:var(--bg);color:var(--text);min-height:100vh;font-size:14px;line-height:1.5}

  header{background:var(--header);height:56px;display:flex;align-items:center;
    padding:0 28px;gap:14px;box-shadow:0 1px 4px rgba(0,0,0,.25);
    position:sticky;top:0;z-index:10}
  .hdr-icon{width:34px;height:34px;background:rgba(255,255,255,.12);border-radius:8px;
    display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .hdr-title{font-size:1.05em;font-weight:700;color:#fff;letter-spacing:.02em}
  .hdr-sub{font-size:.73em;color:rgba(255,255,255,.5);margin-left:2px}

  .layout{display:grid;grid-template-columns:210px 1fr;min-height:calc(100vh - 56px)}

  .sidebar{background:var(--surface);border-right:1px solid var(--border);
    padding:18px 10px;overflow-y:auto}
  .sidebar-heading{font-size:.68em;font-weight:700;color:var(--text-muted);
    letter-spacing:.1em;text-transform:uppercase;padding:0 8px;margin-bottom:8px}
  .job-btn{display:flex;align-items:center;gap:9px;width:100%;padding:8px 10px;
    margin-bottom:2px;background:transparent;border:none;border-radius:6px;
    color:var(--text-sub);font-size:.88em;text-align:left;cursor:pointer;transition:background .12s}
  .job-btn:hover{background:var(--bg)}
  .job-btn.active{background:var(--primary-light);color:var(--primary);font-weight:600}
  .job-abbr{width:26px;height:26px;border-radius:5px;background:var(--bg);
    border:1px solid var(--border);color:var(--text-muted);font-size:.78em;font-weight:700;
    display:flex;align-items:center;justify-content:center;flex-shrink:0;
    transition:background .12s,color .12s,border-color .12s}
  .job-btn.active .job-abbr{background:var(--primary);color:#fff;border-color:var(--primary)}

  .main{padding:24px 28px;overflow-x:hidden}

  .empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
    height:60vh;color:var(--text-muted);text-align:center;gap:10px}
  .empty-icon{width:60px;height:60px;background:var(--border);border-radius:14px;
    display:flex;align-items:center;justify-content:center;margin-bottom:4px}
  .empty h3{font-size:.95em;font-weight:600;color:var(--text-sub)}
  .empty p{font-size:.83em;line-height:1.7}

  /* プログレス */
  .progress-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
    padding:16px 20px;margin-bottom:18px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
  .progress-head{display:flex;align-items:center;gap:8px;font-size:.78em;font-weight:700;
    color:var(--text-muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
  .site-rows{display:flex;flex-direction:column;gap:4px}
  .site-row{display:flex;align-items:center;gap:10px;padding:7px 12px;border-radius:6px;
    background:var(--bg);border:1px solid var(--border);transition:all .22s;min-height:38px}
  .site-row.searching{background:var(--warning-light);border-color:var(--warning-border)}
  .site-row.done    {background:var(--success-light);border-color:var(--success-border)}
  .site-row.error   {background:var(--error-light);border-color:var(--error-border)}
  .site-row.login-wait{background:var(--login-light);border-color:var(--login-border)}
  .site-row.scraping{background:var(--primary-light);border-color:var(--primary-border)}
  .s-dot{width:7px;height:7px;border-radius:50%;background:var(--border);flex-shrink:0}
  .site-row.searching .s-dot,.site-row.login-wait .s-dot,.site-row.scraping .s-dot{
    animation:blink .85s infinite}
  .site-row.searching .s-dot{background:var(--warning)}
  .site-row.login-wait .s-dot{background:var(--login)}
  .site-row.scraping .s-dot{background:var(--primary)}
  .site-row.done  .s-dot{background:var(--success)}
  .site-row.error .s-dot{background:var(--error)}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
  .s-name{font-size:.85em;font-weight:600;min-width:96px;color:var(--text-sub)}
  .s-status{font-size:.78em;color:var(--text-muted);flex:1}
  .s-status b{color:var(--login);font-weight:700}
  .s-badge{font-size:.72em;color:var(--text-muted);background:rgba(0,0,0,.05);
    padding:2px 8px;border-radius:10px;white-space:nowrap}

  .meta-bar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  .kw-pill{font-size:.83em;font-weight:700;background:var(--primary);color:#fff;
    padding:3px 13px;border-radius:20px}
  .meta-ts{font-size:.78em;color:var(--text-muted)}
  .meta-total{font-size:.78em;color:var(--success);font-weight:600;margin-left:auto}

  .rankings{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:960px){.rankings{grid-template-columns:1fr}}

  .rank-card{background:var(--surface);border:1px solid var(--border);
    border-radius:10px;overflow:hidden;box-shadow:var(--shadow)}
  .rank-head{display:flex;align-items:center;gap:8px;padding:11px 16px;
    font-size:.83em;font-weight:700;border-bottom:1px solid var(--border)}
  .rank-head.t-jikyu {background:var(--warning-light);color:var(--warning);
    border-bottom-color:var(--warning-border)}
  .rank-head.t-salary{background:var(--success-light);color:var(--success);
    border-bottom-color:var(--success-border)}
  .head-badge{width:20px;height:20px;border-radius:4px;display:flex;align-items:center;
    justify-content:center;font-size:.72em;font-weight:800;flex-shrink:0}
  .t-jikyu  .head-badge{background:var(--warning-border);color:var(--warning)}
  .t-salary .head-badge{background:var(--success-border);color:var(--success)}

  /* ランクアイテム */
  .rank-item{display:flex;align-items:flex-start;gap:9px;
    padding:10px 14px;border-bottom:1px solid var(--border);transition:background .1s}
  .rank-item:last-child{border-bottom:none}
  .rank-item:hover{background:var(--bg)}
  .rank-num{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;
    justify-content:center;font-size:.74em;font-weight:800;flex-shrink:0;margin-top:2px}
  .n1{background:#fef3c7;color:#92400e;border:2px solid #f59e0b}
  .n2{background:#f1f5f9;color:#475569;border:2px solid #94a3b8}
  .n3{background:#fdf4e7;color:#7c3a00;border:2px solid #d97706}
  .n4,.n5,.n6,.n7,.n8,.n9,.n10{background:#f9fafb;color:#9ca3af;border:2px solid #e5e7eb}

  .job-info{flex:1;min-width:0}
  .tags{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px;align-items:center}
  .job-title{font-size:.82em;line-height:1.4;color:var(--text)}
  .job-company{font-size:.72em;color:var(--text-muted);margin-top:2px}
  .salary-val{font-size:.88em;font-weight:700;color:var(--primary);white-space:nowrap;
    text-align:right;flex-shrink:0;margin-top:3px}

  /* タグ類 */
  .site-tag{font-size:.64em;font-weight:700;padding:2px 6px;border-radius:4px;
    white-space:nowrap;flex-shrink:0}
  .tag-Indeed       {background:#1a56db;color:#fff}
  .tag-エンゲージ   {background:#e8620f;color:#fff}
  .tag-マイナビ     {background:#e60020;color:#fff}
  .tag-マイナビ転職 {background:#cc0033;color:#fff}
  .tag-エン転職     {background:#059669;color:#fff}
  .tag-バイトル     {background:#0891b2;color:#fff}

  .emp-tag{font-size:.63em;font-weight:700;padding:2px 5px;border-radius:3px;white-space:nowrap}
  .emp-haken  {background:#dbeafe;color:#1e40af}
  .emp-baito  {background:#fef9c3;color:#854d0e}
  .emp-part   {background:#dcfce7;color:#166534}
  .emp-seisya {background:#f3e8ff;color:#6b21a8}
  .emp-other  {background:#f3f4f6;color:#374151}

  .sched-tag{font-size:.63em;padding:2px 5px;border-radius:3px;white-space:nowrap;
    background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}

  .rank-empty{padding:24px 16px;text-align:center;color:var(--text-muted);font-size:.84em}

  .note{font-size:.75em;color:var(--text-muted);margin-top:14px;padding:9px 12px;
    background:var(--surface);border-left:3px solid var(--border-mid);border-radius:0 6px 6px 0}

  .spinner{display:inline-block;width:11px;height:11px;border:2px solid var(--border-mid);
    border-top-color:var(--primary);border-radius:50%;animation:spin .65s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .done-mark{display:inline-flex;align-items:center;justify-content:center;
    width:15px;height:15px;border-radius:50%;background:var(--success);color:#fff;
    font-size:9px;font-weight:800;flex-shrink:0}
</style>
</head>
<body>
<header>
  <div class="hdr-icon">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
         stroke="rgba(255,255,255,.9)" stroke-width="2.2"
         stroke-linecap="round" stroke-linejoin="round">
      <rect x="18" y="3" width="4" height="18"/>
      <rect x="10" y="8" width="4" height="13"/>
      <rect x="2" y="13" width="4" height="8"/>
    </svg>
  </div>
  <span class="hdr-title">求人給与ランキング</span>
  <span class="hdr-sub">Indeed / エンゲージ / マイナビ / マイナビ転職 / エン転職 / バイトル</span>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-heading">職種を選択</div>
    <div id="jobButtons"></div>
  </aside>
  <main class="main" id="main">
    <div class="empty">
      <div class="empty-icon">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none"
             stroke="#9ca3af" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
      </div>
      <h3>職種を選んでください</h3>
      <p>左のリストから職種を選ぶと<br>リアルタイムでランキングを集計します</p>
    </div>
  </main>
</div>

<script>
const JOBS=[["営","営業職"],["事","事務職"],["エ","エンジニア"],
  ["販","販売・接客"],["製","製造・工場"],["介","介護・福祉"],
  ["ド","ドライバー"],["軽","軽作業"],["医","医療・看護"],["デ","デザイナー"]];
const SITES=["マイナビ転職","エン転職","バイトル","Indeed","マイナビ","エンゲージ"];
const RC=["n1","n2","n3","n4","n5","n6","n7","n8","n9","n10"];
const EMP_CLS={
  "派遣":"emp-haken","アルバイト":"emp-baito","パート":"emp-part",
  "正社員":"emp-seisya"
};
let es=null;

const ba=document.getElementById("jobButtons");
JOBS.forEach(([a,n])=>{
  const b=document.createElement("button");
  b.className="job-btn";b.dataset.kw=n;
  b.innerHTML=`<span class="job-abbr">${a}</span>${n}`;
  b.onclick=()=>go(n);ba.appendChild(b);
});

function setActive(kw){
  document.querySelectorAll(".job-btn").forEach(b=>b.classList.toggle("active",b.dataset.kw===kw));
}

function go(kw){
  if(es){es.close();es=null;}
  setActive(kw);
  document.getElementById("main").innerHTML=loadUI(kw);
  es=new EventSource("/search?keyword="+encodeURIComponent(kw));
  es.onmessage=ev=>{
    const d=JSON.parse(ev.data);
    if(d.type==="progress") onProg(d);
    else if(d.type==="result") onResult(d,kw);
    else if(d.type==="end"){es.close();es=null;}
  };
  es.onerror=()=>{es.close();es=null;};
}

function loadUI(kw){
  return`<div class="progress-card">
    <div class="progress-head"><span class="spinner"></span>&nbsp;「${x(kw)}」を検索中...</div>
    <div class="site-rows">${SITES.map(s=>`
      <div class="site-row" id="r-${s}">
        <div class="s-dot"></div>
        <div class="s-name">${s}</div>
        <div class="s-status">待機中</div>
      </div>`).join("")}
    </div>
  </div>
  <div id="ra"></div>`;
}

function onProg(d){
  const r=document.getElementById("r-"+d.site);
  if(!r) return;
  const dot=r.querySelector(".s-dot");
  const st=r.querySelector(".s-status");
  const existBadge=r.querySelector(".s-badge");
  if(d.status==="searching"){
    r.className="site-row searching";
    st.textContent="検索中...";
  } else if(d.status==="login_wait"){
    r.className="site-row login-wait";
    st.innerHTML="<b>ブラウザが開きました。Indeedにログインしてください</b>";
  } else if(d.status==="countdown"){
    r.className="site-row login-wait";
    st.innerHTML=`<b>ログイン待機中... あと${d.seconds}秒</b>`;
  } else if(d.status==="scraping"){
    r.className="site-row scraping";
    st.textContent="スクレイピング中...";
  } else if(d.status==="done"){
    if(existBadge) existBadge.remove();
    r.className="site-row done";
    st.textContent=d.count+"件取得";
    r.insertAdjacentHTML("beforeend",
      `<div class="s-badge">${d.salary_count}件 給与あり</div>`);
  } else if(d.status==="error"){
    r.className="site-row error";
    st.textContent="取得できませんでした";
  }
}

function onResult(d,kw){
  const ph=document.querySelector(".progress-head");
  if(ph) ph.innerHTML='<span class="done-mark">&#10003;</span>&nbsp;収集完了';
  document.getElementById("ra").innerHTML=`
    <div class="meta-bar">
      <span class="kw-pill">${x(kw)}</span>
      <span class="meta-ts">${d.timestamp}</span>
      <span class="meta-total">合計 ${d.total} 件収集</span>
    </div>
    <div class="rankings">
      ${card("時給ランキング TOP10","jikyu",d.jikyu_ranking)}
      ${card("給与ランキング TOP10（時給換算）","salary",d.salary_ranking)}
    </div>
    <div class="note">
      ※ 月給・年収は月160時間勤務換算で時給に変換して比較しています。<br>
      ※ 給与範囲（例:時給1900〜2500円）は上限値を使用しています。<br>
      ※ Indeedはブラウザを開いて検索します。初回はログインが必要です（以降は自動）。
    </div>`;
}

function empTag(emp){
  if(!emp) return '';
  const cls=EMP_CLS[emp]||'emp-other';
  return `<span class="emp-tag ${cls}">${emp}</span>`;
}

function schedTag(sched){
  return sched ? `<span class="sched-tag">${sched}</span>` : '';
}

function card(title,type,jobs){
  const badge=type==="jikyu"?"時":"給";
  const rows=jobs.length
    ? jobs.map((j,i)=>`
      <div class="rank-item">
        <div class="rank-num ${RC[i]}">${i+1}</div>
        <div class="job-info">
          <div class="tags">
            <span class="site-tag tag-${j.site}">${j.site}</span>
            ${empTag(j.emp)}
            ${schedTag(j.sched)}
          </div>
          <div class="job-title">${x(j.title)}</div>
          ${j.company?`<div class="job-company">${x(j.company)}</div>`:''}
        </div>
        <div class="salary-val">${x(j.salary)}</div>
      </div>`).join('')
    : `<div class="rank-empty">給与情報を含む求人が取得できませんでした</div>`;
  return`<div class="rank-card">
    <div class="rank-head t-${type}"><div class="head-badge">${badge}</div>${x(title)}</div>
    ${rows}
  </div>`;
}

function x(s){return(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    print("\n" + "=" * 56)
    print("  求人給与ランキング（ローカル専用・高精度版）起動中")
    print("=" * 56)
    print(f"  ブラウザで開く → http://localhost:{port}")
    print()
    print("  ★ Indeed について:")
    print("    初回: ブラウザが自動で開くのでログインしてください")
    print("    2回目以降: ログイン状態が保存されます")
    print("=" * 56 + "\n")

    import subprocess, time
    def open_browser():
        time.sleep(1.5)
        subprocess.run(["open", f"http://localhost:{port}"])
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
