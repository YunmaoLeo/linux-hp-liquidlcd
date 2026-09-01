# linux-hp-liquidlcd

Linux control for the 480×480 LCD pump cap found in recent HP OMEN 45L
desktops. The tested USB device is `03f0:7397` (`HP LCD-PUMP`).

Features:

- display JPEG, PNG and other still images;
- use Linux `hidraw` or direct `libusb` transport;
- use either the `hp-omen-lcd` command or a Python API.

This controls only the LCD cap. It does **not** change pump speed, fan speed or
firmware.

> The protocol was recovered from HP's signed OMEN Gaming Hub package and the
> official LCD Display SDK SoftPaq `sp159997`. This project is not affiliated
> with or endorsed by HP.

## Install

Requirements: Linux, Python 3.10+, and Pillow.

On Ubuntu/Debian:

```bash
sudo apt install python3 python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

For the optional direct-USB transport:

```bash
pip install '.[usb]'
```

## Device permissions

Install the included udev rule so the active desktop user can access the LCD
without `sudo`:

```bash
sudo install -m 0644 60-hp-omen-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo udevadm trigger --subsystem-match=usb
```

Unplug/replug the internal LCD USB cable or reboot if permissions do not update.
Then verify:

```bash
hp-omen-lcd probe
```

## Usage

Images are resized to 480×480. `contain` adds black bars when needed;
`cover` crops the edges.

```bash
hp-omen-lcd handshake
hp-omen-lcd off
hp-omen-lcd image photo.png
hp-omen-lcd image photo.jpg --fit cover --rotate 90
```

The default `hidraw` transport leaves the kernel driver attached. If it does
not work on a particular kernel, use direct USB:

```bash
hp-omen-lcd --transport usb image photo.png
```

Direct USB temporarily detaches only interface 1 and reattaches it when the
program exits normally.

## Python API

```python
from pathlib import Path
from omen_lcd import open_lcd, prepare_jpeg

with open_lcd() as lcd:
    lcd.handshake()
    lcd.upload_jpeg(prepare_jpeg(Path("photo.png"), "contain", 0))
```

## 中文快速开始

```bash
sudo apt install python3 python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install .

sudo install -m 0644 60-hp-omen-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo udevadm trigger --subsystem-match=usb

hp-omen-lcd image 图片.png --fit contain
hp-omen-lcd off
```

## Protocol notes

The vendor-defined HID interface exposes two relevant report IDs:

- report 1 is 64 bytes and carries wire command `0x41` for the handshake;
- report 2 is 1024 bytes;
- wire command `0x6e` transfers a JPEG in 1013-byte payload chunks following an
  11-byte header.

The `off` command follows HP's own `Apply_Black_Image` path and uploads a
480×480 black JPEG. This model does not expose a separate reliable backlight-off
command.

HP's native `CWitmodHid_SynchronizeImage` waits for each host USB write to
complete but does not wait for a device-side `0x6e` acknowledgement.

## Supported hardware

Currently tested: HP OMEN 45L GT22 series, USB ID `03f0:7397`, 480×480 LCD
pump cap. Other OMEN LCD devices may use different packet layouts.

## Development

```bash
python3 -m unittest -v
python3 -m build
```

Raw reports can be inspected with `--trace` before the subcommand.

## License

MIT
