import RPi.GPIO as GPIO  # pyright: ignore[reportMissingModuleSource]  (RPi.GPIO only installs on the Pi)
from time import sleep

PIN = 17
GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)   # released

def press_start(duration=1.0):
    GPIO.setup(PIN, GPIO.OUT, initial=GPIO.LOW)      # press
    sleep(duration)
    GPIO.setup(PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # release