"""
Run the real front-door firmware script (BBMSDoorEntry/rfid-lock.bash.sh) against a BBMS server.

How it works
------------
1. The firmware script is copied into a temporary sandbox and a small number of
   *named, exact* substitutions are applied (server URL, file paths, timings, and a
   "ready" marker). If any substitution no longer matches, the firmware has changed
   and we stop with a clear error rather than silently testing something else.
2. The script talks plain HTTP to a local proxy (`BbmsProxy`), which forwards each request
   over HTTPS to the BBMS server under test. The proxy records every request and response,
   and can pretend the server is down (connection refused) or broken (HTTP 503).
3. A fake Discord webhook (`DiscordCapture`) records the "... is in the space!" posts.
4. `DoorProcess` starts the script, feeds it fob reads (as the USB reader would) and
   reports what happened: did the door open, which requests were made, what was cached.

Only the Python standard library is used.
"""

import http.client
import http.server
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PRODUCTION_ACS_URL = "https://bbms.buildbrighton.com/acs/"
READY_MARKER = "@@READY"


class FirmwareChanged(Exception):
    """The firmware script no longer looks the way this harness expects."""


# --------------------------------------------------------------------------------------
# Tag ids
# --------------------------------------------------------------------------------------


def tag_for(card_number: int, prefix_hex: str) -> str:
    """Full 12-hex tag id, computed exactly like the firmware's append_checksum().

    10 hex digits of (vendor prefix + card number), then a 2-hex XOR of those 5 bytes.
    """
    value = int(prefix_hex, 16) + card_number
    hex10 = f"{value:010x}"
    checksum = 0
    for i in range(0, 10, 2):
        checksum ^= int(hex10[i : i + 2], 16)
    return f"{value:010X}{checksum:02X}"


def firmware_prefixes(script_text: str) -> list[str]:
    """The vendor prefixes the firmware tries, in order."""
    match = re.search(r'^PREFIXES="(.*?)"', script_text, re.S | re.M)
    if not match:
        raise FirmwareChanged("PREFIXES list not found in firmware script")
    return match.group(1).split()


# --------------------------------------------------------------------------------------
# Proxy between the door script and BBMS
# --------------------------------------------------------------------------------------


@dataclass
class Exchange:
    """One request the door made, and what it got back."""

    method: str
    path: str
    body: dict | None
    status: int | None  # None = connection refused / not answered
    response: str = ""

    @property
    def tag_id(self) -> str | None:
        return (self.body or {}).get("tagId")


class BbmsProxy:
    """Local HTTP endpoint for the door script; forwards to BBMS over HTTPS.

    mode:
      "online"  - forward requests to BBMS
      "error"   - answer every request with HTTP 503 (BBMS broken)
      "down"    - stop listening, so connections are refused (BBMS unreachable)
    """

    def __init__(self, target_host: str, target_port: int, host_header: str, verify_tls: bool):
        self.target_host = target_host
        self.target_port = target_port
        self.host_header = host_header
        self.tls_context = ssl.create_default_context()
        if not verify_tls:
            self.tls_context.check_hostname = False
            self.tls_context.verify_mode = ssl.CERT_NONE
        self.exchanges: list[Exchange] = []
        self.mode = "online"
        self.port = _free_port()
        self._server = None
        self._lock = threading.Lock()
        self._listen()

    # -- control -----------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        assert mode in ("online", "error", "down")
        if mode == "down" and self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        elif mode != "down" and not self._server:
            self._listen()
        self.mode = mode

    def close(self) -> None:
        self.set_mode("down")

    # -- internals ---------------------------------------------------------------------
    def _listen(self) -> None:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = {"_raw": raw.decode(errors="replace")}

                if proxy.mode == "error":
                    status, payload = 503, b""
                else:
                    status, payload = proxy._forward(self.command, self.path, raw, self.headers)

                with proxy._lock:
                    proxy.exchanges.append(
                        Exchange(self.command, self.path, body, status, payload.decode(errors="replace"))
                    )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        http.server.ThreadingHTTPServer.allow_reuse_address = True
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _forward(self, method, path, raw, headers):
        conn = http.client.HTTPSConnection(self.target_host, self.target_port, context=self.tls_context, timeout=30)
        forwarded = {k: v for k, v in headers.items() if k.lower() in ("content-type", "accept", "apikey")}
        forwarded["Host"] = self.host_header
        conn.request(method, path, body=raw or None, headers=forwarded)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, payload


# --------------------------------------------------------------------------------------
# Fake Discord webhook
# --------------------------------------------------------------------------------------


class DiscordCapture:
    def __init__(self):
        self.messages: list[str] = []
        capture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    capture.messages.append(json.loads(raw).get("content", ""))
                except ValueError:
                    capture.messages.append(raw.decode(errors="replace"))
                self.send_response(204)
                self.end_headers()

        self.port = _free_port()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/webhook"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


# --------------------------------------------------------------------------------------
# The door script itself
# --------------------------------------------------------------------------------------


@dataclass
class TapResult:
    card: str
    door_opened: bool
    exchanges: list[Exchange]
    discord: list[str]
    log: list[str] = field(default_factory=list)

    @property
    def statuses(self) -> list[int | None]:
        return [e.status for e in self.exchanges if e.path.startswith("/acs/activity")]


