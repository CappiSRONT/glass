"""Glass Assistant - a local, offline coding helper for the Glass language.

It doesn't call any cloud service (nothing leaves your PC, matching Glass's
privacy stance). Instead it holds a curated knowledge base of the Glass
language + wiki and answers questions by ranking topics against what you ask,
returning an explanation plus a runnable code example.
"""

import re

# Each topic: id, title, keywords (weighted extra), body, code.
KB = [
    {
        "id": "structure",
        "title": "Script structure & entry menu",
        "keywords": ["structure", "start", "entry", "main", "menu", "basics",
                     "begin", "skeleton", "template", "hello", "world", "first"],
        "body": "Every .glass screen is a menu block: `*.name { menu.<type> ... }`. "
                "`*.main` is the entry screen. Inside you set a background and add "
                "elements (text, button, holder, image...). Menu types: `menu.full` "
                "(normal page), `menu.ui` (overlay UI), `menu.dynamic` (camera that "
                "scales the scene to the window - used for games).",
        "code": '*.main {\n'
                '    menu.full\n'
                '    background: #0f1419\n'
                '    center { center }\n'
                '    title: "My Page"\n\n'
                '    text "Hello, Glass!" { color: #6cf09a }\n'
                '}',
    },
    {
        "id": "text",
        "title": "Text / labels",
        "keywords": ["text", "label", "write", "words", "title", "heading", "color",
                     "font", "display", "show", "message"],
        "body": "`text \"...\" { }` shows a line of text. Style it with `color`, "
                "`background`, `width`, `height`. Put a variable inside with {Name} "
                "and it updates live as the value changes.",
        "code": 'text "Score: {Score}" { color: #e7edf3 }\n'
                'text "Welcome" { color: #6cf09a, background: #10141b }',
    },
    {
        "id": "button",
        "title": "Buttons & actions",
        "keywords": ["button", "click", "press", "action", "link", "navigate",
                     "getglass", "opennew", "target", "call", "tap", "go"],
        "body": "A button runs an action when clicked. Two common forms:\n"
                "  button \"Label\" { action: getGlass, target: page2 }\n"
                "  button { { color: #1e88e5, width: 200, height: 38 } \"Label\" } { ... }\n"
                "Actions: `getGlass` loads another script in the same project "
                "(`target:` = its name, `openNew: true` opens a new tab); `call: pkg.fn` "
                "runs a Python function from an imported package.",
        "code": 'button "Go to page 2" { action: getGlass, target: page2, openNew: false }\n'
                'button "Run code"    { call: demo.run }',
    },
    {
        "id": "getglass",
        "title": "Linking screens with getGlass",
        "keywords": ["getglass", "link", "navigate", "page", "screen", "open",
                     "next", "goto", "target", "opennew", "tab", "connect"],
        "body": "getGlass loads a sibling .glass file from the CURRENT project. "
                "`target:` is the file name without extension. `openNew: false` "
                "replaces the current view; `openNew: true` opens it in a new tab.",
        "code": 'button "Next" { action: getGlass, target: level2 }\n'
                'button "Open in new tab" { action: getGlass, target: help, openNew: true }',
    },
    {
        "id": "holder",
        "title": "Holders (containers / panels)",
        "keywords": ["holder", "container", "group", "panel", "box", "frame",
                     "layout", "wrap", "outline", "sprite"],
        "body": "A `holder ( ) { }` groups elements together. It can have an "
                "`outline`, a `background`, or a `sprite`. Elements inside it stack "
                "in order. Great for cards, side panels, or HUD groups.",
        "code": 'holder ( ) {\n'
                '    background: #10141b\n'
                '    text "Inventory" { color: #6cf09a }\n'
                '    button "Close" { action: getGlass, target: main }\n'
                '}',
    },
    {
        "id": "image",
        "title": "Images & sprites",
        "keywords": ["image", "picture", "sprite", "png", "texture", "icon",
                     "vcr.image", "show image", "graphic", "art"],
        "body": "Show a picture with `vcr.image \"file.png\" { size: WxH }`, or give "
                "any element its own look with `sprite: \"sprites/name.png\"` (it "
                "stretches to fit and auto-scales). In the editor, click the pencil "
                "in the right gutter of an element's line to draw its sprite.",
        "code": 'vcr.image "logo.png" { size: 128x128 }\n'
                'button "Play" { sprite: "sprites/play.png", width: 160, height: 48 }',
    },
    {
        "id": "compress",
        "title": "Image/video compression (retro looks)",
        "keywords": ["compress", "pixel", "pixelate", "standr", "retro", "type",
                     "posterize", "downscale", "quality", "effect", "vcr"],
        "body": "On vcr.image / vcr.video use `compress:` (or `type:`) for a stylised "
                "look: `pixel` or `pixel(N)` pixelates; `standR(v)` posterizes colors "
                "(v 0..1, lower = stronger). Works on both images and video frames.",
        "code": 'vcr.image "photo.png" { size: 200x120, compress: pixel(8) }\n'
                'vcr.video "clip.mp4" { size: 320x180, compress: standR(0.3) }',
    },
    {
        "id": "video",
        "title": "Playing video",
        "keywords": ["video", "play", "movie", "clip", "mp4", "vcr.video",
                     "speed", "starton", "loop", "media"],
        "body": "`vcr.video \"file.mp4\" { size: WxH }` plays a video. Optional "
                "`speed:` changes playback rate, `startOn:` sets a start time, and "
                "`compress:` applies the retro effects above per frame.",
        "code": 'vcr.video "intro.mp4" { size: 480x270, speed: 1.0, startOn: 0 }',
    },
    {
        "id": "input",
        "title": "Text input fields",
        "keywords": ["input", "field", "type", "textbox", "entry", "form",
                     "enter", "typing", "box"],
        "body": "`input { }` gives the user a text field. Bind it to a variable so "
                "you can read what they typed, and react in an update/call.",
        "code": 'string Name = ""\n'
                'input { bind: Name, width: 240 }\n'
                'text "Hi {Name}" { color: #6cf09a }',
    },
    {
        "id": "variables",
        "title": "Variables & live values",
        "keywords": ["variable", "var", "string", "int", "number", "value",
                     "state", "counter", "live", "update", "bind", "dynamic"],
        "body": "Declare variables above the menu: `string Name = \"idle\"` or "
                "`int Score = 0`. Reference them anywhere in text with {Name}; the "
                "text updates automatically whenever the value changes (Glass only "
                "redraws when it actually changes, so it's cheap).",
        "code": 'int Score = 0\n'
                'string Status = "ready"\n\n'
                '*.main {\n'
                '    menu.full\n'
                '    text "Score: {Score}  -  {Status}" { color: #e7edf3 }\n'
                '}',
    },
    {
        "id": "colors",
        "title": "Colors, size & background",
        "keywords": ["color", "colour", "background", "size", "width", "height",
                     "style", "dimensions", "hex", "bg"],
        "body": "Colors are hex like `#6cf09a`. `background:` sets the fill, `color:` "
                "the text/foreground. Size an element with `width:` and `height:` (in "
                "px) or `size: WxH`.",
        "code": 'text "Big"  { color: #ffffff, background: #1e88e5, width: 200, height: 40 }',
    },
    {
        "id": "center",
        "title": "Centering & titles",
        "keywords": ["center", "centre", "align", "middle", "title", "layout",
                     "position", "place"],
        "body": "`center { center }` centers the menu's content. `title:` sets the "
                "screen title. On individual elements, `center: center` centers them.",
        "code": '*.main {\n'
                '    menu.full\n'
                '    center { center }\n'
                '    title: "Menu" { center: center, color: #6cf09a }\n'
                '}',
    },
    {
        "id": "imports",
        "title": "Imports & packages (running Python)",
        "keywords": ["import", "package", "python", "call", "function", "module",
                     "pkg", "code", "backend", "logic"],
        "body": "Add `import name` at the top to load a package (a Python file in the "
                "project). Then a button's `call: name.func` runs that function. Great "
                "for real logic behind your UI.",
        "code": 'import demo\n\n'
                '*.main {\n'
                '    menu.full\n'
                '    button "Run Python" { call: demo.run }\n'
                '}',
    },
    {
        "id": "game",
        "title": "Making a game (engine basics)",
        "keywords": ["game", "engine", "move", "player", "sprite", "update",
                     "setup", "loop", "physics", "gravity", "jump", "2d", "make game"],
        "body": "Use `menu.dynamic` for a game screen. Give objects a `name:` and "
                "move them with `adjvcr(rot, pos, scale, name)` from an `update` "
                "script (runs every frame). Read input with `input.getHeld(\"key\")` / "
                "`input.getClick(\"key\")`. Glass resolves collisions between solid "
                "objects; test overlaps with `adjvcr.detect(a, b)`.",
        "code": 'int x = 0\nint y = 0\n\n*.main {\n    menu.dynamic\n'
                '    vcr.image "player.png" { name: player, size: 32x32 }\n\n'
                '    update {\n'
                '        if (input.getHeld("right") == "1") { x = x + 4 }\n'
                '        if (input.getHeld("left")  == "1") { x = x - 4 }\n'
                '        adjvcr( (0,0,0), (x,y,0), (1,1,1), "player" )\n'
                '    }\n'
                '}',
    },
    {
        "id": "collisions",
        "title": "Collisions & solids",
        "keywords": ["collision", "collide", "solid", "hit", "block", "wall",
                     "overlap", "touch", "physics"],
        "body": "Mark objects `solid: true` and Glass pushes them apart when they "
                "overlap (motion-aware, so fast movers don't tunnel). Use a `detect` / "
                "collision check in update to react (e.g. lose a life, pick up an item).",
        "code": 'vcr.image "wall.png" { name: wall, size: 64x64, solid: true }\n'
                'vcr.image "hero.png" { name: hero, size: 32x32, solid: true }\n\n'
                'update {\n'
                '    if (adjvcr.detect("hero","wall") == "1") { hit = "1" }\n'
                '}',
    },
    {
        "id": "cursor",
        "title": "Cursor control & mouse-look (games)",
        "keywords": ["cursor", "mouse", "pointer", "lock", "hide", "confine",
                     "aim", "fps", "look", "dx", "dy", "camera"],
        "body": "Scripts can control the mouse: `cursor.hide`, `cursor.lock` "
                "(hides + locks it to the center for FPS-style mouse-look), and "
                "`cursor.confine` (keep it in the window); undo with `cursor.show` / "
                "`cursor.unlock`. While locked, read how far the mouse moved this "
                "frame with `mouse.dx` and `mouse.dy`; you also always have "
                "`mouse.x`, `mouse.y`, and `mouse.down` (1 while the left button is "
                "held).",
        "code": 'int yaw = 0\nint pitch = 0\n\n*.main {\n    menu.dynamic\n'
                '    setup { cursor.lock }\n'
                '    update {\n'
                '        yaw = yaw + mouse.dx\n'
                '        pitch = pitch + mouse.dy\n'
                '    }\n'
                '}',
    },
    {
        "id": "audio",
        "title": "Audio & sound",
        "keywords": ["audio", "sound", "music", "play sound", "volume", "sfx",
                     "wav", "mp3", "bitcrush"],
        "body": "Play sounds from your project via the audio API (from a package "
                "call). You can set volume and apply effects like resample/bitcrush "
                "for a retro sound.",
        "code": 'import sfx\n'
                'button "Jump" { call: sfx.jump }   // sfx.py plays a .wav',
    },
    {
        "id": "sprites_editor",
        "title": "Drawing sprites in the editor",
        "keywords": ["sprite", "draw", "paint", "editor", "pixel", "creator",
                     "texture", "pencil", "canvas"],
        "body": "Each element declaration line shows a pencil button in the editor's "
                "right gutter. Click it to open the pixel-art sprite editor (pencil, "
                "eraser, fill, eyedropper). Saving writes sprites/<name>.png and adds "
                "`sprite: \"...\"` to that element automatically.",
        "code": '// click the pencil next to this line in the editor:\n'
                'button "Play" { width: 160, height: 48 }',
    },
]

