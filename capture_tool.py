#!/usr/bin/env python3
"""
Helper utility for capturing screen frames, creating data/stop.png,
cropping UI icons, and testing template matching confidence.
"""

import os
import sys
import argparse
from pathlib import Path

import cv2
import numpy as np

from utils import ensure_directories, ScreenMatcher, STOP_IMAGE_DEFAULT, START_DIR, DATA_DIR


def capture_screenshot(output_path: Path) -> None:
    ensure_directories()
    matcher = ScreenMatcher()
    saved = matcher.save_screenshot(output_path)
    matcher.close()
    print(f"[+] Screenshot saved to: {saved}")


def test_match(template_path: Path, threshold: float = 0.85) -> None:
    if not template_path.is_file():
        print(f"[!] Error: File '{template_path}' not found.")
        sys.exit(1)

    matcher = ScreenMatcher()
    tmpl = matcher.load_template_gray(template_path)
    if tmpl is None:
        print(f"[!] Error: Could not read image '{template_path}'.")
        sys.exit(1)

    matched, score, loc = matcher.match(tmpl, threshold=threshold)
    matcher.close()

    status = "SUCCESS (MATCHED)" if matched else "NO MATCH"
    print("=" * 50)
    print(f" Template   : {template_path}")
    print(f" Size       : {tmpl.shape[1]}x{tmpl.shape[0]} px")
    print(f" Match Score: {score:.4f} (Threshold: {threshold:.2f})")
    print(f" Best Loc   : {loc}")
    print(f" Result     : {status}")
    print("=" * 50)


def crop_image(input_path: Path, output_path: Path, rect: str) -> None:
    """
    Crops rectangle x,y,w,h from input image and saves to output_path.
    """
    if not input_path.is_file():
        print(f"[!] Input image '{input_path}' not found.")
        sys.exit(1)

    parts = [int(p.strip()) for p in rect.split(",")]
    if len(parts) != 4:
        print("[!] --rect must be formatted as: x,y,width,height (e.g. 500,300,100,50)")
        sys.exit(1)

    x, y, w, h = parts
    img = cv2.imread(str(input_path))
    if img is None:
        print(f"[!] Could not load '{input_path}'")
        sys.exit(1)

    crop = img[y : y + h, x : x + w]
    if crop.size == 0:
        print(f"[!] Invalid crop dimensions: ({x}, {y}, {w}, {h}) out of bounds for image shape {img.shape}")
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), crop)
    print(f"[+] Cropped region ({x}, {y}, {w}x{h}) saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Capture or test templates for game automation.")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: shot
    p_shot = subparsers.add_parser("shot", help="Take a fullscreen screenshot")
    p_shot.add_argument("--out", type=str, default=str(STOP_IMAGE_DEFAULT), help="Destination file path")

    # Command: test
    p_test = subparsers.add_parser("test", help="Test match score of a template against current screen")
    p_test.add_argument("template", type=str, help="Path to template image (e.g. data/stop.png)")
    p_test.add_argument("--threshold", type=float, default=0.85, help="Match threshold")

    # Command: crop
    p_crop = subparsers.add_parser("crop", help="Crop a region from an existing image")
    p_crop.add_argument("input", type=str, help="Source image path")
    p_crop.add_argument("--out", type=str, default=str(STOP_IMAGE_DEFAULT), help="Destination output path")
    p_crop.add_argument("--rect", type=str, required=True, help="Crop rectangle: x,y,width,height")

    args = parser.parse_args()

    if args.command == "shot":
        capture_screenshot(Path(args.out))
    elif args.command == "test":
        test_match(Path(args.template), threshold=args.threshold)
    elif args.command == "crop":
        crop_image(Path(args.input), Path(args.out), args.rect)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
