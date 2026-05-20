from __future__ import annotations

import argparse
import ipaddress
import json
import random
import socket
import ssl
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import browser_manager as core


DEFAULT_FOLDER = getattr(core, "DEFAULT_FOLDER", "BrowserManager")
ALL_FOLDERS = "Все профили"
FOLDERS_FILE = getattr(core, "FOLDERS_FILE", core.DATA_DIR / "folders.json")
PROXY_IP_TARGETS = (
    ("ipify", "https://api.ipify.org?format=json"),
    ("icanhazip", "https://icanhazip.com"),
)
REPUTATION_SERVICE_COUNT = 3


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
            "blocked": 0,
            "total": REPUTATION_SERVICE_COUNT,
            "latency_ms": 0,
            "detail": "Прокси не задан",
        }

    started = time.monotonic()
    try:
        ip = resolve_proxy_ip(profile)
    except Exception as exc:
        return {
            "state": "fail",
            "label": "ошибка",
            "passed": 0,
            "blocked": 0,
            "total": REPUTATION_SERVICE_COUNT,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": f"Прокси не отвечает: {exc}",
        }

    reputation = check_ip_reputation(ip)
    latency_ms = int((time.monotonic() - started) * 1000)
    state = reputation["state"]
    label = reputation["label"]
    detail = f"IP {ip}; {reputation['detail']}"
    return {
        "state": state,
        "label": label,
        "passed": reputation["checked"],
        "blocked": reputation["signals"],
        "total": REPUTATION_SERVICE_COUNT,
        "latency_ms": latency_ms,
        "detail": detail,
    }


def resolve_proxy_ip(profile: core.BrowserProfile) -> str:
    if profile.proxy_type.lower().strip() == "socks5":
        return resolve_socks5_proxy_ip(profile)
    return resolve_http_proxy_ip(profile)


