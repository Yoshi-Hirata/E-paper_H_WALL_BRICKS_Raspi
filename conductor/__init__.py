"""PC-side show control for the garment looks.

One look (garment) is one Radxa unit driving up to 60 boards of 60
scales. The designers hand over two kinds of CSV per look - a map
(which scale sits where, on which board and socket) and one colour grid
per cue - and this package turns them into the 64-byte arrays the
boards take (conductor/look.py), draws what the garment will look like
(conductor/preview.py), and later distributes and fires the cues.
"""
