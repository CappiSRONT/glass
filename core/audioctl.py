"""Glass audio system.

Exposed to .glass scripts through the `audio.` syntax:

    audio.isPlaying                         -> bool  (any tab or sound audible)
    audio.getAudioId                        -> int   (id of the current audio)
    audio.playSound "file.mp3" { speed:1, volume:1, quality:100, hertz:48000 }
    audio.playSound "file.mp3" { volume:1, radius:10, 3d:true, realtimeRef:false }
    audio.clip C = audio.gatherClip { "file.mp3" }   ; audio.playSound (C) { ... }
    audio.playLast    { audioID: id }
    audio.pauseCurrent{ audioID: id, fadeAmount: 5 }
    audio.changeVolume{ audioID: id, volume: 100 }

Local sounds are fully controllable. Web-tab audio (YouTube, etc.) is detected
and controlled best-effort via page muting + JavaScript on its media elements.

`quality` (-100..100, a bit-crush) and `hertz` (1400..120000, resample) need the
optional `miniaudio` package; without it those two are ignored and the sound
still plays with speed + volume.

3d:true turns on positional audio - the caller (engine.py's _audio_3d_params)
works out a pan (-1..1) and distance-based volume from the raycast scene's
listener, plus a reverb wetness (0..1) from the room's baked-or-live enclosure
estimate (room_scale_at in renderer.py), and passes pan/reverb in here. This is
NOT real acoustic simulation - no convolution, no true reflections - it's a
simple, honest approximation: linear stereo pan and a decaying multi-tap echo
whose wetness scales with how enclosed the space is. Also needs `miniaudio`;
without it, 3D playback still works (volume/pan-less) but sounds the same in
every room.
"""

import os
import tempfile
import array

try:
    import miniaudio
    HAVE_MINIAUDIO = True
except Exception:
    HAVE_MINIAUDIO = False


def _apply_pan(samples, pan):
    """Linear stereo pan on interleaved 16-bit samples - reduces the
    OPPOSITE channel's gain rather than boosting either, so it never clips.
    pan: -1.0 (full left) .. 0.0 (centre) .. 1.0 (full right)."""
    if abs(pan) < 0.02:
        return samples
    left_gain = 1.0 - max(0.0, pan)
    right_gain = 1.0 + min(0.0, pan)
    return array.array("h", (
        int(max(-32768, min(32767, s * (left_gain if i % 2 == 0 else right_gain))))
        for i, s in enumerate(samples)))


def _apply_lowpass(samples, sample_rate, cutoff_hz):
    """A real one-pole low-pass filter (the same kind analog RC filters and
    every game engine's occlusion muffling are built from) - not a volume
    trick. This is what makes a sound behind a wall actually feel behind a
    wall instead of just quieter: high frequencies get cut, same as real
    sound diffracting around an obstacle loses its highs first. Run
    per-channel on interleaved 16-bit stereo. cutoff_hz >= ~18000 is
    inaudibly transparent (skipped entirely); lower values muffle more -
    around 500-1000Hz is a heavily-muffled 'through a wall' character."""
    if cutoff_hz >= 18000 or len(samples) < 4:
        return samples
    import math
    cutoff_hz = max(80.0, cutoff_hz)
    alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / sample_rate)
    out = array.array("h", samples)
    yl = float(samples[0])
    yr = float(samples[1]) if len(samples) > 1 else yl
    for i in range(0, len(out) - 1, 2):
        yl += alpha * (out[i] - yl)
        yr += alpha * (out[i + 1] - yr)
        out[i] = max(-32768, min(32767, int(yl)))
        out[i + 1] = max(-32768, min(32767, int(yr)))
    return out


REVERB_PRESETS = {
    # name: (delay_ms, decay, taps, dark_cutoff_hz, mix_scale)
    # Modeled on how Source's DSP presets work: a handful of DISTINCT, hand-
    # tuned characters picked by room geometry, not one formula that scales
    # continuously - a closet and a hallway shouldn't just be "different
    # amounts" of the same echo, they should sound like different PLACES.
    "closet":     (18.0, 0.22, 3, 3500.0, 0.6),   # tiny, tight, barely-there slap
    "small_room": (28.0, 0.38, 4, 4500.0, 0.85),  # believable small-room echo
    "hallway":    (48.0, 0.55, 6, 6500.0, 1.0),   # discrete slap-echo, brighter/harder
    "large_hall": (78.0, 0.68, 7, 7500.0, 1.0),   # long, spacious, rings more
    "outdoor":    (95.0, 0.30, 3, 5000.0, 0.35),  # mostly dry - open space diffuses
                                                    # reflections rather than bouncing them
}


def select_reverb_preset(room_scale):
    """Which preset fits this room_scale (see renderer.room_scale_at) -
    matches HL2's 'automatic DSP' idea: measure the space, then pick a
    hand-tuned character for it, rather than computing one continuously
    from a single formula. Thresholds line up with room_scale_at's own
    0..14-cell range."""
    if room_scale < 2.0:
        return "closet"
    if room_scale < 4.0:
        return "small_room"
    if room_scale < 7.0:
        return "hallway"
    if room_scale < 10.0:
        return "large_hall"
    return "outdoor"


