# แจ้งเตือนข่าว SET เข้า Telegram อัตโนมัติ

ระบบนี้จะคอยตรวจหน้า "ข่าวหลักทรัพย์" ของ SET
(https://www.set.or.th/th/market/news-and-alert/news)
ทุก 10 นาที (ปรับได้) แล้วส่งข่าวใหม่เข้า Telegram โดยรันฟรีบน GitHub Actions
ไม่ต้องมีเซิร์ฟเวอร์หรือเปิดคอมทิ้งไว้

## ภาพรวมไฟล์
- `monitor_set_news.py` — สคริปต์หลัก เปิดหน้าเว็บ อ่านข่าว เทียบของเก่า/ใหม่ ส่ง Telegram
- `requirements.txt` — ไลบรารีที่ต้องใช้
- `state.json` — เก็บรายการข่าวที่เคยแจ้งไปแล้ว (สคริปต์จะอัปเดตไฟล์นี้เอง)
- `.github/workflows/monitor.yml` — ตัวตั้งเวลาให้รันอัตโนมัติบน GitHub

## ขั้นตอนตั้งค่า (ทำครั้งเดียว)

### 1. สร้าง GitHub repository
1. เข้า https://github.com/new สร้าง repo ใหม่ (ตั้งเป็น Private ก็ได้)
2. อัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้นไป (รักษาโครงสร้างโฟลเดอร์ `.github/workflows/` ไว้ด้วย)

### 2. ใส่ Telegram Bot Token และ Chat ID เป็น Secret
ในหน้า repo ไปที่ **Settings → Secrets and variables → Actions → New repository secret**
เพิ่ม 2 ตัว:
- ชื่อ `TELEGRAM_BOT_TOKEN` — ค่าคือ token ของบอทที่คุณมีอยู่แล้ว
- ชื่อ `TELEGRAM_CHAT_ID` — ค่าคือ chat id ที่ต้องการให้ส่งข้อความไปหา

(ห้ามใส่ token/chat id ลงในไฟล์โค้ดตรงๆ เพราะถ้า repo public จะรั่วได้)

### 3. เปิดใช้งาน Actions และทดสอบรันครั้งแรก
1. ไปที่แท็บ **Actions** ของ repo → กด "I understand my workflows, go ahead and enable them" (ถ้าเจอ)
2. เลือก workflow ชื่อ **Monitor SET News to Telegram** → กด **Run workflow** เพื่อทดสอบรันด้วยมือก่อน
3. เข้าไปดู log การรัน ถ้าขึ้นว่า "พบข่าวใหม่ ... รายการ" และคุณได้รับข้อความใน Telegram แปลว่าใช้งานได้แล้ว
4. รันครั้งแรกข่าวเก่าทั้งหมดจะถูกนับเป็น "ใหม่" และส่งเข้า Telegram รวดเดียว (เพราะ state.json ยังว่างอยู่)
   ถ้าไม่อยากให้ยิงข่าวเก่าทั้งหมดในรอบแรก ให้รันสคริปต์นี้ 1 ครั้งบนเครื่องตัวเองก่อน (ดูข้อ 4)
   เพื่อสร้าง state.json ที่มีข่าวปัจจุบันอยู่แล้ว แล้วค่อย commit ไฟล์นั้นขึ้น repo ก่อนเปิด schedule จริง

### 4. (แนะนำ) ทดสอบรันบนเครื่องตัวเองก่อน 1 ครั้ง
```bash
pip install -r requirements.txt
playwright install chromium

export TELEGRAM_BOT_TOKEN="ใส่ token ของคุณ"
export TELEGRAM_CHAT_ID="ใส่ chat id ของคุณ"
export DEBUG=1   # ใส่เพื่อดู log ว่าดักจับ endpoint ข่าวถูกต้องหรือไม่

python monitor_set_news.py
```
ถ้ารันแล้วขึ้น "ไม่พบรายการข่าว" ให้ดู log จาก DEBUG=1 ว่าดักจับ JSON endpoint อะไรมาบ้าง
แล้วแจ้งกลับมาได้ เดี๋ยวช่วยปรับ field ที่ใช้เดาโครงสร้างข่าวในสคริปต์ให้ตรงขึ้น

หลังทดสอบผ่านแล้ว ให้ `git add state.json && git commit && git push` ไฟล์ state.json
ที่มีข้อมูลข่าวปัจจุบันขึ้น repo ก่อน แล้วรอบถัดไปที่ Actions รันอัตโนมัติจะแจ้งเฉพาะข่าวที่ออกใหม่จริงๆ เท่านั้น

## ปรับความถี่การตรวจสอบ
แก้บรรทัด `cron` ในไฟล์ `.github/workflows/monitor.yml`
เช่น ทุก 5 นาที → `*/5 2-10 * * 1-5` (เวลาที่เขียนใน cron เป็น UTC เสมอ ไม่ใช่เวลาไทย)

## ข้อจำกัดที่ควรรู้
- GitHub Actions แบบ schedule มีดีเลย์ได้บ้าง (ปกติไม่เกิน 2-3 นาที) ไม่ใช่ real-time เป๊ะๆ
- สคริปต์เดาโครงสร้างข้อมูลข่าวจาก JSON ที่เว็บ SET ส่งมาเองแบบอัตโนมัติ ถ้าทาง SET เปลี่ยนโครงสร้างเว็บ
  อาจต้องปรับ `TITLE_KEYS` / `DATE_KEYS` / `ID_KEYS` / `LINK_KEYS` ใน `monitor_set_news.py` ใหม่
- ถ้าต้องการกรองเฉพาะบางประเภทข่าว/บางหุ้น สามารถเพิ่มเงื่อนไขกรองใน `main()` ก่อนส่ง Telegram ได้ บอกได้เลยถ้าต้องการให้ช่วยเพิ่ม
