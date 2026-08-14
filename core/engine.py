"""
Glass runtime engine
====================
The pieces that make the `vcr.*` objects and the `update { }` script come alive:
a per-frame Time, keyboard Input state, a simple Physics setting, the VCR object
model, and AABB collision + detection. This module is pure Python (no Qt) so it
can be unit-tested on its own; the renderer attaches a Qt widget to each object
and the browser drives the frame loop.
"""

from __future__ import annotations
import itertools


def _norm_key(k):
    s = str(k).strip().lower()
    aliases = {"space": " ", "spacebar": " ", "esc": "escape",
               "left": "arrowleft", "right": "arrowright",
               "up": "arrowup", "down": "arrowdown"}
    return aliases.get(s, s)


class Time:
    """time.normal = real per-frame delta (varies with fps, like deltaTime).
       time.held   = a fixed step that never changes (like fixedDeltaTime)."""
    def __init__(self, target_fps=60.0):
        step = 1.0 / float(target_fps)
        self.normal = step
        self.held = step

    def tick(self, dt):
        # clamp to avoid huge jumps after a stall (e.g. while resizing the window)
        self.normal = max(0.0, min(float(dt), 0.05))


class InputState:
    """input.GetHeld(k) is true while a key is down; input.GetClick(k) is true
       only on the first frame it goes down."""
    def __init__(self):
        self._held = set()
        self._clicked = set()

    def key_down(self, key):
        k = _norm_key(key)
        if k not in self._held:
            self._clicked.add(k)
        self._held.add(k)

    def key_up(self, key):
        self._held.discard(_norm_key(key))

    def get_held(self, key):
        return _norm_key(key) in self._held

    def get_click(self, key):
        return _norm_key(key) in self._clicked

    def end_frame(self):
        # a click lasts exactly one processed frame
        self._clicked.clear()


class Physics:
    def __init__(self):
        self.gravity = 0.0     # acceleration value the user sets (px/s, roughly)


class VCRObject:
    """An image/gif/video/collider placed by a vcr.* element, addressed by svc."""
    def __init__(self, svc, kind="image"):
        self.svc = str(svc)
        self.kind = kind                 # image | gif | video | colide
        self.x = 0.0
        self.y = 0.0
        self.rot = 0.0                   # degrees
        self.sx = 1.0
        self.sy = 1.0
        self.w = 0.0                     # base draw size (px)
        self.h = 0.0
        self.collider = None             # (cw, ch) or None
        self.friction = 0.0
        self.istrigger = False
        self.tag = ""                    # group label - see properties.get.tag(s)
        self.widget = None               # Qt widget, set by the renderer
        self._px = 0.0                   # previous-frame position (for resolution)
        self._py = 0.0
        self.vx = 0.0                    # velocity (physics.push) - px per frame
        self.vy = 0.0
        self._hitwall = False            # set when a moving object stops at a wall

    def aabb(self):
        cw, ch = self.collider if self.collider else (self.w, self.h)
        return (self.x, self.y, cw * self.sx, ch * self.sy)

    def has_collider(self):
        return self.kind == "colide" or self.collider is not None


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (ax < bx + bw and ax + aw > bx and
            ay < by + bh and ay + ah > by)


def _overlap_pad(a, b, pad):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (ax - pad < bx + bw and ax + aw + pad > bx and
            ay - pad < by + bh and ay + ah + pad > by)


_ram_monitor_timer = None   # the one live #overrideOPLim RAM monitor, if any -
                            # tracked at module level so re-rendering a script
                            # (e.g. live preview on every keystroke) stops the
                            # previous timer instead of stacking a new one


