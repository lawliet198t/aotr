#!/usr/bin/env python3
"""
Simple real-time mouse coordinate tracker.
Displays live cursor coordinates and prints the position whenever you left-click.
Press Ctrl+C or Esc to exit.
"""

import sys
import time
from pynput import mouse, keyboard


class MouseTracker:
    def __init__(self):
        self.mouse_ctrl = mouse.Controller()
        self.running = True

    def on_click(self, x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            # Print click coordinate on a new line
            sys.stdout.write(f"\n[CLICK] Left Click at: X={int(x):4d}, Y={int(y):4d}\n")
            sys.stdout.flush()

    def on_key_press(self, key):
        if key == keyboard.Key.esc:
            print("\n[!] Esc pressed. Exiting...")
            self.running = False
            return False

    def run(self):
        print("=" * 45)
        print("       REAL-TIME MOUSE COORDINATE TRACKER")
        print("=" * 45)
        print(" >> Move your mouse to see live coordinates.")
        print(" >> LEFT CLICK to print & record the coordinates.")
        print(" >> Press [Esc] or [Ctrl+C] to exit.")
        print("-" * 45)

        # Start background listeners
        mouse_listener = mouse.Listener(on_click=self.on_click)
        kb_listener = keyboard.Listener(on_press=self.on_key_press)

        mouse_listener.start()
        kb_listener.start()

        try:
            while self.running:
                x, y = self.mouse_ctrl.position
                sys.stdout.write(f"\r >> Current Position: X={int(x):4d}, Y={int(y):4d}   ")
                sys.stdout.flush()
                time.sleep(0.03)  # ~30 updates per second
        except KeyboardInterrupt:
            print("\n[!] Ctrl+C detected. Exiting...")
        finally:
            mouse_listener.stop()
            kb_listener.stop()


if __name__ == "__main__":
    tracker = MouseTracker()
    tracker.run()