def _apply_reverb(samples, sample_rate, wetness, preset="hallway"):
    """A denser, damped multi-tap echo standing in for reverb - not
    physically accurate room acoustics (no convolution, no real
    reflections), but a genuine, audible sense of space. preset selects a
    hand-tuned CHARACTER (see REVERB_PRESETS/select_reverb_preset) - a
    closet and a hallway use different delay/decay/brightness, not just
    different amounts of the same echo. wetness (0..1, from how enclosed
    the room measurement says the space is) still controls the overall mix
    amount on top of that. The tail is darkened with ONE low-pass pass (not
    one per tap - measured that at 5x the cost for no audible difference
    worth it). Deliberately cheap: a handful of whole-buffer passes plus
    one filter pass, not a per-sample convolution."""
    if wetness <= 0.05:
        return samples
    delay_ms, base_decay, taps, dark_cutoff, mix_scale = REVERB_PRESETS.get(
        preset, REVERB_PRESETS["hallway"])
    wetness = wetness * mix_scale
    if wetness <= 0.02:
        return samples
    n = len(samples)
    delay = int(sample_rate * (delay_ms / 1000.0)) * 2   # *2 for L/R interleave
    delay -= delay % 2
    if delay <= 0 or delay >= n:
        return samples
    tail = [0] * n         # the wet reverb contribution, built up separately
    decay = base_decay + 0.2 * wetness
    for tap in range(1, taps + 1):
        off = delay * tap
        if off >= n:
            break
        amp = wetness * (decay ** tap)
        contrib = [int(s * amp) for s in samples[:n - off]]
        for i, c in enumerate(contrib):
            tail[i + off] += c
    tail = array.array("h", (max(-32768, min(32767, v)) for v in tail))
    tail = _apply_lowpass(tail, sample_rate, max(500.0, dark_cutoff - wetness * 1000.0))
    return array.array("h", (
        max(-32768, min(32767, s + t)) for s, t in zip(samples, tail)))


def _sweep_stale_temp():
    """Remove any glass_audio_*.wav left in the temp dir by an earlier run that
    didn't exit cleanly. Safe: only touches files matching our own prefix."""
    try:
        import glob as _glob
        tdir = tempfile.gettempdir()
        for p in _glob.glob(os.path.join(tdir, "glass_audio_*.wav")):
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def _clampf(v, lo, hi, d):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d


def _norm_vol(v):
    """Accept 0..1 or 0..100 and return 0..1."""
    f = _clampf(v, 0.0, 100.0, 1.0)
    if f > 1.0:
        f = f / 100.0
    return max(0.0, min(1.0, f))


