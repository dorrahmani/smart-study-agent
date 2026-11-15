"""Render the 'Quiet Cartography' canvas for the Smart Study Agent redesign."""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import math, random
from bidi.algorithm import get_display

def heb(s: str) -> str:
    return get_display(s)

OUT = Path(__file__).parent / 'CANVAS.png'
import os
FONTS_LATIN = Path(os.environ.get('CANVAS_FONTS_DIR', Path(__file__).parent / 'fonts'))   # folder with the Instrument Serif / Sans .ttf files
WIN = Path(r'C:\Windows\Fonts')

W, H = 2000, 2750  # ~ A2 portrait at 200dpi-ish
PAPER = (244, 240, 230)        # warm bone
INK   = (28, 28, 30)           # near-black ink
INK2  = (90, 88, 82)            # secondary ink
RULE  = (60, 58, 55)
RED   = (164, 46, 36)           # oxidized red
FAINT = (210, 204, 192)

def F(path, size):
    return ImageFont.truetype(str(path), size)

# Type stack
serif_d  = F(FONTS_LATIN / 'InstrumentSerif-Regular.ttf', 220)   # display serif
serif_xl = F(FONTS_LATIN / 'InstrumentSerif-Regular.ttf', 360)
serif_xl2= F(FONTS_LATIN / 'InstrumentSerif-Regular.ttf', 300)
serif_md = F(FONTS_LATIN / 'InstrumentSerif-Italic.ttf',   54)
grot_xl  = F(FONTS_LATIN / 'InstrumentSans-Regular.ttf',  92)
grot_md  = F(FONTS_LATIN / 'InstrumentSans-Regular.ttf',  28)
grot_sm  = F(FONTS_LATIN / 'InstrumentSans-Regular.ttf',  18)
grot_bd  = F(FONTS_LATIN / 'InstrumentSans-Bold.ttf',     22)
mono_md  = F(FONTS_LATIN / 'JetBrainsMono-Regular.ttf',   20)
mono_sm  = F(FONTS_LATIN / 'JetBrainsMono-Regular.ttf',   15)
num_xxl  = F(FONTS_LATIN / 'InstrumentSerif-Regular.ttf', 560)

# Hebrew (Windows)
heb_md   = F(WIN / 'frank.ttf', 36)
heb_sm   = F(WIN / 'frank.ttf', 22)
heb_xs   = F(WIN / 'segoeui.ttf', 17)
heb_lg   = F(WIN / 'frank.ttf', 64)
heb_xl   = F(WIN / 'FRANKB.TTF', 110)

img = Image.new('RGB', (W, H), PAPER)
d = ImageDraw.Draw(img)

# ---- subtle paper texture (very gentle noise) ----
random.seed(7)
for _ in range(12000):
    x = random.randint(0, W-1); y = random.randint(0, H-1)
    v = random.randint(-6, 4)
    r,g,b = PAPER
    img.putpixel((x,y), (max(0,min(255,r+v)), max(0,min(255,g+v)), max(0,min(255,b+v))))

# ---- tracked uppercase helper ----
def tracked(draw, xy, text, font, fill, tracking=4, anchor='lt'):
    x, y = xy
    if anchor == 'rt':  # right top, draw RTL placement
        # measure
        total = sum(draw.textlength(c, font=font) for c in text) + tracking*(len(text)-1)
        x = x - total
    cx = x
    for c in text:
        draw.text((cx, y), c, font=font, fill=fill)
        cx += draw.textlength(c, font=font) + tracking
    return cx

# Margins
ML, MR, MT, MB = 140, 140, 140, 160

# ---- top hairline frame ----
d.line([(ML, MT), (W-MR, MT)], fill=RULE, width=1)
d.line([(ML, H-MB), (W-MR, H-MB)], fill=RULE, width=1)

# ---- top header strip ----
y0 = MT - 60
tracked(d, (ML, y0), "SMART  STUDY  AGENT", grot_sm, INK, tracking=6)
tracked(d, (W-MR, y0), "PLATE  №  01", grot_sm, INK, tracking=6, anchor='rt')

# ---- masthead ----
y = MT + 50
d.text((ML, y), "Quiet", font=serif_xl, fill=INK)
# Hebrew title beneath, RTL aligned right
d.text((W-MR, y+60), heb("מוח שני"), font=heb_xl, fill=INK, anchor='rt')
y2 = y + 360
d.text((ML, y2 - 40), "Cartography", font=serif_xl2, fill=INK)

# subtitle line
d.line([(ML, y2 + 320), (W-MR, y2 + 320)], fill=RULE, width=1)

