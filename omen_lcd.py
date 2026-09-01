#!/usr/bin/env python3
"""Linux control utility for the HP OMEN LCD-PUMP (03f0:7397).

The packet format was recovered from HP's LCD Display SDK (SoftPaq sp159997).
This utility talks only to the USB LCD cap; it does not alter pump or fan speed.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, Iterator


HP_VID = 0x03F0
LCD_PID = 0x7397
REPORT_CONTROL = 0x01
REPORT_DATA = 0x02
# HP's managed helper builds logical command 0x0f, but CWitmodHid_HidWriteBuff
# maps logical commands 0x0d..0x1e to wire commands 0x6a..0x7b.
CMD_HANDSHAKE = 0x41
CMD_CONFIG = 0x6C
CMD_SYNC_IMAGE = 0x6E
CONTROL_REPORT_SIZE = 64
DATA_REPORT_SIZE = 1024
IMAGE_HEADER_SIZE = 11
IMAGE_PAYLOAD_SIZE = DATA_REPORT_SIZE - IMAGE_HEADER_SIZE


class OmenLcdError(RuntimeError):
    pass


def _hid_identity(hidraw: Path) -> tuple[int, int] | None:
    try:
        lines = (hidraw / "device" / "uevent").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("HID_ID="):
            continue
        parts = line.removeprefix("HID_ID=").split(":")
        if len(parts) == 3:
            return int(parts[1], 16), int(parts[2], 16)
    return None


def find_devices() -> list[Path]:
    """Find the vendor-defined interface, excluding the cap's mouse interface."""
    matches: list[Path] = []
    for sys_hidraw in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        if _hid_identity(sys_hidraw) != (HP_VID, LCD_PID):
            continue
        try:
            descriptor = (sys_hidraw / "device" / "report_descriptor").read_bytes()
        except OSError:
            continue
        # The LCD data interface has report ID 2 and 1023-byte output reports.
        # The other interface identifies as a boot mouse and must not be used.
        if b"\x85\x02" in descriptor and b"\x96\xff\x03" in descriptor:
            matches.append(Path("/dev") / sys_hidraw.name)
    return matches


def resolve_device(requested: str | None) -> Path:
    if requested:
        return Path(requested)
    devices = find_devices()
    if not devices:
        raise OmenLcdError("HP LCD-PUMP 03f0:7397 data interface not found")
    if len(devices) > 1:
        names = ", ".join(map(str, devices))
        raise OmenLcdError(f"multiple LCD-PUMP interfaces found ({names}); use --device")
    return devices[0]


