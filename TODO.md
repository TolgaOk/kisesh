# Todo

## Session isolation and lifecycle

- [x] When a session is opened or selected, show only its tabs. Tabs owned by other
  sessions must remain live but stay out of the active tab bar.
- [x] Add a Vim-style manager key to save and close a live session. Persist its
  commands, scrollback, layout, and tabs first; close its tabs only after a
  successful save, transitioning it from live to saved.
- [x] When no session is active and unowned tabs exist, opening a session must ask
  whether to:
  - attach the current unowned tabs to the session being opened;
  - auto-name and save those tabs as a separate session, then open the requested
    session without mixing their tabs; or
  - cancel without changing tabs or session state.
- [x] Cover multi-tab sessions, multiple simultaneously live sessions, failed saves,
  canceled prompts, and accidental-close recovery with practical rendering and
  end-to-end tests.

## Selected session contents

- [x] Expand the focused session row like a folder tree without expanding every
  session in the list.
- [x] Show its tabs as child rows and each tab's panes or foreground programs on a
  compact second row, separated by commas.
- [x] Mark each Claude and Codex pane with a recognizable agent symbol plus concise
  useful context such as pane count, active command, and restore availability.
- [x] Keep the preview theme-aware, Vim-navigable, readable in narrow overlays, and
  usable with a text fallback when decorative glyphs are unavailable.
- [x] Cover live and saved context, multi-tab and multi-agent sessions, empty tabs,
  long names, narrow rendering, and selection changes with reviewed render tests.