# ---- subtitle row ----
sy = y2 + 340
tracked(d, (ML, sy), "AN  INSTRUMENT  FOR  PATIENT  STUDY", grot_md, INK, tracking=8)
d.text((W-MR, sy+4), heb("מהדורה ב׳ · תשפ״ו"), font=heb_sm, fill=INK2, anchor='rt')

# ---- main grid: left = giant numeral, right = ledger ----
gy = sy + 110
d.line([(ML, gy), (W-MR, gy)], fill=RULE, width=1)

# Left column big number (today's deadlines)
col_split = ML + int((W - ML - MR) * 0.55)
d.line([(col_split, gy), (col_split, gy + 900)], fill=RULE, width=1)

# === LEFT BLOCK ===
lx = ML + 60
ly = gy + 60
tracked(d, (lx, ly), "DEADLINES  ·  72H", grot_sm, INK2, tracking=6)
d.text((lx, ly + 40), heb("מטלות פתוחות"), font=heb_sm, fill=INK2)

# the ONE accent — the big red number
big = "03"
d.text((lx, ly + 80), big, font=num_xxl, fill=RED)

# deadlines listed below the number, inside the left column
ann_x = lx
ann_y = ly + 660
ann_w = (col_split - lx) - 80
for i, (label_he, label_en, when) in enumerate([
    (heb("חשבון אינפי 2 · תרגיל 9"), "CALCULUS II · PSET 09", "30·IV · 14:00"),
    (heb("מבני נתונים · הגשה"),      "DATA STRUCT. · SUBMIT", "02·V  · 23:55"),
    (heb("מיקרו־כלכלה · מבחן ב׳"),   "MICROECON.   · MIDTERM","05·V  · 09:00"),
]):
    yy = ann_y + i*68
    d.line([(ann_x, yy), (ann_x+ann_w, yy)], fill=RULE, width=1)
    tracked(d, (ann_x, yy+10), label_en, grot_bd, INK, tracking=3)
    d.text((ann_x+ann_w, yy+10), when, font=mono_md, fill=INK, anchor='rt')
    d.text((ann_x+ann_w, yy+42), label_he, font=heb_sm, fill=INK2, anchor='rt')

# === RIGHT BLOCK : ledger of the week ===
rx = col_split + 60
ry = gy + 60
tracked(d, (rx, ry), "LEDGER  ·  WEEK  18", grot_sm, INK2, tracking=6)
d.text((W-MR-60, ry+40), heb("פנקס השבוע"), font=heb_sm, fill=INK2, anchor='rt')

ledger = [
    ("28·IV", "L", "Calculus II",       heb("המשפט היסודי"),        "0.42"),
    ("28·IV", "T", "Calculus II",       heb("אינטגרלים מוכללים"),   "0.18"),
    ("27·IV", "L", "Linear Algebra II", heb("ערכים עצמיים"),        "0.51"),
    ("27·IV", "T", "Discrete Math II",  heb("פונקציות יוצרות"),     "0.33"),
    ("26·IV", "L", "Microeconomics",    heb("שיווי משקל כללי"),     "0.27"),
    ("26·IV", "Q", "Intro to CS",       heb("רקורסיה · חידון"),     "0.61"),
    ("25·IV", "L", "Discrete Math II",  heb("נוסחת הנסיגה"),        "0.22"),
    ("25·IV", "P", "Calculus II",       heb("פודקאסט סדרות"),       "—"),
    ("24·IV", "T", "Microeconomics",    heb("פונקציית תועלת"),      "0.14"),
    ("24·IV", "L", "Linear Algebra II", heb("אלגוריתם גרם־שמידט"),  "0.38"),
]

ly2 = ry + 110
row_h = 76
right_edge = W - MR - 60
col_date   = rx
col_type   = rx + 110
col_course = rx + 170
col_w      = right_edge        # weakness number anchor (right-aligned, far right)
col_topic  = right_edge - 100  # topic Hebrew (right-aligned, just left of weakness)

# header row
d.line([(rx, ly2-22), (right_edge, ly2-22)], fill=RULE, width=1)
tracked(d, (col_date, ly2-12), "DATE",    grot_sm, INK2, tracking=3)
tracked(d, (col_type, ly2-12), "T",       grot_sm, INK2, tracking=3)
tracked(d, (col_course, ly2-12), "COURSE", grot_sm, INK2, tracking=3)
tracked(d, (col_topic, ly2-12), "TOPIC", grot_sm, INK2, tracking=3, anchor='rt')
tracked(d, (col_w, ly2-12), "W.", grot_sm, INK2, tracking=3, anchor='rt')
d.line([(rx, ly2+8), (right_edge, ly2+8)], fill=RULE, width=1)

