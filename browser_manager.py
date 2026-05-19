from __future__ import annotations

import json
import os
import random
import re
import shutil
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
PROFILES_FILE = DATA_DIR / "profiles.json"
PROXY_POOL_FILE = DATA_DIR / "proxies.txt"
COOKIE_FILE_NAME = "cookies.json"


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

DEFAULT_TIMEZONES = [
    "Europe/Moscow",
    "Europe/Warsaw",
    "Europe/Berlin",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Dubai",
]

DEFAULT_WEBGL = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)"),
    ("Apple Inc.", "Apple GPU"),
]


def now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def profile_dir(profile_id: str) -> Path:
    return PROFILES_DIR / profile_id


def cookie_file(profile_id: str) -> Path:
    return profile_dir(profile_id) / COOKIE_FILE_NAME


def normalize_proxy(proxy_type: str, host: str, port: str) -> str:
    host = host.strip()
    port = port.strip()
    proxy_type = proxy_type.lower().strip()
    if not host or not port:
        return ""
    if "://" in host:
        return host
    return f"{proxy_type}://{host}:{port}"


def split_proxy(value: str) -> tuple[str, str, str, str, str]:
    value = value.strip()
    if not value:
        return "http", "", "", "", ""

    raw_value = value
    proxy_type = "http"
    login = ""
    password = ""

    if "://" in value:
        proxy_type, value = value.split("://", 1)
        proxy_type = proxy_type.lower()

    if "@" in value:
        auth, value = value.rsplit("@", 1)
        if ":" in auth:
            login, password = auth.split(":", 1)
        else:
            login = auth

    if "://" not in raw_value and "@" not in raw_value:
        parts = value.split(":")
        if len(parts) == 4:
            host, port, login, password = parts
            return proxy_type, host, port, login, password

    if ":" in value:
        host, port = value.rsplit(":", 1)
    else:
        host, port = value, ""

    return proxy_type, host, port, login, password


def proxy_line_to_profile(profile: "BrowserProfile", value: str) -> bool:
    proxy_type, host, port, login, password = split_proxy(value)
    if not host or not port:
        return False
    profile.proxy_type = "socks5" if proxy_type.lower() == "socks5" else "http"
    profile.proxy_host = host
    profile.proxy_port = port
    profile.proxy_login = login
    profile.proxy_password = password
    return True


def proxy_label(profile: "BrowserProfile") -> str:
    if not profile.proxy_host.strip() or not profile.proxy_port.strip():
        return "Без прокси"
    auth = ""
    if profile.proxy_login.strip():
        auth = f"{profile.proxy_login}:***@"
    return f"{profile.proxy_type}://{auth}{profile.proxy_host}:{profile.proxy_port}"