class DoorProcess:
    """A running copy of the door firmware in a sandbox."""

    def __init__(
        self,
        firmware_script: Path,
        proxy: BbmsProxy,
        discord: DiscordCapture,
        api_key: str,
        device_name: str,
        heartbeat_seconds: int = 900,
        preload_cache: dict[str, str] | None = None,
    ):
        self.proxy = proxy
        self.discord = discord
        self.sandbox = Path(tempfile.mkdtemp(prefix="door-compat-"))
        self.cache_file = self.sandbox / "bbms-key-cache"
        self.gpio_file = self.sandbox / "gpio"
        self.gpio_file.write_text("0\n")

        (self.sandbox / "keys").write_text(
            f'API_KEY="{api_key}"\nDEVICE_NAME="{device_name}"\nDISCORD_URL="{discord.url}"\n'
        )
        if preload_cache:
            self.cache_file.write_text("".join(f"export CACHE_{card}={tag}\n" for card, tag in preload_cache.items()))

        self._write_ncat_shim()
        self.script = self.sandbox / "rfid-lock.bash.sh"
        self.script.write_text(self._patch(firmware_script.read_text(), heartbeat_seconds))

        env = dict(os.environ, PATH=f"{self.sandbox / 'bin'}:{os.environ['PATH']}")
        self._process = subprocess.Popen(
            ["bash", str(self.script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=self.sandbox,
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self.log: list[str] = []
        threading.Thread(target=self._read_output, daemon=True).start()
        self.wait_until_ready(timeout=60)

    # -- driving the door --------------------------------------------------------------
    def tap(self, card: str, timeout: float = 120) -> TapResult:
        """Present a fob to the reader and wait until the script is ready for the next one."""
        first_exchange = len(self.proxy.exchanges)
        first_discord = len(self.discord.messages)
        first_log = len(self.log)
        self._process.stdin.write(card + "\n")
        self._process.stdin.flush()
        self.wait_until_ready(timeout)
        time.sleep(0.2)  # let the Discord post (sent after the door opens) arrive
        log = self.log[first_log:]
        return TapResult(
            card=card,
            door_opened=any(line.startswith("opening door to") for line in log),
            exchanges=self.proxy.exchanges[first_exchange:],
            discord=self.discord.messages[first_discord:],
            log=log,
        )

    def wait_until_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("door script did not become ready; log:\n" + "\n".join(self.log[-30:]))
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line == READY_MARKER:
                return
            if line == "<exited>":
                raise RuntimeError("door script exited; log:\n" + "\n".join(self.log[-30:]))

    def cached(self) -> dict[str, str]:
        """The door's offline cache: card number -> full tag id."""
        if not self.cache_file.exists():
            return {}
        cache = {}
        for line in self.cache_file.read_text().splitlines():
            match = re.match(r"export CACHE_(\w+)=(\w+)", line)
            if match:
                cache[match.group(1)] = match.group(2)
        return cache

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        for pipe in (self._process.stdin, self._process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        shutil.rmtree(self.sandbox, ignore_errors=True)

    # -- internals ---------------------------------------------------------------------
    def _read_output(self) -> None:
        for raw in self._process.stdout:
            line = raw.rstrip("\n")
            if line != READY_MARKER:
                self.log.append(line)
            self._lines.put(line)
        self._lines.put("<exited>")

    def _write_ncat_shim(self) -> None:
        """The firmware runs `ncat -z HOST PORT` to tell 'no answer' from 'unreachable'."""
        bin_dir = self.sandbox / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "ncat"
        shim.write_text('#!/bin/bash\n# usage: ncat -z HOST PORT\nexec 3<>"/dev/tcp/$2/$3" 2>/dev/null\n')
        shim.chmod(0o755)

    def _patch(self, text: str, heartbeat_seconds: int) -> str:
        proxy_base = f"http://127.0.0.1:{self.proxy.port}/acs/"
        substitutions = [
            ("server URL", PRODUCTION_ACS_URL, proxy_base),
            ("reachability check", "ncat -z bbms.buildbrighton.com 443", f"ncat -z 127.0.0.1 {self.proxy.port}"),
            ("keys file", "source /root/keys", f"source {self.sandbox / 'keys'}"),
            ("cache file", 'KEY_CACHE="/root/bbms-key-cache"', f'KEY_CACHE="{self.cache_file}"'),
            ("door relay GPIO", 'GPIO_DEV="/sys/class/leds/red:broadband/brightness"', f'GPIO_DEV="{self.gpio_file}"'),
            ("door hold time", "    sleep 5\n", "    sleep 0.1\n"),
            ("heartbeat interval", "HEARTBEAT_RATE=900", f"HEARTBEAT_RATE={heartbeat_seconds}"),
            (
                "ready marker",
                '    if read -p "Enter key code: "',
                f'    echo "{READY_MARKER}"\n    if read -p "Enter key code: "',
            ),
        ]
        for name, old, new in substitutions:
            count = text.count(old)
            if count != 1:
                raise FirmwareChanged(
                    f"expected exactly one '{name}' ({old!r}) in the firmware script, found {count}; "
                    "update tests/door_compat/door_harness.py to match the new firmware"
                )
            text = text.replace(old, new)
        return text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
