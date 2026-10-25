"""Parking AI counter - YOLO + line crossing + web dashboard + ESP32 LED signs + Telegram"""
import asyncio, html, itertools, json, os, queue, re, socket, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request
from contextlib import asynccontextmanager

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("MODEL", "yolov8n.pt")      # yolov8s.pt = แม่นขึ้น ช้าลง
INFER_FPS = float(os.environ.get("INFER_FPS", 8))  # จำนวนเฟรมต่อวินาทีที่ส่งให้ AI
IMGSZ = int(os.environ.get("IMGSZ", 480))          # 640 = แม่นสุดแต่ช้า, 480 = สมดุล (CPU), 320 = เร็วสุด
VIEW_FPS = float(os.environ.get("VIEW_FPS", 15))   # เฟรมต่อวินาทีของภาพสดบนเว็บ (แยกจาก AI)
VIEW_W = int(os.environ.get("VIEW_W", 960))        # ย่อภาพสดให้กว้างไม่เกินนี้ ลดภาระเครื่อง/เน็ต
CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# ---------------- DB ----------------
db_lock = threading.RLock()
db = sqlite3.connect(os.path.join(BASE, "parking.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS floors(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, total INTEGER DEFAULT 20, occupied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cameras(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, ip TEXT DEFAULT '', port INTEGER DEFAULT 8000,
  username TEXT DEFAULT 'admin', password TEXT DEFAULT '', channel TEXT DEFAULT '102', rtsp_url TEXT DEFAULT '', floor_id INTEGER,
  x1 REAL DEFAULT 0.1, y1 REAL DEFAULT 0.5, x2 REAL DEFAULT 0.9, y2 REAL DEFAULT 0.5, mode TEXT DEFAULT 'both',
  ab_delta INTEGER DEFAULT 1, conf REAL DEFAULT 0.4, margin REAL DEFAULT 0.02, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, camera_id INTEGER, floor_id INTEGER,
  direction TEXT, delta INTEGER, cls TEXT, occupied INTEGER);
CREATE TABLE IF NOT EXISTS camera_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER, floor_id INTEGER, delta INTEGER);
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT, floor_id INTEGER, mode TEXT DEFAULT 'free',
  brightness INTEGER DEFAULT 12, full_text INTEGER DEFAULT 1, ip TEXT DEFAULT '', fw TEXT DEFAULT '', created REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
