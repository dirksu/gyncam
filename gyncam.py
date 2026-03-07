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
# Allow controlling OpenCV log level via environment (set before importing cv2)
# Default to ERROR to suppress noisy GStreamer/OpenCV warnings when running
# headless or under systemd. Can be overridden in the environment/service file.
os.environ.setdefault("OPENCV_LOG_LEVEL", os.environ.get("OPENCV_LOG_LEVEL", "ERROR"))

import cv2
import time
import threading
import pygame

# Try to programmatically reduce OpenCV log noise (best-effort).
try:
    if hasattr(cv2, "setLogLevel") and hasattr(cv2, "LOG_LEVEL_ERROR"):
        cv2.setLogLevel(cv2.LOG_LEVEL_ERROR)
    else:
        if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging") and hasattr(cv2.utils.logging, "LOG_LEVEL_ERROR"):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except Exception:
    # If OpenCV does not expose the APIs we attempted, ignore and continue.
    pass


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
        try:
            print(f"Copying snapshot to mounted path: {dest}", file=sys.stderr)
            shutil.copy2(local_file, dest)
        except Exception as e:
            print(f"ERROR: copy to SMB_MOUNT_PATH failed: {e}", file=sys.stderr)
            raise
        print(f"Copied to {dest}", file=sys.stderr)
        return

    if not smb.share:
        raise ValueError("SMB not configured. Set --smb-share or SMB_MOUNT_PATH.")

    cmd = ["smbclient"]
    # Prefer using IP/host from provided share. If force_smb2, add -m SMB2 to
    # request modern protocol. The share argument (//server/share) should be
    # appended as an argument, not as part of options.
    cmd += [smb.share]

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

    # Mask sensitive parts of the command for logging (don't print passwords)
    def _mask_cmd(cmdlist: list[str]) -> str:
        out = []
        for t in cmdlist:
            if "%" in t:
                # mask password after percent
                user, _, rest = t.partition("%")
                out.append(f"{user}%*****")
            else:
                out.append(t)
        return " ".join(out)

    print(f"Running SMB upload command: {_mask_cmd(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Always log stdout/stderr for diagnosis
    if proc.stdout:
        print(f"smbclient stdout:\n{proc.stdout}", file=sys.stderr)
    if proc.stderr:
        print(f"smbclient stderr:\n{proc.stderr}", file=sys.stderr)

    if proc.returncode != 0:
        raise RuntimeError(
            "SMB upload failed. See journal/syslog for smbclient output."
        )


def _env_path(name: str) -> Optional[Path]:
    v = os.environ.get(name)
    return Path(v) if v else None


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _open_capture(device: str) -> cv2.VideoCapture:
    # Backwards-compatible simple open (kept for callers that want raw capture).
    if device.isdigit():
        idx = int(device)
        if hasattr(cv2, "CAP_V4L2"):
            return cv2.VideoCapture(idx, cv2.CAP_V4L2)
        return cv2.VideoCapture(idx)
    if device.startswith("/dev/"):
        if hasattr(cv2, "CAP_V4L2"):
            return cv2.VideoCapture(device, cv2.CAP_V4L2)
        return cv2.VideoCapture(device)
    return cv2.VideoCapture(device)


def _open_capture_with_resolution(device: str, width: int, height: int, fps: int, pix_fmt: str = "auto") -> cv2.VideoCapture:
    """Open capture and try to negotiate the requested resolution/fps.

    Strategy:
    1. Open normally (prefer V4L2 backend), set CAP_PROP_FRAME_WIDTH/HEIGHT/FPS and verify.
    2. If the result does not match and OpenCV has GStreamer support, try a GStreamer
       v4l2src pipeline that requests the desired caps and open that as a capture.
    3. Otherwise return the best-effort capture and leave it to the caller.
    """
    cap = _open_capture(device)
    if not cap.isOpened():
        return cap

    # Compute device path early for potential GStreamer pipelines
    if device.isdigit():
        dev_path = f"/dev/video{int(device)}"
    else:
        dev_path = device

    # Only try negotiation when the user requested a specific width/height
    if width > 0 or height > 0 or fps > 0:
        # Apply properties if provided
        if width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps > 0:
            cap.set(cv2.CAP_PROP_FPS, fps)

        # If the user requested a specific pixel format (mjpeg/yuy2), try to
        # set the FOURCC on the capture. Backends may ignore this but it's
        # a best-effort attempt.
        try:
            if pix_fmt and pix_fmt.lower() != "auto":
                if pix_fmt.lower() == "mjpeg" or pix_fmt.lower() == "mjpg":
                    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
                    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
                elif pix_fmt.lower() == "yuy2":
                    fourcc = cv2.VideoWriter_fourcc(*"YUY2")
                    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        except Exception:
            # Ignore failures to set FOURCC
            pass

        # Allow camera/driver to settle
        time.sleep(0.1)

        # Read back actual size
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        actual_f = int(cap.get(cv2.CAP_PROP_FPS) or 0)

        if (width and actual_w != width) or (height and actual_h != height):
            # Try GStreamer pipeline if available — many UVC devices accept
            # explicit caps via v4l2src and this can force native modes.
            if hasattr(cv2, "CAP_GSTREAMER"):
                try:
                    # Build caps string. If fps is not provided, omit framerate.
                    fr = f", framerate={fps}/1" if fps > 0 else ""
                    # Prefer requesting a specific format when asked. If the
                    # user requested YUY2, include that in the caps; otherwise
                    # omit format to let the device decide.
                    fmt_part = ", format=YUY2" if pix_fmt and pix_fmt.lower() == "yuy2" else ""
                    caps = f"video/x-raw{fmt_part}, width={width if width>0 else actual_w}, height={height if height>0 else actual_h}{fr}"
                    pipeline = f"v4l2src device={dev_path} ! {caps} ! videoconvert ! appsink"
                    new_cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                    if new_cap.isOpened():
                        # Give it a moment and verify
                        time.sleep(0.1)
                        aw = int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                        ah = int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                        if (not width or aw == width) and (not height or ah == height):
                            try:
                                cap.release()
                            except Exception:
                                pass
                            return new_cap
                        # else fallthrough and keep original cap
                except Exception:
                    pass

            # If the raw pipeline didn't yield the requested size, try with
            # MJPEG/Compressed format — some UVC cameras only offer high
            # resolutions as compressed frames.
            if hasattr(cv2, "CAP_GSTREAMER"):
                try:
                    fr = f", framerate={fps}/1" if fps > 0 else ""
                    caps = f"image/jpeg, width={width if width>0 else actual_w}, height={height if height>0 else actual_h}{fr}"
                    pipeline = f"v4l2src device={dev_path} ! {caps} ! jpegdec ! videoconvert ! appsink"
                    jpeg_cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                    if jpeg_cap.isOpened():
                        time.sleep(0.1)
                        jw = int(jpeg_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                        jh = int(jpeg_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                        if (not width or jw == width) and (not height or jh == height):
                            try:
                                cap.release()
                            except Exception:
                                pass
                            return jpeg_cap
                        # else fallthrough
                except Exception:
                    pass

    return cap


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
    p.add_argument("--width", type=int, default=_env_int("CAM_WIDTH", 0))
    p.add_argument("--height", type=int, default=_env_int("CAM_HEIGHT", 0))
    p.add_argument("--fps", type=int, default=_env_int("CAM_FPS", 0))
    p.add_argument("--rotate", type=int, default=_env_int("CAM_ROTATE", 0), choices=[0, 90, 180, 270])

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
    # Fullscreen and snap button: default from environment if present, otherwise the
    # existing defaults (fullscreen True, snap_button True). We expose both
    # --fullscreen/--no-fullscreen and --snap-button/--no-snap-button to allow
    # overriding via command line. argparse handles the CLI flags; we use
    # environment helpers to determine defaults.
    p.add_argument("--fullscreen", action="store_true", default=_env_bool("FULLSCREEN", True), help="Fullscreen on framebuffer")
    p.add_argument("--no-fullscreen", dest="fullscreen", action="store_false")
    p.add_argument("--snap-button", action="store_true", default=_env_bool("SNAP_BUTTON", True), help="On-screen SNAP button")
    p.add_argument("--no-snap-button", dest="snap_button", action="store_false")

    # GPIO
    p.add_argument("--gpio", action="store_true", default=_env_bool("GPIO_ENABLE", False), help="Enable GPIO trigger")
    p.add_argument("--gpio-pin", type=int, default=_env_int("GPIO_PIN", 17), help="BCM pin number")
    p.add_argument("--no-gpio", dest="gpio", action="store_false", help="Disable GPIO trigger (overrides GPIO_ENABLE env)")
    p.add_argument(
        "--gpio-pull",
        type=str,
        default=os.environ.get("GPIO_PULL", "up"),
        choices=["up", "down", "off"],
        help="Internal pull resistor",
    )
    p.add_argument("--gpio-edge", type=str, default=os.environ.get("GPIO_EDGE", "falling"), choices=["rising", "falling", "both"])
    p.add_argument("--gpio-bounce-ms", type=int, default=_env_int("GPIO_BOUNCE_MS", 200))

    # Optional source overlay text (empty -> disabled)
    p.add_argument(
        "--source-text",
        type=str,
        default=os.environ.get(
            "SOURCE_TEXT",
            "Dirk Sudowe Praxis für Frauenheilkunde Voerde",
        ),
        help="Small source text to show as overlay in the top-left (empty to disable)",
    )

    # Advanced capture options
    p.add_argument("--force-gst", action="store_true", default=_env_bool("FORCE_GST", False), help="Force using GStreamer pipeline for capture negotiation")
    p.add_argument("--verbose-capture", action="store_true", default=_env_bool("VERBOSE_CAPTURE", False), help="Verbose capture negotiation logging")
    # SMB protocol helpers

    # Preview resolution (override monitor size). If zero, the monitor
    # resolution is used for the preview. Can be set via PREVIEW_WIDTH/HEIGHT
    # environment variables or the --preview-width/--preview-height CLI flags.
    p.add_argument("--preview-width", type=int, default=_env_int("PREVIEW_WIDTH", 0), help="Preview (stream) width - default: monitor width")
    p.add_argument("--preview-height", type=int, default=_env_int("PREVIEW_HEIGHT", 0), help="Preview (stream) height - default: monitor height")

    # Pixel format preference for capture negotiation. 'auto' lets the
    # driver decide; 'mjpeg' (MJPG) requests compressed frames (lower
    # USB bandwidth), 'yuy2' requests YUY2 planar format.
    p.add_argument("--pix-fmt", type=str, default=os.environ.get("CAM_PIX_FMT", "auto"), choices=["auto", "mjpeg", "mjpg", "yuy2"], help="Preferred camera pixel format (auto/mjpeg/yuy2)")
    p.add_argument("--snap-pix-fmt", type=str, default=os.environ.get("SNAP_PIX_FMT", os.environ.get("CAM_PIX_FMT", "auto")), choices=["auto", "mjpeg", "mjpg", "yuy2"], help="Preferred pixel format for snapshot negotiation (defaults to CAM_PIX_FMT)")

    # Beep control for snapshot feedback
    p.add_argument("--beep", action="store_true", default=_env_bool("BEEP", True), help="Enable short beep on snapshot")
    p.add_argument("--no-beep", dest="beep", action="store_false", help="Disable beep on snapshot")

    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    # We'll open the camera after the display is initialized so we can
    # negotiate a preview resolution that matches the monitor. Snapshot
    # captures will use the resolution provided by --width/--height (or
    # env CAM_WIDTH/CAM_HEIGHT) by opening a temporary capture when
    # taking the snapshot.
    cap = None

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
    # We attempt to open the display using the configured SDL_VIDEODRIVER.
    # If that fails (e.g. fbcon not available in service context), try a few
    # sensible fallbacks so the service doesn't crash immediately.
    pygame.init()
    flags = pygame.FULLSCREEN if args.fullscreen else 0

    def _try_set_mode(flags: int):
        # Try the currently configured SDL_VIDEODRIVER first, then fallbacks.
        tried = []
        env_driver = os.environ.get("SDL_VIDEODRIVER")
        drivers = [env_driver] if env_driver else []
        drivers += [d for d in ("fbcon", "kmsdrm", "directfb", "x11", "dummy") if d not in drivers]

        for drv in drivers:
            if not drv:
                continue
            tried.append(drv)
            os.environ["SDL_VIDEODRIVER"] = drv
            try:
                # Re-init display subsystem to pick up new driver
                try:
                    pygame.display.quit()
                except Exception:
                    pass
                pygame.display.init()
                screen = pygame.display.set_mode((0, 0), flags)
                print(f"Using SDL_VIDEODRIVER={drv}", file=sys.stderr)
                return screen
            except Exception as e:
                print(f"Failed to use SDL_VIDEODRIVER={drv}: {e}", file=sys.stderr)
                continue

        raise RuntimeError(f"Couldn't open pygame display with any driver. Tried: {', '.join([d for d in tried if d])}")

    try:
        screen = _try_set_mode(flags)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
    pygame.display.set_caption("gyncam")
    screen_w, screen_h = screen.get_size()

    # Determine preview capture size: allow CLI/env override via
    # --preview-width/--preview-height. If those are zero, use the
    # monitor/display size so the streamed preview fills the screen.
    preview_w = args.preview_width if getattr(args, 'preview_width', 0) else screen_w
    preview_h = args.preview_height if getattr(args, 'preview_height', 0) else screen_h

    # Open preview capture using the chosen preview resolution. This is
    # best-effort; if the capture cannot be opened the program will exit.
    cap = _open_capture_with_resolution(args.device, preview_w, preview_h, args.fps, pix_fmt=getattr(args, 'pix_fmt', 'auto'))
    if not cap or not cap.isOpened():
        print(f"ERROR: Could not open camera for preview: {args.device}", file=sys.stderr)
        return 2

    # Report actual negotiated resolution/fps so the user can verify
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    actual_f = int(cap.get(cv2.CAP_PROP_FPS) or 0)
    print(f"Camera opened for preview: {actual_w}x{actual_h} @{actual_f}fps (requested preview: {preview_w}x{preview_h})", file=sys.stderr)

    # Hide the mouse cursor (useful for framebuffer touch displays)
    prev_mouse_visible = pygame.mouse.get_visible()
    try:
        pygame.mouse.set_visible(False)
    except Exception:
        # If the platform doesn't support hiding the cursor, ignore.
        prev_mouse_visible = True

    font = pygame.font.Font(None, 36)
    big_font = pygame.font.Font(None, 64)
    # Small font for source overlay (top-left)
    overlay_font = pygame.font.Font(None, 20)
    clock = pygame.time.Clock()

    # SNAP button flash / sound feedback
    snap_flash_start = 0.0
    snap_flash_duration = 0.35  # seconds
    snap_blink_interval = 0.12  # seconds
    beep_sound = None
    # Try to initialize audio and prepare a short beep WAV file (best-effort).
    try:
        try:
            pygame.mixer.init()
        except Exception:
            # Some platforms (or service contexts) may not have audio; ignore failures.
            pass
        # Create a short beep file in the temp dir if not present, then load it.
        import wave, math, struct

        beep_path = Path(tempfile.gettempdir()) / "gyncam_beep.wav"
        if not beep_path.exists():
            duration = 0.12
            freq = 880.0
            volume = 0.5
            sample_rate = 44100
            n_samples = int(sample_rate * duration)
            with wave.open(str(beep_path), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                frames = bytearray()
                for i in range(n_samples):
                    t = i / sample_rate
                    v = int(volume * 32767.0 * math.sin(2 * math.pi * freq * t))
                    frames += struct.pack('<h', v)
                wf.writeframes(frames)

        try:
            beep_sound = pygame.mixer.Sound(str(beep_path))
        except Exception:
            beep_sound = None
    except Exception:
        beep_sound = None

    snap_rect = pygame.Rect(0, 0, 220, 110)
    snap_rect.bottomright = (screen_w - 20, screen_h - 20)

    status_text = "READY"
    # Optional expiry time for transient status messages (seconds since epoch).
    status_expire = 0.0

    # Snapshot flow:
    # - When the user requests a snapshot we interrupt the preview,
    #   switch the camera to the requested CAM_* resolution, grab a
    #   frame, then switch the camera back to the preview resolution.
    # - Upload is performed in a background thread so the UI does not
    #   block on network I/O. We use flags to coordinate the steps.
    snapshot_in_progress = False
    snapshot_requested = False
    # How long to wait (seconds) for the temporary capture to produce a frame
    TMP_CAP_TIMEOUT = 3.0

    def _render_source_overlay() -> None:
        """Draw optional source text overlay (top-left)."""
        if not getattr(args, "source_text", ""):
            return
        try:
            src_surf = overlay_font.render(args.source_text, True, (255, 255, 255))
            pad_x, pad_y = 6, 4
            src_bg = pygame.Rect(
                10,
                10,
                src_surf.get_width() + pad_x * 2,
                src_surf.get_height() + pad_y * 2,
            )
            pygame.draw.rect(screen, (0, 0, 0), src_bg)
            screen.blit(src_surf, (src_bg.x + pad_x, src_bg.y + pad_y))
        except Exception:
            # Never fail the main loop because overlay rendering failed.
            return

    def _render_snap_button(now: float) -> None:
        """Render SNAP button with blink feedback."""
        if not args.snap_button:
            return

        # Blink while a snapshot is in progress OR briefly right after trigger.
        try:
            elapsed = now - snap_flash_start if snap_flash_start else 0.0
            is_temporary = bool(snap_flash_start and (elapsed < snap_flash_duration))
            is_flashing = snapshot_in_progress or is_temporary
            blink_on = bool(is_flashing and (int(now / snap_blink_interval) % 2 == 0))
        except Exception:
            is_flashing = False
            blink_on = False

        if is_flashing and blink_on:
            pygame.draw.rect(screen, (255, 255, 255), snap_rect)
            pygame.draw.rect(screen, (255, 200, 0), snap_rect, 3)
            txt = big_font.render("SNAP", True, (0, 0, 0))
        else:
            pygame.draw.rect(screen, (0, 0, 0), snap_rect)
            pygame.draw.rect(screen, (255, 255, 255), snap_rect, 3)
            txt = big_font.render("SNAP", True, (255, 255, 255))

        tx = snap_rect.centerx - txt.get_width() // 2
        ty = snap_rect.centery - txt.get_height() // 2
        screen.blit(txt, (tx, ty))

    def _render_busy_overlay() -> None:
        """Render translucent overlay while snapshot/upload is running."""
        if not snapshot_in_progress:
            return
        try:
            overlay = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, 140))
            screen.blit(overlay, (0, 0))
            busy_txt = big_font.render("Taking snapshot...", True, (255, 255, 255))
            bx = (screen_w - busy_txt.get_width()) // 2
            by = (screen_h - busy_txt.get_height()) // 2
            screen.blit(busy_txt, (bx, by))
        except Exception:
            return

    def _render_status_line() -> None:
        """Render status line at the bottom-left."""
        try:
            status_surf = font.render(status_text, True, (255, 255, 0))
            screen.blit(status_surf, (20, screen_h - status_surf.get_height() - 20))
        except Exception:
            return

    def _blit_preview_frame(frame_bgr) -> None:
        """Blit a BGR frame to the framebuffer with letterboxing.

        IMPORTANT: The caller is responsible for applying camera rotation.
        In the main preview loop, we rotate the frame before calling this.
        In immediate feedback drawing, we intentionally *do not* rotate again
        (the provided last_frame_bgr is already rotated).
        """
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        src_h, src_w = rgb.shape[:2]
        x, y, w, h = _fit_letterbox(src_w, src_h, screen_w, screen_h)
        rgb_scaled = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
        surf = pygame.image.frombuffer(rgb_scaled.tobytes(), (w, h), "RGB")
        screen.fill((0, 0, 0))
        screen.blit(surf, (x, y))

    def upload_worker(local_path: Path) -> None:
        """Background upload worker: uploads a file and updates UI state."""
        nonlocal status_text, status_expire, snapshot_in_progress
        try:
            remote_name = f"{args.remote_prefix.strip().strip('/')}/{local_path.name}" if args.remote_prefix else local_path.name
            remote_name = remote_name.replace("\\", "/")
            status_text = "Uploading..."
            status_expire = 0.0
            upload_to_smb(local_path, smb, remote_name=remote_name)
            status_text = f"Uploaded: {local_path.name}"
            try:
                status_expire = time.time() + 3.0
            except Exception:
                status_expire = 0.0
        except Exception as e:
            status_text = f"Upload failed: {e}"
            try:
                status_expire = time.time() + 5.0
            except Exception:
                status_expire = 0.0
        finally:
            snapshot_in_progress = False


    def do_snapshot(frame_bgr) -> None:
        nonlocal snap_flash_start, beep_sound
        # Trigger visual flash and sound immediately for user feedback
        try:
            snap_flash_start = time.time()
            if args.beep and beep_sound:
                try:
                    beep_sound.play()
                except Exception:
                    pass
        except Exception:
            pass

        # Update UI state immediately.
        try:
            nonlocal status_text, status_expire
            status_text = "Taking snapshot..."
            status_expire = 0.0
        except Exception:
            pass

        # Draw one UI update immediately so the user sees the feedback
        # before the potentially blocking camera switch.
        try:
            if frame_bgr is not None:
                _blit_preview_frame(frame_bgr)
            now = time.time()
            _render_source_overlay()
            _render_snap_button(now)
            _render_status_line()
            pygame.display.flip()
        except Exception:
            pass

        # Request a snapshot. The main loop will perform the camera
        # switch/capture/restore synchronously to avoid concurrent access
        # to the VideoCapture object.
        try:
            nonlocal snapshot_requested
            snapshot_requested = True
        except Exception:
            pass

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
            elif event.type == getattr(pygame, 'FINGERDOWN', None):
                # Touchscreen (SDL2) sends FINGERDOWN with normalized coords (0..1)
                try:
                    tx = int(event.x * screen_w)
                    ty = int(event.y * screen_h)
                    if args.snap_button and snap_rect.collidepoint((tx, ty)):
                        if last_frame_bgr is not None:
                            do_snapshot(last_frame_bgr)
                except Exception:
                    # Be defensive: do not let touch handling crash the main loop
                    pass

        if gpio_triggered:
            gpio_triggered = False
            if last_frame_bgr is not None:
                do_snapshot(last_frame_bgr)

        # If a snapshot was requested, perform the camera switch/capture
        #/restore sequence synchronously here to avoid concurrent access
        # to the capture device.
        if snapshot_requested and not snapshot_in_progress:
            snapshot_requested = False
            snapshot_in_progress = True
            try:
                frame_to_save = None
                # If a specific CAM_* resolution is requested, stop the
                # preview capture and open a temporary capture at the
                # requested resolution.
                if (args.width or args.height):
                    try:
                        try:
                            cap.release()
                        except Exception:
                            pass
                        tmp_cap = _open_capture_with_resolution(args.device, args.width, args.height, args.fps, pix_fmt=getattr(args, 'snap_pix_fmt', 'auto'))
                        if tmp_cap and tmp_cap.isOpened():
                            # Wait up to TMP_CAP_TIMEOUT seconds for a good frame
                            start_wait = time.time()
                            while time.time() - start_wait < TMP_CAP_TIMEOUT:
                                ok2, f2 = tmp_cap.read()
                                if ok2 and f2 is not None:
                                    frame_to_save = _rotate_frame(f2, args.rotate)
                                    break
                        # Always release the temporary capture if it was opened
                        try:
                            if 'tmp_cap' in locals() and tmp_cap:
                                tmp_cap.release()
                        except Exception:
                            pass
                    except Exception:
                        # Fall back to using the last preview frame if
                        # temporary negotiation failed.
                        frame_to_save = last_frame_bgr
                else:
                    # No special CAM_* resolution requested: use last preview frame
                    frame_to_save = last_frame_bgr

                # Save snapshot to disk
                out_dir: Path = args.local_out
                out_dir.mkdir(parents=True, exist_ok=True)
                stamp = _now_stamp()
                filename = f"snapshot-{stamp}.png"
                local_path = out_dir / filename

                if frame_to_save is None:
                    status_text = "No frame for snapshot"
                    # Start restoring preview below
                    raise RuntimeError("No frame available for snapshot")

                ok = cv2.imwrite(str(local_path), frame_to_save)
                if not ok:
                    status_text = "Write failed"
                    # fall through to restore preview
                    raise RuntimeError("Write failed")

                # Start background upload; upload_worker will clear snapshot_in_progress
                try:
                    t = threading.Thread(target=upload_worker, args=(local_path,), daemon=True)
                    t.start()
                except Exception:
                    # If thread start fails, perform upload synchronously
                    upload_worker(local_path)

            except Exception:
                # Errors are reported via status_text; continue to restore preview
                pass
            finally:
                # Restore preview capture so the live stream continues
                try:
                    cap = _open_capture_with_resolution(args.device, preview_w, preview_h, args.fps, pix_fmt=getattr(args, 'pix_fmt', 'auto'))
                    if cap and cap.isOpened():
                        try:
                            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                            actual_f = int(cap.get(cv2.CAP_PROP_FPS) or 0)
                            print(f"Camera reopened for preview: {actual_w}x{actual_h} @{actual_f}fps (requested preview: {preview_w}x{preview_h})", file=sys.stderr)
                        except Exception:
                            pass
                    else:
                        print("WARNING: Could not reopen camera for preview after snapshot", file=sys.stderr)
                except Exception as e:
                    print(f"WARNING: Error reopening preview capture: {e}", file=sys.stderr)

        # Read frame
        ok, frame = cap.read()
        if ok:
            frame = _rotate_frame(frame, args.rotate)
            last_frame_bgr = frame

            _blit_preview_frame(frame)

        # UI overlay
        now = time.time()
        _render_source_overlay()
        _render_snap_button(now)
        _render_busy_overlay()

        # Auto-reset status_text when expiry reached
        try:
            if status_expire and time.time() > status_expire:
                status_text = "READY"
                status_expire = 0.0
        except Exception:
            pass

        _render_status_line()

        pygame.display.flip()
        clock.tick(30)

    cap.release()
    if gpio:
        try:
            gpio.cleanup()
        except Exception:
            pass
    # Restore previous mouse visibility and quit
    try:
        pygame.mouse.set_visible(prev_mouse_visible)
    except Exception:
        pass
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
