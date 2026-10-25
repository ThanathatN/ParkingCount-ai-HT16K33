/*
  Parking Guidance - ป้าย LED ชั้นจอดรถ
  ESP32 + 0.54" 14-segment LED HT16K33 Backpack (4 หลัก)

  การต่อสาย (I2C)
    ESP32 VIN (5V) -> VCC      (ไฟเลี้ยง LED)
    ESP32 3V3      -> Vi2c     (แนะนำ 3.3V ให้ระดับสัญญาณตรงกับ ESP32)
    ESP32 GND      -> GND
    ESP32 D21      -> SDA
    ESP32 D22      -> SCL

  ไลบรารีที่ต้องติดตั้ง (Arduino IDE > Library Manager)
    - "WebSockets"          by Markus Sattler   (2.4 ขึ้นไป)
    - "ArduinoJson"         by Benoit Blanchon  (7.x)
    - "Adafruit LED Backpack Library"  (จะถามให้ลง Adafruit GFX + BusIO ด้วย กด Install all)
  Board: "ESP32 Dev Module"

  การทำงาน
    - เฟิร์มแวร์เดียวใช้ได้ทุกป้าย อุปกรณ์ใช้ MAC เป็นรหัสประจำตัว
    - เปิดเครื่องแล้วจะขึ้นในหน้าเว็บ "อุปกรณ์ / ชั้น > ป้าย LED" อัตโนมัติ จากนั้นตั้งชื่อ + เลือกชั้น
    - เชื่อมต่อแบบ WebSocket ค้างไว้ เซิร์ฟเวอร์ส่งค่าทันทีที่มีรถผ่าน (ไม่ต้องวนถาม)
    - จอแสดง  WIFI = กำลังต่อ WiFi,  CONN = กำลังต่อเซิร์ฟเวอร์
              จุดที่หลักขวาสุด = หลุดจากเซิร์ฟเวอร์ (ยังแสดงค่าล่าสุด)
              ---- = หลุดเกิน 60 วินาที หรือยังไม่ได้กำหนดชั้น
*/
#include <WiFi.h>
#include <Wire.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include "Adafruit_LEDBackpack.h"

// ================== ตั้งค่า ==================
const char*    WIFI_SSID    = "YOUR_WIFI";
const char*    WIFI_PASS    = "YOUR_PASSWORD";
const char*    SERVER_HOST  = "192.168.1.40";   // IP เครื่องคอมที่รัน server.py (ไม่ใช่ IP ของ ESP32)
const uint16_t SERVER_PORT  = 8000;

// IP ของ ESP32 ตัวนี้ (ล็อกไว้ ไม่ให้เปลี่ยนตาม DHCP) - ถ้าจะให้ router แจกเอง ตั้ง USE_STATIC_IP = false
const bool USE_STATIC_IP = true;
IPAddress LOCAL_IP(192, 168, 1, 51);
IPAddress GATEWAY (192, 168, 1, 1);
IPAddress SUBNET  (255, 255, 255, 0);
IPAddress DNS1    (192, 168, 1, 1);
const char*    DEVICE_TOKEN = "";               // ต้องตรงกับ "รหัสอุปกรณ์" ในหน้าเว็บ (เว้นว่างถ้าไม่ได้ตั้ง)
const uint8_t  I2C_ADDR     = 0x70;             // ค่าเริ่มต้นของ HT16K33 (บัดกรี A0/A1/A2 เพื่อเปลี่ยน)
const int      PIN_SDA = 21, PIN_SCL = 22;
const int      PIN_ONBOARD_LED = 2;             // ไฟสีฟ้าบนบอร์ด กะพริบทุกครั้งที่ค่าเปลี่ยน
// ============================================

#define FW_VERSION "2.0"
const unsigned long STATUS_MS        = 10000;    // ส่งสถานะ (สัญญาณ WiFi ฯลฯ) ให้เซิร์ฟเวอร์ทุก 10 วิ
const unsigned long OFFLINE_BLANK_MS = 60000;    // หลุดนานเกินนี้ แสดง ----
const unsigned long REBOOT_AFTER_MS  = 300000;   // หลุดนานเกิน 5 นาที รีสตาร์ทตัวเอง

Adafruit_AlphaNum4 disp;
WebSocketsClient   ws;
String deviceId, normalText = "----", holdText, shownKey;
bool   wsUp = false, hasValue = false, blinking = false;
unsigned long holdStart = 0, holdMs = 0, lastOnline = 0, lastStatus = 0, ledOffAt = 0;

// แสดงข้อความ 4 หลัก จุด '.' จะไปติดกับตัวอักษรก่อนหน้า
void render(const String& s, bool offlineDot) {
  char ch[4] = {' ', ' ', ' ', ' '};
  bool dot[4] = {false, false, false, false};
  int n = 0;
  for (size_t i = 0; i < s.length() && n <= 4; i++) {
    if (s[i] == '.' && n > 0 && !dot[n - 1]) { dot[n - 1] = true; continue; }
    if (n == 4) break;
    ch[n++] = s[i];
  }
  if (offlineDot) dot[3] = true;
  for (int i = 0; i < 4; i++) disp.writeDigitAscii(i, ch[i], dot[i]);
  disp.writeDisplay();
  // ใช้ทดสอบได้แม้ยังไม่มีจอ: ดูค่าที่ Serial Monitor (115200) + ไฟฟ้าบนบอร์ดกะพริบ
  Serial.printf("[LED] จอแสดง |%c%c%c%c|%s\n", ch[0], ch[1], ch[2], ch[3], offlineDot ? "  (หลุดจากเซิร์ฟเวอร์)" : "");
  digitalWrite(PIN_ONBOARD_LED, HIGH); ledOffAt = millis() + 150;
}

