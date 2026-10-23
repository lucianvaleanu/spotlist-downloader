# Spotlist Downloader

Spotlist Downloader is a local Qt desktop application that reads Spotify
playlist metadata and downloads matching audio through a local `slskd`
container. There is no web server or browser frontend.

## Run

Install the Python dependencies with [uv](https://docs.astral.sh/uv/), ensure
Docker is running, then launch the GUI:

```bash
uv sync
uv run python desktop.py
```

The application starts and configures `slskd` through Docker when a download
begins. Soulseek credentials are stored encrypted in `~/.spotlist`; set
`SPOTLIST_DATA_DIR` to change that location. The app waits for the Soulseek
session to be connected and logged in before searching.