for i, (date, t, course, topic_he, w) in enumerate(ledger):
    yy = ly2 + 24 + i*row_h
    d.text((col_date, yy), date, font=mono_md, fill=INK)
    # type glyph in a tiny box
    d.rectangle([col_type, yy, col_type+26, yy+26], outline=RULE, width=1)
    d.text((col_type+13, yy+13), t, font=grot_sm, fill=INK, anchor='mm')
    d.text((col_course, yy), course, font=grot_md, fill=INK)
    # topic Hebrew, right-aligned
    d.text((col_topic, yy+2), topic_he, font=heb_sm, fill=INK2, anchor='rt')
    # weakness as a small numeral; red iff >= 0.5
    if w != '—':
        wf = float(w)
        fill = RED if wf >= 0.5 else INK
        d.text((col_w, yy+4), w, font=mono_md, fill=fill, anchor='rt')
        # tiny dot to the left of the number when red (the "found-at-a-glance" mark)
        if wf >= 0.5:
            d.ellipse([col_w-72, yy+12, col_w-66, yy+18], fill=RED)
    else:
        d.text((col_w, yy+4), "—", font=mono_md, fill=INK2, anchor='rt')
    d.line([(rx, yy+row_h-12), (right_edge, yy+row_h-12)], fill=FAINT, width=1)

# ---- bottom band : the topographic / concept index ----
by = gy + 960
d.line([(ML, by), (W-MR, by)], fill=RULE, width=1)
tracked(d, (ML, by + 24), "CONCEPT  INDEX  ·  CROSS-COURSE  HUBS", grot_sm, INK2, tracking=6)
d.text((W-MR, by + 28), heb("מושגי־יסוד מקשרים"), font=heb_sm, fill=INK2, anchor='rt')

# concept islands : small clusters of dots with a center label
random.seed(11)
islands = [
    ("recursion",         heb("רקורסיה"),      0.16, 0.58, 7),
    ("eigenvalue",        heb("ערכים עצמיים"), 0.38, 0.74, 9),
    ("integration",       heb("אינטגרציה"),    0.60, 0.52, 11),
    ("equilibrium",       heb("שיווי משקל"),   0.82, 0.78, 6),
    ("generating_fn",     heb("פונקציה יוצרת"),0.28, 0.28, 5),
    ("utility",           heb("תועלת"),        0.72, 0.22, 6),
]
band_top = by + 90
band_bot = H - MB - 200
band_h = band_bot - band_top
band_w = W - ML - MR
for (en, he, fx, fy, n) in islands:
    cx = ML + int(band_w * fx)
    cy = band_top + int(band_h * fy)
    # dot cluster
    for _ in range(n):
        ang = random.random()*math.tau
        r = random.gauss(28, 14)
        px = cx + math.cos(ang)*r
        py = cy + math.sin(ang)*r
        rad = random.choice([1,1,1,2,2,3])
        d.ellipse([px-rad, py-rad, px+rad, py+rad], fill=INK)
    # center mark
    d.ellipse([cx-4, cy-4, cx+4, cy+4], outline=INK, width=1, fill=PAPER)
    # leader line + labels offset
    lx_ = cx + 60
    d.line([(cx+6, cy), (lx_-6, cy)], fill=RULE, width=1)
    tracked(d, (lx_, cy-22), en.upper().replace('_',' '), grot_bd, INK, tracking=2)
    d.text((lx_, cy+4), he, font=heb_xs, fill=INK2)

# faint contour-like horizontal lines across the band (cartographic feel)
for i in range(6):
    yy = band_top + int(band_h * (i/6.0)) + 8
    # broken line
    x = ML
    while x < W - MR:
        seg = random.randint(40, 110)
        d.line([(x, yy), (min(x+seg, W-MR), yy)], fill=FAINT, width=1)
        x += seg + random.randint(20, 60)

# ---- footer ----
fy_ = H - MB + 30
tracked(d, (ML, fy_), "TOPOGRAPHY  OF  ATTENTION", grot_sm, INK2, tracking=6)
d.text((W//2, fy_+4), "—  ◇  —", font=grot_md, fill=INK2, anchor='mt')
tracked(d, (W-MR, fy_), "TEL  AVIV", grot_sm, INK2, tracking=6, anchor='rt')

# ---- masthead micro-rule (decorative single accent dot, the only red gesture besides number+bar) ----
# already used red on big numeral & one weakness bar; keep restrained.

img.save(OUT, 'PNG', optimize=True)
print(f"wrote {OUT}  {OUT.stat().st_size//1024} KB  {img.size}")
