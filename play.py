#!/usr/bin/env python3
"""
3D Game Input Player with Continuous Loop Automation:
1. Replays recorded gameplay sequence (microsecond precision, hardware-accurate relative mouse).
2. When sequence completes, automatically waits for [Retry] button -> Clicks (X=1802, Y=1028).
3. Automatically waits for [Skip] button -> Clicks (X=1802, Y=1028).
4. Automatically waits for gameplay frame -> Repeats loop!
5. Maintains an ongoing tally and timing record of completed loops.
6. Press [Esc] anytime to abort safely and release all inputs.
"""

import os
import sys
import time
import json
import subprocess
import ctypes
import threading
import multiprocessing as mp
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple

import cv2
import numpy as np
from pynput import keyboard
from pynput.keyboard import Key, KeyCode

from utils import (
    deserialize_key,
    deserialize_button,
    accurate_sleep,
    ScreenMatcher,
    XTestController,
    RawMouseListener,
    xinput_device_id,
    serialize_key,
    serialize_button,
    START_DIR,
    RECORDINGS_DIR,
)
from libinput_backend import (
    LibinputUnavailableError,
    choose_devices,
    count_event_frames,
    merge_recording_suffix,
    start_recording as start_libinput_recording,
    start_replay as start_libinput_replay,
    stop_recording as stop_libinput_recording,
    stop_replay as stop_libinput_replay,
    sudo_warmup,
    write_click_recording,
)

XTEST_SLAVE_ID_FALLBACK = 4

DEFAULT_RETRY_IMG = Path("data/retry.png")
DEFAULT_SKIP_IMG = Path("data/skip.png")
DEFAULT_RETRY_COORDS = (1154, 847)
DEFAULT_SKIP_COORDS = (1802, 1028)

def _overlay_process_target(state_queue: mp.Queue):
    """
    Transparent, click-through HUD overlay (top-right).
    Green = CAPTURING, Red = NOT DETECTED.
    Only shown during active recording takeover.
    """
    import tkinter as tk

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.85)

        w, h = 180, 38
        sw = root.winfo_screenwidth()
        x = sw - w - 20
        y = 20
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.configure(bg="#000000")

        frame = tk.Frame(root, bg="#1a0000", highlightthickness=2, highlightbackground="#FF0000")
        frame.pack(expand=True, fill="both")
        label = tk.Label(frame, text="● NOT DETECTED", font=("Ubuntu", 11, "bold"),
                         fg="#FF0000", bg="#1a0000", padx=10, pady=4)
        label.pack(expand=True, fill="both")
        root.update_idletasks()

        try:
            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            xfixes = ctypes.cdll.LoadLibrary("libXfixes.so.3")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            dpy = x11.XOpenDisplay(None)
            if dpy:
                xfixes.XFixesSetWindowShapeRegion.argtypes = [
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int, ctypes.c_ulong
                ]
                xfixes.XFixesSetWindowShapeRegion(dpy, root.winfo_id(), 2, 0, 0, 0)
                x11.XFlush(dpy)
                x11.XCloseDisplay(dpy)
        except Exception:
            pass

        def check_queue():
            try:
                while not state_queue.empty():
                    msg = state_queue.get_nowait()
                    if msg == "RECORDING":
                        label.configure(text="● CAPTURING", fg="#00FF00", bg="#001a00")
                        frame.configure(bg="#001a00", highlightbackground="#00FF00")
                    elif msg == "STOPPED":
                        label.configure(text="● NOT DETECTED", fg="#FF0000", bg="#1a0000")
                        frame.configure(bg="#1a0000", highlightbackground="#FF0000")
                    elif msg == "EXIT":
                        root.destroy()
                        return
            except Exception:
                pass
            root.after(40, check_queue)

        root.after(40, check_queue)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            # Terminal Ctrl+C is delivered to the child too; exit the HUD
            # quietly instead of printing a multiprocessing traceback.
            try:
                root.destroy()
            except Exception:
                pass
    except Exception:
        pass


class ScreenOverlay:
    """Manages the transparent recording HUD overlay in a child process."""

    def __init__(self):
        self.queue = mp.Queue()
        self.process = mp.Process(target=_overlay_process_target, args=(self.queue,), daemon=True)
        self._started = False

    def start(self):
        if not self._started:
            try:
                self.process.start()
                self._started = True
            except Exception as e:
                print(f"[!] Overlay start failed: {e}")

    def set_recording(self, recording: bool):
        if self._started:
            try:
                self.queue.put_nowait("RECORDING" if recording else "STOPPED")
            except Exception:
                pass

    def close(self):
        if self._started:
            try:
                self.queue.put_nowait("EXIT")
                self.process.join(timeout=0.5)
            except Exception:
                pass
            if self.process.is_alive():
                try:
                    self.process.terminate()
                except Exception:
                    pass


