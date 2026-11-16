#!/usr/bin/env python3
"""Small persistent Sway IPC client for temporarily releasing game pointer lock."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path

IPC_MAGIC = b"i3-ipc"
IPC_RUN_COMMAND = 0
MAX_MESSAGE = 1 << 20


class SwaySeat:
    def __init__(self, socket_path: str):
        self.socket_path = Path(socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(str(self.socket_path))
        self.constraints_disabled = False

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("Sway IPC closed the seat-control connection")
            data.extend(chunk)
        return bytes(data)

    def _command(self, command: str) -> None:
        payload = command.encode("utf-8")
        if len(payload) > MAX_MESSAGE:
            raise ValueError("Sway IPC request is too large")
        self.sock.sendall(IPC_MAGIC + struct.pack("<II", len(payload), IPC_RUN_COMMAND) + payload)
        header = self._read_exact(14)
        if header[:6] != IPC_MAGIC:
            raise RuntimeError("Invalid Sway IPC response header")
        size, response_type = struct.unpack("<II", header[6:14])
        if size > MAX_MESSAGE or response_type != IPC_RUN_COMMAND:
            raise RuntimeError("Unexpected Sway IPC response")
        try:
            response = json.loads(self._read_exact(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid Sway IPC response: {exc}") from exc
        failed = [item for item in response if not item.get("success", False)]
        if failed:
            detail = "; ".join(str(item.get("error", "command failed")) for item in failed)
            raise RuntimeError(f"Sway rejected pointer-lock command: {detail}")

    def set_pointer_constraints(self, enabled: bool) -> None:
        if enabled:
            if not self.constraints_disabled:
                return
            self._command("seat seat0 pointer_constraint enable")
            self.constraints_disabled = False
        else:
            # Disable future grabs once, but escape on every request. A game
            # may recapture the pointer after macro playback even while the UI
            # clicker already believes constraints are disabled.
            if not self.constraints_disabled:
                self._command("seat seat0 pointer_constraint disable")
                self.constraints_disabled = True
            try:
                self._command("seat seat0 pointer_constraint escape")
            except RuntimeError:
                # Sway may reject escape when the game has no active lock.
                pass

    def close(self) -> None:
        if self.constraints_disabled:
            try:
                self.set_pointer_constraints(True)
            except (OSError, RuntimeError):
                pass
        try:
            self.sock.close()
        except OSError:
            pass
