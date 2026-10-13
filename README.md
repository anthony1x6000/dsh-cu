# dsh-cu

**Computer use on KDE Plasma 6 Wayland for DeepSeek Harness** — windows, screenshots, pointer and keyboard injection, the AT-SPI accessibility tree, and first-class flatpak support.

dsh-cu registers native harness tools (`cu_*`) that drive the live desktop. The
TypeScript plugin owns the tool surface; a bundled Python bridge owns the desktop
work that needs a resident process: KWin scripting, KWin's EIS input endpoint via
libei, `ScreenShot2`/Spectacle capture, and PyGObject's AT-SPI binding.

Every tool either **proves** where it acts — a KWin rectangle, a matched
accessibility element, a captured frame with a stated logical origin — or it
reports why it cannot. A coordinate that could be wrong is never a coordinate
this plugin hands out.

Derived from [kwin-mcp](https://github.com/isac322/kwin-mcp) (MIT,
Copyright © 2026 Byeonghoon Yoo); that licence is inherited — see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

## Relationship to kwin-mcp (and why this is not a fork)

dsh-cu reuses kwin-mcp's mechanisms for talking to KWin — the scripting
transport, the `ScreenShot2` raw-frame capture, the EIS/libei input client, the
AT-SPI window matching, and the flatpak scope identity — and its `NOTICE` maps
each module to the upstream file it came from, pinned to **v0.11.0
(`dfcc4ea50a32`)**. It is not a git fork of kwin-mcp, deliberately:

* it is a **different artifact for a different host** — a DeepSeek Harness
  plugin with native `cu_*` tools and a JSON-RPC bridge, not an MCP server, and
  it targets the live desktop only (no `kwin_wayland --virtual` sessions);
* it does **not track upstream**: the two share mechanisms, not a roadmap, so a
  fork would advertise a lineage and a merge path that does not exist;
* MIT requires the copyright and permission notice to travel with the work,
  which `LICENSE` and `NOTICE` do. It does not require a fork.

If the goal is to give back to the upstream project rather than merely credit
it, the useful shape is a contribution to kwin-mcp's existing `integrations/`
directory (which already holds its OpenCode and Claude Code plugins) — not a
fork of the whole project.

## What it is good at

| | |
| --- | --- |
| **Flatpak apps** | A sandboxed window is an ordinary Wayland client to KWin, so listing, activating, moving, capturing, and clicking it need no special permission. Identity is the interesting part: window and accessibility results carry `flatpakAppId`, and `cu_launch` waits for the window whose `app-flatpak-<app-id>-<n>.scope` belongs to the app it started. |
| **Verifiable acting** | Screenshots report the logical origin and size of the frame, so a pixel in the PNG converts to the global coordinate `cu_mouse` takes. Accessibility elements report `unavailable` with a reason rather than a plausible-but-wrong rectangle. |
| **Input that reaches anything** | Injection goes through the compositor (KWin EIS + libei), so it lands on Wayland, XWayland, Wine, and flatpak surfaces alike. |
| **Honest failures** | A uniform single-colour frame, a locked session, a missing accessibility bus, an unauthorized screenshot backend, and a refused window action are all reported as such. |

## Requirements

* KDE Plasma 6 on Wayland, with KWin reachable on the session bus.
* `python3` with **PyGObject** (`python3-gi`, `gir1.2-atspi-2.0`), **dbus-python**
  (`python3-dbus`), and **Pillow** (`python3-pil`).
* **libei** (`libei1` on Debian/Ubuntu) for input injection. Everything except
  input still works without it.
