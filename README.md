# gyncam

Framebuffer camera preview with snapshot upload to SMB/CIFS.

gyncam renders a live camera preview directly to the Linux framebuffer (no X/Wayland required) and lets you take PNG snapshots via a touchscreen button, keyboard, or an optional GPIO input. Snapshots are saved locally and can be uploaded to an SMB/CIFS share either by copying to a mounted path or using smbclient.

Features
- Live preview rendered with pygame directly to the framebuffer (SDL fbcon)
- On-screen SNAP button (touchscreen friendly)
- Keyboard shortcuts (Space / Enter to snap, Esc / q to quit)
- Optional GPIO trigger for hardware buttons (Raspberry Pi)
- Save snapshots locally and upload to SMB share (or copy to mounted share)

Quick start

1. Install dependencies (example for Debian/Ubuntu/Raspbian):

   sudo apt update && sudo apt install -y python3 python3-pip python3-opencv python3-pygame smbclient
   pip3 install -r requirements

   Note: this repository ships a requirements file (named "requirements"). Use that with pip.

2. (Framebuffer / touchscreen) set framebuffer/touch env vars when running on a Raspberry Pi framebuffer:

   export SDL_VIDEODRIVER=fbcon
   export SDL_FBDEV=/dev/fb0
   export SDL_MOUSEDRV=TSLIB
   export SDL_MOUSEDEV=/dev/input/touchscreen

3. You can either export environment variables (see contrib/gyncam.env for an example), set GYNCAM_ARGS in a service file, or pass options on the command line. Run gyncam with:

   python3 gyncam.py [options]

Command-line options (high level)
- --device: camera index (0) or path (/dev/video0). Default: CAM_DEVICE or 0
- --width / --height / --fps: requested capture size / rate for snapshot negotiation
- --rotate: rotate captured frames (0, 90, 180, 270)
- --local-out: local directory to save snapshots (default: system temp/gyncam)
- --remote-prefix: optional prefix (folder-like) added to remote filename
- SMB options: --smb-mount-path, --smb-share, --smb-remote-dir, --smb-user, --smb-pass, --smb-domain, --smb-authfile
- UI: --fullscreen / --no-fullscreen, --snap-button / --no-snap-button
- GPIO options: --gpio, --gpio-pin, --gpio-pull, --gpio-edge, --gpio-bounce-ms
- Preview overrides: --preview-width, --preview-height
- Pixel format negotiation: --pix-fmt and --snap-pix-fmt (auto/mjpeg/yuy2)

Environment variables
Most options can also be supplied via environment variables. See contrib/gyncam.env for a comprehensive example. Useful variables include:

- CAM_DEVICE, CAM_WIDTH, CAM_HEIGHT, CAM_FPS, CAM_ROTATE
- PREVIEW_WIDTH, PREVIEW_HEIGHT
- SMB_MOUNT_PATH, SMB_SHARE, SMB_REMOTE_DIR, SMB_USER, SMB_PASS, SMB_DOMAIN, SMB_AUTHFILE
- GPIO_ENABLE, GPIO_PIN, GPIO_PULL, GPIO_EDGE, GPIO_BOUNCE_MS
- OPENCV_LOG_LEVEL (ERROR, WARNING, INFO, DEBUG) — default is ERROR to reduce noise
- CAM_PIX_FMT / SNAP_PIX_FMT — preferred pixel formats for negotiation

SMB configuration
- If you set --smb-mount-path (or SMB_MOUNT_PATH) gyncam will copy snapshots into that local path — useful when you've mounted the share via mount.cifs or automount.
- Otherwise gyncam will use smbclient and the provided SMB credentials (or an authfile) to upload snapshots remotely.

Authfile format (for smbclient -A)
The authfile is a simple text file with lines like:

username = MYUSER
password = MYPASSWORD
domain = MYDOMAIN

Ensure the file is readable only by the account running gyncam (chmod 600).

Systemd / service
This repo contains contrib/gyncam.env and contrib/gyncam.service as examples. The service can create a per-user runtime directory (XDG_RUNTIME_DIR) and source environment variables from the env file — use these as a starting point for a systemd deployment.

GPIO (optional)
- When --gpio is enabled gyncam will try to import RPi.GPIO and use BCM numbering. Configure pin, pull and edge using the flags or environment variables. The program gracefully continues without GPIO if initialization fails.

Usage notes / tips
- On a Raspberry Pi with a touchscreen you typically run with the framebuffer environment variables so pygame renders to /dev/fb0.
- If you get permission errors opening the camera or framebuffer, run under an account with the necessary privileges or adjust device permissions.
- If smbclient upload fails you can inspect the printed stderr/stdout for details; alternatively mount the SMB share and use --smb-mount-path.

Example

   SDL_VIDEODRIVER=fbcon SDL_FBDEV=/dev/fb0 python3 gyncam.py --device 0 --local-out /tmp/gyncam --smb-share //fileserve/photos --smb-user myuser --smb-pass secret --smb-remote-dir gynepics

Development / testing on desktop
- If you don't have a framebuffer, run without the framebuffer environment variables and pygame will open a window on your desktop. The same CLI options apply.

License
MIT

Contact / issues
If you find bugs or have feature requests, please open an issue in the repository.
