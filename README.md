# kitty-workbench

Kitty-native, recoverable sessions without a terminal multiplexer.

`experimental` · `Kitty overlay` · `macOS` · `Python 3.11+` · `Vim keys`

A session can contain many Kitty tabs and panes. Workbench saves their layout,
working directories, bounded terminal context, and safe foreground-app restore
instructions while Kitty continues to own every PTY directly.

## Install

From this checkout:

```sh
./install
```

Restart Kitty once, press `Alt+S`, then `n` to name the current tab as a
session. `Alt+S` toggles the overlay; it never creates a split or a manager
pane. The installer validates the complete Kitty config before changing it and
keeps one backup beside `kitty.conf`.

```sh
./install --disable    # keep code and sessions
./install --uninstall  # keep sessions
./install --purge      # permanently delete session data
```

## Example

Build a research workspace like this:

```text
Research
├─ Agents: 3 panes — Claude, Codex, top
└─ Build:  1 pane  — shell, tests, Git history
```

Open `Alt+S`, select `Research`, then use `a` from each source tab to attach it.
Use `d` to detach a tab without closing it, or `c` to copy only the current
tab's safe layout into an inactive session.

After a command, tab action, or layout change, autosave is triggered and
debounced. If a pane is closed with `Cmd+W`, its state is captured before Kitty
destroys the screen. Opening the session restores:

- tabs, panes, layout, titles, and working directories;
- up to 2,000 shell commands as recallable history, never startup code;
- up to 2,000 lines of normal scrollback and the last completed command output;
- approved interactive apps such as `top`, plus stable Claude and Codex resume
  commands; an unknown command is written at the prompt but not executed.

Archive (`e`) only declutters an inactive session; `u` returns it. Remove (`D`)
moves an inactive session to Workbench trash after confirmation.

## Keys

`j/k` move · `g/G` ends · `Ctrl-d/u` half-page · `/` live search ·
`l/Enter/Space` open · `n` new · `a/d/c` add/detach/copy tab · `s` save ·
`r` rename · `e/u` archive/unarchive · `D` remove · `?` help · `q/Esc/h` close

## Verify

```sh
just check
```

The suite exercises real lifecycle scenarios, PTY history recall when Kitty and
zsh are available, strict Kitty parsing when installed, reviewed golden
rendering, practical terminal sizes, and 100% statement and branch coverage.
