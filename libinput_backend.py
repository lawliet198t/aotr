"""Adapter around libinput recordings and faithful uinput playback.

Screen triggers remain in record.py/play.py. libinput handles the actual
kernel-level input capture. Playback uses evdev/uinput so EV_KEY repeat frames
are not discarded by `libinput replay`.
"""

from __future__ import annotations

import signal
import shutil
import subprocess
import sys
import os
import threading
import time
import re
import select
from collections import Counter
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
        if any(skip in low for skip in ("aotr virtual", "aotr replay", "libinput replay")):
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


def remove_key_events(recording_path: Path, key_code: int) -> int:
    """Remove frames containing only the selected key (plus scan/sync data)."""
    if not recording_path.is_file():
        return 0
    lines = recording_path.read_text(encoding="utf-8").splitlines(keepends=True)
    output: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        if lines[i].rstrip("\n") != "  - evdev:":
            output.append(lines[i])
            i += 1
            continue
        end = i + 1
        while end < len(lines) and not lines[end].startswith(("  - evdev:", "- node:")):
            end += 1
        frame = lines[i:end]
        key_line = re.compile(
            rf"^\s+- \[\s*\d+,\s*\d+,\s*1,\s*{int(key_code)},\s*[012]\s*\]"
        )
        if any(key_line.match(line) for line in frame):
            # libinput may include MSC_SCAN beside a key. Treat that as part
            # of the stop-key frame when no other actual input is present.
            relevant = []
            for line in frame:
                match = _EVENT_FIELDS.match(line)
                if not match:
                    continue
                event_type, code = int(match.group(3)), int(match.group(4))
                if event_type == 1 and code == key_code:
                    continue
                if event_type == 4:  # MSC_SCAN associated with this key
                    continue
                if event_type == 0:  # SYN_REPORT
                    continue
                relevant.append(line)
            if not relevant:
                removed += 1
                i = end
                continue
            frame = [
                line for line in frame
                if not key_line.match(line)
                and not (lambda m: m is not None and int(m.group(3)) == 4)(_EVENT_FIELDS.match(line))
            ]
        output.extend(frame)
        i = end
    if removed:
        temp_path = recording_path.with_name(f".{recording_path.name}.filtering")
        temp_path.write_text("".join(output), encoding="utf-8")
        temp_path.replace(recording_path)
    return removed


def recording_duration_seconds(recording_path: Path) -> float:
    """Return the timestamp of the final recorded input frame, if available."""
    try:
        _, devices = _recording_devices(recording_path)
        return max(
            (stamp for _header, frames in devices.values() for stamp, _frame in frames),
            default=0.0,
        )
    except (OSError, LibinputUnavailableError, ValueError, AttributeError):
        return 0.0


class LibinputActivityMonitor:
    """Incrementally detect meaningful physical input in an active recording."""

    def __init__(self, recording_path: Path, motion_threshold: int = 3):
        self.path = recording_path
        self.motion_threshold = motion_threshold
        try:
            self.offset = recording_path.stat().st_size
        except OSError:
            self.offset = 0
        self.pending = ""
        self.dx = 0
        self.dy = 0

    def has_meaningful_input(self) -> bool:
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as recording:
                recording.seek(self.offset)
                chunk = recording.read()
                self.offset = recording.tell()
        except OSError:
            return False
        if not chunk:
            return False

        text = self.pending + chunk
        lines = text.splitlines(keepends=True)
        self.pending = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()

        meaningful = False
        for line in lines:
            match = _EVENT_FIELDS.match(line)
            if not match:
                continue
            event_type, code, value = int(match.group(3)), int(match.group(4)), int(match.group(5))
            if event_type == 1 and value in (1, 2):
                if code < 256 or code >= 272:
                    meaningful = True
            elif event_type == 2:
                if code == 0:
                    self.dx += value
                elif code == 1:
                    self.dy += value
                elif code in (6, 8, 11, 12) and value:
                    meaningful = True
        if abs(self.dx) >= self.motion_threshold or abs(self.dy) >= self.motion_threshold:
            meaningful = True
            self.dx = self.dy = 0
        return meaningful


