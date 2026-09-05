#!/usr/bin/env python3
"""
callwalkietalkie — phone chat into a cmux workspace.

Requires cmux running on this Mac. Pair over LAN with a QR code, then every
message goes through cmux's socket (surface.send_text / surface.read_text).
"""

from __future__ import annotations

import atexit
import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import qrcode
from flask import Flask, Response, abort, jsonify, request
from qrcode.constants import ERROR_CORRECT_H

# cmux adapter lives in a sibling folder (kept isolated on purpose).
_ADAPTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cmux-adapter")
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)
from cmux_adapter import (  # noqa: E402
    CmuxError,
    browser_screenshot,
    inventory,
    open_session,
    ping as cmux_ping,
)

app = Flask(__name__)
TOKEN = os.environ.get("LONGLEASH_TOKEN", "agnostic-dispatch")
PORT = int(os.environ.get("LONGLEASH_PORT", 8787))
LANDING_URL = os.environ.get(
    "LONGLEASH_LANDING", "https://callwalkietalkie.com"
).rstrip("/")
# Public relay the Mac dials out to. Set LONGLEASH_RELAY=off to stay LAN-only.
DEFAULT_RELAY = "https://callwalkietalkie.com"
RELAY_URL = (
    os.environ.get("CALLWALKIETALKIE_RELAY")
    or os.environ.get("LONGLEASH_RELAY")
    or DEFAULT_RELAY
).rstrip("/")

CAFFEINATE_SECONDS = 60 * 60
_caffeinate_seconds = CAFFEINATE_SECONDS
_caffeinate_proc = None
_caffeinate_atexit = False

_agent_lock = threading.Lock()
_session = None  # cmux Session
_session_ws = None
_session_surface = None
_sent_by_surface: dict = {}
_notified = False

# Machine key from callwalkietalkie (LONGLEASH_KEY). Session is issued by the
# relay as a 6-digit HMAC handshake, or derived locally when relay is off.
MACHINE_KEY = (
    os.environ.get("CALLWALKIETALKIE_KEY") or os.environ.get("LONGLEASH_KEY") or ""
).strip()
SESSION_CODE = ""
KEY_FINGERPRINT = ""
_phone_arrived = False
_tunnel_lock = threading.Lock()
_tunnel_ws = None
_session_ready = threading.Event()


def _set_session(code: str, fingerprint: str = "") -> None:
    global SESSION_CODE, KEY_FINGERPRINT
    SESSION_CODE = str(code or "")
    if fingerprint:
        KEY_FINGERPRINT = fingerprint
    if SESSION_CODE:
        _session_ready.set()