class OmenLcd:
    def __init__(self, path: Path, trace: bool = False):
        self.path = path
        self.trace = trace
        self.fd: int | None = None

    def __enter__(self) -> "OmenLcd":
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise OmenLcdError(
                f"permission denied opening {self.path}; install the included udev rule"
            ) from exc
        except OSError as exc:
            raise OmenLcdError(f"cannot open {self.path}: {exc}") from exc
        # HP's helper waits after opening the HID handle.  Also discard replies
        # left pending in the device endpoint by a previous short-lived client.
        time.sleep(0.035)
        self._drain_input(0.1)
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _write(self, report: bytes, attempts: int = 6) -> None:
        assert self.fd is not None
        last_error: OSError | None = None
        for _ in range(attempts):
            try:
                written = os.write(self.fd, report)
                if written != len(report):
                    raise OmenLcdError(f"short HID write: {written}/{len(report)} bytes")
                if self.trace:
                    preview = report[: min(32, len(report))].hex(" ")
                    print(f"TX {written:4} bytes: {preview}", file=sys.stderr)
                return
            except BlockingIOError as exc:
                last_error = exc
                time.sleep(0.01)
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
        raise OmenLcdError(f"HID write failed after {attempts} attempts: {last_error}")

    def _drain_input(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            response = self._read(remaining)
            if response is None:
                return
            if self.trace:
                preview = response[: min(64, len(response))].hex(" ")
                print(f"RX (drain) {len(response):4} bytes: {preview}", file=sys.stderr)

    def _read(self, timeout: float) -> bytes | None:
        assert self.fd is not None
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return None
        try:
            return os.read(self.fd, DATA_REPORT_SIZE)
        except BlockingIOError:
            return None

    def handshake(self) -> None:
        report = bytearray(CONTROL_REPORT_SIZE)
        report[0] = REPORT_CONTROL
        report[1] = CMD_HANDSHAKE
        report[5] = 1
        self._write(report)
        # The cap sends one or more 0x41 status replies.  HP's image path stops
        # its handshake timer and waits before starting a 1024-byte transfer.
        self._drain_input(0.3)

    def set_brightness(self, percent: int) -> None:
        self._write(build_config_report(percent))

    def upload_jpeg(self, jpeg: bytes, progress: bool = True) -> None:
        reports = build_image_reports(jpeg)
        for index, report in enumerate(reports, 1):
            self._write(report)
            if progress:
                print(f"\rUploading image: {index}/{len(reports)}", end="", flush=True)
        if progress:
            print()


class OmenUsb(OmenLcd):
    """Direct libusb transport for comparing hidraw with the USB endpoints."""

    INTERFACE = 1
    ENDPOINT_IN = 0x81
    ENDPOINT_OUT = 0x02

    def __init__(self, trace: bool = False):
        super().__init__(Path("usb:03f0:7397"), trace)
        self.usb_device = None
        self.detached_kernel_driver = False

    def __enter__(self) -> "OmenUsb":
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise OmenLcdError(
                "direct USB mode requires PyUSB: sudo apt install python3-usb"
            ) from exc

        device = usb.core.find(idVendor=HP_VID, idProduct=LCD_PID)
        if device is None:
            raise OmenLcdError("HP LCD-PUMP 03f0:7397 not found by libusb")
        self.usb_device = device
        try:
            if device.is_kernel_driver_active(self.INTERFACE):
                device.detach_kernel_driver(self.INTERFACE)
                self.detached_kernel_driver = True
            usb.util.claim_interface(device, self.INTERFACE)
        except usb.core.USBError as exc:
            self.__exit__()
            raise OmenLcdError(f"cannot claim LCD USB interface 1: {exc}") from exc
        time.sleep(0.035)
        self._drain_input(0.1)
        return self

    def __exit__(self, *_: object) -> None:
        device = self.usb_device
        if device is None:
            return
        try:
            import usb.util

            try:
                usb.util.release_interface(device, self.INTERFACE)
            finally:
                if self.detached_kernel_driver:
                    try:
                        device.attach_kernel_driver(self.INTERFACE)
                    except Exception:
                        pass
                usb.util.dispose_resources(device)
        finally:
            self.usb_device = None
            self.detached_kernel_driver = False

    def _write(self, report: bytes, attempts: int = 6) -> None:
        assert self.usb_device is not None
        import usb.core

        last_error = None
        for _ in range(attempts):
            try:
                written = self.usb_device.write(
                    self.ENDPOINT_OUT, report, timeout=2000
                )
                if written != len(report):
                    raise OmenLcdError(f"short USB write: {written}/{len(report)} bytes")
                if self.trace:
                    preview = report[: min(32, len(report))].hex(" ")
                    print(f"TX/USB {written:4} bytes: {preview}", file=sys.stderr)
                return
            except usb.core.USBError as exc:
                last_error = exc
                time.sleep(0.01)
        raise OmenLcdError(f"USB write failed after {attempts} attempts: {last_error}")

    def _read(self, timeout: float) -> bytes | None:
        assert self.usb_device is not None
        import usb.core

        timeout_ms = max(1, int(timeout * 1000))
        try:
            data = self.usb_device.read(
                self.ENDPOINT_IN, 512, timeout=timeout_ms
            )
            return bytes(data)
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as exc:
            raise OmenLcdError(f"USB read failed: {exc}") from exc


def open_lcd(
    transport: str = "hidraw",
    device: str | Path | None = None,
    *,
    trace: bool = False,
) -> OmenLcd:
    """Create an LCD transport for use as a context manager."""
    if transport == "hidraw":
        return OmenLcd(resolve_device(str(device) if device is not None else None), trace=trace)
    if transport == "usb":
        return OmenUsb(trace=trace)
    raise ValueError("transport must be 'hidraw' or 'usb'")


def build_config_report(brightness: int, rotation: int = 0, fps: int = 25) -> bytes:
    if not 0 <= brightness <= 100:
        raise ValueError("brightness must be between 0 and 100")
    if not 0 <= rotation <= 3:
        raise ValueError("rotation code must be between 0 and 3")
    if not 0 <= fps <= 255:
        raise ValueError("fps must fit in one byte")
    report = bytearray(DATA_REPORT_SIZE)
    report[0] = REPORT_DATA
    report[1] = CMD_CONFIG
    report[2:6] = (1).to_bytes(4, "big")
    report[6:9] = (1).to_bytes(3, "big")
    report[10] = 8
    report[11] = 4  # HP SDK's display configuration selector
    report[12] = brightness
    report[13] = rotation
    report[18] = fps
    return bytes(report)


def build_image_reports(jpeg: bytes) -> list[bytes]:
    if not jpeg:
        raise ValueError("image data is empty")
    if len(jpeg) > 0xFFFFFFFF:
        raise ValueError("image is too large")
    reports: list[bytes] = []
    total_size = len(jpeg)
    for packet_index, offset in enumerate(range(0, total_size, IMAGE_PAYLOAD_SIZE)):
        payload = jpeg[offset : offset + IMAGE_PAYLOAD_SIZE]
        report = bytearray(DATA_REPORT_SIZE)
        report[0] = REPORT_DATA
        report[1] = CMD_SYNC_IMAGE
        report[2:6] = total_size.to_bytes(4, "big")
        report[6:9] = packet_index.to_bytes(3, "big")
        report[9:11] = len(payload).to_bytes(2, "big")
        report[IMAGE_HEADER_SIZE : IMAGE_HEADER_SIZE + len(payload)] = payload
        reports.append(bytes(report))
    return reports


def prepare_jpeg(source: Path, fit: str, rotate: int) -> bytes:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise OmenLcdError("image upload requires Pillow: sudo apt install python3-pil") from exc
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            if rotate:
                image = image.rotate(-rotate, expand=True)
            if fit == "cover":
                image = ImageOps.fit(image, (480, 480), method=Image.Resampling.LANCZOS)
            else:
                image.thumbnail((480, 480), Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (480, 480), "black")
                canvas.paste(image, ((480 - image.width) // 2, (480 - image.height) // 2))
                image = canvas
            with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as output:
                image.save(output, "JPEG", quality=100, subsampling=0)
                output.seek(0)
                return output.read()
    except OSError as exc:
        raise OmenLcdError(f"cannot prepare image {source}: {exc}") from exc


def iter_mjpeg_frames(stream: BinaryIO, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Split an MJPEG byte stream into complete JPEG images."""
    buffer = bytearray()
    while True:
        chunk = stream.read(chunk_size)
        if chunk:
            buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                if len(buffer) > 1:
                    del buffer[:-1]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start:
                    del buffer[:start]
                break
            end += 2
            yield bytes(buffer[start:end])
            del buffer[:end]
        if not chunk:
            break


def _video_filter(fit: str, rotate: int, fps: float) -> str:
    filters: list[str] = []
    if rotate == 90:
        filters.append("transpose=clock")
    elif rotate == 180:
        filters.extend(("hflip", "vflip"))
    elif rotate == 270:
        filters.append("transpose=cclock")
    if fit == "cover":
        filters.extend((
            "scale=480:480:force_original_aspect_ratio=increase",
            "crop=480:480",
        ))
    else:
        filters.extend((
            "scale=480:480:force_original_aspect_ratio=decrease",
            "pad=480:480:(ow-iw)/2:(oh-ih)/2:black",
        ))
    filters.append(f"fps={fps:g}")
    return ",".join(filters)


def ffmpeg_frames(
    source: Path,
    *,
    fit: str = "contain",
    rotate: int = 0,
    fps: float = 10.0,
    quality: int = 85,
    loop: bool = False,
) -> Iterator[bytes]:
    """Decode a video or animation into 480x480 JPEG frames using ffmpeg."""
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise OmenLcdError("video playback requires ffmpeg: sudo apt install ffmpeg")
    if not source.is_file():
        raise OmenLcdError(f"media file not found: {source}")
    if not 0 < fps <= 30:
        raise ValueError("fps must be greater than 0 and no more than 30")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")

    # ffmpeg's MJPEG qscale runs from 2 (best) to 31 (smallest).
    qscale = max(2, min(31, round(31 - quality * 29 / 100)))
    command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if loop:
        command.extend(("-stream_loop", "-1"))
    command.extend((
        "-i", str(source), "-an", "-vf", _video_filter(fit, rotate, fps),
        "-c:v", "mjpeg", "-q:v", str(qscale), "-f", "image2pipe", "pipe:1",
    ))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    completed = False
    try:
        assert process.stdout is not None
        yield from iter_mjpeg_frames(process.stdout)
        completed = True
    finally:
        if process.poll() is None:
            if completed:
                process.wait()
            else:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        assert process.stderr is not None
        error = process.stderr.read().decode("utf-8", errors="replace").strip()
        if completed and process.returncode:
            raise OmenLcdError(f"ffmpeg failed: {error or f'exit status {process.returncode}'}")


def play_video(
    lcd: OmenLcd,
    source: Path,
    *,
    fit: str = "contain",
    rotate: int = 0,
    fps: float = 10.0,
    quality: int = 85,
    loop: bool = False,
    duration: float | None = None,
) -> int:
    """Play media on an open LCD and return the number of displayed frames."""
    if duration is not None and duration <= 0:
        raise ValueError("duration must be greater than 0")
    started = time.monotonic()
    count = 0
    frames = ffmpeg_frames(
        source, fit=fit, rotate=rotate, fps=fps, quality=quality, loop=loop
    )
    try:
        for frame in frames:
            if duration is not None and time.monotonic() - started >= duration:
                break
            target = started + count / fps
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            lcd.upload_jpeg(frame, progress=False)
            count += 1
            print(f"\rPlaying: {count} frames", end="", flush=True)
    finally:
        frames.close()
    if count == 0:
        raise OmenLcdError("ffmpeg produced no video frames")
    print()
    return count


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control an HP OMEN 480x480 LCD pump cap")
    parser.add_argument("--device", help="hidraw data interface (normally auto-detected)")
    parser.add_argument(
        "--transport", choices=("hidraw", "usb"), default="hidraw",
        help="transport to use; usb directly claims interface 1",
    )
    parser.add_argument("--trace", action="store_true", help="print raw HID writes and replies")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="detect the LCD without writing to it")
    sub.add_parser("handshake", help="send HP's low-risk keepalive command")
    brightness = sub.add_parser("brightness", help="set LCD backlight brightness")
    brightness.add_argument("percent", type=int, metavar="0..100")
    on = sub.add_parser("on", help="turn the LCD backlight on")
    on.add_argument("--brightness", type=int, default=100, metavar="11..100")
    sub.add_parser("off", help="turn the LCD backlight off")
    image = sub.add_parser("image", help="display a still image")
    image.add_argument("path", type=Path)
    image.add_argument("--fit", choices=("contain", "cover"), default="contain")
    image.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    image.add_argument("--brightness", type=int, default=100, metavar="11..100")
    video = sub.add_parser("video", help="play a video or animated image using ffmpeg")
    video.add_argument("path", type=Path)
    video.add_argument("--fit", choices=("contain", "cover"), default="contain")
    video.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    video.add_argument("--brightness", type=int, default=100, metavar="11..100")
    video.add_argument("--fps", type=float, default=10.0, metavar="FPS")
    video.add_argument("--quality", type=int, default=85, metavar="1..100")
    video.add_argument("--loop", action="store_true", help="repeat until interrupted")
    video.add_argument("--duration", type=float, metavar="SECONDS")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        if args.command == "probe":
            devices = find_devices()
            if not devices:
                print("HP LCD-PUMP 03f0:7397: not found", file=sys.stderr)
                return 1
            for device in devices:
                access = "read/write access" if os.access(device, os.R_OK | os.W_OK) else "permission needed"
                print(f"HP LCD-PUMP 03f0:7397: {device} ({access})")
            return 0

        device = resolve_device(args.device) if args.transport == "hidraw" else Path("usb:03f0:7397")
        transport = open_lcd(args.transport, args.device, trace=args.trace)
        with transport as lcd:
            lcd.handshake()
            if args.command == "handshake":
                print(f"Handshake sent to {device}")
            elif args.command == "brightness":
                value = 0 if 0 < args.percent <= 10 else args.percent
                lcd.set_brightness(value)
                print(f"Brightness set to {value}%")
            elif args.command == "on":
                if not 11 <= args.brightness <= 100:
                    raise ValueError("brightness must be between 11 and 100")
                lcd.set_brightness(args.brightness)
                print(f"LCD backlight on at {args.brightness}%")
            elif args.command == "off":
                lcd.set_brightness(0)
                print("LCD backlight off")
            elif args.command == "image":
                if not 11 <= args.brightness <= 100:
                    raise ValueError("brightness must be between 11 and 100")
                jpeg = prepare_jpeg(args.path, args.fit, args.rotate)
                lcd.upload_jpeg(jpeg)
                lcd.set_brightness(args.brightness)
                print(f"Image displayed at {args.brightness}% brightness")
            elif args.command == "video":
                if not 11 <= args.brightness <= 100:
                    raise ValueError("brightness must be between 11 and 100")
                lcd.set_brightness(args.brightness)
                count = play_video(
                    lcd, args.path, fit=args.fit, rotate=args.rotate,
                    fps=args.fps, quality=args.quality, loop=args.loop,
                    duration=args.duration,
                )
                print(f"Video stopped after {count} frames")
        return 0
    except KeyboardInterrupt:
        print("\nPlayback stopped", file=sys.stderr)
        return 130
    except (OmenLcdError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
