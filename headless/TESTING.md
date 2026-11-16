# Headless Sober test

This runs Sober in a private headless Sway session and provides direct macro
replay plus image-triggered two-phase automation. Automation captures the
private display through its localhost WayVNC connection, and sends both macro
events and absolute UI clicks through XTest on the private XWayland display.
UI clicks temporarily escape Sway's game pointer capture. It does not read the
host desktop or modify saved YAML macros.

## Install manually

On Ubuntu/Mint, install the session and inspection tools yourself:

```bash
sudo apt update
sudo apt install sway wayvnc grim wtype wlrctl tigervnc-viewer \
  build-essential libx11-dev libxtst-dev
```

Sober should already be installed as `org.vinegarhq.Sober`. Its Flatpak
manifest grants DRI and network access. Do not disable network access.

Build the private input service before starting the session:

```bash
make -C headless
```

## Start and inspect

From the repository root:

```bash
./headless/aotr-headless start
./headless/aotr-headless status
./headless/aotr-headless screenshot
```

The script creates a private runtime directory under the current user's
`XDG_RUNTIME_DIR`, selects an accessible Intel render node (`0x8086`) when
available, and starts Sway with the headless backend and no libinput devices.
The output is explicitly configured for 1920×1080 at 120 Hz so the compositor
has a 120 Hz frame schedule.
It passes `--unsupported-gpu` because Sway 1.9 otherwise exits when the
proprietary NVIDIA module is loaded. This flag only bypasses that refusal; it
does not guarantee compatibility.

Review the renderer and startup messages:

```bash
grep -Ei 'GL renderer|renderer|EGL|headless|wayland display|error' \
  "$XDG_RUNTIME_DIR/aotr-headless/sway.log"
```

The output in `status` must report `1920x1080 @ 120.000 Hz`. This confirms the
headless output mode, not the game's rendered FPS. Use Roblox's in-game debug
stats (try `Shift+F5` in the VNC viewer) to check the actual client FPS. Roblox
documents this shortcut for its client. If Sober does not respond to it, check
the game's settings for a performance-stats option if available.

To change the already-running session without restarting Sober, run:

```bash
./headless/aotr-headless 120hz
./headless/aotr-headless status
```

The log must show a hardware renderer for the compositor before judging the
GPU test successful. If the script cannot find an accessible Intel node, first
identify the correct node and then pass it explicitly, for example:

```bash
AOTR_RENDER_NODE=/dev/dri/renderD129 ./headless/aotr-headless start
```

That example node is only a placeholder; do not copy it without checking your
machine. The script does not use sudo to change device permissions.

## View and launch Sober

```bash
./headless/aotr-headless game
./headless/aotr-headless viewer
```

`game` launches Sober through a private XWayland server by default. Your normal
Sober log reports `X-Cinnamon (on x11)`, while headless Sway provides Wayland;
the installed Flatpak permission is `fallback-x11`, which makes X11 unavailable
inside a Wayland session. That backend switch can change Sober/Roblox mouse
capture and sensitivity. Matching X11 here is intended to make the existing
macro behave like it does on the normal desktop; no rerecording is needed.

To compare against the previous native-Wayland path, run:

```bash
./headless/aotr-headless game wayland
```

Both launch modes are per-run Flatpak permissions and do not change the saved
global Flatpak overrides. The script requires the host's `Xwayland` binary for
the default X11 mode.

The viewer connects to `127.0.0.1:5900` in view-only mode by default. To
explicitly enable control from the viewer, run:

```bash
./headless/aotr-headless viewer --control
```

The view-only mode prevents that VNC window from sending keyboard and mouse
events to the headless game; it does not disable your host's local input. The
interactive mode is opt-in. Closing the viewer disconnects it but leaves Sway
and Sober running. Confirm Sober is rendering in view-only mode; use
`--control` only when you intentionally need to send input through the viewer.
Do not start the test if Sober is already running on the normal desktop; the
script checks for this before launching.

