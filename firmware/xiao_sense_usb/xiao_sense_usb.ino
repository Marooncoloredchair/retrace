// XIAO ESP32S3 Sense camera and GY-521 IMU over native USB.
// JPEG frames are the only image data. IMU samples are a separate frame type.
// Diagnostics use text frames, so neither text nor IMU bytes sit inside a JPEG.
// No Wi-Fi and no SD card.
// IMU wiring: VCC 3V3, GND GND, SDA D4/GPIO5, SCL D5/GPIO6.

#include "esp_camera.h"
#include <Wire.h>

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
static const size_t MAX_JPEG = 100000;
static const int IMU_SDA = 5;
static const int IMU_SCL = 6;
static const uint8_t IMU_ADDR_LOW = 0x68;
static const uint8_t IMU_ADDR_HIGH = 0x69;
static const uint32_t IMU_PERIOD_MS = 20;
static const uint32_t USB_WRITE_TIMEOUT_MS = 400;

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

static bool writeAll(const uint8_t *data, size_t length) {
  size_t sent = 0;
  uint32_t started = millis();
  while (sent < length) {
    if (millis() - started > USB_WRITE_TIMEOUT_MS) return false;
    size_t chunk = length - sent;
    if (chunk > 256) chunk = 256;
    size_t wrote = Serial.write(data + sent, chunk);
    if (wrote == 0) {
      delay(1);
      continue;
    }
    sent += wrote;
    started = millis();
  }
  return true;
}

static bool sendFrame(uint8_t type, const uint8_t *payload, uint32_t length) {
  uint8_t header[8] = {
    FRAME_MAGIC_0,
    FRAME_MAGIC_1,
    type,
    0,
    (uint8_t)(length & 0xFF),
    (uint8_t)((length >> 8) & 0xFF),
    (uint8_t)((length >> 16) & 0xFF),
    (uint8_t)((length >> 24) & 0xFF),
  };
  uint32_t crc = crc32_ieee(payload, length);
  uint8_t tail[4] = {
    (uint8_t)(crc & 0xFF),
    (uint8_t)((crc >> 8) & 0xFF),
    (uint8_t)((crc >> 16) & 0xFF),
    (uint8_t)((crc >> 24) & 0xFF),
  };
  if (!writeAll(header, sizeof(header))) return false;
  if (length && !writeAll(payload, length)) return false;
  return writeAll(tail, sizeof(tail));
}

static void sendText(const String &text) {
  sendFrame(FRAME_TEXT, reinterpret_cast<const uint8_t *>(text.c_str()), text.length());
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
    imuLine = scan + " no usable imu";
    return false;
  }

  if (!i2cWrite(address, 0x6B, 0x80)) {
    imuReady = false;
    imuLine = scan + " reset write failed";
    sendText(imuLine);
    return false;
  }
  delay(100);
  if (!i2cWrite(address, 0x6B, 0x00) || !i2cWrite(address, 0x1C, 0x00) || !i2cWrite(address, 0x1B, 0x00)) {
    imuReady = false;
    imuLine = scan + " config write failed";
    sendText(imuLine);
    return false;
  }
  delay(50);

  uint8_t accelCfg = 0xFF;
  uint8_t gyroCfg = 0xFF;
  uint8_t whoRead = 0;
  if (!i2cRead(address, 0x1C, &accelCfg, 1) || !i2cRead(address, 0x1B, &gyroCfg, 1) ||
      !i2cRead(address, 0x75, &whoRead, 1) || !usableWhoami(whoRead)) {
    imuReady = false;
    imuLine = scan + " config readback failed";
    sendText(imuLine);
    return false;
  }

  imuAddress = address;
  imuWho = whoRead;
  imuAccelFs = (accelCfg >> 3) & 0x03;
  imuGyroFs = (gyroCfg >> 3) & 0x03;
  imuReady = true;
  imuLine = String("imu ") + imuName(whoRead) + " address 0x" + String(address, HEX) +
            " whoami 0x" + String(whoRead, HEX) +
            " sda GPIO5 scl GPIO6 accel_cfg 0x" + String(accelCfg, HEX) +
            " gyro_cfg 0x" + String(gyroCfg, HEX);
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
  config.frame_size = FRAMESIZE_QVGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
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
  sensor->set_framesize(sensor, FRAMESIZE_QVGA);
  sensor->set_quality(sensor, 12);
  uint16_t pid = sensor->id.PID;
  sensorLine = String("sensor ") + sensorName(pid) + " pid 0x" + String(pid, HEX) +
               " xclk " + xclkHz + " framesize QVGA jpeg";
  sendText(sensorLine);
  cameraError = "";
  return true;
}

static bool beginImuBus(TwoWire &wire, const char *name) {
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
  Serial.begin(115200);
  delay(300);
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
  uint32_t now = millis();
  if (imuReady && (int32_t)(now - nextImuMs) >= 0) {
    nextImuMs = now + IMU_PERIOD_MS;
    uint8_t packet[22];
    if (!readImuPacket(packet)) {
      if ((int32_t)(now - nextImuTextMs) >= 0) {
        nextImuTextMs = now + 1000;
        sendText(String("imu read failed i2c ") + lastI2cError + " " + imuLine);
      }
    } else {
      sendFrame(FRAME_IMU, packet, sizeof(packet));
    }
  } else if (!imuReady && i2cReady && (int32_t)(now - nextImuTextMs) >= 0) {
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
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) {
    captureErrors++;
    sendText(String("capture failed, count ") + captureErrors);
    delay(100);
    return;
  }
  bool jpeg = frame->format == PIXFORMAT_JPEG && frame->len >= 4 && frame->len <= MAX_JPEG &&
              frame->buf[0] == 0xFF && frame->buf[1] == 0xD8;
  if (jpeg) {
    if (sendFrame(FRAME_JPEG, frame->buf, frame->len)) {
      framesSent++;
      if (framesSent == 1 || framesSent % 30 == 0) {
        sendText(sensorLine + " sent " + framesSent + " psram free " + ESP.getFreePsram());
      }
    }
  } else {
    captureErrors++;
    sendText(String("frame was not a usable jpeg, bytes ") + frame->len);
  }
  esp_camera_fb_return(frame);
}
