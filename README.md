# Glass

A transparent, no-AI, no-ads, fully scriptable browser shell, plus a custom
code editor with **live preview** for building its UI in your own `.glass`
language. Everything is visible: full page source, a live log of every network
request, and a UI you define and restyle yourself.

## What it is (one honest caveat)

A from-scratch HTML/CSS/JS rendering engine is millions of lines, so Glass
renders real pages with Chromium (via Qt WebEngine). **Everything around the
engine is yours**: no telemetry, no AI, real request blocking, always-on
source/network visibility, and a UI system driven by editable `.glass` files
and your own `api.py`.

## Run it (Windows)

1. Install Python 3.10+ (check **Add to PATH**).
2. Double-click **`run.bat`** (or run `python launch.py`).

The **first launch installs everything automatically** — if PyQt6 /
PyQt6-WebEngine are missing, Glass pip-installs them and then opens. That check
runs on every start, so if a dependency ever goes missing it self-heals next
time. On macOS / Linux use **`./run.sh`** (or `python3 launch.py`).

The **editor** opens from the browser's **Edit** button, with **Ctrl+E**, or by
running `python editor.py`. Put the browser on one half of your screen and the
editor on the other — the editor's right pane previews your UI as you type.

## The editor

- Left: a VS-Code-style code view (line numbers, syntax highlighting).
- Right: a live preview that re-renders on every keystroke; drag moveable
  menus, toggle holders, and see fonts/scale applied for real.
- A **Tutorial** (top-right, auto-opens on first launch) walks through the
  whole language: menus, text, fonts, custom buttons, holders/pages, scale,
  full-screen mains, center, variables, conditions, every built-in action, and
  how to add and call your own action in `api.py`.
- An **Edits** panel (top-right, next to the tutorial) has a colour wheel:
  click to read a colour's hex and RGBA, copy it, or insert it at your cursor.
  Hex values in the code (like `#6cf09a`) show a colour underline beneath them.
- Open/Save works against the `ui/` folder; saving there hot-reloads the
  running browser.

## The Glass language

Every definition has the same shape:

```
scope.component ( traits ) {
    property: value          // one per line, or comma-separated
    element "Label" { ... }
}
```

- **scope** — `*` (all sites) or a domain like `example.com`.
- **component** — `menu`, `holder`, `panel`, or `bar`.
- **traits** — flags: `moveable`, `resizable`, `closable`, `pinned`, `hidden`,
  `remember`. Your namespaced form (`menu.moveable`) also works — the last word
  is the trait. Add `remember` (or `persist`) so a menu keeps its last dragged
  position across page reloads and UI reloads instead of snapping back to its
  `x`/`y`. A `closable` menu gets an X button; once you close it, it **stays
  closed in every tab** (including new ones) until you reload the UI
  (**Ctrl+Shift+U** / the `reloadui` action), which brings closed panels back.

Your original line still parses: `websitename.menu ( menu.moveable ) {}`

### text

```
text "Hello" { color: #6cf09a, width: 200, height: 30, backgroundcolor: #111, font: Heading }
```

`font` may be a textgroup name or a font family directly.

### input (search/address box)

```
input "Search or enter address" { width: 300, height: 34, font: UI }
```

With no `width`, the box stretches to fill its container — so marking the menu
`resizable` and dragging its corner grip resizes the search box too. Pressing
Enter runs the text through the address bar (URL or search).

### textgroups (named fonts)

Declared at the top of a menu or holder, then referenced by name:

```
textgroup { Heading, timesnewroman }
textgroup { Body, arial }

text "Title" { font: Heading }
```

### buttons (now customizable)

Simple form:

```
button "Reload" { action: reload }
```

Styled form — a style block, then the name, then the action:

```
button { { color, height, width, textgroup } "Name" } { action }
```

```
button { { color: #1e88e5, width: 160, height: 34, textgroup: Body } "Open" } { action: toggle, target: page2 }
```

