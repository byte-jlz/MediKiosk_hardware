import subprocess
from time import sleep

PIN = "27"   # pin 13 (GPIO17 / pin 11 is damaged)

def release():
    subprocess.run(["pinctrl", "set", PIN, "ip", "pn"])  # no pull: the monitor has its own

def press_start(duration=0.9):
    subprocess.run(["pinctrl", "set", PIN, "op", "dl"])  # press
    sleep(duration)
    release()                                            # release -> starts

release()
input("Press Enter to trigger the BP monitor...")
press_start()
print("Triggered!")