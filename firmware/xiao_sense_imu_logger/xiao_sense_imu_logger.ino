// Standalone IMU logger. No Wi-Fi, no camera, no host link while logging.
// MPU6050 at 100 Hz, ±8 g, ±1000 deg/s. Samples live in a PSRAM staging buffer
// and are appended to a LittleFS session so the log survives power loss.
// BOOT tap (released before the 3 s hold): flush, two 80 ms pulses 150 ms apart,
// and the LED off for half a second. A failed flush is three quick pulses.
// Hold BOOT 3 s, or send 'N': new session, two long pulses.
// USB 'v' is one 80 ms pulse. USB 'D' dumps the flash session.
// Boot never erases an existing session.
// The ring also opens an open Wi-Fi named retrace-ring at 192.168.4.1
// and serves the pocket guide there. A pulse request on that page fires
// the same 80 ms pulse, with no laptop in between.
//
// IMU: SDA D4/GPIO5, SCL D5/GPIO6. Motor: D3/GPIO4. User LED: GPIO21, active low.

#include <string.h>
#include <LittleFS.h>
#include <WiFi.h>
#include <Wire.h>
#include "esp_wifi.h"
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_https_server.h"
#include "esp_timer.h"
#include "guide_cert.h"
#include "guide_page.h"

static const int LED_PIN = 21;
static const int MOTOR_PIN = 4;
static const int BUTTON_PIN = 0;
static const int IMU_SDA = 5;
static const int IMU_SCL = 6;
static const uint8_t IMU_ADDR_LOW = 0x68;
static const uint8_t IMU_ADDR_HIGH = 0x69;
static const uint8_t IMU_DLPF_CFG = 0x03;
static const uint8_t IMU_SMPLRT_DIV = 9;
static const uint8_t IMU_ACCEL_CFG = 0x10;  // ±8 g
static const uint8_t IMU_GYRO_CFG = 0x10;   // ±1000 deg/s
static const int IMU_FRAME_BYTES = 12;
static const uint32_t SAMPLE_PERIOD_US = 10000;
static const uint32_t SAMPLE_CAP = 60u * 100u;  // unsaved staging, not the session length
static const uint32_t MARK_CAP = 32;
static const uint32_t IMU_RETRY_MS = 2000;
static const uint32_t BOOT_DEBOUNCE_MS = 10;
static const uint32_t HOLD_NEW_SESSION_MS = 3000;
static const uint32_t TAP_ALWAYS_MS = 1000;
static const uint32_t MARK_PULSE_MS = 80;
static const uint32_t MARK_GAP_MS = 150;
static const uint32_t MARK_LED_OFF_MS = 500;
static const uint32_t QUICK_PULSE_MS = 40;
static const uint32_t QUICK_REST_MS = 70;
static const uint32_t LONG_PULSE_MS = 200;
static const uint32_t LONG_REST_MS = 300;
static const uint32_t FLUSH_MS = 2000;
static const uint32_t FLUSH_MAX_MS = 30000;
static const uint32_t KEEPALIVE_MS = 8000;
static const uint32_t KEEPALIVE_PULSE_MS = 40;
static const uint32_t KEEPALIVE_REST_MS = 200;
static const uint32_t META_MAGIC = 0x31474F4C;  // 'LOG1'
static const char *FS_LABEL = "littlefs";
static const char *GUIDE_SSID = "retrace-ring";
static volatile bool guidePulseRequested = false;

struct __attribute__((packed)) Sample {
  uint32_t t_us;
  int16_t ax;
  int16_t ay;
  int16_t az;
  int16_t gx;
  int16_t gy;
  int16_t gz;
};
static_assert(sizeof(Sample) == 16, "logger sample must be 16 bytes");

struct __attribute__((packed)) Meta {
  uint32_t magic;
  uint32_t version;
  uint32_t period_us;
  uint32_t sample_count;
  uint32_t mark_count;
  uint32_t next_index;
  int64_t origin_us;
  uint32_t dropped;
};
static_assert(sizeof(Meta) == 36, "session meta size");