def _lan_session_from_key(key: str) -> str:
    """Same shape as relay sessions: 6 digits from HMAC(key)."""
    digest = hmac.new(
        b"callwalkietalkie-lan-v1", key.encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def _bootstrap_session() -> None:
    """LAN-only or pre-relay: derive a local 6-digit code from the machine key."""
    if SESSION_CODE:
        return
    if MACHINE_KEY:
        fp = hashlib.sha256(MACHINE_KEY.encode("utf-8")).hexdigest()[:8]
        _set_session(_lan_session_from_key(MACHINE_KEY), fp)
    else:
        _set_session(f"{secrets.randbelow(1_000_000):06d}")


# ---------------------------------------------------------------- auth


@app.before_request
def check_token():
    if request.path.startswith("/static"):
        return
    # Pairing landing is unguessable by session code, not by token.
    if request.path.startswith("/p/"):
        return
    t = request.args.get("t") or request.headers.get("X-Longleash-Token")
    if t != TOKEN:
        abort(401)


# ---------------------------------------------------------------- cmux session


def _agent_error_message(exc):
    if isinstance(exc, SystemExit):
        msg = exc.code if isinstance(exc.code, str) else (exc.args[0] if exc.args else "cmux unavailable")
        return str(msg)
    return str(exc) or "cmux unavailable"


def _relay_enabled() -> bool:
    return bool(RELAY_URL) and RELAY_URL.lower() not in {"off", "0", "false", "none"}


def _public_scan_url() -> str:
    code = SESSION_CODE or "······"
    if _relay_enabled():
        return f"{RELAY_URL}/p/{code}"
    return f"http://{lan_ip()}:{PORT}/p/{code}"


def _public_pair_url() -> str:
    if _relay_enabled() and SESSION_CODE:
        return f"{RELAY_URL}/pair/{SESSION_CODE}"
    return f"http://127.0.0.1:{PORT}/setup?t={TOKEN}"


def _api_base() -> str:
    prefix = (request.headers.get("X-Longleash-Prefix") or "").rstrip("/")
    return f"{prefix}/agent" if prefix else "/agent"


def _tunnel_ws_url() -> str:
    if RELAY_URL.startswith("https://"):
        return "wss://" + RELAY_URL[len("https://") :] + "/host"
    if RELAY_URL.startswith("http://"):
        return "ws://" + RELAY_URL[len("http://") :] + "/host"
    return RELAY_URL + "/host"


def _handle_tunnel_req(msg: dict) -> None:
    global _tunnel_ws
    from werkzeug.test import Client

    client = Client(app)
    path = msg.get("path") or "/"
    query = msg.get("query") or ""
    raw = base64.b64decode(msg.get("body") or "")
    headers = {"X-Longleash-Prefix": msg.get("prefix") or ""}
    extra = msg.get("headers") or {}
    content_type = extra.get("content-type") or extra.get("Content-Type") or None
    try:
        resp = client.open(
            path,
            method=msg.get("method") or "GET",
            query_string=query,
            data=raw or None,
            headers=headers,
            content_type=content_type,
        )
        out_headers = {}
        for key, value in resp.headers.items():
            lk = key.lower()
            if lk in {"content-type", "location", "cache-control", "pragma"}:
                out_headers[lk] = value
        payload = {
            "type": "res",
            "id": msg.get("id"),
            "status": resp.status_code,
            "headers": out_headers,
            "body": base64.b64encode(resp.get_data() or b"").decode("ascii"),
        }
    except Exception as e:
        payload = {
            "type": "res",
            "id": msg.get("id"),
            "status": 500,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(
                json.dumps({"ok": False, "error": str(e)}).encode()
            ).decode("ascii"),
        }
    data = json.dumps(payload)
    with _tunnel_lock:
        ws = _tunnel_ws
        if ws is not None:
            try:
                ws.send(data)
            except Exception:
                pass


def _tunnel_loop() -> None:
    global _tunnel_ws
    try:
        import websocket
    except ImportError:
        print("  relay skipped · pip install websocket-client")
        return
    if not MACHINE_KEY:
        print("  relay skipped · missing key (run via npx callwalkietalkie)")
        return
    backoff = 1.0
    url = _tunnel_ws_url()
    while True:
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=30)
            with _tunnel_lock:
                _tunnel_ws = ws
            ws.send(json.dumps({"type": "hello", "key": MACHINE_KEY}))
            raw = ws.recv()
            if not raw:
                raise RuntimeError("empty hello reply")
            hello = json.loads(raw)
            if hello.get("type") != "ok" or not hello.get("session"):
                err = hello.get("error") or "bad hello"
                print(f"  relay rejected · {err}")
                raise RuntimeError(err)
            _set_session(str(hello["session"]), str(hello.get("fingerprint") or ""))
            backoff = 1.0
            print(f"  relay live · session {SESSION_CODE} · {_public_scan_url()}")
            while True:
                raw = ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("type") == "req":
                    threading.Thread(
                        target=_handle_tunnel_req, args=(msg,), daemon=True
                    ).start()
        except Exception:
            pass
        with _tunnel_lock:
            if _tunnel_ws is ws:
                _tunnel_ws = None
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 20.0)


