"""Hardware pin map and UI constants for the Waveshare 1.3inch LCD HAT.

Pin numbers are BCM, taken from the Waveshare 1.3inch LCD HAT wiki. The
HAT drives an ST7789 over SPI0 and exposes a 5-way joystick plus three
push buttons, all active-low with pull-ups.
"""

# --- ST7789 display (SPI0, CE0) ---
SPI_BUS = 0
SPI_DEVICE = 0
SPI_HZ = 40_000_000
PIN_DC = 25
PIN_RST = 27
PIN_BL = 24

WIDTH = 240
HEIGHT = 240

# --- inputs (active low, internal pull-up) ---
PIN_KEY1 = 21
PIN_KEY2 = 20
PIN_KEY3 = 16
PIN_UP = 6
PIN_DOWN = 19
PIN_LEFT = 5
PIN_RIGHT = 26
PIN_PRESS = 13

BUTTON_PINS = {
    "up": PIN_UP,
    "down": PIN_DOWN,
    "left": PIN_LEFT,
    "right": PIN_RIGHT,
    "press": PIN_PRESS,
    "key1": PIN_KEY1,
    "key2": PIN_KEY2,
    "key3": PIN_KEY3,
}

# Holding KEY1 this long resets the demo instead of pausing it. The hold
# is reported as its own event and suppresses the short press, so one
# press never does both.
KEY1_HOLD_S = 1.0
EVENTS = tuple(BUTTON_PINS) + ("key1_hold",)

# --- UI behaviour ---
# Input poll interval. Redraws are separate and only happen when the
# screen content actually changes (see App._display_key), so this can be
# short enough to feel responsive without wasting frames.
FRAME_INTERVAL_S = 0.1
LOG_LINES = 6            # log rows visible on the running screen
LOG_HISTORY = 200        # lines kept in memory

# Blank the backlight after this long without a button press; 0 disables
# it. Off by default - an operator wants to glance at the panel state
# without touching anything, and a dark screen reads as "it died". Turn
# it on for battery work with `--blank-after 10`, worth roughly 20-40 mA
# (docs/POWER.md). The demo keeps running either way, and any press wakes
# the screen without changing anything else.
BLANK_AFTER_S = 0.0

# Show lock. Locked, the buttons do nothing, so a knock during a show
# cannot stop the demo. This sequence within UNLOCK_WINDOW_S unlocks;
# the lock comes back on its own after RELOCK_AFTER_S of no input, so an
# operator never has to remember to re-arm it.
UNLOCK_SEQUENCE = ("key2", "key3", "key2")
UNLOCK_WINDOW_S = 5.0
RELOCK_AFTER_S = 60.0

# How often to pet systemd's watchdog (WatchdogSec must be well above).
WATCHDOG_PERIOD_S = 5.0