class World:
    def __init__(self, target_fps=60.0):
        self.objects = {}                # svc -> VCRObject
        self.time = Time(target_fps)
        self.input = InputState()
        self.physics = Physics()
        self._overlaps = set()           # frozenset({svcA, svcB}) currently touching
        self.vars = {}                   # shared script variables (= UI variables)
        self.setup_script = ""           # run once
        self.update_script = ""          # run every frame
        self._setup_done = False
        self.dynamic = False             # menu.dynamic: scale objects to fit at startup
        self.host = None                 # the panel objects live on (set by renderer)
        self._fit_bounds = None          # initial content bounds (captured once)
        self._fit_size = None            # host size we last fitted to
        self.view_scale = 1.0            # display-only zoom (physics stays authored)
        self.view_offx = 0.0
        self.view_offy = 0.0
        self.cursor = {"hide": False, "lock": False, "confine": False}
        self.mouse = {"x": 0.0, "y": 0.0, "dx": 0.0, "dy": 0.0, "down": False}
        self.snippets = {}               # name -> {"params": [(type,name)], "body": src}
        self._snip_depth = 0             # guard against runaway snippet recursion
        self.timers = []                 # scheduled after{} blocks: {"at": float, "body": [...]}
        self.spawn_queue = []            # runtime create() requests for the renderer
        self.burst_queue = []            # runtime particle burst() requests
        self.particles_3d = []           # {x,y,vx,vy,age,life,color,size,gravity} in
                                          # WORLD space - drawn by any RaycasterWidget
                                          # using its own camera projection, so they
                                          # correctly appear at the right screen spot,
                                          # scale with distance, and hide behind walls
        self.emitters_3d = []            # continuous particleSystem(mode:3d) sources
        self.active_3d_sounds = {}       # sound_id -> {x,y,radius,base_volume} - lets
                                          # 3D audio's VOLUME keep updating every frame
                                          # as the listener moves (see audio_3d_step)
        self._sound_field = None         # cached {(cx,cy): path_dist} from the
        self._sound_field_cell = None    # listener's cell, see sound_path_distance()
        self._light_owner = {}           # light_id -> maze, so setColor/destroy/
        self._next_light_id = 1          # setPos don't need the mazeId repeated -
                                          # see light.create/dynamic_lights_step
        self.post_profiles = {}          # name -> {cache, quality, effects} - every
                                          # *.postEffect rule, compiled once by
                                          # render_rules regardless of whether it's
                                          # ever loaded
        self.active_post = None          # the currently loaded profile's runtime
                                          # state (see loadPost/removePost/postQuality)
        self._compiling_post = None      # non-None only while render_rules is
                                          # compiling an effect{} block - see _kwcall
        self.destroy_queue = []          # objects removed at runtime (renderer cleans widgets)
        self.mazes = {}                  # mazeID -> raycaster (for mesh collision)
        self.meshes = {}                 # meshID -> imported MeshData (glassmesh)
        self.nav_agents = {}             # svc -> {maze, path, i, speed, arrive, done}
        self._reachable_cache = {}       # (mazeID, srcCell, tgtCell) -> bool, see
                                          # nav_reachable - walls never move at runtime
                                          # (no API mutates maze.grid), so this is a
                                          # pure memo, never invalidated
        self._spawn_n = 0
        # ---- #overrideOPLim (see apply_override_limits) --------------------
        self.use_cpu = False        # compile-time only - raises simulation budgets
        self.use_gpu = False        # reserved: no GPU rendering path exists yet (see below)
        self.use_a_ram = False      # compile-time only - turns on live RAM monitoring
        self.ram_cap_bytes = None   # the declared ramAllocated cap, or None if unset
        self.ram_over_limit = False   # live: only updated while use_a_ram is on
        self.meets_ram_requirement = True   # dUMR - system RAM vs ramAllocated cap

    def apply_override_limits(self, limits):
        """Interpret a parsed #overrideOPLim {...} dict (dsl.py). Called once
        when a script's Document is turned into a World (see renderer.py).

        ramAllocated is a CAP on how much memory THIS script is allowed to
        use, not a reservation - Glass never pre-allocates memory on a
        script's behalf. Pre-allocating wouldn't make anything run faster
        (that's not how Python/Qt memory management works) and would just be
        a ready-made way for a shared/downloaded .glass file to eat someone's
        RAM. dUMR (does-user-meet-requirement) compares the CAP against the
        system's actual total RAM, once, at load. If use_a_ram is also on,
        Glass separately watches this process's live usage against the same
        cap the whole time the script runs, and exposes ram.overLimit so the
        script can react (show a warning, stop spawning more things, etc.) -
        it never force-kills or throttles anything on its own.

        use_cpu/use_gpu/use_a_ram are read-only once parsed: they live as
        plain World attributes, not in self.vars, so an ordinary `useCPU = x`
        assignment in a script just creates an unrelated same-named variable
        - it can never reach these. There is no adjustable-at-runtime path.

        use_gpu is currently a reserved no-op: Glass's renderer (raycaster,
        mesh viewer) is pure software, there is no GPU-accelerated rendering
        path to turn on yet. It's parsed and stored so scripts/tools can
        check it, but it doesn't change anything today."""
        import hardware
        global _ram_monitor_timer
        if _ram_monitor_timer is not None:
            _ram_monitor_timer.stop()
            _ram_monitor_timer = None

        def _flag(name):
            return str(limits.get(name, "false")).strip().lower() in ("true", "1", "yes")

        self.use_cpu = _flag("useCPU")
        self.use_gpu = _flag("useGPU")
        self.use_a_ram = _flag("useARam")
        self.ram_cap_bytes = hardware.parse_ram_spec(limits.get("ramAllocated"))

        if self.ram_cap_bytes is None:
            self.meets_ram_requirement = True     # nothing declared -> nothing to fail
        else:
            total = hardware.total_ram_bytes()
            self.meets_ram_requirement = total is None or total >= self.ram_cap_bytes

        if self.use_a_ram and self.ram_cap_bytes is not None:
            from PyQt6.QtCore import QTimer
            timer = QTimer()

            def _sample(w=self):
                used = hardware.process_ram_bytes()
                if used is not None and w.ram_cap_bytes:
                    w.ram_over_limit = used > w.ram_cap_bytes
            timer.timeout.connect(_sample)
            timer.start(2000)          # every 2s - live RAM doesn't need frame-rate precision
            _ram_monitor_timer = timer
            _sample()                  # first reading immediately, not 2s from now

    def create(self, sprite, x=0.0, y=0.0, w=0.0, h=0.0):
        """Spawn a new sprite object at runtime (like Unity's Instantiate).
        Returns the new object's name so scripts can adjvcr / detect it."""
        self._spawn_n += 1
        svc = "spawn_%d" % self._spawn_n
        obj = VCRObject(svc, "image")
        obj.w = w or 32.0
        obj.h = h or 32.0
        obj.x = float(x)
        obj.y = float(y)
        obj.sprite = str(sprite)
        self.objects[svc] = obj
        self.spawn_queue.append({"svc": svc, "sprite": str(sprite),
                                 "w": obj.w, "h": obj.h})
        return svc

    def spawn_object(self, look, x=0.0, y=0.0, collide=False, scale=1.0, opacity=1.0):
        """Spawn a raycast billboard (Doom-style sprite) at runtime. `look` is a
        colour like '#ff5d5d' or a sprite path. Shows up in the 3D view; no widget."""
        self._spawn_n += 1
        svc = "spawn_%d" % self._spawn_n
        obj = VCRObject(svc, "raycastobject")
        obj.x = float(x)
        obj.y = float(y)
        obj.collide = bool(collide)
        obj.rc_scale = float(scale) or 1.0
        obj.rc_opacity = float(opacity)
        look = str(look)
        if look.startswith("#"):
            obj.rc_color = look
            obj.sprite = None
        else:
            obj.sprite = look
            obj.rc_color = None
        obj.name = svc
        self.objects[svc] = obj
        return svc

    def destroy(self, svc):
        """Remove an object at runtime (enemy dies, pickup taken, ...)."""
        obj = self.objects.pop(_svc_str(svc), None)
        if obj is not None:
            self.destroy_queue.append(obj)          # renderer deletes any widget
            return 1.0
        return 0.0

    def clone(self, svc, x=None, y=None):
        """Duplicate an existing object (a 'prefab') at a new position, copying its
        look/scale/collide. Returns the new object's svc. Great for spawning waves."""
        src = self.get(svc)
        if src is None:
            return 0
        self._spawn_n += 1
        new = "spawn_%d" % self._spawn_n
        obj = VCRObject(new, src.kind)
        obj.x = float(x) if x is not None else src.x
        obj.y = float(y) if y is not None else src.y
        obj.rot = src.rot
        obj.sx, obj.sy = src.sx, src.sy
        obj.w, obj.h = src.w, src.h
        obj.collider = src.collider
        obj.friction = src.friction
        obj.istrigger = src.istrigger
        obj.tag = getattr(src, "tag", "")
        obj.collide = getattr(src, "collide", False)
        obj.rc_scale = getattr(src, "rc_scale", 1.0)
        obj.rc_color = getattr(src, "rc_color", None)
        obj.rc_opacity = getattr(src, "rc_opacity", 1.0)
        obj.sprite = getattr(src, "sprite", None)
        obj.name = new
        self.objects[new] = obj
        if src.kind != "raycastobject" and obj.sprite:
            self.spawn_queue.append({"svc": new, "sprite": obj.sprite,
                                     "w": obj.w, "h": obj.h})
        return new

    # ---- object registry ---------------------------------------------------
    def add(self, obj):
        obj._world = self
        self.objects[obj.svc] = obj
        return obj

    def get(self, svc):
        return self.objects.get(_svc_str(svc))

    def clear(self):
        self.objects.clear()
        self._overlaps.clear()

    # ---- adjVCR ------------------------------------------------------------
    def adjust(self, svc, rot=(0, 0, 0), pos=(0, 0, 0), scale=None):
        o = self.get(svc)
        if o is None:
            return
        if rot:
            o.rot += float(rot[-1])              # last component = 2D spin (deg)
        if pos:
            o.x += float(pos[0])
            o.y += float(pos[1]) if len(pos) > 1 else 0.0
        if scale is not None and scale:
            o.sx = float(scale[0])
            o.sy = float(scale[1]) if len(scale) > 1 else float(scale[0])

    # ---- collision ---------------------------------------------------------
    def compute_overlaps(self):
        self._overlaps.clear()
        colliders = [o for o in self.objects.values() if o.has_collider()]
        for a, b in itertools.combinations(colliders, 2):
            if _overlap_pad(a.aabb(), b.aabb(), 3.0):
                self._overlaps.add(frozenset((a.svc, b.svc)))

    def detect(self, a, b=None):
        """Live overlap test at the moment it's called (no frame lag).
        detect(svc) -> touching anything; detect(svcA, svcB) -> those two."""
        oa = self.get(a)
        if oa is None or not oa.has_collider():
            return False
        if b is None:
            for o in self.objects.values():
                if o is oa or not o.has_collider():
                    continue
                if _overlap_pad(oa.aabb(), o.aabb(), 3.0):
                    return True
            return False
        ob = self.get(b)
        if ob is None or not ob.has_collider():
            return False
        return _overlap_pad(oa.aabb(), ob.aabb(), 3.0)

    # ---- frame -------------------------------------------------------------
    def begin_frame(self, dt):
        self.time.tick(dt)
        for o in self.objects.values():       # remember where things were
            o._px, o._py = o.x, o.y

    def resolve_collisions(self):
        """Push apart overlapping solid (non-trigger) colliders so they block.
        Resolution uses each mover's previous position + direction of travel so
        it always pushes out the side it entered from (no popping through walls)."""
        solids = [o for o in self.objects.values()
                  if o.has_collider() and not o.istrigger]
        for i in range(len(solids)):
            for j in range(i + 1, len(solids)):
                self._resolve_pair(solids[i], solids[j])

    def _resolve_pair(self, A, B):
        a_moved = abs(A.x - A._px) > 1e-6 or abs(A.y - A._py) > 1e-6
        b_moved = abs(B.x - B._px) > 1e-6 or abs(B.y - B._py) > 1e-6
        if not a_moved and not b_moved:
            return
        ax, ay, aw, ah = A.aabb()
        bx, by, bw, bh = B.aabb()
        ox = min(ax + aw, bx + bw) - max(ax, bx)
        oy = min(ay + ah, by + bh) - max(ay, by)
        if ox > 0 and oy > 0:                       # overlapping now -> push out
            if a_moved and not b_moved:
                self._push_out(A, B)
            elif b_moved and not a_moved:
                self._push_out(B, A)
            else:
                self._push_out(A, B)                # both moving: resolve A vs B
            return
        # no overlap now: a fast mover may have passed clean through a thin solid
        # this frame (tunneling, e.g. a big dt after a resize stall). Sweep it back.
        if a_moved:
            self._sweep(A, B)
        if b_moved:
            self._sweep(B, A)

    def _push_out(self, M, O):
        """Resolve mover M out of solid O, choosing the axis it crossed and the
        side it came from (using M's previous position and its movement)."""
        mx, my, mw, mh = M.aabb()
        ox, oy, ow, oh = O.aabb()
        overlap_x = min(mx + mw, ox + ow) - max(mx, ox)
        overlap_y = min(my + mh, oy + oh) - max(my, oy)
        if overlap_x <= 0 or overlap_y <= 0:
            return
        prev_x = (M._px < ox + ow) and (M._px + mw > ox)   # already overlapping on X before?
        prev_y = (M._py < oy + oh) and (M._py + mh > oy)   # ...on Y?
        if prev_x and not prev_y:
            axis_y = True                       # came in vertically -> resolve Y
        elif prev_y and not prev_x:
            axis_y = False                      # came in horizontally -> resolve X
        else:
            axis_y = overlap_y < overlap_x      # fallback: least penetration
        if axis_y:
            if M.y > M._py:                     # moving down -> rest on top
                M.y = oy - mh
            elif M.y < M._py:                   # moving up -> hit underside
                M.y = oy + oh
            else:
                M.y += (-overlap_y if (my + mh / 2) < (oy + oh / 2) else overlap_y)
        else:
            if M.x > M._px:                     # moving right -> stop at left face
                M.x = ox - mw
            elif M.x < M._px:                   # moving left -> stop at right face
                M.x = ox + ow
            else:
                M.x += (-overlap_x if (mx + mw / 2) < (ox + ow / 2) else overlap_x)

    def _sweep(self, M, O):
        """If mover M's path crossed solid O this frame, snap M to O's surface."""
        mx, my, mw, mh = M.aabb()
        ox, oy, ow, oh = O.aabb()
        horiz = mx < ox + ow and mx + mw > ox
        vert = my < oy + oh and my + mh > oy
        if horiz:                              # check vertical pass-through
            prev_bottom = M._py + mh
            if prev_bottom <= oy and M.y + mh >= oy:        # fell through the top
                M.y = oy - mh
                return
            if M._py >= oy + oh and M.y <= oy + oh:         # rose through the bottom
                M.y = oy + oh
                return
        if vert:                               # check horizontal pass-through
            prev_right = M._px + mw
            if prev_right <= ox and M.x + mw >= ox:         # crossed left edge
                M.x = ox - mw
                return
            if M._px >= ox + ow and M.x <= ox + ow:         # crossed right edge
                M.x = ox + ow

    def run_setup_once(self):
        if not self._setup_done:
            ac = getattr(self, "audio", None)
            if ac is not None:
                # closes a real race: _render_ui() can fire multiple times in
                # quick succession (browser.py calls it from 5 different
                # places), each with its own stop_all()+new-World cycle, but
                # setup{} only actually runs later via the timer-driven
                # _frame() tick. If that tick lands between two renders, an
                # intermediate World's setup can start a sound that the
                # LATER render's stop_all() already ran past, before it
                # existed - leaving it stuck playing alongside whatever
                # starts next. Stopping right here, immediately before this
                # World's own setup runs, closes that gap completely.
                try:
                    ac.stop_all()
                except Exception:
                    pass
            if self.setup_script:
                run_script(self.setup_script, self)
            self._setup_done = True

    def run_update(self):
        if self.update_script:
            run_script(self.update_script, self)
        self.nav_step()
        self.physics_step()
        self.particles_3d_step()
        self.audio_3d_step()
        self.dynamic_lights_step()
        self.post_effects_step()

    def particles_3d_step(self):
        """Age/move every 3D particle in world space, and top up any
        continuous 3D emitters. Frame-rate independent (scaled by
        time.normal, same convention as physics.gravity).

        Particles carry a real height axis (z, world pixels above the
        floor) separate from x/y (the flat top-down plane) - gravity pulls
        z down, not y, so it actually looks like falling in the first-person
        view instead of drifting sideways across the map. direction/spread
        pick a point on a sphere (elevation from direction+-spread/2, a
        random full-circle azimuth for the horizontal component) rather
        than the flat-plane-only spray the original 2D particles used."""
        import math, random
        dt = self.time.normal
        if self.emitters_3d:
            for em in self.emitters_3d:
                em["_acc"] = em.get("_acc", 0.0) + em["rate"] * dt
                while em["_acc"] >= 1.0:
                    em["_acc"] -= 1.0
                    elev = math.radians(em["direction"] + random.uniform(-em["spread"] / 2, em["spread"] / 2))
                    az = random.uniform(0, 2 * math.pi)
                    sp = em["speed"] * random.uniform(0.5, 1.0)
                    sp_h = math.cos(elev) * sp
                    self.particles_3d.append({
                        "x": em["x"], "y": em["y"], "z": em.get("z", 0.0),
                        "vx": math.cos(az) * sp_h, "vy": math.sin(az) * sp_h,
                        "vz": -math.sin(elev) * sp,
                        "age": 0.0, "life": em["life"] * random.uniform(0.7, 1.05),
                        "color": em["color"], "size": em["size"], "gravity": em["gravity"],
                        "bounceA": em.get("bounceA", 0.0),
                        "sizeOverLife": em.get("sizeOverLife", 0.0),
                        "lit": em.get("lit", False),
                    })
        if not self.particles_3d:
            return
        mazes = list(self.mazes.values())
        alive = []
        for pt in self.particles_3d:
            pt["age"] += dt
            if pt["age"] >= pt["life"]:
                continue
            bounceA = pt.get("bounceA", 0.0)
            pt["vz"] = pt.get("vz", 0.0) - pt.get("gravity", 0.0) * dt
            nx = pt["x"] + pt["vx"] * dt
            ny = pt["y"] + pt["vy"] * dt
            nz = pt.get("z", 0.0) + pt["vz"] * dt

            blocked = False
            for mz in mazes:
                try:
                    if mz._solid(nx / mz.cellsize, ny / mz.cellsize):
                        blocked = True
                        break
                except Exception:
                    pass
            if blocked:
                if bounceA > 0.0:
                    pt["vx"] *= -bounceA
                    pt["vy"] *= -bounceA
                else:
                    pt["vx"] = pt["vy"] = 0.0
                nx, ny = pt["x"], pt["y"]      # don't clip into the wall this frame

            if nz <= 0.0:
                nz = 0.0
                if bounceA > 0.0 and pt["vz"] < 0.0:
                    pt["vz"] *= -bounceA
                else:
                    pt["vz"] = 0.0

            pt["x"], pt["y"], pt["z"] = nx, ny, nz
            alive.append(pt)
        self.particles_3d = alive

    def audio_3d_step(self):
        """Keep every active is3D sound's VOLUME and pitch (doppler)
        updating every frame as the listener moves - and for sounds routed
        through the live mixer, PAN, the OCCLUSION low-pass, and the
        REVERB PRESET too, so they genuinely track the room you're
        actually in right now instead of staying locked to whatever room
        the sound happened to start in (that mismatch is why reverb could
        feel 'wrongly placed' before this). QMediaPlayer-based sounds still
        can't do the pan/occlusion/reverb part live - Qt has no live pan
        control at all (confirmed directly against the docs) - which is
        exactly why is3D sounds needing those now route through the live
        mixer instead (see audioctl.play_sound's routing notes).

        A sound with no x/y (it defaulted to the listener's own position at
        the moment it started) has nothing to recompute - it stays at its
        base volume and speed, same as before this method existed."""
        if not self.active_3d_sounds:
            return
        ac = getattr(self, "audio", None)
        if ac is None:
            self.active_3d_sounds = {}
            return
        listener = _find_listener(self)
        live_players = set(getattr(ac, "_players", {})) | set(getattr(ac, "_live_keys", {}))
        dt = max(1e-4, self.time.normal)
        DOPPLER_REF_SPEED = 40.0     # cells/sec - tunable; higher = subtler effect
        DOPPLER_MAX_SHIFT = 0.15     # cap the pitch shift at +-15%, not full physics
        alive = {}
        for sid, info in self.active_3d_sounds.items():
            if sid not in live_players:
                continue      # naturally finished, or stopped elsewhere - drop it
            has_position = info.get("parent") or (info.get("x") is not None and info.get("y") is not None)
            if listener is not None and has_position:
                lx, ly, lfacing, mz = listener
                cellsize = getattr(mz, "cellsize", 40.0) or 40.0
                sx, sy = info.get("x"), info.get("y")
                parent_ref = info.get("parent")
                if parent_ref:
                    obj = _resolve_object_ref(self, parent_ref)
                    if obj is not None:
                        sx, sy = obj.x, obj.y
                        info["x"], info["y"] = sx, sy   # remember the last-known spot,
                                                         # so a destroyed parent (monster
                                                         # died) leaves the sound playing
                                                         # from where it last was instead
                                                         # of erroring or freezing
                is_live = False
                try:
                    is_live = ac.is_live_mixed(sid)
                except Exception:
                    pass
                if is_live:
                    pan, dist_atten, wetness, occlusion, preset = _audio_live_params(
                        self, mz, sx, sy, lx, ly, lfacing, cellsize,
                        info["radius"], realtime=info.get("realtime_ref", False))
                    try:
                        cutoff = 18000.0 - occlusion * 17300.0
                        ac.update_live_params(sid, pan=pan, lowpass_cutoff=cutoff,
                                              reverb_preset=preset, reverb_wetness=wetness)
                    except Exception:
                        pass
                else:
                    acoustic_dist, _occ = _acoustic_distance(self, mz, sx, sy, lx, ly,
                                                             cellsize, realtime=info.get("realtime_ref", False))
                    dist_atten = max(0.0, min(1.0, 1.0 - acoustic_dist / info["radius"]))
                try:
                    ac.change_volume(sid, info["base_volume"] * dist_atten)
                except Exception:
                    pass
                if not is_live:
                    prev = info.get("prev_dist")
                    if prev is not None and acoustic_dist != float("inf") and prev != float("inf"):
                        closing_speed = (prev - acoustic_dist) / dt     # + = approaching
                        shift = max(-DOPPLER_MAX_SHIFT, min(DOPPLER_MAX_SHIFT,
                                    closing_speed / DOPPLER_REF_SPEED))
                        try:
                            ac.change_speed(sid, info.get("base_speed", 1.0) * (1.0 + shift))
                        except Exception:
                            pass
                    info["prev_dist"] = acoustic_dist
            alive[sid] = info
        self.active_3d_sounds = alive

    def dynamic_lights_step(self):
        """Re-resolve parent: for every dynamic light, every frame - a light
        parented to a monster genuinely moves with it, the same idea as
        audio's parent: (see audio_3d_step and _resolve_object_ref). Cheap:
        this is just an attribute lookup and two float writes per light,
        nothing like the per-pixel bake baked (shadow-casting) lights need.
        A light whose parent object no longer exists (destroyed) just
        keeps shining from wherever it last was - no error, no snap."""
        if not self._light_owner:
            return
        dead = []
        for lid, maze in self._light_owner.items():
            L = maze.dynamic_lights.get(lid)
            if L is None:
                dead.append(lid)
                continue
            parent_ref = L.get("parent")
            if parent_ref:
                obj = _resolve_object_ref(self, parent_ref)
                if obj is not None:
                    L["x"], L["y"] = obj.x, obj.y
        for lid in dead:
            del self._light_owner[lid]

    def post_effects_step(self):
        """Advance the active post-effect's smoothness blend by one frame's
        worth. loadPost(name, smoothness: N) starts this at blend=0 (or 1.0
        immediately if no smoothness was given); here it climbs toward 1.0
        at a rate of 1/N per second, so 'smoothness: 0.7' reaches full
        strength in 0.7 seconds. Frame-rate independent (uses time.normal,
        the same delta-time everything else in the engine steps by)."""
        ap = self.active_post
        if ap is None or ap["blend"] >= 1.0:
            return
        dt = max(0.0, self.time.normal)
        ap["blend"] = min(1.0, ap["blend"] + ap["blend_rate"] * dt)

    def _line_of_sight(self, maze, sx, sy, tx, ty, step=0.25):
        """True if a straight ray from (sx,sy) to (tx,ty) - both in CELL
        units - never crosses a wall. Used to tell 'same room, clear shot'
        sounds (crisp, closer to raw distance) apart from 'around a corner'
        ones (which should feel muffled even at the same straight-line
        distance) - see sound_path_distance."""
        import math
        dx, dy = tx - sx, ty - sy
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return True
        steps = max(1, int(dist / step))
        for i in range(1, steps):
            t = i / steps
            try:
                if maze._solid(sx + dx * t, sy + dy * t):
                    return False
            except Exception:
                return False
        return True

    def _flood_fill_from(self, maze, cx, cy, max_dist=40.0):
        """Dijkstra flood-fill (8-directional, diagonal cost 1.414, same
        model as _nav_astar) from one cell to every reachable cell within
        max_dist. Returns {(cx,cy): path_distance_in_cells}. This is the
        expensive part of sound propagation - not raycasting through walls
        like light, but walking the same grid nav.follow already paths
        through, so a source 5 rooms over comes out with a genuinely large
        distance (it has to walk all the way around), not just whatever a
        straight line through the walls would say."""
        import heapq
        cx, cy = int(cx), int(cy)

        def walk(x, y):
            try:
                return not maze._solid(x + 0.5, y + 0.5)
            except Exception:
                return False

        if not walk(cx, cy):
            return {}
        steps = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
        dist = {(cx, cy): 0.0}
        openq = [(0.0, cx, cy)]
        area = max(1, getattr(maze, "mh", 0) *
                   max((len(r) for r in getattr(maze, "grid", [])), default=0))
        cap = 2000000 if self.use_cpu else 500000
        guard_max = min(cap, max(20000, area * 6))
        guard = 0
        while openq and guard < guard_max:
            guard += 1
            d, x, y = heapq.heappop(openq)
            if d > dist.get((x, y), float("inf")) or d > max_dist:
                continue
            for dx, dy, cost in steps:
                nx, ny = x + dx, y + dy
                if dx and dy and (not walk(x + dx, y) or not walk(x, y + dy)):
                    continue          # no corner-cutting through a wall corner
                if not walk(nx, ny):
                    continue
                nd = d + cost
                if nd < dist.get((nx, ny), float("inf")) and nd <= max_dist:
                    dist[(nx, ny)] = nd
                    heapq.heappush(openq, (nd, nx, ny))
        return dist

    def sound_path_distance(self, maze, sx, sy, lx, ly, cellsize, realtime=False, max_dist=40.0):
        """The real distance a 3D sound needs, in cells: how far you'd
        actually have to WALK to get from the source to the listener,
        instead of a straight line that cuts through walls - so a source
        several rooms over comes out properly far/quiet, not just whatever
        raw distance a straight line through solid walls would claim.
        Returns (path_distance, has_line_of_sight); path_distance is None
        if the source is unreachable within max_dist at all.

        The flood-fill is cached from the LISTENER's cell (not the source's)
        so it's shared across every active 3D sound regardless of how many
        there are - one flood-fill serves all of them each time it refreshes,
        instead of one per source (which would also be impossible to bake
        ahead of time, since sources are usually created dynamically at
        runtime, not known when a level is baked). realtimeRef: false (the
        default) reuses the cached field until the listener actually moves
        to a new cell; realtimeRef: true recomputes it fresh every single
        call, for the cost of a fresh flood-fill every frame instead of
        only when you've actually moved."""
        scx, scy = sx / cellsize, sy / cellsize
        lcx, lcy = lx / cellsize, ly / cellsize
        cell = (int(lcx), int(lcy))
        if realtime or self._sound_field is None or self._sound_field_cell != cell:
            self._sound_field = self._flood_fill_from(maze, lcx, lcy, max_dist)
            self._sound_field_cell = cell
        pd = self._sound_field.get((int(scx), int(scy)))
        los = self._line_of_sight(maze, scx, scy, lcx, lcy)
        return pd, los

    def _nav_astar(self, maze, sx, sy, tx, ty):
        """A* over the maze grid (8-directional, no corner-cutting through walls).
        Returns a list of (cx, cy) cells from start to goal, or None if unreachable."""
        import heapq

        def walk(cx, cy):
            try:
                return not maze._solid(cx + 0.5, cy + 0.5)
            except Exception:
                return False

        if not walk(sx, sy) or not walk(tx, ty):
            return None
        if (sx, sy) == (tx, ty):
            return [(sx, sy)]
        openq = [(0.0, sx, sy)]
        came = {}
        gsc = {(sx, sy): 0.0}
        closed = set()
        steps = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
        # Scale the search budget to the maze's actual size instead of a flat
        # constant - a fixed cap could give up on a genuinely reachable goal
        # on a big enough procedurally generated map, silently reporting
        # "unreachable" when the search just ran out of budget. Falls back to
        # the old 20000 if the maze's dimensions aren't available for some
        # reason, so behavior is unchanged when area can't be determined.
        area = max(1, getattr(maze, "mh", 0) *
                   max((len(r) for r in getattr(maze, "grid", [])), default=0))
        cap = 2000000 if self.use_cpu else 500000     # #overrideOPLim useCPU: true
        guard_max = min(cap, max(20000, area * 6))
        guard = 0
        while openq and guard < guard_max:
            guard += 1
            _, cx, cy = heapq.heappop(openq)
            if (cx, cy) in closed:
                continue                    # a stale duplicate entry - this cell
            closed.add((cx, cy))            # was already finalized with a better g
            if (cx, cy) == (tx, ty):
                path = [(cx, cy)]
                while (cx, cy) in came:
                    cx, cy = came[(cx, cy)]
                    path.append((cx, cy))
                path.reverse()
                return path
            base = gsc[(cx, cy)]
            for dx, dy, cost in steps:
                nx, ny = cx + dx, cy + dy
                if (nx, ny) in closed:
                    continue
                if not walk(nx, ny):
                    continue
                if dx != 0 and dy != 0:                 # don't slip through a wall corner
                    if not walk(cx + dx, cy) or not walk(cx, cy + dy):
                        continue
                ng = base + cost
                if ng < gsc.get((nx, ny), 1e18):
                    gsc[(nx, ny)] = ng
                    came[(nx, ny)] = (cx, cy)
                    h = ((nx - tx) ** 2 + (ny - ty) ** 2) ** 0.5
                    heapq.heappush(openq, (ng + h, nx, ny))
        return None

    def nav_set_destination(self, mid, svc, tx, ty, speed=1.5, arrive=6.0):
        """Pathfind svc -> world point (tx,ty) and register it as a moving agent.
        Returns 1 if a path was found, else 0. Calling this every frame with an
        unchanged goal is cheap: it keeps the existing path instead of re-routing
        (which would otherwise stall the agent by pinning it to its cell centre)."""
        svc = _svc_str(svc)
        maze = self.mazes.get(_mid(mid))
        obj = self.objects.get(svc)
        if maze is None or obj is None:
            return 0
        cs = maze.cellsize
        goal = (int(tx / cs), int(ty / cs))
        ex = self.nav_agents.get(svc)
        if (ex is not None and not ex.get("done") and ex.get("goal") == goal
                and ex.get("maze") == _mid(mid)):
            ex["speed"] = float(speed)                 # same target -> keep progress
            return 1
        path = self._nav_astar(maze, int(obj.x / cs), int(obj.y / cs),
                               goal[0], goal[1])
        if not path:
            self.nav_agents.pop(svc, None)
            return 0
        self.nav_agents[svc] = {"maze": _mid(mid), "path": path, "i": 0,
                                "speed": float(speed), "arrive": float(arrive),
                                "goal": goal, "done": False}
        return 1

    def nav_reachable(self, mid, sx, sy, tx, ty):
        """1 if a path exists between two world points in a maze, else 0.
        Memoized per (maze, source cell, target cell) - reachability between
        two cells is a pure function of the static grid (nothing in this
        engine can move/destroy a wall at runtime), so once computed it's
        computed forever. This is the call a chase-AI update{} loop typically
        makes every single frame per agent (nav.reachable before nav.follow),
        which without this cache means a full, from-scratch A* search every
        frame for an answer that essentially never changes - measured
        directly on a real 26x26, 5-agent level: ~2.2ms/frame of pure waste
        before this cache, effectively free after (a dict lookup)."""
        rmid = _mid(mid)
        maze = self.mazes.get(rmid)
        if maze is None:
            return 0
        cs = maze.cellsize
        sc = (int(sx / cs), int(sy / cs))
        tc = (int(tx / cs), int(ty / cs))
        key = (rmid, sc, tc)
        cached = self._reachable_cache.get(key)
        if cached is not None:
            return cached
        p = self._nav_astar(maze, sc[0], sc[1], tc[0], tc[1])
        result = 1 if p else 0
        self._reachable_cache[key] = result
        return result

    def nav_step(self):
        """Advance every nav agent along its path (walls already avoided).
        Call once per frame - internally frame-rate independent (scales by
        time.normal), so speed values already tuned assuming ~60 calls/sec
        keep meaning the same thing at any other real frame rate."""
        if not self.nav_agents:
            return
        rate = max(0.0, self.time.normal) * 60.0   # 1.0 at exactly 60fps
        for svc, ag in list(self.nav_agents.items()):
            obj = self.objects.get(svc)
            if obj is None:
                self.nav_agents.pop(svc, None)
                continue
            maze = self.mazes.get(ag["maze"])
            if maze is None:
                continue
            cs = maze.cellsize
            path = ag["path"]
            arrive = ag["arrive"]
            # skip any waypoints we've already reached (in this same frame)
            while ag["i"] < len(path):
                wx = (path[ag["i"]][0] + 0.5) * cs
                wy = (path[ag["i"]][1] + 0.5) * cs
                if ((wx - obj.x) ** 2 + (wy - obj.y) ** 2) ** 0.5 <= arrive:
                    ag["i"] += 1
                else:
                    break
            if ag["i"] >= len(path):
                ag["done"] = True
                continue
            # step toward the next unreached waypoint
            wx = (path[ag["i"]][0] + 0.5) * cs
            wy = (path[ag["i"]][1] + 0.5) * cs
            dx, dy = wx - obj.x, wy - obj.y
            d = (dx * dx + dy * dy) ** 0.5
            if d > 1e-6:
                sp = min(ag["speed"] * rate, d)
                obj.x += dx / d * sp
                obj.y += dy / d * sp

    def physics_step(self):
        """Move objects that have a velocity (physics.push). Stops at maze walls;
        applies each object's friction. Player/monsters that move via adjvcr are
        untouched (they carry no velocity). Frame-rate independent: vx/vy were
        tuned as a fixed pixels-per-tick amount assuming ~60 calls/sec, so
        they're scaled by the same rate nav_step uses. Friction decay is an
        exponential, not a linear, process - decaying velocity by (1-f)**rate
        alone gets the final velocity right but not the DISTANCE travelled
        while decaying (a coarse step at low fps covers more ground before
        friction catches up than many fine steps at high fps would - a real
        ~4.5% position gap measured between 60fps and 500fps). Using the
        closed-form integral of v0*k^t dt over the tick instead - not just
        the decayed endpoint - makes accumulated distance match regardless
        of step size, same as the velocity/position update above already do."""
        if not self.objects:
            return
        rate = max(0.0, self.time.normal) * 60.0   # 1.0 at exactly 60fps
        mazes = list(self.mazes.values())
        for obj in list(self.objects.values()):
            vx, vy = getattr(obj, "vx", 0.0), getattr(obj, "vy", 0.0)
            if vx == 0.0 and vy == 0.0:
                continue
            nx, ny = obj.x + vx * rate, obj.y + vy * rate
            blocked = False
            for mz in mazes:
                try:
                    if mz._solid(nx / mz.cellsize, ny / mz.cellsize):
                        blocked = True
                        break
                except Exception:
                    pass
            if blocked:
                obj.vx = obj.vy = 0.0
                obj._hitwall = True
            else:
                f = getattr(obj, "friction", 0.0) or 0.0
                if f and rate > 0.0:
                    k = max(0.0, 1.0 - f)
                    if k <= 0.0:
                        # friction >= 1.0: stops within this tick - move a
                        # negligible amount rather than dividing by ln(0)
                        obj.x, obj.y = obj.x + vx * 0.001, obj.y + vy * 0.001
                        obj.vx = obj.vy = 0.0
                    else:
                        import math as _m
                        # exact integral of v0*k^t from 0..rate, not the
                        # per-tick (1-f)**rate approximation - see docstring
                        dist_factor = rate if k >= 0.999999 else (k ** rate - 1.0) / _m.log(k)
                        nx2, ny2 = obj.x + vx * dist_factor, obj.y + vy * dist_factor
                        if any(mz._solid(nx2 / mz.cellsize, ny2 / mz.cellsize) for mz in mazes):
                            obj.x, obj.y = nx, ny   # fall back to the simple step if the
                        else:                        # exact-integral point would clip a wall
                            obj.x, obj.y = nx2, ny2
                        decay = k ** rate
                        obj.vx *= decay
                        obj.vy *= decay
                        if abs(obj.vx) < 0.01 and abs(obj.vy) < 0.01:
                            obj.vx = obj.vy = 0.0
                else:
                    obj.x, obj.y = nx, ny

    def end_frame(self):
        self.input.end_frame()
        self.mouse["dx"] = 0.0           # per-frame movement; reset after scripts read it
        self.mouse["dy"] = 0.0

    def tick_timers(self):
        """Fire any after{} blocks whose delay has elapsed. Call once per frame."""
        if not self.timers:
            return
        import time as _t
        now = _t.monotonic()
        due = [d for d in self.timers if d["at"] <= now]
        if not due:
            return
        self.timers = [d for d in self.timers if d["at"] > now]
        for d in due:
            try:
                for s in d["body"]:
                    _exec(s, self)
            except _Return:
                pass
            except Exception:
                pass

    def fit(self, avail_w, avail_h, margin=0.9):
        """menu.dynamic: compute a display zoom+offset so every object fits the
        screen. Bounds are captured once (from the initial layout) so the view
        stays put as objects move; the zoom/offset re-derive whenever the window
        size changes. Physics/positions stay in authored units."""
        objs = list(self.objects.values())
        if not objs or avail_w <= 1 or avail_h <= 1:
            return
        if self._fit_bounds is None:
            minx = min(o.x for o in objs)
            miny = min(o.y for o in objs)
            maxx = max(o.x + o.w * o.sx for o in objs)
            maxy = max(o.y + o.h * o.sy for o in objs)
            self._fit_bounds = (minx, miny, maxx, maxy)
        minx, miny, maxx, maxy = self._fit_bounds
        cw = max(1.0, maxx - minx)
        ch = max(1.0, maxy - miny)
        f = min(avail_w * margin / cw, avail_h * margin / ch)
        self.view_scale = f
        self.view_offx = (avail_w - cw * f) / 2.0 - minx * f
        self.view_offy = (avail_h - ch * f) / 2.0 - miny * f

    def maybe_fit(self):
        """Run/refresh the fit once the host has a size, and again on resize."""
        if not self.dynamic or self.host is None:
            return
        try:
            w, h = self.host.width(), self.host.height()
        except Exception:
            return
        if w > 1 and h > 1 and (w, h) != self._fit_size:
            self.fit(w, h)
            self._fit_size = (w, h)

    def active(self):
        return bool(self.objects) or bool(self.update_script) or bool(self.setup_script)