static Sample *samples = nullptr;
static uint32_t sampleCount = 0;
static uint32_t pendingMarks[MARK_CAP];
static uint32_t pendingMarkCount = 0;
static uint32_t savedSamples = 0;
static uint32_t savedMarks = 0;
static uint32_t dropped = 0;
static bool full = false;
static bool dumping = false;
static bool fsReady = false;
static int64_t originUs = 0;
static uint32_t nextFlushMs = 0;
static uint32_t nextMaxFlushMs = 0;

static bool i2cReady = false;
static bool imuReady = false;
static TwoWire *imuBus = nullptr;
static uint8_t imuAddress = 0;
static uint8_t imuFail = 1;
static uint32_t imuSampleCounter = 0;
static bool imuGapPending = false;
static uint32_t lastImuDrainMs = 0;
static uint32_t nextImuTryMs = 0;
static int lastI2cError = 0;

static esp_timer_handle_t motorTimer = nullptr;
static volatile bool motorOn = false;
static volatile bool motorEndPending = false;
static uint32_t motorRestUntilMs = 0;
static uint32_t motorStartMs = 0;
static uint32_t motorPulseMs = 0;
static uint32_t motorRestMs = 0;
static uint32_t nextKeepMs = 0;

struct PulseJob {
  uint16_t onMs;
  uint16_t restMs;
};
static PulseJob pulseQueue[8];
static uint8_t pulseHead = 0;
static uint8_t pulseCount = 0;

static volatile bool levelHigh = true;
static volatile uint32_t levelSinceMs = 0;
static bool stableHigh = true;
static bool buttonHeld = false;
static bool holdConsumed = false;
static bool buttonLive = false;
static uint32_t pressBeganMs = 0;
static uint32_t ledForceOffUntilMs = 0;
static esp_timer_handle_t buttonTimer = nullptr;

static void ledSet(bool on) {
  digitalWrite(LED_PIN, on ? LOW : HIGH);
}

static void ledService() {
  uint32_t now = millis();
  if ((int32_t)(ledForceOffUntilMs - now) > 0) {
    ledSet(false);
    return;
  }
  if (!imuReady) {
    ledSet(((now / 100) % 2) == 0);
    return;
  }
  // Mostly on, with a short off so logging is still a slow blink.
  ledSet((now % 2000) < 1800);
}

static void IRAM_ATTR pollButtonTimer(void *) {
  bool high = digitalRead(BUTTON_PIN) == HIGH;
  if (high == levelHigh) return;
  levelHigh = high;
  levelSinceMs = millis();
}

static void motorTimerOff(void *) {
  digitalWrite(MOTOR_PIN, LOW);
  motorOn = false;
  motorEndPending = true;
}

// WiFi.setSleep(false) only stores a flag unless the STA interface has started.
// This sketch is AP-only, so that call never reaches the driver. The driver
// default is WIFI_PS_MIN_MODEM, and station-disconnected power save is on, so
// the radio stops beaconing while softAPIP() still returns 192.168.4.1.
// The phone associates, misses beacons, and leaves, which stops POST /pulse.
static bool guideRadioHeld = false;

static void holdGuideRadio() {
  if (esp_wifi_set_ps(WIFI_PS_NONE) != ESP_OK) return;
  if (!guideRadioHeld && esp_wifi_force_wakeup_acquire() == ESP_OK) guideRadioHeld = true;
}

static void onGuideWifi(arduino_event_id_t event) {
  if (event == ARDUINO_EVENT_WIFI_AP_START || event == ARDUINO_EVENT_WIFI_AP_STACONNECTED ||
      event == ARDUINO_EVENT_WIFI_AP_STADISCONNECTED) {
    holdGuideRadio();
  }
}

static void serviceGuideRadio() {
  static uint32_t nextMs = 0;
  uint32_t now = millis();
  if ((int32_t)(now - nextMs) < 0) return;
  nextMs = now + 1000;
  holdGuideRadio();
}

static void motorBegin() {
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
  motorPulseMs = pulseMs;
  motorRestMs = restMs;
  motorStartMs = millis();
  motorOn = true;
  digitalWrite(MOTOR_PIN, HIGH);
  if (esp_timer_start_once(motorTimer, (uint64_t)pulseMs * 1000ULL) != ESP_OK) {
    digitalWrite(MOTOR_PIN, LOW);
    motorOn = false;
    motorRestUntilMs = millis() + restMs;
    return false;
  }
  return true;
}

