#!/usr/bin/env python3
"""
Local Plex cleanup GUI.

Scans Plex libraries for movies and shows that have never been watched or have
not been watched within a configured age, then deletes explicitly selected
items through Radarr and Sonarr.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PLEX_CLIENT_ID = "plex-cleanup-gui"


DEFAULT_CONFIG: dict[str, Any] = {
    "plex": {
        "url": "http://localhost:32400",
        "token": "",
        "movie_library": "",
        "show_library": "",
    },
    "radarr": {
        "url": "http://localhost:7878",
        "api_key": "",
        "add_import_exclusion": True,
    },
    "sonarr": {
        "url": "http://localhost:8989",
        "api_key": "",
        "add_import_list_exclusion": True,
    },
    "scan": {
        "inactive_days": 365,
        "include_never_watched": True,
        "include_watched_before_cutoff": True,
    },
    "delete": {
        "mode": "arr_plex_disk",
    },
}


class ApiError(RuntimeError):
    pass


@dataclass
class Service:
    url: str
    token: str = ""
    api_key: str = ""

    def endpoint(self, path: str) -> str:
        base = self.url.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return base + path


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            return deep_merge(DEFAULT_CONFIG, json.load(handle))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(config: dict[str, Any]) -> None:
    merged = deep_merge(DEFAULT_CONFIG, config)
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: int = 60,
) -> Any:
    data = None
    final_headers = {"Accept": "application/json"}
    if headers:
        final_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return None
            content_type = response.headers.get("Content-Type", "")
            if "json" not in content_type and not raw.strip().startswith((b"{", b"[")):
                return raw.decode("utf-8", errors="replace")
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from exc


def plex_get(config: dict[str, Any], path: str, params: dict[str, Any] | None = None) -> Any:
    return plex_get_with_headers(config, path, params=params)


def plex_delete(config: dict[str, Any], path: str, params: dict[str, Any] | None = None) -> Any:
    plex = config["plex"]
    service = Service(normalize_url(plex["url"]), token=plex["token"].strip())
    query = {"X-Plex-Token": service.token}
    if params:
        query.update({k: v for k, v in params.items() if v not in (None, "")})
    url = service.endpoint(path) + "?" + urllib.parse.urlencode(query)
    return request_json("DELETE", url, headers={"Accept": "application/json"})


def plex_get_with_headers(
    config: dict[str, Any],
    path: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    plex = config["plex"]
    service = Service(normalize_url(plex["url"]), token=plex["token"].strip())
    query = {"X-Plex-Token": service.token}
    if params:
        query.update({k: v for k, v in params.items() if v not in (None, "")})
    url = service.endpoint(path) + "?" + urllib.parse.urlencode(query)
    final_headers = {"Accept": "application/json"}
    if headers:
        final_headers.update(headers)
    return request_json("GET", url, headers=final_headers)


def arr_get(service: Service, path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = service.endpoint(path)
    if query:
        url += "?" + query
    return request_json("GET", url, headers={"X-Api-Key": service.api_key})


def arr_delete(service: Service, path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = service.endpoint(path)
    if query:
        url += "?" + query
    return request_json("DELETE", url, headers={"X-Api-Key": service.api_key})


def arr_put(service: Service, path: str, body: Any) -> Any:
    return request_json("PUT", service.endpoint(path), headers={"X-Api-Key": service.api_key}, body=body)


def media_container(data: Any) -> dict[str, Any]:
    return data.get("MediaContainer", {}) if isinstance(data, dict) else {}


def metadata_list(data: Any) -> list[dict[str, Any]]:
    metadata = media_container(data).get("Metadata", [])
    return metadata if isinstance(metadata, list) else []


def directory_list(data: Any) -> list[dict[str, Any]]:
    directories = media_container(data).get("Directory", [])
    return directories if isinstance(directories, list) else []


def first_metadata(data: Any) -> dict[str, Any]:
    items = metadata_list(data)
    return items[0] if items else {}


def metadata_detail(config: dict[str, Any], rating_key: str) -> dict[str, Any]:
    return first_metadata(plex_get(config, f"/library/metadata/{rating_key}"))


def extract_guid_ids(item: dict[str, Any]) -> dict[str, str]:
    ids: dict[str, str] = {}
    candidates = []
    if item.get("guid"):
        candidates.append(str(item["guid"]))
    for guid in item.get("Guid", []) or []:
        if isinstance(guid, dict) and guid.get("id"):
            candidates.append(str(guid["id"]))
    for candidate in candidates:
        lowered = candidate.lower()
        for key in ("tmdb", "imdb", "tvdb"):
            match = re.search(rf"{key}[:/]+([^/?]+)", lowered)
            if match:
                ids[key] = match.group(1)
    return ids


def normalize_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path))


def normalize_title(value: Any) -> str:
    title = str(value or "").lower()
    title = re.sub(r"\([^)]*\)", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


def extract_locations(item: dict[str, Any]) -> list[str]:
    locations: list[str] = []
    for location in item.get("Location", []) or []:
        if isinstance(location, dict) and location.get("path"):
            locations.append(str(location["path"]))
    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            file_path = str(part.get("file") or "").strip()
            if file_path:
                locations.append(str(Path(file_path).parent))
    unique_locations: list[str] = []
    seen = set()
    for location in locations:
        normalized = normalize_path(location)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_locations.append(location)
    return unique_locations


def item_has_guid_data(item: dict[str, Any]) -> bool:
    if item.get("guid"):
        return True
    for guid in item.get("Guid", []) or []:
        if isinstance(guid, dict) and guid.get("id"):
            return True
    return False


def series_title_candidates(series: dict[str, Any]) -> list[str]:
    titles = [
        series.get("title"),
        series.get("sortTitle"),
        series.get("cleanTitle"),
        series.get("titleSlug"),
    ]
    for alternate in series.get("alternateTitles", []) or []:
        if isinstance(alternate, dict):
            titles.append(alternate.get("title"))
        else:
            titles.append(alternate)
    normalized = [normalize_title(title) for title in titles]
    return [title for title in normalized if title]


def detail_if_needed(
    config: dict[str, Any],
    item: dict[str, Any],
    *,
    need_guids: bool = False,
    need_locations: bool = False,
    need_file: bool = False,
) -> dict[str, Any]:
    if not item:
        return item
    missing_guids = need_guids and not item_has_guid_data(item)
    missing_locations = need_locations and not extract_locations(item)
    missing_file = need_file and not item_has_file(item)
    if not (missing_guids or missing_locations or missing_file):
        return item
    rating_key = str(item.get("ratingKey") or "")
    if not rating_key:
        return item
    detail = metadata_detail(config, rating_key)
    return detail or item


def parse_size(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def item_size(item: dict[str, Any]) -> int:
    total = 0
    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            total += parse_size(part.get("size"))
    return total


def item_has_file(item: dict[str, Any]) -> bool:
    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            if part.get("file") or parse_size(part.get("size")) > 0:
                return True
    return False


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def latest_timestamp(values: list[int | None]) -> int | None:
    timestamps = [int(value) for value in values if value]
    return max(timestamps) if timestamps else None


def parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def account_last_viewed(item: dict[str, Any]) -> int | None:
    view_count = int(item.get("viewCount") or 0)
    last_viewed = parse_int(item.get("lastViewedAt"))
    if not view_count or not last_viewed:
        return None
    return last_viewed


def watched_state_for_last_viewed(
    last_viewed: int | None,
    cutoff: int,
    never_reason: str,
    old_reason: str,
    recent_reason: str,
    last_viewed_by: str | None = None,
) -> dict[str, Any]:
    if not last_viewed:
        return {"candidate": True, "reason": never_reason, "lastViewedAt": None, "lastViewedBy": None}
    if last_viewed < cutoff:
        return {"candidate": True, "reason": old_reason, "lastViewedAt": last_viewed, "lastViewedBy": last_viewed_by}
    return {"candidate": False, "reason": recent_reason, "lastViewedAt": last_viewed, "lastViewedBy": last_viewed_by}


def history_viewed_at(history_entry: dict[str, Any] | None) -> int | None:
    if not history_entry:
        return None
    return parse_int(history_entry.get("viewedAt"))


def history_viewed_by(history_entry: dict[str, Any] | None) -> str | None:
    if not history_entry:
        return None
    return str(history_entry.get("accountTitle") or "").strip() or None


def watched_state(
    item: dict[str, Any],
    cutoff: int,
    watch_source: str = "account",
    history_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_viewed = account_last_viewed(item)
    if watch_source == "all_users":
        history_last_viewed = history_viewed_at(history_entry)
        last_viewed = latest_timestamp([account_viewed, history_last_viewed])
        last_viewed_by = history_viewed_by(history_entry) if history_last_viewed and history_last_viewed == last_viewed else None
        return watched_state_for_last_viewed(
            last_viewed,
            cutoff,
            "Never watched by any user",
            "Not watched recently by any user",
            "Watched recently by any user",
            last_viewed_by=last_viewed_by,
        )
    return watched_state_for_last_viewed(
        account_viewed,
        cutoff,
        "Never watched",
        "Not watched recently",
        "Watched recently",
    )


def playback_history_items(data: Any) -> list[dict[str, Any]]:
    metadata = metadata_list(data)
    if metadata:
        return metadata
    items: list[dict[str, Any]] = []
    for value in media_container(data).values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and item.get("ratingKey") and item.get("viewedAt"):
                items.append(item)
    return items


def plex_tv_xml(url: str, token: str) -> str:
    payload = request_json(
        "GET",
        url,
        headers={
            "Accept": "application/xml",
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
        },
    )
    return payload if isinstance(payload, str) else ""


def plex_tv_current_user(config: dict[str, Any]) -> dict[str, Any]:
    plex = config["plex"]
    token = plex["token"].strip()
    return request_json(
        "GET",
        "https://plex.tv/api/v2/user",
        headers={
            "Accept": "application/json",
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
        },
    )


def merge_account_names_from_xml(target: dict[int, str], xml_text: str) -> None:
    if not xml_text.strip():
        return
    root = ET.fromstring(xml_text)
    for element in root.iter():
        if not element.tag.endswith("User"):
            continue
        user_id = parse_int(element.attrib.get("id"))
        title = str(element.attrib.get("title") or "").strip()
        if user_id and title and user_id not in target:
            target[user_id] = title


def plex_account_names(config: dict[str, Any]) -> dict[int, str]:
    plex = config["plex"]
    token = plex["token"].strip()
    names: dict[int, str] = {}
    try:
        current_user = plex_tv_current_user(config)
        current_id = parse_int(current_user.get("id"))
        current_title = str(current_user.get("title") or current_user.get("username") or "").strip()
        if current_id and current_title:
            names[current_id] = current_title
        if current_title:
            names[1] = current_title
    except Exception:
        pass
    for url in ("https://plex.tv/api/home/users", "https://plex.tv/api/users"):
        try:
            merge_account_names_from_xml(names, plex_tv_xml(url, token))
        except Exception:
            continue
    return names


def playback_history_latest_info(
    config: dict[str, Any],
    library_section_id: str,
    account_names: dict[int, str] | None = None,
) -> dict[str, dict[str, Any]]:
    latest_by_rating_key: dict[str, dict[str, Any]] = {}
    offset = 0
    page_size = 1000
    params = {"librarySectionID": library_section_id, "sort": "viewedAt:desc"}
    account_names = account_names or {}
    while True:
        data = plex_get_with_headers(
            config,
            "/status/sessions/history/all",
            params=params,
            headers={
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": str(page_size),
            },
        )
        container = media_container(data)
        items = playback_history_items(data)
        for item in items:
            rating_key = str(item.get("ratingKey") or "")
            viewed_at = parse_int(item.get("viewedAt"))
            account_id = parse_int(item.get("accountID"))
            if rating_key and viewed_at:
                current = latest_by_rating_key.get(rating_key)
                if current and parse_int(current.get("viewedAt")) and int(current["viewedAt"]) >= viewed_at:
                    continue
                latest_by_rating_key[rating_key] = {
                    "viewedAt": viewed_at,
                    "accountID": account_id,
                    "accountTitle": account_names.get(account_id or 0) or (f"Account {account_id}" if account_id else None),
                }
        current_offset = parse_int(container.get("offset")) or offset
        total_size = parse_int(container.get("totalSize")) or 0
        if not items:
            break
        next_offset = current_offset + len(items)
        if total_size and next_offset >= total_size:
            break
        if next_offset <= offset:
            break
        offset = next_offset
    return latest_by_rating_key


def plex_can_read_server_history(config: dict[str, Any], library_section_id: str | None = None) -> tuple[bool, str | None]:
    params: dict[str, Any] = {"sort": "viewedAt:desc"}
    if library_section_id:
        params["librarySectionID"] = library_section_id
    try:
        plex_get_with_headers(
            config,
            "/status/sessions/history/all",
            params=params,
            headers={
                "X-Plex-Container-Start": "0",
                "X-Plex-Container-Size": "1",
            },
        )
        return True, None
    except Exception as exc:
        return False, str(exc)


def plex_server_capabilities(config: dict[str, Any]) -> dict[str, Any]:
    return media_container(plex_get(config, "/"))


def paged_metadata(
    config: dict[str, Any],
    path: str,
    params: dict[str, Any] | None = None,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = plex_get_with_headers(
            config,
            path,
            params=params,
            headers={
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": str(page_size),
            },
        )
        container = media_container(data)
        batch = metadata_list(data)
        if not batch:
            break
        items.extend(batch)
        current_offset = parse_int(container.get("offset")) or offset
        total_size = parse_int(container.get("totalSize")) or 0
        next_offset = current_offset + len(batch)
        if total_size and next_offset >= total_size:
            break
        if next_offset <= offset:
            break
        offset = next_offset
    return items


def plex_libraries(config: dict[str, Any]) -> list[dict[str, str]]:
    sections = directory_list(plex_get(config, "/library/sections"))
    libraries = []
    for section in sections:
        section_type = str(section.get("type", ""))
        if section_type not in ("movie", "show"):
            continue
        libraries.append(
            {
                "key": str(section.get("key", "")),
                "title": str(section.get("title", "")),
                "type": section_type,
            }
        )
    return libraries


def find_library_key(config: dict[str, Any], wanted_type: str, configured_name: str) -> str | None:
    if not configured_name:
        return None
    matching_type = [s for s in plex_libraries(config) if s.get("type") == wanted_type]
    for section in matching_type:
        if str(section.get("title", "")).lower() == configured_name.lower():
            return str(section.get("key"))
        if str(section.get("key", "")) == configured_name:
            return str(section.get("key"))
    return None

def build_show_seasons(
    config: dict[str, Any],
    episodes: list[dict[str, Any]],
    cutoff: int,
    watch_source: str = "account",
    history_last_viewed: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    seasons_by_key: dict[str, dict[str, Any]] = {}
    skipped_episodes = 0
    history_last_viewed = history_last_viewed or {}
    for episode in episodes:
        season_key = str(episode.get("parentRatingKey") or "")
        if not season_key:
            continue
        detail = detail_if_needed(config, episode, need_file=True)
        if not item_has_file(detail):
            skipped_episodes += 1
            continue
        season_number = int(detail.get("parentIndex") or episode.get("parentIndex") or 0)
        season = seasons_by_key.setdefault(
            season_key,
            {
                "ratingKey": season_key,
                "title": detail.get("parentTitle") or episode.get("parentTitle") or f"Season {season_number}",
                "seasonNumber": season_number,
                "episodes": [],
            },
        )
        item_rating_key = str(detail.get("ratingKey") or episode.get("ratingKey") or "")
        state = watched_state(
            detail,
            cutoff,
            watch_source=watch_source,
            history_entry=history_last_viewed.get(item_rating_key),
        )
        size = item_size(detail)
        season["episodes"].append(
            {
                "ratingKey": item_rating_key,
                "title": detail.get("title") or episode.get("title") or "Episode",
                "index": detail.get("index") or episode.get("index"),
                "lastViewedAt": state["lastViewedAt"],
                "lastViewedBy": state["lastViewedBy"],
                "reason": state["reason"],
                "candidate": state["candidate"],
                "size": size,
                "sizeText": human_size(size),
            }
        )
    seasons = []
    for season in sorted(seasons_by_key.values(), key=lambda item: (int(item.get("seasonNumber") or 0), str(item.get("title") or ""))):
        episode_items = season["episodes"]
        total_size = sum(int(episode.get("size") or 0) for episode in episode_items)
        watched_episodes = sum(1 for episode in episode_items if episode["lastViewedAt"])
        latest_episode = max(
            (episode for episode in episode_items if episode.get("lastViewedAt")),
            key=lambda episode: int(episode["lastViewedAt"]),
            default=None,
        )
        last_viewed_at = latest_episode.get("lastViewedAt") if latest_episode else None
        last_viewed_by = latest_episode.get("lastViewedBy") if latest_episode else None
        is_candidate = bool(episode_items) and (last_viewed_at is None or last_viewed_at < cutoff)
        reason = "Never watched" if last_viewed_at is None else (
            "Not watched recently" if is_candidate else "Watched recently"
        )
        seasons.append(
            {
                "ratingKey": season["ratingKey"],
                "title": season["title"],
                "seasonNumber": season["seasonNumber"],
                "episodeCount": len(episode_items),
                "watchedEpisodeCount": watched_episodes,
                "candidate": is_candidate,
                "reason": reason,
                "lastViewedAt": last_viewed_at,
                "lastViewedBy": last_viewed_by,
                "size": total_size,
                "sizeText": human_size(total_size),
                "episodes": sorted(
                    episode_items,
                    key=lambda item: (parse_int(item.get("index")) or 0, str(item.get("title") or "")),
                ),
            }
        )
    return seasons, skipped_episodes


def scan_media(config: dict[str, Any]) -> dict[str, Any]:
    inactive_days = int(config["scan"].get("inactive_days") or 365)
    cutoff = int(time.time()) - inactive_days * 86400
    libraries = plex_libraries(config)
    movie_key = None
    show_key = None
    movie_library_name = config["plex"].get("movie_library", "")
    show_library_name = config["plex"].get("show_library", "")
    for section in libraries:
        section_key = str(section.get("key", ""))
        section_title = str(section.get("title", "")).lower()
        section_type = str(section.get("type", ""))
        if section_type == "movie" and movie_library_name:
            if section_key == str(movie_library_name) or section_title == str(movie_library_name).lower():
                movie_key = section_key
        if section_type == "show" and show_library_name:
            if section_key == str(show_library_name) or section_title == str(show_library_name).lower():
                show_key = section_key
    result: dict[str, Any] = {
        "generatedAt": int(time.time()),
        "inactiveDays": inactive_days,
        "watchSource": "account",
        "watchSourceLabel": "This Plex account",
        "movies": [],
        "shows": [],
        "warnings": [],
        "skippedNoFile": {"movies": 0, "episodes": 0},
    }
    if not movie_key:
        result["warnings"].append("No Plex movie library selected.")
    if not show_key:
        result["warnings"].append("No Plex TV library selected.")
    plex_caps = plex_server_capabilities(config)
    allow_media_deletion = bool(plex_caps.get("allowMediaDeletion"))
    result["allowMediaDeletion"] = allow_media_deletion
    history_ok, history_error = plex_can_read_server_history(config, movie_key or show_key)
    watch_source = "all_users" if history_ok else "account"
    result["watchSource"] = watch_source
    result["watchSourceLabel"] = "Any user on server" if watch_source == "all_users" else "This Plex account"
    if watch_source == "all_users":
        result["warnings"].append(
            "Using Plex playback history to detect watches from any user on the server."
        )
    else:
        result["warnings"].append(
            "Plex token cannot read server-wide watch history, so only this account's watch data is being used."
        )
        if history_error:
            result["warnings"].append(f"History access check failed: {history_error}")
    if not allow_media_deletion:
        result["warnings"].append(
            "Plex media deletion is not allowed for this token/server. Plex/disk delete mode will fail until deletion rights are enabled."
        )
    movie_history_last_viewed: dict[str, dict[str, Any]] = {}
    show_history_last_viewed: dict[str, dict[str, Any]] = {}
    if watch_source == "all_users":
        account_names = plex_account_names(config)
        if movie_key:
            movie_history_last_viewed = playback_history_latest_info(config, movie_key, account_names)
        if show_key:
            show_history_last_viewed = playback_history_latest_info(config, show_key, account_names)

    if movie_key:
        movies = paged_metadata(config, f"/library/sections/{movie_key}/all", {"type": 1})
        for movie in movies:
            detail = detail_if_needed(config, movie, need_file=True)
            if not item_has_file(detail):
                result["skippedNoFile"]["movies"] += 1
                continue
            item_rating_key = str(detail.get("ratingKey") or movie.get("ratingKey") or "")
            state = watched_state(
                detail,
                cutoff,
                watch_source=watch_source,
                history_entry=movie_history_last_viewed.get(item_rating_key),
            )
            detail = detail_if_needed(config, detail, need_guids=True, need_locations=True)
            size = item_size(detail)
            ids = extract_guid_ids(detail)
            result["movies"].append(
                {
                    "kind": "movie",
                    "ratingKey": item_rating_key,
                    "title": detail.get("title") or movie.get("title") or "Movie",
                    "year": detail.get("year"),
                    "lastViewedAt": state["lastViewedAt"],
                    "lastViewedBy": state["lastViewedBy"],
                    "reason": state["reason"],
                    "size": size,
                    "sizeText": human_size(size),
                    "ids": ids,
                    "locations": extract_locations(detail),
                }
            )

    if show_key:
        shows = paged_metadata(config, f"/library/sections/{show_key}/all", {"type": 2})
        episodes = paged_metadata(config, f"/library/sections/{show_key}/allLeaves")
        shows_by_rating_key = {str(show.get("ratingKey") or ""): show for show in shows}
        episodes_by_show: dict[str, list[dict[str, Any]]] = {}
        for episode in episodes:
            show_rating_key = str(episode.get("grandparentRatingKey") or "")
            if not show_rating_key:
                continue
            episodes_by_show.setdefault(show_rating_key, []).append(episode)
        for show_rating_key, show_episodes in episodes_by_show.items():
            detail = shows_by_rating_key.get(show_rating_key, {"ratingKey": show_rating_key})
            all_seasons, skipped_episodes = build_show_seasons(
                config,
                show_episodes,
                cutoff,
                watch_source=watch_source,
                history_last_viewed=show_history_last_viewed,
            )
            result["skippedNoFile"]["episodes"] += skipped_episodes
            if not all_seasons:
                continue
            total_size = sum(season["size"] for season in all_seasons)
            last_viewed_at = latest_timestamp([season["lastViewedAt"] for season in all_seasons])
            show_state = watched_state_for_last_viewed(
                last_viewed_at,
                cutoff,
                "Never watched" if watch_source == "account" else "Never watched by any user",
                "Not watched recently" if watch_source == "account" else "Not watched recently by any user",
                "Watched recently" if watch_source == "account" else "Watched recently by any user",
                last_viewed_by=next(
                    (
                        season.get("lastViewedBy")
                        for season in sorted(all_seasons, key=lambda entry: int(entry.get("lastViewedAt") or 0), reverse=True)
                        if season.get("lastViewedAt")
                    ),
                    None,
                ),
            )
            detail = detail_if_needed(config, detail, need_guids=True, need_locations=True)
            ids = extract_guid_ids(detail)
            result["shows"].append(
                {
                    "kind": "show",
                    "ratingKey": str(detail.get("ratingKey") or show_rating_key),
                    "title": detail.get("title") or show_episodes[0].get("grandparentTitle") or "Show",
                    "year": detail.get("year"),
                    "size": total_size,
                    "sizeText": human_size(total_size),
                    "totalSize": total_size,
                    "totalSizeText": human_size(total_size),
                    "canDeleteWholeShow": bool(all_seasons),
                    "lastViewedAt": last_viewed_at,
                    "lastViewedBy": show_state["lastViewedBy"],
                    "reason": show_state["reason"],
                    "candidate": show_state["candidate"],
                    "ids": ids,
                    "locations": extract_locations(detail),
                    "seasons": all_seasons,
                }
            )
    return result


def radarr_service(config: dict[str, Any]) -> Service:
    return Service(normalize_url(config["radarr"]["url"]), api_key=config["radarr"]["api_key"].strip())


def sonarr_service(config: dict[str, Any]) -> Service:
    return Service(normalize_url(config["sonarr"]["url"]), api_key=config["sonarr"]["api_key"].strip())


def delete_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("delete", {}).get("mode") or "arr_plex_disk")
    return mode if mode in {"arr_only", "arr_plex_disk"} else "arr_plex_disk"


def delete_mode_deletes_plex(config: dict[str, Any]) -> bool:
    return delete_mode(config) == "arr_plex_disk"


def delete_mode_deletes_arr_files(config: dict[str, Any]) -> bool:
    return delete_mode(config) == "arr_plex_disk"


def plex_delete_metadata_item(config: dict[str, Any], rating_key: Any) -> None:
    key = str(rating_key or "").strip()
    if not key:
        raise ApiError("Missing Plex ratingKey for deletion")
    plex_delete(config, f"/library/metadata/{key}")


def selected_season_items(item: dict[str, Any], season_numbers: list[int]) -> list[dict[str, Any]]:
    wanted = set(season_numbers)
    selected = []
    for season in item.get("seasons", []) or []:
        if int(season.get("seasonNumber") or 0) in wanted:
            selected.append(season)
    return selected


def plex_delete_selected_seasons(config: dict[str, Any], item: dict[str, Any], season_numbers: list[int]) -> dict[str, Any]:
    deleted_seasons = 0
    deleted_episodes = 0
    errors: list[str] = []
    for season in selected_season_items(item, season_numbers):
        season_key = str(season.get("ratingKey") or "").strip()
        if season_key:
            try:
                plex_delete_metadata_item(config, season_key)
                deleted_seasons += 1
                continue
            except Exception as exc:
                errors.append(f"Season {season.get('seasonNumber')}: {exc}")
        for episode in season.get("episodes", []) or []:
            episode_key = str(episode.get("ratingKey") or "").strip()
            if not episode_key:
                continue
            try:
                plex_delete_metadata_item(config, episode_key)
                deleted_episodes += 1
            except Exception as exc:
                errors.append(f"Episode {episode.get('title')}: {exc}")
    return {
        "deletedSeasons": deleted_seasons,
        "deletedEpisodes": deleted_episodes,
        "errors": errors,
    }


def match_radarr_movie(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    service = radarr_service(config)
    movies = arr_get(service, "/api/v3/movie")
    ids = item.get("ids", {})
    title = str(item.get("title", "")).lower()
    year = item.get("year")
    locations = [normalize_path(location) for location in item.get("locations", [])]
    for movie in movies:
        if ids.get("tmdb") and str(movie.get("tmdbId")) == str(ids["tmdb"]):
            return movie
        if ids.get("imdb") and str(movie.get("imdbId", "")).lower() == str(ids["imdb"]).lower():
            return movie
    if locations:
        for movie in movies:
            movie_path = normalize_path(movie.get("path"))
            if movie_path and movie_path in locations:
                return movie
    for movie in movies:
        if str(movie.get("title", "")).lower() == title and (not year or movie.get("year") == year):
            return movie
    return None


def match_sonarr_series(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    service = sonarr_service(config)
    series_list = arr_get(service, "/api/v3/series")
    ids = item.get("ids", {})
    title = str(item.get("title", "")).lower()
    normalized_title = normalize_title(item.get("title"))
    year = item.get("year")
    locations = [normalize_path(location) for location in item.get("locations", [])]
    for series in series_list:
        if ids.get("tvdb") and str(series.get("tvdbId")) == str(ids["tvdb"]):
            return series
        if ids.get("imdb") and str(series.get("imdbId", "")).lower() == str(ids["imdb"]).lower():
            return series
    if locations:
        for series in series_list:
            series_path = normalize_path(series.get("path"))
            if series_path and any(location == series_path or location.startswith(series_path + os.sep) for location in locations):
                return series
    for series in series_list:
        if str(series.get("title", "")).lower() == title and (not year or series.get("year") == year):
            return series
    if normalized_title:
        for series in series_list:
            candidates = series_title_candidates(series)
            if normalized_title in candidates and (not year or series.get("year") == year):
                return series
        for series in series_list:
            candidates = series_title_candidates(series)
            if normalized_title in candidates:
                return series
    return None


def delete_movie(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    mode = delete_mode(config)
    delete_arr_files = delete_mode_deletes_arr_files(config)
    delete_from_plex = delete_mode_deletes_plex(config)
    movie = match_radarr_movie(config, item)
    deleted_from = []
    errors = []
    if movie:
        try:
            params = {
                "deleteFiles": "true" if delete_arr_files else "false",
                "addImportExclusion": "true",
            }
            arr_delete(radarr_service(config), f"/api/v3/movie/{movie['id']}", params)
            deleted_from.append("radarr")
        except Exception as exc:
            errors.append(f"Radarr delete failed: {exc}")
    elif delete_from_plex:
        errors.append("No Radarr match found; deleting through Plex only")
    else:
        errors.append("No Radarr match found")
    if delete_from_plex:
        try:
            plex_delete_metadata_item(config, item.get("ratingKey"))
            deleted_from.append("plex")
        except Exception as exc:
            errors.append(f"Plex delete failed: {exc}")
    arr_satisfied = ("radarr" in deleted_from) or (movie is None and delete_from_plex)
    plex_satisfied = (not delete_from_plex) or ("plex" in deleted_from)
    ok = arr_satisfied and plex_satisfied
    return {
        "ok": ok,
        "kind": "movie",
        "ratingKey": item.get("ratingKey"),
        "title": item.get("title"),
        "deleteMode": mode,
        "deletedFromArr": "radarr" in deleted_from,
        "deletedFromPlex": "plex" in deleted_from,
        "service": ",".join(deleted_from) if deleted_from else "",
        "matchedTitle": movie.get("title") if movie else None,
        "errors": errors,
        "error": None if ok else "; ".join(errors) or "Movie deletion failed",
    }


def delete_show(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    mode = delete_mode(config)
    delete_arr_files = delete_mode_deletes_arr_files(config)
    delete_from_plex = delete_mode_deletes_plex(config)
    series = match_sonarr_series(config, item)
    deleted_from = []
    errors = []
    if series:
        try:
            arr_delete(
                sonarr_service(config),
                f"/api/v3/series/{series['id']}",
                {
                    "deleteFiles": "true" if delete_arr_files else "false",
                    "addImportListExclusion": "true",
                },
            )
            deleted_from.append("sonarr")
        except Exception as exc:
            errors.append(f"Sonarr delete failed: {exc}")
    elif delete_from_plex:
        errors.append("No Sonarr match found; deleting through Plex only")
    else:
        errors.append("No Sonarr match found")
    if delete_from_plex:
        try:
            plex_delete_metadata_item(config, item.get("ratingKey"))
            deleted_from.append("plex")
        except Exception as exc:
            errors.append(f"Plex delete failed: {exc}")
    arr_satisfied = ("sonarr" in deleted_from) or (series is None and delete_from_plex)
    plex_satisfied = (not delete_from_plex) or ("plex" in deleted_from)
    ok = arr_satisfied and plex_satisfied
    return {
        "ok": ok,
        "kind": "show",
        "ratingKey": item.get("ratingKey"),
        "deleteWholeShow": True,
        "title": item.get("title"),
        "deleteMode": mode,
        "deletedFromArr": "sonarr" in deleted_from,
        "deletedFromPlex": "plex" in deleted_from,
        "service": ",".join(deleted_from) if deleted_from else "",
        "matchedTitle": series.get("title") if series else None,
        "errors": errors,
        "error": None if ok else "; ".join(errors) or "Show deletion failed",
    }


def unmonitor_sonarr_seasons(service: Service, series: dict[str, Any], season_numbers: list[int]) -> int:
    wanted = set(season_numbers)
    changed = 0
    for season in series.get("seasons", []) or []:
        if season.get("seasonNumber") in wanted and season.get("monitored"):
            season["monitored"] = False
            changed += 1
    if changed:
        arr_put(service, f"/api/v3/series/{series['id']}", series)
    return changed


def delete_seasons(config: dict[str, Any], item: dict[str, Any], season_numbers: list[int]) -> dict[str, Any]:
    mode = delete_mode(config)
    delete_arr_files = delete_mode_deletes_arr_files(config)
    delete_from_plex = delete_mode_deletes_plex(config)
    series = match_sonarr_series(config, item)
    errors = []
    deleted_from = []
    deleted = 0
    unmonitored = 0
    if series:
        try:
            service = sonarr_service(config)
            unmonitored = unmonitor_sonarr_seasons(service, series, season_numbers)
            if delete_arr_files:
                episodes = arr_get(service, "/api/v3/episode", {"seriesId": series["id"]})
                episode_file_ids = sorted(
                    {
                        episode.get("episodeFileId")
                        for episode in episodes
                        if episode.get("seasonNumber") in season_numbers and episode.get("episodeFileId")
                    }
                )
                for episode_file_id in episode_file_ids:
                    try:
                        arr_delete(service, f"/api/v3/episodeFile/{episode_file_id}")
                        deleted += 1
                    except ApiError as exc:
                        errors.append(str(exc))
            if not errors:
                deleted_from.append("sonarr")
        except ApiError as exc:
            errors.append(f"Sonarr delete failed: {exc}")
    elif delete_from_plex:
        errors.append("No Sonarr match found; deleting through Plex only")
    else:
        errors.append("No Sonarr match found")
    plex_result = {"deletedSeasons": 0, "deletedEpisodes": 0, "errors": []}
    if delete_from_plex:
        plex_result = plex_delete_selected_seasons(config, item, season_numbers)
        if not plex_result["errors"]:
            deleted_from.append("plex")
        else:
            errors.extend(plex_result["errors"])
            if plex_result["deletedSeasons"] or plex_result["deletedEpisodes"]:
                deleted_from.append("plex")
    sonarr_satisfied = ("sonarr" in deleted_from) or (series is None and delete_from_plex)
    plex_satisfied = (not delete_from_plex) or ("plex" in deleted_from)
    ok = sonarr_satisfied and plex_satisfied
    return {
        "ok": ok,
        "kind": "show",
        "ratingKey": item.get("ratingKey"),
        "title": item.get("title"),
        "deleteMode": mode,
        "deletedFromArr": "sonarr" in deleted_from,
        "deletedFromPlex": "plex" in deleted_from,
        "service": ",".join(deleted_from) if deleted_from else "",
        "matchedTitle": series.get("title") if series else None,
        "seasonNumbers": season_numbers,
        "unmonitoredSeasons": unmonitored,
        "deletedEpisodeFiles": deleted,
        "deletedPlexSeasons": plex_result["deletedSeasons"],
        "deletedPlexEpisodes": plex_result["deletedEpisodes"],
        "errors": errors,
        "error": None if ok else "; ".join(errors) or "Season deletion failed",
    }


def perform_delete(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    results = []
    for movie in payload.get("movies", []):
        results.append(delete_movie(config, movie))
    for show in payload.get("shows", []):
        if show.get("deleteWholeShow"):
            results.append(delete_show(config, show))
            continue
        season_numbers = [int(n) for n in show.get("seasonNumbers", [])]
        if season_numbers:
            results.append(delete_seasons(config, show, season_numbers))
    return {"results": results, "ok": all(item.get("ok") for item in results)}


def test_connections(config: dict[str, Any]) -> dict[str, Any]:
    checks = {}
    try:
        account = plex_server_capabilities(config)
        history_ok, history_error = plex_can_read_server_history(config)
        checks["plex"] = {
            "ok": True,
            "name": account.get("friendlyName") or "Plex",
            "allowMediaDeletion": account.get("allowMediaDeletion"),
            "canReadServerHistory": history_ok,
            "serverHistoryError": history_error,
        }
    except Exception as exc:
        checks["plex"] = {"ok": False, "error": str(exc)}
    try:
        system = arr_get(radarr_service(config), "/api/v3/system/status")
        checks["radarr"] = {"ok": True, "version": system.get("version")}
    except Exception as exc:
        checks["radarr"] = {"ok": False, "error": str(exc)}
    try:
        system = arr_get(sonarr_service(config), "/api/v3/system/status")
        checks["sonarr"] = {"ok": True, "version": system.get("version")}
    except Exception as exc:
        checks["sonarr"] = {"ok": False, "error": str(exc)}
    return checks


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Plex Cleanup</title>
  <style>
    :root {
      --bg: #f5f7f8;
      --panel: #ffffff;
      --text: #1c2529;
      --muted: #64747c;
      --line: #d8e0e4;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --danger: #b42318;
      --danger-bg: #fff1f0;
      --ok-bg: #ebf7ee;
      --shadow: 0 1px 2px rgba(28, 37, 41, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .bar {
      max-width: 1180px;
      margin: 0 auto;
      padding: 14px 18px;
      display: flex;
      align-items: center;
      gap: 14px;
      justify-content: space-between;
    }
    h1 { font-size: 20px; margin: 0; letter-spacing: 0; }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px;
      display: grid;
      gap: 16px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .section-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .section-title-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    h2 { font-size: 16px; margin: 0; }
    .content { padding: 16px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .connections-layout {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .connection-card {
      background: linear-gradient(180deg, #ffffff 0%, #f7fafb 100%);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
      display: grid;
      gap: 12px;
    }
    .connection-card.wide {
      grid-column: 1 / -1;
    }
    .card-head {
      display: grid;
      gap: 4px;
    }
    .card-title {
      font-size: 14px;
      font-weight: 800;
      color: var(--text);
    }
    .card-subtitle {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .field-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .field-span-2 {
      grid-column: span 2;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 650; }
    .inline-field {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .inline-field select {
      min-width: 150px;
      width: auto;
    }
    .help-link {
      color: var(--accent-2);
      font-size: 12px;
      font-weight: 600;
      text-decoration: none;
    }
    .help-link:hover { text-decoration: underline; }
    .help-text {
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      line-height: 1.4;
    }
    .setting-note {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    input, select {
      min-width: 0;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      color: var(--text);
      background: #fff;
      font-size: 14px;
    }
    input[type="checkbox"] { width: 18px; height: 18px; accent-color: var(--accent); }
    .check-label { display: flex; align-items: center; gap: 8px; color: var(--text); font-size: 14px; font-weight: 500; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    button {
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.blue { background: var(--accent-2); border-color: var(--accent-2); color: #fff; }
    button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .status { color: var(--muted); font-size: 13px; }
    .status strong { color: var(--text); }
    .status-stack {
      display: grid;
      justify-items: end;
      gap: 4px;
      text-align: right;
    }
    .status-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 750;
      background: #eef3f5;
      color: var(--muted);
    }
    .pill.danger { background: var(--danger-bg); color: var(--danger); }
    .pill.ok { background: var(--ok-bg); color: #087443; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 72px;
    }
    .metric b { display: block; font-size: 22px; }
    .metric span { color: var(--muted); font-size: 12px; }
    .list { display: grid; gap: 10px; }
    .item {
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: #fff;
    }
    .row {
      display: grid;
      grid-template-columns: 28px 1fr auto auto;
      gap: 10px;
      align-items: center;
      padding: 12px;
    }
    .title { font-weight: 800; min-width: 0; overflow-wrap: anywhere; }
    .sub { color: var(--muted); font-size: 13px; }
    details.seasons { border-top: 1px solid var(--line); }
    details.seasons summary {
      cursor: pointer;
      padding: 10px 12px;
      color: var(--muted);
      font-weight: 700;
    }
    .season {
      display: grid;
      grid-template-columns: 28px 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px 10px 34px;
      border-top: 1px solid var(--line);
      background: #fbfcfd;
    }
    .hidden { display: none; }
    pre {
      overflow: auto;
      max-height: 280px;
      background: #11181c;
      color: #d7f7e8;
      border-radius: 8px;
      padding: 12px;
      font-size: 12px;
    }
    @media (max-width: 820px) {
      .grid, .summary, .connections-layout, .field-grid { grid-template-columns: 1fr; }
      .connection-card.wide, .field-span-2 { grid-column: auto; }
      .row { grid-template-columns: 28px 1fr; }
      .row > .pill, .row > .sub { justify-self: start; grid-column: 2; }
      .bar { align-items: flex-start; flex-direction: column; }
      .status-stack { justify-items: start; text-align: left; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <h1>Plex Cleanup</h1>
      <div class="actions">
        <button id="saveBtn">Save</button>
        <button id="testBtn">Test</button>
        <button id="loadLibrariesBtn" class="blue">Load libraries</button>
        <button id="scanBtn" class="primary">Scan</button>
        <button id="deleteBtn" class="danger" disabled>Delete selected</button>
      </div>
    </div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <div class="section-title-row">
          <h2>Settings</h2>
          <button id="toggleSettingsBtn">Collapse</button>
        </div>
        <div class="actions">
          <div class="status-stack">
            <span class="status-label">Status</span>
            <span id="connectionStatus" class="status">Not tested</span>
          </div>
        </div>
      </div>
      <div id="settingsPanel" class="content connections-layout">
        <div class="connection-card">
          <div class="card-head">
            <div class="card-title">Plex</div>
            <div class="card-subtitle">Server access and the libraries we scan.</div>
          </div>
          <div class="field-grid">
            <label class="field-span-2">Plex URL<input id="plexUrl" placeholder="http://server:32400"></label>
            <label class="field-span-2">Plex admin token<input id="plexToken" type="password"><a class="help-link" href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noreferrer">How to find your Plex token</a><span class="help-text">Click <b>Test</b> to check for admin rights.</span></label>
            <label>Filter days<input id="inactiveDays" type="number" min="1"></label>
            <label>Movie library<select id="movieLibrary"><option value="">Load Plex libraries</option></select></label>
            <label>TV library<select id="showLibrary"><option value="">Load Plex libraries</option></select></label>
          </div>
        </div>
        <div class="connection-card">
          <div class="card-head">
            <div class="card-title">Automation</div>
            <div class="card-subtitle">Radarr and Sonarr are used for matching, cleanup, and preventing re-downloads.</div>
          </div>
          <div class="field-grid">
            <label>Radarr URL<input id="radarrUrl" placeholder="http://server:7878"></label>
            <label>Radarr API key<input id="radarrKey" type="password"></label>
            <label>Sonarr URL<input id="sonarrUrl" placeholder="http://server:8989"></label>
            <label>Sonarr API key<input id="sonarrKey" type="password"></label>
          </div>
        </div>
        <div class="connection-card wide">
          <div class="card-head">
            <div class="card-title">Delete Behavior</div>
            <div class="card-subtitle">Choose whether delete actions only update Radarr or Sonarr, or also remove the media from Plex and disk.</div>
          </div>
          <div class="field-grid">
            <label>Delete target<select id="deleteMode"><option value="arr_plex_disk">Radarr/Sonarr + Plex/disk</option><option value="arr_only">Radarr/Sonarr only</option></select></label>
            <div class="setting-note">Whole-show deletes remove the full series from Sonarr. Season-only deletes unmonitor the selected seasons in Sonarr and only remove Plex files when Plex/disk deletion is enabled.</div>
          </div>
        </div>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>Results</h2>
        <span id="scanStatus" class="status">Run a scan to begin</span>
      </div>
      <div class="content">
        <div id="summary" class="summary hidden"></div>
        <div id="warnings" class="status"></div>
      </div>
    </section>
    <section>
      <div class="section-head">
        <div class="section-title-row">
          <h2>Movies</h2>
          <button id="toggleMoviesBtn">Collapse</button>
        </div>
        <div class="actions">
          <span id="movieCount" class="status">0</span>
          <label class="inline-field">Sort<select id="movieSort"><option value="title_asc">A-Z</option><option value="title_desc">Z-A</option><option value="size_desc">Largest</option><option value="size_asc">Smallest</option></select></label>
          <label class="inline-field">Filter<select id="movieFilter"><option value="all">All</option><option value="never">Never watched</option><option value="older">Not watched in set days</option><option value="recent">Watched in set days</option></select></label>
          <button id="selectAllMoviesBtn">Select all</button>
          <button id="clearMoviesBtn">Clear</button>
        </div>
      </div>
      <div id="movies" class="content list"></div>
    </section>
    <section>
      <div class="section-head">
        <div class="section-title-row">
          <h2>TV Shows</h2>
          <button id="toggleShowsBtn">Collapse</button>
        </div>
        <div class="actions">
          <span id="showCount" class="status">0</span>
          <label class="inline-field">Sort<select id="showSort"><option value="title_asc">A-Z</option><option value="title_desc">Z-A</option><option value="size_desc">Largest</option><option value="size_asc">Smallest</option></select></label>
          <label class="inline-field">Filter<select id="showFilter"><option value="all">All</option><option value="never">Never watched</option><option value="older">Not watched in set days</option><option value="recent">Watched in set days</option></select></label>
          <button id="selectAllShowsBtn">Select all</button>
          <button id="clearShowsBtn">Clear</button>
        </div>
      </div>
      <div id="shows" class="content list"></div>
    </section>
    <section id="logSection" class="hidden">
      <div class="section-head"><h2>Result</h2></div>
      <div class="content"><pre id="log"></pre></div>
    </section>
  </main>
<script>
const state = {
  config: null,
  scan: null,
  selection: {
    movies: new Set(),
    shows: new Set(),
    seasons: new Set(),
  },
  ui: {
    movieSort: "title_asc",
    movieFilter: "all",
    showSort: "title_asc",
    showFilter: "all",
    settingsCollapsed: false,
    moviesCollapsed: false,
    showsCollapsed: false,
  },
};
const $ = (id) => document.getElementById(id);

function selectedValueOrText(select) {
  return select.value || select.dataset.pendingValue || "";
}

function formatDate(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleDateString();
}

function formatWatchedSummary(label, ts, watcher) {
  const suffix = watcher ? ` by ${escapeHtml(watcher)}` : "";
  return `${label} ${formatDate(ts)}${suffix}`;
}

function formatLastWatchedOrNever(ts, watcher, label = "last watched") {
  if (!ts) return "never";
  return formatWatchedSummary(label, ts, watcher);
}

function seasonSelectionKey(showKey, seasonNumber) {
  return `${showKey}:${seasonNumber}`;
}

function currentInactiveDays() {
  const value = Number($("inactiveDays")?.value || state.scan?.inactiveDays || 365);
  return value > 0 ? value : 365;
}

function currentCutoff() {
  return Math.floor(Date.now() / 1000) - currentInactiveDays() * 86400;
}

function watchStateForLastViewed(lastViewedAt) {
  const allUsers = state.scan?.watchSource === "all_users";
  if (!lastViewedAt) {
    return {
      candidate: true,
      reason: allUsers ? "Never watched by any user" : "Never watched",
    };
  }
  if (Number(lastViewedAt) < currentCutoff()) {
    return {
      candidate: true,
      reason: allUsers ? "Not watched recently by any user" : "Not watched recently",
    };
  }
  return {
    candidate: false,
    reason: allUsers ? "Watched recently by any user" : "Watched recently",
  };
}

function matchesWatchFilter(lastViewedAt, filterMode) {
  if (filterMode === "never") return !lastViewedAt;
  if (filterMode === "older") return !lastViewedAt || Number(lastViewedAt) < currentCutoff();
  if (filterMode === "recent") return Boolean(lastViewedAt && Number(lastViewedAt) >= currentCutoff());
  return true;
}

function clearSelectionState() {
  state.selection.movies.clear();
  state.selection.shows.clear();
  state.selection.seasons.clear();
}

function compareText(left, right) {
  return String(left || "").localeCompare(String(right || ""), undefined, { numeric: true, sensitivity: "base" });
}

function sortItems(items, sortMode) {
  return [...items].sort((left, right) => {
    if (sortMode === "size_desc") {
      return Number(right.size || 0) - Number(left.size || 0) || compareText(left.title, right.title);
    }
    if (sortMode === "size_asc") {
      return Number(left.size || 0) - Number(right.size || 0) || compareText(left.title, right.title);
    }
    if (sortMode === "title_desc") {
      return compareText(right.title, left.title);
    }
    return compareText(left.title, right.title);
  });
}

function filteredMovies() {
  const movies = state.scan?.movies || [];
  return sortItems(
    movies.filter(movie => matchesWatchFilter(movie.lastViewedAt, state.ui.movieFilter)),
    state.ui.movieSort,
  );
}

function filteredShows() {
  const shows = state.scan?.shows || [];
  return sortItems(
    shows.filter(show => matchesWatchFilter(show.lastViewedAt, state.ui.showFilter)),
    state.ui.showSort,
  );
}

function applySelectionToDom() {
  document.querySelectorAll(".movie-select").forEach(el => {
    el.checked = state.selection.movies.has(el.value);
  });
  document.querySelectorAll(".show-select").forEach(el => {
    el.checked = state.selection.shows.has(el.value);
  });
  document.querySelectorAll(".season-select").forEach(el => {
    el.checked = state.selection.seasons.has(seasonSelectionKey(el.dataset.show, el.value));
  });
}

function syncShowSeasonDisabledStates() {
  document.querySelectorAll(".show-select").forEach(el => {
    document.querySelectorAll(`.season-select[data-show="${el.value}"]`).forEach(season => {
      season.disabled = el.checked;
      if (el.checked) season.checked = false;
    });
  });
}

function applyListUiState() {
  $("movieSort").value = state.ui.movieSort;
  $("movieFilter").value = state.ui.movieFilter;
  $("showSort").value = state.ui.showSort;
  $("showFilter").value = state.ui.showFilter;
  $("settingsPanel").classList.toggle("hidden", state.ui.settingsCollapsed);
  $("movies").classList.toggle("hidden", state.ui.moviesCollapsed);
  $("shows").classList.toggle("hidden", state.ui.showsCollapsed);
  $("toggleSettingsBtn").textContent = state.ui.settingsCollapsed ? "Expand" : "Collapse";
  $("toggleMoviesBtn").textContent = state.ui.moviesCollapsed ? "Expand" : "Collapse";
  $("toggleShowsBtn").textContent = state.ui.showsCollapsed ? "Expand" : "Collapse";
}

function bindSelectionListeners() {
  document.querySelectorAll(".movie-select").forEach(el => el.addEventListener("change", () => {
    if (el.checked) state.selection.movies.add(el.value);
    else state.selection.movies.delete(el.value);
    updateDeleteButton();
  }));
  document.querySelectorAll(".show-select").forEach(el => el.addEventListener("change", () => {
    if (el.checked) {
      state.selection.shows.add(el.value);
      [...state.selection.seasons]
        .filter(key => key.startsWith(`${el.value}:`))
        .forEach(key => state.selection.seasons.delete(key));
    } else {
      state.selection.shows.delete(el.value);
    }
    applySelectionToDom();
    syncShowSeasonDisabledStates();
    updateDeleteButton();
  }));
  document.querySelectorAll(".season-select").forEach(el => el.addEventListener("change", () => {
    const key = seasonSelectionKey(el.dataset.show, el.value);
    state.selection.shows.delete(el.dataset.show);
    if (el.checked) state.selection.seasons.add(key);
    else state.selection.seasons.delete(key);
    updateDeleteButton();
  }));
}

function renderMediaLists() {
  const movies = filteredMovies();
  const shows = filteredShows();
  $("movies").innerHTML = movies.length ? movies.map(renderMovie).join("") : `<div class="status">No movies match the current filter.</div>`;
  $("shows").innerHTML = shows.length ? shows.map(renderShow).join("") : `<div class="status">No shows match the current filter.</div>`;
  bindSelectionListeners();
  applySelectionToDom();
  syncShowSeasonDisabledStates();
  applyListUiState();
  updateDeleteButton();
}

function selectedPayload() {
  if (!state.scan) return { movies: [], shows: [] };
  const movieKeys = state.selection.movies;
  const showKeys = state.selection.shows;
  const movies = state.scan.movies.filter(movie => movieKeys.has(movie.ratingKey));
  const shows = state.scan.shows.map(show => {
    const whole = showKeys.has(show.ratingKey);
    const seasonNumbers = show.seasons
      .filter(season => state.selection.seasons.has(seasonSelectionKey(show.ratingKey, season.seasonNumber)))
      .map(season => Number(season.seasonNumber));
    return { ...show, deleteWholeShow: whole, seasonNumbers };
  }).filter(show => show.deleteWholeShow || show.seasonNumbers.length);
  return { movies, shows };
}

function updateDeleteButton() {
  const payload = selectedPayload();
  const count = payload.movies.length + payload.shows.length;
  $("deleteBtn").disabled = count === 0;
}

function setMovieSelection(checked) {
  filteredMovies().forEach(movie => {
    if (checked) state.selection.movies.add(movie.ratingKey);
    else state.selection.movies.delete(movie.ratingKey);
  });
  applySelectionToDom();
  updateDeleteButton();
}

function setShowSelection(checked) {
  filteredShows().forEach(show => {
    if (checked) {
      state.selection.shows.add(show.ratingKey);
      show.seasons.forEach(season => state.selection.seasons.delete(seasonSelectionKey(show.ratingKey, season.seasonNumber)));
      return;
    }
    state.selection.shows.delete(show.ratingKey);
    show.seasons.forEach(season => state.selection.seasons.delete(seasonSelectionKey(show.ratingKey, season.seasonNumber)));
  });
  applySelectionToDom();
  syncShowSeasonDisabledStates();
  updateDeleteButton();
}

function readConfig() {
  return {
    plex: {
      url: $("plexUrl").value,
      token: $("plexToken").value,
      movie_library: selectedValueOrText($("movieLibrary")),
      show_library: selectedValueOrText($("showLibrary")),
    },
    radarr: {
      url: $("radarrUrl").value,
      api_key: $("radarrKey").value,
      add_import_exclusion: true,
    },
    sonarr: {
      url: $("sonarrUrl").value,
      api_key: $("sonarrKey").value,
    },
    delete: {
      mode: $("deleteMode").value || "arr_plex_disk",
    },
    scan: {
      inactive_days: Number($("inactiveDays").value || 365),
      include_never_watched: true,
      include_watched_before_cutoff: true,
    },
  };
}

function setLibraryPending(select, value) {
  select.dataset.pendingValue = value || "";
  if (!select.options.length || (select.options.length === 1 && !select.options[0].value)) {
    select.innerHTML = `<option value="">${value ? `Saved: ${escapeHtml(value)}` : "Load Plex libraries"}</option>`;
  }
}

function fillConfig(config) {
  state.config = config;
  $("plexUrl").value = config.plex.url || "";
  $("plexToken").value = config.plex.token || "";
  setLibraryPending($("movieLibrary"), config.plex.movie_library || "");
  setLibraryPending($("showLibrary"), config.plex.show_library || "");
  $("inactiveDays").value = config.scan.inactive_days || 365;
  $("radarrUrl").value = config.radarr.url || "";
  $("radarrKey").value = config.radarr.api_key || "";
  $("sonarrUrl").value = config.sonarr.url || "";
  $("sonarrKey").value = config.sonarr.api_key || "";
  $("deleteMode").value = config.delete.mode || "arr_plex_disk";
}

function renderLibrarySelect(select, libraries, type, savedValue) {
  const matching = libraries.filter(library => library.type === type);
  const saved = savedValue || select.dataset.pendingValue || "";
  const matched = matching.find(library => library.key === saved || library.title.toLowerCase() === saved.toLowerCase());
  const missingSaved = saved && !matched ? `<option value="${escapeHtml(saved)}">Saved: ${escapeHtml(saved)} (not found)</option>` : "";
  select.innerHTML = `<option value="">Do not scan ${type === "movie" ? "movies" : "TV"}</option>` + missingSaved + matching.map(library => {
    const label = `${escapeHtml(library.title)} (${library.key})`;
    return `<option value="${escapeHtml(library.key)}">${label}</option>`;
  }).join("");
  select.value = matched ? matched.key : (saved || "");
  select.dataset.pendingValue = matched ? "" : saved;
}

async function loadLibraries(options = {}) {
  const saveBefore = options.saveBefore !== false;
  const saveAfter = options.saveAfter !== false;
  const quiet = Boolean(options.quiet);
  if (saveBefore) await saveConfig();
  if (!quiet) $("connectionStatus").textContent = "Loading libraries...";
  const libraries = await api("/api/libraries", { method: "POST", body: JSON.stringify(readConfig()) });
  renderLibrarySelect($("movieLibrary"), libraries, "movie", selectedValueOrText($("movieLibrary")));
  renderLibrarySelect($("showLibrary"), libraries, "show", selectedValueOrText($("showLibrary")));
  const movieCount = libraries.filter(library => library.type === "movie").length;
  const showCount = libraries.filter(library => library.type === "show").length;
  $("connectionStatus").textContent = `Loaded ${movieCount} movie and ${showCount} TV libraries`;
  if (saveAfter) await saveConfig();
}

async function loadSavedLibrariesIfPossible() {
  const config = readConfig();
  const hasPlex = config.plex.url && config.plex.token;
  const hasSavedLibrary = config.plex.movie_library || config.plex.show_library;
  if (!hasPlex || !hasSavedLibrary) return;
  try {
    await loadLibraries({ saveBefore: false, saveAfter: false, quiet: true });
  } catch (err) {
    $("connectionStatus").textContent = "Saved libraries not loaded";
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

async function saveConfig() {
  $("connectionStatus").textContent = "Saving...";
  const config = readConfig();
  await api("/api/config", { method: "POST", body: JSON.stringify(config) });
  $("connectionStatus").textContent = "Saved";
}

async function testConnections() {
  await saveConfig();
  $("connectionStatus").textContent = "Testing...";
  const checks = await api("/api/test", { method: "POST", body: JSON.stringify(readConfig()) });
  const pills = ["plex", "radarr", "sonarr"].map(name => {
    const check = checks[name];
    const klass = check.ok ? "ok" : "danger";
    return `<span class="pill ${klass}">${name}: ${check.ok ? "ok" : "failed"}</span>`;
  });
  const adminOk = Boolean(checks.plex?.ok && checks.plex?.canReadServerHistory);
  const allowDeleteOk = Boolean(checks.plex?.ok && checks.plex?.allowMediaDeletion);
  pills.splice(1, 0, `<span class="pill ${adminOk ? "ok" : "danger"}">admin: ${adminOk ? "ok" : "failed"}</span>`);
  pills.splice(2, 0, `<span class="pill ${allowDeleteOk ? "ok" : "danger"}">media delete: ${allowDeleteOk ? "ok" : "failed"}</span>`);
  $("connectionStatus").innerHTML = pills.join(" ");
  showLog(checks);
}

function showLog(data) {
  $("logSection").classList.remove("hidden");
  $("log").textContent = JSON.stringify(data, null, 2);
}

function formatElapsed(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

async function scan() {
  await saveConfig();
  $("scanBtn").disabled = true;
  const startedAt = Date.now();
  const updateScanTimer = () => {
    $("scanStatus").textContent = `Scanning Plex... ${formatElapsed(Date.now() - startedAt)}`;
  };
  updateScanTimer();
  const scanTimer = setInterval(updateScanTimer, 1000);
  try {
    clearSelectionState();
    state.scan = await api("/api/scan", { method: "POST", body: JSON.stringify(readConfig()) });
    renderScan();
    $("scanStatus").textContent = `Scanned ${new Date(state.scan.generatedAt * 1000).toLocaleString()} in ${formatElapsed(Date.now() - startedAt)}`;
  } finally {
    clearInterval(scanTimer);
    $("scanBtn").disabled = false;
  }
}

function renderScan() {
  if (!state.scan) return;
  $("summary").classList.remove("hidden");
  $("summary").innerHTML = `
    <div class="metric"><b>${state.scan.movies.length}</b><span>movies scanned</span></div>
    <div class="metric"><b>${state.scan.shows.length}</b><span>shows scanned</span></div>
  `;
  const movies = filteredMovies();
  const shows = filteredShows();
  $("warnings").textContent = (state.scan.warnings || []).join(" ");
  $("movieCount").textContent = `${movies.length} of ${state.scan.movies.length}`;
  $("showCount").textContent = `${shows.length} of ${state.scan.shows.length}`;
  renderSkippedNoFile();
  renderMediaLists();
}

function renderSkippedNoFile() {
  const skipped = state.scan.skippedNoFile || {};
  const messages = [...(state.scan.warnings || [])];
  if (skipped.movies) messages.push(`Skipped ${skipped.movies} movie metadata item${skipped.movies === 1 ? "" : "s"} with no file.`);
  if (skipped.episodes) messages.push(`Skipped ${skipped.episodes} TV episode metadata item${skipped.episodes === 1 ? "" : "s"} with no file.`);
  $("warnings").textContent = messages.join(" ");
}

function renderMovie(movie) {
  const year = movie.year ? ` (${movie.year})` : "";
  const watchState = watchStateForLastViewed(movie.lastViewedAt);
  return `<div class="item">
    <div class="row">
      <input class="movie-select" type="checkbox" value="${movie.ratingKey}">
      <div><div class="title">${escapeHtml(movie.title)}${year}</div><div class="sub">${watchState.reason}; ${formatWatchedSummary("last watched", movie.lastViewedAt, movie.lastViewedBy)}</div></div>
      <span class="pill ${watchState.candidate ? "danger" : "ok"}">${movie.sizeText}</span>
      <span class="sub">${idsMarkup(movie.ids)}</span>
    </div>
  </div>`;
}

function renderShow(show) {
  const year = show.year ? ` (${show.year})` : "";
  const wholeHelp = "Whole show delete removes it from Sonarr; selecting it clears season selections";
  const watchState = watchStateForLastViewed(show.lastViewedAt);
  return `<div class="item">
    <div class="row">
      <input class="show-select" type="checkbox" value="${show.ratingKey}" title="${escapeHtml(wholeHelp)}">
      <div><div class="title">${escapeHtml(show.title)}${year}</div><div class="sub">${watchState.reason}; ${formatWatchedSummary("latest watched", show.lastViewedAt, show.lastViewedBy)}</div></div>
      <span class="pill ${watchState.candidate ? "danger" : "ok"}">${show.sizeText}</span>
      <span class="sub">${idsMarkup(show.ids)}</span>
    </div>
    <details class="seasons" open>
      <summary>${show.seasons.length} season${show.seasons.length === 1 ? "" : "s"}</summary>
      ${show.seasons.map(season => {
        const seasonState = watchStateForLastViewed(season.lastViewedAt);
        return `<div class="season">
        <input class="season-select" data-show="${show.ratingKey}" type="checkbox" value="${season.seasonNumber}">
        <div><div class="title">${escapeHtml(season.title)}</div><div class="sub">${season.watchedEpisodeCount}/${season.episodeCount} episodes watched; ${formatLastWatchedOrNever(season.lastViewedAt, season.lastViewedBy, "latest watched")}</div></div>
        <span class="pill ${seasonState.candidate ? "danger" : "ok"}">${season.sizeText}</span>
      </div>`;
      }).join("")}
    </details>
  </div>`;
}

function imdbUrl(imdbId) {
  const clean = String(imdbId || "").trim();
  return clean ? `https://www.imdb.com/title/${encodeURIComponent(clean)}/` : "";
}

function tmdbUrl(tmdbId) {
  const clean = String(tmdbId || "").trim();
  return clean ? `https://www.themoviedb.org/movie/${encodeURIComponent(clean)}` : "";
}

function tvdbUrl(tvdbId) {
  const clean = String(tvdbId || "").trim();
  return clean ? `https://thetvdb.com/dereferrer/series/${encodeURIComponent(clean)}` : "";
}

function idsMarkup(ids) {
  const values = ids || {};
  const parts = Object.entries(values)
    .filter(([key]) => !["imdb", "tmdb", "tvdb"].includes(key))
    .map(([key, value]) => `${escapeHtml(key)}:${escapeHtml(value)}`);
  if (values.tmdb) {
    parts.push(`<a class="help-link" href="${tmdbUrl(values.tmdb)}" target="_blank" rel="noreferrer">TMDb</a>`);
  }
  if (values.tvdb) {
    parts.push(`<a class="help-link" href="${tvdbUrl(values.tvdb)}" target="_blank" rel="noreferrer">TVDb</a>`);
  }
  if (values.imdb) {
    parts.push(`<a class="help-link" href="${imdbUrl(values.imdb)}" target="_blank" rel="noreferrer">IMDb</a>`);
  }
  return parts.join(" ");
}

function mediaTitleWithYear(item) {
  const year = item?.year ? ` (${item.year})` : "";
  return `${item?.title || "Item"}${year}`;
}

function deletePreview(payload) {
  const lines = [];
  let totalSize = 0;
  for (const movie of payload.movies || []) {
    const size = Number(movie.size || 0);
    totalSize += size;
    lines.push(`- Movie: ${mediaTitleWithYear(movie)} (${humanBytes(size)})`);
  }
  for (const show of payload.shows || []) {
    if (show.deleteWholeShow) {
      const size = Number(show.totalSize || show.size || 0);
      totalSize += size;
      lines.push(`- TV: ${mediaTitleWithYear(show)} - whole show (${humanBytes(size)})`);
      continue;
    }
    const selectedSeasons = (show.seasons || []).filter(season => (show.seasonNumbers || []).includes(Number(season.seasonNumber)));
    const size = selectedSeasons.reduce((sum, season) => sum + Number(season.size || 0), 0);
    totalSize += size;
    const seasonNames = selectedSeasons.map(season => season.title || `Season ${season.seasonNumber}`).join(", ");
    lines.push(`- TV: ${mediaTitleWithYear(show)} - ${seasonNames} (${humanBytes(size)})`);
  }
  return { lines, totalSize };
}

function humanBytes(size) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(size || 0);
  for (const unit of units) {
    if (amount < 1024 || unit === "TB") return unit === "B" ? `${amount} ${unit}` : `${amount.toFixed(1)} ${unit}`;
    amount /= 1024;
  }
}

function latestTimestamp(values) {
  const timestamps = values.filter(Boolean).map(Number);
  return timestamps.length ? Math.max(...timestamps) : null;
}

function refreshShowCandidateFields(show) {
  show.size = show.seasons.reduce((sum, season) => sum + Number(season.size || 0), 0);
  show.sizeText = humanBytes(show.size);
  show.totalSize = show.size;
  show.totalSizeText = show.sizeText;
  const latestSeason = [...show.seasons]
    .filter(season => season.lastViewedAt)
    .sort((left, right) => Number(right.lastViewedAt) - Number(left.lastViewedAt))[0];
  show.lastViewedAt = latestSeason ? latestSeason.lastViewedAt : null;
  show.lastViewedBy = latestSeason ? latestSeason.lastViewedBy || null : null;
  show.canDeleteWholeShow = show.seasons.length > 0;
  return show;
}

function removeDeletedFromScan(result) {
  if (!state.scan || !result || !Array.isArray(result.results)) return 0;
  let removed = 0;
  for (const item of result.results) {
    if (!item.ok) continue;
    if (item.kind === "movie") {
      state.selection.movies.delete(item.ratingKey);
      if (!item.deletedFromPlex) continue;
      const before = state.scan.movies.length;
      state.scan.movies = state.scan.movies.filter(movie => movie.ratingKey !== item.ratingKey);
      removed += before - state.scan.movies.length;
      continue;
    }
    if (item.kind === "show" && item.deleteWholeShow) {
      state.selection.shows.delete(item.ratingKey);
      [...state.selection.seasons]
        .filter(key => key.startsWith(`${item.ratingKey}:`))
        .forEach(key => state.selection.seasons.delete(key));
      if (!item.deletedFromPlex) continue;
      const before = state.scan.shows.length;
      state.scan.shows = state.scan.shows.filter(show => show.ratingKey !== item.ratingKey);
      removed += before - state.scan.shows.length;
      continue;
    }
    if (item.kind === "show" && Array.isArray(item.seasonNumbers)) {
      const seasonNumbers = new Set(item.seasonNumbers.map(Number));
      seasonNumbers.forEach(seasonNumber => state.selection.seasons.delete(seasonSelectionKey(item.ratingKey, seasonNumber)));
      if (!item.deletedFromPlex) continue;
      state.scan.shows = state.scan.shows.map(show => {
        if (show.ratingKey !== item.ratingKey) return show;
        const before = show.seasons.length;
        show.seasons = show.seasons.filter(season => !seasonNumbers.has(Number(season.seasonNumber)));
        removed += before - show.seasons.length;
        return refreshShowCandidateFields(show);
      }).filter(show => show.seasons.length > 0);
    }
  }
  renderScan();
  return removed;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]));
}

async function deleteSelected() {
  const payload = selectedPayload();
  const movieCount = payload.movies.length;
  const showCount = payload.shows.length;
  const deleteMode = $("deleteMode").value || "arr_plex_disk";
  const preview = deletePreview(payload);
  const modeText = deleteMode === "arr_only"
    ? "Only Radarr/Sonarr will be updated. Plex and disk files will stay in place."
    : "Radarr/Sonarr will be updated and Plex/disk media will also be deleted.";
  const previewText = preview.lines.length ? preview.lines.join("\n") : "- Nothing selected";
  if (!confirm(
    `Delete ${movieCount} movie selection(s) and ${showCount} TV selection(s)?\n\n` +
    `${modeText}\n\n` +
    `Total selected size: ${humanBytes(preview.totalSize)}\n\n` +
    `${previewText}`
  )) return;
  $("deleteBtn").disabled = true;
  $("scanStatus").textContent = "Deleting selected media...";
  try {
    const result = await api("/api/delete", { method: "POST", body: JSON.stringify({ config: readConfig(), selection: payload }) });
    const removed = removeDeletedFromScan(result);
    showLog(result);
    const suffix = removed ? `; removed ${removed} item${removed === 1 ? "" : "s"} from the list` : "";
    $("scanStatus").textContent = (result.ok ? "Delete completed" : "Delete completed with errors") + suffix;
  } finally {
    $("deleteBtn").disabled = false;
  }
}

async function init() {
  try {
    const config = await api("/api/config");
    fillConfig(config);
    loadSavedLibrariesIfPossible();
    $("saveBtn").addEventListener("click", () => saveConfig().catch(err => showLog({ error: err.message })));
    $("testBtn").addEventListener("click", () => testConnections().catch(err => showLog({ error: err.message })));
    $("loadLibrariesBtn").addEventListener("click", () => loadLibraries().catch(err => {
      $("connectionStatus").textContent = "Loading libraries failed";
      showLog({ error: err.message });
    }));
    $("scanBtn").addEventListener("click", () => scan().catch(err => {
      $("scanStatus").textContent = "Scan failed";
      showLog({ error: err.message });
    }));
    $("deleteBtn").addEventListener("click", () => deleteSelected().catch(err => showLog({ error: err.message })));
    $("selectAllMoviesBtn").addEventListener("click", () => setMovieSelection(true));
    $("clearMoviesBtn").addEventListener("click", () => setMovieSelection(false));
    $("selectAllShowsBtn").addEventListener("click", () => setShowSelection(true));
    $("clearShowsBtn").addEventListener("click", () => setShowSelection(false));
    $("movieSort").addEventListener("change", () => {
      state.ui.movieSort = $("movieSort").value;
      renderMediaLists();
    });
    $("showSort").addEventListener("change", () => {
      state.ui.showSort = $("showSort").value;
      renderMediaLists();
    });
    $("movieFilter").addEventListener("change", () => {
      state.ui.movieFilter = $("movieFilter").value;
      renderScan();
    });
    $("showFilter").addEventListener("change", () => {
      state.ui.showFilter = $("showFilter").value;
      renderScan();
    });
    $("inactiveDays").addEventListener("change", () => {
      if (state.scan) renderScan();
    });
    $("toggleSettingsBtn").addEventListener("click", () => {
      state.ui.settingsCollapsed = !state.ui.settingsCollapsed;
      applyListUiState();
    });
    $("toggleMoviesBtn").addEventListener("click", () => {
      state.ui.moviesCollapsed = !state.ui.moviesCollapsed;
      applyListUiState();
    });
    $("toggleShowsBtn").addEventListener("click", () => {
      state.ui.showsCollapsed = !state.ui.showsCollapsed;
      applyListUiState();
    });
    applyListUiState();
  } catch (err) {
    showLog({ error: err.message });
  }
}
init();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PlexCleanup/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self) -> None:
        data = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html_headers(self) -> None:
        data = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()

    def handle_error(self, exc: Exception) -> None:
        traceback.print_exc()
        self.send_json({"error": str(exc)}, 500)

    def do_GET(self) -> None:
        try:
            if self.path == "/" or self.path.startswith("/index.html"):
                self.send_html()
            elif self.path == "/api/config":
                self.send_json(load_config())
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.handle_error(exc)

    def do_HEAD(self) -> None:
        try:
            if self.path == "/" or self.path.startswith("/index.html"):
                self.send_html_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/config":
                config = deep_merge(DEFAULT_CONFIG, self.read_json())
                save_config(config)
                self.send_json({"ok": True, "config": config})
            elif self.path == "/api/test":
                config = deep_merge(DEFAULT_CONFIG, self.read_json())
                self.send_json(test_connections(config))
            elif self.path == "/api/libraries":
                config = deep_merge(DEFAULT_CONFIG, self.read_json())
                self.send_json(plex_libraries(config))
            elif self.path == "/api/scan":
                config = deep_merge(DEFAULT_CONFIG, self.read_json())
                save_config(config)
                self.send_json(scan_media(config))
            elif self.path == "/api/delete":
                body = self.read_json()
                config = deep_merge(DEFAULT_CONFIG, body.get("config", load_config()))
                self.send_json(perform_delete(config, body.get("selection", {})))
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.handle_error(exc)


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"Plex Cleanup GUI running at {url}")
    print("Press Ctrl+C to stop.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    host = os.environ.get("PLEX_CLEANUP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("PLEX_CLEANUP_PORT", DEFAULT_PORT))
    run(host, port)