def _resolve_object_ref(w, ref):
    """Find an object by svc number OR by name/label (e.g. a vcr.colide's
    quoted label) - the same two-step lookup RaycasterWidget._find_parent
    uses for raycast{}'s own parent:, reused here so 'parent: monster1'
    means the same thing everywhere in Glass, whether it's a camera or a
    sound following something around."""
    if not ref:
        return None
    obj = w.objects.get(_svc_str(ref))              # by svc
    if obj is not None:
        return obj
    for cand in w.objects.values():                  # by name/label
        if getattr(cand, "name", None) == ref:
            return cand
    return None


def _find_listener(w):
    """The player's position/facing, derived from whichever raycaster's
    parent object is currently registered - same object that already
    drives the first-person camera, so audio and the visual view always
    agree on where 'you' are. Returns (x, y, facing_degrees, maze) or None
    if there's no raycast scene at all (e.g. a plain 2D page).

    parent: on a raycast can be either an svc number OR a name/label (e.g.
    parent: player, matching vcr.colide "player" { ... })."""
    for mz in w.mazes.values():
        parent_ref = getattr(mz, "parent", None)
        if not parent_ref:
            continue
        obj = _resolve_object_ref(w, parent_ref)
        if obj is not None:
            return (obj.x, obj.y, getattr(obj, "rot", 0.0), mz)
    return None


