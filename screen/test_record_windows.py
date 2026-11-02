#!/usr/bin/env python3
"""The Windows recorder's pure parts, which run on any OS: dshow device
parsing and choice, the ffmpeg command, the process-tree walk, record.pid.

    python3 screen/test_record_windows.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import record_windows as rw  # noqa: E402

NEW_FFMPEG = '''[dshow @ 0000] "Integrated Camera" (video)
[dshow @ 0000]   Alternative name "@device_pnp_\\\\?\\usb"
[dshow @ 0000] "Microphone Array (Realtek(R) Audio)" (audio)
[dshow @ 0000] "CABLE Output (VB-Audio Virtual Cable)" (audio)
[dshow @ 0000]   Alternative name "@device_cm_{33D9A762}"
'''
OLD_FFMPEG = '''[dshow @ 0000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000]  "Integrated Camera"
[dshow @ 0000] DirectShow audio devices
[dshow @ 0000]  "CABLE Output (VB-Audio Virtual Cable)"
[dshow @ 0000]  "Microphone (Realtek)"
'''


class DeviceTest(unittest.TestCase):
    def test_both_ffmpeg_listing_formats(self):
        self.assertEqual(rw.parse_dshow_audio_devices(NEW_FFMPEG),
                         ["Microphone Array (Realtek(R) Audio)",
                          "CABLE Output (VB-Audio Virtual Cable)"])
        self.assertEqual(rw.parse_dshow_audio_devices(OLD_FFMPEG),
                         ["CABLE Output (VB-Audio Virtual Cable)", "Microphone (Realtek)"])

    def test_the_cable_is_found_even_renamed_never_the_microphone(self):
        avail = ["Microphone (Realtek)", "CABLE Output (VB-Audio Virtual Cable 16ch)"]
        self.assertEqual(rw.pick_audio_device(avail, rw.DEFAULT_AUDIO_DEVICE), avail[1])
        self.assertIsNone(rw.pick_audio_device(["Microphone (Realtek)"], rw.DEFAULT_AUDIO_DEVICE))
        self.assertEqual(rw.pick_audio_device(["X", "Y"], "Y"), "Y")


class CommandTest(unittest.TestCase):
    def test_video_records_the_window_by_title_with_the_linux_encoder(self):
        cmd = rw.ffmpeg_command("ffmpeg", "out.mp4", "video", "CABLE Output (VB-Audio Virtual Cable)",
                                "Meet - abc — Mozilla Firefox", "1920x1080", "15")
        s = " ".join(cmd)
        self.assertIn("-f gdigrab", s)
        self.assertIn("title=Meet - abc — Mozilla Firefox", cmd)
        self.assertIn("audio=CABLE Output (VB-Audio Virtual Cable)", cmd)
        for part in ("-preset ultrafast", "-crf 28", "-b:a 128k", "-pix_fmt yuv420p",
                     "-draw_mouse 0", "scale=1920:1080"):
            self.assertIn(part, s)
        self.assertEqual(cmd[-1], "out.mp4")

    def test_audio_only_has_no_picture(self):
        cmd = rw.ffmpeg_command("ffmpeg", "out.m4a", "audio", "CABLE Output (VB-Audio Virtual Cable)")
        self.assertNotIn("gdigrab", cmd)
        self.assertIn("aac", cmd)


class TreeAndPidFileTest(unittest.TestCase):
    def test_descendants(self):
        parents = {10: 1, 11: 10, 12: 11, 13: 1, 14: 12}
        self.assertEqual(rw.descendants_of(10, parents), {10, 11, 12, 14})

    def test_pid_file_is_record_screens_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "record.pid"
            rw.write_pid_file(p, record=1, join=2, ffmpeg=None, display="window", sink="CABLE")
            self.assertEqual(p.read_text(), "record=1\njoin=2\ndisplay=window\nsink=CABLE\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
