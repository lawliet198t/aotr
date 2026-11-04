#!/usr/bin/env python3
"""
High-Speed 3D Game Input Recorder with:
1. Transparent, click-through screen overlay at the bottom-right corner:
   - BRIGHT GREEN ("● CAPTURING") when recording.
   - BRIGHT RED ("● NOT DETECTED") when waiting/stopped.
2. Continuous re-recording loop:
   - Does NOT terminate when recording stops.
   - Saves the recording atomically to data/recordings/<name>.json.
   - Automatically waits for the START frame again and re-records!
3. Press [Esc] anytime to stop and exit.
"""

import os
import sys
import time
import json
import argparse
import threading
import multiprocessing as mp
import ctypes
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np
from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode

from utils import (
    ensure_directories,
    serialize_key,
    serialize_button,
    ScreenMatcher,
    RawMouseListener,
    START_DIR,
    RECORDINGS_DIR,
    STOP_IMAGE_DEFAULT,
)
from libinput_backend import (
    LibinputUnavailableError,
    choose_devices,
    count_event_frames,
    start_recording as start_libinput_recording,
    stop_recording as stop_libinput_recording,
    sudo_warmup,
)


def _overlay_process_target(state_queue: mp.Queue):
    """
    Runs in a dedicated process to provide a 100% stable, click-through,
    transparent always-on-top status overlay in the bottom-right corner.
    - Bright Red: ● NOT DETECTED (Standby / Waiting)
    - Bright Green: ● CAPTURING (Recording in progress)
    """
    import tkinter as tk

    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.85)

        w, h = 180, 38
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = sw - w - 20
        y = 20
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.configure(bg="#000000")

        frame = tk.Frame(root, bg="#1a0000", highlightthickness=2, highlightbackground="#FF0000")
        frame.pack(expand=True, fill="both")

        label = tk.Label(
            frame,
            text="● NOT DETECTED",
            font=("Ubuntu", 11, "bold"),
            fg="#FF0000",
            bg="#1a0000",
            padx=10,
            pady=4,
        )
        label.pack(expand=True, fill="both")
        root.update_idletasks()

        # Enable X11 click-through (all mouse clicks pass through directly to the game)
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
        root.mainloop()
    except Exception:
        pass


class ScreenOverlay:
    """Controller for the transparent bottom-right HUD overlay."""

    def __init__(self):
        self.queue = mp.Queue()
        self.process = mp.Process(target=_overlay_process_target, args=(self.queue,), daemon=True)

    def start(self):
        try:
            self.process.start()
        except Exception as e:
            print(f"[!] Warning: Could not launch HUD overlay: {e}")

    def set_recording(self, is_recording: bool):
        try:
            self.queue.put_nowait("RECORDING" if is_recording else "STOPPED")
        except Exception:
            pass

    def close(self):
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