WayVNC is started with a 120 FPS capture limit. Its default limit is 30 FPS, so
the viewer can look capped even while Roblox itself renders at 120. This limit
affects only the remote picture; the macro input service sends directly to
Sway and does not pass through VNC. WayVNC performance counters are written to
`$XDG_RUNTIME_DIR/aotr-headless/wayvnc.log` for diagnostics. To apply the new
limit to an already-running session without stopping Sway or Sober, run:

```bash
./headless/aotr-headless vnc-restart
```

This disconnects the current viewer briefly; reconnect with `viewer` afterward.

The host's physical input devices are not attached to the headless Sway
backend. Macro replay uses XTest on the isolated XWayland display. The VNC
viewer's input is virtual input delivered to the headless Sway session.
`pointer-test` sends a five-pixel relative motion and may move the camera if the
game is focused:

```bash
./headless/aotr-headless pointer-test
```

For the first pass, use the viewer to check keyboard, clicking, and camera
response. This tests manual VNC input only. The `replay` command below injects
keyboard and relative mouse events into private XWayland; it does not use the
host's `/dev/uinput` or change the existing recorder/player.
For a single absolute-coordinate click, use the XTest helper. It temporarily
escapes game pointer capture, sends the click through the same private XWayland
input path as macros, and restores pointer capture afterward:

```bash
./headless/aotr-headless click 1764 952
```

## Replay a saved macro in the private session

Build the small XTest replay helper once:

```bash
make -C headless
```

Validate the recording before sending input. This reads the YAML file and
refuses recordings containing Escape presses:

```bash
./headless/aotr-headless replay data/recordings/1.yml --dry-run
./headless/aotr-headless replay data/recordings/2.yml --dry-run
```

With Sober running and focused in the private session, replay one macro:

```bash
./headless/aotr-headless replay data/recordings/1.yml
```

The session starts one persistent XTest service connected to the private
XWayland display. Replay starts at the first recorded event and preserves the
gaps using absolute monotonic deadlines.
`--speed 1.0` is the normal rate. Press Ctrl+C to stop; the service releases
held keys and buttons without removing any host input devices. The source
recording is never written. Direct macro replay does not run image-trigger detection or the
automatic two-phase loop, so start it only when the game is at the matching
point. Phase 2 remains a separate explicit command in direct replay mode.

## Run image-triggered farming automation

Start Sober in the private session, leave it at the game's starting/result
screen, and run:

```bash
./headless/aotr-headless automate
```

The detector requests 60 FPS RGB565 frames, leaving Sober and Sway with more
CPU/bandwidth headroom while keeping detection latency below about 17 ms. The
headless output itself remains at 120 Hz. It uses
`data/start/1` at threshold `0.80` and `data/start/2` at
`0.70`, then plays `data/recordings/1.yml` and `2.yml`. Phase 2 is locked.
Skip and Retry are detected from the private display and clicked every 100 ms.
Retry resets the cycle to Phase 1. If Skip advances the game but the next
phase trigger does not appear, that phase macro starts after one second. During
the post-Phase-1 wait, if neither Skip nor a Phase 2 trigger appears for five
seconds, it taps `2` every 500 ms. Heal handling runs before Retry handling,
performs the configured coordinate sequence, then repeats the final Heal click
every 200 ms until Retry appears. Each loop summary reports the detected
mission result.

Press Ctrl+C to stop automation; the input service releases held keys/buttons
and remains available. Paths, thresholds, and speed can be overridden with
`./headless/aotr-headless automate --help`. Use the default view-only viewer to
inspect the game without sending host input.

## Check background behavior

With Sober running, close the VNC viewer for a few minutes, then reconnect:

```bash
./headless/aotr-headless viewer
```

Check that the game kept running, remained responsive, and did not slow down
enough to break the farming loop. The headless compositor's renderer log proves
only which GPU Sway uses; Sober can select a different GPU. A successful Sway
startup alone does not prove Sober's GPU choice or background frame rate.

## Stop

```bash
./headless/aotr-headless stop
```

The logs remain under `$XDG_RUNTIME_DIR/aotr-headless/` for review. The
localhost-only WayVNC server has no password configured, so keep it bound to
localhost as the script does.
