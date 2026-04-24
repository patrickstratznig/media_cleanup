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
- Movie and TV library fields can be left blank. The first Plex movie/show
  library will be used. You can also enter the library name or key.
- Inactive days controls the cutoff. Anything never watched or last watched
  before that cutoff appears as a candidate.

## Deletion Behavior

- Movies are matched to Radarr by TMDB ID, IMDb ID, then title/year fallback.
- Shows are matched to Sonarr by TVDB ID, IMDb ID, then title/year fallback.
- Whole-show deletion calls Sonarr series deletion with `deleteFiles=true`.
- Season deletion deletes matching Sonarr episode files for the selected
  season numbers. The series remains in Sonarr.
- Nothing is deleted during scan. Deletion only happens after selecting rows and
  confirming in the browser.

## Getting Tokens

- Plex token: Plex account/profile token used by your server. Plex documents how
  to find it here:
  <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>
- Radarr/Sonarr API keys: `Settings -> General -> Security -> API Key`.

Keep this GUI bound to `127.0.0.1` unless you put it behind your own trusted
network controls.