`color` is the background; `textgroup` picks the font. A button's action is one
of `action: <name>` (a method in `api.py`), `url: <address>`, or `js: <code>`.

### holders (toggleable pages)

A holder is a sub-window inside a menu. Configure it in its parentheses:

```
holder ( size, backgroundColor, opacity, outline, outlinecolor, outlineThickness ) { }
```

`outline` is a boolean — if `false`, the outline color/thickness are ignored.

Add the `autosize` trait (or `size: auto`) so a holder grows to fit its
contents instead of being pinned to a fixed box — use it whenever a page has
enough buttons/text to overflow. With `autosize`, a `size: 230` value sets only
the width and the height follows the content:

```
holder ( autosize, size: 230, outline: true, outlinecolor: #6cf09a, hidden: true ) { }
```

(If you give a full `size: WxH` without `autosize`, that size is treated as a
*minimum* — the holder still grows rather than clipping its content.) When a
holder is toggled on, its menu automatically expands to make room, so pages
never draw on top of the controls below them.

Add the `scroll` trait (with a `size`) to turn a holder into a scroll box for
lots of text or data:

```
holder ( scroll, size: 300x160 ) { ...many elements... }
```
Give a holder a `name`, start it `hidden: true`, and toggle it from a button to
build multi-page menus:

```
*.menu ( moveable ) {
    button "Settings" { action: toggle, target: settings }

    holder ( size: 240x160, outline: true, outlinecolor: #6cf09a, outlineThickness: 2, hidden: true ) {
        name: settings
        title: Settings
        button { { color: #e53935 } "Back" } { action: toggle, target: settings }
    }
}
```

You can also place a nested `menu` inside a holder.

### scale

Zoom a whole menu or holder — the box **and** everything inside it (text,
buttons, the title) — like resizing a window:

```
scale { 1.5 }              // 1.5x everything; 1 = normal
```

A single number is the simplest. You can pass up to three
(`scale { width, height, content }`) if you ever want to stretch the box on one
axis, but any value you leave out just repeats the last one, so equal values
(the usual case) zoom uniformly.

### main (full-screen home page)

A `menu` floats; a `main` can fill the whole screen like a website homepage.
Put `menu.full` inside for full-screen, or `menu.ui` for a normal draggable
menu:

```
*.main {
    menu.full
    background: #0d1117      // or  image: bg.png  (looked up in projects/ or ui/)
    center { center }
    title: My Home Page
    text "Welcome" { color: #6cf09a }
    button "Enter" { action: reload }
}
```

A full-screen main covers everything behind it and resizes with the window;
your floating menus still sit on top. The launch home screen is exactly this:
**`ui/home.glass`** is a full-screen `main` you can edit freely. It shows only
on the start page (and when you hit Home), never on top of a real website.

### center (positioning)

Aligns content in a full-screen `main`, or inside a holder. Put it at the top:

```
center { center }       // left  right  up  down
center { topleft }      // topright  bottomleft  bottomright
center { topcenter }    // bottomcenter
```

A single `text` or `button` can also align just itself with a `center:`
property (`left` / `right` / `center`), independent of the container:

```
text "left bound"  { width: 300, center: left }
button "to the right" { action: reload, center: right }
```

### variables

Declare values Unity-style at the top of a script (or inside a container) - no
semicolons:

```
bool ShowNews = true
int  Count    = 3
float Speed   = 1.5
```

Types: `int`, `float`, `double`, `bool` (alias `boolean`). A bare `bool Flag`
defaults to `false`.

**Changing variables (Unity-style math).** A button can update a variable with
`set:`, and the UI re-renders so conditions and displayed values update:

```
button "Add"    { set: Count += 1 }    // also  -=  *=  /=  =
button "Double" { set: Count *= 2 }
```

Show a variable's value anywhere in text with `{braces}`:

```
text "Score: {Count}"
```

**One button, many objects.** `show`/`hide`/`toggle` take several space-separated
targets, and `do:` runs a `;`-separated sequence so a single button can enable
some things and disable others:

