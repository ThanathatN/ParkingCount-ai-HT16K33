"""ดึงภาพกล้อง Hikvision ผ่าน HCNetSDK (แทน RTSP)

  NET_DVR_Login_V40 -> NET_DVR_RealPlay_V40 (callback ข้อมูลสตรีม) -> PlayCtrl ถอดรหัส (YV12) -> OpenCV BGR

ใช้แทน cv2.VideoCapture ได้ทันที:
    cap = HikCapture("192.168.1.64", 8000, "admin", "pass", "102")
    ok, frame = cap.read()      # frame = numpy BGR
    cap.release()

โครงสร้างเธรด (ไม่ทำงานหนักใน callback ของ SDK):
    SDK callback ข้อมูลสตรีม   -> คัดลอก bytes ใส่ _packets (queue)
    เธรด feeder                -> ป้อนเข้า PlayCtrl (เป็นเธรดเดียวที่เรียก PlayM4_*)
    PlayCtrl callback ถอดรหัส -> คัดลอก YV12 ใส่ _frames (queue เก็บแค่ล่าสุด)
    read()                     -> แปลง YV12 -> BGR ในเธรดของผู้เรียก
    เธรด supervisor            -> login / เปิดสตรีม / ตรวจหลุด / reconnect อัตโนมัติ

ทดสอบเดี่ยว:  venv\\Scripts\\python hik_capture.py <ip> [port] [user] [password] [channel]
"""
import atexit, ctypes as C, itertools, os, queue, struct, sys, threading, time
from ctypes import wintypes as W

import cv2
import numpy as np

if sys.platform != "win32":
    raise ImportError("hik_capture ใช้ได้เฉพาะ Windows")
if struct.calcsize("P") != 8:
    raise ImportError("ต้องใช้ Python 64-bit ให้ตรงกับ HCNetSDK x64")

# path คำนวณจากตำแหน่งไฟล์นี้ ไม่ขึ้นกับ current directory (รันผ่าน .bat ได้)
BASE = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(BASE, "hiksdk", "lib")
COM_DIR = os.path.join(LIB_DIR, "HCNetSDKCom")

STALL_SEC = 8.0        # ไม่มีข้อมูลจากกล้องนานเกินนี้ = หลุด -> reconnect
RETRY_SEC = 3.0        # รอก่อนลองเชื่อมต่อใหม่
PLAY_BUF = 2 * 1024 * 1024

# ---------------- types (ตาม HCNetSDK.h / plaympeg4.h) ----------------
BYTE, WORD, DWORD, LONG, BOOL = C.c_ubyte, C.c_ushort, C.c_uint32, C.c_int32, C.c_int32

SERIALNO_LEN = 48
NET_DVR_DEV_ADDRESS_MAX_LEN = 129
NET_DVR_LOGIN_USERNAME_MAX_LEN = 64
NET_DVR_LOGIN_PASSWD_MAX_LEN = 64
STREAM_ID_LEN = 32
NET_SDK_MAX_FILE_PATH = 256

NET_SDK_INIT_CFG_SDK_PATH = 2
NET_SDK_INIT_CFG_LIBEAY_PATH = 3
NET_SDK_INIT_CFG_SSLEAY_PATH = 4
NET_DVR_SYSHEAD, NET_DVR_STREAMDATA = 1, 2
EXCEPTION_PREVIEW = 0x8003
EXCEPTION_PREVIEW_RECONNECT_CLOSED = 0x8045
STREAME_REALTIME = 0
T_YV12 = 3

class NET_DVR_DEVICEINFO_V30(C.Structure):
    _fields_ = [("sSerialNumber", BYTE * SERIALNO_LEN),
                ("byAlarmInPortNum", BYTE), ("byAlarmOutPortNum", BYTE), ("byDiskNum", BYTE), ("byDVRType", BYTE),
                ("byChanNum", BYTE), ("byStartChan", BYTE), ("byAudioChanNum", BYTE), ("byIPChanNum", BYTE),
                ("byZeroChanNum", BYTE), ("byMainProto", BYTE), ("bySubProto", BYTE), ("bySupport", BYTE),
                ("bySupport1", BYTE), ("bySupport2", BYTE), ("wDevType", WORD), ("bySupport3", BYTE),
                ("byMultiStreamProto", BYTE), ("byStartDChan", BYTE), ("byStartDTalkChan", BYTE),
                ("byHighDChanNum", BYTE), ("bySupport4", BYTE), ("byLanguageType", BYTE),
                ("byVoiceInChanNum", BYTE), ("byStartVoiceInChanNo", BYTE), ("bySupport5", BYTE),
                ("bySupport6", BYTE), ("byMirrorChanNum", BYTE), ("wStartMirrorChanNo", WORD),
                ("bySupport7", BYTE), ("byRes2", BYTE)]

