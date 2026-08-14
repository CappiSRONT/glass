"""
glassnet  -  share .glass files from PC to PC.

`name.glass/serv` starts a small HTTP server that serves your projects/ folder
to anyone on your network (like hosting a Minecraft server on a port). Another
Glass user reaches it by typing  host-ip:port/name.glass  in their address bar,
and Glass fetches and renders the returned file.

This is plain HTTP on your LAN. Don't expose it to the open internet.
"""

from __future__ import annotations
import functools
import http.server
import socket
import socketserver
import threading
import urllib.request

DEFAULT_PORT = 8765


def lan_ip():
    """Best-effort primary LAN IP so others can reach this host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


class GlassServer:
    def __init__(self, directory, port=DEFAULT_PORT):
        self.directory = directory
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        if self.httpd is not None:
            return self.address()
        handler = functools.partial(_QuietHandler, directory=self.directory)
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        # try a few ports if the chosen one is busy
        last_err = None
        for p in range(self.port, self.port + 10):
            try:
                self.httpd = socketserver.ThreadingTCPServer(("0.0.0.0", p), handler)
                self.port = p
                break
            except OSError as e:
                last_err = e
        if self.httpd is None:
            raise last_err
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.address()

    def address(self):
        return (lan_ip(), self.port)

    def is_running(self):
        return self.httpd is not None

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def fetch(host, port, filename, timeout=5):
    """Fetch a .glass file's text from a remote Glass host."""
    url = f"http://{host}:{port}/{filename.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Glass"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")
