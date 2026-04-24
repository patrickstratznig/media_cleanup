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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


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
        "add_import_exclusion": False,
    },
    "sonarr": {
        "url": "http://localhost:8989",
        "api_key": "",
    },
    "scan": {
        "inactive_days": 180,
        "include_never_watched": True,
        "include_watched_before_cutoff": True,
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
    plex = config["plex"]
    service = Service(normalize_url(plex["url"]), token=plex["token"].strip())
    query = {"X-Plex-Token": service.token}
    if params:
        query.update({k: v for k, v in params.items() if v not in (None, "")})
    url = service.endpoint(path) + "?" + urllib.parse.urlencode(query)
    return request_json("GET", url, headers={"Accept": "application/json"})


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


def watched_state(item: dict[str, Any], cutoff: int) -> dict[str, Any]:
    view_count = int(item.get("viewCount") or 0)
    last_viewed = int(item.get("lastViewedAt") or 0)
    if not view_count or not last_viewed:
        return {"candidate": True, "reason": "Never watched", "lastViewedAt": None}
    if last_viewed < cutoff:
        return {"candidate": True, "reason": "Not watched recently", "lastViewedAt": last_viewed}
    return {"candidate": False, "reason": "Watched recently", "lastViewedAt": last_viewed}


def find_library_key(config: dict[str, Any], wanted_type: str, configured_name: str) -> str | None:
    sections = directory_list(plex_get(config, "/library/sections"))
    matching_type = [s for s in sections if s.get("type") == wanted_type]
    if configured_name:
        for section in matching_type:
            if str(section.get("title", "")).lower() == configured_name.lower():
                return str(section.get("key"))
            if str(section.get("key", "")) == configured_name:
                return str(section.get("key"))
    return str(matching_type[0].get("key")) if matching_type else None


def get_movie_detail(config: dict[str, Any], rating_key: str) -> dict[str, Any]:
    return first_metadata(plex_get(config, f"/library/metadata/{rating_key}"))


def get_show_seasons(config: dict[str, Any], rating_key: str, cutoff: int) -> list[dict[str, Any]]:
    seasons = []
    for season in metadata_list(plex_get(config, f"/library/metadata/{rating_key}/children")):
        if season.get("type") != "season":
            continue
        season_key = str(season.get("ratingKey"))
        episodes = []
        total_size = 0
        candidate_episodes = 0
        for episode in metadata_list(plex_get(config, f"/library/metadata/{season_key}/children")):
            detail = first_metadata(plex_get(config, f"/library/metadata/{episode.get('ratingKey')}"))
            if not detail:
                detail = episode
            state = watched_state(detail, cutoff)
            size = item_size(detail)
            total_size += size
            if state["candidate"]:
                candidate_episodes += 1
            episodes.append(
                {
                    "ratingKey": str(detail.get("ratingKey") or episode.get("ratingKey")),
                    "title": detail.get("title") or episode.get("title") or "Episode",
                    "index": detail.get("index") or episode.get("index"),
                    "lastViewedAt": state["lastViewedAt"],
                    "reason": state["reason"],
                    "candidate": state["candidate"],
                    "size": size,
                    "sizeText": human_size(size),
                }
            )
        is_candidate = bool(episodes) and candidate_episodes == len(episodes)
        seasons.append(
            {
                "ratingKey": season_key,
                "title": season.get("title") or f"Season {season.get('index', '')}",
                "seasonNumber": int(season.get("index") or 0),
                "episodeCount": len(episodes),
                "candidateEpisodeCount": candidate_episodes,
                "candidate": is_candidate,
                "size": total_size,
                "sizeText": human_size(total_size),
                "episodes": episodes,
            }
        )
    return seasons


def scan_media(config: dict[str, Any]) -> dict[str, Any]:
    inactive_days = int(config["scan"].get("inactive_days") or 180)
    cutoff = int(time.time()) - inactive_days * 86400
    movie_key = find_library_key(config, "movie", config["plex"].get("movie_library", ""))
    show_key = find_library_key(config, "show", config["plex"].get("show_library", ""))
    result: dict[str, Any] = {
        "generatedAt": int(time.time()),
        "inactiveDays": inactive_days,
        "movies": [],
        "shows": [],
        "warnings": [],
    }
    if not movie_key:
        result["warnings"].append("No Plex movie library was found.")
    if not show_key:
        result["warnings"].append("No Plex TV library was found.")

    if movie_key:
        for movie in metadata_list(plex_get(config, f"/library/sections/{movie_key}/all", {"type": 1})):
            detail = get_movie_detail(config, str(movie.get("ratingKey")))
            if not detail:
                detail = movie
            state = watched_state(detail, cutoff)
            if not state["candidate"]:
                continue
            size = item_size(detail)
            ids = extract_guid_ids(detail)
            result["movies"].append(
                {
                    "kind": "movie",
                    "ratingKey": str(detail.get("ratingKey") or movie.get("ratingKey")),
                    "title": detail.get("title") or movie.get("title") or "Movie",
                    "year": detail.get("year"),
                    "lastViewedAt": state["lastViewedAt"],
                    "reason": state["reason"],
                    "size": size,
                    "sizeText": human_size(size),
                    "ids": ids,
                }
            )

    if show_key:
        for show in metadata_list(plex_get(config, f"/library/sections/{show_key}/all", {"type": 2})):
            detail = first_metadata(plex_get(config, f"/library/metadata/{show.get('ratingKey')}")) or show
            ids = extract_guid_ids(detail)
            all_seasons = get_show_seasons(config, str(show.get("ratingKey")), cutoff)
            candidate_seasons = [season for season in all_seasons if season["candidate"]]
            if not candidate_seasons:
                continue
            total_size = sum(season["size"] for season in all_seasons)
            candidate_size = sum(season["size"] for season in candidate_seasons)
            can_delete_whole_show = bool(all_seasons) and len(candidate_seasons) == len(all_seasons)
            result["shows"].append(
                {
                    "kind": "show",
                    "ratingKey": str(detail.get("ratingKey") or show.get("ratingKey")),
                    "title": detail.get("title") or show.get("title") or "Show",
                    "year": detail.get("year"),
                    "size": candidate_size,
                    "sizeText": human_size(candidate_size),
                    "totalSize": total_size,
                    "totalSizeText": human_size(total_size),
                    "canDeleteWholeShow": can_delete_whole_show,
                    "ids": ids,
                    "seasons": candidate_seasons,
                }
            )
    return result


def radarr_service(config: dict[str, Any]) -> Service:
    return Service(normalize_url(config["radarr"]["url"]), api_key=config["radarr"]["api_key"].strip())


def sonarr_service(config: dict[str, Any]) -> Service:
    return Service(normalize_url(config["sonarr"]["url"]), api_key=config["sonarr"]["api_key"].strip())


def match_radarr_movie(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    service = radarr_service(config)
    movies = arr_get(service, "/api/v3/movie")
    ids = item.get("ids", {})
    title = str(item.get("title", "")).lower()
    year = item.get("year")
    for movie in movies:
        if ids.get("tmdb") and str(movie.get("tmdbId")) == str(ids["tmdb"]):
            return movie
        if ids.get("imdb") and str(movie.get("imdbId", "")).lower() == str(ids["imdb"]).lower():
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
    year = item.get("year")
    for series in series_list:
        if ids.get("tvdb") and str(series.get("tvdbId")) == str(ids["tvdb"]):
            return series
        if ids.get("imdb") and str(series.get("imdbId", "")).lower() == str(ids["imdb"]).lower():
            return series
    for series in series_list:
        if str(series.get("title", "")).lower() == title and (not year or series.get("year") == year):
            return series
    return None


def delete_movie(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    movie = match_radarr_movie(config, item)
    if not movie:
        return {"ok": False, "title": item.get("title"), "error": "No Radarr match found"}
    params = {
        "deleteFiles": "true",
        "addImportExclusion": "true" if config["radarr"].get("add_import_exclusion") else "false",
    }
    arr_delete(radarr_service(config), f"/api/v3/movie/{movie['id']}", params)
    return {"ok": True, "title": item.get("title"), "service": "radarr", "matchedTitle": movie.get("title")}


def delete_show(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    series = match_sonarr_series(config, item)
    if not series:
        return {"ok": False, "title": item.get("title"), "error": "No Sonarr match found"}
    arr_delete(sonarr_service(config), f"/api/v3/series/{series['id']}", {"deleteFiles": "true"})
    return {"ok": True, "title": item.get("title"), "service": "sonarr", "matchedTitle": series.get("title")}


def delete_seasons(config: dict[str, Any], item: dict[str, Any], season_numbers: list[int]) -> dict[str, Any]:
    series = match_sonarr_series(config, item)
    if not series:
        return {"ok": False, "title": item.get("title"), "error": "No Sonarr match found"}
    service = sonarr_service(config)
    episodes = arr_get(service, "/api/v3/episode", {"seriesId": series["id"]})
    episode_file_ids = sorted(
        {
            episode.get("episodeFileId")
            for episode in episodes
            if episode.get("seasonNumber") in season_numbers and episode.get("episodeFileId")
        }
    )
    deleted = 0
    errors = []
    for episode_file_id in episode_file_ids:
        try:
            arr_delete(service, f"/api/v3/episodeFile/{episode_file_id}")
            deleted += 1
        except ApiError as exc:
            errors.append(str(exc))
    ok = not errors
    return {
        "ok": ok,
        "title": item.get("title"),
        "service": "sonarr",
        "matchedTitle": series.get("title"),
        "seasonNumbers": season_numbers,
        "deletedEpisodeFiles": deleted,
        "errors": errors,
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
        account = media_container(plex_get(config, "/"))
        checks["plex"] = {"ok": True, "name": account.get("friendlyName") or "Plex"}
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
    h2 { font-size: 16px; margin: 0; }
    .content { padding: 16px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 650; }
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
      .grid, .summary { grid-template-columns: 1fr; }
      .row { grid-template-columns: 28px 1fr; }
      .row > .pill, .row > .sub { justify-self: start; grid-column: 2; }
      .bar { align-items: flex-start; flex-direction: column; }
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
        <button id="scanBtn" class="primary">Scan</button>
        <button id="deleteBtn" class="danger" disabled>Delete selected</button>
      </div>
    </div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>Connections</h2>
        <span id="connectionStatus" class="status">Not tested</span>
      </div>
      <div class="content grid">
        <label>Plex URL<input id="plexUrl" placeholder="http://server:32400"></label>
        <label>Plex token<input id="plexToken" type="password"></label>
        <label>Inactive days<input id="inactiveDays" type="number" min="1"></label>
        <label>Movie library name or key<input id="movieLibrary" placeholder="Movies"></label>
        <label>TV library name or key<input id="showLibrary" placeholder="TV Shows"></label>
        <label class="check-label"><input id="addImportExclusion" type="checkbox"> Add Radarr import exclusion</label>
        <label>Radarr URL<input id="radarrUrl" placeholder="http://server:7878"></label>
        <label>Radarr API key<input id="radarrKey" type="password"></label>
        <span></span>
        <label>Sonarr URL<input id="sonarrUrl" placeholder="http://server:8989"></label>
        <label>Sonarr API key<input id="sonarrKey" type="password"></label>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>Candidates</h2>
        <span id="scanStatus" class="status">Run a scan to begin</span>
      </div>
      <div class="content">
        <div id="summary" class="summary hidden"></div>
        <div id="warnings" class="status"></div>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>Movies</h2>
        <span id="movieCount" class="status">0</span>
      </div>
      <div id="movies" class="content list"></div>
    </section>
    <section>
      <div class="section-head">
        <h2>TV Shows</h2>
        <span id="showCount" class="status">0</span>
      </div>
      <div id="shows" class="content list"></div>
    </section>
    <section id="logSection" class="hidden">
      <div class="section-head"><h2>Result</h2></div>
      <div class="content"><pre id="log"></pre></div>
    </section>
  </main>
<script>
const state = { config: null, scan: null };
const $ = (id) => document.getElementById(id);

function formatDate(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleDateString();
}

function selectedPayload() {
  if (!state.scan) return { movies: [], shows: [] };
  const movieKeys = new Set([...document.querySelectorAll(".movie-select:checked")].map(el => el.value));
  const showKeys = new Set([...document.querySelectorAll(".show-select:checked")].map(el => el.value));
  const movies = state.scan.movies.filter(movie => movieKeys.has(movie.ratingKey));
  const shows = state.scan.shows.map(show => {
    const whole = showKeys.has(show.ratingKey);
    const seasonNumbers = [...document.querySelectorAll(`.season-select[data-show="${show.ratingKey}"]:checked`)].map(el => Number(el.value));
    return { ...show, deleteWholeShow: whole, seasonNumbers };
  }).filter(show => show.deleteWholeShow || show.seasonNumbers.length);
  return { movies, shows };
}

function updateDeleteButton() {
  const payload = selectedPayload();
  const count = payload.movies.length + payload.shows.length;
  $("deleteBtn").disabled = count === 0;
}

function readConfig() {
  return {
    plex: {
      url: $("plexUrl").value,
      token: $("plexToken").value,
      movie_library: $("movieLibrary").value,
      show_library: $("showLibrary").value,
    },
    radarr: {
      url: $("radarrUrl").value,
      api_key: $("radarrKey").value,
      add_import_exclusion: $("addImportExclusion").checked,
    },
    sonarr: {
      url: $("sonarrUrl").value,
      api_key: $("sonarrKey").value,
    },
    scan: {
      inactive_days: Number($("inactiveDays").value || 180),
      include_never_watched: true,
      include_watched_before_cutoff: true,
    },
  };
}

function fillConfig(config) {
  state.config = config;
  $("plexUrl").value = config.plex.url || "";
  $("plexToken").value = config.plex.token || "";
  $("movieLibrary").value = config.plex.movie_library || "";
  $("showLibrary").value = config.plex.show_library || "";
  $("inactiveDays").value = config.scan.inactive_days || 180;
  $("radarrUrl").value = config.radarr.url || "";
  $("radarrKey").value = config.radarr.api_key || "";
  $("addImportExclusion").checked = Boolean(config.radarr.add_import_exclusion);
  $("sonarrUrl").value = config.sonarr.url || "";
  $("sonarrKey").value = config.sonarr.api_key || "";
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
  $("connectionStatus").innerHTML = ["plex", "radarr", "sonarr"].map(name => {
    const check = checks[name];
    const klass = check.ok ? "ok" : "danger";
    return `<span class="pill ${klass}">${name}: ${check.ok ? "ok" : "failed"}</span>`;
  }).join(" ");
  showLog(checks);
}

function showLog(data) {
  $("logSection").classList.remove("hidden");
  $("log").textContent = JSON.stringify(data, null, 2);
}

async function scan() {
  await saveConfig();
  $("scanBtn").disabled = true;
  $("scanStatus").textContent = "Scanning Plex...";
  try {
    state.scan = await api("/api/scan", { method: "POST", body: JSON.stringify(readConfig()) });
    renderScan();
    $("scanStatus").textContent = `Scanned ${new Date(state.scan.generatedAt * 1000).toLocaleString()}`;
  } finally {
    $("scanBtn").disabled = false;
  }
}

function renderScan() {
  const movieTotal = state.scan.movies.reduce((sum, item) => sum + item.size, 0);
  const showTotal = state.scan.shows.reduce((sum, item) => sum + item.size, 0);
  $("summary").classList.remove("hidden");
  $("summary").innerHTML = `
    <div class="metric"><b>${state.scan.movies.length}</b><span>movie candidates</span></div>
    <div class="metric"><b>${state.scan.shows.length}</b><span>show candidates</span></div>
    <div class="metric"><b>${humanBytes(movieTotal)}</b><span>movie storage</span></div>
    <div class="metric"><b>${humanBytes(showTotal)}</b><span>show storage</span></div>
  `;
  $("warnings").textContent = (state.scan.warnings || []).join(" ");
  $("movieCount").textContent = String(state.scan.movies.length);
  $("showCount").textContent = String(state.scan.shows.length);
  $("movies").innerHTML = state.scan.movies.length ? state.scan.movies.map(renderMovie).join("") : `<div class="status">No movie candidates.</div>`;
  $("shows").innerHTML = state.scan.shows.length ? state.scan.shows.map(renderShow).join("") : `<div class="status">No show candidates.</div>`;
  document.querySelectorAll("input[type=checkbox]").forEach(el => el.addEventListener("change", updateDeleteButton));
  document.querySelectorAll(".show-select").forEach(el => el.addEventListener("change", () => {
    document.querySelectorAll(`.season-select[data-show="${el.value}"]`).forEach(season => {
      season.disabled = el.checked;
      if (el.checked) season.checked = false;
    });
    updateDeleteButton();
  }));
  updateDeleteButton();
}

function renderMovie(movie) {
  const year = movie.year ? ` (${movie.year})` : "";
  return `<div class="item">
    <div class="row">
      <input class="movie-select" type="checkbox" value="${movie.ratingKey}">
      <div><div class="title">${escapeHtml(movie.title)}${year}</div><div class="sub">${movie.reason}; last watched ${formatDate(movie.lastViewedAt)}</div></div>
      <span class="pill">${movie.sizeText}</span>
      <span class="sub">${idsText(movie.ids)}</span>
    </div>
  </div>`;
}

function renderShow(show) {
  const year = show.year ? ` (${show.year})` : "";
  const wholeDisabled = show.canDeleteWholeShow ? "" : "disabled";
  const wholeHelp = show.canDeleteWholeShow ? "Select whole show or individual seasons" : "Whole show is locked because some seasons were watched recently";
  return `<div class="item">
    <div class="row">
      <input class="show-select" type="checkbox" value="${show.ratingKey}" ${wholeDisabled} title="${escapeHtml(wholeHelp)}">
      <div><div class="title">${escapeHtml(show.title)}${year}</div><div class="sub">${wholeHelp}</div></div>
      <span class="pill">${show.sizeText} inactive</span>
      <span class="sub">${idsText(show.ids)}</span>
    </div>
    <details class="seasons" open>
      <summary>${show.seasons.length} season${show.seasons.length === 1 ? "" : "s"}</summary>
      ${show.seasons.map(season => `<div class="season">
        <input class="season-select" data-show="${show.ratingKey}" type="checkbox" value="${season.seasonNumber}">
        <div><div class="title">${escapeHtml(season.title)}</div><div class="sub">${season.candidateEpisodeCount}/${season.episodeCount} episodes inactive</div></div>
        <span class="pill">${season.sizeText}</span>
      </div>`).join("")}
    </details>
  </div>`;
}

function idsText(ids) {
  return Object.entries(ids || {}).map(([key, value]) => `${key}:${value}`).join(" ");
}

function humanBytes(size) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(size || 0);
  for (const unit of units) {
    if (amount < 1024 || unit === "TB") return unit === "B" ? `${amount} ${unit}` : `${amount.toFixed(1)} ${unit}`;
    amount /= 1024;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[ch]));
}

async function deleteSelected() {
  const payload = selectedPayload();
  const movieCount = payload.movies.length;
  const showCount = payload.shows.length;
  if (!confirm(`Delete ${movieCount} movie selection(s) and ${showCount} TV selection(s) through Radarr/Sonarr? Files will be deleted.`)) return;
  $("deleteBtn").disabled = true;
  $("scanStatus").textContent = "Deleting selected media...";
  try {
    const result = await api("/api/delete", { method: "POST", body: JSON.stringify({ config: readConfig(), selection: payload }) });
    showLog(result);
    $("scanStatus").textContent = result.ok ? "Delete completed" : "Delete completed with errors";
  } finally {
    $("deleteBtn").disabled = false;
  }
}

async function init() {
  try {
    fillConfig(await api("/api/config"));
    $("saveBtn").addEventListener("click", () => saveConfig().catch(err => showLog({ error: err.message })));
    $("testBtn").addEventListener("click", () => testConnections().catch(err => showLog({ error: err.message })));
    $("scanBtn").addEventListener("click", () => scan().catch(err => {
      $("scanStatus").textContent = "Scan failed";
      showLog({ error: err.message });
    }));
    $("deleteBtn").addEventListener("click", () => deleteSelected().catch(err => showLog({ error: err.message })));
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