* **Spectacle** for the screenshot fallback (see
  [Screenshot backends](#screenshot-backends)).
* Optional: **flatpak** for the flatpak tools.

```bash
# Debian / Ubuntu / KDE neon
sudo apt install python3-gi gir1.2-atspi-2.0 python3-dbus python3-pil libei1 spectacle flatpak
# Fedora
sudo dnf install python3-gobject python3-dbus python3-pillow libei spectacle flatpak
# Arch
sudo pacman -S python-gobject python-dbus python-pillow libei spectacle flatpak
```

## Install

```bash
# from a git host
dsh plugin --profile web add "github:anthony1x6000/dsh-cu#main"

# from a checkout
dsh plugin --profile web add "link:/home/virt/Projects/dsh-cu"
```

The bundle layer inserts one row, and the plugin resolves its own bridge inside
the installed package. To load it without installing, point a patch at the built
file:

```yaml
- insert:
    - id: dsh-cu
      name: '/path/to/dsh-cu/lib/index.js'
```

## Tools

| Tool | What it does |
| --- | --- |
| `cu_status` | Capability report: KWin and output geometry, input injection, screenshot backends and authorization, accessibility bus, lock state, whether the caller is sandboxed, Klipper, installed and running flatpak apps, plus an actionable hint per degraded part. |
| `cu_windows` | Windows from KWin with exact geometry, ids, pids, and `flatpakAppId`. The only view that also sees apps publishing no accessibility tree. |
| `cu_window` | `info`, `activate`, `close`, `minimize`, `maximize`, `fullscreen`, `keep_above`, `on_all_desktops`, `geometry`. Reports state before/after and `refused` when the window disallows it. |
| `cu_screenshot` | Workspace, one window, or one area → PNG, with the logical origin/size and a coordinate note. Flags a uniform frame and a locked session. |
| `cu_mouse` | `move`, `click`, `double_click`, `triple_click`, `down`, `up`, `drag`, `scroll`, `position` — global logical coordinates. |
| `cu_keyboard` | `type` (US-QWERTY keycodes, or clipboard paste with `unicode: true`), `press`/`down`/`up` chords like `ctrl+alt+t`. |
| `cu_a11y_tree` | AT-SPI tree with rectangles translated to global coordinates, per element `unavailable` reasons. |
| `cu_a11y_find` | Search by name/role/description and required states, with `waitMs` polling. |
| `cu_a11y_invoke` | Run one accessibility action on exactly one match — the reliable way to press a control. Refuses ambiguity. |
| `cu_a11y_windows` | Applications AT-SPI knows, with `pid`, `flatpakAppId`, and the sandbox `proxyPid` when a flatpak app publishes through its proxy. |
| `cu_launch` | Launch a flatpak app (`appId`) or a host program (`command`), then wait for its window and report it. |
| `cu_flatpak` | Installed apps, running instances, and one app's permissions — including whether the sandbox may talk to the accessibility bus. |
| `cu_clipboard` | Read or write the clipboard through Klipper. |

A bundled `plasma-computer-use` skill is registered when the deployment provides
`ctx.skills`, and explains the observe → activate → act → verify loop and the
traps below.

## The loop that works

1. `cu_status` — confirm the capabilities you are about to depend on, and check
   that the session is not locked.
2. `cu_windows` — take the window id.
3. `cu_window { action: "activate" }` — synthetic input lands on the focused
   window, so this is not optional.
4. Observe: `cu_screenshot` and read the PNG back with `read_image`, or
   `cu_a11y_find` when the app publishes a widget tree.
5. Act: `cu_a11y_invoke` for a named control, otherwise `cu_mouse`/`cu_keyboard`.
6. Verify again. A click that was not observed did not happen.

## Coordinates

Everything is **global logical pixels**: `cu_windows` rectangles, `cu_a11y_find`
rectangles, and `cu_mouse` targets. A screenshot is a window onto that space:
its result states `logical.origin` and `logical.size`, a 1:1 capture satisfies
`logical = origin + pixel`, and a downscaled one carries the exact formula in
`coordinateNote`.

Elements whose window could not be matched with certainty come back with
`rect: null` and an `unavailable` reason (`no-kwin-window`, `ambiguous`,
`windows-changed`, `no-extents`, …). Do not click them.

## Flatpak

Input and screenshots need nothing special: KWin composites the window and
compositor-level injection reaches the sandbox.

Identity and accessibility do:

* **Windows.** `cu_windows` resolves each pid to its
  `app-flatpak-<app-id>-<n>.scope` and reports `flatpakAppId`, from three sources
  in order: `/proc/<pid>/cgroup` (exact, and it also resolves the sandbox's
  `xdg-dbus-proxy`), then the user systemd manager's `GetUnitByPID` (systemd
  resolves the number on the host side, so this works from inside a PID
  namespace where `/proc` shows nothing and cgroupfs reports foreign pids as 0),
  then the `flatpak ps` instance table.
* **Accessibility.** A sandboxed app reaches the accessibility bus through its
  sandbox's `xdg-dbus-proxy`, so AT-SPI reports the *proxy's* pid while KWin
  reports the app's. `cu_a11y_windows` shows both (`pid` is the proxy,
  `proxyPid` marks it, `flatpakAppId` is the app), and the tree walk aliases the
  proxy pid to the windows of its own instance scope before matching.
