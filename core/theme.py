"""Glass design system - "Frosted Glass".

Near-black base, surfaces that read as lit glass panes (a faint sheen on the top
edge + a hairline border), one teal accent used sparingly, and a deliberately
raw light-hatched scrollbar. One set of tokens for the browser, the editor, and
the default .glass menus so the whole thing feels like one object.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- tokens ---------------------------------------------------------------
BASE = "#0a0c10"          # window background
PANE = "#10141b"          # surface
PANE2 = "#141a22"         # raised surface
EDGE = "rgba(255,255,255,0.09)"     # hairline border
EDGE_TOP = "rgba(255,255,255,0.16)"  # lit top edge of the "glass"
SHEEN = "rgba(255,255,255,0.06)"     # faint light catch at the top
TEXT = "#e7edf3"
MUTED = "#8b95a1"
ACCENT = "#6cf09a"
ACCENT_DIM = "rgba(108,240,154,0.38)"
ACCENT_FILL = "rgba(108,240,154,0.14)"
RADIUS = 6


def _u(name):
    return os.path.join(HERE, "assets", name).replace("\\", "/")


# ---- frosted surfaces -----------------------------------------------------
def frost(base=PANE, radius=RADIUS):
    """A glass pane: faint top sheen over `base`, hairline border, lit top edge."""
    return (f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {SHEEN}, stop:0.10 {base}, stop:1 {base});"
            f"border:1px solid {EDGE};border-top:1px solid {EDGE_TOP};"
            f"border-radius:{radius}px;")


def button(face=None, talign="center"):
    bg = face or ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                  f"stop:0 {SHEEN}, stop:0.14 {PANE}, stop:1 #0d1117)")
    hover = face or ("qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                     f"stop:0 {SHEEN}, stop:0.14 {PANE2}, stop:1 #11161d)")
    return (
        f"QPushButton{{background:{bg};color:{TEXT};border:1px solid {EDGE};"
        f"border-top:1px solid {EDGE_TOP};border-radius:{RADIUS}px;"
        f"padding:6px 12px;text-align:{talign};}}"
        f"QPushButton:hover{{background:{hover};border:1px solid {ACCENT_DIM};"
        f"border-top:1px solid {ACCENT_DIM};}}"
        f"QPushButton:pressed{{background:rgba(0,0,0,0.25);}}")


def primary_button():
    return (
        f"QPushButton{{background:{ACCENT_FILL};color:{ACCENT};"
        f"border:1px solid {ACCENT_DIM};border-radius:{RADIUS}px;padding:6px 12px;"
        f"font-weight:600;}}"
        f"QPushButton:hover{{background:rgba(108,240,154,0.22);}}")


def input_field(bg=None):
    return (
        f"QLineEdit{{background:{bg or 'rgba(0,0,0,0.30)'};color:{TEXT};"
        f"border:1px solid {EDGE};border-top:1px solid {EDGE_TOP};"
        f"border-radius:{RADIUS}px;padding:6px 10px;selection-background-color:{ACCENT_DIM};}}"
        f"QLineEdit:focus{{border:1px solid {ACCENT_DIM};}}")


# ---- raw hatched scrollbar (matches the classic look) ---------------------
def scrollbar_qss():
    track = _u("hatch_track.png")
    up, down = _u("h_up.png"), _u("h_down.png")
    left, right = _u("h_left.png"), _u("h_right.png")
    edge = "1px solid #5a5a5a"
    return f"""
QScrollBar:vertical {{
    background-image:url("{track}"); width:16px; margin:16px 0 16px 0; border:{edge};
}}
QScrollBar::handle:vertical {{ background:#141414; min-height:28px; border:1px solid #000; }}
QScrollBar::sub-line:vertical {{ subcontrol-position:top; subcontrol-origin:margin;
    height:15px; background-image:url("{track}"); border:{edge}; }}
QScrollBar::add-line:vertical {{ subcontrol-position:bottom; subcontrol-origin:margin;
    height:15px; background-image:url("{track}"); border:{edge}; }}
QScrollBar::up-arrow:vertical {{ image:url("{up}"); width:9px; height:9px; }}
QScrollBar::down-arrow:vertical {{ image:url("{down}"); width:9px; height:9px; }}

QScrollBar:horizontal {{
    background-image:url("{track}"); height:16px; margin:0 16px 0 16px; border:{edge};
}}
QScrollBar::handle:horizontal {{ background:#141414; min-width:28px; border:1px solid #000; }}
QScrollBar::sub-line:horizontal {{ subcontrol-position:left; subcontrol-origin:margin;
    width:15px; background-image:url("{track}"); border:{edge}; }}
QScrollBar::add-line:horizontal {{ subcontrol-position:right; subcontrol-origin:margin;
    width:15px; background-image:url("{track}"); border:{edge}; }}
QScrollBar::left-arrow:horizontal {{ image:url("{left}"); width:9px; height:9px; }}
QScrollBar::right-arrow:horizontal {{ image:url("{right}"); width:9px; height:9px; }}

QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
"""


def apply_theme(app):
    """App-wide: the raw hatched scrollbar everywhere."""
    try:
        app.setStyleSheet((app.styleSheet() or "") + scrollbar_qss())
    except Exception:
        pass


def web_scrollbar_css():
    """CSS that gives web pages the same light-hatched scrollbar (Chromium)."""
    hatch = ("repeating-linear-gradient(45deg,#8c8c8c 0,#8c8c8c 1px,"
             "#b3b3b3 1px,#b3b3b3 4px)")
    up = ("url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" "
          "width=\"9\" height=\"9\"><polygon points=\"4,2 7,6 1,6\" fill=\"%23141414\"/></svg>')")
    down = ("url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" "
            "width=\"9\" height=\"9\"><polygon points=\"1,3 7,3 4,7\" fill=\"%23141414\"/></svg>')")
    return (
        "::-webkit-scrollbar{width:16px !important;height:16px !important;}"
        f"::-webkit-scrollbar-track{{background-color:#b3b3b3 !important;"
        f"background-image:{hatch} !important;border:1px solid #5a5a5a !important;}}"
        "::-webkit-scrollbar-thumb{background:#141414 !important;"
        "border:1px solid #000 !important;border-radius:0 !important;}"
        f"::-webkit-scrollbar-button{{display:block !important;height:15px !important;"
        f"width:16px !important;background-color:#b3b3b3 !important;"
        f"background-image:{hatch} !important;border:1px solid #5a5a5a !important;"
        "background-repeat:no-repeat;background-position:center;}"
        f"::-webkit-scrollbar-button:vertical:decrement{{background-image:{up},{hatch} !important;}}"
        f"::-webkit-scrollbar-button:vertical:increment{{background-image:{down},{hatch} !important;}}"
        "::-webkit-scrollbar-button:horizontal{display:block !important;}"
        "::-webkit-scrollbar-corner{background:#b3b3b3 !important;}")
