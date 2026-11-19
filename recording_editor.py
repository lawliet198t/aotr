#!/usr/bin/env python3
"""Portable keyboard-timeline editor for AOTR JSON recordings."""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parent


def key_label(event: dict) -> str:
    key = event.get("key", {})
    value = key.get("value") if isinstance(key, dict) else str(key)
    return str(value or "UNKNOWN").upper()


class RecordingEditor:
    def __init__(self, root: tk.Tk, initial: Path | None = None):
        self.root = root
        self.root.title("AOTR Recording Editor")
        self.root.geometry("1100x720")
        self.path: Path | None = None
        self.document: dict = {"metadata": {}, "events": []}
        self.saved_document = copy.deepcopy(self.document)
        self.undo_stack: list[dict] = []
        self.playing = False
        self.play_start = 0.0
        self.play_end = 0.0
        self.play_clock = 0.0
        self.play_clock_start = 0.0
        self.drag_start: float | None = None
        self.drag_end: float | None = None
        self.duration = 0.0
        self._build_ui()
        if initial:
            self.open_file(initial)

    def _build_ui(self) -> None:
        self.canvas = tk.Canvas(self.root, bg="#151a21", height=330, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.canvas.bind("<Configure>", lambda _event: self.draw_timeline())
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_update)
        self.canvas.bind("<ButtonRelease-1>", self._drag_finish)

        table = ttk.Treeview(self.root, columns=("time", "key", "action"), show="headings", height=10)
        table.heading("time", text="Time (seconds)")
        table.heading("key", text="Keyboard input")
        table.heading("action", text="Action")
        table.column("time", width=120, anchor="w")
        table.column("key", width=160, anchor="w")
        table.column("action", width=120, anchor="w")
        table.pack(fill="both", expand=True, padx=10, pady=4)
        self.table = table

        controls = ttk.Frame(self.root)
        controls.pack(fill="x", padx=10, pady=6)
        ttk.Button(controls, text="Open…", command=self.choose_file).pack(side="left", padx=(0, 5))
        self.play_button = ttk.Button(controls, text="Play", command=self.toggle_play)
        self.play_button.pack(side="left", padx=5)
        ttk.Button(controls, text="Undo cut", command=self.undo).pack(side="left", padx=5)
        ttk.Button(controls, text="Reset to saved", command=self.reset).pack(side="left", padx=5)
        ttk.Label(controls, text="From (s)").pack(side="left", padx=(12, 3))
        self.from_var = tk.StringVar(value="0.000")
        ttk.Entry(controls, textvariable=self.from_var, width=9).pack(side="left")
        ttk.Label(controls, text="To (s)").pack(side="left", padx=(8, 3))
        self.to_var = tk.StringVar(value="0.000")
        ttk.Entry(controls, textvariable=self.to_var, width=9).pack(side="left")
        ttk.Button(controls, text="Cut selection", command=self.cut).pack(side="left", padx=8)
        ttk.Button(controls, text="Save", command=self.save).pack(side="left", padx=5)

        self.status = ttk.Label(self.root, text="Open a JSON recording. Drag the timeline or enter exact cut times.")
        self.status.pack(fill="x", padx=10, pady=(0, 8))

    def choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            initialdir=str(ROOT / "data" / "recordings"),
            filetypes=(("AOTR JSON recordings", "*.json"), ("All files", "*")),
        )
        if selected:
            self.open_file(Path(selected))

    def open_file(self, path: Path) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc.get("events"), list):
                raise ValueError("The file has no events list.")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            messagebox.showerror("Open failed", str(exc))
            return
        self.path = path
        self.document = doc
        self.saved_document = copy.deepcopy(doc)
        self.undo_stack.clear()
        self.playing = False
        self._refresh()

    def _keyboard_events(self) -> list[dict]:
        return [e for e in self.document.get("events", [])
                if e.get("type") in ("key_press", "key_release")]

    def _refresh(self) -> None:
        events = self.document.get("events", [])
        self.duration = max(
            float(self.document.get("metadata", {}).get("duration_seconds", 0.0) or 0.0),
            max((float(e.get("time", 0.0) or 0.0) for e in events), default=0.0),
        )
        self.table.delete(*self.table.get_children())
        for event in self._keyboard_events():
            self.table.insert("", "end", values=(
                f"{float(event.get('time', 0.0)):.6f}", key_label(event),
                "DOWN" if event["type"] == "key_press" else "UP",
            ))
        filename = self.path.name if self.path else "(unsaved)"
        self.root.title(f"AOTR Recording Editor — {filename}")
        self.status.configure(text=f"{filename}  |  {self.duration:.2f}s  |  {len(self._keyboard_events())} keyboard events")
        self.draw_timeline()

    def _scale(self) -> tuple[float, float, float]:
        width = max(200, self.canvas.winfo_width())
        left, right = 90.0, width - 18.0
        return left, right, max(1.0, self.duration)

    def _x_for_time(self, timestamp: float) -> float:
        left, right, duration = self._scale()
        return left + max(0.0, min(duration, timestamp)) / duration * (right - left)

    def _time_for_x(self, x: float) -> float:
        left, right, duration = self._scale()
        return max(0.0, min(duration, (x - left) / max(1.0, right - left) * duration))

    def draw_timeline(self) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete("all")
        left, right, duration = self._scale()
        lanes: dict[str, int] = {}
        for event in self._keyboard_events():
            label = key_label(event)
            if label not in lanes:
                lanes[label] = len(lanes)
        if not lanes:
            lanes["(no keys)"] = 0
        row_h = 27
        for key, row in lanes.items():
            y = 34 + row * row_h
            c.create_text(8, y, text=key, anchor="w", fill="#dce6f1", font=("TkFixedFont", 9))
            c.create_line(left, y + 9, right, y + 9, fill="#303944")
        for i in range(11):
            x = left + (right - left) * i / 10
            c.create_line(x, 18, x, max(45, 36 + len(lanes) * row_h), fill="#53606d")
            c.create_text(x, 13, text=f"{duration * i / 10:.0f}s", fill="#c5d2df", anchor="s")
        for event in self._keyboard_events():
            key = key_label(event)
            row = lanes.get(key, 0)
            x = self._x_for_time(float(event.get("time", 0.0) or 0.0))
            y = 34 + row * row_h
            color = "#d483ff" if event["type"] == "key_press" else "#91b7d7"
            c.create_rectangle(x - 2, y + 2, x + 3, y + 16, fill=color, outline="")
        if self.drag_start is not None and self.drag_end is not None:
            x1, x2 = sorted((self._x_for_time(self.drag_start), self._x_for_time(self.drag_end)))
            c.create_rectangle(x1, 18, x2, max(45, 36 + len(lanes) * row_h),
                               fill="#587697", stipple="gray25", outline="#80bfff")
        if self.playing:
            x = self._x_for_time(self.play_clock)
            c.create_line(x, 18, x, max(45, 36 + len(lanes) * row_h), fill="#ffcc55", width=2)

    def _drag_begin(self, event) -> None:
        self.drag_start = self._time_for_x(event.x)
        self.drag_end = self.drag_start
        self.draw_timeline()

    def _drag_update(self, event) -> None:
        self.drag_end = self._time_for_x(event.x)
        self.draw_timeline()

    def _drag_finish(self, event) -> None:
        self.drag_end = self._time_for_x(event.x)
        a, b = sorted((self.drag_start or 0.0, self.drag_end))
        self.from_var.set(f"{a:.3f}")
        self.to_var.set(f"{b:.3f}")
        self.draw_timeline()

    def toggle_play(self) -> None:
        if self.playing:
            self.playing = False
            self.play_button.configure(text="Play")
            self.draw_timeline()
            return
        try:
            start = max(0.0, float(self.from_var.get()))
            end = min(self.duration, float(self.to_var.get())) if float(self.to_var.get()) > start else self.duration
        except ValueError:
            messagebox.showerror("Invalid time", "Enter numeric start and end times in seconds.")
            return
        self.play_start, self.play_end = start, end
        self.play_clock = start
        self.play_clock_start = time.perf_counter()
        self.playing = True
        self.play_button.configure(text="Stop")
        self._preview_tick()

    def _preview_tick(self) -> None:
        if not self.playing:
            return
        self.play_clock = min(self.play_end, self.play_start + (time.perf_counter() - self.play_clock_start))
        self.draw_timeline()
        if self.play_clock >= self.play_end:
            self.playing = False
            self.play_button.configure(text="Play")
            return
        self.root.after(20, self._preview_tick)

    def cut(self) -> None:
        try:
            start, end = float(self.from_var.get()), float(self.to_var.get())
        except ValueError:
            messagebox.showerror("Invalid range", "Enter numeric start and end times in seconds.")
            return
        if not 0 <= start < end <= self.duration:
            messagebox.showerror("Invalid range", f"Choose a range within 0–{self.duration:.3f} seconds.")
            return
        events = self.document.get("events", [])
        removed_mouse = [e for e in events if start <= float(e.get("time", 0.0) or 0.0) < end
                         and e.get("type") == "mouse_move" and "dx" in e and "dy" in e]
        correction = (sum(float(e.get("dx", 0.0)) for e in removed_mouse),
                      sum(float(e.get("dy", 0.0)) for e in removed_mouse))
        self.undo_stack.append(copy.deepcopy(self.document))
        cut_duration = end - start
        kept = []
        first_later_mouse = None
        for original in events:
            event = copy.deepcopy(original)
            stamp = float(event.get("time", 0.0) or 0.0)
            if start <= stamp < end:
                continue
            if stamp >= end:
                event["time"] = round(stamp - cut_duration, 6)
                if event.get("type") == "mouse_move" and "dx" in event and first_later_mouse is None:
                    first_later_mouse = event
            kept.append(event)
        # Relative mouse motion is additive: carry the removed segment's net
        # delta into the first later motion so the final aim stays aligned.
        if correction != (0.0, 0.0):
            if first_later_mouse is not None:
                first_later_mouse["dx"] = round(float(first_later_mouse.get("dx", 0.0)) + correction[0], 3)
                first_later_mouse["dy"] = round(float(first_later_mouse.get("dy", 0.0)) + correction[1], 3)
            else:
                kept.append({"time": round(start, 6), "type": "mouse_move",
                             "dx": round(correction[0], 3), "dy": round(correction[1], 3)})
        kept.sort(key=lambda event: float(event.get("time", 0.0) or 0.0))
        self.document["events"] = kept
        metadata = self.document.setdefault("metadata", {})
        metadata["duration_seconds"] = round(max(0.0, self.duration - cut_duration), 6)
        self.drag_start = self.drag_end = None
        self._refresh()

    def undo(self) -> None:
        if self.undo_stack:
            self.document = self.undo_stack.pop()
            self._refresh()

    def reset(self) -> None:
        self.document = copy.deepcopy(self.saved_document)
        self.undo_stack.clear()
        self._refresh()

    def save(self) -> None:
        if self.path is None:
            self.choose_file()
            if self.path is None:
                return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(self.document, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.saved_document = copy.deepcopy(self.document)
        self.undo_stack.clear()
        self._refresh()

def main() -> None:
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    root = tk.Tk()
    app = RecordingEditor(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
