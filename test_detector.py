import csv
import math
import tempfile
import unittest
from pathlib import Path

from main import BreathDetector, HybridBreathDetector, MeasurementRecorder


class BreathDetectorTest(unittest.TestCase):
    def test_tracks_breath_rate_without_manual_threshold(self):
        detector = BreathDetector(calibration_seconds=8.0)
        detected = 0
        for index in range(20 * 50):
            timestamp = index / 20
            raw = 1450 + 230 * math.sin(timestamp / 4.0 * math.tau)
            detected += detector.process(round(raw), timestamp)

        self.assertTrue(detector.calibrated)
        self.assertGreater(detector.threshold, detector.release_level)
        self.assertGreaterEqual(detected, 8)
        self.assertLessEqual(detected, 12)
        self.assertAlmostEqual(detector.rate(), 15.0, delta=1.0)

    def test_threshold_follows_baseline_drift(self):
        detector = BreathDetector(calibration_seconds=4.0)
        threshold_before_drift = None
        for index in range(20 * 50):
            timestamp = index / 20
            baseline = 1000 if timestamp < 20 else 1450
            raw = baseline + 180 * math.sin(timestamp / 5.0 * math.tau)
            detector.process(round(raw), timestamp)
            if index == 20 * 19:
                threshold_before_drift = detector.threshold

        self.assertIsNotNone(threshold_before_drift)
        self.assertGreater(detector.threshold, threshold_before_drift + 250)


class HybridBreathDetectorTest(unittest.TestCase):
    def test_acf_and_spectrum_confirm_threshold_rate(self):
        detector = HybridBreathDetector()
        for index in range(20 * 60):
            timestamp = index / 20
            raw = 1450 + 220 * math.sin(timestamp / 4.0 * math.tau)
            detector.process(round(raw), timestamp)

        self.assertEqual(detector.state, "agreement")
        self.assertIsNotNone(detector.rate())
        self.assertAlmostEqual(detector.rate(), 15.0, delta=0.7)

    def test_old_rate_is_cleared_after_no_recent_breath(self):
        detector = HybridBreathDetector()
        for index in range(20 * 45):
            timestamp = index / 20
            raw = 1450 + 220 * math.sin(timestamp / 4.0 * math.tau)
            detector.process(round(raw), timestamp)
        self.assertIsNotNone(detector.rate())

        for index in range(20 * 16):
            timestamp = 45 + index / 20
            detector.process(1450, timestamp)

        self.assertEqual(detector.state, "no_recent_breath")
        self.assertIsNone(detector.rate())

    def test_consensus_can_retain_low_amplitude_breathing(self):
        detector = HybridBreathDetector()
        for index in range(20 * 60):
            timestamp = index / 20
            raw = 1450 + 42 * math.sin(timestamp / 4.0 * math.tau)
            detector.process(round(raw), timestamp)

        self.assertIsNotNone(detector.rate())
        self.assertAlmostEqual(detector.rate(), 15.0, delta=1.0)


class MeasurementRecorderTest(unittest.TestCase):
    def test_timestamp_starts_at_zero_and_unmarked_rows_are_nan(self):
        recorder = MeasurementRecorder()
        recorder.add(12_500, 1440)
        recorder.add(12_550, 1452)

        self.assertEqual(recorder.rows, [(0, 1440, "NaN"), (50, 1452, "NaN")])

    def test_valid_mark_is_written_to_next_sample_only(self):
        recorder = MeasurementRecorder()
        self.assertTrue(recorder.queue_mark("3"))
        recorder.add(1000, 1400)
        recorder.add(1050, 1410)

        self.assertEqual(recorder.rows[0], (0, 1400, "3"))
        self.assertEqual(recorder.rows[1], (50, 1410, "NaN"))
        for invalid_mark in ("0", "a", "10", ""):
            self.assertFalse(recorder.queue_mark(invalid_mark))

    def test_device_timestamp_wraparound_and_csv_output(self):
        recorder = MeasurementRecorder()
        recorder.add(0xFFFFFFF0, 1500)
        recorder.queue_mark(9)
        recorder.add(0x00000022, 1510)

        self.assertEqual(recorder.rows[1], (50, 1510, "9"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "measurement.csv"
            recorder.save_csv(output_path)
            with output_path.open(encoding="utf-8-sig", newline="") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(rows[0], ["time_ms", "raw_pressure", "mark"])
        self.assertEqual(rows[1], ["0", "1500", "NaN"])
        self.assertEqual(rows[2], ["50", "1510", "9"])


if __name__ == "__main__":
    unittest.main()
