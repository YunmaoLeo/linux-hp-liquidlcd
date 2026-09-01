# linux-hp-liquidlcd

Linux control and media playback for the 480×480 LCD pump cap found in recent
HP OMEN 45L desktops. The tested USB device is `03f0:7397` (`HP LCD-PUMP`).

This project can:

- show JPEG, PNG and other still images;
- play videos and animated images through `ffmpeg`;
- control LCD brightness and turn the backlight on or off;
- work through Linux `hidraw` or direct `libusb`;
- be used as either the `hp-omen-lcd` command or a Python module.

It controls only the LCD cap. It does **not** change pump speed, fan speed or
firmware.

> The protocol was recovered from HP's signed OMEN Gaming Hub package and the
> official LCD Display SDK SoftPaq `sp159997`. This project is not affiliated
> with or endorsed by HP.

## Requirements

- Linux and Python 3.10+
- Pillow for still-image conversion
- `ffmpeg` for videos and animated images
- PyUSB only when using the optional direct-USB transport

On Ubuntu/Debian:

```bash
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

For the direct-USB fallback, install the optional dependency:

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
Then verify detection:

```bash
hp-omen-lcd probe
```

## Usage

Still images are resized to 480×480. `contain` adds black bars when needed;
`cover` crops the edges.

```bash
hp-omen-lcd image photo.png
hp-omen-lcd image photo.jpg --fit cover --rotate 90 --brightness 80

hp-omen-lcd off
hp-omen-lcd on --brightness 80
hp-omen-lcd brightness 50
```

Play an MP4, WebM, GIF or any other format supported by `ffmpeg`:

```bash
hp-omen-lcd video clip.mp4
hp-omen-lcd video animation.gif --loop
hp-omen-lcd video clip.mp4 --fit cover --fps 15 --quality 80
hp-omen-lcd video clip.mp4 --loop --duration 60
```

Stop looping playback with `Ctrl+C`. The default is 10 FPS and JPEG quality 85.
Higher values use more USB bandwidth and CPU; the practical maximum depends on
the complexity of the frames and the host.

The default `hidraw` transport leaves the kernel driver attached. If it does not
work on a particular kernel, use direct USB:

```bash
hp-omen-lcd --transport usb image photo.png
hp-omen-lcd --transport usb video clip.mp4 --loop
```

Direct USB temporarily detaches only interface 1 and reattaches it when the
program exits normally.

## Python API

```python
from pathlib import Path
from omen_lcd import open_lcd, play_video, prepare_jpeg

with open_lcd() as lcd:
    lcd.handshake()
    lcd.upload_jpeg(prepare_jpeg(Path("photo.png"), "contain", 0))
    lcd.set_brightness(80)

with open_lcd(transport="usb") as lcd:
    lcd.handshake()
    play_video(lcd, Path("clip.mp4"), fps=10, loop=False)
```

## 中文快速开始

```bash
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install .

sudo install -m 0644 60-hp-omen-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo udevadm trigger --subsystem-match=usb

hp-omen-lcd image 图片.png --fit contain
hp-omen-lcd video 视频.mp4 --loop --fps 10
```

视频功能由 `ffmpeg` 解码并缩放为连续的 480×480 JPEG，再通过已经验证的
HP USB 协议逐帧发送。它不会操作水泵、风扇或固件。

## Protocol notes

The vendor-defined HID interface exposes two relevant report IDs:

- report 1 is 64 bytes and carries wire command `0x41` for the handshake;
- report 2 is 1024 bytes;
- wire command `0x6c` configures brightness;
- wire command `0x6e` transfers a JPEG in 1013-byte payload chunks following an
  11-byte header.

HP's native `CWitmodHid_SynchronizeImage` waits for each host USB write to
complete but does not wait for a device-side `0x6e` acknowledgement.

## Supported hardware

Currently tested:

- HP OMEN 45L GT22 series
- USB ID `03f0:7397`
- 480×480 LCD pump cap

Other OMEN LCD devices may use different packet layouts. Open an issue with the
USB ID and HID report descriptor before trying to add one.

## Development

```bash
python3 -m unittest -v
python3 -m build
```

Raw reports can be inspected with `--trace` before the subcommand.

## License

MIT
