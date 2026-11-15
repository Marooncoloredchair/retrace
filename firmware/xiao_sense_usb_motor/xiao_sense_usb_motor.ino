// XIAO ESP32S3 Sense camera, GY-521 IMU and vibration motor.
// JPEG frames are the only image data. IMU samples are a separate frame type.
// Diagnostics use text frames, so neither text nor IMU bytes sit inside a JPEG.
// USB serial stays the flash port and the fallback. When wifi_config.h has a
// hotspot, the same frames go over TCP to the laptop and 'V' / 'v' come back.
// IMU wiring: VCC 3V3, GND GND, SDA D4/GPIO5, SCL D5/GPIO6.
// Motor wiring: IN D3/GPIO4, VCC 3V3, GND GND.
//
// Motor: D3 is driven LOW before anything else and stays LOW. The single byte
// 'V' from the host starts one 200 ms pulse. A one-shot hardware timer ends the
// pulse, so the off time does not depend on the camera or USB. Further 'V'
// bytes are ignored during a pulse and for 800 ms after it. Motor and reset
// diagnostics go out as type-2 text frames.

#include "esp_camera.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <fcntl.h>
#include <sys/socket.h>
#if __has_include("wifi_config.h")
#include "wifi_config.h"
#define RETRACE_WIFI_COMPILED 1
#else
#define RETRACE_WIFI_COMPILED 0
#define RETRACE_WIFI_SSID ""
#define RETRACE_WIFI_PASSWORD ""
#define RETRACE_HOST ""
#define RETRACE_PORT 8011
#endif

#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13

static const uint8_t FRAME_MAGIC_0 = 0xA5;
static const uint8_t FRAME_MAGIC_1 = 0x5A;
static const uint8_t FRAME_JPEG = 1;
static const uint8_t FRAME_TEXT = 2;
static const uint8_t FRAME_IMU = 3;
static const uint8_t FRAME_IMU_BATCH = 4;  // FIFO batch; kind 3 single samples stay readable
static const size_t MAX_JPEG = 100000;
static const int IMU_SDA = 5;
static const int IMU_SCL = 6;
static const uint8_t IMU_ADDR_LOW = 0x68;
static const uint8_t IMU_ADDR_HIGH = 0x69;
static const uint32_t IMU_PERIOD_MS = 20;
static const uint32_t USB_WRITE_TIMEOUT_MS = 400;
static const int LED_PIN = 21;  // XIAO ESP32S3 user LED, active low
static const uint32_t WIFI_BOOT_MS = 8000;
static const uint32_t WIFI_RETRY_MS = 10000;
static const uint32_t WIFI_CONNECT_MS = 500;

static const int MOTOR_PIN = 4;  // D3
static const uint8_t CMD_VIBRATE = 'V';
static const uint8_t CMD_TICK = 'v';
static const uint8_t CMD_IMU_ONLY = 'i';  // calibration: stop pictures so the IMU is not dropped
static const uint8_t CMD_CAMERA = 'c';
static const uint32_t MOTOR_PULSE_MS = 200;
static const uint32_t MOTOR_REST_MS = 800;
static const uint32_t MOTOR_TICK_PULSE_MS = 80;
static const uint32_t MOTOR_TICK_REST_MS = 220;
// MPU6050: DLPF_CFG 3 → gyro 42 Hz / accel 44 Hz, sample rate 1 kHz / (1+9) = 100 Hz.
static const uint8_t IMU_DLPF_CFG = 0x03;
static const uint8_t IMU_SMPLRT_DIV = 9;
static const uint8_t IMU_ACCEL_CFG = 0x10;  // AFS_SEL 2 → ±8 g
static const uint8_t IMU_GYRO_CFG = 0x10;   // FS_SEL 2 → ±1000 deg/s
static const uint8_t IMU_FIFO_EN_AG = 0x78; // gyro XYZ + accel, no temperature
static const int IMU_FRAME_BYTES = 12;      // ax ay az gx gy gz, big-endian in the FIFO
static const int IMU_BATCH_MAX = 8;  // 8*12 = 96 bytes, under the 128-byte I2C buffer
static const uint32_t MOTOR_OVERRUN_MS = 60;
static const uint32_t STATUS_PERIOD_MS = 1000;
static const uint32_t BOOT_MAGIC = 0x52543031;

// Kept across resets that do not remove power, so a restart loop is countable.
RTC_NOINIT_ATTR static uint32_t bootMagic;
RTC_NOINIT_ATTR static uint32_t bootsSincePowerOn;

static bool cameraReady = false;
static uint32_t captureErrors = 0;
static uint32_t framesSent = 0;
static String sensorLine = "sensor not read";
static bool i2cReady = false;
static bool imuReady = false;
static TwoWire *imuBus = nullptr;
static const char *imuBusName = "none";
static int lastI2cError = 0;
static String cameraError = "camera is not running";
static uint8_t imuAddress = 0;
static uint8_t imuWho = 0;
static uint8_t imuAccelFs = 0;
static uint8_t imuGyroFs = 0;
static String imuLine = "imu not checked";
static uint32_t nextImuMs = 0;
static uint32_t nextImuTextMs = 0;

