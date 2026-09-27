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


def normalize_list_artwork_url(uri: object) -> str:
    if not isinstance(uri, str) or not is_allowed_artwork_uri(uri):
        return ""
    parsed = urlparse(uri)
    path = re.sub(
        r"-(?:large|original|t\d+x\d+)(\.(?:jpe?g|png))$",
        r"-t120x120\1",
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
      const publish = async (path, text, context, routeGeneration) => {
        try {
          if (!(text.length > 0 && text.length <= 2 * 1024 * 1024)) throw new Error('invalid response');
          const payload = JSON.parse(text);
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
          while (!latestRequestContext && Date.now() < waitDeadline) {
            if (generation !== playbackGeneration) throw new Error('selection superseded');
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          const id = target.substring('soundcloud:tracks:'.length);
          const context = latestRequestContext;
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
        loadItem().then((item) => originalFetch(item.endpoint, {
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
          window.webkit.messageHandlers.omarchyPlayback.postMessage(JSON.stringify({
            ok: true,
            requestId,
            streamUrl: stream.href,
            protocol,
            mimeType,
            track: item.track
          }));
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
          headers: new Headers(),
          credentials: 'include',
          requestUrl: path ? new URL(url, location.href).href : ''
        });
        return originalOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        const context = xhrRequests.get(this);
        if (context && context.path
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
      const playerPresent = !!audio;
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
      return {
        playerPresent,
        playing: playerPresent ? !audio.paused : false,
        title: playerPresent
          ? text('.playbackSoundBadge__titleLink span[aria-hidden="true"]', '.playbackSoundBadge__titleLink') : '',
        artist: playerPresent
          ? text('.playbackSoundBadge__lightLink', '.playbackSoundBadge__titleContextContainer a') : '',
        artUrl: playerPresent ? artUrl : '',
        loggedIn: !/^\/(signin|register)/.test(location.pathname) &&
          !!document.querySelector('.header__userNavButton, a[href*="/you/library"]'),
        url: location.href,
        duration: playerPresent && isFinite(audio.duration) ? audio.duration : 0,
        position: playerPresent && isFinite(audio.currentTime) ? audio.currentTime : 0
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
        **({"error": error} if error else {}),
    }


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
    }
    return scripts.get(action)


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

        result["python_gobject"] = True
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst, Gtk, WebKit2  # noqa: F401

        Gst.init(None)
        result["gstreamer_autoaudiosink"] = bool(
            Gst.ElementFactory.find("autoaudiosink")
        )
        result["gstreamer_aac_decoder"] = bool(Gst.ElementFactory.find("avdec_aac"))
        required_elements = (
            "playbin3", "appsrc", "mpegaudioparse", "mpg123audiodec",
            "audioconvert", "audioresample", "autoaudiosink", "hlsdemux2", "souphttpsrc",
        )
        missing_elements = [
            name for name in required_elements if not Gst.ElementFactory.find(name)
        ]
        result["gstreamer_playback_elements"] = not missing_elements
        if not result["gstreamer_autoaudiosink"]:
            result["error"] = "GStreamer autoaudiosink is missing; install gst-plugins-good"
        elif not result["gstreamer_aac_decoder"]:
            result["error"] = "GStreamer AAC decoder is missing; install gst-libav"
        elif missing_elements:
            result["error"] = "Missing GStreamer elements: " + ", ".join(missing_elements)
        result["ok"] = bool(
            result["gstreamer_autoaudiosink"]
            and result["gstreamer_aac_decoder"]
            and result["gstreamer_playback_elements"]
        )
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
        self.metadata = {}
        self.error = error[:128]
        self.state = "error" if self.error else "idle"
        self.on_change()

    def begin(self, metadata: dict[str, object]) -> int:
        self._stop_current()
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
                "mpg123audiodec ! audioconvert ! audioresample ! autoaudiosink"
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
                lambda: GLib.idle_add(self._publish_status_once),
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
            self.track_cache = track_cache
            self.playback_tracks = {
                track["playbackId"]: track
                for tracks in track_cache.values()
                for track in tracks
                if is_playback_id(track.get("playbackId"))
            }
            self.selection_generation = 0
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
            self.control_server.start()
            GLib.timeout_add(750, self._publish_status)
            GLib.timeout_add_seconds(5, self._check_installation)
            if show_on_start:
                self.show_login()
            else:
                self.window.show_all()
                GLib.idle_add(self.window.hide)

        def do_shutdown(self):
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
                    selected_track,
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

        def _hide_window(self, *_args):
            self.window.hide()
            return True

        def show_login(self):
            self.window.show_all()
            self.window.present()

        def _create_popup(self, source, _navigation_action):
            popup = WebKit2.WebView.new_with_related_view(source)
            popup.connect("decide-policy", self._decide_policy)
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
            if is_soundcloud_uri(uri):
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
            if self.playback.state != "idle":
                base = payload if isinstance(payload, dict) else {}
                payload = {**base, **self.playback.status()}
            state = validate_status_payload(payload)
            if state is None:
                return {}
            remote_art = state.pop("artUrl")
            state["artDataUrl"] = self.artwork_cache.get(
                remote_art, self._artwork_ready_from_thread
            )
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
            if not self.status_subscribers or self.status_evaluation_pending:
                return True
            self.status_evaluation_pending = True

            def finished(payload):
                self.status_evaluation_pending = False
                state = self._secure_status(payload)
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
            elif (source := track_source_from_command(command)) is not None:
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
            elif command in {"play-pause", "next", "previous"}:
                if command == "play-pause" and self.playback.active:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": self.playback.toggle(), "running": True},
                        publish_status=True,
                    )
                else:
                    if self.playback.active or self.playback.error:
                        self.playback.stop()
                    self._evaluate(
                        command_script(command),
                        lambda payload: self._respond(
                            connection,
                            request_id,
                            {"ok": bool(payload), "running": True},
                            publish_status=True,
                        ),
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
                    if self.playback.active:
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
            elif (playback_id := playback_id_from_command(command)) is not None:
                metadata = self.playback_tracks.get(playback_id)
                if metadata is None:
                    self._respond(
                        connection,
                        request_id,
                        {"ok": False, "error": "SoundCloud track details are not ready"},
                    )
                    return False
                self.selection_generation = (self.selection_generation + 1) % (2**31)
                generation = self.selection_generation
                self.playback.begin(metadata)
                self._publish_status_once()
                script = play_track_script(playback_id, generation)

                def track_selected(payload):
                    if generation != self.selection_generation:
                        self._respond(
                            connection,
                            request_id,
                            {"ok": False, "error": "selection superseded"},
                        )
                        return
                    if not payload:
                        self.playback.stop("SoundCloud track details are not ready")
                    self._respond(
                        connection,
                        request_id,
                        {
                            "ok": bool(payload),
                            "running": True,
                            **({} if payload else {"error": "SoundCloud track details are not ready"}),
                        },
                        publish_status=True,
                    )

                self._evaluate(script, track_selected)
                GLib.timeout_add_seconds(15, self._resolution_timed_out, generation)
            elif source_uri(command):
                self._publish_cached_tracks(command)
                self.webview.load_uri(source_uri(command))
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
    if command in {"launch", "ensure"}:
        print(json.dumps(launch_backend(show=command == "launch"), sort_keys=True))
        return 0
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
