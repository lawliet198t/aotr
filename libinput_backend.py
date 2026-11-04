"""Thin adapter around the established libinput record/replay tools.

Screen triggers remain in record.py/play.py. libinput handles the actual
kernel-level input stream so relative mouse motion and keyboard timing are not
reimplemented here.
"""

from __future__ import annotations

import signal
import shutil
import subprocess
import threading
import time
import re
from pathlib import Path
from typing import Iterable, Optional


class LibinputUnavailableError(RuntimeError):
    pass


_sudo_keepalive_started = False
_sudo_keepalive_lock = threading.Lock()


def require_libinput() -> str:
    executable = shutil.which("libinput")
    if not executable:
        raise LibinputUnavailableError(
            "libinput was not found. Install the system package `libinput-tools`, "
            "then run this program again."
        )
    return executable


def sudo_warmup() -> None:
    """Ask for sudo credentials once before recording/replay needs them."""
    global _sudo_keepalive_started
    require_libinput()
    try:
        subprocess.run(["sudo", "-v"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LibinputUnavailableError(
            "libinput record/replay needs permission to read input devices and "
            "create a virtual input device; `sudo -v` did not succeed."
        ) from exc
    with _sudo_keepalive_lock:
        if not _sudo_keepalive_started:
            def keep_credentials_fresh() -> None:
                while True:
                    time.sleep(60)
                    try:
                        subprocess.run(
                            ["sudo", "-n", "-v"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass

            threading.Thread(target=keep_credentials_fresh, daemon=True,
                             name="SudoCredentialKeepalive").start()
            _sudo_keepalive_started = True


def discover_devices() -> list[tuple[str, str, str]]:
    """List kernel event nodes using readable sysfs names (no evdev access needed)."""
    candidates: list[tuple[str, str, str]] = []
    for node in sorted(Path("/dev/input").glob("event*")):
        sysfs_name = Path("/sys/class/input") / node.name / "device/name"
        try:
            name = sysfs_name.read_text(encoding="utf-8").strip()
        except OSError:
            name = node.name
        low = name.lower()
        if any(skip in low for skip in ("aotr virtual", "libinput replay")):
            continue
        pointer = any(word in low for word in ("mouse", "touchpad", "trackpoint", "pointer", "receiver"))
        keyboard = "keyboard" in low or "receiver" in low
        if pointer or keyboard:
            role = "mouse+keyboard" if pointer and keyboard else "mouse" if pointer else "keyboard"
            candidates.append((str(node), name, role))
    return candidates


def choose_devices(requested: Optional[Iterable[str]] = None) -> list[str]:
    """Use explicit nodes or choose a keyboard and best physical mouse automatically."""
    if requested:
        paths = list(dict.fromkeys(str(Path(p)) for p in requested))
        if not all(Path(p).exists() for p in paths):
            missing = [p for p in paths if not Path(p).exists()]
            raise LibinputUnavailableError(f"Input device path not found: {', '.join(missing)}")
        return paths

    candidates = discover_devices()
    if not candidates:
        raise LibinputUnavailableError(
            "No keyboard/mouse event nodes were found. Pass --event-device /dev/input/eventN."
        )

    keyboard_candidates = [c for c in candidates if c[2] in ("keyboard", "mouse+keyboard")]
    mouse_candidates = [c for c in candidates if c[2] in ("mouse", "mouse+keyboard")]
    paths: list[str] = []

    # Prefer the actual USB mouse over generic PS/2 pointer nodes (commonly
    # touchpads or duplicate compatibility devices on laptops).
    if keyboard_candidates:
        paths.append(keyboard_candidates[0][0])
    if mouse_candidates:
        preferred = [c for c in mouse_candidates if "usb" in c[1].lower() and "generic" not in c[1].lower()]
        mouse = preferred[0] if preferred else mouse_candidates[0]
        if mouse[0] not in paths:
            paths.append(mouse[0])

    if not paths:
        raise LibinputUnavailableError("Select at least one mouse or keyboard device.")
    print("\n[+] Automatically selected input devices:")
    for path, name, _role in candidates:
        if path in paths:
            print(f"    {name} — {path}")
    return paths


def start_recording(output_path: Path, device_paths: Iterable[str]) -> subprocess.Popen:
    """Start libinput recording; SIGINT finalizes its YAML output cleanly."""
    executable = require_libinput()
    paths = list(device_paths)
    if not paths:
        raise LibinputUnavailableError("No input devices selected for recording.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "sudo", executable, "record", "--show-keycodes",
        "--output-file", str(output_path), *paths,
    ]
    try:
        # Keep sudo attached to the caller's terminal so it can request a
        # password if the cached sudo credential expires while waiting for a
        # screen trigger.
        proc = subprocess.Popen(command)
        proc._aotr_started_at = time.perf_counter()
    except OSError as exc:
        raise LibinputUnavailableError(f"Could not start libinput record: {exc}") from exc
    time.sleep(0.15)
    if proc.poll() is not None:
        raise LibinputUnavailableError(
            f"libinput record exited immediately with status {proc.returncode}. "
            "Check the selected event devices and permissions."
        )
    return proc


def stop_recording(proc: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2.0)


def count_event_frames(recording_path: Path) -> int:
    """Count recorded evdev frames without parsing/re-serializing libinput YAML."""
    if not recording_path.is_file():
        return 0
    try:
        return sum(1 for line in recording_path.read_text(encoding="utf-8").splitlines() if "- evdev:" in line)
    except OSError:
        return 0


_DEVICE_HEADER = re.compile(r"^- node:\s*(.*?)\s*$")
_DEVICE_NAME = re.compile(r"^\s*# Name:\s*(.*?)\s*$")
_EVENT_FRAME = re.compile(r"^  - evdev:\s*$")
_EVENT_TIME = re.compile(r"^\s+- \[\s*(\d+),\s*(\d+),")
_EVENT_LINE = re.compile(r"^(\s+- \[\s*)(\d+),\s*(\d+)(,.*)$")


def _recording_devices(path: Path) -> tuple[list[str], dict[str, tuple[list[str], list[tuple[float, list[str]]]]]]:
    """Read libinput's stable device/event block structure without a YAML dependency."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if _DEVICE_HEADER.match(line.rstrip("\n"))]
    if not starts:
        raise LibinputUnavailableError(f"No device sections found in recording: {path}")
    top = lines[:starts[0]]
    devices = {}
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else len(lines)
        block = lines[start:end]
        node = _DEVICE_HEADER.match(block[0].rstrip("\n")).group(1)
        events_index = next((i for i, line in enumerate(block) if line.rstrip("\n") == "  events:"), None)
        if events_index is None:
            raise LibinputUnavailableError(f"No event list found for {node} in {path}")
        header = block[:events_index + 1]
        event_lines = block[events_index + 1:]
        markers = [i for i, line in enumerate(event_lines) if _EVENT_FRAME.match(line.rstrip("\n"))]
        frames = []
        for marker_index, marker in enumerate(markers):
            stop = markers[marker_index + 1] if marker_index + 1 < len(markers) else len(event_lines)
            frame = event_lines[marker:stop]
            stamp = next((_EVENT_TIME.match(line) for line in frame if _EVENT_TIME.match(line)), None)
            if stamp is None:
                continue
            timestamp = int(stamp.group(1)) + int(stamp.group(2)) / 1_000_000
            frames.append((timestamp, frame))
        device_name = next(
            (match.group(1) for line in header if (match := _DEVICE_NAME.match(line.rstrip("\n")))),
            node,
        )
        key = device_name if device_name not in devices else f"{device_name} [{number}]"
        devices[key] = (header, frames)
    return top, devices


def _shift_frame(frame: list[str], timestamp: float) -> list[str]:
    """Rewrite event timestamps while preserving libinput's documented YAML layout."""
    sec = max(0, int(timestamp))
    usec = max(0, min(999_999, round((timestamp - sec) * 1_000_000)))
    shifted = []
    for line in frame:
        if "# Current time is" in line:
            continue
        match = _EVENT_LINE.match(line.rstrip("\n"))
        if match:
            line = f"{match.group(1)}{sec:3d}, {usec:6d}{match.group(4)}\n"
        shifted.append(line)
    return shifted


def merge_recording_suffix(
    prefix_path: Path,
    suffix_path: Path,
    output_path: Path,
    takeover_seconds: float,
    suffix_origin_seconds: float,
) -> int:
    """Keep the old prefix through takeover and append newly captured input."""
    top, original_devices = _recording_devices(prefix_path)
    _, suffix_devices = _recording_devices(suffix_path)
    if set(original_devices) != set(suffix_devices):
        raise LibinputUnavailableError(
            "Takeover recording used different input devices; kept the original macro unchanged."
        )

    first_suffix_time = min(
        (max(0.0, stamp - suffix_origin_seconds)
         for _, frames in suffix_devices.values()
         for stamp, _ in frames
         ),
        default=takeover_seconds,
    )
    effective_cutoff = min(takeover_seconds, first_suffix_time)

    output = list(top)
    frame_count = 0
    for node, (header, old_frames) in original_devices.items():
        new_frames = suffix_devices[node][1]
        merged: list[tuple[float, int, list[str]]] = []
        for stamp, frame in old_frames:
            if stamp <= effective_cutoff:
                merged.append((stamp, 0, frame))
        for stamp, frame in new_frames:
            mapped = max(0.0, stamp - suffix_origin_seconds)
            merged.append((mapped, 1, _shift_frame(frame, mapped)))
        merged.sort(key=lambda item: (item[0], item[1]))
        output.extend(header)
        for _, _, frame in merged:
            output.extend(frame)
            frame_count += 1

    output_path.write_text("".join(output), encoding="utf-8")
    return frame_count


def write_click_recording(template_path: Path, output_path: Path, hold_seconds: float = 0.06) -> None:
    """Build a tiny native BTN_LEFT recording from a known mouse description."""
    top, devices = _recording_devices(template_path)
    mouse_header = None
    for header, _frames in devices.values():
        description = "".join(header)
        if "Event code 272 (BTN_LEFT)" in description and "Event type 1 (EV_KEY)" in description:
            mouse_header = header
            break
    if mouse_header is None:
        raise LibinputUnavailableError(
            f"No mouse device with BTN_LEFT was found in {template_path}."
        )

    top = [re.sub(r"^(ndevices:)\s*\d+", r"\1 1", line) for line in top]
    hold_usec = max(10_000, int(hold_seconds * 1_000_000))
    sec, usec = divmod(hold_usec, 1_000_000)
    frames = [
        "  - evdev:\n",
        "    - [   0,      0,   1, 272,       1] # EV_KEY / BTN_LEFT 1\n",
        "    - [   0,      0,   0,   0,       0] # SYN_REPORT\n",
        "  - evdev:\n",
        f"    - [ {sec:3d}, {usec:6d},   1, 272,       0] # EV_KEY / BTN_LEFT 0\n",
        f"    - [ {sec:3d}, {usec:6d},   0,   0,       0] # SYN_REPORT\n",
    ]
    output_path.write_text("".join(top + mouse_header + frames), encoding="utf-8")


def start_replay(recording_path: Path) -> subprocess.Popen:
    executable = require_libinput()
    if not recording_path.is_file():
        raise FileNotFoundError(f"libinput recording not found: {recording_path}")
    command = [
        "sudo", executable, "replay", "--once", "--replay-after=0", str(recording_path)
    ]
    try:
        # Keep sudo attached to the terminal for authentication prompts.
        return subprocess.Popen(command)
    except OSError as exc:
        raise LibinputUnavailableError(f"Could not start libinput replay: {exc}") from exc


def stop_replay(proc: Optional[subprocess.Popen], timeout: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=2.0)