static esp_reset_reason_t resetReason = ESP_RST_UNKNOWN;
static esp_timer_handle_t motorTimer = nullptr;
static volatile bool motorOn = false;
static volatile bool motorEndPending = false;
static volatile uint32_t motorLastOnUs = 0;
static int64_t motorStartUs = 0;
static uint32_t motorStartMs = 0;
static uint32_t motorPulseMs = MOTOR_PULSE_MS;
static uint32_t motorRestMs = MOTOR_REST_MS;
static uint32_t motorRestUntilMs = 0;
static uint32_t imuSampleCounter = 0;
static uint32_t imuDropped = 0;
static uint32_t lastImuDrainMs = 0;
static bool imuGapPending = false;
static uint16_t imuLastCount = 0;
static uint8_t imuLastOverflow = 0;
static uint32_t motorPulses = 0;
static uint32_t motorIgnored = 0;
static uint32_t nextStatusMs = 0;

enum LinkMode { LINK_USB = 0, LINK_JOINING = 1, LINK_STREAM = 2 };
static LinkMode linkMode = LINK_USB;
static WiFiClient wifiClient;
static bool wifiConfigured = false;
static uint32_t nextWifiTryMs = 0;
static bool flushingStash = false;
static bool cameraHeld = false;
static uint8_t imuStash[40 * 12];
static uint8_t imuStashCount = 0;
static bool imuStashGap = false;
static uint32_t imuStashFirst = 0;
static uint16_t imuStashDropped = 0;
static int jpegQuality = 10;
static int frameHold = 1;
static int framePhase = 0;
static int goodLoops = 0;
static uint32_t lastJpegSendMs = 0;
static uint32_t loopStartedMs = 0;

static uint32_t crc32_ieee(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < length; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) {
      uint32_t mask = -(crc & 1u);
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

static void pullImuStash();
static void flushImuStash();
static void readMotorBytes();
static bool abandonJpeg = false;

// Set when a TCP write ended in the middle of a frame. The caller drops the socket.
static bool streamBroken = false;
static bool usbSending = false;

static bool writeUsb(const uint8_t *data, size_t length) {
  // Small frames are all-or-nothing. A picture is written in buffer-sized
  // pieces so a VGA JPEG can leave over USB. Bytes already handed to the
  // host are finished, and no other frame is inserted in the middle.
  if (!Serial || data == nullptr || length == 0 || usbSending) return false;
  int room = Serial.availableForWrite();
  if (room <= 0) return false;
  if (length <= 400 && room < (int)length) return false;
  usbSending = true;
  size_t offset = 0;
  uint32_t started = millis();
  while (offset < length) {
    room = Serial.availableForWrite();
    if (room <= 0) {
      if (millis() - started > 800) break;
      pullImuStash();
      delay(1);
      continue;
    }
    size_t n = (size_t)room;
    if (n > length - offset) n = length - offset;
    size_t wrote = Serial.write(data + offset, n);
    if (wrote == 0) {
      if (millis() - started > 800) break;
      pullImuStash();
      delay(1);
      continue;
    }
    offset += wrote;
    pullImuStash();
  }
  usbSending = false;
  flushImuStash();
  return offset == length;
}

static bool writeWifi(const uint8_t *data, size_t length) {
  if (!wifiClient.connected() || length == 0) return false;
  // A picture send used to block for tens of seconds, so the pause command
  // never arrived and the motion samples were thrown away.
  if (abandonJpeg && cameraHeld) return false;
  // WiFiClient.write blocks inside the TCP stack while the laptop is quiet.
  // That stall froze the IMU. A non-blocking send either takes the whole
  // frame or leaves the socket untouched.
  int fd = wifiClient.fd();
  if (fd < 0) return false;
  int flags = fcntl(fd, F_GETFL, 0);
  if (flags >= 0 && (flags & O_NONBLOCK) == 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  int wrote = ::send(fd, (const char *)data, length, MSG_DONTWAIT);
  if (wrote == (int)length) return true;
  if (wrote > 0) streamBroken = true;
  return false;
}

static bool writeAll(const uint8_t *data, size_t length) {
  if (linkMode == LINK_STREAM) return writeWifi(data, length);
  // Wi-Fi is configured, so the cable is only there for charging and commands.
  // Framing bytes on that port block the sampler whenever the laptop is not reading.
  if (wifiConfigured) return false;
  return writeUsb(data, length);
}

static bool sendFrame(uint8_t type, const uint8_t *payload, uint32_t length) {
  // One buffer, one write. Three separate writes left a header on the socket
  // when the picture body did not fit, and the laptop then dropped the link.
  size_t total = 8u + (size_t)length + 4u;
  if (payload == nullptr || total > MAX_JPEG + 16) return false;
  uint8_t *buf = (uint8_t *)ps_malloc(total);
  if (buf == nullptr) return false;
  buf[0] = FRAME_MAGIC_0;
  buf[1] = FRAME_MAGIC_1;
  buf[2] = type;
  buf[3] = 0;
  buf[4] = (uint8_t)(length & 0xFF);
  buf[5] = (uint8_t)((length >> 8) & 0xFF);
  buf[6] = (uint8_t)((length >> 16) & 0xFF);
  buf[7] = (uint8_t)((length >> 24) & 0xFF);
  if (length) memcpy(buf + 8, payload, length);
  uint32_t crc = crc32_ieee(payload, length);
  buf[8 + length] = (uint8_t)(crc & 0xFF);
  buf[9 + length] = (uint8_t)((crc >> 8) & 0xFF);
  buf[10 + length] = (uint8_t)((crc >> 16) & 0xFF);
  buf[11 + length] = (uint8_t)((crc >> 24) & 0xFF);
  bool ok = writeAll(buf, total);
  free(buf);
  return ok;
}

static void sendText(const String &text) {
  sendFrame(FRAME_TEXT, reinterpret_cast<const uint8_t *>(text.c_str()), text.length());
}

static const char *resetName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_EXT: return "EXT";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: return "INT_WDT";
    case ESP_RST_TASK_WDT: return "TASK_WDT";
    case ESP_RST_WDT: return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SDIO: return "SDIO";
    case ESP_RST_USB: return "USB";
    case ESP_RST_JTAG: return "JTAG";
    default: return "UNKNOWN";
  }
}