""")
if not db.execute("SELECT 1 FROM floors").fetchone():
    db.executemany("INSERT INTO floors(name,total) VALUES(?,20)", [("1A",), ("1B",), ("2A",), ("2B",)])
db.commit()

def q(sql, args=(), one=False):
    with db_lock:
        rows = [dict(r) for r in db.execute(sql, args).fetchall()]
        db.commit()
    return (rows[0] if rows else None) if one else rows

DEFAULTS = {"tg_enabled": "0", "tg_token": "", "tg_chat": "", "tg_mode": "event", "tg_merge": "3",
            "tg_silent": "0", "tg_live": "{}", "device_token": ""}
def gs(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS.get(k, "")
def ss(k, v):
    q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

# ย้ายค่าเดิม (กล้อง 1 ตัว -> 1 ชั้น) มาเป็นกฎแบบหลายชั้น ทำครั้งเดียว
if gs("migr_rules") != "1":
    for c in q("SELECT id, floor_id, ab_delta FROM cameras WHERE floor_id IS NOT NULL"):
        q("INSERT INTO camera_rules(camera_id,floor_id,delta) VALUES(?,?,?)", (c["id"], c["floor_id"], c["ab_delta"] or 1))
    ss("migr_rules", "1")

# เปลี่ยนจาก RTSP (554) มาใช้ HCNetSDK (8000) ทำครั้งเดียว
if gs("migr_sdk_port") != "1":
    q("UPDATE cameras SET port=8000 WHERE port=554")
    ss("migr_sdk_port", "1")

def floors_state():
    return [dict(f, free=max(0, f["total"] - f["occupied"])) for f in q("SELECT * FROM floors ORDER BY id")]

# ---------------- realtime push (web UI) ----------------
clients, loop = set(), None
async def _bcast(msg):
    for ws in list(clients):
        try: await ws.send_text(msg)
        except Exception: clients.discard(ws)
def broadcast(payload):
    if loop: asyncio.run_coroutine_threadsafe(_bcast(json.dumps(payload)), loop)

# ---------------- ESP32 LED signs ----------------
dev_conns = {}            # device id -> WebSocket ที่ต่ออยู่ (ใช้ใน event loop เท่านั้น)
dev_rt = {}               # device id -> ข้อมูลสด rssi / uptime / rtt / ค่าที่แสดง
pings, ping_seq = {}, itertools.count(1)

def display_text(d, fmap):
    f = fmap.get(d["floor_id"])
    if not f: return "----"
    if d["mode"] == "occupied": return str(min(f["occupied"], 9999)).rjust(4)
    if f["free"] == 0 and d["full_text"]: return "FULL"
    return str(min(f["free"], 9999)).rjust(4)

async def dev_send(did, obj):
    ws = dev_conns.get(did)
    if not ws: return False
    try: await ws.send_text(json.dumps(obj)); return True
    except Exception: return False

async def _push_devices(floor_ids=None, ids=None):
    fmap = {f["id"]: f for f in floors_state()}
    for d in q("SELECT * FROM devices"):
        if d["id"] not in dev_conns: continue
        if ids is not None and d["id"] not in ids: continue
        if floor_ids is not None and d["floor_id"] not in floor_ids: continue
        text = display_text(d, fmap)
        if await dev_send(d["id"], {"type": "show", "text": text, "brightness": d["brightness"]}):
            dev_rt.setdefault(d["id"], {})["text"] = text

def push_devices(floor_ids=None, ids=None):
    if loop: asyncio.run_coroutine_threadsafe(_push_devices(floor_ids, ids), loop)

def devices_state():
    out = []
    for d in q("SELECT * FROM devices ORDER BY floor_id IS NULL DESC, floor_id, name"):
        rt = dev_rt.get(d["id"], {})
        d.update(online=d["id"] in dev_conns, rssi=rt.get("rssi"), uptime=rt.get("uptime"),
                 rtt=rt.get("rtt"), text=rt.get("text"), heap=rt.get("heap"))
        out.append(d)
    return out

# ---------------- Telegram ----------------
def esc(s): return html.escape(str(s), quote=False)
def hms(ts): return time.strftime("%H:%M:%S", time.localtime(ts))

def floor_line(f):
    free, tot = f["free"], f["total"]
    icon = "🔴" if free == 0 else "🟡" if free <= max(3, tot * 0.1) else "🟢"
    st = "<b>เต็ม</b>" if free == 0 else f"ว่าง <b>{free}</b>"
    return f"{icon} <b>{esc(f['name'])}</b> · {st}  <i>(จอด {f['occupied']}/{tot})</i>"

def status_block(fs):
    tf, tt = sum(f["free"] for f in fs), sum(f["total"] for f in fs)
    return "\n".join(floor_line(f) for f in fs) + f"\n━━━━━━━━━━━━━━\n🅿️ รวมว่าง <b>{tf}</b> / {tt} คัน"

def change_text(e, names):
    return ", ".join(f"{esc(names.get(c['floor_id'], '?'))} {'เข้า +' if c['delta'] > 0 else 'ออก −'}{abs(c['delta'])}"
                     for c in e["changes"])

def fmt_event(evs, fs):
    names = {f["id"]: f["name"] for f in fs}
    head = "🚗 <b>มีรถเคลื่อนที่</b>" if len(evs) == 1 else f"🚗 <b>มีรถเคลื่อนที่ {len(evs)} ครั้ง</b>"
    lines = [head]
    for e in evs[-10:]:
        tag = " <i>[ทดสอบ]</i>" if e.get("source") == "sim" else ""
        lines.append(f"• {hms(e['ts'])} 📷 {esc(e['camera'])}{tag}\n   → {change_text(e, names)}")
    if len(evs) > 10: lines.append(f"… และอีก {len(evs) - 10} เหตุการณ์")
    return "\n".join(lines) + "\n\n📊 <b>สถานะทุกชั้น</b>\n" + status_block(fs)

def fmt_live(fs, last=None, title="📊 <b>สถานะที่จอดแบบเรียลไทม์</b>"):
    s = f"{title}\n\n{status_block(fs)}\n\n🕒 อัปเดต {time.strftime('%d/%m/%Y %H:%M:%S')}"
    if last:
        names = {f["id"]: f["name"] for f in fs}
        s += f"\n📷 ล่าสุด: {esc(last['camera'])} → {change_text(last, names)}"
    return s

class Telegram(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.qu, self.chats, self.last_event = queue.Queue(), {}, None

    @staticmethod
    def chat_ids():
        return [c.strip() for c in gs("tg_chat").replace(";", ",").split(",") if c.strip()]

    def active(self):
        return gs("tg_enabled") == "1" and gs("tg_token") and self.chat_ids()

    def notify(self, ev):
        if self.active(): self.qu.put(ev)

    def call(self, method, params, token=None, timeout=10):
        token = token or gs("tg_token")
        for _ in range(3):
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                         data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r: res = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try: res = json.loads(e.read())
                except Exception: res = {"ok": False, "description": f"HTTP {e.code}"}
            except Exception as e:
                res = {"ok": False, "description": f"เชื่อมต่อ Telegram ไม่ได้ ({e})"}
            if res.get("error_code") != 429: return res
            time.sleep(min(30, res.get("parameters", {}).get("retry_after", 3)))
        return res

    def send(self, chat, text, silent=None):
        return self.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                         "disable_notification": gs("tg_silent") == "1" if silent is None else silent})

    def live(self, chat, fs):
        ids = json.loads(gs("tg_live") or "{}")
        text, mid = fmt_live(fs, self.last_event), ids.get(str(chat))
        if mid:
            r = self.call("editMessageText", {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML"})
            if r.get("ok") or "not modified" in r.get("description", ""): return r
        r = self.send(chat, text, silent=True)
        if r.get("ok"):
            ids[str(chat)] = r["result"]["message_id"]; ss("tg_live", json.dumps(ids))
            self.call("pinChatMessage", {"chat_id": chat, "message_id": ids[str(chat)], "disable_notification": True})
        return r

    def run(self):
        threading.Thread(target=self.poll, daemon=True).start()
        while True:
            evs = [self.qu.get()]
            end = time.time() + max(0.0, float(gs("tg_merge") or 0))   # รวมเหตุการณ์ที่เกิดติดกันเป็นข้อความเดียว
            while (rem := end - time.time()) > 0:
                try: evs.append(self.qu.get(timeout=rem))
                except queue.Empty: break
            while not self.qu.empty(): evs.append(self.qu.get_nowait())
            try: self.deliver(evs)
            except Exception as e: print("telegram:", e)

    def deliver(self, evs):
        if not self.active(): return
        real = [e for e in evs if e.get("kind") != "manual"]
        if real: self.last_event = real[-1]
        mode, fs = gs("tg_mode"), floors_state()
        for chat in self.chat_ids():
            if mode in ("event", "both") and real: self.send(chat, fmt_event(real, fs))
            if mode in ("live", "both"): self.live(chat, fs)

    def poll(self):   # รับคำสั่ง /status และจำห้องแชทที่คุยกับบอท (ใช้หา Chat ID)
        off, tok = 0, None
        while True:
            t = gs("tg_token")
            if not t: time.sleep(3); continue
            if t != tok: tok, off, self.chats = t, 0, {}
            r = self.call("getUpdates", {"offset": off, "timeout": 25,
                                         "allowed_updates": ["message", "channel_post", "my_chat_member"]}, timeout=35)
            if not r.get("ok"): time.sleep(5); continue
            for u in r["result"]:
                off = u["update_id"] + 1
                m = u.get("message") or u.get("channel_post") or {}
                ch = m.get("chat") or (u.get("my_chat_member") or {}).get("chat")
                if not ch: continue
                cid = str(ch["id"])
                self.chats[cid] = {"id": cid, "type": ch.get("type"), "name": ch.get("title") or
                                   " ".join(filter(None, [ch.get("first_name"), ch.get("last_name")])) or ch.get("username") or ""}
                txt = (m.get("text") or "").strip().lower()
                if txt.startswith(("/status", "สถานะ")) and cid in self.chat_ids():
                    self.send(cid, fmt_live(floors_state(), self.last_event, title="📊 <b>สถานะที่จอดตอนนี้</b>"), silent=False)
                elif txt.startswith(("/start", "/id", "/status")):
                    self.send(cid, f"Chat ID ของห้องนี้คือ <code>{cid}</code>\nนำไปใส่ในหน้าตั้งค่า Telegram ของระบบ Parking Guidance", silent=False)

telegram = Telegram()

# ---------------- counting ----------------
def apply_count(cam, direction, cls, source="ai"):
    rules = q("SELECT floor_id, delta FROM camera_rules WHERE camera_id=? ORDER BY id", (cam["id"],))
    sign, ts, changes = (1 if direction == "AB" else -1), time.time(), []   # B→A = กลับเครื่องหมายของ A→B
    with db_lock:
        for r in rules:
            row = db.execute("SELECT occupied FROM floors WHERE id=?", (r["floor_id"],)).fetchone()
            if not row: continue
            delta = r["delta"] * sign
            occ = max(0, row["occupied"] + delta)
            db.execute("UPDATE floors SET occupied=? WHERE id=?", (occ, r["floor_id"]))
            db.execute("INSERT INTO events(ts,camera_id,floor_id,direction,delta,cls,occupied) VALUES(?,?,?,?,?,?,?)",
                       (ts, cam["id"], r["floor_id"], direction, delta, cls, occ))
            changes.append({"floor_id": r["floor_id"], "delta": delta, "occupied": occ})
        db.commit()
    if not changes: return
    ev = {"type": "count", "camera_id": cam["id"], "camera": cam["name"], "direction": direction, "cls": cls,
          "ts": ts, "source": source, "changes": changes}
    broadcast(dict(ev, floors=floors_state()))
    push_devices({c["floor_id"] for c in changes})
    telegram.notify(ev)

# ---------------- camera worker ----------------
class Worker(threading.Thread):
    """กล้อง 1 ตัว = 3 เธรดแยกกัน ไม่รอกัน
       reader : อ่านภาพจากกล้องตลอด เก็บแค่เฟรมล่าสุด (ไม่มีคิวค้าง = ไม่หน่วง)
       AI     : (เธรดหลัก) หยิบเฟรมล่าสุดไปตรวจจับ/นับ ตามความเร็วที่เครื่องทำได้
       view   : วาดกรอบล่าสุดทับภาพสด แล้วส่งขึ้นเว็บที่ VIEW_FPS คงที่"""
    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam, self.stop_flag = cam, False
        self.raw, self.raw_id = None, 0               # เฟรมล่าสุดจากกล้อง
        self.frame, self.frame_id = None, 0           # JPEG ล่าสุดสำหรับเว็บ
        self.new_frame = threading.Condition()
        self.dets = []                                # กรอบรถล่าสุดจาก AI
        self.status, self.ai, self.fps, self.cam_fps, self.side = "starting", "loading", 0.0, 0.0, {}

    def open_capture(self):
        """กล้อง Hikvision ใช้ HCNetSDK (port 8000) ถ้ากรอก RTSP URL เองจะใช้ OpenCV แทน"""
        c = self.cam
        if c["rtsp_url"]:
            cap = cv2.VideoCapture(c["rtsp_url"], cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap
        from hik_capture import HikCapture
        return HikCapture(c["ip"], c["port"], c["username"], c["password"], c["channel"])

    def reader(self):
        while not self.stop_flag:
            self.status = "starting" if self.status == "starting" else "reconnecting"
            try: cap = self.open_capture()
            except Exception as e:
                self.status = f"error: {e}"; time.sleep(5); continue
            if not cap.isOpened():
                self.status = "offline"; time.sleep(3); continue
            n, t0 = 0, time.time()
            while not self.stop_flag:
                ok, frame = cap.read()
                if not ok:
                    if getattr(cap, "auto_reconnect", False):   # HikCapture reconnect เอง แค่รายงานสถานะ
                        self.status, self.cam_fps = cap.status, 0.0
                        continue
                    break
                self.status = "online"
                self.raw, self.raw_id = frame, self.raw_id + 1
                n += 1
                if (now := time.time()) - t0 >= 2: self.cam_fps, n, t0 = n / (now - t0), 0, now
            cap.release()
            self.cam_fps = 0.0
            if not self.stop_flag: self.status = "reconnecting"; time.sleep(2)

    def viewer(self):
        last_id = 0
        while not self.stop_flag:
            t0 = time.time()
            frame, fid = self.raw, self.raw_id
            if frame is not None and fid != last_id:
                last_id = fid
                c, (h, w) = self.cam, frame.shape[:2]
                out = frame.copy()
                cv2.line(out, (int(c["x1"] * w), int(c["y1"] * h)), (int(c["x2"] * w), int(c["y2"] * h)), (0, 215, 255), 2)
                for bx1, by1, bx2, by2, label in self.dets:
                    cv2.rectangle(out, (int(bx1), int(by1)), (int(bx2), int(by2)), (80, 220, 80), 2)
                    cv2.putText(out, label, (int(bx1), int(by1) - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 220, 80), 2)
                if w > VIEW_W: out = cv2.resize(out, (VIEW_W, int(h * VIEW_W / w)), interpolation=cv2.INTER_AREA)
                jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes()
                with self.new_frame:
                    self.frame, self.frame_id = jpg, self.frame_id + 1
                    self.new_frame.notify_all()
            time.sleep(max(0.0, 1.0 / VIEW_FPS - (time.time() - t0)))

    def run(self):
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.viewer, daemon=True).start()   # ภาพสดขึ้นได้แม้ AI ยังโหลดไม่เสร็จ
        try:
            from ultralytics import YOLO
            model = YOLO(MODEL)
        except Exception as e:
            self.ai = f"error: AI/model load failed ({type(e).__name__}) - pip install ultralytics / ต้องใช้เน็ตโหลดโมเดลครั้งแรก"
            return
        self.ai, last_id, last, n, t0 = "ok", 0, 0.0, 0, time.time()
        while not self.stop_flag:
            frame, fid, now = self.raw, self.raw_id, time.time()
            if frame is None or fid == last_id or now - last < 1.0 / INFER_FPS:
                time.sleep(0.005); continue
            last_id, last = fid, now
            try: self.process(model, frame); self.ai = "ok"
            except Exception as e: self.ai = f"error: {e}"
            n += 1
            if now - t0 >= 2: self.fps, n, t0 = n / (now - t0), 0, now

    def process(self, model, frame):
        c = self.cam
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = c["x1"] * w, c["y1"] * h, c["x2"] * w, c["y2"] * h
        L = max(1.0, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
        margin = c["margin"] * w
        res = model.track(frame, persist=True, classes=list(CLASSES), conf=c["conf"], imgsz=IMGSZ,
                          verbose=False, tracker="bytetrack.yaml")[0]
        dets = []
        if res.boxes is not None and res.boxes.id is not None:
            boxes = res.boxes.xyxy.cpu().numpy()
            ids = res.boxes.id.int().cpu().tolist()
            cls = res.boxes.cls.int().cpu().tolist()
            for (bx1, by1, bx2, by2), tid, cl in zip(boxes, ids, cls):
                cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
                d = ((x2 - x1) * (cy - y1) - (y2 - y1) * (cx - x1)) / L   # ระยะจากเส้น (มีเครื่องหมาย)
                sign = 1 if d > margin else -1 if d < -margin else 0       # โซนกันสั่น (hysteresis)
                if sign:
                    prev = self.side.get(tid)
                    if prev is not None and prev != sign:
                        direction = "AB" if sign > 0 else "BA"
                        if c["mode"] in ("both", direction):
                            apply_count(c, direction, CLASSES[cl])
                    self.side[tid] = sign
                dets.append((bx1, by1, bx2, by2, f"{CLASSES[cl]} #{tid}"))
        self.dets = dets
        if len(self.side) > 3000: self.side.clear()

workers = {}
def stop_worker(cid):
    w = workers.pop(cid, None)
    if w: w.stop_flag = True
def start_worker(cid):
    stop_worker(cid)
    cam = q("SELECT * FROM cameras WHERE id=?", (cid,), one=True)
    if cam and cam["enabled"]:
        workers[cid] = Worker(cam); workers[cid].start()

# ---------------- API ----------------
@asynccontextmanager
async def lifespan(app):
    global loop
    loop = asyncio.get_running_loop()
    telegram.start()
    for c in q("SELECT id FROM cameras"): start_worker(c["id"])
    yield
    for cid in list(workers): stop_worker(cid)

app = FastAPI(lifespan=lifespan)

class FloorIn(BaseModel):
    name: str
    total: int = 20
class OccIn(BaseModel):
    occupied: int
class RuleIn(BaseModel):
    floor_id: int
    delta: int = 1
class CamIn(BaseModel):
    name: str
    ip: str = ""
    port: int = 8000
    username: str = "admin"
    password: str = ""
    channel: str = "102"
    rtsp_url: str = ""
    floor_id: int | None = None
    x1: float = 0.1
    y1: float = 0.5
    x2: float = 0.9
    y2: float = 0.5
    mode: str = "both"
    ab_delta: int = 1
    conf: float = 0.4
    margin: float = 0.02
    enabled: bool = True
    rules: list[RuleIn] = []
class SimIn(BaseModel):
    direction: str = "AB"
class DevIn(BaseModel):
    name: str
    floor_id: int | None = None
    mode: str = "free"
    brightness: int = 12
    full_text: bool = True
class DevTestIn(BaseModel):
    text: str = "8888"
    seconds: int = 5
class FwIn(BaseModel):
    ssid: str = ""
    password: str = ""

def push_state(manual=True):
    broadcast({"type": "state", "floors": floors_state()})
    push_devices()
    if manual: telegram.notify({"kind": "manual"})   # อัปเดตข้อความสถานะสดใน Telegram

def cam_rules():
    rules = {}
    for r in q("SELECT * FROM camera_rules ORDER BY id"):
        rules.setdefault(r["camera_id"], []).append({"floor_id": r["floor_id"], "delta": r["delta"]})
    return rules

@app.get("/api/state")
def state():
    cams, rules = q("SELECT * FROM cameras ORDER BY id"), cam_rules()
    for c in cams:
        w = workers.get(c["id"])
        c["status"] = w.status if w else "disabled"
        c["fps"] = round(w.fps, 1) if w else 0          # ความเร็ว AI
        c["cam_fps"] = round(w.cam_fps, 1) if w else 0  # ความเร็วภาพจากกล้อง
        c["ai"] = w.ai if w else ""
        c["rules"] = rules.get(c["id"], [])
    return {"floors": floors_state(), "cameras": cams, "devices": devices_state()}

@app.post("/api/floors")
def add_floor(f: FloorIn):
    q("INSERT INTO floors(name,total) VALUES(?,?)", (f.name, f.total)); push_state(False); return {"ok": True}
@app.put("/api/floors/{fid}")
def upd_floor(fid: int, f: FloorIn):
    q("UPDATE floors SET name=?,total=? WHERE id=?", (f.name, f.total, fid)); push_state(); return {"ok": True}
@app.post("/api/floors/{fid}/occupied")
def set_occ(fid: int, o: OccIn):
    q("UPDATE floors SET occupied=? WHERE id=?", (max(0, o.occupied), fid)); push_state(); return {"ok": True}
@app.delete("/api/floors/{fid}")
def del_floor(fid: int):
    q("DELETE FROM floors WHERE id=?", (fid,))
    q("DELETE FROM camera_rules WHERE floor_id=?", (fid,))
    q("UPDATE devices SET floor_id=NULL WHERE floor_id=?", (fid,))
    push_state(); return {"ok": True}

def save_rules(cid, rules):
    rows = [(cid, r.floor_id, max(-5, min(5, r.delta))) for r in rules if r.delta]
    with db_lock:
        db.execute("DELETE FROM camera_rules WHERE camera_id=?", (cid,))
        db.executemany("INSERT INTO camera_rules(camera_id,floor_id,delta) VALUES(?,?,?)", rows)
        db.commit()

@app.post("/api/cameras")
def add_cam(c: CamIn):
    d = c.model_dump(exclude={"rules"}); d["enabled"] = int(d["enabled"])
    with db_lock:
        cur = db.execute(f"INSERT INTO cameras({','.join(d)}) VALUES({','.join('?' * len(d))})", list(d.values()))
        db.commit(); cid = cur.lastrowid
    save_rules(cid, c.rules)
    start_worker(cid); return {"id": cid}

@app.put("/api/cameras/{cid}")
def upd_cam(cid: int, c: CamIn):
    d = c.model_dump(exclude={"rules"}); d["enabled"] = int(d["enabled"])
    old = q("SELECT * FROM cameras WHERE id=?", (cid,), one=True)
    if not old: raise HTTPException(404)
    q(f"UPDATE cameras SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [cid])
    save_rules(cid, c.rules)
    new = q("SELECT * FROM cameras WHERE id=?", (cid,), one=True)
    w = workers.get(cid)
    if w and all(old[k] == new[k] for k in ("ip", "port", "username", "password", "channel", "rtsp_url", "enabled")):
        w.cam.update(new)      # แก้เส้น/โหมด/ความไว แบบสดโดยไม่ต้องรีสตาร์ท
    else:
        start_worker(cid)
    return {"ok": True}

@app.delete("/api/cameras/{cid}")
def del_cam(cid: int):
    stop_worker(cid); q("DELETE FROM cameras WHERE id=?", (cid,)); q("DELETE FROM camera_rules WHERE camera_id=?", (cid,))
    return {"ok": True}

@app.post("/api/cameras/{cid}/simulate")   # จำลองรถผ่านเส้น ใช้ทดสอบทั้งระบบ กล้อง -> ชั้น -> ป้าย LED -> Telegram
def simulate(cid: int, s: SimIn):
    cam = q("SELECT * FROM cameras WHERE id=?", (cid,), one=True)
    if not cam: raise HTTPException(404, "ไม่พบกล้อง")
    if not q("SELECT 1 FROM camera_rules WHERE camera_id=?", (cid,), one=True):
        raise HTTPException(400, "กล้องนี้ยังไม่ได้กำหนดชั้นที่ได้รับผล")
    apply_count(cam, "AB" if s.direction == "AB" else "BA", "จำลอง", source="sim")
    return {"ok": True}

@app.get("/api/cameras/{cid}/snapshot")
def snapshot(cid: int):
    w = workers.get(cid)
    if not w or w.raw is None: raise HTTPException(404, "ยังไม่มีภาพจากกล้อง")
    return Response(cv2.imencode(".jpg", w.raw)[1].tobytes(), media_type="image/jpeg")

def mjpeg(cid):
    last = 0
    while True:
        w = workers.get(cid)
        if not w: time.sleep(0.5); continue
        with w.new_frame:   # รอเฟรมใหม่จริง ๆ ส่งทันทีที่มา (ไม่หน่วงตามรอบ sleep)
            if w.frame_id == last: w.new_frame.wait(1.0)
            frame, last = w.frame, w.frame_id
        if frame: yield b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

@app.get("/stream/{cid}")
def stream(cid: int):
    return StreamingResponse(mjpeg(cid), media_type="multipart/x-mixed-replace; boundary=f")

@app.get("/api/events")
def events(limit: int = 50):
    return q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

@app.get("/api/esp/{fid}", response_class=PlainTextResponse)   # แบบเดิม (polling) ยังใช้ได้ ได้เลขว่างเป็นข้อความล้วน
def esp(fid: int):
    f = q("SELECT * FROM floors WHERE id=?", (fid,), one=True)
    if not f: raise HTTPException(404)
    return str(max(0, f["total"] - f["occupied"]))

# ---- ESP32 devices ----
@app.put("/api/devices/{did}")
def upd_dev(did: str, d: DevIn):
    if not q("SELECT 1 FROM devices WHERE id=?", (did,), one=True): raise HTTPException(404, "ไม่พบอุปกรณ์")
    q("UPDATE devices SET name=?,floor_id=?,mode=?,brightness=?,full_text=? WHERE id=?",
      (d.name.strip() or did, d.floor_id, d.mode if d.mode in ("free", "occupied") else "free",
       max(0, min(15, d.brightness)), int(d.full_text), did))
    push_devices(ids={did}); return {"ok": True}

@app.delete("/api/devices/{did}")
async def del_dev(did: str):
    q("DELETE FROM devices WHERE id=?", (did,))
    ws = dev_conns.pop(did, None)
    if ws:
        try: await ws.close()
        except Exception: pass
    return {"ok": True}

@app.post("/api/devices/{did}/ping")
async def dev_ping(did: str):
    if did not in dev_conns: return {"ok": False, "error": "อุปกรณ์ออฟไลน์ (ไม่ได้เชื่อมต่อกับเซิร์ฟเวอร์)"}
    n, fut, t0 = next(ping_seq), asyncio.get_running_loop().create_future(), time.perf_counter()
    pings[(did, n)] = fut
    await dev_send(did, {"type": "ping", "n": n})
    try: t1 = await asyncio.wait_for(fut, 3)
    except asyncio.TimeoutError:
        pings.pop((did, n), None)
        return {"ok": False, "error": "อุปกรณ์ไม่ตอบสนองภายใน 3 วินาที"}
    rtt = round((t1 - t0) * 1000)
    dev_rt.setdefault(did, {})["rtt"] = rtt
    return {"ok": True, "rtt_ms": rtt, **{k: dev_rt[did].get(k) for k in ("rssi", "uptime", "heap")}}

@app.post("/api/devices/{did}/test")
async def dev_test(did: str, t: DevTestIn):
    txt = t.text[:8]
    if not txt or not all(32 <= ord(ch) < 127 for ch in txt):
        raise HTTPException(400, "ป้ายแสดงได้เฉพาะ A–Z, 0–9 และสัญลักษณ์ภาษาอังกฤษ")
    if txt.isdigit(): txt = txt.rjust(4)
    if not await dev_send(did, {"type": "show", "text": txt, "hold": max(1, min(60, t.seconds)) * 1000}):
        raise HTTPException(409, "อุปกรณ์ออฟไลน์")
    return {"ok": True}

@app.post("/api/devices/{did}/identify")
async def dev_identify(did: str):
    if not await dev_send(did, {"type": "identify", "ms": 6000}): raise HTTPException(409, "อุปกรณ์ออฟไลน์")
    return {"ok": True}

@app.websocket("/ws/device")
async def device_ws(ws: WebSocket, id: str = "", token: str = "", fw: str = ""):
    did = re.sub(r"[^A-Za-z0-9_-]", "", id).upper()[:32]
    need = gs("device_token")
    if not did or (need and token != need):
        await ws.close(code=1008); return
    await ws.accept()
    old = dev_conns.get(did)
    if old:
        try: await old.close()
        except Exception: pass
    dev_conns[did] = ws
    ip, now = ws.client.host if ws.client else "", time.time()
    q("INSERT OR IGNORE INTO devices(id,name,created) VALUES(?,?,?)", (did, "ป้าย " + did[-4:], now))
    q("UPDATE devices SET ip=?,fw=?,last_seen=? WHERE id=?", (ip, fw[:16], now, did))
    dev_rt[did] = {"since": now}
    await _push_devices(ids={did})
    broadcast({"type": "devices"})
    try:
        while True:
            try: m = json.loads(await ws.receive_text())
            except (ValueError, TypeError): continue
            t = m.get("type")
            if t == "status":
                dev_rt[did].update({k: m.get(k) for k in ("rssi", "uptime", "heap")})
                q("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), did))
            elif t == "pong":
                fut = pings.pop((did, m.get("n")), None)
                if fut and not fut.done(): fut.set_result(time.perf_counter())
    except Exception:
        pass
    finally:
        if dev_conns.get(did) is ws:
            dev_conns.pop(did, None)
            q("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), did))
            broadcast({"type": "devices"})

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()

@app.post("/api/firmware")   # ดาวน์โหลดเฟิร์มแวร์ที่กรอก WiFi / IP เซิร์ฟเวอร์ / รหัสอุปกรณ์ ให้แล้ว
def firmware(f: FwIn, request: Request):
    with open(os.path.join(BASE, "esp32_display", "esp32_display.ino"), encoding="utf-8") as fh: src = fh.read()
    host = request.url.hostname or ""
    if host in ("", "localhost", "127.0.0.1", "::1"): host = lan_ip()
    cstr = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    for k, v in (("WIFI_SSID", f.ssid), ("WIFI_PASS", f.password), ("SERVER_HOST", host), ("DEVICE_TOKEN", gs("device_token"))):
        if k.startswith("WIFI") and not v: continue
        src = re.sub(rf'({k}\s*=\s*)"[^"]*"', lambda m: m.group(1) + cstr(v), src, count=1)
    src = re.sub(r"(SERVER_PORT\s*=\s*)\d+", lambda m: m.group(1) + str(request.url.port or 80), src, count=1)
    return Response(src, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="esp32_display.ino"'})

# ---- settings / Telegram ----
@app.get("/api/settings")
def get_settings():
    tok = gs("tg_token")
    return {"tg_enabled": gs("tg_enabled") == "1", "tg_token_set": bool(tok), "tg_token_hint": ("••••" + tok[-4:]) if tok else "",
            "tg_chat": gs("tg_chat"), "tg_mode": gs("tg_mode"), "tg_merge": float(gs("tg_merge") or 0),
            "tg_silent": gs("tg_silent") == "1", "device_token": gs("device_token"), "server_ip": lan_ip()}

@app.put("/api/settings")
def put_settings(s: dict):
    for k in ("tg_enabled", "tg_silent"):
        if k in s: ss(k, "1" if s[k] else "0")
    if s.get("tg_token"):
        if s["tg_token"].strip() != gs("tg_token"): ss("tg_live", "{}")
        ss("tg_token", s["tg_token"].strip())
    if s.get("tg_token_clear"): ss("tg_token", "")
    if "tg_chat" in s: ss("tg_chat", str(s["tg_chat"]).strip())
    if s.get("tg_mode") in ("event", "live", "both"): ss("tg_mode", s["tg_mode"])
    if "tg_merge" in s: ss("tg_merge", max(0.0, min(60.0, float(s["tg_merge"] or 0))))
    if "device_token" in s: ss("device_token", re.sub(r"[^A-Za-z0-9_-]", "", str(s["device_token"]))[:32])
    return get_settings()

@app.get("/api/telegram/info")
def tg_info():
    if not gs("tg_token"): return {"ok": False, "error": "ยังไม่ได้ใส่ Bot Token"}
    r = telegram.call("getMe", {})
    if not r.get("ok"): return {"ok": False, "error": r.get("description", "Token ไม่ถูกต้อง")}
    return {"ok": True, "bot": r["result"].get("username"), "chats": list(telegram.chats.values())}

@app.post("/api/telegram/test")
def tg_test():
    if not gs("tg_token"): raise HTTPException(400, "ยังไม่ได้ใส่ Bot Token")
    chats = telegram.chat_ids()
    if not chats: raise HTTPException(400, "ยังไม่ได้ใส่ Chat ID")
    out = []
    for c in chats:
        r = telegram.send(c, fmt_live(floors_state(), title="✅ <b>ทดสอบการเชื่อมต่อ Parking Guidance สำเร็จ</b>"), silent=False)
        out.append({"chat": c, "ok": bool(r.get("ok")), "error": r.get("description", "")})
    return {"results": out}

@app.get("/api/telegram/preview")
def tg_preview():
    fs, cams = floors_state(), q("SELECT id, name FROM cameras ORDER BY id LIMIT 1")
    ch = [{"floor_id": fs[0]["id"], "delta": -1}] + ([{"floor_id": fs[1]["id"], "delta": 1}] if len(fs) > 1 else []) if fs else []
    sample = {"ts": time.time(), "camera": cams[0]["name"] if cams else "ทางขึ้น ชั้น 1→2", "changes": ch}
    return {"event": fmt_event([sample], fs), "live": fmt_live(fs, sample)}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept(); clients.add(ws)
    await ws.send_text(json.dumps({"type": "state", "floors": floors_state()}))
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)

app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True), name="static")