def _get_session(workspace=None, surface=None, window=None, *, force=False):
    global _session, _session_ws, _session_surface, _notified
    want_ws = workspace if workspace is not None else _session_ws
    want_sf = surface if surface is not None else _session_surface
    # Persist phone-echoed sends per surface before swapping sessions.
    if _session is not None and _session_surface:
        _sent_by_surface[_session_surface] = list(_session._sent)
    fresh = (
        force
        or _session is None
        or (want_ws and want_ws != _session_ws)
        or (want_sf and want_sf != _session_surface)
    )
    if fresh:
        _session = open_session(workspace=want_ws, surface=want_sf, window=window)
        _session_ws = _session.target.workspace_ref
        _session_surface = _session.target.surface_ref
        _session._sent = list(_sent_by_surface.get(_session_surface, []))
        if not _notified:
            _session.notify_linked()
            _notified = True
    else:
        try:
            _session.refresh_target_meta()
        except CmuxError:
            pass
    return _session


# ---------------------------------------------------------------- setup UI

PAPER_CSS = """
  :root {
    --canvas:#ffffff; --soft:#f7f6f3; --ink:#37352f;
    --muted:#787774; --faint:#9b9a97;
    --line:rgba(55,53,47,0.09); --line-strong:rgba(55,53,47,0.16);
    --action:#2383e2; --cta:#0075de; --cta-hover:#005bab; --ok:#448361;
    --code-bg:#f9f8f7;
    --mono:"SFMono-Regular", Menlo, Consolas, "Liberation Mono", Courier, monospace;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  [hidden] { display:none !important; }
  html, body { height:100%; }
  body {
    min-height:100%; display:flex; flex-direction:column;
    font-family:Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background:var(--canvas); color:var(--ink);
    font-size:14px; line-height:1.55; -webkit-font-smoothing:antialiased;
  }
  .top {
    flex:none; display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:13px 28px; border-bottom:1px solid var(--line);
  }
  .brand {
    display:inline-flex; align-items:center; gap:7px;
    font-size:15px; font-weight:600; color:var(--ink);
  }
  .brand .lasso { flex:none; color:#000; }
  .meta { font-size:12px; color:var(--muted); }
  .stage {
    flex:1; display:flex; align-items:center; justify-content:center;
    width:100%; padding:24px 0;
  }
  main { width:100%; max-width:640px; margin:0 auto; padding:0 28px; }
  h1 { font-size:22px; font-weight:600; letter-spacing:-0.02em; line-height:1.2; }
  .steps { list-style:none; }
  .step { position:relative; display:flex; gap:14px; padding-bottom:30px; }
  .step:last-child { padding-bottom:0; }
  .rail { flex:none; width:18px; }
  .wire {
    position:absolute; top:23px; bottom:2px; left:8px; width:2px;
    border-radius:1px; background:var(--line); overflow:hidden;
  }
  .wire span {
    display:block; width:100%; height:0;
    background:var(--ink); transition:height 300ms ease;
  }
  .box {
    flex:none; width:18px; height:18px; margin-top:1px;
    border:1px solid var(--line-strong); border-radius:4px;
    display:flex; align-items:center; justify-content:center;
    transition:background 150ms ease, border-color 150ms ease;
  }
  .box svg { opacity:0; transition:opacity 150ms ease; }
  .step[data-state="done"] .box, .box.done { background:var(--ink); border-color:var(--ink); }
  .step[data-state="done"] .box svg, .box.done svg { opacity:1; }
  .step-body { flex:1; min-width:0; }
  .step-head { display:flex; align-items:baseline; justify-content:space-between; gap:16px; }
  .eyebrow {
    font-size:11px; font-weight:600; letter-spacing:0.06em;
    text-transform:uppercase; color:var(--faint);
  }
  .step[data-state="done"] .eyebrow { color:var(--ok); }
  .label { font-size:16px; font-weight:600; letter-spacing:-0.01em; }
  .status {
    flex:none; display:flex; align-items:center; gap:6px;
    font-size:12px; color:var(--faint); font-variant-numeric:tabular-nums;
  }
  .status .dot { width:6px; height:6px; border-radius:50%; background:currentColor; }
  .step[data-state="waiting"] .status .dot { animation:pulse 1.6s linear infinite; }
  .step[data-state="done"] .status { color:var(--ok); }
  .step[data-state="done"] .status .dot { animation:none; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.25; } }
  .hint { margin-top:6px; color:var(--muted); }
  .qr {
    display:flex; align-items:center; gap:16px; margin-top:12px; padding:16px;
    border:1px solid var(--line-strong); border-radius:8px;
  }
  .qr img { flex:none; display:block; }
  .qr-aside { min-width:0; }
  .qr-aside p { color:var(--muted); }
  .session { margin-bottom:12px; }
  .session strong {
    font-family:var(--mono); font-size:15px; font-weight:500;
    letter-spacing:0.04em; color:var(--ink);
  }
  .qr-url {
    display:inline-block; margin-top:6px; max-width:100%;
    overflow-wrap:anywhere; color:var(--action); text-decoration:none;
  }
  .qr-url:hover { color:var(--cta-hover); text-decoration:underline; }
  .countdown { color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }
  .centered { max-width:380px; margin:0 auto; padding:0 28px; text-align:center; }
  .centered .box { margin:0 auto 16px; width:28px; height:28px; }
  .centered .box svg { transform:scale(1.4); }
  .centered p { color:var(--muted); margin-top:8px; }
  @media (max-width:420px) {
    main, .centered { padding-left:18px; padding-right:18px; }
    .top { padding-left:18px; padding-right:18px; }
    .stage { align-items:flex-start; padding-top:28px; }
    .qr { flex-direction:column; align-items:flex-start; }
  }
"""

