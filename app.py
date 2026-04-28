"""
求人給与ランキング Webアプリ
起動: python3 app.py
ブラウザ: http://localhost:8080
"""

import asyncio
import json
import queue
import re
import threading
from datetime import datetime
from flask import Flask, Response, render_template_string, request, stream_with_context
from playwright.async_api import async_playwright

app = Flask(__name__)

SHOKUSHU_LIST = [
    "営業職", "事務職", "エンジニア", "販売・接客",
    "製造・工場", "介護・福祉", "ドライバー", "軽作業",
    "医療・看護", "デザイナー",
]


# ─────────────────────────────────────────
# 給与パース
# ─────────────────────────────────────────

def parse_salary(text: str) -> dict:
    result = {"raw": text, "jikyu": None, "monthly": None, "annual": None, "jikyu_normalized": None}
    if not text:
        return result
    t = text.replace(",", "").replace("，", "").replace(" ", "").replace("　", "")

    m = re.search(r"時給[^\d]*(\d+)", t)
    if m:
        result["jikyu"] = int(m.group(1))
        result["jikyu_normalized"] = int(m.group(1))
        return result

    m = re.search(r"月[給収][^\d]*(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(1))
        if m.group(2) == "万":
            val *= 10000
        result["monthly"] = int(val)
        result["jikyu_normalized"] = int(val / 160)
        return result

    m = re.search(r"年[収給][^\d]*(\d+(?:\.\d+)?)(万)?", t)
    if m:
        val = float(m.group(1))
        if m.group(2) == "万":
            val *= 10000
        result["annual"] = int(val)
        result["jikyu_normalized"] = int(val / (12 * 160))
        return result

    m = re.search(r"(\d+(?:\.\d+)?)万円?", t)
    if m:
        val = float(m.group(1)) * 10000
        result["monthly"] = int(val)
        result["jikyu_normalized"] = int(val / 160)

    return result


def salary_display(parsed: dict) -> str:
    if parsed.get("jikyu"):
        return f"時給 {parsed['jikyu']:,}円"
    if parsed.get("monthly"):
        return f"月給 {parsed['monthly']:,}円"
    if parsed.get("annual"):
        return f"年収 {parsed['annual']:,}円"
    return parsed.get("raw") or "—"


# ─────────────────────────────────────────
# 共通：ボディテキストからの汎用フォールバック
# ─────────────────────────────────────────

NOISE_PATTERN = re.compile(
    r"^(検索|絞り込み|ログイン|新規登録|会員|エリア|こだわり|条件|特集|新着|おすすめ|"
    r"アルバイト|バイト|正社員|派遣|パート|契約社員|求人|仕事|サイト|ページ|一覧|"
    r"トップ|ホーム|ナビ|メニュー|クリック|詳細|閲覧|確認|\d+件|\d+ページ)$"
)

async def generic_body_fallback(page, site_name: str) -> list:
    jobs = []
    try:
        body = await page.inner_text("body")
        lines = [l.strip() for l in body.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if not re.search(r"(時給|月給|年収|給与)[^\d]*\d+", line):
                continue
            title, company = "", ""
            for j in range(i - 1, max(0, i - 12), -1):
                ln = lines[j]
                if len(ln) < 5 or NOISE_PATTERN.match(ln):
                    continue
                if re.search(r"\d+万|\d+円|〜|以上|以下|応相談", ln):
                    continue
                if not title:
                    title = ln[:80]
                elif not company:
                    company = ln[:50]
                    break
            if title:
                jobs.append({
                    "site": site_name, "title": title,
                    "company": company, "location": "", "salary_text": line,
                })
            if len(jobs) >= 20:
                break
    except Exception:
        pass
    return jobs


# ─────────────────────────────────────────
# スクレイパー（各サイト）
# ─────────────────────────────────────────

async def scrape_indeed(page, keyword):
    jobs = []
    try:
        await page.goto(
            f"https://jp.indeed.com/jobs?q={keyword}&sort=date&limit=20",
            timeout=30000, wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(5000)
        title_text = await page.title()
        body = await page.inner_text("body")
        if "Security Check" in title_text or "追加認証" in body or "Captcha" in body:
            await asyncio.sleep(12)
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)

        for card in (await page.query_selector_all("div.job_seen_beacon, div[data-jk]"))[:20]:
            try:
                te = await card.query_selector("h2.jobTitle span, h2 a span")
                ce = await card.query_selector("[data-testid='company-name'], .companyName")
                se = await card.query_selector(
                    "[data-testid='attribute_snippet_testid'], .salary-snippet-container,"
                    "[class*='salary'],[class*='Salary']")
                le = await card.query_selector("[data-testid='text-location'], .companyLocation")
                t = (await te.inner_text()).strip() if te else ""
                if t:
                    jobs.append({
                        "site": "Indeed", "title": t,
                        "company": (await ce.inner_text()).strip() if ce else "",
                        "location": (await le.inner_text()).strip() if le else "",
                        "salary_text": (await se.inner_text()).strip() if se else "",
                    })
            except Exception:
                continue

        if not jobs:
            jobs = await generic_body_fallback(page, "Indeed")
    except Exception:
        pass
    return jobs


async def scrape_engage(page, keyword):
    jobs = []
    try:
        await page.goto(
            f"https://en-gage.net/user/search/?searchWord={keyword}",
            timeout=30000, wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(5000)

        cards = await page.query_selector_all(
            "article.job-card, li.job-card, div[class*='jobCard'], "
            "div[class*='job-item'], article[class*='job'], [class*='JobCard']",
        )
        if cards:
            for card in cards[:20]:
                try:
                    te = await card.query_selector("h2, h3, [class*='title'], [class*='Title']")
                    ce = await card.query_selector("[class*='company'], [class*='Company']")
                    se = await card.query_selector(
                        "[class*='salary'], [class*='Salary'], [class*='wage'], [class*='kyuyo']")
                    t = (await te.inner_text()).strip() if te else ""
                    if t:
                        jobs.append({
                            "site": "エンゲージ", "title": t,
                            "company": (await ce.inner_text()).strip() if ce else "",
                            "location": "",
                            "salary_text": (await se.inner_text()).strip() if se else "",
                        })
                except Exception:
                    continue

        if not jobs:
            jobs = await generic_body_fallback(page, "エンゲージ")
    except Exception:
        pass
    return jobs


async def scrape_mynavi_baito(page, keyword):
    jobs = []
    try:
        word = keyword.replace("職", "").replace("・", "")
        await page.goto(
            f"https://baito.mynavi.jp/ai/word_{word}/",
            timeout=30000, wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(3000)
        for card in await page.query_selector_all(".tabJobOfferCard"):
            try:
                lines = [l.strip() for l in (await card.inner_text()).split("\n") if l.strip()]
                if len(lines) < 2:
                    continue
                company = re.sub(r"/\d+$", "", lines[0]).strip()
                title = lines[1]
                salary_text = ""
                for i, line in enumerate(lines):
                    if line == "給与" and i + 1 < len(lines):
                        salary_text = lines[i + 1]
                        break
                if title:
                    jobs.append({
                        "site": "マイナビ", "title": title,
                        "company": company, "location": "", "salary_text": salary_text,
                    })
            except Exception:
                continue

        if not jobs:
            jobs = await generic_body_fallback(page, "マイナビ")
    except Exception:
        pass
    return jobs


async def scrape_mynavi_tenshoku(page, keyword):
    jobs = []
    try:
        await page.goto(
            f"https://tenshoku.mynavi.jp/list/?keyword={keyword}",
            timeout=30000, wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(6000)

        # Try to wait for job cards
        try:
            await page.wait_for_selector(".cassetteRecruit, [class*='cassette'], [class*='recruit']", timeout=5000)
        except Exception:
            pass

        cards = await page.query_selector_all(
            ".cassetteRecruit, [class*='cassetteRecruit'], [class*='jobItem']"
        )

        for card in cards[:30]:
            try:
                full = (await card.inner_text()).strip()
                company, title = "", ""
                he = await card.query_selector(
                    ".cassetteRecruit__heading, [class*='heading'], [class*='Heading'], h2, h3")
                if he:
                    hl = [l.strip() for l in (await he.inner_text()).split("\n") if l.strip()]
                    if hl:
                        company = hl[0].split("|")[0].strip() if "|" in hl[0] else hl[0]
                    if len(hl) > 1:
                        title = hl[1]
                if not title:
                    lines = [l.strip() for l in full.split("\n") if l.strip()]
                    title = lines[1] if len(lines) > 1 else (lines[0] if lines else "")

                salary_text = ""
                for pattern in [
                    r"給与\s*\t([^\n]+)",
                    r"給与\s*\n\s*([^\n]+)",
                    r"月給[^\d]*(\d[\d,万円〜\-～以上]+)",
                    r"年収[^\d]*(\d[\d,万円〜\-～以上]+)",
                ]:
                    m = re.search(pattern, full)
                    if m:
                        salary_text = m.group(1).strip()
                        break

                if title:
                    jobs.append({
                        "site": "マイナビ転職", "title": title,
                        "company": company, "location": "", "salary_text": salary_text,
                    })
            except Exception:
                continue

        if not jobs:
            jobs = await generic_body_fallback(page, "マイナビ転職")
    except Exception:
        pass
    return jobs


async def scrape_en_tenshoku(page, keyword):
    jobs = []
    try:
        await page.goto(
            f"https://employment.en-japan.com/keyword/{keyword}/",
            timeout=30000, wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(5000)

        try:
            await page.wait_for_selector(".jobSearchListUnit, [class*='jobSearch'], [class*='job-list']", timeout=5000)
        except Exception:
            pass

        cards = await page.query_selector_all(
            ".jobSearchListUnit, [class*='jobSearchList'], [class*='job-unit']"
        )

        for card in cards[:30]:
            try:
                te = await card.query_selector(".jobNameText, [class*='jobName'], h2 a, h3 a")
                ce = await card.query_selector(".company, [class*='company'], [class*='Company']")
                title = (await te.inner_text()).strip() if te else ""
                company = (await ce.inner_text()).strip() if ce else ""
                card_text = (await card.inner_text()).strip()
                salary_text = ""
                for pattern in [
                    r"給与\s*\n\s*([^\n]+)",
                    r"給与\s*([^\n]+)",
                    r"月給[^\d]*(\d[\d,万円〜\-～以上]+)",
                    r"年収[^\d]*(\d[\d,万円〜\-～以上]+)",
                ]:
                    m = re.search(pattern, card_text)
                    if m:
                        salary_text = m.group(1).strip()
                        break
                if title:
                    jobs.append({
                        "site": "エン転職", "title": title,
                        "company": company, "location": "", "salary_text": salary_text,
                    })
            except Exception:
                continue

        if not jobs:
            jobs = await generic_body_fallback(page, "エン転職")
    except Exception:
        pass
    return jobs


# ─────────────────────────────────────────
# メインスクレイピング
# ─────────────────────────────────────────

async def run_scraper(keyword: str, q: queue.Queue):
    scrapers = [
        ("Indeed",      scrape_indeed),
        ("エンゲージ",   scrape_engage),
        ("マイナビ",     scrape_mynavi_baito),
        ("マイナビ転職", scrape_mynavi_tenshoku),
        ("エン転職",     scrape_en_tenshoku),
    ]
    all_jobs = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--window-size=1280,800",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        # Remove webdriver fingerprint
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP','ja','en-US','en']});
            window.chrome = {runtime:{}};
        """)

        for site_name, fn in scrapers:
            q.put({"type": "progress", "site": site_name, "status": "searching"})
            page = await context.new_page()
            try:
                jobs = await fn(page, keyword)
                all_jobs.extend(jobs)
                salary_count = sum(1 for j in jobs if j["salary_text"])
                q.put({"type": "progress", "site": site_name,
                       "status": "done", "count": len(jobs), "salary_count": salary_count})
            except Exception as e:
                q.put({"type": "progress", "site": site_name, "status": "error", "msg": str(e)})
            finally:
                await page.close()
            await asyncio.sleep(2)

        await browser.close()

    # ランキング計算
    parsed_jobs = []
    for job in all_jobs:
        parsed = parse_salary(job["salary_text"])
        if parsed["jikyu_normalized"]:
            parsed_jobs.append({**job, **parsed})

    jikyu_ranking = sorted(
        [j for j in parsed_jobs if j.get("jikyu")],
        key=lambda x: x["jikyu"], reverse=True,
    )[:5]

    salary_ranking = sorted(
        parsed_jobs, key=lambda x: x["jikyu_normalized"], reverse=True,
    )[:5]

    def fmt(jobs):
        return [{"site": j["site"], "title": j["title"], "company": j["company"],
                 "salary": salary_display(j), "raw": j["salary_text"]} for j in jobs]

    q.put({
        "type": "result",
        "total": len(all_jobs),
        "timestamp": datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        "jikyu_ranking": fmt(jikyu_ranking),
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
# HTML テンプレート（ライトビジネス）
# ─────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>求人給与ランキング</title>
<style>
  :root {
    --bg: #f0f4f8;
    --surface: #ffffff;
    --header: #1a3654;
    --primary: #1d4ed8;
    --primary-light: #eff6ff;
    --primary-border: #bfdbfe;
    --success: #15803d;
    --success-light: #f0fdf4;
    --success-border: #86efac;
    --warning: #b45309;
    --warning-light: #fffbeb;
    --warning-border: #fcd34d;
    --error: #dc2626;
    --error-light: #fef2f2;
    --error-border: #fca5a5;
    --text: #111827;
    --text-sub: #374151;
    --text-muted: #6b7280;
    --border: #e5e7eb;
    --border-mid: #d1d5db;
    --shadow-sm: 0 1px 2px rgba(0,0,0,.05);
    --shadow: 0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.06);
  }

  *{box-sizing:border-box;margin:0;padding:0}

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', 'Yu Gothic UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    font-size: 14px;
    line-height: 1.5;
  }

  /* ── ヘッダー ── */
  header {
    background: var(--header);
    height: 56px;
    display: flex;
    align-items: center;
    padding: 0 28px;
    gap: 14px;
    box-shadow: 0 1px 4px rgba(0,0,0,.25);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .hdr-icon {
    width: 34px; height: 34px;
    background: rgba(255,255,255,.12);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .hdr-icon svg { display: block; }
  .hdr-title {
    font-size: 1.05em;
    font-weight: 700;
    color: #fff;
    letter-spacing: .02em;
  }
  .hdr-sub {
    font-size: .75em;
    color: rgba(255,255,255,.5);
    margin-left: 2px;
  }

  /* ── レイアウト ── */
  .layout {
    display: grid;
    grid-template-columns: 210px 1fr;
    min-height: calc(100vh - 56px);
  }

  /* ── サイドバー ── */
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 18px 10px;
    overflow-y: auto;
  }
  .sidebar-heading {
    font-size: .68em;
    font-weight: 700;
    color: var(--text-muted);
    letter-spacing: .1em;
    text-transform: uppercase;
    padding: 0 8px;
    margin-bottom: 8px;
  }
  .job-btn {
    display: flex;
    align-items: center;
    gap: 9px;
    width: 100%;
    padding: 8px 10px;
    margin-bottom: 2px;
    background: transparent;
    border: none;
    border-radius: 6px;
    color: var(--text-sub);
    font-size: .88em;
    text-align: left;
    cursor: pointer;
    transition: background .12s;
  }
  .job-btn:hover { background: var(--bg); }
  .job-btn.active {
    background: var(--primary-light);
    color: var(--primary);
    font-weight: 600;
  }
  .job-abbr {
    width: 26px; height: 26px;
    border-radius: 5px;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text-muted);
    font-size: .78em;
    font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    transition: background .12s, color .12s, border-color .12s;
  }
  .job-btn.active .job-abbr {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }

  /* ── メイン ── */
  .main { padding: 28px 30px; overflow-x: hidden; }

  /* ── 初期状態 ── */
  .empty {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 60vh;
    color: var(--text-muted);
    text-align: center;
    gap: 10px;
  }
  .empty-icon {
    width: 60px; height: 60px;
    background: var(--border);
    border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 4px;
  }
  .empty h3 { font-size: .95em; font-weight: 600; color: var(--text-sub); }
  .empty p  { font-size: .83em; line-height: 1.7; }

  /* ── プログレス ── */
  .progress-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 18px;
    box-shadow: var(--shadow-sm);
  }
  .progress-head {
    display: flex; align-items: center; gap: 8px;
    font-size: .78em; font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: .08em;
    margin-bottom: 14px;
  }
  .site-rows { display: flex; flex-direction: column; gap: 5px; }
  .site-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px;
    border-radius: 6px;
    background: var(--bg);
    border: 1px solid var(--border);
    transition: all .22s;
  }
  .site-row.searching { background: var(--warning-light); border-color: var(--warning-border); }
  .site-row.done      { background: var(--success-light); border-color: var(--success-border); }
  .site-row.error     { background: var(--error-light);   border-color: var(--error-border); }

  .s-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--border); flex-shrink: 0;
  }
  .site-row.searching .s-dot { background: var(--warning); animation: blink .85s infinite; }
  .site-row.done      .s-dot { background: var(--success); }
  .site-row.error     .s-dot { background: var(--error); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.2} }

  .s-name   { font-size: .86em; font-weight: 600; min-width: 88px; color: var(--text-sub); }
  .s-status { font-size: .79em; color: var(--text-muted); }
  .s-badge  {
    margin-left: auto; font-size: .73em; color: var(--text-muted);
    background: rgba(0,0,0,.05); padding: 2px 8px; border-radius: 10px;
  }

  /* ── メタバー ── */
  .meta-bar {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 16px; flex-wrap: wrap;
  }
  .kw-pill {
    font-size: .83em; font-weight: 700;
    background: var(--primary); color: #fff;
    padding: 3px 13px; border-radius: 20px;
  }
  .meta-ts    { font-size: .78em; color: var(--text-muted); }
  .meta-total { font-size: .78em; color: var(--success); font-weight: 600; margin-left: auto; }

  /* ── ランキングカード ── */
  .rankings {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
  }
  @media(max-width:920px){ .rankings{ grid-template-columns:1fr } }

  .rank-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .rank-head {
    display: flex; align-items: center; gap: 8px;
    padding: 12px 16px;
    font-size: .83em; font-weight: 700;
    border-bottom: 1px solid var(--border);
  }
  .rank-head.t-jikyu  { background: var(--warning-light); color: var(--warning); border-bottom-color: var(--warning-border); }
  .rank-head.t-salary { background: var(--success-light); color: var(--success); border-bottom-color: var(--success-border); }
  .head-badge {
    width: 20px; height: 20px; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: .72em; font-weight: 800; flex-shrink: 0;
  }
  .t-jikyu  .head-badge { background: var(--warning-border); color: var(--warning); }
  .t-salary .head-badge { background: var(--success-border); color: var(--success); }

  .rank-item {
    display: grid;
    grid-template-columns: 38px 82px 1fr auto;
    align-items: center;
    padding: 10px 14px; gap: 8px;
    border-bottom: 1px solid var(--border);
    transition: background .1s;
  }
  .rank-item:last-child { border-bottom: none; }
  .rank-item:hover { background: var(--bg); }

  .rank-num {
    width: 26px; height: 26px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: .75em; font-weight: 800; margin: 0 auto;
    flex-shrink: 0;
  }
  .n1 { background: #fef3c7; color: #92400e; border: 2px solid #f59e0b; }
  .n2 { background: #f1f5f9; color: #475569; border: 2px solid #94a3b8; }
  .n3 { background: #fdf4e7; color: #7c3a00; border: 2px solid #d97706; }
  .n4,.n5 { background: #f9fafb; color: #9ca3af; border: 2px solid #e5e7eb; }

  .site-tag {
    font-size: .67em; font-weight: 700;
    padding: 2px 6px; border-radius: 4px;
    text-align: center; white-space: nowrap;
  }
  .tag-Indeed       { background: #1a56db; color: #fff; }
  .tag-エンゲージ   { background: #e8620f; color: #fff; }
  .tag-マイナビ     { background: #e60020; color: #fff; }
  .tag-マイナビ転職 { background: #cc0033; color: #fff; }
  .tag-エン転職     { background: #059669; color: #fff; }

  .job-title   { font-size: .83em; line-height: 1.4; color: var(--text); }
  .job-company { font-size: .73em; color: var(--text-muted); margin-top: 2px; }
  .salary-val  { font-size: .9em; font-weight: 700; color: var(--primary); white-space: nowrap; text-align: right; }

  .rank-empty { padding: 28px 16px; text-align: center; color: var(--text-muted); font-size: .84em; }

  /* ── ノート ── */
  .note {
    font-size: .75em; color: var(--text-muted);
    margin-top: 16px; padding: 9px 12px;
    background: var(--surface);
    border-left: 3px solid var(--border-mid);
    border-radius: 0 6px 6px 0;
  }

  /* ── スピナー ── */
  .spinner {
    display: inline-block; width: 11px; height: 11px;
    border: 2px solid var(--border-mid);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin .65s linear infinite;
  }
  @keyframes spin{ to{ transform:rotate(360deg) } }
  .done-mark {
    display: inline-flex; align-items: center; justify-content: center;
    width: 15px; height: 15px; border-radius: 50%;
    background: var(--success); color: #fff;
    font-size: 9px; font-weight: 800; flex-shrink: 0;
  }
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
      <rect x="2"  y="13" width="4" height="8"/>
    </svg>
  </div>
  <span class="hdr-title">求人給与ランキング</span>
  <span class="hdr-sub">Indeed / エンゲージ / マイナビ / マイナビ転職 / エン転職</span>
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
const JOBS = [
  ["営","営業職"],["事","事務職"],["エ","エンジニア"],
  ["販","販売・接客"],["製","製造・工場"],["介","介護・福祉"],
  ["ド","ドライバー"],["軽","軽作業"],["医","医療・看護"],["デ","デザイナー"]
];
const SITES = ["Indeed","エンゲージ","マイナビ","マイナビ転職","エン転職"];
const RANK_CLS = ["n1","n2","n3","n4","n5"];

let es = null;

const btnArea = document.getElementById("jobButtons");
JOBS.forEach(([abbr, name]) => {
  const b = document.createElement("button");
  b.className = "job-btn";
  b.dataset.kw = name;
  b.innerHTML = `<span class="job-abbr">${abbr}</span>${name}`;
  b.onclick = () => go(name);
  btnArea.appendChild(b);
});

function setActive(kw) {
  document.querySelectorAll(".job-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.kw === kw));
}

function go(kw) {
  if (es) { es.close(); es = null; }
  setActive(kw);
  document.getElementById("main").innerHTML = loadingUI(kw);
  es = new EventSource("/search?keyword=" + encodeURIComponent(kw));
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.type === "progress") onProg(d);
    else if (d.type === "result") onResult(d, kw);
    else if (d.type === "end") { es.close(); es = null; }
  };
  es.onerror = () => {
    const r = document.querySelector(".site-row.searching");
    if (r) r.className = "site-row error";
    es.close(); es = null;
  };
}

function loadingUI(kw) {
  return `
  <div class="progress-card">
    <div class="progress-head"><span class="spinner"></span>&nbsp;「${x(kw)}」を検索中</div>
    <div class="site-rows">${SITES.map(s => `
      <div class="site-row" id="r-${s}">
        <div class="s-dot"></div>
        <div class="s-name">${s}</div>
        <div class="s-status">待機中</div>
      </div>`).join("")}
    </div>
  </div>
  <div id="ra"></div>`;
}

function onProg(d) {
  const r = document.getElementById("r-" + d.site);
  if (!r) return;
  if (d.status === "searching") {
    r.className = "site-row searching";
    r.querySelector(".s-status").textContent = "検索中...";
  } else if (d.status === "done") {
    r.className = "site-row done";
    r.querySelector(".s-status").textContent = d.count + "件取得";
    r.insertAdjacentHTML("beforeend",
      `<div class="s-badge">${d.salary_count}件 給与あり</div>`);
  } else {
    r.className = "site-row error";
    r.querySelector(".s-status").textContent = "取得できませんでした";
  }
}

function onResult(d, kw) {
  const ph = document.querySelector(".progress-head");
  if (ph) ph.innerHTML = '<span class="done-mark">&#10003;</span>&nbsp;収集完了';
  document.getElementById("ra").innerHTML = `
    <div class="meta-bar">
      <span class="kw-pill">${x(kw)}</span>
      <span class="meta-ts">${d.timestamp}</span>
      <span class="meta-total">合計 ${d.total} 件収集</span>
    </div>
    <div class="rankings">
      ${card("時給ランキング TOP5","jikyu",d.jikyu_ranking)}
      ${card("給与ランキング TOP5（時給換算）","salary",d.salary_ranking)}
    </div>
    <div class="note">
      ※ 月給・年収は月160時間勤務換算で時給に変換して比較しています。<br>
      ※ Indeed はクラウドサーバーからのアクセスでブロックされる場合があります。
    </div>`;
}

function card(title, type, jobs) {
  const badge = type === "jikyu" ? "時" : "給";
  const rows = jobs.length
    ? jobs.map((j,i) => `
      <div class="rank-item">
        <div class="rank-num ${RANK_CLS[i]}">${i+1}</div>
        <div><span class="site-tag tag-${j.site}">${j.site}</span></div>
        <div>
          <div class="job-title">${x(j.title)}</div>
          ${j.company ? `<div class="job-company">${x(j.company)}</div>` : ""}
        </div>
        <div class="salary-val">${x(j.salary)}</div>
      </div>`).join("")
    : `<div class="rank-empty">給与情報を含む求人が取得できませんでした</div>`;

  return `
  <div class="rank-card">
    <div class="rank-head t-${type}">
      <div class="head-badge">${badge}</div>${x(title)}
    </div>
    <div>${rows}</div>
  </div>`;
}

function x(s) {
  return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    is_local = os.environ.get("RENDER") is None

    print("\n" + "=" * 50)
    print("  求人給与ランキング Webアプリ 起動中...")
    print("=" * 50)
    if is_local:
        print(f"  ブラウザで開く → http://localhost:{port}")
        print("  停止: Ctrl+C")
        import subprocess, time
        def open_browser():
            time.sleep(1.5)
            subprocess.run(["open", f"http://localhost:{port}"])
        threading.Thread(target=open_browser, daemon=True).start()
    else:
        print("  Render上で起動中...")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