static void queuePulse(uint16_t onMs, uint16_t restMs) {
  // A keep-alive rest must not delay or swallow a mark, a failure, or a new session.
  if (!motorOn) motorRestUntilMs = millis();
  if (pulseCount >= 8) return;
  pulseQueue[(pulseHead + pulseCount) % 8] = {onMs, restMs};
  pulseCount++;
}

static void queueQuickFail() {
  queuePulse(QUICK_PULSE_MS, QUICK_REST_MS);
  queuePulse(QUICK_PULSE_MS, QUICK_REST_MS);
  queuePulse(QUICK_PULSE_MS, QUICK_REST_MS);
}

static void queueNewSession() {
  queuePulse(LONG_PULSE_MS, LONG_REST_MS);
  queuePulse(LONG_PULSE_MS, LONG_REST_MS);
}

static void queueMarkPulses() {
  queuePulse(MARK_PULSE_MS, MARK_GAP_MS);
  queuePulse(MARK_PULSE_MS, MARK_GAP_MS);
}

static void motorService() {
  if (motorEndPending) {
    motorEndPending = false;
    if (pulseCount > 0) motorRestUntilMs = millis();
    else motorRestUntilMs = millis() + motorRestMs;
    // A power-bank pulse can sag the rail. Put the radio back on a beacon
    // schedule immediately instead of waiting for the one-second hold.
    holdGuideRadio();
  }
  if (pulseCount == 0 || motorOn || motorEndPending) return;
  PulseJob job = pulseQueue[pulseHead];
  if (!motorStartPulse(job.onMs, job.restMs)) return;
  pulseHead = (pulseHead + 1) % 8;
  pulseCount--;
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
  if (imuBus->endTransmission(false) != 0) return false;
  if (imuBus->requestFrom((int)address, (int)length) != (int)length) return false;
  for (size_t i = 0; i < length; i++) dest[i] = imuBus->read();
  return true;
}

static bool probeImu(uint8_t address, uint8_t *whoami) {
  return i2cRead(address, 0x75, whoami, 1);
}

static bool usableWhoami(uint8_t whoami) {
  return whoami != 0x00 && whoami != 0xFF;
}

