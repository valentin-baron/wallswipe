# wallswipe

Tinder for wallpapers. Search several wallpaper sites at once, get one fused stream,
swipe **right to download** the original-resolution file into a folder, **left to skip**.
Anything you've swiped is remembered and never shown again.

Single-file Python (stdlib only) + one HTML page. It runs a local server on 127.0.0.1
and opens itself in a chromeless Chromium app window (falls back to your default browser).

## Run

```sh
./wallswipe.py              # opens the window; closing it stops the server
./wallswipe.py --no-window  # just serve, prints the URL
```

Install as an app:

```sh
ln -s "$PWD/wallswipe.py" ~/.local/bin/wallswipe
cp wallswipe.desktop ~/.local/share/applications/
```

## Sources

| Source    | Key needed | Env var               | Notes |
|-----------|------------|-----------------------|-------|
| Wallhaven | no         | `WALLHAVEN_API_KEY`   | SFW only. Server-side ratio / resolution / category filters. |
| Unsplash  | yes (free) | `UNSPLASH_ACCESS_KEY` | https://unsplash.com/oauth/applications |
| Pexels    | yes (free) | `PEXELS_API_KEY`      | https://www.pexels.com/api/ |

Sources without a key show up greyed out. If the env var is unset and a `secret`
command is on `PATH`, it tries `secret get wallhaven.api_key` / `unsplash.access_key` /
`pexels.api_key`.

## Controls

| Action           | Mouse                | Keys                  |
|------------------|----------------------|-----------------------|
| Download         | drag right / ♥       | `→` `L` `D`           |
| Skip             | drag left / ✕        | `←` `H` `A`           |
| Undo             | ↶                    | `Z` `U` `Backspace` (also deletes the downloaded file) |
| Open source page | ↗                    | `O`                   |
| Search           |                      | `/`                   |

Filters: sort (relevance / top / latest / random), aspect ratio (incl. 21:9 and 32:9),
minimum resolution, sources, Wallhaven categories. They persist between runs.

## Files

- Downloads: `~/Pictures/Wallpapers` by default — click the folder button to change it.
  Named `<source>-<id>.<ext>`.
- State: `~/.local/state/wallswipe/` (`config.json`, `seen.json`). Delete `seen.json`
  to see skipped wallpapers again.
