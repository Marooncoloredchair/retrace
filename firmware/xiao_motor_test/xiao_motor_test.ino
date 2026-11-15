// One-shot vibration-motor test for the XIAO ESP32S3 Sense.
// IN is D3 / GPIO4. VCC stays on the module's existing supply.
// Hold LOW, count down 5 seconds, pulse 200 ms three times with 1 second
// pauses, then hold LOW. The sequence is stored before the first pulse.

#include <Preferences.h>
#include "esp_system.h"

static const int MOTOR_PIN = 4;
static const char *TEST_ID = "countdown5-200ms-pause1000-x3";
RTC_DATA_ATTR uint32_t bootCount = 0;
static const uint32_t PAUSE_MS = 1000;
static const uint32_t PULSE_MS = 200;

static void holdLow() {
  digitalWrite(MOTOR_PIN, LOW);
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(MOTOR_PIN, LOW);
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

static void logLine(const String &text) {
  Serial.print("ms ");
  Serial.print(millis());
  Serial.print(" ");
  Serial.println(text);
  Serial.flush();
}

static void pulse(const char *name, uint32_t highMs) {
  holdLow();
  logLine(String(name) + " HIGH " + highMs + " ms");
  digitalWrite(MOTOR_PIN, HIGH);
  delay(highMs);
  digitalWrite(MOTOR_PIN, LOW);
  logLine(String(name) + " LOW latch " + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));
}

static void pauseLow() {
  holdLow();
  logLine(String("pause ") + PAUSE_MS + " ms");
  delay(PAUSE_MS);
  logLine(String("pause done latch ") + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));
}

static void countdown() {
  for (int second = 5; second >= 1; second--) {
    holdLow();
    logLine(String("countdown ") + second);
    delay(1000);
  }
  holdLow();
  logLine(String("countdown done latch ") + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));
}

static bool waitForGo() {
  uint32_t started = millis();
  uint32_t nextReport = 0;
  while (millis() - started < 20000) {
    holdLow();
    if (Serial.available() > 0 && Serial.read() == 'G') return true;
    uint32_t now = millis();
    if ((int32_t)(now - nextReport) >= 0) {
      nextReport = now + 300;
      logLine(String("waiting boot ") + bootCount + " reset " + resetName(esp_reset_reason()) +
              " latch " + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));
    }
    delay(10);
  }
  return false;
}

void setup() {
  holdLow();
  bootCount++;
  Serial.begin(115200);
  uint32_t waitStart = millis();
  while (!Serial && millis() - waitStart < 8000) {
    holdLow();
    delay(10);
  }
  holdLow();
  if (!waitForGo()) {
    logLine("no host start, D3 stays LOW");
    return;
  }

  logLine(String("motor-test ") + TEST_ID + " boot " + bootCount + " reset " + resetName(esp_reset_reason()));
  logLine(String("D3 GPIO4 latch ") + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));

  Preferences prefs;
  if (!prefs.begin("rt-motor", false)) {
    logLine("nvs unavailable, D3 stays LOW");
    return;
  }
  if (prefs.getString("id", "") == TEST_ID) {
    logLine("sequence already stored, D3 stays LOW");
    return;
  }
  countdown();
  if (prefs.putString("id", TEST_ID) == 0) {
    logLine("could not store one-shot flag, D3 stays LOW");
    return;
  }

  pulse("pulse1", PULSE_MS);
  pauseLow();
  pulse("pulse2", PULSE_MS);
  pauseLow();
  pulse("pulse3", PULSE_MS);
  holdLow();
  logLine("test finished, D3 stays LOW");
}

void loop() {
  holdLow();
  static uint32_t nextReport = 0;
  uint32_t now = millis();
  if ((int32_t)(now - nextReport) >= 0) {
    nextReport = now + 1000;
    logLine(String("alive latch ") + (digitalRead(MOTOR_PIN) == LOW ? "LOW" : "HIGH"));
  }
  delay(20);
}
