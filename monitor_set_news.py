"""
ตรวจข่าวใหม่จากหน้า "ข่าวหลักทรัพย์" ของตลาดหลักทรัพย์แห่งประเทศไทย (SET)
แล้วส่งแจ้งเตือนเข้า Telegram เมื่อพบข่าวที่ยังไม่เคยแจ้งมาก่อน

เงื่อนไขการกรองข่าว (แก้ไขในไฟล์ .txt ได้เลย ไม่ต้องแตะโค้ด):
- symbol_filter.txt: รายชื่อหุ้นที่สนใจ ต้องเป็นหุ้นในลิสต์นี้เท่านั้นถึงจะพิจารณาส่ง
- topic_keywords.txt: คำที่ต้องมีในหัวข้อ/ประเภทข่าว ต้องตรงคำใดคำหนึ่งด้วยถึงจะส่ง
- ทั้งสองเงื่อนไขเป็น "AND" กัน คือต้องผ่านทั้งคู่ ข่าวถึงจะถูกส่งเข้า Telegram
- holidays.txt: วันที่ระบุไว้ในนี้ บอทจะไม่ทำงานเลยทั้งวัน
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright
import requests

PAGE_URL = "https://www.set.or.th/th/market/news-and-alert/news"
STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# เปิดไว้ถาวรชั่วคราวเพื่อวินิจฉัยปัญหา — log จะโชว์รายละเอียด endpoint ที่ดักจับได้ทั้งหมด
DEBUG = os.environ.get("DEBUG", "1") == "1"

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")
LAST_RUN_HOUR = 22  # ชั่วโมง (เวลาไทย) ของรอบทำงานสุดท้ายในแต่ละวัน ต้องตรงกับตารางใน .github/workflows/monitor.yml

# คำใบ้ที่ใช้เดาว่า field ไหนคือ "หัวข้อข่าว" / "วันที่" / "รหัสข่าว" / "ลิงก์" / "ชื่อหุ้น"
# (ยืนยันจากข้อมูลจริงของเว็บ SET แล้วว่า field จริงคือ: id, datetime, symbol, headline, url, tag)
TITLE_KEYS = ["headline", "subject", "title", "header", "newsSubject"]
DATE_KEYS = ["datetime", "date", "newsDate", "publishDate", "createDate"]
ID_KEYS = ["id", "newsId", "docId", "no", "seq"]
LINK_KEYS = ["url", "link", "newsUrl", "detailUrl"]
CATEGORY_KEYS = ["tag", "_group_label", "newsType", "category", "type", "typeName", "newsCategory", "group"]
SYMBOL_KEYS = ["symbol", "stockSymbol", "securitySymbol", "companySymbol", "ticker"]

# endpoint ข่าวจริงของเว็บ SET ที่ยืนยันแล้วจาก DEBUG log
NEWS_ENDPOINT_HINT = "/api/cms/v1/news/set"

# ==== ตั้งค่าหัวข้อข่าว/หุ้นที่สนใจ (อ่านจากไฟล์ .txt แยกต่างหาก ไม่ต้องแก้โค้ดตรงนี้) ====
TOPIC_KEYWORDS_FILE = Path(__file__).parent / "topic_keywords.txt"
SYMBOL_FILTER_FILE = Path(__file__).parent / "symbol_filter.txt"

# ==== วันหยุดตลาดหลักทรัพย์ (อ่านจากไฟล์ holidays.txt) ====
HOLIDAYS_FILE = Path(__file__).parent / "holidays.txt"
_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def log(*args):
    if DEBUG:
        print(*args, file=sys.stderr)


def load_lines_file(path: Path) -> list:
    """อ่านไฟล์ .txt แบบ 1 บรรทัดต่อ 1 ค่า ข้ามบรรทัดว่างและบรรทัดที่ขึ้นต้นด้วย #
    ถ้าไฟล์หายหรืออ่านไม่ได้ ให้คืนลิสต์ว่าง (ไม่ทำให้บอทพังทั้งระบบ)"""
    if not path.exists():
        log(f"ไม่พบไฟล์ {path} ใช้ค่าว่างแทน (ไม่กรอง)")
        return []
    try:
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            result.append(line)
        return result
    except Exception as e:
        log(f"อ่านไฟล์ {path} ไม่สำเร็จ: {e} — ใช้ค่าว่างแทน")
        return []


def load_holidays() -> set:
    """อ่านรายชื่อวันหยุดจากไฟล์ holidays.txt (1 บรรทัดต่อ 1 วัน, บรรทัดที่ขึ้นต้นด้วย # คือคอมเมนต์)"""
    if not HOLIDAYS_FILE.exists():
        log(f"ไม่พบไฟล์ {HOLIDAYS_FILE} ข้ามการเช็ควันหยุด")
        return set()
    try:
        holidays = set()
        for line in HOLIDAYS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _DATE_PATTERN.match(line)
            if m:
                holidays.add(m.group(1))
        return holidays
    except Exception as e:
        log(f"อ่านไฟล์ {HOLIDAYS_FILE} ไม่สำเร็จ: {e} — ข้ามการเช็ควันหยุด")
        return set()


def is_holiday_today(now_bkk: datetime) -> bool:
    return now_bkk.strftime("%Y-%m-%d") in load_holidays()


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
    """ต้องผ่านทั้ง 2 เงื่อนไข (AND) ถึงจะส่งเข้า Telegram:
    1. ต้องเป็นหุ้นที่อยู่ใน symbol_filter.txt (ถ้าไฟล์นั้นว่างเปล่า = ไม่จำกัดหุ้น)
    2. หัวข้อ/ประเภทข่าวต้องมีคำใดคำหนึ่งใน topic_keywords.txt (ถ้าไฟล์นั้นว่างเปล่า = ไม่จำกัดหัวข้อ)
    """
    symbol_filter = load_lines_file(SYMBOL_FILTER_FILE)
    topic_keywords = load_lines_file(TOPIC_KEYWORDS_FILE)

    if symbol_filter:
        symbol = str(extract_field(item, SYMBOL_KEYS) or "").upper()
        if symbol not in [s.upper() for s in symbol_filter]:
            return False  # ไม่ใช่หุ้นที่สนใจ ไม่ส่ง

    if topic_keywords:
        title = str(extract_field(item, TITLE_KEYS) or "")
        category = str(extract_field(item, CATEGORY_KEYS) or "")
        haystack = f"{title} {category}".lower()
        if not any(kw.lower() in haystack for kw in topic_keywords):
            return False  # หัวข้อไม่ตรงเงื่อนไข ไม่ส่ง

    return True


def extract_list_from_known_endpoint(body):
    """ดึง list ของข่าวออกจาก JSON body ของ endpoint ข่าวที่รู้จักแน่นอนแล้ว
    รองรับกรณี list ว่างเปล่าด้วย (แปลว่าวันนั้นไม่มีข่าว ไม่ใช่ error)

    สำคัญ: โครงสร้างจริงของ SET คือ list ของ "กลุ่มข่าว" (เช่น {"group": "ข่าวงบการเงิน",
    "totalCount": 5, "newsInfoList": [...ข่าวจริงแต่ละชิ้น...]}) ไม่ใช่ list ของข่าวตรงๆ
    ฟังก์ชันนี้จึงต้อง "แตก" (flatten) newsInfoList ของแต่ละกลุ่มออกมาเป็นข่าวจริงทีละชิ้นก่อน
    """
    raw_list = None
    if isinstance(body, list):
        raw_list = body
    elif isinstance(body, dict):
        wrapper_keys = ["data", "list", "items", "results", "securities", "newslist", "rows", "records", "news"]
        for k, v in body.items():
            if k.lower() in wrapper_keys and isinstance(v, list):
                raw_list = v
                break
        if raw_list is None:
            for v in body.values():
                if isinstance(v, list):
                    raw_list = v
                    break
                if isinstance(v, dict):
                    for v2 in v.values():
                        if isinstance(v2, list):
                            raw_list = v2
                            break
                    if raw_list is not None:
                        break

    if raw_list is None:
        return None

    flattened = []
    for entry in raw_list:
        if isinstance(entry, dict) and isinstance(entry.get("newsInfoList"), list):
            group_label = entry.get("group")
            for leaf in entry["newsInfoList"]:
                if isinstance(leaf, dict):
                    if group_label:
                        leaf = {**leaf, "_group_label": group_label}
                    flattened.append(leaf)
        elif isinstance(entry, dict):
            flattened.append(entry)
    return flattened


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

    matched_lists = []
    for url, body in captured_jsons:
        if NEWS_ENDPOINT_HINT in url:
            lst = extract_list_from_known_endpoint(body)
            if lst is not None:
                log(f"พบ endpoint ข่าวที่รู้จัก: {url} -> {len(lst)} รายการ")
                matched_lists.append(lst)

    if matched_lists:
        merged = []
        seen_ids_local = set()
        for lst in matched_lists:
            for item in lst:
                if not isinstance(item, dict):
                    continue
                nid = make_news_id(item)
                if nid not in seen_ids_local:
                    seen_ids_local.add(nid)
                    merged.append(item)
        if merged:
            log("ตัวอย่างรายการแรก (keys ทั้งหมด):", list(merged[0].keys()))
            log("ตัวอย่างรายการแรก:", json.dumps(merged[0], ensure_ascii=False)[:800])
        else:
            log("พบ endpoint ข่าวที่รู้จัก แต่รวมแล้วไม่มีข่าวเลย — ถือว่าดึงสำเร็จ แค่วันนี้ไม่มีข่าว")
        return True, merged

    all_candidates = []
    for url, body in captured_jsons:
        for path, lst in find_news_list(body):
            all_candidates.append((url, path, lst))

    if not all_candidates:
        log("ไม่พบ JSON ที่หน้าตาเหมือนรายการข่าวเลย และไม่พบ endpoint ที่รู้จักด้วย")
        return False, []

    all_candidates.sort(key=lambda x: len(x[2]), reverse=True)
    best_url, best_path, best_list = all_candidates[0]
    log(f"[fallback] เลือกใช้ list จาก {best_url} ({best_path}) จำนวน {len(best_list)} รายการ")
    return True, best_list


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


def send_telegram_message(text: str, max_retries: int = 5):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for attempt in range(1, max_retries + 1):
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
        if resp.status_code == 200:
            return

        if resp.status_code == 429:
            try:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                retry_after = 5
            wait_seconds = int(retry_after) + 1
            print(f"Telegram rate limit (429) — รอ {wait_seconds} วินาทีแล้วลองส่งใหม่ "
                  f"(ครั้งที่ {attempt}/{max_retries})", file=sys.stderr)
            time.sleep(wait_seconds)
            continue

        print(f"Telegram API error response: {resp.text}", file=sys.stderr)
        resp.raise_for_status()

    raise RuntimeError(f"ส่งข้อความเข้า Telegram ไม่สำเร็จหลังลอง {max_retries} ครั้ง (โดน rate limit ต่อเนื่อง)")


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

    if is_holiday_today(now_bkk):
        print(f"วันนี้ ({today_str}) เป็นวันหยุดตลาดหลักทรัพย์ ข้ามการทำงานทั้งหมด")
        return

    if now_bkk.weekday() >= 5:
        print(f"วันนี้ ({today_str}) เป็นวันเสาร์-อาทิตย์ ข้ามการทำงานทั้งหมด")
        return

    state = load_state()

    if state.get("date") != today_str:
        state["date"] = today_str
        state["had_news_today"] = False
        state["first_notified_today"] = False
        state["last_summary_sent_today"] = False

    is_first_run_today = not state["first_notified_today"]
    is_last_run_hour = now_bkk.hour == LAST_RUN_HOUR

    success, items = await fetch_news_items()
    if not success:
        print("ดึงรายการข่าวไม่ได้เลย (ดู DEBUG log ถ้าต้องการตรวจสอบ)")
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
        state["first_notified_today"] = True
        state["seen_ids"] = list(dict.fromkeys(current_ids + list(seen_ids)))[:500]
        save_state(state)
        return

    print(f"พบข่าวใหม่ {len(new_items)} รายการ กำลังส่งเข้า Telegram...")
    sent_count = 0
    try:
        for nid, item in reversed(new_items):
            send_telegram_message(format_message(item))
            sent_count += 1
            seen_ids.add(nid)
            state["had_news_today"] = True
            state["first_notified_today"] = True
            state["seen_ids"] = list(dict.fromkeys(current_ids + list(seen_ids)))[:500]
            save_state(state)
            time.sleep(2)
    except Exception:
        print(f"ส่งข่าวสำเร็จไปแล้ว {sent_count}/{len(new_items)} รายการ ก่อนเกิด error "
              f"(ข่าวที่ส่งสำเร็จแล้วจะไม่ถูกส่งซ้ำในรอบถัดไป ส่วนที่เหลือจะลองใหม่ในรอบหน้า)", file=sys.stderr)
        raise


if __name__ == "__main__":
    asyncio.run(main())
