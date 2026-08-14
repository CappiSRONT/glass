"""
Glass Editor
============
A focused code editor for .glass files:
  - left  : VS-Code-style editor (line numbers, syntax highlighting)
  - right : LIVE preview that re-renders as you type
  - built-in interactive Tutorial (auto-opens on first launch)

Run:  python editor.py   (or click "Edit" in the browser, or Ctrl+E)
"""

from __future__ import annotations
import os
import sys
import math

# First-run / missing-dependency safety net (editor only needs PyQt6).
from bootstrap import ensure_dependencies, EDITOR_DEPS
ensure_dependencies(EDITOR_DEPS)

from PyQt6.QtCore import Qt, QRect, QSize, QTimer, QRegularExpression, QPoint, QEvent
from PyQt6.QtGui import (
    QColor, QFont, QPainter, QImage, QTextFormat, QTextCharFormat,
    QSyntaxHighlighter, QTextCursor, QAction, QStandardItemModel, QStandardItem,
    QShortcut, QKeySequence,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPlainTextEdit, QWidget, QTextEdit, QSplitter,
    QVBoxLayout, QHBoxLayout, QLabel, QToolBar, QFileDialog, QDockWidget,
    QTextBrowser, QPushButton, QComboBox, QSlider,
    QDialog, QListWidget, QLineEdit, QMessageBox, QCompleter, QColorDialog,
    QStyledItemDelegate, QStyle, QFrame, QTabWidget, QListWidgetItem, QScrollArea, QCheckBox, QSpinBox,
)

import dsl
import renderer
import project
import glasspack

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
PROJECTS_DIR = os.path.join(HERE, "projects")
SEEN_MARKER = os.path.join(HERE, ".editor_seen")

os.makedirs(PROJECTS_DIR, exist_ok=True)
renderer.ASSET_DIRS = [PROJECTS_DIR, UI_DIR, HERE]


