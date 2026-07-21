# gyncam

Framebuffer camera preview with snapshot upload to SMB.

gyncam shows a live camera preview directly on the Linux framebuffer (no X/Wayland required) and lets you take PNG snapshots either from a touchscreen button, keyboard, or an optional GPIO input. Snapshots are saved locally and can be copied to an SMB/CIFS share (either by copying to a mountpoint or using smbclient).

Features
- Live preview rendered with pygame directly to the framebuffer (SDL fbcon)
- Snapshot button on-screen (suitable for touchscreens)
- Keyboard shortcuts (Space / Enter / B to snap — B matches a USB foot switch sending the 'b' key, Esc / q to quit)
- Optional GPIO trigger for hardware buttons (Raspberry Pi)
- Snapshots have the source text and timestamp burned into the saved image (top-left corner)
- Save snapshots locally and upload to SMB share (or copy to mounted share)

Camera resolution and MJPEG
gyncam captures continuously at a single, fixed resolution (default 2592x1944 / 5MP) and uses that same stream for both the live preview and snapshots — there is no separate "switch to a higher resolution for the snapshot" step. This only works because the camera is asked to deliver MJPEG (`--pix-fmt mjpeg`, the default): raw/uncompressed YUYV cannot sustain a usable frame rate at high resolution over USB2 (measured ~2 fps at 1920x1080), while MJPEG easily can (measured ~16-25 fps at 1920x1080-2592x1944 on a Raspberry Pi over USB2). If you point gyncam at a camera/USB setup where MJPEG isn't available or fast enough, check `v4l2-ctl --list-formats-ext` for what your camera actually supports before lowering --width/--height.

Quick start

1. Install dependencies (example for Debian/Ubuntu/Raspbian):

   sudo apt update && sudo apt install -y python3 python3-pip python3-opencv python3-pygame smbclient
   pip3 install -r requirements.txt

2. (Framebuffer / touchscreen) set environment variables before running if you use a framebuffer + touchscreen:

   export SDL_VIDEODRIVER=fbcon
   export SDL_FBDEV=/dev/fb0
   export SDL_MOUSEDRV=TSLIB
   export SDL_MOUSEDEV=/dev/input/touchscreen

3. Run gyncam:

   python3 gyncam.py [options]

Command-line options (high level)
- --device: camera index (0) or path (/dev/video0). Default: CAM_DEVICE or 0
- --width / --height / --fps: requested capture size / rate. Default: 2592x1944 (5MP), fps auto-negotiated
- --pix-fmt: preferred camera pixel format (auto/mjpeg/yuy2). Default: mjpeg — see "Camera resolution and MJPEG" above
- --rotate: rotate captured frames (0, 90, 180, 270)
- --local-out: local directory to save snapshots (default: system temp/gyncam)
- --remote-prefix: optional prefix (folder-like) added to remote filename
- --source-text: text overlay burned into the top-left of every saved snapshot, alongside the timestamp (default: practice name; empty string disables it)
- --beep / --no-beep: short audible feedback on snapshot
- SMB options: --smb-mount-path, --smb-share, --smb-remote-dir, --smb-user, --smb-pass, --smb-domain, --smb-authfile
- UI: --fullscreen / --no-fullscreen, --snap-button / --no-snap-button
- GPIO options: --gpio, --gpio-pin, --gpio-pull, --gpio-edge, --gpio-bounce-ms

Environment variables
Many of the same options can be supplied via environment variables instead of CLI flags. The most useful ones:

- CAM_DEVICE, CAM_WIDTH, CAM_HEIGHT, CAM_FPS, CAM_ROTATE, CAM_PIX_FMT
- LOCAL_OUT, REMOTE_PREFIX, SOURCE_TEXT, BEEP
- SMB_MOUNT_PATH, SMB_SHARE, SMB_REMOTE_DIR, SMB_USER, SMB_PASS, SMB_DOMAIN, SMB_AUTHFILE
- GPIO_ENABLE, GPIO_PIN, GPIO_PULL, GPIO_EDGE, GPIO_BOUNCE_MS

SMB configuration
- If you set --smb-mount-path (or SMB_MOUNT_PATH) gyncam will copy snapshots into that local path — useful when you've mounted the share via mount.cifs or automount.
- Otherwise gyncam will use smbclient and the provided SMB credentials (or an authfile) to upload snapshots remotely.
- Prefer --smb-authfile / SMB_AUTHFILE over --smb-user/--smb-pass where possible: a password passed via --smb-pass is visible in the process list (`ps aux`) to any local user for as long as smbclient runs; since this handles patient images, an authfile is the safer choice.

Authfile format (for smbclient -A)
The authfile is a simple text file with lines like:

username = MYUSER
password = MYPASSWORD
domain = MYDOMAIN

Ensure the file is readable only by the account running gyncam (chmod 600).

GPIO (optional)
- When --gpio is enabled gyncam will try to import RPi.GPIO and use BCM numbering. Configure pin, pull and edge using the flags or env vars. The code gracefully falls back if GPIO cannot be initialized.

Usage notes / tips
- On a Raspberry Pi with a touchscreen you typically run with the framebuffer environment variables so pygame renders to /dev/fb0.
- If you get permission errors opening the camera or framebuffer, run under an account with the necessary privileges or adjust device permissions.
- If smbclient upload fails you can inspect the printed stderr/stdout for details; alternatively mount the SMB share and use --smb-mount-path.

Example

   SDL_VIDEODRIVER=fbcon SDL_FBDEV=/dev/fb0 python3 gyncam.py --device 0 --local-out /tmp/gyncam --smb-share //fileserve/photos --smb-authfile /etc/gyncam/smb.auth --smb-remote-dir gynepics

Development / testing on desktop
- If you don't have a framebuffer, run without the framebuffer environment variables and pygame will open a window on your desktop. The same CLI options apply.

License
MIT

Contact / issues
If you find bugs or have feature requests, please open an issue in the repository.
