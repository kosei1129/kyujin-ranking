"""
求人給与ランキング Webアプリ
起動: python3 app.py
ブラウザ: http://localhost:5000
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
# スクレイパー（各サイト）
# ─────────────────────────────────────────

async def scrape_indeed(page, keyword):
    jobs = []
    try:
        await page.goto(f"https://jp.indeed.com/jobs?q={keyword}&sort=date&limit=20",
                        timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        body = await page.inner_text("body")
        if "Security Check" in await page.title() or "追加認証" in body:
            await asyncio.sleep(10)
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
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
                    jobs.append({"site": "Indeed", "title": t,
                                 "company": (await ce.inner_text()).strip() if ce else "",
                                 "location": (await le.inner_text()).strip() if le else "",
                                 "salary_text": (await se.inner_text()).strip() if se else ""})
            except Exception:
                continue
    except Exception as e:
        pass
    return jobs


async def scrape_engage(page, keyword):
    jobs = []
    try:
        await page.goto(f"https://en-gage.net/user/search/?searchWord={keyword}",
                        timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        cards = await page.query_selector_all(
            "article.job-card,li.job-card,div[class*='jobCard'],div[class*='job-item']")
        if not cards:
            lines = [l.strip() for l in (await page.inner_text("body")).split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if re.search(r"時給|月給|年収", line):
                    jobs.append({"site": "エンゲージ",
                                 "title": (lines[i - 1] if i > 0 else "")[:60],
                                 "company": "", "location": "", "salary_text": line})
                    if len(jobs) >= 20:
                        break
            return jobs
        for card in cards[:20]:
            try:
                te = await card.query_selector("h2,h3,[class*='title']")
                ce = await card.query_selector("[class*='company']")
                se = await card.query_selector("[class*='salary'],[class*='wage'],[class*='kyuyo']")
                t = (await te.inner_text()).strip() if te else ""
                if t:
                    jobs.append({"site": "エンゲージ", "title": t,
                                 "company": (await ce.inner_text()).strip() if ce else "",
                                 "location": "",
                                 "salary_text": (await se.inner_text()).strip() if se else ""})
            except Exception:
                continue
    except Exception:
        pass
    return jobs


async def scrape_mynavi_baito(page, keyword):
    jobs = []
    try:
        word = keyword.replace("職", "").replace("・", "")
        await page.goto(f"https://baito.mynavi.jp/ai/word_{word}/",
                        timeout=30000, wait_until="domcontentloaded")
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
                    jobs.append({"site": "マイナビ", "title": title,
                                 "company": company, "location": "", "salary_text": salary_text})
            except Exception:
                continue
    except Exception:
        pass
    return jobs


async def scrape_mynavi_tenshoku(page, keyword):
    jobs = []
    try:
        await page.goto(f"https://tenshoku.mynavi.jp/list/?keyword={keyword}",
                        timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        for card in (await page.query_selector_all(".cassetteRecruit"))[:30]:
            try:
                full = (await card.inner_text()).strip()
                company, title = "", ""
                he = await card.query_selector(".cassetteRecruit__heading")
                if he:
                    hl = [l.strip() for l in (await he.inner_text()).split("\n") if l.strip()]
                    if hl:
                        company = hl[0].split("|")[0].strip() if "|" in hl[0] else hl[0]
                    if len(hl) > 1:
                        title = hl[1]
                salary_text = ""
                m = re.search(r"給与\s*\t([^\n]+)", full)
                if not m:
                    m = re.search(r"給与\n([^\n]+)", full)
                if m:
                    salary_text = m.group(1).strip()
                if title:
                    jobs.append({"site": "マイナビ転職", "title": title,
                                 "company": company, "location": "", "salary_text": salary_text})
            except Exception:
                continue
    except Exception:
        pass
    return jobs


async def scrape_en_tenshoku(page, keyword):
    jobs = []
    try:
        await page.goto(f"https://employment.en-japan.com/keyword/{keyword}/",
                        timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        for card in (await page.query_selector_all(".jobSearchListUnit"))[:30]:
            try:
                te = await card.query_selector(".jobNameText")
                ce = await card.query_selector(".company")
                title = (await te.inner_text()).strip() if te else ""
                company = (await ce.inner_text()).strip() if ce else ""
                card_text = (await card.inner_text()).strip()
                salary_text = ""
                m = re.search(r"給与\s*\n\s*([^\n]+)", card_text)
                if m:
                    salary_text = m.group(1).strip()
                if title:
                    jobs.append({"site": "エン転職", "title": title,
                                 "company": company, "location": "", "salary_text": salary_text})
            except Exception:
                continue
    except Exception:
        pass
    return jobs


# ─────────────────────────────────────────
# メインスクレイピング（キューにイベントを送る）
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
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )

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
        p = parse_salary(job["salary_text"])
        if p["jikyu_normalized"]:
            parsed_jobs.append({**job, **p})

    jikyu_ranking = sorted(
        [j for j in parsed_jobs if j.get("jikyu")],
        key=lambda x: x["jikyu"], reverse=True
    )[:5]

    salary_ranking = sorted(
        parsed_jobs, key=lambda x: x["jikyu_normalized"], reverse=True
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
    q.put(None)  # 終了シグナル


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
                yield "data: {\"type\": \"end\"}\n\n"
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
    --bg: #0f0f1a;
    --surface: #1a1a2e;
    --surface2: #16213e;
    --accent: #e94560;
    --accent2: #0f3460;
    --text: #e0e0e0;
    --text-dim: #888;
    --border: #2a2a4a;
    --green: #00b894;
    --gold: #fdcb6e;
    --silver: #b2bec3;
    --bronze: #e17055;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  /* ヘッダー */
  header {
    background: linear-gradient(135deg, var(--surface), var(--surface2));
    border-bottom: 1px solid var(--border);
    padding: 24px 32px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  header .logo { font-size: 1.8em; }
  header h1 { font-size: 1.4em; font-weight: 700; letter-spacing: .05em; }
  header p { font-size: .82em; color: var(--text-dim); margin-top: 2px; }

  /* メインレイアウト */
  .layout {
    display: grid;
    grid-template-columns: 260px 1fr;
    min-height: calc(100vh - 81px);
  }

  /* サイドバー（職種選択） */
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    padding: 24px 16px;
  }
  .sidebar h2 {
    font-size: .78em;
    color: var(--text-dim);
    letter-spacing: .12em;
    text-transform: uppercase;
    margin-bottom: 12px;
    padding-left: 8px;
  }
  .job-btn {
    display: block;
    width: 100%;
    padding: 12px 16px;
    margin-bottom: 4px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: var(--text);
    font-size: .95em;
    text-align: left;
    cursor: pointer;
    transition: all .2s;
  }
  .job-btn:hover { background: var(--surface2); border-color: var(--border); }
  .job-btn.active {
    background: var(--accent2);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }
  .job-btn .icon { margin-right: 8px; }

  /* メインコンテンツ */
  .main { padding: 32px; overflow-x: hidden; }

  /* 初期状態 */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 60vh;
    color: var(--text-dim);
    text-align: center;
  }
  .empty-state .big-icon { font-size: 4em; margin-bottom: 16px; }
  .empty-state h3 { font-size: 1.1em; margin-bottom: 8px; }
  .empty-state p { font-size: .88em; }

  /* 検索中プログレス */
  .progress-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
  }
  .progress-section h3 {
    font-size: .85em;
    color: var(--text-dim);
    letter-spacing: .1em;
    text-transform: uppercase;
    margin-bottom: 16px;
  }
  .site-list { display: flex; flex-direction: column; gap: 8px; }
  .site-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    transition: all .3s;
  }
  .site-row.searching { border-color: #f0a500; background: rgba(240,165,0,.06); }
  .site-row.done     { border-color: var(--green); background: rgba(0,184,148,.06); }
  .site-row.error    { border-color: var(--accent); background: rgba(233,69,96,.06); }
  .site-row.blocked  { border-color: #888; background: rgba(136,136,136,.06); }
  .site-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--text-dim); flex-shrink: 0;
    transition: background .3s;
  }
  .site-row.searching .site-dot { background: #f0a500; animation: pulse 1s infinite; }
  .site-row.done     .site-dot { background: var(--green); }
  .site-row.error    .site-dot { background: var(--accent); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .site-name { font-weight: 600; font-size: .9em; min-width: 100px; }
  .site-status { font-size: .82em; color: var(--text-dim); }
  .site-badge-count {
    margin-left: auto;
    font-size: .78em;
    padding: 2px 8px;
    border-radius: 12px;
    background: rgba(255,255,255,.08);
  }

  /* ランキングカード */
  .rankings { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 900px) { .rankings { grid-template-columns: 1fr; } }

  .rank-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }
  .rank-header {
    padding: 14px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 700;
    font-size: .95em;
  }
  .rank-header.jikyu  { background: linear-gradient(135deg, #2d1b00, #3d2800); border-bottom: 2px solid #f0a500; }
  .rank-header.salary { background: linear-gradient(135deg, #001a0d, #002a14); border-bottom: 2px solid var(--green); }

  .rank-table { width: 100%; }
  .rank-row {
    display: grid;
    grid-template-columns: 44px 90px 1fr auto;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    gap: 10px;
    transition: background .15s;
  }
  .rank-row:last-child { border-bottom: none; }
  .rank-row:hover { background: rgba(255,255,255,.03); }
  .medal { font-size: 1.4em; text-align: center; }
  .site-tag {
    font-size: .72em;
    font-weight: 700;
    padding: 3px 7px;
    border-radius: 4px;
    text-align: center;
    white-space: nowrap;
  }
  .tag-Indeed        { background: #003A9B; color: #fff; }
  .tag-エンゲージ    { background: #E8620F; color: #fff; }
  .tag-マイナビ      { background: #E60020; color: #fff; }
  .tag-マイナビ転職  { background: #CC0033; color: #fff; }
  .tag-エン転職      { background: #00984F; color: #fff; }
  .job-title  { font-size: .85em; line-height: 1.4; }
  .job-company { font-size: .75em; color: var(--text-dim); margin-top: 2px; }
  .salary-val {
    font-size: .95em;
    font-weight: 700;
    color: var(--accent);
    white-space: nowrap;
    text-align: right;
  }
  .rank-empty {
    padding: 32px;
    text-align: center;
    color: var(--text-dim);
    font-size: .88em;
  }

  /* メタ情報 */
  .meta-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }
  .meta-bar .keyword-tag {
    font-size: 1em;
    font-weight: 700;
    background: var(--accent2);
    border: 1px solid var(--accent);
    padding: 4px 14px;
    border-radius: 20px;
  }
  .meta-bar .timestamp { font-size: .82em; color: var(--text-dim); }
  .meta-bar .total-count { font-size: .82em; color: var(--green); margin-left: auto; }

  /* ローディングスピナー */
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,.2);
    border-top-color: #f0a500;
    border-radius: 50%;
    animation: spin .7s linear infinite;
    margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .note {
    font-size: .78em;
    color: var(--text-dim);
    margin-top: 16px;
    padding: 10px 14px;
    border-left: 3px solid var(--border);
  }
</style>
</head>
<body>

<header>
  <div class="logo">📊</div>
  <div>
    <h1>求人給与ランキング</h1>
    <p>Indeed / エンゲージ / マイナビ / マイナビ転職 / エン転職 — リアルタイム集計</p>
  </div>
</header>

<div class="layout">

  <!-- サイドバー -->
  <aside class="sidebar">
    <h2>職種を選択</h2>
    <div id="jobButtons"></div>
  </aside>

  <!-- メインコンテンツ -->
  <main class="main" id="main">
    <div class="empty-state" id="emptyState">
      <div class="big-icon">🔍</div>
      <h3>職種を選んでください</h3>
      <p>左のリストから職種を選ぶと<br>自動でランキングを集計します</p>
    </div>
  </main>

</div>

<script>
const JOBS = [
  ["💼", "営業職"],   ["🖥", "事務職"],    ["⚙️", "エンジニア"],
  ["🛍", "販売・接客"],["🏭", "製造・工場"],["🏥", "介護・福祉"],
  ["🚚", "ドライバー"],["📦", "軽作業"],    ["🩺", "医療・看護"],
  ["🎨", "デザイナー"]
];

const MEDALS = ["🥇","🥈","🥉","4位","5位"];
const SITES  = ["Indeed","エンゲージ","マイナビ","マイナビ転職","エン転職"];

let currentES = null;
let currentKeyword = null;

// ── 職種ボタンを生成 ──
const jobBtns = document.getElementById("jobButtons");
JOBS.forEach(([icon, name]) => {
  const btn = document.createElement("button");
  btn.className = "job-btn";
  btn.dataset.keyword = name;
  btn.innerHTML = `<span class="icon">${icon}</span>${name}`;
  btn.onclick = () => startSearch(name);
  jobBtns.appendChild(btn);
});

function setActiveBtn(keyword) {
  document.querySelectorAll(".job-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.keyword === keyword);
  });
}

// ── 検索開始 ──
function startSearch(keyword) {
  if (currentES) { currentES.close(); currentES = null; }
  currentKeyword = keyword;
  setActiveBtn(keyword);

  const main = document.getElementById("main");
  main.innerHTML = buildLoadingUI(keyword);

  currentES = new EventSource(`/search?keyword=${encodeURIComponent(keyword)}`);

  currentES.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === "progress") handleProgress(data);
    else if (data.type === "result") handleResult(data, keyword);
    else if (data.type === "end") { currentES.close(); currentES = null; }
  };

  currentES.onerror = () => {
    currentES.close(); currentES = null;
    const row = document.querySelector(".site-row.searching");
    if (row) { row.className = "site-row error"; row.querySelector(".site-status").textContent = "接続エラー"; }
  };
}

// ── ローディングUI ──
function buildLoadingUI(keyword) {
  const rows = SITES.map(s =>
    `<div class="site-row" id="row-${s}">
      <div class="site-dot"></div>
      <div class="site-name">${s}</div>
      <div class="site-status">待機中</div>
    </div>`
  ).join("");
  return `
    <div class="progress-section">
      <h3><span class="spinner"></span>「${keyword}」を検索中...</h3>
      <div class="site-list">${rows}</div>
    </div>
    <div id="rankingArea"></div>`;
}

// ── プログレス更新 ──
function handleProgress(data) {
  const row = document.getElementById(`row-${data.site}`);
  if (!row) return;
  if (data.status === "searching") {
    row.className = "site-row searching";
    row.querySelector(".site-status").textContent = "検索中...";
  } else if (data.status === "done") {
    row.className = "site-row done";
    row.querySelector(".site-status").textContent = `${data.count}件取得`;
    row.innerHTML += `<div class="site-badge-count">${data.salary_count}件 給与あり</div>`;
  } else if (data.status === "error" || data.status === "blocked") {
    row.className = "site-row error";
    row.querySelector(".site-status").textContent = "スキップ";
  }
}

// ── 結果表示 ──
function handleResult(data, keyword) {
  // プログレスヘッダーを完了に
  const h3 = document.querySelector(".progress-section h3");
  if (h3) h3.innerHTML = `✅ 収集完了`;

  const area = document.getElementById("rankingArea");
  area.innerHTML = `
    <div class="meta-bar">
      <span class="keyword-tag">📌 ${keyword}</span>
      <span class="timestamp">${data.timestamp}</span>
      <span class="total-count">合計 ${data.total} 件収集</span>
    </div>
    <div class="rankings">
      ${buildRankCard("⏱ 時給ランキング TOP5", "jikyu", data.jikyu_ranking)}
      ${buildRankCard("💴 給与ランキング TOP5（時給換算）", "salary", data.salary_ranking)}
    </div>
    <div class="note">
      ※ 月給・年収は月160時間勤務換算で時給に変換して比較しています。<br>
      ※ Indeed は短時間の連続アクセスでブロックされることがあります（通常使用では問題ありません）。
    </div>`;
}

function buildRankCard(title, type, jobs) {
  const rows = jobs.length === 0
    ? `<div class="rank-empty">給与情報を含む求人が取得できませんでした</div>`
    : jobs.map((j, i) => `
        <div class="rank-row">
          <div class="medal">${MEDALS[i]}</div>
          <div><span class="site-tag tag-${j.site}">${j.site}</span></div>
          <div>
            <div class="job-title">${escHtml(j.title)}</div>
            ${j.company ? `<div class="job-company">${escHtml(j.company)}</div>` : ""}
          </div>
          <div class="salary-val">${escHtml(j.salary)}</div>
        </div>`
      ).join("");

  return `
    <div class="rank-card">
      <div class="rank-header ${type}">${title}</div>
      <div class="rank-table">${rows}</div>
    </div>`;
}

function escHtml(s) {
  return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    is_local = os.environ.get("RENDER") is None  # Render上ではRENDER環境変数が設定される

    print("\n" + "=" * 50)
    print("  求人給与ランキング Webアプリ 起動中...")
    print("=" * 50)
    if is_local:
        print(f"  ブラウザで開く → http://localhost:{port}")
        print("  停止: Ctrl+C")
        import subprocess, time, threading
        def open_browser():
            time.sleep(1.5)
            subprocess.run(["open", f"http://localhost:{port}"])
        threading.Thread(target=open_browser, daemon=True).start()
    else:
        print("  Render上で起動中...")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