def resolve_http_proxy_ip(profile: core.BrowserProfile) -> str:
    proxy_url = proxy_url_for_check(profile)
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    errors: list[str] = []
    for name, url in PROXY_IP_TARGETS:
        try:
            request = Request(url, headers={"User-Agent": profile.fingerprint.user_agent})
            with opener.open(request, timeout=8) as response:
                raw = response.read(4096).decode("utf-8", errors="replace")
                return parse_ip_response(raw)
        except HTTPError as exc:
            errors.append(f"{name}: HTTP {exc.code}")
        except URLError as exc:
            errors.append(f"{name}: {exc.reason}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("; ".join(errors[:2]) or "IP lookup failed")


def proxy_url_for_check(profile: core.BrowserProfile) -> str:
    auth = ""
    if profile.proxy_login.strip():
        auth = f"{quote(profile.proxy_login.strip(), safe='')}:{quote(profile.proxy_password, safe='')}@"
    return f"http://{auth}{profile.proxy_host.strip()}:{profile.proxy_port.strip()}"


def resolve_socks5_proxy_ip(profile: core.BrowserProfile) -> str:
    errors: list[str] = []
    try:
        raw = https_get_via_socks5(profile, "api.ipify.org", "/?format=json")
        return parse_ip_response(raw)
    except Exception as exc:
        errors.append(f"ipify: {exc}")
    raise RuntimeError("; ".join(errors))


def parse_ip_response(raw: str) -> str:
    text = raw.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            text = str(parsed.get("ip") or parsed.get("query") or "").strip()
    except Exception:
        text = text.splitlines()[0].strip() if text else ""
    ipaddress.ip_address(text)
    return text


def check_ip_reputation(ip: str) -> dict[str, Any]:
    checked = 0
    signals: list[str] = []
    risks: list[int] = []
    errors: list[str] = []

    try:
        data = fetch_json(f"https://proxycheck.io/v2/{quote(ip)}?vpn=1&asn=1&risk=1")
        info = data.get(ip, {}) if isinstance(data, dict) else {}
        if info:
            checked += 1
            if str(info.get("proxy", "")).lower() == "yes":
                signals.append(str(info.get("type") or "proxy").lower())
            risk = int(str(info.get("risk") or "0"))
            risks.append(risk)
    except Exception as exc:
        errors.append(f"proxycheck: {exc}")

    try:
        data = fetch_json(f"https://ipwho.is/{quote(ip)}?security=1")
        if data.get("success", True) is not False:
            checked += 1
            security = data.get("security") if isinstance(data.get("security"), dict) else {}
            for key in ("proxy", "vpn", "tor", "hosting"):
                if security.get(key) is True:
                    signals.append(key)
    except Exception as exc:
        errors.append(f"ipwho.is: {exc}")

    try:
        data = fetch_json(
            f"http://ip-api.com/json/{quote(ip)}?fields=status,message,query,proxy,hosting,mobile,isp,as,country"
        )
        if data.get("status") == "success":
            checked += 1
            if data.get("proxy") is True:
                signals.append("proxy")
            if data.get("hosting") is True:
                signals.append("hosting")
    except Exception as exc:
        errors.append(f"ip-api: {exc}")

    unique_signals = sorted({signal for signal in signals if signal})
    risk = max(risks or [0])
    if unique_signals or risk >= 65:
        label = f"risk {risk}" if risk else ",".join(unique_signals[:2])
        state = "blocked"
    elif checked > 0:
        label = "OK"
        state = "ok"
    else:
        label = "IP OK"
        state = "unknown"
    detail = ", ".join(unique_signals) if unique_signals else "чисто"
    if checked == 0 and errors:
        detail = "репутация не проверена: " + "; ".join(errors[:2])
    return {"state": state, "label": label, "checked": checked, "signals": len(unique_signals), "risk": risk, "detail": detail}


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "BrowserManager/1.0"})
    with urlopen(request, timeout=8) as response:
        raw = response.read(16384).decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def https_get_via_socks5(profile: core.BrowserProfile, target_host: str, path: str) -> str:
    raw = b""
    with open_socks5_tunnel(profile, target_host, 443) as sock:
        context = ssl.create_default_context()
        with context.wrap_socket(sock, server_hostname=target_host) as tls:
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {target_host}\r\n"
                "User-Agent: BrowserManager/1.0\r\n"
                "Accept: application/json,text/plain\r\n"
                "Connection: close\r\n\r\n"
            )
            tls.sendall(request.encode("ascii"))
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                raw += chunk
    headers, _, body = raw.partition(b"\r\n\r\n")
    if b"transfer-encoding: chunked" in headers.lower():
        body = decode_chunked(body)
    return body.decode("utf-8", errors="replace")


def decode_chunked(body: bytes) -> bytes:
    decoded = b""
    cursor = 0
    while cursor < len(body):
        line_end = body.find(b"\r\n", cursor)
        if line_end == -1:
            break
        size_raw = body[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_raw, 16)
        except ValueError:
            break
        cursor = line_end + 2
        if size == 0:
            break
        decoded += body[cursor:cursor + size]
        cursor += size + 2
    return decoded or body


def open_socks5_tunnel(profile: core.BrowserProfile, target_host: str, target_port: int) -> socket.socket:
    proxy_port = int(profile.proxy_port.strip())
    sock = socket.create_connection((profile.proxy_host.strip(), proxy_port), timeout=8)
    try:
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
        return sock
    except Exception:
        sock.close()
        raise


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
                    ):
                        if key in body:
                            value = str(body.get(key) or "")
                            if key == "name" and not value.strip():
                                value = profile.name
                            setattr(profile, key, value)
                    profile.folder = STATE.create_folder(str(body.get("folder") or DEFAULT_FOLDER))
                    if isinstance(body.get("fingerprint"), dict):
                        profile.fingerprint = core.Fingerprint(
                            **{
                                key: value
                                for key, value in body["fingerprint"].items()
                                if key in core.Fingerprint.__dataclass_fields__
                            }
                        )
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
