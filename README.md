# ParkingCount AI + ป้าย LED HT16K33

ระบบนับรถเข้า/ออกที่จอดรถแต่ละชั้นด้วย AI (YOLOv8) จากกล้อง IP แสดงจำนวนช่องว่างบนหน้าเว็บ บนป้าย LED (ESP32 + HT16K33) และแจ้งเตือนผ่าน Telegram

- นับรถด้วยการตีเส้นขวางทางลาด รถผ่านเส้น A→B / B→A แล้วระบบบวก/ลบจำนวนรถในชั้นให้อัตโนมัติ
- ต่อกล้องผ่าน RTSP (กล้องทุกยี่ห้อ) หรือผ่าน Hikvision HCNetSDK
- ป้าย LED ESP32 เชื่อมต่อแบบ WebSocket ค่าบนป้ายเปลี่ยนทันทีที่มีรถผ่าน
- แจ้งเตือนผ่าน Telegram Bot
- หน้าเว็บมีแดชบอร์ด กราฟรถเข้า/ออกรายชั่วโมง และปุ่มจำลองรถผ่านไว้ทดสอบระบบ

## ติดตั้งและรัน (Windows)

1. ติดตั้ง **Python 3.11 หรือ 3.12 (64-bit)** จาก [python.org](https://www.python.org/downloads/) ตอนติดตั้งให้ติ๊ก **Add python.exe to PATH**
2. ดาวน์โหลดโปรเจกต์นี้ (ปุ่ม **Code → Download ZIP** แล้วแตกไฟล์ หรือใช้ `git clone`)
3. ดับเบิลคลิก **`run.bat`**
   - ครั้งแรกจะสร้าง `venv` และติดตั้งไลบรารีให้เอง (ultralytics ไฟล์ใหญ่ ใช้เวลาสักพัก)
   - เปิดพอร์ต 8000 ใน Windows Firewall ให้ ESP32 และเครื่องอื่นในวงแลนเข้าถึงได้ (ถ้ารันแบบ Administrator)
4. เปิดเบราว์เซอร์ไปที่ **http://localhost:8000**

ฐานข้อมูล `parking.db` จะถูกสร้างให้อัตโนมัติตอนรันครั้งแรก

## ต่อกล้อง

เข้าไปที่หน้าตั้งค่า แท็บ **กล้อง** แล้วเลือกวิธีต่อได้ 2 แบบ

- **RTSP (แนะนำ ใช้ได้ทันที):** กรอกช่อง RTSP URL เช่น
  `rtsp://admin:รหัสผ่าน@192.168.1.64:554/Streaming/Channels/102`
- **Hikvision HCNetSDK:** เว้นช่อง RTSP ว่าง แล้วกรอก IP / Port (8000) / Username / Password / Channel
  ต้องดาวน์โหลด HCNetSDK (Windows 64-bit) จาก [Hikvision](https://www.hikvision.com/en/support/download/sdk/) ก่อน แล้วคัดลอกไฟล์ในโฟลเดอร์ `lib` ของ SDK (`HCNetSDK.dll`, `PlayCtrl.dll`, `HCCore.dll`, โฟลเดอร์ `HCNetSDKCom/` ฯลฯ) มาไว้ที่ `hiksdk/lib/` (ไฟล์ DLL ไม่ได้รวมไว้ใน repo)

จากนั้นลากเส้นนับรถให้ขวางทางลาด แล้วเลือกว่ารถที่ผ่านเส้นจะบวกหรือลบจำนวนรถของชั้นไหน

## ป้าย LED (ESP32 + HT16K33)

อุปกรณ์: ESP32 Dev Module + 0.54" 14-segment LED HT16K33 Backpack (4 หลัก)

**ต่อสาย (I2C)**

| ESP32 | LED |
|-------|-----|
| VIN   | VCC |
| GND   | GND |
| D21   | SDA |
| D22   | SCL |

> ถ้าบอร์ด LED มีขา **Vi2c** ให้ต่อ ESP32 **3V3 → Vi2c** เพิ่มด้วย

**แฟลชเฟิร์มแวร์**

1. ในหน้าเว็บไปที่แท็บ **ป้าย LED (ESP32)** กรอก WiFi SSID / Password แล้วกด **ดาวน์โหลดเฟิร์มแวร์** จะได้ไฟล์ `.ino` ที่ใส่ WiFi และ IP เซิร์ฟเวอร์ไว้ให้แล้ว
   (หรือจะเปิด `esp32_display/esp32_display.ino` แล้วแก้ `WIFI_SSID`, `WIFI_PASS`, `SERVER_HOST` เองก็ได้)
2. ใน Arduino IDE ติดตั้งบอร์ด ESP32 และไลบรารีเหล่านี้จาก Library Manager
   - **WebSockets** by Markus Sattler (2.4 ขึ้นไป)
   - **ArduinoJson** by Benoit Blanchon (7.x)
   - **Adafruit LED Backpack Library** (ระบบจะถามให้ลง Adafruit GFX และ BusIO ด้วย ให้กด Install all)
3. เลือก Board **ESP32 Dev Module** แล้วกด Upload
4. ESP32 จะไปขึ้นในหน้าเว็บเอง จากนั้นตั้งชื่อป้ายและเลือกชั้นที่จะแสดง

## แจ้งเตือน Telegram

ในแท็บ **แจ้งเตือน Telegram** ใส่ Bot Token (ขอจาก @BotFather) และ Chat ID ขั้นตอนอย่างละเอียดมีอยู่ในหน้าเว็บ

## ตั้งค่าเพิ่มเติม (Environment variables)

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|--------|-------------|----------|
| `MODEL` | `yolov8n.pt` | โมเดล YOLO (`yolov8s.pt` แม่นขึ้นแต่ช้าลง) |
| `INFER_FPS` | `8` | จำนวนเฟรมต่อวินาทีที่ส่งให้ AI |
| `IMGSZ` | `480` | ขนาดภาพที่ส่งให้ AI (640 แม่นสุด, 320 เร็วสุด) |
| `VIEW_FPS` | `15` | เฟรมต่อวินาทีของภาพสดบนเว็บ |
| `VIEW_W` | `960` | ความกว้างสูงสุดของภาพสดบนเว็บ |

## โครงสร้างไฟล์

```
server.py                 เซิร์ฟเวอร์ FastAPI + AI นับรถ
hik_capture.py            ดึงภาพจากกล้อง Hikvision ผ่าน HCNetSDK
static/index.html         หน้าเว็บ
esp32_display/            เฟิร์มแวร์ป้าย LED
hiksdk/include/           header ของ HCNetSDK (DLL ต้องวางเองใน hiksdk/lib/)
yolov8n.pt                โมเดล YOLOv8n
run.bat                   ติดตั้ง + รันในคลิกเดียว
```
