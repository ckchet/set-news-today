"""
ตรวจข่าวใหม่จากหน้า "ข่าวหลักทรัพย์" ของตลาดหลักทรัพย์แห่งประเทศไทย (SET)
แล้วส่งแจ้งเตือนเข้า Telegram เมื่อพบข่าวที่ยังไม่เคยแจ้งมาก่อน
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright
import requests

PAGE_URL = "https://www.set.or.th/th/market/news-and-alert/news"
STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# เปิดไว้ถาวรชั่วคราวเพื่อวินิจฉัยปัญหา "ดึงข่าวไม่ได้" — log จะโชว์รายละเอียด endpoint ที่ดักจับได้ทั้งหมด
# ดู log ได้ที่ tab Actions -> เลือก run ล่าสุด -> ขั้นตอน "Run monitor script"
DEBUG = os.environ.get("DEBUG", "1") == "1"

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
LAST_RUN_HOUR = 22  # ชั่วโมง (เวลาไทย) ของรอบทำงานสุดท้ายในแต่ละวัน ต้องตรงกับตารางใน .github/workflows/monitor.yml

# คำใบ้ที่ใช้เดาว่า field ไหนคือ "หัวข้อข่าว" / "วันที่" / "รหัสข่าว" / "ลิงก์" / "ชื่อหุ้น"
TITLE_KEYS = ["subject", "title", "header", "newsSubject", "headline", "name"]
DATE_KEYS = ["datetime", "date", "newsDate", "publishDate", "createDate", "dateTime"]
ID_KEYS = ["newsId", "id", "docId", "no", "seq"]
LINK_KEYS = ["url", "link", "newsUrl", "detailUrl"]
CATEGORY_KEYS = ["newsType", "category", "type", "typeName", "newsCategory", "group"]
SYMBOL_KEYS = ["symbol", "stockSymbol", "securitySymbol", "companySymbol", "ticker"]

# ==== ตั้งค่าหัวข้อข่าวที่สนใจ ====
TOPIC_KEYWORDS = [
    "งบการเงิน",
    "ผลประกอบการ",
    "งบไตรมาส",
    "กำไรสุทธิ",
    "Earnings",
]

# หุ้นในลิสต์นี้จะได้รับ "ทุกข่าว" โดยไม่ต้องผ่าน TOPIC_KEYWORDS เลย ส่วนหุ้นอื่นยังต้องผ่านตัวกรองหัวข้อตามปกติ
SYMBOL_FILTER = [
    "ADVANC", "AOT", "AWC", "BANPU", "BBL", "BDMS", "BEM", "BGRIM", "BH", "BTS",
    "CBG", "CENTEL", "COM7", "CPALL", "CPF", "CPN", "CRC", "DELTA", "EA", "EGCO",
    "GLOBAL", "GPSC", "GULF", "HMPRO", "INTUCH", "IVL", "JMART", "KBANK", "KTB", "KTC",
    "LH", "MINT", "MTC", "OR", "OSP", "PTT", "PTTEP", "PTTGC", "RATCH", "SAWAD",
    "SCB", "SCC", "SCGP", "SIRI", "TIDLOR", "TISCO", "TOP", "TRUE", "TTB", "TU",
]


def log(*args):
    if DEBUG:
        print(*args, file=sys.stderr)


def looks_like_news_item(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    keys_lower = {k.lower() for k in d.keys()}
    has_title = any(k.lower() in keys_lower for k in TITLE_KEYS)
    has_date_or_id = any(k.lower() in keys_lower for k in DATE_KEYS + ID_KEYS)
    return has_title and has_date_or_id


def find_news_list(obj, path="root"):
    results = []
    if isinstance(obj, list):
        if obj and all(looks_like_news_item(x) for x in obj[:3]):
            results.append((path, obj))
        for i, item in enumerate(obj):
            results.extend(find_news_list(item, f"{path}[{i}]"))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(find_news_list(v, f"{path}.{k}"))
    return results


def extract_field(item: dict, keys):
    for k in keys:
        for actual_key in item.keys():
            if actual_key.lower() == k.lower():
                return item[actual_key]
    return None


def make_news_id(item: dict) -> str:
    raw_id = extract_field(item, ID_KEYS)
    if raw_id:
        return str(raw_id)
    title = str(extract_field(item, TITLE_KEYS) or "")
    date = str(extract_field(item, DATE_KEYS) or "")
    return hashlib.sha256(f"{title}|{date}".encode("utf-8")).hexdigest()[:16]


def matches_topic_filter(item: dict) -> bool:
    symbol = str(extract_field(item, SYMBOL_KEYS) or "").upper()

    if SYMBOL_FILTER and symbol in [s.upper() for s in SYMBOL_FILTER]:
        return True

    if TOPIC_KEYWORDS:
        title = str(extract_field(item, TITLE_KEYS) or "")
        category = str(extract_field(item, CATEGORY_KEYS) or "")
        haystack = f"{title} {category}".lower()
        return any(kw.lower() in haystack for kw in TOPIC_KEYWORDS)

    return True


async def fetch_news_items():
    captured_jsons = []

    async def on_response(response):
        try:
            ctype = response.headers.get("content-type", "")
            if "application/json" not in ctype:
                return
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            body = await response.json()
            captured_jsons.append((response.url, body))
            log("captured JSON from", response.url)
        except Exception as e:
            log("skip response", e)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("console", lambda msg: log("console:", msg.type, msg.text))
        try:
            await page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            log(f"page.goto ล้มเหลว/timeout: {e}")
            # ลองอีกครั้งด้วยเงื่อนไขที่ผ่อนปรนกว่า เผื่อหน้ามีการยิง request ต่อเนื่องไม่หยุด
            try:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(8000)
            except Exception as e2:
                log(f"page.goto รอบสองก็ล้มเหลว: {e2}")
        await page.wait_for_timeout(3000)

        page_title = await page.title()
        log(f"page title: {page_title!r}")
        log(f"จำนวน response ที่ดักจับได้ทั้งหมด (JSON จาก xhr/fetch): {len(captured_jsons)}")
        for url, _ in captured_jsons:
            log("  -", url)

        await browser.close()

    all_candidates = []
    for url, body in captured_jsons:
        for path, lst in find_news_list(body):
            all_candidates.append((url, path, lst))

    if not all_candidates:
        log("ไม่พบ JSON ที่หน้าตาเหมือนรายการข่าวเลยในบรรดา response ที่ดักจับได้ทั้งหมด")
        return []

    all_candidates.sort(key=lambda x: len(x[2]), reverse=True)
    best_url, best_path, best_list = all_candidates[0]
    log(f"เลือกใช้ list จาก {best_url} ({best_path}) จำนวน {len(best_list)} รายการ")
    if best_list:
        log("ตัวอย่างรายการแรก:", json.dumps(best_list[0], ensure_ascii=False)[:500])
    return best_list


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("seen_ids", [])
    state.setdefault("date", "")
    state.setdefault("had_news_today", False)
    state.setdefault("first_notified_today", False)
    state.setdefault("last_summary_sent_today", False)
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"Telegram API error response: {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def format_message(item: dict) -> str:
    title = escape_html(extract_field(item, TITLE_KEYS) or "(ไม่พบหัวข้อข่าว)")
    symbol = extract_field(item, SYMBOL_KEYS)
    date = escape_html(extract_field(item, DATE_KEYS) or "")
    link = extract_field(item, LINK_KEYS)
    if symbol:
        text = f"📰 <b>ข่าวใหม่จาก SET</b>\n<b>[{escape_html(symbol)}]</b> {title}"
    else:
        text = f"📰 <b>ข่าวใหม่จาก SET</b>\n{title}"
    if date:
        text += f"\n🕒 {date}"
    if link:
        if isinstance(link, str) and link.startswith("/"):
            link = "https://www.set.or.th" + link
        text += f"\n🔗 {escape_html(link)}"
    else:
        text += f"\n🔗 {PAGE_URL}"
    return text


async def main():
    now_bkk = datetime.now(BANGKOK_TZ)
    today_str = now_bkk.strftime("%Y-%m-%d")

    state = load_state()

    if state.get("date") != today_str:
        state["date"] = today_str
        state["had_news_today"] = False
        state["first_notified_today"] = False
        state["last_summary_sent_today"] = False

    is_first_run_today = not state["first_notified_today"]
    is_last_run_hour = now_bkk.hour == LAST_RUN_HOUR

    items = await fetch_news_items()
    if not items:
        print("ไม่พบรายการข่าว (ดู DEBUG log ถ้าต้องการตรวจสอบ)")
        send_telegram_message("⚠️ ตรวจแล้ว แต่ดึงรายการข่าวจากเว็บ SET ไม่ได้เลย (อาจเป็นเพราะเว็บเปลี่ยนโครงสร้าง)")
        save_state(state)
        return

    seen_ids = set(state.get("seen_ids", []))

    new_items = []
    current_ids = []
    for item in items:
        nid = make_news_id(item)
        current_ids.append(nid)
        if nid not in seen_ids and matches_topic_filter(item):
            new_items.append((nid, item))

    if not new_items:
        print("ไม่มีข่าวใหม่")
        if is_first_run_today:
            send_telegram_message("👀 มารอดูกันว่า วันนี้จะมีข่าวอะไรใหม่")
        if is_last_run_hour and not state["had_news_today"] and not state["last_summary_sent_today"]:
            send_telegram_message("🌙 วันนี้ยังไม่มีข่าวอะไรใหม่")
            state["last_summary_sent_today"] = True
    else:
        print(f"พบข่าวใหม่ {len(new_items)} รายการ กำลังส่งเข้า Telegram...")
        for nid, item in reversed(new_items):
            send_telegram_message(format_message(item))
        state["had_news_today"] = True

    state["first_notified_today"] = True

    updated_ids = list(dict.fromkeys(current_ids + list(seen_ids)))[:500]
    state["seen_ids"] = updated_ids
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