_DEVICE_HEADER = re.compile(r"^- node:\s*(.*?)\s*$")
_DEVICE_NAME = re.compile(r"^\s*# Name:\s*(.*?)\s*$")
_EVENT_FRAME = re.compile(r"^  - evdev:\s*$")
_EVENT_TIME = re.compile(r"^\s+- \[\s*(\d+),\s*(\d+),")
_EVENT_LINE = re.compile(r"^(\s+- \[\s*)(\d+),\s*(\d+)(,.*)$")
_EVENT_FIELDS = re.compile(r"^\s+- \[\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(-?\d+)")


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


def resolve_recording_devices(recording_path: Path, saved_paths: Optional[Iterable[str]] = None) -> list[tuple[str, str, str]]:
    """Resolve the recording's physical devices by name before using old event node numbers."""
    _, recorded = _recording_devices(recording_path)
    expected_names = list(recorded)
    current = discover_devices()
    selected: list[tuple[str, str, str]] = []
    used: set[str] = set()
    for expected in expected_names:
        found = next((item for item in current if item[1] == expected and item[0] not in used), None)
        if found is None:
            selected = []
            break
        selected.append(found)
        used.add(found[0])
    if selected and len(selected) == len(expected_names):
        print("[+] Resolved the macro's physical devices by name:")
        for path, name, _role in selected:
            print(f"    {name} — {path}")
        return selected

    saved = list(saved_paths or [])
    if len(saved) == len(expected_names):
        by_path = {item[0]: item for item in current}
        validated = [by_path.get(str(Path(path))) for path in saved]
        if all(item is not None for item in validated) and [item[1] for item in validated] == expected_names:
            return validated

    # A recording can move to a different host or hardware. Use current
    # keyboard/mouse discovery; the merge checks names and preserves the old
    # macro if an equivalent device is unavailable.
    chosen_paths = choose_devices()
    by_path = {item[0]: item for item in current}
    return [by_path[path] for path in chosen_paths if path in by_path]


def has_takeover_input(recording_path: Path, role: str) -> bool:
    """Return true only for a key press, mouse button press, wheel, or motion."""
    if not recording_path.is_file():
        return False
    low_role = role.lower()
    keyboard = "keyboard" in low_role
    mouse = "mouse" in low_role or "pointer" in low_role
    try:
        for line in recording_path.read_text(encoding="utf-8").splitlines():
            match = _EVENT_FIELDS.match(line)
            if not match:
                continue
            event_type, code, value = int(match.group(3)), int(match.group(4)), int(match.group(5))
            if event_type == 1 and value == 1:
                if (keyboard and code < 256) or (mouse and code >= 272):
                    return True
            if mouse and event_type == 2 and code in (0, 1, 6, 8, 11, 12) and value != 0:
                return True
    except OSError:
        return False
    return False


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
    collapse_leading_idle: bool = False,
) -> tuple[int, int]:
    """Keep the played prefix and append captured input at its timeline boundary."""
    top, original_devices = _recording_devices(prefix_path)
    _, suffix_devices = _recording_devices(suffix_path)
    if set(original_devices) != set(suffix_devices):
        raise LibinputUnavailableError(
            "Takeover recording used different input devices; kept the original macro unchanged."
        )

    effective_cutoff = takeover_seconds

    # When capture starts only after a macro naturally ends, the time spent
    # waiting for the next screen trigger is not part of the user's action.
    # Anchor the first captured frame at the macro boundary so a short render
    # delay cannot lengthen every future replay. Preserve intervals between
    # captured frames, and preserve the original clock for mid-playback takeover.
    first_suffix_time = None
    for _header, frames in suffix_devices.values():
        for stamp, _frame in frames:
            mapped = stamp - suffix_origin_seconds
            if mapped >= effective_cutoff - 0.03:
                mapped = max(mapped, effective_cutoff)
                if first_suffix_time is None or mapped < first_suffix_time:
                    first_suffix_time = mapped

    output = list(top)
    frame_count = 0
    suffix_frame_count = 0
    for node, (header, old_frames) in original_devices.items():
        new_frames = suffix_devices[node][1]
        merged: list[tuple[float, int, list[str]]] = []
        held_keys: set[int] = set()
        for stamp, frame in old_frames:
            if stamp <= effective_cutoff:
                merged.append((stamp, 0, frame))
                for line in frame:
                    match = _EVENT_FIELDS.match(line)
                    if not match:
                        continue
                    event_type, code, value = int(match.group(3)), int(match.group(4)), int(match.group(5))
                    if event_type != 1:
                        continue
                    if value in (1, 2):
                        held_keys.add(code)
                    elif value == 0:
                        held_keys.discard(code)
        if held_keys:
            release_time = effective_cutoff
            sec = int(release_time)
            usec = round((release_time - sec) * 1_000_000)
            if usec >= 1_000_000:
                sec += 1
                usec = 0
            release_frame = ["  - evdev:\n"]
            for code in sorted(held_keys):
                release_frame.append(
                    f"    - [ {sec:3d}, {usec:6d},   1, {code:3d},       0] # AOTR handoff release\n"
                )
            release_frame.append(
                f"    - [ {sec:3d}, {usec:6d},   0,   0,       0] # SYN_REPORT\n"
            )
            merged.append((release_time, 1, release_frame))
        for stamp, frame in new_frames:
            # libinput timestamps are relative to the recorder's launch, not
            # to the first input event. Align them to playback's monotonic
            # clock and ignore physical input captured before takeover.
            mapped = stamp - suffix_origin_seconds
            if mapped < takeover_seconds - 0.03:
                continue
            mapped = max(mapped, takeover_seconds)
            if collapse_leading_idle and first_suffix_time is not None:
                mapped = takeover_seconds + max(0.0, mapped - first_suffix_time)
            merged.append((mapped, 2, _shift_frame(frame, mapped)))
            suffix_frame_count += 1
        merged.sort(key=lambda item: (item[0], item[1]))
        output.extend(header)
        for _, _, frame in merged:
            output.extend(frame)
            frame_count += 1

    output_path.write_text("".join(output), encoding="utf-8")
    return frame_count, suffix_frame_count


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


