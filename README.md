<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/kisesh-logo-dark.gif">
  <source media="(prefers-color-scheme: light)" srcset="docs/kisesh-logo-light.gif">
  <img alt="KiSesh" src="docs/kisesh-logo-light.gif" width="650">
</picture>

[![Kitty 0.47.2+](https://img.shields.io/badge/Kitty-0.47.2%2B-7F52FF)](https://sw.kovidgoyal.net/kitty/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://www.apple.com/macos/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Kitty-native, recoverable sessions **without** a terminal multiplexer.

<img src="docs/kisesh.png" alt="KiSesh session manager" width="650">

KiSesh groups native Kitty tabs and panes into named sessions.

> [!NOTE]
> KiSesh does **not** preserve the in-memory state of running processes.


### Features

- Provide `live`, `saved`, `archived` session states
- Autosave: layout, terminal context, and commands
- Session recovery (see `apps.toml` for configuring)
  - bring back history up to 2000 lines
  - rerun **recognized** last running command
  - prefill **unrecognized** commands and arguments without executing them
  - auto resume `claude`, `codex`, and `pi` sessions

> [!IMPORTANT]
>
> Enable agent session recovery hooks:
>
> ```sh
> kisesh agents enable
> ```
>
> Inspect them with `kisesh agents status`; remove them with `kisesh agents disable`.

## Install

**requirements**

- [Kitty](https://sw.kovidgoyal.net/kitty/) >= 0.47.2
- Nerd Font

```sh
curl -LsSf https://raw.githubusercontent.com/TolgaOk/kisesh/v0.1.2-alpha/install.sh | sh
```

Using `uv`:

```sh
uv tool install --python 3.11 https://github.com/TolgaOk/kisesh/archive/refs/tags/v0.1.2-alpha.tar.gz
kisesh install
```

Restart Kitty after either installation method, then press `Alt+S` to open the KiSesh session manager.

## Configuration

Session data is stored locally in `~/.local/share/kisesh/` by default.

`kisesh install` creates `~/.config/kisesh/apps.toml` from the
[bundled defaults](kisesh/default_apps.toml). Existing configuration is kept.

```toml
version = 2

[defaults]
restore = "prefill"
label = "App"
icon = ""

[agents.claude]
match = ["claude", "claude-*"]
restore = "resume"
adapter = "claude"
label = "Claude"
icon = "✻"

[apps.nvim]
match = ["nvim", "nvim-*", "vim", "vi"]
restore = "captured"
label = "Vim"
icon = ""

# Additional profiles follow the same section-specific fields.
```