def _acoustic_distance(w, maze, sx, sy, lx, ly, cellsize, realtime=False):
    """(distance, occlusion) - how far a sound really has to travel to
    reach you, in cells: the walkable path distance around walls (via
    World.sound_path_distance), not a straight line that cuts straight
    through them - so a source several rooms over comes out properly far
    and quiet instead of just however close it happens to be as the crow
    flies. occlusion is 0.0 (clear line of sight) or 1.0 (blocked) - used
    both to penalize distance further (diffracting around a corner loses
    more energy than the walking distance alone accounts for) and to drive
    the low-pass filter that makes a blocked source sound muffled, not just
    quieter. Falls back to (straight-line distance, 0.0) if there's no maze
    to walk (matches the old behavior exactly)."""
    import math
    if maze is None:
        return math.hypot(sx - lx, sy - ly) / (cellsize or 40.0), 0.0
    pd, los = w.sound_path_distance(maze, sx, sy, lx, ly, cellsize or 40.0, realtime=realtime)
    if pd is None:
        return float("inf"), 1.0     # not reachable at all - fully inaudible
    OCCLUSION_PENALTY = 1.5           # tunable - how much extra a blocked path costs
    occlusion = 0.0 if los else 1.0
    return (pd if los else pd * OCCLUSION_PENALTY), occlusion


def _distance_atten(sx, sy, lx, ly, radius, cellsize):
    """0..1 distance-based volume multiplier from a plain straight-line
    distance. Still used as the fallback with no maze (see _acoustic_distance)
    and by anything that only has a raw distance to work with."""
    import math
    dist_cells = math.hypot(sx - lx, sy - ly) / (cellsize or 40.0)
    return max(0.0, min(1.0, 1.0 - dist_cells / (radius or 10.0)))


def _audio_live_params(w, mz, sx, sy, lx, ly, lfacing, cellsize, radius, realtime=False):
    """(pan, dist_atten, wetness, occlusion, preset) computed from CURRENT
    geometry - shared by _audio_3d_params (the initial snapshot, when a
    sound starts) AND audio_3d_step (the continuous per-frame update for
    live-mixed sounds), so both use identical math and can't silently
    drift apart. This is what lets pan/reverb/occlusion genuinely track
    the room you're ACTUALLY in as you walk around, instead of staying
    locked to whatever room a sound happened to start in."""
    import math
    dist_cells = math.hypot(sx - lx, sy - ly) / cellsize
    acoustic_dist, occlusion = _acoustic_distance(w, mz, sx, sy, lx, ly, cellsize, realtime=realtime)
    dist_atten = max(0.0, min(1.0, 1.0 - acoustic_dist / radius))
    if dist_cells < 1e-6:
        pan = 0.0
    else:
        ang_to_source = math.degrees(math.atan2(sy - ly, sx - lx))
        rel = ((ang_to_source - lfacing + 180.0) % 360.0) - 180.0    # -180..180
        # sine, not a linear clamp(rel/90) - a linear clamp is FLAT across
        # the whole 90..180 (and -90..-180) range, so a source directly
        # behind sits at a hard +1.0/-1.0 pan, and the tiniest turn that
        # crosses the +-180 wrap point abruptly flips it from full-right to
        # full-left with nothing in between. sin(rel) still gives the same
        # cardinal values (0 deg=centered, +-90 deg=full pan) but is
        # perfectly continuous all the way around, including behind.
        pan = math.sin(math.radians(rel))
    preset = "hallway"
    try:
        room = mz.room_scale_at(lx / cellsize, ly / cellsize, realtime=realtime)
        wetness = max(0.0, min(1.0, 1.0 - room / 10.0))   # small room -> more reverb
        import audioctl
        preset = audioctl.select_reverb_preset(room)
    except Exception:
        wetness = 0.0
    return pan, dist_atten, wetness, occlusion, preset


def _sound_source_pos(kwargs, w, lx, ly):
    """Where a sound is actually coming from right now: parent: (an svc or
    name, e.g. a monster) takes precedence and is resolved fresh every
    time this is called - so a parented sound genuinely follows a moving
    object instead of being stuck where it was when it started. Falls back
    to plain x/y, then to the listener's own position (no positioning at
    all) if neither is given."""
    parent_ref = kwargs.get("parent")
    if parent_ref:
        obj = _resolve_object_ref(w, parent_ref)
        if obj is not None:
            return obj.x, obj.y
    sx = _num(kwargs["x"]) if "x" in kwargs else lx
    sy = _num(kwargs["y"]) if "y" in kwargs else ly
    return sx, sy


def _audio_3d_params(kwargs, w):
    """pan (-1 left..1 right), distance-volume multiplier (0..1), reverb
    wetness (0..1), occlusion (0..1, drives the low-pass filter), and the
    selected reverb preset name (see audioctl.REVERB_PRESETS - matches
    HL2's approach of picking a hand-tuned character by room geometry
    rather than computing one continuously) for a 3D audio.playSound call.
    All zero/neutral if there's no listener (no raycast scene) - callers
    just get plain, unpositioned audio, same as before this feature
    existed."""
    listener = _find_listener(w)
    if listener is None:
        return 0.0, 1.0, 0.0, 0.0, "hallway"
    lx, ly, lfacing, mz = listener
    sx, sy = _sound_source_pos(kwargs, w, lx, ly)
    radius = _num(kwargs.get("radius", 10.0)) or 10.0
    cellsize = getattr(mz, "cellsize", 40.0) or 40.0
    realtime_ref = str(kwargs.get("realtimeRef", "false")).strip().lower() in ("true", "1", "yes")
    return _audio_live_params(w, mz, sx, sy, lx, ly, lfacing, cellsize, radius, realtime=realtime_ref)


