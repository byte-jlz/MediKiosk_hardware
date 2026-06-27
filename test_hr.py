import max30102, hrcalc

m = max30102.MAX30102()
print("Place a fingertip gently on the sensor and hold still...")

while True:
    red, ir = m.read_sequential()
    hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
    if hr_valid and spo2_valid:
        print(f"Heart rate: {hr:.0f} bpm   SpO2: {spo2:.0f}%")
    else:
        print("...reading, keep your finger still...")