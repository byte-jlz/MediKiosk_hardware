import subprocess
from time import sleep

PIN = "17"

def release():
    subprocess.run(["pinctrl", "set", PIN, "ip", "pu"])

def press_start(duration=0.3):
    subprocess.run(["pinctrl", "set", PIN, "op", "dl"])  # press
    sleep(duration)
    release()                                            # release -> starts

release()
input("Press Enter to trigger the BP monitor...")
press_start()
print("Triggered!")