def _hex_to_rgb01(color):
    """'#rrggbb' or '#rrggbbaa' -> (r,g,b) as 0..1 floats. Alpha, if
    present, is ignored - dynamic lights don't have their own transparency,
    only color and intensity."""
    s = str(color).strip().lstrip("#")
    if len(s) >= 6:
        try:
            return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0)
        except ValueError:
            pass
    return (1.0, 1.0, 1.0)


def _light_call(func, args, kwargs, w):
    """light.create { mazeID:, parent: (or x/y), color:, radius:, intensity: }
    - a cheap, non-shadow-casting light that can move and recolor every
    frame (see RaycasterWidget._light_sample and World.dynamic_lights_step),
    unlike a baked light{} block which needs a full re-bake (measured:
    over a second, ~72x a 60fps frame budget) to change at all. parent:
    reuses the exact same svc-or-name lookup audio.playSound's parent:
    does. Returns a light id for light.setColor/setPos/destroy, or 0 if
    the mazeID doesn't exist."""
    if func != "create":
        return 0.0
    mid = _mid(kwargs.get("mazeID")) if "mazeID" in kwargs else None
    maze = w.mazes.get(mid) if mid is not None else None
    if maze is None:
        return 0.0
    r, g, b = _hex_to_rgb01(kwargs.get("color", "#ffffff"))
    parent_ref = kwargs.get("parent")
    x = _num(kwargs["x"]) if "x" in kwargs else 0.0
    y = _num(kwargs["y"]) if "y" in kwargs else 0.0
    if parent_ref:
        obj = _resolve_object_ref(w, parent_ref)
        if obj is not None:
            x, y = obj.x, obj.y
    lid = w._next_light_id
    w._next_light_id += 1
    maze.dynamic_lights[lid] = {
        "x": x, "y": y, "r": r, "g": g, "b": b,
        "radius": _num(kwargs.get("radius", 4.0)) or 4.0,
        "intensity": _num(kwargs.get("intensity", 1.0)) or 1.0,
        "parent": parent_ref,
    }
    w._light_owner[lid] = maze
    return float(lid)


def _kwcall(parts, posargs, kwargs, w):
    """postEffects.X(...) / loadPost(name, smoothness:) / removePost(name) /
    postQuality(name, quality:) - the post-processing control surface. See
    render_rules' *.postEffect compilation for how effects get registered,
    and World.post_effects_step for the per-frame smoothness lerp."""
    if parts and parts[0] == "postEffects":
        # only ever meaningful while render_rules is compiling an effect{}
        # block (see World._compiling_post) - this is what makes postEffects.*
        # "only work inside effect{}", the same way some calls only work
        # inside setup/update: outside that context there's nothing to
        # register into, so the call is silently a no-op.
        if w._compiling_post is not None and len(parts) > 1:
            w._compiling_post.append((parts[1], dict(kwargs)))
        return 0.0
    if len(parts) != 1:
        return 0.0
    fname = parts[0].lower()
    if fname == "loadpost":
        name = _s(posargs[0]) if posargs else ""
        profile = w.post_profiles.get(name)
        if profile is None:
            return 0.0
        smoothness = _num(kwargs.get("smoothness", 0.0)) or 0.0
        w.active_post = {
            "profile": profile,
            "blend": 0.0 if smoothness > 0 else 1.0,
            "blend_rate": (1.0 / smoothness) if smoothness > 0 else 0.0,
        }
        return 1.0
    if fname == "removepost":
        name = _s(posargs[0]) if posargs else ""
        if w.active_post is not None and w.active_post["profile"]["name"] == name:
            w.active_post = None
            return 1.0
        return 0.0
    if fname == "postquality":
        name = _s(posargs[0]) if posargs else ""
        profile = w.post_profiles.get(name)
        if profile is None:
            return 0.0
        q = kwargs.get("quality")
        if q is not None:
            profile["quality"] = max(0, min(100, int(_num(q))))
        return 1.0
    return 0.0


def _audio_call(func, args, kwargs, w):
    ac = getattr(w, "audio", None)
    if ac is None:                       # editor preview / no audio system
        return False if func == "isPlaying" else 0
    try:
        if func == "isPlaying":
            return bool(ac.is_playing())
        if func == "getAudioId":
            return ac.get_audio_id()
        if func == "gatherClip":
            path = args[0] if args else (kwargs.get("file") or kwargs.get("path"))
            return ac.gather_clip(path)
        if func == "playSound":
            src = args[0] if args else None
            base_volume = _num(kwargs.get("volume", 1.0))
            pan, reverb, occlusion, preset = 0.0, 0.0, 0.0, "hallway"
            is_3d = str(kwargs.get("is3D", "false")).strip().lower() in ("true", "1", "yes")
            volume = base_volume
            if is_3d:
                pan, dist_atten, reverb, occlusion, preset = _audio_3d_params(kwargs, w)
                volume = base_volume * dist_atten
            sid = ac.play_sound(src,
                                speed=kwargs.get("speed", 1.0),
                                volume=volume,
                                quality=kwargs.get("quality", 100),
                                hertz=kwargs.get("hertz"),
                                pan=pan, reverb=reverb, occlusion=occlusion,
                                reverb_preset=preset,
                                loop=str(kwargs.get("loop", "false")).strip().lower() in ("true", "1", "yes"))
            if is_3d and sid:
                # volume (unlike pan/reverb - genuinely no live control exists
                # for those, see audio_3d_step's docstring) CAN keep updating
                # every frame from here on, so a source you walk toward or
                # away from actually gets louder/quieter as you move, not
                # just a one-time snapshot from the moment it started.
                w.active_3d_sounds[sid] = {
                    "x": _num(kwargs["x"]) if "x" in kwargs else None,
                    "y": _num(kwargs["y"]) if "y" in kwargs else None,
                    "parent": kwargs.get("parent"),   # svc/name to follow every frame,
                                                       # takes precedence over x/y - see
                                                       # audio_3d_step and _sound_source_pos
                    "radius": _num(kwargs.get("radius", 10.0)) or 10.0,
                    "base_volume": base_volume,
                    "base_speed": _num(kwargs.get("speed", 1.0)) or 1.0,
                    "realtime_ref": str(kwargs.get("realtimeRef", "false")).strip().lower() in ("true", "1", "yes"),
                    "prev_dist": None,     # for doppler - see audio_3d_step
                }
            return sid
        if func == "playLast":
            return ac.play_last(kwargs.get("audioID", args[0] if args else 0))
        if func == "pauseCurrent":
            return ac.pause_current(kwargs.get("audioID", 0),
                                    kwargs.get("fadeAmount", 0))
        if func == "changeVolume":
            return ac.change_volume(kwargs.get("audioID", 0),
                                    kwargs.get("volume", 100))
    except Exception:
        return 0
    return 0


def _svc_str(s):
    if isinstance(s, (list, tuple)):
        s = s[0] if s else 0
    if isinstance(s, float) and s.is_integer():
        s = int(s)
    return str(s)


# ===========================================================================
#  setup { } / update { } mini-language  (tokenize -> tiny AST -> evaluate)
# ===========================================================================
import re as _re

SCREEN = [1280, 800]    # live window size; screen.width/height in scripts read this

_TOKEN_RE = _re.compile(r"""
    \s+
  | //[^\n]*
  | "(?:\\.|[^"\\])*"
  | '(?:\\.|[^'\\])*'
  | \d*\.\d+ | \d+\.?\d*
  | [A-Za-z_]\w*
  | == | != | <= | >= | && | \|\|
  | [-+*/(){},.<>!=:]
""", _re.X)


def _tok(src):
    out = []
    for m in _TOKEN_RE.finditer(src or ""):
        t = m.group(0)
        if t.isspace() or t.startswith("//"):
            continue
        out.append(t)
    return out


class _P:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def nx(self):
        t = self.peek()
        self.i += 1
        return t

    def eat(self, t):
        if self.peek() == t:
            self.i += 1
            return True
        return False

    def block(self):
        out = []
        while self.peek() is not None and self.peek() != "}":
            s = self.stmt()
            if s is not None:
                out.append(s)
        return out

    def stmt(self):
        t = self.peek()
        if t == "if":
            return self.if_stmt()
        if t == "return":
            self.nx()
            rettype = None
            val = ("str", "")
            if self.peek() == "{":                 # return { returnType: T, value: V }
                self.nx()
                while self.peek() not in ("}", None):
                    key = self.nx()
                    self.eat(":")
                    if key == "returnType":
                        rettype = self.nx()        # a bare type name: bool/int/string/double
                    elif key == "value":
                        val = self.expr()
                    else:
                        self.expr()
                    self.eat(",")
                self.eat("}")
            elif self.peek() not in (None, "}"):
                val = self.expr()
            return ("return", val, rettype)
        if t == "after":                            # after 2s { ... }  (delay)
            self.nx()
            secs = self.expr()
            if self.peek() == "s":                  # optional 's' unit
                self.nx()
            self.eat("{")
            body = self.block()
            self.eat("}")
            return ("after", secs, body)
        if t == "repeat":                           # repeat <n> { ... }
            self.nx()
            count = self.expr()
            self.eat("{")
            body = self.block()
            self.eat("}")
            return ("repeat", count, body)
        if t == "for":                              # for i = a to b { ... }
            self.nx()
            name = self.nx()
            self.eat("=")
            start = self.expr()
            self.eat("to")
            end = self.expr()
            self.eat("{")
            body = self.block()
            self.eat("}")
            return ("for", name, start, end, body)
        if t == "{":
            self.nx(); inner = self.block(); self.eat("}")
            return ("block", inner)
        # audio.clip Name = audio.gatherClip { "..." }   (typed declaration)
        if t == "audio" and self.peek(1) == "." and self.peek(2) == "clip":
            self.nx(); self.nx(); self.nx()        # consume  audio . clip
            name = self.nx()
            self.eat("=")
            return ("assign", name, "=", self.expr())
        if (t is not None and _re.match(r"[A-Za-z_]\w*$", t)
                and self.peek(1) in ("=", "+=", "-=", "*=", "/=")):
            name = self.nx(); op = self.nx(); e = self.expr()
            return ("assign", name, op, e)
        return ("expr", self.expr())

    def _audio_block(self):
        """Parse { key: value, ..., bareExpr } -> (positionals, kwargs)."""
        self.eat("{")
        positionals = []
        kwargs = {}
        if self.peek() != "}":
            while True:
                if (self.peek() and _re.match(r"[A-Za-z_]\w*$", self.peek())
                        and self.peek(1) == ":"):
                    key = self.nx(); self.nx()      # name ':'
                    kwargs[key] = self.expr()
                else:
                    positionals.append(self.expr())
                if not self.eat(","):
                    break
        self.eat("}")
        return positionals, kwargs

    def if_stmt(self):
        self.nx(); self.eat("(")
        cond = self.expr(); self.eat(")"); self.eat("{")
        body = self.block(); self.eat("}")
        els = []
        if self.peek() == "else":
            self.nx()
            if self.peek() == "if":
                els = [self.if_stmt()]
            else:
                self.eat("{"); els = self.block(); self.eat("}")
        return ("if", cond, body, els)

    def expr(self):
        return self.lor()

    def lor(self):
        l = self.land()
        while self.peek() == "||":
            self.nx(); l = ("bin", "||", l, self.land())
        return l

    def land(self):
        l = self.eq()
        while self.peek() == "&&":
            self.nx(); l = ("bin", "&&", l, self.eq())
        return l

    def eq(self):
        l = self.cmp()
        while self.peek() in ("==", "!="):
            op = self.nx(); l = ("bin", op, l, self.cmp())
        return l

    def cmp(self):
        l = self.add()
        while self.peek() in ("<", ">", "<=", ">="):
            op = self.nx(); l = ("bin", op, l, self.add())
        return l

    def add(self):
        l = self.mul()
        while self.peek() in ("+", "-"):
            op = self.nx(); l = ("bin", op, l, self.mul())
        return l

    def mul(self):
        l = self.un()
        while self.peek() in ("*", "/"):
            op = self.nx(); l = ("bin", op, l, self.un())
        return l

    def un(self):
        if self.peek() in ("!", "-"):
            op = self.nx(); return ("unary", op, self.un())
        return self.prim()

    def prim(self):
        t = self.peek()
        if t is None:
            return ("num", 0.0)
        _CASTS = ("int", "float", "double", "string", "bool")
        if t == "(" and self.peek(1) in _CASTS and self.peek(2) == ")":
            self.nx(); ctype = self.nx(); self.nx()      # ( type )
            return ("cast", ctype, self.un())
        if t == "(":
            self.nx(); e = self.expr(); self.eat(")"); return e
        if t == "{":
            self.nx(); items = []
            if self.peek() != "}":
                items.append(self.expr())
                while self.eat(","):
                    items.append(self.expr())
            self.eat("}")
            return ("vec", items)
        if t[0] in "\"'":
            self.nx(); return ("str", t[1:-1])
        if _re.match(r"\d*\.\d+$|\d+\.?\d*$", t):
            self.nx(); return ("num", float(t))
        if _re.match(r"[A-Za-z_]\w*$", t):
            if t.lower() == "true":
                self.nx(); return ("bool", True)
            if t.lower() == "false":
                self.nx(); return ("bool", False)
            parts = [self.nx()]
            while self.peek() == ".":
                self.nx()
                if self.peek() and _re.match(r"[A-Za-z_]\w*$", self.peek()):
                    parts.append(self.nx())
            if parts[0] == "audio" or (parts[0] == "light" and len(parts) > 1 and parts[1] == "create"):
                func = parts[1] if len(parts) > 1 else ""
                posargs = []
                nt = self.peek()
                if nt is not None and nt[0] in "\"'":          # "file.mp3"
                    posargs.append(("str", self.nx()[1:-1]))
                elif nt == "(":                                # (Clip)
                    self.nx(); posargs.append(self.expr()); self.eat(")")
                kwargs = {}
                if self.peek() == "{":                          # { settings } / { "file" }
                    pos2, kw = self._audio_block()
                    posargs += pos2
                    kwargs = kw
                return (parts[0] + "call", func, posargs, kwargs)
            if parts[0] == "postEffects" or (len(parts) == 1 and parts[0] in
                                             ("loadPost", "removePost", "postQuality")):
                # postEffects.bloom(threshold: 0.8, intensity: 1.2) /
                # loadPost(testing, smoothness: 0.7) - REAL named args here,
                # not the generic call parser's cosmetic labels, since post
                # effects have many optional settings each and need actual
                # name-based matching, not strict positional order.
                posargs, kwargs = [], {}
                if self.peek() == "(":
                    self.nx()
                    posargs, kwargs = self._paren_kwargs()
                return ("kwcall", parts, posargs, kwargs)
            if self.peek() == "(":
                self.nx(); args = []
                if self.peek() != ")":
                    args.append(self._arg())
                    while self.eat(","):
                        args.append(self._arg())
                self.eat(")")
                return ("call", parts, args)
            return ("member", parts)
        self.nx()
        return ("num", 0.0)

    def _arg(self):
        # optional named-arg label:  name: value   (the label is cosmetic)
        if (self.peek() and _re.match(r"[A-Za-z_]\w*$", self.peek())
                and self.peek(1) == ":"):
            self.nx(); self.nx()          # skip  name  :
        return self.expr()

    def _paren_kwargs(self):
        """(arg, key: expr, ...) - the opening '(' is already consumed.
        Unlike _arg()'s cosmetic labels, these are REAL named arguments:
        used only by postEffects.*/loadPost/removePost/postQuality, since
        post effects have many optional settings each and need genuine
        name-based matching (arguments in any order), not strict position.
        Returns (posargs, kwargs) as unevaluated expression nodes."""
        posargs, kwargs = [], {}
        if self.peek() == ")":
            self.nx()
            return posargs, kwargs
        while True:
            if (self.peek() and _re.match(r"[A-Za-z_]\w*$", self.peek())
                    and self.peek(1) == ":"):
                key = self.nx()
                self.nx()                 # skip ':'
                kwargs[key] = self.expr()
            else:
                posargs.append(self.expr())
            if not self.eat(","):
                break
        self.eat(")")
        return posargs, kwargs


