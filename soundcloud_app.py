#!/usr/bin/env python3
"""Hidden SoundCloud session/playback backend for an Omarchy bar plugin.

The only normal window is the one-time sign-in surface. Closing it hides the
WebKit view while preserving the session and audio. The bar talks to this
process over a user-only Unix socket.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import pwd
import re
import secrets
import socket
import sqlite3
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

APP_ID = "com.github.brunosilveira.OmarchySoundCloud"
APP_NAME = "SoundCloud"
START_URI = "https://soundcloud.com/you/likes"
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 524288
MAX_TITLE_BYTES = 512
MAX_ARTIST_BYTES = 256
MAX_URL_BYTES = 2048
MAX_TRACKS = 100
MAX_PLAY_COUNT = 10**12
MAX_TRACK_DURATION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ARTWORK_BYTES = 131072
MAX_ARTWORK_PIXELS = 1024 * 1024
LIST_ARTWORK_BYTES = 32768
LIST_ARTWORK_PIXELS = 512 * 512
LIST_ARTWORK_DIMENSION = 512
MAX_COOKIE_STORE_BYTES = 512 * 1024
MAX_TRACK_CACHE_BYTES = 1024 * 1024
MAX_COOKIES = 256
MAX_CLIENTS = 8
MAX_PENDING_DISPATCHES = 32
MAX_EVENT_LOG_BYTES = 512 * 1024
CLIENT_TIMEOUT_SECONDS = 5.0
CLIENT_IDLE_SECONDS = 60.0
SEND_TIMEOUT_SECONDS = 0.1
HLS_MAX_WORKERS = 2
HLS_OPERATION_SECONDS = 45.0
ARTWORK_HOSTS = {"i1.sndcdn.com"}
MAX_WAVEFORM_BARS = 200
# SoundCloud's bot protection (DataDome) shows its captcha in an iframe from here.
BOT_CHECK_HOSTS = {"geo.captcha-delivery.com"}
# "Continue with Google/Apple/Facebook" opens a popup on these providers.
# Only sign-in popups may navigate here; the main view stays on SoundCloud.
SIGN_IN_PROVIDER_HOSTS = {
    "accounts.google.com",
    "accounts.youtube.com",
    "appleid.apple.com",
    "idmsa.apple.com",
    "www.facebook.com",
    "m.facebook.com",
    "facebook.com",
}
SOUNDCLOUD_WEB_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "secure.soundcloud.com",
    "m.soundcloud.com",
}
_event_log_fd: int | None = None
_event_log_lock = threading.Lock()


@dataclass(frozen=True)
class ProfilePaths:
    data_dir: Path
    cache_dir: Path
    cookie_db: Path


def profile_paths(env: Mapping[str, str] | None = None) -> ProfilePaths:
    values: Mapping[str, str] = {} if env is None else env
    trusted_home = pwd.getpwuid(os.geteuid()).pw_dir if env is None else values.get("HOME", str(Path.home()))
    home = Path(trusted_home)
    data_home = Path(values.get("XDG_DATA_HOME", home / ".local" / "share"))
    cache_home = Path(values.get("XDG_CACHE_HOME", home / ".cache"))
    data_dir = data_home / "omarchy-soundcloud"
    return ProfilePaths(
        data_dir=data_dir,
        cache_dir=cache_home / "omarchy-soundcloud",
        cookie_db=data_dir / "cookies.sqlite",
    )


def desktop_entry_path(env: Mapping[str, str] | None = None) -> Path:
    """Return this plugin's application-menu entry path."""
    values: Mapping[str, str] = os.environ if env is None else env
    trusted_home = pwd.getpwuid(os.geteuid()).pw_dir if env is None else values.get("HOME", str(Path.home()))
    data_home = Path(values.get("XDG_DATA_HOME", Path(trusted_home) / ".local" / "share"))
    if not data_home.is_absolute():
        data_home = Path(trusted_home) / ".local" / "share"
    return data_home / "applications" / f"{APP_ID}.desktop"


def desktop_entry_argument(value: str) -> str:
    """Quote a desktop-entry Exec argument only when its path requires it."""
    if not re.search(r'[\s"\\]', value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install_desktop_entry(env: Mapping[str, str] | None = None) -> Path:
    """Install an application-menu launcher for the current plugin checkout."""
    path = desktop_entry_path(env)
    helper_path = desktop_entry_argument(str(Path(__file__).resolve()))
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=SoundCloud playback and browsing in Omarchy\n"
        f"Exec=/usr/bin/python3 -I {helper_path} launch\n"
        "Icon=audio-x-generic\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
        "Keywords=SoundCloud;Music;Audio;\n"
        "StartupNotify=false\n"
    )
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def runtime_socket_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    runtime_dir = values.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is required")
    root = Path(runtime_dir)
    if not root.is_absolute():
        raise RuntimeError("XDG_RUNTIME_DIR must be absolute")
    return root / "omarchy-soundcloud" / "control.sock"


def log_event(event: str, **fields: object) -> None:
    """Write bounded, credential-free pagination diagnostics."""
    allowed_fields = {
        "track-page": {
            "source", "reset", "incomingCount", "previousCount",
            "resultCount", "addedCount", "hasMore",
        },
        "lazy-load-request": {"source", "started", "hasMore"},
    }
    if _event_log_fd is None or event not in allowed_fields:
        return
    record = {"event": event, "time": round(time.time(), 3)}
    record.update({key: value for key, value in fields.items() if key in allowed_fields[event]})
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 2048:
        return
    with _event_log_lock:
        try:
            details = os.fstat(_event_log_fd)
            if details.st_size + len(encoded) > MAX_EVENT_LOG_BYTES:
                os.ftruncate(_event_log_fd, 0)
            os.write(_event_log_fd, encoded)
        except OSError:
            pass


def configure_runtime_event_log() -> None:
    global _event_log_fd
    raw_fd = os.environ.pop("OMARCHY_SOUNDCLOUD_EVENT_LOG_FD", "")
    if not raw_fd.isdecimal():
        return
    fd = int(raw_fd)
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o077
        ):
            raise PermissionError("refusing unsafe pagination log")
        os.set_inheritable(fd, False)
        _event_log_fd = fd
    except (OSError, ValueError):
        try:
            os.close(fd)
        except OSError:
            pass


def close_runtime_event_log() -> None:
    global _event_log_fd
    if _event_log_fd is None:
        return
    try:
        os.close(_event_log_fd)
    except OSError:
        pass
    _event_log_fd = None


def open_runtime_event_log():
    directory_fd = ensure_private_directory(runtime_socket_path().parent)
    try:
        fd = os.open(
            "pagination.log",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise PermissionError("refusing unsafe pagination log")
        os.set_blocking(fd, True)
        os.fchmod(fd, 0o600)
        if details.st_size > MAX_EVENT_LOG_BYTES:
            os.ftruncate(fd, 0)
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def ensure_private_directory(path: Path) -> int:
    """Create and open an owner-only directory without following symlinks."""
    path = path.absolute()
    parts = path.parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts[1:]):
            is_leaf = index == len(parts) - 2
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.mkdir(part, 0o700 if is_leaf else 0o755, dir_fd=fd)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise PermissionError(f"refusing symlink or invalid directory: {path}") from exc
            os.close(fd)
            fd = next_fd
            details = os.fstat(fd)
            if not stat.S_ISDIR(details.st_mode):
                raise PermissionError(f"refusing non-directory path component: {path}")
            if is_leaf:
                if details.st_uid != os.geteuid():
                    raise PermissionError(f"refusing directory owned by another user: {path}")
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_private_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o077
        ):
            raise PermissionError(f"refusing unsafe private file: {path}")
    finally:
        os.close(fd)