# ===========================================================================
#  Syntax highlighting
# ===========================================================================
class GlassHighlighter(QSyntaxHighlighter):
    def __init__(self, doc, theme=None):
        super().__init__(doc)
        self.apply_theme(theme)

    def apply_theme(self, theme):
        import glasstheme
        t = glasstheme._norm(theme or glasstheme.active())
        self.theme = t
        self.rules = []

        def fmt(color, bold=False, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            return f

        keywords = ["setup", "update", "snip", "function", "func", "return", "if", "else",
                    "import", "from", "call", "after", "repeat", "for", "to"]
        types = ["int", "float", "double", "bool", "boolean", "number", "string", "list"]
        booleans = ["true", "false"]
        elements = ["menu", "holder", "panel", "bar", "main", "button", "text",
                    "link", "label", "input", "webInput", "gif", "video", "collider",
                    "vcr", "image", "center", "grab", "scale", "separator", "textgroup",
                    "full", "ui", "dynamic", "particleSystem", "particles",
                    "raycast", "material", "raycastObject"]
        functions = ["adjVCR", "adjvcr", "create", "burst", "random", "rand",
                     "lerp", "clamp", "min", "max", "abs", "sqrt", "sin", "cos",
                     "floor", "round", "input", "getHeld", "getClick", "detect",
                     "cursor", "physics", "gravity", "time", "screen", "pref", "audio",
                     "mouse", "playSound", "isPlaying", "getAudioId", "collide", "createMesh", "properties", "getProperty", "destroy", "spawnObject", "clone", "physics",
                     "dUMR", "alert", "ram"]

        for words, role, bold in ((keywords, "keyword", True), (types, "type", False),
                                  (booleans, "boolean", False),
                                  (elements, "element", True), (functions, "function", True)):
            f = fmt(t[role], bold=bold)
            for w in words:
                self.rules.append((QRegularExpression(rf"\b{w}\b"), f))

        # snip "Name"  -> colour just the name (capture group 1)
        self.rules.append((QRegularExpression(r'\bsnip\s+("[^"]*")'), fmt(t["snipname"]), 1))
        # property keys:  word followed by ':'
        self.rules.append((QRegularExpression(r"\b[A-Za-z_][\w-]*\s*(?=:)"), fmt(t["property"])))
        self.rules.append((QRegularExpression(r"#overrideOPLim\b"), fmt(t["keyword"], bold=True)))
        self.rules.append((QRegularExpression(r"#[0-9A-Fa-f]{3,8}\b"), fmt(t["hexcolor"])))
        self.rules.append((QRegularExpression(r"\b\d+(\.\d+)?\b"), fmt(t["number"])))
        self.rules.append((QRegularExpression(r"(==|!=|<=|>=|&&|\|\||[+\-*/%<>=!])"),
                           fmt(t["operator"])))
        self.rules.append((QRegularExpression(r"[{}()\.,:]"), fmt(t["punctuation"])))
        self.rules.append((QRegularExpression(r"\"[^\"]*\""), fmt(t["string"])))

        # comments:  >> ... <<  (may span lines)
        self.block_fmt = fmt(t["comment"], italic=True)
        self.block_start = QRegularExpression(r">>")
        self.block_end = QRegularExpression(r"<<")
        self.rehighlight()

    def highlightBlock(self, text):
        for rule in self.rules:
            rx, f = rule[0], rule[1]
            grp = rule[2] if len(rule) > 2 else 0
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                start = m.capturedStart(grp)
                length = m.capturedLength(grp)
                if start >= 0 and length > 0:
                    self.setFormat(start, length, f)
        # multiline >> << comments
        self.setCurrentBlockState(0)
        start = 0
        if self.previousBlockState() != 1:
            mm = self.block_start.match(text)
            start = mm.capturedStart() if mm.hasMatch() else -1
        while start >= 0:
            em = self.block_end.match(text, start)
            if em.hasMatch():
                length = em.capturedEnd() - start
                self.setFormat(start, length, self.block_fmt)
                nxt = self.block_start.match(text, start + length)
                start = nxt.capturedStart() if nxt.hasMatch() else -1
            else:
                self.setCurrentBlockState(1)
                self.setFormat(start, len(text) - start, self.block_fmt)
                break


# ===========================================================================
#  Code editor with line numbers
# ===========================================================================
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


COMPLETION_DOCS = {
    # ---- components & structure -------------------------------------------
    "menu": ("A draggable UI panel (overlay window).", [
        ("Basic", '*.menu { title: Menu  text "Hi" {} }'),
        ("With traits", '*.menu ( menu.moveable menu.resizable menu.closable ) { title: Menu }'),
    ]),
    "holder": ("A sub-panel/box you can show, hide or scroll.", [
        ("Toggleable sub-page", 'holder "page2" { text "Hi" {} }'),
        ("Scrollable box", 'holder ( scroll, size: 400x300 ) { text "lots..." {} }'),
        ("Outlined", 'holder ( outline: true, outlinecolor: #6cf09a ) { ... }'),
    ]),
    "panel": ("A menu-style panel.", [("Example", '*.panel { text "hi" {} }')]),
    "bar": ("A toolbar-style panel.", [("Example", '*.bar { button "Go" {} }')]),
    "main": ("A full-screen page (your home screen).", [
        ("Home page", '*.main { menu.full  title: Home }'),
        ("Game page", '*.main { menu.dynamic  title: Game }'),
    ]),
    "button": ("A clickable button (action / url / set / do).", [
        ("Simple", 'button "Reload" { action: reload }'),
        ("Styled (style block first)",
         'button { { color: #1e88e5, width: 160, textgroup: Body } "Open" } { action: toggle, target: page2 }'),
        ("Aligned label", 'button "Go" { action: reload, center: right }'),
    ]),
    "text": ("A line of static text.", [
        ("Simple", 'text "Hello"'),
        ("Advanced", 'text "Hello" { color: #6cf09a, font: Body, width: 300, center: center }'),
    ]),
    "link": ("Like a button but opens a url.", [
        ("Simple", 'link "Site" { url: https://example.com }'),
        ("Styled", 'link { { color: #1e88e5, width: 160 } "Site" } { url: https://example.com }'),
    ]),
    "label": ("A plain text label.", [("Example", 'label "Name" {}')]),
    "input": ("A search / address text box.", [
        ("Simple", 'input "Search or enter address"'),
        ("Sized", 'input "Search" { width: 440, height: 38, font: Body }'),
    ]),
    "separator": ("A horizontal divider line.", [("Example", 'separator')]),
    "textgroup": ("Name a font once, reuse it by name.", [
        ("Define", 'textgroup { Body, consolas }'),
        ("Use", 'text "Hi" { font: Body }'),
    ]),
    "scale": ("Zoom the whole container (box + text).", [
        ("Uniform", 'scale { 1.5 }'),
        ("Width, height", 'scale { 1.5, 1.5 }'),
    ]),
    "sprite": ("Custom texture for an element. Use the \U0001f58c Sprite button "
               "(or Sprites\u2026) to paint one; it auto-scales to the element. "
               "Remove it to go back to the Glass texture.", [
        ("Set by the sprite editor", 'button "Play" { sprite: "sprites/Play.png" }'),
    ]),
    "center": ("Alignment. Works in text, holders/menus, and buttons.", [
        ("In text (text align)", 'text "Hi" { center: center }'),
        ("In a holder/menu (align children)", 'center { center }'),
        ("In a button (label align)", 'button "Go" { center: right }'),
    ]),
    "grab": ("Auto-open a holder on load if a condition is true.", [
        ("Example", 'grab { "news", true }'),
    ]),
    "if": ("Show elements / run code when a condition is true.", [
        ("On a variable", 'if ShowNews { text "News!" {} }'),
        ("In a game script", 'if (input.GetHeld("D")) { adjVCR({0,0},{5,0},{1,1},101) }'),
        ("With else", 'if X { ... } else { ... }'),
    ]),
    "else": ("Fallback block for an if.", [("Example", 'if X { ... } else { ... }')]),
    "setup": ("Runs once when the page loads (game init).", [
        ("Example", 'setup { physics.gravity(700)  cursor.hide }'),
    ]),
    "update": ("Runs every frame (the game loop).", [
        ("Example", 'update { adjVCR({0,0,1},{0,0},{1,1},1) }'),
    ]),
    "string": ("Declare a text variable.", [("Example", 'string Name = "Hero"')]),
    # ---- traits / modes / properties -------------------------------------
    "moveable": ("Let the user drag the panel.", [("Example", '*.menu ( menu.moveable ) {}')]),
    "resizable": ("Let the user resize the panel.", [("Example", '*.menu ( menu.resizable ) {}')]),
    "closable": ("Show a close (X) button.", [("Example", '*.menu ( menu.closable ) {}')]),
    "pinned": ("Keep the panel on top.", [("Example", '*.menu ( menu.pinned ) {}')]),
    "ontop": ("Raise above other panels.", [("Example", '*.menu ( menu.ontop ) {}')]),
    "hidden": ("Start hidden; show it later.", [("Example", 'holder "x" { hidden  text "hi" {} }')]),
    "outline": ("Draw a border around a holder.", [("Example", 'holder ( outline: true ) {}')]),
    "outlinecolor": ("Border color.", [("Example", 'outlinecolor: #6cf09a')]),
    "outlineThickness": ("Border thickness in px.", [("Example", 'outlineThickness: 2')]),
    "size": ("Width or WxH of a box.", [
        ("Width only", 'size: 300'),
        ("Width x height", 'size: 300x200'),
        ("Stretch to window", 'size: screen.width'),
    ]),
    "backgroundColor": ("Background color.", [("Example", 'backgroundColor: #161b22')]),
    "title": ("A heading for a menu/holder. Quote it to also center, color, and font it.", [
        ("Plain", 'title: My Page'),
        ("Styled", 'title: "My Page" { center: center, color: #6cf09a, font: T }'),
    ]),
    "opacity": ("Transparency from 0 to 1.", [("Example", 'opacity: 0.9')]),
    "visible": ("Show or hide.", [("Example", 'visible: false')]),
    "remember": ("Remember the panel's moved position.", [("Example", '*.menu ( menu.remember ) {}')]),
    "persist": ("Keep state across reloads.", [("Example", 'persist')]),
    "autosize": ("Fit the box to its contents.", [("Example", 'holder ( autosize ) {}')]),
    "scroll": ("Scroll the holder if content overflows.", [("Example", 'holder ( scroll, size: 400x300 ) {}')]),
    "scrollable": ("Scroll the holder if content overflows.", [("Example", 'holder ( scrollable, size: 400x300 ) {}')]),
    "full": ("Full-screen mode (with main).", [("Example", 'menu.full')]),
    "ui": ("Normal draggable menu mode (with main).", [("Example", 'menu.ui')]),
    "dynamic": ("Auto-scale + center all objects to fit (games).", [("Example", 'menu.dynamic')]),
    "int": ("Declare a whole-number variable.", [("Example", 'int Count = 0')]),
    "float": ("Declare a decimal variable.", [("Example", 'float Speed = 3.5')]),
    "double": ("Declare a decimal variable.", [("Example", 'double Speed = 3.5')]),
    "bool": ("Declare a true/false variable.", [("Example", 'bool On = true')]),
    "boolean": ("Declare a true/false variable.", [("Example", 'boolean On = true')]),
    "true": ("Boolean value: yes/on.", [("Example", 'bool On = true')]),
    "false": ("Boolean value: no/off.", [("Example", 'bool On = false')]),
    "width": ("Width in px (accepts screen.width + math).", [
        ("Fixed", 'width: 300'),
        ("Responsive", 'width: screen.width - 40'),
    ]),
    "height": ("Height in px.", [("Example", 'height: 38')]),
    "color": ("Color (hex).", [
        ("Text color", 'text "Hi" { color: #6cf09a }'),
        ("Button background", 'button { { color: #1e88e5 } "Go" } { action: reload }'),
    ]),
    "font": ("A textgroup name or font family.", [("Example", 'text "Hi" { font: Body }')]),
    # ---- vcr media --------------------------------------------------------
    "vcr.image": ("Show an image (color box if the file is missing).", [
        ("From a file", 'vcr.image "logo.png" { size: 64, svc: 1 }'),
        ("Color box (no file)", 'vcr.image "box" { size: 64, color: #6cf09a, svc: 1 }'),
        ("Pixelated", 'vcr.image "logo.png" { size: 64, type: pixel, svc: 1 }'),
    ]),
    "vcr.gif": ("Show an animated gif.", [("Example", 'vcr.gif "spin.gif" { size: 64, svc: 2 }')]),
    "vcr.video": ("Play a video. compress/pixelate it, autoplay with startOn.", [
        ("Basic", 'vcr.video "clip.mp4" { size: 320, svc: 3 }'),
        ("With controls", 'vcr.video "clip.mp4" { size: 320, settings: true, volume: 0.5, svc: 3 }'),
        ("Pixelated, autoplay", 'vcr.video "clip.mp4" { size: 320x180, compress: pixel, startOn: true, svc: 3 }'),
        ("Posterize + speed", 'vcr.video "clip.mp4" { size: 320x180, compress: standR(0.4), speed: 1.25, svc: 3 }'),
    ]),
    "vcr.colide": ("A solid collider object (player / platform / trigger).", [
        ("Solid platform", 'vcr.colide "ground" { size: 400x30, {400,30}, svc: 100 }'),
        ("Player", 'vcr.colide "player" { size: 44, color: #6cf09a, {44,44}, svc: 101 }'),
        ("Trigger (detect only)", 'vcr.colide "coin" { size: 26, {26,26}, istrigger: true, svc: 200 }'),
    ]),
    "svc": ("The object's id; adjVCR and detect use it.", [("Example", 'svc: 101')]),
    "type": ("Image compression.", [
        ("Pixelate", 'type: pixel'),
        ("Detail/color", 'type: standR(.5)'),
    ]),
    "collider": ("Collider size {w,h} inside a vcr.colide.", [("Example", '{32,48}')]),
    "friction": ("Surface friction for a collider.", [("Example", 'friction: 0.2')]),
    "istrigger": ("true = detect only, never blocks.", [("Example", 'istrigger: true')]),
    "settings": ("Show the video's controls.", [("Example", 'settings: true')]),
    "pixel": ("Pixelate compression type.", [("Example", 'type: pixel')]),
    "standR": ("Detail/color compression (0-1, lower = more).", [("Example", 'type: standR(.5)')]),
    # ---- engine values & calls -------------------------------------------
    "adjVCR": ("Move / rotate / scale an object (by svc) each call.", [
        ("Move", 'adjVCR( {0,0}, {5,0}, {1,1}, 101 )'),
        ("Rotate (2D angle)", 'adjVCR( {0,0,5}, {0,0}, {1,1}, 101 )'),
        ("Scale", 'adjVCR( {0,0}, {0,0}, {2,2}, 101 )'),
    ]),
    "adjVCR.detect": ("True if objects touch.", [
        ("Two objects", 'if (adjVCR.detect(101, 100)) { ... }'),
        ("One vs anything", 'if (adjVCR.detect(101)) { ... }'),
    ]),
    "physics.gravity": ("Set or read gravity (already x time).", [
        ("Set", 'physics.gravity(700)'),
        ("Use (make it fall)", 'adjVCR( {0,0}, {0, physics.gravity}, {1,1}, 101 )'),
    ]),
    "physics.ui.gravity": ("Same as physics.gravity.", [("Example", 'physics.ui.gravity(9.8)')]),
    "time.normal": ("This frame's delta time (varies with fps).", [("Example", '{100 * time.normal, 0}')]),
    "time.held": ("A fixed time step that never changes.", [("Example", '{100 * time.held, 0}')]),
    "input.GetHeld": ("True every frame a key is held.", [("Example", 'if (input.GetHeld("D")) { ... }')]),
    "input.GetClick": ("True only on the first frame of a press.", [("Example", 'if (input.GetClick("Space")) { ... }')]),
    "screen.width": ("Current window width.", [
        ("Stretch a holder", 'holder ( ) { size: screen.width  ... }'),
        ("With math", 'width: screen.width - 40'),
    ]),
    "screen.height": ("Current window height.", [("Example", 'height: screen.height')]),
    # ---- preferences & cursor --------------------------------------------
    "pref.save": ("Save a value that persists between runs.", [
        ("int", 'pref.save("hp", 100)'),
        ("float / double", 'pref.save("volume", 0.8)'),
        ("string", 'pref.save("name", "Hero")'),
        ("bool", 'pref.save("hardMode", true)'),
    ]),
    "pref.load": ("Load a saved value (0 if unset).", [
        ("number", 'hp = pref.load("hp")'),
        ("bool", 'yes = pref.load("hardMode")'),
    ]),
    "preference.save": ("Alias of pref.save.", [("Example", 'preference.save("hp", 100)')]),
    "preference.load": ("Alias of pref.load.", [("Example", 'hp = preference.load("hp")')]),
    "cursor.lock": ("Hide the cursor and pin it to the centre.", [("Example", 'cursor.lock')]),
    "cursor.unlock": ("Undo cursor.lock.", [("Example", 'cursor.unlock')]),
    "cursor.hide": ("Hide the cursor.", [("Example", 'cursor.hide')]),
    "cursor.show": ("Show the cursor again.", [("Example", 'cursor.show')]),
    "cursor.confine": ("Keep the cursor inside the window/tab.", [("Example", 'cursor.confine')]),
    "cursor.free": ("Undo cursor.confine.", [("Example", 'cursor.free')]),
    # ---- audio ------------------------------------------------------------
    "audio.isPlaying": ("Bool: is any sound or audible tab playing? Use in if(...).", [
        ("Check", 'if (audio.isPlaying) { show nowPlaying }'),
    ]),
    "audio.getAudioId": ("Int: id of whatever is currently making sound (0 if none).", [
        ("Get id", 'songId = audio.getAudioId'),
    ]),
    "audio.playSound": ("Play a sound file, or a pre-loaded clip (advanced).", [
        ("Simple", 'audio.playSound "song.mp3" { speed: 1, volume: 1 }'),
        ("Full", 'audio.playSound "song.mp3" { speed: 1, volume: 1, quality: 100, hertz: 48000 }'),
        ("Clip (advanced)", 'audio.playSound (Track) { volume: 0.8 }'),
    ]),
    "audio.clip": ("Declare a pre-loaded clip handle from gatherClip.", [
        ("Declare", 'audio.clip Track = audio.gatherClip { "song.mp3" }'),
    ]),
    "audio.gatherClip": ("Pre-load a sound file once; returns a clip id.", [
        ("Load", 'audio.clip Track = audio.gatherClip { "song.mp3" }'),
    ]),
    "audio.changeVolume": ("Set the volume (0-100) of an audio id.", [
        ("Set volume", 'audio.changeVolume { audioID: songId, volume: 100 }'),
    ]),
    "audio.pauseCurrent": ("Fade volume down by fadeAmount each tick, then pause.", [
        ("Fade + pause", 'audio.pauseCurrent { audioID: songId, fadeAmount: 5 }'),
    ]),
    "audio.playLast": ("Resume the tab/sound that had this audio id.", [
        ("Resume", 'audio.playLast { audioID: songId }'),
    ]),
    # ---- packages & imports ----------------------------------------------
    "import": ("Load a Python module from this project so the UI can call it.", [
        ("Same-named file", 'import helper'),
        ("Named file", 'import mc from "mclauncher.py"'),
    ]),
    "call": ("Run an imported Python function when a button is clicked.", [
        ("Call a function", 'button "Login" { call: mc.login }'),
    ]),
    # ---- actions ----------------------------------------------------------
    "getGlass": ("Load a sibling script in the current project.", [
        ("Same tab", 'button "Next" { action: getGlass, target: page2 }'),
        ("New tab", 'button "Next" { action: getGlass, target: page2, openNew: true }'),
    ]),
    "navigate": ("Go to a url or glass route.", [("Example", 'action: navigate, url: https://example.com')]),
    "reload": ("Reload the current page.", [("Example", 'button "Reload" { action: reload }')]),
    "cleardata": ("Clear cookies and cache.", [("Example", 'button "Clear" { action: cleardata }')]),
    "wiki": ("Open the built-in Glass wiki.", [("Example", 'button "Wiki" { action: wiki }')]),
    "do": ("Run a sequence of show/hide/set commands.", [("Example", 'do: hide a; show b; set: n += 1')]),
    "set": ("Update a variable.", [
        ("number", 'set: Count += 1'),
        ("string", 'set: Name = "Hero"'),
        ("bool", 'set: On = false'),
    ]),
    "action": ("Call an api.py method by name.", [("Example", 'button "Go" { action: reload }')]),
}

# ---- full language catalog (elements, props, actions, engine fns, values) --
# Direct assignment (overrides base entries too) so everything is accurate.
_EXTRA_DOCS = {
    # ---- declarations & structure --------------------------------------
    "string": ("Declare a text variable.", [("Example", 'string Name = "idle"')]),
    "int": ("Declare a whole-number variable.", [("Example", 'int Score = 0')]),
    "float": ("Declare a decimal variable.", [("Example", 'float Speed = 1.5')]),
    "bool": ("Declare a true/false variable.", [("Example", 'bool Alive = true')]),
    "number": ("Declare a number variable.", [("Example", 'number N = 3')]),
    "import": ("Load a package (a .py file in the project).", [("Example", 'import demo')]),
    "textgroup": ("Define named fonts you can apply with font:.", [
        ("Example", 'textgroup { Body, consolas }')]),
    "setup": ("Script that runs ONCE when the scene starts.", [
        ("Example", 'setup { cursor.lock }')]),
    "update": ("Script that runs EVERY frame (~60fps).", [
        ("Move", 'update {\n    if (input.getHeld("right") == "1") { x = x + 4 }\n}')]),
    "if": ("Condition. Supports == != < > <= >= && || and else / else if.", [
        ("Example", 'if (Score > 10) { Won = "1" } else { Won = "0" }')]),
    # ---- menu types -----------------------------------------------------
    "menu.full": ("Full-page screen.", [("Example", '*.main { menu.full }')]),
    "menu.ui": ("Overlay UI screen (HUD / menus).", [("Example", '*.hud { menu.ui }')]),
    "menu.dynamic": ("Camera that scales the whole scene to fit (games).", [
        ("Example", '*.main { menu.dynamic }')]),
    # ---- elements -------------------------------------------------------
    "main": ("The entry screen. Written as *.main { ... }.", [("Example", '*.main {\n    menu.full\n}')]),
    "menu": ("A screen / container block.", [("Example", '*.name { menu.full }')]),
    "holder": ("Container that groups elements; can outline/scroll/sprite.", [
        ("Example", 'holder ( center: center, size: 400x300 ) { }')]),
    "panel": ("A panel container.", [("Example", 'panel ( ) { }')]),
    "bar": ("A bar container (e.g. a toolbar row).", [("Example", 'bar ( ) { }')]),
    "button": ("Clickable button. Runs action / call / getGlass / url.", [
        ("Simple", 'button "Go" { action: getGlass, target: page2 }'),
        ("Styled", 'button { { color:#1e88e5, width:200 } "Go" } { call: pkg.fn }')]),
    "link": ("Like a button, styled as a hyperlink.", [
        ("Example", 'link "Site" { url: https://example.com }')]),
    "text": ("A line of text. Supports {Var} live values.", [
        ("Example", 'text "Score: {Score}" { color: #e7edf3 }')]),
    "label": ("A text label.", [("Example", 'label "Hello" { color: #6cf09a }')]),
    "input": ("A text input field.", [("Example", 'input { width: 240 }')]),
    "separator": ("A horizontal divider line.", [("Example", 'separator')]),
    "image": ("Show an image.", [("Example", 'image "pic.png" { size: 128x128 }')]),
    "vcr.image": ("Image object (supports compress, name, center, x/y).", [
        ("Simple", 'vcr.image "pic.png" { size: 200x120 }'),
        ("Pixelated", 'vcr.image "pic.png" { size: 200x120, compress: pixel(8) }')]),
    "vcr.video": ("Video object (compress/speed/startOn/volume; live {Var} source).", [
        ("Simple", 'vcr.video "clip.mp4" { size: 320x180 }'),
        ("Live source", 'vcr.video "{clip}" { size: 320x180 }')]),
    "scale": ("Scale a group of elements.", [("Example", 'scale ( 2 ) { }')]),
    "center": ("Center content / an element.", [("Example", 'center { center }')]),
    "collider": ("An invisible collision box for the engine.", [
        ("Example", 'collider ( size: 64x64 ) { name: wall }')]),
    "grab": ("Make an element draggable.", [("Example", 'grab ( ) { }')]),
    # ---- properties -----------------------------------------------------
    "color": ("Text / foreground color (hex).", [("Example", 'color: #6cf09a')]),
    "background": ("Fill color (hex).", [("Example", 'background: #10141b')]),
    "backgroundColor": ("Fill color (hex).", [("Example", 'backgroundColor: #10141b')]),
    "width": ("Width in pixels.", [("Example", 'width: 200')]),
    "height": ("Height in pixels.", [("Example", 'height: 38')]),
    "size": ("Width x height together.", [("Example", 'size: 128x128')]),
    "center": ("Alignment: center / left / right.", [("Example", 'center: center')]),
    "font": ("Use a named textgroup font.", [("Example", 'font: Body')]),
    "title": ("Screen / menu title.", [("Example", 'title: "My Page" { color: #6cf09a }')]),
    "title_style": ("Style block for the title.", [("Example", 'title_style { color: #6cf09a }')]),
    "outline": ("Draw a border around a holder.", [("Example", 'outline: true')]),
    "outlinecolor": ("Border color.", [("Example", 'outlinecolor: #6cf09a')]),
    "scroll": ("Make a holder scrollable.", [("Example", 'holder ( scroll, size: 400x300 ) { }')]),
    "radius": ("Corner radius in px.", [("Example", 'radius: 8')]),
    "sprite": ("Give an element a drawn sprite image.", [("Example", 'sprite: "sprites/play.png"')]),
    "opacity": ("Transparency 0..1.", [("Example", 'opacity: 0.5')]),
    "visible": ("Show/hide the element.", [("Example", 'visible: false')]),
    "hidden": ("Hide the element.", [("Example", 'hidden: true')]),
    "paused": ("Pause a video.", [("Example", 'paused: true')]),
    "settings": ("Show built-in video controls.", [("Example", 'settings: true')]),
    "svc": ("Manual object id (usually auto).", [("Example", 'svc: 3')]),
    "name": ("Name a game object so scripts/collisions can reference it.", [
        ("Example", 'vcr.image "p.png" { name: player }')]),
    "solid": ("Object collides with / blocks other solids.", [
        ("Example", 'vcr.image "wall.png" { name: wall, solid: true }')]),
    "friction": ("Surface friction for physics.", [("Example", 'friction: 0.2')]),
    "istrigger": ("Detect overlap without blocking (trigger zone).", [("Example", 'istrigger: true')]),
    "content": ("Inline content for an element.", [("Example", 'content: "Hello"')]),
    "volume": ("Video volume 0..1.", [("Example", 'volume: 0.3')]),
    "speed": ("Video playback rate.", [("Example", 'speed: 1.5')]),
    "startOn": ("Autoplay: true/false, or a number of seconds to start at.", [
        ("Don't autoplay", 'startOn: false'), ("Start at 5s", 'startOn: 5')]),
    "compress": ("Retro effect: pixel / pixel(N) / standR(v).", [
        ("Pixelate", 'compress: pixel(8)'), ("Posterize", 'compress: standR(0.3)')]),
    "type": ("Alias of compress on images.", [("Example", 'type: pixel(6)')]),
    "x": ("X position (game object).", [("Example", 'x: 50')]),
    "y": ("Y position (game object).", [("Example", 'y: 50')]),
    "bind": ("Bind an input field to a variable.", [("Example", 'input { bind: Name }')]),
    # ---- actions (inside button/link blocks) ----------------------------
    "action": ("Run a built-in action by name.", [("Example", 'action: reload')]),
    "set": ("Set a variable when clicked.", [("Example", 'set: Score = 0')]),
    "do": ("Run a do-command when clicked.", [("Example", 'do: something')]),
    "call": ("Run a Python function from an imported package.", [("Example", 'call: demo.run')]),
    "js": ("Run JavaScript on the current page.", [("Example", 'js: alert(1)')]),
    "url": ("Open a web address.", [("Example", 'url: https://example.com')]),
    "target": ("Which sibling script getGlass loads.", [("Example", 'action: getGlass, target: page2')]),
    "openNew": ("true opens the target in a new tab.", [("Example", 'openNew: true')]),
    "getGlass": ("Load another .glass screen in this project.", [
        ("Example", 'action: getGlass, target: level2, openNew: false')]),
    "navigate": ("Go to a URL.", [("Example", 'action: navigate, url: https://...')]),
    "reload": ("Reload the page.", [("Example", 'action: reload')]),
    "reloadui": ("Reload the current .glass UI.", [("Example", 'action: reloadui')]),
    "back": ("Go back.", [("Example", 'action: back')]),
    "forward": ("Go forward.", [("Example", 'action: forward')]),
    "home": ("Go to the home screen.", [("Example", 'action: home')]),
    "newtab": ("Open a new tab.", [("Example", 'action: newtab')]),
    "closetab": ("Close the current tab.", [("Example", 'action: closetab')]),
    "devtools": ("Open developer tools.", [("Example", 'action: devtools')]),
    "openeditor": ("Open the Glass editor.", [("Example", 'action: openeditor')]),
    "viewsource": ("View page source.", [("Example", 'action: viewsource')]),
    "showlog": ("Show the log panel.", [("Example", 'action: showlog')]),
    "stripads": ("Strip ads on the page.", [("Example", 'action: stripads')]),
    "toggleblock": ("Toggle the ad-blocker.", [("Example", 'action: toggleblock')]),
    "cleardata": ("Clear saved data.", [("Example", 'action: cleardata')]),
    "wiki": ("Open the Glass wiki.", [("Example", 'action: wiki')]),
    "zoomin": ("Zoom in.", [("Example", 'action: zoomin')]),
    "zoomout": ("Zoom out.", [("Example", 'action: zoomout')]),
    "zoomreset": ("Reset zoom.", [("Example", 'action: zoomreset')]),
    "quit": ("Quit the browser.", [("Example", 'action: quit')]),
    "hide": ("Hide a target panel.", [("Example", 'action: hide, target: panelName')]),
    "show": ("Show a target panel.", [("Example", 'action: show, target: panelName')]),
    "toggle": ("Toggle a target panel.", [("Example", 'action: toggle, target: panelName')]),
    # ---- engine / script functions (inside setup / update / if) ---------
    "input.getHeld": ("1 while a key is held. Keys: left/right/up/down/space/enter/"
                      "escape/shift/ctrl/tab/backspace or a letter.", [
        ("Move", 'if (input.getHeld("right") == "1") { x = x + 4 }')]),
    "input.getClick": ("1 on the frame a key is first pressed.", [
        ("Jump", 'if (input.getClick("space") == "1") { vy = -10 }')]),
    "adjvcr": ("Move/rotate/scale an object: adjvcr(rot, pos, scale, name).", [
        ("Move object", 'adjvcr( (0,0,0), (x,y,0), (1,1,1), "player" )')]),
    "adjvcr.detect": ("1 if two named objects overlap (collision).", [
        ("Hit", 'if (adjvcr.detect("player","wall") == "1") { dead = "1" }')]),
    "cursor.hide": ("Hide the mouse cursor.", [("Example", 'cursor.hide')]),
    "cursor.show": ("Show the cursor again.", [("Example", 'cursor.show')]),
    "cursor.lock": ("Hide + lock cursor to center for mouse-look (read mouse.dx/dy).", [
        ("FPS look", 'setup { cursor.lock }\nupdate { yaw = yaw + mouse.dx }')]),
    "cursor.unlock": ("Release a locked cursor.", [("Example", 'cursor.unlock')]),
    "cursor.confine": ("Keep cursor inside the window.", [("Example", 'cursor.confine')]),
    "cursor.free": ("Stop confining the cursor.", [("Example", 'cursor.free')]),
    "mouse.dx": ("Mouse X movement since last frame (with cursor.lock).", [("Look", 'yaw = yaw + mouse.dx')]),
    "mouse.dy": ("Mouse Y movement since last frame (with cursor.lock).", [("Look", 'pitch = pitch + mouse.dy')]),
    "mouse.x": ("Mouse X position in the window.", [("Example", 'if (mouse.x > 400) { }')]),
    "mouse.y": ("Mouse Y position in the window.", []),
    "mouse.down": ("1 while the left mouse button is held.", [
        ("Shoot", 'if (mouse.down == "1") { shoot = "1" }')]),
    "time.normal": ("Frame time scale (multiply movement by this).", [
        ("Example", 'x = x + 4 * time.normal')]),
    "time.held": ("Seconds the current key has been held.", []),
    "screen.width": ("Window width in pixels.", [("Example", 'if (x > screen.width) { }')]),
    "screen.height": ("Window height in pixels.", []),
    "physics.gravity": ("Get/set gravity strength.", [("Example", 'physics.gravity(9.8)')]),
    "pref.save": ("Save a value to disk.", [("Example", 'pref.save("hiscore", Score)')]),
    "pref.load": ("Load a saved value.", [("Example", 'best = pref.load("hiscore")')]),
    "audio.playSound": ("Play a sound (volume/speed/quality/hertz).", [
        ("Example", 'audio.playSound { "beep.wav", volume: 0.8 }')]),
    "audio.gatherClip": ("Load a clip for reuse.", [("Example", 'audio.clip Hit = audio.gatherClip { "hit.wav" }')]),
    "audio.playLast": ("Replay the last clip by id.", [("Example", 'audio.playLast { audioID: Hit }')]),
    "audio.pauseCurrent": ("Pause/fade the current audio.", [("Example", 'audio.pauseCurrent { audioID: Hit }')]),
    "audio.changeVolume": ("Change a clip's volume.", [("Example", 'audio.changeVolume { audioID: Hit, volume: 0.3 }')]),
    "audio.isPlaying": ("1 if a clip is playing.", [("Example", 'if (audio.isPlaying == "1") { }')]),
    "audio.getAudioId": ("Get the id of a clip.", []),
    # ---- values ---------------------------------------------------------
    "pixel": ("Pixelate value for compress. pixel or pixel(N).", [("Example", 'compress: pixel(8)')]),
    "standR": ("Posterize value for compress. standR(v), v 0..1.", [("Example", 'compress: standR(0.3)')]),
    "true": ("Boolean true.", [("Example", 'solid: true')]),
    "false": ("Boolean false.", [("Example", 'visible: false')]),
}
_EXTRA_DOCS["snip"] = ("Define a reusable function (a 'snip'). Call it from setup/"
                       "update as Name(args). Params are typed; it can return a value.", [
    ("Void", 'snip "AddScore" ( int amount ) {\n    Score = Score + amount\n}'),
    ("Returns", 'snip "Double" ( int n ) {\n    return { returnType: int, value: n * 2 }\n}')])
_EXTRA_DOCS["return"] = ("Return a value from a snip: return { returnType: T, value: V } "
                         "where T is bool/int/string/double.", [
    ("Example", 'return { returnType: int, value: n * 2 }')])
_EXTRA_DOCS["after"] = ("Run a block once after a delay (in seconds). Great for "
                        "timed events and cutscenes.", [
    ("Wait 2s", 'after 2s {\n    Prompt = "\u2026time passes\u2026"\n}')])
_EXTRA_DOCS["create"] = ("Spawn a new sprite object at runtime (like Unity Instantiate). "
                         "Returns the new object's name to move/detect it.", [
    ("Spawn", 'b = create("bullet.png", x, y)'),
    ("Sized", 'e = create("enemy.png", 100, 40, 48, 48)')])
_EXTRA_DOCS["particleSystem"] = ("An emitter that spouts animated particles. Settings: "
                                 "count, life, speed, spread, direction, size, color, "
                                 "gravity, shape (circle/square), rate, fade. color can "
                                 "be a #hex color OR an image path (same as sprite: "
                                 "elsewhere) for a textured particle instead of a dot. "
                                 "By default it's a flat 2D screen overlay, laid out "
                                 "like an image. Add mode: 3d (plus x/y world "
                                 "coordinates instead of layout) to make it a real "
                                 "WORLD-space emitter inside a raycast scene - "
                                 "projected through the camera, scaled by distance, "
                                 "hidden behind walls, same as a 3D burst(). Also only "
                                 "in 3D mode: light: true shades it with the scene's "
                                 "baked lighting; bounceA (0 = never bounces, higher = "
                                 "bouncier) makes it bounce off a wall or the floor "
                                 "instead of stopping dead; sizeOverLife (0 = constant "
                                 "size, 1 = shrinks to nothing by the end of its life) "
                                 "shrinks it as it ages. Gravity in 3D mode pulls it "
                                 "down toward the floor (a real height axis), not "
                                 "sideways across the map.", [
    ("Sparkle", 'particleSystem { count: 40, life: 1.2, speed: 90, spread: 360, '
     'color: #6cf09a, size: 5 }'),
    ("Fountain", 'particleSystem { direction: -90, spread: 40, speed: 160, '
     'gravity: 120, color: #ffcb6b, shape: square }'),
    ("Torch flame (3D, lit, in the world)", 'particleSystem { mode: 3d, x: 260, y: 140, '
     'rate: 25, speed: 40, spread: 40, direction: -90, color: #ff8a3c, size: 5, light: true }'),
    ("Bouncy embers that shrink away", 'particleSystem { mode: 3d, x: 260, y: 140, '
     'rate: 15, speed: 60, gravity: 150, bounceA: 0.35, sizeOverLife: 1, color: "ember.png" }')])
_EXTRA_DOCS["burst"] = ("Pop a one-shot particle burst at x,y (fireworks, hits, explosions). "
    "burst(x, y, colorOrTexture, count, speed [, is3D [, lit [, bounceA [, sizeOverLife]]]]). "
    "colorOrTexture can be a #hex color OR an image path (same auto-detect as sprite: "
    "elsewhere) - a texture draws as a small image instead of a filled dot. By default "
    "it's a flat 2D screen overlay - add true as a 6th argument inside a raycast scene "
    "to make it a real WORLD-space burst: it projects through the camera, scales with "
    "distance, and hides behind walls. A 7th true additionally shades it with the SAME "
    "baked lighting the walls and billboards use (needs is3D true too). bounceA (0 = "
    "never bounces off a wall/floor, higher = bouncier) and sizeOverLife (0 = stays the "
    "same size, 1 = shrinks to nothing by the end of its life) only do anything in 3D "
    "mode, since flat 2D particles have no floor/wall to hit.", [
    ("Firework (2D)", 'burst( random(90,690), random(70,260), "#6cf09a", 60, 170 )'),
    ("Bullet hit (3D, lit)", 'burst(hitX, hitY, "#ffcb6b", 24, 160, true, true)'),
    ("Bouncy sparks that shrink away", 'burst(hitX, hitY, "#ffcb6b", 24, 160, true, true, 0.4, 1.0)')])
_EXTRA_DOCS["random"] = ("A random number. random(a,b) in [a,b], random(n) in [0,n], random() in [0,1].", [("Example", 'x = random(0, screen.width)')])
_EXTRA_DOCS["lerp"] = ("Blend between a and b by t (0..1). Great for smooth motion.", [("Smooth follow", "X = lerp(X, TargetX, 0.1)")])
_EXTRA_DOCS["clamp"] = ("Keep a value within a range: clamp(value, low, high).", [("Example", "hp = clamp(hp, 0, 100)")])
_EXTRA_DOCS["min"] = ("Smallest of the given numbers.", [("Example", "m = min(a, b, c)")])
_EXTRA_DOCS["max"] = ("Largest of the given numbers.", [("Example", "m = max(a, b)")])
_EXTRA_DOCS["abs"] = ("Absolute value (drops the sign).", [("Example", "d = abs(a - b)")])
_EXTRA_DOCS["sqrt"] = ("Square root.", [("Example", "dist = sqrt(dx*dx + dy*dy)")])
_EXTRA_DOCS["sin"] = ("Sine (radians) \u2013 great for bobbing/wave motion.",
                      [("Bob", "y = baseY + sin(time.normal) * 10")])
_EXTRA_DOCS["cos"] = ("Cosine (radians) \u2013 pairs with sin for circular motion.",
                      [("Orbit", "x = cx + cos(t) * r")])
_EXTRA_DOCS["floor"] = ("Round down to a whole number.", [("Example", "cell = floor(x / 32)")])
_EXTRA_DOCS["round"] = ("Round to the nearest whole number.", [("Example", "n = round(3.7)")])
_EXTRA_DOCS["rand"] = ("Alias of random(). A random number in a range.",
                       [("Example", "x = rand(0, 100)")])
_EXTRA_DOCS["int"] = ("Declare a whole-number variable.", [("Example", "int Score = 0")])
_EXTRA_DOCS["float"] = ("Declare a decimal-number variable.", [("Example", "float Speed = 2.5")])
_EXTRA_DOCS["double"] = ("Declare a decimal-number variable (like float).",
                        [("Example", "double X = 1.5")])
_EXTRA_DOCS["bool"] = ("Declare a true/false variable.", [("Example", "bool Alive = true")])
_EXTRA_DOCS["boolean"] = ("Declare a true/false variable (same as bool).",
                         [("Example", "boolean Ready = false")])
_EXTRA_DOCS["string"] = ("Declare a text variable.", [("Example", 'string Name = "hero"')])
_EXTRA_DOCS["number"] = ("Declare a number variable.", [("Example", "number N = 10")])
_EXTRA_DOCS["raycast"] = ("A Doom-style first-person 3D view of a grid map. Walk it with "
    "WASD + arrows; each map cell references a material. Writes the camera to "
    "rayX / rayY / rayA.", [("Example",
    'raycast {\n    material "1" { color: #6cf09a }\n    map: "111|1.1|111"\n    fov: 66\n}')])
_EXTRA_DOCS["material"] = ("Inside raycast: defines a wall look for a map character. "
    "Give it a color or an image (+ tiling / tilingVector).", [
    ("Colour", 'material "1" { color: #6cf09a }'),
    ("Texture", 'material "2" { image: "brick.png", tiling: true, tilingVector: 2x2 }')])
_EXTRA_DOCS["parent"] = ("Inside raycast: make the 3D camera follow a game object by "
    "name or svc. The object's position (scaled by cellSize) and rotation become the "
    "camera.", [("Example", "parent: player")])
_EXTRA_DOCS["cellSize"] = ("Inside raycast with parent: how many world pixels equal one "
    "map cell (default 64).", [("Example", "cellSize: 64")])
_EXTRA_DOCS["map"] = ("The raycaster grid. Use a .json file, a [\"row\",\"row\"] array, or a "
    "|-separated string. Each char is a material key; . is empty floor.", [
    ("From file", 'map: "maze.json"'),
    ("Inline", 'map: [ "1111", "1..1", "1111" ]')])
_EXTRA_DOCS["fov"] = ("Field of view for raycast, in degrees (try 66\u201375).",
    [("Example", "fov: 70")])
_EXTRA_DOCS["collide.createMesh"] = ("Build a wall collider from a raycast maze (by its "
    "mazeID). The maze auto-registers when it spawns; call this to (re)confirm it.",
    [("Example", "collide.createMesh(5)")])
_EXTRA_DOCS["collide.detect"] = ("True if an object is inside a maze wall: "
    "collide.detect(svc, mazeID).", [("Example", "if (collide.detect(101, 5)) { }")])
_EXTRA_DOCS["fogColor"] = ("Raycast fog colour that distant walls fade toward.",
    [("Example", "fogColor: #101826")])
_EXTRA_DOCS["mazeID"] = ("An id for a raycast maze so it can be used as a collider "
    "(collide.createMesh / collide.detect).", [("Example", "mazeID: 5")])
_EXTRA_DOCS["mesh"] = ("Inside raycast: render an imported OBJ mesh (by its meshID) "
    "instead of an inline map. Import it first with mesh.import in setup{}.",
    [("Example", "raycast { mesh: 1, parent: \"player\", collide: true }")])
_EXTRA_DOCS["mesh.import"] = ("Import a Wavefront .obj (e.g. from Unity ProBuilder) and "
    "flatten it into a raycaster grid, carrying over its MTL materials. Store it under "
    "a meshID.", [("Import", 'mesh.import("levels/room.obj", 1)'),
                  ("Detail", 'mesh.import("room.obj", 1, 48)  >> 48 = grid detail <<')])
_EXTRA_DOCS["mesh.create"] = ("Confirm an imported mesh is ready to render (returns 1 if "
    "it exists). The render itself happens via  raycast { mesh: id }.",
    [("Example", "mesh.create(1)")])
_EXTRA_DOCS["mesh.createCollider"] = ("Register an imported mesh's flattened grid as a "
    "collider so objects / nav collide with it, even without a visible raycaster.",
    [("Example", "mesh.createCollider(1)")])
_EXTRA_DOCS["input"] = ("A text box the user types in. Bind it to a variable to read "
    "what they type:  UsersInput = input \"...\" { }  \u2013 UsersInput becomes a string.",
    [("Bound", 'Typed = input "your name" { width: 300 }')])
_EXTRA_DOCS["webInput"] = ("A search / address box \u2013 pressing Enter navigates to the "
    "typed URL or search.", [("Example", 'webInput "Search or enter address" { width: 440 }')])
_EXTRA_DOCS["repeat"] = ("Run a block a fixed number of times.", [("Example", "repeat 5 {\n    burst( random(0,700), 80, \"#6cf09a\", 30, 150 )\n}")])
_EXTRA_DOCS["for"] = ("Loop a counter from a start to an end value (inclusive).", [("Example", "for i = 1 to 10 {\n    Total = Total + i\n}")])
_EXTRA_DOCS["properties.get"] = ("Read a property of an object as a Vector3. "
    "properties.get(svc: N, getProperty.position / scale / rotation), and add .x/.y/.z "
    "for one component.", [
    ("Position", "P = properties.get(svc: 1, getProperty.position)"),
    ("One axis", "px = properties.get(svc: 1, getProperty.position.x)")])
_EXTRA_DOCS["getProperty.distance"] = ("Distance between two Vector3 values.", [
    ("Between two objects",
     "d = getProperty.distance( properties.get(svc:1, getProperty.position), properties.get(svc:2, getProperty.position) )")])
_EXTRA_DOCS["getProperty.position"] = ("A selector for properties.get \u2013 position "
    "(x, y, z). Also .x/.y/.z and .forward/.backward/.left/.right/.up/.down.", [
    ("Example", "properties.get(svc: 1, getProperty.position.forward)")])
_EXTRA_DOCS["getProperty.scale"] = ("A selector for properties.get \u2013 scale "
    "(x, y, z). Also .x/.y/.z for one component.", [
    ("Example", "s = properties.get(svc: 1, getProperty.scale)")])
_EXTRA_DOCS["getProperty.rotation"] = ("A selector for properties.get \u2013 rotation "
    "as (0, 0, degrees) - this engine is 2D, so only .z is ever nonzero. "
    "Also .z for just the number.", [
    ("Example", "r = properties.get(svc: 1, getProperty.rotation.z)")])
_EXTRA_DOCS["getProperty.tag"] = ("A selector for properties.get \u2013 1 if the object "
    "at svc: has this exact tag, else 0. Checks the ONE object svc: already "
    "picked out. Not the same as properties.get.tag(\"door\"), which instead "
    "searches every object in the world for one with a matching tag.", [
    ("Check what you hit", 'if (properties.get(svc: hitSvc, getProperty.tag("door")) == 1) {\n    >> hitSvc is a door <<\n}')])
_EXTRA_DOCS["getProperty.size"] = ("A selector for properties.get \u2013 collider "
    "dimensions (x, y, z) if a collider is set, else the object's base draw "
    "size. Also .x/.y/.z for one component.", [
    ("Example", "sz = properties.get(svc: 1, getProperty.size)")])
_EXTRA_DOCS["getProperty.friction"] = ("A selector for properties.get \u2013 the "
    "object's friction (set once via friction: on its vcr.* element, not "
    "changeable at runtime).", [
    ("Example", "f = properties.get(svc: 1, getProperty.friction)")])
_EXTRA_DOCS["getProperty.isTrigger"] = ("A selector for properties.get \u2013 1 if "
    "the object is a trigger (detect-only, never blocks), else 0.", [
    ("Example", "if (properties.get(svc: hitSvc, getProperty.isTrigger) == 1) { }")])
_EXTRA_DOCS["getProperty.velocity"] = ("A selector for properties.get \u2013 current "
    "velocity (x, y, z) from physics.push - stays (0,0,0) unless something "
    "has launched or shoved this object. Also .x/.y/.z. Not affected by "
    "adjvcr or nav.follow movement, which carry no velocity of their own.", [
    ("Example", "v = properties.get(svc: 1, getProperty.velocity)")])
_EXTRA_DOCS["getProperty.speed"] = ("A selector for properties.get \u2013 how fast "
    "the object is currently moving (the magnitude of its velocity), as a "
    "single number. 0 unless physics.push has set it moving.", [
    ("Impact-scaled damage", "hitSpeed = properties.get(svc: hitSvc, getProperty.speed)\nif (hitSpeed > 8) { >> hard hit <<  }")])
_EXTRA_DOCS["getProperty.kind"] = ("A selector for properties.get \u2013 the "
    "object's own type as a string (\"raycastObject\"/\"colide\"/\"image\"/"
    "\"gif\"/\"video\"), set once when it was created. A fallback for \"what "
    "kind of thing did I hit\" when you haven't tagged everything.", [
    ("Example", 'k = properties.get(svc: hitSvc, getProperty.kind)\nif (k == "colide") { }')])
_EXTRA_DOCS["size"] = ("On a raycastObject: billboard size (1 = full cell, 0.4 = small like a coin).", [("Example", "size: 0.4")])
_EXTRA_DOCS["raycastObject"] = ("A Doom-style billboard shown inside a raycast 3D "
    "view (always faces the camera). Give it a sprite or color; collide: true makes "
    "it block movement.", [
    ("Sprite", 'vcr.raycastObject "enemy" { sprite: "enemy.png", x: 256, y: 160, svc: 5 }'),
    ("Solid orb", 'vcr.raycastObject "orb" { color: #ff5d5d, x: 256, y: 160, svc: 5, collide: true }')])
_EXTRA_DOCS["material"] = ("Inside raycast: a wall look keyed by a map character, "
    "OR a surface when labelled \"floor\" / \"roof\". Give a color or an image "
    "(+ tiling / tilingVector). Floor & roof never collide.", [
    ("Wall", 'material "1" { color: #6cf09a }'),
    ("Floor", 'material "floor" { image: "floor.png" }'),
    ("Roof", 'material "roof" { color: #101826 }')])
_EXTRA_DOCS["floorMap"] = ("A grid (like map) of floor-tile material chars, one per cell.", [("Example", 'floorMap: [ "ggg", "gsg", "ggg" ]')])
_EXTRA_DOCS["roofMap"] = ("A grid (like map) of roof-tile material chars, one per cell.", [("Example", 'roofMap: [ "kkk", "kbk", "kkk" ]')])
_EXTRA_DOCS["raycast.cast"] = ("Cast a ray through a maze and get the first thing hit "
    "(an object, or a wall). Great for shooting, line-of-sight, AI vision, sensors. "
    "Sets hitSvc / hitDist / hitX / hitY / hitType (1=object, 2=wall, 0=nothing) and "
    "returns the hit object's svc.", [
    ("From an object", "target = raycast.cast(1, 101)   >> maze 1, from svc 101 <<"),
    ("From a point", "raycast.cast(1, 128, 96, 45)      >> maze, x, y, angle <<")])
_EXTRA_DOCS["destroy"] = ("Remove an object at runtime (enemy dies, pickup taken).", [
    ("Example", "destroy(hitSvc)")])
_EXTRA_DOCS["spawnObject"] = ("Spawn a Doom-style billboard at runtime (colour or "
    "sprite), visible in the raycaster. Returns its svc.", [
    ("Colour", 'fireball = spawnObject("#ff8800", 128, 96)'),
    ("Sprite", 'e = spawnObject("imp.png", 200, 160)')])
_EXTRA_DOCS["clone"] = ("Duplicate an existing object (a prefab) at a new position, "
    "copying its look/scale/collide. Returns the new svc. Great for spawning waves.", [
    ("One copy", "e = clone(9, 200, 120)"),
    ("A wave", "for i=1 to 5 { clone(9, i*40, 300) }")])
_EXTRA_DOCS["create"] = ("Spawn a new 2D sprite object at runtime; returns its svc.", [
    ("Example", 's = create("box.png", 100, 100, 32, 32)')])
_EXTRA_DOCS["exists"] = ("1 if an object (by svc) still exists, else 0. Use before "
    "chasing/damaging so destroyed things are skipped.", [("Example", "if (exists(10)) { }")])
_EXTRA_DOCS["physics.push"] = ("Give an object a velocity (launch a bullet, shove "
    "something). It then moves on its own each frame and stops at maze walls.", [
    ("Launch forward", "physics.push(b, { cos(angle*0.01745)*8, sin(angle*0.01745)*8, 0 })")])
_EXTRA_DOCS["physics.hitWall"] = ("1 if a pushed object has stopped against a wall "
    "(use it to destroy spent bullets).", [("Example", "if (physics.hitWall(b)) { destroy(b) }")])
_EXTRA_DOCS["physics.stop"] = ("Zero an object's velocity.", [("Example", "physics.stop(b)")])
_EXTRA_DOCS["list"] = ("A resizable list (like Unity List). Methods: .add(x) .get(i) "
    ".set(i,x) .remove(x) .removeAt(i) .contains(x) .indexOf(x) .clear() .pop() .last  "
    "and .count. Iterate with: for i=0 to mylist.count-1 { x = mylist.get(i) }.", [
    ("Declare", "list bolts = []"),
    ("Use", "bolts.add(b)   n = bolts.count   first = bolts.get(0)")])
_EXTRA_DOCS["opacity"] = ("On a raycastObject: transparency 0..1 (1 = solid, 0.3 = ghostly). "
    "Colours also accept #RRGGBBAA alpha.", [("Example", "opacity: 0.4")])
_EXTRA_DOCS["lerp"] = ("Blend between two numbers OR two vectors by t (0..1). "
    "t=0 gives a, t=1 gives b, 0.5 is halfway. Great for smooth movement/fades.", [
    ("Number", "x = lerp(0, 100, 0.25)     >> 25 <<"),
    ("Vector", "pos = lerp({0,0,0}, {10,0,0}, 0.5)"),
    ("Ease toward", "hp = lerp(hp, target, 0.1)   >> each frame, glides to target <<")])
_EXTRA_DOCS["slerp"] = ("Spherical blend between two direction vectors by t (0..1). "
    "Rotates smoothly instead of cutting straight across - good for turning/aiming.", [
    ("Turn", "dir = slerp({1,0,0}, {0,1,0}, 0.5)")])
_EXTRA_DOCS["lerpAngle"] = ("Blend between two angles (degrees) the short way round the "
    "circle, so 350 -> 10 goes through 0, not all the way back.", [
    ("Smooth turn", "angle = lerpAngle(angle, target, 0.2)")])
_EXTRA_DOCS["light"] = ("Inside raycast: a baked point light. Its colour, distance "
    "falloff and wall shadows are computed ONCE and baked into the floors, walls "
    "and colours - so it costs nothing per frame. x/y are world pixels (like an "
    "object), radius is in cells, intensity scales the brightness.", [
    ("Torch", 'light { x: 200, y: 160, color: #ff9a3c, radius: 5, intensity: 1.4 }'),
    ("Cool lamp", 'light { x: 480, y: 320, color: #5588ff, radius: 4 }')])
_EXTRA_DOCS["ambient"] = ("Inside raycast: the base light level (0..1) everywhere "
    "before lights are added. Low (e.g. 0.2) makes lights and shadows dramatic; "
    "with no lights the level stays full-bright so it looks unchanged.", [
    ("Moody", "ambient: 0.2")])
_EXTRA_DOCS["intensity"] = ("On a light: how bright it is (1 = normal, higher = brighter).",
    [("Example", "intensity: 1.4")])
_EXTRA_DOCS["function"] = ("Create your OWN reusable function, just like the built-in "
    "lerp or clamp. Give it a name and parameters, do some work, and return a value. "
    "Call it anywhere in your scripts by name. Types on parameters are optional.", [
    ("Simple", "function double(n) {\n    return n * 2\n}\n>> now use  double(21)  ->  42 <<"),
    ("Several params", "function damage(base, armor) {\n    return max(1, base - armor)\n}"),
    ("Typed param", 'function greet(string who) {\n    return "hi " + who\n}')])
_EXTRA_DOCS["func"] = _EXTRA_DOCS["function"]
_EXTRA_DOCS["snip"] = ("Define a reusable function (same as  function ). "
    "snip name(params) { ... return value }, then call name(args) in any script.", [
    ("Example", "snip heal(hp, amt) {\n    return min(100, hp + amt)\n}")])
_EXTRA_DOCS["nav.follow"] = ("Head straight for another object (around walls). Call it every frame (or every few) to chase a MOVING target - the Unity chase pattern.", [("Chase", "nav.follow(1, 10, 101, 2.0)   >> object 10 chases 101 at speed 2 <<")])
_EXTRA_DOCS["nav.setDestination"] = ("Unity-style pathfinding: send an object to a "
    "spot (or another object) and it walks there by itself, going AROUND walls. "
    "Call it once; the engine moves the agent every frame until it arrives.", [
    ("To a point", "nav.setDestination(1, 10, 300, 260)      >> maze 1, object 10 -> (300,260) <<"),
    ("Chase player", "nav.setDestination(1, 10, 101)          >> object 10 -> object 101 <<"),
    ("With speed", "nav.setDestination(1, 10, 300, 260, 2.0)")])
_EXTRA_DOCS["nav.reachable"] = ("1 if there is a clear path (around walls) for an "
    "object to reach a point or another object, else 0. Does not move anything.", [
    ("Check", "if (nav.reachable(1, 10, 101)) { nav.setDestination(1, 10, 101) }")])
_EXTRA_DOCS["nav.arrived"] = ("1 once an agent has reached the end of its path.", [
    ("Example", "if (nav.arrived(10)) { >> pick a new spot << }")])
_EXTRA_DOCS["nav.remainingDistance"] = ("How far (in world units) an agent still has "
    "to travel along its path. 0 when idle or arrived.", [("Example", "d = nav.remainingDistance(10)")])
_EXTRA_DOCS["nav.stop"] = ("Cancel an agent's path so it stops moving.", [("Example", "nav.stop(10)")])
_EXTRA_DOCS["shadowCaster"] = ("On a light: whether it casts shadows (walls block "
    "it). true = realistic shadows, false = the light glows through walls (flatter, "
    "faster). Toggle per-light, Unity-style.", [
    ("Shadowed", "light { x: 100, y: 60, color: #ff8a2c, radius: 5, shadowCaster: true }"),
    ("No shadow", "light { x: 100, y: 60, color: #ffffff, radius: 8, shadowCaster: false }")])
_EXTRA_DOCS["lightmap.generate"] = ("Bake a maze's lighting to a PNG and return its "
    "path. Each maze (by mazeID) gets its own lightmap, so one .glass file can hold "
    "several mazes with different baked lighting.", [
    ("Bake", 'path = lightmap.generate("1")')])
_EXTRA_DOCS["lightmap.grab"] = ("Return the path to a maze's baked lightmap PNG "
    "(baking it first if needed), so you can apply it to a texture.", [
    ("Use it", 'tex = lightmap.grab("1")')])
_EXTRA_DOCS["audio.playSound"] = ("Play a sound file, or a pre-loaded clip. "
    "speed/volume/quality/hertz work always. Add radius, x, y (or parent), "
    "realtimeRef, and is3D for positional 3D audio inside a raycast scene: "
    "is3D: true turns it on; x/y are the sound's WORLD position; parent "
    "(same idea as raycast{}'s own parent:, an svc number or a name - "
    "quoted or not, both work: parent: monster1 and parent: \"monster1\" "
    "are the same thing) makes it FOLLOW that object every frame instead - "
    "a monster's roar genuinely moves with it as it walks, not stuck where "
    "it started. Leave x/y/parent out entirely and it defaults to the "
    "listener's own position (no panning or falloff, just the room echo). "
    "radius is the falloff distance in cells past which it's inaudible. "
    "realtimeRef: true measures the room live instead of using the baked "
    "estimate (Baking tab -> Bake audio acoustics). VOLUME, PAN, the "
    "OCCLUSION muffling, and the REVERB character all genuinely keep "
    "tracking every frame now - walk around a playing sound (or parent it "
    "to something that moves) and the panning, wall-muffling, and room "
    "character all update live, not just at the moment it started, and "
    "panning is smooth and continuous all the way around, including "
    "directly behind you. Simple, honest approximation overall - stereo "
    "pan + a decaying echo that gets stronger in tighter rooms, not real "
    "acoustic simulation. Needs the optional miniaudio package, same as "
    "quality/hertz.", [
    ("Simple", 'audio.playSound "song.mp3" { speed: 1, volume: 1 }'),
    ("Positional 3D", 'id = audio.playSound "boom.mp3" { volume: 1, '
     'x: hitX, y: hitY, radius: 10, is3D: true, realtimeRef: false }'),
    ("Follows a moving object", 'id = audio.playSound "roar.mp3" { volume: 1, '
     'parent: monster1, radius: 15, is3D: true, loop: true }'),
    ("Clip (advanced)", 'audio.playSound (Track) { volume: 0.8 }')])
_EXTRA_DOCS["light.create"] = ("A cheap, DYNAMIC point light inside a raycast "
    "scene - the script-level counterpart to a light{} block, but movable and "
    "recolorable every frame instead of baked once. mazeID picks the scene; "
    "parent (same idea as raycast{}'s own parent: - an svc number or a name, "
    "quoted or not, both work) makes it FOLLOW that object every frame; "
    "x/y are a fixed spot instead if you don't need it to follow anything. "
    "color is a quoted hex string (color: \"#ff8800\", not color: #ff8800 - "
    "bare # starts a comment in this language). radius is in cells, "
    "intensity scales the brightness. Returns a light id for "
    "light.setColor/setPos/destroy.\n\n"
    "The tradeoff, worth knowing: this light does NOT cast shadows and "
    "shines straight through walls - no per-pixel occlusion test at all. "
    "That's not a bug, it's what makes it cheap enough to move every frame - "
    "measured directly: a baked light{} with shadowCaster needs a full "
    "re-bake to change at all, which takes over a second even at the capped "
    "live resolution (~72x a 60fps frame budget), so it can never move live. "
    "Use light{} for static shadow-casting lighting, light.create for "
    "anything that needs to move or change color - a torch a monster "
    "carries, a pulsing warning light, a muzzle flash.", [
    ("Follows a monster", 'lid = light.create { mazeID: 1, parent: monster1, '
     'color: "#ff4422", radius: 4, intensity: 1.3 }'),
    ("Fixed position", 'lid = light.create { mazeID: 1, x: 400, y: 300, '
     'color: "#66ccff", radius: 5, intensity: 1.0 }')])
_EXTRA_DOCS["light.setColor"] = ("Change a dynamic light's color live - "
    "instant, no re-bake, unlike a baked light{} which can't change color "
    "without a full re-bake. color is a quoted hex string.", [
    ("Flash red", 'light.setColor(lid, "#ff0000")')])
_EXTRA_DOCS["light.setPos"] = ("Manually move a dynamic light to a fixed "
    "spot - clears any parent: tracking it had, since a manual move and "
    "automatic parent-following are mutually exclusive.", [
    ("Move it", "light.setPos(lid, 400, 300)")])
_EXTRA_DOCS["light.destroy"] = ("Remove a dynamic light created by "
    "light.create. Returns 1 on success, 0 if the id doesn't exist "
    "(already destroyed, or never existed).", [
    ("Remove it", "light.destroy(lid)")])
_EXTRA_DOCS["loadPost"] = ("Load a *.postEffect profile by name (post.name), "
    "replacing whatever profile was active before it - only one is ever "
    "active at a time. smoothness (seconds, optional) eases it in over "
    "that long instead of snapping fully on instantly; omit it for an "
    "instant switch. The name works quoted or not.", [
    ("Instant", "loadPost(testing)"),
    ("Ease in over 0.7s", "loadPost(testing, smoothness: 0.7)")])
_EXTRA_DOCS["removePost"] = ("Unload a *.postEffect profile by name - only "
    "does anything if that profile is the one currently active.", [
    ("Remove it", "removePost(testing)")])
_EXTRA_DOCS["postQuality"] = ("Change a *.postEffect profile's quality "
    "(0-100) at runtime, live - works whether or not it's the currently "
    "active profile.", [
    ("Lower it", "postQuality(testing, quality: 60)")])
_EXTRA_DOCS["postEffects.vignette"] = ("Darkens the screen edges - only "
    "valid inside a *.postEffect's effect { } block. intensity (0..1, "
    "default 0.5): how dark the corners get. smoothness (0..1, default "
    "0.5): how gradual the falloff is.", [
    ("Strong, hard edge", "postEffects.vignette(intensity: 0.9, smoothness: 0.2)")])
_EXTRA_DOCS["postEffects.bloom"] = ("A soft glow around bright areas - "
    "only valid inside a *.postEffect's effect { } block. Honest about "
    "what this is: a whole-frame blur added back additively, not a true "
    "per-pixel-threshold HDR bloom (that would need per-pixel Python work "
    "this engine's CPU-only renderer can't afford every frame) - still a "
    "real, visible glow. intensity (0..1, default 0.4): how strong. "
    "radius (1-20, default 6): how far it spreads; quality scales the "
    "blur cost down at lower settings.", [
    ("Subtle glow", "postEffects.bloom(intensity: 0.3, radius: 5)")])
_EXTRA_DOCS["postEffects.colorGrading"] = ("Tint/saturation/contrast/"
    "brightness, all independent - only valid inside a *.postEffect's "
    "effect { } block. tint: a quoted hex color, multiplied over the "
    "frame. saturation (0..2, default 1.0): 0 is a real luminosity-"
    "weighted grayscale, 2 is strongly oversaturated. contrast (0..2, "
    "default 1.0): below 1 flattens toward gray, above 1 genuinely pushes "
    "darks darker and brights brighter. brightness (-1..1, default 0.0): "
    "blends toward white or black.", [
    ("Warm and punchy", 'postEffects.colorGrading(tint: "#fff4e0", '
     'saturation: 1.2, contrast: 1.15)'),
    ("Desaturated/moody", "postEffects.colorGrading(saturation: 0.3, contrast: 1.1)")])
_EXTRA_DOCS["postEffects.filmGrain"] = ("Animated noise - only valid "
    "inside a *.postEffect's effect { } block. Genuinely flickers frame "
    "to frame (a random offset into a cached noise tile each time, not "
    "regenerated). Measured directly: about 3.2ms on a 640x480 frame - "
    "real, worth being mindful of if stacking several effects together. "
    "intensity (0..1, default 0.15): grain strength. size (8-128, "
    "default 48): the noise tile's resolution - smaller is finer grain.", [
    ("Subtle", "postEffects.filmGrain(intensity: 0.1)"),
    ("Heavy/old-film", "postEffects.filmGrain(intensity: 0.35, size: 24)")])
_EXTRA_DOCS["postEffects.tonemapping"] = ("A filmic look - only valid "
    "inside a *.postEffect's effect { } block. Honest about what this is: "
    "not true HDR tonemapping (there's no HDR data here, everything's "
    "already 8-bit by the time a post effect sees it) - a midtone "
    "contrast boost plus a highlight rolloff that keeps bright areas "
    "from clipping hard to pure white. strength (0..1, default 0.5): "
    "overall effect strength.", [
    ("Standard", "postEffects.tonemapping(strength: 0.5)")])
_EXTRA_DOCS["postEffects.whiteBalance"] = ("Warm/cool and green/magenta "
    "shift - only valid inside a *.postEffect's effect { } block. "
    "temperature (-100..100, default 0): negative is cooler/blue, "
    "positive is warmer/orange. tint (-100..100, default 0): negative "
    "is green, positive is magenta.", [
    ("Warm sunset", "postEffects.whiteBalance(temperature: 40)"),
    ("Cold/sterile", "postEffects.whiteBalance(temperature: -35, tint: -10)")])
_EXTRA_DOCS["postEffects.antiAliasing"] = ("Softens the raycaster's hard "
    "per-column pixel edges - only valid inside a *.postEffect's "
    "effect { } block. Honest about what this is: a light blur, not true "
    "FXAA/MSAA (real edge detection doesn't fit this engine's per-frame "
    "budget) - genuinely reduces jaggedness without being a heavy blur. "
    "strength (0..1, default 0.5): how much softening.", [
    ("Subtle", "postEffects.antiAliasing(strength: 0.3)")])
_EXTRA_DOCS["postEffects.motionBlur"] = ("A fading trail of recent frames "
    "- only valid inside a *.postEffect's effect { } block. Honest about "
    "what this is: an accumulation-buffer trail, not true per-pixel "
    "velocity vectors (this engine's raycaster doesn't expose per-pixel "
    "motion data) - still a real, visible trail on fast movement/turning. "
    "strength (0..0.95, default 0.4): how much of the trail persists "
    "each frame - higher is a longer trail.", [
    ("Subtle", "postEffects.motionBlur(strength: 0.25)")])
_EXTRA_DOCS["postEffects.autoExposure"] = ("Gradually brightens dark "
    "scenes and dims bright ones toward a target level, like a camera's "
    "exposure adjusting - only valid inside a *.postEffect's effect { } "
    "block. speed (0..1, default 0.05): how fast it adjusts - lower is "
    "slower/more gradual. target (0..1, default 0.5): the brightness "
    "level aimed for. strength (0..1, default 0.5): how strongly the "
    "correction applies.", [
    ("Slow, cinematic", "postEffects.autoExposure(speed: 0.03)")])
_EXTRA_DOCS["overrideOPLim"] = ("A pragma that unlocks ramAllocated/useCPU/useGPU/"
    "useARam. Must be the VERY FIRST thing in the file - before any import, "
    "variable, or rule.", [
    ("Example", '#overrideOPLim {\n    ramAllocated: g3\n    useCPU: true\n    useARam: true\n}')])
_EXTRA_DOCS["ramAllocated"] = ("Declares how much RAM this script is designed to "
    "use - a CAP for checking, not a reservation (Glass never pre-allocates "
    "memory for you). g/m/k = GB/MB/KB. Drives dUMR; only actively monitored "
    "if useARam is also true.", [
    ("Example", "ramAllocated: g3"), ("Smaller", "ramAllocated: m512")])
_EXTRA_DOCS["useCPU"] = ("Inside #overrideOPLim: raises CPU-side simulation "
    "budgets (e.g. the pathfinding search limit on big maps). Compile-time "
    "only - a script can't flip this at runtime, only the pragma sets it.", [
    ("Example", "useCPU: true")])
_EXTRA_DOCS["useGPU"] = ("Inside #overrideOPLim: reserved for GPU-accelerated "
    "rendering. Glass's renderer is pure software right now, so this doesn't "
    "change anything yet - it's parsed and stored for later. Compile-time "
    "only, same as useCPU.", [
    ("Example", "useGPU: true")])
_EXTRA_DOCS["useARam"] = ("Inside #overrideOPLim: turns on LIVE monitoring of "
    "this script's actual RAM use against ramAllocated (checked every couple "
    "of seconds), exposing ram.overLimit. Without this, ramAllocated only "
    "ever feeds the one-time dUMR check at load - nothing is monitored.", [
    ("Example", "useARam: true")])
_EXTRA_DOCS["dUMR"] = ("\"Does User Meet Requirement\" - true/false, comparing "
    "the system's actual total RAM against ramAllocated. Always computed if "
    "ramAllocated is set (needs #overrideOPLim), regardless of useARam. Read "
    "it like any built-in value; it can't be set by a script.", [
    ("Warn if underpowered", 'if (dUMR == 0) { alert("Your PC may not meet the recommended RAM!") }')])
_EXTRA_DOCS["ram.overLimit"] = ("1 if this script's LIVE memory use has "
    "exceeded the ramAllocated cap, else 0. Only ever updates if useARam is "
    "true - otherwise it stays 0 and nothing is being watched.", [
    ("Example", 'if (ram.overLimit == 1) { alert("Running low on the RAM budget!") }')])
_EXTRA_DOCS["alert"] = ("Show a popup message box. Works as a button action "
    "(action: alert, message: \"...\") or called directly from a script.", [
    ("From a script", 'alert("Something happened!")'),
    ("From a button", 'button "Info" { action: alert, message: "Hi!" }')])
_EXTRA_DOCS["tag"] = ("Label a vcr.* object with a group name, so scripts can find "
    "it (or all of its group) without hardcoding svc numbers.", [
    ("Example", "vcr.raycastObject \"e1\" { color: #ff3b3b, svc: 10, tag: enemy }")])
_EXTRA_DOCS["properties.get.tag"] = ("One object (its svc) with this tag - "
    "whichever is found first. \"\" if none exist.", [
    ("Example", 's = properties.get.tag("enemy")')])
_EXTRA_DOCS["properties.get.tags"] = ("EVERY object with this tag, as a real "
    "list - .count/.get(i)/etc work immediately. Only ever contains objects "
    "that are still alive right now (destroy() drops them from later calls "
    "automatically - no more exists() checks needed).", [
    ("Loop over a group", 'Enemies = properties.get.tags("enemy")\nfor i = 0 to Enemies.count - 1 {\n    s = Enemies.get(i)\n}')])
COMPLETION_DOCS.update(_EXTRA_DOCS)

# ---- category sets (drive context-aware, kind-tagged suggestions) ----------
DECL_KEYS = {"string", "int", "float", "double", "bool", "boolean", "number", "list",
             "import", "textgroup", "setup", "update", "snip", "function", "func", "if"}
MENU_KEYS = {"menu.full", "menu.ui", "menu.dynamic"}
ELEMENT_KEYS = {"main", "menu", "holder", "panel", "bar", "button", "link", "text",
                "label", "input", "separator", "image", "vcr.image", "vcr.video",
                "scale", "center", "collider", "grab", "particleSystem", "webInput",
             "raycast", "material", "raycastObject", "light", "overrideOPLim",
             "postEffect"} | DECL_KEYS | MENU_KEYS
PROPERTY_KEYS = {"color", "background", "backgroundColor", "width", "height", "size",
                 "center", "font", "title", "title_style", "outline", "outlinecolor",
                 "scroll", "radius", "sprite", "opacity", "visible", "hidden",
                 "paused", "settings", "svc", "name", "solid", "friction",
                 "istrigger", "content", "volume", "speed", "startOn", "compress",
                 "type", "x", "y", "bind",
                 "map", "fov", "columns", "ceiling", "floor", "parent", "cellSize",
                 "tiling", "tilingVector", "image", "moveSpeed", "turnSpeed",
                 "fogColor", "fogRange", "fogAmount", "mazeID", "collide",
                 "floorMap", "roofMap", "type", "ambient", "intensity", "shadowCaster",
                 "ramAllocated", "useCPU", "useGPU", "useARam", "tag", "mode", "light",
                 "bounceA", "sizeOverLife", "radius", "realtimeRef", "is3D"}
ACTION_KEYS = {"action", "set", "do", "call", "js", "url", "target", "openNew",
               "getGlass", "navigate", "reload", "reloadui", "back", "forward",
               "home", "newtab", "closetab", "devtools", "openeditor", "viewsource",
               "showlog", "stripads", "toggleblock", "cleardata", "wiki", "zoomin",
               "zoomout", "zoomreset", "quit", "hide", "show", "toggle", "alert"}
SCRIPT_KEYS = {"input.getHeld", "input.getClick", "adjvcr", "adjvcr.detect",
               "cursor.hide", "cursor.show", "cursor.lock", "cursor.unlock",
               "cursor.confine", "cursor.free", "mouse.dx", "mouse.dy", "mouse.x",
               "mouse.y", "mouse.down", "time.normal", "time.held", "screen.width",
               "screen.height", "physics.gravity", "pref.save", "pref.load",
               "audio.playSound", "audio.gatherClip", "audio.playLast",
               "audio.pauseCurrent", "audio.changeVolume", "audio.isPlaying",
               "audio.getAudioId", "return", "after", "create", "burst", "random", "rand", "lerp", "clamp", "min", "max", "abs", "sqrt", "sin", "cos", "floor", "round", "repeat", "for", "collide.createMesh", "collide.detect", "properties.get", "getProperty.position", "getProperty.scale", "getProperty.rotation", "getProperty.distance", "getProperty.tag", "getProperty.size", "getProperty.friction", "getProperty.isTrigger", "getProperty.velocity", "getProperty.speed", "getProperty.kind", "raycast.cast", "destroy", "spawnObject", "exists", "clone", "create", "physics.push", "physics.stop", "physics.hitWall", "nav.setDestination", "nav.follow", "nav.reachable", "nav.stop", "nav.arrived", "nav.remainingDistance", "lightmap.generate", "lightmap.grab", "dUMR", "ram.overLimit", "alert", "properties.get.tag", "properties.get.tags",
               "light.create", "light.setColor", "light.setPos", "light.destroy",
               "loadPost", "removePost", "postQuality"}
VALUE_KEYS = {"pixel", "standR", "true", "false"}
POSTEFFECT_BLOCK_KEYS = {"post.name", "post.cache", "post.quality", "effect"}
POSTEFFECT_FX_KEYS = {"postEffects.vignette", "postEffects.bloom",
                      "postEffects.colorGrading", "postEffects.filmGrain",
                      "postEffects.tonemapping", "postEffects.whiteBalance",
                      "postEffects.antiAliasing", "postEffects.motionBlur",
                      "postEffects.autoExposure"}
COMPLETIONS = sorted(COMPLETION_DOCS.keys())

# --- document-aware symbols (Unity-style: your own declarations show up) -----
import re as _re
_DECL_RE = _re.compile(r'(?m)^\s*(string|int|float|double|bool|boolean|number|var)\s+([A-Za-z_]\w*)\s*=')
_NAME_RE = _re.compile(r'\bname\s*:\s*([A-Za-z_]\w*)')
_TG_RE   = _re.compile(r'\btextgroup\s*\{\s*([A-Za-z_]\w*)')
_IMP_RE  = _re.compile(r'(?m)^\s*import\s+([A-Za-z_][\w.]*)')
_STR_RE  = _re.compile(r'"[^"\n]*"')


def _blank_strings(text):
    """Replace the contents of quoted strings with spaces (same length, same
    line/col positions) so scanning below can't mistake literal text inside a
    string - e.g. text "name: Bob" - for a real declaration."""
    return _STR_RE.sub(lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', text)


def scan_symbols(text):
    """Find user-declared identifiers in the current file so they can be offered
    in autocomplete, like an IDE. Returns {name: (kind, description)}."""
    text = _blank_strings(text or "")
    syms = {}
    for typ, name in _DECL_RE.findall(text):
        syms[name] = ("variable", f"your {typ} variable")
    for name in _NAME_RE.findall(text):
        syms.setdefault(name, ("object", "your game object"))
    for name in _TG_RE.findall(text):
        syms.setdefault(name, ("font", "your textgroup / font"))
    for name in _IMP_RE.findall(text):
        syms.setdefault(name, ("package", "imported package"))
    return syms


# ---- IntelliSense look: per-kind icons, row delegate, docs flyout ----------
# (word_role, kind_role, desc_role, examples_role)
WORD_ROLE = int(Qt.ItemDataRole.UserRole) + 11
KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 12
DESC_ROLE = int(Qt.ItemDataRole.UserRole) + 13
EX_ROLE   = int(Qt.ItemDataRole.UserRole) + 14

KIND_STYLE = {   # kind -> (color, glyph, label)
    "element":  ("#1e88e5", "E", "element"),
    "property": ("#26a69a", "P", "property"),
    "action":   ("#ef8e3b", "A", "action"),
    "function": ("#c586c0", "\u0192", "function"),
    "type":     ("#56b6c2", "T", "type"),
    "value":    ("#98c379", "=", "value"),
    "keyword":  ("#8a94a6", "K", "keyword"),
    "variable": ("#3ecb7a", "V", "variable"),
    "object":   ("#5aa2ff", "O", "object"),
    "font":     ("#e5c07b", "F", "font"),
    "package":  ("#c792ea", "{}", "package"),
}


def kind_of(word):
    if word in DECL_KEYS:
        return "type"
    if word in SCRIPT_KEYS:
        return "function"
    if word in ACTION_KEYS:
        return "action"
    if word in VALUE_KEYS:
        return "value"
    if word in ELEMENT_KEYS:
        return "element"
    if word in PROPERTY_KEYS:
        return "property"
    return "keyword"


_KIND_ICON_CACHE = {}


def kind_icon(kind, size=16):
    key = (kind, size)
    if key in _KIND_ICON_CACHE:
        return _KIND_ICON_CACHE[key]
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QFont, QPen
    from PyQt6.QtCore import QRectF, Qt as _Qt
    color, glyph, _ = KIND_STYLE.get(kind, KIND_STYLE["keyword"])
    pm = QPixmap(size, size); pm.fill(_Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    c = QColor(color)
    fill = QColor(c); fill.setAlpha(48)
    p.setBrush(fill); p.setPen(QPen(c, 1.2))
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), 4, 4)
    f = QFont("Segoe UI", int(size * 0.5)); f.setBold(True); p.setFont(f)
    p.setPen(c)
    p.drawText(pm.rect(), _Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    _KIND_ICON_CACHE[key] = pm
    return pm


class CompletionDelegate(QStyledItemDelegate):
    """Draws an IntelliSense-style row: [icon] name .......... kind."""
    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        s.setHeight(max(22, s.height()))
        return s

    def paint(self, painter, option, index):
        from PyQt6.QtCore import QRect, Qt as _Qt
        from PyQt6.QtGui import QColor
        painter.save()
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        if sel:
            painter.fillRect(option.rect, QColor("#1f6feb"))
        r = option.rect
        kind = index.data(KIND_ROLE) or "keyword"
        icon = kind_icon(kind, 16)
        iy = r.top() + (r.height() - 16) // 2
        painter.drawPixmap(r.left() + 6, iy, icon)
        name = index.data(WORD_ROLE) or ""
        color, _, label = KIND_STYLE.get(kind, KIND_STYLE["keyword"])
        painter.setPen(QColor("#ffffff") if sel else QColor("#d7e0ea"))
        name_rect = QRect(r.left() + 28, r.top(), r.width() - 120, r.height())
        painter.drawText(name_rect, _Qt.AlignmentFlag.AlignVCenter
                         | _Qt.AlignmentFlag.AlignLeft, name)
        painter.setPen(QColor("#cfe3f5") if sel else QColor(color))
        tag_rect = QRect(r.right() - 96, r.top(), 90, r.height())
        f = painter.font(); f.setPointSize(max(7, f.pointSize() - 1)); painter.setFont(f)
        painter.drawText(tag_rect, _Qt.AlignmentFlag.AlignVCenter
                         | _Qt.AlignmentFlag.AlignRight, label)
        painter.restore()


class DocFlyout(QFrame):
    """A non-focusable documentation panel shown beside the completion popup."""
    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedWidth(320)
        self.setStyleSheet("QFrame{background:#0b1017;border:1px solid #243140;"
                           "border-radius:8px;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(12, 10, 12, 12); lay.setSpacing(6)
        self.title = QLabel(""); self.title.setStyleSheet(
            "font-size:14px;font-weight:600;color:#6cf09a;background:transparent;border:none;")
        self.title.setWordWrap(True)
        self.body = QLabel(""); self.body.setWordWrap(True)
        self.body.setStyleSheet("color:#c7d2dc;background:transparent;border:none;")
        self.code = QLabel(""); self.code.setWordWrap(True)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code.setStyleSheet(
            "background:#11161c;border:1px solid #1c2530;border-radius:6px;"
            "padding:8px;color:#bfe3c9;font-family:Consolas,'DejaVu Sans Mono',monospace;")
        lay.addWidget(self.title); lay.addWidget(self.body); lay.addWidget(self.code)

    def show_for(self, title, kind, desc, examples):
        label = KIND_STYLE.get(kind, KIND_STYLE["keyword"])[2]
        self.title.setText(f"{title}")
        self.body.setText((f"({label})  " if label else "") + (desc or ""))
        if examples:
            lab, code = examples[0]
            self.code.setText(code); self.code.show()
        else:
            self.code.hide()
        self.adjustSize()

# Tab-to-scaffold templates. "\x00" marks where the cursor lands after insert.
# Anything without an entry just inserts the bare word (old behaviour).
_C = "\x00"
SNIPPETS = {
    # components / elements
    "text":   f'text "{_C}" {{}}',
    "button": f'button "{_C}" {{ action: reload }}',
    "link":   f'link "{_C}" {{ url: https:// }}',
    "label":  f'label "{_C}" {{}}',
    "input":  f'input {{ {_C} }}',
    "holder": f'holder ( size: 300 ) {{\n    {_C}\n}}',
    "menu":   f'*.menu {{\n    {_C}\n}}',
    "panel":  f'*.panel {{\n    {_C}\n}}',
    "bar":    f'*.bar {{\n    {_C}\n}}',
    "main":   f'*.main {{\n    menu.full\n    background: #0d1117\n    {_C}\n}}',
    "separator": "separator",
    "textgroup": f'textgroup {{ {_C}, fontname }}',
    "scale":  f'scale {{ {_C} }}',
    "center": f'center: {_C}',
    "grab":   f'grab {{ "{_C}", Condition }}',
    "if":     f'if ({_C}) {{\n    \n}}',
    "else":   f'else {{\n    {_C}\n}}',
    "setup":  f'setup {{\n    {_C}\n}}',
    "function": f'function name(a, b) {{\n    return {_C}\n}}',
    "func": f'func name(a, b) {{\n    return {_C}\n}}',
    "update": f'update {{\n    {_C}\n}}',
    # variable decls
    "string": f'string {_C} = ""',
    "int":    f'int {_C} = 0',
    "float":  f'float {_C} = 0.0',
    "bool":   f'bool {_C} = true',
    # properties
    "title":  f'title: "{_C}" {{ center: center, color: #ffffff }}',
    "size":   f'size: {_C}',
    "color":  f'color: #{_C}',
    "background": f'background: #{_C}',
    # vcr media
    "vcr.image": f'vcr.image "{_C}" {{ size: 64, svc: 1 }}',
    "vcr.gif":   f'vcr.gif "{_C}" {{ size: 64, svc: 2 }}',
    "vcr.video": f'vcr.video "{_C}" {{ size: 320x180, settings: true, svc: 3 }}',
    "vcr.colide": f'vcr.colide "{_C}" {{ size: 40, {{40,40}}, x: 0, y: 0, svc: 100 }}',
    # packages
    "import": f'import {_C}',
    "call":   f'call: {_C}',
    # audio
    "audio.playSound":   f'audio.playSound "{_C}" {{ speed: 1, volume: 1 }}',
    "audio.clip":        f'audio.clip {_C} = audio.gatherClip {{ "song.mp3" }}',
    "audio.gatherClip":  f'audio.gatherClip {{ "{_C}" }}',
    "audio.changeVolume": f'audio.changeVolume {{ audioID: {_C}, volume: 100 }}',
    "audio.pauseCurrent": f'audio.pauseCurrent {{ audioID: {_C}, fadeAmount: 5 }}',
    "audio.playLast":    f'audio.playLast {{ audioID: {_C} }}',
    # overrideOPLim
    "overrideOPLim": f'overrideOPLim {{\n    ramAllocated: g{_C}\n    useCPU: true\n}}',
    "alert": f'alert("{_C}")',
    "properties.get.tag": f'properties.get.tag("{_C}")',
    "properties.get.tags": f'properties.get.tags("{_C}")',
    "getProperty.tag": f'getProperty.tag("{_C}")',
}


class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(11)
        self.setFont(f)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        import glasstheme
        self.theme = glasstheme.active()
        self._apply_chrome()
        self.line_area = LineNumberArea(self)
        self.sprite_request = None          # set by EditorWindow: fn(name, kind)
        self._sprite_btns = []
        self._problem_sels = []             # error/warning line highlights
        self._els_cache = {}                # cached scan_elements, keyed by doc revision
        self._els_rev = -1
        self._sprite_icon = None            # QIcon cached on first use
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_area)
        self.updateRequest.connect(lambda *_: self._refresh_sprite_buttons())
        self.textChanged.connect(self._refresh_sprite_buttons)
        self.cursorPositionChanged.connect(self._highlight_current)
        self._update_width()
        self._highlight_current()
        self.highlighter = GlassHighlighter(self.document(), self.theme)
        self._setup_completer()
        try:
            import prefs
            self.hover_docs = bool(prefs.load("hover_docs", True))
        except Exception:
            self.hover_docs = True

    # ---- hover descriptions (new-user help) --------------------------------
    def _token_at(self, qpos):
        cur = self.cursorForPosition(qpos)
        line = cur.block().text()
        col = cur.positionInBlock()
        if col > len(line):
            return ""
        L = R = col
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
        while L > 0 and line[L - 1] in chars:
            L -= 1
        while R < len(line) and line[R] in chars:
            R += 1
        return line[L:R].strip(".")

    def _lookup_doc(self, word):
        if not word:
            return None
        d = COMPLETION_DOCS.get(word)
        if d:
            return word, d
        low = word.lower()
        for k, v in COMPLETION_DOCS.items():
            if k.lower() == low:
                return k, v
        return None

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip and getattr(self, "hover_docs", True):
            from PyQt6.QtWidgets import QToolTip
            hit = self._lookup_doc(self._token_at(e.pos()))
            if hit:
                name, (desc, examples) = hit
                html = (f"<b style='color:#6cf09a'>{name}</b><br>{desc}")
                if examples:
                    ex = examples[0][1].replace("<", "&lt;").replace("\n", "<br>")
                    html += f"<br><code style='color:#c3e88d'>{ex}</code>"
                QToolTip.showText(e.globalPos(), html, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def _apply_chrome(self):
        t = self.theme
        self.setStyleSheet(
            f"background:{t['background']};color:{t['foreground']};border:none;"
            f"selection-background-color:{t['selection']};")

    def apply_theme(self, theme):
        """Recolour the whole editor (chrome + syntax) from a theme dict."""
        import glasstheme
        self.theme = glasstheme._norm(theme)
        self._apply_chrome()
        try:
            self.highlighter.apply_theme(self.theme)
        except Exception:
            self.highlighter = GlassHighlighter(self.document(), self.theme)
        if self.line_area:
            self.line_area.update()
        self._highlight_current()

    # ---- autocomplete (IntelliSense) --------------------------------------
    _WORD_ROLE = WORD_ROLE

    def _setup_completer(self):
        # what to suggest in each context (see _active_candidates)
        child_elems = ELEMENT_KEYS - DECL_KEYS - MENU_KEYS - {"main", "menu"}
        self._top_words = sorted(ELEMENT_KEYS)
        self._block_words = sorted(PROPERTY_KEYS | ACTION_KEYS | VALUE_KEYS | child_elems)
        self._script_words = sorted(SCRIPT_KEYS | VALUE_KEYS)
        self._sym_sig = None
        self._user_syms = {}
        self._active_prefix = ""
        self._cand_top = self._cand_block = self._cand_script = self._cand_vars = []
        self._rebuild_candidates({})

        self._completer = QCompleter(QStandardItemModel(self), self)
        self._completer.setWidget(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # we filter/rank ourselves (fuzzy), so the completer shows the model as-is
        self._completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCompletionRole(self._WORD_ROLE)
        self._completer.setMaxVisibleItems(11)
        from PyQt6.QtWidgets import QListView
        view = QListView()
        view.setUniformItemSizes(True)
        view.setMouseTracking(True)
        view.setStyleSheet(
            "background:#0f151c;color:#d7e0ea;border:1px solid #243140;"
            "outline:0;padding:3px;")
        self._completer.setPopup(view)             # QCompleter sets its own delegate here...
        self._cdelegate = CompletionDelegate(view)
        view.setItemDelegate(self._cdelegate)      # ...so override it AFTER setPopup
        self._completer.activated.connect(self._insert_completion)
        self._completer.highlighted[str].connect(self._on_highlight)
        self._doc = DocFlyout()
        view.installEventFilter(self)              # hide the flyout when popup hides

    # -- candidate data ------------------------------------------------------
    def _builtin_cands(self, words):
        out = []
        for w in words:
            desc, ex = COMPLETION_DOCS.get(w, ("", []))
            out.append({"word": w, "kind": kind_of(w), "desc": desc, "ex": ex})
        return out

    def _user_cands(self, syms):
        out = []
        for name in sorted(syms):
            kind, desc = syms[name]
            ex = [("Use it live", f'text "{{{name}}}" {{ }}')] if kind == "variable" else []
            out.append({"word": name, "kind": kind, "desc": desc, "ex": ex})
        return out

    def _rebuild_candidates(self, syms):
        u = self._user_cands(syms)
        vars_objs = self._user_cands(
            {k: v for k, v in syms.items() if v[0] in ("variable", "object")})
        self._cand_top = self._builtin_cands(self._top_words) + u
        self._cand_block = self._builtin_cands(self._block_words) + u
        self._cand_script = self._builtin_cands(self._script_words) + vars_objs
        self._cand_vars = vars_objs
        self._cand_posteffect_block = self._builtin_cands(sorted(POSTEFFECT_BLOCK_KEYS)) + u
        self._cand_posteffect_fx = self._builtin_cands(sorted(POSTEFFECT_FX_KEYS))

    def _ensure_symbols(self):
        syms = scan_symbols(self.toPlainText())
        sig = tuple(sorted((k, v[0]) for k, v in syms.items()))
        if sig != self._sym_sig:
            self._sym_sig = sig
            self._user_syms = syms
            self._rebuild_candidates(syms)

    # -- context -------------------------------------------------------------
    def _enclosing_word(self):
        """The keyword just before the { that encloses the cursor (setup/update/
        holder/button/*.main...), or None at the top level. Drives which kind of
        suggestions to show."""
        text = self.toPlainText()[:self.textCursor().position()]
        # find the '{' that encloses the cursor (skip strings)
        depth = 0; open_at = -1; instr = False
        j = len(text) - 1
        while j >= 0:
            c = text[j]
            if c == '"':
                instr = not instr
            elif not instr:
                if c == "}":
                    depth += 1
                elif c == "{":
                    if depth == 0:
                        open_at = j; break
                    depth -= 1
            j -= 1
        if open_at < 0:
            return None
        k = open_at - 1
        while True:
            while k >= 0 and text[k] in " \t\r\n":
                k -= 1
            if k >= 0 and text[k] == ")":          # holder (...) {  -> skip (...)
                pd = 0
                while k >= 0:
                    if text[k] == ")":
                        pd += 1
                    elif text[k] == "(":
                        pd -= 1
                        if pd == 0:
                            k -= 1; break
                    k -= 1
                continue
            if k >= 0 and text[k] == '"':          # button "label" {  -> skip "label"
                k -= 1
                while k >= 0 and text[k] != '"':
                    k -= 1
                k -= 1
                continue
            break
        e = k
        while k >= 0 and (text[k].isalnum() or text[k] in "_.*"):
            k -= 1
        return text[k + 1:e + 1] or None

    def _in_interpolation(self):
        line = self.textCursor().block().text()[:self.textCursor().positionInBlock()]
        return line.rfind("{") > line.rfind("}") and line.count('"') % 2 == 1

    def _active_candidates(self):
        if self._in_interpolation() and self._cand_vars:
            return self._cand_vars
        w = self._enclosing_word()
        if w is None:
            return self._cand_top                 # top level -> declarations/screens
        lw = w.lower().lstrip("*.")
        if lw == "effect":
            return self._cand_posteffect_fx        # only postEffects.* is valid in here
        if lw == "posteffect":
            return self._cand_posteffect_block      # post.name/cache/quality, effect
        if lw in ("setup", "update", "if", "else", "snip", "function", "func"):
            return self._cand_script              # engine functions + your vars
        return self._cand_block                   # properties + actions + child elements

    # -- fuzzy match + ranked model -----------------------------------------
    @staticmethod
    def _fuzzy(query, word):
        if not query:
            return 5
        q = query.lower(); w = word.lower()
        if w == q:
            return 1200
        if w.startswith(q):
            return 1000 - len(w)
        i = w.find(q)
        if i >= 0:
            return 760 - i - len(w)
        it = iter(w)                      # subsequence match
        if all(ch in it for ch in q):
            return 420 - len(w)
        return None

    def _make_model(self, cands, query):
        scored = []
        for c in cands:
            s = self._fuzzy(query, c["word"])
            if s is not None:
                scored.append((s, c))
        scored.sort(key=lambda x: (-x[0], x[1]["word"].lower()))
        model = QStandardItemModel(self)
        self._doc_index = {}
        for _s, c in scored[:60]:
            it = QStandardItem()
            it.setEditable(False)
            it.setData(c["word"], WORD_ROLE)
            it.setData(c["word"], Qt.ItemDataRole.DisplayRole)   # used for matching
            it.setData(c["kind"], KIND_ROLE)
            it.setData(c["desc"], DESC_ROLE)
            it.setData(c["ex"], EX_ROLE)
            model.appendRow(it)
            self._doc_index[c["word"]] = c
        return model

    # -- flyout --------------------------------------------------------------
    def _on_highlight(self, word):
        c = getattr(self, "_doc_index", {}).get(word)
        if not c:
            self._doc.hide(); return
        self._doc.show_for(c["word"], c["kind"], c["desc"], c["ex"])
        self._position_doc()

    def _position_doc(self):
        pop = self._completer.popup()
        if not pop.isVisible():
            self._doc.hide(); return
        g = pop.geometry()
        from PyQt6.QtGui import QGuiApplication
        scr = (QGuiApplication.screenAt(g.center()) or self.screen()
               or QGuiApplication.primaryScreen())
        screen = scr.availableGeometry()
        x = g.right() + 6
        if x + self._doc.width() > screen.right():
            x = g.left() - self._doc.width() - 6
        self._doc.move(max(screen.left(), x), g.top())
        self._doc.show()

    def eventFilter(self, obj, ev):
        from PyQt6.QtCore import QEvent
        comp = getattr(self, "_completer", None)
        if comp is not None and obj is comp.popup() and ev.type() == QEvent.Type.Hide:
            self._doc.hide()
        return super().eventFilter(obj, ev)

    # -- insertion -----------------------------------------------------------
    def _text_under_cursor(self):
        tc = self.textCursor(); line = tc.block().text(); col = tc.positionInBlock()
        i = col
        while i > 0 and (line[i - 1].isalnum() or line[i - 1] in "_."):
            i -= 1
        return line[i:col]

    def _insert_completion(self, completion):
        tc = self.textCursor()
        for _ in range(len(self._active_prefix)):
            tc.deletePreviousChar()
        snippet = SNIPPETS.get(completion)
        if snippet:
            before, _, after = snippet.partition("\x00")
            tc.insertText(before); pos = tc.position()
            tc.insertText(after); tc.setPosition(pos)
        else:
            tc.insertText(completion)
        self.setTextCursor(tc)
        self._doc.hide()

    def keyPressEvent(self, e):
        comp = self._completer
        pop = comp.popup()
        if pop.isVisible():
            if e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                idx = pop.currentIndex()
                text = (idx.data(self._WORD_ROLE) if idx.isValid()
                        else comp.currentCompletion())
                if text:
                    self._insert_completion(text)
                pop.hide(); e.accept(); return
            if e.key() == Qt.Key.Key_Escape:
                pop.hide(); self._doc.hide(); e.accept(); return
            # Up/Down navigate the popup (handled by the view)
        super().keyPressEvent(e)
        prefix = self._text_under_cursor()
        force = (e.key() == Qt.Key.Key_Space
                 and (e.modifiers() & Qt.KeyboardModifier.ControlModifier))
        is_delete = e.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
        if force or (len(prefix) >= 1
                      and (e.text().isalnum() or e.text() in "._" or is_delete)):
            self._ensure_symbols()
            self._active_prefix = prefix
            model = self._make_model(self._active_candidates(), prefix)
            comp.setModel(model)
            if model.rowCount() > 0:
                pop.setItemDelegate(self._cdelegate)   # keep our IntelliSense rows
                pop.setCurrentIndex(model.index(0, 0))
                cr = self.cursorRect()
                cr.setWidth(320)
                comp.complete(cr)
                self._on_highlight(model.index(0, 0).data(WORD_ROLE))
            else:
                pop.hide(); self._doc.hide()
        else:
            pop.hide(); self._doc.hide()

    def line_number_width(self):
        digits = max(2, len(str(self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    _SPRITE_GUTTER = 26

    def _update_width(self):
        self.setViewportMargins(self.line_number_width(), 0, self._SPRITE_GUTTER, 0)

    def _get_sprite_btn(self, i):
        from PyQt6.QtWidgets import QPushButton
        while i >= len(self._sprite_btns):
            b = QPushButton(self)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, btn=b: self._sprite_clicked(btn))
            self._sprite_btns.append(b)
        return self._sprite_btns[i]

    def _sprite_clicked(self, btn):
        pos = getattr(btn, "_pos", None)
        if pos is not None and callable(self.sprite_request):
            self.sprite_request(pos)

    def _refresh_sprite_buttons(self):
        try:
            import spritestore
            rev = self.document().revision()
            if rev != self._els_rev:          # only rescan when the text changed
                self._els_cache = {e["line"]: e
                                   for e in spritestore.scan_elements(self.toPlainText())}
                self._els_rev = rev
            els = self._els_cache
        except Exception:
            return
        if self._sprite_icon is None:
            import images
            self._sprite_icon = images.button_icon("edit", 14)
        vp = self.viewport()
        vtop = vp.geometry().top()
        x = self.width() - self._SPRITE_GUTTER + 2
        off = self.contentOffset()
        block = self.firstVisibleBlock()
        used = 0
        while block.isValid():
            geo = self.blockBoundingGeometry(block).translated(off)
            if geo.top() > vp.height():
                break
            ln = block.blockNumber()
            if block.isVisible() and ln in els:
                e = els[ln]
                b = self._get_sprite_btn(used); used += 1
                b._pos = e["pos"]
                from PyQt6.QtCore import QSize as _QSize
                if not self._sprite_icon.isNull():
                    if getattr(b, "_has_icon", False) is False:
                        b.setIcon(self._sprite_icon); b.setIconSize(_QSize(14, 14)); b.setText("")
                        b._has_icon = True
                else:
                    b.setText("\U0001f58c")
                b.setToolTip(f"Edit sprite  \u2013  {e['kind']} \u201c{e['name']}\u201d")
                on = e["has_sprite"]
                if getattr(b, "_on_state", None) != on:   # only restyle on change
                    b._on_state = on
                    b.setStyleSheet(
                        "QPushButton{font-size:11px;border-radius:4px;padding:0;"
                        + ("background:rgba(108,240,154,0.20);border:1px solid #6cf09a;color:#bdf5d6;"
                           if on else
                           "background:rgba(255,255,255,0.06);border:1px solid #2a3a4d;color:#8b95a1;")
                        + "}QPushButton:hover{background:rgba(108,240,154,0.30);border:1px solid #6cf09a;}")
                h = min(20, int(geo.height()) - 2) or 18
                b.setGeometry(x, int(vtop + geo.top()) + 1, self._SPRITE_GUTTER - 4, h)
                b.show()
            block = block.next()
        for j in range(used, len(self._sprite_btns)):
            self._sprite_btns[j].hide()

    def _update_area(self, rect, dy):
        if dy:
            self.line_area.scroll(0, dy)
        else:
            self.line_area.update(0, rect.y(), self.line_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.line_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))
        self._refresh_sprite_buttons()

    def _highlight_current(self):
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(self.theme.get("currentLine", "#161d27")))
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel] + self._problem_sels)

    def set_problem_lines(self, err_lines, warn_lines):
        """Highlight error lines (red) and warning lines (amber) in the gutter area."""
        self._problem_sels = []
        doc = self.document()
        for lines, color in ((warn_lines, QColor(120, 96, 20, 70)),
                             (err_lines, QColor(140, 40, 40, 110))):
            for ln in lines:
                block = doc.findBlockByNumber(int(ln) - 1)
                if not block.isValid():
                    continue
                s = QTextEdit.ExtraSelection()
                s.format.setBackground(color)
                s.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                cur = self.textCursor()
                cur.setPosition(block.position())
                cur.clearSelection()
                s.cursor = cur
                self._problem_sels.append(s)
        self._highlight_current()

    def goto_line(self, ln):
        block = self.document().findBlockByNumber(max(0, int(ln) - 1))
        if block.isValid():
            cur = self.textCursor()
            cur.setPosition(block.position())
            self.setTextCursor(cur)
            self.centerCursor()
            self.setFocus()

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_area)
        painter.fillRect(event.rect(), QColor(self.theme.get("gutterBg", "#0a0e13")))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        gutter_fg = QColor(self.theme.get("gutterFg", "#3d4a57"))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(gutter_fg)
                painter.drawText(0, int(top), self.line_area.width() - 6,
                                 self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            num += 1

    _HEX_RX = QRegularExpression(r"#[0-9A-Fa-f]{3,8}\b")

    def paintEvent(self, event):
        super().paintEvent(event)
        # draw a colour underline beneath each #hex - sits under the code so it
        # never overlaps the text that follows.
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fm = self.fontMetrics()
        block = self.firstVisibleBlock()
        off = self.contentOffset()
        while block.isValid():
            geo = self.blockBoundingGeometry(block).translated(off)
            if geo.top() > self.viewport().height():
                break
            if block.isVisible():
                text = block.text()
                it = self._HEX_RX.globalMatch(text)
                while it.hasNext():
                    m = it.next()
                    col = hex_to_qcolor(m.captured(0))
                    if col is None:
                        continue
                    cur = QTextCursor(block)
                    cur.setPosition(block.position() + m.capturedStart())
                    r = self.cursorRect(cur)
                    w = fm.horizontalAdvance(m.captured(0))
                    bar = QRect(r.left(), r.bottom() - 3, max(8, w), 3)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(col)
                    painter.drawRoundedRect(bar, 1.5, 1.5)
            block = block.next()
        painter.end()


def hex_to_qcolor(hx):
    """Parse #rgb / #rgba / #rrggbb / #rrggbbaa into a QColor (None if invalid)."""
    s = hx.lstrip("#")
    try:
        if len(s) == 3:
            r, g, b = (int(c * 2, 16) for c in s)
            return QColor(r, g, b)
        if len(s) == 4:
            r, g, b, a = (int(c * 2, 16) for c in s)
            return QColor(r, g, b, a)
        if len(s) == 6:
            return QColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        if len(s) == 8:
            return QColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    except ValueError:
        return None
    return None


# ===========================================================================
#  Preview-side stub API (toggling works; navigation just logs)
# ===========================================================================
class PreviewAPI:
    def __init__(self, log_fn, window=None):
        self._log = log_fn
        self.window = window
        self.registry = {}

    def navigate(self, url=""):
        self._log(f"navigate -> {url}")

    def js(self, code=""):
        self._log(f"js -> {code[:60]}")

    def _targets(self, target):
        import re as _r
        return [t for t in _r.split(r"[ ,]+", str(target).strip()) if t]

    def _vis(self, verb, target):
        for t in self._targets(target):
            w = self.registry.get(t)
            if w is None:
                continue
            if verb == "toggle":
                w.setVisible(not w.isVisible())
            elif verb == "show":
                w.setVisible(True)
            else:
                w.setVisible(False)

    def run_set(self, stmt=""):
        if self.window:
            self.window.apply_preview_set(stmt)

    def run_do(self, seq=""):
        changed = False
        for cmd in str(seq).split(";"):
            parts = cmd.split()
            if not parts:
                continue
            verb, args = parts[0].lower(), parts[1:]
            if verb in ("show", "hide", "toggle"):
                self._vis(verb, " ".join(args))
            elif verb == "set" and self.window:
                self.window.apply_preview_set(" ".join(args), rerender=False)
                changed = True
            else:
                self.call(verb)
        if changed and self.window:
            self.window._render()
        return changed

    def call(self, action, **kwargs):
        if action in ("toggle", "show", "hide"):
            self._vis(action, kwargs.get("target", ""))
        else:
            self._log(f"action -> {action} {kwargs if kwargs else ''}")


class ProjectDialog(QDialog):
    """Unity-style hub: create a new project or open an existing one."""

    def __init__(self, projects_root, parent=None):
        super().__init__(parent)
        self.projects_root = projects_root
        self.selected = None
        self.created = False
        self.setWindowTitle("Glass Projects")
        self.setMinimumWidth(440)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#d7e0ea;}"
            "QListWidget{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;}"
            "QLineEdit{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;padding:6px;border-radius:5px;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;}"
            "QPushButton:hover{background:#243248;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)

        head = QLabel("Open a project")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        lay.addWidget(QLabel("Each project is a folder of .glass scripts plus a "
                             "project.json the server reads. getGlass only loads "
                             "scripts from the open project."))

        self.list = QListWidget()
        for name in project.list_projects(projects_root):
            man = project.load_manifest(os.path.join(projects_root, name))
            n = len(man.get("scripts", []))
            self.list.addItem(f"{name}    ({n} script{'s' if n != 1 else ''})")
        self.list.itemDoubleClicked.connect(lambda *_: self._open_selected())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        open_btn = QPushButton("Open selected")
        open_btn.clicked.connect(self._open_selected)
        row.addWidget(open_btn)
        exp_btn = QPushButton("Export as .glasspack")
        exp_btn.clicked.connect(self._export_pack)
        row.addWidget(exp_btn)
        imp_btn = QPushButton("Import .glasspack\u2026")
        imp_btn.clicked.connect(self._import_pack)
        row.addWidget(imp_btn)
        row.addStretch(1)
        lay.addLayout(row)

        sep = QLabel("Or create a new project")
        sep.setStyleSheet("color:#8aa0b3;margin-top:8px;")
        lay.addWidget(sep)
        nrow = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("New project name")
        self.name_edit.returnPressed.connect(self._create)
        nrow.addWidget(self.name_edit, 1)
        create_btn = QPushButton("Create")
        create_btn.clicked.connect(self._create)
        nrow.addWidget(create_btn)
        lay.addLayout(nrow)

    def _names(self):
        return project.list_projects(self.projects_root)

    def _open_selected(self):
        idx = self.list.currentRow()
        names = self._names()
        if 0 <= idx < len(names):
            self.selected = os.path.join(self.projects_root, names[idx])
            self.accept()

    def _refresh_list(self):
        self.list.clear()
        for name in project.list_projects(self.projects_root):
            man = project.load_manifest(os.path.join(self.projects_root, name))
            n = len(man.get("scripts", []))
            self.list.addItem(f"{name}    ({n} script{'s' if n != 1 else ''})")

    def _export_pack(self):
        idx = self.list.currentRow()
        names = self._names()
        if not (0 <= idx < len(names)):
            QMessageBox.information(self, "Glass", "Select a project to export first.")
            return
        src = os.path.join(self.projects_root, names[idx])
        out, _ = QFileDialog.getSaveFileName(
            self, "Export package", names[idx] + ".glasspack",
            "Glass package (*.glasspack)")
        if not out:
            return
        try:
            path = glasspack.export_pack(src, out)
            QMessageBox.information(self, "Glass", f"Exported to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Glass", f"Export failed: {e}")

    def _import_pack(self):
        pack, _ = QFileDialog.getOpenFileName(
            self, "Import package", "", "Glass package (*.glasspack *.zip)")
        if not pack:
            return
        name = glasspack.pack_project_name(pack) or os.path.basename(pack)
        if os.path.exists(os.path.join(self.projects_root, name)):
            if QMessageBox.question(
                    self, "Glass",
                    f"'{name}' already exists. Overwrite it?") != QMessageBox.StandardButton.Yes:
                return
        ok = QMessageBox.question(
            self, "Glass",
            "Packages can contain Python code that runs on your computer.\n"
            "Only import packages you trust.\n\nImport this package?")
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = glasspack.install_pack(pack, self.projects_root)
            self._refresh_list()
            QMessageBox.information(self, "Glass",
                                   f"Imported '{os.path.basename(dest)}'.")
        except Exception as e:
            QMessageBox.warning(self, "Glass", f"Import failed: {e}")

    def _create(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Glass", "Type a project name first.")
            return
        self.selected = project.create_project(self.projects_root, name)
        self.created = True
        self.accept()


def make_starter(name, bg, menutype):
    """Build a starter .glass from the new-script menu choices."""
    nm = (name or "main").strip()
    safe = "".join(c for c in nm if c.isalnum() or c in " _-").strip() or "main"
    bg = (bg or "#0d1117").strip()
    menutype = menutype or "menu.full"
    return (
        f"*.main {{\n"
        f"    {menutype}\n"
        f"    background: {bg}\n\n"
        f"    title: \"{safe}\" {{ center: center, color: #f0f6fc }}\n\n"
        f"    setup {{\n\n    }}\n\n"
        f"    update {{\n\n    }}\n"
        f"}}\n"
    )


class NewScriptDialog(QDialog):
    """A little start menu: name, background colour, and menu type."""

    MENU_TYPES = [
        ("Full screen  (menu.full)", "menu.full"),
        ("Camera / game  (menu.dynamic)", "menu.dynamic"),
        ("Floating panel  (menu.ui)", "menu.ui"),
    ]

    def __init__(self, parent=None, default_name=""):
        super().__init__(parent)
        self.setWindowTitle("New Glass script")
        self.setMinimumWidth(420)
        self._bg = "#0d1117"
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#d7e0ea;}"
            "QLineEdit{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;padding:6px;border-radius:5px;}"
            "QComboBox{background:#11161c;color:#d7e0ea;border:1px solid #1c2530;padding:6px;border-radius:5px;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:6px;padding:7px 12px;}"
            "QPushButton:hover{background:#243248;}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        head = QLabel("Create a new script")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)

        lay.addWidget(QLabel("Script name"))
        self.name_edit = QLineEdit()
        self.name_edit.setText(default_name)
        self.name_edit.setPlaceholderText("e.g. home")
        self.name_edit.returnPressed.connect(self._create)
        lay.addWidget(self.name_edit)

        lay.addWidget(QLabel("Menu type"))
        self.type_box = QComboBox()
        for label, _ in self.MENU_TYPES:
            self.type_box.addItem(label)
        lay.addWidget(self.type_box)

        lay.addWidget(QLabel("Background colour"))
        crow = QHBoxLayout()
        self.swatch = QLabel()
        self.swatch.setFixedSize(28, 28)
        self._paint_swatch()
        self.hex_edit = QLineEdit(self._bg)
        self.hex_edit.textChanged.connect(self._hex_typed)
        pick = QPushButton("Pick\u2026")
        pick.clicked.connect(self._pick_colour)
        crow.addWidget(self.swatch)
        crow.addWidget(self.hex_edit, 1)
        crow.addWidget(pick)
        lay.addLayout(crow)

        lay.addSpacing(4)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create")
        create.setStyleSheet("QPushButton{background:#1e7e4f;border-color:#2a9c63;}"
                             "QPushButton:hover{background:#249160;}")
        create.clicked.connect(self._create)
        brow.addWidget(cancel)
        brow.addWidget(create)
        lay.addLayout(brow)

        self.values = None
        self.name_edit.setFocus()

    def _paint_swatch(self):
        self.swatch.setStyleSheet(
            f"background:{self._bg};border:1px solid #28384a;border-radius:4px;")

    def _hex_typed(self, txt):
        t = txt.strip()
        if t.startswith("#") and len(t) in (4, 7):
            self._bg = t
            self._paint_swatch()

    def _pick_colour(self):
        from PyQt6.QtGui import QColor
        col = QColorDialog.getColor(QColor(self._bg), self, "Background colour")
        if col.isValid():
            self._bg = col.name()
            self.hex_edit.setText(self._bg)
            self._paint_swatch()

    def _create(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Glass", "Give the script a name first.")
            return
        menutype = self.MENU_TYPES[self.type_box.currentIndex()][1]
        self.values = (name, self._bg, menutype)
        self.accept()


# ===========================================================================
#  Main editor window
# ===========================================================================
STARTER = '''// Welcome to the Glass editor. Edit on the left, watch the right.
// Press the Tutorial button (top right) any time to learn the language.

*.menu ( menu.moveable menu.closable menu.remember ) {
    title: My Menu
    background: #161b22

    textgroup { Heading, timesnewroman }

    text "Hello, Glass" { color: #6cf09a, font: Heading }

    button "Reload page" { action: reload }
    button { { color: #1e88e5, width: 160, height: 32 } "Open Page 2" } { action: toggle, target: page2 }

    holder ( autosize, size: 220, outline: true, outlinecolor: #6cf09a, outlineThickness: 2, hidden: true ) {
        name: page2
        title: Page Two
        text "You toggled me on!" { color: #ddd }
        button { { color: #e53935 } "Close" } { action: toggle, target: page2 }
    }
}
'''


class AssistantDialog(QDialog):
    """Local Glass coding assistant - ask how to code things, get an answer +
    example. Runs fully offline (no cloud); it searches a built-in knowledge
    base of the Glass language and wiki."""
    def __init__(self, editor_window):
        super().__init__(editor_window)
        import glassai
        from PyQt6.QtWidgets import QTextBrowser, QLineEdit, QPushButton
        self._ew = editor_window
        self._ai = glassai
        self._last_code = ""
        self.setWindowTitle("Ask AI  -  Glass coding assistant")
        self.setMinimumSize(560, 560)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QTextBrowser{background:#0a0e13;color:#d7e0ea;border:1px solid #1c2530;"
            "border-radius:8px;padding:6px;}"
            "QLineEdit{background:#11161c;color:#eaf2fb;border:1px solid #232a31;"
            "border-radius:8px;padding:8px 10px;}"
            "QPushButton{background:#1a2330;color:#e6eef7;border:1px solid #28384a;"
            "border-radius:7px;padding:7px 12px;}QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 14, 14, 14); lay.setSpacing(9)
        head = QLabel("Glass coding assistant")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        sub = QLabel("Ask how something works, or say \u201cmake a\u2026\u201d and I\u2019ll write "
                     "the Glass code. Runs offline \u2013 nothing leaves your PC.")
        sub.setStyleSheet("color:#8b95a1;")
        lay.addWidget(sub)

        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.anchorClicked.connect(self._on_anchor)
        lay.addWidget(self.log, 1)

        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("e.g. make a game where the player moves, or: what does adjvcr do?")
        self.entry.returnPressed.connect(self._send)
        row.addWidget(self.entry, 1)
        send = QPushButton("Ask"); send.clicked.connect(self._send); row.addWidget(send)
        lay.addLayout(row)

        row2 = QHBoxLayout(); row2.addStretch(1)
        self.insert_btn = QPushButton("Insert last example into editor")
        self.insert_btn.setEnabled(False)
        self.insert_btn.clicked.connect(self._insert_code)
        row2.addWidget(self.insert_btn)
        lay.addLayout(row2)

        self._greet()

    # -- helpers -------------------------------------------------------------
    def _esc(self, s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _greet(self):
        chips = "".join(
            f'<a href="ask:{self._esc(t)}" style="color:#6cf09a;text-decoration:none;">'
            f'&nbsp;{self._esc(t)}&nbsp;</a> &middot; '
            for _id, t in self._ai.topics()[:8])
        self.log.setHtml(
            '<div style="color:#8b95a1;line-height:1.5;">Hi! I know the Glass '
            'language and wiki. Ask me anything, or start with a topic:<br><br>'
            + chips + '</div>')

    def _append(self, html):
        self.log.append(html)
        sb = self.log.verticalScrollBar(); sb.setValue(sb.maximum())

    def _send(self):
        q = self.entry.text().strip()
        if not q:
            return
        self.entry.clear()
        self._ask(q)

    def _on_anchor(self, url):
        s = url.toString()
        if s.startswith("ask:"):
            self._ask(s[4:])

    def _ask(self, q):
        self._append(
            f'<div style="margin:8px 0 2px 0;"><span style="color:#7fb2ff;font-weight:600;">'
            f'You:</span> {self._esc(q)}</div>')
        r = self._ai.answer(q)
        parts = [f'<div style="color:#6cf09a;font-weight:600;margin-top:4px;">{self._esc(r["title"])}</div>',
                 f'<div style="color:#d7e0ea;line-height:1.5;">{self._esc(r["body"])}</div>']
        if r.get("code"):
            self._last_code = r["code"]
            self.insert_btn.setEnabled(True)
            parts.append(
                '<pre style="background:#11161c;border:1px solid #1c2530;border-radius:6px;'
                'padding:8px;color:#bfe3c9;white-space:pre-wrap;">'
                + self._esc(r["code"]) + '</pre>')
        if r.get("suggestions"):
            chips = " &middot; ".join(
                f'<a href="ask:{self._esc(t)}" style="color:#6cf09a;text-decoration:none;">'
                f'{self._esc(t)}</a>' for t in r["suggestions"])
            parts.append(f'<div style="color:#8b95a1;margin:4px 0 10px 0;">See also: {chips}</div>')
        self._append("".join(parts))

    def _insert_code(self):
        if self._last_code and hasattr(self._ew, "editor"):
            cur = self._ew.editor.textCursor()
            cur.insertText(self._last_code + "\n")
            self._ew.editor.setFocus()


class ProblemsPanel(QTabWidget):
    """The editor's Debug log: three sectors - Errors, Warnings, Debug.
    Errors/warnings point at the line with a probable cause + fix; click to jump."""
    def __init__(self, on_jump=None):
        super().__init__()
        self._on_jump = on_jump
        self.err = QListWidget()
        self.warn = QListWidget()
        self.dbg = QListWidget()
        for lst in (self.err, self.warn, self.dbg):
            lst.itemClicked.connect(self._clicked)
            lst.setStyleSheet(
                "QListWidget{background:#0a0e13;color:#cdd6df;border:none;}"
                "QListWidget::item{padding:4px 8px;border-bottom:1px solid #141b24;}"
                "QListWidget::item:hover{background:#141d27;}")
        self.addTab(self.err, "Errors")
        self.addTab(self.warn, "Warnings")
        self.addTab(self.dbg, "Debug")
        self.setStyleSheet(
            "QTabWidget::pane{border-top:1px solid #1c2530;}"
            "QTabBar::tab{background:#0d1117;color:#8b95a1;padding:5px 12px;}"
            "QTabBar::tab:selected{background:#141d27;color:#e7edf3;}")

    def _clicked(self, item):
        ln = item.data(Qt.ItemDataRole.UserRole)
        if ln and self._on_jump:
            self._on_jump(ln)

    def apply_theme(self, panel_bg, header_bg, fg, sub, border):
        """Recolour the lists/tabs to match the active .glasstheme. Previously
        these were hardcoded and never changed with the theme, so the Debug
        log always looked like a near-duplicate of the code editor regardless
        of which theme (or which other panel) was active."""
        for lst in (self.err, self.warn, self.dbg):
            lst.setStyleSheet(
                f"QListWidget{{background:{panel_bg};color:{fg};border:none;}}"
                f"QListWidget::item{{padding:4px 8px;border-bottom:1px solid {border};}}"
                f"QListWidget::item:hover{{background:{header_bg};}}")
        self.setStyleSheet(
            f"QTabWidget::pane{{border-top:1px solid {border};}}"
            f"QTabBar::tab{{background:{panel_bg};color:{sub};padding:5px 12px;}}"
            f"QTabBar::tab:selected{{background:{header_bg};color:{fg};}}")

    def update_items(self, res):
        self.err.clear(); self.warn.clear(); self.dbg.clear()
        for e in res.get("errors", []):
            it = QListWidgetItem(f"\u26d4  L{e['line']}: {e['msg']}   \u2192  {e['hint']}")
            it.setData(Qt.ItemDataRole.UserRole, e["line"])
            it.setForeground(QColor("#ff8a8a"))
            self.err.addItem(it)
        for w in res.get("warnings", []):
            it = QListWidgetItem(f"\u26a0  L{w['line']}: {w['msg']}   \u2192  {w['hint']}")
            it.setData(Qt.ItemDataRole.UserRole, w["line"])
            it.setForeground(QColor("#e5c07b"))
            self.warn.addItem(it)
        for d in res.get("debug", []):
            self.dbg.addItem(QListWidgetItem("\u2022  " + d["msg"]))
        self.setTabText(0, f"Errors ({self.err.count()})")
        self.setTabText(1, f"Warnings ({self.warn.count()})")
        self.setTabText(2, f"Debug ({self.dbg.count()})")
        if self.err.count():                     # surface errors first
            self.setCurrentIndex(0)


_THEME_SAMPLE = '''>> a Glass theme preview <<
int Score = 0
string Prompt = "hello"

snip "Add" ( int n ) {
    Score = Score + n
    return { returnType: int, value: Score }
}

*.main {
    menu.full
    background: #0d1117
    text "Score: {Score}" { center: center, color: #6cf09a }
    button "Go" { action: reload, width: 200 }
    update {
        if (input.getClick("space") == "1") { Add(10) }
    }
}'''


class ThemeCreatorDialog(QDialog):
    """Build/edit a .glasstheme: pick a colour per role with a live preview."""
    def __init__(self, parent, base_theme, source_path=None):
        super().__init__(parent)
        import glasstheme
        self.setWindowTitle("Theme creator")
        self.resize(820, 580)
        self.parent_win = parent
        self.theme = glasstheme._norm(base_theme)
        # the file this theme was loaded from, if it's an existing custom theme -
        # None means "built-in" or "not saved yet". Lets Save rename/overwrite
        # in place (instead of always creating a new file) and lets Delete work.
        self._source_path = source_path

        lay = QHBoxLayout(self)
        # left: colour rows
        left = QWidget(); lcol = QVBoxLayout(left); lcol.setSpacing(4)
        nrow = QHBoxLayout(); nrow.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(self.theme.get("name", "My Theme"))
        nrow.addWidget(self.name_edit); lcol.addLayout(nrow)
        self.swatches = {}
        for role, label in glasstheme.ROLES:
            row = QHBoxLayout()
            lb = QLabel(label); lb.setMinimumWidth(240); lb.setStyleSheet("color:#c7d2dc;")
            btn = QPushButton(); btn.setFixedSize(52, 22)
            btn.clicked.connect(lambda _c, r=role: self._pick(r))
            self.swatches[role] = btn
            row.addWidget(lb); row.addWidget(btn); row.addStretch(1)
            lcol.addLayout(row)
        lcol.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(left)
        scroll.setStyleSheet("border:none;")
        lay.addWidget(scroll, 1)

        # right: live preview + actions
        right = QWidget(); rcol = QVBoxLayout(right)
        rcol.addWidget(QLabel("Preview"))
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        self.preview.setPlainText(_THEME_SAMPLE)
        f = QFont("Consolas"); f.setStyleHint(QFont.StyleHint.Monospace); f.setPointSize(10)
        self.preview.setFont(f)
        self._hl = GlassHighlighter(self.preview.document(), self.theme)
        rcol.addWidget(self.preview, 1)
        btns = QHBoxLayout()
        for text, slot in (("Apply", self._apply), ("Save", self._save),
                           ("Save As\u2026", self._save_as), ("Delete", self._delete),
                           ("Close", self.accept)):
            b = QPushButton(text); b.clicked.connect(slot); btns.addWidget(b)
        rcol.addLayout(btns)
        lay.addWidget(right, 1)
        self._refresh()

    def _refresh(self):
        for role, btn in self.swatches.items():
            c = self.theme.get(role, "#000000")
            btn.setStyleSheet(f"background:{c};border:1px solid #666;border-radius:3px;")
        self.preview.setStyleSheet(
            f"background:{self.theme['background']};color:{self.theme['foreground']};"
            f"border:1px solid #243140;")
        self._hl.apply_theme(self.theme)

    def _pick(self, role):
        c = QColorDialog.getColor(QColor(self.theme.get(role, "#000000")), self, "Pick colour")
        if c.isValid():
            self.theme[role] = c.name()
            self._refresh()

    def _validated_name(self):
        """Sanitize + reflect the cleaned-up name back into the field, so what
        you see is exactly what will be used as the filename."""
        import glasstheme
        name = glasstheme.sanitize_name(self.name_edit.text())
        self.name_edit.setText(name)
        return name

    def _warn_if_builtin_collision(self, name):
        import glasstheme
        if glasstheme.name_collides_with_builtin(name):
            QMessageBox.warning(self, "Theme name taken",
                f'"{name}" is a built-in theme name. Pick a different name for '
                f"your theme, or it'll always lose to the built-in one.")
            return True
        return False

    def _apply(self):
        self.theme["name"] = self.name_edit.text().strip() or "My Theme"
        if self.parent_win:
            self.parent_win.editor.apply_theme(self.theme)
            self.parent_win.apply_chrome_theme(self.theme)   # recolour the whole
                                                               # window, not just the
                                                               # code view

    def _save(self):
        import glasstheme
        name = self._validated_name()
        if self._warn_if_builtin_collision(name):
            return
        self.theme["name"] = name
        new_path = os.path.join(glasstheme.THEME_DIR, name + ".glasstheme")
        try:
            glasstheme.save_file(new_path, self.theme)
            if self._source_path and os.path.abspath(self._source_path) != os.path.abspath(new_path):
                glasstheme.delete_file(self._source_path)   # renamed -> drop the
        except OSError as e:                                # old file instead of
            QMessageBox.warning(self, "Theme", f"Couldn't save that theme:\n{e}")   # leaving an orphan
            return
        self._source_path = new_path
        self._apply()
        if self.parent_win:
            self.parent_win._reload_theme_list()
            self.parent_win._apply_theme_ref(name)
        QMessageBox.information(self, "Theme", "Saved to your themes.")

    def _save_as(self):
        import glasstheme
        name = self._validated_name()
        if self._warn_if_builtin_collision(name):
            return
        self.theme["name"] = name
        p, _ = QFileDialog.getSaveFileName(self, "Save .glasstheme",
                                           name + ".glasstheme",
                                           "Glass theme (*.glasstheme)")
        if not p:
            return
        try:
            glasstheme.save_file(p, self.theme)
        except OSError as e:
            QMessageBox.warning(self, "Theme", f"Couldn't save that theme:\n{e}")
            return
        # Save As always writes a separate new file - unlike Save, it never
        # touches/removes whatever theme this dialog was originally opened from.
        self._apply()
        if self.parent_win:
            self.parent_win._reload_theme_list()

    def _delete(self):
        import glasstheme
        if not self._source_path:
            QMessageBox.information(self, "Theme",
                "This theme hasn't been saved yet, so there's nothing to delete.")
            return
        name = self.theme.get("name", "this theme")
        if QMessageBox.question(
                self, "Delete theme", f'Delete "{name}" permanently?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return
        glasstheme.delete_file(self._source_path)
        self._source_path = None
        if self.parent_win:
            self.parent_win._reload_theme_list()
            self.parent_win._apply_theme_ref("Frosted Dark")   # fall back to default
        self.accept()


class LightingDialog(QDialog):
    """The Baking tab: baked-lighting quality/shadows, baked audio-reverb
    acoustics, and quick references for both."""
    _QUALITIES = [
        ("Draft", "draft", "Fastest preview. Very chunky, no smoothing."),
        ("Low", "low", "Quick. Chunky floors, rough shadows."),
        ("Medium", "medium", "Balanced - smooth, good for most levels."),
        ("High", "high", "Sharp floors and crisp shadow edges."),
        ("Ultra", "ultra", "Very high detail. Slower to bake."),
        ("Extreme", "extreme", "Maximum detail. Slowest - use for final bakes."),
    ]

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowTitle("Baking")
        self.setMinimumWidth(420)
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#c9d1d9;}"
            "QComboBox{background:#161b22;color:#d7e0ea;border:1px solid #243140;"
            "border-radius:4px;padding:5px 8px;}"
            "QCheckBox{color:#c9d1d9;}"
            "QPushButton{background:#1f6feb;color:#fff;border:none;border-radius:5px;"
            "padding:7px 16px;}"
            "QPushButton#ghost{background:#161b22;color:#c9d1d9;border:1px solid #243140;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        title = QLabel("Baked Lighting")
        title.setStyleSheet("color:#6cf09a;font-size:17px;font-weight:600;")
        lay.addWidget(title)
        blurb = QLabel("Lights in a raycast level are baked once - distance falloff, "
                       "wall shadows, and a tint picked up from nearby materials are "
                       "all pre-computed into the floors, walls and colours, so they "
                       "cost nothing while playing.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color:#8b949e;")
        lay.addWidget(blurb)

        # quality
        lay.addWidget(self._label("Quality"))
        self.quality = QComboBox()
        for name, _key, _desc in self._QUALITIES:
            self.quality.addItem(name)
        cur = str(getattr(renderer, "LIGHT_QUALITY", "medium")).lower()
        idx = next((i for i, q in enumerate(self._QUALITIES) if q[1] == cur), 1)
        self.quality.setCurrentIndex(idx)
        self.quality.currentIndexChanged.connect(self._on_quality)
        lay.addWidget(self.quality)
        self.qdesc = QLabel(self._QUALITIES[idx][2])
        self.qdesc.setStyleSheet("color:#8b949e;font-style:italic;")
        lay.addWidget(self.qdesc)

        # shadows
        self.shadows = QCheckBox("Cast wall shadows")
        self.shadows.setChecked(bool(getattr(renderer, "LIGHT_SHADOWS", True)))
        self.shadows.setToolTip("Off = lights shine through walls (flatter, faster to bake).")
        lay.addWidget(self.shadows)

        # shadow resolution (raycasted, pixel-perfect)
        lay.addWidget(self._label("Shadow resolution"))
        self.res = QComboBox()
        self._RES = [("512", 512), ("1024 (HD)", 1024), ("2048 (2K)", 2048),
                     ("4096 (4K)", 4096)]
        for name, _v in self._RES:
            self.res.addItem(name)
        curres = int(getattr(renderer, "SHADOW_RESOLUTION", 1024))
        ridx = next((i for i, rv in enumerate(self._RES) if rv[1] == curres), 1)
        self.res.setCurrentIndex(ridx)
        lay.addWidget(self.res)
        rnote = QLabel("Shadows are raycasted per pixel, so edges are pixel-perfect. "
                       "Higher = sharper but slower to bake (4K is for final bakes). "
                       "The live view is capped for speed; exports use the full size.")
        rnote.setWordWrap(True)
        rnote.setStyleSheet("color:#8b949e;font-style:italic;")
        lay.addWidget(rnote)

        hint = QLabel("Tip: add lights inside a raycast block -\n"
                      "    light { x: 200, y: 160, color: #ff9a3c, radius: 5,\n"
                      "            intensity: 1.4, shadowCaster: true }\n"
                      "    ambient: 0.3")
        hint.setStyleSheet("color:#7d8590;font-family:monospace;background:#161b22;"
                           "border:1px solid #21262d;border-radius:6px;padding:8px;")
        lay.addWidget(hint)

        # --- bake lightmaps to PNG -----------------------------------------
        bake_row = QHBoxLayout()
        bake_btn = QPushButton("Bake lightmaps")
        bake_btn.setObjectName("primary")
        bake_btn.setToolTip("Bake every lit maze in the current scene to a "
                            "lightmap PNG (lightmap_<mazeID>.png in the project).")
        bake_btn.clicked.connect(self._bake)
        bake_row.addWidget(bake_btn)
        self.bake_status = QLabel("")
        self.bake_status.setStyleSheet("color:#8b949e;")
        self.bake_status.setWordWrap(True)
        bake_row.addWidget(self.bake_status, 1)
        lay.addLayout(bake_row)

        sep = QLabel(); sep.setFixedHeight(1)
        sep.setStyleSheet("background:#21262d;")
        lay.addWidget(sep)

        atitle = QLabel("Audio Reverb")
        atitle.setStyleSheet("color:#6cf09a;font-size:17px;font-weight:600;")
        lay.addWidget(atitle)
        ablurb = QLabel("audio.playSound(..., 3d: true) picks up a simple echo "
                        "that gets stronger in tighter, more enclosed rooms - not "
                        "real acoustic simulation, just an honest approximation "
                        "(linear stereo pan + a decaying multi-tap echo). "
                        "realtimeRef: true measures the room live each time instead "
                        "of using the baked estimate below.")
        ablurb.setWordWrap(True)
        ablurb.setStyleSheet("color:#8b949e;")
        lay.addWidget(ablurb)

        abake_row = QHBoxLayout()
        abake_btn = QPushButton("Bake audio acoustics")
        abake_btn.setObjectName("primary")
        abake_btn.setToolTip("Pre-measure every room in the current scene, so the "
                             "first 3D sound played anywhere doesn't pay for a live "
                             "measurement - same idea as baking lightmaps.")
        abake_btn.clicked.connect(self._bake_audio)
        abake_row.addWidget(abake_btn)
        self.audio_bake_status = QLabel("")
        self.audio_bake_status.setStyleSheet("color:#8b949e;")
        self.audio_bake_status.setWordWrap(True)
        abake_row.addWidget(self.audio_bake_status, 1)
        lay.addLayout(abake_row)

        ahint = QLabel("Tip: on a sound - \n"
                       "    audio.playSound \"boom.mp3\" { volume: 1,\n"
                       "        radius: 10, 3d: true, realtimeRef: false }")
        ahint.setStyleSheet("color:#7d8590;font-family:monospace;background:#161b22;"
                           "border:1px solid #21262d;border-radius:6px;padding:8px;")
        lay.addWidget(ahint)

        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close"); close.setObjectName("ghost")
        close.clicked.connect(self.accept)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply)
        row.addWidget(close); row.addWidget(apply_btn)
        lay.addLayout(row)

    def _label(self, t):
        l = QLabel(t)
        l.setStyleSheet("color:#c9d1d9;font-weight:600;")
        return l

    def _on_quality(self, i):
        if 0 <= i < len(self._QUALITIES):
            self.qdesc.setText(self._QUALITIES[i][2])

    def _apply(self):
        i = self.quality.currentIndex()
        key = self._QUALITIES[i][1] if 0 <= i < len(self._QUALITIES) else "medium"
        ri = self.res.currentIndex()
        resolution = self._RES[ri][1] if 0 <= ri < len(self._RES) else 1024
        try:
            self.editor.apply_lighting(key, self.shadows.isChecked(), resolution)
        except Exception:
            pass

    def _bake(self):
        self._apply()                          # bake at the chosen quality/shadows
        try:
            baked = self.editor.bake_lightmaps()
        except Exception as e:
            self.bake_status.setText("Bake failed: %s" % e)
            return
        if baked:
            names = ", ".join(os.path.basename(p) for p in baked)
            t = getattr(self.editor, "_last_bake_time", None)
            tstr = (" in %.1fs" % t) if t else ""
            self.bake_status.setText("Baked %d lightmap%s%s: %s"
                                     % (len(baked), "" if len(baked) == 1 else "s", tstr, names))
        else:
            self.bake_status.setText("No lit mazes found. Add light { } to a raycast, "
                                     "then Update (F5) and bake.")

    def _bake_audio(self):
        try:
            n = self.editor.bake_audio_acoustics()
        except Exception as e:
            self.audio_bake_status.setText("Bake failed: %s" % e)
            return
        if n:
            t = getattr(self.editor, "_last_audio_bake_time", None)
            tstr = (" in %.1fs" % t) if t else ""
            paths = getattr(self.editor, "_last_audio_bake_paths", [])
            saved = f" Saved to {len(paths)} file(s)." if paths else " (not saved to disk - check the project folder is writable)"
            self.audio_bake_status.setText("Measured %d cell%s%s.%s" %
                                           (n, "" if n == 1 else "s", tstr, saved))
        else:
            self.audio_bake_status.setText("No raycast mazes found in the current scene.")


class AnimatorDialog(QDialog):
    """A 2D sprite animator: load frames, preview the loop, export an animated GIF
    (for engines like Flowlab). Uses the dependency-free gifwriter."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowTitle("Sprite Animator")
        self.setMinimumSize(560, 420)
        self.frames = []                    # list of QImage
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#c9d1d9;}"
            "QListWidget{background:#161b22;color:#d7e0ea;border:1px solid #243140;"
            "border-radius:5px;}"
            "QSpinBox{background:#161b22;color:#d7e0ea;border:1px solid #243140;"
            "border-radius:4px;padding:3px 6px;}"
            "QPushButton{background:#161b22;color:#c9d1d9;border:1px solid #243140;"
            "border-radius:5px;padding:6px 12px;}"
            "QPushButton#primary{background:#1f6feb;color:#fff;border:none;}")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        title = QLabel("Sprite Animator")
        title.setStyleSheet("color:#6cf09a;font-size:17px;font-weight:600;")
        root.addWidget(title)

        body = QHBoxLayout()
        root.addLayout(body, 1)

        # left: frame list + buttons
        left = QVBoxLayout()
        body.addLayout(left, 1)
        left.addWidget(QLabel("Frames (in order)"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_select)
        left.addWidget(self.list, 1)
        brow = QHBoxLayout()
        for label, fn in (("Add\u2026", self._add), ("Remove", self._remove),
                          ("\u2191", self._up), ("\u2193", self._down)):
            b = QPushButton(label); b.clicked.connect(fn); brow.addWidget(b)
        left.addLayout(brow)

        # right: preview + controls
        right = QVBoxLayout()
        body.addLayout(right, 1)
        right.addWidget(QLabel("Preview"))
        self.preview = QLabel()
        self.preview.setMinimumSize(220, 220)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(
            "background:#0a0e14;border:1px solid #21262d;border-radius:6px;")
        right.addWidget(self.preview, 1)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("FPS"))
        self.fps = QSpinBox(); self.fps.setRange(1, 60); self.fps.setValue(8)
        self.fps.valueChanged.connect(self._retime)
        fps_row.addWidget(self.fps)
        self.play_btn = QPushButton("Play"); self.play_btn.clicked.connect(self._toggle)
        fps_row.addWidget(self.play_btn)
        fps_row.addStretch(1)
        right.addLayout(fps_row)

        # bottom: export
        exp = QHBoxLayout()
        self.info = QLabel("0 frames")
        self.info.setStyleSheet("color:#8b949e;")
        exp.addWidget(self.info)
        exp.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        exp.addWidget(close)
        save = QPushButton("Export GIF\u2026"); save.setObjectName("primary")
        save.clicked.connect(self._export)
        exp.addWidget(save)
        root.addLayout(exp)

        from PyQt6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._play_i = 0

    # ---- frame management ---------------------------------------------
    def _add(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add frames", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
        from PyQt6.QtGui import QImage
        added = 0
        for p in paths:
            img = QImage(p)
            if img.isNull():
                continue
            img = img.convertToFormat(QImage.Format.Format_RGBA8888)
            self.frames.append(img)
            self.list.addItem(os.path.basename(p))
            added += 1
        if added and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self._refresh()

    def _remove(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.frames):
            self.frames.pop(r)
            self.list.takeItem(r)
            self._refresh()

    def _up(self):
        r = self.list.currentRow()
        if r > 0:
            self.frames[r - 1], self.frames[r] = self.frames[r], self.frames[r - 1]
            it = self.list.takeItem(r); self.list.insertItem(r - 1, it)
            self.list.setCurrentRow(r - 1)

    def _down(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.frames) - 1:
            self.frames[r + 1], self.frames[r] = self.frames[r], self.frames[r + 1]
            it = self.list.takeItem(r); self.list.insertItem(r + 1, it)
            self.list.setCurrentRow(r + 1)

    def _refresh(self):
        self.info.setText("%d frame%s" % (len(self.frames),
                                          "" if len(self.frames) == 1 else "s"))
        if not self.frames and self._timer.isActive():
            self._toggle()

    def _on_select(self, r):
        if 0 <= r < len(self.frames) and not self._timer.isActive():
            self._show(self.frames[r])

    def _show(self, img):
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtCore import QSize
        pm = QPixmap.fromImage(img).scaled(
            QSize(200, 200), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.preview.setPixmap(pm)

    # ---- playback -----------------------------------------------------
    def _toggle(self):
        if self._timer.isActive():
            self._timer.stop(); self.play_btn.setText("Play")
        elif self.frames:
            self._play_i = 0
            self._timer.start(int(1000 / max(1, self.fps.value())))
            self.play_btn.setText("Stop")

    def _retime(self):
        if self._timer.isActive():
            self._timer.start(int(1000 / max(1, self.fps.value())))

    def _tick(self):
        if not self.frames:
            return
        self._show(self.frames[self._play_i % len(self.frames)])
        self._play_i += 1

    # ---- export -------------------------------------------------------
    def _export(self):
        if not self.frames:
            self.info.setText("Add some frames first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export animated GIF",
                                              "animation.gif", "GIF (*.gif)")
        if not path:
            return
        if not path.lower().endswith(".gif"):
            path += ".gif"
        try:
            import gifwriter
            from PyQt6.QtGui import QImage
            delay = int(1000 / max(1, self.fps.value()))
            packed = []
            for img in self.frames:
                im = img.convertToFormat(QImage.Format.Format_RGBA8888) \
                    if img.format() != QImage.Format.Format_RGBA8888 else img
                w, h = im.width(), im.height()
                ptr = im.constBits(); ptr.setsize(w * h * 4)
                packed.append((w, h, bytes(ptr)))
            gifwriter.write_gif(path, packed, delay, loop=0)
            self.info.setText("Exported %d frames -> %s" % (len(self.frames),
                                                            os.path.basename(path)))
        except Exception as e:
            self.info.setText("Export failed: %s" % e)


class MeshViewDialog(QDialog):
    """A Unity-style 3D scene view. Import an OBJ mesh (e.g. from ProBuilder),
    inspect it in 3D (orbit / pan / zoom), see the materials and the flattened
    grid it will raycast as, and copy a ready-made .glass snippet."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowTitle("3D Mesh View")
        self.setMinimumSize(720, 520)
        self.md = None
        self.path = None
        self.setStyleSheet(
            "QDialog{background:#0d1117;}"
            "QLabel{color:#c9d1d9;}"
            "QPushButton{background:#1f6feb;color:#fff;border:none;border-radius:5px;"
            "padding:7px 12px;font-weight:600;}"
            "QPushButton:disabled{background:#30363d;color:#8b949e;}"
            "QPushButton#ghost{background:#21262d;color:#c9d1d9;}"
            "QPlainTextEdit{background:#161b22;color:#9ecbff;border:1px solid #243140;"
            "border-radius:5px;font-family:monospace;}")

        from mesh3d import MeshView3D
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        openb = QPushButton("Open .obj\u2026")
        openb.clicked.connect(self._open)
        top.addWidget(openb)
        self.wire = QCheckBox("Wireframe")
        self.wire.setStyleSheet("color:#c9d1d9;")
        self.wire.toggled.connect(self._toggle_wire)
        top.addWidget(self.wire)
        frameb = QPushButton("Frame"); frameb.setObjectName("ghost")
        frameb.clicked.connect(self._frame)
        top.addWidget(frameb)
        top.addStretch(1)
        self.info = QLabel("Open a ProBuilder-exported .obj to view it in 3D.")
        top.addWidget(self.info)
        lay.addLayout(top)

        self.view = MeshView3D()
        lay.addWidget(self.view, 1)

        hint = QLabel("Drag = orbit   \u2022   right-drag = pan   \u2022   wheel = zoom "
                      "\u2022   W = wireframe   \u2022   F = frame")
        hint.setStyleSheet("color:#6e7681;font-style:italic;")
        lay.addWidget(hint)

        snipbar = QHBoxLayout()
        snipbar.addWidget(QLabel(".glass snippet:"))
        snipbar.addStretch(1)
        self.copyb = QPushButton("Copy snippet"); self.copyb.setObjectName("ghost")
        self.copyb.clicked.connect(self._copy)
        self.copyb.setEnabled(False)
        snipbar.addWidget(self.copyb)
        lay.addLayout(snipbar)
        from PyQt6.QtWidgets import QPlainTextEdit
        self.snippet = QPlainTextEdit()
        self.snippet.setReadOnly(True)
        self.snippet.setMaximumHeight(120)
        self.snippet.setPlainText(
            "import an .obj to generate its  mesh.import / raycast  snippet")
        lay.addWidget(self.snippet)

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open OBJ mesh", "",
                                              "Wavefront OBJ (*.obj)")
        if not path:
            return
        try:
            import glassmesh
            self.md = glassmesh.import_obj(path, 1)
            self.path = path
        except Exception as e:
            self.info.setText("Import failed: %s" % e)
            return
        self.view.set_mesh(self.md)
        nmat = len(self.md.materials)
        self.info.setText("%d verts \u2022 %d faces \u2022 %d materials  \u2192  grid %d\u00d7%d"
                          % (len(self.md.verts), len(self.md.faces), nmat,
                             self.md.cols, self.md.rows))
        import os
        name = os.path.basename(path)
        self.snippet.setPlainText(
            'setup {\n'
            '    mesh.import("%s", 1)\n'
            '    mesh.createCollider(1)\n'
            '}\n'
            'raycast {\n'
            '    mesh: 1\n'
            '    parent: "player"\n'
            '    collide: true\n'
            '    ambient: 0.4\n'
            '    mazeID: 1\n'
            '}' % name)
        self.copyb.setEnabled(True)

    def _toggle_wire(self, on):
        self.view.wireframe = bool(on)
        self.view.update()

    def _frame(self):
        self.view._frame(); self.view.update()

    def _copy(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.snippet.toPlainText())
        self.info.setText("Snippet copied to clipboard.")


class EditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Glass Editor")
        try:
            import images
            self.setWindowIcon(images.load_icon("editor"))
        except Exception:
            pass
        self.resize(1280, 820)
        self.path = None
        self.project_dir = None

        self._build_toolbar()

        self.editor = CodeEditor()
        self.editor.setPlainText(STARTER)
        self.editor.sprite_request = lambda pos: self._open_sprite_at(pos)
        self.editor.textChanged.connect(self._schedule)

        # preview side
        right = QWidget()
        rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(0)
        self.status = QLabel("ready")
        self.status.setObjectName("statusBar")
        rv.addWidget(self.status)
        self.canvas = QWidget()
        self.canvas.setObjectName("previewCanvas")
        rv.addWidget(self.canvas, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.editor)
        split.addWidget(right)
        split.setSizes([640, 640])
        self.setCentralWidget(split)

        # debug log (Errors / Warnings / Debug) - offline analysis of your .glass
        self.problems = ProblemsPanel(on_jump=self.editor.goto_line)
        self._prob_dock = QDockWidget("Debug log", self)
        self._prob_dock.setWidget(self.problems)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._prob_dock)
        QTimer.singleShot(200, self._run_lint)     # initial analysis
        self._active_theme_path = None
        try:
            import prefs, glasstheme
            cur_ref = prefs.load("editor_theme", "Frosted Dark")
            self._active_theme_path = glasstheme.user_themes().get(cur_ref)
        except Exception:
            pass
        try:
            self.apply_chrome_theme()              # theme the whole window chrome
        except Exception:
            pass

        self._panels = []
        self.var_overrides = {}
        self._doc_vars = {}
        self.world = None
        self._pframe = QTimer(self)
        self._pframe.timeout.connect(self._preview_frame)
        self._pframe.start(0)           # uncapped - see RENDER_INTERVAL_MS in renderer.py
        self._timer = QTimer(self); self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_live)
        try:
            import prefs
            self.live_preview = bool(prefs.load("live_preview", True))
            self.scene_view = bool(prefs.load("scene_view", False))
            self.preview_audio = bool(prefs.load("preview_audio", False))
            renderer.LIGHT_QUALITY = str(prefs.load("light_quality", "medium"))
            renderer.LIGHT_SHADOWS = bool(prefs.load("light_shadows", True))
            renderer.SHADOW_RESOLUTION = int(prefs.load("shadow_resolution", 1024))
        except Exception:
            self.live_preview = True
            self.scene_view = False
            self.preview_audio = False
        try:
            import audioctl
            self.audio = audioctl.AudioController(self)
        except Exception:
            self.audio = None

        self.tutorial = TutorialDock(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.tutorial)
        self.edits = EditsDock(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.edits)
        self.tabifyDockWidget(self.tutorial, self.edits)
        self.tutorial.hide()
        self.edits.hide()

        self._refresh_file_list()
        self._render()

        # pick / create a project before editing (like Unity's project hub)
        QTimer.singleShot(0, self._choose_project)

        if not os.path.exists(SEEN_MARKER):
            self.tutorial.show()
            self.tutorial.goto(0)
            try:
                open(SEEN_MARKER, "w").close()
            except OSError:
                pass

    # ---- projects ----------------------------------------------------------
    def _choose_project(self):
        dlg = ProjectDialog(PROJECTS_DIR, self)
        dlg.exec()
        if dlg.selected:
            self._open_project(dlg.selected)
            if dlg.created:                  # brand-new empty project
                self.new_file()              # jump straight to making a script

    def _open_project(self, project_dir):
        self.project_dir = project_dir
        renderer.ASSET_DIRS = [project_dir, PROJECTS_DIR, UI_DIR, HERE]  # preview assets
        man = project.refresh_manifest(project_dir)
        name = man.get("name", os.path.basename(project_dir))
        self.setWindowTitle(f"Glass Editor - {name}")
        self._refresh_file_list()
        entry = man.get("entry", "")
        p = os.path.join(project_dir, entry) if entry else ""
        if p and os.path.exists(p):
            self._load(p)
        self.status.setText(f"project '{name}' - {len(man.get('scripts', []))} script(s)")

    def _save_manifest(self):
        if self.project_dir:
            project.refresh_manifest(self.project_dir)
            self._refresh_file_list()

    # ---- toolbar -----------------------------------------------------------
    def _build_toolbar(self):
        import images
        from PyQt6.QtCore import QSize
        tb = QToolBar(); tb.setMovable(False)
        tb.setObjectName("mainToolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        def act(text, slot, icon=None):
            a = QAction(text, self)
            if icon:
                ic = images.button_icon(icon)
                if not ic.isNull():
                    a.setIcon(ic)
            a.triggered.connect(slot); tb.addAction(a); return a

        act("New", self.new_file, "new")
        act("Open", self.open_file, "open")
        act("Save", self.save_file, "save")
        act("Save As", self.save_as, "saveas")
        tb.addSeparator()
        act("Sprites\u2026", self.open_sprites_panel, "sprites")
        tb.addSeparator()
        act("Update  \u25b6", self.update_scene, "reload")
        from PyQt6.QtGui import QAction as _QAction
        self.live_action = _QAction("Live", self)
        self.live_action.setCheckable(True)
        self.live_action.setChecked(getattr(self, "live_preview", True))
        self.live_action.setToolTip("Live preview: rebuild the scene as you type. "
                                    "Turn off to update only when you click Update (F5).")
        self.live_action.toggled.connect(self.set_live_preview)
        tb.addAction(self.live_action)

        self.preview_audio_action = _QAction("Preview Audio", self)
        self.preview_audio_action.setCheckable(True)
        self.preview_audio_action.setChecked(getattr(self, "preview_audio", False))
        self.preview_audio_action.setToolTip("Play audio.playSound calls here in the "
                                             "editor too, not just in the real game. Off "
                                             "by default. Only plays on a manual Update "
                                             "(F5) - Live mode's automatic keystroke "
                                             "rebuilds never touch it, so real audio "
                                             "doesn't restart on every keystroke while "
                                             "you're just typing nearby code.")
        self.preview_audio_action.toggled.connect(self.set_preview_audio)
        tb.addAction(self.preview_audio_action)
        # Scene (top-down 2D) vs Game (3D runner) view for raycaster levels
        self.scene_action = _QAction("Scene view", self)
        self.scene_action.setCheckable(True)
        self.scene_action.setChecked(bool(getattr(self, "scene_view", False)))
        self.scene_action.setToolTip("Scene view: draw raycaster levels top-down (2D) "
                                     "like a map editor. Off = Game view (3D first-person).")
        self.scene_action.toggled.connect(self.set_scene_view)
        tb.addAction(self.scene_action)
        # Baking tab: baked-lighting quality/shadows + baked audio acoustics
        self.light_action = _QAction("Baking\u2026", self)
        self.light_action.setToolTip("Baking: lighting quality/shadows and audio-reverb acoustics.")
        self.light_action.triggered.connect(self.open_lighting_panel)
        tb.addAction(self.light_action)
        # Sprite animator + GIF export
        self.anim_action = _QAction("Animator\u2026", self)
        self.anim_action.setToolTip("Sprite animator: assemble frames and export an animated GIF.")
        self.anim_action.triggered.connect(self.open_animator)
        tb.addAction(self.anim_action)
        # 3D View: import an OBJ mesh (e.g. from Unity ProBuilder) and inspect it
        self.mesh_action = _QAction("3D View\u2026", self)
        self.mesh_action.setToolTip("Import an .obj mesh and inspect it in a Unity-style 3D scene view.")
        self.mesh_action.triggered.connect(self.open_mesh_view)
        tb.addAction(self.mesh_action)
        QShortcut(QKeySequence("F5"), self, activated=self.update_scene)
        tb.addSeparator()

        # ---- editor theme picker ------------------------------------------
        tb.addWidget(QLabel(" Theme "))
        self.theme_combo = QComboBox()
        self.theme_combo.setStyleSheet(
            "QComboBox{background:#0d1117;color:#d7e0ea;border:1px solid #243140;"
            "border-radius:4px;padding:3px 6px;}")
        self._reload_theme_list()
        self.theme_combo.activated.connect(self._on_theme_pick)
        tb.addWidget(self.theme_combo)
        act("New theme\u2026", self.open_theme_creator)
        tb.addSeparator()

        self.proj_label = QLabel("  (no project)  ")
        self.proj_label.setStyleSheet("color:#7fdbca;")
        tb.addWidget(self.proj_label)
        self.filebox = QComboBox()
        self.filebox.setStyleSheet("QComboBox{background:#0d1117;color:#d7e0ea;padding:4px;border:1px solid #1c2530;}")
        self.filebox.activated.connect(self._pick_file)
        tb.addWidget(self.filebox)
        act("Project\u2026", self._choose_project, "project")

        spacer = QWidget()
        from PyQt6.QtWidgets import QSizePolicy
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        act("Edits", lambda: (self.edits.show(), self.edits.raise_()), "edits")
        act("Ask AI", self.open_assistant, "assistant")
        tut = act("Tutorial", lambda: (self.tutorial.show(), self.tutorial.raise_(), self.tutorial.goto(0)), "tutorial")

    def _refresh_file_list(self):
        self.filebox.blockSignals(True)
        self.filebox.clear()
        if self.project_dir:
            name = os.path.basename(self.project_dir)
            self.proj_label.setText(f"  {name}/  ")
            self.filebox.addItem("(new script)", None)
            for n in project.scan_scripts(self.project_dir):
                self.filebox.addItem(n, os.path.join(self.project_dir, n))
        else:
            self.proj_label.setText("  (no project)  ")
            self.filebox.addItem("(no project)", None)
        self.filebox.blockSignals(False)

    def _pick_file(self, idx):
        path = self.filebox.itemData(idx)
        if path:
            self._load(path)
        elif self.project_dir and self.filebox.itemText(idx) == "(new script)":
            self.new_file()

    # ---- file ops ----------------------------------------------------------
    def new_file(self):
        dlg = NewScriptDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.values:
            return
        name, bg, menutype = dlg.values
        text = make_starter(name, bg, menutype)
        self.editor.setPlainText(text)
        safe = "".join(c for c in name if c.isalnum() or c in " _-").strip()
        if safe and self.project_dir:
            self.path = os.path.join(self.project_dir, safe + ".glass")
            self.save_file()                 # writes file + updates manifest
            self._refresh_file_list()
            i = self.filebox.findData(self.path)
            if i >= 0:
                self.filebox.setCurrentIndex(i)
        else:
            self.path = None
            title = os.path.basename(self.project_dir) if self.project_dir else ""
            self.setWindowTitle(f"Glass Editor - {title} (new)")

    def open_file(self):
        start = self.project_dir or HERE
        p, _ = QFileDialog.getOpenFileName(self, "Open .glass", start, "Glass UI (*.glass)")
        if p:
            self._load(p)

    def _load(self, p):
        with open(p, "r", encoding="utf-8") as f:
            self.editor.setPlainText(f.read())
        self.path = p
        proj = os.path.basename(self.project_dir) + " - " if self.project_dir else ""
        self.setWindowTitle(f"Glass Editor - {proj}{os.path.basename(p)}")

    # ---- sprite creator ----------------------------------------------------
    def _sprite_base(self):
        if self.project_dir:
            return self.project_dir
        if self.path:
            return os.path.dirname(self.path)
        return None

    def _open_sprite_at(self, pos):
        import spritestore, spriteeditor
        base = self._sprite_base()
        if not base:
            self.status.setText("open or save into a project first to add sprites")
            return
        text = self.editor.toPlainText()
        el = spritestore._element_at(text, pos)
        if not el:
            self.status.setText("couldn't find the element for that sprite button")
            return
        name, kind = el["name"], el["kind"]
        w, h = spritestore.element_size_at(text, pos)
        existing = spritestore.load_image(base, name)
        dlg = spriteeditor.SpriteEditorDialog(self, base, name, kind, w, h, existing)
        if dlg.exec() and dlg.result_rel:
            new_text = spritestore.set_sprite_at(text, pos, dlg.result_rel)
            self.editor.setPlainText(new_text)
            self.save_file()
            self.status.setText(f"sprite saved for {name}")
            self._schedule()

    def reset_sprite_at(self, pos):
        import spritestore
        text = self.editor.toPlainText()
        el = spritestore._element_at(text, pos)
        self.editor.setPlainText(spritestore.remove_sprite_at(text, pos))
        base = self._sprite_base()
        if base and el:
            try:
                os.remove(spritestore.sprite_abs(base, el["name"]))
            except OSError:
                pass
        self.save_file()
        self.status.setText("sprite reset (back to Glass texture)")
        self._schedule()

    def open_assistant(self):
        if getattr(self, "_assistant", None) is None:
            self._assistant = AssistantDialog(self)
        self._assistant.show(); self._assistant.raise_(); self._assistant.activateWindow()

    def open_sprites_panel(self):
        import spritestore
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                     QPushButton, QScrollArea, QWidget)
        text = self.editor.toPlainText()
        els = spritestore.scan_elements(text)
        dlg = QDialog(self); dlg.setWindowTitle("Sprites"); dlg.setMinimumSize(440, 360)
        dlg.setStyleSheet("QDialog{background:#0d1117;}QLabel{color:#d7e0ea;}"
                          "QPushButton{background:#161b20;color:#e6eef7;border:1px solid #28384a;"
                          "border-radius:6px;padding:5px 10px;}QPushButton:hover{background:#243248;}")
        lay = QVBoxLayout(dlg); lay.setContentsMargins(14, 14, 14, 14); lay.setSpacing(8)
        head = QLabel("Element textures")
        head.setStyleSheet("font-size:16px;font-weight:600;color:#6cf09a;")
        lay.addWidget(head)
        lay.addWidget(QLabel("Each named element can have its own sprite. Unedited "
                             "elements use the default Glass texture."))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); col = QVBoxLayout(inner); col.setSpacing(6)
        if not els:
            col.addWidget(QLabel("No elements yet."))
        for e in els:
            row = QHBoxLayout()
            tag = "\u25c9" if e["has_sprite"] else "\u25cb"
            row.addWidget(QLabel(f"{tag} {e['kind']}  \u201c{e['name']}\u201d  (line {e['line']+1})"), 1)
            edit = QPushButton("Edit sprite")
            edit.clicked.connect(lambda _c, p=e["pos"]: (dlg.accept(), self._open_sprite_at(p)))
            row.addWidget(edit)
            rst = QPushButton("Reset")
            rst.clicked.connect(lambda _c, p=e["pos"]: (dlg.accept(), self.reset_sprite_at(p)))
            row.addWidget(rst)
            col.addLayout(row)
        col.addStretch(1)
        scroll.setWidget(inner); lay.addWidget(scroll, 1)
        close = QPushButton("Close"); close.clicked.connect(dlg.accept); lay.addWidget(close)
        dlg.exec()

    def save_file(self):
        if not self.path:
            return self.save_as()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.editor.toPlainText())
        self.status.setText(f"saved {os.path.basename(self.path)}")
        self._save_manifest()

    def save_as(self):
        start = self.project_dir or PROJECTS_DIR
        os.makedirs(start, exist_ok=True)
        p, _ = QFileDialog.getSaveFileName(self, "Save .glass", start, "Glass UI (*.glass)")
        if p:
            if not p.endswith(".glass"):
                p += ".glass"
            self.path = p
            self.save_file()
            self._load(p)

    # ---- live preview ------------------------------------------------------
    def _schedule(self):
        self.var_overrides = {}      # manual edits reset variables to declared
        self._timer.start(350)       # always schedule: runs lint (+ preview if on)

    def _render_live(self):
        # Don't rebuild the preview while the autocomplete popup is open - doing
        # so steals focus and closes it. Defer until the popup is gone.
        try:
            if self.editor._completer.popup().isVisible():
                self._timer.start(300)
                return
        except Exception:
            pass
        if getattr(self, "live_preview", True):
            had_focus = self.editor.hasFocus()
            self._render(manual=False)
            if had_focus:            # rebuilding the canvas can steal focus back
                self.editor.setFocus()
        self._run_lint()

    def _run_lint(self):
        """Analyze the current source and populate the Debug log + line marks."""
        try:
            import glasslint
            res = glasslint.analyze(self.editor.toPlainText())
        except Exception:
            return
        try:
            self.problems.update_items(res)
            self.editor.set_problem_lines([e["line"] for e in res["errors"]],
                                          [w["line"] for w in res["warnings"]])
        except Exception:
            pass

    # ---- editor themes -----------------------------------------------------
    def apply_chrome_theme(self, theme=None):
        """Recolour the whole editor window (toolbar, docks, buttons, panels) to
        match the active .glasstheme - not just the code view. Each region gets
        its own step of the same base colour (toolbar/status < preview canvas <
        dock content < dock headers/tabs < buttons/inputs) so the code view, the
        output/preview view, and the various docks read as clearly distinct
        panes instead of all blurring into the same near-identical dark."""
        import glasstheme
        t = glasstheme._norm(theme or self.editor.theme)
        bg = t["background"]; fg = t["foreground"]; acc = t["function"]
        light = glasstheme.is_light(bg)
        step = -1 if light else 1
        chrome   = glasstheme.shade(bg, step * 0.045)   # toolbar / status bar
        panel    = glasstheme.shade(bg, step * 0.085)   # preview canvas
        dockbody = glasstheme.shade(bg, step * 0.125)   # dock content (debug
                                                          # log, edits, tutorial)
        dockhdr  = glasstheme.shade(bg, step * 0.165)   # dock title bars / tabs
        raised   = glasstheme.shade(bg, step * 0.22)    # buttons / inputs
        border   = glasstheme.shade(bg, step * 0.28)
        sub = glasstheme.shade(fg, -0.35)
        qss = f"""
        QMainWindow, QWidget {{ background:{bg}; color:{fg}; }}
        QToolBar, #mainToolbar {{ background:{chrome}; border:none;
                    border-bottom:1px solid {border}; spacing:6px; padding:4px; }}
        #mainToolbar QToolButton {{ background:transparent; border:none; color:{fg}; }}
        #mainToolbar QToolButton:hover {{ background:{glasstheme.shade(chrome,0.10)};
                    border-radius:5px; }}
        #statusBar {{ background:{chrome}; color:{acc}; padding:6px 10px;
                    border-bottom:1px solid {border}; }}
        #previewCanvas {{ background:{panel}; }}
        #editsDockBody {{ background:{dockbody}; }}
        QToolButton, QPushButton {{ background:{raised}; color:{fg};
                    border:1px solid {border}; border-radius:6px; padding:5px 10px; }}
        QToolButton:hover, QPushButton:hover {{ background:{glasstheme.shade(raised,0.10)}; }}
        QComboBox, QLineEdit, QSpinBox {{ background:{raised}; color:{fg};
                    border:1px solid {border}; border-radius:6px; padding:4px 8px; }}
        QComboBox QAbstractItemView {{ background:{panel}; color:{fg};
                    selection-background-color:{acc}; }}
        QLabel {{ color:{fg}; background:transparent; }}
        QDockWidget {{ color:{fg}; titlebar-close-icon:none; }}
        QDockWidget::title {{ background:{dockhdr}; padding:5px; color:{sub}; }}
        QTabBar::tab {{ background:{dockhdr}; color:{sub}; padding:5px 12px;
                    border:1px solid {border}; border-bottom:none; }}
        QTabBar::tab:selected {{ background:{raised}; color:{fg}; }}
        QListWidget {{ background:{dockbody}; color:{fg}; border:none; }}
        QListWidget::item:hover {{ background:{dockhdr}; }}
        QScrollBar:vertical {{ background:{bg}; width:12px; }}
        QScrollBar::handle:vertical {{ background:{border}; border-radius:5px; min-height:24px; }}
        QSlider::groove:horizontal {{ background:{border}; height:4px; border-radius:2px; }}
        QSlider::handle:horizontal {{ background:{acc}; width:14px; border-radius:7px;
                    margin:-6px 0; }}
        QMenu {{ background:{panel}; color:{fg}; border:1px solid {border}; }}
        QMenu::item:selected {{ background:{acc}; color:{bg}; }}
        """
        self.setStyleSheet(qss)
        try:
            self.problems.apply_theme(dockbody, dockhdr, fg, sub, border)
        except Exception:
            pass
        try:
            self.tutorial.apply_theme(chrome, dockbody, fg, border)
        except Exception:
            pass

    def _reload_theme_list(self):
        import glasstheme
        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        self._theme_refs = []
        for name in glasstheme.builtin_names():
            self.theme_combo.addItem(name); self._theme_refs.append(name)
        user = glasstheme.user_themes()
        if user:
            self.theme_combo.insertSeparator(self.theme_combo.count())
            self._theme_refs.append(None)
            for name in user:
                # ref is the theme's NAME, not its file path - names are portable
                # across reinstalls/reset PCs (an absolute path silently stops
                # resolving if Glass ever lands in a different folder), and
                # glasstheme.resolve() already knows how to look a name up.
                self.theme_combo.addItem(name + "  (custom)"); self._theme_refs.append(name)
        self.theme_combo.insertSeparator(self.theme_combo.count())
        self._theme_refs.append(None)
        self.theme_combo.addItem("Import .glasstheme\u2026"); self._theme_refs.append("__import__")
        try:
            import prefs
            cur = prefs.load("editor_theme", "Frosted Dark")
            for i, ref in enumerate(self._theme_refs):
                if ref == cur:
                    self.theme_combo.setCurrentIndex(i); break
        except Exception:
            pass
        self.theme_combo.blockSignals(False)

    def _on_theme_pick(self, idx):
        ref = self._theme_refs[idx] if 0 <= idx < len(self._theme_refs) else None
        if ref is None:
            return
        if ref == "__import__":
            self.import_theme(); return
        self._apply_theme_ref(ref)

    def _apply_theme_ref(self, ref):
        import glasstheme
        theme = glasstheme.resolve(ref)
        self.editor.apply_theme(theme)
        self.apply_chrome_theme(theme)
        glasstheme.set_active(ref)
        self._active_theme_path = glasstheme.user_themes().get(ref)   # None for a built-in
        self.status.setText(f"Theme: {theme.get('name', '?')}")

    def import_theme(self):
        import glasstheme
        p, _ = QFileDialog.getOpenFileName(self, "Import a .glasstheme", "",
                                           "Glass theme (*.glasstheme);;All files (*)")
        if not p:
            self._reload_theme_list(); return
        try:
            theme = glasstheme.load_file(p)
            name = glasstheme.sanitize_name(theme.get("name") or "imported")
            if glasstheme.name_collides_with_builtin(name):
                name += " (imported)"          # never shadow a built-in silently
            theme["name"] = name
            dest = os.path.join(glasstheme.THEME_DIR, name + ".glasstheme")
            glasstheme.save_file(dest, theme)   # writes JSON w/ the (possibly
            self._reload_theme_list()           # renamed) name, not a raw copy
            self._apply_theme_ref(name)
        except Exception as e:
            QMessageBox.warning(self, "Import theme", f"Couldn't load that theme:\n{e}")
            self._reload_theme_list()

    def open_theme_creator(self):
        dlg = ThemeCreatorDialog(self, dict(self.editor.theme), source_path=self._active_theme_path)
        dlg.exec()
        self._reload_theme_list()

    def set_live_preview(self, on):
        self.live_preview = bool(on)
        try:
            import prefs
            prefs.save("live_preview", self.live_preview)
        except Exception:
            pass
        if self.live_preview:
            self._render_live()
            self.status.setText("Live preview on \u2013 the scene updates as you type.")
        else:
            self.status.setText("Manual mode \u2013 click Update (or F5) to refresh the scene.")

    def set_preview_audio(self, on):
        """Toggle whether audio.playSound actually plays in the editor's own
        preview (off by default - see the toolbar tooltip for why)."""
        self.preview_audio = bool(on)
        try:
            import prefs
            prefs.save("preview_audio", self.preview_audio)
        except Exception:
            pass
        if not self.preview_audio and self.audio is not None:
            self.audio.stop_all()          # turning it off should also go quiet immediately
        if self.preview_audio:
            self.status.setText("Preview audio on \u2013 audio.playSound will play here too.")
        else:
            self.status.setText("Preview audio off \u2013 sound only plays in the real game.")

    def set_scene_view(self, on):
        """Toggle raycaster levels between top-down 2D (scene) and 3D (game)."""
        self.scene_view = bool(on)
        try:
            import prefs
            prefs.save("scene_view", self.scene_view)
        except Exception:
            pass
        self._render()
        self.status.setText("Scene view (top-down 2D)." if self.scene_view
                            else "Game view (3D first-person).")

    def update_scene(self):
        """Manual render (Update button / F5)."""
        self._render()
        self.editor.setFocus()

    def open_lighting_panel(self):
        """Open the Baking tab (lighting quality/shadows + audio acoustics)."""
        dlg = LightingDialog(self)
        dlg.exec()

    def open_animator(self):
        """Open the sprite animator (frames -> animated GIF)."""
        dlg = AnimatorDialog(self)
        dlg.exec()

    def open_mesh_view(self):
        """Open the Unity-style 3D scene view (import + inspect an OBJ mesh)."""
        dlg = MeshViewDialog(self)
        dlg.exec()

    def bake_lightmaps(self):
        """Bake every lit maze in the current scene to a lightmap PNG, showing a
        Unity-style progress bar with elapsed time. Returns the files written."""
        self._render()
        found = []
        for p in getattr(self, "_panels", []):
            try:
                found += p.findChildren(renderer.RaycasterWidget)
            except Exception:
                pass
        found = [mz for mz in found if getattr(mz, "lights", None)]
        if not found:
            return []

        import time
        from PyQt6.QtWidgets import QProgressDialog, QApplication
        from PyQt6.QtCore import Qt
        total = len(found)
        dlg = QProgressDialog("Baking lightmaps\u2026", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Baking Lighting")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumWidth(360)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        start = time.time()
        state = {"cancel": False}

        def show(frac, i, mid):
            overall = (i + max(0.0, min(1.0, frac))) / total
            dlg.setValue(int(overall * 1000))
            dlg.setLabelText("Baking maze %s  (%d/%d)  -  %.1fs"
                             % (mid, i + 1, total, time.time() - start))
            QApplication.processEvents()
            if dlg.wasCanceled():
                state["cancel"] = True

        baked = []
        for i, mz in enumerate(found):
            if state["cancel"]:
                break
            mz.lightgrid = None
            mz._lm_img = None
            mz._lightmap_path = None
            try:
                path = mz.save_lightmap(progress=lambda f, i=i, m=mz.maze_id: show(f, i, m))
                if path:
                    baked.append(path)
            except Exception:
                pass
        elapsed = time.time() - start
        dlg.setValue(1000)
        dlg.setLabelText("Baked %d lightmap%s in %.1fs."
                         % (len(baked), "" if len(baked) == 1 else "s", elapsed))
        QApplication.processEvents()
        time.sleep(0.4)
        dlg.close()
        self._last_bake_time = elapsed
        return baked

    def bake_audio_acoustics(self):
        """Pre-compute room_scale_at for every floor cell of every raycast
        maze in the current scene, and SAVE it (roomscale_<mazeID>.json,
        next to lightmap_<mazeID>.png) so it survives closing the editor and
        actually applies when the real game loads - previously this only
        populated an in-memory cache on that one RaycasterWidget instance,
        which gets rebuilt from scratch on literally every render (every
        Live keystroke, every reload), so a bake had no lasting effect at
        all: the very next render started from empty again, in the editor
        AND in the real game. Not real acoustic simulation (see
        renderer.room_scale_at's docstring) - just a cheap enclosure
        estimate used to scale a simple reverb effect in audioctl.py."""
        self._render()
        found = []
        for p in getattr(self, "_panels", []):
            try:
                found += p.findChildren(renderer.RaycasterWidget)
            except Exception:
                pass
        if not found:
            return 0
        import time
        start = time.time()
        n_cells = 0
        saved_paths = []
        for mz in found:
            mz._room_scale_cache = {}
            for y, row in enumerate(getattr(mz, "grid", [])):
                for x, ch in enumerate(row):
                    if ch in renderer._RC_EMPTY:
                        mz.room_scale_at(x + 0.5, y + 0.5)
                        n_cells += 1
            p = mz.save_room_scale()
            if p:
                saved_paths.append(p)
        self._last_audio_bake_time = time.time() - start
        self._last_audio_bake_cells = n_cells
        self._last_audio_bake_paths = saved_paths
        return n_cells

    def apply_lighting(self, quality, shadows, resolution=None):
        """Set the baked-lighting quality/shadows/resolution, persist, re-render."""
        renderer.LIGHT_QUALITY = str(quality)
        renderer.LIGHT_SHADOWS = bool(shadows)
        if resolution is not None:
            renderer.SHADOW_RESOLUTION = int(resolution)
        try:
            import prefs
            prefs.save("light_quality", renderer.LIGHT_QUALITY)
            prefs.save("light_shadows", renderer.LIGHT_SHADOWS)
            prefs.save("shadow_resolution", int(renderer.SHADOW_RESOLUTION))
        except Exception:
            pass
        self._render()
        self.status.setText("Lighting: %s quality, shadows %s, %spx shadows."
                            % (renderer.LIGHT_QUALITY, "on" if shadows else "off",
                               renderer.SHADOW_RESOLUTION))

    def _clear_canvas(self):
        for p in self._panels:
            p.setParent(None)
            p.deleteLater()
        self._panels = []

    def load_example(self, text):
        self.var_overrides = {}
        self.editor.setPlainText(text)

    def apply_preview_set(self, stmt, rerender=True):
        m = __import__("re").match(r"\s*([A-Za-z_]\w*)\s*(\+=|-=|\*=|/=|=)\s*(.+)$", stmt or "")
        if not m:
            return
        name, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        scope = dict(self._doc_vars); scope.update(self.var_overrides)
        if op == "=" and rhs.lower() in ("true", "false"):
            self.var_overrides[name] = (rhs.lower() == "true")
        else:
            cur = scope.get(name, 0)
            cur = (1.0 if cur is True else 0.0 if cur is False else float(cur or 0))
            val = dsl.eval_number(rhs, scope)
            new = {"=": val, "+=": cur + val, "-=": cur - val,
                   "*=": cur * val, "/=": (cur / val if val else cur)}.get(op, val)
            self.var_overrides[name] = int(new) if float(new).is_integer() else new
        if rerender:
            self._render()

    def _preview_frame(self):
        w = self.world
        if w is None:
            return
        import time as _time
        now = _time.perf_counter()
        last = getattr(self, "_last_frame_ts", None)
        # real measured delta, not a hardcoded 0.016 - previously this lied to
        # every script that uses time.normal for frame-independent behavior
        # (e.g. damage-over-time): it always claimed exactly 16ms passed even
        # when the real frame took longer (a slow frame) or less (fast
        # hardware once the render/logic timers are uncapped), so anything
        # scaled by time.normal was quietly wrong at any fps other than 60.
        # World.time.tick() already clamps this to 0.05s max, so a real stall
        # (e.g. window resize) still can't cause a huge logic jump.
        dt = (now - last) if last is not None else (1.0 / 60.0)
        self._last_frame_ts = now
        try:
            w.begin_frame(dt)
            w.run_setup_once()
            w.maybe_fit()
            w.run_update()
            w.tick_timers()
            renderer.spawn_pending(w, getattr(w, "host", None))
            w.resolve_collisions()
            renderer.refresh_var_bindings(w.vars)
            renderer.refresh_media_bindings(w.vars)
            if getattr(w, "dynamic", False):
                for obj in list(w.objects.values()):
                    renderer.apply_transform(obj)
            w.end_frame()
        except Exception:
            pass

    def log(self, msg):
        """Minimal log sink so audioctl.py's error-surfacing (playback
        failures, missing output device, etc.) has somewhere visible to go
        in the editor too, not just the browser."""
        print(msg)
        try:
            self.status.setStyleSheet("background:#1a0e0e;color:#ff6b6b;padding:6px 10px;border-bottom:1px solid #2a1414;")
            self.status.setText(msg)
        except Exception:
            pass

    def _render(self, manual=True):
        self._clear_canvas()
        text = self.editor.toPlainText()
        try:
            rules = dsl.parse(text)
        except dsl.DSLError as e:
            self.status.setStyleSheet("background:#1a0e0e;color:#ff6b6b;padding:6px 10px;border-bottom:1px solid #2a1414;")
            self.status.setText(f"parse error: {e}")
            return
        except Exception as e:
            self.status.setStyleSheet("background:#1a0e0e;color:#ff6b6b;padding:6px 10px;border-bottom:1px solid #2a1414;")
            self.status.setText(f"couldn't render: {e}")
            return
        self.status.setStyleSheet("background:#0a0e13;color:#7fdbca;padding:6px 10px;border-bottom:1px solid #1c2530;")
        self._doc_vars = dict(getattr(rules, "variables", {}) or {})
        scope = dict(self._doc_vars); scope.update(getattr(self, "var_overrides", {}))
        api = PreviewAPI(lambda m: self.status.setText(m), self)
        cw = max(1, self.canvas.width()); ch = max(1, self.canvas.height())
        renderer.set_screen(cw, ch)               # screen.* tracks the preview area
        renderer.SCENE_VIEW = bool(getattr(self, "scene_view", False))
        if manual and getattr(self, "audio", None) is not None:
            self.audio.stop_all()          # a fresh render means a fresh setup{} run -
                                            # stop whatever the LAST render started first,
                                            # or a looping sound stacks a new copy on top
                                            # of the old one every single rebuild
        try:
            panels, registry = renderer.render_rules(
                rules, api, self.canvas, host=None, registry=api.registry, variables=scope)
        finally:
            renderer.SCENE_VIEW = False            # never leak into the browser runner
        if manual and getattr(self, "preview_audio", False) and self.world is not None and self.audio is not None:
            self.world.audio = self.audio
            # manual-only: Live mode can re-render on every keystroke, and
            # restarting a REAL playing sound (stop + reload + replay) that
            # often is not something to do dozens of times a minute while
            # someone is just typing nearby code - press Update (F5) any
            # time you actually want to hear the current audio.playSound
            # calls, rather than it firing automatically as you type.
        for p in panels:
            p.raise_()
        self._panels = panels
        n = len(panels)
        self.status.setText(f"OK - {n} top-level component{'s' if n != 1 else ''} rendered. Drag moveable ones around.")


# ===========================================================================
#  Tutorial
# ===========================================================================
def _build_steps():
    try:
        from api import BrowserAPI
        acts = BrowserAPI.actions()
    except Exception:
        acts = {}
    action_rows = "".join(
        f"<tr><td style='color:#7fdbca;padding:2px 10px 2px 0'><code>{k}</code></td>"
        f"<td style='color:#cdd6e0'>{v}</td></tr>"
        for k, v in sorted(acts.items())
    )

    steps = []
    steps.append(("Welcome", """
        <h2>Welcome to Glass</h2>
        <p>You build browser UI in <b>.glass</b> files. The shape of every line is:</p>
        <pre>scope.component ( traits ) {
    property: value
    element "Label" { ... }
}</pre>
        <ul>
        <li><b>scope</b> = <code>*</code> (all sites) or a domain like <code>example.com</code></li>
        <li><b>component</b> = <code>menu</code>, <code>holder</code>, <code>panel</code>, <code>bar</code>, or <code>main</code></li>
        <li><b>traits</b> = flags like <code>moveable</code>, <code>resizable</code>, <code>closable</code></li>
        </ul>
        <p>Type in the editor; the right side updates live. Hit <b>Next</b>.</p>
        """, None))

    steps.append(("Your first menu", """
        <h2>A menu</h2>
        <p>Click <b>Load example</b>, then drag the menu around the preview.</p>
        <p><code>menu.moveable</code> and plain <code>moveable</code> mean the same thing
        - the last word after the dot is the trait.</p>
        """, '''*.menu ( menu.moveable menu.closable ) {
    title: First Menu
    background: #0f1419
    width: 240

    button "Reload" { action: reload }
    link   "Wikipedia" { url: https://wikipedia.org }
    separator
    text "I am a menu." { color: #6cf09a }
}'''))

    steps.append(("text + textgroups", """
        <h2>Text and fonts</h2>
        <p><b>textgroup</b> names a font once, then any text/button can reuse it
        by name. Form: <code>textgroup { Name, fonttype }</code>.</p>
        <p>A <b>text</b> element: <code>text "words" { color, width, height, backgroundcolor, font }</code>.
        Its <code>font</code> can be a textgroup name <i>or</i> a family directly.</p>
        """, '''*.menu ( moveable ) {
    title: Fonts
    textgroup { Title, timesnewroman }
    textgroup { Mono, consolas }

    text "Serif heading" { color: #6cf09a, font: Title }
    text "Monospace line" { color: #ffcb6b, font: Mono, backgroundcolor: #11161c }
}'''))

    steps.append(("Customizable buttons", """
        <h2>Buttons you can style</h2>
        <p>Simple button: <code>button "Name" { action: reload }</code></p>
        <p>Styled button - put a style block before the name:</p>
        <pre>button { { color, height, width, textgroup } "Name" } { action }</pre>
        <p><code>color</code> is the background; <code>textgroup</code> picks the font.</p>
        """, '''*.menu ( moveable ) {
    title: Buttons
    textgroup { B, arial }

    button "Plain" { action: reload }
    button { { color: #1e88e5, width: 180, height: 36, textgroup: B } "Big blue" } { action: viewsource }
    button { { color: #e53935 } "Red danger" } { js: alert('hi') }
}'''))

    steps.append(("Holders + multi-page", """
        <h2>Holders = toggleable pages</h2>
        <p>A <b>holder</b> is a sub-window inside a menu. Give it a name and start it
        <code>hidden: true</code>, then toggle it from a button:</p>
        <pre>button "Open" { action: toggle, target: <i>holdername</i> }</pre>
        <p>Holder knobs go in its parentheses:</p>
        <pre>holder ( size, backgroundColor, opacity,
        outline, outlinecolor, outlineThickness ) { }</pre>
        <p><code>outline</code> is true/false; if false, the color is ignored.
        Add <code>autosize</code> so the holder grows to fit its content instead
        of clipping, and add <code>remember</code> to the menu so it keeps its
        dragged position after a reload.</p>
        """, '''*.menu ( moveable closable remember ) {
    title: Main
    button { { color: #1e88e5 } "Go to settings" } { action: toggle, target: settings }

    holder ( autosize, size: 240, outline: true, outlinecolor: #6cf09a, outlineThickness: 2, hidden: true ) {
        name: settings
        title: Settings
        text "A second page." { color: #ddd }
        button { { color: #e53935 } "Back" } { action: toggle, target: settings }
    }
}'''))

    steps.append(("Scale", """
        <h2>Scale</h2>
        <p>Zoom a whole menu or holder - box, text, and buttons together,
        like resizing a window:</p>
        <pre>scale { 1.5 }      // one number = uniform zoom</pre>
        <p>One is normal. You can pass up to three
        (<code>width, height, content</code>) for non-uniform stretching, but a
        single value is the easy path. Try bumping it to 2.</p>
        """, '''*.menu ( moveable ) {
    title: Scaled
    scale { 1.5 }
    text "Bigger!" { color: #6cf09a }
    button "Reload" { action: reload }
}'''))

    steps.append(("Full-screen main", """
        <h2>A home page with <code>.main</code></h2>
        <p>A <code>menu</code> floats; a <code>main</code> can take the whole
        screen like a website homepage. Put <code>menu.full</code> inside for a
        full-screen page, or <code>menu.ui</code> for a normal draggable menu.</p>
        <p>Give it a <code>background</code> colour (or <code>image:</code>) and
        it fills everything behind your other menus.</p>
        """, '''*.main {
    menu.full
    background: #0d1117
    center { center }
    title: My Home Page
    text "Everything centered on screen." { color: #6cf09a }
    button "Enter" { action: reload }
}'''))

    steps.append(("center", """
        <h2>Positioning with <code>center</code></h2>
        <p>Place content in a full-screen <code>main</code> (or align it inside a
        holder):</p>
        <pre>center { center }     // also: left right up down
center { topleft }    // topright bottomleft bottomright
center { topcenter }  // bottomcenter</pre>
        <p>Use it at the top of a <code>.main</code>/holder. Try the corners.</p>
        """, '''*.main {
    menu.full
    background: #11161c
    center { bottomright }
    text "Bottom-right corner" { color: #6cf09a }
    button "Reload" { action: reload }
}'''))

    steps.append(("Variables + grab", """
        <h2>Variables and <code>grab</code></h2>
        <p>Declare values Unity-style at the top of a script - no semicolons:</p>
        <pre>bool ShowNews = true
int Count = 3
float Speed = 1.5</pre>
        <p><code>grab</code> opens a holder on load, but only if a boolean is
        true:</p>
        <pre>grab { "news", ShowNews }</pre>
        <p>Flip <code>ShowNews</code> to <code>false</code> and the News page
        stays hidden until a button toggles it.</p>
        """, '''bool ShowNews = true
grab { "news", ShowNews }

*.menu ( moveable ) {
    title: Home
    button "Toggle news" { action: toggle, target: news }
    holder ( autosize, hidden: true ) {
        name: news
        title: News
        text "Grabbed open because ShowNews is true." { color: #ddd }
    }
}'''))

    steps.append(("If / else", """
        <h2>Conditions</h2>
        <p>Two ways to branch on a variable. <b>Inline</b> - add <code>if:</code>
        to any element so it only appears when true:</p>
        <pre>button "Admin" { action: reload, if: IsAdmin }</pre>
        <p><b>Blocks</b> - <code>if</code> / <code>else if</code> / <code>else</code>
        wrap a group of entries (Unity-style conditions, no semicolons):</p>
        <pre>if (Count > 5) { text "many" {} }
else if (Count == 3) { text "three" {} }
else { text "few" {} }</pre>
        <p>Operators: <code>== != &gt; &lt; &gt;= &lt;=</code> and
        <code>&amp;&amp; || !</code>. Try changing <code>Count</code> below.</p>
        """, '''int Count = 3
bool IsAdmin = true

*.menu ( moveable ) {
    title: Conditions
    button "Admin tools" { action: reload, if: IsAdmin }

    if (Count > 5) {
        text "Count is big" { color: #f87171 }
    } else if (Count == 3) {
        text "Count is exactly three" { color: #6cf09a }
    } else {
        text "Count is small" { color: #60a5fa }
    }
}'''))

    steps.append(("Save and share", """
        <h2>Save, load, and share</h2>
        <p>Save a file into the <b>projects/</b> folder (Save As), then in the
        browser's address bar:</p>
        <ul>
        <li><code>home.glass/my</code> - load it locally from projects/</li>
        <li><code>home.glass/serv</code> - host it on your network</li>
        </ul>
        <p>Anyone on your network then types
        <code>your-ip:8765/home.glass</code> to load what you're serving -
        like joining a game server by address.</p>
        """, None))

    steps.append(("Scroll boxes", """
        <h2>Scrollable holders</h2>
        <p>Add the <code>scroll</code> trait to a holder and give it a size - it
        becomes a scroll box that can hold a ton of text or data:</p>
        <pre>holder ( scroll, size: 300x160 ) { ...lots of stuff... }</pre>
        """, '''*.menu ( moveable ) {
    title: Log
    holder ( scroll, size: 280x140, outline: true ) {
        text "Line 1" { color: #ddd }
        text "Line 2" { color: #ddd }
        text "Line 3" { color: #ddd }
        text "Line 4" { color: #ddd }
        text "Line 5" { color: #ddd }
        text "Line 6" { color: #ddd }
        text "Line 7" { color: #ddd }
        text "Line 8 - scroll to see me" { color: #6cf09a }
    }
}'''))

    steps.append(("Math and live values", """
        <h2>Changing variables (Unity-style math)</h2>
        <p>Buttons can change variables with <code>set:</code> - then the UI
        updates instantly. Show a value with <code>{braces}</code>:</p>
        <pre>button "Add"   { set: Count += 1 }   // also -=  *=  /=  =
text "Score: {Count}"</pre>
        <p>And one button can flip several holders at once:</p>
        <pre>action: show, target: a b c        // space-separated
do: hide a c; toggle b             // a ;-separated sequence</pre>
        """, '''int Count = 0

*.menu ( moveable ) {
    title: Counter
    text "Score: {Count}" { color: #6cf09a }
    button "Add 1"  { set: Count += 1 }
    button "Double" { set: Count *= 2 }
    button "Reset"  { set: Count = 0 }
}'''))

    steps.append(("Aligning elements", """
        <h2>Per-element alignment</h2>
        <p>Besides the container-wide <code>center { }</code> block, any single
        <code>text</code> or <code>button</code> can take a <code>center:</code>
        property to align just itself - <code>left</code>, <code>right</code> or
        <code>center</code>:</p>
        <pre>text "Hello" { width: 300, center: left }</pre>
        """, '''*.menu ( moveable ) {
    title: Alignment
    text "Your space to think \\u2014 no ads, no AI, no tracking." { color: #6cf09a, width: 300, center: left }
    text "centered line" { width: 300, center: center }
    button "pushed right" { action: reload, center: right }
}'''))

    steps.append(("Projects and getGlass", """
        <h2>Projects &amp; multiple scripts</h2>
        <p>The editor works inside a <b>project</b> - a folder of
        <code>.glass</code> scripts plus a <code>project.json</code> the server
        reads. Use the <b>Project...</b> button to create or switch projects, and
        the dropdown to jump between scripts in the open project.</p>
        <p>Link scripts together with the <code>getGlass</code> action. It loads
        another script <b>from the same project only</b>. Add
        <code>openNew</code> to choose where it opens - <code>false</code>
        replaces the current tab, <code>true</code> opens a new one:</p>
        <pre>button "Page 2" { action: getGlass, target: page2, openNew: false }</pre>
        <p>In the browser, open a whole project with
        <code>ProjectName/my</code>, or host it with
        <code>ProjectName/serv</code>. Use the <b>+</b> on the tab bar (or
        Ctrl+T) for more tabs.</p>
        """, '''*.main {
    menu.full
    center { center }
    title: Home
    text "Home page of this project." { color: #6cf09a }
    button { { color: #1e88e5, width: 200, height: 38 } "Page 2 (here)" } { action: getGlass, target: page2, openNew: false }
    button { { color: #2e7d32, width: 200, height: 38 } "Page 2 (new tab)" } { action: getGlass, target: page2, openNew: true }
}'''))

    steps.append(("The actions (API)", f"""
        <h2>Every action you can call</h2>
        <p>When a button says <code>action: X</code>, X is a method in <b>api.py</b>.
        These ship built in:</p>
        <table>{action_rows}</table>
        <p>Some take a target, e.g. <code>action: toggle, target: page2</code>.</p>
        """, None))

    steps.append(("Add your own action", """
        <h2>Make a new action</h2>
        <p>Open <b>api.py</b> and add a method to <code>BrowserAPI</code>. The method
        name becomes the action name; the docstring shows up in this tutorial.</p>
        <pre>def hello(self):
    \"\"\"Print a greeting to the log.\"\"\"
    self.window.log("Hello from my own action!")</pre>
        <p>Save api.py. (In the browser, anything self.window.* is fair game -
        navigate, run_js, current_view, interceptor, etc.)</p>
        """, None))

    steps.append(("Use your action", """
        <h2>Call it</h2>
        <p>Reference the new method by name from any button:</p>
        <pre>button "Say hi" { action: hello }</pre>
        <p>Save the .glass file (or, in the browser, hit <b>UI</b> / Ctrl+Shift+U)
        and your button now runs your code. That's the whole loop:</p>
        <p style='color:#6cf09a'>edit api.py - reference by name - reload UI.</p>
        """, '''*.menu ( moveable ) {
    title: My action
    button "Say hi" { action: hello }
}'''))

    steps.append(("Done", """
        <h2>You've got it</h2>
        <p>Recap: <b>menu/holder/panel/bar</b> are containers; <b>traits</b> in (),
        <b>properties</b> and <b>elements</b> in {}. <b>textgroups</b> name fonts,
        <b>holders</b> make pages, <b>scale</b> resizes, and <b>actions</b> live in api.py.</p>
        <p>Close this panel and build. Save into the <code>ui/</code> folder and the
        browser hot-reloads it.</p>
        """, None))
    return steps


class TutorialDock(QDockWidget):
    def __init__(self, window):
        super().__init__("Tutorial", window)
        self.window = window
        self.steps = _build_steps()
        self.idx = 0
        self.setMinimumWidth(360)

        body = QWidget()
        v = QVBoxLayout(body); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        # navigation bar pinned to the TOP so it's always visible
        row = QWidget()
        self._nav_row = row
        row.setStyleSheet("background:#0c1117;border-bottom:1px solid #1c2530;")
        h = QHBoxLayout(row); h.setContentsMargins(8, 8, 8, 8); h.setSpacing(8)
        self.btn_prev = QPushButton("\u2190 Back")
        self.btn_load = QPushButton("Load example")
        self.btn_next = QPushButton("Next \u2192")
        for b in (self.btn_prev, self.btn_load, self.btn_next):
            b.setMinimumHeight(30)
            b.setStyleSheet("QPushButton{background:#1b2a3a;color:#eaf2fb;border:1px solid #2c4257;"
                            "border-radius:6px;padding:6px 10px;font-weight:600;}"
                            "QPushButton:hover{background:#244763;}"
                            "QPushButton:disabled{background:#141a22;color:#566677;border-color:#1c2530;}")
        self.btn_prev.clicked.connect(lambda: self.goto(self.idx - 1))
        self.btn_next.clicked.connect(lambda: self.goto(self.idx + 1))
        self.btn_load.clicked.connect(self._load_example)
        h.addWidget(self.btn_prev); h.addWidget(self.btn_load, 1); h.addWidget(self.btn_next)
        v.addWidget(row)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet("background:#0d1117;color:#cdd6e0;border:none;padding:8px;")
        v.addWidget(self.view, 1)
        self.setWidget(body)

    def goto(self, i):
        self.idx = max(0, min(i, len(self.steps) - 1))
        title, html, example = self.steps[self.idx]
        n = len(self.steps)
        self.setWindowTitle(f"Tutorial  ({self.idx + 1}/{n})  -  {title}")
        self.view.setHtml(html)
        self.btn_load.setEnabled(example is not None)
        self.btn_load.setText("Load example" if example else "(no example)")
        self.btn_prev.setEnabled(self.idx > 0)
        self.btn_next.setEnabled(self.idx < n - 1)

    def apply_theme(self, chrome_bg, panel_bg, fg, border):
        """Recolour the nav bar + body to match the active .glasstheme (the
        nav buttons keep their own blue accent - that's a deliberate look,
        not part of the background that was blurring into every other panel)."""
        self._nav_row.setStyleSheet(f"background:{chrome_bg};border-bottom:1px solid {border};")
        self.view.setStyleSheet(f"background:{panel_bg};color:{fg};border:none;padding:8px;")

    def _load_example(self):
        _, _, example = self.steps[self.idx]
        if example:
            self.window.load_example(example)


class ColorWheel(QWidget):
    """HSV colour wheel: angle = hue, radius = saturation. Click/drag to pick."""
    def __init__(self, on_change, size=216):
        super().__init__()
        self.setFixedSize(size, size)
        self._size = size
        self.on_change = on_change
        self.value = 1.0
        self.hue = 0.0
        self.sat = 0.0
        self._img = self._build_wheel()

    def _build_wheel(self):
        size = self._size
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        cx = cy = size / 2.0
        R = size / 2.0 - 2
        for y in range(size):
            for x in range(size):
                dx = x - cx
                dy = y - cy
                r = math.hypot(dx, dy)
                if r <= R:
                    h = (math.degrees(math.atan2(dy, dx)) + 360) % 360
                    img.setPixelColor(x, y, QColor.fromHsvF(h / 360.0, min(1.0, r / R), 1.0))
        return img

    def set_value(self, v):
        self.value = max(0.0, min(1.0, v))
        self.update()
        self._emit()

    def color(self):
        return QColor.fromHsvF(self.hue / 360.0, self.sat, self.value)

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawImage(0, 0, self._img)
        cx = cy = self._size / 2.0
        R = self._size / 2.0 - 2
        a = math.radians(self.hue)
        rr = self.sat * R
        mx = cx + math.cos(a) * rr
        my = cy + math.sin(a) * rr
        p.setPen(QColor("#0a0e13"))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPoint(int(mx), int(my)), 6, 6)
        p.setPen(QColor("#ffffff"))
        p.drawEllipse(QPoint(int(mx), int(my)), 5, 5)
        p.end()

    def _pick(self, pos):
        cx = cy = self._size / 2.0
        R = self._size / 2.0 - 2
        dx = pos.x() - cx
        dy = pos.y() - cy
        r = math.hypot(dx, dy)
        self.hue = (math.degrees(math.atan2(dy, dx)) + 360) % 360
        self.sat = min(1.0, r / R)
        self.update()
        self._emit()

    def mousePressEvent(self, e):
        self._pick(e.position())

    def mouseMoveEvent(self, e):
        self._pick(e.position())

    def _emit(self):
        if self.on_change:
            self.on_change(self.color())


class EditsDock(QDockWidget):
    """A colour wheel; click to read RGBA + hex, copy or insert into the editor."""
    def __init__(self, window):
        super().__init__("Edits", window)
        self.window = window
        self.setMinimumWidth(260)
        self._hex = "#ffffff"

        body = QWidget()
        body.setObjectName("editsDockBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        self.wheel = ColorWheel(self._changed)
        v.addWidget(self.wheel, 0, Qt.AlignmentFlag.AlignHCenter)

        def slider(lo, hi, val, cb):
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(lo, hi); s.setValue(val); s.valueChanged.connect(cb)
            s.setStyleSheet("QSlider{height:18px;}")
            return s

        v.addWidget(self._lbl("Brightness"))
        self.val = slider(0, 100, 100, lambda x: self.wheel.set_value(x / 100.0))
        v.addWidget(self.val)
        v.addWidget(self._lbl("Alpha"))
        self.alpha = slider(0, 255, 255, lambda _x: self._changed(self.wheel.color()))
        v.addWidget(self.alpha)

        self.swatch = QLabel()
        self.swatch.setFixedHeight(44)
        v.addWidget(self.swatch)

        self.hexlbl = QLabel(self._hex)
        self.rgbalbl = QLabel("rgba(255, 255, 255, 255)")
        for l in (self.hexlbl, self.rgbalbl):
            l.setStyleSheet("font-family:Consolas,monospace;color:#e6edf5;font-size:13px;")
        v.addWidget(self.hexlbl)
        v.addWidget(self.rgbalbl)

        row = QHBoxLayout()
        cp = QPushButton("Copy #hex")
        ins = QPushButton("Insert at cursor")
        for b in (cp, ins):
            b.setStyleSheet("QPushButton{background:#1b2a3a;color:#eaf2fb;border:1px solid #2c4257;"
                            "border-radius:6px;padding:7px 10px;}QPushButton:hover{background:#244763;}")
        cp.clicked.connect(self._copy)
        ins.clicked.connect(self._insert)
        row.addWidget(cp); row.addWidget(ins)
        v.addLayout(row)

        v.addSpacing(6)
        proj = QPushButton("\U0001f4c1  Open project folder")
        proj.setStyleSheet("QPushButton{background:#152232;color:#cfe0f5;border:1px solid "
                           "#2c4257;border-radius:6px;padding:7px 10px;}"
                           "QPushButton:hover{background:#20364d;}")
        proj.clicked.connect(self._open_project_folder)
        v.addWidget(proj)

        from PyQt6.QtWidgets import QCheckBox
        self.hover_cb = QCheckBox("Show hover descriptions")
        self.hover_cb.setToolTip("Hover a keyword (like action:) to see what it does.")
        self.hover_cb.setStyleSheet("color:#c7d2dc;")
        try:
            import prefs
            self.hover_cb.setChecked(bool(prefs.load("hover_docs", True)))
        except Exception:
            self.hover_cb.setChecked(True)
        self.hover_cb.toggled.connect(self._toggle_hover)
        v.addWidget(self.hover_cb)
        v.addStretch(1)

        body.setStyleSheet("background:#0d1117;")
        self.setWidget(body)
        self._changed(self.wheel.color())

    def _lbl(self, t):
        l = QLabel(t)
        l.setStyleSheet("color:#8aa0b2;font-size:12px;")
        return l

    def _current(self):
        c = self.wheel.color()
        c.setAlpha(self.alpha.value())
        return c

    def _changed(self, _c):
        c = self._current()
        base = "#%02x%02x%02x" % (c.red(), c.green(), c.blue())
        self._hex = base + ("%02x" % c.alpha() if c.alpha() < 255 else "")
        self.hexlbl.setText(self._hex)
        self.rgbalbl.setText(f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})")
        self.swatch.setStyleSheet(f"background:{base};border:1px solid #2c4257;border-radius:8px;")

    def _copy(self):
        QApplication.clipboard().setText(self._hex)
        self.window.status.setText(f"copied {self._hex}")

    def _insert(self):
        self.window.editor.insertPlainText(self._hex)

    def _open_project_folder(self):
        import subprocess
        folder = getattr(self.window, "project_dir", None) or \
            (os.path.dirname(self.window.path) if getattr(self.window, "path", None) else None)
        if not folder or not os.path.isdir(folder):
            self.window.status.setText("No project folder yet - save your .glass first.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)                       # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            self.window.status.setText(f"Couldn't open folder: {e}")

    def _toggle_hover(self, on):
        try:
            import prefs
            prefs.save("hover_docs", bool(on))
        except Exception:
            pass
        if hasattr(self.window, "editor"):
            self.window.editor.hover_docs = bool(on)


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Anthropic.Glass.Editor")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Glass Editor")
    try:
        import images
        app.setWindowIcon(images.load_icon("editor"))
    except Exception:
        pass
    try:
        import theme
        theme.apply_theme(app)
    except Exception:
        pass
    win = EditorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
