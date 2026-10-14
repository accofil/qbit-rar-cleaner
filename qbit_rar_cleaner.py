#!/usr/bin/env python3
"""Extract on-disk RAR releases and ISO images safely.

The filesystem is authoritative for discovery. qBittorrent is used only to
prove exact file ownership, stop matching torrents, and remove their list
entries with deleteFiles=false. RAR and ISO processing are separate modes.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import requests

PART_RAR_RE = re.compile(r"^(?P<prefix>.+)\.part(?P<number>\d+)\.rar$", re.IGNORECASE)
OLD_VOLUME_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<letter>[r-z])(?P<number>\d{2,3})$", re.IGNORECASE
)
RARISH_RE = re.compile(
    r"(?:\.rar|\.part\d+\.rar|\.[r-z]\d{2,3})$", re.IGNORECASE
)
WORK_DIR_NAME = ".qbit-rar-cleaner-work"
QUARANTINE_DIR_NAME = ".qbit-rar-cleaner-quarantine"
STOPPED_STATES = {"stoppedUP", "stoppedDL", "pausedUP", "pausedDL"}
DEFAULT_PROTECTED_TRACKER_DOMAINS = ("aither.cc",)


_RUN_LOG = None
_HISTORY_LOG = None


def log_event(message: str, action: bool = False) -> None:
    """Write a timestamped line to the run log (and the history log if action)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for handle in ((_RUN_LOG, _HISTORY_LOG) if action else (_RUN_LOG,)):
        if handle is None:
            continue
        try:
            for line in str(message).splitlines() or [""]:
                prefix = f"{stamp} [{os.getpid()}] " if handle is _HISTORY_LOG else f"{stamp} "
                handle.write(f"{prefix}{line}\n")
            handle.flush()
        except (OSError, ValueError):
            pass


class _Tee:
    """Copy everything written to stdout/stderr into the run log."""

    def __init__(self, stream, label: str):
        self._stream = stream
        self._label = label
        self._pending = ""

    def write(self, data: str) -> int:
        written = self._stream.write(data)
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            log_event(f"{self._label} {line}")
        return written

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def setup_logging(
    config: dict[str, object], mode: str, args: argparse.Namespace
) -> Path | None:
    """Open <log_dir>/<mode>-<time>[-apply].log and a shared history.log."""
    global _RUN_LOG, _HISTORY_LOG
    try:
        log_dir = Path(
            str(config.get("log_dir") or Path.home() / "logs" / "qbit-rar-cleaner")
        ).expanduser()
        log_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = "apply" if args.apply else "dryrun"
        run_path = log_dir / f"{mode}-{stamp}-{suffix}.log"
        history_path = log_dir / "history.log"
        owner = None
        if os.geteuid() == 0 and config.get("service_uid") is not None:
            owner = (int(config["service_uid"]), int(config["service_gid"]))
        paths = (run_path, history_path)
        handles = []
        for path in paths:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            if owner is not None:
                os.fchown(descriptor, owner[0], owner[1])
            handles.append(os.fdopen(descriptor, "a", encoding="utf-8"))
        if owner is not None:
            os.chown(log_dir, owner[0], owner[1])
        _RUN_LOG, _HISTORY_LOG = handles
    except OSError as exc:
        print(f"FIGYELEM: a naplózás nem indítható: {exc}", file=sys.stderr)
        return None
    sys.stdout = _Tee(sys.stdout, "OUT")
    sys.stderr = _Tee(sys.stderr, "ERR")
    log_event(
        f"INDÍTÁS mód={mode} apply={args.apply} yes={args.yes} "
        f"felhasználó={os.geteuid()} parancs={' '.join(sys.argv)}",
        action=True,
    )
    log_event(f"Naplófájl: {run_path}", action=True)
    return run_path


class CleanerError(RuntimeError):
    pass


class ApiError(CleanerError):
    pass


class ArchiveError(CleanerError):
    pass


class IsoError(CleanerError):
    pass


class IsoUnmountError(IsoError):
    pass


def normalize_qbit_url(raw_url: str) -> str:
    """Validate and normalize a qBittorrent WebUI base URL."""
    value = raw_url.strip()
    if not value or any(character.isspace() for character in value):
        raise CleanerError("A qBittorrent WebUI URL üres vagy szóközt tartalmaz")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CleanerError(
            f"Érvénytelen qBittorrent WebUI URL: {value!r} ({exc})"
        ) from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise CleanerError(
            "A qBittorrent WebUI URL-nek http:// vagy https:// címmel kell kezdődnie"
        )
    if parsed.username is not None or parsed.password is not None:
        raise CleanerError("A WebUI felhasználónevet és jelszót ne írd az URL-be")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise CleanerError(
            "A qBittorrent WebUI URL csak a gépcímet és a portot tartalmazhatja"
        )
    if port is not None and not 1 <= port <= 65535:
        raise CleanerError(f"Érvénytelen qBittorrent WebUI port: {port}")

    display_host = f"[{hostname}]" if ":" in hostname else hostname
    normalized = f"{parsed.scheme.casefold()}://{display_host}"
    if port is not None:
        normalized += f":{port}"
    return normalized


@dataclass(frozen=True)
class Torrent:
    hash: str
    name: str
    state: str
    progress: float
    save_path: Path
    content_path: Path
    tracker: str = ""


@dataclass(frozen=True)
class ArchiveSet:
    first_volume: Path
    volumes: tuple[Path, ...]
    owners: frozenset[str] = frozenset()

    @property
    def byte_size(self) -> int:
        return sum(path.stat().st_size for path in self.volumes)


@dataclass(frozen=True)
class ScanResult:
    archive_sets: tuple[ArchiveSet, ...]
    orphan_volumes: tuple[Path, ...]
    rarish_files: tuple[Path, ...]


@dataclass(frozen=True)
class Component:
    archive_sets: tuple[ArchiveSet, ...]
    torrent_hashes: frozenset[str]


@dataclass(frozen=True)
class ArchiveEntry:
    relative_path: Path
    is_directory: bool
    size: int


@dataclass(frozen=True)
class IsoImage:
    path: Path
    owners: frozenset[str] = frozenset()
    existing_bdmv: Path | None = None
    replace_bdmv: tuple[Path, ...] = ()

    @property
    def byte_size(self) -> int:
        return self.path.stat().st_size


@dataclass(frozen=True)
class IsoComponent:
    images: tuple[IsoImage, ...]
    torrent_hashes: frozenset[str]


@dataclass(frozen=True)
class MountedEntry:
    source: Path
    relative_path: Path
    is_directory: bool
    size: int


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_existing_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise CleanerError(f"Nem szabályos fájl: {path}")
    if not is_within(resolved, root):
        raise CleanerError(f"A fájl kilóg a scan root alól: {resolved}")
    return resolved


def filter_scanned_directories(current: Path, names: list[str]) -> None:
    names[:] = [
        name
        for name in names
        if name not in {WORK_DIR_NAME, QUARANTINE_DIR_NAME}
        and not (current / name).is_symlink()
    ]


def _volume_sort_key(path: Path) -> tuple[int, int, str]:
    part_match = PART_RAR_RE.match(path.name)
    if part_match:
        return (0, int(part_match.group("number")), path.name.casefold())
    if path.suffix.casefold() == ".rar":
        return (1, -1, path.name.casefold())
    old_match = OLD_VOLUME_RE.match(path.name)
    if old_match:
        letter_offset = ord(old_match.group("letter").casefold()) - ord("r")
        return (2 + letter_offset, int(old_match.group("number")), path.name.casefold())
    return (99, 0, path.name.casefold())