void sendJson(JsonDocument& d) {
  String out;
  serializeJson(d, out);
  ws.sendTXT(out);
}

void sendStatus() {
  JsonDocument d;
  d["type"] = "status";
  d["rssi"] = WiFi.RSSI();
  d["uptime"] = millis() / 1000;
  d["heap"] = ESP.getFreeHeap();
  sendJson(d);
}

void startHold(const String& text, unsigned long ms) {
  holdText = text; holdStart = millis(); holdMs = ms ? ms : 1;
}

void handleMessage(uint8_t* payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char* type = doc["type"] | "";
  if (strcmp(type, "ping")) Serial.printf("[รับจากเซิร์ฟเวอร์] %.*s\n", (int)len, (const char*)payload);

  if (!strcmp(type, "show")) {                       // ค่าที่ต้องแสดง
    if (doc["brightness"].is<int>()) disp.setBrightness(constrain(doc["brightness"].as<int>(), 0, 15));
    String text = doc["text"] | "----";
    unsigned long hold = doc["hold"] | 0;            // มี hold = ข้อความทดสอบชั่วคราว
    if (hold) startHold(text, hold);
    else { normalText = text; hasValue = true; }
  } else if (!strcmp(type, "ping")) {                // ทดสอบการเชื่อมต่อจากหน้าเว็บ
    JsonDocument r;
    r["type"] = "pong";
    r["n"] = doc["n"];
    sendJson(r);
    sendStatus();
  } else if (!strcmp(type, "identify")) {            // กะพริบรหัส 4 ตัวท้าย ให้รู้ว่าเป็นป้ายไหน
    startHold(deviceId.substring(deviceId.length() - 4), doc["ms"] | 6000);
    disp.blinkRate(HT16K33_BLINK_2HZ);
    blinking = true;
  } else if (!strcmp(type, "reboot")) {
    ESP.restart();
  }
}

void onWsEvent(WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      wsUp = true;
      Serial.println("[WS] connected");
      sendStatus();
      break;
    case WStype_DISCONNECTED:
      if (wsUp) Serial.println("[WS] disconnected");
      wsUp = false;
      break;
    case WStype_TEXT:
      handleMessage(payload, len);
      break;
    default:
      break;
  }
}

void refreshDisplay() {
  unsigned long now = millis();
  bool holding = holdMs && (now - holdStart < holdMs);
  if (!holding && holdMs) {
    holdMs = 0;
    if (blinking) { disp.blinkRate(HT16K33_BLINK_OFF); blinking = false; }
  }
  String text;
  bool dot = false;
  if (holding)                                  text = holdText;
  else if (!hasValue)                           text = WiFi.status() != WL_CONNECTED ? "WIFI" : "CONN";
  else if (wsUp)                                text = normalText;
  else if (now - lastOnline > OFFLINE_BLANK_MS) text = "----";
  else                                        { text = normalText; dot = true; }

  String key = text + (dot ? "*" : "");
  if (key != shownKey) { shownKey = key; render(text, dot); }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_ONBOARD_LED, OUTPUT);
  Wire.begin(PIN_SDA, PIN_SCL);
  if (!disp.begin(I2C_ADDR)) Serial.println("!! ไม่พบจอ HT16K33 - ตรวจสาย SDA/SCL/ไฟเลี้ยง");
  disp.setBrightness(12);
  render("BOOT", false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[13];
  snprintf(buf, sizeof(buf), "%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  deviceId = buf;
  Serial.printf("\nParking LED sign  FW %s  DEVICE ID = %s\n", FW_VERSION, deviceId.c_str());

  if (USE_STATIC_IP && !WiFi.config(LOCAL_IP, GATEWAY, SUBNET, DNS1))
    Serial.println("!! ตั้ง Static IP ไม่สำเร็จ");
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  String path = String("/ws/device?id=") + deviceId + "&fw=" FW_VERSION "&token=" + DEVICE_TOKEN;
  ws.begin(SERVER_HOST, SERVER_PORT, path);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);   // ตรวจสายหลุดภายใน ~20 วิ
  lastOnline = millis();
}

void loop() {
  static bool wifiWasUp = false;
  bool wifiUp = WiFi.status() == WL_CONNECTED;
  if (wifiUp != wifiWasUp) {
    wifiWasUp = wifiUp;
    if (wifiUp) Serial.printf("[WiFi] IP %s  RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    else { Serial.println("[WiFi] lost"); wsUp = false; }
  }
  if (wifiUp) ws.loop();

  unsigned long now = millis();
  if (wsUp) {
    lastOnline = now;
    if (now - lastStatus >= STATUS_MS) { lastStatus = now; sendStatus(); }
  } else if (now - lastOnline > REBOOT_AFTER_MS) {
    Serial.println("offline too long -> restart");
    ESP.restart();
  }
  refreshDisplay();
  if (ledOffAt && (long)(now - ledOffAt) >= 0) { digitalWrite(PIN_ONBOARD_LED, LOW); ledOffAt = 0; }
  delay(5);
}
