"""
core/dev_stream.py

Full-res ANPR camera preview over HTTP, gated behind DEV_MODE (core/config.py)
-- lets you watch what the camera/ANPR pipeline is actually seeing during a
field-test session from a browser, without a second process fighting
sensors.presence.PresenceSensor for the camera. Picamera2 only lets one
process hold the camera at a time (confirmed empirically: a second process
opening it gets "Failed to acquire camera: Device or resource busy"), so
this runs as a background daemon thread inside the same process that
already holds it, rather than as a standalone script.

Grabs frames off PresenceSensor.capture_frame() -- the same full-res,
rotated-upright stream anpr.yolov11.ANPRPipeline actually reads, not the
low-res motion stream -- at DEV_STREAM_FPS and serves them as an MJPEG
multipart stream. No extra dependency (no Flask etc): just the stdlib
http.server, which is more than enough for one or two people watching a
field test.

No authentication -- by request, for quick local viewing. If this is ever
tunneled somewhere public (e.g. ngrok), anyone with the URL can watch the
feed; add a check back in _make_handler if that matters again.

Usage (from core/main.py, only when dev mode is active):
    from core.dev_stream import start_mjpeg_server
    start_mjpeg_server(presence)   # presence: sensors.presence.PresenceSensor
"""

import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

from core.config import DEV_STREAM_FPS, DEV_STREAM_PORT

logger = logging.getLogger(__name__)

_BOUNDARY = "smarttollframe"

_INDEX_HTML = b"""<!doctype html>
<title>Smart Toll -- live camera preview (DEV_MODE)</title>
<body style="margin:0;background:#111">
<img src="/stream.mjpg" style="width:100%;height:auto;display:block">
</body>
"""


def _local_ip() -> str:
    """Best-effort LAN IP for the printed preview URL.

    Debian boxes often resolve socket.gethostname() to 127.0.1.1, which is
    useless in a URL meant for another device on the network. Connecting a
    UDP socket doesn't actually send a packet -- it just makes the kernel
    pick the outbound interface/address it would use -- so this works even
    fully offline. Falls back to "<pi-ip>" (a placeholder) if that lookup
    itself fails for some reason.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "<pi-ip>"
    finally:
        s.close()


def _make_handler(presence):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002 -- stdlib signature
            logger.debug("dev_stream: %s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(_INDEX_HTML)))
                self.end_headers()
                self.wfile.write(_INDEX_HTML)
                return

            if self.path == "/stream.mjpg":
                self._serve_stream()
                return

            self.send_response(404)
            self.end_headers()

        def _serve_stream(self) -> None:
            self.send_response(200)
            self.send_header(
                "Content-Type", f"multipart/x-mixed-replace; boundary={_BOUNDARY}"
            )
            self.send_header("Cache-Control", "no-cache, private")
            self.end_headers()

            period = 1.0 / DEV_STREAM_FPS
            logger.info("dev_stream: viewer connected from %s", self.address_string())
            try:
                while True:
                    started = time.perf_counter()
                    frame = presence.capture_frame()
                    ok, encoded = cv2.imencode(".jpg", frame)
                    if not ok:
                        logger.warning("dev_stream: JPEG encode failed, skipping frame")
                        continue
                    payload = encoded.tobytes()
                    self.wfile.write(f"--{_BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
                    self.wfile.write(payload)
                    self.wfile.write(b"\r\n")
                    time.sleep(max(0.0, period - (time.perf_counter() - started)))
            except (BrokenPipeError, ConnectionResetError):
                logger.info("dev_stream: viewer at %s disconnected", self.address_string())

    return Handler


def start_mjpeg_server(presence) -> ThreadingHTTPServer:
    """Start the preview server on a background daemon thread.

    Returns the server object (a daemon thread backs it, so it dies with
    the process -- no explicit shutdown() needed for this dev-only tool,
    same as core/main.py's Turso background sync thread).
    """
    server = ThreadingHTTPServer(("0.0.0.0", DEV_STREAM_PORT), _make_handler(presence))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(
        "Dev camera preview: http://%s:%d/ (full-res ANPR frame, %.1f fps)",
        _local_ip(), DEV_STREAM_PORT, DEV_STREAM_FPS,
    )
    return server
