/*
  scale_esp32.ino - MediKiosk weight scale (ESP32 + HX711 + 4x 50kg load cells)

  The ESP32 is the ONLY thing that talks to the HX711. It reads the load cells
  with exact timing (HX711_ADC library) and sends the finished weight to the
  Raspberry Pi over the USB cable. The Pi never touches DT/SCK.

  ---------------------------------------------------------------------------
  WIRING (same as the 2026-10-27 test that read 5 kg to within ~2 g)
      HX711 VCC -> ESP32 3.3V        HX711 GND -> ESP32 GND
      HX711 DT  -> ESP32 GPIO4       HX711 SCK -> ESP32 GPIO5
      Load cells -> HX711 E+/E-/A+/A- (unchanged)
      ESP32 USB  -> Raspberry Pi USB (power + data)

  LIBRARY: HX711_ADC by Olav Kallhovd (keep SCK_DELAY 1 in its config.h)
  BOARD:   "ESP32 Dev Module", upload, then close the Serial Monitor so the
           Pi can use the port.

  ---------------------------------------------------------------------------
  SERIAL PROTOCOL  (115200 baud, one message per line)

  ESP32 -> Pi
      READY:SCALE factor=19.1700   sent once after boot
      W:72.453                     weight in KG, 5 times per second
      OK:TARE                      tare finished
      CAL:19.1700                  new calibration factor (raw units per gram)
      FACTOR:19.1700               current factor
      OK:SAVED 19.1700             factor saved to flash
      PONG:SCALE                   reply to PING
      ERR:<text>                   something went wrong

  Pi -> ESP32
      PING           are you the scale?
      T              tare (platform must be EMPTY)
      C:<kg>         calibrate with a known weight on the platform, e.g. C:5
      S              save the current factor to flash (survives power off)
      F?             what is the current factor?
      F:<value>      set the factor by hand (not saved until S)

  You can also type these commands in the Arduino Serial Monitor (115200,
  "Newline") to test without the Pi.
*/

#include <HX711_ADC.h>
#include <EEPROM.h>

// ---------------- Settings ----------------
const int   HX711_DOUT  = 4;        // HX711 DT
const int   HX711_SCK   = 5;        // HX711 SCK
const float DEFAULT_CAL = 19.17;    // raw units per GRAM (5 kg calibration, 2026-10-27)
const int   EEPROM_ADDR = 0;        // same address the Calibration example saved to
const unsigned long SEND_MS = 200;  // send a weight every 200 ms (5 per second)
const unsigned long STABILIZE_MS = 2000;  // settle time after power-up

HX711_ADC LoadCell(HX711_DOUT, HX711_SCK);
float calFactor = DEFAULT_CAL;
bool tarePending = false;
bool newData = false;
unsigned long lastSend = 0;
String rxLine;

// ---------------- Calibration storage ----------------
float loadCalFactor() {
  float v;
  EEPROM.get(EEPROM_ADDR, v);
  // empty/garbage flash -> use the known-good default
  if (isnan(v) || isinf(v) || fabs(v) < 0.01 || fabs(v) > 1000000.0) v = DEFAULT_CAL;
  return v;
}

void saveCalFactor(float v) {
  EEPROM.put(EEPROM_ADDR, v);
  EEPROM.commit();
}

// ---------------- Commands from the Pi ----------------
void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "PING") {
    Serial.println("PONG:SCALE");

  } else if (cmd == "T") {
    LoadCell.tareNoDelay();               // finishes in the loop -> OK:TARE
    tarePending = true;

  } else if (cmd.startsWith("C:")) {
    float kg = cmd.substring(2).toFloat();
    if (kg <= 0) {
      Serial.println("ERR:bad mass - send C:<kg>, e.g. C:5");
      return;
    }
    LoadCell.refreshDataSet();            // fresh readings of the known weight
    calFactor = LoadCell.getNewCalibration(kg * 1000.0);   // factor stays per GRAM
    Serial.print("CAL:");
    Serial.println(calFactor, 4);

  } else if (cmd == "S") {
    saveCalFactor(calFactor);
    Serial.print("OK:SAVED ");
    Serial.println(calFactor, 4);

  } else if (cmd == "F?") {
    Serial.print("FACTOR:");
    Serial.println(calFactor, 4);

  } else if (cmd.startsWith("F:")) {
    float f = cmd.substring(2).toFloat();
    if (f == 0) {
      Serial.println("ERR:bad factor");
      return;
    }
    calFactor = f;
    LoadCell.setCalFactor(calFactor);
    Serial.print("FACTOR:");
    Serial.println(calFactor, 4);

  } else {
    Serial.print("ERR:unknown command ");
    Serial.println(cmd);
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLine.length() > 0) handleCommand(rxLine);
      rxLine = "";
    } else if (rxLine.length() < 40) {
      rxLine += c;
    }
  }
}

// ---------------- Setup / loop ----------------
void setup() {
  Serial.begin(115200);
  delay(50);
  EEPROM.begin(512);
  calFactor = loadCalFactor();

  LoadCell.begin();
  LoadCell.start(STABILIZE_MS, true);     // settle + tare (platform should be empty)
  if (LoadCell.getTareTimeoutFlag() || LoadCell.getSignalTimeoutFlag()) {
    while (true) {                        // keep telling the Pi what's wrong
      Serial.println("ERR:HX711 not responding - check DT=GPIO4, SCK=GPIO5, VCC, GND");
      delay(1000);
    }
  }
  LoadCell.setCalFactor(calFactor);

  Serial.print("READY:SCALE factor=");
  Serial.println(calFactor, 4);
}

void loop() {
  if (LoadCell.update()) newData = true;

  if (tarePending && LoadCell.getTareStatus()) {
    tarePending = false;
    Serial.println("OK:TARE");
  }

  if (newData && millis() - lastSend >= SEND_MS) {
    float kg = LoadCell.getData() / 1000.0;   // calFactor is per gram -> grams -> kg
    Serial.print("W:");
    Serial.println(kg, 3);
    newData = false;
    lastSend = millis();
  }

  readSerial();
}