class SequenceEntry:
    def __init__(
        self,
        json_path: Path,
        trigger_override: Optional[Path] = None,
        phase_hint: Optional[int] = None,
    ):
        self.json_path = json_path
        self.native_recording = json_path.suffix.lower() in {".yml", ".yaml"}
        if self.native_recording:
            self.events = [None] * count_event_frames(json_path)
            metadata_path = json_path.with_name(f"{json_path.stem}.meta.json")
            try:
                self.metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.metadata = {}
            self.name = self.metadata.get("name", json_path.stem)
            self.data = {"metadata": self.metadata, "events": []}
        else:
            self.data = None
            self.metadata = {}
            self.events = []
            self.name = json_path.stem
        try:
            if not self.native_recording:
                with open(json_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        raise ValueError(f"Recording JSON file '{json_path}' is empty (0 bytes). Please re-record with record.py.")
                    self.data = json.loads(content)
                self.metadata = self.data.get("metadata", {})
                self.events = self.data.get("events", [])
                self.name = self.metadata.get("name", json_path.stem)
        except json.JSONDecodeError:
            raise ValueError(f"Corrupted or incomplete JSON in '{json_path}'. Please re-record.") from None

        # Load all trigger templates (supports multi-template folders like data/start/1/)
        self.templates_gray: List[Tuple[str, np.ndarray]] = []

        # 1. Trigger override (file or directory)
        if trigger_override:
            if trigger_override.is_dir():
                for p in sorted(trigger_override.iterdir()):
                    if p.suffix.lower() in [".png", ".jpg", ".jpeg"] and p.is_file():
                        t = ScreenMatcher.load_template_gray(p)
                        if t is not None:
                            self.templates_gray.append((f"{trigger_override.name}/{p.name}", t))
            elif trigger_override.is_file():
                t = ScreenMatcher.load_template_gray(trigger_override)
                if t is not None:
                    self.templates_gray.append((trigger_override.name, t))

        # 2. Check phase folder (e.g. data/start/1/ or data/start/2/)
        inferred_phase = phase_hint
        if inferred_phase is None:
            if "phase1" in self.name.lower() or self.name.endswith("_1") or self.name == "1":
                inferred_phase = 1
            elif "phase2" in self.name.lower() or self.name.endswith("_2") or self.name == "2":
                inferred_phase = 2

        if inferred_phase in (1, 2):
            phase_dir = START_DIR / str(inferred_phase)
            if phase_dir.is_dir():
                for p in sorted(phase_dir.iterdir()):
                    if p.suffix.lower() in [".png", ".jpg", ".jpeg"] and p.is_file():
                        t = ScreenMatcher.load_template_gray(p)
                        if t is not None and not any(name.endswith(p.name) for name, _ in self.templates_gray):
                            self.templates_gray.append((f"phase{inferred_phase}/{p.name}", t))

        # 3. Check metadata saved start image (file or directory)
        saved = self.metadata.get("start_image")
        if saved:
            saved_p = Path(saved)
            if saved_p.is_dir():
                for p in sorted(saved_p.iterdir()):
                    if p.suffix.lower() in [".png", ".jpg", ".jpeg"] and p.is_file():
                        t = ScreenMatcher.load_template_gray(p)
                        if t is not None and not any(name.endswith(p.name) for name, _ in self.templates_gray):
                            self.templates_gray.append((f"{saved_p.name}/{p.name}", t))
            elif saved_p.is_file():
                t = ScreenMatcher.load_template_gray(saved_p)
                if t is not None and not any(name.endswith(saved_p.name) for name, _ in self.templates_gray):
                    self.templates_gray.append((saved_p.name, t))

        # 4. Check data/start/<stem>.png or glob variations
        cand = START_DIR / f"{json_path.stem}.png"
        if cand.is_file() and not any(name.endswith(cand.name) for name, _ in self.templates_gray):
            t = ScreenMatcher.load_template_gray(cand)
            if t is not None:
                self.templates_gray.append((cand.name, t))

        for p in sorted(START_DIR.glob(f"{json_path.stem}*.png")):
            if p.is_file() and not any(name.endswith(p.name) for name, _ in self.templates_gray):
                t = ScreenMatcher.load_template_gray(p)
                if t is not None:
                    self.templates_gray.append((p.name, t))

        self.trigger_path = Path(self.templates_gray[0][0]) if self.templates_gray else cand
        self.template_gray: Optional[np.ndarray] = self.templates_gray[0][1] if self.templates_gray else None


class InputPlayer:
    def __init__(
        self,
        target_path: Optional[Any] = None,
        all_sequences: bool = False,
        trigger_override: Optional[Path] = None,
        threshold: float = 0.80,
        fps: float = 60.0,
        speed: float = 1.0,
        loop: bool = True,
        cooldown: float = 1.0,
        retry_img: Path = DEFAULT_RETRY_IMG,
        skip_img: Path = DEFAULT_SKIP_IMG,
        retry_coords: Tuple[int, int] = DEFAULT_RETRY_COORDS,
        skip_coords: Tuple[int, int] = DEFAULT_SKIP_COORDS,
        monitor_idx: int = 1,
        dry_run: bool = False,
        mouse_mode: str = "relative",
    ):
        self.threshold = threshold
        self.check_interval = max(0.005, 1.0 / max(1.0, fps))
        self.speed = max(0.1, speed)
        self.loop = loop
        self.cooldown = cooldown
        self.monitor_idx = monitor_idx
        self.dry_run = dry_run
        self.mouse_mode = mouse_mode.lower()

        # Retry and Skip configuration
        self.retry_path = Path(retry_img)
        self.skip_path = Path(skip_img)
        self.retry_coords = retry_coords
        self.skip_coords = skip_coords
        self._last_click_log: Dict[str, float] = {}

        self.retry_template: Optional[np.ndarray] = None
        if self.retry_path.is_file():
            self.retry_template = ScreenMatcher.load_template_gray(self.retry_path)

        self.skip_template: Optional[np.ndarray] = None
        if self.skip_path.is_file():
            self.skip_template = ScreenMatcher.load_template_gray(self.skip_path)

        # Loop record keeping
        self.loop_count = 0
        self.loop_history: List[Dict[str, Any]] = []

        # Input controllers
        self.kb_controller = keyboard.Controller()
        self.xtest = XTestController()
        print(f"[+] Mouse playback backend: {self.xtest.backend_name}")
        if not self.xtest.using_uinput and self.xtest.uinput_error:
            print("[!] Kernel-level game mouse input is unavailable until /dev/uinput permissions are configured.")
        # Resolve synthetic XTest devices by name. A fixed XI2 id can collide
        # with a physical mouse on another X session and suppress mouse takeover.
        self.playback_source_ids: Set[int] = set()
        xtest_pointer_id = xinput_device_id("Virtual core XTEST pointer")
        self.playback_source_ids.add(
            xtest_pointer_id if xtest_pointer_id is not None else XTEST_SLAVE_ID_FALLBACK
        )
        if self.xtest.using_uinput:
            virtual_mouse_id = xinput_device_id("AOTR Virtual Mouse")
            if virtual_mouse_id is not None:
                self.playback_source_ids.add(virtual_mouse_id)

        # Track currently held keys for safe cleanup
        self.held_keys: Set[Any] = set()

        # Delta accumulator for sub-pixel / fractional motion
        self._accum_dx: float = 0.0
        self._accum_dy: float = 0.0
        self._last_legacy_x: Optional[int] = None
        self._last_legacy_y: Optional[int] = None

        # Screen matcher
        self.matcher = ScreenMatcher(monitor_idx=self.monitor_idx)

        # Abort control
        self.abort_requested = False
        self._setup_emergency_listener()

        # Load sequences
        self.sequences: List[SequenceEntry] = []
        self._load_sequences(target_path, all_sequences, trigger_override)

        # HUD overlay — only shown during recording takeover
        self.overlay = ScreenOverlay()
        self._button_watcher_stop = threading.Event()
        self._button_watcher: Optional[threading.Thread] = None
        self._retry_detected = threading.Event()

    def _resolve_json_path(self, p_in: Any) -> Path:
        p = Path(p_in)
        if p.is_file():
            return p
        if p.suffix.lower() not in {".json", ".yml", ".yaml"}:
            for suffix in (".yml", ".yaml", ".json"):
                candidate = Path(str(p) + suffix)
                if candidate.is_file():
                    return candidate
        if not p.name.endswith(".json"):
            with_ext = Path(str(p) + ".json")
            if with_ext.is_file():
                return with_ext
        alt = RECORDINGS_DIR / p.name
        if alt.is_file():
            return alt
        if p.suffix.lower() not in {".json", ".yml", ".yaml"}:
            for suffix in (".yml", ".yaml", ".json"):
                alt_ext = RECORDINGS_DIR / f"{p.name}{suffix}"
                if alt_ext.is_file():
                    return alt_ext
        raise FileNotFoundError(f"Recording file not found: {p_in}")

    def _load_sequences(
        self,
        target_path: Optional[Any],
        all_sequences: bool,
        trigger_override: Optional[Path]
    ) -> None:
        if all_sequences:
            dir_to_scan = target_path if (target_path and Path(target_path).is_dir()) else RECORDINGS_DIR
            if not dir_to_scan.is_dir():
                raise FileNotFoundError(f"Recordings directory not found: {dir_to_scan}")

            native_files = sorted(dir_to_scan.glob("*.yml")) + sorted(dir_to_scan.glob("*.yaml"))
            json_files = native_files or sorted(dir_to_scan.glob("*.json"))
            if not json_files:
                raise FileNotFoundError(f"No supported recording files found in {dir_to_scan}")

            for p in json_files:
                try:
                    entry = SequenceEntry(p)
                    if entry.templates_gray and entry.events:
                        self.sequences.append(entry)
                except Exception as e:
                    print(f"[!] Warning: Skipped {p.name}: {e}")

            if not self.sequences:
                raise ValueError("No valid sequences with existing trigger images found.")
            print(f"[+] Loaded {len(self.sequences)} sequence(s) from {dir_to_scan}")

        elif target_path:
            paths = list(target_path) if isinstance(target_path, (list, tuple)) else [target_path]

            # If a single name was given (e.g. "combo"), check if phase1 and phase2 exist
            if len(paths) == 1 and not Path(paths[0]).is_file():
                base_name = str(paths[0]).removesuffix(".json").removesuffix(".yml").removesuffix(".yaml")
                p1 = RECORDINGS_DIR / f"{Path(base_name).name}_phase1.yml"
                p2 = RECORDINGS_DIR / f"{Path(base_name).name}_phase2.yml"
                if not (p1.is_file() and p2.is_file()):
                    p1 = RECORDINGS_DIR / f"{Path(base_name).name}_phase1.json"
                    p2 = RECORDINGS_DIR / f"{Path(base_name).name}_phase2.json"
                if p1.is_file() and p2.is_file():
                    paths = [p1, p2]
                    print(f"[+] Auto-detected 2-phase sequence files: {p1.name} and {p2.name}")
                else:
                    p1_alt = RECORDINGS_DIR / f"{Path(base_name).name}_1.yml"
                    p2_alt = RECORDINGS_DIR / f"{Path(base_name).name}_2.yml"
                    if not (p1_alt.is_file() and p2_alt.is_file()):
                        p1_alt = RECORDINGS_DIR / f"{Path(base_name).name}_1.json"
                        p2_alt = RECORDINGS_DIR / f"{Path(base_name).name}_2.json"
                    if p1_alt.is_file() and p2_alt.is_file():
                        paths = [p1_alt, p2_alt]
                        print(f"[+] Auto-detected 2-phase sequence files: {p1_alt.name} and {p2_alt.name}")

            for idx, p_item in enumerate(paths, start=1):
                resolved = self._resolve_json_path(p_item)
                hint = idx if len(paths) == 2 else None
                entry = SequenceEntry(
                    resolved,
                    trigger_override=(trigger_override if len(paths) == 1 else None),
                    phase_hint=hint,
                )
                if not entry.events:
                    raise ValueError(f"No events found in {resolved}")
                if not entry.templates_gray:
                    print(f"[!] Warning: Trigger image '{entry.trigger_path}' not found on disk.")
                self.sequences.append(entry)
        else:
            raise ValueError("Must specify sequence file(s) or use --all.")

    def _setup_emergency_listener(self) -> None:
        def on_press(key):
            if key == Key.esc:
                print("\n[!] EMERGENCY ABORT: [Esc] pressed! Stopping playback and releasing inputs...")
                self.abort_requested = True
                self.release_all_inputs()
                return False

        self.esc_listener = keyboard.Listener(on_press=on_press)
        self.esc_listener.daemon = True
        self.esc_listener.start()

    def release_all_inputs(self) -> None:
        """Cleanly releases any currently held keys or mouse buttons."""
        for key in list(self.held_keys):
            try:
                self.kb_controller.release(key)
            except Exception:
                pass
        self.held_keys.clear()
        self.xtest.release_all()

    def click_screen(self, x: int, y: int, label: str = "") -> bool:
        """Move to a screen coordinate and send a kernel-level left click."""
        def log_rate_limited(key: str, message: str) -> None:
            now = time.perf_counter()
            if now - self._last_click_log.get(key, 0.0) >= 1.0:
                print(message)
                self._last_click_log[key] = now

        if self.dry_run:
            print(f"    [DRY RUN] Would click '{label}' at ({x}, {y})")
            return True
        self.xtest.absolute_move(x, y)
        time.sleep(0.04)
        if self.xtest.using_uinput:
            self.xtest.click("left", True)
            time.sleep(0.04)
            self.xtest.click("left", False)
            return True

        template_path = next(
            (seq.json_path for seq in self.sequences
             if seq.native_recording and seq.json_path.is_file()),
            None,
        )
        if template_path is None:
            template_path = next(
                (RECORDINGS_DIR / name for name in ("1.yml", "2.yml", "combo_phase1.yml", "phase1.yml")
                 if (RECORDINGS_DIR / name).is_file()),
                None,
            )
        if template_path is None:
            log_rate_limited(f"{label}:error", f"[!] Cannot send a game-compatible {label} click: no libinput mouse recording is available.")
            return False

        click_path = RECORDINGS_DIR / f".aotr-click-{os.getpid()}.yml"
        try:
            write_click_recording(template_path, click_path)
            click_proc = start_libinput_replay(click_path)
            try:
                click_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                stop_libinput_replay(click_proc)
                raise RuntimeError("kernel click replay timed out")
            if click_proc.returncode != 0:
                log_rate_limited(f"{label}:error", f"[!] Kernel {label} click failed (libinput replay exit {click_proc.returncode}).")
                return False
            log_rate_limited(label, f"[+] Sent kernel-level {label} click at ({x}, {y}).")
            return True
        except (OSError, LibinputUnavailableError, RuntimeError) as exc:
            log_rate_limited(f"{label}:error", f"[!] Could not send kernel-level {label} click: {exc}")
            return False
        finally:
            click_path.unlink(missing_ok=True)

    def _start_button_watcher(self) -> None:
        """Click the fixed Skip/Retry buttons independently of macro phases."""
        if self._button_watcher and self._button_watcher.is_alive():
            return
        self._button_watcher_stop.clear()

        def watch() -> None:
            next_skip = next_retry = 0.0
            last_skip_log = last_retry_log = 0.0
            last_error = None
            while not self._button_watcher_stop.is_set() and not self.abort_requested:
                try:
                    now = time.perf_counter()
                    screen_gray = self.matcher.grab_gray()
                    if self.skip_template is not None and now >= next_skip:
                        matched, score, _ = self.matcher.match(
                            self.skip_template, screen_gray=screen_gray, threshold=self.threshold
                        )
                        if matched:
                            if now - last_skip_log >= 1.0:
                                print(f"\n[AUTO] SKIP detected ({score:.3f}); clicking fixed coordinates {self.skip_coords} every 100 ms while visible.")
                                last_skip_log = now
                            self.click_screen(*self.skip_coords, label="SKIP")
                            next_skip = now + 0.1
                    if self.retry_template is not None and now >= next_retry:
                        matched, score, _ = self.matcher.match(
                            self.retry_template, screen_gray=screen_gray, threshold=self.threshold
                        )
                        if matched:
                            if now - last_retry_log >= 1.0:
                                print(f"\n[AUTO] RETRY detected ({score:.3f}); clicking fixed coordinates {self.retry_coords} every 100 ms while visible.")
                                last_retry_log = now
                            clicked = self.click_screen(*self.retry_coords, label="RETRY")
                            if clicked:
                                self._retry_detected.set()
                            next_retry = now + 0.1
                    last_error = None
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    if message != last_error:
                        print(f"\n[!] Button watcher error: {message}")
                        last_error = message
                self._button_watcher_stop.wait(0.005)

        self._button_watcher = threading.Thread(target=watch, daemon=True, name="SkipRetryWatcher")
        self._button_watcher.start()

    def _stop_button_watcher(self) -> None:
        self._button_watcher_stop.set()
        if self._button_watcher and self._button_watcher is not threading.current_thread():
            self._button_watcher.join(timeout=1.0)
        self._button_watcher = None

    def _wait_for_retry_reset(self) -> bool:
        """Wait for the always-on watcher to click Retry and signal cycle reset."""
        print("\n[MATCH END] Waiting for Retry detection; next cycle restarts at Phase 1.")
        while not self.abort_requested:
            if self._retry_detected.wait(timeout=0.1):
                self._retry_detected.clear()
                print("[+] Retry handled. Resetting cycle to Phase 1; Skip remains auto-clicked independently.")
                return True
        return False

    def _consume_retry_reset(self) -> bool:
        """Consume a Retry detected during a phase wait or macro replay."""
        if not self._retry_detected.is_set():
            return False
        self._retry_detected.clear()
        print("\n[+] Retry handled. Discarding the current phase position and resetting to Phase 1.")
        return True

    def scan_for_gameplay_trigger(self, screen_gray: np.ndarray) -> Optional[Tuple[SequenceEntry, float, Tuple[int, int]]]:
        """Check screen against all loaded sequence trigger templates."""
        best_entry: Optional[SequenceEntry] = None
        best_score = -1.0
        best_loc = (0, 0)

        for seq in self.sequences:
            for _, tmpl in seq.templates_gray:
                matched, score, loc = self.matcher.match(
                    tmpl, screen_gray=screen_gray, threshold=self.threshold
                )
                if matched and score > best_score:
                    best_score = score
                    best_entry = seq
                    best_loc = loc

        if best_entry is not None:
            return best_entry, best_score, best_loc
        return None

    def replay_sequence(
        self,
        seq: SequenceEntry,
        stop_template: Optional[np.ndarray] = None,
        stop_label: str = "RETRY",
    ) -> float:
        """
        Replay recorded events with microsecond precision.
        If real user input is detected mid-replay, stop playback and record
        from that point forward, replacing remaining events.
        Stops recording when stop_template (e.g. retry.png or skip.png) is detected.
        """
        total_events = len(seq.events)
        if seq.native_recording:
            if self.speed != 1.0:
                print("[!] libinput replay preserves recorded timing; --speed is ignored for native .yml recordings.")
            if self.dry_run:
                print(f"\n[DRY RUN] Would replay native libinput recording '{seq.json_path}' ({total_events} frames).")
                return 0.0
            print(f"\n[+] Replaying '{seq.name}' ({total_events} native input frames via libinput)...")
            capture_path = RECORDINGS_DIR / f".{seq.name}.takeover-{os.getpid()}.partial.yml"
            capture_proc = None
            capture_started = 0.0
            try:
                # Event node numbers can change after reconnect/reboot.
                devices = choose_devices()
                print("[+] Physical keyboard and mouse monitoring is armed during replay; the macro is saved only if you take control.")
                capture_path.unlink(missing_ok=True)
                capture_proc = start_libinput_recording(capture_path, devices)
                capture_started = getattr(capture_proc, "_aotr_started_at", time.perf_counter())
                process = start_libinput_replay(seq.json_path)
            except (OSError, LibinputUnavailableError) as exc:
                if capture_proc:
                    stop_libinput_recording(capture_proc)
                capture_path.unlink(missing_ok=True)
                self.abort_requested = True
                print(f"[!] Could not arm takeover recording or start libinput replay: {exc}")
                return 0.0
            playback_started = time.perf_counter()
            takeover_detected = False
            takeover_detected_at = 0.0
            try:
                while process.poll() is None:
                    if self.abort_requested or self._retry_detected.is_set():
                        break
                    if count_event_frames(capture_path) > 0:
                        takeover_detected = True
                        takeover_detected_at = time.perf_counter()
                        break
                    time.sleep(0.03)
                if self.abort_requested or self._retry_detected.is_set():
                    stop_libinput_replay(process)
                elif takeover_detected:
                    print("\n[!] Physical input detected. Taking control and recording your input...")
                    stop_libinput_replay(process)
                elif process.returncode:
                    print(f"[!] libinput replay exited with status {process.returncode}.")
            except KeyboardInterrupt:
                if process.poll() is None:
                    stop_libinput_replay(process)
                stop_libinput_recording(capture_proc)
                capture_path.unlink(missing_ok=True)
                raise
            finally:
                if process.poll() is None:
                    stop_libinput_replay(process)
                self.release_all_inputs()

            elapsed = time.perf_counter() - playback_started
            if takeover_detected and not self.abort_requested and not self._retry_detected.is_set():
                elapsed = self._handle_native_takeover(
                    seq, capture_path, capture_proc, devices,
                    takeover_seconds=takeover_detected_at - playback_started,
                    suffix_origin_seconds=playback_started - capture_started,
                    stop_template=stop_template, stop_label=stop_label,
                )
                capture_proc = None
            if capture_proc:
                stop_libinput_recording(capture_proc)
            capture_path.unlink(missing_ok=True)
            print(f"[+] Replay of '{seq.name}' finished in {elapsed:.2f}s.")
            return elapsed

        mode_desc = "Relative (3D motion)" if self.mouse_mode == "relative" else "Absolute"
        print(f"\n[+] Replaying '{seq.name}' ({total_events} events, mouse: {mode_desc}, speed: {self.speed:.2f}x)...")

        playback_start = time.perf_counter()
        prev_event_time = 0.0
        self._accum_dx = 0.0
        self._accum_dy = 0.0
        self._last_legacy_x = None
        self._last_legacy_y = None

        # Takeover detection: listen for real (non-XTest) user input
        self._takeover_triggered = False
        self._takeover_events_prefix: List[Dict[str, Any]] = []
        self._takeover_initial_event: Optional[Dict[str, Any]] = None
        self._takeover_sequence_time = 0.0
        takeover_state = {"active": True, "prefix_ready": False}
        _takeover_lock = threading.Lock()

        def _on_real_input(event: Dict[str, Any]) -> None:
            """Switch exactly once from scripted playback to user recording."""
            with _takeover_lock:
                if takeover_state["active"] and not self._takeover_triggered:
                    self._takeover_triggered = True
                    # Keep the event that caused takeover; otherwise the first
                    # real movement/click is lost while listeners are switched.
                    self._takeover_initial_event = event

        _takeover_listener = RawMouseListener(
            on_move=lambda dx, dy: _on_real_input({"type": "mouse_move", "dx": round(dx, 2), "dy": round(dy, 2)}),
            on_click=lambda btn, pressed: _on_real_input({"type": "mouse_click", "button": btn, "pressed": pressed}),
            on_scroll=lambda dx, dy: _on_real_input({"type": "mouse_scroll", "dx": int(dx), "dy": int(dy)}),
            exclude_slave_ids=self.playback_source_ids,
        )
        _takeover_listener.start()
        _takeover_started = True

        for idx, ev in enumerate(seq.events):
            if self.abort_requested:
                print("[!] Playback aborted by user.")
                break
            if self._retry_detected.is_set():
                print("\n[!] Retry detected during playback; stopping this macro and resetting its phase.")
                break

            # Check if user has taken over
            with _takeover_lock:
                if self._takeover_triggered:
                    # Keep events played so far as prefix
                    self._takeover_events_prefix = list(seq.events[:idx])
                    takeover_state["prefix_ready"] = True
                    self._takeover_sequence_time = max(
                        self._takeover_events_prefix[-1].get("time", 0.0) if self._takeover_events_prefix else 0.0,
                        (time.perf_counter() - playback_start) * self.speed,
                    )
                    print(f"\n[!] Real input detected at event {idx}/{total_events}. Switching to record mode...")
                    break

            ev_time = ev.get("time", 0.0) / self.speed
            ev_type = ev.get("type")

            # Wait until this event's timestamp
            delay = ev_time - prev_event_time
            if delay > 0:
                # Check every 5ms so user takeover never waits for a long
                # scripted pause to finish before recording begins.
                deadline = time.perf_counter() + delay
                while True:
                    with _takeover_lock:
                        if self._takeover_triggered or self._retry_detected.is_set():
                            break
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    if remaining <= 0.004:
                        accurate_sleep(remaining)
                    else:
                        time.sleep(min(0.005, remaining - 0.003))
                with _takeover_lock:
                    if self._takeover_triggered:
                        self._takeover_events_prefix = list(seq.events[:idx])
                        takeover_state["prefix_ready"] = True
                        self._takeover_sequence_time = max(
                            self._takeover_events_prefix[-1].get("time", 0.0) if self._takeover_events_prefix else 0.0,
                            (time.perf_counter() - playback_start) * self.speed,
                        )
                        print(f"\n[!] Real input detected at event {idx}/{total_events}. Switching to record mode...")
                        break
                if self._retry_detected.is_set():
                    print("\n[!] Retry detected during playback; stopping this macro and resetting its phase.")
                    break
            prev_event_time = ev_time

            if self.dry_run:
                continue

            try:
                if ev_type == "key_press":
                    k = deserialize_key(ev.get("key", {}))
                    if k is not None:
                        self.kb_controller.press(k)
                        self.held_keys.add(k)

                elif ev_type == "key_release":
                    k = deserialize_key(ev.get("key", {}))
                    if k is not None:
                        self.kb_controller.release(k)
                        self.held_keys.discard(k)

                elif ev_type == "mouse_move":
                    if "dx" in ev and "dy" in ev:
                        self._accum_dx += ev["dx"]
                        self._accum_dy += ev["dy"]
                        step_x = int(self._accum_dx)
                        step_y = int(self._accum_dy)
                        self._accum_dx -= step_x
                        self._accum_dy -= step_y
                        if step_x != 0 or step_y != 0:
                            self.xtest.relative_move(step_x, step_y)
                    else:
                        cur_x = int(ev.get("x", 0))
                        cur_y = int(ev.get("y", 0))
                        if self.mouse_mode == "relative":
                            if self._last_legacy_x is not None and self._last_legacy_y is not None:
                                r_dx = cur_x - self._last_legacy_x
                                r_dy = cur_y - self._last_legacy_y
                                if r_dx != 0 or r_dy != 0:
                                    self.xtest.relative_move(r_dx, r_dy)
                            self._last_legacy_x = cur_x
                            self._last_legacy_y = cur_y
                        else:
                            self.xtest.absolute_move(cur_x, cur_y)

                elif ev_type == "mouse_click":
                    btn_name = ev.get("button", "left")
                    pressed = ev.get("pressed", True)
                    self.xtest.click(btn_name, pressed)

                elif ev_type == "mouse_scroll":
                    dy = ev.get("dy", 1)
                    if dy != 0:
                        self.xtest.scroll(dy)

            except Exception as _e:
                print(f"[!] Replay event error ({ev_type}): {_e}")

        # Close the takeover window before stopping the listener. Input after
        # the final macro event must not start a replacement recording with
        # an empty prefix.
        with _takeover_lock:
            takeover_state["active"] = False
            if self._takeover_triggered and not takeover_state["prefix_ready"]:
                self._takeover_triggered = False
                self._takeover_initial_event = None

        elapsed = time.perf_counter() - playback_start
        self.release_all_inputs()
        _takeover_listener.stop()
        print(f"[+] Replay of '{seq.name}' finished in {elapsed:.2f}s.")

        # If user took over mid-replay, handle recording
        if self._takeover_triggered and not self._retry_detected.is_set():
            elapsed = self._handle_takeover_recording(
                seq, self._takeover_events_prefix, elapsed,
                sequence_time_offset=self._takeover_sequence_time,
                initial_event=self._takeover_initial_event,
                stop_template=stop_template, stop_label=stop_label
            )

        return elapsed

    def _handle_native_takeover(
        self,
        seq: SequenceEntry,
        capture_path: Path,
        capture_proc,
        devices: List[str],
        takeover_seconds: float,
        suffix_origin_seconds: float,
        stop_template: Optional[np.ndarray],
        stop_label: str,
    ) -> float:
        """Continue the already-running physical-device capture through Skip/Retry."""
        label = stop_label.upper()
        print(f"\n[!] >>> TAKEOVER: Recording your input. Stops when [{label}] is detected. <<")
        self.overlay.start()
        self.overlay.set_recording(True)
        takeover_recording_started = time.perf_counter()
        target_template = stop_template
        if target_template is None:
            target_template = self.retry_template
            label = "RETRY"
        print(f"[+] Watching for [{label}] to stop takeover recording...")

        while not self.abort_requested and not self._retry_detected.is_set():
            if target_template is not None:
                screen = self.matcher.grab_gray()
                matched, score, _ = self.matcher.match(
                    target_template, screen_gray=screen, threshold=self.threshold
                )
                if matched:
                    print(f"[!] >>> {label} DETECTED (score={score:.3f}). Stopping takeover recording. <<<")
                    break
            time.sleep(0.016)

        stop_libinput_recording(capture_proc)
        self.overlay.set_recording(False)
        suffix_frames = count_event_frames(capture_path)
        if suffix_frames < 1:
            print(f"[!] Takeover captured only {suffix_frames} event frames; keeping the existing macro.")
            return takeover_seconds

        temp_path = seq.json_path.with_name(f".{seq.name}.merged.partial.yml")
        backup_path = seq.json_path.with_suffix(".before-takeover.yml")
        try:
            merged_frames = merge_recording_suffix(
                seq.json_path, capture_path, temp_path,
                takeover_seconds=takeover_seconds,
                suffix_origin_seconds=suffix_origin_seconds,
            )
            if merged_frames < 1:
                temp_path.unlink(missing_ok=True)
                print("[!] Merged recording was too short; keeping the existing macro.")
                return takeover_seconds
            if not backup_path.exists():
                import shutil as _shutil
                _shutil.copy2(seq.json_path, backup_path)
            temp_path.replace(seq.json_path)
            seq.events = [None] * merged_frames
            metadata = dict(seq.metadata)
            metadata["duration_seconds"] = round(
                takeover_seconds + (time.perf_counter() - takeover_recording_started), 4
            )
            metadata.update({
                "created_at": datetime.now().isoformat(),
                "event_frames": merged_frames,
                "input_devices": devices,
                "stop_reason": f"Takeover recording ({label} detected)",
                "takeover_seconds": round(takeover_seconds, 4),
            })
            meta_path = seq.json_path.with_name(f"{seq.json_path.stem}.meta.json")
            meta_temp = meta_path.with_suffix(".tmp")
            meta_temp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            meta_temp.replace(meta_path)
            seq.metadata = metadata
            print(f"[+] Saved updated sequence to {seq.json_path} ({merged_frames} event frames)")
            print(f"[+] Original macro backup: {backup_path}")
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            recovery_path = seq.json_path.with_name(
                f"{seq.name}.takeover-recovery-{datetime.now().strftime('%Y%m%d_%H%M%S')}.yml"
            )
            try:
                capture_path.replace(recovery_path)
                print(f"[!] Merge failed; original macro kept and takeover input saved to {recovery_path}: {exc}")
            except OSError:
                print(f"[!] Could not merge takeover recording; original macro kept: {exc}")
        return takeover_seconds + (time.perf_counter() - takeover_recording_started)

    def _handle_takeover_recording(
        self,
        seq: SequenceEntry,
        prefix_events: List[Dict[str, Any]],
        elapsed_so_far: float,
        sequence_time_offset: float,
        initial_event: Optional[Dict[str, Any]],
        stop_template: Optional[np.ndarray] = None,
        stop_label: str = "RETRY",
    ) -> float:
        """
        Record user input in real time, stop when stop_template (retry or skip) is detected.
        Merges prefix_events + new events and saves back to seq.json_path.
        """
        target_template = stop_template if stop_template is not None else self.retry_template
        label = stop_label if stop_template is not None else "RETRY"
        print(f"\n[!] >>> TAKEOVER: Recording your input. Stops when [{label}] is detected. <<<")

        self.overlay.start()
        self.overlay.set_recording(True)

        new_events: List[Dict[str, Any]] = []
        new_events_lock = threading.Lock()
        rec_start = time.perf_counter()
        # Offset to the moment takeover happened, not merely to the last
        # event. This keeps the replacement timeline continuous through gaps.
        time_offset = max(
            prefix_events[-1].get("time", 0.0) if prefix_events else 0.0,
            sequence_time_offset,
        )

        def _log(ev: Dict[str, Any]):
            ev["time"] = round(time_offset + (time.perf_counter() - rec_start), 6)
            with new_events_lock:
                new_events.append(ev)

        if initial_event is not None:
            first_event = dict(initial_event)
            first_event["time"] = round(time_offset, 6)
            new_events.append(first_event)

        # Raw mouse listener — exclude XTest so we only capture real input
        raw_listener = RawMouseListener(
            on_move=lambda dx, dy: _log({"type": "mouse_move", "dx": round(dx, 2), "dy": round(dy, 2)}),
            on_click=lambda btn, pressed: _log({"type": "mouse_click", "button": btn, "pressed": pressed}),
            on_scroll=lambda dx, dy: _log({"type": "mouse_scroll", "dx": int(dx), "dy": int(dy)}),
            exclude_slave_ids=self.playback_source_ids,
        )
        raw_listener.start()

        # Keyboard listener for recording
        def _on_key_press(key):
            if key == Key.esc:
                return False
            _log({"type": "key_press", "key": serialize_key(key)})

        def _on_key_release(key):
            _log({"type": "key_release", "key": serialize_key(key)})

        kb_listener = keyboard.Listener(on_press=_on_key_press, on_release=_on_key_release)
        kb_listener.start()

        # Poll for stop detection (retry or skip)
        stop_rec = False
        if target_template is not None:
            print(f"[+] Watching for [{label}] to stop recording...")
            while not self.abort_requested and not stop_rec:
                sg = self.matcher.grab_gray()
                m, score, _ = self.matcher.match(target_template, screen_gray=sg, threshold=self.threshold)
                if m:
                    print(f"[!] >>> {label} DETECTED (score={score:.3f}). Stopping recording. <<<")
                    stop_rec = True
                time.sleep(0.016)
        else:
            print(f"[!] No {label} template found; press [Esc] to stop recording.")
            kb_listener.join()

        raw_listener.stop()
        if kb_listener.is_alive():
            kb_listener.stop()

        self.overlay.set_recording(False)

        # Merge and save
        with new_events_lock:
            merged = list(prefix_events) + list(new_events)

        rec_duration = time.perf_counter() - rec_start
        if len(new_events) < 10:
            print(
                f"[!] Takeover captured only {len(new_events)} new event(s) in {rec_duration:.2f}s; "
                f"discarding this incomplete update and keeping the existing {len(seq.events)}-event macro."
            )
            return elapsed_so_far + rec_duration

        new_metadata = dict(seq.metadata)
        new_metadata["duration_seconds"] = round(time_offset + rec_duration, 4)
        new_metadata["event_count"] = len(merged)
        new_metadata["stop_reason"] = f"Takeover recording ({label} detected)"
        new_metadata["created_at"] = datetime.now().isoformat()

        data_to_save = {"metadata": new_metadata, "events": merged}
        temp_path = seq.json_path.with_suffix(".tmp")
        try:
            backup_path = seq.json_path.with_suffix(".before-takeover.json")
            if not backup_path.exists():
                import shutil as _shutil
                _shutil.copy2(seq.json_path, backup_path)
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_path.replace(seq.json_path)
            print(f"[+] Saved updated sequence to {seq.json_path} ({len(merged)} events)")
        except Exception as e:
            print(f"[!] Error saving takeover recording: {e}")

        # Reload events from disk into seq object so next replay uses new data
        seq.events = merged

        print(f"[+] Takeover recording done. {len(new_events)} new events appended.")
        return elapsed_so_far + rec_duration

    def wait_for_retry(self) -> bool:
        """
        Watches screen for [Retry] button.
        When detected, clicks at self.retry_coords (X=1802, Y=1028).
        """
        if self.retry_template is None:
            print("[!] Note: 'data/retry.png' not loaded, skipping retry check.")
            return True

        print(f"\n[+] Watching screen for [RETRY] button (target: >={self.threshold:.2f})...")
        peak_score = 0.0
        last_log = time.perf_counter()

        while not self.abort_requested:
            screen_gray = self.matcher.grab_gray()

            # Check retry match
            is_match, score, loc = self.matcher.match(
                self.retry_template, screen_gray=screen_gray, threshold=self.threshold
            )
            if score > peak_score:
                peak_score = score

            if is_match:
                sys.stdout.write(f"\r >> [RETRY DETECTED] Score: {score:.3f} >= {self.threshold:.2f}   \n")
                sys.stdout.flush()
                print(f"[!] >>> 'RETRY' DETECTED! <<< (Confidence: {score:.4f})")
                self.click_screen(self.retry_coords[0], self.retry_coords[1], label="RETRY")
                time.sleep(0.5)  # Cooldown after clicking
                return True

            # If skip button appears directly, handle it
            if self.skip_template is not None:
                skip_match, skip_score, _ = self.matcher.match(
                    self.skip_template, screen_gray=screen_gray, threshold=self.threshold
                )
                if skip_match:
                    print(f"\n[!] Note: [SKIP] appeared directly (Confidence: {skip_score:.4f}).")
                    self.click_screen(self.skip_coords[0], self.skip_coords[1], label="SKIP")
                    time.sleep(0.5)
                    return True

            # Live terminal readout
            now = time.perf_counter()
            if now - last_log >= 0.15:
                last_log = now
                sys.stdout.write(
                    f"\r >> [WAITING FOR RETRY] Current: {max(0.0, score):.3f} | Peak: {peak_score:.3f} | Target: >={self.threshold:.2f}   "
                )
                sys.stdout.flush()

            time.sleep(self.check_interval)

        return False

    def wait_for_skip(self, target_gameplay_templates: Optional[List[Tuple[str, np.ndarray]]] = None) -> bool:
        """
        Watches screen for [Skip] button.
        When detected, clicks at self.skip_coords (X=1802, Y=1028).
        If target_gameplay_templates appear directly (skip was already clicked/bypassed), advances immediately.
        """
        if self.skip_template is None:
            print("[!] Note: 'data/skip.png' not loaded, skipping skip check.")
            return True

        print(f"\n[+] Watching screen for [SKIP] button (target: >={self.threshold:.2f})...")
        peak_score = 0.0
        last_log = time.perf_counter()

        while not self.abort_requested:
            screen_gray = self.matcher.grab_gray()

            # Check skip match
            is_match, score, loc = self.matcher.match(
                self.skip_template, screen_gray=screen_gray, threshold=self.threshold
            )
            if score > peak_score:
                peak_score = score

            if is_match:
                sys.stdout.write(f"\r >> [SKIP DETECTED] Score: {score:.3f} >= {self.threshold:.2f}   \n")
                sys.stdout.flush()
                print(f"[!] >>> 'SKIP' DETECTED! <<< (Confidence: {score:.4f})")
                self.click_screen(self.skip_coords[0], self.skip_coords[1], label="SKIP")
                time.sleep(0.5)  # Cooldown after clicking
                return True

            # If the upcoming gameplay trigger frame appears directly
            if target_gameplay_templates:
                for name, tmpl in target_gameplay_templates:
                    g_match, g_score, _ = self.matcher.match(
                        tmpl, screen_gray=screen_gray, threshold=self.threshold
                    )
                    if g_match:
                        print(f"\n[+] Note: Gameplay trigger frame '{name}' appeared directly (Confidence: {g_score:.4f}).")
                        return True
            else:
                game_match = self.scan_for_gameplay_trigger(screen_gray)
                if game_match is not None:
                    print("\n[+] Note: Gameplay trigger frame appeared directly.")
                    return True

            # Live terminal readout
            now = time.perf_counter()
            if now - last_log >= 0.15:
                last_log = now
                sys.stdout.write(
                    f"\r >> [WAITING FOR SKIP] Current: {max(0.0, score):.3f} | Peak: {peak_score:.3f} | Target: >={self.threshold:.2f}   "
                )
                sys.stdout.flush()

            time.sleep(self.check_interval)

        return False

    def wait_for_specific_gameplay(
        self, seq: SequenceEntry, threshold: Optional[float] = None
    ) -> Optional[SequenceEntry]:
        """Watches screen specifically for sequence trigger template(s)."""
        match_threshold = self.threshold if threshold is None else threshold
        if not seq.templates_gray:
            print(f"[!] Warning: No trigger images loaded for '{seq.name}'. Proceeding immediately.")
            return seq

        names_str = ", ".join(name for name, _ in seq.templates_gray)
        print(f"\n[+] Waiting for [{seq.name}] trigger(s) ({names_str}, target: >={match_threshold:.2f})...")
        peak_score = 0.0
        last_log = time.perf_counter()

        while not self.abort_requested:
            if self._retry_detected.is_set():
                return None
            screen_gray = self.matcher.grab_gray()

            for name, tmpl in seq.templates_gray:
                matched, score, loc = self.matcher.match(
                    tmpl, screen_gray=screen_gray, threshold=match_threshold
                )
                if score > peak_score:
                    peak_score = score

                if matched:
                    sys.stdout.write(f"\r >> [{seq.name} TRIGGER DETECTED ({name})] Score: {score:.3f} >= {match_threshold:.2f}\n")
                    sys.stdout.flush()
                    print(f"[!] >>> GAMEPLAY TRIGGER MATCHED ({seq.name} / {name})! <<< (Confidence: {score:.4f} at {loc})")
                    return seq

            # Live progress readout
            now = time.perf_counter()
            if now - last_log >= 0.15:
                last_log = now
                sys.stdout.write(
                    f"\r >> [WAITING FOR {seq.name.upper()}] Current Peak: {peak_score:.3f} | Target: >={match_threshold:.2f}   "
                )
                sys.stdout.flush()

            time.sleep(self.check_interval)

        return None

    def wait_for_gameplay_start(self) -> Optional[SequenceEntry]:
        """Watches screen for any gameplay trigger frame, with fallback handling for retry/skip."""
        print(f"\n[+] Waiting for GAMEPLAY trigger frame... (Loops completed: {self.loop_count})")
        peak_score = 0.0
        last_log = time.perf_counter()

        while not self.abort_requested:
            if self._retry_detected.is_set():
                return None
            screen_gray = self.matcher.grab_gray()

            # Check sequence trigger
            match_res = self.scan_for_gameplay_trigger(screen_gray)
            if match_res is not None:
                seq, score, loc = match_res
                sys.stdout.write(f"\r >> [GAMEPLAY TRIGGER DETECTED] Score: {score:.3f} >= {self.threshold:.2f}\n")
                sys.stdout.flush()
                print(f"\n[!] >>> GAMEPLAY TRIGGER MATCHED! <<<")
                print(f"    Sequence   : {seq.name}")
                print(f"    Confidence : {score:.4f} >= {self.threshold:.2f} at {loc}")
                return seq

            # Live progress readout
            now = time.perf_counter()
            if now - last_log >= 0.15:
                last_log = now
                cur_max = 0.0
                for seq in self.sequences:
                    if seq.template_gray is not None:
                        _, s_val, _ = self.matcher.match(seq.template_gray, screen_gray=screen_gray, threshold=0.0)
                        if s_val > cur_max:
                            cur_max = s_val
                if cur_max > peak_score:
                    peak_score = cur_max

                sys.stdout.write(
                    f"\r >> [WAITING FOR GAMEPLAY] Current: {max(0.0, cur_max):.3f} | Peak: {peak_score:.3f} | Target: >={self.threshold:.2f} (Loops: {self.loop_count})   "
                )
                sys.stdout.flush()

            time.sleep(self.check_interval)

        return None

    def run_two_phase_loop(self) -> None:
        """Automated 2-phase continuous loop: Skip1 -> Phase1 -> Skip2 -> Phase2 -> Retry."""
        seq1 = self.sequences[0]
        seq2 = self.sequences[1]

        t1_names = ", ".join(name for name, _ in seq1.templates_gray) or seq1.trigger_path.name
        t2_names = ", ".join(name for name, _ in seq2.templates_gray) or seq2.trigger_path.name

        print("=" * 60)
        print("    3D GAME INPUT PLAYER (TWO-PHASE CONTINUOUS LOOP)")
        print("=" * 60)
        print(f" Phase 1 Macro    : [{seq1.name}] (Triggers: {t1_names}, Events: {len(seq1.events)})")
        print(f" Phase 2 Macro    : [{seq2.name}] (Triggers: {t2_names}, Events: {len(seq2.events)})")
        print(f" Playback Speed   : {self.speed:.2f}x")
        print(f" Mouse Mode       : {self.mouse_mode.upper()} (Optimal for 3D games)")
        print(f" Phase 1 Threshold: {self.threshold:.2f}")
        print(" Phase 2 Threshold: 0.70")
        print(f" Skip Button Img  : {self.skip_path.name}  -> Click at {self.skip_coords}")
        print(f" Retry Button Img : {self.retry_path.name} -> Click at {self.retry_coords}")
        print(f" Loop Automation  : {'ENABLED (Continuous)' if self.loop else 'DISABLED (One-shot)'}")
        print(" Button Handling  : Skip/Retry are clicked immediately by the independent watcher")
        print(" Cycle Reset      : After Retry is detected/clicked, restart at Phase 1")
        print(" Flow             : Phase 1 trigger -> Phase 1 -> Phase 2 trigger -> Phase 2 -> Retry reset")
        print(f" Emergency Exit   : Press [Esc] anytime to abort")
        print("=" * 60)

        total_session_start = time.perf_counter()

        try:
            while not self.abort_requested:
                loop_num = self.loop_count + 1
                print(f"\n" + "=" * 60)
                print(f"             >>> STARTING LOOP #{loop_num} <<<")
                print("=" * 60)

                # ---------------------------------------------------------
                # 1. Wait for Gameplay 1 Trigger Frame. Skip is handled by
                # the independent watcher and is not a sequencing gate.
                # ---------------------------------------------------------
                print(f"\n[PHASE 1] Step 1/2: Waiting for Gameplay 1 Trigger ({t1_names})...")
                matched_seq = self.wait_for_specific_gameplay(seq1)
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue
                if matched_seq is None:
                    break

                # ---------------------------------------------------------
                # 2. Replay Phase 1 Sequence (Takeover stops on skip.png)
                # ---------------------------------------------------------
                print(f"\n[PHASE 1] Step 2/2: Replaying Phase 1 Macro ({seq1.name})...")
                duration1 = self.replay_sequence(seq1, stop_template=self.skip_template, stop_label="SKIP")
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue

                # ---------------------------------------------------------
                # 1. Wait for Gameplay 2 Trigger Frame
                # ---------------------------------------------------------
                print(f"\n[PHASE 2] Step 1/2: Waiting for Gameplay 2 Trigger ({t2_names})...")
                matched_seq = self.wait_for_specific_gameplay(seq2, threshold=0.70)
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue
                if matched_seq is None:
                    break

                # ---------------------------------------------------------
                # 2. Replay Phase 2 Sequence (Takeover stops on retry.png)
                # ---------------------------------------------------------
                print(f"\n[PHASE 2] Step 2/2: Replaying Phase 2 Macro ({seq2.name})...")
                duration2 = self.replay_sequence(seq2, stop_template=self.retry_template, stop_label="RETRY")
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue

                # Retry detection is the explicit cycle boundary. The global
                # watcher clicks it wherever it appears; after it is handled,
                # restart the macro state from Phase 1. Skip remains automatic.
                if not self._wait_for_retry_reset():
                    break

                # ---------------------------------------------------------
                # 3. Record Loop Completion & Log Statistics
                # ---------------------------------------------------------
                self.loop_count += 1
                now_str = datetime.now().strftime("%H:%M:%S")
                total_elapsed = time.perf_counter() - total_session_start

                self.loop_history.append({
                    "loop": self.loop_count,
                    "sequence": f"{seq1.name} + {seq2.name}",
                    "play_duration_sec": round(duration1 + duration2, 2),
                    "completed_at": now_str,
                })

                print("\n" + "=" * 60)
                print(f"       ★ ★ ★  LOOP #{self.loop_count} COMPLETED SUCCESSFULLY!  ★ ★ ★")
                print("=" * 60)
                print(f"  Phase 1 Time     : {duration1:.2f}s ({seq1.name})")
                print(f"  Phase 2 Time     : {duration2:.2f}s ({seq2.name})")
                print(f"  Total Play Time  : {(duration1 + duration2):.2f}s")
                print(f"  Total Loops Done : {self.loop_count}")
                print(f"  Total Run Time   : {total_elapsed / 60:.1f} minutes")
                print("=" * 60)

                if not self.loop:
                    print("[+] One-shot mode completed. Exiting.")
                    break

                if self.cooldown > 0:
                    time.sleep(self.cooldown)

        except KeyboardInterrupt:
            print("\n[!] Ctrl+C detected. Exiting safely...")

    def run_single_phase_loop(self) -> None:
        """Standard 1-phase loop; Skip/Retry are handled independently."""
        print("=" * 60)
        print("    3D GAME INPUT PLAYER (SINGLE-PHASE CONTINUOUS LOOP)")
        print("=" * 60)
        print(f" Loaded Sequences : {len(self.sequences)}")
        for s in self.sequences:
            t_status = "OK" if (s.template_gray is not None) else "MISSING IMAGE"
            print(f"  - [{s.name}] Trigger: {s.trigger_path.name} ({t_status}), Events: {len(s.events)}")
        print(f" Playback Speed   : {self.speed:.2f}x")
        print(f" Mouse Mode       : {self.mouse_mode.upper()} (Optimal for 3D games)")
        print(f" Match Threshold  : {self.threshold:.2f}")
        print(f" Retry Button Img : {self.retry_path.name} -> Click at {self.retry_coords}")
        print(f" Skip Button Img  : {self.skip_path.name}  -> Click at {self.skip_coords}")
        print(" Button Handling  : Skip/Retry are clicked by the independent watcher")
        print(f" Loop Automation  : {'ENABLED (Continuous)' if self.loop else 'DISABLED (One-shot)'}")
        print(f" Emergency Exit   : Press [Esc] anytime to abort")
        print("=" * 60)

        total_session_start = time.perf_counter()

        try:
            while not self.abort_requested:
                # 1. Wait for Gameplay Trigger Frame
                matched_seq = self.wait_for_gameplay_start()
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue
                if matched_seq is None:
                    break

                # 2. Replay the Gameplay Sequence
                play_duration = self.replay_sequence(matched_seq)
                if self.abort_requested:
                    break
                if self._consume_retry_reset():
                    continue

                # 3. Record Loop Completion & Log Statistics
                self.loop_count += 1
                now_str = datetime.now().strftime("%H:%M:%S")
                total_elapsed = time.perf_counter() - total_session_start

                self.loop_history.append({
                    "loop": self.loop_count,
                    "sequence": matched_seq.name,
                    "play_duration_sec": round(play_duration, 2),
                    "completed_at": now_str,
                })

                print("\n" + "=" * 60)
                print(f"       ★ ★ ★  LOOP #{self.loop_count} COMPLETED SUCCESSFULLY!  ★ ★ ★")
                print("=" * 60)
                print(f"  Sequence Played  : {matched_seq.name}")
                print(f"  Sequence Time    : {play_duration:.2f} seconds")
                print(f"  Total Loops Done : {self.loop_count}")
                print(f"  Total Run Time   : {total_elapsed / 60:.1f} minutes")
                print("=" * 60)

                if not self.loop:
                    print("[+] One-shot mode completed. Exiting.")
                    break

                if self.cooldown > 0:
                    time.sleep(self.cooldown)

        except KeyboardInterrupt:
            print("\n[!] Ctrl+C detected. Exiting safely...")

    def run(self) -> None:
        """Entry point; button handling runs independently of macro phases."""
        try:
            self._start_button_watcher()
            if len(self.sequences) == 2:
                self.run_two_phase_loop()
            else:
                self.run_single_phase_loop()
        finally:
            self._stop_button_watcher()
            self.release_all_inputs()
            self.xtest.close()
            self.matcher.close()
            self.overlay.close()
            print("\n" + "=" * 60)
            print(f"               SESSION SUMMARY")
            print("=" * 60)
            print(f"  Total Loops Completed : {self.loop_count}")
            for h in self.loop_history:
                print(f"   - Loop #{h['loop']}: {h['sequence']} ({h['play_duration_sec']}s at {h['completed_at']})")
            print("=" * 60)


def parse_coords(coord_str: str, default: Tuple[int, int] = DEFAULT_SKIP_COORDS) -> Tuple[int, int]:
    """Parse 'X,Y' string into (int, int)."""
    try:
        parts = coord_str.replace(" ", "").split(",")
        return int(parts[0]), int(parts[1])
    except Exception:
        return default


def main():
    parser = argparse.ArgumentParser(
        description="Continuous 3D Game Player with Automated 2-Phase Sequence Execution, Retry/Skip Button Clicking, and Loop Counting."
    )
    parser.add_argument("sequences", nargs="*", default=[], help="Path(s) or name(s) of recording JSON file(s) (e.g. combo_phase1 combo_phase2)")
    parser.add_argument("--phase1", "--p1", type=str, default=None, help="Phase 1 recording name/path")
    parser.add_argument("--phase2", "--p2", type=str, default=None, help="Phase 2 recording name/path")
    parser.add_argument("--all", action="store_true", help="Monitor and match ALL recorded sequences in data/recordings/")
    parser.add_argument("--trigger", type=str, default=None, help="Override start trigger image path (single sequence mode)")
    parser.add_argument("--threshold", type=float, default=0.80, help="Template match threshold (default: 0.80)")
    parser.add_argument("--fps", type=float, default=60.0, help="Screen scan rate (checks/sec, default: 60)")
    parser.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier (e.g. 1.0, 1.5, 2.0)")

    # Retry and Skip automation options
    parser.add_argument("--retry-img", type=str, default=str(DEFAULT_RETRY_IMG), help="Path to Retry button image (default: data/retry.png)")
    parser.add_argument("--skip-img", type=str, default=str(DEFAULT_SKIP_IMG), help="Path to Skip button image (default: data/skip.png)")
    parser.add_argument("--retry-coords", type=str, default="1154,847", help="Click coordinates for Retry as X,Y (default: 1154,847)")
    parser.add_argument("--skip-coords", type=str, default="1802,1028", help="Click coordinates for Skip as X,Y (default: 1802,1028)")

    # Loop options (default is continuous looping!)
    parser.add_argument("--one-shot", action="store_true", help="Run once and stop (disables continuous looping)")
    parser.add_argument("--cooldown", type=float, default=0.5, help="Brief cooldown between loops (seconds, default: 0.5)")

    parser.add_argument("--monitor", type=int, default=1, help="Monitor index to capture (default 1 = primary)")
    parser.add_argument("--mouse-mode", type=str, choices=["relative", "absolute"], default="relative",
                        help="Mouse replay mode: 'relative' (default, 3D raw motion) or 'absolute' (desktop UI)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate trigger matching and timing without sending inputs")
    args = parser.parse_args()

    # Determine sequence targets
    target_sequences = []
    if args.phase1 and args.phase2:
        target_sequences = [args.phase1, args.phase2]
    elif args.sequences:
        target_sequences = args.sequences
    elif not args.all:
        # Check priority pairs in data/recordings/
        candidates = [
            ("1.yml", "2.yml"),
            ("1.json", "2.json"),
            ("combo_phase1.yml", "combo_phase2.yml"),
            ("combo_phase1.json", "combo_phase2.json"),
            ("phase1.yml", "phase2.yml"),
            ("phase1.json", "phase2.json"),
            ("combo_1.yml", "combo_2.yml"),
            ("combo_1.json", "combo_2.json"),
        ]
        found = False
        for f1, f2 in candidates:
            p1 = RECORDINGS_DIR / f1
            p2 = RECORDINGS_DIR / f2
            if p1.is_file() and p2.is_file():
                target_sequences = [str(p1), str(p2)]
                found = True
                break

        if not found:
            for single in ["1.yml", "1.json", "combo_phase1.yml", "combo_phase1.json", "phase1.yml", "phase1.json", "combo_1.yml", "combo_1.json"]:
                p = RECORDINGS_DIR / single
                if p.is_file():
                    target_sequences = [str(p)]
                    break

        if not target_sequences:
            parser.error("Please specify sequence file(s) (e.g. 1 2 or combo_phase1 combo_phase2) or use --all.")

    native_requested = args.all and (
        any(RECORDINGS_DIR.glob("*.yml")) or any(RECORDINGS_DIR.glob("*.yaml"))
    )
    native_requested = native_requested or any(
        str(item).lower().endswith((".yml", ".yaml"))
        or (RECORDINGS_DIR / f"{Path(str(item)).name}.yml").is_file()
        or (RECORDINGS_DIR / f"{Path(str(item)).name}.yaml").is_file()
        for item in target_sequences
    )
    if native_requested and not args.dry_run:
        try:
            sudo_warmup()
        except LibinputUnavailableError as exc:
            parser.error(str(exc))

    player = InputPlayer(
        target_path=target_sequences if target_sequences else None,
        all_sequences=args.all,
        trigger_override=Path(args.trigger) if args.trigger else None,
        threshold=args.threshold,
        fps=args.fps,
        speed=args.speed,
        loop=(not args.one_shot),
        cooldown=args.cooldown,
        retry_img=Path(args.retry_img),
        skip_img=Path(args.skip_img),
        retry_coords=parse_coords(args.retry_coords, default=DEFAULT_RETRY_COORDS),
        skip_coords=parse_coords(args.skip_coords, default=DEFAULT_SKIP_COORDS),
        monitor_idx=args.monitor,
        dry_run=args.dry_run,
        mouse_mode=args.mouse_mode,
    )
    player.run()


if __name__ == "__main__":
    main()