class NET_DVR_DEVICEINFO_V40(C.Structure):
    _fields_ = [("struDeviceV30", NET_DVR_DEVICEINFO_V30),
                ("bySupportLock", BYTE), ("byRetryLoginTime", BYTE), ("byPasswordLevel", BYTE), ("byProxyType", BYTE),
                ("dwSurplusLockTime", DWORD), ("byCharEncodeType", BYTE), ("bySupportDev5", BYTE),
                ("bySupport", BYTE), ("byLoginMode", BYTE), ("dwOEMCode", DWORD), ("iResidualValidity", C.c_int),
                ("byResidualValidity", BYTE), ("bySingleStartDTalkChan", BYTE), ("bySingleDTalkChanNums", BYTE),
                ("byPassWordResetLevel", BYTE), ("bySupportStreamEncrypt", BYTE), ("byMarketType", BYTE),
                ("byTLSCap", BYTE), ("byChildManage", BYTE), ("byPlaybackNewPosCap", BYTE),
                ("bySecondaryAuth", BYTE), ("byHttpsResult", BYTE), ("byRes2", BYTE * 233)]

fLoginResultCallBack = C.WINFUNCTYPE(None, LONG, DWORD, C.POINTER(NET_DVR_DEVICEINFO_V30), C.c_void_p)
fLoginResultCallBack_V40 = C.WINFUNCTYPE(None, LONG, DWORD, C.POINTER(NET_DVR_DEVICEINFO_V40), C.c_void_p)

class NET_DVR_USER_LOGIN_INFO(C.Structure):
    _fields_ = [("sDeviceAddress", C.c_char * NET_DVR_DEV_ADDRESS_MAX_LEN), ("byUseTransport", BYTE), ("wPort", WORD),
                ("sUserName", C.c_char * NET_DVR_LOGIN_USERNAME_MAX_LEN),
                ("sPassword", C.c_char * NET_DVR_LOGIN_PASSWD_MAX_LEN),
                ("cbLoginResult", fLoginResultCallBack), ("pUser", C.c_void_p), ("bUseAsynLogin", BOOL),
                ("byProxyType", BYTE), ("byUseUTCTime", BYTE), ("byLoginMode", BYTE), ("byHttps", BYTE),
                ("iProxyID", LONG), ("byVerifyMode", BYTE), ("byRes1", BYTE * 3),
                ("cbLoginResultV40", fLoginResultCallBack_V40),     # OS_WINDOWS64: ไม่มี byRes2[3]
                ("byRes3", BYTE * 109)]

class NET_DVR_PREVIEWINFO(C.Structure):
    _fields_ = [("lChannel", LONG), ("dwStreamType", DWORD), ("dwLinkMode", DWORD), ("hPlayWnd", W.HWND),
                ("bBlocked", DWORD), ("bPassbackRecord", DWORD), ("byPreviewMode", BYTE),
                ("byStreamID", BYTE * STREAM_ID_LEN), ("byProtoType", BYTE), ("byRes1", BYTE),
                ("byVideoCodingType", BYTE), ("dwDisplayBufNum", DWORD), ("byNPQMode", BYTE),
                ("byRecvMetaData", BYTE), ("byDataType", BYTE), ("byReconnect", BYTE), ("byRes", BYTE * 212)]

class NET_DVR_LOCAL_SDK_PATH(C.Structure):
    _fields_ = [("sPath", C.c_char * NET_SDK_MAX_FILE_PATH), ("byRes", BYTE * 128)]

class FRAME_INFO(C.Structure):          # long บน Windows = 32 บิต
    _fields_ = [("nWidth", C.c_long), ("nHeight", C.c_long), ("nStamp", C.c_long), ("nType", C.c_long),
                ("nFrameRate", C.c_long), ("dwFrameNum", DWORD)]

# header ระบุ offset นี้ไว้ชัดเจน ถ้าไม่ตรง SDK จะอ่าน pointer ผิดที่แล้วแครช
assert NET_DVR_USER_LOGIN_INFO.cbLoginResultV40.offset == 296, "NET_DVR_USER_LOGIN_INFO layout ไม่ตรง header"