* **Launching.** `cu_launch { appId: "com.github.tchx84.Flatseal" }` runs
  `flatpak run` and waits for the window belonging to that app's scope — exact
  even with several instances of the same app running.
* **Diagnosis.** `cu_flatpak { appId }` reports the app's declared permissions
  (`a11yBusDeclared`) and, authoritatively, whether it actually appears on the
  accessibility bus (`appearsOnA11yBus`). Flatpak grants that bus by default, so
  a missing declaration proves nothing — Brave declares nothing and still
  publishes a tree. Toolkit accessibility must also be on for the session; the
  accessibility tools turn it on (`org.a11y.Status.IsEnabled`), but an app that
  started while it was off may need a restart before it publishes one.

## Roblox Studio via Vinegar

Roblox Studio runs under Wine inside the Vinegar flatpak, and this plugin was
verified against exactly that setup:

* `cu_windows` reports Studio's window with `flatpakAppId: org.vinegarhq.Vinegar`
  and its real frame; the window's pid resolves through the instance's
  `app-flatpak-org.vinegarhq.Vinegar-<n>.scope`. Note that `flatpak ps` reports
  the launcher process, which may still sit in the shell's scope — the sandboxed
  processes are the ones under the app scope, and that is what the identity
  lookup uses.
* `cu_screenshot { target: "window", app: "robloxstudiobeta" }` activates the
  window and captures it at exactly the frame size with a proven 1:1 mapping, so
  screenshot pixels are click coordinates. This works **even while the session is
  locked** and the lock screen covers the display, because a window capture
  renders the window itself.
* **Wine publishes no accessibility tree**, so `cu_a11y_*` finds nothing for
  Studio. `cu_flatpak { appId: "org.vinegarhq.Vinegar" }` says so explicitly
  (`publishesTree: false`, `a11yDeclared: false`). Drive Studio with
  `cu_screenshot` + `cu_mouse`, and use the Roblox Studio MCP server from the
  `dsh-rbx` bundle for game-tree work — that is what it is for.
* Input injection is compositor-level, so it reaches Wine surfaces like any other
  client: activate the Studio window, then click at coordinates taken from its
  capture.
* `cu_launch { appId: "org.vinegarhq.Vinegar" }` starts it and waits for the
  window of that scope. A second call reports `alreadyRunning: true` and returns
  the existing window, because `flatpak run` on a running app brings that
  instance forward instead of opening a new one.

## Flatpak round trip (verified)

```
cu_launch { appId: "com.github.tchx84.Flatseal" }   → window matched by scope, alreadyRunning false
cu_screenshot { target: "window", windowId }        → 1024x768 at logical (448, 129), mapping proven
cu_window { action: "close" }                       → closed
cu_launch { appId: "com.github.tchx84.Flatseal" }   → alreadyRunning true, same window returned
```

## Input

Pointer and keyboard events are injected through KWin's private
`org.kde.KWin.EIS.RemoteDesktop` endpoint using libei — no portal prompt, no
`ydotool`, no `uinput`, no extra privileges beyond the session bus.

* Text is typed as **US-QWERTY evdev keycodes**, which is the session keymap.
  `unicode: true` switches to clipboard paste (Klipper writes the text, the paste
  chord is pressed, and the previous clipboard is restored). Text that cannot be
  typed is refused *before* any input is sent.
* The paste chord is chosen per target: `ctrl+shift+v` for the terminals that
  bind it, `ctrl+v` elsewhere, and **no chord at all** for a terminal whose
  binding is unknown — a wrong chord sends a literal `^V` to the shell, which no
  later key can undo.
* The pointer's first motion after the handshake is repeated, because it can be
  coalesced with the device-resume frame; an absolute motion is idempotent.

## Screenshot backends