static const char *motorLatch() {
  return digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH";
}

static const char *transportName() {
  if (linkMode == LINK_STREAM) return "wifi";
  if (linkMode == LINK_JOINING) return "joining";
  return "usb";
}

static WiFiUDP imuUdp;
static uint32_t udpSent = 0;
static uint32_t udpFail = 0;
// 0 ok, 1 bus, 2 whoami, 3 reset, 4 config write, 5 readback
static uint8_t imuFail = 1;

static String statusLine() {
  // Once a second: uptime, reset reason, free heap, and RSSI. A falling up_ms means a reboot.
  return String("status boot ") + bootsSincePowerOn + " reset " + resetName(resetReason) +
         " up_ms " + millis() + " heap " + ESP.getFreeHeap() + " motor " + (motorOn ? "on" : "idle") +
         " pulses " + motorPulses + " ignored " + motorIgnored + " d3 " + motorLatch() +
         " imu_drop " + imuDropped + " fifo_count " + imuLastCount + " fifo_oflow " + imuLastOverflow +
         " transport " + transportName() + " jpeg_ms " + lastJpegSendMs +
         " quality " + jpegQuality + " rssi " + (WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0) +
         " udp_tx " + udpSent + " udp_fail " + udpFail +
         " imu_ok " + (imuReady ? 1 : 0) + " imu_fail " + imuFail + " imu_n " + imuSampleCounter;
}

static bool udpReady = false;

// One datagram, one frame. TCP never carries motion while Wi-Fi is configured.
static bool sendUdpFrame(uint8_t kind, const uint8_t *payload, uint32_t length) {
  IPAddress ip;
  if (WiFi.status() != WL_CONNECTED || length > 1200 || !ip.fromString(RETRACE_HOST)) return false;
  if (!udpReady) {
    // A fresh socket per packet was dropping every other datagram and then wedging.
    if (!imuUdp.begin(8013)) return false;
    udpReady = true;
  }
  uint8_t header[8] = {
    FRAME_MAGIC_0, FRAME_MAGIC_1, kind, 0,
    (uint8_t)(length & 0xFF), (uint8_t)((length >> 8) & 0xFF),
    (uint8_t)((length >> 16) & 0xFF), (uint8_t)((length >> 24) & 0xFF),
  };
  uint32_t crc = crc32_ieee(payload, length);
  uint8_t tail[4] = {
    (uint8_t)(crc & 0xFF), (uint8_t)((crc >> 8) & 0xFF),
    (uint8_t)((crc >> 16) & 0xFF), (uint8_t)((crc >> 24) & 0xFF),
  };
  for (int attempt = 0; attempt < 2; attempt++) {
    if (!imuUdp.beginPacket(ip, RETRACE_PORT + 1)) continue;
    imuUdp.write(header, sizeof(header));
    if (length) imuUdp.write(payload, length);
    imuUdp.write(tail, sizeof(tail));
    if (imuUdp.endPacket() == 1) return true;
  }
  return false;
}

static void ledSet(bool on) {
  digitalWrite(LED_PIN, on ? LOW : HIGH);
}

// Serial: one short blink every 2 s. Joining: fast blink. Streaming: two quick blinks, then a pause.
static void ledService() {
  uint32_t now = millis();
  bool on = false;
  if (linkMode == LINK_JOINING) {
    on = ((now / 120) % 2) == 0;
  } else if (linkMode == LINK_STREAM) {
    uint32_t phase = now % 1600;
    on = phase < 50 || (phase >= 160 && phase < 210);
  } else {
    on = (now % 2000) < 50;
  }
  ledSet(on);
}

static void readMotorBytes() {
  for (int budget = 0; budget < 32; budget++) {
    int cmd = -1;
    if (linkMode == LINK_STREAM && wifiClient.connected() && wifiClient.available() > 0) {
      cmd = wifiClient.read();
    } else if (Serial.available() > 0) {
      cmd = Serial.read();
    } else {
      break;
    }
    bool accepted = false;
    if (cmd == CMD_VIBRATE) accepted = motorStartPulse(MOTOR_PULSE_MS, MOTOR_REST_MS);
    else if (cmd == CMD_TICK) accepted = motorStartPulse(MOTOR_TICK_PULSE_MS, MOTOR_TICK_REST_MS);
    else if (cmd == CMD_IMU_ONLY) {
      cameraHeld = true;
      continue;
    } else if (cmd == CMD_CAMERA) {
      cameraHeld = false;
      continue;
    } else continue;
    if (!accepted) motorIgnored++;
  }
}

// Runs in the esp_timer task. It only touches the pin and flags. It never
// writes to Serial, so it cannot land inside a frame the loop is sending.
static void motorTimerOff(void *) {
  digitalWrite(MOTOR_PIN, LOW);
  motorLastOnUs = (uint32_t)(esp_timer_get_time() - motorStartUs);
  motorOn = false;
  motorEndPending = true;
}

static void motorBegin() {
  gpio_set_level((gpio_num_t)MOTOR_PIN, 0);
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(MOTOR_PIN, LOW);
  esp_timer_create_args_t args = {};
  args.callback = &motorTimerOff;
  args.name = "motor_off";
  if (esp_timer_create(&args, &motorTimer) != ESP_OK) motorTimer = nullptr;
}

