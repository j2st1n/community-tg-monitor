"""A fresh desktop Chromium, with a container-local X display and CDP port."""
import json
import os
import select
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack, contextmanager, suppress
from types import SimpleNamespace


def _stop_process_group(process):
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def _browser_env(display=None):
    # Subprocesses do not need the bot's API keys, tokens, or session Cookie.
    result = {key: os.environ[key] for key in
              ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "FONTCONFIG_PATH")
              if key in os.environ}
    if display is not None:
        result["DISPLAY"] = display
    return result


def _x_display(resources, timeout):
    read_fd, write_fd = os.pipe()
    try:
        try:
            process = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1600x1200x24",
                 "-nolisten", "tcp", "-noreset"],
                pass_fds=(write_fd,), env=_browser_env(), start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            resources.callback(_stop_process_group, process)
        finally:
            os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], timeout)
        if not ready:
            raise TimeoutError("display_start_timeout")
        number = os.read(read_fd, 32).decode("ascii").strip()
        if not number.isdigit() or process.poll() is not None:
            raise RuntimeError("display_start_failed")
        return ":" + number
    finally:
        os.close(read_fd)


def _available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_cdp(endpoint, process, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("desktop_browser_exited")
        try:
            with opener.open(endpoint + "/json/version", timeout=0.5) as response:
                body = json.load(response)
            if isinstance(body, dict) and body.get("webSocketDebuggerUrl"):
                return
        except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError):
            pass
        time.sleep(0.1)
    raise TimeoutError("desktop_browser_start_timeout")


class DesktopChromium:
    def __init__(self, chromium, resources):
        self.chromium = chromium
        self.resources = resources

    def launch(self, *, headless=False, timeout=20000):
        if headless:
            raise ValueError("desktop_mode_requires_display")
        seconds = timeout / 1000
        display = _x_display(self.resources, seconds)
        profile = self.resources.enter_context(tempfile.TemporaryDirectory(prefix="sbsb-desktop-"))
        port = _available_port()
        process = subprocess.Popen(
            [self.chromium.executable_path, "--no-sandbox", "--disable-dev-shm-usage",
             "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
             "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port),
             "--user-data-dir=" + profile, "--window-size=1280,800", "about:blank"],
            env=_browser_env(display), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.resources.callback(_stop_process_group, process)
        endpoint = "http://127.0.0.1:" + str(port)
        _wait_cdp(endpoint, process, seconds)
        return self.chromium.connect_over_cdp(endpoint, timeout=timeout, no_defaults=True)


@contextmanager
def desktop_playwright():
    from playwright.sync_api import sync_playwright
    with ExitStack() as resources:
        runtime = resources.enter_context(sync_playwright())
        yield SimpleNamespace(chromium=DesktopChromium(runtime.chromium, resources))