| Backend | Notes |
| --- | --- |
| **KWin ScreenShot2** | Fast, per-window and per-area, no consent prompt. Restricted: KWin authorizes a caller only when its executable is named by the `Exec` of a `.desktop` file declaring `X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2`. |
| **Spectacle** | A KDE application, so already authorized. Captures the whole desktop or the active window (no shadow, so the image matches the window rectangle and pixels map to coordinates); an area is cropped from the full-desktop frame. It cannot capture a window that is not on top, and it needs a rendering session. |
| **Blank-frame report** | A capture that is a single uniform colour is flagged, because it means the display is off, the compositor is not rendering, or the session is locked — not that the desktop is black. |

To authorize ScreenShot2 for the bridge's interpreter:

```bash
tools/authorize-screenshot.sh            # install, refresh the service cache, verify
tools/authorize-screenshot.sh --verify   # check only
tools/authorize-screenshot.sh --remove   # take the grant away
```

The script prints the truth when it fails: KWin resolves the caller through the
KSycoca service database, and a long-running KWin has been observed to keep
using its cached copy until it restarts. Log out and back in, or
`systemctl --user restart plasma-kwin_wayland.service` (which closes every app's
windows). Until then, the Spectacle fallback keeps working.

The grant authorizes **every** process running that interpreter, because KWin
matches on the executable path. Point `DSH_CU_PYTHON` at a dedicated interpreter
(for example `python -m venv --copies`) when that matters.

## Configuration

```yaml
- id: dsh-cu
  config:
    python: python3                       # interpreter for the bridge
    bridgePath: /opt/dsh-cu/bin/dsh-cu-bridge
    scriptDir: /home/me/.cache/dsh-cu/kwin-scripts   # must be readable by KWin
    stateDir: /home/me/.local/state/dsh-cu
    screenshotDir: /home/me/Pictures/dsh-cu
    requestTimeoutMs: 60000
    launchTimeoutMs: 20000
    toolPrefix: cu_
    enableInput: true                     # false = observation-only surface
    enableSkill: true
    env: {}
```

Environment equivalents: `DSH_CU_PYTHON`, `DSH_CU_SCRIPT_DIR`, `DSH_CU_STATE_DIR`,
`DSH_CU_SCREENSHOT_DIR`.

`enableInput: false` registers only the observation tools (`status`, `windows`,
`window action=info`, `screenshot`, `a11y_*`, `flatpak`) and drops the six
mutating ones (`window`, `mouse`, `keyboard`, `a11y_invoke`, `launch`,
`clipboard`).

## Pitfalls

* **Locked session.** `cu_status` reports it, and screenshots carry
  `sessionLocked`. Input lands on the lock screen and screenshots show it; unlock
  before driving applications.
* **Display off.** An idle session can blank the output, and captures come back a
  uniform colour. Synthetic input usually wakes it.
* **Sandboxed callers.** The plugin belongs in the DSH server process, which is
  not sandboxed. If the bridge is instead driven from a sandboxed shell (the DSH
  bash tool bind-mounts the filesystem read-only), three things break and are
  handled automatically:
  * *Spectacle refuses to capture* — it cannot write `spectaclerc`, so it shows a
    modal "Configuration file … not writable" dialog and exits 0 **without an
    image**. The bridge now gives Spectacle private writable XDG directories, so
    capture works and the dialog never appears.
  * *KWin cannot read a script written to a private `/tmp`.* The bridge falls back
    to a `.dsh-cu-scripts` directory under the current directory (the shared
    workspace); set `scriptDir` when the working directory is not shared either.
  * *The default screenshot directory may be unwritable.* An explicit `saveTo`
    always wins; the default falls back to the temporary directory.
  `cu_status` reports `sandboxed` and the writability of each directory.
* **Chromium trees.** A minimized or unrendered Chromium window may expose only
  its top-level frame; activate it and re-query.
* **Wine, Flutter, some Electron apps** publish no usable accessibility tree.
  Use `cu_screenshot` plus coordinates there.
* **`devicePixelRatio`.** Everything is logical; a fractional-scale output does
  not change the coordinate space, but captured pixels are compared against
  KWin's logical size, and a mismatch is reported rather than silently accepted.

## Testing

```bash
pnpm selfcheck          # tool/registry contract, schema validity, bridge protocol, packaging, licence
pnpm test:python        # unit tests for identity, keys, matching, mapping, framing
pnpm test:integration   # live checks against the running desktop
python3 tests/validate_schemas.py            # meta-schema check of every tool schema
python3 tests/validate_schemas.py --session <log.jsonl>   # every tool in a recorded request
```

