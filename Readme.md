# K2S Downloader

Python tools for downloading Keep2Share files with parallel range requests. The project now offers both a command line workflow and a PySide6 desktop UI.

![](src/assets/GUI.png)

## Environment
- Tested on Python 3.13.5 on Windows

## Prerequisites
1. Install uv (https://github.com/astral-sh/uv)
2. Ensure ffmpeg is available in your PATH if you want automatic media validation

## Setup
```
uv sync
```
This creates an isolated environment and installs the dependencies declared in pyproject.toml.

## Command Line Usage
```
uv run k2s-downloader <k2s_url> [--filename NAME] [--threads N] [--split-size SIZE] [--no-ffmpeg-check]
```
Example:
```
uv run k2s-downloader "https://k2s.cc/file/abc123" --threads 20 --split-size 20MB
```

## Security Note: Public Proxies
This tool can route chunk downloads through third-party HTTPS proxies to work around IP blocks/rate limits. The candidate list is fetched from a public, unauthenticated source (proxyscrape.com) and only lightly vetted (a basic reachability check), and the proxy connection itself is plain, unauthenticated HTTP. A malicious or compromised proxy in this list is in a position to observe or tamper with traffic routed through it. The downloader always prefers a direct connection over a proxy when one is available; only fall back to the proxy pool for downloads where that risk is acceptable to you.

## GUI Usage
```
uv run k2s-downloader-gui
```
Alternative:
```
uv run -- python -m k2s_downloader.gui.app
```
Provide the K2S link inside the application, optionally override the output filename, adjust the thread count and split size, then start the download. Captcha prompts appear inline.

## Captcha Handling
Keep2Share requires solving an image captcha to authorize each download session. Behavior differs slightly by interface:
- **GUI**: the captcha image appears inline in the app; type your answer into the field below it and submit. The download proceeds automatically once accepted.
- **CLI**: the captcha image opens in your system's default image viewer and the program waits for your answer on stdin (or supply your own `captcha_callback` if you're using `k2s_downloader` as a library, e.g. to automate solving).

If an answer is rejected, you'll be prompted again with a fresh challenge; after 3 rejected attempts the download aborts with an error suggesting your IP may be blocked or rate-limited rather than continuing to prompt indefinitely.

## Legal Notice
This is an unofficial, third-party tool and is not affiliated with, endorsed by, or supported by Keep2Share. It automates the same download flow available through Keep2Share's own website. You are responsible for complying with [Keep2Share's Terms of Service](https://k2s.cc/) and all applicable copyright and data-protection law for anything you download with it; the maintainers accept no liability for how it's used. See [LICENSE](LICENSE) for the software license itself (MIT — provided as-is, without warranty).

## Alternative Entry Points
Without the console scripts, the same entry points are reachable as plain modules/scripts:
```
uv run python -m k2s_downloader <k2s_url> [options]   # CLI
uv run python k2s_gui_entry.py                        # GUI
```

## Development
```
uv sync --extra dev        # or: pip install -e ".[dev]"
uv run pytest -q           # run the test suite
uv run ruff check .        # lint
```
Both checks also run automatically in CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) for every push and pull request. To build a standalone executable, install the optional build extra (`pip install -e ".[build]"`) for PyInstaller. See [CONTRIBUTING.md](CONTRIBUTING.md) for code style, testing conventions, and the commit-message format used in this repo.
