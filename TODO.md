# Todo

## Session isolation and lifecycle

- When a session is opened or selected, show only its tabs. Tabs owned by other
  sessions must remain live but stay out of the active tab bar.
- Add a Vim-style manager key to save and close a live session. Persist its
  commands, scrollback, layout, and tabs first; close its tabs only after a
  successful save, transitioning it from live to saved.
- When no session is active and unowned tabs exist, opening a session must ask
  whether to:
  - attach the current unowned tabs to the session being opened;
  - auto-name and save those tabs as a separate session, then open the requested
    session without mixing their tabs; or
  - cancel without changing tabs or session state.
- Cover multi-tab sessions, multiple simultaneously live sessions, failed saves,
  canceled prompts, and accidental-close recovery with practical rendering and
  end-to-end tests.
