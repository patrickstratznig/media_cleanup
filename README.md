# Plex Cleanup GUI

A local browser GUI that scans Plex for movies and TV seasons that have never
been watched, or have not been watched in a configured number of days. You can
review candidates, see file sizes, select individual movies, whole shows, or TV
seasons, then delete them through Radarr or Sonarr.

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

- Plex URL and token are required for scanning.
- Radarr URL and API key are required for movie deletion.
- Sonarr URL and API key are required for TV deletion.
- Use `Load libraries` in the GUI to fetch Plex movie and TV libraries, then
  select exactly which movie library and TV library to scan. Leaving a library
  unselected skips that media type.
- Inactive days controls the cutoff and defaults to 365 days. Anything never
  watched or last watched before that cutoff appears as a candidate.

## Deletion Behavior

- Movies are matched to Radarr by TMDB ID, IMDb ID, then title/year fallback.
- Shows are matched to Sonarr by TVDB ID, IMDb ID, then title/year fallback.
- Movie deletion adds a Radarr import exclusion by default so Radarr will not
  re-add the movie later.
- Whole-show deletion calls Sonarr series deletion with `deleteFiles=true` and
  adds a Sonarr import-list exclusion by default.
- Season deletion deletes matching Sonarr episode files for the selected
  season numbers. The app first unmonitors those seasons in Sonarr so they are
  not downloaded again. The series remains in Sonarr.
- Sonarr does not remove individual season entries from a series. To make a TV
  item disappear from Sonarr entirely, delete the whole show.
- TV scans include the whole show when at least one season is inactive. All
  seasons are shown, but recently watched seasons are visible only and cannot be
  selected for deletion.
- Nothing is deleted during scan. Deletion only happens after selecting rows and
  confirming in the browser.

## Getting Tokens

- Plex token: Plex account/profile token used by your server. Plex documents how
  to find it here:
  <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>
- Radarr/Sonarr API keys: `Settings -> General -> Security -> API Key`.

Keep this GUI bound to `127.0.0.1` unless you put it behind your own trusted
network controls.
