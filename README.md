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
`kitty.conf`. It also restores any previous custom tab bar on disable.

The installer creates `~/.config/kitty-workbench/apps.toml` once and never
overwrites it. It maps executable globs to restore behavior, exact arguments,
labels, icons, and agent identity. Icons require a Nerd Font—or another font
containing the configured glyphs; replace them with plain text if needed.

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
comma-separated pane row. Configured icons such as `✻ Claude`, `󰋙 Codex`, and
` Vim` appear there. Each native top-bar tab shows only its focused pane's icon
and name. `•` marks the focused pane,
and `↻` marks restorable or prefilled state. ` Session` remains a separate
inactive segment; `󰌸 Unattached` clearly marks tabs with no session owner.

If the current window contains unowned tabs, opening a session asks whether to
attach them, edit a random unused name and save them separately, discard them
after confirmation, or cancel unchanged. Session names are unique across the
active and archived lists. A tab opened with Kitty's `new_tab_with_cwd` while a
session is active joins and autosaves with that session, so this choice appears
only for unrelated tabs. Use `x` to save all commands, scrollback, layout, and
tabs before closing the live session. Each save resolves the current TOML rules
into its snapshot. Restore recreates tabs, panes, layout, directories, up to
2,000 shell-history and scrollback lines, and only the last command's output. It
then applies the saved mode: `resume` safely normalizes Claude/Codex, `captured`
runs captured arguments, `configured` runs exact `argv`, `prefill` types a
reminder without Enter, and `ignore` starts only the normal shell. Unmatched
apps use the safe default, `prefill`.

`Cmd+W` closes one tab and autosaves the remaining session. On the final tab it
defaults to a native **No** confirmation, saves before closing, then shows the
next live session in that Kitty window. Repeated presses cannot close through
the confirmation into the next session.

Archive (`e`) moves an inactive session to the lower list. Remove (`D`) sends
one to recoverable Workbench trash.

## Keys

`j/k` move · `g/G` ends · `Ctrl-d/u` half-page · `/` search ·
`l/Enter/Space` open · `n` new · `a/d/c` add/detach/copy tab · `s` save ·
`x` save+close · `r` rename session · `Shift+R` rename tab ·
`e/u` archive/unarchive · `Shift+D` remove · `?` help · `q/Esc/h` close

## Verify

```sh
just check
```

The suite exercises real lifecycle scenarios, PTY history recall when Kitty and
zsh are available, strict Kitty parsing when installed, reviewed golden
rendering, practical terminal sizes, and 100% statement and branch coverage.
