/*
  esp32_temp_test.ino - standalone MLX90614 (IR thermometer) test for the ESP32

  Wiring (power the sensor from 3.3V, NOT 5V):
    MLX90614 VIN -> ESP32 3V3
    MLX90614 GND -> ESP32 GND
    MLX90614 SDA -> ESP32 GPIO21
    MLX90614 SCL -> ESP32 GPIO22
  (can share SDA/SCL with the MAX30102 - different addresses: 0x5A vs 0x57)

  Library: Arduino IDE -> Library Manager -> install
    "Adafruit MLX90614 Library"

  Serial Monitor at 115200 baud.
  Type  m + Enter  to take one averaged "measurement" (like the kiosk will do),
  otherwise it prints live readings every second.
*/

#include <Wire.h>
#include <Adafruit_MLX90614.h>

// ---------------- Config (tune here) ----------------
const int   SDA_PIN       = 21;
const int   SCL_PIN       = 22;
const uint8_t MLX_ADDR    = 0x5A;
const int   READ_RETRIES  = 3;       // retry a failed read instead of slowing the whole bus
const int   AVG_SAMPLES   = 10;      // samples for one measurement
const int   SAMPLE_GAP_MS = 100;
const float OFFSET_C      = 0.0;     // calibration offset (forehead/wrist reads low - tune vs a real thermometer)
const float BODY_MIN_C    = 30.0;    // plausibility filter for a person
const float BODY_MAX_C    = 43.0;
const unsigned long PRINT_MS = 1000;

Adafruit_MLX90614 mlx;
unsigned long lastPrint = 0;

void scanI2C() {
  Serial.println("[info] scanning I2C bus...");
  int found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  found device at 0x%02X%s\n", addr,
                    addr == MLX_ADDR ? "  <- MLX90614" :
                    addr == 0x57     ? "  <- MAX30102" : "");
      found++;
    }
  }
  if (!found) Serial.println("  nothing found - check SDA/SCL wiring and 3.3V power");
}

// read with retries; returns NAN if every try failed
float readObject() {
  for (int i = 0; i < READ_RETRIES; i++) {
    float t = mlx.readObjectTempC();
    if (!isnan(t) && t > -70 && t < 380) return t;   // sensor's real range
    delay(20);
  }
  return NAN;
}

float readAmbient() {
  for (int i = 0; i < READ_RETRIES; i++) {
    float t = mlx.readAmbientTempC();
    if (!isnan(t) && t > -40 && t < 125) return t;
    delay(20);
  }
  return NAN;
}

// one kiosk-style measurement: several samples, drop highest + lowest, average the rest
void measure() {
  Serial.println("[info] measuring - hold still ~3 cm from the sensor...");
  float s[AVG_SAMPLES];
  int n = 0;
  for (int i = 0; i < AVG_SAMPLES; i++) {
    float t = readObject();
    if (!isnan(t)) s[n++] = t;
    delay(SAMPLE_GAP_MS);
  }
  if (n < 3) {
    Serial.println("[error] too many failed reads - check wiring/power");
    return;
  }
  // simple sort, then trim one from each end
  for (int i = 0; i < n - 1; i++)
    for (int j = i + 1; j < n; j++)
      if (s[j] < s[i]) { float x = s[i]; s[i] = s[j]; s[j] = x; }
  float sum = 0;
  for (int i = 1; i < n - 1; i++) sum += s[i];
  float temp = sum / (n - 2) + OFFSET_C;

  Serial.printf("[RESULT] %.1f C  (%d good samples)\n", temp, n);
  if (temp < BODY_MIN_C || temp > BODY_MAX_C)
    Serial.println("         outside normal body range - too far away or nothing in front of the sensor?");
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== MLX90614 temperature test (ESP32) ===");

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);   // MLX90614 supports max 100 kHz
  scanI2C();

  while (!mlx.begin(MLX_ADDR, &Wire)) {
    Serial.println("[error] MLX90614 not found at 0x5A - retrying in 2 s...");
    delay(2000);
  }
  Serial.printf("[info] MLX90614 ready (emissivity %.2f)\n", mlx.readEmissivity());
  Serial.println("[info] live readings below - type m + Enter for one averaged measurement");
}

void loop() {
  while (Serial.available()) {
    if (Serial.read() == 'm') measure();
  }

  if (millis() - lastPrint >= PRINT_MS) {
    lastPrint = millis();
    float obj = readObject();
    float amb = readAmbient();
    if (isnan(obj) || isnan(amb)) {
      Serial.println("[warn] read failed after retries");
    } else {
      Serial.printf("Object=%5.1f C  Ambient=%5.1f C\n", obj + OFFSET_C, amb);
    }
  }
}
