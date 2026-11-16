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

## Standalone C player

`play.c` plus the `play-c` Makefile target provide a standalone C two-phase
player for the existing `1.yml` and `2.yml` libinput recordings. It does not
start Python. It reads the existing phase trigger folders and Skip, Retry,
Heal, Completed, and Failed images. Phase 2 stays fixed at threshold `0.70`.
The uinput keyboard does not advertise `EV_REP`: it replays repeat events from
the recording without adding kernel-generated repeats for held keys.

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
