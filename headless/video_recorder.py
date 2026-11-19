#!/usr/bin/env python3
"""Low-priority 720p/30 encoding of the private WayVNC framebuffer."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


WIDTH = 1920
HEIGHT = 1080
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
FPS = 30


class VideoSegment:
    """One uniquely named, temporary clip from the isolated game framebuffer."""

    def __init__(self, output_dir: Path, loop_number: int, phase: str):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed")

        self.output_dir = output_dir
        self.loop_number = loop_number
        self.phase = phase
        self.token = uuid.uuid4().hex
        fd, temporary_path = tempfile.mkstemp(
            prefix=f"aotr-loop-{loop_number:05d}-{phase}-{self.token}-", suffix=".mp4"
        )
        os.close(fd)
        self.partial_path = Path(temporary_path)
        self._stop_requested = False
        self._timed_out = False
        self._writer_error = False
        self._dropped_frames = 0
        self._next_frame_at = 0.0
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=2)

        command = [
            ffmpeg,
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb565le",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS),
            "-i", "pipe:0",
            "-filter_threads", "1",
            "-vf", f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=fast_bilinear",
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-tune", "zerolatency", "-crf", "30", "-threads", "1",
            "-pix_fmt", "yuv420p",
            str(self.partial_path),
        ]
        nice = shutil.which("nice")
        if nice:
            command = [nice, "-n", "15", *command]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self.partial_path.unlink(missing_ok=True)
            raise
        if self.process.stdin is None:
            self.process.terminate()
            self.partial_path.unlink(missing_ok=True)
            raise RuntimeError("could not open ffmpeg's frame input")
        self._writer = threading.Thread(
            target=self._write_frames,
            name=f"VideoWriter-{loop_number}-{phase}",
            daemon=True,
        )
        try:
            self._writer.start()
        except RuntimeError:
            self.process.terminate()
            self.process.wait()
            self.partial_path.unlink(missing_ok=True)
            raise

    def submit(self, pixels) -> None:
        """Queue a WayVNC RGB565 frame without waiting for ffmpeg."""
        if self._stop_requested or self.process.poll() is not None:
            return
        now = time.monotonic()
        if now < self._next_frame_at:
            return
        self._next_frame_at = now + 1.0 / FPS
        if self._frames.full():
            self._dropped_frames += 1
            return
        frame = pixels.astype("<u2", copy=False).tobytes(order="C")
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            # Bound memory and never make game control wait for the encoder.
            self._dropped_frames += 1

    def _write_frames(self) -> None:
        try:
            while not self._stop_requested or not self._frames.empty():
                try:
                    frame = self._frames.get(timeout=0.05)
                except queue.Empty:
                    if self.process.poll() is not None:
                        break
                    continue
                try:
                    if self.process.stdin is None:
                        break
                    self.process.stdin.write(frame)
                except (BrokenPipeError, OSError):
                    self._writer_error = True
                    break
        finally:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except OSError:
                    self._writer_error = True

    def request_stop(self) -> None:
        self._stop_requested = True

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def finish(self) -> Path | None:
        self.request_stop()
        self._writer.join(timeout=3.0)
        if self._writer.is_alive():
            self._timed_out = True
            self.process.terminate()
            self._writer.join(timeout=1.0)
            if self._writer.is_alive():
                self.process.kill()
                self._writer.join()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._timed_out = True
                self.process.terminate()
                try:
                    self.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()

        if (self._timed_out or self._writer_error or self.process.returncode != 0
                or not self.partial_path.is_file()):
            self.partial_path.unlink(missing_ok=True)
            return None
        if self.partial_path.stat().st_size < 1024:
            self.partial_path.unlink(missing_ok=True)
            return None
        return self.partial_path

    def save_failed(self) -> Path | None:
        source = self.finish()
        if source is None:
            return None
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            source.unlink(missing_ok=True)
            return None

        while True:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            name = f"failed-loop-{self.loop_number:05d}-{self.phase}-{stamp}-{uuid.uuid4().hex[:8]}.mp4"
            destination = self.output_dir / name
            try:
                # Hard-link creation fails instead of replacing any existing
                # clip, even if a generated filename were to collide.
                os.link(source, destination)
            except FileExistsError:
                continue
            except OSError:
                # Keep a no-overwrite fallback for filesystems that disallow
                # hard links.
                try:
                    with source.open("rb") as reader, destination.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
                except FileExistsError:
                    continue
                except OSError:
                    destination.unlink(missing_ok=True)
                    return None
                try:
                    source.unlink()
                except OSError:
                    pass
                return destination
            try:
                source.unlink()
            except OSError:
                pass
            return destination

    def discard(self) -> None:
        self.finish()
        self.partial_path.unlink(missing_ok=True)
