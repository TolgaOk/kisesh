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

Restart Kitty once, press `Alt+S`, then `n` to name a session. In an unattached
Kitty window, choose whether it contains all current tabs or starts with one
fresh shell after the existing tabs are preserved or discarded. `Alt+S` toggles
the overlay; it never creates a split or a manager pane. The installer validates
the complete Kitty config before changing it and keeps one backup beside
`kitty.conf`.

```sh
./install --disable    # keep code and sessions
./install --uninstall  # keep sessions
./install --purge      # permanently delete session data
```

## Example

Name the current project with `Alt+S`, then `n`. If several unattached tabs are
already open, use them all as one session, preserve them under the suggested
editable name and start fresh, or discard them after confirmation. A session
may contain many tabs and panes. Opening another session shows only that
session's tabs in the current Kitty window while every other session keeps
running. Other Kitty OS windows retain their normal tab bars.

Moving with `j/k` expands only the selected session: each tab is followed by a
comma-separated pane row. `✻ Claude` and `◇ Codex` identify agent panes, `•`
marks the focused pane, and `↻` marks saved state that can be restored or
prefilled.

If the current window contains unowned tabs, opening a session asks whether to
attach them, edit a random unused name and save them separately, discard them
after confirmation, or cancel unchanged. Session names are unique across the
active and archived lists. A tab opened with Kitty's `new_tab_with_cwd` while a
session is active joins and autosaves with that session, so this choice appears
only for unrelated tabs. Use `x` to save all commands, scrollback, layout, and
tabs before closing the live session. Reopening restores up to 2,000 history
entries and scrollback lines, the last command output, safe `top` state, and
Claude/Codex resume commands; unknown commands are left at the prompt without
being run.

`Cmd+W` closes one tab and autosaves the remaining session. On the final tab it
defaults to a native **No** confirmation, saves before closing, then shows the
next live session in that Kitty window. Repeated presses cannot close through
the confirmation into the next session.

Archive (`e`) moves an inactive session to the lower list. Remove (`D`) sends
one to recoverable Workbench trash.

## Keys

`j/k` move · `g/G` ends · `Ctrl-d/u` half-page · `/` search ·
`l/Enter/Space` open · `n` new · `a/d/c` add/detach/copy tab · `s` save ·
`x` save+close · `r` rename · `e/u` archive/unarchive · `Shift+D` remove ·
`?` help · `q/Esc/h` close

## Verify

```sh
just check
```

The suite exercises real lifecycle scenarios, PTY history recall when Kitty and
zsh are available, strict Kitty parsing when installed, reviewed golden
rendering, practical terminal sizes, and 100% statement and branch coverage.