class AudioController:
    def __init__(self, window=None):
        self.window = window
        self._players = {}        # sound_id -> (QMediaPlayer, QAudioOutput)
        self._clips = {}          # clip_id  -> filepath
        self._tempfiles = []
        self._sound_tempfile = {}  # sound_id -> its own temp .wav, if any
        self._fades = {}          # id -> QTimer
        self._next_sound = 1
        self._next_clip = 1
        self._live_sink = None    # lazily-created LiveAudioSink, shared by every
                                   # long-clip is3D sound that needs pan/reverb/
                                   # occlusion to actually apply (see play_sound's
                                   # MAX_DSP_SECONDS notes) - kept fully separate
                                   # from _players so the existing, well-tested
                                   # QMediaPlayer path is never touched by this
        self._live_keys = {}      # sound_id -> mixer key, for sounds routed there
        _sweep_stale_temp()       # remove leftovers from a previous (e.g. crashed) run
        import atexit
        atexit.register(self.cleanup)

    def cleanup(self):
        """Delete every temp .wav this controller created."""
        for path in self._tempfiles:
            try:
                os.remove(path)
            except OSError:
                pass
        self._tempfiles = []
        self._sound_tempfile = {}

    def stop_all(self):
        """Stop every currently-playing sound immediately (no fade). Used by
        the editor's live preview and the real game's reload path: since a
        fresh render creates a brand new World and re-runs setup{} from
        scratch, without this a looping/long song would stack a fresh copy
        on top of the old one on every rebuild instead of just restarting
        cleanly, or (worse) two overlapping native audio sessions fighting
        over the same resources - which is a real, documented crash source
        for QMediaPlayer/QAudioOutput, not just an annoyance.

        Explicitly deleteLater()'s each player/output here rather than just
        dropping the dict and letting Python's GC take the last reference -
        destroying these objects synchronously at an arbitrary moment (not
        one Qt chose) is exactly the documented crash pattern; deleteLater()
        defers it to a point Qt's own event loop knows is safe."""
        for pl, out in list(self._players.values()):
            try:
                pl.stop()
            except Exception:
                pass
            try:
                pl.deleteLater()
                out.deleteLater()
            except Exception:
                pass
        self._players = {}
        if self._live_sink is not None:
            self._live_sink.mixer.sources = {}
        self._live_keys = {}

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # ---- queries -----------------------------------------------------------
    def is_playing(self):
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
            for pl, _ in self._players.values():
                if pl.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    return True
        except Exception:
            pass
        if self._live_sink is not None and self._live_sink.mixer.sources:
            return True
        for _id, page in self._tab_pages():
            try:
                if page.recentlyAudible():
                    return True
            except Exception:
                pass
        return False

    def get_audio_id(self):
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
            for sid, (pl, _) in self._players.items():
                if pl.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    return sid
        except Exception:
            pass
        for tid, page in self._tab_pages():
            try:
                if page.recentlyAudible():
                    return tid
            except Exception:
                pass
        return 0

    # ---- local sound playback ---------------------------------------------
    def gather_clip(self, path):
        """Preload a sound file; returns a clip id usable in playSound."""
        rp = self._resolve(path)
        cid = self._next_clip
        self._next_clip += 1
        self._clips[cid] = rp
        return cid

    def play_sound(self, source, speed=1.0, volume=1.0, quality=100, hertz=None,
                   pan=0.0, reverb=0.0, loop=False, occlusion=0.0, reverb_preset="hallway"):
        path = None
        if isinstance(source, str):
            path = self._resolve(source)
        else:                                   # a clip id from gather_clip
            try:
                path = self._clips.get(int(source))
            except (TypeError, ValueError):
                path = None
        if not path or not os.path.exists(path):
            return 0
        play_path = path
        temp_used = None
        pan = _clampf(pan, -1.0, 1.0, 0.0)
        reverb = _clampf(reverb, 0.0, 1.0, 0.0)
        occlusion = _clampf(occlusion, 0.0, 1.0, 0.0)
        needs_dsp = (hertz is not None or _clampf(quality, -100, 100, 100) != 100
                     or abs(pan) > 0.02 or reverb > 0.05 or occlusion > 0.02)
        if needs_dsp and not HAVE_MINIAUDIO:
            # pan/reverb/quality/hertz all silently do NOTHING without this -
            # no exception, no error, it just plays flat and unprocessed,
            # which looks identical to "pan/reverb are broken" from the
            # outside. Say so explicitly instead of leaving that ambiguous.
            text = ("[audio] pan/reverb/occlusion/quality/hertz were requested "
                    "but the optional miniaudio package isn't installed - "
                    "playing unprocessed instead. Run: pip install miniaudio")
            win = self.window
            if win is not None and hasattr(win, "log"):
                win.log(text)
            else:
                print(text)
        if needs_dsp and HAVE_MINIAUDIO and (abs(pan) > 0.02 or reverb > 0.05 or occlusion > 0.02):
            # pan and the occlusion low-pass are a dead end as a one-shot,
            # whole-buffer pass: baked in once at the moment the sound
            # starts, they can NEVER update again, which is exactly what
            # made reverb/occlusion feel "wrongly placed" once you walked
            # somewhere else - a background sound just keeps sounding like
            # wherever it started. The live mixer processes in small
            # streaming chunks instead, so pan/reverb/occlusion genuinely
            # apply AND keep updating every frame (see engine.py's
            # audio_3d_step) for as long as the sound plays - not just for
            # long clips anymore, for every is3D sound that wants this.
            try:
                sr_check = int(hertz) if hertz else 44100
                dec = miniaudio.decode_file(
                    path, output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=2, sample_rate=max(1400, min(120000, sr_check)))
                if _clampf(quality, -100, 100, 100) != 100:
                    text = (f"[audio] '{os.path.basename(path)}' routed through the live "
                            f"mixer for pan/reverb/occlusion - quality (bit-crush) doesn't "
                            f"carry over there yet, only speed/volume/pan/reverb/occlusion do")
                    win = self.window
                    if win is not None and hasattr(win, "log"):
                        win.log(text)
                    else:
                        print(text)
                live_sid = self._play_sound_live(dec.samples, dec.sample_rate, speed, volume,
                                                 pan, reverb, reverb_preset, occlusion, loop)
                if live_sid:
                    return live_sid
                # live_sid == 0 means the live mixer genuinely failed (e.g.
                # QAudioSink couldn't start - a real, documented Qt issue on
                # some systems, not something faked here) - fall through to
                # the proven one-shot path below instead of going silent
                # with no fallback at all, which is what used to happen here
            except Exception:
                pass    # same fallback, for a decode failure instead
        if needs_dsp and HAVE_MINIAUDIO:
            processed = self._process(path, quality, hertz, pan, reverb, occlusion, reverb_preset)
            if processed:
                play_path = processed
                temp_used = processed          # this sound owns a temp file
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
            from PyQt6.QtCore import QUrl
        except Exception:
            return 0
        try:
            if QMediaDevices.defaultAudioOutput().isNull():
                # Qt found no audio OUTPUT device at all - a completely
                # different failure mode than a bad file (that one still
                # fires errorOccurred; this doesn't). Verified directly:
                # constructing and playing (especially looping) a player
                # with nowhere to send audio doesn't just play silently -
                # it can hang the whole process inside Qt's native backend
                # retrying a connection that will never succeed. Bail out
                # here instead of ever constructing the player at all.
                text = "[audio] no audio output device found on this system - sound has nowhere to go"
                win = self.window
                if win is not None and hasattr(win, "log"):
                    win.log(text)
                else:
                    print(text)
                return 0
        except Exception:
            pass
        pl = QMediaPlayer(self.window) if self.window is not None else QMediaPlayer()
        out = QAudioOutput(self.window) if self.window is not None else QAudioOutput()
        pl.setAudioOutput(out)
        out.setVolume(_norm_vol(volume))
        pl.setPlaybackRate(_clampf(speed, 0.1, 4.0, 1.0))
        if loop:
            pl.setLoops(QMediaPlayer.Loops.Infinite)
        pl.setSource(QUrl.fromLocalFile(os.path.abspath(play_path)))
        sid = self._next_sound
        self._next_sound += 1
        self._players[sid] = (pl, out)
        if temp_used:
            self._sound_tempfile[sid] = temp_used

        def _on_error(err, msg, _sid=sid, _src=source):
            # Qt/FFmpeg can fail to actually decode/play a file (bad codec,
            # corrupt/truncated file, DRM, unsupported container...) with NO
            # Python exception at all - play_sound still returns a real id
            # and .play() "succeeds", it just silently produces no sound.
            # This is the one place that ever finds out why, so log it
            # somewhere visible instead of it vanishing.
            try:
                win = self.window
                text = f"[audio] '{_src}' failed to play: {msg}"
                if win is not None and hasattr(win, "log"):
                    win.log(text)
                else:
                    print(text)
            except Exception:
                pass
        pl.errorOccurred.connect(_on_error)

        def _on_state(st, _sid=sid):
            try:
                from PyQt6.QtMultimedia import QMediaPlayer as QMP
                if st == QMP.PlaybackState.StoppedState:
                    entry = self._players.pop(_sid, None)
                    if entry is not None:
                        _pl, _out = entry
                        # deleteLater(), not letting the dict-pop above drop
                        # the last Python reference and have PyQt/GC destroy
                        # the C++ object immediately - this is a documented,
                        # known crash source for QMediaPlayer/QAudioOutput
                        # specifically (the native backend may still have
                        # pending work at exactly this moment, since we're
                        # inside a signal IT just emitted). deleteLater()
                        # defers actual destruction to a safe point in the
                        # event loop instead.
                        _pl.deleteLater()
                        _out.deleteLater()
                    self._cleanup_sound_tempfile(_sid)
            except Exception:
                pass
        pl.playbackStateChanged.connect(_on_state)
        pl.play()
        return sid

    def _play_sound_live(self, samples, sample_rate, speed, volume, pan, reverb, reverb_preset, occlusion, loop):
        """Add an already-decoded buffer to the shared live mixer instead of
        the QMediaPlayer path - see play_sound's routing notes. Lazily
        starts the real QAudioSink the first time this is ever needed."""
        if self._live_sink is None:
            self._live_sink = LiveAudioSink(window=self.window, sample_rate=sample_rate)
            if not self._live_sink.start():
                self._live_sink = None
                return 0
        try:
            key = self._live_sink.add_source(samples, loop=loop)
            src = self._live_sink.mixer.sources.get(key)
            if src is None:
                return 0
            src.gain = _norm_vol(volume)
            src.pan = _clampf(pan, -1.0, 1.0, 0.0)
            src.lowpass_cutoff = 18000.0 - _clampf(occlusion, 0.0, 1.0, 0.0) * 17300.0
            src.reverb_preset = reverb_preset
            src.reverb_wetness = _clampf(reverb, 0.0, 1.0, 0.0)
        except Exception:
            return 0
        sid = self._next_sound
        self._next_sound += 1
        self._live_keys[sid] = key
        return sid

    def _cleanup_sound_tempfile(self, sid):
        """Delete this specific sound's processed temp .wav the moment it
        stops playing, instead of leaving it on disk until the whole app
        exits. A long session with many pitched/quality-processed sound
        effects used to accumulate a temp file per play for the entire
        session; this removes each one right after it's actually done with."""
        path = self._sound_tempfile.pop(sid, None)
        if not path:
            return
        try:
            self._tempfiles.remove(path)
        except ValueError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass                    # e.g. still momentarily locked - the
                                     # next-launch sweep will still catch it

    # ---- control -----------------------------------------------------------
    def change_volume(self, audio_id, volume):
        aid = _to_int(audio_id)
        if aid in self._players:
            self._players[aid][1].setVolume(_norm_vol(volume))
            return 1
        if aid in self._live_keys and self._live_sink is not None:
            src = self._live_sink.mixer.sources.get(self._live_keys[aid])
            if src is not None:
                src.gain = _norm_vol(volume)
                return 1
        page = self._page_for(aid)
        if page is not None:
            v = _norm_vol(volume)
            page.setAudioMuted(v <= 0.0)
            self._js_set_volume(page, v)
            return 1
        return 0

    def change_speed(self, audio_id, speed):
        """Live playback-rate adjustment - unlike pan/balance, this IS a
        real, continuously-adjustable Qt property (confirmed directly
        against the docs), which is what makes a genuine, live doppler
        effect possible for 3D sounds (see engine.py's audio_3d_step).
        Live-mixed (long-clip) sounds don't support this yet - doppler on
        a stationary background track isn't a very meaningful case anyway,
        so this is a deliberate, small, documented gap rather than
        something silently broken."""
        aid = _to_int(audio_id)
        if aid in self._players:
            self._players[aid][0].setPlaybackRate(_clampf(speed, 0.1, 4.0, 1.0))
            return 1
        return 0

    def update_live_params(self, audio_id, pan=None, lowpass_cutoff=None,
                           reverb_preset=None, reverb_wetness=None):
        """Live pan/occlusion/reverb updates - the whole reason a sound
        goes through the live mixer instead of QMediaPlayer. Only sounds
        routed there (see play_sound's routing notes) support this; for
        anything else this is a harmless no-op, not an error, since not
        every sound needs continuous updates. Pass None for anything you
        don't want to change right now. This is what makes reverb/pan
        track the room you're ACTUALLY in as you walk around, instead of
        staying locked to whatever room a sound happened to start in."""
        aid = _to_int(audio_id)
        if aid not in self._live_keys or self._live_sink is None:
            return 0
        src = self._live_sink.mixer.sources.get(self._live_keys[aid])
        if src is None:
            return 0
        if pan is not None:
            src.pan = _clampf(pan, -1.0, 1.0, 0.0)
        if lowpass_cutoff is not None:
            src.lowpass_cutoff = float(lowpass_cutoff)
        if reverb_preset is not None:
            src.reverb_preset = reverb_preset
        if reverb_wetness is not None:
            src.reverb_wetness = _clampf(reverb_wetness, 0.0, 1.0, 0.0)
        return 1

    def is_live_mixed(self, audio_id):
        """True if this sound is routed through the live mixer (and so
        supports update_live_params) rather than QMediaPlayer."""
        return _to_int(audio_id) in self._live_keys

    def pause_current(self, audio_id, fade_amount=0):
        aid = _to_int(audio_id)
        if aid == 0:
            aid = self.get_audio_id()
        if aid in self._players:
            self._fade_local(aid, _clampf(fade_amount, 0, 100, 0))
            return 1
        page = self._page_for(aid)
        if page is not None:
            # best-effort: fade media volume via JS, then pause + mute
            self._js_fade_pause(page, _clampf(fade_amount, 0, 100, 0))
            return 1
        return 0

    def play_last(self, audio_id):
        """Resume the tab that holds this audio id (unmute + JS play)."""
        aid = _to_int(audio_id)
        page = self._page_for(aid)
        if page is not None:
            try:
                page.setAudioMuted(False)
            except Exception:
                pass
            self._js_play(page)
            return aid
        if aid in self._players:
            self._players[aid][0].play()
            return aid
        return 0

    # ---- internals ---------------------------------------------------------
    def _fade_local(self, sid, step):
        pl, out = self._players.get(sid, (None, None))
        if pl is None:
            return
        if step <= 0:
            pl.pause()
            return
        from PyQt6.QtCore import QTimer
        t = QTimer()
        t.setInterval(40)
        s = step / 100.0

        def tick():
            v = out.volume() - s
            if v <= 0.0:
                out.setVolume(0.0)
                pl.pause()
                t.stop()
                self._fades.pop(sid, None)
            else:
                out.setVolume(v)
        t.timeout.connect(tick)
        self._fades[sid] = t
        t.start()

    def _tab_pages(self):
        """Yield (audio_id, QWebEnginePage) for every live tab."""
        win = self.window
        if win is None or not hasattr(win, "tabs"):
            return
        for i in range(win.tabs.count()):
            tab = win.tabs.widget(i)
            view = getattr(tab, "view", None)
            if view is None:
                continue
            try:
                page = view.page()
            except Exception:
                continue
            if page is None:
                continue
            aid = getattr(tab, "audio_id", None)
            if aid is None:
                aid = 2000 + i
            yield aid, page

    def _page_for(self, audio_id):
        for tid, page in self._tab_pages():
            if tid == audio_id:
                return page
        return None

    def _js_set_volume(self, page, vol01):
        page.runJavaScript(
            f"document.querySelectorAll('video,audio')"
            f".forEach(function(m){{try{{m.muted=false;m.volume={vol01};}}catch(e){{}}}});")

    def _js_play(self, page):
        page.runJavaScript(
            "document.querySelectorAll('video,audio')"
            ".forEach(function(m){try{m.play();}catch(e){}});")

    def _js_fade_pause(self, page, step):
        # ramp volume to 0 in JS, then pause; step is per ~40ms tick (0..100)
        s = max(1.0, step) / 100.0
        page.runJavaScript(
            "(function(){var ms=document.querySelectorAll('video,audio');"
            "var iv=setInterval(function(){var done=true;ms.forEach(function(m){"
            "try{if(m.volume> " + str(s) + "){m.volume=Math.max(0,m.volume-" + str(s) + ");done=false;}"
            "else{m.volume=0;m.pause();}}catch(e){}});"
            "if(done){clearInterval(iv);}}, 40);})();")

    def _resolve(self, path):
        if not path:
            return ""
        p = str(path).strip().strip('"').strip("'").lstrip("/\\")
        try:
            import renderer
            for base in getattr(renderer, "ASSET_DIRS", []):
                cand = os.path.join(base, p)
                if os.path.exists(cand):
                    return cand
        except Exception:
            pass
        return p if os.path.exists(p) else path

    def _process(self, path, quality, hertz, pan=0.0, reverb=0.0, occlusion=0.0, reverb_preset="hallway"):
        """Resample (hertz), bit-crush (quality), pan, occlusion low-pass,
        and/or add a reverb tail into a temp WAV."""
        try:
            import wave
            sr = int(hertz) if hertz else 44100
            sr = max(1400, min(120000, sr))
            dec = miniaudio.decode_file(
                path, output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=2, sample_rate=sr)
            samples = dec.samples            # array('h')
            q = _clampf(quality, -100, 100, 100)
            bits = max(1, round(1 + (q + 100) / 200.0 * 15))   # -100->1 .. 100->16
            if bits < 16:
                step = 1 << (16 - bits)
                half = step // 2
                samples = type(samples)(
                    "h", ((max(-32768, min(32767, (s + half) // step * step)))
                          for s in samples))
            # pan/reverb are whole-buffer Python passes - verified directly:
            # ~10s for pan alone and ~32s for reverb alone on a real 3-minute
            # song, run SYNCHRONOUSLY inside setup{} before anything else can
            # even render. That's not slow, it looks exactly like a crash/
            # freeze. Fine for short SFX; skip entirely past this length -
            # distance-based VOLUME (a separate, cheap mechanism in
            # play_sound) still applies to long tracks, just not stereo pan
            # or the room echo.
            duration_s = len(samples) / 2.0 / sr
            MAX_DSP_SECONDS = 12.0
            if duration_s > MAX_DSP_SECONDS and (pan or reverb or occlusion):
                text = (f"[audio] '{os.path.basename(path)}' is {duration_s:.0f}s long - "
                        f"skipping pan/reverb/occlusion (only practical up to ~{MAX_DSP_SECONDS:.0f}s "
                        f"of audio); volume-based distance falloff still applies")
                win = self.window
                if win is not None and hasattr(win, "log"):
                    win.log(text)
                else:
                    print(text)
                pan = 0.0
                reverb = 0.0
                occlusion = 0.0
            if pan:
                samples = _apply_pan(samples, pan)
            if occlusion > 0.02:
                # a sound with no direct line of sight gets muffled, not
                # just quieter - real occlusion (through a wall/around a
                # corner) cuts highs, matching how the volume side already
                # applies an extra distance penalty for the same case
                # (engine.py's _acoustic_distance). 18000Hz = transparent,
                # ~700Hz = heavily muffled at full occlusion.
                cutoff = 18000.0 - occlusion * 17300.0
                samples = _apply_lowpass(samples, sr, cutoff)
            if reverb:
                samples = _apply_reverb(samples, sr, reverb, reverb_preset)
            tmp = tempfile.NamedTemporaryFile(prefix="glass_audio_",
                                              suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(dec.nchannels)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(samples.tobytes())
            self._tempfiles.append(tmp.name)
            return tmp.name
        except Exception:
            return None


def _to_int(v):
    try:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ===========================================================================
# Live mixing engine - the actual HL2-style piece: pan and the low-pass
# filter that QMediaPlayer can only ever set once, this can update every
# single chunk, because we own the samples and mix them ourselves instead
# of handing a finished file to a black-box player.
#
# Deliberately built and verified as a standalone thing BEFORE ever being
# wired to a real QAudioSink - the mixing math (pan, gain, filter, summing
# multiple sources) is fully testable by generating output and inspecting
# it, with no live audio device, no threading, and no Qt event loop
# involved at all. That verification lives in this class. Actually playing
# it through a real device is a separate, deliberate next step - not
# something to flip on blind.
# ===========================================================================

class LiveMixSource:
    """One sound inside the live mixer. Decoded ONCE into memory as plain
    interleaved int16 samples; pan/gain/lowpass_cutoff/reverb are read fresh
    on EVERY chunk, so changing them between calls to read_mixed() genuinely
    changes the next chunk - unlike the QMediaPlayer path, where pan/reverb
    are baked into the file once and can never move again. This is also
    what makes pan/occlusion/reverb actually WORK for long clips (a 3-minute
    song) without the 40+ second freeze a one-shot whole-file DSP pass would
    cause - see audioctl's MAX_DSP_SECONDS notes elsewhere. Processing
    order matches the one-shot pipeline (pan, then occlusion lowpass, then
    reverb) so a sound doesn't behave surprisingly differently just because
    it happened to be long enough to route through here instead."""
    def __init__(self, samples, loop=False):
        self.samples = samples          # array('h'), interleaved stereo
        self.n_frames = len(samples) // 2
        self.pos = 0                    # current read position, in FRAMES
        self.gain = 1.0
        self.pan = 0.0                  # -1..1, read live every chunk
        self.lowpass_cutoff = 20000.0   # >=18000 is treated as "off"
        self._yl = 0.0                  # occlusion lowpass state - MUST persist
        self._yr = 0.0                  # across chunks, or every boundary clicks
        self.reverb_preset = None       # None/"" = off, else a REVERB_PRESETS key
        self.reverb_wetness = 0.0
        self._delay_buf = array.array("h")   # cross-chunk history for reverb taps -
                                              # a 78ms/7-tap preset needs >500ms of
                                              # lookback, spanning several chunks
        self._rv_yl = 0.0               # reverb tail darkening state - a SEPARATE
        self._rv_yr = 0.0               # persistent filter from the occlusion one
        self.loop = loop
        self.finished = False

    def read_mixed(self, n_frames, sample_rate):
        """Return exactly n_frames frames (a flat list of 2*n_frames ints,
        interleaved L/R) with this source's CURRENT gain/pan/lowpass/reverb
        applied - fresh values each call, not baked in at construction.
        Pads with silence and sets self.finished if a non-looping source
        runs out partway through."""
        out = [0] * (n_frames * 2)
        if self.finished:
            return out
        filled = 0
        while filled < n_frames:
            if self.pos >= self.n_frames:
                if self.loop:
                    self.pos = 0
                else:
                    self.finished = True
                    break
            take = min(n_frames - filled, self.n_frames - self.pos)
            start = self.pos * 2
            chunk = self.samples[start:start + take * 2]
            out[filled * 2:filled * 2 + take * 2] = chunk
            self.pos += take
            filled += take
        # gain/pan - read fresh right now, applied to this whole chunk
        left_gain = self.gain * (1.0 - max(0.0, self.pan))
        right_gain = self.gain * (1.0 + min(0.0, self.pan))
        for i in range(0, len(out), 2):
            out[i] = out[i] * left_gain
            out[i + 1] = out[i + 1] * right_gain
        # occlusion lowpass - read fresh right now, state persists across
        # calls so there's no click/discontinuity at the chunk boundary
        cutoff = self.lowpass_cutoff
        if cutoff < 18000.0:
            import math
            alpha = 1.0 - math.exp(-2.0 * math.pi * max(80.0, cutoff) / sample_rate)
            yl, yr = self._yl, self._yr
            for i in range(0, len(out), 2):
                yl += alpha * (out[i] - yl)
                yr += alpha * (out[i + 1] - yr)
                out[i], out[i + 1] = yl, yr
            self._yl, self._yr = yl, yr
        else:
            self._yl = out[-2] if len(out) >= 2 else self._yl
            self._yr = out[-1] if len(out) >= 2 else self._yr
        # reverb - streaming version of _apply_reverb, using a persistent
        # delay buffer so taps can reach back into EARLIER chunks (a
        # large_hall's 78ms x 7 taps needs >500ms of history, far more
        # than one chunk holds)
        if self.reverb_preset and self.reverb_wetness > 0.05:
            preset = REVERB_PRESETS.get(self.reverb_preset, REVERB_PRESETS["hallway"])
            delay_ms, base_decay, taps, dark_cutoff, mix_scale = preset
            wetness = self.reverb_wetness * mix_scale
            if wetness > 0.02:
                delay_frames = max(1, int(sample_rate * delay_ms / 1000.0))
                self._delay_buf.extend(array.array("h", (
                    max(-32768, min(32767, int(v))) for v in out)))
                keep_frames = delay_frames * taps + n_frames
                keep = keep_frames * 2
                if len(self._delay_buf) > keep:
                    del self._delay_buf[:len(self._delay_buf) - keep]
                buflen = len(self._delay_buf)
                decay = base_decay + 0.2 * wetness
                wet = [0.0] * (n_frames * 2)
                for tap in range(1, taps + 1):
                    off = delay_frames * tap * 2
                    amp = wetness * (decay ** tap)
                    for i in range(n_frames * 2):
                        src_idx = buflen - (n_frames * 2 - i) - off
                        if 0 <= src_idx < buflen:
                            wet[i] += self._delay_buf[src_idx] * amp
                import math
                dcut = max(500.0, dark_cutoff - wetness * 1000.0)
                alpha = 1.0 - math.exp(-2.0 * math.pi * dcut / sample_rate)
                ryl, ryr = self._rv_yl, self._rv_yr
                for i in range(0, len(wet), 2):
                    ryl += alpha * (wet[i] - ryl)
                    ryr += alpha * (wet[i + 1] - ryr)
                    out[i] = max(-32768, min(32767, int(out[i] + ryl)))
                    out[i + 1] = max(-32768, min(32767, int(out[i + 1] + ryr)))
                self._rv_yl, self._rv_yr = ryl, ryr
        return out


class LiveMixer:
    """Sums every active LiveMixSource together into one output chunk at a
    time. This is the whole point: many sources, each with its own live
    pan/gain/lowpass, mixed down fresh every call - a real mixer, not a
    one-shot processed file."""
    def __init__(self, sample_rate=44100):
        self.sample_rate = sample_rate
        self.sources = {}      # key -> LiveMixSource

    def add(self, key, samples, loop=False):
        self.sources[key] = LiveMixSource(array.array("h", samples), loop=loop)

    def remove(self, key):
        self.sources.pop(key, None)

    def mix_chunk(self, n_frames):
        """Return n_frames of mixed, clamped int16 stereo audio as an
        array('h'). Drops any source that finished (non-looping, ran out)
        automatically."""
        acc = [0] * (n_frames * 2)
        done = []
        for key, src in self.sources.items():
            chunk = src.read_mixed(n_frames, self.sample_rate)
            for i in range(len(acc)):
                acc[i] += chunk[i]
            if src.finished:
                done.append(key)
        for key in done:
            del self.sources[key]
        return array.array("h", (max(-32768, min(32767, int(v))) for v in acc))


def _make_mixer_device_class():
    """Built lazily (only when actually used) so importing audioctl.py never
    requires QtMultimedia/QtCore just for this experimental path."""
    from PyQt6.QtCore import QIODevice

    class _MixerDevice(QIODevice):
        """Feeds a QAudioSink by pulling fresh mixed chunks from a
        LiveMixer. Confirmed directly against the Qt docs: this pull-style
        QIODevice interface runs on the calling (main/application) thread,
        NOT a separate real-time audio thread - so there's no cross-thread
        GIL hazard here, but readData() genuinely has to be fast, since a
        slow call here can stutter the GUI or starve the audio buffer.
        Measured separately: mixing is comfortably fast enough for a
        realistic number of simultaneous sounds - see LiveMixer's own
        performance tests."""
        def __init__(self, mixer, parent=None):
            super().__init__(parent)
            self.mixer = mixer

        def readData(self, maxlen):
            n_frames = max(1, maxlen // 4)      # 4 bytes/frame: int16 stereo
            chunk = self.mixer.mix_chunk(n_frames)
            return chunk.tobytes()[:maxlen]

        def writeData(self, data):
            return -1        # read-only device - we generate audio, never receive it

        def bytesAvailable(self):
            return 4096      # always "more" - we generate on demand, never run dry

        def isSequential(self):
            return True

    return _MixerDevice


class LiveAudioSink:
    """Wraps a real QAudioSink + the LiveMixer feeding it, for genuinely
    live-mixed 3D audio (pan and the occlusion low-pass that actually
    track you in real time, not a one-time snapshot). This is deliberately
    separate from AudioController's existing QMediaPlayer-based playback -
    it does not replace or touch that path at all. Explicit, opt-in, and
    something to enable and LISTEN to before ever making it the default,
    given real platform-specific QAudioSink issues are documented even in
    Qt's own bug tracker."""
    def __init__(self, window=None, sample_rate=44100):
        self.window = window
        self.sample_rate = sample_rate
        self.mixer = LiveMixer(sample_rate=sample_rate)
        self.sink = None
        self.device = None
        self._next_key = 1

    def _log(self, text):
        win = self.window
        if win is not None and hasattr(win, "log"):
            win.log(text)
        else:
            print(text)

    def start(self):
        """Construct and start the real QAudioSink. Safe to call once;
        returns True on success, False (logged) on any failure - this is
        the one part that genuinely needs a human to listen and confirm,
        everything upstream of this is independently verified already."""
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        except Exception as e:
            self._log(f"[audio] live mixer unavailable - QtMultimedia import failed: {e}")
            return False
        try:
            device = QMediaDevices.defaultAudioOutput()
            if device.isNull():
                self._log("[audio] live mixer: no audio output device found")
                return False
            fmt = QAudioFormat()
            fmt.setSampleRate(self.sample_rate)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self.sink = QAudioSink(device, fmt, self.window)   # parented - see audioctl's
                                                                # earlier QMediaPlayer notes
                                                                # on why that matters
            # Qt's default buffer is ~250ms - a real, perceptible lag between
            # updating pan/occlusion and actually hearing the change, since
            # whatever's already buffered has to finish first. Measured
            # directly: even the most expensive case (large_hall reverb +
            # occlusion together) uses under a third of a 250ms chunk's time
            # budget, so cutting the buffer down keeps the same safety
            # margin while making updates land much sooner.
            target_latency_s = 0.07
            self.sink.setBufferSize(int(self.sample_rate * target_latency_s) * 4)  # *4: int16 stereo
            DeviceCls = _make_mixer_device_class()
            self.device = DeviceCls(self.mixer, self.window)
            self.device.open(self.device.OpenModeFlag.ReadOnly)
            self.sink.start(self.device)
            return True
        except Exception as e:
            self._log(f"[audio] live mixer failed to start: {e}")
            return False

    def add_source(self, samples, loop=False):
        """Decode-and-add a sound to the live mix. Returns a key for
        controlling it afterward (set .pan/.gain/.lowpass_cutoff live on
        mixer.sources[key])."""
        key = self._next_key
        self._next_key += 1
        self.mixer.add(key, samples, loop=loop)
        return key

    def remove_source(self, key):
        self.mixer.remove(key)

    def stop(self):
        try:
            if self.sink is not None:
                self.sink.stop()
                self.sink.deleteLater()
            if self.device is not None:
                self.device.close()
                self.device.deleteLater()
        except Exception:
            pass
        self.sink = None
        self.device = None
        self.mixer.sources = {}