class InputRecorder:
    def __init__(
        self,
        name: Optional[str] = None,
        phase: Optional[int] = None,
        start_image_path: Optional[Path] = None,
        stop_image_path: Any = STOP_IMAGE_DEFAULT,
        threshold: float = 0.75,
        fps: float = 60.0,
        stop_delay: float = 0.5,
        monitor_idx: int = 1,
        input_devices: Optional[List[str]] = None,
    ):
        ensure_directories()
        self.phase = phase
        self.threshold = threshold
        self.check_interval = max(0.002, 1.0 / max(1.0, fps))
        self.stop_delay = max(0.0, stop_delay)
        self.monitor_idx = monitor_idx

        # Sequence name & paths
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.name = name or (f"phase{phase}" if phase else f"sequence_{timestamp_str}")
        self.start_image_path = Path(start_image_path) if start_image_path else (START_DIR / f"{self.name}.png")

        # Parse stop image path(s) - supports single Path, string, comma-separated, or list
        if stop_image_path == STOP_IMAGE_DEFAULT and self.phase == 1:
            self.stop_image_paths = [Path("data/skip.png")]
        elif stop_image_path == STOP_IMAGE_DEFAULT and self.phase == 2:
            self.stop_image_paths = [Path("data/retry.png"), STOP_IMAGE_DEFAULT]
        elif isinstance(stop_image_path, (list, tuple)):
            self.stop_image_paths = [Path(p) for p in stop_image_path]
        elif isinstance(stop_image_path, str) and "," in stop_image_path:
            self.stop_image_paths = [Path(p.strip()) for p in stop_image_path.split(",") if p.strip()]
        else:
            self.stop_image_paths = [Path(stop_image_path)]
        self.stop_image_path = self.stop_image_paths[0]

        self.recording_path = RECORDINGS_DIR / f"{self.name}.yml"
        self.metadata_path = RECORDINGS_DIR / f"{self.name}.meta.json"
        self.input_devices = list(input_devices or [])
        self.libinput_process: Optional[subprocess.Popen] = None
        self.partial_recording_path = self.recording_path.with_name(f".{self.name}.partial.yml")

        # Continuous loop tracking
        self.record_count = 0
        self.abort_requested = False

        # State flags for current active recording session
        self.recording_started = False
        self.recording_finished = False
        self.start_time: Optional[float] = None
        self.stop_reason = "Manual / Unknown"

        # Events buffer
        self.events: List[Dict[str, Any]] = []
        self.events_lock = threading.Lock()

        # Listeners and threads
        self.kb_listener: Optional[keyboard.Listener] = None
        self.raw_mouse_listener: Optional[RawMouseListener] = None
        self.pynput_mouse_listener: Optional[mouse.Listener] = None
        self.using_raw_mouse = False

        self.stop_detector_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Screen matcher
        self.matcher = ScreenMatcher(monitor_idx=self.monitor_idx)

        # Transparent HUD Overlay
        self.overlay = ScreenOverlay()

        # Fallback delta tracking for pynput
        self._last_pynput_x: Optional[int] = None
        self._last_pynput_y: Optional[int] = None

    def _log_event(self, event_data: Dict[str, Any]) -> None:
        if not self.recording_started or self.recording_finished:
            return
        with self.events_lock:
            self.events.append(event_data)

    def _trigger_start(self, reason: str = "Screen detection") -> None:
        """Called when start frame matches."""
        if self.recording_started:
            return
        try:
            self.partial_recording_path.unlink(missing_ok=True)
            self.libinput_process = start_libinput_recording(
                self.partial_recording_path, self.input_devices
            )
        except (OSError, LibinputUnavailableError) as exc:
            print(f"\n[!] Could not start libinput recording: {exc}")
            self.abort_requested = True
            self.stop_event.set()
            return
        self.start_time = time.perf_counter()
        self.recording_started = True
        self.recording_finished = False
        self.stop_event.clear()

        # Update HUD to Bright Green
        self.overlay.set_recording(True)

        print(f"\n[!] >>> RECORDING STARTED! <<< ({reason})")

        # Launch background stop image detector
        self.stop_detector_thread = threading.Thread(
            target=self._stop_detection_loop, daemon=True, name="StopDetector"
        )
        self.stop_detector_thread.start()

    def _stop_detection_loop(self) -> None:
        """Background loop watching for stop templates (e.g. skip.png, retry.png) at high frequency."""
        stop_templates: List[Tuple[str, np.ndarray]] = []
        for p in self.stop_image_paths:
            if not p.is_file():
                continue
            tpl = self.matcher.load_template_gray(p)
            if tpl is not None:
                stop_templates.append((p.name, tpl))

        if not stop_templates:
            print(f"[!] Warning: None of the specified stop images exist on disk: {[str(p) for p in self.stop_image_paths]}")
            print("    Recording will continue until you press [Esc] or [F12] to stop manually.")
            return

        # Brief delay to allow initial screen state to change before checking stop
        if self.stop_delay > 0:
            time.sleep(self.stop_delay)

        while not self.stop_event.is_set() and not self.abort_requested:
            screen_gray = self.matcher.grab_gray()
            for name, stop_template in stop_templates:
                matched, confidence, loc = self.matcher.match(
                    stop_template, screen_gray=screen_gray, threshold=self.threshold
                )
                if matched:
                    print(f"\n[!] >>> STOP FRAME DETECTED ({name})! <<<")
                    print(f"    Confidence: {confidence:.4f} >= {self.threshold:.2f} at screen pos {loc}")
                    self.stop_reason = f"Image match {name} ({confidence:.3f})"
                    self.stop_event.set()
                    return

            time.sleep(self.check_interval)

    # ---------------- Keyboard Callbacks ----------------
    def on_key_press(self, key: Any) -> None:
        if not self.recording_started:
            if key == Key.esc:
                print("\n[x] Cancelled before recording started.")
                self.abort_requested = True
                self.stop_event.set()
                return False
            return

        # Emergency / manual stop keys while recording
        if key == Key.esc:
            print(f"\n[!] [Esc] pressed. Stopping recorder and exiting...")
            self.stop_reason = "Manual exit key (Esc)"
            self.abort_requested = True
            self.stop_event.set()
            return False

        if key == Key.f12:
            print(f"\n[!] [F12] pressed. Ending current recording session...")
            self.stop_reason = "Manual stop key (F12)"
            self.stop_event.set()
            return

        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "key_press",
            "key": serialize_key(key),
        })

    def on_key_release(self, key: Any) -> None:
        if not self.recording_started or self.recording_finished:
            return

        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "key_release",
            "key": serialize_key(key),
        })

    # ---------------- Raw XInput2 Mouse Callbacks ----------------
    def on_raw_mouse_move(self, dx: float, dy: float) -> None:
        if not self.recording_started or self.recording_finished:
            return
        if dx == 0.0 and dy == 0.0:
            return

        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "mouse_move",
            "dx": round(dx, 2),
            "dy": round(dy, 2),
        })

    def on_raw_mouse_click(self, button_name: str, pressed: bool) -> None:
        if not self.recording_started or self.recording_finished:
            return
        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "mouse_click",
            "button": serialize_button(button_name),
            "pressed": bool(pressed),
        })

    def on_raw_mouse_scroll(self, dx: int, dy: int) -> None:
        if not self.recording_started or self.recording_finished:
            return
        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "mouse_scroll",
            "dx": int(dx),
            "dy": int(dy),
        })

    # ---------------- Fallback Pynput Mouse Callbacks ----------------
    def on_pynput_mouse_move(self, x: int, y: int) -> None:
        if not self.recording_started or self.recording_finished:
            return
        elapsed = time.perf_counter() - self.start_time
        ix, iy = int(x), int(y)

        if self._last_pynput_x is not None:
            dx = ix - self._last_pynput_x
            dy = iy - self._last_pynput_y
            if dx != 0 or dy != 0:
                self._log_event({
                    "time": round(elapsed, 6),
                    "type": "mouse_move",
                    "dx": dx,
                    "dy": dy,
                })
        self._last_pynput_x = ix
        self._last_pynput_y = iy

    def on_pynput_mouse_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        if not self.recording_started or self.recording_finished:
            return
        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "mouse_click",
            "button": serialize_button(button),
            "pressed": bool(pressed),
        })

    def on_pynput_mouse_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self.recording_started or self.recording_finished:
            return
        elapsed = time.perf_counter() - self.start_time
        self._log_event({
            "time": round(elapsed, 6),
            "type": "mouse_scroll",
            "dx": int(dx),
            "dy": int(dy),
        })

    def finish_recording(self) -> None:
        """Finalize a native libinput recording and save trigger metadata."""
        if self.recording_finished:
            return
        self.recording_finished = True
        self.stop_event.set()
        process = self.libinput_process
        self.libinput_process = None
        stop_libinput_recording(process)

        # Update HUD back to Bright Red
        self.overlay.set_recording(False)

        total_duration = 0.0
        if self.start_time:
            total_duration = time.perf_counter() - self.start_time

        frame_count = count_event_frames(self.partial_recording_path)
        if frame_count < 10:
            print(
                f"[!] Incomplete capture ({frame_count} event frames); keeping any existing "
                f"recording and discarding this partial take."
            )
            self.partial_recording_path.unlink(missing_ok=True)
            return

        temp_path = self.metadata_path.with_suffix(".tmp")
        try:
            self.partial_recording_path.replace(self.recording_path)
            metadata = {
                "name": self.name,
                "created_at": datetime.now().isoformat(),
                "duration_seconds": round(total_duration, 4),
                "event_frames": frame_count,
                "stop_reason": self.stop_reason,
                "start_image": str(self.start_image_path),
                "stop_images": [str(p) for p in self.stop_image_paths],
                "input_devices": self.input_devices,
                "mouse_mode": "kernel relative events",
            }
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_path.replace(self.metadata_path)
        except Exception as e:
            print(f"[!] Error finalizing libinput recording: {e}")
            if temp_path.is_file():
                temp_path.rename(self.metadata_path)
            return

        self.record_count += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        print("\n" + "=" * 60)
        print(f"       RECORDING #{self.record_count} COMPLETED AND SAVED ({now_str})")
        print("=" * 60)
        print(f"  Sequence Name  : {self.name}")
        print(f"  Duration       : {total_duration:.2f} seconds")
        print(f"  Event Frames   : {frame_count}")
        print(f"  Stop Trigger   : {self.stop_reason}")
        print(f"  Input Recording: {self.recording_path}")
        print("=" * 60)
        print(" >> Standby... Watching screen for next START frame to re-record!")
        print(" >> (HUD is RED: NOT DETECTED. Press [Esc] to exit).")
        print("-" * 60)

    def _load_start_templates(self) -> Dict[str, np.ndarray]:
        """Loads primary start template(s) from file or directory (e.g. data/start/1/)."""
        templates = {}

        # 1. If start_image_path is a directory, load all image files inside it
        if self.start_image_path.is_dir():
            for p in sorted(self.start_image_path.iterdir()):
                if p.suffix.lower() in [".png", ".jpg", ".jpeg"] and p.is_file():
                    tmpl = self.matcher.load_template_gray(p)
                    if tmpl is not None:
                        templates[f"{self.start_image_path.name}/{p.name}"] = tmpl
        elif self.start_image_path.is_file():
            tmpl = self.matcher.load_template_gray(self.start_image_path)
            if tmpl is not None:
                templates[self.start_image_path.name] = tmpl

        # 2. If phase is specified, check data/start/<phase>/
        if self.phase:
            phase_dir = START_DIR / str(self.phase)
            if phase_dir.is_dir():
                for p in sorted(phase_dir.iterdir()):
                    if p.suffix.lower() in [".png", ".jpg", ".jpeg"] and p.is_file():
                        key = f"{phase_dir.name}/{p.name}"
                        if key not in templates:
                            tmpl = self.matcher.load_template_gray(p)
                            if tmpl is not None:
                                templates[key] = tmpl

        # 3. Check for name variations in START_DIR (e.g. combo_1_*.png)
        for p in sorted(START_DIR.glob(f"{self.name}*.png")):
            if p.is_file() and p.name not in templates:
                tmpl = self.matcher.load_template_gray(p)
                if tmpl is not None:
                    templates[p.name] = tmpl

        return templates

    def run(self) -> None:
        """Continuous automated recording loop with transparent HUD overlay."""
        stop_targets_str = ", ".join(p.name for p in self.stop_image_paths)
        print("=" * 60)
        print(" 3D GAME INPUT RECORDER (CONTINUOUS RE-RECORDING + HUD)")
        print("=" * 60)
        print(f" Target Sequence Name : {self.name}")
        print(f" Start Image Target   : {self.start_image_path.name}")
        print(f" Stop Image Target(s) : {stop_targets_str}")
        print(f" Match Threshold      : {self.threshold:.2f}")
        print(f" HUD Overlay          : Top-Right (Click-Through)")
        print(f"   - Bright Red       : ● NOT DETECTED / STANDBY")
        print(f"   - Bright Green     : ● CAPTURING / RECORDING")
        print(f" Mode                 : Continuous (Re-records on each detection)")
        print(f" Emergency Exit       : Press [Esc] anytime to stop")
        print("-" * 60)

        # Launch HUD Overlay
        self.overlay.start()
        self.overlay.set_recording(False)

        # Start input listeners
        self.kb_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release
        )
        self.kb_listener.start()

        print("[+] Mouse and keyboard capture: libinput kernel event recorder.")
        print(f"[+] Devices: {', '.join(self.input_devices)}")

        # Load start templates
        start_templates = self._load_start_templates()
        if start_templates:
            print(f"[+] Loaded {len(start_templates)} start template(s): {', '.join(start_templates.keys())}")
            print(" >> SCANNING SCREEN: Recording will START automatically when start frame appears.")
        else:
            print(f"[!] Warning: No start trigger images found in '{self.start_image_path}'.")
            print(" >> Please place your start trigger frames in data/start/1/ or data/start/2/.")
        print("-" * 60)

        try:
            # Main Continuous Re-recording Loop
            while not self.abort_requested:
                # -------------------------------------------------------------
                # Phase 1: Wait for START frame (HUD: Red "● NOT DETECTED")
                # -------------------------------------------------------------
                self.recording_started = False
                self.recording_finished = False
                self.stop_event.clear()
                with self.events_lock:
                    self.events.clear()

                self.overlay.set_recording(False)
                peak_score = 0.0
                last_log_time = time.perf_counter()
                frames_scanned = 0
                fps_timer = time.perf_counter()
                current_fps = 0.0

                while not self.recording_started and not self.abort_requested:
                    if start_templates:
                        screen_gray = self.matcher.grab_gray()
                        best_match_name = None
                        best_match_score = -1.0

                        for name, tmpl in start_templates.items():
                            is_match, score, loc = self.matcher.match(
                                tmpl, screen_gray=screen_gray, threshold=self.threshold
                            )
                            if score > best_match_score:
                                best_match_score = score
                                best_match_name = name

                        if best_match_score > peak_score:
                            peak_score = best_match_score

                        frames_scanned += 1
                        now = time.perf_counter()
                        if now - fps_timer >= 1.0:
                            current_fps = frames_scanned / (now - fps_timer)
                            frames_scanned = 0
                            fps_timer = now

                        # Check start trigger
                        if best_match_score >= self.threshold:
                            sys.stdout.write(f"\r >> [SCANNING] Match: {best_match_score:.3f} | Peak: {peak_score:.3f} | Target: >={self.threshold:.2f}\n")
                            sys.stdout.flush()
                            print(f"\n[!] >>> START FRAME DETECTED! <<<")
                            print(f"    Matched: {best_match_name} (Confidence: {best_match_score:.4f} >= {self.threshold:.2f})")
                            self._trigger_start(reason=f"Start frame match '{best_match_name}' ({best_match_score:.3f})")
                            break

                        # Terminal live readout every 150ms
                        if now - last_log_time >= 0.15:
                            last_log_time = now
                            fps_str = f"{current_fps:.0f} FPS" if current_fps > 0 else "Scanning..."
                            rec_str = f"Runs: {self.record_count}"
                            sys.stdout.write(
                                f"\r >> [SCANNING FOR START] Current: {max(0.0, best_match_score):.3f} | Peak: {peak_score:.3f} | Target: >={self.threshold:.2f} ({fps_str}, {rec_str})   "
                            )
                            sys.stdout.flush()

                    time.sleep(self.check_interval)

                if self.abort_requested:
                    break

                # -------------------------------------------------------------
                # Phase 2: Actively Recording (HUD: Green "● CAPTURING")
                # -------------------------------------------------------------
                while self.recording_started and not self.stop_event.is_set() and not self.abort_requested:
                    time.sleep(0.02)

                if self.abort_requested:
                    if self.recording_started:
                        self.finish_recording()
                    break

                # -------------------------------------------------------------
                # Phase 3: Stop Detected -> Save recording and prepare next run
                # -------------------------------------------------------------
                self.finish_recording()

                # Brief cooldown (e.g. 1.0s) so stop screen clears before re-detecting start
                time.sleep(1.0)

        except KeyboardInterrupt:
            print("\n[!] Ctrl+C detected. Exiting safely...")
        finally:
            if self.libinput_process:
                stop_libinput_recording(self.libinput_process)
                self.libinput_process = None
            self.overlay.close()
            if self.kb_listener and self.kb_listener.is_alive():
                try:
                    self.kb_listener.stop()
                except Exception:
                    pass
            if self.raw_mouse_listener:
                try:
                    self.raw_mouse_listener.stop()
                except Exception:
                    pass
            if self.pynput_mouse_listener and self.pynput_mouse_listener.is_alive():
                try:
                    self.pynput_mouse_listener.stop()
                except Exception:
                    pass
            self.matcher.close()
            print("\n" + "=" * 60)
            print("                 RECORDER CLOSED")
            print("=" * 60)
            print(f"  Total Recording Sessions Saved: {self.record_count}")
            print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Continuous 3D Game Input Recorder with Transparent Screen HUD Overlay."
    )
    parser.add_argument("--name", type=str, default=None, help="Sequence name (e.g. combo_phase1)")
    parser.add_argument("--phase", type=int, choices=[1, 2], default=None,
                        help="Phase selector: 1 = Phase 1 (stops on skip.png), 2 = Phase 2 (stops on retry.png)")
    parser.add_argument("--start-img", type=str, default=None, help="Path to start trigger image (default: data/start/<name>.png)")
    parser.add_argument("--stop-img", type=str, default=None,
                        help="Path to stop trigger image (or comma-separated paths). Defaults to skip.png for phase 1, retry.png for phase 2")
    parser.add_argument("--threshold", type=float, default=0.75, help="Template match threshold (default: 0.75)")
    parser.add_argument("--fps", type=float, default=60.0, help="Screen scan rate (checks/sec, default: 60)")
    parser.add_argument("--stop-delay", type=float, default=0.5, help="Seconds to wait after start before checking stop condition (default: 0.5)")
    parser.add_argument("--monitor", type=int, default=1, help="Monitor index to capture (default: 1 = primary)")
    parser.add_argument("--event-device", action="append", dest="input_devices", default=None,
                        help="evdev device path to record (repeat for mouse and keyboard); otherwise select detected devices")
    args = parser.parse_args()

    try:
        sudo_warmup()
        input_devices = choose_devices(args.input_devices)
    except LibinputUnavailableError as exc:
        parser.error(str(exc))

    # Automatic phase defaults
    seq_name = args.name
    start_img = Path(args.start_img) if args.start_img else None
    stop_imgs: List[Path] = []

    if args.stop_img:
        for p in args.stop_img.split(","):
            if p.strip():
                stop_imgs.append(Path(p.strip()))
    elif args.phase == 1:
        seq_name = seq_name or "1"
        if not start_img and (START_DIR / "1").is_dir():
            start_img = START_DIR / "1"
        stop_imgs = [Path("data/skip.png")]
    elif args.phase == 2:
        seq_name = seq_name or "2"
        if not start_img and (START_DIR / "2").is_dir():
            start_img = START_DIR / "2"
        stop_imgs = [Path("data/retry.png"), STOP_IMAGE_DEFAULT]
    else:
        # Default behavior
        stop_imgs = [STOP_IMAGE_DEFAULT]

    recorder = InputRecorder(
        name=seq_name,
        phase=args.phase,
        start_image_path=start_img,
        stop_image_path=stop_imgs,
        threshold=args.threshold,
        fps=args.fps,
        stop_delay=args.stop_delay,
        monitor_idx=args.monitor,
        input_devices=input_devices,
    )
    recorder.run()


if __name__ == "__main__":
    main()