_CHECK_SVG = """<svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
  <path d="M1.5 5.8 4 8.3 9.5 2.8" stroke="#fff" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round"/></svg>"""

_LASSO_SVG = """<svg class="lasso" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <ellipse cx="10.5" cy="10" rx="7" ry="6.5" stroke="currentColor" stroke-width="1.85"/>
  <path d="M16.2 13.8c1.6 1.4 3.4 3.8 4.6 6.4" stroke="currentColor" stroke-width="1.85" stroke-linecap="round"/>
</svg>"""

_BRAND = f'<span class="brand">{_LASSO_SVG}WalkieTalkie</span>'

SETUP_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link your phone — WalkieTalkie</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head><body>
<div class="top">__BRAND__<span class="meta">Setup · cmux</span></div>
<div class="stage">
<main>
  <ol class="steps">
    <li class="step" id="step-computer" data-state="done">
      <div class="rail">
        <span class="box">__CHECK__</span>
        <span class="wire" aria-hidden="true"><span id="wire-fill" style="height:50%"></span></span>
      </div>
      <div class="step-body">
        <div class="step-head">
          <div>
            <p class="eyebrow">Step 1/2</p>
            <h2 class="label">Link your computer</h2>
          </div>
          <div class="status"><span class="dot"></span><span>__HOSTNAME__</span></div>
        </div>
      </div>
    </li>

    <li class="step" id="step-phone" data-state="waiting">
      <div class="rail"><span class="box">__CHECK__</span></div>
      <div class="step-body">
        <div class="step-head">
          <div>
            <p class="eyebrow">Step 2/2</p>
            <h2 class="label">Link your phone</h2>
          </div>
          <div class="status"><span class="dot"></span><span id="meta-phone">Waiting for a scan</span></div>
        </div>
        <div class="qr" id="qr-block">
          __QR__
          <div class="qr-aside">
            <p class="session">Session <strong>__CODE__</strong></p>
            <p>If it won't scan, open this on your phone instead:</p>
            <a class="qr-url" href="__SCAN_URL__">__SCAN_URL__</a>
          </div>
        </div>
        <p class="hint" id="done-detail" hidden></p>
      </div>
    </li>
  </ol>
