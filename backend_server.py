from __future__ import annotations

import argparse
import json
import random
import socket
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

import browser_manager as core


DEFAULT_FOLDER = getattr(core, "DEFAULT_FOLDER", "BrowserManager")
ALL_FOLDERS = "Все профили"
FOLDERS_FILE = getattr(core, "FOLDERS_FILE", core.DATA_DIR / "folders.json")
PROXY_CHECK_TARGETS = (
    ("ipify", "https://api.ipify.org?format=json"),
    ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
    ("google", "https://www.google.com/generate_204"),
    ("browserleaks", "https://browserleaks.com/ip"),
)
SOCKS_CHECK_TARGETS = (
    ("google", "www.google.com", 443),
    ("cloudflare", "www.cloudflare.com", 443),
    ("ipify", "api.ipify.org", 443),
    ("browserleaks", "browserleaks.com", 443),
)


def normalize_folder(name: Any) -> str:
    value = str(name or "").strip()
    return value or DEFAULT_FOLDER


class BackendState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.store = core.ProfileStore(core.PROFILES_FILE)
        self.profiles = self.store.load()
        self.folders = self.load_folders()
        self.proxy_checks: dict[str, dict[str, Any]] = {}
        self.logs: list[dict[str, Any]] = []
        self.runtime = core.BrowserRuntime(self.add_log)
        self.started_at = time.time()
        self.closed = False

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(
                {
                    "index": len(self.logs),
                    "time": core.now_label(),
                    "message": message,
                }
            )
            if len(self.logs) > 1000:
                self.logs = self.logs[-1000:]
                for index, item in enumerate(self.logs):
                    item["index"] = index

    def refresh(self) -> None:
        self.profiles = self.store.profiles

    def profile_by_id(self, profile_id: str) -> core.BrowserProfile | None:
        return self.store.get(profile_id)

    def selected_profiles(self, ids: list[str] | None) -> list[core.BrowserProfile]:
        with self.lock:
            if ids is None:
                return list(self.store.profiles)
            found = [self.store.get(profile_id) for profile_id in ids]
            return [profile for profile in found if profile is not None]

    def profile_payload(self, profile: core.BrowserProfile) -> dict[str, Any]:
        payload = profile.to_dict()
        folder = normalize_folder(getattr(profile, "folder", DEFAULT_FOLDER))
        payload["folder"] = folder
        check = self.proxy_checks.get(profile.id, {})
        payload.update(
            {
                "running": self.runtime.is_running(profile.id),
                "status": "Запущен" if self.runtime.is_running(profile.id) else "Остановлен",
                "proxy_label": core.proxy_label(profile),
                "proxy_check_label": check.get("label", ""),
                "proxy_check_state": check.get("state", ""),
                "proxy_check_detail": check.get("detail", ""),
                "cookies_path": str(core.cookie_file(profile.id)),
                "tabs_path": str(core.tabs_file(profile.id)),
                "cookies_saved": core.cookie_file(profile.id).exists(),
                "tabs_saved": core.tabs_file(profile.id).exists(),
            }
        )
        return payload

    def random_fingerprint(self) -> core.Fingerprint:
        return core.ManagerApp.random_fingerprint(None)

    def load_proxy_pool(self) -> list[str]:
        if not core.PROXY_POOL_FILE.exists():
            return []
        return [
            line.strip()
            for line in core.PROXY_POOL_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def save_proxy_pool(self, lines: list[str]) -> None:
        core.ensure_data_dirs()
        core.PROXY_POOL_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def load_folders(self) -> list[str]:
        core.ensure_data_dirs()
        names = {DEFAULT_FOLDER}
        if FOLDERS_FILE.exists():
            try:
                raw = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    names.update(normalize_folder(item) for item in raw if str(item or "").strip())
            except Exception:
                pass
        names.update(normalize_folder(getattr(profile, "folder", DEFAULT_FOLDER)) for profile in self.store.profiles)
        return sorted(names, key=str.casefold)

    def save_folders(self) -> None:
        core.ensure_data_dirs()
        FOLDERS_FILE.write_text(json.dumps(self.folders, ensure_ascii=False, indent=2), encoding="utf-8")

    def create_folder(self, name: str) -> str:
        folder = normalize_folder(name)
        with self.lock:
            current = {item.casefold(): item for item in self.folders}
            if folder.casefold() not in current:
                self.folders.append(folder)
                self.folders = sorted(self.folders, key=str.casefold)
                self.save_folders()
            return current.get(folder.casefold(), folder)

    def shutdown(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.runtime.stop_all_sync()
        self.store.save()


STATE = BackendState()


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def check_profile_proxy(profile: core.BrowserProfile) -> dict[str, Any]:
    if not profile.proxy_host.strip() or not profile.proxy_port.strip():
        return {
            "state": "none",
            "label": "нет прокси",
            "passed": 0,
            "blocked": len(PROXY_CHECK_TARGETS),
            "total": len(PROXY_CHECK_TARGETS),
            "latency_ms": 0,
            "detail": "Прокси не задан",
        }

    started = time.monotonic()
    if profile.proxy_type.lower().strip() == "socks5":
        passed, errors = check_socks5_proxy(profile)
        total = len(SOCKS_CHECK_TARGETS)
    else:
        passed, errors = check_http_proxy(profile)
        total = len(PROXY_CHECK_TARGETS)
    blocked = max(total - passed, 0)
    latency_ms = int((time.monotonic() - started) * 1000)
    state = "ok" if blocked == 0 else ("blocked" if passed else "fail")
    detail = "OK" if not errors else "; ".join(errors[:3])
    return {
        "state": state,
        "label": f"{blocked}/{total}",
        "passed": passed,
        "blocked": blocked,
        "total": total,
        "latency_ms": latency_ms,
        "detail": detail,
    }


def check_http_proxy(profile: core.BrowserProfile) -> tuple[int, list[str]]:
    proxy_url = proxy_url_for_check(profile)
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    passed = 0
    errors: list[str] = []
    for name, url in PROXY_CHECK_TARGETS:
        try:
            request = Request(url, headers={"User-Agent": profile.fingerprint.user_agent})
            with opener.open(request, timeout=8) as response:
                if 200 <= int(getattr(response, "status", 0)) < 400:
                    response.read(128)
                    passed += 1
                else:
                    errors.append(f"{name}: HTTP {getattr(response, 'status', 0)}")
        except HTTPError as exc:
            errors.append(f"{name}: HTTP {exc.code}")
        except URLError as exc:
            errors.append(f"{name}: {exc.reason}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    return passed, errors


def proxy_url_for_check(profile: core.BrowserProfile) -> str:
    auth = ""
    if profile.proxy_login.strip():
        auth = f"{quote(profile.proxy_login.strip(), safe='')}:{quote(profile.proxy_password, safe='')}@"
    return f"http://{auth}{profile.proxy_host.strip()}:{profile.proxy_port.strip()}"


def check_socks5_proxy(profile: core.BrowserProfile) -> tuple[int, list[str]]:
    passed = 0
    errors: list[str] = []
    for name, host, port in SOCKS_CHECK_TARGETS:
        try:
            socks5_connect(profile, host, port)
            passed += 1
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return passed, errors


def socks5_connect(profile: core.BrowserProfile, target_host: str, target_port: int) -> None:
    proxy_port = int(profile.proxy_port.strip())
    with socket.create_connection((profile.proxy_host.strip(), proxy_port), timeout=8) as sock:
        sock.settimeout(8)
        methods = [0, 2] if profile.proxy_login.strip() else [0]
        sock.sendall(bytes([5, len(methods), *methods]))
        version, method = recv_exact(sock, 2)
        if version != 5 or method == 255:
            raise RuntimeError("SOCKS5 auth failed")
        if method == 2:
            username = profile.proxy_login.strip().encode("utf-8")
            password = profile.proxy_password.encode("utf-8")
            if len(username) > 255 or len(password) > 255:
                raise RuntimeError("SOCKS5 auth is too long")
            sock.sendall(bytes([1, len(username)]) + username + bytes([len(password)]) + password)
            auth_version, auth_status = recv_exact(sock, 2)
            if auth_version != 1 or auth_status != 0:
                raise RuntimeError("SOCKS5 auth rejected")
        elif method != 0:
            raise RuntimeError("SOCKS5 method unsupported")

        host_bytes = target_host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + int(target_port).to_bytes(2, "big"))
        header = recv_exact(sock, 4)
        if header[0] != 5 or header[1] != 0:
            raise RuntimeError(f"SOCKS5 connect {header[1]}")
        atyp = header[3]
        if atyp == 1:
            recv_exact(sock, 4)
        elif atyp == 3:
            recv_exact(sock, recv_exact(sock, 1)[0])
        elif atyp == 4:
            recv_exact(sock, 16)
        recv_exact(sock, 2)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("connection closed")
        data += chunk
    return data


class BrowserManagerHandler(BaseHTTPRequestHandler):
    server_version = "BrowserManagerBackend/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path, query = self._path_query()
            if path == "/api/health":
                self._json(
                    {
                        "ok": True,
                        "name": "BrowserManager Backend",
                        "uptime": round(time.time() - STATE.started_at, 2),
                        "profiles": len(STATE.store.profiles),
                    }
                )
                return
            if path == "/api/profiles":
                with STATE.lock:
                    folder = normalize_folder((query.get("folder") or [""])[0])
                    profiles = list(STATE.store.profiles)
                    if folder != ALL_FOLDERS and (query.get("folder") or [""])[0].strip():
                        profiles = [
                            profile
                            for profile in profiles
                            if normalize_folder(getattr(profile, "folder", DEFAULT_FOLDER)) == folder
                        ]
                    self._json({"profiles": [STATE.profile_payload(profile) for profile in profiles]})
                return
            if path == "/api/folders":
                with STATE.lock:
                    STATE.folders = STATE.load_folders()
                    self._json({"folders": STATE.folders})
                return
            if path == "/api/logs":
                since = int((query.get("since") or ["0"])[0])
                with STATE.lock:
                    self._json({"logs": [item for item in STATE.logs if item["index"] >= since]})
                return
            if path == "/api/proxies":
                self._json({"proxies": STATE.load_proxy_pool()})
                return
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {path}")
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        try:
            path, _query = self._path_query()
            body = self._read_json()
            if path == "/api/profiles":
                with STATE.lock:
                    profile = STATE.store.make_default_profile()
                    profile.name = str(body.get("name") or profile.name)
                    profile.folder = STATE.create_folder(str(body.get("folder") or DEFAULT_FOLDER))
                    STATE.store.add(profile)
                    STATE.refresh()
                    STATE.add_log(f"[{core.now_label()}] Профиль создан: {profile.name}")
                    self._json({"profile": STATE.profile_payload(profile)})
                return
            if path == "/api/folders":
                folder = STATE.create_folder(str(body.get("name") or ""))
                STATE.add_log(f"[{core.now_label()}] Папка создана: {folder}")
                self._json({"ok": True, "folder": folder, "folders": STATE.folders})
                return
            if path == "/api/profiles/update":
                self._update_profile(body)
                return
            if path == "/api/profiles/duplicate":
                self._duplicate_profiles(body)
                return
            if path == "/api/profiles/delete":
                self._delete_profiles(body)
                return
            if path == "/api/profiles/start":
                self._start_profiles(body)
                return
            if path == "/api/profiles/stop":
                self._stop_profiles(body)
                return
            if path == "/api/fingerprint/randomize":
                self._randomize_fingerprints(body)
                return
            if path == "/api/proxy/random-assign":
                self._assign_random_proxy(body)
                return
            if path == "/api/proxy/check":
                self._check_proxies(body)
                return
            if path == "/api/proxies":
                lines = body.get("proxies") or []
                if isinstance(lines, str):
                    lines = lines.splitlines()
                if not isinstance(lines, list):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "proxies must be a list or text")
                STATE.save_proxy_pool([str(line).strip() for line in lines if str(line).strip()])
                STATE.add_log(f"[{core.now_label()}] Proxy pool сохранён: {len(lines)} строк")
                self._json({"ok": True, "proxies": STATE.load_proxy_pool()})
                return
            if path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self._shutdown_server, daemon=True).start()
                return
            raise ApiError(HTTPStatus.NOT_FOUND, f"Unknown endpoint: {path}")
        except Exception as exc:
            self._handle_error(exc)

    def _update_profile(self, body: dict[str, Any]) -> None:
        profile_id = str(body.get("id") or "")
        with STATE.lock:
            profile = STATE.profile_by_id(profile_id)
            if not profile:
                raise ApiError(HTTPStatus.NOT_FOUND, "Profile not found")
            for key in (
                "name",
                "proxy_type",
                "proxy_host",
                "proxy_port",
                "proxy_login",
                "proxy_password",
                "start_url",
                "browser_path",
                "local_port",
                "notes",
                "folder",
            ):
                if key in body:
                    value = str(body.get(key) or "")
                    if key == "folder":
                        value = STATE.create_folder(value)
                    setattr(profile, key, value)
            if isinstance(body.get("fingerprint"), dict):
                profile.fingerprint = core.Fingerprint(
                    **{
                        key: value
                        for key, value in body["fingerprint"].items()
                        if key in core.Fingerprint.__dataclass_fields__
                    }
                )
            STATE.store.save()
            STATE.add_log(f"[{core.now_label()}] Профиль сохранён: {profile.name}")
            self._json({"profile": STATE.profile_payload(profile)})

    def _duplicate_profiles(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        created: list[core.BrowserProfile] = []
        with STATE.lock:
            for profile in targets:
                clone = core.BrowserProfile.from_dict(profile.to_dict())
                clone.id = uuid.uuid4().hex
                clone.name = f"{profile.name} copy"
                core.profile_dir(clone.id).mkdir(parents=True, exist_ok=True)
                STATE.store.add(clone)
                created.append(clone)
            STATE.refresh()
            STATE.add_log(f"[{core.now_label()}] Дублировано профилей: {len(created)}")
            self._json({"profiles": [STATE.profile_payload(profile) for profile in created]})

    def _delete_profiles(self, body: dict[str, Any]) -> None:
        ids = self._ids(body)
        with STATE.lock:
            for profile_id in ids:
                STATE.runtime.stop(profile_id)
                STATE.store.remove(profile_id, remove_files=True)
            STATE.refresh()
            STATE.add_log(f"[{core.now_label()}] Удалено профилей: {len(ids)}")
            self._json({"ok": True, "deleted": ids})

    def _start_profiles(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        for profile in targets:
            STATE.runtime.start(profile)
        STATE.add_log(f"[{core.now_label()}] Команда запуска: {len(targets)} проф.")
        self._json({"ok": True, "profiles": [STATE.profile_payload(profile) for profile in targets]})

    def _stop_profiles(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        for profile in targets:
            STATE.runtime.stop(profile.id)
        STATE.add_log(f"[{core.now_label()}] Команда остановки: {len(targets)} проф.")
        self._json({"ok": True})

    def _randomize_fingerprints(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        with STATE.lock:
            for profile in targets:
                profile.fingerprint = STATE.random_fingerprint()
            STATE.store.save()
        STATE.add_log(f"[{core.now_label()}] Fingerprint рандомизирован: {len(targets)} проф.")
        self._json({"ok": True, "profiles": [STATE.profile_payload(profile) for profile in targets]})

    def _assign_random_proxy(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        pool = STATE.load_proxy_pool()
        valid = [line for line in pool if core.split_proxy(line)[1] and core.split_proxy(line)[2]]
        if not valid:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Proxy pool is empty")
        with STATE.lock:
            for profile in targets:
                core.proxy_line_to_profile(profile, random.choice(valid))
            STATE.store.save()
        STATE.add_log(f"[{core.now_label()}] Случайные proxy назначены: {len(targets)} проф.")
        self._json({"ok": True, "profiles": [STATE.profile_payload(profile) for profile in targets]})

    def _check_proxies(self, body: dict[str, Any]) -> None:
        targets = STATE.selected_profiles(self._ids(body))
        results: list[dict[str, Any]] = []
        for profile in targets:
            result = check_profile_proxy(profile)
            with STATE.lock:
                STATE.proxy_checks[profile.id] = result
            results.append({"id": profile.id, **result})
            STATE.add_log(
                f"[{core.now_label()}] Проверка proxy {profile.name}: {result['label']} {result.get('detail', '')}".strip()
            )
        self._json({"ok": True, "results": results, "profiles": [STATE.profile_payload(profile) for profile in targets]})

    def _ids(self, body: dict[str, Any]) -> list[str]:
        if body.get("all") is True:
            return [profile.id for profile in STATE.store.profiles]
        ids = body.get("ids") or []
        if isinstance(ids, str):
            return [ids]
        if not isinstance(ids, list):
            return []
        return [str(item) for item in ids if str(item).strip()]

    def _path_query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return unquote(parsed.path), parse_qs(parsed.query)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON object expected")
        return data

    def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._json({"ok": False, "error": exc.message}, status=exc.status)
            return
        self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _shutdown_server(self) -> None:
        STATE.shutdown()
        self.server.shutdown()

    def log_message(self, format: str, *args: Any) -> None:
        STATE.add_log(f"[{core.now_label()}] API: {format % args}")


class BrowserManagerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="BrowserManager local backend API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = BrowserManagerHTTPServer((args.host, args.port), BrowserManagerHandler)
    print(f"BrowserManager backend listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        STATE.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
