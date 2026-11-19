# Game input recorder and player

Screenshot matching remains the trigger system. New recordings use the
established Linux `libinput record` format. Native YAML playback uses AOTR's
uinput scheduler so every recorded kernel event, including key-repeat frames,
is replayed at its original timestamp. Existing JSON macros remain readable by
the legacy player.

## Install the system tool

Install this yourself on Ubuntu/Debian:

```bash
sudo apt update
sudo apt install libinput-tools
```

The app requests sudo credentials at startup for input-device capture and
virtual-device playback. Native YAML playback also needs `python-evdev` in the
project virtual environment (`.venv/bin/pip install evdev`); it creates the
virtual devices through `/dev/uinput` as root.

## Record

Put phase trigger screenshots in `data/start/1/` and `data/start/2/`. Then run:

```bash
.venv/bin/python3 record.py --phase 1
.venv/bin/python3 record.py --phase 2
```

At startup, select the physical mouse and keyboard event nodes together. The
recorder starts when a phase trigger matches and stops on Skip for phase 1 or
Retry for phase 2. It writes `data/recordings/1.yml` and `2.yml` plus metadata
sidecars. The recording HUD is used by `record.py` only.

You can explicitly choose event nodes, repeating the option for each one:

```bash
.venv/bin/python3 record.py --phase 1 --event-device /dev/input/eventN --event-device /dev/input/eventM
```

## Play

```bash
.venv/bin/python3 play.py 1 2
```

Or run `play.py` without arguments to auto-select the standard `1.yml` and
`2.yml` pair. The existing screenshot triggers, automatic Skip/Retry clicks,
phase thresholds, and loop reset behavior remain in the Python coordinator.
Native YAML recordings replay once at their recorded timing; `--speed` applies
only to legacy JSON macros. YAML playback writes all recorded EV_KEY values
directly through virtual input devices, including value-2 key-repeat events
that `libinput replay` intentionally omits.

While a macro plays, the player watches the physical keyboard and mouse. Your
first input stops playback and starts takeover recording; recording continues
until the phase's Skip or Retry image appears. The old macro prefix is kept,
the unused remainder is replaced with your live input, and a `.before-takeover.yml`
backup is retained. The old JSON recordings are not removed by this change.

## Dependencies

The existing `.venv` should contain the Python packages used for screenshot
matching and the legacy JSON path (`opencv-python`, `mss`, `numpy`, `pynput`).
The native input capture uses system `libinput-tools`; native replay uses the
`evdev` Python package. The existing `.venv` already has `evdev` installed.

## Windows desktop mode

The Python desktop tools detect Windows automatically. Windows uses MSS for
screen capture, pynput for keyboard/mouse input, and portable JSON recordings;
Linux keeps the existing libinput/uinput native YAML path. Native `.yml`
recordings cannot be replayed on Windows, so record the phases on Windows or
use JSON recordings copied into `data/recordings/`.

