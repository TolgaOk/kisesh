# kitty-workbench

Kitty-native, recoverable sessions without a terminal multiplexer.

`experimental` · `Kitty 0.47.2+` · `tested 0.48.2` · `macOS` · `Vim keys`

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

Name the current project with `Alt+S`, then `n`. Attach its other tabs with
`a`; a session may contain many tabs and panes. Opening another session shows
only that session's tabs in the current Kitty window while every other session
keeps running. Other Kitty OS windows retain their normal tab bars.

Moving with `j/k` expands only the selected session: each tab is followed by a
comma-separated pane row. `✻ Claude` and `◇ Codex` identify agent panes, `•`
marks the focused pane, and `↻` marks saved state that can be restored or
prefilled.

If the current window contains unowned tabs, opening a session asks whether to
attach them, save them together under an automatic name, or cancel unchanged.
Use `x` to save all commands, scrollback, layout, and tabs before closing the
live session. Reopening restores up to 2,000 history entries and scrollback
lines, the last command output, safe `top` state, and Claude/Codex resume
commands; unknown commands are left at the prompt without being run.

Archive (`e`) moves an inactive session to the lower list. Remove (`D`) sends
one to recoverable Workbench trash.

## Keys

`j/k` move · `g/G` ends · `Ctrl-d/u` half-page · `/` search ·
`l/Enter/Space` open · `n` new · `a/d/c` add/detach/copy tab · `s` save ·
`x` save+close · `r` rename · `e/u` archive/unarchive · `D` remove ·
`?` help · `q/Esc/h` close

## Verify

```sh
just check
```

The suite exercises real lifecycle scenarios, PTY history recall when Kitty and
zsh are available, strict Kitty parsing when installed, reviewed golden
rendering, practical terminal sizes, and 100% statement and branch coverage.