static bool motorStartPulse(uint32_t pulseMs, uint32_t restMs) {
  if (motorTimer == nullptr || motorOn || motorEndPending) return false;
  if ((int32_t)(millis() - motorRestUntilMs) < 0) return false;
  motorPulses++;
  motorPulseMs = pulseMs;
  motorRestMs = restMs;
  sendText(String("motor pulse ") + motorPulses + " start " + pulseMs + " ms");
  motorStartMs = millis();
  motorStartUs = esp_timer_get_time();
  motorOn = true;
  digitalWrite(MOTOR_PIN, HIGH);
  if (esp_timer_start_once(motorTimer, (uint64_t)pulseMs * 1000ULL) != ESP_OK) {
    digitalWrite(MOTOR_PIN, LOW);
    motorOn = false;
    motorRestUntilMs = millis() + restMs;
    sendText(String("motor pulse ") + motorPulses + " aborted, off timer did not start, d3 " + motorLatch());
  }
  return true;
}

// Called from loop(). Reports the end of a pulse, reads host commands, and
// sends the periodic status line. Nothing here waits.
static void motorService() {
  if (motorEndPending) {
    motorEndPending = false;
    motorRestUntilMs = millis() + motorRestMs;
    sendText(String("motor pulse ") + motorPulses + " end on_us " + motorLastOnUs + " d3 " + motorLatch());
  } else if (motorOn && millis() - motorStartMs > motorPulseMs + MOTOR_OVERRUN_MS) {
    // Backstop. The timer should already have ended the pulse.
    digitalWrite(MOTOR_PIN, LOW);
    esp_timer_stop(motorTimer);
    motorOn = false;
    motorRestUntilMs = millis() + motorRestMs;
    sendText(String("motor pulse ") + motorPulses + " forced off by loop after " +
             (millis() - motorStartMs) + " ms d3 " + motorLatch());
  }

  uint32_t ignoredBefore = motorIgnored;
  readMotorBytes();
  if (motorIgnored != ignoredBefore) {
    sendText(String("motor command ignored, ") +
             (motorTimer == nullptr ? "off timer unavailable" : "pulse active or resting") +
             ", ignored " + motorIgnored);
  }

  uint32_t now = millis();
  if ((int32_t)(now - nextStatusMs) >= 0) {
    nextStatusMs = now + STATUS_PERIOD_MS;
    String line = statusLine();
    sendText(line);
    // The TCP socket can be down while the sampler is fine. The recorder reads this.
    sendUdpFrame(FRAME_TEXT, (const uint8_t *)line.c_str(), line.length());
  }
}

static bool i2cWrite(uint8_t address, uint8_t reg, uint8_t value) {
  if (imuBus == nullptr) return false;
  imuBus->beginTransmission(address);
  imuBus->write(reg);
  imuBus->write(value);
  uint8_t result = imuBus->endTransmission();
  if (result != 0) lastI2cError = result;
  return result == 0;
}

static bool i2cRead(uint8_t address, uint8_t reg, uint8_t *dest, size_t length) {
  if (imuBus == nullptr) return false;
  imuBus->beginTransmission(address);
  imuBus->write(reg);
  uint8_t result = imuBus->endTransmission(false);
  if (result != 0) {
    lastI2cError = result;
    return false;
  }
  size_t got = imuBus->requestFrom(address, length);
  if (got != length) {
    lastI2cError = 100 + (int)got;
    return false;
  }
  for (size_t i = 0; i < length; i++) dest[i] = imuBus->read();
  return true;
}

static bool probeImu(uint8_t address, uint8_t *whoami) {
  return i2cRead(address, 0x75, whoami, 1);
}

static const char *imuName(uint8_t whoami) {
  if (whoami == 0x68) return "MPU6050";
  if (whoami == 0x70) return "MPU6500";
  if (whoami == 0x71) return "MPU9250";
  return "unknown-id";
}

static bool usableWhoami(uint8_t whoami) {
  return whoami != 0x00 && whoami != 0xFF;
}