def scan_rar_sets(scan_root: Path) -> ScanResult:
    """Discover RAR sets from the real filesystem, never from torrent names."""
    by_directory: dict[Path, list[Path]] = defaultdict(list)

    def walk_error(error: OSError) -> None:
        raise CleanerError(f"A könyvtárfa nem olvasható teljesen: {error}")

    for dir_path, dir_names, file_names in os.walk(
        scan_root, followlinks=False, onerror=walk_error
    ):
        current = Path(dir_path)
        filter_scanned_directories(current, dir_names)
        for file_name in file_names:
            if not RARISH_RE.search(file_name):
                continue
            candidate = current / file_name
            if candidate.is_symlink():
                continue
            by_directory[current].append(resolve_existing_file(candidate, scan_root))

    archive_sets: list[ArchiveSet] = []
    orphans: set[Path] = set()
    all_rarish: set[Path] = set()

    for directory, paths in by_directory.items():
        del directory
        all_rarish.update(paths)
        modern: dict[str, list[tuple[int, Path]]] = defaultdict(list)
        normal_rars: dict[str, list[Path]] = defaultdict(list)
        old_continuations: dict[str, list[Path]] = defaultdict(list)

        for path in paths:
            part_match = PART_RAR_RE.match(path.name)
            if part_match:
                modern[part_match.group("prefix").casefold()].append(
                    (int(part_match.group("number")), path)
                )
                continue
            old_match = OLD_VOLUME_RE.match(path.name)
            if old_match:
                old_continuations[old_match.group("stem").casefold()].append(path)
                continue
            if path.suffix.casefold() == ".rar":
                normal_rars[path.stem.casefold()].append(path)

        claimed: set[Path] = set()

        for numbered_paths in modern.values():
            firsts = [path for number, path in numbered_paths if number == 1]
            if len(firsts) != 1:
                orphans.update(path for _, path in numbered_paths)
                continue
            volumes = tuple(path for _, path in sorted(numbered_paths, key=lambda item: item[0]))
            archive_sets.append(ArchiveSet(firsts[0], volumes))
            claimed.update(volumes)

        for stem_key, firsts in normal_rars.items():
            if len(firsts) != 1:
                orphans.update(firsts)
                orphans.update(old_continuations.get(stem_key, ()))
                continue
            volumes = tuple(
                sorted(
                    [firsts[0], *old_continuations.get(stem_key, ())],
                    key=_volume_sort_key,
                )
            )
            archive_sets.append(ArchiveSet(firsts[0], volumes))
            claimed.update(volumes)

        orphans.update(path for path in paths if path not in claimed)

    return ScanResult(
        archive_sets=tuple(sorted(archive_sets, key=lambda item: str(item.first_volume))),
        orphan_volumes=tuple(sorted(orphans)),
        rarish_files=tuple(sorted(all_rarish)),
    )


def scan_iso_files(scan_root: Path) -> tuple[Path, ...]:
    """Discover real, non-symlink ISO files recursively from the filesystem."""
    images: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise CleanerError(f"A könyvtárfa nem olvasható teljesen: {error}")

    for dir_path, dir_names, file_names in os.walk(
        scan_root, followlinks=False, onerror=walk_error
    ):
        current = Path(dir_path)
        filter_scanned_directories(current, dir_names)
        for file_name in file_names:
            if Path(file_name).suffix.casefold() != ".iso":
                continue
            candidate = current / file_name
            if candidate.is_symlink():
                continue
            images.append(resolve_existing_file(candidate, scan_root))
    return tuple(sorted(images))


def case_insensitive_children(parent: Path, name: str) -> tuple[Path, ...]:
    try:
        with os.scandir(parent) as children:
            matches = [
                Path(item.path)
                for item in children
                if item.name.casefold() == name.casefold()
            ]
    except OSError as exc:
        raise CleanerError(f"A könyvtár nem olvasható: {parent}: {exc}") from exc
    return tuple(sorted(matches))


def case_insensitive_child(parent: Path, name: str) -> Path | None:
    matches = case_insensitive_children(parent, name)
    if len(matches) != 1:
        return None
    return matches[0]


def bdmv_structure_state(
    release_directory: Path,
) -> tuple[Path | None, tuple[Path, ...]]:
    """Return either one complete BDMV or entries that must be replaced."""
    entries = case_insensitive_children(release_directory, "BDMV")
    if len(entries) != 1:
        return None, entries
    bdmv = entries[0]
    if bdmv.is_symlink() or not bdmv.is_dir():
        return None, entries
    for file_name in ("index.bdmv", "MovieObject.bdmv"):
        path = case_insensitive_child(bdmv, file_name)
        if path is None or path.is_symlink() or not path.is_file():
            return None, entries
    for directory_name in ("PLAYLIST", "CLIPINF", "STREAM"):
        path = case_insensitive_child(bdmv, directory_name)
        if path is None or path.is_symlink() or not path.is_dir():
            return None, entries
    return bdmv.resolve(strict=True), ()


def find_complete_bdmv_structure(release_directory: Path) -> Path | None:
    return bdmv_structure_state(release_directory)[0]


class QbitClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = normalize_qbit_url(base_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        # This is a direct qBittorrent WebUI administration connection. Do not
        # route credentials through inherited HTTP(S)_PROXY settings, which can
        # also contain malformed IPv6 proxy URLs on long-lived shell sessions.
        self.session.trust_env = False

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v2/{path.lstrip('/')}"

    def login(self) -> None:
        response = self.session.post(
            self._url("auth/login"),
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise ApiError(
                f"qBittorrent bejelentkezési hiba: HTTP {response.status_code}"
            )

    def torrents(self, hashes: Sequence[str] | None = None) -> list[Torrent]:
        # Include incomplete torrents too: they must protect any shared RAR files
        # from being deleted under them.
        params: dict[str, str] = {"filter": "all"}
        if hashes:
            params["hashes"] = "|".join(hashes)
        response = self.session.get(
            self._url("torrents/info"), params=params, timeout=self.timeout
        )
        if response.status_code != 200:
            raise ApiError(f"Torrentlista lekérése sikertelen: HTTP {response.status_code}")
        result: list[Torrent] = []
        for item in response.json():
            result.append(
                Torrent(
                    hash=str(item["hash"]),
                    name=str(item["name"]),
                    state=str(item.get("state", "")),
                    progress=float(item.get("progress", 0)),
                    save_path=Path(str(item["save_path"])),
                    content_path=Path(str(item.get("content_path") or item["save_path"])),
                    tracker=str(item.get("tracker", "")),
                )
            )
        return result

    def files(self, torrent_hash: str) -> list[str]:
        response = self.session.get(
            self._url("torrents/files"),
            params={"hash": torrent_hash},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise ApiError(
                f"Fájllista lekérése sikertelen ({torrent_hash}): HTTP "
                f"{response.status_code}"
            )
        return [str(item["name"]) for item in response.json()]

    def trackers(self, torrent_hash: str) -> list[str]:
        response = self.session.get(
            self._url("torrents/trackers"),
            params={"hash": torrent_hash},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise ApiError(
                f"Trackerlista lekérése sikertelen ({torrent_hash}): HTTP "
                f"{response.status_code}"
            )
        return [str(item.get("url", "")) for item in response.json()]

    def _post_hashes(self, endpoint: str, hashes: Sequence[str], **data: str) -> None:
        payload = {"hashes": "|".join(hashes), **data}
        response = self.session.post(
            self._url(endpoint), data=payload, timeout=self.timeout
        )
        log_event(
            f"QBIT {endpoint} hashes={payload['hashes']} "
            f"{ {k: v for k, v in data.items()} } -> HTTP {response.status_code}",
            action=True,
        )
        if response.status_code != 200:
            raise ApiError(f"qBittorrent {endpoint} sikertelen: HTTP {response.status_code}")

    def stop(self, hashes: Sequence[str]) -> None:
        self._post_hashes("torrents/stop", hashes)

    def start(self, hashes: Sequence[str]) -> None:
        self._post_hashes("torrents/start", hashes)

    def remove_list_only(self, hashes: Sequence[str]) -> None:
        # Safety invariant: this method must never accept or send deleteFiles=true.
        self._post_hashes("torrents/delete", hashes, deleteFiles="false")


def qbit_relative_path_is_safe(name: str) -> bool:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return not pure.is_absolute() and ".." not in pure.parts and bool(pure.parts)


def map_torrent_files(
    torrent: Torrent, names: Iterable[str], scan_root: Path
) -> set[Path]:
    save_path = torrent.save_path.resolve(strict=False)
    mapped: set[Path] = set()
    for name in names:
        if not qbit_relative_path_is_safe(name):
            raise CleanerError(f"Nem biztonságos qBittorrent fájlútvonal: {name!r}")
        candidate = (save_path / Path(name.replace("\\", "/"))).resolve(strict=False)
        if is_within(candidate, scan_root):
            mapped.add(candidate)
    return mapped


def torrent_may_cover_root(torrent: Torrent, scan_root: Path) -> bool:
    save_path = torrent.save_path.resolve(strict=False)
    content_path = torrent.content_path.resolve(strict=False)
    return is_within(save_path, scan_root) or is_within(content_path, scan_root)


def tracker_url_matches_domain(url: str, domain: str) -> bool:
    value = url.strip()
    if not value:
        return False
    try:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        hostname = parsed.hostname
    except ValueError:
        # qBittorrent can retain a syntactically broken tracker URL (most
        # commonly an unmatched IPv6 bracket). It cannot prove a protected
        # domain match and must not abort the complete disk scan.
        return False
    host = (hostname or "").rstrip(".").casefold()
    expected = domain.strip().rstrip(".").casefold()
    return bool(host and expected) and (host == expected or host.endswith(f".{expected}"))


def torrent_uses_protected_tracker(
    torrent: Torrent,
    client: QbitClient,
    protected_domains: Sequence[str],
) -> bool:
    urls = [torrent.tracker]
    tracker_reader = getattr(client, "trackers", None)
    if callable(tracker_reader):
        urls.extend(tracker_reader(torrent.hash))
    return any(
        tracker_url_matches_domain(url, domain)
        for url in urls
        for domain in protected_domains
    )


def cross_reference(
    scan: ScanResult,
    torrents: Sequence[Torrent],
    client: QbitClient,
    scan_root: Path,
    protected_domains: Sequence[str] = DEFAULT_PROTECTED_TRACKER_DOMAINS,
) -> tuple[tuple[ArchiveSet, ...], frozenset[str], dict[str, Torrent]]:
    """Match already-discovered disk files to exact qBittorrent file paths."""
    rarish = set(scan.rarish_files)
    owners_by_path: dict[Path, set[str]] = defaultdict(set)
    torrent_by_hash = {torrent.hash: torrent for torrent in torrents}

    listed_rarish_by_hash: dict[str, set[Path]] = defaultdict(set)
    for torrent in torrents:
        if not torrent_may_cover_root(torrent, scan_root):
            continue
        names = client.files(torrent.hash)
        mapped = map_torrent_files(torrent, names, scan_root)
        for path in rarish.intersection(mapped):
            owners_by_path[path].add(torrent.hash)
        rar_names = [name for name in names if RARISH_RE.search(Path(name).name)]
        listed_rarish_by_hash[torrent.hash].update(
            map_torrent_files(torrent, rar_names, scan_root)
        )

    blocked_hashes: set[str] = set()
    relevant_hashes = {
        torrent_hash
        for owners in owners_by_path.values()
        for torrent_hash in owners
    }
    protected_hashes = {
        torrent_hash
        for torrent_hash in relevant_hashes
        if torrent_uses_protected_tracker(
            torrent_by_hash[torrent_hash], client, protected_domains
        )
    }
    for orphan in scan.orphan_volumes:
        blocked_hashes.update(owners_by_path.get(orphan, ()))

    for torrent_hash, listed_paths in listed_rarish_by_hash.items():
        if listed_paths.difference(rarish):
            blocked_hashes.add(torrent_hash)

    candidates: list[ArchiveSet] = []
    for archive_set in scan.archive_sets:
        owner_sets = [owners_by_path.get(volume, set()) for volume in archive_set.volumes]
        if not owner_sets or any(not owners for owners in owner_sets):
            continue
        common_owners = set.intersection(*owner_sets)
        all_owners = set.union(*owner_sets)
        if not common_owners or common_owners != all_owners:
            blocked_hashes.update(all_owners)
            continue
        if all_owners.intersection(protected_hashes):
            blocked_hashes.update(all_owners)
            continue
        if any(torrent_by_hash[item].progress < 1 for item in common_owners):
            blocked_hashes.update(common_owners)
            continue
        candidates.append(replace(archive_set, owners=frozenset(common_owners)))

    # Propagate a block across shared sets. No processed volume may remain in a
    # torrent entry that is intentionally being kept.
    changed = True
    while changed:
        changed = False
        for archive_set in candidates:
            if archive_set.owners.intersection(blocked_hashes):
                before = len(blocked_hashes)
                blocked_hashes.update(archive_set.owners)
                changed = changed or len(blocked_hashes) != before

    matched_sets = [
        archive_set
        for archive_set in candidates
        if not archive_set.owners.intersection(blocked_hashes)
    ]

    return tuple(matched_sets), frozenset(blocked_hashes), torrent_by_hash


def build_components(archive_sets: Sequence[ArchiveSet]) -> tuple[Component, ...]:
    remaining = set(range(len(archive_sets)))
    components: list[Component] = []
    while remaining:
        pending = {remaining.pop()}
        set_indexes: set[int] = set()
        torrent_hashes: set[str] = set()
        while pending:
            index = pending.pop()
            set_indexes.add(index)
            torrent_hashes.update(archive_sets[index].owners)
            connected = {
                other
                for other in remaining
                if archive_sets[other].owners.intersection(torrent_hashes)
            }
            if connected:
                remaining.difference_update(connected)
                pending.update(connected)
        components.append(
            Component(
                archive_sets=tuple(archive_sets[index] for index in sorted(set_indexes)),
                torrent_hashes=frozenset(torrent_hashes),
            )
        )
    return tuple(components)


def cross_reference_isos(
    disk_images: Sequence[Path],
    torrents: Sequence[Torrent],
    client: QbitClient,
    scan_root: Path,
    protected_domains: Sequence[str] = DEFAULT_PROTECTED_TRACKER_DOMAINS,
) -> tuple[
    tuple[IsoImage, ...],
    frozenset[str],
    tuple[Path, ...],
    dict[str, Torrent],
]:
    """Match on-disk ISOs to qBittorrent, while allowing ownerless images.

    Ownership is exact-file based: a separate torrent containing an already
    extracted BDMV tree in the same release directory never owns the ISO and
    therefore must never be selected for removal because of that ISO.
    """
    image_set = set(disk_images)
    owners_by_path: dict[Path, set[str]] = defaultdict(set)
    listed_isos_by_hash: dict[str, set[Path]] = defaultdict(set)
    torrent_by_hash = {torrent.hash: torrent for torrent in torrents}

    for torrent in torrents:
        if not torrent_may_cover_root(torrent, scan_root):
            continue
        names = client.files(torrent.hash)
        mapped = map_torrent_files(torrent, names, scan_root)
        for path in image_set.intersection(mapped):
            owners_by_path[path].add(torrent.hash)
        iso_names = [
            name for name in names if Path(name.replace("\\", "/")).suffix.casefold() == ".iso"
        ]
        listed_isos_by_hash[torrent.hash].update(
            map_torrent_files(torrent, iso_names, scan_root)
        )

    relevant_hashes = {
        torrent_hash
        for owners in owners_by_path.values()
        for torrent_hash in owners
    }
    protected_hashes = {
        torrent_hash
        for torrent_hash in relevant_hashes
        if torrent_uses_protected_tracker(
            torrent_by_hash[torrent_hash], client, protected_domains
        )
    }
    blocked_hashes: set[str] = set(protected_hashes)
    for torrent_hash, listed_paths in listed_isos_by_hash.items():
        if torrent_hash in relevant_hashes and listed_paths.difference(image_set):
            blocked_hashes.add(torrent_hash)

    candidates: list[IsoImage] = []
    for path in disk_images:
        owners = owners_by_path.get(path, set())
        existing_bdmv, replace_bdmv = bdmv_structure_state(path.parent)
        if not owners:
            candidates.append(
                IsoImage(
                    path,
                    existing_bdmv=existing_bdmv,
                    replace_bdmv=replace_bdmv,
                )
            )
            continue
        if any(torrent_by_hash[item].progress < 1 for item in owners):
            blocked_hashes.update(owners)
            continue
        if owners.intersection(protected_hashes):
            blocked_hashes.update(owners)
            continue
        candidates.append(
            IsoImage(
                path,
                frozenset(owners),
                existing_bdmv,
                replace_bdmv,
            )
        )

    changed = True
    while changed:
        changed = False
        for image in candidates:
            if image.owners.intersection(blocked_hashes):
                before = len(blocked_hashes)
                blocked_hashes.update(image.owners)
                changed = changed or len(blocked_hashes) != before

    matched = tuple(
        image for image in candidates if not image.owners.intersection(blocked_hashes)
    )
    blocked_paths = tuple(
        sorted(
            path
            for path in disk_images
            if owners_by_path.get(path, set()).intersection(blocked_hashes)
        )
    )
    return matched, frozenset(blocked_hashes), blocked_paths, torrent_by_hash


def build_iso_components(images: Sequence[IsoImage]) -> tuple[IsoComponent, ...]:
    ownerless = [
        IsoComponent((image,), frozenset()) for image in images if not image.owners
    ]
    owned = [image for image in images if image.owners]
    remaining = set(range(len(owned)))
    components: list[IsoComponent] = []
    while remaining:
        pending = {remaining.pop()}
        image_indexes: set[int] = set()
        torrent_hashes: set[str] = set()
        while pending:
            index = pending.pop()
            image_indexes.add(index)
            torrent_hashes.update(owned[index].owners)
            connected = {
                other
                for other in remaining
                if owned[other].owners.intersection(torrent_hashes)
            }
            if connected:
                remaining.difference_update(connected)
                pending.update(connected)
        components.append(
            IsoComponent(
                images=tuple(owned[index] for index in sorted(image_indexes)),
                torrent_hashes=frozenset(torrent_hashes),
            )
        )
    return tuple(sorted([*ownerless, *components], key=lambda item: str(item.images[0].path)))


def _safe_archive_member(raw_path: str) -> Path:
    if not raw_path:
        raise ArchiveError("Üres fájlnév az archívumban")
    posix = PurePosixPath(raw_path.replace("\\", "/"))
    windows = PureWindowsPath(raw_path)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or not posix.parts
    ):
        raise ArchiveError(f"Nem biztonságos útvonal az archívumban: {raw_path!r}")
    return Path(*posix.parts)


def expected_volume_names(archive_set: ArchiveSet) -> dict[Path, str]:
    """Names 7-Zip will look up for every volume, derived from the first one.

    7-Zip builds the names of the following volumes from the first volume's
    name and opens them case-sensitively.  Some releases mix the case of the
    volume names (``….pfa.r01`` next to ``….PFa.r02``), which fails with
    "Missing volume" although every volume is present.
    """

    first = archive_set.first_volume
    names: dict[Path, str] = {}
    part_first = PART_RAR_RE.match(first.name)
    for volume in archive_set.volumes:
        if volume == first:
            names[volume] = first.name
            continue
        if part_first is not None:
            part = PART_RAR_RE.match(volume.name)
            if part is None:
                raise ArchiveError(f"Váratlan kötetnév: {volume.name}")
            prefix = part_first.group("prefix")
            tail = first.name[len(prefix):]
            width = len(part_first.group("number"))
            names[volume] = prefix + re.sub(
                r"\d+", f"{int(part.group('number')):0{width}d}", tail, count=1
            )
            continue
        old = OLD_VOLUME_RE.match(volume.name)
        if old is None:
            raise ArchiveError(f"Váratlan kötetnév: {volume.name}")
        letter = old.group("letter")
        letter = letter.lower() if first.suffix == first.suffix.lower() else letter.upper()
        names[volume] = f"{first.stem}.{letter}{old.group('number')}"
    return names


@contextlib.contextmanager
def openable_first_volume(archive_set: ArchiveSet, links_root: Path) -> Iterator[Path]:
    """Yield the first-volume path to hand to 7-Zip.

    Normally that is the real file.  When volume names differ from what 7-Zip
    will look for only in case, a private directory of symlinks with the
    expected names is built (the real files are never renamed or touched).
    """

    expected = expected_volume_names(archive_set)
    if all(volume.name == name for volume, name in expected.items()):
        yield archive_set.first_volume
        return

    chosen: dict[str, Path] = {}
    for volume, name in sorted(expected.items(), key=lambda item: str(item[0])):
        current = chosen.get(name)
        if current is None:
            chosen[name] = volume
            continue
        if current.stat().st_size != volume.stat().st_size:
            raise ArchiveError(
                f"Két eltérő méretű kötet ugyanazt a kötetnevet adja: {current.name}, {volume.name}"
            )
        if volume.name == name:
            chosen[name] = volume  # prefer the file whose name is already exact

    link_dir = links_root / uuid.uuid4().hex
    link_dir.mkdir(parents=True, mode=0o700)
    try:
        for name, volume in chosen.items():
            (link_dir / name).symlink_to(volume)
        log_event(
            f"KÖTETNÉV-ALIAS: {len(chosen)} kötet szimlinkelve a 7-Zip számára "
            f"(kisbetű/nagybetű eltérés): {archive_set.first_volume}",
            action=True,
        )
        yield link_dir / archive_set.first_volume.name
    finally:
        shutil.rmtree(link_dir, ignore_errors=True)


class SevenZip:
    def __init__(self, executable: str):
        self.executable = executable

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        completed = subprocess.run(
            [self.executable, *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
        log_event(
            f"7Z {' '.join(arguments)} -> exit={completed.returncode}", action=True
        )
        if completed.returncode != 0:
            log_event(f"7Z kimenet:\n{completed.stdout}")
            tail = "\n".join(completed.stdout.splitlines()[-20:])
            raise ArchiveError(
                f"7-Zip hiba (exit={completed.returncode}):\n{tail}"
            )
        return completed

    def test(self, first_volume: Path) -> None:
        self._run(["t", "-y", "-bb0", "--", str(first_volume)])

    def entries(self, first_volume: Path) -> tuple[ArchiveEntry, ...]:
        output = self._run(["l", "-slt", "--", str(first_volume)]).stdout
        after_separator = False
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if line.startswith("----------"):
                after_separator = True
                current = {}
                continue
            if not after_separator:
                continue
            if not line.strip():
                if current:
                    records.append(current)
                    current = {}
                continue
            if " = " in line:
                key, value = line.split(" = ", 1)
                current[key] = value
        if current:
            records.append(current)

        entries: list[ArchiveEntry] = []
        for record in records:
            if "Path" not in record:
                continue
            if any("symbolic link" in key.casefold() for key in record):
                raise ArchiveError("Az archívum szimbolikus linket tartalmaz")
            relative = _safe_archive_member(record["Path"])
            attributes = record.get("Attributes", "")
            is_directory = attributes.startswith("D") or record.get("Folder") == "+"
            try:
                size = int(record.get("Size", "0"))
            except ValueError as exc:
                raise ArchiveError("Érvénytelen méretadat a RAR-listában") from exc
            entries.append(ArchiveEntry(relative, is_directory, size))
        if not entries:
            raise ArchiveError(f"Az archívum nem tartalmaz listázható elemet: {first_volume}")
        return tuple(entries)

    def extract(self, first_volume: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        self._run(
            ["x", "-y", "-aoa", "-bb0", f"-o{destination}", "--", str(first_volume)]
        )


class IsoMounter:
    def __init__(self, mount_command: str, umount_command: str):
        self.mount_command = mount_command
        self.umount_command = umount_command

    @staticmethod
    def _run(command: Sequence[str]) -> None:
        completed = subprocess.run(
            list(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log_event(
            f"CMD {' '.join(command)} -> exit={completed.returncode}", action=True
        )
        if completed.returncode != 0:
            log_event(f"CMD kimenet:\n{completed.stdout}")
            tail = "\n".join(completed.stdout.splitlines()[-20:])
            raise IsoError(
                f"Parancshiba (exit={completed.returncode}): {' '.join(command[:2])}\n{tail}"
            )

    def mount(self, image: Path, mount_point: Path) -> None:
        mount_point.mkdir(parents=True, exist_ok=False, mode=0o700)
        self._run(
            [
                self.mount_command,
                "-o",
                "loop,ro,nosuid,nodev,noexec",
                "--",
                str(image),
                str(mount_point),
            ]
        )

    def unmount(self, mount_point: Path) -> None:
        try:
            self._run([self.umount_command, "--", str(mount_point)])
        except IsoError as exc:
            raise IsoUnmountError(f"Az ISO unmount sikertelen: {mount_point}: {exc}") from exc


def inspect_mounted_tree(mount_root: Path) -> tuple[MountedEntry, ...]:
    entries: list[MountedEntry] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise IsoError(f"Az ISO tartalma nem olvasható: {directory}: {exc}") from exc
        for child in children:
            source = Path(child.path)
            relative = source.relative_to(mount_root)
            if child.is_symlink():
                raise IsoError(f"Az ISO szimbolikus linket tartalmaz: {relative}")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise IsoError(f"Az ISO bejegyzése nem olvasható: {relative}: {exc}") from exc
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(MountedEntry(source, relative, True, 0))
                visit(source)
            elif stat.S_ISREG(metadata.st_mode):
                entries.append(
                    MountedEntry(source, relative, False, int(metadata.st_size))
                )
            else:
                raise IsoError(f"Az ISO nem támogatott fájltípust tartalmaz: {relative}")

    visit(mount_root)
    return tuple(entries)


def copy_mounted_entries(entries: Sequence[MountedEntry], stage_root: Path) -> None:
    stage_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for entry in entries:
        destination = stage_root / entry.relative_path
        if entry.is_directory:
            destination.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with entry.source.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(destination, 0o644)
        if destination.stat().st_size != entry.size:
            raise IsoError(f"Az ISO-fájl másolási mérete eltér: {entry.relative_path}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_identical(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    return sha256(first) == sha256(second)


def assert_no_symlinks(root: Path) -> None:
    for dir_path, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(dir_path)
        for name in [*dir_names, *file_names]:
            if (current / name).is_symlink():
                raise ArchiveError("A kicsomagolt tartalom szimbolikus linket tartalmaz")


def assert_destination_chain_safe(base: Path, relative: Path) -> None:
    current = base
    if current.is_symlink():
        raise ArchiveError(f"Szimbolikus link a célútvonalban: {current}")
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArchiveError(f"Szimbolikus link a célútvonalban: {current}")


@dataclass(frozen=True)
class StagedFile:
    source: Path
    destination: Path


@dataclass(frozen=True)
class StagedDirectory:
    destination: Path


def prepare_staged_directories(
    stages: Sequence[tuple[Path, Path]], scan_root: Path
) -> tuple[StagedDirectory, ...]:
    destinations: set[Path] = set()
    for stage_root, destination_base in stages:
        assert_no_symlinks(stage_root)
        for dir_path, dir_names, _ in os.walk(stage_root, followlinks=False):
            current = Path(dir_path)
            for dir_name in dir_names:
                source = current / dir_name
                relative = source.relative_to(stage_root)
                assert_destination_chain_safe(destination_base, relative)
                destination = (destination_base / relative).resolve(strict=False)
                if not is_within(destination, scan_root):
                    raise ArchiveError(
                        f"A kicsomagolási cél kilóg a scan root alól: {destination}"
                    )
                if destination.exists() and (
                    destination.is_symlink() or not destination.is_dir()
                ):
                    raise ArchiveError(f"Nem létrehozható célkönyvtár: {destination}")
                destinations.add(destination)
    return tuple(
        StagedDirectory(destination)
        for destination in sorted(
            destinations, key=lambda path: (len(path.parts), str(path))
        )
    )


def prepare_staged_files(
    stages: Sequence[tuple[Path, Path]], scan_root: Path
) -> tuple[StagedFile, ...]:
    by_destination: dict[Path, Path] = {}
    staged: list[StagedFile] = []
    for stage_root, destination_base in stages:
        assert_no_symlinks(stage_root)
        for dir_path, _, file_names in os.walk(stage_root, followlinks=False):
            current = Path(dir_path)
            for file_name in file_names:
                source = current / file_name
                if source.is_symlink() or not source.is_file():
                    raise ArchiveError(f"Nem támogatott kicsomagolt fájltípus: {source}")
                relative = source.relative_to(stage_root)
                assert_destination_chain_safe(destination_base, relative)
                destination = (destination_base / relative).resolve(strict=False)
                if not is_within(destination, scan_root):
                    raise ArchiveError(f"A kicsomagolási cél kilóg a scan root alól: {destination}")
                previous = by_destination.get(destination)
                if previous is not None:
                    if not files_identical(previous, source):
                        raise ArchiveError(
                            f"Két archívum eltérő tartalmat írna ugyanoda: {destination}"
                        )
                    source.unlink()
                    continue
                if destination.exists():
                    if destination.is_symlink() or not destination.is_file():
                        raise ArchiveError(f"Nem felülírható cél létezik: {destination}")
                    if not files_identical(source, destination):
                        raise ArchiveError(f"Eltérő tartalmú célfájl már létezik: {destination}")
                by_destination[destination] = source
                staged.append(StagedFile(source, destination))
    return tuple(staged)


def validate_staged_destinations(
    directories: Sequence[StagedDirectory], files: Sequence[StagedFile]
) -> None:
    directory_paths = {item.destination for item in directories}
    file_paths = {item.destination for item in files}
    conflict = directory_paths.intersection(file_paths)
    if conflict:
        raise ArchiveError(f"Fájl/könyvtár célütközés: {min(conflict)}")
    for destination in file_paths:
        if any(parent in file_paths for parent in destination.parents):
            raise ArchiveError(f"Egymásba ágyazott fájlcélok ütköznek: {destination}")


def copy_file_atomic(
    source: Path,
    destination: Path,
    owner: tuple[int, int] | None = None,
) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.qbit-cleaner-", dir=destination.parent
    )
    temporary = Path(temp_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as target_handle:
            descriptor = -1
            shutil.copyfileobj(
                source_handle, target_handle, length=8 * 1024 * 1024
            )
            target_handle.flush()
            os.fsync(target_handle.fileno())
            source_mode = stat.S_IMODE(source.stat().st_mode) & 0o777
            os.fchmod(target_handle.fileno(), source_mode or 0o644)
            if owner is not None:
                os.fchown(target_handle.fileno(), owner[0], owner[1])
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def move_file_cross_device(
    source: Path,
    destination: Path,
    owner: tuple[int, int] | None = None,
) -> None:
    log_event(f"MOVE {source} -> {destination}")
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        copy_file_atomic(source, destination, owner)
        try:
            source.unlink()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return
    if owner is not None:
        try:
            os.chown(destination, owner[0], owner[1])
        except Exception:
            os.replace(destination, source)
            raise


def merge_staged_directories(
    staged: Sequence[StagedDirectory],
    owner: tuple[int, int] | None = None,
) -> tuple[Path, ...]:
    created: list[Path] = []
    try:
        for item in staged:
            if item.destination.exists():
                continue
            item.destination.mkdir(mode=0o755)
            if owner is not None:
                os.chown(item.destination, owner[0], owner[1])
            created.append(item.destination)
    except Exception:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return tuple(created)


def merge_staged_files(
    staged: Sequence[StagedFile],
    owner: tuple[int, int] | None = None,
) -> None:
    moved: list[StagedFile] = []
    try:
        for item in staged:
            if item.destination.exists():
                if item.destination.is_symlink() or not item.destination.is_file():
                    raise ArchiveError(f"Nem felülírható cél létezik: {item.destination}")
                if not files_identical(item.source, item.destination):
                    raise ArchiveError(
                        f"Eltérő tartalmú célfájl már létezik: {item.destination}"
                    )
                log_event(
                    f"SKIP (azonos tartalmú cél már létezik): {item.destination}"
                )
                item.source.unlink()
                continue
            move_file_cross_device(item.source, item.destination, owner)
            moved.append(item)
    except Exception:
        for item in reversed(moved):
            if item.destination.exists() and not item.source.exists():
                item.source.parent.mkdir(parents=True, exist_ok=True)
                move_file_cross_device(item.destination, item.source)
        raise


def merge_staged_tree(
    stages: Sequence[tuple[Path, Path]],
    scan_root: Path,
    owner: tuple[int, int] | None = None,
) -> None:
    directories = prepare_staged_directories(stages, scan_root)
    files = prepare_staged_files(stages, scan_root)
    validate_staged_destinations(directories, files)
    created_directories: tuple[Path, ...] = ()
    try:
        created_directories = merge_staged_directories(directories, owner)
        merge_staged_files(files, owner)
    except Exception:
        for directory in sorted(
            created_directories, key=lambda path: len(path.parts), reverse=True
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def wait_for_stopped(client: QbitClient, hashes: Sequence[str], timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.torrents(hashes)
        if len(current) == len(hashes) and all(item.state in STOPPED_STATES for item in current):
            return
        time.sleep(1)
    raise ApiError("A torrentek nem álltak le időben")


def wait_for_removed(client: QbitClient, hashes: Sequence[str], timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.torrents(hashes):
            return
        time.sleep(1)
    raise ApiError("A torrentek nem tűntek el időben a qBittorrent listájáról")


def restore_quarantine(moves: Sequence[tuple[Path, Path]]) -> None:
    for original, quarantined in reversed(moves):
        if os.path.lexists(quarantined) and not os.path.lexists(original):
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantined, original)


def remove_empty_quarantine(quarantine_root: Path) -> None:
    if quarantine_root.exists():
        for dir_path, _, _ in os.walk(quarantine_root, topdown=False):
            try:
                Path(dir_path).rmdir()
            except OSError:
                pass
    try:
        quarantine_root.parent.rmdir()
    except OSError:
        pass


def process_component(
    component: Component,
    torrent_by_hash: dict[str, Torrent],
    client: QbitClient,
    extractor: SevenZip,
    scan_root: Path,
    work_root: Path,
    min_free_bytes: int,
) -> tuple[bool, str]:
    work_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    transaction_id = f"txn-{uuid.uuid4().hex}"
    transaction = work_root / transaction_id
    extract_root = transaction / "extract"
    links_root = work_root / f"{transaction_id}-links"
    quarantine_root = scan_root / QUARANTINE_DIR_NAME / transaction_id
    hashes = sorted(component.torrent_hashes)
    originally_running = [
        torrent_hash
        for torrent_hash in hashes
        if torrent_by_hash[torrent_hash].state not in STOPPED_STATES
    ]
    quarantine_moves: list[tuple[Path, Path]] = []
    removed_from_qbit = False
    log_event(
        f"RAR TRANZAKCIÓ {transaction_id} indul; torrentek: "
        + "; ".join(f"{h} {torrent_by_hash[h].name}" for h in hashes)
        + "; archívumok: "
        + "; ".join(str(a.first_volume) for a in component.archive_sets),
        action=True,
    )

    try:
        expected_bytes = 0
        entries_by_set: list[tuple[ArchiveSet, tuple[ArchiveEntry, ...]]] = []
        for archive_set in component.archive_sets:
            with openable_first_volume(archive_set, links_root) as openable:
                extractor.test(openable)
                entries = extractor.entries(openable)
            expected_bytes += sum(entry.size for entry in entries if not entry.is_directory)
            entries_by_set.append((archive_set, entries))

        work_free_bytes = shutil.disk_usage(work_root).free
        storage_free_bytes = shutil.disk_usage(scan_root).free
        if work_free_bytes - expected_bytes < min_free_bytes:
            raise ArchiveError(
                "Nincs elég hely a munkakönyvtárban a kicsomagoláshoz: "
                f"szükséges={expected_bytes}, szabad={work_free_bytes}, "
                f"tartalék={min_free_bytes}"
            )
        if work_root.stat().st_dev != scan_root.stat().st_dev and (
            storage_free_bytes - expected_bytes < min_free_bytes
        ):
            raise ArchiveError(
                "Nincs elég hely a storage-on a kibontott fájlokhoz: "
                f"szükséges={expected_bytes}, szabad={storage_free_bytes}, "
                f"tartalék={min_free_bytes}"
            )

        transaction.mkdir(parents=True, exist_ok=False, mode=0o700)
        stages: list[tuple[Path, Path]] = []
        for index, (archive_set, _) in enumerate(entries_by_set):
            stage = extract_root / str(index)
            with openable_first_volume(archive_set, links_root) as openable:
                extractor.extract(openable, stage)
            stages.append((stage, archive_set.first_volume.parent))

        merge_staged_tree(stages, scan_root)

        client.stop(hashes)
        wait_for_stopped(client, hashes)

        all_volumes = sorted(
            {volume for archive_set in component.archive_sets for volume in archive_set.volumes}
        )
        quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for volume in all_volumes:
            relative = volume.relative_to(scan_root)
            quarantined = quarantine_root / relative
            quarantined.parent.mkdir(parents=True, exist_ok=True)
            os.replace(volume, quarantined)
            quarantine_moves.append((volume, quarantined))
            log_event(f"QUARANTINE {volume} -> {quarantined}")

        # qBittorrent is list-only here. File deletion is exclusively above/below.
        client.remove_list_only(hashes)
        wait_for_removed(client, hashes)
        removed_from_qbit = True

        log_event(
            f"TÖRLÉS: RAR-karantén {quarantine_root} ({len(quarantine_moves)} kötet)",
            action=True,
        )
        shutil.rmtree(quarantine_root)
        try:
            quarantine_root.parent.rmdir()
        except OSError:
            pass
        shutil.rmtree(transaction, ignore_errors=True)
        names = ", ".join(torrent_by_hash[item].name for item in hashes)
        return True, f"SIKER: {names}"
    except BaseException as exc:  # noqa: BLE001 - transaction rollback boundary
        log_event(
            f"RAR TRANZAKCIÓ {transaction_id} HIBA, visszaállítás: {exc!r}\n"
            + traceback.format_exc(),
            action=True,
        )
        if not removed_from_qbit:
            restore_quarantine(quarantine_moves)
            remove_empty_quarantine(quarantine_root)
            if originally_running:
                try:
                    client.start(originally_running)
                except Exception as restart_exc:  # noqa: BLE001  # pragma: no cover
                    if not isinstance(exc, Exception):
                        raise
                    return False, f"HIBA: {exc}; újraindítási hiba: {restart_exc}"
        else:
            if not isinstance(exc, Exception):
                raise
            return False, (
                f"KRITIKUS: a listaelem eltűnt, de a RAR-karantén törlése nem sikerült: "
                f"{quarantine_root}; ok: {exc}"
            )
        if not isinstance(exc, Exception):
            raise
        return False, f"HIBA: {exc}"
    finally:
        shutil.rmtree(links_root, ignore_errors=True)
        if transaction.exists() and not removed_from_qbit:
            shutil.rmtree(transaction, ignore_errors=True)


def process_iso_component(
    component: IsoComponent,
    torrent_by_hash: dict[str, Torrent],
    client: QbitClient,
    mounter: IsoMounter,
    scan_root: Path,
    work_root: Path,
    min_free_bytes: int,
    output_owner: tuple[int, int],
) -> tuple[bool, str]:
    work_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    transaction_id = f"iso-{uuid.uuid4().hex}"
    transaction = work_root / transaction_id
    mount_root = transaction / "mount"
    copy_root = transaction / "copy"
    quarantine_root = scan_root / QUARANTINE_DIR_NAME / transaction_id
    replacement_root = quarantine_root / "replaced-bdmv"
    iso_quarantine_root = quarantine_root / "iso-files"
    hashes = sorted(component.torrent_hashes)
    originally_running = [
        torrent_hash
        for torrent_hash in hashes
        if torrent_by_hash[torrent_hash].state not in STOPPED_STATES
    ]
    replacement_moves: list[tuple[Path, Path]] = []
    iso_quarantine_moves: list[tuple[Path, Path]] = []
    removed_from_qbit = False
    completed = False
    unmount_failed = False
    log_event(
        f"ISO TRANZAKCIÓ {transaction_id} indul; ISO-k: "
        + "; ".join(str(i.path) for i in component.images)
        + "; torrentek: "
        + ("; ".join(f"{h} {torrent_by_hash[h].name}" for h in hashes) or "nincs"),
        action=True,
    )

    try:
        transaction.mkdir(parents=True, exist_ok=False, mode=0o700)
        stages: list[tuple[Path, Path]] = []
        bdmv_to_replace: set[Path] = set()
        expected_on_storage = 0

        for index, image in enumerate(component.images):
            image_path = resolve_existing_file(image.path, scan_root)
            existing_bdmv, replace_bdmv = bdmv_structure_state(image_path.parent)
            if existing_bdmv is not None:
                log_event(
                    f"ISO másolás kihagyva (teljes BDMV már megvan): {existing_bdmv}",
                    action=True,
                )
                print(
                    f"  Meglévő teljes BDMV, ISO mount nélkül törlendő: {existing_bdmv}"
                )
                continue
            mount_point = mount_root / str(index)
            stage = copy_root / str(index)
            operation_error: BaseException | None = None
            entries: tuple[MountedEntry, ...] = ()

            mounter.mount(image_path, mount_point)
            try:
                entries = inspect_mounted_tree(mount_point)
                if not entries:
                    raise IsoError(f"Az ISO üres: {image_path}")
                image_content_bytes = sum(
                    entry.size for entry in entries if not entry.is_directory
                )
                expected_on_storage += image_content_bytes
                work_free_bytes = shutil.disk_usage(work_root).free
                if work_free_bytes - image_content_bytes < min_free_bytes:
                    raise IsoError(
                        "Nincs elég hely az ISO ideiglenes másolatához: "
                        f"szükséges={image_content_bytes}, "
                        f"szabad={work_free_bytes}, tartalék={min_free_bytes}"
                    )
                if work_root.stat().st_dev != scan_root.stat().st_dev:
                    storage_free_bytes = shutil.disk_usage(scan_root).free
                    if storage_free_bytes - expected_on_storage < min_free_bytes:
                        raise IsoError(
                            "Nincs elég hely a storage-on az ISO tartalmához: "
                            f"szükséges={expected_on_storage}, "
                            f"szabad={storage_free_bytes}, tartalék={min_free_bytes}"
                        )
                copy_mounted_entries(entries, stage)
                stages.append((stage, image_path.parent))
            except BaseException as exc:  # noqa: BLE001 - unmount must still happen
                operation_error = exc

            try:
                mounter.unmount(mount_point)
            except Exception as exc:  # noqa: BLE001 - fatal mount boundary
                unmount_failed = True
                detail = f"; eredeti hiba: {operation_error}" if operation_error else ""
                raise IsoUnmountError(f"{exc}{detail}") from exc
            try:
                mount_point.rmdir()
            except OSError:
                pass
            if operation_error is not None:
                raise operation_error
            if replace_bdmv:
                if find_complete_bdmv_structure(stage) is None:
                    raise IsoError(
                        "A meglévő részleges BDMV nem cserélhető: az ISO "
                        f"nem tartalmaz teljes gyökérszintű BDMV-struktúrát: {image_path}"
                    )
                bdmv_to_replace.update(replace_bdmv)

        for bdmv_path in sorted(bdmv_to_replace):
            if not os.path.lexists(bdmv_path):
                continue
            if bdmv_path.name.casefold() != "bdmv" or not is_within(
                bdmv_path.parent.resolve(strict=True), scan_root
            ):
                raise IsoError(f"Nem biztonságos BDMV-cél: {bdmv_path}")
            relative = bdmv_path.relative_to(scan_root)
            quarantined = replacement_root / relative
            quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            quarantined.parent.mkdir(parents=True, exist_ok=True)
            os.replace(bdmv_path, quarantined)
            replacement_moves.append((bdmv_path, quarantined))
            log_event(
                f"BDMV-CSERE: régi részleges BDMV karanténba: {bdmv_path} -> {quarantined}",
                action=True,
            )

        merge_staged_tree(stages, scan_root, output_owner)
        if replacement_root.exists():
            shutil.rmtree(replacement_root)
        replacement_moves.clear()

        if hashes:
            client.stop(hashes)
            wait_for_stopped(client, hashes)

        for image in component.images:
            relative = image.path.relative_to(scan_root)
            quarantined = iso_quarantine_root / relative
            quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            quarantined.parent.mkdir(parents=True, exist_ok=True)
            os.replace(image.path, quarantined)
            iso_quarantine_moves.append((image.path, quarantined))
            log_event(f"QUARANTINE ISO {image.path} -> {quarantined}")

        if hashes:
            # This is intentionally and permanently list-only.
            client.remove_list_only(hashes)
            wait_for_removed(client, hashes)
            removed_from_qbit = True

        log_event(
            f"TÖRLÉS: ISO-karantén {quarantine_root} "
            f"({len(iso_quarantine_moves)} ISO)",
            action=True,
        )
        shutil.rmtree(quarantine_root)
        try:
            quarantine_root.parent.rmdir()
        except OSError:
            pass
        shutil.rmtree(transaction, ignore_errors=True)
        completed = True

        image_names = ", ".join(str(image.path) for image in component.images)
        if hashes:
            torrent_names = ", ".join(
                torrent_by_hash[torrent_hash].name for torrent_hash in hashes
            )
            return True, f"SIKER: {image_names}; qBittorrent: {torrent_names}"
        return True, (
            f"SIKER: {image_names}; nem volt hozzá qBittorrent-listaelem, "
            "ezért a kliensben nem történt módosítás"
        )
    except BaseException as exc:  # noqa: BLE001 - transaction rollback boundary
        log_event(
            f"ISO TRANZAKCIÓ {transaction_id} HIBA, visszaállítás: {exc!r}\n"
            + traceback.format_exc(),
            action=True,
        )
        if not removed_from_qbit:
            restore_quarantine(iso_quarantine_moves)
            restore_quarantine(replacement_moves)
            remove_empty_quarantine(quarantine_root)
            if originally_running:
                try:
                    client.start(originally_running)
                except Exception as restart_exc:  # noqa: BLE001  # pragma: no cover
                    if isinstance(exc, IsoUnmountError):
                        raise IsoUnmountError(
                            f"{exc}; torrent-újraindítási hiba: {restart_exc}"
                        ) from exc
                    if not isinstance(exc, Exception):
                        raise
                    return False, f"HIBA: {exc}; újraindítási hiba: {restart_exc}"
        else:
            if not isinstance(exc, Exception):
                raise
            return False, (
                "KRITIKUS: a listaelem eltűnt, de az ISO-karantén törlése "
                f"nem sikerült: {quarantine_root}; ok: {exc}"
            )
        if isinstance(exc, IsoUnmountError):
            raise
        if not isinstance(exc, Exception):
            raise
        return False, f"HIBA: {exc}"
    finally:
        if transaction.exists() and not completed and not unmount_failed:
            shutil.rmtree(transaction, ignore_errors=True)


def load_config(path: Path) -> dict[str, object]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanerError(f"A konfiguráció nem olvasható: {path}") from exc
    # Az ISO-mód szándékosan független a RAR-kibontótól. A seven_zip mezőt
    # ezért csak a RAR-mód ellenőrzi, nem a közös konfigurációbetöltés.
    required = ["qb_url", "qb_username", "qb_password", "scan_root"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise CleanerError(f"Hiányzó konfigurációs mezők: {', '.join(missing)}")
    return config


def acquire_lock(lock_path: Path) -> object:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise CleanerError("Egy másik RAR/ISO-feldolgozás már fut") from exc
    return handle


def gib(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def print_plan(
    scan: ScanResult,
    components: Sequence[Component],
    torrent_by_hash: dict[str, Torrent],
    blocked_hashes: Iterable[str],
) -> None:
    print(f"Lemezen talált RAR-készletek: {len(scan.archive_sets)}")
    print(f"Árva/hiányos RAR-kötetek: {len(scan.orphan_volumes)}")
    print(f"Pontosan qBittorrenthez egyeztetett feldolgozási egységek: {len(components)}")
    for index, component in enumerate(components, 1):
        volumes = {
            volume for archive_set in component.archive_sets for volume in archive_set.volumes
        }
        print(f"\n[{index}] {len(component.archive_sets)} archívum, "
              f"{len(volumes)} RAR-kötet, {gib(sum(path.stat().st_size for path in volumes))}")
        for archive_set in component.archive_sets:
            print(f"  RAR: {archive_set.first_volume}")
        for torrent_hash in sorted(component.torrent_hashes):
            print(f"  qBittorrent (csak lista): {torrent_by_hash[torrent_hash].name}")
    blocked = list(blocked_hashes)
    if blocked:
        print("\nKihagyott torrent (hiányos/árva, nem 100%-os vagy védett tracker):")
        for torrent_hash in sorted(blocked):
            torrent = torrent_by_hash.get(torrent_hash)
            print(f"  {torrent.name if torrent else torrent_hash}")


def print_iso_plan(
    disk_images: Sequence[Path],
    components: Sequence[IsoComponent],
    torrent_by_hash: dict[str, Torrent],
    blocked_hashes: Iterable[str],
    blocked_paths: Sequence[Path],
) -> None:
    print(f"Lemezen talált ISO-fájlok: {len(disk_images)}")
    print(f"Biztonságosan feldolgozható ISO-egységek: {len(components)}")
    for index, component in enumerate(components, 1):
        print(
            f"\n[{index}] {len(component.images)} ISO, "
            f"{gib(sum(image.byte_size for image in component.images))}"
        )
        for image in component.images:
            print(f"  ISO: {image.path}")
            if image.existing_bdmv is not None:
                print(
                    "  Meglévő teljes BDMV: "
                    f"{image.existing_bdmv} (mount/másolás nélkül csak ISO-törlés)"
                )
            for bdmv_path in image.replace_bdmv:
                print(
                    "  Részleges/kétes BDMV törlendő és ISO-ból újraépítendő: "
                    f"{bdmv_path}"
                )
        if component.torrent_hashes:
            for torrent_hash in sorted(component.torrent_hashes):
                print(
                    "  qBittorrent (csak lista): "
                    f"{torrent_by_hash[torrent_hash].name}"
                )
        else:
            print("  qBittorrent: nincs listaelem; az ISO ettől még feldolgozható")

    if blocked_paths:
        print("\nKihagyott ISO-k:")
        for path in blocked_paths:
            print(f"  {path}")
    blocked = list(blocked_hashes)
    if blocked:
        print("\nKihagyott torrent (nem 100%-os, hiányos ISO-lista vagy védett tracker):")
        for torrent_hash in sorted(blocked):
            torrent = torrent_by_hash.get(torrent_hash)
            print(f"  {torrent.name if torrent else torrent_hash}")


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RAR release-ek vagy ISO-k biztonságos, különálló feldolgozása"
    )
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/qbit-rar-cleaner/config.json")
    )
    parser.add_argument(
        "--apply", action="store_true", help="Valódi kicsomagolás és eltávolítás"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Megerősítő kérdés kihagyása --apply mellett"
    )
    parser.add_argument("--mode", choices=("rar", "iso"), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def protected_domains_from_config(config: dict[str, object]) -> tuple[str, ...]:
    raw = config.get(
        "protected_tracker_domains", list(DEFAULT_PROTECTED_TRACKER_DOMAINS)
    )
    if not isinstance(raw, list) or not raw or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise CleanerError("A protected_tracker_domains csak nem üres szövegeket tartalmazhat")
    return tuple(item.strip().casefold() for item in raw)


def run_rar_mode(
    args: argparse.Namespace,
    config: dict[str, object],
    client: QbitClient,
    scan_root: Path,
    work_root: Path,
    protected_domains: Sequence[str],
) -> int:
    if not config.get("seven_zip"):
        raise CleanerError(
            "Nincs működő RAR-támogatású 7-Zip. Telepítsd a 7zip + 7zip-rar "
            "vagy a p7zip-full + p7zip-rar csomagpárt, majd futtasd újra a "
            "telepítőt. Az ISO-mód ettől függetlenül használható."
        )

    # Discovery order is deliberate: disk first, qBittorrent second.
    scan = scan_rar_sets(scan_root)
    torrents = client.torrents()
    matched_sets, blocked_hashes, torrent_by_hash = cross_reference(
        scan, torrents, client, scan_root, protected_domains
    )
    components = build_components(matched_sets)
    print_plan(scan, components, torrent_by_hash, blocked_hashes)

    if not components:
        print("\nNincs biztonságosan feldolgozható, torrenthez egyeztetett RAR release.")
        return 0
    if not args.apply:
        print("\nPRÓBAÜZEM: nem történt fájlművelet és qBittorrent-módosítás.")
        print("Valódi RAR-futás: qbit-rar-cleaner --apply")
        return 0
    if not args.yes:
        answer = input(
            "\nÍrd be pontosan, hogy FELDOLGOZOM a RAR-kibontás és törlés indításához: "
        )
        if answer != "FELDOLGOZOM":
            print("Megszakítva; nem történt módosítás.")
            return 2

    min_free_bytes = int(config.get("min_free_bytes", 1024 ** 3))
    extractor = SevenZip(str(config["seven_zip"]))
    failures = 0
    for index, component in enumerate(components, 1):
        print(f"\nRAR-feldolgozás {index}/{len(components)}...")
        success, message = process_component(
            component,
            torrent_by_hash,
            client,
            extractor,
            scan_root,
            work_root,
            min_free_bytes,
        )
        print(message)
        log_event(f"EREDMÉNY {index}/{len(components)}: {message}", action=True)
        failures += 0 if success else 1
    return 1 if failures else 0


def run_iso_mode(
    args: argparse.Namespace,
    config: dict[str, object],
    client: QbitClient,
    scan_root: Path,
    work_root: Path,
    protected_domains: Sequence[str],
) -> int:
    if os.geteuid() != 0:
        raise CleanerError("Az ISO mountolásához ezt rootként futtasd: sudo qbit-iso-cleaner")
    required = ["mount_command", "umount_command", "service_uid", "service_gid"]
    missing = [key for key in required if config.get(key) in (None, "")]
    if missing:
        raise CleanerError(
            "Az ISO-módhoz újra kell futtatni a telepítőt; hiányzik: "
            + ", ".join(missing)
        )

    disk_images = scan_iso_files(scan_root)
    torrents = client.torrents()
    matched_images, blocked_hashes, blocked_paths, torrent_by_hash = (
        cross_reference_isos(
            disk_images, torrents, client, scan_root, protected_domains
        )
    )
    components = build_iso_components(matched_images)
    print_iso_plan(
        disk_images, components, torrent_by_hash, blocked_hashes, blocked_paths
    )

    if not components:
        print("\nNincs biztonságosan feldolgozható ISO-fájl.")
        return 0
    if not args.apply:
        print("\nPRÓBAÜZEM: nem történt mount, fájlművelet vagy qBittorrent-módosítás.")
        print("Valódi ISO-futás: sudo qbit-iso-cleaner --apply")
        return 0
    if not args.yes:
        answer = input(
            "\nÍrd be pontosan, hogy ISO_FELDOLGOZOM a mountolás, másolás és törlés indításához: "
        )
        if answer != "ISO_FELDOLGOZOM":
            print("Megszakítva; nem történt módosítás.")
            return 2

    mounter = IsoMounter(
        str(config["mount_command"]), str(config["umount_command"])
    )
    output_owner = (int(config["service_uid"]), int(config["service_gid"]))
    min_free_bytes = int(config.get("min_free_bytes", 1024 ** 3))
    failures = 0
    for index, component in enumerate(components, 1):
        print(f"\nISO-feldolgozás {index}/{len(components)}...")
        success, message = process_iso_component(
            component,
            torrent_by_hash,
            client,
            mounter,
            scan_root,
            work_root,
            min_free_bytes,
            output_owner,
        )
        print(message)
        log_event(f"EREDMÉNY {index}/{len(components)}: {message}", action=True)
        failures += 0 if success else 1
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    mode = os.environ.get("QBIT_CLEANER_MODE") or args.mode or "rar"
    if mode not in {"rar", "iso"}:
        print(f"HIBA: ismeretlen futási mód: {mode}", file=sys.stderr)
        return 1
    if args.yes and not args.apply:
        print("HIBA: a --yes csak --apply mellett használható", file=sys.stderr)
        return 2

    lock_handle = None
    try:
        config = load_config(args.config)
        scan_root = Path(str(config["scan_root"])).resolve(strict=True)
        if not scan_root.is_dir():
            raise CleanerError(f"A scan root nem könyvtár: {scan_root}")
        if not os.access(scan_root, os.R_OK | os.W_OK | os.X_OK):
            raise CleanerError(f"Nincs teljes hozzáférés a scan roothoz: {scan_root}")

        configured_work_root = config.get("work_root")
        work_root = Path(
            str(configured_work_root or (scan_root / WORK_DIR_NAME))
        ).expanduser()
        work_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        work_root = work_root.resolve(strict=True)
        if not work_root.is_dir() or not os.access(
            work_root, os.R_OK | os.W_OK | os.X_OK
        ):
            raise CleanerError(f"A munkakönyvtár nem használható: {work_root}")

        setup_logging(config, mode, args)
        lock_handle = acquire_lock(work_root / ".operation.lock")
        client = QbitClient(
            str(config["qb_url"]),
            str(config["qb_username"]),
            str(config["qb_password"]),
        )
        client.login()
        protected_domains = protected_domains_from_config(config)

        runner = run_iso_mode if mode == "iso" else run_rar_mode
        exit_code = runner(
            args,
            config,
            client,
            scan_root,
            work_root,
            protected_domains,
        )
        log_event(f"VÉGE kilépési kód={exit_code}", action=True)
        return exit_code
    except (CleanerError, requests.RequestException, OSError, ValueError) as exc:
        print(f"HIBA: {exc}", file=sys.stderr)
        log_event("HIBA részletei:\n" + traceback.format_exc(), action=True)
        return 1
    except BaseException:
        log_event("VÁRATLAN LEÁLLÁS:\n" + traceback.format_exc(), action=True)
        raise
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
