#!/usr/bin/env python3
"""
cmux adapter — thin read/write chat over cmux's Unix socket.

Opinionated: cmux is the host. We pick a workspace/surface, send text into the
terminal, and parse scrollback into chat + command blocks for the phone UI.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _socket_candidates() -> list[str]:
    # Re-read env each call — empty strings are not valid paths
    # (Path("").exists() is True on macOS, which would falsely "find" cwd).
    out: list[str] = []
    for path in (
        os.environ.get("CMUX_SOCKET_PATH") or "",
        os.environ.get("CMUX_SOCKET") or "",
        str(Path.home() / ".local/state/cmux/cmux.sock"),
        "/tmp/cmux.sock",
        "/tmp/cmux-debug.sock",
    ):
        path = path.strip()
        if path and path not in out:
            out.append(path)
    return out


class CmuxError(RuntimeError):
    pass


def find_socket() -> str:
    for path in _socket_candidates():
        try:
            if Path(path).is_socket() or Path(path).exists():
                # Prefer real sockets; skip directories / empty-path weirdness.
                if Path(path).is_dir():
                    continue
                return path
        except OSError:
            continue
    raise CmuxError(
        "cmux is not running (no socket).\n"
        "Open cmux, then re-run. Socket expected at ~/.local/state/cmux/cmux.sock"
    )


def _recv_json_line(sock: socket.socket) -> dict:
    """Read newline-delimited JSON, skipping blank lines / partial frames."""
    buf = b""
    while True:
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            raw_line, buf = buf.split(b"\n", 1)
            line = raw_line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                # Incomplete JSON that happened to contain a newline — keep reading.
                buf = raw_line + b"\n" + buf
                break
        # Fast path: whole buffer is one JSON object (no trailing newline yet).
        stripped = buf.strip()
        if stripped.startswith(b"{") and stripped.endswith(b"}"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
    preview = buf[:120].decode("utf-8", "replace")
    if "Access denied" in preview:
        raise CmuxError(
            "cmux blocked this process (socket is cmux-only).\n"
            "In ~/.config/cmux/cmux.json set automation.socketControlMode to "
            '"automation", then Quit and reopen cmux.\n'
            "npx callwalkietalkie has to run outside a cmux terminal, so the default "
            "cmuxOnly mode rejects it."
        )
    raise CmuxError(f"empty/partial response from cmux ({preview!r})")


def _rpc_socket(method: str, params: Optional[dict], path: str, timeout: float) -> Any:
    payload = {
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params or {},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(raw)
        return _recv_json_line(sock)


def _rpc_cli(method: str, params: Optional[dict], path: str, timeout: float) -> Any:
    """Fallback via `cmux rpc` when the raw socket returns junk."""
    import subprocess

    cli = (
        os.environ.get("CMUX_BUNDLED_CLI_PATH")
        or "/Applications/cmux.app/Contents/Resources/bin/cmux"
    )
    if not Path(cli).exists():
        cli = "cmux"
    env = os.environ.copy()
    env["CMUX_SOCKET_PATH"] = path
    # Don't pass a blank CMUX_SOCKET through — it confuses some builds.
    if not (env.get("CMUX_SOCKET") or "").strip():
        env.pop("CMUX_SOCKET", None)
    proc = subprocess.run(
        [cli, "--socket", path, "rpc", method, json.dumps(params or {}), "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip() or f"cmux rpc exited {proc.returncode}"
        raise CmuxError(err)
    # CLI may print a deprecation line before JSON — take the last JSON object.
    data = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
    if data is None:
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise CmuxError(f"bad cmux CLI JSON: {e}") from e
    return data


def rpc(
    method: str,
    params: Optional[dict] = None,
    *,
    socket_path: Optional[str] = None,
    timeout: float = 8.0,
) -> Any:
    path = socket_path or find_socket()
    last_err: Optional[Exception] = None
    data: Any = None
    for attempt in range(3):
        try:
            data = _rpc_socket(method, params, path, timeout)
            break
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError, CmuxError) as e:
            last_err = e
            if attempt == 1:
                # Midway: try CLI once.
                try:
                    data = _rpc_cli(method, params, path, timeout)
                    break
                except Exception as cli_err:
                    last_err = cli_err
            continue
    if data is None:
        raise CmuxError(str(last_err) if last_err else "cmux rpc failed")

    if not isinstance(data, dict):
        return data

    # Error envelope (socket or CLI)
    if data.get("ok") is False or (
        data.get("error") and "result" not in data and "pong" not in data
    ):
        err = data.get("error") or {}
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or str(err)
        else:
            msg = str(err)
        raise CmuxError(msg or "cmux error")

    if "result" in data:
        return data.get("result")
    return data


@dataclass
class Target:
    workspace_id: str
    workspace_ref: str
    surface_id: str
    surface_ref: str
    window_id: Optional[str] = None
    window_ref: Optional[str] = None
    title: str = "cmux"
    cwd: Optional[str] = None
    surface_type: str = "terminal"
    url: Optional[str] = None

    @property
    def is_browser(self) -> bool:
        return (self.surface_type or "terminal") == "browser"


@dataclass
class ChatMessage:
    role: str  # user | assistant | system
    text: str
    kind: str = "message"  # message | command | system


@dataclass
class Session:
    """Stateful bridge onto one cmux surface."""

    target: Target
    socket_path: str
    _sent: list[str] = field(default_factory=list)
    _last_text: str = ""

    def refresh_target_meta(self) -> None:
        # Prefer surface-level title from inventory. Workspace.list title is shared
        # across tabs in the same workspace and would make every session look identical.
        try:
            for item in inventory(socket_path=self.socket_path):
                if self.target.surface_ref in (
                    item.get("surface"),
                    item.get("surface_id"),
                ) or self.target.surface_id in (
                    item.get("surface"),
                    item.get("surface_id"),
                ):
                    if item.get("title"):
                        self.target.title = item["title"]
                    if item.get("cwd"):
                        self.target.cwd = item["cwd"]
                    if item.get("window"):
                        self.target.window_ref = item.get("window")
                    if item.get("type"):
                        self.target.surface_type = item["type"]
                    if "url" in item:
                        self.target.url = item.get("url")
                    return
        except CmuxError:
            pass
        workspaces = list_workspaces(socket_path=self.socket_path)
        for ws in workspaces:
            if ws["id"] == self.target.workspace_id or ws.get("ref") == self.target.workspace_ref:
                if not self.target.title or self.target.title == "cmux":
                    self.target.title = (
                        ws.get("custom_title")
                        or ws.get("title")
                        or self.target.title
                    )
                self.target.cwd = ws.get("current_directory") or self.target.cwd
                break

    def read_text(self, *, lines: int = 800, scrollback: bool = True) -> str:
        if self.target.is_browser:
            return ""
        result = rpc(
            "surface.read_text",
            {
                "workspace_id": self.target.workspace_ref,
                "surface_id": self.target.surface_ref,
                "scrollback": scrollback,
                "lines": lines,
            },
            socket_path=self.socket_path,
        )
        text = result.get("text")
        if not text and result.get("base64"):
            text = base64.b64decode(result["base64"]).decode("utf-8", "replace")
        return text or ""

    def interrupt(self) -> None:
        """Stop a running agent / command in the selected surface.

        Agents disagree on the cancel key (Ctrl+C vs Escape), and some cmux
        builds only honor one of send_key / send_text — so we fire all of them.
        """
        if self.target.is_browser:
            raise CmuxError("Can't interrupt a browser tab")
        self.focus()
        base = {
            "workspace_id": self.target.workspace_ref,
            "surface_id": self.target.surface_ref,
        }
        attempts = [
            ("surface.send_key", {**base, "key": "c", "ctrl": True}),
            ("surface.send_text", {**base, "text": "\x03"}),
            ("surface.send_key", {**base, "key": "Escape"}),
        ]
        ok = False
        last_err: CmuxError | None = None
        for method, params in attempts:
            try:
                rpc(method, params, socket_path=self.socket_path)
                ok = True
            except CmuxError as e:
                last_err = e
        if not ok and last_err is not None:
            raise last_err

    def focus(self) -> None:
        """Bring this surface to front so send/interrupt land in the visible tab."""
        try:
            rpc(
                "surface.focus",
                {
                    "workspace_id": self.target.workspace_ref,
                    "surface_id": self.target.surface_ref,
                },
                socket_path=self.socket_path,
            )
        except CmuxError:
            pass

    def send(self, text: str) -> None:
        if self.target.is_browser:
            raise CmuxError("This is a browser live view — open a terminal to chat")
        text = text.rstrip("\n")
        if not text:
            raise CmuxError("empty message")
        self.focus()
        # Agents expect a submit. Always end with Enter.
        payload = text + "\n"
        rpc(
            "surface.send_text",
            {
                "workspace_id": self.target.workspace_ref,
                "surface_id": self.target.surface_ref,
                "text": payload,
            },
            socket_path=self.socket_path,
        )
        self._sent.append(text)

    def transcript(self) -> list[ChatMessage]:
        if self.target.is_browser:
            return []
        screen = self.read_text()
        self._last_text = screen
        parsed = parse_screen(screen)
        # Ensure phone-originated sends show up even if the agent UI ate the echo.
        return merge_sent(parsed, self._sent)

    def busy(self) -> bool:
        if self.target.is_browser:
            return False
        return is_busy(self._last_text or self.read_text())

    def notify_linked(self) -> None:
        try:
            rpc(
                "notification.create",
                {
                    "title": "Call Walkie Talkie",
                    "body": "Phone linked — chatting into this workspace",
                    "workspace_id": self.target.workspace_ref,
                    "surface_id": self.target.surface_ref,
                },
                socket_path=self.socket_path,
            )
        except CmuxError:
            pass
        try:
            # Best-effort sidebar presence. Method names vary across cmux builds.
            rpc(
                "notification.create",
                {
                    "title": "Call Walkie Talkie · live",
                    "body": self.target.title,
                },
                socket_path=self.socket_path,
            )
        except CmuxError:
            pass


def browser_screenshot(
    *,
    surface: Optional[str] = None,
    workspace: Optional[str] = None,
    socket_path: Optional[str] = None,
) -> bytes:
    """Capture a cmux browser surface as PNG bytes. Does not focus/steal foreground."""
    path = socket_path or find_socket()
    params: dict[str, Any] = {}
    if surface:
        params["surface_id"] = surface
    if workspace:
        params["workspace_id"] = workspace
    result = rpc("browser.screenshot", params, socket_path=path) or {}
    b64 = result.get("png_base64") or result.get("base64")
    if b64:
        return base64.b64decode(b64)
    file_path = result.get("path")
    if file_path and Path(file_path).is_file():
        return Path(file_path).read_bytes()
    raise CmuxError("browser screenshot returned no image")


# ---------------------------------------------------------------- discovery


def ping(socket_path: Optional[str] = None) -> dict:
    path = socket_path or find_socket()
    result = rpc("system.ping", socket_path=path)
    return {"ok": True, "socket_path": path, "pong": bool((result or {}).get("pong", True))}


def identify(socket_path: Optional[str] = None) -> dict:
    return rpc("system.identify", socket_path=socket_path or find_socket())


def list_workspaces(socket_path: Optional[str] = None) -> list[dict]:
    result = rpc("workspace.list", socket_path=socket_path or find_socket())
    return list((result or {}).get("workspaces") or [])


def list_surfaces(workspace_ref: str, socket_path: Optional[str] = None) -> list[dict]:
    result = rpc(
        "surface.list",
        {"workspace_id": workspace_ref},
        socket_path=socket_path or find_socket(),
    )
    return list((result or {}).get("surfaces") or [])


def inventory(socket_path: Optional[str] = None) -> list[dict]:
    """Flat list of pickable terminal + browser sessions across windows/workspaces."""
    path = socket_path or find_socket()
    items: list[dict] = []
    tree = None
    try:
        tree = rpc("system.tree", socket_path=path)
    except CmuxError:
        try:
            tree = rpc("tree", socket_path=path)
        except CmuxError:
            tree = None

    def _append(win_ref, ws_ref, ws_id, ws_title, ws_cwd, surf):
        kind = surf.get("type") or "terminal"
        if kind not in ("terminal", "browser"):
            return
        items.append(
            {
                "window": win_ref,
                "workspace": ws_ref or ws_id,
                "workspace_id": ws_id,
                "surface": surf.get("ref") or surf.get("id"),
                "surface_id": surf.get("id"),
                "title": surf.get("title") or ws_title or kind,
                "cwd": ws_cwd if kind == "terminal" else None,
                "url": surf.get("url") if kind == "browser" else None,
                "type": kind,
                "active": bool(surf.get("active") or surf.get("focused")),
            }
        )

    if isinstance(tree, dict) and tree.get("windows"):
        for win in tree.get("windows") or []:
            win_ref = win.get("ref") or win.get("window_ref")
            for ws in win.get("workspaces") or []:
                ws_ref = ws.get("ref") or (f"workspace:{ws['index']}" if "index" in ws else None)
                ws_id = ws.get("id")
                ws_title = ws.get("custom_title") or ws.get("title") or ws_ref
                ws_cwd = ws.get("current_directory")
                for pane in ws.get("panes") or []:
                    for surf in pane.get("surfaces") or []:
                        _append(win_ref, ws_ref, ws_id, ws_title, ws_cwd, surf)
        if items:
            return items

    # Fallback: workspace.list + surface.list
    for ws in list_workspaces(socket_path=path):
        ws_ref = ws.get("ref") or ws.get("id")
        for surf in list_surfaces(ws_ref, socket_path=path):
            _append(
                None,
                ws_ref,
                ws.get("id"),
                ws.get("custom_title") or ws.get("title") or ws_ref,
                ws.get("current_directory"),
                surf,
            )
    return items


def resolve_target(
    *,
    workspace: Optional[str] = None,
    surface: Optional[str] = None,
    window: Optional[str] = None,
    socket_path: Optional[str] = None,
) -> Target:
    path = socket_path or find_socket()
    inv = inventory(socket_path=path)
    ident = identify(socket_path=path)
    focused = (ident or {}).get("focused") or {}

    pick = None
    if surface or workspace:
        for item in inv:
            if surface and surface not in (
                item.get("surface"),
                item.get("surface_id"),
            ):
                continue
            if workspace and workspace not in (
                item.get("workspace"),
                item.get("workspace_id"),
            ):
                continue
            if window and window not in (item.get("window"),):
                continue
            pick = item
            break
    if pick is None:
        for item in inv:
            if item.get("active") and (item.get("type") or "terminal") == "terminal":
                pick = item
                break
    if pick is None:
        for item in inv:
            if item.get("active"):
                pick = item
                break
    if pick is None:
        for item in inv:
            if (item.get("type") or "terminal") == "terminal":
                pick = item
                break
    if pick is None and inv:
        pick = inv[0]
    if pick is None:
        # Last resort: focused refs from identify
        if not focused.get("surface_ref") and not focused.get("surface_id"):
            raise CmuxError("No cmux terminal open. Create one in cmux, then retry.")
        pick = {
            "workspace": focused.get("workspace_ref") or focused.get("workspace_id"),
            "workspace_id": focused.get("workspace_id"),
            "surface": focused.get("surface_ref") or focused.get("surface_id"),
            "surface_id": focused.get("surface_id"),
            "window": focused.get("window_ref"),
            "title": "cmux",
            "cwd": None,
            "type": focused.get("surface_type") or "terminal",
            "url": None,
        }

    # Enrich title/cwd from workspace.list when tree omitted them
    if (pick.get("type") or "terminal") == "terminal" and (
        not pick.get("cwd") or pick.get("title") in (None, "terminal", "cmux")
    ):
        for ws in list_workspaces(socket_path=path):
            if pick.get("workspace") in (ws.get("ref"), ws.get("id")):
                pick["cwd"] = pick.get("cwd") or ws.get("current_directory")
                if pick.get("title") in (None, "terminal", "cmux"):
                    pick["title"] = ws.get("custom_title") or ws.get("title") or pick.get("title")
                break

    return Target(
        workspace_id=pick.get("workspace_id") or pick.get("workspace"),
        workspace_ref=pick.get("workspace"),
        surface_id=pick.get("surface_id") or pick.get("surface"),
        surface_ref=pick.get("surface"),
        window_id=focused.get("window_id"),
        window_ref=pick.get("window") or focused.get("window_ref"),
        title=pick.get("title") or "cmux",
        cwd=pick.get("cwd"),
        surface_type=pick.get("type") or "terminal",
        url=pick.get("url"),
    )


def open_session(
    *,
    workspace: Optional[str] = None,
    surface: Optional[str] = None,
    window: Optional[str] = None,
) -> Session:
    path = find_socket()
    target = resolve_target(
        workspace=workspace, surface=surface, window=window, socket_path=path
    )
    return Session(target=target, socket_path=path)


# ---------------------------------------------------------------- screen → messages


_BOXY = re.compile(r"[╭╮╯╰│─┌┐└┘▄▀━┃╌╍╎╏╔╗╚╝║═]")
_CMD_START = re.compile(r"^\s*\$\s+(.*)$")
_SHELL_PROMPT = re.compile(
    r"^[^\n]*[@%][^\n]*[ %#❯] |^[^\n]*[ %#❯]\s*$"
)
_HIDDEN = re.compile(r"^\s*…\s+\d+.*(hidden|expand)", re.I)
_STATUSY = re.compile(
    r"(Running|Waiting|tokens|Goal active|Run Everything|ctrl\+[co]|Auto ·)",
    re.I,
)
_BRAILLE = re.compile(r"[⠁-⣿⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]")


def is_busy(screen: str) -> bool:
    """True when the agent UI looks mid-run (stoppable with Ctrl+C / Esc)."""
    lines = [ln.strip() for ln in (screen or "").splitlines() if ln.strip()]
    for s in lines[-40:]:
        low = s.lower()
        # Shell / CLI echo — not an agent run (e.g. "Already running.").
        if low.startswith("already running"):
            continue
        # Explicit stop affordance from agent UIs.
        if "ctrl+c" in low and (
            "stop" in low or s.lstrip().startswith("→") or "press" in low
        ):
            return True
        # Progress lines: "Running  2.83k tokens" / spinner + running|waiting|…
        if re.search(r"\brunning\b", low) and (
            "token" in low or _BRAILLE.search(s) or "…" in s or "..." in s
        ):
            return True
        if _BRAILLE.search(s) and re.search(
            r"\b(running|waiting|thinking|generating)\b", low
        ):
            return True
        if re.search(r"\bwaiting\b", low) and (
            "token" in low or "model" in low or "response" in low or "tool" in low
        ):
            return True
    return False


def _is_chrome(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    label = re.sub(r"^[→›>►▶•·▸▹➤➜\-\s]+", "", s).strip().lower()
    if label in {
        "add a follow-up",
        "add follow-up",
        "follow-up",
        "follow up",
    } or label.startswith("add a follow-up"):
        return True
    if _BOXY.search(s) and len(_BOXY.findall(s)) >= 3:
        return True
    if s.startswith(("→ ", "→")) and ("ctrl+" in s.lower() or "stop" in s.lower()):
        return True
    if s.startswith("Auto ·") or "Run Everything" in s:
        return True
    if _STATUSY.search(s) and len(s) < 140:
        # Status footers / progress — skip as chat content.
        if "tokens" in s.lower() or "goal active" in s.lower() or s.startswith(("⠠", "⠰", "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")):
            return True
        if re.match(r"^[⠁-⣿\s]+", s):
            return True
    if s in {"1 task", "Press Ctrl+C again to exit"}:
        return True
    if s.startswith("Tip:") and len(s) < 100:
        return True
    if re.match(r"^~?/.*·\s*(main|master|develop)", s):
        return True
    return False


def _is_shell_echo(line: str) -> bool:
    # Classic zsh/bash prompt lines the user typed locally — treat as command.
    if re.search(r"%\s+\S+", line) and ("@" in line or "Documents" in line or "Projects" in line or "~" in line):
        # `user@host dir % cmd`
        if re.search(r"[%$#]\s+\S+", line):
            return True
    return False


_USER_META = re.compile(
    r"^(Read\b|Grepped\b|Edited\b|Monitored\b|Ran\b|Waited\b|Finished\b|Found \d|SyntaxError|File \"|<stdin>)",
    re.I,
)
_TOOL_START = re.compile(
    r"^(Read\b|Read,|Grepped\b|Edited\b|Monitored\b|Ran\b|Waited\b|Finished\b)",
    re.I,
)


def _tool_line_text(line: str) -> str:
    if line.startswith("  ") and not line.startswith("    "):
        return _strip_assistant_line(line)
    return line.rstrip()


def _is_tool_start_line(line: str) -> bool:
    if not line.strip() or _CMD_START.match(line):
        return False
    return bool(_TOOL_START.match(_tool_line_text(line).strip()))


def _is_tool_block_continuation(line: str) -> bool:
    if not line.strip():
        return True
    if _HIDDEN.match(line):
        return True
    if line.startswith("    ") or line.startswith("\t"):
        return True
    stripped = line.strip()
    if stripped.startswith("▎"):
        return True
    if _is_tool_start_line(line):
        return True
    if re.match(r"^Found \d+\s+match", stripped, re.I):
        return True
    if line.startswith("  ") and not line.startswith("    "):
        inner = _strip_assistant_line(line)
        if _TOOL_START.match(inner):
            return True
        if inner.startswith("…"):
            return True
        if re.search(r"\.(html|py|js|tsx|ts|json|md|mjs|css|txt)\b", inner, re.I):
            return True
    return False


def _split_message_tools(text: str) -> list[ChatMessage]:
    """Pull Read/Grep/Edit/Monitored blocks out of assistant prose."""
    lines = text.splitlines()
    out: list[ChatMessage] = []
    abuf: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_tool_start_line(line):
            if abuf:
                body = "\n".join(abuf).strip()
                abuf = []
                if body:
                    out.append(ChatMessage(role="assistant", text=body, kind="message"))
            block = [_tool_line_text(line)]
            i += 1
            while i < len(lines) and _is_tool_block_continuation(lines[i]):
                if lines[i].strip():
                    block.append(lines[i].rstrip())
                i += 1
            body = "\n".join(block).strip()
            if body:
                out.append(ChatMessage(role="assistant", text=body, kind="command"))
            continue
        if line.strip():
            abuf.append(_strip_assistant_line(line))
        i += 1
    if abuf:
        body = "\n".join(abuf).strip()
        if body:
            out.append(ChatMessage(role="assistant", text=body, kind="message"))
    return out


def _split_tool_messages(msgs: list[ChatMessage]) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for m in msgs:
        if m.role != "assistant" or m.kind != "message":
            out.append(m)
            continue
        out.extend(_split_message_tools(m.text))
    return out


def _norm_chat_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _is_user_padding_only(line: str) -> bool:
    return bool(line.strip() == "" and len(line) >= 20)


def _is_user_input(line: str) -> bool:
    """cmux agent UI right-aligns user turns with heavy trailing padding."""
    if not line.strip() or _CMD_START.match(line) or _is_chrome(line):
        return False
    stripped = line.strip()
    if stripped.startswith("▎") or "ctrl+o" in stripped or "ctrl+r" in stripped:
        return False
    if _HIDDEN.match(line) or _USER_META.match(stripped):
        return False
    lead = len(line) - len(line.lstrip(" "))
    trail = len(line) - len(line.rstrip(" "))
    pad = lead + trail
    if lead > 4:
        return False
    if trail >= 10:
        return True
    # Wrapped user turns still sit in the ~54-col right-aligned slot.
    if len(line) >= 48 and pad >= 18 and len(stripped) <= 120:
        return True
    return False


def _is_user_continuation(line: str) -> bool:
    """Next line of a wrapped / pasted user turn (paths, lowercase, etc.)."""
    if not line.strip() or _CMD_START.match(line) or _is_chrome(line):
        return False
    stripped = line.strip()
    if stripped.startswith("▎") or _HIDDEN.match(line) or _USER_META.match(stripped):
        return False
    lead = len(line) - len(line.lstrip(" "))
    if lead > 4:
        return False
    if _is_user_input(line):
        return True
    if stripped.startswith(("/", ".", "~")) or "\\" in stripped or "/" in stripped:
        return True
    if any(tok in stripped for tok in (".png", ".jpg", "Screenshot", "TemporaryItems")):
        return True
    if stripped and stripped[0].islower():
        return True
    trail = len(line) - len(line.rstrip(" "))
    if trail >= 8:
        return True
    return False


def _strip_assistant_line(line: str) -> str:
    if line.startswith("  ") and not line.startswith("    "):
        return line[2:].rstrip()
    return line.rstrip()


def parse_screen(text: str) -> list[ChatMessage]:
    """Turn a cmux terminal dump into chat + command blocks."""
    out: list[ChatMessage] = []
    abuf: list[str] = []
    ubuf: list[str] = []

    def flush_assistant():
        nonlocal abuf
        body = "\n".join(abuf).strip()
        abuf = []
        if body:
            out.append(ChatMessage(role="assistant", text=body, kind="message"))

    def flush_user():
        nonlocal ubuf
        body = " ".join(p.strip() for p in ubuf if p.strip()).strip()
        ubuf = []
        if body:
            out.append(ChatMessage(role="user", text=body, kind="message"))

    def flush_all():
        flush_user()
        flush_assistant()

    def flush_command(lines: list[str]):
        body = "\n".join(lines).strip()
        if body:
            out.append(ChatMessage(role="assistant", text=body, kind="command"))

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_chrome(line):
            flush_all()
            i += 1
            continue

        m = _CMD_START.match(line)
        if m:
            flush_all()
            cmd = [m.group(1).rstrip()]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if _CMD_START.match(nxt) or _is_chrome(nxt):
                    break
                if _HIDDEN.match(nxt):
                    i += 1
                    continue
                # Indented continuation / tool output belongs to the command.
                if nxt.startswith("    ") or nxt.startswith("\t") or not nxt.strip():
                    if nxt.strip():
                        cmd.append(nxt.rstrip())
                    i += 1
                    continue
                # Bare output still part of command until a prose break.
                if nxt.startswith("  ") and not nxt.startswith("  $"):
                    cmd.append(nxt.rstrip())
                    i += 1
                    continue
                break
            flush_command(cmd)
            continue

        if _is_shell_echo(line):
            flush_all()
            # Take the part after the final prompt marker.
            parts = re.split(r"[%$#❯]\s+", line, maxsplit=1)
            cmd_text = parts[-1].strip() if len(parts) > 1 else line.strip()
            if cmd_text:
                flush_command([cmd_text])
            i += 1
            continue

        if _HIDDEN.match(line):
            i += 1
            continue

        if _is_user_padding_only(line):
            flush_user()
            i += 1
            continue

        if ubuf and _is_user_continuation(line):
            ubuf.append(line.strip())
            i += 1
            continue

        if _is_user_input(line):
            flush_assistant()
            ubuf.append(line.strip())
            i += 1
            continue

        if _is_tool_start_line(line):
            flush_user()
            flush_assistant()
            tool = [_tool_line_text(line)]
            i += 1
            while i < len(lines) and _is_tool_block_continuation(lines[i]):
                if lines[i].strip():
                    tool.append(lines[i].rstrip())
                i += 1
            flush_command(tool)
            continue

        flush_user()
        abuf.append(_strip_assistant_line(line))
        i += 1

    flush_all()
    return _split_tool_messages(_resplit_assistant_blobs(_coalesce(out)))


def _coalesce(msgs: list[ChatMessage]) -> list[ChatMessage]:
    if not msgs:
        return msgs
    merged: list[ChatMessage] = [msgs[0]]
    for m in msgs[1:]:
        prev = merged[-1]
        if prev.kind == m.kind == "message" and prev.role == m.role:
            prev.text = (prev.text + "\n" + m.text).strip()
        else:
            merged.append(m)
    # Drop tiny noise crumbs
    return [m for m in merged if len(m.text.strip()) >= 2]


def _resplit_assistant_blobs(msgs: list[ChatMessage]) -> list[ChatMessage]:
    """Recover user turns that were previously merged into assistant prose."""
    out: list[ChatMessage] = []
    for m in msgs:
        if m.role != "assistant" or m.kind != "message" or "\n" not in m.text:
            out.append(m)
            continue
        abuf: list[str] = []
        ubuf: list[str] = []

        def flush_a():
            nonlocal abuf
            body = "\n".join(abuf).strip()
            abuf = []
            if body:
                out.append(ChatMessage(role="assistant", text=body, kind="message"))

        def flush_u():
            nonlocal ubuf
            body = " ".join(p.strip() for p in ubuf if p.strip()).strip()
            ubuf = []
            if body:
                out.append(ChatMessage(role="user", text=body, kind="message"))

        for line in m.text.splitlines():
            if _is_user_padding_only(line):
                flush_u()
                continue
            if ubuf and _is_user_continuation(line):
                ubuf.append(line.strip())
                continue
            if _is_user_input(line):
                flush_a()
                ubuf.append(line.strip())
                continue
            flush_u()
            abuf.append(_strip_assistant_line(line))
        flush_u()
        flush_a()
    return out or msgs


def _dedupe_user_messages(msgs: list[ChatMessage]) -> list[ChatMessage]:
    drop: set[int] = set()
    user_idxs = [i for i, m in enumerate(msgs) if m.role == "user"]
    norms = [_norm_chat_text(msgs[i].text) for i in user_idxs]
    for ai, ni in enumerate(norms):
        for bi, nj in enumerate(norms):
            if ai == bi or not ni or not nj or ni == nj:
                continue
            if len(ni) < len(nj) and ni in nj:
                drop.add(user_idxs[ai])
            elif len(nj) < len(ni) and nj in ni:
                drop.add(user_idxs[bi])
    return [m for i, m in enumerate(msgs) if i not in drop]


def merge_sent(parsed: list[ChatMessage], sent: list[str]) -> list[ChatMessage]:
    """Append phone sends; drop fragment crumbs the screen parser split out."""
    if not sent:
        return _dedupe_user_messages(parsed)
    sent_norms = [_norm_chat_text(t) for t in sent if _norm_chat_text(t)]
    sent_set = set(sent_norms)

    def is_user_crumb(text: str) -> bool:
        n = _norm_chat_text(text)
        if not n or n in sent_set:
            return False
        return any(len(n) >= 3 and n in sn for sn in sent_norms)

    base = [m for m in parsed if not (m.role == "user" and is_user_crumb(m.text))]
    existing = {_norm_chat_text(m.text) for m in base if m.role == "user"}
    extras = [
        ChatMessage(role="user", text=t, kind="message")
        for t in sent
        if _norm_chat_text(t) not in existing
    ]
    return _dedupe_user_messages(base + extras)