class CookieStore:
    """Descriptor-anchored storage for the small persistent cookie jar."""

    def __init__(self, directory_fd: int):
        self.directory_fd = directory_fd
        self.filename = "cookies.json"

    @staticmethod
    def _valid_record(record: object) -> bool:
        if not isinstance(record, dict):
            return False
        required_strings = ("name", "value", "domain", "path")
        if any(not isinstance(record.get(key), str) for key in required_strings):
            return False
        if any(len(record[key].encode("utf-8")) > 4096 for key in required_strings):
            return False
        domain = record["domain"].lower().lstrip(".").rstrip(".")
        if domain != "soundcloud.com" and not domain.endswith(".soundcloud.com"):
            return False
        expires = record.get("expires")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int)):
            return False
        return isinstance(record.get("secure"), bool) and isinstance(record.get("httpOnly"), bool)

    def load(self) -> list[dict[str, object]]:
        try:
            fd = os.open(
                self.filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return []
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > MAX_COOKIE_STORE_BYTES
            ):
                raise PermissionError("refusing unsafe cookie store")
            os.set_blocking(fd, True)
            data = bytearray()
            while len(data) <= MAX_COOKIE_STORE_BYTES:
                chunk = os.read(fd, min(65536, MAX_COOKIE_STORE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_COOKIE_STORE_BYTES:
                raise PermissionError("cookie store exceeds security limit")
            payload = json.loads(data.decode("utf-8"))
            if (
                not isinstance(payload, list)
                or len(payload) > MAX_COOKIES
                or not all(self._valid_record(record) for record in payload)
            ):
                raise PermissionError("invalid cookie store")
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermissionError("invalid cookie store") from exc
        finally:
            os.close(fd)

    def save(self, records: list[dict[str, object]]) -> None:
        if len(records) > MAX_COOKIES or not all(self._valid_record(record) for record in records):
            raise ValueError("invalid cookie records")
        encoded = json.dumps(records, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_COOKIE_STORE_BYTES:
            raise ValueError("cookie store exceeds security limit")
        temporary = f".cookies-{secrets.token_hex(12)}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(
                temporary,
                self.filename,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def migrate_legacy_sqlite(self) -> list[dict[str, object]]:
        try:
            fd = os.open(
                "cookies.sqlite",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return []
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > MAX_COOKIE_STORE_BYTES
            ):
                raise PermissionError("refusing unsafe legacy cookie store")
            os.set_blocking(fd, True)
            chunks = []
            remaining = details.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise PermissionError("truncated legacy cookie store")
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        database = sqlite3.connect(":memory:")
        try:
            database.deserialize(b"".join(chunks))
            rows = database.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
                "FROM moz_cookies LIMIT ?",
                (MAX_COOKIES + 1,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PermissionError("invalid legacy cookie store") from exc
        finally:
            database.close()
        if len(rows) > MAX_COOKIES:
            raise PermissionError("too many legacy cookies")
        records = []
        for name, value, domain, path, expiry, secure, http_only in rows:
            normalized = str(domain).lower().lstrip(".").rstrip(".")
            if normalized != "soundcloud.com" and not normalized.endswith(".soundcloud.com"):
                continue
            record = {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(path),
                "expires": int(expiry) if expiry else None,
                "secure": bool(secure),
                "httpOnly": bool(http_only),
            }
            if not self._valid_record(record):
                raise PermissionError("invalid legacy cookies")
            records.append(record)
        self.save(records)
        os.unlink("cookies.sqlite", dir_fd=self.directory_fd)
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.unlink("cookies.sqlite" + suffix, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
        return records


class PrivateJsonStore:
    """Descriptor-anchored storage for bounded non-secret cached state."""

    def __init__(self, directory_fd: int, filename: str, maximum_bytes: int):
        self.directory_fd = directory_fd
        self.filename = filename
        self.maximum_bytes = maximum_bytes

    def load(self) -> object | None:
        try:
            fd = os.open(
                self.filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except FileNotFoundError:
            return None
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or details.st_mode & 0o077
                or details.st_size > self.maximum_bytes
            ):
                return None
            os.set_blocking(fd, True)
            data = bytearray()
            while len(data) <= self.maximum_bytes:
                chunk = os.read(fd, min(65536, self.maximum_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > self.maximum_bytes:
                return None
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            os.close(fd)

    def save(self, payload: object) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.maximum_bytes:
            return
        temporary = f".{self.filename}-{secrets.token_hex(12)}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(
                temporary,
                self.filename,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass


def prepare_profile_paths(paths: ProfilePaths) -> None:
    os.close(ensure_private_directory(paths.data_dir))
    for suffix in ("", "-journal", "-wal", "-shm"):
        validate_private_file(Path(str(paths.cookie_db) + suffix))


def prepare_runtime_socket_path(path: Path) -> None:
    runtime_root = path.parent.parent
    details = runtime_root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o077
    ):
        raise PermissionError("XDG_RUNTIME_DIR must be an owner-only directory")
    os.close(ensure_private_directory(path.parent))


def is_soundcloud_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host in SOUNDCLOUD_WEB_HOSTS


def is_bot_check_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in BOT_CHECK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def is_sign_in_provider_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    host = parsed.hostname.lower().rstrip(".")
    # Google bounces through its country domains to set cookies.
    return host in SIGN_IN_PROVIDER_HOSTS or re.fullmatch(r"accounts\.google(\.com?)?\.[a-z]{2,3}", host) is not None


def is_playback_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"soundcloud:tracks:[1-9][0-9]*", value) is not None


def is_allowed_artwork_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower().rstrip(".") in ARTWORK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and len(uri.encode("utf-8")) <= MAX_URL_BYTES
        and not any(ord(character) < 32 for character in uri)
    )


def normalize_list_artwork_url(uri: object, size: str = "t120x120") -> str:
    if not isinstance(uri, str) or not is_allowed_artwork_uri(uri):
        return ""
    if not re.fullmatch(r"t\d{2,3}x\d{2,3}", size):
        return ""
    parsed = urlparse(uri)
    path = re.sub(
        r"-(?:large|original|t\d+x\d+)(\.(?:jpe?g|png))$",
        rf"-{size}\1",
        parsed.path,
        flags=re.IGNORECASE,
    )
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def apply_graphics_workarounds(env: dict[str, str] | None = None) -> bool:
    """Avoid WebKitGTK's invalid explicit-sync commits on Hyprland."""
    values = os.environ if env is None else env
    if "WEBKIT_DISABLE_DMABUF_RENDERER" in values:
        return False
    if values.get("HYPRLAND_INSTANCE_SIGNATURE"):
        values["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        return True
    return False


def source_uri(source: str) -> str | None:
    return {
        "home": "https://soundcloud.com/discover",
        "likes": "https://soundcloud.com/you/likes",
        "feed": "https://soundcloud.com/feed",
    }.get(source)


def normalize_backend_command(command: str) -> str:
    return "show" if command == "launch" else command


def track_source_from_command(command: str) -> str | None:
    return {
        "tracks:home": "home",
        "tracks:feed": "feed",
    }.get(command)


def load_more_source_from_command(command: str) -> str | None:
    return {
        "load-more:home": "home",
        "load-more:feed": "feed",
    }.get(command)


def playback_id_from_command(command: str) -> str | None:
    if not command.startswith("play:"):
        return None
    value = command.removeprefix("play:")
    return value if is_playback_id(value) else None


def track_like_from_command(command: str) -> tuple[str, bool] | None:
    for prefix, liked in (("like:", True), ("unlike:", False)):
        if command.startswith(prefix):
            value = command.removeprefix(prefix)
            return (value, liked) if is_playback_id(value) else None
    return None


def oauth_token_from_cookies(records: list[dict[str, object]]) -> str | None:
    for record in records:
        domain = str(record.get("domain", "")).lower().lstrip(".").rstrip(".")
        value = record.get("value")
        if (record.get("name") == "oauth_token" and domain == "soundcloud.com"
                and isinstance(value, str) and re.fullmatch(r"[0-9A-Za-z._-]{1,2048}", value)):
            return value
    return None


def merge_track_pages(
    existing: list[dict[str, object]],
    incoming: list[dict[str, object]],
    *,
    reset: bool,
    limit: int = MAX_TRACKS,
) -> list[dict[str, object]]:
    merged: OrderedDict[str, dict[str, object]] = OrderedDict()
    if not reset:
        for track in existing:
            url = track.get("url") if isinstance(track, dict) else None
            if isinstance(url, str) and url:
                merged[url] = track
    for track in incoming:
        url = track.get("url") if isinstance(track, dict) else None
        if isinstance(url, str) and url:
            merged[url] = track
    return list(merged.values())[:max(0, limit)]


def load_more_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      if (typeof window.__omarchyLoadMore !== 'function') {{
        return {{ started: false, hasMore: false }};
      }}
      return window.__omarchyLoadMore({json.dumps(source)});
    }})()"""


def api_tracks_script(source: str) -> str | None:
    if source not in {"home", "feed"}:
        return None
    return rf"""(() => {{
      const source = {json.dumps(source)};
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const state = cache[source];
      if (!state || typeof state !== 'object') {{
        return {{ tracks: [], pending: true, error: '' }};
      }}
      const tracks = Array.isArray(state.tracks) ? state.tracks.slice(0, 100) : [];
      return {{
        tracks,
        pending: state.loading === true || state.completed !== true,
        error: typeof state.error === 'string' ? state.error.slice(0, 128) : ''
      }};
    }})()"""


def api_request_capture_script() -> str:
    return r"""(() => {
      const allowedPaths = new Set(['/stream', '/mixed-selections']);
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      window.__omarchyApiTrackLists = cache;
      const publishedSources = new Set();
      const pagination = Object.create(null);
      const xhrRequests = new WeakMap();
      const playbackTracks = new Map();
      let playbackGeneration = 0;
      let latestRequestContext = null;
      // Any authenticated api-v2 request the frontend makes; search only needs
      // its client id and authorization, not a Home/Feed route.
      let latestApiContext = null;
      const isApiV2 = (input) => {
        try {
          const request = input instanceof Request ? input : null;
          const url = new URL(request ? request.url : input, location.href);
          return url.protocol === 'https:' && url.hostname === 'api-v2.soundcloud.com'
            && (!url.port || url.port === '443') && !url.username && !url.password;
        } catch (_) { return false; }
      };
      const routeGenerations = Object.create(null);
      const nextRouteGeneration = (path) => {
        const source = path === '/stream' ? 'feed' : 'home';
        routeGenerations[source] = (routeGenerations[source] || 0) + 1;
        return routeGenerations[source];
      };
      const boundedText = async (response, maximum) => {
        const declared = Number(response.headers && response.headers.get('content-length'));
        if (Number.isFinite(declared) && declared > maximum) throw new Error('response too large');
        if (!response.body || typeof response.body.getReader !== 'function')
          throw new Error('response body unavailable');
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8', { fatal: true });
        let total = 0;
        let text = '';
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (!(part.value instanceof Uint8Array)) throw new Error('invalid response body');
            total += part.value.byteLength;
            if (total > maximum) {
              await reader.cancel();
              throw new Error('response too large');
            }
            text += decoder.decode(part.value, { stream: true });
          }
          text += decoder.decode();
          return text;
        } catch (error) {
          try { await reader.cancel(); } catch (_) {}
          throw error;
        }
      };
      const normalise = (payload, context, playbackTarget = playbackTracks) => {
        const queue = [[payload, 0]];
        const seenObjects = new Set();
        const seenTracks = new Set();
        const tracks = [];
        let visited = 0;
        while (queue.length && visited++ < 3000 && tracks.length < 50) {
          const current = queue.shift();
          const value = current[0];
          const depth = current[1];
          if (!value || typeof value !== 'object' || seenObjects.has(value)) continue;
          seenObjects.add(value);
          const permalink = typeof value.permalink_url === 'string' ? value.permalink_url : '';
          const title = typeof value.title === 'string' ? value.title.trim().slice(0, 512) : '';
          if ((value.kind === undefined || value.kind === 'track') && permalink && title
              && typeof value.duration === 'number' && !seenTracks.has(permalink)) {
            seenTracks.add(permalink);
            const user = value.user && typeof value.user === 'object' ? value.user : {};
            const publisher = value.publisher_metadata && typeof value.publisher_metadata === 'object'
              ? value.publisher_metadata : {};
            const track = {
              title,
              artist: String(user.username || user.full_name || publisher.artist || '').slice(0, 256),
              url: permalink.slice(0, 2048),
              artUrl: String(value.artwork_url || user.avatar_url || '').slice(0, 2048),
              playCount: Number.isSafeInteger(value.playback_count) ? value.playback_count : 0,
              durationMs: Number.isSafeInteger(value.duration) ? value.duration : 0
            };
            tracks.push(track);
            const transcodings = value.media && Array.isArray(value.media.transcodings)
              ? value.media.transcodings : [];
            const playable = transcodings
              .filter((item) => item && item.format && typeof item.url === 'string'
                && item.url.length <= 4096
                && ['hls', 'progressive'].includes(item.format.protocol))
              .sort((left, right) => {
                const score = (item) => item.format.protocol === 'progressive' ? 0
                  : item.format.mime_type === 'audio/mpeg' ? 1 : 2;
                return score(left) - score(right);
              })[0];
            if (context && context.headers instanceof Headers) {
              try {
                const urn = typeof value.urn === 'string'
                    && /^soundcloud:tracks:[0-9]+$/.test(value.urn)
                  ? value.urn
                  : (Number.isSafeInteger(value.id) && value.id > 0
                    ? 'soundcloud:tracks:' + value.id : '');
                const endpoint = playable ? new URL(playable.url)
                  : (urn
                    ? new URL('https://api.soundcloud.com/tracks/soundcloud:tracks:'
                      + urn.substring('soundcloud:tracks:'.length) + '/streams') : null);
                if (endpoint && !playable) {
                  const clientId = context.headers.get('x-client-id')
                    || context.headers.get('x-soundcloud-client-id');
                  if (clientId) endpoint.searchParams.set('client_id', clientId);
                }
                if (endpoint.protocol === 'https:'
                    && ['api-v2.soundcloud.com', 'api.soundcloud.com'].includes(endpoint.hostname)
                    && !endpoint.port && !endpoint.username && !endpoint.password
                    && (endpoint.pathname.startsWith('/media/')
                      || /^\/tracks\/soundcloud:tracks:[0-9]+\/streams$/.test(endpoint.pathname))) {
                  track.playbackId = urn;
                  if (playbackTarget.size >= 200 && !playbackTarget.has(urn)) {
                    playbackTarget.delete(playbackTarget.keys().next().value);
                  }
                  playbackTarget.set(urn, {
                    endpoint: endpoint.href,
                    headers: new Headers(context.headers),
                    credentials: context.credentials,
                    protocol: playable ? playable.format.protocol : '',
                    mimeType: playable ? playable.format.mime_type : '',
                    streams: !playable,
                    waveformUrl: String(value.waveform_url || '').slice(0, 2048),
                    track
                  });
                }
              } catch (_) {}
            }
          }
          if (depth >= 8) continue;
          if (Array.isArray(value)) {
            for (const child of value.slice(0, 100)) queue.push([child, depth + 1]);
          } else {
            for (const key of Object.keys(value).slice(0, 100)) queue.push([value[key], depth + 1]);
          }
        }
        return tracks;
      };
      const homeTrackIds = (payload) => {
        const ids = [];
        const seen = new Set();
        const selections = payload && Array.isArray(payload.collection) ? payload.collection : [];
        for (const selection of selections.slice(0, 50)) {
          const itemState = selection && selection.items && typeof selection.items === 'object'
            ? selection.items : null;
          const items = Array.isArray(itemState) ? itemState
            : (itemState && Array.isArray(itemState.collection) ? itemState.collection : []);
          for (const item of items.slice(0, 50)) {
            const tracks = item && Array.isArray(item.tracks) ? item.tracks : [];
            for (const track of tracks.slice(0, 100)) {
              const id = track && Number.isSafeInteger(track.id) ? track.id : 0;
              if (id > 0 && !seen.has(id)) {
                seen.add(id);
                ids.push(id);
              }
              if (ids.length >= 50) return ids;
            }
          }
        }
        return ids;
      };
      const homeTracks = async (payload, context, playbackTarget = playbackTracks) => {
        const embedded = normalise(payload, context, playbackTarget);
        const ids = homeTrackIds(payload);
        if (!ids.length || !context || !(context.headers instanceof Headers)) return embedded;
        const request = new URL(context.requestUrl);
        if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
            || request.port || request.username || request.password) return embedded;
        const clientId = request.searchParams.get('client_id');
        request.pathname = '/tracks';
        request.search = '';
        request.searchParams.set('ids', ids.join(','));
        if (clientId) request.searchParams.set('client_id', clientId);
        const response = await originalFetch(request.href, {
          method: 'GET', headers: context.headers, credentials: context.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('home tracks unavailable');
        const text = await boundedText(response, 2 * 1024 * 1024);
        if (!text.length) throw new Error('invalid home tracks response');
        const expanded = normalise(JSON.parse(text), context, playbackTarget);
        const merged = new Map(embedded.map((track) => [track.url, track]));
        for (const track of expanded) merged.set(track.url, track);
        return Array.from(merged.values()).slice(0, 50);
      };
      const validNextUrl = (payload, path) => {
        try {
          const value = payload && typeof payload.next_href === 'string' ? payload.next_href : '';
          if (!value || value.length > 4096) return '';
          const url = new URL(value);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || url.pathname !== path) return '';
          return url.href;
        } catch (_) { return ''; }
      };
      const requestContext = (input, init) => {
        const request = input instanceof Request ? input : null;
        const headers = new Headers(request ? request.headers : undefined);
        if (init && init.headers) {
          for (const [name, value] of new Headers(init.headers)) headers.set(name, value);
        }
        const privateHeaders = new Headers();
        for (const name of ['authorization', 'x-client-id', 'x-soundcloud-client-id']) {
          const value = headers.get(name);
          if (value && value.length <= 2048) privateHeaders.set(name, value);
        }
        return {
          headers: privateHeaders,
          credentials: String((init && init.credentials) || (request && request.credentials) || 'omit'),
          requestUrl: new URL(request ? request.url : input, location.href).href
        };
      };
      const finishWithoutPage = (source, error, requestUrl, routeGeneration) => {
        if (routeGeneration !== routeGenerations[source]) return;
        const state = pagination[source];
        if (state) {
          state.loading = false;
          if (requestUrl && state.seen instanceof Set) state.seen.delete(requestUrl);
          if (state.lastRequested === requestUrl) state.lastRequested = '';
        }
        const current = cache[source];
        if (current) {
          current.loading = false;
          current.completed = true;
          current.error = String(error || '').slice(0, 128);
        }
        window.webkit.messageHandlers.omarchyTracks.postMessage(JSON.stringify({
          source,
          tracks: [],
          reset: false,
          hasMore: true,
          addedCount: 0,
          error: String(error || 'Could not load more tracks').slice(0, 128)
        }));
      };
      // Home's selections (your mixes, stations, playlists) for the Mixes tab.
      let homeSelections = [];
      const rememberSelections = (payload) => {
        const list = payload && Array.isArray(payload.collection) ? payload.collection.slice(0, 50) : [];
        const known = new Set(homeSelections.map((selection) => selection && selection.urn));
        homeSelections = homeSelections
          .concat(list.filter((selection) => selection && typeof selection === 'object' && !known.has(selection.urn)))
          .slice(0, 50);
      };
      window.__omarchyHomeSelections = () => homeSelections;
      const publish = async (path, text, context, routeGeneration) => {
        try {
          if (!(text.length > 0 && text.length <= 2 * 1024 * 1024)) throw new Error('invalid response');
          const payload = JSON.parse(text);
          if (path === '/mixed-selections') rememberSelections(payload);
          const stagedPlayback = new Map();
          const tracks = path === '/mixed-selections'
            ? await homeTracks(payload, context, stagedPlayback)
            : normalise(payload, context, stagedPlayback);
          const source = path === '/stream' ? 'feed' : 'home';
          if (routeGeneration !== routeGenerations[source]) return;
          for (const [urn, item] of stagedPlayback) {
            if (playbackTracks.size >= 200 && !playbackTracks.has(urn)) {
              playbackTracks.delete(playbackTracks.keys().next().value);
            }
            playbackTracks.set(urn, item);
          }
          const reset = !publishedSources.has(source);
          publishedSources.add(source);
          const previous = !reset && cache[source] && Array.isArray(cache[source].tracks)
            ? cache[source].tracks : [];
          const merged = new Map(previous.map((track) => [track.url, track]));
          for (const track of tracks) merged.set(track.url, track);
          const visibleTracks = Array.from(merged.values()).slice(0, 100);
          const addedCount = reset ? visibleTracks.length
            : Math.max(0, visibleTracks.length - previous.length);
          const candidateNext = validNextUrl(payload, path);
          const seen = context.seen instanceof Set ? context.seen : new Set();
          if (context.requestUrl) seen.add(context.requestUrl);
          const hasMore = visibleTracks.length < 100
            && (reset || addedCount > 0)
            && !!candidateNext
            && !seen.has(candidateNext);
          pagination[source] = {
            next: hasMore ? candidateNext : '',
            path,
            headers: context.headers,
            credentials: context.credentials,
            loading: false,
            lastRequested: String(context.requestUrl || ''),
            seen
          };
          cache[source] = {
            tracks: visibleTracks,
            loading: false,
            completed: true,
            error: ''
          };
          window.webkit.messageHandlers.omarchyTracks.postMessage(
            JSON.stringify({ source, tracks, reset, hasMore, addedCount })
          );
        } catch (_) {
          if (context.requestUrl) {
            finishWithoutPage(
              path === '/stream' ? 'feed' : 'home',
              'Could not load more tracks',
              context.requestUrl,
              routeGeneration
            );
          }
        }
      };
      const routeFor = (input) => {
        try {
          const request = input instanceof Request ? input : null;
          const url = new URL(request ? request.url : input, location.href);
          if (url.protocol !== 'https:'
              || url.hostname !== 'api-v2.soundcloud.com'
              || (url.port && url.port !== '443')
              || url.username || url.password
              || !allowedPaths.has(url.pathname)) return '';
          return url.pathname;
        } catch (_) { return ''; }
      };
      const markLoading = (path) => {
        if (!path) return;
        const source = path === '/stream' ? 'feed' : 'home';
        const previous = cache[source];
        cache[source] = {
          tracks: previous && Array.isArray(previous.tracks) ? previous.tracks.slice(0, 100) : [],
          loading: true,
          completed: false,
          error: ''
        };
      };
      const originalFetch = window.fetch.bind(window);
      const loadWaveform = async (value) => {
        const source = new URL(String(value || '').replace(/\.png$/, '.json'));
        if (source.protocol !== 'https:' || source.hostname !== 'wave.sndcdn.com'
            || source.port || source.username || source.password
            || !/^\/[A-Za-z0-9_-]+\.json$/.test(source.pathname)) return [];
        const response = await originalFetch(source.href, {
          method: 'GET', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) return [];
        const payload = JSON.parse(await boundedText(response, 262144));
        const samples = payload && Array.isArray(payload.samples) ? payload.samples : [];
        const height = Number(payload.height);
        if (!samples.length || !(height > 0)) return [];
        const count = 76;
        const bars = [];
        for (let index = 0; index < count; index++) {
          const start = Math.floor(index * samples.length / count);
          const end = Math.max(start + 1, Math.floor((index + 1) * samples.length / count));
          let peak = 0;
          for (let sample = start; sample < end && sample < samples.length; sample++) {
            const level = Number(samples[sample]);
            if (Number.isFinite(level) && level > peak) peak = level;
          }
          bars.push(Math.min(1, peak / height) * 100);
        }
        // Stretch each track's own range so dense mixes still show shape.
        const lowest = Math.min(...bars);
        const highest = Math.max(...bars);
        if (highest - lowest < 5) return bars.map(Math.round);
        return bars.map((level) => Math.round(20 + (level - lowest) / (highest - lowest) * 80));
      };
      window.__omarchyPlayApiTrack = function(value, requestId) {
        const target = String(value || '');
        if (!/^soundcloud:tracks:[0-9]+$/.test(target)
            || !Number.isSafeInteger(requestId) || requestId < 0) return false;
        const generation = ++playbackGeneration;
        const pageAudio = document.querySelector('audio');
        if (pageAudio && !pageAudio.paused) pageAudio.pause();
        const loadItem = async () => {
          let item = playbackTracks.get(target);
          if (item) return item;
          const waitDeadline = Date.now() + 30000;
          while (!latestRequestContext && !latestApiContext && Date.now() < waitDeadline) {
            if (generation !== playbackGeneration) throw new Error('selection superseded');
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          const id = target.substring('soundcloud:tracks:'.length);
          const context = latestRequestContext || latestApiContext;
          if (!context) throw new Error('track context unavailable');
          const request = new URL(context.requestUrl);
          if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
              || request.port || request.username || request.password)
            throw new Error('track context unavailable');
          const clientId = request.searchParams.get('client_id');
          request.pathname = '/tracks';
          request.search = '';
          request.searchParams.set('ids', id);
          if (clientId) request.searchParams.set('client_id', clientId);
          const response = await originalFetch(request.href, {
            method: 'GET', headers: context.headers, credentials: context.credentials,
            cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
          });
          if (!response.ok) throw new Error('track unavailable');
          const text = await boundedText(response, 2 * 1024 * 1024);
          if (!text.length) throw new Error('invalid track response');
          normalise(JSON.parse(text), context);
          item = playbackTracks.get(target);
          if (!item) throw new Error('track unavailable');
          return item;
        };
        const itemLoaded = loadItem();
        const waveformLoaded = itemLoaded.then((item) => loadWaveform(item.waveformUrl)).catch(() => []);
        itemLoaded.then((item) => originalFetch(item.endpoint, {
          method: 'GET', headers: item.headers, credentials: item.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 16384);
        }).then((text) => {
          if (generation !== playbackGeneration) return;
          if (!text || text.length > 16384) throw new Error('invalid stream response');
          const payload = JSON.parse(text);
          let resolved = String(payload.url || '');
          let protocol = item.protocol;
          let mimeType = item.mimeType;
          if (item.streams) {
            if (typeof payload.hls_aac_160_url === 'string') {
              resolved = payload.hls_aac_160_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.hls_aac_96_url === 'string') {
              resolved = payload.hls_aac_96_url;
              protocol = 'hls';
              mimeType = 'audio/mp4';
            } else if (typeof payload.http_mp3_128_url === 'string') {
              resolved = payload.http_mp3_128_url;
              protocol = 'progressive';
              mimeType = 'audio/mpeg';
            } else if (typeof payload.hls_mp3_128_url === 'string') {
              resolved = payload.hls_mp3_128_url;
              protocol = 'hls';
              mimeType = 'audio/mpeg';
            }
          }
          const stream = new URL(resolved);
          const host = stream.hostname.toLowerCase();
          if (stream.protocol !== 'https:' || stream.port || stream.username || stream.password
              || !(host.endsWith('.sndcdn.com') || host.endsWith('.soundcloud.cloud')))
            throw new Error('invalid stream URL');
          // Never hold playback for the waveform: a slow fetch just means no bars.
          let timer;
          const waveformDeadline = new Promise((resolve) => { timer = setTimeout(() => resolve([]), 500); });
          return Promise.race([waveformLoaded, waveformDeadline]).then((waveform) => {
            clearTimeout(timer);
            if (generation !== playbackGeneration) return;
            window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
              ok: true,
              requestId,
              streamUrl: stream.href,
              protocol,
              mimeType,
              track: item.track,
              waveform
            }));
          });
        })).catch((error) => {
          if (generation !== playbackGeneration) return;
          const detail = /^HTTP [0-9]{3}$/.test(String(error && error.message || ''))
            ? ' (' + error.message + ')' : '';
          window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
            ok: false,
            requestId,
            error: 'SoundCloud could not resolve this track' + detail
          }));
        });
        return true;
      };
      window.__omarchyPlaybackTrackCount = () => playbackTracks.size;
      window.fetch = function(input, init) {
        const path = routeFor(input);
        const context = path ? requestContext(input, init) : null;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (context) latestRequestContext = context;
        else if (isApiV2(input)) {
          const apiContext = requestContext(input, init);
          if (apiContext.headers.has('authorization') || new URL(apiContext.requestUrl).searchParams.has('client_id'))
            latestApiContext = apiContext;
        }
        markLoading(path);
        return originalFetch(input, init).then((response) => {
          if (path && response.ok) {
            boundedText(response.clone(), 2 * 1024 * 1024)
              .then((text) => publish(path, text, context, routeGeneration))
              .catch(() => {
                if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return;
                const current = cache[path === '/stream' ? 'feed' : 'home'];
                if (current) current.loading = false;
              });
          } else if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) return response;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          return response;
        }).catch((error) => {
          if (path) {
            if (routeGeneration !== routeGenerations[path === '/stream' ? 'feed' : 'home']) throw error;
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
          throw error;
        });
      };
      const originalOpen = XMLHttpRequest.prototype.open;
      const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
      const originalSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        const path = routeFor(url);
        xhrRequests.set(this, {
          path,
          api: !path && isApiV2(url),
          headers: new Headers(),
          credentials: 'include',
          requestUrl: (path || isApiV2(url)) ? new URL(url, location.href).href : ''
        });
        return originalOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        const context = xhrRequests.get(this);
        if (context && (context.path || context.api)
            && /^(authorization|x-client-id|x-soundcloud-client-id)$/i.test(String(name))) {
          const bounded = String(value);
          if (bounded.length <= 2048) context.headers.set(String(name), bounded);
        }
        return originalSetRequestHeader.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        const context = xhrRequests.get(this) || { path: '', headers: new Headers(), credentials: 'omit' };
        const path = context.path;
        const routeGeneration = path ? nextRouteGeneration(path) : 0;
        if (path) latestRequestContext = context;
        else if (context.api) latestApiContext = context;
        markLoading(path);
        if (path) this.addEventListener('load', function() {
          if (this.status >= 200 && this.status < 300
              && (!this.responseType || this.responseType === 'text')) {
            const text = this.responseText;
            if (typeof text === 'string' && text.length <= 2 * 1024 * 1024)
              publish(path, text, context, routeGeneration);
          } else {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }
        }, { once: true });
        for (const eventName of ['error', 'abort', 'timeout']) {
          if (path) this.addEventListener(eventName, function() {
            const current = cache[path === '/stream' ? 'feed' : 'home'];
            if (current) current.loading = false;
          }, { once: true });
        }
        return originalSend.apply(this, arguments);
      };
      window.__omarchyLoadMore = function(source) {
        const expectedPath = source === 'feed' ? '/stream'
          : (source === 'home' ? '/mixed-selections' : '');
        const state = pagination[source];
        if (!expectedPath || !state || state.path !== expectedPath || !state.next) {
          return { started: false, hasMore: false };
        }
        if (state.loading) return { started: false, hasMore: true };
        const requestUrl = state.next;
        const seen = state.seen instanceof Set ? state.seen : new Set();
        if (seen.has(requestUrl) || seen.size >= 128) {
          state.next = '';
          return { started: false, hasMore: false };
        }
        seen.add(requestUrl);
        state.seen = seen;
        state.loading = true;
        state.lastRequested = requestUrl;
        markLoading(expectedPath);
        const routeGeneration = nextRouteGeneration(expectedPath);
        originalFetch(requestUrl, {
          method: 'GET',
          headers: state.headers,
          credentials: state.credentials
        }).then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return boundedText(response, 2 * 1024 * 1024);
        }).then((text) => publish(expectedPath, text, {
          headers: state.headers,
          credentials: state.credentials,
          requestUrl,
          seen
        }, routeGeneration)).catch(() => finishWithoutPage(
          source, 'Could not load more tracks', requestUrl, routeGeneration
        ));
        return { started: true, hasMore: true };
      };
      // Track SoundCloud's own web player so the backend can keep only one
      // player audible and pause the page when its window closes.
      const pageMedia = new Set();
      if (typeof HTMLMediaElement !== 'undefined' && HTMLMediaElement.prototype
          && typeof HTMLMediaElement.prototype.play === 'function') {
        const originalPlay = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function() {
          try {
            if (pageMedia.size >= 16 && !pageMedia.has(this)) pageMedia.clear();
            pageMedia.add(this);
            if (typeof window.__omarchyVolume === 'number') this.volume = window.__omarchyVolume;
            window.webkit.messageHandlers.omarchyPlayback.postMessage(
              JSON.stringify({ event: 'page-play' }));
          } catch (_) {}
          return originalPlay.apply(this, arguments);
        };
      }
      const pageMediaElements = () => {
        const media = new Set(pageMedia);
        try {
          for (const node of document.querySelectorAll('audio, video')) media.add(node);
        } catch (_) {}
        return media;
      };
      window.__omarchyPagePlaying = function() {
        for (const item of pageMediaElements()) {
          try { if (!item.paused && !item.ended) return true; } catch (_) {}
        }
        return false;
      };
      // SoundCloud plays through media elements it never attaches to the DOM.
      window.__omarchyPageMediaState = function() {
        let chosen = null;
        for (const item of pageMediaElements()) {
          try {
            if (!item.paused && !item.ended) { chosen = item; break; }
            chosen = item;
          } catch (_) {}
        }
        if (!chosen) return { present: false, playing: false, duration: 0, position: 0 };
        const duration = Number(chosen.duration);
        const position = Number(chosen.currentTime);
        return {
          present: true,
          playing: !chosen.paused && !chosen.ended,
          duration: Number.isFinite(duration) && duration > 0 ? duration : 0,
          position: Number.isFinite(position) && position > 0 ? position : 0
        };
      };
      window.__omarchyPausePage = function() {
        if (!window.__omarchyPagePlaying()) return false;
        try {
          const control = document.querySelector('.playControls__play.playing, .playControl.playing');
          if (control) control.click();
        } catch (_) {}
        for (const item of pageMediaElements()) {
          try { if (!item.paused) item.pause(); } catch (_) {}
        }
        return true;
      };
      window.__omarchySetVolume = function(value) {
        const volume = Math.max(0, Math.min(1, Number(value)));
        if (!Number.isFinite(volume)) return false;
        window.__omarchyVolume = volume;
        for (const item of pageMediaElements()) {
          try { item.volume = volume; } catch (_) {}
        }
        return true;
      };
      const apiGet = async (pathname, params) => {
        const context = latestRequestContext || latestApiContext;
        if (!context || !(context.headers instanceof Headers))
          throw new Error('SoundCloud is still loading, try again in a moment');
        const request = new URL(context.requestUrl);
        if (request.protocol !== 'https:' || request.hostname !== 'api-v2.soundcloud.com'
            || request.port || request.username || request.password)
          throw new Error('SoundCloud is unavailable');
        const clientId = request.searchParams.get('client_id');
        request.pathname = pathname;
        request.search = '';
        for (const [name, value] of Object.entries(params || {})) request.searchParams.set(name, String(value));
        if (clientId) request.searchParams.set('client_id', clientId);
        const response = await originalFetch(request.href, {
          method: 'GET', headers: context.headers, credentials: context.credentials,
          cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('SoundCloud request failed (HTTP ' + response.status + ')');
        const text = await boundedText(response, 2 * 1024 * 1024);
        if (!text.length) throw new Error('SoundCloud returned nothing');
        return { payload: JSON.parse(text), context };
      };
      const failure = (error) => {
        const message = String(error && error.message || '');
        return /^SoundCloud /.test(message) ? message.slice(0, 128) : 'SoundCloud request failed';
      };
      // normalise() walks a bounded number of objects; scan each result on its
      // own so long responses are not cut short.
      const normaliseEach = (payload, context) => {
        const items = Array.isArray(payload) ? payload
          : (payload && Array.isArray(payload.collection) ? payload.collection : [payload]);
        const tracks = [];
        const seen = new Set();
        for (const item of items.slice(0, 100)) {
          for (const track of normalise(item, context)) {
            if (seen.has(track.url)) continue;
            seen.add(track.url);
            tracks.push(track);
          }
          if (tracks.length >= 50) break;
        }
        return tracks.slice(0, 50);
      };
      // Follow SoundCloud's next_href (same api-v2 path only) until 50 tracks.
      const pagedTracks = async (pathname, params, limit = 50) => {
        let { payload, context } = await apiGet(pathname, params);
        let tracks = normaliseEach(payload, context);
        for (let page = 1; page < 8 && tracks.length < limit; page++) {
          const next = validNextUrl(payload, pathname);
          if (!next) break;
          const url = new URL(next);
          const nextParams = {};
          for (const [name, value] of url.searchParams) if (name !== 'client_id') nextParams[name] = value;
          ({ payload, context } = await apiGet(pathname, nextParams));
          const known = new Set(tracks.map((track) => track.url));
          tracks = tracks.concat(normaliseEach(payload, context).filter((track) => !known.has(track.url)));
        }
        return tracks.slice(0, limit);
      };
      // Track stubs ({id}) to full tracks, kept in the given order.
      const tracksForIds = async (ids, known = []) => {
        let tracks = known;
        const have = new Set(tracks.map((track) => track.playbackId));
        const missing = ids.filter((trackId) => !have.has('soundcloud:tracks:' + trackId));
        for (let index = 0; index < missing.length && index < 100; index += 50) {
          const page = await apiGet('/tracks', { ids: missing.slice(index, index + 50).join(',') });
          tracks = tracks.concat(normaliseEach(page.payload, page.context));
        }
        return tracksInOrder(ids, tracks).slice(0, 100);
      };
      // Mixes come from Home's selections; a mix is opened by its row number.
      const mixTrackIds = new Map();
      const selectionRank = (selection) => {
        const urn = String(selection && selection.urn || '');
        const order = ['your-moods', 'personalized-tracks', 'artist-stations', 'recently-played'];
        const rank = order.findIndex((name) => urn.includes(':' + name));
        return rank < 0 ? order.length : rank;
      };
      window.__omarchyMixes = async function() {
        if (!homeSelections.length) return JSON.stringify({ collections: [], needsHome: true });
        mixTrackIds.clear();
        const collections = [];
        const seen = new Set();
        const selections = homeSelections.slice().sort((a, b) => selectionRank(a) - selectionRank(b));
        for (const selection of selections) {
          const itemState = selection.items && typeof selection.items === 'object' ? selection.items : null;
          const items = Array.isArray(itemState) ? itemState
            : (itemState && Array.isArray(itemState.collection) ? itemState.collection : []);
          const group = String(selection.title || '').slice(0, 128);
          for (const item of items.slice(0, 30)) {
            if (!item || typeof item !== 'object' || collections.length >= 50) continue;
            const key = String(item.urn || item.id || '');
            if (!key || seen.has(key)) continue;
            const user = item.user && typeof item.user === 'object' ? item.user : {};
            const art = String(item.calculated_artwork_url || item.artwork_url || user.avatar_url || '').slice(0, 2048);
            const link = typeof item.permalink_url === 'string' && /^https:\/\/soundcloud\.com\//.test(item.permalink_url)
              ? item.permalink_url.slice(0, 2048) : 'https://soundcloud.com/discover';
            if (item.kind === 'system-playlist' && Array.isArray(item.tracks)) {
              const ids = item.tracks.slice(0, 100).map((entry) => positiveId(entry && entry.id)).filter(Boolean);
              if (!ids.length) continue;
              seen.add(key);
              mixTrackIds.set(collections.length + 1, ids);
              collections.push({
                kind: 'mix', id: collections.length + 1,
                title: String(item.title || item.short_title || 'Mix').slice(0, 512),
                subtitle: [group, ids.length + ' tracks'].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: art, url: link
              });
            } else if (item.kind === 'playlist' && positiveId(item.id)) {
              seen.add(key);
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'playlist', id: item.id,
                title: String(item.title || '').slice(0, 512) || 'Playlist',
                subtitle: [group, count ? count + ' tracks' : ''].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: art, url: link
              });
            }
          }
        }
        return JSON.stringify({ collections });
      };
      const positiveId = (value) => Number.isSafeInteger(value) && value > 0 ? value : 0;
      const tracksInOrder = (ids, tracks) => {
        const byUrn = new Map(tracks.filter((track) => track.playbackId).map((track) => [track.playbackId, track]));
        const ordered = [];
        for (const id of ids) {
          const track = byUrn.get('soundcloud:tracks:' + id);
          if (track) { ordered.push(track); byUrn.delete('soundcloud:tracks:' + id); }
        }
        return ordered.concat(Array.from(byUrn.values()));
      };
      window.__omarchySearch = async function(value, kind) {
        const query = String(value || '').trim().slice(0, 200);
        const type = ['tracks', 'playlists', 'users'].includes(kind) ? kind : 'tracks';
        if (!query) return JSON.stringify({ tracks: [], collections: [] });
        try {
          const { payload, context } = await apiGet('/search/' + type, { q: query, limit: 40 });
          if (type === 'tracks')
            return JSON.stringify({ tracks: normaliseEach(payload, context) });
          const items = payload && Array.isArray(payload.collection) ? payload.collection.slice(0, 40) : [];
          const collections = [];
          for (const item of items) {
            if (!item || typeof item !== 'object' || !positiveId(item.id)) continue;
            const user = item.user && typeof item.user === 'object' ? item.user : {};
            if (type === 'playlists' && (item.kind === 'playlist' || item.kind === undefined)) {
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'playlist', id: item.id,
                title: String(item.title || '').slice(0, 512),
                subtitle: [String(user.username || ''), count ? count + ' tracks' : ''].filter(Boolean).join('  •  ').slice(0, 256),
                artUrl: String(item.artwork_url || user.avatar_url || '').slice(0, 2048),
                url: String(item.permalink_url || '').slice(0, 2048)
              });
            } else if (type === 'users' && (item.kind === 'user' || item.kind === undefined)) {
              const count = positiveId(item.track_count);
              collections.push({
                kind: 'user', id: item.id,
                title: String(item.username || item.full_name || '').slice(0, 512),
                subtitle: (count ? count + ' tracks' : 'Artist').slice(0, 256),
                artUrl: String(item.avatar_url || '').slice(0, 2048),
                url: String(item.permalink_url || '').slice(0, 2048)
              });
            }
          }
          return JSON.stringify({ collections });
        } catch (error) {
          return JSON.stringify({ tracks: [], collections: [], error: failure(error) });
        }
      };
      window.__omarchyOpenCollection = async function(kind, value) {
        const id = positiveId(Number(value));
        if (!id || !['playlist', 'user', 'mix'].includes(kind))
          return JSON.stringify({ tracks: [], error: 'SoundCloud could not open this' });
        try {
          if (kind === 'mix') {
            const ids = mixTrackIds.get(id);
            if (!ids) throw new Error('SoundCloud mixes changed, open Mixes again');
            return JSON.stringify({ tracks: await tracksForIds(ids) });
          }
          if (kind === 'user') {
            return JSON.stringify({ tracks: await pagedTracks('/users/' + id + '/tracks', { limit: 50 }) });
          }
          const { payload, context } = await apiGet('/playlists/' + id, {});
          const entries = payload && Array.isArray(payload.tracks) ? payload.tracks.slice(0, 100) : [];
          const ids = entries.map((entry) => positiveId(entry && entry.id)).filter(Boolean);
          const embedded = normaliseEach({ collection: entries.filter((entry) => entry && entry.title) }, context);
          return JSON.stringify({ tracks: await tracksForIds(ids, embedded) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
      // Tracks SoundCloud relates to one track, for autoplay after a list ends.
      window.__omarchyRelated = async function(value) {
        const id = positiveId(Number(value));
        if (!id) return JSON.stringify({ tracks: [], error: 'SoundCloud could not find related tracks' });
        try {
          const { payload, context } = await apiGet('/tracks/' + id + '/related', { limit: 20 });
          return JSON.stringify({ tracks: normaliseEach(payload, context).slice(0, 20) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
      window.__omarchyLikes = async function() {
        try {
          const me = await apiGet('/me', {});
          const userId = positiveId(me.payload && me.payload.id);
          if (!userId) throw new Error('SoundCloud did not return your account');
          return JSON.stringify({ tracks: await pagedTracks('/users/' + userId + '/track_likes', { limit: 50 }, 100) });
        } catch (error) {
          return JSON.stringify({ tracks: [], error: failure(error) });
        }
      };
    })()"""


def api_observation_script() -> str:
    return r"""(() => {
      const paths = [];
      for (const name of performance.getEntriesByType('resource').slice(-500).map((entry) => entry.name)) {
        try {
          const candidate = new URL(name);
          if (candidate.hostname === 'api-v2.soundcloud.com' && paths.indexOf(candidate.pathname) < 0) {
            paths.push(candidate.pathname);
          }
        } catch (_) {}
        if (paths.length >= 32) break;
      }
      const cache = window.__omarchyApiTrackLists || Object.create(null);
      const lists = Object.create(null);
      for (const source of ['home', 'feed']) {
        const state = cache[source];
        if (!state) continue;
        lists[source] = {
          count: Array.isArray(state.tracks) ? state.tracks.length : 0,
          loading: state.loading === true,
          error: String(state.error || '').slice(0, 128)
        };
      }
      return {
        paths: paths.slice(0, 32),
        lists,
        playbackCount: typeof window.__omarchyPlaybackTrackCount === 'function'
          ? Number(window.__omarchyPlaybackTrackCount()) : 0
      };
    })()"""


def play_track_script(playback_id: str, request_id: int) -> str | None:
    if (not is_playback_id(playback_id) or isinstance(request_id, bool)
            or not isinstance(request_id, int) or not 0 <= request_id <= 2**31 - 1):
        return None
    target = json.dumps(playback_id)
    return rf"""(() => {{
      const playbackId = {target};
      return typeof window.__omarchyPlayApiTrack === 'function'
        && window.__omarchyPlayApiTrack(playbackId, {request_id});
    }})()"""


def status_script() -> str:
    return r"""(() => {
      const audio = document.querySelector('audio');
      const hooked = typeof window.__omarchyPageMediaState === 'function'
        ? window.__omarchyPageMediaState() : { present: false, playing: false, duration: 0, position: 0 };
      const playerPresent = !!audio || hooked.present === true;
      const playControl = document.querySelector('.playControls__play, .playControl');
      const controlSaysPause = !!playControl && (
        playControl.classList.contains('playing') ||
        /Pause/i.test(playControl.getAttribute('title') || '') ||
        /Pause/i.test(playControl.getAttribute('aria-label') || '')
      );
      const text = (...selectors) => {
        for (const selector of selectors) {
          const node = document.querySelector(selector);
          if (node && node.textContent.trim()) return node.textContent.trim().slice(0, 512);
        }
        return '';
      };
      const parseClock = (value) => {
        const parts = String(value || '').trim().split(':').map(Number);
        if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
        return parts.reduce((total, part) => total * 60 + part, 0);
      };
      const timelinePosition = parseClock(text(
        '.playbackTimeline__timePassed span[aria-hidden="true"]',
        '.playbackTimeline__timePassed'
      ));
      const timelineDuration = parseClock(text(
        '.playbackTimeline__duration span[aria-hidden="true"]',
        '.playbackTimeline__duration'
      ));
      let artUrl = '';
      for (const selector of [
        '.playbackSoundBadge__avatar img',
        '.playbackSoundBadge__avatar [style*="background-image"]',
        '.playbackSoundBadge__avatar .sc-artwork',
        '.playbackSoundBadge__avatar'
      ]) {
        const art = document.querySelector(selector);
        if (!art) continue;
        if (art.src) { artUrl = art.src.slice(0, 2048); break; }
        const background = art.style.backgroundImage || getComputedStyle(art).backgroundImage || '';
        const match = background.match(/url\(["']?(.*?)["']?\)/);
        if (match) { artUrl = match[1].slice(0, 2048); break; }
      }
      const pagePlaying = (!!audio && !audio.paused) || hooked.playing === true;
      return {
        playerPresent,
        pagePlaying,
        playing: playerPresent ? pagePlaying : false,
        title: playerPresent
          ? text('.playbackSoundBadge__titleLink span[aria-hidden="true"]', '.playbackSoundBadge__titleLink') : '',
        artist: playerPresent
          ? text('.playbackSoundBadge__lightLink', '.playbackSoundBadge__titleContextContainer a') : '',
        artUrl: playerPresent ? artUrl : '',
        loggedIn: !/^\/(signin|register)/.test(location.pathname) &&
          !!document.querySelector('.header__userNavButton, a[href*="/you/library"]'),
        url: location.href,
        duration: audio && isFinite(audio.duration) ? audio.duration
          : (playerPresent ? (hooked.duration || timelineDuration) : 0),
        position: audio && isFinite(audio.currentTime) ? audio.currentTime
          : (playerPresent ? (hooked.position || timelinePosition) : 0)
      };
    })()"""


def _bounded_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        return None
    return value


def _finite_number(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return minimum
    return value if minimum <= value <= maximum else minimum


def validate_status_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    title = _bounded_string(payload.get("title", ""), MAX_TITLE_BYTES)
    artist = _bounded_string(payload.get("artist", ""), MAX_ARTIST_BYTES)
    page_url = _bounded_string(payload.get("url", ""), MAX_URL_BYTES)
    art_url = _bounded_string(payload.get("artUrl", ""), MAX_URL_BYTES)
    duration = _finite_number(payload.get("duration", 0), 0, 60 * 60 * 24 * 7)
    position = _finite_number(payload.get("position", 0), 0, 60 * 60 * 24 * 7)
    error = _bounded_string(payload.get("error", ""), 128)
    player_present = payload.get("playerPresent", True)
    playback_state = payload.get("playbackState")
    if playback_state is None:
        playback_state = "playing" if payload.get("playing") is True else (
            "paused" if player_present is True else "idle"
        )
    if (None in (title, artist, page_url, art_url, duration, position, error)
            or not isinstance(player_present, bool)
            or playback_state not in {"idle", "resolving", "buffering", "playing", "paused", "error"}):
        return None
    if page_url and not is_soundcloud_uri(page_url):
        return None
    if art_url and not is_allowed_artwork_uri(art_url):
        art_url = ""
    return {
        "playerPresent": player_present,
        "playbackState": playback_state,
        "playing": payload.get("playing") is True,
        "playbackId": payload.get("playbackId") if is_playback_id(payload.get("playbackId")) else "",
        "title": title,
        "artist": artist,
        "artUrl": art_url,
        "loggedIn": payload.get("loggedIn") is True,
        "url": page_url,
        "duration": duration,
        "position": min(position, duration) if duration else position,
        "waveform": validate_waveform(payload.get("waveform", [])),
        **({"error": error} if error else {}),
    }


def validate_waveform(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_WAVEFORM_BARS:
        return []
    if not all(
        isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100
        for level in value
    ):
        return []
    return list(value)


def validate_tracks_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        return None
    incoming = payload["tracks"]
    if len(incoming) > MAX_TRACKS:
        return None
    tracks = []
    for item in incoming:
        if not isinstance(item, dict):
            return None
        title = _bounded_string(item.get("title", ""), MAX_TITLE_BYTES)
        artist = _bounded_string(item.get("artist", ""), MAX_ARTIST_BYTES)
        track_url = _bounded_string(item.get("url", ""), MAX_URL_BYTES)
        art_url = _bounded_string(item.get("artUrl", ""), MAX_URL_BYTES)
        playback_id = _bounded_string(item.get("playbackId", ""), 64)
        if (None in (title, artist, track_url, art_url, playback_id)
                or not title or not isinstance(track_url, str) or not is_soundcloud_uri(track_url)
                or (playback_id and not is_playback_id(playback_id))):
            return None
        if art_url and not is_allowed_artwork_uri(art_url):
            art_url = ""
        tracks.append(
            {
                "playbackId": playback_id,
                "title": title,
                "artist": artist,
                "url": track_url,
                "artUrl": art_url,
                "playCount": _bounded_integer(item.get("playCount"), 0, MAX_PLAY_COUNT),
                "durationMs": _bounded_integer(
                    item.get("durationMs"), 0, MAX_TRACK_DURATION_MS
                ),
            }
        )
    return {"tracks": tracks}


def sanitize_track_cache(payload: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        return {}
    sanitized = {}
    for source in ("home", "feed"):
        tracks = payload.get(source)
        if not isinstance(tracks, list) or not tracks:
            continue
        result = validate_tracks_payload({"tracks": tracks})
        validated_tracks = result.get("tracks") if result is not None else None
        if isinstance(validated_tracks, list) and validated_tracks:
            sanitized[source] = validated_tracks
    return sanitized


def _image_kind_and_dimensions(data: bytes) -> tuple[str, int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", width, height
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return "image/jpeg", width, height
            index += length
    return None


def _decode_chunked_body(encoded: bytes, maximum: int) -> bytes | None:
    position = 0
    decoded = bytearray()
    while True:
        line_end = encoded.find(b"\r\n", position)
        if line_end < 0 or line_end - position > 64:
            return None
        size_text = encoded[position:line_end].split(b";", 1)[0]
        try:
            size = int(size_text, 16)
        except ValueError:
            return None
        if size < 0 or len(decoded) + size > maximum:
            return None
        position = line_end + 2
        if size == 0:
            return bytes(decoded) if encoded[position:position + 2] == b"\r\n" else None
        end = position + size
        if end + 2 > len(encoded) or encoded[end:end + 2] != b"\r\n":
            return None
        decoded.extend(encoded[position:end])
        position = end + 2


def fetch_artwork_data_url(
    uri: str,
    max_bytes: int = MAX_ARTWORK_BYTES,
    max_pixels: int = MAX_ARTWORK_PIXELS,
    max_dimension: int = 2048,
) -> str:
    if not is_allowed_artwork_uri(uri):
        return ""
    parsed = urlparse(uri)
    host = parsed.hostname.lower().rstrip(".")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        request_target = target.encode("ascii").decode("ascii")
    except UnicodeEncodeError:
        return ""
    deadline = time.monotonic() + 5.0
    resolver = (
        "import ipaddress,json,socket,sys;"
        "values=[];"
        "[(values.append(x[4][0])) for x in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM) "
        "if x[4][0] not in values];"
        "print(json.dumps(values[:16]))"
    )
    try:
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-c", resolver, host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except OSError:
        return ""
    try:
        output, _ = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.2)
        return ""
    if process.returncode != 0 or len(output) > 4096:
        return ""
    try:
        resolved = json.loads(output.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(resolved, list) or len(resolved) > 16:
        return ""
    addresses = []
    for value in resolved:
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            return ""
        if not address.is_global:
            return ""
        if address.version == 6:
            addresses.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, (str(address), 443, 0, 0)))
        else:
            addresses.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (str(address), 443)))
    if not addresses:
        return ""

    context = ssl.create_default_context()
    for family, socktype, proto, sockaddr in addresses:
        raw = None
        tls = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(remaining)
            raw.connect(sockaddr)
            tls = context.wrap_socket(raw, server_hostname=host)
            raw = None
            request = (
                f"GET {request_target} HTTP/1.1\r\nHost: {host}\r\n"
                "Accept: image/png,image/jpeg\r\nConnection: close\r\n"
                "User-Agent: Omarchy-SoundCloud/0.1\r\n\r\n"
            ).encode("ascii")
            tls.sendall(request)
            received = bytearray()
            while b"\r\n\r\n" not in received:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(received) > 16384:
                    return ""
                tls.settimeout(remaining)
                chunk = tls.recv(min(4096, 16385 - len(received)))
                if not chunk:
                    return ""
                received.extend(chunk)
            header_bytes, body_prefix = bytes(received).split(b"\r\n\r\n", 1)
            try:
                lines = header_bytes.decode("iso-8859-1").split("\r\n")
                status_parts = lines[0].split(" ", 2)
                status = int(status_parts[1])
                headers = {}
                for line in lines[1:]:
                    name, value = line.split(":", 1)
                    headers[name.lower().strip()] = value.strip()
            except (UnicodeDecodeError, ValueError, IndexError):
                return ""
            if status != 200:
                continue
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/png", "image/jpeg"}:
                continue
            length = headers.get("content-length")
            transfer_encoding = headers.get("transfer-encoding", "").lower()
            if transfer_encoding:
                if transfer_encoding != "chunked" or length is not None:
                    continue
                encoded = bytearray(body_prefix)
                encoded_limit = max_bytes + 65536
                data = _decode_chunked_body(encoded, max_bytes)
                while data is None and len(encoded) <= encoded_limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    tls.settimeout(remaining)
                    chunk = tls.recv(min(16384, encoded_limit + 1 - len(encoded)))
                    if not chunk:
                        return ""
                    encoded.extend(chunk)
                    data = _decode_chunked_body(encoded, max_bytes)
                if data is None:
                    return ""
            else:
                if length is None or not length.isdigit() or int(length) > max_bytes:
                    continue
                expected = int(length)
                if len(body_prefix) > expected:
                    body_prefix = body_prefix[:expected]
                chunks = [body_prefix]
                size = len(body_prefix)
                while size < expected:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    tls.settimeout(remaining)
                    chunk = tls.recv(min(16384, expected - size))
                    if not chunk:
                        return ""
                    chunks.append(chunk)
                    size += len(chunk)
                data = b"".join(chunks)
            details = _image_kind_and_dimensions(data)
            if details is None:
                return ""
            mime, width, height = details
            if (
                width <= 0
                or height <= 0
                or width > max_dimension
                or height > max_dimension
                or width * height > max_pixels
            ):
                return ""
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        except (OSError, ssl.SSLError):
            continue
        finally:
            if tls is not None:
                try:
                    tls.close()
                except OSError:
                    pass
            if raw is not None:
                raw.close()
    return ""


class ArtworkCache:
    def __init__(self, maximum_entries: int = 8, maximum_pending: int = 8, fetcher=None):
        self.maximum_entries = maximum_entries
        self.maximum_pending = maximum_pending
        self.fetcher = fetcher
        self.values: OrderedDict[str, str] = OrderedDict()
        self.inflight: set[str] = set()
        self.waiters: dict[str, list] = {}
        self.lock = threading.Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="soundcloud-artwork"
        )

    def get(self, uri: str, on_ready) -> str:
        if not uri:
            return ""
        with self.lock:
            if uri in self.values:
                self.values.move_to_end(uri)
                return self.values[uri]
            if uri in self.inflight:
                self.waiters.setdefault(uri, []).append(lambda _value: on_ready())
                return ""
            if len(self.inflight) >= self.maximum_pending:
                return ""
            self.inflight.add(uri)
            self.waiters[uri] = [lambda _value: on_ready()]

        self._submit(uri)
        return ""

    def request(self, uri: str, callback) -> None:
        if not uri:
            callback("")
            return
        with self.lock:
            if uri in self.values:
                self.values.move_to_end(uri)
                value = self.values[uri]
            else:
                value = None
                if uri in self.inflight:
                    self.waiters.setdefault(uri, []).append(callback)
                    return
                if len(self.inflight) >= self.maximum_pending:
                    callback("")
                    return
                self.inflight.add(uri)
                self.waiters[uri] = [callback]
        if value is not None:
            callback(value)
            return
        self._submit(uri)

    def _submit(self, uri: str) -> None:

        def worker():
            try:
                fetcher = self.fetcher or fetch_artwork_data_url
                value = fetcher(uri)
            except Exception:
                value = ""
            finally:
                with self.lock:
                    self.inflight.discard(uri)
                    waiters = self.waiters.pop(uri, [])
                    self.values[uri] = value
                    self.values.move_to_end(uri)
                    while len(self.values) > self.maximum_entries:
                        self.values.popitem(last=False)
                for waiter in waiters:
                    waiter(value)

        self.executor.submit(worker)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def command_script(action: str) -> str | None:
    scripts = {
        "play-pause": r"""(() => {
          const button = document.querySelector(
            '.playControls__play, .playControl, button[title="Play"], button[title="Pause"], button[aria-label="Play"], button[aria-label="Pause"]'
          );
          if (button) { button.click(); return true; }
          const audio = document.querySelector('audio');
          if (!audio) return false;
          if (audio.paused) { audio.play(); } else { audio.pause(); }
          return true;
        })()""",
        "next": r"""(() => {
          const button = document.querySelector('.skipControl__next, button[title="Next"], button[aria-label="Next"]');
          if (!button) return false; button.click(); return true;
        })()""",
        "previous": r"""(() => {
          const button = document.querySelector('.skipControl__previous, button[title="Previous"], button[aria-label="Previous"]');
          if (!button) return false; button.click(); return true;
        })()""",
        "pause-page": r"""(() => {
          if (typeof window.__omarchyPausePage === 'function') window.__omarchyPausePage();
          return true;
        })()""",
    }
    return scripts.get(action)


# These run through call_async_javascript_function, which awaits the promise.
PAGE_FUNCTION_MISSING = "return JSON.stringify({ tracks: [], collections: [], error: 'SoundCloud is still loading' });"
SEARCH_TRACKS_FUNCTION = f"""
if (typeof window.__omarchySearch !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchySearch(query, kind);
"""
OPEN_COLLECTION_FUNCTION = f"""
if (typeof window.__omarchyOpenCollection !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyOpenCollection(kind, id);
"""
RELATED_TRACKS_FUNCTION = f"""
if (typeof window.__omarchyRelated !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyRelated(id);
"""
MIXES_FUNCTION = f"""
if (typeof window.__omarchyMixes !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyMixes();
"""
LIKED_TRACKS_FUNCTION = f"""
if (typeof window.__omarchyLikes !== 'function') {PAGE_FUNCTION_MISSING}
return await window.__omarchyLikes();
"""
MAX_SEARCH_QUERY_BYTES = 200
SEARCH_KINDS = {"search": "tracks", "search-playlists": "playlists", "search-users": "users"}
# Lists a track can be played from; each becomes the queue for next/previous.
QUEUE_SOURCES = ("home", "feed", "likes", "search", "collection")
MAX_QUEUE = 100
PLAYER_STATE_BYTES = 256 * 1024


def search_from_command(command: str) -> tuple[str, str] | None:
    """Return (kind, query) for search:, search-playlists:, and search-users:."""
    prefix, separator, query = command.partition(":")
    if not separator or prefix not in SEARCH_KINDS:
        return None
    query = query.strip()
    if (not query or len(query.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in query)):
        return None
    return SEARCH_KINDS[prefix], query


def search_query_from_command(command: str) -> str | None:
    found = search_from_command(command)
    return found[1] if found and command.startswith("search:") else None


def collection_from_command(command: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"open:(playlist|user|mix):([1-9][0-9]{0,15})", command)
    return (match.group(1), int(match.group(2))) if match else None


def extend_queue(queue: list[dict[str, object]], index: int,
                 extra: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Append unseen playable tracks, dropping played ones so the queue stays bounded."""
    known = {track.get("playbackId") for track in queue}
    added = []
    for track in extra:
        playback_id = track.get("playbackId")
        if is_playback_id(playback_id) and playback_id not in known:
            known.add(playback_id)
            added.append(track)
    combined = list(queue) + added
    overflow = max(0, len(combined) - MAX_QUEUE)
    overflow = min(overflow, max(0, index))
    return combined[overflow:][:MAX_QUEUE], index - overflow


def shuffle_from_command(command: str) -> str | None:
    """shuffle:<source> plays that list in a random order."""
    source = command.removeprefix("shuffle:") if command.startswith("shuffle:") else ""
    return source if source in QUEUE_SOURCES else None


def shuffled_tracks(tracks: list[dict[str, object]], current: str = "") -> list[dict[str, object]]:
    """Playable tracks in random order, never starting on the current track."""
    playable = [track for track in tracks if is_playback_id(track.get("playbackId"))][:MAX_QUEUE]
    secrets.SystemRandom().shuffle(playable)
    if len(playable) > 1 and playable[0].get("playbackId") == current:
        playable.append(playable.pop(0))
    return playable


def queued_play_from_command(command: str) -> tuple[str, str] | None:
    """play-in:<source>:<playback id> plays a track and queues its list."""
    match = re.fullmatch(r"play-in:([a-z]+):(soundcloud:tracks:[1-9][0-9]*)", command)
    if not match or match.group(1) not in QUEUE_SOURCES:
        return None
    return match.group(1), match.group(2)


def volume_from_command(command: str) -> float | None:
    if not command.startswith("volume:"):
        return None
    try:
        value = float(command.removeprefix("volume:"))
    except ValueError:
        return None
    return max(0.0, min(1.0, value)) if math.isfinite(value) else None


def volume_script(volume: float) -> str:
    return rf"""(() => typeof window.__omarchySetVolume === 'function'
      && window.__omarchySetVolume({max(0.0, min(1.0, float(volume)))!r}))()"""


def queue_step(length: int, index: int, step: int) -> int | None:
    """Index of the next or previous queued track, or None at either end."""
    target = index + step
    return target if 0 <= index < length and 0 <= target < length else None


def validate_collections_payload(payload: object) -> list[dict[str, object]] | None:
    items = payload.get("collections") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > 50:
        return None
    collections = []
    for item in items:
        if not isinstance(item, dict):
            return None
        kind = item.get("kind")
        identifier = item.get("id")
        title = _bounded_string(item.get("title", ""), MAX_TITLE_BYTES)
        subtitle = _bounded_string(item.get("subtitle", ""), MAX_ARTIST_BYTES)
        url = _bounded_string(item.get("url", ""), MAX_URL_BYTES)
        art_url = _bounded_string(item.get("artUrl", ""), MAX_URL_BYTES)
        if (kind not in {"playlist", "user", "mix"} or isinstance(identifier, bool)
                or not isinstance(identifier, int) or not 0 < identifier < 10**16
                or None in (title, subtitle, url, art_url) or not title
                or not is_soundcloud_uri(url)):
            return None
        collections.append({
            "kind": kind,
            "id": identifier,
            "title": title,
            "subtitle": subtitle,
            "url": url,
            "artUrl": art_url if art_url and is_allowed_artwork_uri(art_url) else "",
        })
    return collections


def sanitize_player_state(payload: object) -> dict[str, object]:
    """The remembered volume, last track, and its queue, all re-validated."""
    if not isinstance(payload, dict):
        return {}
    state: dict[str, object] = {}
    volume = _finite_number(payload.get("volume"), 0, 1)
    if volume is not None:
        state["volume"] = volume
    track_result = validate_tracks_payload({"tracks": [payload.get("track")]}) if payload.get("track") else None
    if track_result and is_playback_id(track_result["tracks"][0].get("playbackId")):
        state["track"] = track_result["tracks"][0]
        state["position"] = _finite_number(payload.get("position", 0), 0, 60 * 60 * 24 * 7) or 0.0
        queue_result = validate_tracks_payload({"tracks": payload.get("queue", [])})
        queue = [
            track for track in (queue_result["tracks"] if queue_result else [])
            if is_playback_id(track.get("playbackId"))
        ][:MAX_QUEUE]
        source = payload.get("queueSource")
        index = payload.get("queueIndex")
        if (queue and source in QUEUE_SOURCES and isinstance(index, int)
                and not isinstance(index, bool) and 0 <= index < len(queue)
                and queue[index]["playbackId"] == state["track"]["playbackId"]):
            state["queue"], state["queueSource"], state["queueIndex"] = queue, source, index
    return state


def seek_script(ratio: float) -> str:
    clamped = max(0.0, min(1.0, float(ratio)))
    return rf"""(() => {{
      const ratio = {clamped!r};
      const audio = document.querySelector('audio');
      if (audio && isFinite(audio.duration) && audio.duration > 0) {{
        audio.currentTime = audio.duration * ratio;
        return true;
      }}
      const timeline = document.querySelector(
        '.playbackTimeline__progressWrapper, .playbackTimeline__progressBackground, [role="slider"]'
      );
      if (!timeline) return false;
      const bounds = timeline.getBoundingClientRect();
      const clientX = bounds.left + bounds.width * ratio;
      const eventOptions = {{ bubbles: true, cancelable: true, clientX, clientY: bounds.top + bounds.height / 2 }};
      for (const type of ['mousedown', 'mouseup', 'click'])
        timeline.dispatchEvent(new MouseEvent(type, eventOptions));
      return true;
    }})()"""


GSTREAMER_PACKAGE_ELEMENTS = (
    ("gst-plugins-good", (
        "playbin3", "appsrc", "mpegaudioparse", "mpg123audiodec",
        "audioconvert", "audioresample", "autoaudiosink", "hlsdemux2", "souphttpsrc",
    )),
    ("gst-libav", ("avdec_aac",)),
)


def missing_gstreamer_packages(find) -> list[str]:
    return [
        package for package, elements in GSTREAMER_PACKAGE_ELEMENTS
        if not all(find(name) for name in elements)
    ]


def install_hint(packages: list[str]) -> str:
    return f"Missing {', '.join(packages)}. Run: omarchy pkg add {' '.join(packages)}"


def dependency_check() -> dict[str, object]:
    result: dict[str, object] = {
        "python_gobject": False,
        "gtk": "3.0",
        "webkit2": "4.1",
        "gstreamer_autoaudiosink": False,
        "gstreamer_aac_decoder": False,
        "gstreamer_playback_elements": False,
        "ok": False,
    }
    try:
        import gi
    except ImportError:
        result["error"] = install_hint(["python-gobject"])
        return result
    result["python_gobject"] = True
    missing_libraries = []
    for namespace, version, package in (
        ("Gtk", "3.0", "gtk3"), ("WebKit2", "4.1", "webkit2gtk-4.1"), ("Gst", "1.0", "gstreamer"),
    ):
        try:
            gi.require_version(namespace, version)
        except ValueError:
            missing_libraries.append(package)
    if missing_libraries:
        result["error"] = install_hint(missing_libraries)
        return result
    try:
        from gi.repository import Gst, Gtk, WebKit2  # noqa: F401

        Gst.init(None)
        find = lambda name: bool(Gst.ElementFactory.find(name))
        result["gstreamer_autoaudiosink"] = find("autoaudiosink")
        result["gstreamer_aac_decoder"] = find("avdec_aac")
        result["gstreamer_playback_elements"] = all(
            find(name) for name in GSTREAMER_PACKAGE_ELEMENTS[0][1]
        )
        missing_packages = missing_gstreamer_packages(find)
        if missing_packages:
            result["error"] = install_hint(missing_packages)
        result["ok"] = not missing_packages
    except (ImportError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def _load_gi():
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Soup", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gst, Gtk, Soup, WebKit2

    return Gio, GLib, Gst, Gtk, Soup, WebKit2


def validate_stream_uri(uri: object) -> str | None:
    value = _bounded_string(uri, 8192)
    if value is None:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    host = parsed.hostname.lower().rstrip(".")
    if not (host.endswith(".sndcdn.com") or host.endswith(".soundcloud.cloud")):
        return None
    return value


class _NoMediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_media_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)


def _set_media_response_timeout(response, timeout: float) -> None:
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "settimeout"):
            candidate.settimeout(timeout)
            return


def fetch_private_media(
    uri: str,
    maximum: int,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> bytes:
    if cancelled():
        raise concurrent.futures.CancelledError
    remaining = 15.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("media operation deadline exceeded")
    validated = validate_stream_uri(uri)
    if validated is None:
        raise ValueError("invalid media URL")
    request = Request(validated, headers={"User-Agent": "Mozilla/5.0"})
    with _media_opener.open(request, timeout=min(15.0, remaining)) as response:
        if cancelled():
            raise concurrent.futures.CancelledError
        if validate_stream_uri(response.geturl()) is None:
            raise ValueError("invalid media response URL")
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise ValueError("media response too large")
        data = bytearray()
        while len(data) <= maximum:
            if cancelled():
                raise concurrent.futures.CancelledError
            remaining = 15.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("media operation deadline exceeded")
            _set_media_response_timeout(response, min(15.0, remaining))
            read = getattr(response, "read1", None)
            chunk_size = min(64 * 1024, maximum + 1 - len(data))
            part = cast(
                bytes,
                read(chunk_size) if callable(read) else response.read(min(1, chunk_size)),
            )
            if not part:
                break
            data.extend(part)
    if cancelled():
        raise concurrent.futures.CancelledError
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("media operation deadline exceeded")
    if len(data) > maximum:
        raise ValueError("media response too large")
    return bytes(data)


def hls_segment_urls(
    uri: str,
    depth: int = 0,
    *,
    deadline: float | None = None,
    cancelled=lambda: False,
) -> list[str]:
    if depth > 2:
        raise ValueError("nested HLS playlist")
    text = fetch_private_media(
        uri, 512 * 1024, deadline=deadline, cancelled=cancelled
    ).decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    media_lines = [line for line in lines if not line.startswith("#")]
    if not media_lines:
        raise ValueError("empty HLS playlist")
    resolved = [urljoin(uri, line) for line in media_lines]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return hls_segment_urls(
            resolved[0], depth + 1, deadline=deadline, cancelled=cancelled
        )
    if len(resolved) > 4096 or any(validate_stream_uri(value) is None for value in resolved):
        raise ValueError("invalid HLS segments")
    return resolved


_api_opener = build_opener(ProxyHandler({}), _NoMediaRedirect)


def soundcloud_api_request(method: str, path: str, token: str, maximum: int = 256 * 1024) -> object:
    """Call SoundCloud's private web API directly with the signed-in session token."""
    if not path.startswith("/") or method not in {"GET", "PUT", "DELETE"}:
        raise ValueError("invalid API request")
    request = Request(
        "https://api-v2.soundcloud.com" + path,
        method=method,
        headers={
            "Authorization": "OAuth " + token,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with _api_opener.open(request, timeout=10.0) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("API response too large")
    return json.loads(data) if data.strip() else None


class TrackLikes:
    """The signed-in account's liked tracks, read over the private API.

    Writes go through `write` because SoundCloud's bot protection rejects
    like/unlike requests that do not come from its own web page.
    """

    RETRY_SECONDS = 60.0
    MAX_LIKE_PAGES = 50

    def __init__(self, load_token, write, request=soundcloud_api_request, spawn=None):
        self.load_token = load_token
        self.write = write
        self.request = request
        self.spawn = spawn or (lambda target: threading.Thread(target=target, daemon=True).start())
        self.lock = threading.Lock()
        self.token: str | None = None
        self.user_id: int | None = None
        self.liked_ids: set[int] | None = None
        self.loading = False
        self.last_attempt = -math.inf

    def _session(self) -> str:
        token = self.load_token()
        if not token:
            raise PermissionError("not signed in")
        if token != self.token:
            self.token, self.user_id, self.liked_ids = token, None, None
        if self.user_id is None:
            me = self.request("GET", "/me", token)
            user_id = me.get("id") if isinstance(me, dict) else None
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise ValueError("invalid account response")
            self.user_id = user_id
        if self.liked_ids is None:
            liked_ids = set()
            path = "/me/track_likes/ids?limit=200"
            # SoundCloud pages these 200 at a time; stop at 10,000 likes.
            for _page in range(self.MAX_LIKE_PAGES):
                payload = self.request("GET", path, token, 1024 * 1024)
                ids = payload.get("collection") if isinstance(payload, dict) else None
                if not isinstance(ids, list):
                    raise ValueError("invalid likes response")
                liked_ids.update(
                    value for value in ids
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0
                )
                next_href = payload.get("next_href")
                parsed = urlparse(next_href) if isinstance(next_href, str) else None
                if (not ids or parsed is None or parsed.scheme != "https"
                        or parsed.netloc != "api-v2.soundcloud.com"
                        or parsed.path != "/me/track_likes/ids"):
                    break
                path = parsed.path + "?" + parsed.query
            self.liked_ids = liked_ids
        return token

    def is_liked(self, track_id: int) -> bool | None:
        liked_ids = self.liked_ids
        return None if liked_ids is None else track_id in liked_ids

    def load_in_background(self, on_loaded) -> None:
        now = time.monotonic()
        if self.liked_ids is not None or self.loading or now - self.last_attempt < self.RETRY_SECONDS:
            return
        self.loading = True
        self.last_attempt = now

        def worker():
            try:
                with self.lock:
                    self._session()
                on_loaded()
            except (OSError, ValueError):
                pass
            finally:
                self.loading = False

        self.spawn(worker)

    def set_liked(self, track_id: int, liked: bool) -> None:
        with self.lock:
            token = self._session()
            self.write(cast(int, self.user_id), track_id, liked, token)
            liked_ids = cast(set[int], self.liked_ids)
            if liked:
                liked_ids.add(track_id)
            else:
                liked_ids.discard(track_id)


BOT_CHECK_MESSAGE = "SoundCloud wants a quick check. Solve it in the SoundCloud window, then tap the heart again."


class BotCheckRequired(OSError):
    """SoundCloud's bot protection answered with a captcha instead of the API."""


# Runs in the SoundCloud page so its DataDome tag, which watches
# /users/*/track_likes/*, vouches for the request and shows any captcha.
LIKE_TRACK_FUNCTION = r"""
  if (!Number.isSafeInteger(userId) || userId <= 0 || !Number.isSafeInteger(trackId) || trackId <= 0
      || (method !== 'PUT' && method !== 'DELETE') || typeof token !== 'string') return 'failed';
  const response = await fetch(
    'https://api-v2.soundcloud.com/users/' + userId + '/track_likes/' + trackId,
    {
      method,
      headers: { Authorization: 'OAuth ' + token },
      credentials: 'include',
      cache: 'no-store',
      redirect: 'error'
    }
  );
  if (response.ok) return 'ok';
  if (response.status !== 403) return 'failed';
  try {
    const text = await response.text();
    const url = new URL(text.length <= 4096 ? JSON.parse(text).url : '');
    return url.protocol === 'https:' && url.hostname === 'geo.captcha-delivery.com'
      ? 'bot-check' : 'failed';
  } catch (_) {
    return 'failed';
  }
"""


class GstPlayback:
    def __init__(self, Gst, on_change, schedule=lambda callback, *args: callback(*args)):
        self.Gst = Gst
        Gst.init(None)
        self.current = None
        self.on_change = on_change
        self.schedule = schedule
        self.active = False
        self.playing = False
        self.state = "idle"
        self.has_started = False
        self.metadata: dict[str, object] = {}
        self.error = ""
        self.generation = 0
        self.volume = 1.0
        self.ended = False
        self.bus_watches = {}
        self.hls_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=HLS_MAX_WORKERS,
            thread_name_prefix="soundcloud-hls",
        )
        self.hls_slots = threading.BoundedSemaphore(HLS_MAX_WORKERS)

    def _watch_bus(self, pipeline, generation: int) -> None:
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        handler = bus.connect("message", self._message, pipeline, generation)
        self.bus_watches[id(pipeline)] = (bus, handler)

    def _message(self, _bus, message, pipeline, generation):
        if pipeline is not self.current or generation != self.generation:
            return
        if message.type == self.Gst.MessageType.ERROR:
            self.stop("SoundCloud could not play this track")
        elif message.type == self.Gst.MessageType.EOS:
            self.playing = False
            self.state = "paused"
            self.ended = True
            self.on_change()
        elif message.type == self.Gst.MessageType.STATE_CHANGED and message.src == self.current:
            _old, current, _pending = message.parse_state_changed()
            self.playing = current == self.Gst.State.PLAYING
            if self.playing:
                self.has_started = True
                self.state = "playing"
            elif current == self.Gst.State.PAUSED:
                self.state = "paused" if self.has_started else "buffering"
            self.on_change()

    def _stop_current(self) -> None:
        self.generation += 1
        previous = self.current
        self.current = None
        if previous is None:
            return
        watch = self.bus_watches.pop(id(previous), None)
        if watch:
            bus, handler = watch
            bus.disconnect(handler)
            bus.remove_signal_watch()
        previous.set_state(self.Gst.State.NULL)

    def stop(self, error: str = "") -> None:
        self._stop_current()
        self.active = False
        self.playing = False
        self.has_started = False
        # Keep the failed track's id so the popup can show the error on its row.
        self.metadata = (
            {"playbackId": self.metadata.get("playbackId", "")} if error else {}
        )
        self.error = error[:128]
        self.state = "error" if self.error else "idle"
        self.on_change()

    def set_volume(self, volume: float) -> bool:
        self.volume = max(0.0, min(1.0, float(volume)))
        if self.current is None:
            return True
        element = self.current.get_by_name("volume") if hasattr(self.current, "get_by_name") else None
        target = element if element is not None else self.current
        try:
            target.set_property("volume", self.volume)
        except (TypeError, ValueError):
            return False
        return True

    def begin(self, metadata: dict[str, object]) -> int:
        self._stop_current()
        self.ended = False
        self.active = False
        self.playing = False
        self.has_started = False
        self.metadata = dict(metadata)
        self.error = ""
        self.state = "resolving"
        self.on_change()
        return self.generation

    def play(
        self,
        uri: str,
        metadata: dict[str, object],
        protocol: str = "",
        mime_type: str = "",
    ) -> bool:
        self.metadata = dict(metadata)
        self.error = ""
        self.active = True
        self.playing = False
        self.has_started = False
        self.ended = False
        self.state = "buffering"
        if protocol == "hls" and mime_type == "audio/mpeg":
            return self._play_hls(uri)
        pipeline = self.Gst.ElementFactory.make(
            "playbin3", f"omarchy-soundcloud-player-{self.generation}"
        )
        if pipeline is None:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        self.current = pipeline
        self._watch_bus(pipeline, self.generation)
        pipeline.set_property("uri", uri)
        pipeline.set_property("volume", self.volume)
        result = pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            self.active = False
            self.error = "SoundCloud could not play this track"
            self.state = "error"
            return False
        return True

    def _play_hls(self, uri: str) -> bool:
        try:
            pipeline = self.Gst.parse_launch(
                "appsrc name=source format=time is-live=false block=true "
                "caps=audio/mpeg,mpegversion=1,layer=3 ! mpegaudioparse ! "
                "mpg123audiodec ! audioconvert ! audioresample ! "
                f"volume name=volume volume={self.volume!r} ! autoaudiosink"
            )
        except Exception:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        source = pipeline.get_by_name("source")
        if source is None:
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        self.current = pipeline
        self._watch_bus(pipeline, self.generation)
        generation = self.generation
        if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            self._stop_current()
            self.active = False
            self.error = "SoundCloud could not initialize audio playback"
            self.state = "error"
            return False
        if not self.hls_slots.acquire(blocking=False):
            self._stop_current()
            self.active = False
            self.error = "SoundCloud playback is busy"
            self.state = "error"
            return False
        self.hls_executor.submit(
            self._feed_hls_bounded,
            generation,
            source,
            uri,
            time.monotonic() + HLS_OPERATION_SECONDS,
        )
        return True

    def _feed_hls_bounded(self, generation: int, source, uri: str, deadline: float) -> None:
        try:
            self._feed_hls(generation, source, uri, deadline)
        finally:
            self.hls_slots.release()

    def _feed_hls(self, generation: int, source, uri: str, deadline: float) -> None:
        try:
            total_bytes = 0
            cancelled = lambda: generation != self.generation
            for segment in hls_segment_urls(uri, deadline=deadline, cancelled=cancelled):
                if generation != self.generation:
                    return
                data = fetch_private_media(
                    segment,
                    4 * 1024 * 1024,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                total_bytes += len(data)
                if total_bytes > 128 * 1024 * 1024:
                    raise ValueError("track stream too large")
                buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
                buffer.fill(0, data)
                if source.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
                    raise RuntimeError("audio pipeline rejected data")
            if generation == self.generation:
                source.emit("end-of-stream")
        except (OSError, RuntimeError, TimeoutError, UnicodeError, ValueError,
                concurrent.futures.CancelledError):
            if generation == self.generation:
                self.schedule(self._hls_failed, generation)

    def _hls_failed(self, generation: int) -> bool:
        if generation == self.generation:
            self.stop("SoundCloud could not stream this track")
        return False

    def toggle(self) -> bool:
        if not self.active:
            return False
        target = self.Gst.State.PAUSED if self.playing else self.Gst.State.PLAYING
        return self.current.set_state(target) != self.Gst.StateChangeReturn.FAILURE

    def pause(self) -> bool:
        if not self.active or self.current is None:
            return False
        return self.current.set_state(self.Gst.State.PAUSED) != self.Gst.StateChangeReturn.FAILURE

    def seek(self, ratio: float) -> bool:
        if not self.active:
            return False
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        if duration <= 0:
            return False
        return bool(self.current.seek_simple(
            self.Gst.Format.TIME,
            self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.KEY_UNIT,
            int(duration * max(0.0, min(1.0, ratio)) * self.Gst.SECOND),
        ))

    def _query_time(self, method: str) -> float:
        try:
            ok, value = getattr(self.current, method)(self.Gst.Format.TIME)
            return value / self.Gst.SECOND if ok and value >= 0 else 0.0
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def status(self) -> dict[str, object]:
        if not self.active:
            return {
                "playerPresent": False,
                "playbackState": self.state,
                "playing": False,
                "playbackId": str(self.metadata.get("playbackId", "")),
                "title": str(self.metadata.get("title", "")),
                "artist": str(self.metadata.get("artist", "")),
                "artUrl": "",
                "duration": 0.0,
                "position": 0.0,
                "waveform": [],
                **({"error": self.error} if self.error else {}),
            }
        duration = self._query_time("query_duration")
        if duration <= 0:
            duration = float(self.metadata.get("durationMs", 0) or 0) / 1000
        return {
            "playerPresent": True,
            "playbackState": self.state,
            "playing": self.playing,
            "playbackId": str(self.metadata.get("playbackId", "")),
            "title": str(self.metadata.get("title", "")),
            "artist": str(self.metadata.get("artist", "")),
            "artUrl": str(self.metadata.get("artUrl", "")),
            "duration": duration,
            "position": self._query_time("query_position"),
            "waveform": list(self.metadata.get("waveform", [])),
            **({"error": self.error} if self.error else {}),
        }

    def close(self) -> None:
        self._stop_current()
        self.hls_executor.shutdown(wait=False, cancel_futures=True)


class ClientConnection:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.send_lock = threading.Lock()

    def send(self, payload: object) -> bool:
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = b'{"ok":false,"error":"response exceeds security limit"}\n'
        try:
            with self.send_lock:
                previous_timeout = self.connection.gettimeout()
                self.connection.settimeout(SEND_TIMEOUT_SECONDS)
                self.connection.sendall(encoded)
                self.connection.settimeout(previous_timeout)
            return True
        except OSError:
            return False

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class StatusSubscribers:
    def __init__(self):
        self.connections: set[ClientConnection] = set()
        self.lock = threading.Lock()

    def add(self, connection: ClientConnection) -> None:
        with self.lock:
            self.connections.add(connection)

    def remove(self, connection: ClientConnection) -> None:
        with self.lock:
            self.connections.discard(connection)

    def __bool__(self) -> bool:
        with self.lock:
            return bool(self.connections)

    def count(self) -> int:
        with self.lock:
            return len(self.connections)

    def publish(self, state: dict[str, object]) -> None:
        self.publish_message({"type": "status", "state": state})

    def publish_message(self, payload: dict[str, object]) -> None:
        with self.lock:
            connections = list(self.connections)
        failed = [
            connection
            for connection in connections
            if not connection.send(payload)
        ]
        if failed:
            with self.lock:
                for connection in failed:
                    self.connections.discard(connection)


def send_response(
    connection: ClientConnection,
    payload: dict[str, object],
    request_id: object | None = None,
) -> bool:
    response = dict(payload)
    if request_id is not None:
        response = {"type": "response", "id": request_id, **response}
    return connection.send(response)


class ControlServer(threading.Thread):
    def __init__(self, socket_path: Path, dispatcher, on_disconnect=None):
        super().__init__(name="soundcloud-control", daemon=True)
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self.on_disconnect = on_disconnect
        self.server: socket.socket | None = None
        self.connections: set[ClientConnection] = set()
        self.connections_lock = threading.Lock()
        self.lock_fd: int | None = None
        self.bound_identity: tuple[int, int] | None = None

    def _serve_client(self, client: ClientConnection) -> None:
        buffer = b""
        last_input = time.monotonic()
        client.connection.settimeout(CLIENT_TIMEOUT_SECONDS)
        try:
            while True:
                try:
                    chunk = client.connection.recv(MAX_REQUEST_BYTES + 1)
                except socket.timeout:
                    if buffer or time.monotonic() - last_input >= CLIENT_IDLE_SECONDS:
                        break
                    continue
                if not chunk:
                    break
                last_input = time.monotonic()
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if len(raw) > MAX_REQUEST_BYTES:
                        return
                    if not raw.strip():
                        continue
                    try:
                        request = json.loads(raw.decode())
                        if not isinstance(request, dict):
                            raise TypeError
                        command = str(request.get("command", ""))
                        request_id = request.get("id")
                        if len(command.encode("utf-8")) > MAX_URL_BYTES:
                            raise ValueError
                        if request_id is not None and (
                            isinstance(request_id, bool)
                            or not isinstance(request_id, int)
                            or not 0 <= request_id <= 2**31 - 1
                        ):
                            raise ValueError
                    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                        send_response(client, {"ok": False, "error": "invalid request"})
                        continue
                    self.dispatcher(command, client, request_id)
                if len(buffer) > MAX_REQUEST_BYTES:
                    break
        except OSError:
            pass
        finally:
            with self.connections_lock:
                self.connections.discard(client)
            if self.on_disconnect:
                self.on_disconnect(client)
            client.close()

    def run(self) -> None:
        try:
            parent = self.socket_path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077
            ):
                return
            lock_path = self.socket_path.with_suffix(".lock")
            self.lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                existing = self.socket_path.stat(follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
                    return
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.socket_path))
                    return
                except OSError:
                    self.socket_path.unlink()
                finally:
                    probe.close()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous_umask = os.umask(0o077)
            try:
                self.server.bind(str(self.socket_path))
            finally:
                os.umask(previous_umask)
            bound = self.socket_path.stat(follow_symlinks=False)
            self.bound_identity = (bound.st_dev, bound.st_ino)
            self.server.listen(8)
            while True:
                connection, _ = self.server.accept()
                try:
                    _pid, uid, _gid = struct.unpack(
                        "3i",
                        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
                    )
                except (OSError, struct.error):
                    connection.close()
                    continue
                if uid != os.geteuid():
                    connection.close()
                    continue
                client = ClientConnection(connection)
                with self.connections_lock:
                    if len(self.connections) >= MAX_CLIENTS:
                        client.close()
                        continue
                    self.connections.add(client)
                threading.Thread(
                    target=self._serve_client,
                    args=(client,),
                    name="soundcloud-client",
                    daemon=True,
                ).start()
        except OSError:
            return
        finally:
            self._unlink_owned_socket()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None

    def _unlink_owned_socket(self) -> None:
        if self.bound_identity is None:
            return
        try:
            details = self.socket_path.stat(follow_symlinks=False)
            if (details.st_dev, details.st_ino) == self.bound_identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.bound_identity = None

    def close(self) -> None:
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        with self.connections_lock:
            connections = list(self.connections)
            self.connections.clear()
        for connection in connections:
            connection.close()
        self._unlink_owned_socket()


MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.omarchy_soundcloud"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT = "org.mpris.MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
MPRIS_XML = """<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""
MPRIS_ROOT_PROPERTIES = {
    "CanQuit": ("b", False),
    "CanRaise": ("b", True),
    "HasTrackList": ("b", False),
    "Identity": ("s", APP_NAME),
    "DesktopEntry": ("s", APP_ID),
    "SupportedUriSchemes": ("as", []),
    "SupportedMimeTypes": ("as", []),
}


def mpris_player_properties(state: dict[str, object], art_url: str, volume: float) -> dict[str, tuple[str, object]]:
    """MPRIS Player properties (signature, value) for a validated status."""
    title = str(state.get("title") or "")
    playing = state.get("playing") is True
    duration = float(state.get("duration") or 0)
    playback_id = str(state.get("playbackId") or "")
    track_number = playback_id.rpartition(":")[2] if is_playback_id(playback_id) else "none"
    metadata: dict[str, tuple[str, object]] = {
        "mpris:trackid": ("o", f"{MPRIS_PATH}/track/{track_number}"),
    }
    if title:
        metadata["xesam:title"] = ("s", title)
        artist = str(state.get("artist") or "")
        if artist:
            metadata["xesam:artist"] = ("as", [artist])
        if duration > 0:
            metadata["mpris:length"] = ("x", int(duration * 1_000_000))
        if art_url and is_allowed_artwork_uri(art_url):
            metadata["mpris:artUrl"] = ("s", art_url)
    return {
        "PlaybackStatus": ("s", "Playing" if playing else ("Paused" if title else "Stopped")),
        "Rate": ("d", 1.0),
        "MinimumRate": ("d", 1.0),
        "MaximumRate": ("d", 1.0),
        "Metadata": ("a{sv}", metadata),
        "Volume": ("d", float(volume)),
        "CanGoNext": ("b", bool(title)),
        "CanGoPrevious": ("b", bool(title)),
        "CanPlay": ("b", True),
        "CanPause": ("b", True),
        "CanSeek": ("b", duration > 0),
        "CanControl": ("b", True),
    }


class _NullConnection:
    """Response sink for commands the backend issues to itself (MPRIS)."""

    def send(self, _payload: object) -> bool:
        return True

    def close(self) -> None:
        pass


def build_application(show_on_start: bool = False):
    Gio, GLib, Gst, Gtk, Soup, WebKit2 = _load_gi()
    paths = profile_paths()
    prepare_profile_paths(paths)
    profile_fd = ensure_private_directory(paths.data_dir)
    cookie_store = CookieStore(profile_fd)
    cookie_records = cookie_store.load()
    if not cookie_records:
        cookie_records = cookie_store.migrate_legacy_sqlite()
    track_cache_store = PrivateJsonStore(profile_fd, "tracks.json", MAX_TRACK_CACHE_BYTES)
    track_cache = sanitize_track_cache(track_cache_store.load())
    player_state_store = PrivateJsonStore(profile_fd, "player.json", PLAYER_STATE_BYTES)
    try:
        saved_player_state = sanitize_player_state(player_state_store.load())
    except OSError:
        saved_player_state = {}

    def variant(signature, value):
        if signature == "a{sv}":
            return GLib.Variant("a{sv}", {key: variant(*entry) for key, entry in value.items()})
        return GLib.Variant(signature, value)

    class MprisService:
        """Exposes the player on D-Bus so media keys and the shell can drive it."""

        def __init__(self, app):
            self.app = app
            self.connection = None
            self.registrations = []
            self.properties: dict[str, tuple[str, object]] = {}
            self.position = 0.0
            self.duration = 0.0
            self.playing = False
            self.node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
            self.owner_id = Gio.bus_own_name(
                Gio.BusType.SESSION, MPRIS_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                self._acquired, None, None,
            )

        def _acquired(self, connection, _name):
            self.connection = connection
            for interface in self.node.interfaces:
                self.registrations.append(connection.register_object(
                    MPRIS_PATH, interface, self._method, self._get, self._set,
                ))

        def _get(self, _connection, _sender, _path, interface, name):
            if interface == MPRIS_ROOT and name in MPRIS_ROOT_PROPERTIES:
                return variant(*MPRIS_ROOT_PROPERTIES[name])
            if name == "Position":
                return GLib.Variant("x", int(self.position * 1_000_000))
            entry = self.properties.get(name)
            return variant(*entry) if entry else None

        def _set(self, _connection, _sender, _path, interface, name, value):
            if interface == MPRIS_PLAYER and name == "Volume":
                self.app._run_internal(f"volume:{max(0.0, min(1.0, value.unpack())):.3f}")
            return True

        def _seek_to(self, seconds):
            if self.duration > 0:
                self.app._run_internal(f"seek:{max(0.0, min(1.0, seconds / self.duration)):.5f}")

        def _method(self, _connection, _sender, _path, interface, method, parameters, invocation):
            commands = {
                "Raise": "show", "PlayPause": "play-pause", "Next": "next", "Previous": "previous",
            }
            if method in commands:
                self.app._run_internal(commands[method])
            elif method == "Play" and not self.playing:
                self.app._run_internal("play-pause")
            elif method in {"Pause", "Stop"} and self.playing:
                self.app._run_internal("play-pause")
            elif method == "Seek":
                self._seek_to(self.position + parameters.unpack()[0] / 1_000_000)
            elif method == "SetPosition":
                self._seek_to(parameters.unpack()[1] / 1_000_000)
            invocation.return_value(None)

        def update(self, state, art_url, volume):
            self.position = float(state.get("position") or 0)
            self.duration = float(state.get("duration") or 0)
            self.playing = state.get("playing") is True
            properties = mpris_player_properties(state, art_url, volume)
            changed = {
                name: entry for name, entry in properties.items()
                if self.properties.get(name) != entry
            }
            self.properties = properties
            if changed and self.connection is not None:
                self.connection.emit_signal(
                    None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (
                        MPRIS_PLAYER,
                        {name: variant(*entry) for name, entry in changed.items()},
                        [],
                    )),
                )

        def close(self):
            if self.connection is not None:
                for registration in self.registrations:
                    self.connection.unregister_object(registration)
            Gio.bus_unown_name(self.owner_id)
    socket_path = runtime_socket_path()
    prepare_runtime_socket_path(socket_path)
    install_path = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".config/omarchy/plugins/brunosilveira.soundcloud"

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("omarchy-soundcloud")

    class SoundCloudApplication(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.window = None
            self.webview = None
            self.status_subscribers = StatusSubscribers()
            self.status_evaluation_pending = False
            self.artwork_cache = ArtworkCache()
            self.playback = GstPlayback(
                Gst,
                lambda: GLib.idle_add(self._playback_changed),
                GLib.idle_add,
            )
            self.list_artwork_cache = ArtworkCache(
                maximum_entries=48,
                maximum_pending=8,
                fetcher=lambda uri: fetch_artwork_data_url(
                    uri,
                    max_bytes=LIST_ARTWORK_BYTES,
                    max_pixels=LIST_ARTWORK_PIXELS,
                    max_dimension=LIST_ARTWORK_DIMENSION,
                ),
            )
            self.track_artwork_urls: OrderedDict[str, str] = OrderedDict()
            self.track_likes = TrackLikes(
                lambda: oauth_token_from_cookies(cookie_store.load()),
                self._write_like_in_page,
            )
            self.track_cache = track_cache
            self.playback_tracks = {
                track["playbackId"]: track
                for tracks in track_cache.values()
                for track in tracks
                if is_playback_id(track.get("playbackId"))
            }
            self.selection_generation = 0
            # True while SoundCloud's own web player (inside the WebKit view) is audible.
            self.page_playing = False
            # True while the web player has a track loaded, playing or paused.
            self.page_media_present = False
            # Likes, search, and opened playlist/artist lists (Home/Feed live in track_cache).
            self.lists: dict[str, list[dict[str, object]]] = {}
            # The list the current track was played from; next/previous walk it.
            self.queue: list[dict[str, object]] = []
            self.queue_index = -1
            self.queue_source = ""
            # Home/Feed answered from cache instead of reloading the page.
            self.cached_sources: set[str] = set()
            self.pending_seek = 0.0
            self.last_art_url = ""
            self.last_state_save = 0.0
            self.mpris = None
            self.restored: dict[str, object] | None = None
            self.playback.volume = float(saved_player_state.get("volume", 1.0))
            if saved_player_state.get("track"):
                track = saved_player_state["track"]
                self.restored = {"track": track, "position": saved_player_state.get("position", 0.0)}
                self.playback_tracks[track["playbackId"]] = track
                if saved_player_state.get("queue"):
                    self.queue = list(saved_player_state["queue"])
                    self.queue_index = saved_player_state["queueIndex"]
                    self.queue_source = saved_player_state["queueSource"]
                    for queued in self.queue:
                        self.playback_tracks.setdefault(queued["playbackId"], queued)
            self.track_cache_save_pending = False
            self.had_subscriber = False
            self.disconnect_generation = 0
            self.cookie_save_source = 0
            self.cookie_manager = None
            self.dispatch_slots = threading.BoundedSemaphore(MAX_PENDING_DISPATCHES)
            self.control_server = ControlServer(
                socket_path,
                self._dispatch_from_thread,
                self._disconnect_from_thread,
            )

        def do_activate(self):
            self._create_window()
            self.hold()
            try:
                self.mpris = MprisService(self)
            except Exception:
                self.mpris = None
            self.control_server.start()
            GLib.timeout_add(750, self._publish_status)
            GLib.timeout_add_seconds(5, self._check_installation)
            if show_on_start:
                self.show_login()
            else:
                self.window.show_all()
                GLib.idle_add(self.window.hide)

        def do_shutdown(self):
            self._save_player_state()
            if self.mpris is not None:
                self.mpris.close()
            self.control_server.close()
            self.playback.close()
            self.artwork_cache.close()
            self.list_artwork_cache.close()
            os.close(profile_fd)
            Gtk.Application.do_shutdown(self)

        def _check_installation(self):
            if not install_path.exists():
                self.quit()
                return False
            return True

        def _create_window(self):
            manager = WebKit2.WebsiteDataManager.new_ephemeral()
            context = WebKit2.WebContext.new_with_website_data_manager(manager)
            cookies = context.get_cookie_manager()
            self.cookie_manager = cookies
            cookies.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)
            cookies.connect("changed", self._cookies_changed)

            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("SoundCloud sign in — close when finished")
            self.window.set_default_size(1080, 760)
            self.window.set_icon_name("audio-x-generic")
            self.window.connect("delete-event", self._hide_window)

            self.webview = WebKit2.WebView.new_with_context(context)
            content_manager = self.webview.get_user_content_manager()
            if not content_manager.register_script_message_handler("omarchyTracks"):
                raise RuntimeError("could not register SoundCloud track message handler")
            if not content_manager.register_script_message_handler("omarchyPlayback"):
                raise RuntimeError("could not register SoundCloud playback message handler")
            content_manager.connect(
                "script-message-received::omarchyTracks",
                self._tracks_message_received,
            )
            content_manager.connect(
                "script-message-received::omarchyPlayback",
                self._playback_message_received,
            )
            content_manager.add_script(WebKit2.UserScript.new(
                api_request_capture_script(),
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                ["https://soundcloud.com/*"],
                [],
            ))
            settings = self.webview.get_settings()
            for name, value in (
                ("enable-webaudio", True),
                ("enable-mediasource", True),
                ("enable-media-stream", True),
                ("enable-media", True),
                ("media-playback-requires-user-gesture", False),
            ):
                settings.set_property(name, value)
            self.webview.connect("load-changed", self._page_load_changed)
            self.webview.connect("create", self._create_popup)
            self.webview.connect("decide-policy", self._decide_policy)
            self.window.add(self.webview)
            restored = []
            now = int(time.time())
            for record in cookie_records:
                expires = record["expires"]
                max_age = -1 if expires is None else max(0, int(expires) - now)
                cookie = Soup.Cookie.new(
                    record["name"],
                    record["value"],
                    record["domain"],
                    record["path"],
                    max_age,
                )
                cookie.set_secure(record["secure"])
                cookie.set_http_only(record["httpOnly"])
                restored.append(cookie)
            if restored:
                cookies.replace_cookies(restored, None, self._cookies_restored, None)
            else:
                self.webview.load_uri(START_URI)

        def _tracks_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    return
                payload = json.loads(raw)
                source = payload.get("source") if isinstance(payload, dict) else None
                if source not in {"home", "feed"}:
                    return
                page = validate_tracks_payload({"tracks": payload.get("tracks")})
                if page is None:
                    return
                incoming_tracks = page.get("tracks")
                if not isinstance(incoming_tracks, list):
                    return
                for track in incoming_tracks:
                    playback_id = track.get("playbackId")
                    if is_playback_id(playback_id):
                        self.playback_tracks[playback_id] = track
                while len(self.playback_tracks) > MAX_TRACKS * 2:
                    self.playback_tracks.pop(next(iter(self.playback_tracks)))
                existing_tracks = self.track_cache.get(source, [])
                reset = payload.get("reset") is True
                merged_tracks = merge_track_pages(
                    existing_tracks,
                    incoming_tracks,
                    reset=reset,
                )
                previous_count = 0 if reset else len(existing_tracks)
                added_count = max(0, len(merged_tracks) - previous_count)
                pagination_error = _bounded_string(payload.get("error", ""), 128) or ""
                has_more = payload.get("hasMore") is True and (
                    added_count > 0 or bool(pagination_error)
                )
                result = self._secure_tracks({"tracks": merged_tracks})
                if result is None:
                    return
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return
            self.status_subscribers.publish_message(
                {
                    "type": "tracks",
                    "source": source,
                    "reset": reset,
                    "hasMore": has_more,
                    "addedCount": added_count,
                    **({"error": pagination_error} if pagination_error else {}),
                    **result,
                }
            )
            log_event(
                "track-page",
                source=source,
                reset=reset,
                incomingCount=len(incoming_tracks),
                previousCount=previous_count,
                resultCount=len(merged_tracks),
                addedCount=added_count,
                hasMore=has_more,
            )
            if result["tracks"]:
                self.track_cache[source] = merged_tracks
                if not self.track_cache_save_pending:
                    self.track_cache_save_pending = True
                    GLib.idle_add(self._save_track_cache)

        def _playback_message_received(self, _manager, message):
            try:
                value = message.get_js_value()
                encoded = value.to_json(0)
                raw = json.loads(encoded)
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
                    return
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return
                if payload.get("event") == "page-play":
                    # The web player started: it wins, so only one player is audible.
                    self.page_playing = True
                    if self.playback.playing:
                        self.playback.pause()
                    GLib.timeout_add(150, self._publish_status_once)
                    return
                request_id = payload.get("requestId")
                if (isinstance(request_id, bool) or not isinstance(request_id, int)
                        or request_id != self.selection_generation):
                    return
                if payload.get("ok") is not True:
                    self.playback.stop(
                        _bounded_string(payload.get("error", ""), 128)
                        or "SoundCloud could not resolve this track"
                    )
                    return
                stream_uri = validate_stream_uri(payload.get("streamUrl"))
                validated = validate_tracks_payload({"tracks": [payload.get("track")]})
                validated_tracks = validated.get("tracks") if validated is not None else None
                if (stream_uri is None or not isinstance(validated_tracks, list)
                        or len(validated_tracks) != 1):
                    self.playback.stop("SoundCloud returned invalid stream details")
                    return
                selected_track = validated_tracks[0]
                if not isinstance(selected_track, dict):
                    self.playback.stop("SoundCloud returned invalid track details")
                    return
                if selected_track.get("playbackId") != self.playback.metadata.get("playbackId"):
                    return
                protocol = payload.get("protocol") if payload.get("protocol") in {"hls", "progressive"} else ""
                mime_type = payload.get("mimeType") if payload.get("mimeType") in {"audio/mpeg", "audio/mp4"} else ""
                started = self.playback.play(
                    stream_uri,
                    {**selected_track, "waveform": validate_waveform(payload.get("waveform"))},
                    protocol,
                    mime_type,
                )
                if not started:
                    self.playback.error = "SoundCloud could not play this track"
                self._publish_status_once()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return

        def _save_track_cache(self):
            self.track_cache_save_pending = False
            try:
                track_cache_store.save(self.track_cache)
            except OSError:
                pass
            return False

        def _publish_cached_tracks(self, source):
            cached = self.track_cache.get(source)
            if not cached:
                return
            result = self._secure_tracks({"tracks": cached})
            if result is not None and result["tracks"]:
                self.status_subscribers.publish_message(
                    {"type": "tracks", "source": source, "cached": True, **result}
                )

        def _cookies_restored(self, manager, result, _data):
            try:
                manager.replace_cookies_finish(result)
            except Exception:
                pass
            self.webview.load_uri(START_URI)

        def _cookies_changed(self, _manager):
            if self.cookie_save_source:
                GLib.source_remove(self.cookie_save_source)
            self.cookie_save_source = GLib.timeout_add_seconds(2, self._save_cookies)

        def _save_cookies(self):
            self.cookie_save_source = 0
            self.cookie_manager.get_all_cookies(None, self._cookies_collected, None)
            return False

        def _cookies_collected(self, manager, result, _data):
            try:
                cookies = manager.get_all_cookies_finish(result)
                records = []
                for cookie in cookies[:MAX_COOKIES]:
                    domain = cookie.get_domain() or ""
                    normalized = domain.lower().lstrip(".").rstrip(".")
                    if normalized != "soundcloud.com" and not normalized.endswith(".soundcloud.com"):
                        continue
                    expires = cookie.get_expires()
                    records.append({
                        "name": cookie.get_name() or "",
                        "value": cookie.get_value() or "",
                        "domain": domain,
                        "path": cookie.get_path() or "/",
                        "expires": int(expires.to_unix()) if expires else None,
                        "secure": bool(cookie.get_secure()),
                        "httpOnly": bool(cookie.get_http_only()),
                    })
                cookie_store.save(records)
            except Exception:
                pass

        def _page_load_changed(self, _webview, event):
            if event == WebKit2.LoadEvent.FINISHED:
                # A fresh page forgets the volume; hand it over again.
                self._evaluate(volume_script(self.playback.volume), lambda _payload: None)

        def _run_internal(self, command):
            if not self.dispatch_slots.acquire(blocking=False):
                return
            GLib.idle_add(self._handle_command_safely, command, _NullConnection(), None)

        def _list_tracks(self, source):
            if source in {"home", "feed"}:
                return self.track_cache.get(source, [])
            return self.lists.get(source, [])

        def _set_queue(self, source, playback_id):
            tracks = [
                track for track in self._list_tracks(source)
                if is_playback_id(track.get("playbackId"))
            ][:MAX_QUEUE]
            ids = [track["playbackId"] for track in tracks]
            if playback_id not in ids:
                return False
            self.queue = list(tracks)
            self.queue_index = ids.index(playback_id)
            self.queue_source = source
            for track in tracks:
                self.playback_tracks.setdefault(track["playbackId"], track)
            return True

        def _queue_for_unsourced_play(self, playback_id):
            for source in ("search", "collection", "likes", "home", "feed"):
                if self._set_queue(source, playback_id):
                    return
            self.queue, self.queue_index, self.queue_source = [], -1, ""

        def _start_track(self, playback_id, done=None, resume_at=0.0):
            finish = done or (lambda _ok, _error: None)
            metadata = self.playback_tracks.get(playback_id)
            if metadata is None:
                finish(False, "SoundCloud track details are not ready")
                return
            self.restored = None
            self.pending_seek = max(0.0, float(resume_at))
            self.selection_generation = (self.selection_generation + 1) % (2**31)
            generation = self.selection_generation
            self.playback.begin(metadata)
            self._publish_status_once()

            def track_selected(payload):
                if generation != self.selection_generation:
                    finish(False, "selection superseded")
                    return
                if not payload:
                    self.playback.stop("SoundCloud track details are not ready")
                finish(bool(payload), "" if payload else "SoundCloud track details are not ready")

            self._evaluate(play_track_script(playback_id, generation), track_selected)
            GLib.timeout_add_seconds(15, self._resolution_timed_out, generation)

        def _advance(self, step, done=None):
            target = queue_step(len(self.queue), self.queue_index, step)
            if target is None and step == 1 and self.queue:
                # The list is over; carry on with tracks related to the last one.
                self._autoplay_related(done)
                return True
            if target is None:
                if done:
                    done(False, "No more tracks in this list")
                return False
            self.queue_index = target
            self._start_track(self.queue[target]["playbackId"], done)
            return True

        def _autoplay_related(self, done=None):
            finish = done or (lambda _ok, _error: None)
            last = self.queue[self.queue_index] if 0 <= self.queue_index < len(self.queue) else self.queue[-1]
            playback_id = str(last.get("playbackId") or "")
            if not is_playback_id(playback_id) or not self.webview \
                    or not is_soundcloud_uri(self.webview.get_uri() or ""):
                finish(False, "No more tracks in this list")
                return
            generation = self.selection_generation

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    raw = value.to_string() if value.is_string() else ""
                    payload = json.loads(raw) if raw and len(raw.encode("utf-8")) <= MAX_RESPONSE_BYTES else {}
                except Exception:
                    payload = {}
                validated = validate_tracks_payload({"tracks": payload.get("tracks", [])}) \
                    if isinstance(payload, dict) else None
                if generation != self.selection_generation:
                    finish(False, "selection superseded")
                    return
                queue, index = extend_queue(self.queue, self.queue_index, validated["tracks"] if validated else [])
                if index + 1 >= len(queue):
                    finish(False, "No more tracks in this list")
                    return
                for track in queue[index + 1:]:
                    self.playback_tracks.setdefault(track["playbackId"], track)
                self.queue, self.queue_index = queue, index + 1
                self._start_track(queue[index + 1]["playbackId"], finish)

            self.webview.call_async_javascript_function(
                RELATED_TRACKS_FUNCTION, -1,
                GLib.Variant("a{sv}", {"id": GLib.Variant("d", float(playback_id.rpartition(":")[2]))}),
                None, None, None, finished, None,
            )

        def _playback_changed(self):
            if self.pending_seek and self.playback.playing:
                duration = float(self.playback.metadata.get("durationMs", 0) or 0) / 1000
                if duration > 0:
                    self.playback.seek(min(1.0, self.pending_seek / duration))
                self.pending_seek = 0.0
            if self.playback.ended:
                self.playback.ended = False
                self._advance(1)
            if self.playback.state in {"playing", "paused"}:
                self._save_player_state()
            self._publish_status_once()
            return False

        def _player_snapshot(self):
            snapshot: dict[str, object] = {"volume": self.playback.volume}
            if self.playback.active:
                track = self.playback.metadata
                position = self.playback._query_time("query_position")
            elif self.restored:
                track = self.restored["track"]
                position = self.restored.get("position", 0.0)
            else:
                return snapshot
            if not is_playback_id(track.get("playbackId")):
                return snapshot
            snapshot["track"] = {
                key: track.get(key) for key in
                ("playbackId", "title", "artist", "url", "artUrl", "playCount", "durationMs")
            }
            snapshot["position"] = max(0.0, float(position or 0))
            if self.queue and 0 <= self.queue_index < len(self.queue):
                snapshot["queue"] = self.queue
                snapshot["queueIndex"] = self.queue_index
                snapshot["queueSource"] = self.queue_source
            return snapshot

        def _save_player_state(self):
            self.last_state_save = time.monotonic()
            try:
                player_state_store.save(self._player_snapshot())
            except (OSError, TypeError, ValueError):
                pass

        def _hide_window(self, *_args):
            self.window.hide()
            # Closing the window stops SoundCloud's web player; the bar's own
            # player is unaffected.
            self._evaluate(command_script("pause-page"), lambda _payload: self._publish_status_once())
            return True

        def show_login(self):
            self.window.show_all()
            self.window.present()

        def _create_popup(self, source, _navigation_action):
            popup = WebKit2.WebView.new_with_related_view(source)
            popup.connect("decide-policy", self._decide_popup_policy)
            popup_window = Gtk.Window(application=self)
            popup_window.set_default_size(720, 720)
            popup_window.set_title("SoundCloud sign in")
            popup_window.add(popup)
            popup.connect("close", lambda *_: popup_window.destroy())
            popup_window.show_all()
            return popup

        def _decide_policy(self, _webview, decision, decision_type):
            if decision_type not in {
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
            }:
                return False
            request = decision.get_request()
            uri = request.get_uri() if request else ""
            if is_soundcloud_uri(uri) or is_bot_check_uri(uri):
                decision.use()
            else:
                decision.ignore()
            return True

        def _decide_popup_policy(self, _webview, decision, decision_type):
            if decision_type not in {
                WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
            }:
                return False
            request = decision.get_request()
            uri = request.get_uri() if request else ""
            if is_soundcloud_uri(uri) or is_bot_check_uri(uri) or is_sign_in_provider_uri(uri):
                decision.use()
            else:
                decision.ignore()
            return True

        def _dispatch_from_thread(self, command, connection, request_id):
            if not self.dispatch_slots.acquire(blocking=False):
                send_response(connection, {"ok": False, "error": "server busy"}, request_id)
                return
            GLib.idle_add(self._handle_command_safely, command, connection, request_id)

        def _handle_command_safely(self, command, connection, request_id):
            try:
                return self._handle_command(command, connection, request_id)
            except Exception:
                self._respond(connection, request_id, {"ok": False, "error": "request failed"})
                return False

        def _disconnect_from_thread(self, connection):
            self.status_subscribers.remove(connection)
            if self.had_subscriber and self.status_subscribers.count() == 0:
                self.disconnect_generation += 1
                generation = self.disconnect_generation
                GLib.timeout_add_seconds(10, self._quit_if_abandoned, generation)

        def _quit_if_abandoned(self, generation):
            if generation == self.disconnect_generation and self.status_subscribers.count() == 0:
                self.quit()
            return False

        def _artwork_ready_from_thread(self):
            GLib.idle_add(self._publish_status_once)

        def _secure_status(self, payload):
            if isinstance(payload, dict) and "pagePlaying" in payload:
                self.page_playing = payload.get("pagePlaying") is True
                self.page_media_present = payload.get("playerPresent") is True
            if (self.restored and self.playback.state == "idle"
                    and not self.page_playing and not self.page_media_present):
                # The last track from before a restart, ready to resume.
                track = self.restored["track"]
                base = payload if isinstance(payload, dict) else {}
                payload = {
                    **base,
                    "playerPresent": True,
                    "playbackState": "paused",
                    "playing": False,
                    "playbackId": track["playbackId"],
                    "title": track["title"],
                    "artist": track["artist"],
                    "artUrl": track.get("artUrl", ""),
                    "duration": float(track.get("durationMs", 0) or 0) / 1000,
                    "position": float(self.restored.get("position", 0.0)),
                    "waveform": [],
                }
            # Show whichever player is audible; the bar's player only when the
            # web player is not.
            if self.playback.state != "idle" and not (self.page_playing and not self.playback.playing):
                base = payload if isinstance(payload, dict) else {}
                payload = {**base, **self.playback.status()}
            elif self.page_playing and isinstance(payload, dict):
                payload = {**payload, "playing": True, "playbackState": "playing"}
            state = validate_status_payload(payload)
            if state is None:
                return {}
            # A fixed mid-size rendition: SoundCloud's full-size artwork can
            # exceed the fetcher's limits, and the bar only needs 300px.
            remote_art = normalize_list_artwork_url(state.pop("artUrl"), "t300x300")
            self.last_art_url = remote_art
            state["artDataUrl"] = self.artwork_cache.get(
                remote_art, self._artwork_ready_from_thread
            )
            playback_id = str(state["playbackId"])
            liked = None
            if playback_id:
                liked = self.track_likes.is_liked(int(playback_id.rpartition(":")[2]))
                if liked is None and state["loggedIn"]:
                    self.track_likes.load_in_background(
                        lambda: GLib.idle_add(self._publish_status_once)
                    )
            state["liked"] = liked is True
            state["volume"] = round(self.playback.volume, 3)
            state["queueSource"] = self.queue_source if self.queue else ""
            return state

        def _secure_tracks(self, payload):
            result = validate_tracks_payload(payload)
            if result is None:
                return None
            for track in result["tracks"]:
                original_art = track.pop("artUrl")
                remote_art = normalize_list_artwork_url(original_art)
                track["artworkId"] = ""
                if not remote_art:
                    continue
                token = hashlib.sha256(remote_art.encode("utf-8")).hexdigest()[:24]
                self.track_artwork_urls[token] = remote_art
                self.track_artwork_urls.move_to_end(token)
                while len(self.track_artwork_urls) > 200:
                    self.track_artwork_urls.popitem(last=False)
                track["artworkId"] = token
            return result

        def _respond_track_artwork(self, connection, request_id, artwork_id, data_url):
            self._respond(
                connection,
                request_id,
                {"ok": bool(data_url), "artworkId": artwork_id, "artDataUrl": data_url},
            )
            return False

        def _publish_status_once(self):
            self._publish_status()
            return False

        def _publish_status(self):
            # Runs even without a bar subscriber so MPRIS stays current.
            if self.status_evaluation_pending:
                return True
            self.status_evaluation_pending = True

            def finished(payload):
                self.status_evaluation_pending = False
                state = self._secure_status(payload)
                if self.mpris is not None and state:
                    self.mpris.update(state, self.last_art_url, self.playback.volume)
                if self.playback.playing and time.monotonic() - self.last_state_save > 15:
                    self._save_player_state()
                if self.status_subscribers:
                    self.status_subscribers.publish({"running": True, **state})

            self._evaluate(status_script(), finished)
            return True

        def _resolution_timed_out(self, generation):
            if (generation == self.selection_generation
                    and self.playback.state == "resolving"):
                self.selection_generation += 1
                self.playback.stop("SoundCloud took too long to resolve this track")
            return False

        def _respond(self, connection, request_id, payload, publish_status=False):
            try:
                send_response(connection, payload, request_id=request_id)
                if publish_status:
                    GLib.timeout_add(80, self._publish_status_once)
            finally:
                self.dispatch_slots.release()

        def _handle_command(self, command, connection, request_id):
            command = normalize_backend_command(command)
            if command == "status":
                self._evaluate(
                    status_script(),
                    lambda payload: self._respond(
                        connection,
                        request_id,
                        {"ok": True, "running": True, **self._secure_status(payload)},
                    ),
                )
            elif command == "subscribe":
                self.status_subscribers.add(connection)
                self.had_subscriber = True
                self.disconnect_generation += 1
                self._respond(connection, request_id, {"ok": True, "running": True})
                GLib.idle_add(self._publish_status_once)
            elif (source := load_more_source_from_command(command)) is not None:
                def load_more_finished(payload):
                    valid = isinstance(payload, dict)
                    started = valid and payload.get("started") is True
                    has_more = valid and payload.get("hasMore") is True
                    log_event(
                        "lazy-load-request",
                        source=source,
                        started=started,
                        hasMore=has_more,
                    )
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": valid,
                            "running": True,
                            "started": started,
                            "hasMore": has_more,
                        },
                    )

                self._evaluate(
                    load_more_script(source),
                    load_more_finished,
                )
            elif (source := track_source_from_command(command)) in self.cached_sources:
                result = self._secure_tracks({"tracks": self.track_cache.get(source, [])})
                self._respond(connection, request_id, {
                    "ok": result is not None, "running": True, "cached": True,
                    **(result or {"tracks": []}),
                })
            elif source is not None:
                def tracks_finished(payload):
                    pending = isinstance(payload, dict) and payload.get("pending") is True
                    remote_error = (
                        _bounded_string(payload.get("error", ""), 128)
                        if isinstance(payload, dict)
                        else None
                    ) or ""
                    result = self._secure_tracks(payload)
                    if pending and result is not None:
                        result["pending"] = True
                    if remote_error and result is not None:
                        result["error"] = remote_error
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": result is not None,
                            "running": True,
                            **(result or {"tracks": [], "error": "invalid track data"}),
                        },
                    )

                self._evaluate(
                    api_tracks_script(source),
                    tracks_finished,
                )
            elif command == "inspect-api":
                def observation_finished(payload):
                    paths = payload.get("paths", []) if isinstance(payload, dict) else []
                    paths = [
                        path for path in paths[:32]
                        if isinstance(path, str) and len(path.encode("utf-8")) <= 512
                    ]
                    lists = payload.get("lists", {}) if isinstance(payload, dict) else {}
                    safe_lists = {
                        source: {
                            "count": _bounded_integer(state.get("count"), 0, MAX_TRACKS),
                            "loading": state.get("loading") is True,
                            "error": _bounded_string(state.get("error", ""), 128) or "",
                        }
                        for source, state in lists.items()
                        if source in {"home", "feed"} and isinstance(state, dict)
                    }
                    playback_count = _bounded_integer(
                        payload.get("playbackCount"), 0, MAX_TRACKS * 2
                    ) if isinstance(payload, dict) else 0
                    self._respond(connection, request_id, {
                        "ok": True,
                        "paths": paths,
                        "lists": safe_lists,
                        "playbackCount": playback_count,
                    })

                self._evaluate(api_observation_script(), observation_finished)
            elif command.startswith("artwork:"):
                artwork_id = command.partition(":")[2]
                remote_art = self.track_artwork_urls.get(artwork_id)
                if (
                    len(artwork_id) != 24
                    or any(character not in "0123456789abcdef" for character in artwork_id)
                    or not remote_art
                ):
                    self._respond(connection, request_id, {"ok": False, "error": "unknown artwork"})
                else:
                    self.list_artwork_cache.request(
                        remote_art,
                        lambda data_url: GLib.idle_add(
                            self._respond_track_artwork,
                            connection,
                            request_id,
                            artwork_id,
                            data_url,
                        ),
                    )
            elif command == "play-pause":
                # Act on whichever player is audible, and never leave two playing.
                def toggle_audible_player(payload):
                    page_playing = isinstance(payload, dict) and payload.get("pagePlaying") is True
                    self.page_playing = page_playing
                    respond = lambda result: self._respond(
                        connection,
                        request_id,
                        {"ok": bool(result), "running": True},
                        publish_status=True,
                    )
                    if page_playing:
                        if self.playback.playing:
                            self.playback.pause()
                        self._evaluate(command_script("pause-page"), respond)
                    elif self.playback.active:
                        respond(self.playback.toggle())
                    elif (self.restored and self.playback.state in {"idle", "error"}
                            and not (isinstance(payload, dict) and payload.get("playerPresent") is True)):
                        # Resume the track from before the restart where it stopped.
                        restored = self.restored
                        self._start_track(
                            restored["track"]["playbackId"],
                            lambda ok, error: self._respond(
                                connection, request_id,
                                {"ok": ok, "running": True, **({"error": error} if error else {})},
                                publish_status=True,
                            ),
                            resume_at=float(restored.get("position", 0.0)),
                        )
                    else:
                        self._evaluate(command_script("play-pause"), respond)

                self._evaluate(status_script(), toggle_audible_player)
            elif command in {"next", "previous"}:
                respond_step = lambda ok, error="": self._respond(
                    connection, request_id,
                    {"ok": ok, "running": True, **({"error": error} if error else {})},
                    publish_status=True,
                )
                bar_player_current = (
                    self.playback.active or self.playback.state in {"resolving", "error"}
                    or bool(self.restored)
                )
                if self.queue and bar_player_current and not self.page_playing:
                    # Walk the list the current track came from.
                    position = self.playback._query_time("query_position") if self.playback.active else 0.0
                    if command == "previous" and position > 3:
                        respond_step(self.playback.seek(0.0))
                    else:
                        self._advance(1 if command == "next" else -1, respond_step)
                elif command == "previous" and self.playback.active and not self.page_playing:
                    respond_step(self.playback.seek(0.0))
                else:
                    if self.playback.active or self.playback.error:
                        self.playback.stop()
                    self.restored = None
                    self._evaluate(
                        command_script(command),
                        lambda payload: respond_step(bool(payload)),
                    )
            elif (search := search_from_command(command)) is not None:
                kind, query = search
                self._page_list(
                    SEARCH_TRACKS_FUNCTION,
                    {"query": GLib.Variant("s", query), "kind": GLib.Variant("s", kind)},
                    "search" if kind == "tracks" else "",
                    connection, request_id, {"query": query, "kind": kind},
                )
            elif command.partition(":")[0] in SEARCH_KINDS:
                self._respond(connection, request_id, {"ok": False, "error": "Enter a shorter search"})
            elif (collection := collection_from_command(command)) is not None:
                kind, identifier = collection
                self._page_list(
                    OPEN_COLLECTION_FUNCTION,
                    {"kind": GLib.Variant("s", kind), "id": GLib.Variant("d", float(identifier))},
                    "collection", connection, request_id, {"kind": kind, "collectionId": identifier},
                )
            elif command == "likes":
                self._page_list(LIKED_TRACKS_FUNCTION, {}, "likes", connection, request_id, {})
            elif command == "mixes":
                self._load_mixes(connection, request_id, 0)
            elif (shuffle_source := shuffle_from_command(command)) is not None:
                current = self.queue[self.queue_index].get("playbackId", "") if 0 <= self.queue_index < len(self.queue) else ""
                tracks = shuffled_tracks(self._list_tracks(shuffle_source), current)
                if not tracks:
                    self._respond(connection, request_id, {"ok": False, "error": "Nothing to shuffle yet"})
                else:
                    self.queue, self.queue_index, self.queue_source = tracks, 0, shuffle_source
                    for track in tracks:
                        self.playback_tracks.setdefault(track["playbackId"], track)

                    def shuffle_started(ok, error):
                        self._respond(
                            connection, request_id,
                            {"ok": ok, "running": True, "shuffled": len(tracks), **({"error": error} if error else {})},
                            publish_status=ok or error != "selection superseded",
                        )

                    self._start_track(tracks[0]["playbackId"], shuffle_started)
            elif (volume := volume_from_command(command)) is not None:
                self.playback.set_volume(volume)
                self._evaluate(volume_script(volume), lambda _payload: None)
                self._save_player_state()
                self._respond(
                    connection, request_id,
                    {"ok": True, "running": True, "volume": volume},
                    publish_status=True,
                )
            elif command.startswith("seek:"):
                try:
                    ratio = float(command.partition(":")[2])
                except ValueError:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "invalid seek position"},
                    )
                else:
                    if self.playback.active and not self.page_playing:
                        self._respond(
                            connection,
                            request_id,
                            {"ok": self.playback.seek(ratio), "running": True},
                            publish_status=True,
                        )
                    else:
                        self._evaluate(
                            seek_script(ratio),
                            lambda payload: self._respond(
                                connection,
                                request_id,
                                {"ok": bool(payload), "running": True},
                                publish_status=True,
                            ),
                        )
            elif ((queued := queued_play_from_command(command)) is not None
                    or playback_id_from_command(command) is not None):
                if queued is not None:
                    source, playback_id = queued
                    if not self._set_queue(source, playback_id):
                        self._queue_for_unsourced_play(playback_id)
                else:
                    playback_id = playback_id_from_command(command)
                    self._queue_for_unsourced_play(playback_id)

                def track_started(ok, error):
                    self._respond(
                        connection,
                        request_id,
                        {"ok": ok, "running": True, **({"error": error} if error else {})},
                        publish_status=ok or error != "selection superseded",
                    )

                self._start_track(playback_id, track_started)
            elif (like := track_like_from_command(command)) is not None:
                playback_id, liked = like

                def update_like():
                    try:
                        self.track_likes.set_liked(int(playback_id.rpartition(":")[2]), liked)
                        payload = {"ok": True, "running": True, "playbackId": playback_id, "liked": liked}
                    except BotCheckRequired:
                        GLib.idle_add(self.show_login)
                        payload = {"ok": False, "error": BOT_CHECK_MESSAGE}
                    except (OSError, ValueError):
                        payload = {"ok": False, "error": "SoundCloud could not update this like"}
                    GLib.idle_add(
                        lambda: self._respond(connection, request_id, payload, publish_status=True)
                    )

                threading.Thread(target=update_like, daemon=True).start()
            elif source_uri(command) or (
                    command.startswith("refresh:") and source_uri(command.removeprefix("refresh:"))):
                forced = command.startswith("refresh:")
                source = command.removeprefix("refresh:")
                if (not forced and source in {"home", "feed"} and self.track_cache.get(source)
                        and (self.page_media_present or self.page_playing)):
                    # Reloading would drop the web player's track; answer from cache.
                    self.cached_sources.add(source)
                    self._publish_cached_tracks(source)
                    self._respond(connection, request_id, {"ok": True, "running": True, "cached": True})
                else:
                    self.cached_sources.discard(source)
                    self._publish_cached_tracks(source)
                    self.webview.load_uri(source_uri(source))
                    self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "show":
                self.show_login()
                self._respond(connection, request_id, {"ok": True, "running": True})
            elif command == "stop":
                self._respond(connection, request_id, {"ok": True, "running": False})
                self.quit()
            else:
                self._respond(connection, request_id, {"ok": False, "error": "unknown command"})
            return False

        def _secure_collections(self, collections):
            for item in collections:
                remote_art = normalize_list_artwork_url(item.pop("artUrl"))
                item["artworkId"] = ""
                if remote_art:
                    token = hashlib.sha256(remote_art.encode("utf-8")).hexdigest()[:24]
                    self.track_artwork_urls[token] = remote_art
                    self.track_artwork_urls.move_to_end(token)
                    while len(self.track_artwork_urls) > 200:
                        self.track_artwork_urls.popitem(last=False)
                    item["artworkId"] = token
            return collections

        def _load_mixes(self, connection, request_id, attempt):
            """Mixes come from Home's selections; load Home first when the page has none."""
            def needs_home():
                if attempt == 0:
                    if self.page_media_present or self.page_playing:
                        self._respond(connection, request_id, {
                            "ok": False, "running": True, "tracks": [], "collections": [],
                            "error": "Open Home once to load your mixes",
                        })
                        return
                    self.cached_sources.discard("home")
                    self.webview.load_uri(source_uri("home"))
                if attempt >= 15:
                    self._respond(connection, request_id, {
                        "ok": False, "running": True, "tracks": [], "collections": [],
                        "error": "SoundCloud mixes did not load",
                    })
                    return
                GLib.timeout_add(1000, lambda: self._load_mixes(connection, request_id, attempt + 1) and False)

            self._page_list(MIXES_FUNCTION, {}, "", connection, request_id, {}, needs_home)

        def _page_list(self, function, arguments, list_name, connection, request_id, echo, needs_home=None):
            """Run a page list function (search, likes, playlist, artist) and answer with its rows."""
            def respond(payload):
                self._respond(connection, request_id, {"running": True, **echo, **payload})

            def failed(error):
                respond({"ok": False, "tracks": [], "collections": [], "error": error})

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    raw = value.to_string() if value.is_string() else ""
                    if not raw or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                        failed("SoundCloud request failed")
                        return
                    payload = json.loads(raw)
                except Exception:
                    failed("SoundCloud request failed")
                    return
                if not isinstance(payload, dict):
                    failed("SoundCloud request failed")
                    return
                if needs_home and payload.get("needsHome") is True:
                    needs_home()
                    return
                error = _bounded_string(payload.get("error", ""), 128) or ""
                if payload.get("collections"):
                    collections = validate_collections_payload(payload)
                    if collections is None:
                        failed("SoundCloud returned invalid results")
                        return
                    respond({
                        "ok": not error, "tracks": [],
                        "collections": self._secure_collections(collections),
                        **({"error": error} if error else {}),
                    })
                    return
                validated = validate_tracks_payload({"tracks": payload.get("tracks", [])})
                if validated is None:
                    failed("SoundCloud returned invalid results")
                    return
                for track in validated["tracks"]:
                    playback_id = track.get("playbackId")
                    if is_playback_id(playback_id):
                        self.playback_tracks[playback_id] = track
                while len(self.playback_tracks) > MAX_TRACKS * 4:
                    self.playback_tracks.pop(next(iter(self.playback_tracks)))
                if list_name and not error:
                    self.lists[list_name] = validated["tracks"]
                result = self._secure_tracks(validated) or {"tracks": []}
                respond({
                    "ok": not error, "collections": [], **result,
                    **({"error": error} if error else {}),
                })

            if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                failed("SoundCloud is still loading")
                return
            self.webview.call_async_javascript_function(
                function, -1, GLib.Variant("a{sv}", arguments), None, None, None, finished, None
            )

        def _write_like_in_page(self, user_id, track_id, liked, token):
            """Run on a worker thread; blocks until the page's fetch finishes."""
            done = threading.Event()
            result = {"outcome": "failed"}

            def finished(webview, async_result, _data):
                try:
                    value = webview.call_async_javascript_function_finish(async_result)
                    result["outcome"] = value.to_string() if value.is_string() else "failed"
                except Exception:
                    pass
                done.set()

            def start():
                if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                    done.set()
                    return False
                arguments = GLib.Variant("a{sv}", {
                    "userId": GLib.Variant("d", user_id),
                    "trackId": GLib.Variant("d", track_id),
                    "method": GLib.Variant("s", "PUT" if liked else "DELETE"),
                    "token": GLib.Variant("s", token),
                })
                self.webview.call_async_javascript_function(
                    LIKE_TRACK_FUNCTION, -1, arguments, None, None, None, finished, None
                )
                return False

            GLib.idle_add(start)
            if not done.wait(15.0):
                raise TimeoutError("like request timed out")
            if result["outcome"] == "bot-check":
                raise BotCheckRequired("SoundCloud asked for a captcha")
            if result["outcome"] != "ok":
                raise OSError("like request failed")

        def _evaluate(self, script, callback):
            if not self.webview or not is_soundcloud_uri(self.webview.get_uri() or ""):
                callback({"error": "refusing untrusted page"})
                return
            def finished(webview, result, _data):
                try:
                    js_result = webview.run_javascript_finish(result)
                    value = js_result.get_js_value()
                    payload = json.loads(value.to_json(0))
                except Exception as exc:
                    payload = {"error": str(exc)}
                callback(payload)

            self.webview.run_javascript(script, None, finished, None)

    return SoundCloudApplication()


def request_backend(command: str, timeout: float = 4.0) -> dict[str, object] | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        client.settimeout(max(0.1, deadline - time.monotonic()))
        client.connect(str(runtime_socket_path()))
        client.sendall((json.dumps({"command": command}) + "\n").encode())
        response = bytearray()
        while b"\n" not in response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            client.settimeout(remaining)
            chunk = client.recv(min(16384, MAX_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_RESPONSE_BYTES:
                return None
        raw, marker, trailing = bytes(response).partition(b"\n")
        if not marker or trailing or len(raw) > MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError):
        return None
    finally:
        client.close()


def launch_backend(show: bool) -> dict[str, object]:
    existing = request_backend("show" if show else "status", timeout=0.8)
    if existing is not None:
        return existing
    check = dependency_check()
    if not check["ok"]:
        return {"ok": False, "running": False, "error": check.get("error", "")}
    command = ["/usr/bin/python3", "-I", str(Path(__file__).resolve()), "daemon"]
    if show:
        command.append("--show")
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key in {
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "GDK_BACKEND",
            "HOME",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "LANG",
            "LC_ALL",
            "PIPEWIRE_REMOTE",
            "PULSE_SERVER",
            "WAYLAND_DISPLAY",
            "WEBKIT_DISABLE_DMABUF_RENDERER",
            "XAUTHORITY",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_TYPE",
        }
    }
    allowed_environment["PATH"] = "/usr/bin:/bin"
    event_log = open_runtime_event_log()
    try:
        event_log_fd = event_log.fileno()
        allowed_environment["OMARCHY_SOUNDCLOUD_EVENT_LOG_FD"] = str(event_log_fd)
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(event_log_fd,),
            start_new_session=True,
            env=allowed_environment,
        )
    finally:
        event_log.close()
    return {"ok": True, "starting": True, "running": True}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SoundCloud backend for the Omarchy bar")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--show", action="store_true")
    seek = subparsers.add_parser("seek")
    seek.add_argument("ratio", type=float)
    play = subparsers.add_parser("play")
    play.add_argument("playback_id")
    subparsers.add_parser("install-desktop-entry")
    subparsers.add_parser("launch")
    subparsers.add_parser("ensure")
    for action in ("status", "play-pause", "next", "previous", "home", "likes", "feed", "show", "stop"):
        subparsers.add_parser(action)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    command = args.command or "status"
    if command == "check":
        result = dependency_check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if command == "install-desktop-entry":
        print(install_desktop_entry())
        return 0
    if command in {"launch", "ensure"}:
        result = launch_backend(show=command == "launch")
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("running") else 1
    if command != "daemon":
        if command == "seek":
            backend_command = f"seek:{args.ratio}"
        elif command == "play":
            backend_command = f"play:{args.playback_id}"
        else:
            backend_command = command
        response = request_backend(backend_command)
        if response is None:
            response = {"ok": False, "running": False, "loggedIn": False}
        print(json.dumps(response, sort_keys=True))
        return 0 if response.get("ok") or command == "status" else 1

    configure_runtime_event_log()
    try:
        apply_graphics_workarounds()
        check = dependency_check()
        if not check["ok"]:
            print(check.get("error", "GTK/WebKit dependencies are missing"), file=sys.stderr)
            return 1
        app = build_application(show_on_start=bool(args.show))
        return app.run([sys.argv[0]])
    finally:
        close_runtime_event_log()


if __name__ == "__main__":
    raise SystemExit(main())