In PowerShell, from the project folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install opencv-python mss numpy pynput
.\.venv\Scripts\python.exe record.py --phase 1
.\.venv\Scripts\python.exe record.py --phase 2
.\.venv\Scripts\python.exe play.py 1 2
```

Quick-time prompts use `data/a.png`, `data/s.png`, `data/d.png`, and
`data/w.png`:

```powershell
.\.venv\Scripts\python.exe qte.py
```

`capture_tool.py` and `mouse_coords.py` use the portable Python backends too.
Windows macro movement is based on desktop pointer deltas; games that require
raw, device-level relative input may behave differently from Linux native
playback. Use `recording_editor.py` to edit JSON macros on Windows; its Play
button is a visual keyboard-timeline preview and does not send game input.
`--allow-takeover`, the standalone `play-c` binary, the GTK C editor, and the
headless Sway tools remain Linux-only. Windows execution has not been
validated on a Windows machine.

## Headless Sober automation

The optional headless setup runs Sober inside a private Sway session, with a
1920×1080 virtual display and separate input. Automation reads screenshots
through localhost WayVNC, detects the configured phase and UI images, and sends
macro input and absolute clicks through private XWayland. See
[`headless/TESTING.md`](headless/TESTING.md) for system requirements and setup.

Build the input helper, then start the session and game:

```bash
make -C headless
./headless/aotr-headless start
./headless/aotr-headless game
```

In separate terminals, open the viewer and start automation:

```bash
./headless/aotr-headless viewer
./headless/aotr-headless automate
```

The viewer is view-only by default. Use `viewer --control` only when you want
its keyboard and mouse input sent to the game. Automation uses the existing
Phase 1 and Phase 2 recordings and trigger images under `data/`; Phase 2 stays
locked. Skip, Retry, and Heal handling and loop summaries are enabled. Retry
clicks stay at fixed coordinates; when Retry remains visible for five seconds,
automation runs the Heal sequence rather than moving the Retry target. The final
Heal click repeats every 200 ms until Retry clears. For a
single click or macro replay, use `./headless/aotr-headless click X Y` or
`./headless/aotr-headless replay data/recordings/1.yml`. Stop the private
session with `./headless/aotr-headless stop`. If the Phase 1 macro finishes
naturally without Skip, automation waits five seconds for a Phase 2 trigger,
then taps `2` every 500 ms. Five seconds after its first tap, the mouse moves
down continuously and left-clicks at the current pointer position every 100 ms
while waiting for Phase 2. Both fallbacks stop immediately when Skip or Retry
is detected, or when Phase 2 starts. A Phase 1 macro stopped by a detected Skip
goes directly to the normal Phase 2 transition without starting these fallbacks.
Automation also records the same private WayVNC framebuffer used for detection
at 1280×720 and 30 FPS, using a low-priority, single-thread ffmpeg encoder and
a bounded frame queue. Phase 1 recording starts 70 seconds into its macro;
capture stops at Skip or Retry. It does not record Phase 2. A temporary clip is
kept only until the loop result is known. Only clips from `FAILED` loops are
saved, each with a unique name under
`data/recordings/failed-loops/`; completed or unclassified loops are discarded.
Install `ffmpeg` if it is not already available.

## Standalone C player

`play.c` plus the `play-c` Makefile target provide a standalone C two-phase
player for the existing `1.yml` and `2.yml` libinput recordings. It does not
start Python. It reads the existing phase trigger folders and Skip, Retry,
Heal, Completed, and Failed images. Phase 2 stays fixed at threshold `0.70`.
The uinput keyboard does not advertise `EV_REP`: it replays repeat events from
the recording without adding kernel-generated repeats for held keys.
Retry clicks use fixed coordinates. If Retry remains visible for five seconds,
the C player runs the Heal sequence, then repeats its final click every 200 ms
until Retry clears. A normal Heal detection repeats the final click until Retry
is detected.

Build and run from the repository root:

```bash
make play-c
./play-c 1 2
```

The build needs X11, Xext, Xtst, Xfixes, libpng, and libjpeg development
packages. Runtime needs an X11 session and access to `/dev/uinput`.
`--one-shot` exits after a Retry cycle; `--threshold 0.80` changes the Phase 1
trigger threshold.

This C player is an independent playback path. Recording and optional Phase 1
takeover editing remain in the Python program; C playback reads the shared
YAML recordings without changing them.

For Phase 1, if a macro ends before Skip appears, the first captured post-macro
input is aligned to the macro boundary. This drops the idle delay spent waiting
for the screen transition while preserving the timing between captured inputs.
The intentional five-second delay before the automatic `2` fallback remains
in the legacy JSON timeline.

## Recording timeline editor

Build the native GTK editor with `make recording-editor`, then open the
recording (defaults to `data/recordings/1.yml`):

```bash
./recording-editor data/recordings/1.yml
```

The editor lists each keyboard event with its timestamp, key, and press/release
or repeat action. **Play** previews the sequence visually and highlights the
current event without sending input to the game. Select a time range by dragging
on the keyboard timeline or enter exact start/end seconds, then choose **Cut
selection**. It removes frames inside `[start, end)` and shifts all later event
timestamps earlier by the cut length across every input device. **Undo cut** and
**Reset to saved** work before saving. **Save** writes the edited YAML atomically
and creates `*.before-edit.yml` the first time; other event data and recording
headers are preserved. For relative mouse motion, it sums the removed `REL_X` /
`REL_Y` deltas and adds that net displacement back to later mouse frames using
a smoothstep blend over 0.5 seconds. This restores the recorded endpoint without
a sudden mouse jump; the editor reports the correction vector after each cut.
If there are no later mouse-motion frames to carry the correction, the editor
reports that limitation in its status line. Relative motion preserves the net
endpoint; it cannot reconstruct the exact intermediate camera path removed by
the cut.