static bool startImu() {
  uint8_t who68 = 0;
  uint8_t who69 = 0;
  bool ack68 = i2cReady && probeImu(IMU_ADDR_LOW, &who68);
  bool ack69 = i2cReady && probeImu(IMU_ADDR_HIGH, &who69);
  String scan = String("i2c ") + imuBusName + " SDA GPIO5 SCL GPIO6 0x68 " +
                (ack68 ? String("ack whoami 0x") + String(who68, HEX) : "nack") +
                " 0x69 " +
                (ack69 ? String("ack whoami 0x") + String(who69, HEX) : "nack");
  sendText(scan);

  uint8_t address = 0;
  uint8_t whoami = 0;
  if (ack68 && usableWhoami(who68)) {
    address = IMU_ADDR_LOW;
    whoami = who68;
  } else if (ack69 && usableWhoami(who69)) {
    address = IMU_ADDR_HIGH;
    whoami = who69;
  } else {
    imuReady = false;
    imuFail = 2;
    imuLine = scan + " no usable imu";
    return false;
  }

  if (!i2cWrite(address, 0x6B, 0x80)) {
    imuReady = false;
    imuFail = 3;
    imuLine = scan + " reset write failed";
    sendText(imuLine);
    return false;
  }
  delay(100);
  // Clock from the X-gyro PLL, 42 Hz DLPF, 100 Hz sample rate, ±8 g, ±1000 deg/s, FIFO on.
  bool configured =
      i2cWrite(address, 0x6B, 0x01) &&
      i2cWrite(address, 0x1A, IMU_DLPF_CFG) &&
      i2cWrite(address, 0x19, IMU_SMPLRT_DIV) &&
      i2cWrite(address, 0x1C, IMU_ACCEL_CFG) &&
      i2cWrite(address, 0x1B, IMU_GYRO_CFG) &&
      i2cWrite(address, 0x23, IMU_FIFO_EN_AG) &&
      i2cWrite(address, 0x6A, 0x04) &&  // FIFO reset
      i2cWrite(address, 0x6A, 0x40);    // FIFO enable
  if (!configured) {
    imuReady = false;
    imuFail = 4;
    imuLine = scan + " config write failed";
    sendText(imuLine);
    return false;
  }
  delay(50);

  uint8_t accelCfg = 0xFF;
  uint8_t gyroCfg = 0xFF;
  uint8_t dlpf = 0xFF;
  uint8_t div = 0xFF;
  uint8_t whoRead = 0;
  if (!i2cRead(address, 0x1C, &accelCfg, 1) || !i2cRead(address, 0x1B, &gyroCfg, 1) ||
      !i2cRead(address, 0x1A, &dlpf, 1) || !i2cRead(address, 0x19, &div, 1) ||
      !i2cRead(address, 0x75, &whoRead, 1) || !usableWhoami(whoRead) ||
      accelCfg != IMU_ACCEL_CFG || gyroCfg != IMU_GYRO_CFG ||
      (dlpf & 0x07) != IMU_DLPF_CFG || div != IMU_SMPLRT_DIV) {
    imuReady = false;
    imuFail = 5;
    imuLine = scan + " config readback failed";
    sendText(imuLine);
    return false;
  }

  imuAddress = address;
  imuWho = whoRead;
  imuAccelFs = (accelCfg >> 3) & 0x03;
  imuGyroFs = (gyroCfg >> 3) & 0x03;
  imuReady = true;
  imuFail = 0;
  imuSampleCounter = 0;
  imuDropped = 0;
  imuGapPending = false;
  fifoReset();
  delay(100);
  uint16_t grown = fifoCount();
  // 100 Hz * 12 bytes * 0.1 s ≈ 120. A full FIFO here means the sample rate did not take.
  if (grown >= 800) {
    i2cWrite(address, 0x1A, IMU_DLPF_CFG);
    i2cWrite(address, 0x19, IMU_SMPLRT_DIV);
    fifoReset();
    delay(100);
    grown = fifoCount();
  }
  lastImuDrainMs = millis();
  uint8_t ignoredStatus = 0;
  i2cRead(address, 0x3A, &ignoredStatus, 1);
  imuLine = String("imu ") + imuName(whoRead) + " address 0x" + String(address, HEX) +
            " whoami 0x" + String(whoRead, HEX) +
            " sda GPIO5 scl GPIO6 accel_cfg 0x" + String(accelCfg, HEX) +
            " gyro_cfg 0x" + String(gyroCfg, HEX) +
            " fifo 100Hz dlpf 42Hz accel_fs 8g gyro_fs 1000dps growth " + grown;
  sendText(imuLine);
  return true;
}

