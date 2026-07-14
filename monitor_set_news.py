"""
ตรวจข่าวใหม่จากหน้า "ข่าวหลักทรัพย์" ของตลาดหลักทรัพย์แห่งประเทศไทย (SET)
แล้วส่งแจ้งเตือนเข้า Telegram เมื่อพบข่าวที่ยังไม่เคยแจ้งมาก่อน

วิธีทำงาน:
1. เปิดหน้าเว็บ https://www.set.or.th/th/market/news-and-alert/news ด้วย headless browser (Playwright)
   เพราะหน้านี้โหลดรายการข่าวด้วย JavaScript (ไม่ได้อยู่ใน HTML ตรงๆ)
2. ระหว่างโหลดหน้า จะดักจับ (intercept) คำตอบ JSON ทุกตัวที่เบราว์เซอร์ขอจากเซิร์ฟเวอร์ SET
   แล้วเดาว่าตัวไหนคือ "รายการข่าว" โดยดูจาก field ที่หน้าตาคล้ายข่าว (มีหัวข้อ/วันที่/รหัสข่าว)
3. เทียบกับรายการข่าวที่เคยเห็นแล้ว (เก็บไว้ในไฟล์ state.json) ถ้าเจอข่าวใหม่ -> ส่งเข้า Telegram
4. บันทึก state.json ใหม่ (ต้องถูก commit กลับเข้า repo ถ้ารันผ่าน GitHub Actions ดู workflow ไฟล์ประกอบ)

หมายเหตุสำคัญ:
- สคริปต์นี้ใช้วิธี "เดาโครงสร้าง" ข้อมูลข่าวโดยอัตโนมัติ เพราะไม่สามารถเข้าถึงอินเทอร์เน็ตจริง
  ระหว่างที่เตรียมสคริปต์ให้ได้ ควรรันครั้งแรกแบบ DEBUG=1 เพื่อดูว่าดักจับ endpoint/field ถูกต้องหรือไม่
  แล้วค่อยปรับ NEWS_KEY_HINTS / ITEM_FIELDS ด้านล่างให้ตรงกับของจริงถ้าจำเป็น
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright
import requests

PAGE_URL = "https://www.set.or.th/th/market/news-and-alert/news"
STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEBUG = os.environ.get("DEBUG", "0") == "1"

# คำใบ้ที่ใช้เดาว่า field ไหนคือ "หัวข้อข่าว" / "วันที่" / "รหัสข่าว" / "ลิงก์"
TITLE_KEYS = ["subject", "title", "header", "newsSubject", "headline", "name"]
DATE_KEYS = ["datetime", "date", "newsDate", "publishDate", "createDate", "dateTime"]
ID_KEYS = ["newsId", "id", "docId", "no", "seq"]
LINK_KEYS = ["url", "link", "newsUrl", "detailUrl"]
CATEGORY_KEYS = ["newsType", "category", "type", "typeName", "newsCategory", "group"]

# ==== ตั้งค่าหัวข้อข่าวที่สนใจ ====
# ใส่คำที่ต้องการกรองไว้ในลิสต์นี้ (ไม่สนตัวพิมพ์เล็ก/ใหญ่)
# ถ้าปล่อยเป็น [] (ลิสต์ว่าง) = ไม่กรอง ส่งทุกข่าวเหมือนเดิม
# ระบบจะเช็คคำเหล่านี้ทั้งจาก "หัวข้อข่าว" และ "ประเภทข่าว" (ถ้าเว็บส่งฟิลด์ประเภทข่าวมาด้วย)
TOPIC_KEYWORDS = [
    "งบการเงิน",
    "งบการเงินรายปี",
    "ผลประกอบการ",
    "งบไตรมาส",
    "กำไรสุทธิ",
    "Earnings",
    "คำอธิบายและวิเคราะห์ของฝ่ายจัดการ",
    "แจ้งเลิกกิจการ",
    "แผนปรับโครงสร้างธุรกิจ",
    "ชี้แจงข้อเท็จจริง",
    "สรุปผลการดำเนินงานของ",
    "จ่ายปันผล",
    "รายงานประจำปี",
    "แจ้งการจัดตั้งบริษัทย่อย",
    "แจ้งการเลิกบริษัทย่อย",
    "XD",
]


def log(*args):
    if DEBUG:
        print(*args, file=sys.stderr)


def looks_like_news_item(d: dict) -> bool:
    """เดาว่า dict นี้หน้าตาเหมือนข่าวหนึ่งชิ้นหรือไม่"""
    if not isinstance(d, dict):
        return False
    keys_lower = {k.lower() for k in d.keys()}
    has_title = any(k.lower() in keys_lower for k in TITLE_KEYS)
    has_date_or_id = any(k.lower() in keys_lower for k in DATE_KEYS + ID_KEYS)
    return has_title and has_date_or_id


def find_news_list(obj, path="root"):
    """ไล่หา list ของ dict ที่ดูเหมือนรายการข่าวใน object ที่ซ้อนกันหลายชั้น"""
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


def matches_topic_filter(item: dict) -> bool:
    """คืนค่า True ถ้าข่าวนี้ผ่านตัวกรองหัวข้อ (หรือไม่ได้ตั้งตัวกรองไว้เลย)"""
    if not TOPIC_KEYWORDS:
        return True
    title = str(extract_field(item, TITLE_KEYS) or "")
    category = str(extract_field(item, CATEGORY_KEYS) or "")
    haystack = f"{title} {category}".lower()
    return any(kw.lower() in haystack for kw in TOPIC_KEYWORDS)


def make_news_id(item: dict) -> str:
    """สร้าง id เฉพาะของข่าวแต่ละชิ้น ไว้เทียบว่าเคยแจ้งไปหรือยัง"""
    raw_id = extract_field(item, ID_KEYS)
    if raw_id:
        return str(raw_id)
    # ถ้าไม่มี id ให้ hash จากหัวข้อ+วันที่แทน
    title = str(extract_field(item, TITLE_KEYS) or "")
    date = str(extract_field(item, DATE_KEYS) or "")
    return hashlib.sha256(f"{title}|{date}".encode("utf-8")).hexdigest()[:16]


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
        await page.goto(PAGE_URL, wait_until="networkidle", timeout=60000)
        # ให้เวลาเว็บยิง XHR เพิ่มเติมหลังโหลดหน้าเสร็จ
        await page.wait_for_timeout(3000)
        await browser.close()

    all_candidates = []
    for url, body in captured_jsons:
        for path, lst in find_news_list(body):
            all_candidates.append((url, path, lst))

    if not all_candidates:
        log("ไม่พบ JSON ที่หน้าตาเหมือนรายการข่าวเลย ลองรันด้วย DEBUG=1 เพื่อดู endpoint ที่ถูกดักจับทั้งหมด")
        return []

    # เลือก list ที่มีจำนวนรายการมากที่สุด (มักจะเป็นรายการข่าวจริง ไม่ใช่ dropdown ตัวกรอง)
    all_candidates.sort(key=lambda x: len(x[2]), reverse=True)
    best_url, best_path, best_list = all_candidates[0]
    log(f"เลือกใช้ list จาก {best_url} ({best_path}) จำนวน {len(best_list)} รายการ")
    return best_list


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
    resp.raise_for_status()


def format_message(item: dict) -> str:
    title = extract_field(item, TITLE_KEYS) or "(ไม่พบหัวข้อข่าว)"
    date = extract_field(item, DATE_KEYS) or ""
    link = extract_field(item, LINK_KEYS)
    text = f"📰 <b>ข่าวใหม่จาก SET</b>\n{title}"
    if date:
        text += f"\n🕒 {date}"
    if link:
        if isinstance(link, str) and link.startswith("/"):
            link = "https://www.set.or.th" + link
        text += f"\n🔗 {link}"
    else:
        text += f"\n🔗 {PAGE_URL}"
    return text


async def main():
    items = await fetch_news_items()
    if not items:
        print("ไม่พบรายการข่าว (ดู DEBUG log ถ้าต้องการตรวจสอบ)")
        return

    state = load_state()
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
    else:
        print(f"พบข่าวใหม่ {len(new_items)} รายการ กำลังส่งเข้า Telegram...")
        # ส่งจากเก่าไปใหม่ จะได้เรียงลำดับใน Telegram สวยงาม
        for nid, item in reversed(new_items):
            send_telegram_message(format_message(item))

    # เก็บเฉพาะ id ที่ยังปรากฏอยู่ในหน้าเว็บล่าสุด + ที่เคยเห็น (กันไฟล์บวมไม่รู้จบ เก็บแค่ 500 รายการล่าสุด)
    updated_ids = list(dict.fromkeys(current_ids + list(seen_ids)))[:500]
    save_state({"seen_ids": updated_ids})


if __name__ == "__main__":
    asyncio.run(main())
