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
- [x] When `n` is pressed in a Kitty window with no attached session, ask whether
  the new session should:
  - contain every current unowned tab without adding another tab;
  - preserve those tabs under an editable random unused name and start with one
    fresh shell; or
  - discard those tabs after confirmation and start with one fresh shell.
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

## Unowned tabs during switching

- [x] Offer an editable random unused name before saving unowned tabs separately.
- [x] Reject duplicate names across active and archived sessions.
- [x] Make newly opened Kitty-session tabs inherit ownership and autosave without
  showing the unrelated-tab choice.
- [x] Offer discard only behind explicit confirmation and close exact tab IDs only
  after the target session opens successfully.
- [x] Keep attach and cancel unchanged, and cover blank names, declined discard,
  failed restore, failed save, CLI routing, and modal rendering.

## Safe native tab close

- [x] After `Cmd+W` closes a non-final tab, capture its panes and trigger a full
  save of the remaining tabs so the closed tab leaves both snapshot and context.
- [ ] Intercept the final tab of a tracked session with a native confirmation,
  complete a full save before closing, and promote the next live session.
- [ ] Make repeated close keys, cancellation, save failure, stale ownership, and
  unavailable Kitty state fail safely without crossing into another session.

## Persistent session bar

- [ ] Prototype one native custom tab-bar row that combines session identity and
  Kitty tabs without a resident process, terminal layer, or separate panel.
  - Prefer `● Research │ shell │ tests │ ✻ Claude`, with a clear unattached state.
  - Let `tab_bar_edge` place the same design at the top or bottom.
  - Read cached Kitty session/user-variable metadata only; never run subprocesses
    or read session files in the draw path.
  - Preserve the active theme, compact gracefully, and compare reviewed top and
    bottom renders before choosing a default.
- [ ] Decide whether the first version shows only session name and tab count or
  also cached autosave and agent indicators. Treat a true second tab-bar row as
  out of scope unless Kitty gains a native multi-row rendering hook.
- [ ] Replace the existing custom tab bar's legacy `ksm` lookup only in a dedicated
  integration change; do not rewrite unrelated Kitty font or theme settings.