```
button "Open all"  { action: show, target: news settings help }
button "Swap pages" { do: hide pageA; show pageB }
```

### grab

Open a named holder on load, but only if a boolean is true. Put it at the start
of the script:

```
grab { "news", ShowNews }    // shows holder "news" only when ShowNews is true
```

### if / else

Branch on variables two ways. **Inline** - `if:` on any element shows it only
when the condition is true:

```
button "Admin" { action: reload, if: IsAdmin }
```

**Blocks** - `if` / `else if` / `else` wrap a group of entries; the winning
branch's properties and elements merge into the surrounding container:

```
if (Count > 5) {
    text "many" { color: #f00 }
} else if (Count == 3) {
    text "three" { color: #0f0 }
} else {
    text "few" { color: #00f }
}
```

Operators: `==  !=  >  <  >=  <=` plus `&&  ||  !` and parentheses. A bare name
(`if: ShowNews`) just tests truthiness.

## The API — adding your own actions

`api.py` defines every `action:` name. Add a method (with a one-line docstring,
which the tutorial displays), then reference it by name:

```python
def hello(self):
    """Print a greeting to the log."""
    self.window.log("Hello from my own action!")
```

```
button "Say hi" { action: hello }
```

Loop: **edit `api.py` → reference by name → reload UI** (Ctrl+Shift+U).

Built-in actions: `navigate, reload, back, forward, stop, home, newtab,
closetab, zoomin, zoomout, zoomreset, viewsource, showlog, devtools,
toggleblock, stripads, js, reloadui, openeditor, wiki, quit`, plus the holder
controls `toggle, show, hide` (each takes one or more `target` names) and the
variable helpers behind `set:`/`do:`.

A **built-in wiki** (`projects/wiki.glass`) documents the whole language inside
a scrollable page; open it with the **Wiki** button in the menu/home screen
(action `wiki`), and **Back to menu** returns you.

## Projects, getGlass &amp; sharing