def _truthy(v):
    if isinstance(v, str):
        return len(v) > 0 and v.lower() not in ("false", "0")
    return bool(v)


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _as_vec(v):
    """Coerce a value to an (x, y, z) tuple."""
    if isinstance(v, (tuple, list)):
        x = float(v[0]) if len(v) > 0 else 0.0
        y = float(v[1]) if len(v) > 1 else 0.0
        z = float(v[2]) if len(v) > 2 else 0.0
        return (x, y, z)
    return (_num(v), 0.0, 0.0)


def _get_property(obj, sel):
    """properties.get(svc, "position"/"position.x"/"scale.y"/"rotation"/
    "tag.door"/"friction"/"isTrigger"/"kind"/"speed"/"size"/"velocity"/...)."""
    if obj is None:
        return 0.0
    import math
    parts = str(sel).split(".")
    kind = parts[0] if parts else ""      # which PROPERTY was asked for - not
                                           # to be confused with the object's
                                           # own obj.kind (image/colide/...),
                                           # handled separately below
    if kind == "tag":
        # getProperty.tag("door") - 1.0 if THIS object's tag matches "door",
        # else 0.0. Joins parts[1:] back together (not just parts[1]) so a
        # tag value that happens to contain a "." isn't truncated.
        wanted = ".".join(parts[1:])
        return 1.0 if wanted and getattr(obj, "tag", "") == wanted else 0.0
    if kind == "friction":
        return float(getattr(obj, "friction", 0.0) or 0.0)
    if kind == "istrigger":
        return 1.0 if getattr(obj, "istrigger", False) else 0.0
    if kind == "speed":
        # magnitude of (vx, vy) - a derived value, not a raw field. How fast
        # something set moving by physics.push is currently going, useful
        # for impact-scaled damage, "has this projectile slowed down" checks.
        vx, vy = getattr(obj, "vx", 0.0), getattr(obj, "vy", 0.0)
        return math.sqrt(vx * vx + vy * vy)
    if kind == "kind":
        # the object's own kind - image/gif/video/colide/raycastobject/... -
        # set once at creation (vcr.* element type), never changes after.
        return str(getattr(obj, "kind", ""))
    sub = parts[1] if len(parts) > 1 else None
    if kind == "position":
        base = (obj.x, obj.y, 0.0)
    elif kind == "scale":
        base = (obj.sx, obj.sy, 1.0)
    elif kind == "rotation":
        base = (0.0, 0.0, obj.rot)
    elif kind == "velocity":
        # (vx, vy) from physics.push - 0,0 unless something has launched or
        # shoved this object. Untouched by adjvcr/nav-driven movement, which
        # carry no velocity of their own (see physics_step's docstring).
        base = (getattr(obj, "vx", 0.0), getattr(obj, "vy", 0.0), 0.0)
    elif kind == "size":
        # collider dimensions if one is set, else the object's base draw
        # size - custom hit-zone/bounds math beyond what collide.detect gives.
        coll = getattr(obj, "collider", None)
        if coll:
            base = (float(coll[0]), float(coll[1]), 0.0)
        else:
            base = (getattr(obj, "w", 0.0), getattr(obj, "h", 0.0), 0.0)
    else:
        return 0.0
    if sub is None:
        return base
    if sub in ("x", "y", "z"):
        return base[{"x": 0, "y": 1, "z": 2}[sub]]
    if kind == "position":
        a = math.radians(obj.rot)
        off = {
            "forward": (math.cos(a), math.sin(a)),
            "backward": (-math.cos(a), -math.sin(a)),
            "left": (math.cos(a - math.pi / 2), math.sin(a - math.pi / 2)),
            "right": (math.cos(a + math.pi / 2), math.sin(a + math.pi / 2)),
            "up": (0.0, -1.0),
            "down": (0.0, 1.0),
        }.get(sub)
        if off:
            return (obj.x + off[0], obj.y + off[1], 0.0)
    return 0.0


def _mid(v):
    """Coerce a mazeID arg (5.0 -> 5) so it matches the registered key."""
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return str(v)


def _mesh_resolve_path(path):
    """Resolve a mesh file path: as-is, else relative to the active asset dirs."""
    import os
    if os.path.isfile(path):
        return path
    try:
        import renderer
        for d in (getattr(renderer, "ASSET_DIRS", []) or []):
            cand = os.path.join(d, path)
            if os.path.isfile(cand):
                return cand
    except Exception:
        pass
    return path


class _Return(Exception):
    """Raised by a `return { ... }` inside a snippet to unwind with a value."""
    def __init__(self, value):
        self.value = value


def _coerce(val, typename):
    t = (typename or "").lower()
    if t in ("bool", "boolean"):
        return _truthy(val)
    if t in ("int", "integer"):
        return float(int(_num(val)))
    if t in ("double", "float", "number"):
        return _num(val)
    if t in ("string", "str"):
        return _s(val)
    return val


def _call_snippet(snip, args, w):
    """Run a user 'snip' with typed params in a local scope; return its value.
    Params shadow globals for the duration; other variable writes persist (so a
    snip can act like a void with side effects). A `return {..}` yields a value."""
    if getattr(w, "_snip_depth", 0) > 24:      # runaway-recursion guard
        return ""
    params = snip.get("params", [])
    body = snip.get("body", "")
    saved, had = {}, {}
    for i, (ptype, pname) in enumerate(params):
        had[pname] = pname in w.vars
        if had[pname]:
            saved[pname] = w.vars[pname]
        av = args[i] if i < len(args) else ""
        w.vars[pname] = _coerce(av, ptype)
    result = ""
    w._snip_depth = getattr(w, "_snip_depth", 0) + 1
    try:
        for s in _P(_tok(body)).block():
            _exec(s, w)
    except _Return as r:
        result = r.value
    except Exception:
        pass
    finally:
        w._snip_depth -= 1
        for (ptype, pname) in params:          # params are local: restore/remove
            if had.get(pname):
                w.vars[pname] = saved[pname]
            else:
                w.vars.pop(pname, None)
    return result


def _ev(node, w):
    k = node[0]
    if k == "num":
        return node[1]
    if k == "bool":
        return node[1]
    if k == "str":
        return node[1]
    if k == "vec":
        return [_ev(x, w) for x in node[1]]
    if k == "audiocall":
        args = [_ev(a, w) for a in node[2]]
        kwargs = {kk: _ev(vv, w) for kk, vv in node[3].items()}
        return _audio_call(node[1], args, kwargs, w)
    if k == "lightcall":
        args = [_ev(a, w) for a in node[2]]
        kwargs = {kk: _ev(vv, w) for kk, vv in node[3].items()}
        return _light_call(node[1], args, kwargs, w)
    if k == "kwcall":
        posargs = [_ev(a, w) for a in node[2]]
        kwargs = {kk: _ev(vv, w) for kk, vv in node[3].items()}
        return _kwcall(node[1], posargs, kwargs, w)
    if k == "member":
        handled, val = _list_op(w, node[1], None)
        if handled:
            return val
        return _resolve(node[1], None, w)
    if k == "call":
        name = ".".join(node[1])
        snips = getattr(w, "snippets", None) or {}
        snip = snips.get(name) or (snips.get(node[1][0]) if node[1] else None)
        if snip is not None:
            return _call_snippet(snip, [_ev(a, w) for a in node[2]], w)
        cargs = [_ev(a, w) for a in node[2]]
        handled, val = _list_op(w, node[1], cargs)
        if handled:
            return val
        return _resolve(node[1], cargs, w)
    if k == "cast":
        val = _ev(node[2], w)
        ct = node[1]
        if ct == "string":
            return _s(val)
        if ct == "int":
            return float(int(_num(val)))
        if ct in ("float", "double"):
            return _num(val)
        if ct == "bool":
            return 1.0 if _truthy(val) else 0.0
        return val
    if k == "unary":
        v = _ev(node[2], w)
        return (0.0 if _truthy(v) else 1.0) if node[1] == "!" else -_num(v)
    if k == "bin":
        op = node[1]
        if op == "&&":
            return 1.0 if (_truthy(_ev(node[2], w)) and _truthy(_ev(node[3], w))) else 0.0
        if op == "||":
            return 1.0 if (_truthy(_ev(node[2], w)) or _truthy(_ev(node[3], w))) else 0.0
        a, b = _ev(node[2], w), _ev(node[3], w)
        if op == "+":
            if isinstance(a, str) or isinstance(b, str):
                return f"{_s(a)}{_s(b)}"
            return _num(a) + _num(b)
        if op == "-":
            return _num(a) - _num(b)
        if op == "*":
            return _num(a) * _num(b)
        if op == "/":
            return _num(a) / _num(b) if _num(b) else 0.0
        if op == "==":
            return 1.0 if _s(a) == _s(b) else 0.0
        if op == "!=":
            return 0.0 if _s(a) == _s(b) else 1.0
        if op == "<":
            return 1.0 if _num(a) < _num(b) else 0.0
        if op == ">":
            return 1.0 if _num(a) > _num(b) else 0.0
        if op == "<=":
            return 1.0 if _num(a) <= _num(b) else 0.0
        if op == ">=":
            return 1.0 if _num(a) >= _num(b) else 0.0
    return 0.0