REALDATACALLBACK = C.WINFUNCTYPE(None, LONG, DWORD, C.POINTER(BYTE), DWORD, C.c_void_p)
EXCEPTIONCALLBACK = C.WINFUNCTYPE(None, DWORD, LONG, LONG, C.c_void_p)
DECCALLBACK = C.WINFUNCTYPE(None, C.c_long, C.POINTER(C.c_char), C.c_long, C.POINTER(FRAME_INFO), C.c_long, C.c_long)

ERRORS = {1: "ชื่อผู้ใช้หรือรหัสผ่านผิด", 2: "สิทธิ์ผู้ใช้ไม่พอ", 3: "SDK ยังไม่ได้ Init", 4: "หมายเลขช่อง (channel) ผิด",
          5: "จำนวนผู้เชื่อมต่อกล้องเต็ม", 7: "เชื่อมต่อกล้องไม่ได้ (ตรวจ IP / port 8000 / เครือข่าย)",
          8: "ส่งข้อมูลไปกล้องไม่ได้", 9: "รับข้อมูลจากกล้องไม่ได้", 10: "กล้องตอบช้าเกินกำหนด",
          23: "กล้องไม่รองรับคำสั่งนี้", 46: "เกินจำนวนสูงสุดที่ SDK รองรับ", 47: "ไม่มีชื่อผู้ใช้นี้ในกล้อง",
          64: "โหลด PlayCtrl ไม่ได้", 153: "บัญชีถูกล็อก (ใส่รหัสผิดหลายครั้ง) รอสักครู่"}

# ---------------- โหลด DLL + init SDK (ครั้งเดียวต่อโปรเซส) ----------------
_sdk = _play = None
_sdk_lock = threading.Lock()
_sdk_refs = 0
_keep = []                                  # buffer ที่ส่งให้ SDK ต้องอยู่ตลอด
_instances, _by_user, _by_port = {}, {}, {}
_reg_lock = threading.Lock()
_ids = itertools.count(1)                   # pUser = id (เริ่ม 1 เพราะ 0 จะกลายเป็น NULL)

def _load():
    global _sdk, _play
    if _sdk: return
    if not os.path.isfile(os.path.join(LIB_DIR, "HCNetSDK.dll")):
        raise FileNotFoundError(f"ไม่พบ HCNetSDK.dll ใน {LIB_DIR}")
    for d in (LIB_DIR, COM_DIR):
        os.add_dll_directory(d)
    os.environ["PATH"] = LIB_DIR + os.pathsep + COM_DIR + os.pathsep + os.environ.get("PATH", "")  # SDK โหลดโมดูลย่อยเองด้วย LoadLibrary
    sdk = C.WinDLL(os.path.join(LIB_DIR, "HCNetSDK.dll"))
    play = C.WinDLL(os.path.join(LIB_DIR, "PlayCtrl.dll"))
    for name, res, args in [
        ("NET_DVR_SetSDKInitCfg", BOOL, [C.c_int, C.c_void_p]),
        ("NET_DVR_Init", BOOL, []), ("NET_DVR_Cleanup", BOOL, []),
        ("NET_DVR_SetConnectTime", BOOL, [DWORD, DWORD]), ("NET_DVR_SetReconnect", BOOL, [DWORD, BOOL]),
        ("NET_DVR_SetExceptionCallBack_V30", BOOL, [C.c_uint, C.c_void_p, EXCEPTIONCALLBACK, C.c_void_p]),
        ("NET_DVR_Login_V40", LONG, [C.POINTER(NET_DVR_USER_LOGIN_INFO), C.POINTER(NET_DVR_DEVICEINFO_V40)]),
        ("NET_DVR_Logout", BOOL, [LONG]),
        ("NET_DVR_RealPlay_V40", LONG, [LONG, C.POINTER(NET_DVR_PREVIEWINFO), REALDATACALLBACK, C.c_void_p]),
        ("NET_DVR_StopRealPlay", BOOL, [LONG]), ("NET_DVR_GetLastError", DWORD, []),
    ]:
        f = getattr(sdk, name); f.restype, f.argtypes = res, args
    for name, res, args in [
        ("PlayM4_GetPort", BOOL, [C.POINTER(LONG)]), ("PlayM4_FreePort", BOOL, [LONG]),
        ("PlayM4_SetStreamOpenMode", BOOL, [LONG, DWORD]),
        ("PlayM4_OpenStream", BOOL, [LONG, C.c_void_p, DWORD, DWORD]), ("PlayM4_CloseStream", BOOL, [LONG]),
        ("PlayM4_SetDecCallBack", BOOL, [LONG, DECCALLBACK]),
        ("PlayM4_Play", BOOL, [LONG, W.HWND]), ("PlayM4_Stop", BOOL, [LONG]),
        ("PlayM4_InputData", BOOL, [LONG, C.c_void_p, DWORD]), ("PlayM4_GetLastError", DWORD, [LONG]),
    ]:
        f = getattr(play, name); f.restype, f.argtypes = res, args
    _sdk, _play = sdk, play