</main>
</div>
<script>
(function() {
  var TOKEN = "__TOKEN__";
  var CHAT = "/agent?t=" + encodeURIComponent(TOKEN);
  var SECONDS = 3;
  var stepPhone = document.getElementById("step-phone");
  var metaPhone = document.getElementById("meta-phone");
  var wireFill = document.getElementById("wire-fill");
  var qrBlock = document.getElementById("qr-block");
  var doneDetail = document.getElementById("done-detail");
  var timer = null;

  function arrive() {
    clearInterval(timer);
    stepPhone.dataset.state = "done";
    metaPhone.textContent = "Connected";
    wireFill.style.height = "100%";
    qrBlock.hidden = true;
    doneDetail.hidden = false;

    var count = document.createElement("span");
    count.className = "countdown";
    count.textContent = SECONDS;
    var now = document.createElement("a");
    now.className = "qr-url";
    now.href = CHAT;
    now.textContent = "Open it now";
    doneDetail.textContent = "Opening the chat here too in ";
    doneDetail.append(count, document.createTextNode(" \\u00b7 "), now);

    var left = SECONDS;
    var ticker = setInterval(function() {
      left -= 1;
      if (left <= 0) {
        clearInterval(ticker);
        window.location.assign(CHAT);
        return;
      }
      count.textContent = left;
    }, 1000);
  }

  async function poll() {
    try {
      var r = await fetch("/setup/state?t=" + encodeURIComponent(TOKEN));
      var s = await r.json();
      if (s.paired) arrive();
    } catch (e) {}
  }

  poll();
  timer = setInterval(poll, 1200);
})();
</script>
</body></html>"""

HANDOFF_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Phone linked — WalkieTalkie</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head><body>
<div class="top">__BRAND__<span class="meta">Setup</span></div>
<div class="stage">
  <div class="centered">
    <span class="box done">__CHECK__</span>
    <h1>Phone linked</h1>
    <p>Opening cmux chat…</p>
  </div>
</div>
<script>setTimeout(function(){ location.replace("__CHAT__"); }, 600);</script>
</body></html>"""

STALE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session expired — WalkieTalkie</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head><body>
<div class="top">__BRAND__<span class="meta">Setup</span></div>
<div class="stage">
  <div class="centered">
    <h1>Session expired</h1>
    <p>Run <code>npx callwalkietalkie</code> on your Mac and scan again.</p>
  </div>