A **project** is a folder under **projects/** that holds one or more `.glass`
scripts plus a `project.json` manifest — the file the server reads to know the
project's name, its scripts, and the entry script:

```
projects/
  MyProject/
    project.json     <- manifest (name, entry, scripts)
    home.glass       <- entry script
    page2.glass
```

**In the editor**, you pick or create a project on launch (a Unity-style hub:
*Create* a new one, or *Open* an existing one). The dropdown lists the scripts
in the open project; **Project…** switches projects. Saving a script updates the
manifest automatically.

**Linking scripts** is done with the `getGlass` action, which loads another
script **from the same project only** — it never reaches into a different
project. Add `openNew` to control where it opens (`false` = this tab, the
default; `true` = a new tab):

```
button "Page 2" { action: getGlass, target: page2, openNew: false }
```

Tabs work like any browser: the **+** on the tab bar (or **Ctrl+T**) opens a
new one, **Ctrl+W** closes the current one, and each tab keeps its own `.glass`
UI, so `openNew: true` genuinely opens the page beside the current one.

**In the address bar:**

| type this | what happens |
|---|---|
| `MyProject/my` | open the project and load its entry script |
| `MyProject/serv` | open the project and host its folder on the network |
| `home.glass/my` | load a single `.glass` (current project, else projects/) |
| `home.glass/serv` | host the current project (or projects/) folder |
| `192.168.1.5:8765/home.glass` | load a `.glass` served by another PC |

When you `/serv`, the log prints your address (e.g. `192.168.1.5:8765`) and the
exact line others type to reach you. It's plain HTTP on your LAN — handy for
PC-to-PC sharing, not meant to face the open internet. Loading a project
replaces the on-screen UI; hit **UI** (Ctrl+Shift+U) to return to your ui/
folder.

## Ad / tracker blocking

- `blocklist.txt` — one domain per line, or paste any hosts-format list.
  Subdomains are blocked automatically.
- `cosmetic.css` — selectors for ad containers to hide on every page.
- Toggle live with the green pill in the toolbar (shows a running kill count).

## Privacy (what Chromium is and isn't doing)

Glass renders pages with Qt's build of Chromium, which already leaves out the
parts of Chrome people worry about: there's **no Google sign-in or sync, no
usage/telemetry reporting, no field-trial machinery, and no AI**. On top of that
Glass hardens the engine:

- **No phoning home.** Background networking, component/"optimization-hints"
  downloads, domain-reliability uploads, crash reporting, Safe-Browsing lookups,
  autofill-server calls, hyperlink-audit pings and network prediction are all
  turned off at startup.
- **No cross-site tracking cookies.** Third-party cookies are blocked; only
  first-party cookies (so your logins still work) are kept.
- **No silent sensor access.** Camera, microphone, geolocation and notification
  requests are denied by default (each denial is printed to the log).
- **No local-IP leak.** WebRTC is restricted so pages can't discover your
  internal IP addresses; JS can't read your clipboard without a gesture.
- **Privacy signals sent.** Every request carries `DNT: 1` and `Sec-GPC: 1`.
- **No spell-check dictionary downloads.**
- **Panic button.** "Clear my data" in the menu (or **Ctrl+Shift+Del**, or the
  `cleardata` action) wipes all cookies and the cache immediately.

Honest limits: this is best-effort hardening, not a formal audit, and it can't
stop a website from trying to fingerprint or track you with JavaScript — that's
what the ad/tracker blocking and denied permissions are for. For anonymity at
the network level (hiding your IP from sites/ISP) you'd still want a VPN or Tor;
Glass doesn't route your traffic.

## Performance / memory

Glass wraps Chromium, which is memory-hungry by default, so a few things keep it
lean:

- **Background-tab discarding.** Only the few most-recently-used tabs keep a live
  renderer process (default **3**, set by `max_live_tabs` in `browser.py`).
  Older tabs are put to sleep — their renderer is freed and they drop to almost
  zero memory, then reload when you click them. This means 10 (or 50) open tabs
  cost about the same as 3, comfortably under a gigabyte on typical sites.
- **Low-end Chromium flags** are set before the engine starts: low-end-device
  mode, process-per-site (tabs on the same site share a renderer), a renderer
  process cap, and a small 50 MB disk cache.
- Ad/tracker blocking also cuts memory by not loading junk in the first place.

To keep even more tabs warm (at the cost of RAM), raise `max_live_tabs`; to be
more aggressive, lower it to 2 or 1.

## Files

| file | what it is |
|---|---|
| `browser.py` | the browser: tabs, engine, chrome, transparency tools, tab discarding |
| `launch.py` | start here — installs missing deps on first run, then opens Glass |
| `bootstrap.py` | first-run / missing-dependency auto-installer |
| `tabbudget.py` | decides which background tabs to sleep (memory) |
| `editor.py` | the `.glass` editor: highlighting, live preview, tutorial, projects |
| `dsl.py` | parser for the Glass language |
| `renderer.py` | turns parsed rules into widgets (menus, holders, buttons, text) |
| `api.py` | **your action surface — edit this to extend** |
| `project.py` | project folders + `project.json` manifest helpers |
| `adblock.py` | request interceptor + cosmetic filtering |
| `glassnet.py` | LAN server + fetch for sharing `.glass` files PC-to-PC |
| `blocklist.txt` / `cosmetic.css` | editable block rules |
| `ui/*.glass` | **your auto-loaded UI definitions — edit these** |
| `projects/<Name>/` | **a project: scripts + `project.json` manifest** |

## Keyboard

Browser: `Ctrl+U` source · `Ctrl+L` log · `Ctrl+R` reload · `Ctrl+T`/`Ctrl+W`
tab · `F12` devtools · `Ctrl+Shift+U` reload UI · `Ctrl+E` editor.

## Formatting note

Inside a `{ }` block, put one property/element per line, or separate them with
commas. Values containing commas should be wrapped in `"quotes"`.