static int16_t readBe16(const uint8_t *data) {
  return (int16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static void putLe16(uint8_t *dest, int16_t value) {
  dest[0] = (uint8_t)(value & 0xFF);
  dest[1] = (uint8_t)((value >> 8) & 0xFF);
}

static bool fifoReset() {
  // Disable, pulse the reset bit, then enable. A back-to-back write does not clear a full FIFO.
  i2cWrite(imuAddress, 0x6A, 0x00);
  delay(1);
  i2cWrite(imuAddress, 0x6A, 0x04);
  delay(1);
  return i2cWrite(imuAddress, 0x6A, 0x40);
}

static uint16_t fifoCount() {
  uint8_t raw[2];
  if (!i2cRead(imuAddress, 0x72, raw, 2)) return 0xFFFF;
  return (uint16_t)(((uint16_t)raw[0] << 8) | raw[1]);
}

static bool fifoOverflow() {
  uint8_t status = 0;
  if (!i2cRead(imuAddress, 0x3A, &status, 1)) return false;
  return (status & 0x10) != 0;
}

static bool readFifoBytes(uint8_t *dest, size_t length) {
  if (imuBus == nullptr || length == 0 || length > 128) return false;
  imuBus->beginTransmission(imuAddress);
  imuBus->write(0x74);
  if (imuBus->endTransmission(false) != 0) return false;
  size_t got = imuBus->requestFrom((int)imuAddress, (int)length);
  if (got != length) return false;
  for (size_t i = 0; i < length; i++) dest[i] = imuBus->read();
  return true;
}

static void noteFifoGap(uint32_t missed) {
  if (missed < 1) missed = 1;
  imuDropped += missed;
  imuSampleCounter += missed;
  imuGapPending = true;
  fifoReset();
  lastImuDrainMs = millis();
  sendText(String("imu fifo overflow dropped ") + missed + " total " + imuDropped);
}

static void sendImuBatch(const uint8_t *fifoBytes, uint8_t count, uint32_t firstCounter, bool gap, uint16_t dropped) {
  if (count == 0 || count > IMU_BATCH_MAX) return;
  uint8_t packet[12 + IMU_BATCH_MAX * IMU_FRAME_BYTES];
  packet[0] = imuAddress;
  packet[1] = imuWho;
  packet[2] = imuAccelFs;
  packet[3] = imuGyroFs;
  packet[4] = gap ? 1 : 0;
  packet[5] = count;
  packet[6] = (uint8_t)(dropped & 0xFF);
  packet[7] = (uint8_t)((dropped >> 8) & 0xFF);
  packet[8] = (uint8_t)(firstCounter & 0xFF);
  packet[9] = (uint8_t)((firstCounter >> 8) & 0xFF);
  packet[10] = (uint8_t)((firstCounter >> 16) & 0xFF);
  packet[11] = (uint8_t)((firstCounter >> 24) & 0xFF);
  // FIFO words are big-endian. Host packets stay little-endian, matching the old single-sample frame.
  for (uint8_t i = 0; i < count; i++) {
    const uint8_t *src = fifoBytes + (i * IMU_FRAME_BYTES);
    uint8_t *dest = packet + 12 + (i * IMU_FRAME_BYTES);
    for (int axis = 0; axis < 6; axis++) {
      dest[axis * 2] = src[axis * 2 + 1];
      dest[axis * 2 + 1] = src[axis * 2];
    }
  }
  uint32_t length = 12 + (uint32_t)count * IMU_FRAME_BYTES;
  if (wifiConfigured) {
    // USB serial is not the motion path while a network is configured.
    if (linkMode == LINK_STREAM) {
      if (sendUdpFrame(FRAME_IMU_BATCH, packet, length)) udpSent++;
      else udpFail++;
    }
    return;
  }
  sendFrame(FRAME_IMU_BATCH, packet, length);
}

// Drain every sample the FIFO is holding. One pass can emit several batches.
// Overflow throws the FIFO away and leaves a hole in the sample counter.
static void drainImu() {
  if (!imuReady) return;
  // The overflow flag can stick after boot. Only throw the FIFO away when it is actually full.
  imuLastOverflow = fifoOverflow() ? 1 : 0;
  uint16_t bytes = fifoCount();
  if (bytes == 0xFFFF) return;
  imuLastCount = bytes;
  if (bytes >= 1024) {
    uint32_t elapsed = millis() - lastImuDrainMs;
    noteFifoGap(elapsed / 10);
    return;
  }
  uint16_t samples = bytes / IMU_FRAME_BYTES;
  if (samples == 0) return;
  while (samples > 0) {
    uint8_t n = samples > IMU_BATCH_MAX ? IMU_BATCH_MAX : (uint8_t)samples;
    uint8_t raw[IMU_BATCH_MAX * IMU_FRAME_BYTES];
    bool readOk = true;
    for (uint8_t i = 0; i < n; i++) {
      // One sample per transaction. A 96-byte burst was failing on this wiring and discarding the FIFO.
      if (!i2cRead(imuAddress, 0x74, raw + (i * IMU_FRAME_BYTES), IMU_FRAME_BYTES)) {
        readOk = false;
        n = i;
        break;
      }
    }
    if (!readOk && n == 0) return;
    bool gap = imuGapPending;
    uint16_t dropped = gap ? (uint16_t)(imuDropped > 65535 ? 65535 : imuDropped) : 0;
    uint32_t first = imuSampleCounter;
    sendImuBatch(raw, n, first, gap, dropped);
    imuSampleCounter += n;
    imuGapPending = false;
    samples -= n;
  }
  lastImuDrainMs = millis();
}

// Hold samples while a large JPEG is going out, then send them as normal batches.
static void pullImuStash() {
  if (!imuReady || imuStashCount >= 40) return;
  uint16_t bytes = fifoCount();
  if (bytes == 0xFFFF || bytes < IMU_FRAME_BYTES) return;
  if (bytes >= 1024) {
    uint32_t missed = (millis() - lastImuDrainMs) / 10;
    if (missed < 1) missed = 1;
    imuDropped += missed;
    imuSampleCounter += missed;
    imuGapPending = true;
    fifoReset();
    lastImuDrainMs = millis();
    return;
  }
  while (imuStashCount < 40 && bytes >= IMU_FRAME_BYTES) {
    if (!i2cRead(imuAddress, 0x74, imuStash + (imuStashCount * IMU_FRAME_BYTES), IMU_FRAME_BYTES)) return;
    if (imuStashCount == 0) {
      imuStashGap = imuGapPending;
      imuStashFirst = imuSampleCounter;
      imuStashDropped = imuStashGap ? (uint16_t)(imuDropped > 65535 ? 65535 : imuDropped) : 0;
    }
    imuStashCount++;
    imuSampleCounter++;
    imuGapPending = false;
    bytes -= IMU_FRAME_BYTES;
  }
}

static void flushImuStash() {
  if (imuStashCount == 0) return;
  flushingStash = true;
  // One datagram per call. Two back-to-back UDP sends were dropping the second.
  uint8_t n = imuStashCount > IMU_BATCH_MAX ? IMU_BATCH_MAX : imuStashCount;
  sendImuBatch(imuStash, n, imuStashFirst, imuStashGap, imuStashDropped);
  if (n < imuStashCount) {
    memmove(imuStash, imuStash + (n * IMU_FRAME_BYTES), (imuStashCount - n) * IMU_FRAME_BYTES);
    imuStashCount -= n;
    imuStashFirst += n;
    imuStashGap = false;
    imuStashDropped = 0;
  } else {
    imuStashCount = 0;
  }
  flushingStash = false;
  lastImuDrainMs = millis();
}

static bool connectLaptop(uint32_t budgetMs) {
  IPAddress ip;
  bool haveIp = false;
  if (strlen(RETRACE_HOST) > 0 && ip.fromString(RETRACE_HOST)) haveIp = true;
  if (haveIp) {
    wifiClient.setTimeout(budgetMs);
    if (wifiClient.connect(ip, RETRACE_PORT, budgetMs)) return true;
    wifiClient.stop();
  }
  if (!MDNS.begin("retrace-ring")) return false;
  IPAddress found = MDNS.queryHost("retrace", budgetMs);
  if (found == IPAddress()) return false;
  wifiClient.setTimeout(budgetMs);
  if (wifiClient.connect(found, RETRACE_PORT, budgetMs)) return true;
  wifiClient.stop();
  return false;
}

static void tryWifi(uint32_t budgetMs) {
  if (!wifiConfigured) {
    linkMode = LINK_USB;
    return;
  }
  linkMode = LINK_JOINING;
  uint32_t started = millis();
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(RETRACE_WIFI_SSID, RETRACE_WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED && millis() - started < budgetMs) {
      ledService();
      pullImuStash();
      flushImuStash();
      delay(20);
    }
  }
  if (WiFi.status() != WL_CONNECTED) {
    linkMode = LINK_USB;
    nextWifiTryMs = millis() + WIFI_RETRY_MS;
    return;
  }
  uint32_t left = budgetMs > (millis() - started) ? budgetMs - (millis() - started) : 200;
  if (left > WIFI_CONNECT_MS) left = WIFI_CONNECT_MS;
  if (connectLaptop(left)) {
    wifiClient.setNoDelay(true);
    wifiClient.setTimeout(50);
    WiFi.setSleep(false);
    linkMode = LINK_STREAM;
    sendText(String("wifi streaming ") + WiFi.localIP().toString() + " -> " + RETRACE_HOST);
    return;
  }
  linkMode = LINK_USB;
  nextWifiTryMs = millis() + WIFI_RETRY_MS;
}

static void serviceWifi() {
  if (!wifiConfigured) return;
  if (linkMode == LINK_STREAM) {
    if (!wifiClient.connected()) {
      wifiClient.stop();
      linkMode = LINK_USB;
      // Pictures-paused mode retries at once. A 10 s wait was a guaranteed hole in the motion stream.
      nextWifiTryMs = cameraHeld ? millis() : millis() + WIFI_RETRY_MS;
      sendText("wifi dropped, usb fallback");
    }
    return;
  }
  if ((int32_t)(millis() - nextWifiTryMs) >= 0) tryWifi(WIFI_BOOT_MS);
}

static void applyQuality() {
  sensor_t *sensor = esp_camera_sensor_get();
  if (sensor) sensor->set_quality(sensor, jpegQuality);
}

// Keep 10 fps on Wi-Fi by worsening JPEG quality, then skipping frames. IMU rate stays 100 Hz.
static void adaptRate(uint32_t loopMs) {
  if (linkMode != LINK_STREAM) {
    if (jpegQuality != 10 || frameHold != 1) {
      jpegQuality = 10;
      frameHold = 1;
      applyQuality();
    }
    return;
  }
  if (loopMs > 100) {
    goodLoops = 0;
    if (jpegQuality < 20) {
      jpegQuality += 2;
      applyQuality();
      sendText(String("wifi quality ") + jpegQuality + " loop_ms " + loopMs);
    } else if (frameHold < 4) {
      frameHold++;
      sendText(String("wifi frame_hold ") + frameHold + " loop_ms " + loopMs);
    }
  } else if (loopMs < 60) {
    goodLoops++;
    if (goodLoops < 8) return;
    goodLoops = 0;
    if (frameHold > 1) frameHold--;
    else if (jpegQuality > 10) {
      jpegQuality--;
      applyQuality();
    }
  } else {
    goodLoops = 0;
  }
}

static bool readImuPacket(uint8_t packet[22]) {
  uint8_t raw[14];
  if (!i2cRead(imuAddress, 0x3B, raw, 14)) return false;
  packet[0] = imuAddress;
  packet[1] = imuWho;
  packet[2] = imuAccelFs;
  packet[3] = imuGyroFs;
  uint32_t sampleMs = millis();
  packet[4] = (uint8_t)(sampleMs & 0xFF);
  packet[5] = (uint8_t)((sampleMs >> 8) & 0xFF);
  packet[6] = (uint8_t)((sampleMs >> 16) & 0xFF);
  packet[7] = (uint8_t)((sampleMs >> 24) & 0xFF);
  for (int i = 0; i < 7; i++) putLe16(packet + 8 + (i * 2), readBe16(raw + (i * 2)));
  return true;
}

static const char *sensorName(uint16_t pid) {
  switch (pid) {
    case 0x26:
    case 0x2642: return "OV2640";
    case 0x3660: return "OV3660";
    case 0x5640: return "OV5640";
    default: return "unknown";
  }
}

static bool startCamera(int xclkHz) {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = xclkHz;
  config.frame_size = FRAMESIZE_VGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  // Lower number = higher JPEG quality (esp32-camera). Start at 10; raise to 12/15 if capture fails.
  config.jpeg_quality = 10;
  config.fb_count = 2;
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    cameraError = String("camera init failed, xclk ") + xclkHz + ", error " + err;
    sendText(cameraError);
    return false;
  }
  sensor_t *sensor = esp_camera_sensor_get();
  if (!sensor) {
    cameraError = "camera init returned no sensor";
    sendText(cameraError);
    return false;
  }
  sensor->set_framesize(sensor, FRAMESIZE_VGA);
  sensor->set_quality(sensor, 10);
  // Auto exposure at ae_level 0 (neutral); raised gain ceiling for indoor light.
  sensor->set_exposure_ctrl(sensor, 1);   // auto exposure
  sensor->set_aec2(sensor, 1);
  sensor->set_ae_level(sensor, 0);
  sensor->set_gain_ctrl(sensor, 1);       // auto gain
  sensor->set_gainceiling(sensor, GAINCEILING_32X);
  uint16_t pid = sensor->id.PID;
  sensorLine = String("sensor ") + sensorName(pid) + " pid 0x" + String(pid, HEX) +
               " xclk " + xclkHz + " framesize VGA jpeg_q 10 aec auto ae_level 0 gainceil 32x";
  sendText(sensorLine);
  cameraError = "";
  return true;
}