def start_replay_at_user_boundary(recording_path: Path, timeout: float = 10.0) -> tuple[subprocess.Popen, float]:
    """Replay kernel events through uinput, including EV_KEY repeat frames."""
    if not recording_path.is_file():
        raise FileNotFoundError(f"libinput recording not found: {recording_path}")
    try:
        proc = subprocess.Popen(
            ["sudo", "-n", sys.executable, str(Path(__file__).resolve()), "--native-replay", str(recording_path.resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            # Isolate a signalable process group without detaching from the
            # user's terminal session, which sudo may need for authentication.
            process_group=0,
        )
    except OSError as exc:
        raise LibinputUnavailableError(f"Could not start native event replay: {exc}") from exc
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                message = proc.stdout.readline().strip() if proc.stdout else ""
                raise LibinputUnavailableError(message or f"Native replay exited before startup (status {proc.returncode}).")
            if proc.stdout is not None and select.select([proc.stdout], [], [], 0.05)[0]:
                message = proc.stdout.readline().strip()
                if message.startswith("ERROR "):
                    raise LibinputUnavailableError(message[6:])
                if message == "READY":
                    if proc.stdin is None:
                        raise LibinputUnavailableError("Native replay start pipe was not available.")
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                    start_deadline = time.monotonic() + timeout
                    while time.monotonic() < start_deadline:
                        if proc.poll() is not None:
                            raise LibinputUnavailableError(f"Native replay exited during startup (status {proc.returncode}).")
                        if select.select([proc.stdout], [], [], 0.05)[0]:
                            started = proc.stdout.readline().strip()
                            if started.startswith("STARTED "):
                                started_at = float(started.split(maxsplit=1)[1])
                                proc._aotr_started_at = started_at
                                return proc, started_at
                            if started.startswith("ERROR "):
                                raise LibinputUnavailableError(started[6:])
                    raise LibinputUnavailableError("Timed out waiting for native replay to start.")
        raise LibinputUnavailableError("Timed out creating native virtual input devices.")
    except BaseException:
        stop_replay(proc)
        raise


def stop_replay(proc: Optional[subprocess.Popen], timeout: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return

    def signal_replay_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except (AttributeError, OSError):
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    signal_replay_group(signal.SIGINT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        signal_replay_group(signal.SIGTERM)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            signal_replay_group(signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # Never let a failed child shutdown mask the original stop
                # reason or crash the playback coordinator.
                pass


def _native_replay(recording_path: Path) -> int:
    """Privileged uinput scheduler preserving every recorded kernel event."""
    try:
        from evdev import UInput, ecodes
    except ImportError as exc:
        raise LibinputUnavailableError(
            "Native replay needs python-evdev in this virtual environment. Install it with: .venv/bin/pip install evdev"
        ) from exc

    _top, devices = _recording_devices(recording_path)
    event_type_re = re.compile(r"^\s*# Event type (\d+) \(")
    event_code_re = re.compile(r"^\s*#\s+Event code (\d+) \(")
    id_re = re.compile(r"^\s*# ID: bus 0x([0-9a-fA-F]+) vendor 0x([0-9a-fA-F]+) product 0x([0-9a-fA-F]+) version 0x([0-9a-fA-F]+)")
    runtime_devices = []
    frames = []
    for device_index, (name, (header, recorded_frames)) in enumerate(devices.items()):
        capabilities = {}
        current_type = None
        identity = (3, 1, 1, 1)
        for line in header:
            type_match = event_type_re.match(line)
            if type_match:
                current_type = int(type_match.group(1))
                # Replay recorded EV_KEY value=2 frames ourselves. Advertising
                # EV_REP would also make the kernel synthesize repeats for a
                # held key, duplicating the repeat frames from the recording.
                if current_type not in (ecodes.EV_SYN, ecodes.EV_REP):
                    capabilities.setdefault(current_type, [])
                continue
            code_match = event_code_re.match(line)
            if (code_match and current_type is not None
                    and current_type not in (ecodes.EV_SYN, ecodes.EV_REP)):
                capabilities[current_type].append(int(code_match.group(1)))
            id_match = id_re.match(line)
            if id_match:
                identity = tuple(int(value, 16) for value in id_match.groups())
        capabilities = {
            kind: sorted(set(codes))
            for kind, codes in capabilities.items()
            if codes
        }
        if not capabilities:
            raise LibinputUnavailableError(f"Recording device '{name}' has no replayable capabilities.")
        try:
            ui = UInput(
                events=capabilities, name=f"AOTR replay {name}",
                bustype=identity[0], vendor=identity[1], product=identity[2], version=identity[3],
            )
        except (OSError, ValueError) as exc:
            for opened in runtime_devices:
                opened.close()
            raise LibinputUnavailableError(f"Could not create virtual device for '{name}': {exc}") from exc
        runtime_devices.append(ui)
        for frame_index, (timestamp, lines) in enumerate(recorded_frames):
            event_list = []
            for line in lines:
                match = _EVENT_FIELDS.match(line)
                if match:
                    sec, usec, event_type, code, value = map(int, match.groups())
                    if event_type == ecodes.EV_REP:
                        continue
                    event_list.append((event_type, code, value))
            if event_list:
                frames.append((timestamp, device_index, frame_index, event_list))

    if not frames:
        for ui in runtime_devices:
            ui.close()
        raise LibinputUnavailableError("Recording contains no input events to replay.")

    print("READY", flush=True)
    if sys.stdin.readline() == "":
        for ui in runtime_devices:
            ui.close()
        return 1
    start = time.perf_counter()
    print(f"STARTED {start:.9f}", flush=True)
    frames.sort(key=lambda item: (item[0], item[1], item[2]))
    held = [set() for _ in runtime_devices]
    interrupted = {"value": False}

    def request_stop(_signum, _frame):
        interrupted["value"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        for timestamp, device_index, _frame_index, event_list in frames:
            target = start + timestamp
            while not interrupted["value"]:
                remaining = target - time.perf_counter()
                if remaining <= 0:
                    break
                if remaining > 0.003:
                    # Python may restart sleep after a handled signal. Bound
                    # the wait so takeover/Skip/Retry interrupts are observed
                    # promptly even across long gaps in the macro timeline.
                    time.sleep(min(remaining - 0.002, 0.01))
            if interrupted["value"]:
                break
            ui = runtime_devices[device_index]
            for event_type, code, value in event_list:
                if event_type == ecodes.EV_SYN:
                    continue
                ui.write(event_type, code, value)
                if event_type == ecodes.EV_KEY:
                    if value == 1:
                        held[device_index].add(code)
                    elif value == 0:
                        held[device_index].discard(code)
            ui.syn()
    finally:
        for index, ui in enumerate(runtime_devices):
            for code in held[index]:
                ui.write(ecodes.EV_KEY, code, 0)
            if held[index]:
                ui.syn()
            ui.close()
    return 0


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--native-replay":
    try:
        raise SystemExit(_native_replay(Path(sys.argv[2])))
    except (LibinputUnavailableError, OSError, ValueError) as exc:
        print(f"ERROR {exc}", flush=True)
        raise SystemExit(1)