def int_or_default(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Fingerprint:
    user_agent: str = DEFAULT_USER_AGENTS[0]
    platform: str = "Win32"
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    screen_width: int = 1366
    screen_height: int = 768
    viewport_width: int = 1280
    viewport_height: int = 720
    hardware_concurrency: int = 8
    device_memory: int = 8
    max_touch_points: int = 0
    webgl_vendor: str = DEFAULT_WEBGL[0][0]
    webgl_renderer: str = DEFAULT_WEBGL[0][1]
    canvas_noise: bool = True
    webrtc_protection: bool = True


@dataclass
class BrowserProfile:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Новый профиль"
    proxy_type: str = "http"
    proxy_host: str = ""
    proxy_port: str = ""
    proxy_login: str = ""
    proxy_password: str = ""
    start_url: str = "https://browserleaks.com/ip"
    browser_path: str = ""
    local_port: str = ""
    notes: str = ""
    fingerprint: Fingerprint = field(default_factory=Fingerprint)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BrowserProfile":
        fp_raw = raw.get("fingerprint") or {}
        fingerprint = Fingerprint(**{k: v for k, v in fp_raw.items() if k in Fingerprint.__dataclass_fields__})
        values = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "fingerprint"}
        return cls(**values, fingerprint=fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def proxy_server(self) -> str:
        return normalize_proxy(self.proxy_type, self.proxy_host, self.proxy_port)


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.profiles: list[BrowserProfile] = []

    def load(self) -> list[BrowserProfile]:
        ensure_data_dirs()
        if not self.path.exists():
            self.profiles = [self.make_default_profile()]
            self.save()
            return self.profiles

        with self.path.open("r", encoding="utf-8") as fh:
            raw_profiles = json.load(fh)
        self.profiles = [BrowserProfile.from_dict(item) for item in raw_profiles]
        if not self.profiles:
            self.profiles = [self.make_default_profile()]
            self.save()
        for profile in self.profiles:
            profile_dir(profile.id).mkdir(parents=True, exist_ok=True)
        return self.profiles

    def save(self) -> None:
        ensure_data_dirs()
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump([profile.to_dict() for profile in self.profiles], fh, ensure_ascii=False, indent=2)
        for profile in self.profiles:
            profile_dir(profile.id).mkdir(parents=True, exist_ok=True)

    def make_default_profile(self) -> BrowserProfile:
        profile = BrowserProfile(name=f"Профиль {len(self.profiles) + 1}")
        profile_dir(profile.id).mkdir(parents=True, exist_ok=True)
        return profile

    def get(self, profile_id: str) -> BrowserProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def add(self, profile: BrowserProfile) -> None:
        self.profiles.append(profile)
        self.save()

    def remove(self, profile_id: str, remove_files: bool = False) -> None:
        self.profiles = [profile for profile in self.profiles if profile.id != profile_id]
        self.save()
        if remove_files:
            target = profile_dir(profile_id).resolve()
            root = PROFILES_DIR.resolve()
            if root in target.parents and target.exists():
                shutil.rmtree(target)


class LegacyBrowserRuntime:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self._playwright: Any = None
        self._contexts: dict[str, Any] = {}
        self._lock = threading.RLock()

    def is_running(self, profile_id: str) -> bool:
        with self._lock:
            return profile_id in self._contexts

    def start(self, profile: BrowserProfile) -> None:
        with self._lock:
            if profile.id in self._contexts:
                self.log(f"[{now_label()}] Уже запущен: {profile.name}")
                return

        thread = threading.Thread(target=self._start_worker, args=(profile,), daemon=True)
        thread.start()

    def stop(self, profile_id: str) -> None:
        thread = threading.Thread(target=self._stop_worker, args=(profile_id,), daemon=True)
        thread.start()

    def stop_all_sync(self) -> None:
        with self._lock:
            ids = list(self._contexts.keys())
        for profile_id in ids:
            self._stop_worker(profile_id)
        with self._lock:
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    def export_cookies(self, profile_id: str) -> Path:
        with self._lock:
            context = self._contexts.get(profile_id)
        target = cookie_file(profile_id)
        if not context:
            return target
        state = context.storage_state()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            json.dump(state.get("cookies", []), fh, ensure_ascii=False, indent=2)
        return target

    def _start_worker(self, profile: BrowserProfile) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.log(f"[{now_label()}] Playwright не установлен: {exc}")
            self.log(f"[{now_label()}] Установите: python -m pip install -r requirements.txt")
            self.log(f"[{now_label()}] Затем: python -m playwright install chromium")
            return

        try:
            with self._lock:
                if self._playwright is None:
                    self._playwright = sync_playwright().start()

            data_dir = profile_dir(profile.id)
            data_dir.mkdir(parents=True, exist_ok=True)
            fp = profile.fingerprint
            proxy = self._make_proxy(profile)
            args = self._launch_args(profile)
            launch_options: dict[str, Any] = {
                "headless": False,
                "args": args,
                "viewport": {
                    "width": int(fp.viewport_width),
                    "height": int(fp.viewport_height),
                },
                "screen": {
                    "width": int(fp.screen_width),
                    "height": int(fp.screen_height),
                },
                "user_agent": fp.user_agent,
                "locale": fp.locale,
                "timezone_id": fp.timezone,
                "ignore_https_errors": True,
                "accept_downloads": True,
            }
            if proxy:
                launch_options["proxy"] = proxy

            if profile.browser_path.strip():
                launch_options["executable_path"] = profile.browser_path.strip()
            else:
                detected = find_local_browser()
                if detected:
                    launch_options["executable_path"] = detected

            self.log(f"[{now_label()}] Запуск: {profile.name}")
            context = self._playwright.chromium.launch_persistent_context(
                str(data_dir),
                **launch_options,
            )
            context.add_init_script(build_fingerprint_script(fp))
            self._load_cookies(context, profile.id)

            with self._lock:
                self._contexts[profile.id] = context

            existing_pages = list(context.pages)
            page = existing_pages[0] if existing_pages else context.new_page()
            if profile.start_url.strip():
                page.goto(profile.start_url.strip(), wait_until="domcontentloaded", timeout=45_000)

            context.on("close", lambda: self._forget_context(profile.id))
            self.log(f"[{now_label()}] Браузер запущен: {profile.name}")
        except Exception:
            self.log(f"[{now_label()}] Ошибка запуска {profile.name}")
            self.log(traceback.format_exc().strip())
            with self._lock:
                self._contexts.pop(profile.id, None)

    def _stop_worker(self, profile_id: str) -> None:
        with self._lock:
            context = self._contexts.pop(profile_id, None)
        if not context:
            self.log(f"[{now_label()}] Профиль уже остановлен")
            return
        try:
            state = context.storage_state()
            target = cookie_file(profile_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as fh:
                json.dump(state.get("cookies", []), fh, ensure_ascii=False, indent=2)
            context.close()
            self.log(f"[{now_label()}] Cookies сохранены: {target}")
            self.log(f"[{now_label()}] Браузер остановлен")
        except Exception:
            self.log(f"[{now_label()}] Ошибка остановки профиля")
            self.log(traceback.format_exc().strip())

    def _forget_context(self, profile_id: str) -> None:
        with self._lock:
            self._contexts.pop(profile_id, None)

    def _load_cookies(self, context: Any, profile_id: str) -> None:
        path = cookie_file(profile_id)
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                cookies = json.load(fh)
            if isinstance(cookies, dict):
                cookies = cookies.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
                self.log(f"[{now_label()}] Cookies загружены: {len(cookies)}")
        except Exception as exc:
            self.log(f"[{now_label()}] Cookies не загружены: {exc}")

    def _make_proxy(self, profile: BrowserProfile) -> dict[str, str] | None:
        server = profile.proxy_server
        if not server:
            return None
        proxy: dict[str, str] = {"server": server}
        if profile.proxy_login.strip():
            proxy["username"] = profile.proxy_login.strip()
            proxy["password"] = profile.proxy_password
        return proxy

    def _launch_args(self, profile: BrowserProfile) -> list[str]:
        fp = profile.fingerprint
        args = [
            f"--window-size={fp.viewport_width},{fp.viewport_height}",
            f"--lang={fp.locale}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if fp.webrtc_protection:
            args.extend(
                [
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--force-webrtc-ip-handling-policy",
                ]
            )
        return args


class BrowserRuntime:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self._tasks: Queue[tuple[str, Any, Queue[Any] | None]] = Queue()
        self._contexts: dict[str, Any] = {}
        self._states: dict[str, str] = {}
        self._state_lock = threading.RLock()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def is_running(self, profile_id: str) -> bool:
        with self._state_lock:
            return self._states.get(profile_id) in {"starting", "running", "stopping"}

    def start(self, profile: BrowserProfile) -> None:
        with self._state_lock:
            if self._states.get(profile.id) in {"starting", "running", "stopping"}:
                self.log(f"[{now_label()}] Уже запущен: {profile.name}")
                return
            self._states[profile.id] = "starting"

        self._tasks.put(("start", BrowserProfile.from_dict(profile.to_dict()), None))

    def stop(self, profile_id: str) -> None:
        with self._state_lock:
            if profile_id not in self._states:
                self.log(f"[{now_label()}] Профиль уже остановлен")
                return
            self._states[profile_id] = "stopping"
        self._tasks.put(("stop", profile_id, None))

    def stop_all_sync(self) -> None:
        response: Queue[Any] = Queue(maxsize=1)
        self._tasks.put(("shutdown", None, response))
        try:
            response.get(timeout=30)
        except Empty:
            pass

    def export_cookies(self, profile_id: str) -> Path:
        target = cookie_file(profile_id)
        if not self.is_running(profile_id):
            return target

        response: Queue[Any] = Queue(maxsize=1)
        self._tasks.put(("export", profile_id, response))
        try:
            result = response.get(timeout=20)
            return result if isinstance(result, Path) else target
        except Empty:
            self.log(f"[{now_label()}] Экспорт cookies занял слишком много времени")
            return target

    def _worker_loop(self) -> None:
        playwright: Any = None
        while True:
            action, payload, response = self._tasks.get()
            if action == "shutdown":
                self._shutdown_worker(playwright)
                if response:
                    response.put(True)
                return

            if playwright is None:
                try:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                except Exception as exc:
                    self.log(f"[{now_label()}] Playwright не установлен: {exc}")
                    self.log(f"[{now_label()}] Установите: python -m pip install -r requirements.txt")
                    self.log(f"[{now_label()}] Затем: python -m playwright install chromium")
                    if action == "start" and isinstance(payload, BrowserProfile):
                        self._clear_state(payload.id)
                    if response:
                        response.put(cookie_file(str(payload)))
                    continue

            try:
                if action == "start" and isinstance(payload, BrowserProfile):
                    self._run_start(playwright, payload)
                elif action == "stop" and isinstance(payload, str):
                    self._run_stop(payload)
                elif action == "export" and isinstance(payload, str):
                    path = self._run_export(payload)
                    if response:
                        response.put(path)
            except Exception:
                self.log(traceback.format_exc().strip())
                if action == "start" and isinstance(payload, BrowserProfile):
                    self._clear_state(payload.id)
                if action == "stop" and isinstance(payload, str):
                    self._clear_state(payload)
                if response:
                    response.put(cookie_file(str(payload)))

    def _run_start(self, playwright: Any, profile: BrowserProfile) -> None:
        try:
            if profile.id in self._contexts:
                self._set_state(profile.id, "running")
                self.log(f"[{now_label()}] Уже запущен: {profile.name}")
                return

            data_dir = profile_dir(profile.id)
            data_dir.mkdir(parents=True, exist_ok=True)
            fp = profile.fingerprint
            proxy = self._make_proxy(profile)
            args = self._launch_args(profile)
            launch_options: dict[str, Any] = {
                "headless": False,
                "args": args,
                "viewport": {
                    "width": int(fp.viewport_width),
                    "height": int(fp.viewport_height),
                },
                "screen": {
                    "width": int(fp.screen_width),
                    "height": int(fp.screen_height),
                },
                "user_agent": fp.user_agent,
                "locale": fp.locale,
                "timezone_id": fp.timezone,
                "ignore_https_errors": True,
                "accept_downloads": True,
            }
            if proxy:
                launch_options["proxy"] = proxy

            if profile.browser_path.strip():
                launch_options["executable_path"] = profile.browser_path.strip()
            else:
                detected = find_local_browser()
                if detected:
                    launch_options["executable_path"] = detected

            self.log(f"[{now_label()}] Запуск: {profile.name}")
            context = playwright.chromium.launch_persistent_context(
                str(data_dir),
                **launch_options,
            )
            context.add_init_script(build_fingerprint_script(fp))
            self._load_cookies(context, profile.id)
            self._contexts[profile.id] = context
            self._set_state(profile.id, "running")

            existing_pages = list(context.pages)
            page = existing_pages[0] if existing_pages else context.new_page()
            if profile.start_url.strip():
                page.goto(profile.start_url.strip(), wait_until="domcontentloaded", timeout=45_000)

            context.on("close", lambda: self._forget_context(profile.id))
            self.log(f"[{now_label()}] Браузер запущен: {profile.name}")
        except Exception:
            self.log(f"[{now_label()}] Ошибка запуска {profile.name}")
            self.log(traceback.format_exc().strip())
            self._contexts.pop(profile.id, None)
            self._clear_state(profile.id)

    def _run_stop(self, profile_id: str) -> None:
        context = self._contexts.pop(profile_id, None)
        if not context:
            self._clear_state(profile_id)
            self.log(f"[{now_label()}] Профиль уже остановлен")
            return
        try:
            target = self._run_export(profile_id, context=context)
            context.close()
            self.log(f"[{now_label()}] Cookies сохранены: {target}")
            self.log(f"[{now_label()}] Браузер остановлен")
        except Exception:
            self.log(f"[{now_label()}] Ошибка остановки профиля")
            self.log(traceback.format_exc().strip())
        finally:
            self._clear_state(profile_id)

    def _run_export(self, profile_id: str, context: Any | None = None) -> Path:
        context = context or self._contexts.get(profile_id)
        target = cookie_file(profile_id)
        if not context:
            return target
        state = context.storage_state()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            json.dump(state.get("cookies", []), fh, ensure_ascii=False, indent=2)
        return target

    def _shutdown_worker(self, playwright: Any) -> None:
        for profile_id in list(self._contexts.keys()):
            self._run_stop(profile_id)
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass
        with self._state_lock:
            self._states.clear()

    def _forget_context(self, profile_id: str) -> None:
        self._contexts.pop(profile_id, None)
        self._clear_state(profile_id)

    def _set_state(self, profile_id: str, state: str) -> None:
        with self._state_lock:
            self._states[profile_id] = state

    def _clear_state(self, profile_id: str) -> None:
        with self._state_lock:
            self._states.pop(profile_id, None)

    def _load_cookies(self, context: Any, profile_id: str) -> None:
        path = cookie_file(profile_id)
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                cookies = json.load(fh)
            if isinstance(cookies, dict):
                cookies = cookies.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
                self.log(f"[{now_label()}] Cookies загружены: {len(cookies)}")
        except Exception as exc:
            self.log(f"[{now_label()}] Cookies не загружены: {exc}")

    def _make_proxy(self, profile: BrowserProfile) -> dict[str, str] | None:
        server = profile.proxy_server
        if not server:
            return None
        proxy: dict[str, str] = {"server": server}
        if profile.proxy_login.strip():
            proxy["username"] = profile.proxy_login.strip()
            proxy["password"] = profile.proxy_password
        return proxy

    def _launch_args(self, profile: BrowserProfile) -> list[str]:
        fp = profile.fingerprint
        args = [
            f"--window-size={fp.viewport_width},{fp.viewport_height}",
            f"--lang={fp.locale}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        local_port_value = profile.local_port.strip()
        local_port = int_or_default(local_port_value, 0)
        if 1 <= local_port <= 65535:
            args.append(f"--remote-debugging-port={local_port}")
        elif local_port_value:
            self.log(f"[{now_label()}] Локальный порт пропущен: нужен диапазон 1-65535")
        if fp.webrtc_protection:
            args.extend(
                [
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--force-webrtc-ip-handling-policy",
                ]
            )
        return args


def find_local_browser() -> str:
    env_path = os.environ.get("BROWSER_MANAGER_CHROME_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for candidate in candidates:
        if str(candidate) and candidate.exists():
            return str(candidate)
    return ""


def build_fingerprint_script(fp: Fingerprint) -> str:
    script_data = {
        "platform": fp.platform,
        "hardwareConcurrency": fp.hardware_concurrency,
        "deviceMemory": fp.device_memory,
        "maxTouchPoints": fp.max_touch_points,
        "webglVendor": fp.webgl_vendor,
        "webglRenderer": fp.webgl_renderer,
        "canvasNoise": fp.canvas_noise,
        "screenWidth": fp.screen_width,
        "screenHeight": fp.screen_height,
        "locale": fp.locale,
    }
    encoded = json.dumps(script_data, ensure_ascii=False)
    return f"""
(() => {{
  const fp = {encoded};
  const overrideGetter = (target, prop, value) => {{
    try {{
      Object.defineProperty(target, prop, {{
        get: () => value,
        configurable: true
      }});
    }} catch (e) {{}}
  }};

  overrideGetter(Navigator.prototype, "webdriver", undefined);
  overrideGetter(Navigator.prototype, "platform", fp.platform);
  overrideGetter(Navigator.prototype, "hardwareConcurrency", fp.hardwareConcurrency);
  overrideGetter(Navigator.prototype, "deviceMemory", fp.deviceMemory);
  overrideGetter(Navigator.prototype, "maxTouchPoints", fp.maxTouchPoints);
  overrideGetter(Navigator.prototype, "language", fp.locale);
  overrideGetter(Navigator.prototype, "languages", [fp.locale, fp.locale.split("-")[0], "en-US", "en"]);

  overrideGetter(Screen.prototype, "width", fp.screenWidth);
  overrideGetter(Screen.prototype, "height", fp.screenHeight);
  overrideGetter(Screen.prototype, "availWidth", fp.screenWidth);
  overrideGetter(Screen.prototype, "availHeight", Math.max(0, fp.screenHeight - 40));

  if (window.WebGLRenderingContext) {{
    const parameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameterId) {{
      if (parameterId === 37445) return fp.webglVendor;
      if (parameterId === 37446) return fp.webglRenderer;
      return parameter.call(this, parameterId);
    }};
  }}

  if (window.WebGL2RenderingContext) {{
    const parameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(parameterId) {{
      if (parameterId === 37445) return fp.webglVendor;
      if (parameterId === 37446) return fp.webglRenderer;
      return parameter2.call(this, parameterId);
    }};
  }}

  if (fp.canvasNoise) {{
    const toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {{
      const context = this.getContext("2d");
      if (context && this.width && this.height) {{
        const shift = ((fp.hardwareConcurrency + fp.deviceMemory) % 7) + 1;
        context.globalAlpha = 0.01;
        context.fillStyle = `rgb(${{shift}},${{shift * 2}},${{shift * 3}})`;
        context.fillRect(0, 0, 1, 1);
        context.globalAlpha = 1;
      }}
      return toDataURL.apply(this, args);
    }};

    const getImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
      const imageData = getImageData.apply(this, args);
      if (imageData && imageData.data && imageData.data.length > 4) {{
        imageData.data[0] = (imageData.data[0] + 1) % 255;
      }}
      return imageData;
    }};
  }}
}})();
"""


class DarkApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BrowserManager - профили браузеров")
        self.geometry("1040x760")
        self.minsize(980, 680)
        self.configure(bg="#262626")

        self.store = ProfileStore(PROFILES_FILE)
        self.profiles = self.store.load()
        self.selected_profile_id: str | None = self.profiles[0].id if self.profiles else None
        self.log_queue: Queue[str] = Queue()
        self.runtime = BrowserRuntime(self.enqueue_log)
        self.vars: dict[str, tk.Variable] = {}
        self.profile_list: tk.Listbox | None = None
        self.log_text: tk.Text | None = None
        self.status_var = tk.StringVar(value="Готов")
        self.cookie_status_var = tk.StringVar(value="")
        self.running_status_var = tk.StringVar(value="")
        self.progress_var = tk.IntVar(value=0)

        self._setup_style()
        self._build_ui()
        self._load_profile_to_form()
        self.after(150, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#262626")
        style.configure("Dark.TLabelframe", background="#262626", foreground="#ffffff", bordercolor="#777777")
        style.configure("Dark.TLabelframe.Label", background="#262626", foreground="#ffffff")
        style.configure("TLabel", background="#262626", foreground="#ffffff")
        style.configure("Dim.TLabel", background="#262626", foreground="#c9c9c9")
        style.configure("Ok.TLabel", background="#262626", foreground="#57ff57")
        style.configure("Danger.TLabel", background="#262626", foreground="#ff6a6a")
        style.configure("TButton", background="#363636", foreground="#ffffff", bordercolor="#999999", focusthickness=1)
        style.map(
            "TButton",
            background=[("active", "#454545"), ("disabled", "#303030")],
            foreground=[("disabled", "#9a9a9a")],
        )
        style.configure("TCheckbutton", background="#262626", foreground="#ffffff")
        style.map("TCheckbutton", background=[("active", "#262626")])
        style.configure("TCombobox", fieldbackground="#3a3a3a", background="#3a3a3a", foreground="#ffffff")
        style.configure("Horizontal.TProgressbar", troughcolor="#d9d9d9", background="#6ca0dc", bordercolor="#777777")

        self.option_add("*Font", ("Consolas", 9))
        self.option_add("*Entry.Background", "#3a3a3a")
        self.option_add("*Entry.Foreground", "#ffffff")
        self.option_add("*Entry.InsertBackground", "#ffffff")
        self.option_add("*Text.Background", "#303030")
        self.option_add("*Text.Foreground", "#ffffff")
        self.option_add("*Listbox.Background", "#303030")
        self.option_add("*Listbox.Foreground", "#ffffff")
        self.option_add("*Listbox.SelectBackground", "#505050")
        self.option_add("*Listbox.SelectForeground", "#ffffff")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="Создать профиль", command=self.create_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Дублировать", command=self.duplicate_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Удалить", command=self.delete_profile).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Button(toolbar, text="Сохранить", command=self.save_current_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Рандом FP", command=self.randomize_fingerprint).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Button(toolbar, text="Запустить браузер", command=self.start_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Остановить", command=self.stop_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Проверить IP", command=self.open_ip_check).pack(side=tk.LEFT, padx=(0, 6))

        profiles_box = ttk.LabelFrame(root, text=" Профили ", style="Dark.TLabelframe", padding=8)
        profiles_box.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        profiles_box.rowconfigure(0, weight=1)
        self.profile_list = tk.Listbox(profiles_box, width=28, height=24, exportselection=False)
        self.profile_list.grid(row=0, column=0, sticky="ns")
        self.profile_list.bind("<<ListboxSelect>>", self.on_profile_select)

        details = ttk.Frame(root)
        details.grid(row=1, column=1, sticky="nsew")
        details.columnconfigure(0, weight=1)
        details.rowconfigure(4, weight=1)

        self._build_general_box(details)
        self._build_proxy_box(details)
        self._build_fingerprint_box(details)
        self._build_cookie_box(details)
        self._build_log_box(details)

        status = tk.Label(
            root,
            textvariable=self.status_var,
            bg="#262626",
            fg="#ffffff",
            anchor="w",
            relief=tk.SUNKEN,
            bd=1,
        )
        status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.refresh_profile_list()

    def _build_general_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" Основное ", style="Dark.TLabelframe", padding=8)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)

        self.vars["name"] = tk.StringVar()
        self.vars["start_url"] = tk.StringVar()
        self.vars["browser_path"] = tk.StringVar()
        self.vars["local_port"] = tk.StringVar()

        label_entry(box, "Имя профиля:", self.vars["name"], row=0, column=0)
        label_entry(box, "Локальный порт:", self.vars["local_port"], row=0, column=2, width=10)
        label_entry(box, "Стартовая страница:", self.vars["start_url"], row=1, column=0, columnspan=3)
        label_entry(box, "Путь браузера:", self.vars["browser_path"], row=2, column=0, columnspan=3)
        ttk.Button(box, text="Обзор", command=self.browse_browser_path).grid(row=2, column=4, padx=(8, 0))
        ttk.Label(box, textvariable=self.running_status_var, style="Ok.TLabel").grid(row=0, column=4, sticky="w", padx=(8, 0))

    def _build_proxy_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" Прокси HTTP / SOCKS5 ", style="Dark.TLabelframe", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)
        box.columnconfigure(5, weight=1)

        self.vars["proxy_type"] = tk.StringVar(value="http")
        self.vars["proxy_host"] = tk.StringVar()
        self.vars["proxy_port"] = tk.StringVar()
        self.vars["proxy_login"] = tk.StringVar()
        self.vars["proxy_password"] = tk.StringVar()
        self.vars["proxy_line"] = tk.StringVar()

        ttk.Label(box, text="Тип:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            box,
            values=("http", "socks5"),
            width=10,
            state="readonly",
            textvariable=self.vars["proxy_type"],
        ).grid(row=0, column=1, sticky="w", padx=(8, 14))
        label_entry(box, "Хост:", self.vars["proxy_host"], row=0, column=2, width=24)
        label_entry(box, "Порт:", self.vars["proxy_port"], row=0, column=4, width=10)
        label_entry(box, "Логин:", self.vars["proxy_login"], row=1, column=0, width=24)
        label_entry(box, "Пароль:", self.vars["proxy_password"], row=1, column=2, width=24, show="*")
        label_entry(box, "Строка прокси:", self.vars["proxy_line"], row=2, column=0, columnspan=3)
        ttk.Button(box, text="Разобрать строку", command=self.apply_proxy_line).grid(row=2, column=4, columnspan=2, sticky="w", padx=(8, 0))

    def _build_fingerprint_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" Fingerprint браузера ", style="Dark.TLabelframe", padding=8)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for col in (1, 3, 5):
            box.columnconfigure(col, weight=1)

        self.vars["user_agent"] = tk.StringVar()
        self.vars["platform"] = tk.StringVar()
        self.vars["locale"] = tk.StringVar()
        self.vars["timezone"] = tk.StringVar()
        self.vars["screen_width"] = tk.StringVar()
        self.vars["screen_height"] = tk.StringVar()
        self.vars["viewport_width"] = tk.StringVar()
        self.vars["viewport_height"] = tk.StringVar()
        self.vars["hardware_concurrency"] = tk.StringVar()
        self.vars["device_memory"] = tk.StringVar()
        self.vars["max_touch_points"] = tk.StringVar()
        self.vars["webgl_vendor"] = tk.StringVar()
        self.vars["webgl_renderer"] = tk.StringVar()
        self.vars["canvas_noise"] = tk.BooleanVar()
        self.vars["webrtc_protection"] = tk.BooleanVar()

        label_entry(box, "User-Agent:", self.vars["user_agent"], row=0, column=0, columnspan=5)
        label_entry(box, "Platform:", self.vars["platform"], row=1, column=0, width=14)
        label_entry(box, "Locale:", self.vars["locale"], row=1, column=2, width=14)
        label_entry(box, "Timezone:", self.vars["timezone"], row=1, column=4, width=18)
        label_entry(box, "Screen W:", self.vars["screen_width"], row=2, column=0, width=10)
        label_entry(box, "Screen H:", self.vars["screen_height"], row=2, column=2, width=10)
        label_entry(box, "Viewport W:", self.vars["viewport_width"], row=2, column=4, width=10)
        label_entry(box, "Viewport H:", self.vars["viewport_height"], row=3, column=0, width=10)
        label_entry(box, "CPU:", self.vars["hardware_concurrency"], row=3, column=2, width=10)
        label_entry(box, "RAM:", self.vars["device_memory"], row=3, column=4, width=10)
        label_entry(box, "Touch:", self.vars["max_touch_points"], row=4, column=0, width=10)
        label_entry(box, "WebGL vendor:", self.vars["webgl_vendor"], row=4, column=2, width=22)
        label_entry(box, "WebGL renderer:", self.vars["webgl_renderer"], row=5, column=0, columnspan=5)
        ttk.Checkbutton(box, text="Canvas noise", variable=self.vars["canvas_noise"]).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(box, text="WebRTC protection", variable=self.vars["webrtc_protection"]).grid(row=6, column=2, columnspan=2, sticky="w", pady=(6, 0))

    def _build_cookie_box(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" Cookies / Профильные данные ", style="Dark.TLabelframe", padding=8)
        box.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(3, weight=1)
        ttk.Button(box, text="Экспорт cookies", command=self.export_cookies).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Button(box, text="Импорт cookies", command=self.import_cookies).grid(row=0, column=1, sticky="w", padx=(0, 6))
        ttk.Button(box, text="Открыть папку", command=self.open_profile_folder).grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Label(box, textvariable=self.cookie_status_var, style="Dim.TLabel").grid(row=0, column=3, sticky="w")

    def _build_log_box(self, parent: ttk.Frame) -> None:
        progress_box = ttk.LabelFrame(parent, text=" Прогресс ", style="Dark.TLabelframe", padding=8)
        progress_box.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        progress_box.columnconfigure(0, weight=1)
        ttk.Progressbar(progress_box, mode="determinate", variable=self.progress_var).grid(row=0, column=0, sticky="ew")

        box = ttk.LabelFrame(parent, text=" Лог ", style="Dark.TLabelframe", padding=8)
        box.grid(row=5, column=0, sticky="nsew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.log_text = tk.Text(box, height=10, wrap=tk.WORD, relief=tk.SUNKEN, bd=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def refresh_profile_list(self) -> None:
        if self.profile_list is None:
            return
        self.profile_list.delete(0, tk.END)
        selected_index = 0
        for index, profile in enumerate(self.profiles):
            marker = "● " if self.runtime.is_running(profile.id) else "  "
            self.profile_list.insert(tk.END, f"{marker}{profile.name}")
            if profile.id == self.selected_profile_id:
                selected_index = index
        if self.profiles:
            self.profile_list.selection_set(selected_index)
            self.profile_list.activate(selected_index)

    def on_profile_select(self, _event: tk.Event | None = None) -> None:
        if self.profile_list is None:
            return
        selection = self.profile_list.curselection()
        if not selection:
            return
        index = selection[0]
        if 0 <= index < len(self.profiles):
            self.selected_profile_id = self.profiles[index].id
            self._load_profile_to_form()

    def selected_profile(self) -> BrowserProfile | None:
        if not self.selected_profile_id:
            return None
        return self.store.get(self.selected_profile_id)

    def _load_profile_to_form(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        fp = profile.fingerprint
        mapping = {
            "name": profile.name,
            "start_url": profile.start_url,
            "browser_path": profile.browser_path,
            "local_port": profile.local_port,
            "proxy_type": profile.proxy_type,
            "proxy_host": profile.proxy_host,
            "proxy_port": profile.proxy_port,
            "proxy_login": profile.proxy_login,
            "proxy_password": profile.proxy_password,
            "proxy_line": self._profile_proxy_line(profile),
            "user_agent": fp.user_agent,
            "platform": fp.platform,
            "locale": fp.locale,
            "timezone": fp.timezone,
            "screen_width": str(fp.screen_width),
            "screen_height": str(fp.screen_height),
            "viewport_width": str(fp.viewport_width),
            "viewport_height": str(fp.viewport_height),
            "hardware_concurrency": str(fp.hardware_concurrency),
            "device_memory": str(fp.device_memory),
            "max_touch_points": str(fp.max_touch_points),
            "webgl_vendor": fp.webgl_vendor,
            "webgl_renderer": fp.webgl_renderer,
            "canvas_noise": fp.canvas_noise,
            "webrtc_protection": fp.webrtc_protection,
        }
        for key, value in mapping.items():
            if key in self.vars:
                self.vars[key].set(value)
        cookie_path = cookie_file(profile.id)
        self.cookie_status_var.set(f"Cookies: {cookie_path}")
        self.running_status_var.set("● Запущен" if self.runtime.is_running(profile.id) else "")
        self.status_var.set(f"Выбран: {profile.name}")

    def _form_to_profile(self, profile: BrowserProfile) -> None:
        fp = profile.fingerprint
        profile.name = self.vars["name"].get().strip() or "Без имени"
        profile.start_url = self.vars["start_url"].get().strip()
        profile.browser_path = self.vars["browser_path"].get().strip()
        profile.local_port = self.vars["local_port"].get().strip()
        profile.proxy_type = self.vars["proxy_type"].get().strip() or "http"
        profile.proxy_host = self.vars["proxy_host"].get().strip()
        profile.proxy_port = self.vars["proxy_port"].get().strip()
        profile.proxy_login = self.vars["proxy_login"].get().strip()
        profile.proxy_password = self.vars["proxy_password"].get()

        fp.user_agent = self.vars["user_agent"].get().strip() or DEFAULT_USER_AGENTS[0]
        fp.platform = self.vars["platform"].get().strip() or "Win32"
        fp.locale = self.vars["locale"].get().strip() or "ru-RU"
        fp.timezone = self.vars["timezone"].get().strip() or "Europe/Moscow"
        fp.screen_width = int_or_default(self.vars["screen_width"].get(), 1366)
        fp.screen_height = int_or_default(self.vars["screen_height"].get(), 768)
        fp.viewport_width = int_or_default(self.vars["viewport_width"].get(), 1280)
        fp.viewport_height = int_or_default(self.vars["viewport_height"].get(), 720)
        fp.hardware_concurrency = int_or_default(self.vars["hardware_concurrency"].get(), 8)
        fp.device_memory = int_or_default(self.vars["device_memory"].get(), 8)
        fp.max_touch_points = int_or_default(self.vars["max_touch_points"].get(), 0)
        fp.webgl_vendor = self.vars["webgl_vendor"].get().strip() or DEFAULT_WEBGL[0][0]
        fp.webgl_renderer = self.vars["webgl_renderer"].get().strip() or DEFAULT_WEBGL[0][1]
        fp.canvas_noise = bool(self.vars["canvas_noise"].get())
        fp.webrtc_protection = bool(self.vars["webrtc_protection"].get())

    def save_current_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        self._form_to_profile(profile)
        self.store.save()
        self.refresh_profile_list()
        self.status_var.set(f"Сохранено: {profile.name}")
        self.enqueue_log(f"[{now_label()}] Профиль сохранён: {profile.name}")

    def create_profile(self) -> None:
        profile = self.store.make_default_profile()
        self.profiles = self.store.profiles
        self.store.add(profile)
        self.selected_profile_id = profile.id
        self.refresh_profile_list()
        self._load_profile_to_form()
        self.enqueue_log(f"[{now_label()}] Создан профиль: {profile.name}")

    def duplicate_profile(self) -> None:
        current = self.selected_profile()
        if not current:
            return
        self._form_to_profile(current)
        raw = current.to_dict()
        raw["id"] = uuid.uuid4().hex
        raw["name"] = f"{current.name} копия"
        clone = BrowserProfile.from_dict(raw)
        self.store.add(clone)
        self.selected_profile_id = clone.id
        self.refresh_profile_list()
        self._load_profile_to_form()
        self.enqueue_log(f"[{now_label()}] Дубликат создан: {clone.name}")

    def delete_profile(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        if self.runtime.is_running(profile.id):
            messagebox.showwarning("Профиль запущен", "Сначала остановите браузер этого профиля.")
            return
        ok = messagebox.askyesno("Удалить профиль", f"Удалить профиль «{profile.name}» вместе с файлами?")
        if not ok:
            return
        self.store.remove(profile.id, remove_files=True)
        self.profiles = self.store.profiles
        self.selected_profile_id = self.profiles[0].id if self.profiles else None
        self.refresh_profile_list()
        self._load_profile_to_form()
        self.enqueue_log(f"[{now_label()}] Профиль удалён: {profile.name}")

    def randomize_fingerprint(self) -> None:
        ua = random.choice(DEFAULT_USER_AGENTS)
        platform = "MacIntel" if "Macintosh" in ua else "Linux x86_64" if "X11" in ua else "Win32"
        screen = random.choice([(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)])
        webgl_vendor, webgl_renderer = random.choice(DEFAULT_WEBGL)
        self.vars["user_agent"].set(ua)
        self.vars["platform"].set(platform)
        self.vars["locale"].set(random.choice(["ru-RU", "en-US", "en-GB", "de-DE", "pl-PL"]))
        self.vars["timezone"].set(random.choice(DEFAULT_TIMEZONES))
        self.vars["screen_width"].set(str(screen[0]))
        self.vars["screen_height"].set(str(screen[1]))
        self.vars["viewport_width"].set(str(max(800, screen[0] - random.choice([0, 80, 120]))))
        self.vars["viewport_height"].set(str(max(600, screen[1] - random.choice([40, 80, 120]))))
        self.vars["hardware_concurrency"].set(str(random.choice([4, 6, 8, 12, 16])))
        self.vars["device_memory"].set(str(random.choice([4, 8, 16])))
        self.vars["max_touch_points"].set(str(random.choice([0, 0, 0, 1, 5])))
        self.vars["webgl_vendor"].set(webgl_vendor)
        self.vars["webgl_renderer"].set(webgl_renderer)
        self.vars["canvas_noise"].set(True)
        self.vars["webrtc_protection"].set(True)
        self.status_var.set("Fingerprint сгенерирован")

    def start_selected(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        self.save_current_profile()
        self.progress_var.set(15)
        self.runtime.start(profile)
        self.after(500, self._update_running_state)

    def stop_selected(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        self.progress_var.set(60)
        self.runtime.stop(profile.id)
        self.after(500, self._update_running_state)

    def open_ip_check(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        self.vars["start_url"].set("https://browserleaks.com/ip")
        self.start_selected()

    def browse_browser_path(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите chrome.exe / msedge.exe",
            filetypes=[("Browser executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.vars["browser_path"].set(path)

    def apply_proxy_line(self) -> None:
        proxy_type, host, port, login, password = split_proxy(self.vars["proxy_line"].get())
        self.vars["proxy_type"].set("socks5" if proxy_type.lower() == "socks5" else "http")
        self.vars["proxy_host"].set(host)
        self.vars["proxy_port"].set(port)
        self.vars["proxy_login"].set(login)
        self.vars["proxy_password"].set(password)
        self.status_var.set("Прокси разобран")

    def export_cookies(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        if self.runtime.is_running(profile.id):
            target = self.runtime.export_cookies(profile.id)
        else:
            target = cookie_file(profile.id)
        save_path = filedialog.asksaveasfilename(
            title="Сохранить cookies",
            defaultextension=".json",
            initialfile=f"{profile.name}_cookies.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not save_path:
            return
        if target.exists():
            shutil.copyfile(target, save_path)
            self.enqueue_log(f"[{now_label()}] Cookies экспортированы: {save_path}")
        else:
            with open(save_path, "w", encoding="utf-8") as fh:
                json.dump([], fh, ensure_ascii=False, indent=2)
            self.enqueue_log(f"[{now_label()}] Cookies ещё пустые, создан файл: {save_path}")

    def import_cookies(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        path = filedialog.askopenfilename(
            title="Импорт cookies JSON",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        target = cookie_file(profile.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            cookies = raw.get("cookies", raw) if isinstance(raw, dict) else raw
            if not isinstance(cookies, list):
                raise ValueError("Файл должен содержать массив cookies или storage_state")
            with target.open("w", encoding="utf-8") as fh:
                json.dump(cookies, fh, ensure_ascii=False, indent=2)
            self.enqueue_log(f"[{now_label()}] Cookies импортированы: {len(cookies)}")
        except Exception as exc:
            messagebox.showerror("Ошибка импорта", str(exc))

    def open_profile_folder(self) -> None:
        profile = self.selected_profile()
        if not profile:
            return
        target = profile_dir(profile.id)
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc))

    def _profile_proxy_line(self, profile: BrowserProfile) -> str:
        if not profile.proxy_host.strip() or not profile.proxy_port.strip():
            return ""
        auth = ""
        if profile.proxy_login.strip():
            auth = f"{profile.proxy_login}:{profile.proxy_password}@"
        return f"{profile.proxy_type}://{auth}{profile.proxy_host}:{profile.proxy_port}"

    def enqueue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        changed = False
        while True:
            try:
                message = self.log_queue.get_nowait()
            except Empty:
                break
            self._append_log(message)
            changed = True
        if changed:
            self._update_running_state()
        self.after(150, self._drain_log_queue)

    def _append_log(self, message: str) -> None:
        if self.log_text is None:
            return
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.status_var.set(re.sub(r"\s+", " ", message)[-120:])

    def _update_running_state(self) -> None:
        self.refresh_profile_list()
        self._load_profile_to_form()
        self.progress_var.set(100 if self.selected_profile() and self.runtime.is_running(self.selected_profile().id) else 0)

    def on_close(self) -> None:
        self.save_current_profile()
        self.status_var.set("Останавливаю браузеры...")
        self.update_idletasks()
        self.runtime.stop_all_sync()
        self.destroy()


class ManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BrowserManager")
        self.geometry("1280x780")
        self.minsize(1100, 680)
        self.configure(bg="#181818")

        self.store = ProfileStore(PROFILES_FILE)
        self.profiles = self.store.load()
        self.selected_profile_id: str | None = self.profiles[0].id if self.profiles else None
        self.checked_profile_ids: set[str] = set()
        self.log_queue: Queue[str] = Queue()
        self.runtime = BrowserRuntime(self.enqueue_log)
        self.status_var = tk.StringVar(value="Готов")
        self.bulk_count_var = tk.StringVar(value="")
        self.profile_table: ttk.Treeview | None = None
        self.bulk_panel: tk.Frame | None = None
        self.log_text: tk.Text | None = None

        self._setup_style()
        self._build_ui()
        self.refresh_profile_table()
        self.after(150, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#181818")
        style.configure("TLabel", background="#181818", foreground="#f2f2f2")
        style.configure("Dim.TLabel", background="#181818", foreground="#9c9c9c")
        style.configure("TButton", background="#2d2d2d", foreground="#f4f4f4", bordercolor="#444444")
        style.map("TButton", background=[("active", "#3a3a3a"), ("disabled", "#232323")])
        style.configure(
            "Treeview",
            background="#1d1d1d",
            fieldbackground="#1d1d1d",
            foreground="#e8e8e8",
            rowheight=34,
            bordercolor="#2e2e2e",
        )
        style.configure("Treeview.Heading", background="#161616", foreground="#f2f2f2", relief=tk.FLAT)
        style.map("Treeview", background=[("selected", "#26384a")], foreground=[("selected", "#ffffff")])
        style.configure("TCombobox", fieldbackground="#242424", background="#242424", foreground="#ffffff")
        self.option_add("*Font", ("Segoe UI", 9))
        self.option_add("*Entry.Background", "#242424")
        self.option_add("*Entry.Foreground", "#ffffff")
        self.option_add("*Entry.InsertBackground", "#ffffff")
        self.option_add("*Text.Background", "#202020")
        self.option_add("*Text.Foreground", "#f5f5f5")

    def _build_ui(self) -> None:
        shell = tk.Frame(self, bg="#181818")
        shell.pack(fill=tk.BOTH, expand=True)
        shell.columnconfigure(2, weight=1)
        shell.rowconfigure(0, weight=1)

        nav = tk.Frame(shell, width=46, bg="#101010")
        nav.grid(row=0, column=0, sticky="ns")
        nav.grid_propagate(False)
        tk.Label(nav, text="B", bg="#8b3cf6", fg="#ffffff", font=("Segoe UI", 16, "bold")).pack(fill=tk.X, padx=8, pady=(10, 24))
        for item in ("☰", "↔", "⚙", "?"):
            tk.Label(nav, text=item, bg="#101010", fg="#9d9d9d", font=("Segoe UI", 13)).pack(pady=12)

        side = tk.Frame(shell, width=205, bg="#202020")
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)
        tk.Entry(side, relief=tk.FLAT, fg="#cfcfcf", insertbackground="#fff").pack(fill=tk.X, padx=12, pady=(12, 18), ipady=6)
        tk.Label(side, text="Все профили", anchor="w", bg="#202020", fg="#ffffff", font=("Segoe UI", 9, "bold")).pack(fill=tk.X, padx=16)
        tk.Label(side, text="📁 Default", anchor="w", bg="#202020", fg="#aeb8c2").pack(fill=tk.X, padx=22, pady=(18, 4))
        tk.Label(side, text="📁 BrowserManager", anchor="w", bg="#203244", fg="#43a8ff").pack(fill=tk.X, padx=8, pady=4, ipady=8)

        main = tk.Frame(shell, bg="#181818")
        main.grid(row=0, column=2, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        topbar = tk.Frame(main, bg="#121212", height=58)
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_propagate(False)
        tk.Label(topbar, text="📂  ПРОФИЛИ", bg="#121212", fg="#f5f5f5", font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=18)
        self._bar_button(topbar, "Создать профиль", self.create_profile).pack(side=tk.RIGHT, padx=(6, 16), pady=12)
        self._bar_button(topbar, "Прокси пул", self.open_proxy_pool_panel).pack(side=tk.RIGHT, padx=6, pady=12)
        self._bar_button(topbar, "Быстрый профиль", self.duplicate_profile).pack(side=tk.RIGHT, padx=6, pady=12)

        banner = tk.Frame(main, bg="#102231", height=56)
        banner.grid(row=1, column=0, sticky="ew", padx=8, pady=(8, 0))
        banner.grid_propagate(False)
        tk.Label(
            banner,
            text="Proxy, cookies и fingerprint вынесены в панели управления профилями",
            bg="#102231",
            fg="#2fa8ff",
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=22)

        table_wrap = tk.Frame(main, bg="#181818")
        table_wrap.grid(row=2, column=0, sticky="nsew", padx=8, pady=(10, 0))
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)
        columns = ("select", "platform", "name", "status", "tags", "proxy", "local_port", "actions")
        table = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="browse")
        self.profile_table = table
        headings = {
            "select": "",
            "platform": "ОС",
            "name": "Название",
            "status": "Статус",
            "tags": "Теги",
            "proxy": "Прокси",
            "local_port": "Лок. порт",
            "actions": "",
        }
        widths = {
            "select": 42,
            "platform": 58,
            "name": 260,
            "status": 120,
            "tags": 130,
            "proxy": 390,
            "local_port": 110,
            "actions": 48,
        }
        for col in columns:
            table.heading(col, text=headings[col])
            table.column(col, width=widths[col], minwidth=widths[col], anchor=tk.W if col not in {"select", "actions"} else tk.CENTER)
        table.tag_configure("running", foreground="#6dff7a")
        table.tag_configure("stopped", foreground="#e8e8e8")
        table.grid(row=0, column=0, sticky="nsew")
        table.bind("<Button-1>", self.on_table_click)
        table.bind("<Double-1>", lambda _event: self.open_profile_settings())
        scroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scroll.set)

        self.bulk_panel = tk.Frame(main, bg="#2d9dfb", bd=0)
        self.bulk_panel.grid(row=3, column=0, pady=10)
        tk.Label(self.bulk_panel, textvariable=self.bulk_count_var, bg="#58b5ff", fg="#ffffff", font=("Segoe UI", 11, "bold"), width=4).pack(side=tk.LEFT, padx=(8, 6), pady=8, ipady=7)
        self._bulk_button("▶ Запуск", self.start_profiles).pack(side=tk.LEFT, padx=4, pady=8)
        self._bulk_button("Ⅱ Стоп", self.stop_profiles).pack(side=tk.LEFT, padx=4, pady=8)
        self._bulk_button("Fingerprint", lambda: self.open_fingerprint_panel()).pack(side=tk.LEFT, padx=4, pady=8)
        self._bulk_button("Cookies", lambda: self.open_cookie_panel()).pack(side=tk.LEFT, padx=4, pady=8)
        self._bulk_button("Proxy random", lambda: self.assign_random_proxy_to_profiles()).pack(side=tk.LEFT, padx=4, pady=8)
        self._bulk_button("⋯ Настройки", self.open_profile_settings).pack(side=tk.LEFT, padx=(4, 8), pady=8)
        self.bulk_panel.grid_remove()

        log_box = tk.Frame(main, bg="#181818")
        log_box.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        tk.Label(log_box, text="Лог", bg="#181818", fg="#9b9b9b").pack(anchor="w")
        self.log_text = tk.Text(log_box, height=5, wrap=tk.WORD, relief=tk.FLAT, bg="#202020", fg="#eeeeee")
        self.log_text.pack(fill=tk.X, expand=False)

        status = tk.Label(self, textvariable=self.status_var, bg="#181818", fg="#cfcfcf", anchor="w", relief=tk.SUNKEN, bd=1)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _bar_button(self, parent: tk.Widget, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg="#2b2b2b", fg="#f5f5f5", activebackground="#353535", activeforeground="#ffffff", relief=tk.FLAT, padx=14)

    def _bulk_button(self, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(self.bulk_panel, text=text, command=command, bg="#2d9dfb", fg="#ffffff", activebackground="#47adff", activeforeground="#ffffff", relief=tk.FLAT, padx=10)

    def refresh_profile_table(self) -> None:
        if self.profile_table is None:
            return
        table = self.profile_table
        table.delete(*table.get_children())
        for profile in self.profiles:
            running = self.runtime.is_running(profile.id)
            table.insert(
                "",
                tk.END,
                iid=profile.id,
                values=(
                    "☑" if profile.id in self.checked_profile_ids else "☐",
                    "WIN",
                    profile.name,
                    "Запущен" if running else "Остановлен",
                    profile.notes.strip() or "теги",
                    proxy_label(profile),
                    profile.local_port.strip() or "авто",
                    "⋯",
                ),
                tags=("running" if running else "stopped",),
            )
        if self.selected_profile_id and self.selected_profile_id in table.get_children():
            table.selection_set(self.selected_profile_id)
            table.focus(self.selected_profile_id)
        self.refresh_bulk_panel()

    def refresh_bulk_panel(self) -> None:
        if self.bulk_panel is None:
            return
        count = len(self.checked_profile_ids)
        if count:
            self.bulk_count_var.set(str(count))
            self.bulk_panel.grid()
        else:
            self.bulk_panel.grid_remove()

    def on_table_click(self, event: tk.Event) -> str | None:
        if self.profile_table is None:
            return None
        table = self.profile_table
        row_id = table.identify_row(event.y)
        column = table.identify_column(event.x)
        if not row_id:
            return None
        self.selected_profile_id = row_id
        table.selection_set(row_id)
        table.focus(row_id)
        if column == "#1":
            if row_id in self.checked_profile_ids:
                self.checked_profile_ids.remove(row_id)
            else:
                self.checked_profile_ids.add(row_id)
            self.refresh_profile_table()
            return "break"
        if column == "#8":
            self.open_profile_settings(self.store.get(row_id))
            return "break"
        profile = self.store.get(row_id)
        if profile:
            self.status_var.set(f"Выбран: {profile.name}")
        return None

    def selected_profile(self) -> BrowserProfile | None:
        if not self.selected_profile_id:
            return None
        return self.store.get(self.selected_profile_id)

    def checked_profiles(self) -> list[BrowserProfile]:
        return [profile for profile in self.profiles if profile.id in self.checked_profile_ids]

    def action_profiles(self) -> list[BrowserProfile]:
        checked = self.checked_profiles()
        if checked:
            return checked
        profile = self.selected_profile()
        return [profile] if profile else []

    def create_profile(self) -> None:
        profile = self.store.make_default_profile()
        self.store.add(profile)
        self.profiles = self.store.profiles
        self.selected_profile_id = profile.id
        self.refresh_profile_table()
        self.open_profile_settings(profile)
        self.enqueue_log(f"[{now_label()}] Создан профиль: {profile.name}")

    def duplicate_profile(self) -> None:
        source = self.selected_profile() or (self.profiles[0] if self.profiles else None)
        if not source:
            self.create_profile()
            return
        raw = source.to_dict()
        raw["id"] = uuid.uuid4().hex
        raw["name"] = f"{source.name} копия"
        clone = BrowserProfile.from_dict(raw)
        self.store.add(clone)
        self.profiles = self.store.profiles
        self.selected_profile_id = clone.id
        self.refresh_profile_table()
        self.enqueue_log(f"[{now_label()}] Дубликат создан: {clone.name}")

    def delete_profile(self) -> None:
        targets = self.action_profiles()
        if not targets:
            return
        running = [profile.name for profile in targets if self.runtime.is_running(profile.id)]
        if running:
            messagebox.showwarning("Профили запущены", "Сначала остановите: " + ", ".join(running), parent=self)
            return
        if not messagebox.askyesno("Удалить профили", f"Удалить профилей: {len(targets)}?", parent=self):
            return
        for profile in targets:
            self.store.remove(profile.id, remove_files=True)
            self.checked_profile_ids.discard(profile.id)
        self.profiles = self.store.profiles
        self.selected_profile_id = self.profiles[0].id if self.profiles else None
        self.refresh_profile_table()
        self.enqueue_log(f"[{now_label()}] Удалено профилей: {len(targets)}")

    def start_profiles(self) -> None:
        targets = self.action_profiles()
        for profile in targets:
            self.runtime.start(profile)
        self.status_var.set(f"Запуск профилей: {len(targets)}")
        self.after(700, self.refresh_profile_table)

    def stop_profiles(self) -> None:
        targets = self.action_profiles()
        for profile in targets:
            self.runtime.stop(profile.id)
        self.status_var.set(f"Остановка профилей: {len(targets)}")
        self.after(700, self.refresh_profile_table)

    def open_ip_check(self) -> None:
        targets = self.action_profiles()
        for profile in targets:
            profile.start_url = "https://browserleaks.com/ip"
        self.store.save()
        self.start_profiles()

    def load_proxy_pool(self) -> list[str]:
        if not PROXY_POOL_FILE.exists():
            return []
        lines = PROXY_POOL_FILE.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

    def save_proxy_pool(self, lines: list[str]) -> None:
        ensure_data_dirs()
        PROXY_POOL_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def assign_random_proxy_to_profiles(self, profiles: list[BrowserProfile] | None = None, pool: list[str] | None = None) -> None:
        targets = profiles or self.action_profiles()
        proxy_pool = pool or self.load_proxy_pool()
        valid = [line for line in proxy_pool if split_proxy(line)[1] and split_proxy(line)[2]]
        if not targets:
            messagebox.showinfo("Нет профилей", "Выберите профили для назначения proxy.", parent=self)
            return
        if not valid:
            messagebox.showwarning("Proxy pool пуст", "Добавьте proxy в общий список.", parent=self)
            self.open_proxy_pool_panel()
            return
        for profile in targets:
            proxy_line_to_profile(profile, random.choice(valid))
        self.store.save()
        self.refresh_profile_table()
        self.enqueue_log(f"[{now_label()}] Рандомно назначены proxy: {len(targets)} профилей")

    def open_proxy_pool_panel(self) -> None:
        win, panel = self._modal("Общий список proxy", 760, 560)
        tk.Label(panel, text="Proxy pool", bg="#1f1f1f", fg="#ffffff", font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 12))
        tk.Label(panel, text="Форматы: http://host:port, socks5://login:password@host:port, host:port:login:password", bg="#1f1f1f", fg="#a8a8a8").pack(anchor="w")
        text = tk.Text(panel, height=18, relief=tk.FLAT, bg="#242424", fg="#ffffff", insertbackground="#ffffff")
        text.pack(fill=tk.BOTH, expand=True, pady=12)
        text.insert("1.0", "\n".join(self.load_proxy_pool()))

        buttons = tk.Frame(panel, bg="#1f1f1f")
        buttons.pack(fill=tk.X)

        def current_lines() -> list[str]:
            lines = [line.strip() for line in text.get("1.0", tk.END).splitlines() if line.strip()]
            self.save_proxy_pool(lines)
            return lines

        def import_file() -> None:
            path = filedialog.askopenfilename(parent=win, title="Загрузить список proxy", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
            if not path:
                return
            with open(path, "r", encoding="utf-8") as fh:
                loaded = [line.strip() for line in fh.read().splitlines() if line.strip()]
            text.delete("1.0", tk.END)
            text.insert("1.0", "\n".join(loaded))

        self._bar_button(buttons, "Загрузить TXT", import_file).pack(side=tk.LEFT, padx=(0, 8))
        self._bar_button(buttons, "Сохранить", lambda: (current_lines(), self.enqueue_log(f"[{now_label()}] Proxy pool сохранён"))).pack(side=tk.LEFT, padx=8)
        self._bar_button(buttons, "Рандом выбранным", lambda: self.assign_random_proxy_to_profiles(self.action_profiles(), current_lines())).pack(side=tk.LEFT, padx=8)
        self._bar_button(buttons, "Рандом всем", lambda: self.assign_random_proxy_to_profiles(self.profiles, current_lines())).pack(side=tk.LEFT, padx=8)
        self._bar_button(buttons, "Закрыть", win.destroy).pack(side=tk.RIGHT)

    def open_profile_settings(self, profile: BrowserProfile | None = None) -> None:
        profile = profile or self.selected_profile()
        if not profile:
            return
        win, panel = self._modal(f"Редактировать профиль {profile.name}", 960, 620)
        top = tk.Frame(panel, bg="#1f1f1f")
        top.pack(fill=tk.X)
        tk.Label(top, text=f"Редактировать профиль {profile.name}", bg="#1f1f1f", fg="#ffffff", font=("Segoe UI", 20, "bold")).pack(side=tk.LEFT)
        self._bar_button(top, "×", win.destroy).pack(side=tk.RIGHT)

        body = tk.Frame(panel, bg="#1f1f1f")
        body.pack(fill=tk.BOTH, expand=True, pady=(18, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)

        left = tk.Frame(body, bg="#1f1f1f")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
        right = tk.Frame(body, bg="#292929", padx=18, pady=18)
        right.grid(row=0, column=1, sticky="ns")

        vars_: dict[str, tk.Variable] = {
            "name": tk.StringVar(value=profile.name),
            "start_url": tk.StringVar(value=profile.start_url),
            "browser_path": tk.StringVar(value=profile.browser_path),
            "local_port": tk.StringVar(value=profile.local_port),
            "proxy_type": tk.StringVar(value=profile.proxy_type),
            "proxy_host": tk.StringVar(value=profile.proxy_host),
            "proxy_port": tk.StringVar(value=profile.proxy_port),
            "proxy_login": tk.StringVar(value=profile.proxy_login),
            "proxy_password": tk.StringVar(value=profile.proxy_password),
            "proxy_line": tk.StringVar(value=self._profile_proxy_line(profile)),
            "notes": tk.StringVar(value=profile.notes),
        }
        self._field(left, "Название профиля", vars_["name"], 0, 0, colspan=3)
        self._field(left, "Стартовая страница", vars_["start_url"], 1, 0, colspan=3)
        self._field(left, "Путь браузера", vars_["browser_path"], 2, 0, colspan=2)
        self._bar_button(left, "Обзор", lambda: self._browse_to_var(vars_["browser_path"], win)).grid(row=2, column=3, sticky="w", padx=8)
        self._field(left, "Локальный порт", vars_["local_port"], 3, 0, width=16)
        self._field(left, "Теги", vars_["notes"], 3, 2, width=24)

        tk.Label(left, text="Прокси", bg="#1f1f1f", fg="#ffffff", font=("Segoe UI", 12, "bold")).grid(row=4, column=0, sticky="w", pady=(18, 6))
        ttk.Combobox(left, values=("http", "socks5"), textvariable=vars_["proxy_type"], width=10, state="readonly").grid(row=5, column=0, sticky="w", pady=4)
        self._field(left, "Host", vars_["proxy_host"], 5, 1, width=24)
        self._field(left, "Port", vars_["proxy_port"], 5, 3, width=10)
        self._field(left, "Login", vars_["proxy_login"], 6, 0, width=24)
        self._field(left, "Password", vars_["proxy_password"], 6, 2, width=24, show="*")
        self._field(left, "Строка proxy", vars_["proxy_line"], 7, 0, colspan=2)

        def parse_proxy() -> None:
            proxy_type, host, port, login, password = split_proxy(vars_["proxy_line"].get())
            vars_["proxy_type"].set("socks5" if proxy_type == "socks5" else "http")
            vars_["proxy_host"].set(host)
            vars_["proxy_port"].set(port)
            vars_["proxy_login"].set(login)
            vars_["proxy_password"].set(password)

        self._bar_button(left, "Разобрать", parse_proxy).grid(row=7, column=3, sticky="w", padx=8)

        tools = tk.Frame(left, bg="#1f1f1f")
        tools.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(18, 0))
        self._bar_button(tools, "Fingerprint", lambda: self.open_fingerprint_panel([profile])).pack(side=tk.LEFT, padx=(0, 8))
        self._bar_button(tools, "Cookies", lambda: self.open_cookie_panel([profile])).pack(side=tk.LEFT, padx=8)
        self._bar_button(tools, "Рандом proxy", lambda: self.assign_random_proxy_to_profiles([profile])).pack(side=tk.LEFT, padx=8)

        tk.Label(right, text="СВОДКА", bg="#292929", fg="#ffffff", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 14))
        for label, value in (
            ("ID", profile.id[:10]),
            ("Название", profile.name),
            ("Прокси", proxy_label(profile)),
            ("Порт", profile.local_port or "авто"),
            ("UserAgent", profile.fingerprint.user_agent[:48] + "..."),
            ("Timezone", profile.fingerprint.timezone),
            ("Canvas", "noise" if profile.fingerprint.canvas_noise else "real"),
            ("WebRTC", "protected" if profile.fingerprint.webrtc_protection else "default"),
        ):
            tk.Label(right, text=label, bg="#292929", fg="#ffffff", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 0))
            tk.Label(right, text=value, bg="#292929", fg="#bdbdbd", wraplength=260, justify=tk.LEFT).pack(anchor="w")

        footer = tk.Frame(panel, bg="#1b1b1b", pady=12)
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        def save() -> None:
            profile.name = vars_["name"].get().strip() or "Без имени"
            profile.start_url = vars_["start_url"].get().strip()
            profile.browser_path = vars_["browser_path"].get().strip()
            profile.local_port = vars_["local_port"].get().strip()
            profile.notes = vars_["notes"].get().strip()
            profile.proxy_type = vars_["proxy_type"].get().strip() or "http"
            profile.proxy_host = vars_["proxy_host"].get().strip()
            profile.proxy_port = vars_["proxy_port"].get().strip()
            profile.proxy_login = vars_["proxy_login"].get().strip()
            profile.proxy_password = vars_["proxy_password"].get()
            self.store.save()
            self.refresh_profile_table()
            self.enqueue_log(f"[{now_label()}] Профиль сохранён: {profile.name}")
            win.destroy()

        self._bar_button(footer, "Отмена", win.destroy).pack(side=tk.RIGHT, padx=8)
        self._bar_button(footer, "✓ Сохранить", save).pack(side=tk.RIGHT, padx=8)

    def open_fingerprint_panel(self, profiles: list[BrowserProfile] | None = None) -> None:
        targets = profiles or self.action_profiles()
        if not targets:
            targets = self.profiles
        if not targets:
            messagebox.showinfo("Нет профилей", "Создайте профиль перед настройкой fingerprint.", parent=self)
            return
        first = targets[0]
        fp = first.fingerprint
        win, panel = self._modal("Fingerprint настройки", 820, 610)
        tk.Label(panel, text=f"Fingerprint: {len(targets)} проф.", bg="#1f1f1f", fg="#ffffff", font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 16))
        form = tk.Frame(panel, bg="#1f1f1f")
        form.pack(fill=tk.BOTH, expand=True)
        vars_: dict[str, tk.Variable] = {
            "user_agent": tk.StringVar(value=fp.user_agent),
            "platform": tk.StringVar(value=fp.platform),
            "locale": tk.StringVar(value=fp.locale),
            "timezone": tk.StringVar(value=fp.timezone),
            "screen_width": tk.StringVar(value=str(fp.screen_width)),
            "screen_height": tk.StringVar(value=str(fp.screen_height)),
            "viewport_width": tk.StringVar(value=str(fp.viewport_width)),
            "viewport_height": tk.StringVar(value=str(fp.viewport_height)),
            "hardware_concurrency": tk.StringVar(value=str(fp.hardware_concurrency)),
            "device_memory": tk.StringVar(value=str(fp.device_memory)),
            "max_touch_points": tk.StringVar(value=str(fp.max_touch_points)),
            "webgl_vendor": tk.StringVar(value=fp.webgl_vendor),
            "webgl_renderer": tk.StringVar(value=fp.webgl_renderer),
            "canvas_noise": tk.BooleanVar(value=fp.canvas_noise),
            "webrtc_protection": tk.BooleanVar(value=fp.webrtc_protection),
        }
        self._field(form, "User-Agent", vars_["user_agent"], 0, 0, colspan=5)
        self._field(form, "Platform", vars_["platform"], 1, 0, width=14)
        self._field(form, "Locale", vars_["locale"], 1, 2, width=14)
        self._field(form, "Timezone", vars_["timezone"], 1, 4, width=18)
        self._field(form, "Screen W", vars_["screen_width"], 2, 0, width=10)
        self._field(form, "Screen H", vars_["screen_height"], 2, 2, width=10)
        self._field(form, "Viewport W", vars_["viewport_width"], 2, 4, width=10)
        self._field(form, "Viewport H", vars_["viewport_height"], 3, 0, width=10)
        self._field(form, "CPU", vars_["hardware_concurrency"], 3, 2, width=10)
        self._field(form, "RAM", vars_["device_memory"], 3, 4, width=10)
        self._field(form, "Touch", vars_["max_touch_points"], 4, 0, width=10)
        self._field(form, "WebGL vendor", vars_["webgl_vendor"], 4, 2, width=22)
        self._field(form, "WebGL renderer", vars_["webgl_renderer"], 5, 0, colspan=5)
        tk.Checkbutton(form, text="Canvas noise", variable=vars_["canvas_noise"], bg="#1f1f1f", fg="#ffffff", selectcolor="#242424", activebackground="#1f1f1f").grid(row=6, column=0, columnspan=2, sticky="w", pady=12)
        tk.Checkbutton(form, text="WebRTC protection", variable=vars_["webrtc_protection"], bg="#1f1f1f", fg="#ffffff", selectcolor="#242424", activebackground="#1f1f1f").grid(row=6, column=2, columnspan=2, sticky="w", pady=12)

        def read_fp() -> Fingerprint:
            return Fingerprint(
                user_agent=vars_["user_agent"].get().strip() or DEFAULT_USER_AGENTS[0],
                platform=vars_["platform"].get().strip() or "Win32",
                locale=vars_["locale"].get().strip() or "ru-RU",
                timezone=vars_["timezone"].get().strip() or "Europe/Moscow",
                screen_width=int_or_default(vars_["screen_width"].get(), 1366),
                screen_height=int_or_default(vars_["screen_height"].get(), 768),
                viewport_width=int_or_default(vars_["viewport_width"].get(), 1280),
                viewport_height=int_or_default(vars_["viewport_height"].get(), 720),
                hardware_concurrency=int_or_default(vars_["hardware_concurrency"].get(), 8),
                device_memory=int_or_default(vars_["device_memory"].get(), 8),
                max_touch_points=int_or_default(vars_["max_touch_points"].get(), 0),
                webgl_vendor=vars_["webgl_vendor"].get().strip() or DEFAULT_WEBGL[0][0],
                webgl_renderer=vars_["webgl_renderer"].get().strip() or DEFAULT_WEBGL[0][1],
                canvas_noise=bool(vars_["canvas_noise"].get()),
                webrtc_protection=bool(vars_["webrtc_protection"].get()),
            )

        def save_to(target_list: list[BrowserProfile]) -> None:
            fp_value = read_fp()
            for profile in target_list:
                profile.fingerprint = Fingerprint(**asdict(fp_value))
            self.store.save()
            self.refresh_profile_table()
            self.enqueue_log(f"[{now_label()}] Fingerprint применён: {len(target_list)} профилей")
            win.destroy()

        def randomize(target_list: list[BrowserProfile]) -> None:
            for profile in target_list:
                profile.fingerprint = self.random_fingerprint()
            self.store.save()
            self.refresh_profile_table()
            self.enqueue_log(f"[{now_label()}] Fingerprint рандомизирован: {len(target_list)} профилей")
            win.destroy()

        footer = tk.Frame(panel, bg="#1f1f1f")
        footer.pack(fill=tk.X, pady=(12, 0))
        self._bar_button(footer, "Сохранить выбранным", lambda: save_to(targets)).pack(side=tk.LEFT, padx=(0, 8))
        self._bar_button(footer, "Сохранить всем", lambda: save_to(self.profiles)).pack(side=tk.LEFT, padx=8)
        self._bar_button(footer, "Рандом выбранным", lambda: randomize(targets)).pack(side=tk.LEFT, padx=8)
        self._bar_button(footer, "Рандом всем", lambda: randomize(self.profiles)).pack(side=tk.LEFT, padx=8)
        self._bar_button(footer, "Закрыть", win.destroy).pack(side=tk.RIGHT)

    def open_cookie_panel(self, profiles: list[BrowserProfile] | None = None) -> None:
        targets = profiles or self.action_profiles()
        if not targets:
            return
        win, panel = self._modal("Cookies профилей", 620, 360)
        tk.Label(panel, text=f"Cookies: {len(targets)} проф.", bg="#1f1f1f", fg="#ffffff", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        tk.Label(panel, text="Импорт применяет один JSON-файл cookies ко всем выбранным профилям.", bg="#1f1f1f", fg="#a8a8a8").pack(anchor="w", pady=(6, 16))
        list_box = tk.Listbox(panel, bg="#242424", fg="#ffffff", relief=tk.FLAT, height=6)
        list_box.pack(fill=tk.BOTH, expand=True)
        for profile in targets:
            list_box.insert(tk.END, f"{profile.name}  →  {cookie_file(profile.id)}")

        def import_cookies() -> None:
            path = filedialog.askopenfilename(parent=win, title="Импорт cookies JSON", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                cookies = raw.get("cookies", raw) if isinstance(raw, dict) else raw
                if not isinstance(cookies, list):
                    raise ValueError("Файл должен содержать массив cookies или storage_state")
                for profile in targets:
                    target = cookie_file(profile.id)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("w", encoding="utf-8") as fh:
                        json.dump(cookies, fh, ensure_ascii=False, indent=2)
                self.enqueue_log(f"[{now_label()}] Cookies импортированы: {len(targets)} профилей")
            except Exception as exc:
                messagebox.showerror("Ошибка импорта", str(exc), parent=win)

        def export_one() -> None:
            if len(targets) != 1:
                messagebox.showinfo("Экспорт", "Экспорт доступен для одного выбранного профиля.", parent=win)
                return
            profile = targets[0]
            source = self.runtime.export_cookies(profile.id) if self.runtime.is_running(profile.id) else cookie_file(profile.id)
            save_path = filedialog.asksaveasfilename(parent=win, title="Сохранить cookies", defaultextension=".json", initialfile=f"{profile.name}_cookies.json")
            if not save_path:
                return
            if source.exists():
                shutil.copyfile(source, save_path)
            else:
                with open(save_path, "w", encoding="utf-8") as fh:
                    json.dump([], fh, ensure_ascii=False, indent=2)
            self.enqueue_log(f"[{now_label()}] Cookies экспортированы: {save_path}")

        footer = tk.Frame(panel, bg="#1f1f1f")
        footer.pack(fill=tk.X, pady=(14, 0))
        self._bar_button(footer, "Импорт выбранным", import_cookies).pack(side=tk.LEFT, padx=(0, 8))
        self._bar_button(footer, "Экспорт одного", export_one).pack(side=tk.LEFT, padx=8)
        self._bar_button(footer, "Папка cookies", lambda: os.startfile(profile_dir(targets[0].id))).pack(side=tk.LEFT, padx=8)  # type: ignore[attr-defined]
        self._bar_button(footer, "Закрыть", win.destroy).pack(side=tk.RIGHT)

    def random_fingerprint(self) -> Fingerprint:
        ua = random.choice(DEFAULT_USER_AGENTS)
        platform = "MacIntel" if "Macintosh" in ua else "Linux x86_64" if "X11" in ua else "Win32"
        screen = random.choice([(1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)])
        webgl_vendor, webgl_renderer = random.choice(DEFAULT_WEBGL)
        return Fingerprint(
            user_agent=ua,
            platform=platform,
            locale=random.choice(["ru-RU", "en-US", "en-GB", "de-DE", "pl-PL"]),
            timezone=random.choice(DEFAULT_TIMEZONES),
            screen_width=screen[0],
            screen_height=screen[1],
            viewport_width=max(800, screen[0] - random.choice([0, 80, 120])),
            viewport_height=max(600, screen[1] - random.choice([40, 80, 120])),
            hardware_concurrency=random.choice([4, 6, 8, 12, 16]),
            device_memory=random.choice([4, 8, 16]),
            max_touch_points=random.choice([0, 0, 0, 1, 5]),
            webgl_vendor=webgl_vendor,
            webgl_renderer=webgl_renderer,
            canvas_noise=True,
            webrtc_protection=True,
        )

    def _modal(self, title: str, width: int, height: int) -> tuple[tk.Toplevel, tk.Frame]:
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg="#0f0f0f")
        win.transient(self)
        win.lift()
        self.update_idletasks()
        x = self.winfo_rootx() + max(24, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(24, (self.winfo_height() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")
        shadow = tk.Frame(win, bg="#0b0b0b")
        shadow.place(x=8, y=8, relwidth=1, relheight=1, width=-8, height=-8)
        panel = tk.Frame(win, bg="#1f1f1f", padx=24, pady=22, highlightbackground="#343434", highlightthickness=1)
        panel.place(x=0, y=0, relwidth=1, relheight=1, width=-8, height=-8)
        return win, panel

    def _field(self, parent: tk.Widget, label: str, variable: tk.Variable, row: int, column: int, width: int | None = None, colspan: int = 1, show: str | None = None) -> tk.Entry:
        tk.Label(parent, text=label, bg="#1f1f1f", fg="#9d9d9d").grid(row=row, column=column, sticky="w", pady=6)
        entry = tk.Entry(parent, textvariable=variable, width=width, show=show, relief=tk.FLAT, bg="#242424", fg="#ffffff", insertbackground="#ffffff")
        entry.grid(row=row, column=column + 1, columnspan=colspan, sticky="ew", padx=(8, 12), pady=6, ipady=5)
        return entry

    def _browse_to_var(self, variable: tk.Variable, parent: tk.Widget) -> None:
        path = filedialog.askopenfilename(parent=parent, title="Выберите chrome.exe / msedge.exe", filetypes=[("Browser executable", "*.exe"), ("All files", "*.*")])
        if path:
            variable.set(path)

    def _profile_proxy_line(self, profile: BrowserProfile) -> str:
        if not profile.proxy_host.strip() or not profile.proxy_port.strip():
            return ""
        auth = ""
        if profile.proxy_login.strip():
            auth = f"{profile.proxy_login}:{profile.proxy_password}@"
        return f"{profile.proxy_type}://{auth}{profile.proxy_host}:{profile.proxy_port}"

    def enqueue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        changed = False
        while True:
            try:
                message = self.log_queue.get_nowait()
            except Empty:
                break
            self._append_log(message)
            changed = True
        if changed:
            self.refresh_profile_table()
        self.after(150, self._drain_log_queue)

    def _append_log(self, message: str) -> None:
        if self.log_text is not None:
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
        self.status_var.set(re.sub(r"\s+", " ", message)[-140:])

    def on_close(self) -> None:
        self.store.save()
        self.status_var.set("Останавливаю браузеры...")
        self.update_idletasks()
        self.runtime.stop_all_sync()
        self.destroy()


def label_entry(
    parent: ttk.Frame,
    label: str,
    variable: tk.Variable,
    row: int,
    column: int,
    width: int | None = None,
    columnspan: int = 1,
    show: str | None = None,
) -> tk.Entry:
    ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", pady=3)
    entry = tk.Entry(parent, textvariable=variable, width=width, show=show)
    entry.grid(row=row, column=column + 1, columnspan=columnspan, sticky="ew", padx=(8, 10), pady=3)
    return entry


def main() -> int:
    ensure_data_dirs()
    app = ManagerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