static bool beginImuBus(TwoWire &wire, const char *name) {
  wire.setBufferSize(128);
  if (!wire.begin(IMU_SDA, IMU_SCL, 100000)) {
    sendText(String(name) + " begin failed on SDA GPIO5 SCL GPIO6");
    return false;
  }
  wire.setTimeOut(50);
  imuBus = &wire;
  imuBusName = name;
  i2cReady = true;
  return startImu();
}

static void attachImu() {
  if (beginImuBus(Wire1, "Wire1")) return;
  Wire1.end();
  imuBus = nullptr;
  i2cReady = false;
  imuReady = false;
  if (beginImuBus(Wire, "Wire")) return;
  Wire.end();
  imuBus = nullptr;
  i2cReady = false;
  imuReady = false;
  if (imuLine.length() == 0) imuLine = "imu not found on Wire1 or Wire";
  sendText(imuLine);
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  ledSet(false);
  motorBegin();
  resetReason = esp_reset_reason();
  if (resetReason == ESP_RST_POWERON || bootMagic != BOOT_MAGIC) {
    bootMagic = BOOT_MAGIC;
    bootsSincePowerOn = 0;
  }
  bootsSincePowerOn++;
  Serial.setTxBufferSize(16384);
  Serial.begin(115200);
  Serial.setTxTimeoutMs(20);
  delay(300);
  wifiConfigured = RETRACE_WIFI_COMPILED && strlen(RETRACE_WIFI_SSID) > 0;
  if (wifiConfigured) tryWifi(WIFI_BOOT_MS);
  else nextWifiTryMs = millis() + WIFI_RETRY_MS;
  sendText(statusLine());
  sendText(String("psram ") + (psramFound() ? "yes" : "no") +
            " bytes " + ESP.getPsramSize() +
            " free " + ESP.getFreePsram());
  if (!psramFound()) {
    sendText("PSRAM missing. Camera buffers need the XIAO ESP32S3 OPI PSRAM.");
    return;
  }
  cameraReady = startCamera(20000000);
  if (!cameraReady) {
    esp_camera_deinit();
    cameraReady = startCamera(10000000);
  }
  attachImu();
}

