/*
  esp32_hr_test.ino - standalone MAX30102 heart rate test for the ESP32

  Wiring (power the sensor from 3.3V, NOT 5V):
    MAX30102 VIN -> ESP32 3V3
    MAX30102 GND -> ESP32 GND
    MAX30102 SDA -> ESP32 GPIO21
    MAX30102 SCL -> ESP32 GPIO22
    (INT pin not needed)

  Library: Arduino IDE -> Library Manager -> install
    "SparkFun MAX3010x Pulse and Proximity Sensor Library"

  Serial Monitor at 115200 baud.
  Type  p + Enter  to switch to plotter mode (raw IR only, for Tools > Serial Plotter),
  type  t + Enter  to go back to text mode.
*/

#include <Wire.h>
#include "MAX30105.h"      // works for the MAX30102 too
#include "heartRate.h"     // checkForBeat()

// ---------------- Config (tune here) ----------------
const int  SDA_PIN        = 21;
const int  SCL_PIN        = 22;
const long FINGER_IR_MIN  = 50000;  // IR below this = no finger on the sensor
const int  BPM_MIN        = 30;     // plausibility filter (same as the Pi side)
const int  BPM_MAX        = 220;
const byte AVG_BEATS      = 4;      // average over this many beats
const unsigned long PRINT_MS = 1000;

// sensor settings
const byte LED_POWER   = 0x1F;  // 0=off .. 0xFF=50mA (0x1F ~6mA is enough for a finger)
const byte SAMPLE_AVG  = 4;     // 1,2,4,8,16,32
const byte LED_MODE    = 2;     // 1=red only, 2=red+IR, 3=red+IR+green
const int  SAMPLE_RATE = 400;   // 50..3200
const int  PULSE_WIDTH = 411;   // 69,118,215,411
const int  ADC_RANGE   = 4096;  // 2048,4096,8192,16384

MAX30105 sensor;

byte  beats[AVG_BEATS];
byte  beatIndex   = 0;
byte  beatCount   = 0;
long  lastBeatMs  = 0;
float bpm         = 0;
int   avgBpm      = 0;
bool  plotMode    = false;
unsigned long lastPrint = 0;

void scanI2C() {
  Serial.println("[info] scanning I2C bus...");
  int found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  found device at 0x%02X%s\n", addr,
                    addr == 0x57 ? "  <- MAX30102" : "");
      found++;
    }
  }
  if (!found) Serial.println("  nothing found - check SDA/SCL wiring and 3.3V power");
}

void resetAverage() {
  beatIndex = beatCount = 0;
  avgBpm = 0;
  bpm = 0;
  lastBeatMs = 0;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== MAX30102 heart rate test (ESP32) ===");

  Wire.begin(SDA_PIN, SCL_PIN);
  scanI2C();

  // standard 100 kHz speed - clone boards are more reliable at this speed
  while (!sensor.begin(Wire, I2C_SPEED_STANDARD)) {
    Serial.println("[error] MAX30102 not found at 0x57 - retrying in 2 s...");
    delay(2000);
  }
  Serial.printf("[info] MAX30102 ready (part ID 0x%02X, rev %d)\n",
                sensor.readPartID(), sensor.getRevisionID());

  sensor.setup(LED_POWER, SAMPLE_AVG, LED_MODE, SAMPLE_RATE, PULSE_WIDTH, ADC_RANGE);
  sensor.setPulseAmplitudeGreen(0);   // green LED not used

  // die temperature = quick sanity check that registers read correctly
  sensor.enableDIETEMPRDY();
  Serial.printf("[info] sensor die temp: %.1f C\n", sensor.readTemperature());

  Serial.println("[info] place your finger gently on the sensor and keep still");
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'p') { plotMode = true;  }
    if (c == 't') { plotMode = false; Serial.println("[info] text mode"); }
  }
}

void loop() {
  handleSerial();

  long ir = sensor.getIR();
  bool finger = ir >= FINGER_IR_MIN;

  if (!finger) {
    if (beatCount) resetAverage();   // finger lifted -> start over
  } else if (checkForBeat(ir)) {
    long now = millis();
    long delta = now - lastBeatMs;
    lastBeatMs = now;
    bpm = 60000.0 / delta;

    if (bpm >= BPM_MIN && bpm <= BPM_MAX) {       // ignore impossible values
      beats[beatIndex] = (byte)bpm;
      beatIndex = (beatIndex + 1) % AVG_BEATS;
      if (beatCount < AVG_BEATS) beatCount++;

      int sum = 0;
      for (byte i = 0; i < beatCount; i++) sum += beats[i];
      avgBpm = sum / beatCount;
    }
  }

  if (plotMode) {                    // one number per line for Serial Plotter
    Serial.println(ir);
    return;
  }

  if (millis() - lastPrint >= PRINT_MS) {
    lastPrint = millis();
    if (!finger) {
      Serial.printf("IR=%6ld  no finger\n", ir);
    } else if (beatCount < AVG_BEATS) {
      Serial.printf("IR=%6ld  finger detected, reading... (%d/%d beats)\n",
                    ir, beatCount, AVG_BEATS);
    } else {
      Serial.printf("IR=%6ld  BPM=%5.1f  AVG BPM=%d\n", ir, bpm, avgBpm);
    }
  }
}
