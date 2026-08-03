"""Waveshare 1.3inch LCD HAT user interface for the e-paper demo.

Layers, so everything except `display`/`inputs` runs without hardware:

  config   - pin numbers and layout constants
  patterns - the selectable demo catalogue (pure data + generators)
  runner   - background thread driving the panels, emits log lines
  render   - draws a screen into a PIL image (pure, snapshot-testable)
  display  - ST7789 backend, plus PNG/null backends for headless work
  inputs   - GPIO buttons, plus a keyboard backend for headless work
  app      - state machine gluing the above together
"""