void loop() {
  loopStartedMs = millis();
  if (wifiConfigured) WiFi.setSleep(false);
  ledService();
  motorService();
  if (!cameraHeld) {
    drainImu();
    flushImuStash();
  }
  serviceWifi();
  uint32_t now = millis();
  if (!imuReady && i2cReady && (int32_t)(now - nextImuTextMs) >= 0) {
    nextImuTextMs = now + 2000;
    startImu();
  }
  if (imuReady && (int32_t)(now - nextImuTextMs) >= 0) {
    nextImuTextMs = now + 2000;
    sendText(imuLine);
  } else if (!imuReady && !i2cReady && (int32_t)(now - nextImuTextMs) >= 0) {
    nextImuTextMs = now + 2000;
    sendText(imuLine.length() ? imuLine : String("imu not checked"));
  }
  if (!cameraReady) {
    delay(1000);
    sendText(cameraError.length() ? cameraError : String("camera is not running"));
    return;
  }
  if (cameraHeld) {
    drainImu();
    flushImuStash();
    delay(2);
    return;
  }
  camera_fb_t *frame = esp_camera_fb_get();
  motorService();
  drainImu();
  if (!frame) {
    captureErrors++;
    sendText(String("capture failed, count ") + captureErrors);
    delay(100);
    return;
  }
  bool jpeg = frame->format == PIXFORMAT_JPEG && frame->len >= 4 && frame->len <= MAX_JPEG &&
              frame->buf[0] == 0xFF && frame->buf[1] == 0xD8;
  framePhase++;
  bool sendThis = frameHold <= 1 || (framePhase % frameHold) == 0;
  if (jpeg && sendThis) {
    uint32_t sendStarted = millis();
    abandonJpeg = true;
    bool sent = sendFrame(FRAME_JPEG, frame->buf, frame->len);
    abandonJpeg = false;
    lastJpegSendMs = millis() - sendStarted;
    flushImuStash();
    if (sent) {
      framesSent++;
      if (framesSent == 1 || framesSent % 30 == 0) {
        sendText(sensorLine + " sent " + framesSent + " psram free " + ESP.getFreePsram());
      }
    } else if (linkMode == LINK_STREAM && streamBroken) {
      streamBroken = false;
      wifiClient.stop();
      linkMode = LINK_USB;
      nextWifiTryMs = cameraHeld ? millis() : millis() + WIFI_RETRY_MS;
      sendText("wifi send failed, usb fallback");
    }
  } else if (!jpeg) {
    captureErrors++;
    sendText(String("frame was not a usable jpeg, bytes ") + frame->len);
  }
  esp_camera_fb_return(frame);
  adaptRate(millis() - loopStartedMs);
}
