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

# --- UI behaviour ---
# Input poll interval. Redraws are separate and only happen when the
# screen content actually changes (see App._display_key), so this can be
# short enough to feel responsive without wasting frames.
FRAME_INTERVAL_S = 0.1
LOG_LINES = 6            # log rows visible on the running screen
LOG_HISTORY = 200        # lines kept in memory
