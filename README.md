# Plex Cleanup GUI

A local browser GUI that scans Plex for all movies and TV shows in the selected
libraries, shows last watched details, lets you filter by watch age, and then
delete selected movies, whole shows, or TV seasons through Radarr or Sonarr.

## Run

```bash
python3 plex_cleanup_gui.py
```

Open:

```text
http://127.0.0.1:8765
```

The app writes `config.json` next to the script after you save settings.

## Settings

- Plex URL and an admin token are required for scanning.
- Radarr URL and API key are required for movie deletion.
- Sonarr URL and API key are required for TV deletion.
- Use `Load libraries` in the GUI to fetch Plex movie and TV libraries, then
  select exactly which movie library and TV library to scan. Leaving a library
  unselected skips that media type.
- Watch data is automatic. If the Plex token can read server playback history,
  the scan uses watch data from any user on the server. Otherwise it falls back
  to the watch data for the token's own Plex account and shows a warning.
- Delete target controls whether deletes only update Radarr or Sonarr, or also
  remove the selected media from Plex and disk.
- Inactive days controls the filter threshold and defaults to 365 days. You can
  use it with the movie and TV filter controls to show everything, only never
  watched items, items not watched in that many days, or items watched within
  that many days.
- Movies and TV results can be sorted by title or size, filtered by watch age,
  and collapsed while you review the list.
- When server playback history is available, movie and season results can also
  show which user last watched the item when Plex history includes a resolvable
  account.

## Deletion Behavior

- Movies are matched to Radarr by TMDB ID, IMDb ID, then title/year fallback.
- Shows are matched to Sonarr by TVDB ID, IMDb ID, then title/year fallback.
- In `Radarr/Sonarr only` mode, movie deletes remove the item only from Radarr,
  whole-show deletes remove the full series only from Sonarr, and season-only
  deletes only unmonitor those seasons in Sonarr.
- In `Radarr/Sonarr + Plex/disk` mode, movie deletes remove the movie from
  Radarr when matched and also delete it from Plex and disk.
- In `Radarr/Sonarr + Plex/disk` mode, whole-show deletes remove the full series
  from Sonarr when matched and also delete it from Plex and disk.
- In `Radarr/Sonarr + Plex/disk` mode, season-only deletes unmonitor the
  selected seasons in Sonarr, delete matching Sonarr episode files, and also
  delete the selected season from Plex and disk.
- Sonarr does not remove individual season entries from a series. To make a TV
  item disappear from Sonarr entirely, delete the whole show.
- If a movie or show is no longer present in Radarr or Sonarr, `Radarr/Sonarr +
  Plex/disk` mode falls back to Plex-only deletion.
- TV scans show every show that has episode files in the selected Plex library.
  All seasons are shown, and you can choose recent seasons, old seasons, or the
  whole show for deletion from the review list.
- When the Plex token can read server playback history, the scan uses that data
  for the selected libraries so it can see activity from shared users.
- When the Plex token cannot read server playback history, the scan falls back
  to the token's own account watch data and shows a warning in the UI.
- If the Plex token does not have media deletion rights, the UI shows a warning
  that Plex/disk delete mode will fail until deletion is allowed.
- Nothing is deleted during scan. Deletion only happens after selecting rows and
  confirming in the browser.

## Getting Tokens

- Plex token: Plex account/profile token used by your server. Plex documents how
  to find it here:
  <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>
- Radarr/Sonarr API keys: `Settings -> General -> Security -> API Key`.

Keep this GUI bound to `127.0.0.1` unless you put it behind your own trusted
network controls.
