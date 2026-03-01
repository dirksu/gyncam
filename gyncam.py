"""gyncam.py - framebuffer camera preview + snapshot to SMB.

Design goals:
  - No X/Wayland required: render directly to Linux framebuffer via SDL/pygame.
  - Touchscreen: on-screen SNAP button (touch generates mouse events in pygame).
  - Hardware button: optional GPIO input triggers snapshot.
  - Snapshot: saves PNG and uploads to SMB share.

Runtime environment (typical Raspberry Pi):
  export SDL_VIDEODRIVER=fbcon
  export SDL_FBDEV=/dev/fb0
  export SDL_MOUSEDRV=TSLIB
  export SDL_MOUSEDEV=/dev/input/touchscreen

See README.md (if present) for setup notes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import cv2
import pygame


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True)
class SmbConfig:
    mount_path: Optional[Path]
    share: Optional[str]  # //server/share
    remote_dir: str
    username: Optional[str]
    password: Optional[str]
    domain: Optional[str]
    authfile: Optional[Path]


def upload_to_smb(local_file: Path, smb: SmbConfig, remote_name: str) -> None:
    if smb.mount_path:
        smb.mount_path.mkdir(parents=True, exist_ok=True)
        dest = smb.mount_path / remote_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_file, dest)
        return

    if not smb.share:
        raise ValueError("SMB not configured. Set --smb-share or SMB_MOUNT_PATH.")

    cmd = ["smbclient", smb.share]

    if smb.authfile:
        cmd += ["-A", str(smb.authfile)]
    else:
        if not smb.username:
            raise ValueError("SMB username missing. Set SMB_USER or --smb-user.")
        if smb.password is None:
            raise ValueError("SMB password missing. Set SMB_PASS or --smb-pass.")
        if smb.domain:
            cmd += ["-W", smb.domain]
        cmd += ["-U", f"{smb.username}%{smb.password}"]

    remote_dir = smb.remote_dir.strip().replace("\\", "/").strip("/")
    cd_part = f"cd {remote_dir}; " if remote_dir else ""
    put_cmd = f'{cd_part}put "{str(local_file)}" "{remote_name}"'
    cmd += ["-c", put_cmd]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "SMB upload failed.\n"
            f"Command: {' '.join(cmd)}\n\n"
            f"stdout:\n{proc.stdout}\n\n"
            f"stderr:\n{proc.stderr}\n"
        )


def _env_path(name: str) -> Optional[Path]:
    v = os.environ.get(name)
    return Path(v) if v else None


def _open_capture(device: str) -> cv2.VideoCapture:
    if device.isdigit():
        return cv2.VideoCapture(int(device))
    return cv2.VideoCapture(device)


def _rotate_frame(frame, rotate: int):
    if rotate == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotate == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotate == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def _fit_letterbox(src_w: int, src_h: int, dst_w: int, dst_h: int) -> Tuple[int, int, int, int]:
    """Return (x, y, w, h) to fit src into dst with aspect ratio."""
    if src_w <= 0 or src_h <= 0:
        return 0, 0, dst_w, dst_h

    src_aspect = src_w / src_h
    dst_aspect = dst_w / dst_h

    if src_aspect > dst_aspect:
        # fit width
        w = dst_w
        h = int(dst_w / src_aspect)
    else:
        # fit height
        h = dst_h
        w = int(dst_h * src_aspect)

    x = (dst_w - w) // 2
    y = (dst_h - h) // 2
    return x, y, w, h


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Framebuffer UVC preview + snapshot to SMB")

    # Camera
    p.add_argument("--device", type=str, default=os.environ.get("CAM_DEVICE", "0"), help="Camera index (0) or path (/dev/video0)")
    p.add_argument("--width", type=int, default=int(os.environ.get("CAM_WIDTH", "0")))
    p.add_argument("--height", type=int, default=int(os.environ.get("CAM_HEIGHT", "0")))
    p.add_argument("--fps", type=int, default=int(os.environ.get("CAM_FPS", "0")))
    p.add_argument("--rotate", type=int, default=int(os.environ.get("CAM_ROTATE", "0")), choices=[0, 90, 180, 270])

    # Output
    p.add_argument("--local-out", type=Path, default=Path(os.environ.get("LOCAL_OUT", str(Path(tempfile.gettempdir()) / "gyncam"))))
    p.add_argument("--remote-prefix", type=str, default=os.environ.get("REMOTE_PREFIX", ""), help="Prefix (folder-ish) for remote filename")

    # SMB
    p.add_argument("--smb-mount-path", type=Path, default=_env_path("SMB_MOUNT_PATH"), help="If set: copy to this mounted path")
    p.add_argument("--smb-share", type=str, default=os.environ.get("SMB_SHARE"), help="SMB share //server/share")
    p.add_argument("--smb-remote-dir", type=str, default=os.environ.get("SMB_REMOTE_DIR", ""))
    p.add_argument("--smb-user", type=str, default=os.environ.get("SMB_USER"))
    p.add_argument("--smb-pass", type=str, default=os.environ.get("SMB_PASS"))
    p.add_argument("--smb-domain", type=str, default=os.environ.get("SMB_DOMAIN"))
    p.add_argument("--smb-authfile", type=Path, default=_env_path("SMB_AUTHFILE"))

    # UI
    p.add_argument("--fullscreen", action="store_true", default=True, help="Fullscreen on framebuffer")
    p.add_argument("--no-fullscreen", dest="fullscreen", action="store_false")
    p.add_argument("--snap-button", action="store_true", default=True, help="On-screen SNAP button")
    p.add_argument("--no-snap-button", dest="snap_button", action="store_false")

    # GPIO
    p.add_argument("--gpio", action="store_true", default=bool(int(os.environ.get("GPIO_ENABLE", "0"))), help="Enable GPIO trigger")
    p.add_argument("--gpio-pin", type=int, default=int(os.environ.get("GPIO_PIN", "17")), help="BCM pin number")
    p.add_argument(
        "--gpio-pull",
        type=str,
        default=os.environ.get("GPIO_PULL", "up"),
        choices=["up", "down", "off"],
        help="Internal pull resistor",
    )
    p.add_argument("--gpio-edge", type=str, default=os.environ.get("GPIO_EDGE", "falling"), choices=["rising", "falling", "both"])
    p.add_argument("--gpio-bounce-ms", type=int, default=int(os.environ.get("GPIO_BOUNCE_MS", "200")))

    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    cap = _open_capture(args.device)
    if not cap.isOpened():
        print(f"ERROR: Could not open camera: {args.device}", file=sys.stderr)
        return 2

    if args.width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    if args.height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if args.fps:
        cap.set(cv2.CAP_PROP_FPS, args.fps)

    smb = SmbConfig(
        mount_path=args.smb_mount_path,
        share=args.smb_share,
        remote_dir=args.smb_remote_dir,
        username=args.smb_user,
        password=args.smb_pass,
        domain=args.smb_domain,
        authfile=args.smb_authfile,
    )

    # Optional GPIO
    gpio = None
    gpio_triggered = False

    def request_snapshot():
        nonlocal gpio_triggered
        gpio_triggered = True

    if args.gpio:
        try:
            import RPi.GPIO as GPIO  # type: ignore

            gpio = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            pull = {"up": GPIO.PUD_UP, "down": GPIO.PUD_DOWN, "off": GPIO.PUD_OFF}[args.gpio_pull]
            GPIO.setup(args.gpio_pin, GPIO.IN, pull_up_down=pull)
            edge = {"rising": GPIO.RISING, "falling": GPIO.FALLING, "both": GPIO.BOTH}[args.gpio_edge]
            GPIO.add_event_detect(args.gpio_pin, edge, callback=lambda ch: request_snapshot(), bouncetime=args.gpio_bounce_ms)
        except Exception as e:
            print(f"WARNING: GPIO init failed, continuing without GPIO: {e}", file=sys.stderr)
            gpio = None

    # Pygame framebuffer init
    pygame.init()
    flags = pygame.FULLSCREEN if args.fullscreen else 0
    screen = pygame.display.set_mode((0, 0), flags)
    pygame.display.set_caption("gyncam")
    screen_w, screen_h = screen.get_size()

    font = pygame.font.Font(None, 36)
    big_font = pygame.font.Font(None, 64)
    clock = pygame.time.Clock()

    snap_rect = pygame.Rect(0, 0, 220, 110)
    snap_rect.bottomright = (screen_w - 20, screen_h - 20)

    status_text = "Ready"

    def do_snapshot(frame_bgr) -> None:
        nonlocal status_text

        out_dir: Path = args.local_out
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_stamp()
        filename = f"snapshot-{stamp}.png"
        local_path = out_dir / filename

        ok = cv2.imwrite(str(local_path), frame_bgr)
        if not ok:
            status_text = "Write failed"
            return

        remote_name = f"{args.remote_prefix.strip().strip('/')}/{filename}" if args.remote_prefix else filename
        remote_name = remote_name.replace("\\", "/")
        try:
            status_text = "Uploading..."
            upload_to_smb(local_path, smb, remote_name=remote_name)
            status_text = f"Uploaded: {filename}"
        except Exception as e:
            status_text = f"Upload failed: {e}"

    last_frame_bgr = None
    running = True
    while running:
        # Events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key in (pygame.K_SPACE, pygame.K_RETURN):
                    if last_frame_bgr is not None:
                        do_snapshot(last_frame_bgr)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if args.snap_button and snap_rect.collidepoint(event.pos):
                    if last_frame_bgr is not None:
                        do_snapshot(last_frame_bgr)

        if gpio_triggered:
            gpio_triggered = False
            if last_frame_bgr is not None:
                do_snapshot(last_frame_bgr)

        # Read frame
        ok, frame = cap.read()
        if ok:
            frame = _rotate_frame(frame, args.rotate)
            last_frame_bgr = frame

            # Convert for pygame (BGR->RGB) and scale
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            src_h, src_w = rgb.shape[:2]
            x, y, w, h = _fit_letterbox(src_w, src_h, screen_w, screen_h)
            rgb_scaled = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)

            surf = pygame.image.frombuffer(rgb_scaled.tobytes(), (w, h), "RGB")
            screen.fill((0, 0, 0))
            screen.blit(surf, (x, y))

        # UI overlay
        if args.snap_button:
            pygame.draw.rect(screen, (0, 0, 0), snap_rect)
            pygame.draw.rect(screen, (255, 255, 255), snap_rect, 3)
            txt = big_font.render("SNAP", True, (255, 255, 255))
            tx = snap_rect.centerx - txt.get_width() // 2
            ty = snap_rect.centery - txt.get_height() // 2
            screen.blit(txt, (tx, ty))

        status_surf = font.render(status_text, True, (255, 255, 0))
        screen.blit(status_surf, (20, screen_h - status_surf.get_height() - 20))

        pygame.display.flip()
        clock.tick(30)

    cap.release()
    if gpio:
        try:
            gpio.cleanup()
        except Exception:
            pass
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
