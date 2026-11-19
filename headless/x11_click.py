#!/usr/bin/env python3
"""Send absolute clicks through XTest to the private XWayland display."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import sys
import time

from sway_seat import SwaySeat


class XTestClicker:
    """Own a separate XTest client, independent of the macro replay socket."""

    def __init__(self):
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise RuntimeError("libX11 and libXtst are required for private XWayland clicks")
        self.x11 = ctypes.CDLL(x11_path)
        self.xtst = ctypes.CDLL(xtst_path)
        self._declare_functions()
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("Could not open private XWayland; DISPLAY is missing or unavailable")
        self.screen = self.x11.XDefaultScreen(self.display)
        self.width = self.x11.XDisplayWidth(self.display, self.screen)
        self.height = self.x11.XDisplayHeight(self.display, self.screen)
        if (self.width, self.height) != (1920, 1080):
            self.close()
            raise RuntimeError(f"Expected private XWayland at 1920x1080; found {self.width}x{self.height}")
        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        major = ctypes.c_int()
        minor = ctypes.c_int()
        if not self.xtst.XTestQueryExtension(
                self.display, ctypes.byref(event_base), ctypes.byref(error_base),
                ctypes.byref(major), ctypes.byref(minor)):
            self.close()
            raise RuntimeError("Private XWayland does not expose the XTest extension")

    def _declare_functions(self) -> None:
        display_ptr = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = display_ptr
        self.x11.XDefaultScreen.argtypes = [display_ptr]
        self.x11.XDefaultScreen.restype = ctypes.c_int
        self.x11.XDisplayWidth.argtypes = [display_ptr, ctypes.c_int]
        self.x11.XDisplayWidth.restype = ctypes.c_int
        self.x11.XDisplayHeight.argtypes = [display_ptr, ctypes.c_int]
        self.x11.XDisplayHeight.restype = ctypes.c_int
        self.x11.XSync.argtypes = [display_ptr, ctypes.c_int]
        self.x11.XSync.restype = ctypes.c_int
        self.x11.XCloseDisplay.argtypes = [display_ptr]
        self.x11.XCloseDisplay.restype = ctypes.c_int
        self.xtst.XTestQueryExtension.argtypes = [
            display_ptr, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        self.xtst.XTestQueryExtension.restype = ctypes.c_int
        self.xtst.XTestFakeMotionEvent.argtypes = [
            display_ptr, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        self.xtst.XTestFakeRelativeMotionEvent.argtypes = [
            display_ptr, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeRelativeMotionEvent.restype = ctypes.c_int
        self.xtst.XTestFakeButtonEvent.argtypes = [
            display_ptr, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.restype = ctypes.c_int

    def click(self, x: int, y: int, hold_seconds: float = .06) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"Coordinates must be inside the {self.width}x{self.height} XWayland screen")
        if not self.xtst.XTestFakeMotionEvent(self.display, self.screen, x, y, 0):
            raise RuntimeError("XTest absolute pointer motion failed")
        self.x11.XSync(self.display, 0)
        time.sleep(.01)
        self.click_current(hold_seconds)

    def move_relative(self, dx: int, dy: int) -> None:
        if not self.xtst.XTestFakeRelativeMotionEvent(self.display, dx, dy, 0):
            raise RuntimeError("XTest relative pointer motion failed")
        self.x11.XSync(self.display, 0)

    def click_current(self, hold_seconds: float = .06) -> None:
        if not self.xtst.XTestFakeButtonEvent(self.display, 1, 1, 0):
            raise RuntimeError("XTest left-button press failed")
        self.x11.XSync(self.display, 0)
        try:
            time.sleep(max(0.0, hold_seconds))
        finally:
            self.xtst.XTestFakeButtonEvent(self.display, 1, 0, 0)
            self.x11.XSync(self.display, 0)

    def close(self) -> None:
        if getattr(self, "display", None):
            self.x11.XCloseDisplay(self.display)
            self.display = None


def click_absolute(x: int, y: int, sway_socket: str | None = None) -> None:
    """Temporarily escape game pointer capture and click private XWayland."""
    seat = SwaySeat(sway_socket) if sway_socket else None
    clicker = None
    try:
        if seat is not None:
            seat.set_pointer_constraints(False)
        clicker = XTestClicker()
        clicker.click(x, y)
    finally:
        if clicker is not None:
            clicker.close()
        if seat is not None:
            seat.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("x", type=int)
    parser.add_argument("y", type=int)
    parser.add_argument("--sway-socket", default=os.environ.get("SWAYSOCK", ""))
    args = parser.parse_args()
    try:
        click_absolute(args.x, args.y, args.sway_socket or None)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Click failed: {exc}", file=sys.stderr)
        return 1
    print(f"Sent one XTest click to the private XWayland session at ({args.x}, {args.y}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