static int16_t readBe16(const uint8_t *data) {
  return (int16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static bool fifoReset() {
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

static bool startImu() {
  uint8_t who68 = 0;
  uint8_t who69 = 0;
  bool ack68 = i2cReady && probeImu(IMU_ADDR_LOW, &who68);
  bool ack69 = i2cReady && probeImu(IMU_ADDR_HIGH, &who69);
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
    if (Serial) {
      Serial.printf("imu nack 0x68 %s 0x69 %s\n", ack68 ? "ack" : "nack", ack69 ? "ack" : "nack");
    }
    return false;
  }
  if (!i2cWrite(address, 0x6B, 0x80)) {
    imuReady = false;
    imuFail = 3;
    return false;
  }
  delay(100);
  bool configured =
      i2cWrite(address, 0x6B, 0x01) &&
      i2cWrite(address, 0x1A, IMU_DLPF_CFG) &&
      i2cWrite(address, 0x19, IMU_SMPLRT_DIV) &&
      i2cWrite(address, 0x1C, IMU_ACCEL_CFG) &&
      i2cWrite(address, 0x1B, IMU_GYRO_CFG) &&
      i2cWrite(address, 0x23, 0x78) &&
      i2cWrite(address, 0x6A, 0x04) &&
      i2cWrite(address, 0x6A, 0x40);
  if (!configured) {
    imuReady = false;
    imuFail = 4;
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
    return false;
  }
  imuAddress = address;
  imuReady = true;
  imuFail = 0;
  imuGapPending = false;
  fifoReset();
  delay(20);
  lastImuDrainMs = millis();
  if (Serial) {
    Serial.printf("imu ok whoami 0x%02x addr 0x%02x accel 8g gyro 1000dps\n", whoRead, address);
  }
  return true;
}

static bool beginImuBus(TwoWire &wire) {
  wire.setBufferSize(128);
  if (!wire.begin(IMU_SDA, IMU_SCL, 100000)) return false;
  wire.setTimeOut(50);
  imuBus = &wire;
  i2cReady = true;
  return startImu();
}

static void attachImu() {
  if (imuBus != nullptr) {
    imuBus->end();
    imuBus = nullptr;
  }
  i2cReady = false;
  imuReady = false;
  if (beginImuBus(Wire1)) return;
  Wire1.end();
  imuBus = nullptr;
  i2cReady = false;
  if (beginImuBus(Wire)) return;
  Wire.end();
  imuBus = nullptr;
  i2cReady = false;
  imuReady = false;
  imuFail = 2;
}

static void noteGap(uint32_t missed) {
  if (missed < 1) missed = 1;
  dropped += missed;
  imuSampleCounter += missed;
  imuGapPending = true;
  fifoReset();
  lastImuDrainMs = millis();
}

static void storeSample(const uint8_t *frame) {
  uint32_t tUs = imuSampleCounter * SAMPLE_PERIOD_US;
  imuSampleCounter++;
  imuGapPending = false;
  if (samples == nullptr || sampleCount >= SAMPLE_CAP) {
    full = true;
    return;
  }
  Sample &row = samples[sampleCount++];
  row.t_us = tUs;
  row.ax = readBe16(frame + 0);
  row.ay = readBe16(frame + 2);
  row.az = readBe16(frame + 4);
  row.gx = readBe16(frame + 6);
  row.gy = readBe16(frame + 8);
  row.gz = readBe16(frame + 10);
}

static void drainImu() {
  if (!imuReady || dumping || samples == nullptr) return;
  uint16_t bytes = fifoCount();
  if (bytes == 0xFFFF) return;
  if (fifoOverflow() && bytes >= 1024) {
    uint32_t elapsed = millis() - lastImuDrainMs;
    noteGap(elapsed / 10);
    return;
  }
  uint16_t pending = bytes / IMU_FRAME_BYTES;
  while (pending > 0) {
    uint8_t raw[IMU_FRAME_BYTES];
    if (!i2cRead(imuAddress, 0x74, raw, IMU_FRAME_BYTES)) return;
    storeSample(raw);
    pending--;
  }
  lastImuDrainMs = millis();
}

static uint32_t fileRecords(const char *path, uint32_t recordBytes) {
  if (!fsReady || recordBytes == 0 || !LittleFS.exists(path)) return 0;
  File file = LittleFS.open(path, "r");
  if (!file) return 0;
  uint32_t count = (uint32_t)(file.size() / recordBytes);
  file.close();
  return count;
}

static bool readMeta(Meta *meta) {
  if (!fsReady || !LittleFS.exists("/meta.bin")) return false;
  File file = LittleFS.open("/meta.bin", "r");
  if (!file || file.size() != sizeof(Meta)) {
    if (file) file.close();
    return false;
  }
  size_t got = file.read((uint8_t *)meta, sizeof(Meta));
  file.close();
  return got == sizeof(Meta) && meta->magic == META_MAGIC && meta->version == 1;
}

static bool writeMeta() {
  Meta meta = {};
  meta.magic = META_MAGIC;
  meta.version = 1;
  meta.period_us = SAMPLE_PERIOD_US;
  meta.sample_count = savedSamples;
  meta.mark_count = savedMarks;
  meta.next_index = imuSampleCounter;
  meta.origin_us = originUs;
  meta.dropped = dropped;
  File file = LittleFS.open("/meta.bin", "w");
  if (!file) return false;
  size_t wrote = file.write((const uint8_t *)&meta, sizeof(meta));
  file.flush();
  file.close();
  return wrote == sizeof(meta);
}

static bool appendFile(const char *path, const uint8_t *data, size_t length) {
  if (length == 0) return true;
  File file = LittleFS.open(path, "a");
  if (!file) return false;
  size_t wrote = 0;
  while (wrote < length) {
    size_t ask = length - wrote;
    if (ask > 128) ask = 128;
    size_t n = file.write(data + wrote, ask);
    if (n == 0) break;
    wrote += n;
  }
  file.flush();
  file.close();
  return wrote == length;
}

static void loadSession() {
  savedSamples = fileRecords("/samples.bin", sizeof(Sample));
  savedMarks = fileRecords("/marks.bin", sizeof(uint32_t));
  Meta meta = {};
  bool haveMeta = readMeta(&meta);
  if (haveMeta && meta.sample_count < savedSamples) {
    // The sample append landed and the meta rewrite did not.
    savedSamples = fileRecords("/samples.bin", sizeof(Sample));
  } else if (haveMeta && meta.sample_count <= savedSamples) {
    savedSamples = meta.sample_count < savedSamples ? savedSamples : meta.sample_count;
  }
  if (haveMeta && meta.mark_count > savedMarks) savedMarks = meta.mark_count;
  if (savedMarks > fileRecords("/marks.bin", sizeof(uint32_t))) {
    savedMarks = fileRecords("/marks.bin", sizeof(uint32_t));
  }
  uint32_t nextIndex = 0;
  if (savedSamples > 0) {
    File file = LittleFS.open("/samples.bin", "r");
    Sample last = {};
    if (file && file.seek((savedSamples - 1) * sizeof(Sample), SeekSet) &&
        file.read((uint8_t *)&last, sizeof(last)) == sizeof(last)) {
      nextIndex = last.t_us / SAMPLE_PERIOD_US + 1;
    }
    if (file) file.close();
  }
  if (haveMeta && meta.next_index > nextIndex && meta.sample_count == savedSamples) nextIndex = meta.next_index;
  imuSampleCounter = nextIndex;
  if (haveMeta) {
    originUs = meta.origin_us;
    dropped = meta.dropped;
  }
  if (Serial) {
    Serial.printf("logger session samples=%u marks=%u next=%u\n", savedSamples, savedMarks, imuSampleCounter);
  }
}

static bool flushSession() {
  if (!fsReady) return false;
  if (sampleCount == 0 && pendingMarkCount == 0) return writeMeta();
  if (sampleCount > 0) {
    if (!appendFile("/samples.bin", (const uint8_t *)samples, sampleCount * sizeof(Sample))) {
      full = true;
      return false;
    }
    savedSamples += sampleCount;
    sampleCount = 0;
    full = false;
  }
  if (pendingMarkCount > 0) {
    if (!appendFile("/marks.bin", (const uint8_t *)pendingMarks, pendingMarkCount * sizeof(uint32_t))) {
      full = true;
      return false;
    }
    savedMarks += pendingMarkCount;
    pendingMarkCount = 0;
  }
  if (!writeMeta()) return false;
  return true;
}

static void beginNewSession() {
  if (fsReady) {
    LittleFS.remove("/samples.bin");
    LittleFS.remove("/marks.bin");
    LittleFS.remove("/meta.bin");
  }
  sampleCount = 0;
  pendingMarkCount = 0;
  savedSamples = 0;
  savedMarks = 0;
  dropped = 0;
  full = false;
  imuSampleCounter = 0;
  originUs = esp_timer_get_time();
  queueNewSession();
  motorService();
  if (Serial) Serial.println("logger new session");
}

static bool registerMark() {
  if (pendingMarkCount >= MARK_CAP) return false;
  uint32_t tUs = imuSampleCounter * SAMPLE_PERIOD_US;
  pendingMarks[pendingMarkCount++] = tUs;
  ledForceOffUntilMs = millis() + MARK_LED_OFF_MS;
  ledSet(false);
  if (flushSession()) {
    queueMarkPulses();
    motorService();
    return true;
  }
  queueQuickFail();
  motorService();
  return false;
}

static void serviceButton() {
  if (buttonTimer == nullptr) {
    bool highNow = digitalRead(BUTTON_PIN) == HIGH;
    if (highNow != levelHigh) {
      levelHigh = highNow;
      levelSinceMs = millis();
    }
  }
  noInterrupts();
  bool high = levelHigh;
  uint32_t edgeMs = levelSinceMs;
  interrupts();
  uint32_t now = millis();
  if (!buttonLive) {
    if (!high) return;
    buttonLive = true;
    stableHigh = true;
    buttonHeld = false;
    return;
  }
  if (!high) buttonHeld = true;
  if ((uint32_t)(now - edgeMs) < BOOT_DEBOUNCE_MS) return;
  if (!high && stableHigh) {
    stableHigh = false;
    pressBeganMs = edgeMs;
    holdConsumed = false;
    if (Serial) {
      Serial.println("button pressed");
      Serial.flush();
    }
  }
  if (!high && !stableHigh && !holdConsumed && (uint32_t)(now - pressBeganMs) >= HOLD_NEW_SESSION_MS) {
    holdConsumed = true;
    beginNewSession();
  }
  if (high && !stableHigh) {
    uint32_t duration = edgeMs - pressBeganMs;
    stableHigh = true;
    buttonHeld = false;
    bool tap = !holdConsumed || duration < TAP_ALWAYS_MS;
    bool stored = false;
    if (tap) stored = registerMark();
    if (Serial) {
      Serial.printf(
          "button released duration_ms=%u class=%s mark=%s\n",
          duration,
          tap ? "tap" : "hold",
          stored ? "yes" : "no");
      Serial.flush();
    }
  }
}

static void serviceKeepAlive() {
  // USB host power does not need a load. A power bank does not enumerate, so Serial is false.
  // Never start while a finger is down or a mark pattern is waiting: that pattern owns the motor.
  if ((bool)Serial || !imuReady || buttonHeld || pulseCount > 0 || motorOn) return;
  if ((int32_t)(millis() - motorRestUntilMs) < 0) return;
  if ((int32_t)(millis() - nextKeepMs) < 0) return;
  nextKeepMs = millis() + KEEPALIVE_MS;
  motorStartPulse(KEEPALIVE_PULSE_MS, KEEPALIVE_REST_MS);
}

static void serviceFlush() {
  uint32_t now = millis();
  bool due = (int32_t)(now - nextFlushMs) >= 0 || (int32_t)(now - nextMaxFlushMs) >= 0;
  if (!due || (sampleCount == 0 && pendingMarkCount == 0)) {
    if ((int32_t)(now - nextMaxFlushMs) >= 0) nextMaxFlushMs = now + FLUSH_MAX_MS;
    return;
  }
  nextFlushMs = now + FLUSH_MS;
  nextMaxFlushMs = now + FLUSH_MAX_MS;
  if (!flushSession()) {
    queueQuickFail();
    motorService();
  }
}

static void writeAll(const uint8_t *data, size_t length) {
  size_t off = 0;
  while (off < length) {
    size_t n = length - off;
    if (n > 1024) n = 1024;
    Serial.write(data + off, n);
    off += n;
    delay(0);
  }
}

static void streamFile(const char *path, uint32_t bytes) {
  if (bytes == 0 || !LittleFS.exists(path)) return;
  File file = LittleFS.open(path, "r");
  if (!file) return;
  uint8_t chunk[1024];
  uint32_t left = bytes;
  while (left > 0) {
    size_t ask = left > sizeof(chunk) ? sizeof(chunk) : left;
    size_t got = file.read(chunk, ask);
    if (got == 0) break;
    writeAll(chunk, got);
    left -= got;
  }
  file.close();
}

static void dumpLog() {
  dumping = true;
  flushSession();
  uint32_t sampleBytes = savedSamples * sizeof(Sample);
  uint32_t markBytes = savedMarks * sizeof(uint32_t);
  Serial.printf("RETRACE_LOG v1\n");
  Serial.printf("samples %u\n", savedSamples + sampleCount);
  Serial.printf("marks %u\n", savedMarks + pendingMarkCount);
  Serial.printf("period_us %u\n", SAMPLE_PERIOD_US);
  Serial.printf("t0_us %lld\n", (long long)originUs);
  Serial.printf("accel_lsb_per_g 4096\n");
  Serial.printf("gyro_lsb_per_dps 32.8\n");
  Serial.printf("full %u\n", full ? 1 : 0);
  Serial.printf("dropped %u\n", dropped);
  Serial.printf("imu_fail %u\n", imuFail);
  Serial.printf("END_HEADER\n");
  streamFile("/samples.bin", sampleBytes);
  if (sampleCount > 0 && samples != nullptr) writeAll((const uint8_t *)samples, sampleCount * sizeof(Sample));
  streamFile("/marks.bin", markBytes);
  if (pendingMarkCount > 0) writeAll((const uint8_t *)pendingMarks, pendingMarkCount * sizeof(uint32_t));
  Serial.printf("END\n");
  Serial.flush();
  dumping = false;
}

static esp_err_t guideSendPage(httpd_req_t *req) {
  const bool script = req->uri != nullptr && strstr(req->uri, "pocket.js") != nullptr;
  httpd_resp_set_type(req, script ? "text/javascript" : "text/html");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  const char *body = script ? GUIDE_JS : GUIDE_HTML;
  return httpd_resp_send(req, body, strlen(body));
}

static esp_err_t guidePulse(httpd_req_t *req) {
  guidePulseRequested = true;
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_sendstr(req, "{\"ok\":true,\"error\":\"\"}");
}

static esp_err_t guideRedirect(httpd_req_t *req) {
  httpd_resp_set_status(req, "302 Found");
  httpd_resp_set_hdr(req, "Location", "https://192.168.4.1/");
  return httpd_resp_send(req, "", 0);
}

static void startGuideServer() {
  httpd_ssl_config_t secure = HTTPD_SSL_CONFIG_DEFAULT();
  secure.servercert = (const uint8_t *)GUIDE_CERT;
  secure.servercert_len = strlen(GUIDE_CERT) + 1;
  secure.prvtkey_pem = (const uint8_t *)GUIDE_KEY;
  secure.prvtkey_len = strlen(GUIDE_KEY) + 1;
  secure.httpd.stack_size = 16384;
  // Page, pocket.js, and /pulse are in flight together. Two sockets plus LRU
  // purge resets an iOS handshake, and iOS then drops the association.
  // Idle handshakes wait in the backlog instead of resetting the phone.
  secure.httpd.max_open_sockets = 4;
  secure.httpd.lru_purge_enable = false;
  httpd_handle_t https = nullptr;
  if (httpd_ssl_start(&https, &secure) != ESP_OK) {
    // Four sessions are about 40 kB each. If the heap cannot take that, two
    // still serves the page as long as nothing is purged out from under iOS.
    secure.httpd.max_open_sockets = 2;
    if (httpd_ssl_start(&https, &secure) != ESP_OK) {
      if (Serial) Serial.println("guide https failed");
      return;
    }
  }
  httpd_uri_t page = {};
  page.uri = "/";
  page.method = HTTP_GET;
  page.handler = guideSendPage;
  httpd_register_uri_handler(https, &page);
  httpd_uri_t script = {};
  script.uri = "/pocket.js";
  script.method = HTTP_GET;
  script.handler = guideSendPage;
  httpd_register_uri_handler(https, &script);
  httpd_uri_t pulse = {};
  pulse.uri = "/pulse";
  pulse.method = HTTP_POST;
  pulse.handler = guidePulse;
  httpd_register_uri_handler(https, &pulse);

  httpd_config_t plain = HTTPD_DEFAULT_CONFIG();
  plain.server_port = 80;
  plain.ctrl_port = ESP_HTTPD_DEF_CTRL_PORT + 2;
  plain.max_open_sockets = 2;
  plain.lru_purge_enable = false;
  plain.uri_match_fn = httpd_uri_match_wildcard;
  httpd_handle_t http = nullptr;
  if (httpd_start(&http, &plain) == ESP_OK) {
    httpd_uri_t hop = {};
    hop.uri = "/*";
    hop.method = HTTP_GET;
    hop.handler = guideRedirect;
    httpd_register_uri_handler(http, &hop);
  }
  if (Serial) Serial.println("guide wifi retrace-ring https://192.168.4.1/");
}

static void serviceSerial() {
  while (Serial.available() > 0) {
    int cmd = Serial.read();
    if (cmd == 'D') dumpLog();
    else if (cmd == 'N') beginNewSession();
    else if (cmd == 'v') queuePulse(MARK_PULSE_MS, 220);
  }
}

static void printStatus() {
  static uint32_t nextMs = 0;
  if (!Serial) return;
  if ((int32_t)(millis() - nextMs) < 0) return;
  nextMs = millis() + 1000;
  uint32_t fsBytes = fsReady ? (uint32_t)LittleFS.totalBytes() : 0;
  uint32_t minutes = fsBytes / (16u * 100u * 60u);
  String apIp = WiFi.softAPIP().toString();
  Serial.printf(
      "logger samples=%u saved=%u marks=%u imu=%u full=%u dropped=%u fail=%u boot=%u fs=%u min=%u ap=%s\n",
      savedSamples + sampleCount,
      savedSamples,
      savedMarks + pendingMarkCount,
      imuReady ? 1 : 0,
      full ? 1 : 0,
      dropped,
      imuFail,
      digitalRead(BUTTON_PIN) == HIGH ? 1 : 0,
      fsBytes,
      minutes,
      apIp.c_str());
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  ledSet(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  levelHigh = digitalRead(BUTTON_PIN) == HIGH;
  levelSinceMs = millis();
  stableHigh = levelHigh;
  buttonHeld = !levelHigh;
  buttonLive = false;
  motorBegin();
  esp_timer_create_args_t buttonArgs = {};
  buttonArgs.callback = &pollButtonTimer;
  buttonArgs.name = "button";
  if (esp_timer_create(&buttonArgs, &buttonTimer) == ESP_OK) {
    esp_timer_start_periodic(buttonTimer, 5000);
  }
  originUs = esp_timer_get_time();
  nextKeepMs = millis() + KEEPALIVE_MS;
  Serial.setTxBufferSize(16384);
  Serial.begin(115200);
  Serial.setTxTimeoutMs(20);
  delay(200);
  WiFi.persistent(false);
  WiFi.mode(WIFI_AP);
  WiFi.onEvent(onGuideWifi);
  // The core defaults to WPA2, which rejects an empty password and never starts the radio.
  bool ap = WiFi.softAP(GUIDE_SSID, nullptr, 6, 0, 4, false, WIFI_AUTH_OPEN);
  if (Serial) {
    Serial.printf("guide ap %s\n", ap ? "up" : "failed");
  }
  if (ap) {
    holdGuideRadio();
    startGuideServer();
  }
  fsReady = LittleFS.begin(false, "/littlefs", 10, FS_LABEL);
  if (!fsReady) {
    if (Serial) Serial.println("logger fs mount failed, formatting");
    if (LittleFS.format()) fsReady = LittleFS.begin(false, "/littlefs", 10, FS_LABEL);
  }
  if (!fsReady) {
    if (Serial) Serial.println("logger fs unavailable");
  } else {
    loadSession();
    if (Serial) {
      uint32_t fsBytes = (uint32_t)LittleFS.totalBytes();
      Serial.printf("logger fs bytes=%u minutes=%u\n", fsBytes, fsBytes / (16u * 100u * 60u));
    }
  }
  if (!psramFound()) {
    imuFail = 1;
    if (Serial) Serial.println("logger psram missing");
    return;
  }
  samples = (Sample *)heap_caps_malloc(SAMPLE_CAP * sizeof(Sample), MALLOC_CAP_SPIRAM);
  if (samples == nullptr) {
    if (Serial) Serial.println("logger psram alloc failed");
    return;
  }
  if (Serial) {
    Serial.printf("logger ready staging=%u psram=%u\n", SAMPLE_CAP, ESP.getFreePsram());
  }
  attachImu();
  nextImuTryMs = millis() + IMU_RETRY_MS;
  nextFlushMs = millis() + FLUSH_MS;
  nextMaxFlushMs = millis() + FLUSH_MAX_MS;
}

void loop() {
  if (guidePulseRequested) {
    guidePulseRequested = false;
    queuePulse(MARK_PULSE_MS, 220);
  }
  ledService();
  motorService();
  serviceButton();
  serviceKeepAlive();
  serviceSerial();
  serviceFlush();
  uint32_t now = millis();
  if (!imuReady && samples != nullptr && (int32_t)(now - nextImuTryMs) >= 0) {
    nextImuTryMs = now + IMU_RETRY_MS;
    attachImu();
  }
  drainImu();
  serviceGuideRadio();
  printStatus();
}
