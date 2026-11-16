#!/usr/bin/env python3
"""Replay a libinput-record YAML file through XTest into private XWayland."""

from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME_RE = re.compile(r"^  - evdev:\s*$")
FIELDS_RE = re.compile(r"^\s+- \[\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(-?\d+)")
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
KEY_ESC = 1
BTN_MOUSE = 0x110
REL_X, REL_Y = 0, 1
REL_WHEEL, REL_HWHEEL = 8, 6
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 11, 12
MAX_EVENTS_PER_FRAME = 64


def read_macro(path: Path, speed: float) -> tuple[bytes, int, int, float]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ValueError(f"Cannot read recording {path}: {exc}") from exc

    devices = []
    for index, line in enumerate(lines):
        if not line.startswith("- node:"):
            continue
        end = next((j for j in range(index + 1, len(lines)) if lines[j].startswith("- node:")), len(lines))
        block = lines[index:end]
        name = next((m.group(1) for row in block if (m := re.match(r"\s*# Name:\s*(.*?)\s*$", row))), "")
        frames = []
        frame_lines: list[str] | None = None
        for row in block:
            if FRAME_RE.match(row):
                if frame_lines is not None:
                    frames.append(frame_lines)
                frame_lines = []
            elif frame_lines is not None:
                frame_lines.append(row)
        if frame_lines is not None:
            frames.append(frame_lines)
        devices.append((name, frames))

    if not devices:
        raise ValueError("No libinput evdev device sections found in the recording.")

    # Keep the recording's event times, but remove coarse wheel entries when
    # high-resolution wheel events are present in the same frame.
    output: list[tuple[int, int, list[tuple[int, int, int]]]] = []
    escape_presses = 0
    for _name, frames in devices:
        for frame in frames:
            rows = [FIELDS_RE.match(row) for row in frame]
            raw = [m for m in rows if m]
            if not raw:
                continue
            first = raw[0]
            timestamp = int(first.group(1)) * 1_000_000 + int(first.group(2))
            keyboard_events: list[tuple[int, int, int]] = []
            pointer_events: list[tuple[int, int, int]] = []
            has_hi_res_vertical = any(int(m.group(3)) == EV_REL and int(m.group(4)) == REL_WHEEL_HI_RES for m in raw)
            has_hi_res_horizontal = any(int(m.group(3)) == EV_REL and int(m.group(4)) == REL_HWHEEL_HI_RES for m in raw)
            for match in raw:
                sec, usec, event_type, code, value = map(int, match.groups())
                if event_type == EV_KEY and code == KEY_ESC and value == 1:
                    escape_presses += 1
                if event_type == EV_KEY:
                    event = (event_type, code, value)
                    (pointer_events if code >= BTN_MOUSE else keyboard_events).append(event)
                elif event_type == EV_REL:
                    if code == REL_WHEEL and has_hi_res_vertical:
                        continue
                    if code == REL_HWHEEL and has_hi_res_horizontal:
                        continue
                    pointer_events.append((event_type, code, value))
            for kind, events in ((1, keyboard_events), (2, pointer_events)):
                if events:
                    if len(events) > MAX_EVENTS_PER_FRAME:
                        raise ValueError(f"A recording frame contains {len(events)} events; supported maximum is {MAX_EVENTS_PER_FRAME}.")
                    output.append((timestamp, kind, events))

    if not output:
        raise ValueError("The recording contains no keyboard or mouse events to replay.")
    if escape_presses:
        raise ValueError(
            f"Refusing to replay: this recording contains {escape_presses} Escape press(es), which could close the game. "
            "Remove Escape from the recording first."
        )

    output.sort(key=lambda item: (item[0], item[1]))
    origin = output[0][0]
    frames = [(round((stamp - origin) / speed), kind, events) for stamp, kind, events in output]
    if any(len(events) > 0xFFFF for _, _, events in frames):
        raise ValueError("A replay frame has too many events.")

    payload = bytearray(struct.pack("<4sII", b"AOTR", 1, len(frames)))
    for at_usec, kind, events in frames:
        payload.extend(struct.pack("<QBBH", at_usec, kind, 0, len(events)))
        for event_type, code, value in events:
            payload.extend(struct.pack("<iii", event_type, code, value))
    duration = max(at for at, _, _ in frames) / 1_000_000
    key_events = sum(len(events) for _, kind, events in frames if kind == 1)
    pointer_events = sum(len(events) for _, kind, events in frames if kind == 2)
    return bytes(payload), len(frames), key_events + pointer_events, duration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", nargs="?", type=Path, default=ROOT / "data/recordings/1.yml")
    parser.add_argument("--speed", type=float, default=1.0, help="Playback multiplier (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate only; send no input")
    args = parser.parse_args()
    if not 0.05 <= args.speed <= 10:
        parser.error("--speed must be between 0.05 and 10")
    try:
        payload, frame_count, event_count, duration = read_macro(args.recording, args.speed)
    except (ValueError, UnicodeError) as exc:
        print(f"[headless-replay] ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"[headless-replay] {args.recording}: {frame_count} frames, {event_count} events, {duration:.2f}s at {args.speed:.2f}x")
    if args.dry_run:
        print("[headless-replay] Validation complete; no input sent.")
        return 0

    socket_path = os.environ.get("AOTR_INPUT_SOCKET")
    if not socket_path:
        print("[headless-replay] Headless input service is not running. Run: ./headless/aotr-headless start", file=sys.stderr)
        return 2
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(payload)
            with client.makefile("rb") as response:
                state = response.readline().decode("utf-8", errors="replace").strip()
                if state != "READY":
                    print(f"[headless-replay] Service rejected the macro: {state or 'no response'}", file=sys.stderr)
                    return 2
                print("[headless-replay] Replay started; input device remains connected after completion.")
                result = response.readline().decode("utf-8", errors="replace").strip()
                if result == "DONE":
                    return 0
                print(f"[headless-replay] Replay ended: {result or 'service disconnected'}", file=sys.stderr)
                return 130 if result == "CANCELLED" else 1
    except KeyboardInterrupt:
        print("\n[headless-replay] Interrupted; the persistent service will release held inputs.", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"[headless-replay] Could not contact the private input service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
