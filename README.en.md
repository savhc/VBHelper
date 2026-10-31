# VBHelper

> **V**ibe + **H**elper — a desktop companion for vibe coding.

A floating ball that lives on your Windows desktop. Click it to get the project
tree, and from there the tools you actually use: dependency health checks,
local project indexing, an AI tool bus. One self-contained executable. No
runtime to install, no browser engine inside.

`Windows 10 19045+` · `Python 3.10+` · `Apache-2.0` · `Single-file EXE`

[简体中文](README.md) | English

![VBHelper](vbhelper/assets/vbhelper_256.png)

---

## Contents

- [Why](#why)
- [Features](#features)
- [Interaction](#interaction)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Install and run](#install-and-run)
- [Build](#build)
- [Tests](#tests)
- [Layout](#layout)
- [Data and configuration](#data-and-configuration)
- [Design constraints](#design-constraints)
- [Known limitations](#known-limitations)
- [FAQ](#faq)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Why

File browsers in AI coding tools are thin: no directory structure, reopening a
project means restarting, and reaching a file means hunting for its path first.
VBHelper turns "project context" into a floating ball:

- always on the desktop, no taskbar button;
- one click opens the full tree of the current project, scanned in the
  background and expanded on demand;
- right-click menus on a directory, a file, or empty space offer actions scoped
  to that path;
- common developer actions (dependency health, project index, API sites, AI
  tool bus) live in the same menu instead of yet another window.

It does not replace an IDE and does not take over editing. It handles the
"look at / edit / search this path" class of actions.

## Features

### Files and navigation

| Capability | Description |
|---|---|
| Project tree | Floating ball expands into a file tree; background scanning, non-blocking expansion; directory mtime invalidates the cache automatically |
| Marks and notes | Colour marks and free-text notes per path, stored in `.vbhelper.json` inside the project; falls back to global config when the project is read-only |
| Quick edit | Six viewers: text, Hex, PE, JAR, Class, Markdown; large files load in the background, binaries use mmap, text above 2 MB is paged read-only |
| Quick search | Filename index plus full-text content search; `Enter` opens the hit (`Ctrl+P` inside the panel) |
| Git status | Tree colours files by porcelain state; stage, unstage and commit from the tree |

### Developer tools

| Capability | Description |
|---|---|
| Dependency doctor | Parses `requirements*.txt`, `pyproject.toml`, `Pipfile`, `setup.cfg`, `package.json` + lock, `pom.xml`, `build.gradle[.kts]`, `libs.versions.toml`, `go.mod`; resolves latest versions from PyPI, npm, Maven Central and the Go proxy; checks OSV for vulnerabilities; emits upgrade commands and a Markdown / JSON report |
| API workbench | Manage API sites: base URL, key, connectivity test, balance query. Keys are encrypted with Windows DPAPI and shown masked; can write Claude Code settings in one click |
| Project RAG | Silent multi-threaded indexing into SQLite FTS5 (unicode61) plus a trigram index; short CJK queries fall back to trigram; hits carry `relative/path:start-end` so an AI can read exactly the right lines |
| Prompt workbench | 128 engineering prompt templates (architecture, refactoring, review, reverse engineering, performance, frameworks) with categories, search, placeholder filling, import and export |

### AI collaboration

| Capability | Description |
|---|---|
| MCP tool bus | 18 local tools for AI clients: screen capture, mouse and keyboard, web search, article extraction, clipboard, RAG lookup, project file read, system info, notifications |
| Transport and auth | Binds `127.0.0.1` only, random port plus bearer token (DPAPI-backed, constant-time comparison); both direct HTTP and a stdio bridge are supported |
| Safety model | Risky tools are off by default; input automation follows an ask / session / never policy; `Ctrl+Alt+Shift+Esc` is a global panic stop; every call is audited and redacted |
| Self-healing bridge | When the stdio bridge finds no running instance, it launches the GUI windowless and waits up to 15 seconds for the service |

### Desktop and system

| Capability | Description |
|---|---|
| Music player | Local library, online search (source script supplied by the user), LRC lyrics, mini player; FLAC / M4A are transcoded through ffmpeg |
| Network tools | DNS latency benchmark and switching, DoH / DoT timing with certificate details, ECH detection, hosts editing (admin) |
| HUD | Always-on-top CPU / memory / disk / network / GPU / temperature dashboard; three background modes, draggable and lockable |
| Themes | 12 presets plus a custom accent colour, applied live without restart |

## Interaction

| Action | Result |
|---|---|
| Drag the ball | Free movement; snaps to the screen edge on release |
| Click the ball | Expand or collapse the project panel |
| Right-click a directory | Expand, drill down, build RAG index, open a terminal, run a launcher entry |
| Right-click a file | Quick edit, open in the prompt workbench, copy path |
| Right-click empty space | Refresh, open in Explorer, run a launcher entry |
| `Ctrl+P` in the panel | Quick search |
| `Esc` in the panel | Collapse |

Both the ball and the panel can be locked; a locked window ignores dragging.

## Architecture

```text
run.py
  └─ app.py  VBHelper(FloatingMixin, MetaMixin, ScanMixin, TreeMixin)
       ├─ AppContext          config / paths / theme / tasks / HTTP / db / windows / feature registry
       ├─ ui/                 DPI scaling, theme tokens, PIL rendering, widgets, virtual list, async, window shell
       ├─ store/              SQLite (WAL), HTTP conditional cache, DPAPI secret store, audit log
       ├─ services/           Git, RAG, web extraction and search, screen capture, input, clipboard
       └─ features/           deps / apiwork / mcp / rag, loaded by feature flag
```

**Threading**: Tk stays on the main thread. Network, disk and subprocess work
runs through `TaskRunner` and returns through a queue. Every task carries a `key`
and a `group`; closing a window or quitting cancels the group.

**Feature protocol**: each feature implements `Feature` (`mount` / `open` /
`shutdown`) and is loaded by `FeatureRegistry` according to `config.features`.
A disabled feature imports nothing, creates no window and binds no port.

**Dependency boundary**: three third-party runtime packages — Pillow, psutil,
certifi. HTTP uses the standard library, JSON-RPC is hand-written, Markdown is
hand-written, and OCR / models / browsers are always external.

## Requirements

- Windows 10 19045+ / Windows 11
- Python 3.10+ (running from source)
- Runtime dependencies: Pillow ≥ 10, psutil ≥ 5.9, certifi
- Optional external tools:
  - `git.exe` — status, staging, commits
  - `ffmpeg` — FLAC / M4A transcoding
  - `node.exe` — host for online source scripts

## Install and run

```bash
python -m pip install -r requirements.txt
python run.py
```

The first start creates `%APPDATA%\VBHelper\config.json`. Quit from the tray menu.

## Build

```bash
python -m pip install "pyinstaller>=6.21"
python -m PyInstaller --clean VBHelper.spec
```

Output is `dist\VBHelper.exe`: one file, about 19 MB, 3–6 s cold start.

The build configuration lives in `VBHelper.spec`; do not regenerate it from the
command line:

- `hiddenimports` lists modules that are loaded dynamically. A missing entry
  makes the feature vanish from the packaged app without an error;
- `icon` and the resource list are also in the spec. Passing `--specpath`
  overwrites the whole file with the default template.

## Self-check

Development scripts are not shipped with the source. After cloning, at minimum
confirm it compiles and starts:

```bash
python -m compileall vbhelper
python run.py
```

## Layout

```text
vbhelper/
  app.py            Application class: startup order, tray, floating ball
  context.py        AppContext: config, paths, tasks, HTTP, windows, feature registry
  core.py           Constants, Win32 helpers, config read/write
  floating.py       Ball and panel interaction
  tree.py           Tree rendering and expansion
  scan.py           Scan thread pool and cache
  meta.py           Marks and notes persistence
  editor.py         Quick edit window
  music*.py         Player, online interfaces, source runtime, AES
  promptlab.py      Prompt workbench
  nettools.py       Network tools
  hud.py            Always-on-top dashboard
  mcp_stdio.py      MCP stdio bridge
  ui/               Theme, scaling, rendering, widgets, virtual list, async, window shell
  store/            SQLite, cache, secret store, audit
  services/         Git, RAG, web, screen capture, input, clipboard
  features/         Feature modules loaded by flag
  viewers/          Hex / PE / JAR / Class / Markdown viewers
## Data and configuration

| Path | Content |
|---|---|
| `%APPDATA%\VBHelper\config.json` | Theme, feature flags, music sources, window geometry, recent directories |
| `%APPDATA%\VBHelper\debug.log` | Diagnostic log, written only when enabled in settings |
| `%LOCALAPPDATA%\VBHelper\db\vbhelper.db` | HTTP cache, audit log, RAG index |
| `%LOCALAPPDATA%\VBHelper\mcp\endpoint.json` | MCP endpoint and token, only while the service runs |
| `.vbhelper.json` in a project | Marks and notes for that project |

Secrets are never stored in plain text: `store/secrets.py` encrypts with
`CryptProtectData`, stores base64, and binds the blob to the current Windows user.

## Design constraints

- Single-file executable; no embedded browser engine, model or driver
- Three third-party runtime dependencies only
- Everything drawn with Tk; visual quality comes from PIL supersampling and
  layered windows rather than a GUI framework
- Network, disk and subprocess work never runs on the main thread
- Every feature must be switchable, and a disabled feature loads nothing

## Known limitations

- Windows only. The theme and widgets rely on Win32 layered windows and DWM
- Online playback requires a source script supplied by the user; this repository
  ships none and hosts no audio content
- FLAC / M4A playback requires `ffmpeg` on the system
- Changing DNS or editing hosts requires administrator rights; the app prompts
  for elevation at the entry point
- Git features depend on a system `git.exe`; entries show as unavailable without it
- Global hotkeys are not implemented yet; `Ctrl+P` only works while the panel has focus

## FAQ

**The ball disappeared.**
Right-click the tray icon and choose "Show panel", or end the process and restart.

**A feature is missing from the packaged app.**
`hiddenimports` in `VBHelper.spec` is missing that feature package. PyInstaller
only does static analysis and cannot see module names built with `__import__`.

**The MCP client cannot connect.**
Start the service from the panel and confirm
`%LOCALAPPDATA%\VBHelper\mcp\endpoint.json` exists. The stdio bridge launches the
GUI automatically when nothing is running, waiting up to 15 seconds.

**FLAC plays no sound.**
Install `ffmpeg` and put it on `PATH`. The player transcodes to a 320 kbps mp3
cache before playback.

## Disclaimer

This project is intended for technical study and developer productivity. The
music module does not provide, distribute or host any audio data. Online playback
depends on source scripts supplied by the user, who is responsible for their
legality and accuracy. The project is not affiliated with Kuwo, Kugou, QQ Music,
NetEase Cloud Music, Migu or any other platform; all names and trademarks belong
to their respective owners. Use it in compliance with the laws that apply to you.

## License

Apache License 2.0, see [LICENSE](LICENSE). Third-party attribution is listed in
[NOTICE](NOTICE).
