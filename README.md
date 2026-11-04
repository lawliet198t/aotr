# Game input recorder and player

Screenshot matching remains the trigger system. Keyboard and mouse event
recording/replay use the established Linux `libinput record` and `libinput
replay` tools. New recordings use native libinput YAML files; existing JSON
macros remain readable by the legacy player.

## Install the system tool

Install this yourself on Ubuntu/Debian:

```bash
sudo apt update
sudo apt install libinput-tools
```

The app requests sudo credentials at startup for input-device capture and
virtual-device replay. No Python `evdev` package or `/dev/uinput` group setup is
needed for this backend.

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
Native libinput files replay once at their recorded timing; `--speed` applies
only to legacy JSON macros.

While a macro plays, the player watches the physical keyboard and mouse. Your
first input stops playback and starts takeover recording; recording continues
until the phase's Skip or Retry image appears. The old macro prefix is kept,
the unused remainder is replaced with your live input, and a `.before-takeover.yml`
backup is retained. The old JSON recordings are not removed by this change.

## Dependencies

The existing `.venv` should contain the Python packages used for screenshot
matching and the legacy JSON path (`opencv-python`, `mss`, `numpy`, `pynput`).
The native input backend itself uses the system `libinput-tools` package.