def _s(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (tuple, list)):
        return "(" + ", ".join(_s(float(x)) for x in v) + ")"
    return str(v)


def _list_eq(a, b):
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-9
    except Exception:
        pass
    return _s(a) == _s(b) or str(a) == str(b)


def _list_op(w, parts, args):
    """Handle Unity-style list methods on a variable that holds a list:
    add/get/set/remove/removeAt/contains/indexOf/clear/pop/last and .count.
    Returns (handled, value)."""
    if not parts:
        return (False, None)
    lst = w.vars.get(parts[0])
    if not isinstance(lst, list):
        return (False, None)
    if len(parts) == 1:
        return (True, lst)
    m = parts[1].lower()
    a0 = args[0] if args else None
    if m in ("count", "length", "size"):
        return (True, float(len(lst)))
    if m in ("add", "append", "push"):
        if args:
            lst.append(a0)
        return (True, float(len(lst)))
    if m == "get":
        i = int(_num(a0)) if args else 0
        return (True, lst[i] if 0 <= i < len(lst) else 0)
    if m == "set":
        i = int(_num(a0)) if args else -1
        if len(args) > 1 and 0 <= i < len(lst):
            lst[i] = args[1]
        return (True, 0.0)
    if m in ("removeat", "remove_at"):
        i = int(_num(a0)) if args else -1
        if 0 <= i < len(lst):
            lst.pop(i)
        return (True, 0.0)
    if m == "remove":
        for idx, e in enumerate(lst):
            if _list_eq(e, a0):
                lst.pop(idx)
                break
        return (True, 0.0)
    if m in ("contains", "has"):
        return (True, 1.0 if any(_list_eq(e, a0) for e in lst) else 0.0)
    if m in ("indexof", "index_of"):
        for idx, e in enumerate(lst):
            if _list_eq(e, a0):
                return (True, float(idx))
        return (True, -1.0)
    if m == "clear":
        lst.clear()
        return (True, 0.0)
    if m == "last":
        return (True, lst[-1] if lst else 0)
    if m == "pop":
        return (True, lst.pop() if lst else 0)
    return (True, 0.0)


