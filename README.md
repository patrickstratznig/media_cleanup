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

- Plex URL and an admin token are required for scanning.
- Radarr URL and API key are required for movie deletion.
- Sonarr URL and API key are required for TV deletion.
- Use `Load libraries` in the GUI to fetch Plex movie and TV libraries, then
  select exactly which movie library and TV library to scan. Leaving a library
  unselected skips that media type.
- Watch data controls whether inactivity is based on the Plex account tied to
  the token or Plex server playback history for any user on the server. `Any
  user on server` requires a Plex server admin token.
- Inactive days controls the cutoff and defaults to 365 days. Anything never
  watched or last watched before that cutoff appears as a candidate.
- Movies and TV results can be sorted by title or size and collapsed while you
  review candidates.
- In `Any user on server` mode, movie and season results can also show which
  user last watched the item when Plex history includes a resolvable account.

## Deletion Behavior

- Movies are matched to Radarr by TMDB ID, IMDb ID, then title/year fallback.
- Shows are matched to Sonarr by TVDB ID, IMDb ID, then title/year fallback.
- Movie deletion removes the movie from Radarr when matched, and also deletes it
  from Plex so the file is removed from disk and the item disappears from Plex.
- Whole-show deletion removes the show from Sonarr when matched, and also
  deletes it from Plex so the files are removed from disk and the item
  disappears from Plex.
- Season deletion deletes matching Sonarr episode files for the selected
  season numbers. The app first unmonitors those seasons in Sonarr so they are
  not downloaded again. It also deletes the selected season from Plex so the
  files are removed from disk and the season disappears from Plex.
- Sonarr does not remove individual season entries from a series. To make a TV
  item disappear from Sonarr entirely, delete the whole show.
- If a movie or show is no longer present in Radarr or Sonarr, deletion falls
  back to Plex-only deletion.
- TV scans include the whole show when at least one season is inactive. All
  seasons are shown, and you can still choose recent seasons or the whole show
  for deletion from the review list.
- `Any user on server` mode uses Plex playback history for the selected library.
  That lets the scan see activity from shared users, but it requires a Plex
  server admin token with access to playback history.
- Nothing is deleted during scan. Deletion only happens after selecting rows and
  confirming in the browser.

## Getting Tokens

- Plex token: Plex account/profile token used by your server. Plex documents how
  to find it here:
  <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>
- Radarr/Sonarr API keys: `Settings -> General -> Security -> API Key`.

Keep this GUI bound to `127.0.0.1` unless you put it behind your own trusted
network controls.