`validate_schemas.py` exists because a malformed tool schema is not a local
problem: schemas travel with **every** model request, so one bad schema makes
every turn fail with a provider-side 400 instead of degrading one tool. That
happened here — `required: true` written inside a property (the `defineTool`
authoring form) instead of a root `required` array, which the provider answers
with `True is not of type 'array'`. Two gates now stand in the way:

* `apply()` compiles every parameter spec with `compileParameters()` and
  validates the result against the harness's own `assertSupportedJsonSchema`
  (resolved at runtime, like the MCP client) plus built-in structural checks. A
  spec that fails is **skipped with a loud message** rather than registered, so
  one bad tool can never take down every request.
* `selfcheck` runs the same gates, and `validate_schemas.py` checks the shipped
  schemas against the JSON Schema 2020-12 meta-schema — the validator class the
  provider uses — optionally over a whole recorded request.

Worth knowing if you write another plugin: the harness does **not** validate
plugin-supplied parameter schemas when they are registered, so nothing catches
this for you at load time.

### Updating an installed plugin needs a service restart

A plugin installed under the profile's `node_modules` is **not** watched for
changes, and the harness re-applies the profile patch without invalidating the
ESM module cache. Reinstalling a new build therefore re-registers the *old* code:
the startup line keeps describing the previous generation, and the tool schemas
sent to the provider stay stale — which is fatal, because a stale schema can make
every turn fail with a provider-side 400 while the files on disk look correct.

The startup line carries a fingerprint of the exact schemas the process is
serving, so this is checkable instead of guessable:

```
dsh-cu: registered 14 computer-use entries (prefix "cu_", …, schemas ee6c0b0a)
```

Compare `schemas <hash>` against the build's own value
(`node -e "import('./lib/index.js').then(m => console.log(m.schemaFingerprint()))"`).
If it differs, or the field is missing entirely, the process is serving an older
generation and needs a restart:

```bash
systemctl --user restart dsh-web.service   # then reload the GUI page
```

A `remove` + `add` cycle is not enough on its own; it triggers a reload, but of
the cached module.

`selfcheck` runs a fake bridge that speaks the same frames, so it needs no
desktop. `test:integration` drives the real session and is conservative: it
restores the pointer to where it found it, skips keyboard input while the session
is locked, and closes only the flatpak window it launched itself. It also runs the
bridge inside a read-only `bwrap` sandbox to prove that capture, window control,
and flatpak identity work there with no environment hints. On the machine this was
built on: 45 unit tests, and 17 integration checks passing.

## Architecture

```
index.ts                     tool surface: schemas, descriptions, registration, bridge client
lib/index.js                 committed build artifact an installed profile loads
bin/dsh-cu-bridge            launcher: PYTHONPATH + `python -m dsh_cu_bridge`
python/dsh_cu_bridge/
  __main__.py                ops and the newline-delimited JSON-RPC serve loop
  rpc.py                     frame envelope and routable error codes
  kwin.py                    KWin scripting: windows, actions, outputs, cursor
  eis.py                     KWin EIS + libei: pointer, buttons, scroll, keyboard
  keys.py                    evdev keycodes, US-QWERTY text plan, chords, paste policy
  shot.py                    ScreenShot2 + Spectacle, mapping, blank-frame detection
  atspi.py                   accessibility tree, matching, coordinate translation
  flatpak.py                 scope identity, instances, permissions, launch
  clipboard.py               Klipper read/write
  session.py                 lock state
  status.py                  capability probe
tools/authorize-screenshot.sh
tests/test_pure.py           unit tests (identity, keys, matching, mapping, sandbox fallbacks)
tests/integration.py         live desktop checks, including a read-only-sandbox run
tests/validate_schemas.py    meta-schema validation of the tool schemas
```

The bridge is spawned once (`--serve`) on first tool use and kept resident: the
EIS handshake and PyGObject's per-process D-Bus binding both want a long-lived
process. `bin/dsh-cu-bridge` is also usable directly
(`bin/dsh-cu-bridge --detect`, `--ops`, or `window.list '{"app":"brave"}'`).