def _err(code=None):
    code = _sdk.NET_DVR_GetLastError() if code is None else code
    return f"{ERRORS.get(code, 'ข้อผิดพลาด SDK')} (รหัส {code})"

def _sdk_acquire():
    global _sdk_refs
    with _sdk_lock:
        _load()
        if _sdk_refs == 0:
            p = NET_DVR_LOCAL_SDK_PATH(); p.sPath = LIB_DIR.encode("mbcs")
            crypto = C.create_string_buffer(os.path.join(LIB_DIR, "libcrypto-3-x64.dll").encode("mbcs"))
            ssl = C.create_string_buffer(os.path.join(LIB_DIR, "libssl-3-x64.dll").encode("mbcs"))
            _keep[:] = [p, crypto, ssl]
            # ต้องตั้งก่อน NET_DVR_Init เพื่อให้ SDK หา HCNetSDKCom / OpenSSL จากโฟลเดอร์นี้
            _sdk.NET_DVR_SetSDKInitCfg(NET_SDK_INIT_CFG_SDK_PATH, C.byref(p))
            _sdk.NET_DVR_SetSDKInitCfg(NET_SDK_INIT_CFG_LIBEAY_PATH, C.cast(crypto, C.c_void_p))
            _sdk.NET_DVR_SetSDKInitCfg(NET_SDK_INIT_CFG_SSLEAY_PATH, C.cast(ssl, C.c_void_p))
            if not _sdk.NET_DVR_Init(): raise RuntimeError("NET_DVR_Init ล้มเหลว: " + _err())
            _sdk.NET_DVR_SetConnectTime(3000, 1)
            _sdk.NET_DVR_SetReconnect(10000, 0)      # reconnect เองใน supervisor (คุมได้ทั้ง login + สตรีม)
            _sdk.NET_DVR_SetExceptionCallBack_V30(0, None, _EXC_CB, None)
        _sdk_refs += 1

def _sdk_release():
    global _sdk_refs
    with _sdk_lock:
        if _sdk_refs <= 0: return
        _sdk_refs -= 1
        if _sdk_refs == 0:
            _sdk.NET_DVR_Cleanup()

# ---------------- callbacks (เก็บไว้ระดับโมดูล ไม่ถูก garbage collect) ----------------
def _on_realdata(handle, dtype, pbuf, size, puser):
    cap = _instances.get(puser)
    if cap is None or size <= 0: return
    if dtype in (NET_DVR_SYSHEAD, NET_DVR_STREAMDATA):
        cap._push_packet(dtype, C.string_at(pbuf, size))   # แค่คัดลอกแล้วใส่คิว

def _on_exception(dtype, user_id, handle, puser):
    if dtype in (EXCEPTION_PREVIEW, EXCEPTION_PREVIEW_RECONNECT_CLOSED):
        cap = _by_user.get(user_id)
        if cap: cap._broken.set()

def _on_decoded(port, pbuf, size, pinfo, r1, r2):
    cap = _by_port.get(port)
    if cap is None or not pinfo: return
    fi = pinfo.contents
    if fi.nType == T_YV12 and size > 0:
        cap._push_frame(fi.nWidth, fi.nHeight, C.string_at(pbuf, size))  # แปลงสีทำใน read()

_REAL_CB = REALDATACALLBACK(_on_realdata)
_EXC_CB = EXCEPTIONCALLBACK(_on_exception)
_DEC_CB = DECCALLBACK(_on_decoded)