def _resolve(parts, args, w):
    p = [x.lower() for x in parts]
    if p and p[0] == "getproperty":
        if p[1:2] == ["distance"] and len(args) >= 2:
            import math
            ax, ay, az = _as_vec(args[0])
            bx, by, bz = _as_vec(args[1])
            return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)
        if p[1:2] == ["tag"] and args:
            # getProperty.tag("door") used as properties.get(svc: X, ...)'s
            # selector - folds the wanted tag into the selector string (same
            # trick as "position.x") so _get_property can check it against
            # the ONE object svc: already picked out. Different from
            # properties.get.tag("door"), which searches every object in
            # the world for one with a matching tag instead of checking a
            # specific, already-known object.
            return "tag." + _s(args[0])
        return ".".join(p[1:])                # a selector string (position, scale.y, ...)
    if p == ["properties", "get"]:
        if len(args) < 2:
            return 0.0
        return _get_property(w.get(args[0]), _s(args[1]))
    if p == ["properties", "get", "tag"]:
        # one object with this tag (whichever is found first), or "" if none
        if not args:
            return ""
        tag = _s(args[0])
        for obj in w.objects.values():
            if getattr(obj, "tag", "") == tag:
                return obj.svc
        return ""
    if p == ["properties", "get", "tags"]:
        # EVERY object with this tag, as a real list - .count/.get(i)/etc all
        # work on it immediately, and it only ever contains objects that are
        # still alive right now (destroy() really removes them from w.objects,
        # so a dead enemy just silently drops out - no more exists() checks)
        if not args:
            return []
        tag = _s(args[0])
        return [obj.svc for obj in w.objects.values() if getattr(obj, "tag", "") == tag]
    if p == ["raycast", "cast"]:
        # raycast.cast(mazeID, fromSvc)  OR  raycast.cast(mazeID, x, y, angleDeg)
        import math
        maze = w.mazes.get(_mid(args[0])) if args else None
        if maze is None:
            return 0.0
        ignore = None
        if len(args) >= 4:
            x, y, ang = _num(args[1]), _num(args[2]), math.radians(_num(args[3]))
        elif len(args) >= 2:
            o = w.get(args[1])
            if o is None:
                return 0.0
            x, y, ang, ignore = o.x, o.y, math.radians(o.rot), o.svc
        else:
            return 0.0
        cs = maze.cellsize
        try:
            obj, dist, hx, hy, wall = maze.hitscan(x / cs, y / cs, ang, ignore=ignore)
        except Exception:
            return 0.0
        w.vars["hitDist"] = dist * cs
        w.vars["hitX"] = hx * cs
        w.vars["hitY"] = hy * cs
        if obj is not None:
            w.vars["hitSvc"] = obj.svc
            w.vars["hitType"] = 1.0
            try:
                return float(obj.svc)
            except (TypeError, ValueError):
                return 1.0
        w.vars["hitSvc"] = 0
        w.vars["hitType"] = 2.0 if wall else 0.0
        return 0.0
    if p == ["physics", "push"]:
        # physics.push(svc, {dx, dy, dz}) - add velocity (launch a bullet, shove, ...)
        if len(args) < 2:
            return 0.0
        o = w.get(args[0])
        if o is None:
            return 0.0
        vec = _as_vec(args[1])
        o.vx = getattr(o, "vx", 0.0) + vec[0]
        o.vy = getattr(o, "vy", 0.0) + vec[1]
        o._hitwall = False
        return 1.0
    if p == ["physics", "stop"]:
        o = w.get(args[0]) if args else None
        if o is not None:
            o.vx = o.vy = 0.0
        return 0.0
    if p == ["physics", "hitwall"]:
        o = w.get(args[0]) if args else None
        return 1.0 if (o is not None and getattr(o, "_hitwall", False)) else 0.0
    if p == ["mesh", "import"]:
        # mesh.import("path.obj", meshID [, targetCells]) -> parse OBJ (+MTL),
        # flatten to a raycaster grid, store as meshID. Returns 1 on success.
        if len(args) < 2:
            return 0.0
        import glassmesh
        path = _mesh_resolve_path(_s(args[0]))
        mid = _mid(args[1])
        tc = int(_num(args[2])) if len(args) > 2 else 40
        try:
            md = glassmesh.import_obj(path, mid, target_cells=max(4, tc))
            w.meshes[mid] = md
            return 1.0
        except Exception:
            return 0.0
    if p == ["mesh", "create"]:
        # mesh.create(meshID) -> confirm the mesh is imported and ready to render
        # (the actual render happens via  raycast { mesh: meshID }  ). Returns 1/0.
        if not args:
            return 0.0
        import glassmesh
        return 1.0 if glassmesh.has(_mid(args[0])) else 0.0
    if p == ["mesh", "createcollider"]:
        # mesh.createCollider(meshID) -> register the flattened grid as a collider
        # so objects/nav collide with the mesh even without a visible raycaster.
        if not args:
            return 0.0
        import glassmesh
        mid = _mid(args[0])
        md = glassmesh.get(mid)
        if md is None:
            return 0.0
        if mid not in w.mazes:                    # don't clobber a live raycaster
            w.mazes[mid] = glassmesh.MeshCollider(md)
        return 1.0

        # lightmap.generate("mazeID") -> bake + save that maze's lightmap PNG, return path
        if not args:
            return ""
        maze = w.mazes.get(_mid(args[0]))
        if maze is None or not hasattr(maze, "save_lightmap"):
            return ""
        try:
            maze._lightmap_path = None            # force a fresh bake
            maze._lm_img = None
            maze.lightgrid = None
            return maze.save_lightmap() or ""
        except Exception:
            return ""
    if p == ["lightmap", "grab"]:
        # lightmap.grab("mazeID") -> path to that maze's baked lightmap (bakes if needed)
        if not args:
            return ""
        maze = w.mazes.get(_mid(args[0]))
        if maze is None or not hasattr(maze, "lightmap_path"):
            return ""
        try:
            return maze.lightmap_path() or ""
        except Exception:
            return ""
    if p == ["nav", "setdestination"]:
        # nav.setDestination(mazeID, svc, tx, ty [, speed])  -> go to a point
        # nav.setDestination(mazeID, svc, targetSvc)         -> go to an object
        if len(args) < 3:
            return 0.0
        mid, svc = args[0], args[1]
        if len(args) >= 4:
            tx, ty = _num(args[2]), _num(args[3])
            speed = _num(args[4]) if len(args) >= 5 else 1.5
        else:
            tgt = w.get(args[2])
            if tgt is None:
                return 0.0
            tx, ty, speed = tgt.x, tgt.y, 1.5
        return float(w.nav_set_destination(mid, svc, tx, ty, speed))
    if p == ["nav", "follow"]:
        # nav.follow(mazeID, svc, targetSvc [, speed]) - head to an object's current
        # spot (call each frame, or every few, to chase a moving target)
        if len(args) < 3:
            return 0.0
        tgt = w.get(args[2])
        if tgt is None:
            return 0.0
        speed = _num(args[3]) if len(args) >= 4 else 1.5
        return float(w.nav_set_destination(args[0], args[1], tgt.x, tgt.y, speed))
    if p in (["nav", "reachable"], ["nav", "isreachable"]):
        if len(args) < 3:
            return 0.0
        src = w.get(args[1])
        if src is None:
            return 0.0
        if len(args) >= 4:
            tx, ty = _num(args[2]), _num(args[3])
        else:
            tgt = w.get(args[2])
            if tgt is None:
                return 0.0
            tx, ty = tgt.x, tgt.y
        return float(w.nav_reachable(args[0], src.x, src.y, tx, ty))
    if p == ["nav", "stop"]:
        if args:
            w.nav_agents.pop(_svc_str(args[0]), None)
        return 0.0
    if p == ["nav", "arrived"]:
        if not args:
            return 0.0
        ag = w.nav_agents.get(_svc_str(args[0]))
        if ag is None:
            return 1.0                                # no active path = idle/arrived
        return 1.0 if ag.get("done") else 0.0
    if p == ["nav", "remainingdistance"]:
        if not args:
            return 0.0
        ag = w.nav_agents.get(_svc_str(args[0]))
        obj = w.get(args[0])
        if ag is None or obj is None:
            return 0.0
        maze = w.mazes.get(ag["maze"])
        if maze is None:
            return 0.0
        import math
        cs = maze.cellsize
        path, i = ag["path"], ag["i"]
        if i >= len(path):
            return 0.0
        wx, wy = (path[i][0] + 0.5) * cs, (path[i][1] + 0.5) * cs
        total = math.hypot(wx - obj.x, wy - obj.y)
        for j in range(i, len(path) - 1):
            ax, ay = (path[j][0] + 0.5) * cs, (path[j][1] + 0.5) * cs
            bx, by = (path[j + 1][0] + 0.5) * cs, (path[j + 1][1] + 0.5) * cs
            total += math.hypot(bx - ax, by - ay)
        return total
    if p == ["exists"]:
        return 1.0 if (args and w.get(args[0]) is not None) else 0.0
    if p == ["clone"]:
        if not args:
            return 0
        x = _num(args[1]) if len(args) > 1 else None
        y = _num(args[2]) if len(args) > 2 else None
        return w.clone(args[0], x, y)
    if p == ["destroy"]:
        return w.destroy(args[0]) if args else 0.0
    if p == ["light", "setcolor"]:
        if len(args) < 2:
            return 0.0
        lid = int(_num(args[0]))
        maze = w._light_owner.get(lid)
        L = maze.dynamic_lights.get(lid) if maze is not None else None
        if L is None:
            return 0.0
        L["r"], L["g"], L["b"] = _hex_to_rgb01(args[1])
        return 1.0
    if p == ["light", "setpos"]:
        if len(args) < 3:
            return 0.0
        lid = int(_num(args[0]))
        maze = w._light_owner.get(lid)
        L = maze.dynamic_lights.get(lid) if maze is not None else None
        if L is None:
            return 0.0
        L["x"], L["y"] = _num(args[1]), _num(args[2])
        L["parent"] = None      # a manual move overrides any parent tracking
        return 1.0
    if p == ["light", "destroy"]:
        if not args:
            return 0.0
        lid = int(_num(args[0]))
        maze = w._light_owner.pop(lid, None)
        if maze is not None:
            maze.dynamic_lights.pop(lid, None)
            return 1.0
        return 0.0
    if p == ["alert"]:
        message = _s(args[0]) if args else ""
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(w.host, "Glass", message)
        except Exception:
            pass
        return 0.0
    if p in (["spawnobject"], ["spawn", "object"]):
        look = args[0] if args else "#ffffff"
        x = _num(args[1]) if len(args) > 1 else 0.0
        y = _num(args[2]) if len(args) > 2 else 0.0
        col = _truthy(args[3]) if len(args) > 3 else False
        sc = _num(args[4]) if len(args) > 4 else 1.0
        op = _num(args[5]) if len(args) > 5 else 1.0
        return w.spawn_object(look, x, y, col, sc, op)
    if p == ["collide", "createmesh"]:
        mid = _mid(args[0]) if args else None
        return 1.0 if (mid is not None and mid in w.mazes) else 0.0
    if p == ["collide", "detect"]:
        if len(args) < 2:
            return 0.0
        obj = w.get(args[0])
        maze = w.mazes.get(_mid(args[1]))
        if obj is None or maze is None:
            return 0.0
        try:
            return 1.0 if maze._solid(obj.x / maze.cellsize, obj.y / maze.cellsize) else 0.0
        except Exception:
            return 0.0
    if p == ["lerp"] and len(args) >= 3:
        t = _num(args[2])
        a, b = args[0], args[1]
        if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
            av, bv = _as_vec(a), _as_vec(b)
            return [av[i] + (bv[i] - av[i]) * t for i in range(3)]
        a, b = _num(a), _num(b)
        return a + (b - a) * t
    if p in (["lerpangle"], ["lerpAngle".lower()]) and len(args) >= 3:
        a, b, t = _num(args[0]), _num(args[1]), _num(args[2])
        d = ((b - a + 180.0) % 360.0) - 180.0     # shortest way round the circle
        return a + d * t
    if p == ["slerp"] and len(args) >= 3:
        import math
        a, b, t = _as_vec(args[0]), _as_vec(args[1]), _num(args[2])
        la = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
        lb = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
        if la < 1e-9 or lb < 1e-9:                # a zero vector -> plain lerp
            return [a[i] + (b[i] - a[i]) * t for i in range(3)]
        ua = [a[i] / la for i in range(3)]
        ub = [b[i] / lb for i in range(3)]
        dot = max(-1.0, min(1.0, ua[0] * ub[0] + ua[1] * ub[1] + ua[2] * ub[2]))
        theta = math.acos(dot)
        mag = la + (lb - la) * t
        if theta < 1e-6:                          # nearly aligned -> lerp the dirs
            res = [ua[i] + (ub[i] - ua[i]) * t for i in range(3)]
        else:
            s = math.sin(theta)
            w1, w2 = math.sin((1 - t) * theta) / s, math.sin(t * theta) / s
            res = [ua[i] * w1 + ub[i] * w2 for i in range(3)]
        return [res[i] * mag for i in range(3)]
    if p == ["clamp"] and len(args) >= 3:
        v, lo, hi = _num(args[0]), _num(args[1]), _num(args[2])
        return max(lo, min(hi, v))
    if p == ["min"] and args:
        return min(_num(a) for a in args)
    if p == ["max"] and args:
        return max(_num(a) for a in args)
    if p == ["abs"] and args:
        return abs(_num(args[0]))
    if p == ["sqrt"] and args:
        import math as _m
        return _m.sqrt(max(0.0, _num(args[0])))
    if p in (["sin"], ["cos"], ["floor"], ["round"]) and args:
        import math as _m
        v = _num(args[0])
        return {"sin": _m.sin, "cos": _m.cos,
                "floor": lambda x: float(_m.floor(x)),
                "round": lambda x: float(round(x))}[p[0]](v)
    if p == ["random"] or p == ["rand"]:
        import random as _r
        if len(args) >= 2:
            return _r.uniform(_num(args[0]), _num(args[1]))
        if len(args) == 1:
            return _r.uniform(0.0, _num(args[0]))
        return _r.random()
    if p == ["burst"]:                              # spawn a particle burst at x,y
        x = _num(args[0]) if args else 0.0
        y = _num(args[1]) if len(args) > 1 else 0.0
        color = _s(args[2]) if len(args) > 2 else "#ffcb6b"
        count = int(_num(args[3])) if len(args) > 3 else 40
        speed = _num(args[4]) if len(args) > 4 else 150.0
        is_3d = len(args) > 5 and _s(args[5]).strip().lower() in ("true", "1", "yes")
        lit = len(args) > 6 and _s(args[6]).strip().lower() in ("true", "1", "yes")
        bounceA = _num(args[7]) if len(args) > 7 else 0.0
        size_over_life = _num(args[8]) if len(args) > 8 else 0.0
        if is_3d:
            # WORLD-space particles - a RaycasterWidget projects and draws
            # these itself each frame with its own camera, so they land at
            # the right screen spot, scale with distance, and hide behind
            # walls, instead of a flat 2D overlay ignoring the 3D view.
            # lit=true additionally shades them with the SAME baked lightmap
            # sample walls/billboards already use (see _cell_light) - no
            # separate lighting model, so they match everything around them.
            # color can be a texture path instead of a #hex string, same
            # auto-detect spawnObject already uses elsewhere in this file.
            import math, random
            for _ in range(max(0, count)):
                # an upward-biased cone (not a flat ring) so gravity/bounce
                # actually has something to arc and land - a pure horizontal
                # spray would just sit at floor height with nothing to fall.
                elev = math.radians(random.uniform(-75, -15))
                az = random.uniform(0, 2 * math.pi)
                sp = speed * random.uniform(0.5, 1.0)
                sp_h = math.cos(elev) * sp
                w.particles_3d.append({
                    "x": x, "y": y, "z": 0.0,
                    "vx": math.cos(az) * sp_h, "vy": math.sin(az) * sp_h,
                    "vz": -math.sin(elev) * sp,
                    "age": 0.0, "life": 1.3 * random.uniform(0.7, 1.05),
                    "color": color, "size": 5.0, "gravity": 60.0, "lit": lit,
                    "bounceA": bounceA, "sizeOverLife": size_over_life,
                })
        else:
            w.burst_queue.append({"x": x, "y": y, "color": color,
                                  "count": count, "speed": speed})
        return 0.0
    if p == ["create"]:
        sprite = _s(args[0]) if args else ""
        x = _num(args[1]) if len(args) > 1 else 0.0
        y = _num(args[2]) if len(args) > 2 else 0.0
        ww = _num(args[3]) if len(args) > 3 else 0.0
        hh = _num(args[4]) if len(args) > 4 else 0.0
        return w.create(sprite, x, y, ww, hh)
    if p == ["time", "normal"]:
        return w.time.normal
    if p == ["time", "held"]:
        return w.time.held
    if p == ["screen", "width"]:
        return float(SCREEN[0])
    if p == ["screen", "height"]:
        return float(SCREEN[1])
    if p == ["dumr"]:
        return 1.0 if w.meets_ram_requirement else 0.0
    if p == ["ram", "overlimit"]:
        return 1.0 if w.ram_over_limit else 0.0
    if p[0] in ("pref", "preference"):
        import prefs
        if p[-1] == "save" and args:
            value = args[1] if len(args) > 1 else 0
            prefs.save(args[0], value)
            return value
        if p[-1] == "load":
            return prefs.load(args[0], 0) if args else 0
        return 0
    if p[0] == "mouse" and len(p) > 1:
        sub = p[1]
        if sub == "down":
            return 1.0 if w.mouse.get("down") else 0.0
        return _num(w.mouse.get(sub, 0.0))
    if p[0] == "cursor" and len(p) > 1:
        c = w.cursor
        sub = p[1]
        if sub == "lock":
            c["lock"] = True
        elif sub == "unlock":
            c["lock"] = False
        elif sub == "hide":
            c["hide"] = True
        elif sub == "show":
            c["hide"] = False
        elif sub == "confine":
            c["confine"] = True
        elif sub in ("free", "unconfine", "release"):
            c["confine"] = False
        return 0
    if p[0] in ("physics", "physcis") and p[-1] == "gravity":
        if args is not None and len(args) >= 1:
            w.physics.gravity = _num(args[0])
            return w.physics.gravity
        return w.physics.gravity * w.time.normal     # pre-multiplied by time
    if p == ["input", "getheld"]:
        return 1.0 if (args and w.input.get_held(args[0])) else 0.0
    if p == ["input", "getclick"]:
        return 1.0 if (args and w.input.get_click(args[0])) else 0.0
    if p == ["adjvcr", "detect"]:
        if args and len(args) >= 2:
            return 1.0 if w.detect(args[0], args[1]) else 0.0
        return 1.0 if (args and w.detect(args[0])) else 0.0
    if p == ["adjvcr"] and args is not None:
        rot = args[0] if len(args) > 0 else (0, 0, 0)
        pos = args[1] if len(args) > 1 else (0, 0, 0)
        scale = args[2] if len(args) > 2 else None
        svc = args[3] if len(args) > 3 else None
        w.adjust(svc, _seq(rot), _seq(pos), _seq(scale) if scale is not None else None)
        return 0.0
    if len(parts) == 1:
        if parts[0] in w.vars:
            return w.vars[parts[0]]
        for obj in w.objects.values():
            if getattr(obj, "name", None) == parts[0]:
                # an unquoted bare name that isn't a script variable but
                # DOES match an object's label - let it resolve to that
                # name rather than silently returning 0. This is exactly
                # what makes parent: monster1 work without quotes, matching
                # raycast{}'s own parent: player convention; previously
                # this silently fell back to 0 (a plain, undefined-variable
                # default), which made a parented sound/light quietly
                # collapse to flat/2D with no error at all.
                return parts[0]
        if parts[0] in getattr(w, "post_profiles", {}):
            # same idea, for loadPost(testing)/removePost(testing) - "testing"
            # matches a *.postEffect { post.name = "testing" } profile, so it
            # resolves to that name instead of silently becoming 0
            return parts[0]
        return 0.0
    return 0.0


def _seq(v):
    if isinstance(v, (list, tuple)):
        return [float(_num(x)) for x in v]
    return [float(_num(v))]


_LOOP_CAP = 100000  # safety: a loop can't run more than this per frame


def _exec(stmt, w):
    k = stmt[0]
    if k == "expr":
        _ev(stmt[1], w)
    elif k == "assign":
        name, op, e = stmt[1], stmt[2], stmt[3]
        val = _ev(e, w)
        if op == "=":
            w.vars[name] = val
        else:
            cur = w.vars.get(name, 0)
            if isinstance(cur, str) or isinstance(val, str):
                if op == "+=":
                    w.vars[name] = f"{_s(cur)}{_s(val)}"
            else:
                c, v = _num(cur), _num(val)
                w.vars[name] = {"+=": c + v, "-=": c - v,
                                "*=": c * v, "/=": c / v if v else c}[op]
    elif k == "if":
        body = stmt[2] if _truthy(_ev(stmt[1], w)) else stmt[3]
        for s in body:
            _exec(s, w)
    elif k == "block":
        for s in stmt[1]:
            _exec(s, w)
    elif k == "return":
        val = _ev(stmt[1], w)
        if stmt[2]:
            val = _coerce(val, stmt[2])
        raise _Return(val)
    elif k == "after":
        import time as _t
        secs = _num(_ev(stmt[1], w))
        w.timers.append({"at": _t.monotonic() + max(0.0, secs), "body": stmt[2]})
    elif k == "repeat":
        n = int(_num(_ev(stmt[1], w)))
        for _i in range(max(0, min(n, _LOOP_CAP))):
            for s in stmt[2]:
                _exec(s, w)
    elif k == "for":
        name, a, b = stmt[1], int(_num(_ev(stmt[2], w))), int(_num(_ev(stmt[3], w)))
        step = 1 if b >= a else -1
        count = 0
        i = a
        while (i <= b if step > 0 else i >= b) and count < _LOOP_CAP:
            w.vars[name] = float(i)
            for s in stmt[4]:
                _exec(s, w)
            i += step
            count += 1


def run_script(src, world):
    try:
        for s in _P(_tok(src)).block():
            _exec(s, world)
    except Exception:
        pass     # a bad frame must never crash the browser
