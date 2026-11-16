#!/usr/bin/env python3
"""Headless two-phase AOTR player with live screen detection and automation."""

from __future__ import annotations

import argparse
import os
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Keep image matching from taking every available CPU core away from Sober.
cv2.setNumThreads(2)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import read_macro  # noqa: E402
from x11_click import XTestClicker  # noqa: E402
from sway_seat import SwaySeat  # noqa: E402

DEFAULTS = {
    "p1": ROOT / "data/recordings/1.yml",
    "p2": ROOT / "data/recordings/2.yml",
    "p1_templates": ROOT / "data/start/1",
    "p2_templates": ROOT / "data/start/2",
    "skip": ROOT / "data/skip.png",
    "retry": ROOT / "data/retry.png",
    "heal": ROOT / "data/heal.png",
    "completed": ROOT / "data/completed.png",
    "failed": ROOT / "data/failed.png",
}

KEY_2 = 3


def load_gray(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Cannot load image: {path}")
    return image


def match_template(screen: np.ndarray, template: np.ndarray,
                   threshold: float, region: tuple[int, int, int, int] | None = None
                   ) -> tuple[bool, float]:
    """Use the same downsampled scene/HUD and UI matching as play.py."""
    source = screen
    th, tw = template.shape[:2]
    if region is not None:
        x1, y1, x2, y2 = region
        h, w = source.shape[:2]
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
        if x2 - x1 >= tw and y2 - y1 >= th:
            source = source[y1:y2, x1:x2]

    sh, sw = source.shape[:2]
    if th > 300 and tw > 400:
        scaled_screen = cv2.resize(source, (480, 270), interpolation=cv2.INTER_AREA)
        scaled_template = cv2.resize(template, (480, 270), interpolation=cv2.INTER_AREA)
        whole = cv2.matchTemplate(scaled_screen, scaled_template, cv2.TM_CCOEFF_NORMED)
        scene_score = cv2.minMaxLoc(whole)[1]
        x1, x2, y2 = int(480 * .40), int(480 * .84), int(270 * .25)
        hud = cv2.matchTemplate(scaled_screen[:y2, x1:x2],
                                scaled_template[:y2, x1:x2], cv2.TM_CCOEFF_NORMED)[0, 0]
        score = float(max(scene_score, hud))
        return score >= threshold, score

    if th > sh or tw > sw:
        template = cv2.resize(template, (min(tw, sw), min(th, sh)))
        th, tw = template.shape[:2]
    if (sh >= 720 and sw >= 1280 or region is not None) and th >= 16 and tw >= 16:
        small_screen = cv2.resize(source, (sw // 2, sh // 2), interpolation=cv2.INTER_AREA)
        small_template = cv2.resize(template, (max(8, tw // 2), max(8, th // 2)),
                                    interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(small_screen, small_template, cv2.TM_CCOEFF_NORMED)
    else:
        result = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
    score = float(cv2.minMaxLoc(result)[1])
    return score >= threshold, score


class RFBScreen:
    """Persistent localhost RFB connection for framebuffer capture."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5900):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.sock.settimeout(3)
        self.buffer = bytearray()
        self.width = self.height = 0
        self.pixels: np.ndarray | None = None
        self.gray: np.ndarray | None = None
        self.capture_interval = 1.0 / 60.0
        self.next_request_at = 0.0
        self.request_pending = False
        self._handshake()

    def _read_exact(self, count: int, timeout: float | None = None) -> bytes | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self.buffer) < count:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                readable, _, _ = select.select([self.sock], [], [], remaining)
                if not readable:
                    return None
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise ConnectionError("WayVNC closed the automation connection")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def _send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def _handshake(self) -> None:
        version = self._read_exact(12)
        if version is None or not version.startswith(b"RFB 003."):
            raise RuntimeError("WayVNC returned an invalid RFB version")
        self._send(b"RFB 003.008\n")
        count = self._read_exact(1)
        if count is None or count[0] == 0:
            raise RuntimeError("WayVNC rejected the RFB connection")
        security = self._read_exact(count[0])
        if security is None or 1 not in security:
            raise RuntimeError("Automation requires the localhost no-auth WayVNC mode")
        self._send(b"\x01")
        result = self._read_exact(4)
        if result is None or struct.unpack(">I", result)[0] != 0:
            raise RuntimeError("WayVNC rejected the RFB session")
        self._send(b"\x01")
        init = self._read_exact(24)
        if init is None:
            raise RuntimeError("WayVNC ended during framebuffer setup")
        self.width, self.height = struct.unpack(">HH", init[:4])
        name_size = struct.unpack(">I", init[20:24])[0]
        if (self.width, self.height) != (1920, 1080):
            raise RuntimeError(f"Expected 1920x1080; WayVNC reports {self.width}x{self.height}")
        if self._read_exact(name_size) is None:
            raise RuntimeError("WayVNC ended while sending its desktop name")

        # RGB565 cuts each raw framebuffer update in half versus XRGB8888.
        pixel_format = struct.pack(">BBBBHHHBBB3x", 16, 16, 0, 1,
                                   31, 63, 31, 11, 5, 0)
        self._send(b"\x00\x00\x00\x00" + pixel_format)
        self._send(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", 0))  # Raw encoding
        self._request_frame(False)
        self.request_pending = True

    def _request_frame(self, incremental: bool) -> None:
        self._send(struct.pack(">BBHHHH", 3, int(incremental), 0, 0,
                               self.width, self.height))
        self.request_pending = True

    def next_frame(self, timeout: float = .01) -> np.ndarray | None:
        if not self.request_pending:
            delay = self.next_request_at - time.monotonic()
            if delay > timeout:
                if timeout > 0:
                    time.sleep(timeout)
                return None
            if delay > 0:
                time.sleep(delay)
            self._request_frame(True)
        kind = self._read_exact(1, timeout)
        if kind is None:
            return None
        if kind[0] == 2:  # Bell
            return None
        if kind[0] == 3:  # ServerCutText
            header = self._read_exact(7)
            if header is None:
                raise ConnectionError("Truncated WayVNC cut-text message")
            size = struct.unpack(">I", header[3:])[0]
            self._read_exact(size)
            return None
        if kind[0] != 0:
            raise RuntimeError(f"Unsupported WayVNC server message {kind[0]}")

        update = self._read_exact(3)
        if update is None:
            raise ConnectionError("Truncated WayVNC framebuffer update")
        rect_count = struct.unpack(">H", update[1:])[0]
        for _ in range(rect_count):
            header = self._read_exact(12)
            if header is None:
                raise ConnectionError("Truncated WayVNC rectangle header")
            x, y, w, h, encoding = struct.unpack(">HHHHi", header)
            if encoding != 0:
                raise RuntimeError(f"WayVNC sent unrequested framebuffer encoding {encoding}")
            raw = self._read_exact(w * h * 2)
            if raw is None:
                raise ConnectionError("Truncated WayVNC raw framebuffer rectangle")
            if self.pixels is None:
                self.pixels = np.zeros((self.height, self.width), dtype=np.uint16)
            rect = np.frombuffer(raw, dtype="<u2").reshape((h, w))
            self.pixels[y:y + h, x:x + w] = rect
            if self.gray is None:
                self.gray = np.zeros((self.height, self.width), dtype=np.uint8)
            blue = ((rect & 0x1F) * 255 // 31).astype(np.uint8)
            green = (((rect >> 5) & 0x3F) * 255 // 63).astype(np.uint8)
            red = (((rect >> 11) & 0x1F) * 255 // 31).astype(np.uint8)
            bgr = cv2.merge((blue, green, red))
            self.gray[y:y + h, x:x + w] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        if self.pixels is None or self.gray is None:
            raise RuntimeError("WayVNC returned an empty initial framebuffer")
        self.request_pending = False
        self.next_request_at = time.monotonic() + self.capture_interval
        return self.gray

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@dataclass
class MacroRun:
    sock: socket.socket
    started: float
    duration: float
    name: str


class Automation:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.vnc = RFBScreen()
        if not args.sway_socket:
            self.vnc.close()
            raise ValueError("Sway IPC socket is missing; launch automation through aotr-headless")
        self.sway_seat = SwaySeat(args.sway_socket)
        self.clicker = XTestClicker()
        self.input_path = Path(args.input_socket)
        self.templates = {
            "p1": [(p.name, load_gray(p)) for p in sorted(Path(args.phase1_images).glob("*"))
                   if p.suffix.lower() in {".png", ".jpg", ".jpeg"}],
            "p2": [(p.name, load_gray(p)) for p in sorted(Path(args.phase2_images).glob("*"))
                   if p.suffix.lower() in {".png", ".jpg", ".jpeg"}],
        }
        if not self.templates["p1"] or not self.templates["p2"]:
            raise ValueError("Both phase trigger directories must contain images")
        self.images = {name: load_gray(Path(getattr(args, f"{name}_image")))
                       for name in ("skip", "retry", "heal", "completed", "failed")}
        for path in (args.phase1, args.phase2):
            if not Path(path).is_file():
                raise ValueError(f"Macro file does not exist: {path}")
        self.macros = {}
        for label, path in (("1", Path(args.phase1)), ("2", Path(args.phase2))):
            self.macros[label] = read_macro(path, args.speed)
        self.frame: np.ndarray | None = None
        self.state = "WAIT_P1"
        self.run: MacroRun | None = None
        self.skip_stop_armed = True
        self.last_click_log_at = 0.0
        self.loop = 0
        self.loop_started = time.monotonic()
        self.phase1_duration = 0.0
        self.phase2_duration = 0.0
        self.result = "UNKNOWN"
        self.abort = False
        self.skip_visible = self.retry_visible = self.heal_visible = False
        self.retry_position = (1154, 847)
        self.retry_handled = False
        self.skip_since = self.retry_since = None
        self.retry_clear_since = None
        self.next_skip_click = self.next_retry_click = 0.0
        self.next_heal_click = 0.0
        self.heal_actions: list[tuple[int, int]] = []
        self.heal_actions_done = False
        self.ui_clear_since: float | None = None
        self.phase_fallback: float | None = None
        self.next_key2 = None
        self.mission_tracking = False
        self.result_scores = {"COMPLETED": 0.0, "FAILED": 0.0}
        self.last_progress = 0.0
        self.loops: list[tuple[int, str, float]] = []

    def _button_match(self, label: str, x: int, y: int
                      ) -> tuple[bool, float, tuple[int, int] | None]:
        h, w = self.frame.shape[:2]
        x1, y1 = max(0, x - 320), max(0, y - 220)
        x2, y2 = min(w, x + 320), min(h, y + 220)
        matched, score = match_template(
            self.frame, self.images[label], self.args.threshold, (x1, y1, x2, y2))

        # RFB mouse positions use framebuffer coordinates. Find the matched
        # button center in that same framebuffer, rather than assuming the
        # source game's desktop coordinates still describe this sandbox.
        crop = self.frame[y1:y2, x1:x2]
        template = self.images[label]
        ch, cw = crop.shape[:2]
        th, tw = template.shape[:2]
        if ch < th or cw < tw:
            return matched, score, None
        small_crop = cv2.resize(crop, (cw // 2, ch // 2), interpolation=cv2.INTER_AREA)
        small_template = cv2.resize(template, (max(8, tw // 2), max(8, th // 2)),
                                    interpolation=cv2.INTER_AREA)
        if (small_template.shape[0] > small_crop.shape[0]
                or small_template.shape[1] > small_crop.shape[1]):
            return matched, score, None
        result = cv2.matchTemplate(small_crop, small_template, cv2.TM_CCOEFF_NORMED)
        _min_score, _max_score, _min_loc, max_loc = cv2.minMaxLoc(result)
        center_x = x1 + (max_loc[0] + small_template.shape[1] / 2) * 2
        center_y = y1 + (max_loc[1] + small_template.shape[0] / 2) * 2
        return matched, score, (round(center_x), round(center_y))

    def _phase_match(self, phase: str, threshold: float) -> tuple[str | None, float]:
        best_name, best_score = None, 0.0
        for name, template in self.templates[phase]:
            matched, score = match_template(self.frame, template, threshold)
            if score > best_score:
                best_name, best_score = name, score
            if matched:
                return name, score
        return None, best_score

    def _classify_result(self, force: bool = False) -> None:
        if self.frame is None or (not self.mission_tracking and not force):
            return
        for result in ("COMPLETED", "FAILED"):
            _, score = match_template(self.frame, self.images[result.lower()], 0.0)
            self.result_scores[result] = max(self.result_scores[result], score)
        result = max(self.result_scores, key=self.result_scores.get)
        score = self.result_scores[result]
        if force or score >= self.args.threshold:
            self.result = result

    def _start_macro(self, name: str) -> None:
        payload, _frames, _events, duration = self.macros[name]
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(self.input_path))
        client.sendall(payload)
        client.settimeout(3)
        response = bytearray()
        while not response.endswith(b"\n"):
            block = client.recv(1)
            if not block:
                raise RuntimeError("Input service closed before starting the macro")
            response.extend(block)
        status = response.decode().strip()
        if status != "READY":
            raise RuntimeError(f"Input service rejected macro {name}: {status}")
        client.setblocking(False)
        self.run = MacroRun(client, time.monotonic(), duration, name)
        self.state = f"PLAY_{name}"
        # The Skip that caused the one-second fallback is still the same UI
        # event; don't treat it as a fresh stop signal for the macro we just
        # launched. Arm stop-on-Skip only after that button clears.
        self.skip_stop_armed = not self.skip_visible
        self.phase_fallback = None
        if name == "1":
            self.mission_tracking = True
            self.result = "UNKNOWN"
            self.result_scores = {"COMPLETED": 0.0, "FAILED": 0.0}
            self.phase1_duration = 0.0
            self.phase2_duration = 0.0
            self.next_key2 = None
        print(f"\n[PHASE {name}] Playing macro {name} ({duration:.2f}s, {len(payload)} bytes).", flush=True)

    def _stop_macro(self) -> None:
        if self.run is None:
            return
        run = self.run
        try:
            run.sock.shutdown(socket.SHUT_WR)
            run.sock.setblocking(True)
            run.sock.settimeout(.5)
            response = run.sock.recv(64)
            if response.strip() == b"DONE":
                pass
        except OSError:
            pass
        run.sock.close()
        elapsed = max(0.0, time.monotonic() - run.started)
        if run.name == "1":
            self.phase1_duration = elapsed
        else:
            self.phase2_duration = elapsed
        print(f"[PHASE {run.name}] Stopped by screen automation after {elapsed:.2f}s.", flush=True)
        self.run = None

    def _poll_macro(self) -> None:
        if self.run is None:
            return
        try:
            ready, _, _ = select.select([self.run.sock], [], [], 0)
            if not ready:
                return
            response = self.run.sock.recv(64)
            if response.strip() == b"DONE":
                run = self.run
                elapsed = time.monotonic() - run.started
                if run.name == "1":
                    self.phase1_duration = elapsed
                    self.state = "WAIT_P2"
                    self.next_key2 = time.monotonic() + 5.0
                    print("[PHASE 1] Macro ended; waiting for Phase 2 trigger / Skip.", flush=True)
                else:
                    self.phase2_duration = elapsed
                    self.state = "WAIT_RETRY"
                    print("[PHASE 2] Macro ended; waiting for Retry.", flush=True)
                run.sock.close()
                self.run = None
            elif response.strip() == b"CANCELLED":
                self.run.sock.close()
                self.run = None
        except BlockingIOError:
            return

    def _click(self, x: int, y: int) -> None:
        self.sway_seat.set_pointer_constraints(False)
        self.clicker.click(x, y)
        now = time.monotonic()
        if now - self.last_click_log_at >= 1.0:
            print(f"[input] XTest click sent at ({x}, {y}).", flush=True)
            self.last_click_log_at = now

    def _tap_key2(self) -> None:
        now = time.monotonic()
        if self.run is not None or now < (self.next_key2 or now):
            return
        # The input service schedules a short startup lead-in for each
        # request. Anchor the next tap to this one so that lead-in does not
        # stretch the requested 500 ms cadence.
        self.next_key2 = now + .5
        payload = bytearray(struct.pack("<4sII", b"AOTR", 1, 2))
        payload.extend(struct.pack("<QBBHiii", 0, 1, 0, 1, 1, KEY_2, 1))
        payload.extend(struct.pack("<QBBHiii", 50000, 1, 0, 1, 1, KEY_2, 0))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self.input_path))
            client.sendall(payload)
            client.settimeout(.5)
            if client.recv(6).strip() != b"READY":
                return
            client.recv(64)
        finally:
            client.close()
        print("[PHASE 1] No trigger/Skip yet; tapping 2.", flush=True)

    def _finish_loop(self) -> None:
        # A loop ends only on Retry, whose shared result screen is expected to
        # show either Completed or Failed. Preserve that invariant in logs even
        # when a threshold missed both templates by a small amount.
        self._classify_result(force=True)
        elapsed = time.monotonic() - self.loop_started
        self.loop += 1
        self.loops.append((self.loop, self.result, elapsed))
        print("\n" + "=" * 58, flush=True)
        print(f"LOOP #{self.loop} {self.result} | {elapsed:.2f}s | phase1 {self.phase1_duration:.2f}s | phase2 {self.phase2_duration:.2f}s", flush=True)
        print("=" * 58, flush=True)
        self.loop_started = time.monotonic()
        self.result = "UNKNOWN"
        self.result_scores = {"COMPLETED": 0.0, "FAILED": 0.0}
        self.mission_tracking = False
        self.state = "WAIT_P1"
        self.phase_fallback = None

    def _screens(self, gray: np.ndarray) -> None:
        self.frame = gray
        now = time.monotonic()
        # Heal takes precedence over Retry on the shared result screen.
        self.heal_visible, heal_score, _heal_position = self._button_match("heal", 1863, 73)
        self.retry_visible, retry_score, retry_position = self._button_match("retry", 1154, 847)
        self.skip_visible, skip_score, _skip_position = self._button_match("skip", 1802, 1028)
        if self.skip_visible or self.retry_visible or self.heal_visible:
            self.ui_clear_since = None
            if not self.sway_seat.constraints_disabled:
                self.sway_seat.set_pointer_constraints(False)
                print("[input] Game pointer capture paused for UI clicks.", flush=True)
        elif self.sway_seat.constraints_disabled:
            if self.ui_clear_since is None:
                self.ui_clear_since = now
            elif now - self.ui_clear_since >= .5:
                self.sway_seat.set_pointer_constraints(True)
                self.ui_clear_since = None
                print("[input] Game pointer capture restored.", flush=True)
        # Result templates are only relevant on the shared Heal/Retry screen.
        # Searching them during gameplay wastes detector time and can retain a
        # false high score from an unrelated scene.
        if self.mission_tracking and (self.retry_visible or self.heal_visible):
            self._classify_result()

        if self.heal_visible and not self.heal_actions_done and not self.heal_actions:
            self.heal_actions = [(668, 890), (668, 890), (1193, 280), (1193, 280)]
            self.next_heal_click = now
            print(f"\n[AUTO] HEAL detected ({heal_score:.3f}); running heal clicks.", flush=True)
        if self.skip_visible and self.skip_since is None:
            self.skip_since = now
            print(f"\n[AUTO] SKIP detected ({skip_score:.3f}).", flush=True)
        elif not self.skip_visible:
            self.skip_since = None
            self.next_skip_click = 0.0
        if self.retry_visible and self.retry_since is None:
            self.retry_since = now
            if retry_position is not None:
                self.retry_position = retry_position
            self.retry_clear_since = None
            print(f"\n[AUTO] RETRY detected ({retry_score:.3f}); fixed target {self.retry_position}, every 100 ms.", flush=True)
        elif self.retry_visible:
            self.retry_clear_since = None
        elif self.retry_since is not None:
            # Keep the click target through brief template-score dips; reset
            # it only after the button has stayed gone for half a second.
            if self.retry_clear_since is None:
                self.retry_clear_since = now
            elif now - self.retry_clear_since >= .5:
                self.retry_since = None
                self.retry_clear_since = None
                self.next_retry_click = 0.0
                self.retry_handled = False

    def _button_actions(self, now: float) -> None:
        """Run click cadences independently of framebuffer damage/update rate."""

        if self.heal_visible:
            if not self.heal_actions_done:
                if now >= self.next_heal_click and self.heal_actions:
                    x, y = self.heal_actions.pop(0)
                    self._click(x, y)
                    self.next_heal_click = now + .5
                    if not self.heal_actions:
                        self.heal_actions_done = True
                        self.next_heal_click = now + .5
            elif not self.retry_visible and now >= self.next_heal_click:
                self._click(1863, 73)
                self.next_heal_click = now + .2
        elif not self.retry_visible:
            self.heal_actions_done = False
            self.heal_actions.clear()

        if self.skip_visible:
            if now >= self.next_skip_click:
                self._click(1802, 1028)
                self.next_skip_click = now + .1

        if self.retry_visible:
            if (now >= self.next_retry_click
                    and (not self.heal_visible or self.heal_actions_done)):
                click_x, click_y = self.retry_position
                self._click(click_x, click_y)
                self.next_retry_click = now + .1
                self._classify_result(force=True)

    def _step(self) -> None:
        now = time.monotonic()
        try:
            frame = self.vnc.next_frame(.005)
        except ConnectionError:
            # Reopen capture if WayVNC drops the framebuffer session instead
            # of aborting the automation loop.
            self.vnc.close()
            self.vnc = RFBScreen()
            self.frame = None
            return
        if frame is not None:
            self._screens(frame)
        if self.frame is None:
            return

        if self.retry_visible:
            if self.run is not None:
                self._stop_macro()
            self._button_actions(time.monotonic())
            if not self.retry_handled:
                self.retry_handled = True
                self._finish_loop()
            return
        if self.retry_since is not None:
            # Wait for a stable disappearance before treating a brief image
            # match dip as a new gameplay phase.
            self._button_actions(time.monotonic())
            return

        if self.run is not None:
            if not self.skip_visible:
                self.skip_stop_armed = True
            if self.skip_visible and self.skip_stop_armed:
                self._stop_macro()
                if self.run is not None:
                    return
                self.state = "WAIT_P2" if self.state == "PLAY_1" else "WAIT_RETRY"
                self.phase_fallback = now + 1.0
            else:
                self._poll_macro()

        # Stop an armed macro on Skip before injecting its absolute click, so
        # relative events from the replay cannot move the pointer off target.
        self._button_actions(time.monotonic())

        if self.state == "WAIT_P1":
            name, score = self._phase_match("p1", self.args.phase1_threshold)
            if name:
                print(f"\n[PHASE 1] Trigger {name} matched ({score:.3f}).", flush=True)
                self._start_macro("1")
                return
            if self.skip_visible and self.phase_fallback is None:
                self.phase_fallback = now + 1.0
            if self.phase_fallback is not None and now >= self.phase_fallback:
                print("[PHASE 1] Skip seen; trigger missed for 1s, starting macro.", flush=True)
                self._start_macro("1")
        elif self.state == "WAIT_P2":
            name, score = self._phase_match("p2", self.args.phase2_threshold)
            if name:
                print(f"\n[PHASE 2] Trigger {name} matched ({score:.3f}).", flush=True)
                self._start_macro("2")
                return
            if self.skip_visible and self.phase_fallback is None:
                self.phase_fallback = now + 1.0
            if self.phase_fallback is not None and now >= self.phase_fallback:
                print("[PHASE 2] Skip seen; trigger missed for 1s, starting macro.", flush=True)
                self._start_macro("2")
                return
            if not self.skip_visible:
                self._tap_key2()

    def run_forever(self) -> None:
        p1_frames = self.macros["1"][1]
        p2_frames = self.macros["2"][1]
        print("=" * 60)
        print(" AOTR HEADLESS TWO-PHASE AUTOMATION")
        print(" Screen detection: RGB565 stream, capped at 60 FPS (output remains 120 Hz)")
        print(f" Phase 1: {self.args.phase1} ({p1_frames} frames, threshold {self.args.phase1_threshold:.2f})")
        print(f" Phase 2: {self.args.phase2} ({p2_frames} frames, threshold {self.args.phase2_threshold:.2f}; locked)")
        print(" Skip/Retry: auto-click every 100ms | Heal: priority sequence, final click every 200ms")
        print(" Phase fallback: start macro 1s after Skip if its trigger image is absent")
        print(" Ctrl+C stops automation and releases macro inputs")
        print("=" * 60, flush=True)
        try:
            while True:
                self._step()
        except KeyboardInterrupt:
            print("\n[!] Stopping headless automation...", flush=True)
        finally:
            self._stop_macro()
            self.clicker.close()
            self.sway_seat.close()
            self.vnc.close()
            print("\nSESSION SUMMARY")
            print(f"Loops completed: {self.loop}")
            for number, result, elapsed in self.loops:
                print(f"  Loop #{number}: {result} ({elapsed:.2f}s)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase1", default=str(DEFAULTS["p1"]))
    parser.add_argument("--phase2", default=str(DEFAULTS["p2"]))
    parser.add_argument("--phase1-images", default=str(DEFAULTS["p1_templates"]))
    parser.add_argument("--phase2-images", default=str(DEFAULTS["p2_templates"]))
    parser.add_argument("--skip-image", dest="skip_image", default=str(DEFAULTS["skip"]))
    parser.add_argument("--retry-image", dest="retry_image", default=str(DEFAULTS["retry"]))
    parser.add_argument("--heal-image", dest="heal_image", default=str(DEFAULTS["heal"]))
    parser.add_argument("--completed-image", dest="completed_image", default=str(DEFAULTS["completed"]))
    parser.add_argument("--failed-image", dest="failed_image", default=str(DEFAULTS["failed"]))
    parser.add_argument("--phase1-threshold", type=float, default=.80)
    parser.add_argument("--phase2-threshold", type=float, default=.70)
    parser.add_argument("--threshold", type=float, default=.80)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--input-socket", default=os.environ.get(
        "AOTR_INPUT_SOCKET", f"/run/user/{os.getuid()}/aotr-headless/input.sock"))
    parser.add_argument("--sway-socket", default=os.environ.get("AOTR_SWAYSOCK", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        Automation(args).run_forever()
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[headless-automation] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
