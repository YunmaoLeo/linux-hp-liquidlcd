from pathlib import Path
import unittest

import omen_lcd


class PacketTests(unittest.TestCase):
    def test_handshake_uses_mapped_wire_command(self):
        self.assertEqual(omen_lcd.CMD_HANDSHAKE, 0x41)

    def test_image_packet_boundaries(self):
        data = bytes((i % 251 for i in range(omen_lcd.IMAGE_PAYLOAD_SIZE + 7)))
        reports = omen_lcd.build_image_reports(data)
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(len(report) == 1024 for report in reports))
        self.assertEqual(reports[0][0:2], bytes([2, 0x6E]))
        self.assertEqual(reports[0][2:6], len(data).to_bytes(4, "big"))
        self.assertEqual(reports[0][6:9], bytes([0, 0, 0]))
        self.assertEqual(reports[0][9:11], omen_lcd.IMAGE_PAYLOAD_SIZE.to_bytes(2, "big"))
        self.assertEqual(reports[1][6:9], bytes([0, 0, 1]))
        self.assertEqual(reports[1][9:11], bytes([0, 7]))
        self.assertEqual(reports[1][11:18], data[-7:])

    def test_image_upload_does_not_wait_for_device_ack(self):
        class RecordingLcd(omen_lcd.OmenLcd):
            def __init__(self):
                super().__init__(Path("test"))
                self.writes = []

            def _drain_input(self, duration):
                pass

            def _write(self, report, attempts=6):
                self.writes.append(report)

        lcd = RecordingLcd()
        data = b"x" * (omen_lcd.IMAGE_PAYLOAD_SIZE + 1)
        lcd.upload_jpeg(data, progress=False)
        self.assertEqual(len(lcd.writes), 2)


if __name__ == "__main__":
    unittest.main()