_STOP = set("a an the is are how do i to my me you can with of in on for and or "
            "what make made making create want need help show tell please glass "
            "code coding write add use using it this that get set some like".split())

_SYN = {
    "colour": "color", "centre": "center", "pic": "image", "picture": "image",
    "photo": "image", "pixelate": "pixel", "pixelated": "pixel", "movie": "video",
    "clip": "video", "container": "holder", "textbox": "input", "field": "input",
    "counter": "int", "sound": "audio", "music": "audio", "sfx": "audio",
    "collide": "collision", "键": "key",
}


def _tok(s):
    out = []
    for w in re.findall(r"[a-z0-9.]+", (s or "").lower()):
        w = _SYN.get(w, w)
        if w and w not in _STOP and len(w) > 1:
            out.append(w)
    return out


def topics():
    return [(t["id"], t["title"]) for t in KB]


# ---- knowledge of the WHOLE language (pulled from the editor's catalog) -----
_CATALOG = None


def _catalog():
    """{word: (desc, example)} for every element/property/action/function."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = {}
        try:
            import editor
            for word, (desc, ex) in editor.COMPLETION_DOCS.items():
                _CATALOG[word] = (desc, ex[0][1] if ex else "")
        except Exception:
            _CATALOG = {}
    return _CATALOG


# ---- composition: turn "make X" into working .glass -------------------------
_FEATURES = {
    "game":   ["game", "player", "move", "movement", "platformer", "fps", "enemy",
               "jump", "shoot", "controls", "wasd", "arrow"],
    "input":  ["input", "form", "textbox", "login", "sign up", "signup", "search",
               "enter text", "type in", "entry"],
    "video":  ["video", "movie", "clip"],
    "audio":  ["audio", "sound", "music", "beep", "sfx"],
    "image":  ["image", "picture", "photo", "sprite", "icon"],
    "link":   ["link", "another page", "next page", "go to", "navigate", "website",
               "open page", "url", "hyperlink"],
    "button": ["button", "click", "press", "tap"],
    "text":   ["text", "label", "title", "heading", "message", "greeting"],
    "holder": ["holder", "container", "panel", "card", "box", "group", "menu"],
    "variable": ["variable", "counter", "score", "state", "live value"],
}


def _detect(qtext):
    ql = " " + qtext.lower() + " "
    feats = set()
    for f, words in _FEATURES.items():
        if any(w in ql for w in words):
            feats.add(f)
    return feats


def compose(question):
    """Assemble a runnable .glass snippet for a 'create/make X' request."""
    feats = _detect(question)
    if not feats:
        return None

    # --- a game (movement) ------------------------------------------------
    if "game" in feats:
        code = ('int x = 0\nint y = 0\n\n'
                '*.main {\n    menu.dynamic\n    background: #0b0d12\n\n'
                '    vcr.image "player.png" { name: player, size: 32x32 }\n\n'
                '    setup { }\n'
                '    update {\n'
                '        if (input.getHeld("right") == "1") { x = x + 4 }\n'
                '        if (input.getHeld("left")  == "1") { x = x - 4 }\n'
                '        if (input.getHeld("down")  == "1") { y = y + 4 }\n'
                '        if (input.getHeld("up")    == "1") { y = y - 4 }\n'
                '        adjvcr( (0,0,0), (x,y,0), (1,1,1), "player" )\n'
                '    }\n}')
        return {"title": "A movable player (game)",
                "body": "A menu.dynamic screen with a named object moved every frame "
                        "by reading the arrow keys and calling adjvcr. Draw player.png "
                        "with the sprite pencil, or swap in your own image.",
                "code": code, "score": 9.0,
                "suggestions": ["Collisions & solids", "Cursor control & mouse-look (games)"]}

    # --- a login / input form --------------------------------------------
    if "input" in feats:
        body_lines = ['    text "Sign in" { color: #6cf09a }',
                      '    input { width: 260 }',
                      '    input { width: 260 }',
                      '    button "Log in" { call: auth.login }']
        code = ('import auth\n\n*.main {\n    menu.full\n    background: #0f1419\n'
                '    center { center }\n\n    holder ( size: 320x220 ) {\n'
                + "\n".join("    " + b for b in body_lines) + "\n    }\n}")
        return {"title": "An input form",
                "body": "A centered holder with input fields and a button that calls a "
                        "Python function (auth.login) in the project. Bind an input to a "
                        "variable to read what was typed.",
                "code": code, "score": 8.5,
                "suggestions": ["Text input fields", "Imports & packages (running Python)"]}

    # --- otherwise assemble a menu from the detected parts ----------------
    parts = []
    if "text" in feats or not feats - {"holder"}:
        parts.append('text "Hello, Glass!" { color: #e7edf3 }')
    if "image" in feats:
        parts.append('vcr.image "pic.png" { size: 160x160 }')
    if "video" in feats:
        parts.append('vcr.video "clip.mp4" { size: 480x270 }')
    if "button" in feats and "audio" in feats:
        parts.append('button "Play sound" { call: sfx.play }')
    elif "button" in feats and "link" in feats:
        parts.append('button "Next page" { action: getGlass, target: page2 }')
    elif "button" in feats:
        parts.append('button "Click me" { action: reload }')
    elif "audio" in feats:
        parts.append('button "Play" { call: sfx.play }')
    if "link" in feats and "button" not in feats:
        parts.append('link "Website" { url: https://example.com }')

    if not parts:
        return None
    imports = "import sfx\n\n" if "audio" in feats else ""
    inner = "\n".join("        " + p for p in parts)
    code = (f'{imports}*.main {{\n    menu.full\n    background: #0f1419\n'
            f'    center {{ center }}\n\n    holder ( size: 480x360 ) {{\n{inner}\n    }}\n}}')
    have = ", ".join(sorted(feats))
    return {"title": "A screen with " + have,
            "body": "A centered holder containing the pieces you asked for. Tweak the "
                    "labels, sizes and colors, and point media/calls at your own files.",
            "code": code, "score": 7.5,
            "suggestions": ["Buttons & actions", "Holders (containers / panels)",
                            "Images & sprites"]}


_CREATE_HINTS = ("create", "make", "build", "how do i", "how to", "set up",
                 "give me", "generate", "add a", "write a", "start a", "i want")


def _score_entry(qset, keywords, title_toks):
    score = 0.0
    for w in qset:
        if w in keywords:
            score += 2.0
        if w in title_toks:
            score += 2.5
        elif any(w in k or k in w for k in keywords):
            score += 0.6
    return score


def answer(question):
    """Answer a question: build code for 'create X', else explain the best match
    from the curated topics AND the full language catalog."""
    ql = (question or "").lower().strip()

    # greetings / meta
    if ql in ("hi", "hey", "hello", "yo", "help", "what can you do", "?"):
        return {"title": "Ask me about Glass",
                "body": "I know the whole Glass language. Ask how something works, what "
                        "a keyword does, or say \u201cmake a\u2026\u201d and I\u2019ll write the code. "
                        "Try a topic:",
                "code": "", "score": 0.0,
                "suggestions": [t["title"] for t in KB[:8]]}

    q = _tok(question)
    qset = set(q)

    # 1) "create / make X" -> compose real code
    if any(h in ql for h in _CREATE_HINTS) or ql.split()[:1] in (["make"], ["create"]):
        built = compose(question)
        if built:
            return built

    # 2) rank curated topics
    ranked = []
    for t in KB:
        s = _score_entry(qset, set(t["keywords"]), set(_tok(t["title"])))
        ranked.append((s, "topic", t))

    # 3) rank the full language catalog (every keyword/function)
    for word, (desc, ex) in _catalog().items():
        kw = set(_tok(word)) | set(_tok(desc))
        s = _score_entry(qset, kw, set(_tok(word)))
        wl = word.lower()
        # they named the keyword directly - only for distinctive words, matched
        # as a whole token (so short words like "do"/"if"/"x" don't hijack English)
        if ("." in wl or len(wl) >= 4) and \
                re.search(r"(?<![a-z0-9])" + re.escape(wl) + r"(?![a-z0-9])", ql):
            s += 4.0
        if s > 0:
            ranked.append((s, "cat", (word, desc, ex)))

    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked or ranked[0][0] < 1.0:
        # nothing matched - maybe still buildable
        built = compose(question)
        if built:
            return built
        return {"title": "I'm not sure yet",
                "body": "Try naming a keyword (button, holder, cursor.lock, adjvcr\u2026), "
                        "or say \u201cmake a\u2026\u201d and I\u2019ll build it. Topics I know:",
                "code": "", "score": 0.0,
                "suggestions": [t["title"] for t in KB[:8]]}

    top = ranked[0]
    # gather a few different suggestions
    sugg = []
    for s, kind, obj in ranked[1:6]:
        if s <= 0:
            break
        name = obj["title"] if kind == "topic" else obj[0]
        if name not in sugg:
            sugg.append(name)

    if top[1] == "topic":
        t = top[2]
        return {"title": t["title"], "body": t["body"], "code": t["code"],
                "score": top[0], "suggestions": sugg[:3]}
    word, desc, ex = top[2]
    return {"title": word, "body": desc, "code": ex, "score": top[0],
            "suggestions": sugg[:3]}