def parse_channel(ch, info=None):
    """'102' -> (ช่อง 1, sub stream) แบบเดียวกับ RTSP /Streaming/Channels/102
       ถ้าเป็น NVR (ไม่มีช่องอนาล็อก) จะแปลงเป็นช่อง IP ให้อัตโนมัติ"""
    n = int(str(ch).strip() or 101)
    chan, stream = (n // 100, n % 100 - 1) if n >= 100 else (n, 0)
    if info is not None:
        v = info.struDeviceV30
        if v.byChanNum == 0 and (v.byIPChanNum or v.byHighDChanNum):
            chan = v.byStartDChan + chan - 1
    return chan, max(0, stream)

# ---------------- ตัวดึงภาพ ----------------
class HikCapture:
    auto_reconnect = True     # read() คืน False ได้ชั่วคราวระหว่าง reconnect ผู้เรียกไม่ต้องสร้างใหม่

    def __init__(self, ip, port=8000, username="admin", password="", channel="101"):
        self.ip, self.port, self.username, self.password, self.channel = ip, int(port or 8000), username, password, channel
        self.status = "connecting"
        self.device = None
        self._id = next(_ids)
        self._user, self._handle, self._play_port = -1, -1, -1
        self._session = 0
        self._last_data = self._last_frame = 0.0
        self._packets = queue.Queue(maxsize=600)
        self._frames = queue.Queue(maxsize=2)
        self._broken, self._stop = threading.Event(), threading.Event()
        _sdk_acquire()
        with _reg_lock: _instances[self._id] = self
        self._feeder = threading.Thread(target=self._feed_loop, daemon=True, name=f"hik-feed-{ip}")
        self._super = threading.Thread(target=self._supervise, daemon=True, name=f"hik-super-{ip}")
        self._feeder.start(); self._super.start()

    # --- interface แบบ cv2.VideoCapture ---
    def isOpened(self):
        return not self._stop.is_set()

    def read(self, timeout=2.0):
        try: w, h, buf = self._frames.get(timeout=timeout)
        except queue.Empty: return False, None
        try:
            yuv = np.frombuffer(buf, np.uint8, count=w * h * 3 // 2).reshape(h * 3 // 2, w)
            return True, cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_YV12)
        except ValueError:
            return False, None

    def set(self, *_):
        return False

    def release(self):
        if self._stop.is_set(): return
        self._stop.set()
        self._super.join(timeout=5)
        self._teardown()
        self._packets.put(("quit", 0, None))
        self._feeder.join(timeout=5)
        with _reg_lock: _instances.pop(self._id, None)
        _sdk_release()
        self.status = "closed"

    def __del__(self):
        try: self.release()
        except Exception: pass

    # --- เรียกจาก callback: งานเบาเท่านั้น ---
    def _push_packet(self, dtype, data):
        self._last_data = time.time()
        try: self._packets.put_nowait(("data", self._session, (dtype, data)))
        except queue.Full: pass         # feeder ตามไม่ทัน ทิ้งแพ็กเก็ต (PlayCtrl จะรอ key frame ถัดไป)

    def _push_frame(self, w, h, data):
        self._last_frame = time.time()
        while True:
            try: self._frames.put_nowait((w, h, data)); return
            except queue.Full:
                try: self._frames.get_nowait()   # เก็บแค่เฟรมล่าสุด ไม่ให้ภาพหน่วง
                except queue.Empty: pass

    # --- เธรด supervisor: login / เปิดสตรีม / ตรวจหลุด / reconnect ---
    def _supervise(self):
        while not self._stop.is_set():
            if not self._connect():
                self._teardown()
                self._stop.wait(RETRY_SEC); continue
            self._broken.clear()
            self.status = "connecting: เข้าระบบแล้ว รอภาพแรก"
            while not self._stop.is_set():
                self._stop.wait(0.5)
                now = time.time()
                if self._broken.is_set():
                    self.status = "reconnecting: สตรีมหลุด"; break
                if now - self._last_data > STALL_SEC:
                    self.status = f"reconnecting: ไม่มีข้อมูลจากกล้อง {STALL_SEC:.0f} วินาที"; break
                if now - self._last_frame < 3:
                    self.status = "online"
                elif self.status == "online":
                    self.status = "connecting: ได้ข้อมูลแต่ถอดรหัสภาพไม่ได้"
            self._teardown()
            if not self._stop.is_set(): self._stop.wait(1.0)

    def _connect(self):
        self.status = "connecting"
        li, info = NET_DVR_USER_LOGIN_INFO(), NET_DVR_DEVICEINFO_V40()
        li.sDeviceAddress = self.ip.encode()
        li.wPort = self.port
        li.sUserName = self.username.encode()
        li.sPassword = self.password.encode()
        li.bUseAsynLogin = 0
        user = _sdk.NET_DVR_Login_V40(C.byref(li), C.byref(info))
        if user < 0:
            self.status = "offline: login ไม่สำเร็จ - " + _err(); return False
        self._user, self.device = user, info
        with _reg_lock: _by_user[user] = self
        chan, stream = parse_channel(self.channel, info)
        pv = NET_DVR_PREVIEWINFO()
        pv.lChannel, pv.dwStreamType, pv.dwLinkMode = chan, stream, 0   # TCP
        pv.hPlayWnd, pv.bBlocked, pv.dwDisplayBufNum = None, 1, 1
        self._session += 1
        self._last_data = time.time()
        handle = _sdk.NET_DVR_RealPlay_V40(user, C.byref(pv), _REAL_CB, C.c_void_p(self._id))
        if handle < 0:
            self.status = f"offline: เปิดสตรีมช่อง {chan} ไม่สำเร็จ - " + _err(); return False
        self._handle = handle
        return True

    def _teardown(self):
        if self._handle >= 0:
            _sdk.NET_DVR_StopRealPlay(self._handle); self._handle = -1
        if self._user >= 0:
            with _reg_lock: _by_user.pop(self._user, None)
            _sdk.NET_DVR_Logout(self._user); self._user = -1
        self._session += 1                                   # แพ็กเก็ตเก่าที่ค้างในคิวจะถูกทิ้ง
        self._packets.put(("close", self._session, None))    # ให้ feeder ปิด PlayCtrl เอง

    # --- เธรด feeder: เธรดเดียวที่เรียก PlayM4_* ---
    def _feed_loop(self):
        while True:
            try: kind, session, item = self._packets.get(timeout=1.0)
            except queue.Empty: continue
            if kind == "quit":
                self._close_player(); return
            if kind == "close":
                self._close_player(); continue
            if session != self._session: continue
            dtype, data = item
            if dtype == NET_DVR_SYSHEAD:
                self._open_player(data)
            elif self._play_port >= 0:
                for _ in range(20):                               # buffer เต็มชั่วคราว รอสั้น ๆ
                    if _play.PlayM4_InputData(self._play_port, data, len(data)): break
                    time.sleep(0.002)

    def _open_player(self, head):
        self._close_player()
        port = LONG(-1)
        if not _play.PlayM4_GetPort(C.byref(port)):
            self.status = "error: PlayM4_GetPort ล้มเหลว"; return
        p = port.value
        ok = (_play.PlayM4_SetStreamOpenMode(p, STREAME_REALTIME)
              and _play.PlayM4_OpenStream(p, head, len(head), PLAY_BUF)
              and _play.PlayM4_SetDecCallBack(p, _DEC_CB))
        if ok:
            with _reg_lock: _by_port[p] = self
            ok = _play.PlayM4_Play(p, None)                      # ไม่มีหน้าต่าง = ถอดรหัสส่ง callback อย่างเดียว
        if not ok:
            self.status = f"error: PlayCtrl เปิดไม่ได้ (รหัส {_play.PlayM4_GetLastError(p)})"
            with _reg_lock: _by_port.pop(p, None)
            _play.PlayM4_CloseStream(p); _play.PlayM4_FreePort(p); return
        self._play_port = p

    def _close_player(self):
        p, self._play_port = self._play_port, -1
        if p < 0: return
        _play.PlayM4_Stop(p)
        _play.PlayM4_CloseStream(p)
        with _reg_lock: _by_port.pop(p, None)
        _play.PlayM4_FreePort(p)

@atexit.register
def _cleanup_all():
    for cap in list(_instances.values()):
        try: cap.release()
        except Exception: pass

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        _sdk_acquire(); print("โหลด HCNetSDK + PlayCtrl จาก", LIB_DIR, "และ NET_DVR_Init สำเร็จ"); _sdk_release(); sys.exit(0)
    cap = HikCapture(a[0], int(a[1]) if len(a) > 1 else 8000, a[2] if len(a) > 2 else "admin",
                     a[3] if len(a) > 3 else "", a[4] if len(a) > 4 else "102")
    t0, n, frame = time.time(), 0, None
    while time.time() - t0 < 15:
        ok, f = cap.read()
        if ok: n, frame = n + 1, f
        print(f"\r{cap.status:60.60s} เฟรม {n}", end="", flush=True)
    print()
    if frame is not None:
        out = os.path.join(BASE, "hik_test.jpg"); cv2.imwrite(out, frame)
        print(f"ได้ภาพ {frame.shape[1]}x{frame.shape[0]} เฉลี่ย {n / 15:.1f} fps บันทึกที่ {out}")
    cap.release()