</div>
</body></html>"""


def _hostname():
    return socket.gethostname().replace(".local", "")


def _fill(template, **values):
    out = template
    for key, val in values.items():
        out = out.replace(f"__{key}__", str(val))
    return out


def _qr_svg_data_uri(target_url, module_px=6, fill="#12202a", radius=0):
    """Real, scannable QR for target_url as an inline SVG data URI."""
    qr = qrcode.QRCode(border=2, error_correction=ERROR_CORRECT_H)
    qr.add_data(target_url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    size = n * module_px
    rx = f' rx="{radius}"' if radius else ""
    rects = "".join(
        f'<rect x="{x * module_px}" y="{y * module_px}" '
        f'width="{module_px}" height="{module_px}"{rx}/>'
        for y, row in enumerate(matrix)
        for x, on in enumerate(row)
        if on
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<g fill="{fill}">{rects}</g>'
        f'</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.route("/")
def root():
    return Response(
        f'<meta http-equiv="refresh" content="0;url=/setup?t={TOKEN}">',
        mimetype="text/html",
    )


@app.route("/setup")
def setup_page():
    scan_url = _public_scan_url()
    qr = f'<img src="{_qr_svg_data_uri(scan_url)}" width="148" height="148" alt="QR code">'
    return _fill(
        SETUP_HTML,
        CSS=PAPER_CSS,
        BRAND=_BRAND,
        CHECK=_CHECK_SVG,
        HOSTNAME=_hostname(),
        TOKEN=TOKEN,
        CODE=SESSION_CODE,
        SCAN_URL=scan_url,
        QR=qr,
    )


@app.route("/setup/state")
def setup_state():
    ready = bool(SESSION_CODE) and (
        not _relay_enabled() or _session_ready.is_set()
    )
    return jsonify(
        {
            "paired": _phone_arrived,
            "code": SESSION_CODE or None,
            "ready": ready,
            "fingerprint": KEY_FINGERPRINT or None,
            "pair_url": _public_pair_url() if SESSION_CODE else None,
        }
    )


@app.route("/p/<code>")
def phone_landed(code):
    """The listener. Reaching this route *is* the proof the phone scanned it."""
    global _phone_arrived
    if code != SESSION_CODE:
        return _fill(STALE_HTML, CSS=PAPER_CSS, BRAND=_BRAND), 404
    _phone_arrived = True
    return _fill(
        HANDOFF_HTML,
        CSS=PAPER_CSS,
        BRAND=_BRAND,
        CHECK=_CHECK_SVG,
        CHAT=f"{_api_base()}?t={TOKEN}",
    )


# ---------------------------------------------------------------- agent (cmux chat)


@app.route("/agent")
def agent_page():
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_ui.html")
    html = open(ui_path, encoding="utf-8").read()
    html = html.replace("__TOKEN_JSON__", json.dumps(TOKEN))
    html = html.replace("__LANDING_JSON__", json.dumps(LANDING_URL))
    html = html.replace("__API_BASE_JSON__", json.dumps(_api_base()))
    return Response(
        html,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.route("/agent/status")
def agent_status():
    try:
        with _agent_lock:
            session = _get_session()
            session.refresh_target_meta()
            t = session.target
            sessions = []
            try:
                sessions = inventory(socket_path=session.socket_path)
            except CmuxError:
                pass
            payload = {
                "ok": True,
                "app": "cmux",
                "title": t.title,
                "cwd": t.cwd,
                "url": t.url,
                "type": t.surface_type,
                "workspace": t.workspace_ref,
                "surface": t.surface_ref,
                "window": t.window_ref,
                "socket": session.socket_path,
                "sessions": sessions,
                # Back-compat for older phone UI
                "workspaces": [
                    {
                        "id": s.get("workspace_id"),
                        "ref": s.get("workspace"),
                        "title": s.get("title"),
                        "custom_title": None,
                        "current_directory": s.get("cwd") or s.get("url"),
                        "selected": s.get("active"),
                        "surface": s.get("surface"),
                        "type": s.get("type"),
                        "url": s.get("url"),
                    }
                    for s in sessions
                ],
            }
        return jsonify(payload)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


@app.route("/agent/select", methods=["POST"])
def agent_select():
    body = request.get_json(force=True, silent=True) or {}
    workspace = (body.get("workspace") or "").strip() or None
    surface = (body.get("surface") or "").strip() or None
    window = (body.get("window") or "").strip() or None
    try:
        with _agent_lock:
            session = _get_session(
                workspace=workspace, surface=surface, window=window, force=True
            )
            session.focus()
            # Bring cmux to the foreground so the selected tab is actually visible.
            try:
                subprocess.run(
                    ["/usr/bin/open", "-a", "cmux"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
            t = session.target
        return jsonify(
            {
                "ok": True,
                "workspace": t.workspace_ref,
                "surface": t.surface_ref,
                "window": t.window_ref,
                "title": t.title,
                "cwd": t.cwd,
                "url": t.url,
                "type": t.surface_type,
            }
        )
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


@app.route("/agent/preview")
def agent_preview():
    """Live PNG frame of a cmux browser tab. Runs outside the agent lock."""
    surface = (request.args.get("surface") or "").strip() or None
    workspace = (request.args.get("workspace") or "").strip() or None
    try:
        with _agent_lock:
            session = _get_session()
            path = session.socket_path
            t = session.target
            if not surface:
                if not t.is_browser:
                    return jsonify(
                        {"ok": False, "error": "select a browser tab first"}
                    ), 400
                surface = t.surface_ref
                workspace = t.workspace_ref
            elif not workspace:
                workspace = t.workspace_ref
        # Screenshot outside the lock so chat/select stay responsive.
        png = browser_screenshot(
            surface=surface, workspace=workspace, socket_path=path
        )
        return Response(
            png,
            mimetype="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


@app.route("/agent/transcript")
def agent_transcript():
    try:
        with _agent_lock:
            session = _get_session()
            msgs = session.transcript()
            payload = {
                "ok": True,
                "busy": session.busy(),
                "messages": [
                    {"role": m.role, "text": m.text, "kind": m.kind} for m in msgs
                ],
            }
        return jsonify(payload)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


@app.route("/agent/send", methods=["POST"])
def agent_send():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty message"}), 400
    try:
        with _agent_lock:
            session = _get_session()
            session.send(text)
            if _session_surface:
                _sent_by_surface[_session_surface] = list(session._sent)
        return jsonify({"ok": True})
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


@app.route("/agent/interrupt", methods=["POST"])
def agent_interrupt():
    try:
        with _agent_lock:
            session = _get_session()
            session.interrupt()
        return jsonify({"ok": True})
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return jsonify({"ok": False, "error": _agent_error_message(e)}), 503


def _caffeinate_active():
    return _caffeinate_proc is not None and _caffeinate_proc.poll() is None


def _stop_caffeinate():
    global _caffeinate_proc
    proc = _caffeinate_proc
    _caffeinate_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_caffeinate(seconds=None):
    global _caffeinate_proc, _caffeinate_seconds, _caffeinate_atexit
    if seconds is not None:
        _caffeinate_seconds = int(seconds)
    if _caffeinate_seconds <= 0:
        _stop_caffeinate()
        return False
    _stop_caffeinate()
    try:
        _caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-dims", "-t", str(_caffeinate_seconds)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        _caffeinate_proc = None
        return False
    if not _caffeinate_atexit:
        atexit.register(_stop_caffeinate)
        _caffeinate_atexit = True
    return True


@app.route("/agent/caffeinate", methods=["GET", "POST"])
def agent_caffeinate():
    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        want = body.get("on")
        if want is None:
            want = not _caffeinate_active()
        if want:
            ok = _start_caffeinate()
            if not ok and _caffeinate_seconds > 0:
                return jsonify(ok=False, on=False, error="caffeinate unavailable"), 503
        else:
            _stop_caffeinate()
    return jsonify(ok=True, on=_caffeinate_active(), seconds=_caffeinate_seconds)


@app.route("/agent/shutdown", methods=["POST"])
def agent_shutdown():
    def _die():
        time.sleep(0.6)
        _stop_caffeinate()
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- boot


def _terminal_hyperlink(text, url):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _open_browser_later(url, delay=0.75):
    def _go():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="callwalkietalkie — phone ↔ cmux")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    port = args.port

    # Fail fast with a clear message if cmux isn't up.
    try:
        info = cmux_ping()
        print(f"  cmux ok · {info['socket_path']}")
    except CmuxError as e:
        print(f"\n  {e}\n", file=sys.stderr)
        sys.exit(1)

    setup_url = f"http://127.0.0.1:{port}/setup?t={TOKEN}"

    if _relay_enabled():
        if not MACHINE_KEY:
            print(
                "\n  Missing machine key. Run via: npx callwalkietalkie\n",
                file=sys.stderr,
            )
            sys.exit(1)
        threading.Thread(target=_tunnel_loop, daemon=True).start()
        if not _session_ready.wait(timeout=25):
            print(
                "\n  Relay did not issue a session. Check callwalkietalkie.com / network.\n",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        _bootstrap_session()

    pair_url = _public_pair_url()
    phone_hint = _public_scan_url()

    print(f"  Scan the QR here: {_terminal_hyperlink(pair_url, pair_url)}")
    print(f"  Phone path: {phone_hint}")
    print(f"  Session {SESSION_CODE}")
    if KEY_FINGERPRINT:
        print(f"  Key {KEY_FINGERPRINT}")

    def _on_signal(signum, frame):
        _stop_caffeinate()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    open_url = pair_url if _relay_enabled() else setup_url
    if not args.no_open:
        _open_browser_later(open_url)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
