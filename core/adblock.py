"""
Glass blocking engine
======================
Two layers, both fully under your control:

1. Network blocking - every request is checked against blocklist.txt
   (hosts-format or one-domain-per-line). Blocked requests are dropped
   before they ever leave your machine, and logged so nothing is secret.

2. Cosmetic filtering - cosmetic.css is injected into every page to hide
   leftover ad/promo containers that aren't separate network requests.

No telemetry. No phone-home. Edit the lists; they reload on restart.
"""

from __future__ import annotations
import os

try:
    from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor
except Exception:  # allows importing/parsing without PyQt installed
    QWebEngineUrlRequestInterceptor = object

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_blocklist(path):
    domains = set()
    if not os.path.exists(path):
        return domains
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            # hosts format: "0.0.0.0 ads.example.com"
            parts = line.split()
            domain = parts[-1].lower()
            if domain in ("localhost", "0.0.0.0", "127.0.0.1"):
                continue
            domains.add(domain)
    return domains


class Interceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, window=None):
        super().__init__()
        self.window = window
        self.enabled = True
        self.blocked_count = 0
        self.domains = _load_blocklist(os.path.join(HERE, "blocklist.txt"))
        # path/URL fragments to drop without blocking the whole host
        # (YouTube ad pings live on youtube.com itself, so host-blocking
        # would break the site - these target just the ad endpoints).
        self.url_patterns = [
            "/pagead/", "/ptracking", "/api/stats/ads",
            "/youtubei/v1/player/ad_break", "/get_midroll_info",
            "/ads.js", "/adsbygoogle.js", "/advertisement.js",
            "/ad-banner", "/adframe", "googlesyndication", "doubleclick",
            "/gampad/", "/pagead2", "/adsense/",
        ]

    def toggle(self):
        self.enabled = not self.enabled
        return self.enabled

    def _is_blocked(self, host):
        """Exact match, or a subdomain of any listed domain. Walks up the
        host's own labels (ads.tracker.example.com -> tracker.example.com ->
        example.com -> com) doing an O(1) set lookup at each step, instead of
        the old O(N) scan comparing against every blocklist entry on every
        single request - fine at 234 entries, but a real hosts-format
        blocklist can run 50,000+ entries, and this runs on every image,
        script, and ping a page makes, not just page loads."""
        labels = host.lower().split(".")
        domains = self.domains
        for i in range(len(labels)):
            if ".".join(labels[i:]) in domains:
                return True
        return False

    def interceptRequest(self, info):
        # Privacy signals on every request, even when blocking is toggled off:
        # Do Not Track + Global Privacy Control tell sites not to track/sell.
        try:
            info.setHttpHeader(b"DNT", b"1")
            info.setHttpHeader(b"Sec-GPC", b"1")
        except Exception:
            pass
        if not self.enabled:
            return
        try:
            url = info.requestUrl()
            host = url.host()
            full = url.toString()
        except Exception:
            return
        if host and self._is_blocked(host):
            self.blocked_count += 1
            if self.window:
                self.window.log(f"[blocked] {full}")
            info.block(True)
            return
        for pat in self.url_patterns:
            if pat in full:
                self.blocked_count += 1
                if self.window:
                    self.window.log(f"[blocked-ad] {full}")
                info.block(True)
                return

    def cosmetic_css(self):
        path = os.path.join(HERE, "cosmetic.css")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        return ""

    def cosmetic_js(self):
        """Inject cosmetic.css into the current page via JS (also used by 'stripads')."""
        css = self.cosmetic_css().replace("\\", "\\\\").replace("`", "\\`")
        return (
            "(function(){var s=document.getElementById('glass-cosmetic');"
            "if(!s){s=document.createElement('style');s.id='glass-cosmetic';"
            "document.documentElement.appendChild(s);}"
            f"s.textContent=`{css}`;}})();"
        )

    def youtube_js(self):
        """Skip / fast-forward YouTube video ads and dismiss overlays.

        In-stream ads are served from the same host as the video, so they
        can't be network-blocked; this skips them in the player instead.
        Safe no-op on non-YouTube pages.
        """
        return r"""
(function(){
  try{
    if(!/(^|\.)youtube\.com$|youtube-nocookie\.com$/.test(location.hostname)) return;
  }catch(e){return;}
  if(window.__glassYT) return; window.__glassYT=true;
  function tick(){
    try{
      var p=document.querySelector('.html5-video-player');
      var v=document.querySelector('video');
      if(p&&v&&(p.classList.contains('ad-showing')||p.classList.contains('ad-interrupting'))){
        if(isFinite(v.duration)&&v.duration>0){ v.currentTime=v.duration; }
        v.muted=true; try{v.playbackRate=16;}catch(e){}
      }
      document.querySelectorAll(
        '.ytp-ad-skip-button,.ytp-ad-skip-button-modern,.ytp-skip-ad-button,'+
        '.ytp-ad-overlay-close-button,.ytp-ad-overlay-close-container button'
      ).forEach(function(b){ try{b.click();}catch(e){} });
    }catch(e){}
  }
  if(!window.__glassYTint){ window.__glassYTint=setInterval(tick,300); }
  tick();
})();
"""
