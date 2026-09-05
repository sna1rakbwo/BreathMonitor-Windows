import asyncio
import csv
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from statistics import median

# Bleak's WinRT backend must run in an MTA thread. Set this before any package
# can import pythoncom and initialize the process as STA.
if sys.platform == "win32":
    sys.coinit_flags = 0

import numpy as np
import pyqtgraph as pg
from bleak import BleakClient, BleakScanner
from scipy.signal import butter, find_peaks, sosfiltfilt, welch
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


DATA_PATTERN = re.compile(r"^(\d+),(\d+),(\d+)$")
DEVICE_NAME = "BreathSensor-ESP32"
SERVICE_UUID = "9b3e2f50-7b2f-4c60-a3b9-9f71c2b4f101"
CHARACTERISTIC_UUID = "9b3e2f51-7b2f-4c60-a3b9-9f71c2b4f101"
APP_VERSION = "1.1.0"


def resource_path(relative_path: str) -> Path:
    """Resolve bundled assets both in source and in a PyInstaller app."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


class BleReader(QThread):
    sample_received = Signal(int, int, int)
    connection_opened = Signal(str)
    connection_error = Signal(str)
    connection_closed = Signal()

    def __init__(self, device_name: str = DEVICE_NAME, parent=None):
        super().__init__(parent)
        self.device_name = device_name
        self._client = None
        self._stopping = False
        self._disconnected = False

    def run(self):
        try:
            asyncio.run(self._connect_and_listen())
        except Exception as exc:
            if not self._stopping:
                self.connection_error.emit(str(exc))
        finally:
            self.connection_closed.emit()

    async def _connect_and_listen(self):
        device = await BleakScanner.find_device_by_filter(
            lambda candidate, advertisement: (
                candidate.name == self.device_name
                or advertisement.local_name == self.device_name
            ),
            timeout=10.0,
        )
        if device is None:
            raise RuntimeError(
                f"未找到 {self.device_name}。请确认 ESP32 已由移动电源供电且距离电脑较近。"
            )

        self._disconnected = False
        async with BleakClient(device, disconnected_callback=self._on_disconnect) as client:
            self._client = client
            await client.start_notify(CHARACTERISTIC_UUID, self._on_notification)
            self.connection_opened.emit(device.name or self.device_name)

            while not self.isInterruptionRequested() and not self._disconnected:
                await asyncio.sleep(0.15)

            if client.is_connected:
                await client.stop_notify(CHARACTERISTIC_UUID)
        self._client = None

    def _on_notification(self, _sender, data: bytearray):
        line = bytes(data).decode("utf-8", errors="ignore").strip()
        match = DATA_PATTERN.match(line)
        if match:
            device_ms, raw, millivolts = map(int, match.groups())
            self.sample_received.emit(device_ms, raw, millivolts)

    def _on_disconnect(self, _client):
        self._disconnected = True

    def stop(self):
        self._stopping = True
        self.requestInterruption()


class BreathDetector:
    """用滚动稳健统计量自动追踪基线、幅度和呼吸检测阈值。"""

    def __init__(self, calibration_seconds: float = 8.0, window_seconds: float = 30.0):
        self.calibration_seconds = calibration_seconds
        self.window_seconds = window_seconds
        self.history = deque(maxlen=1200)
        self.breath_times = deque(maxlen=20)
        self.reset()

    def reset(self):
        self.history.clear()
        self.breath_times.clear()
        self.started_at = None
        self.last_breath_at = None
        self.last_update_at = 0.0
        self.smoothed = None
        self.baseline = 0.0
        self.threshold = 0.0
        self.release_level = 0.0
        self.amplitude = 0.0
        self.noise = 0.0
        self.quality = 0
        self.armed = False

    @property
    def calibrated(self) -> bool:
        if self.started_at is None or not self.history:
            return False
        return self.history[-1][0] - self.started_at >= self.calibration_seconds

    @property
    def calibration_progress(self) -> int:
        if self.started_at is None or not self.history:
            return 0
        elapsed = self.history[-1][0] - self.started_at
        return min(100, max(0, int(elapsed / self.calibration_seconds * 100)))

    def process(self, raw: int, timestamp: float) -> bool:
        if self.started_at is None:
            self.started_at = timestamp
        self.smoothed = float(raw) if self.smoothed is None else self.smoothed + 0.16 * (raw - self.smoothed)
        self.history.append((timestamp, self.smoothed))

        cutoff = timestamp - self.window_seconds
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

        if timestamp - self.last_update_at >= 0.5 and len(self.history) >= 20:
            self._update_dynamic_threshold()
            self.last_update_at = timestamp

        if not self.calibrated:
            return False

        if self.smoothed <= self.release_level:
            self.armed = True

        enough_time = self.last_breath_at is None or timestamp - self.last_breath_at >= 1.5
        if self.armed and enough_time and self.smoothed >= self.threshold:
            self.armed = False
            self.last_breath_at = timestamp
            self.breath_times.append(timestamp)
            return True
        return False

    def _update_dynamic_threshold(self):
        temporal_values = [value for _, value in self.history]
        values = sorted(temporal_values)

        def percentile(fraction: float) -> float:
            position = (len(values) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(values) - 1)
            weight = position - lower
            return values[lower] * (1 - weight) + values[upper] * weight

        low = percentile(0.15)
        high = percentile(0.85)
        center = median(values)
        robust_range = max(0.0, high - low)
        differences = [
            abs(b - a) for a, b in zip(temporal_values[:-1], temporal_values[1:])
        ]
        # 相邻采样变化的中位数用于估计噪声；最低跨度防止上下阈值重合。
        noise = max(1.0, median(differences) * 1.4826 if differences else 1.0)
        minimum_span = max(8.0, noise * 6.0)
        effective_range = max(robust_range, minimum_span)
        target_release = low + effective_range * 0.30
        target_threshold = low + effective_range * 0.68

        blend = 1.0 if self.threshold == 0 else 0.20
        self.baseline += blend * (center - self.baseline)
        self.amplitude += blend * (robust_range - self.amplitude)
        self.noise += blend * (noise - self.noise)
        self.release_level += blend * (target_release - self.release_level)
        self.threshold += blend * (target_threshold - self.threshold)
        signal_ratio = self.amplitude / max(self.noise * 6.0, 1.0)
        self.quality = min(100, max(0, int(signal_ratio * 65)))

    def rate(
        self,
        since: float | None = None,
        now: float | None = None,
        stale_after: float = 12.0,
    ) -> float | None:
        if (
            now is not None
            and self.last_breath_at is not None
            and now - self.last_breath_at > stale_after
        ):
            return None
        times = list(self.breath_times)
        if since is not None:
            times = [value for value in times if value >= since]
        if len(times) < 2:
            return None

        intervals = [b - a for a, b in zip(times, times[1:]) if 1.5 <= b - a <= 10.0]
        if not intervals:
            return None
        return 60.0 / median(intervals[-6:])


class ConsensusBreathEstimator:
    """用 ACF 周期和 Welch 主频共识复核呼吸频率。"""

    def __init__(self, sample_rate: int = 20, window_seconds: float = 24.0):
        self.sample_rate = sample_rate
        self.window_seconds = window_seconds
        self.history = deque(maxlen=int(sample_rate * (window_seconds + 4)))
        self.filter = butter(
            3, (0.05, 0.8), btype="bandpass", fs=sample_rate, output="sos"
        )
        self.reset()

    def reset(self):
        self.history.clear()
        self.last_evaluation_at = None
        self.rate = None
        self.confidence = 0.0
        self.ready = False
        self.acf_rate = None
        self.spectrum_rate = None
        self.recent_activity = 0.0

    def process(self, value: float, timestamp: float):
        self.history.append((timestamp, float(value)))
        if (
            self.last_evaluation_at is not None
            and timestamp - self.last_evaluation_at < 1.0
        ):
            return
        self.last_evaluation_at = timestamp
        self._evaluate(timestamp)

    def _evaluate(self, timestamp: float):
        start = timestamp - self.window_seconds
        samples = [(stamp, value) for stamp, value in self.history if stamp >= start - 0.1]
        if not samples or samples[0][0] > start + 0.5:
            self.ready = False
            self.rate = None
            return

        sample_times = np.asarray([stamp for stamp, _ in samples], dtype=float)
        sample_values = np.asarray([value for _, value in samples], dtype=float)
        target_times = np.arange(start, timestamp, 1.0 / self.sample_rate)
        if len(target_times) < int(self.sample_rate * self.window_seconds * 0.95):
            self.ready = False
            self.rate = None
            return

        regular = np.interp(target_times, sample_times, sample_values)
        filtered = sosfiltfilt(self.filter, regular)
        filtered -= np.mean(filtered)
        energy = float(np.dot(filtered, filtered))
        self.ready = True
        if energy < 1e-6:
            self.recent_activity = 0.0
            self._reject()
            return
        recent_samples = min(len(regular), int(self.sample_rate * 6.0))
        recent_raw = regular[-recent_samples:]
        positions = np.arange(recent_samples, dtype=float)
        trend = np.polyval(np.polyfit(positions, recent_raw, 1), positions)
        recent_detrended = recent_raw - trend
        recent_span = float(
            np.quantile(recent_detrended, 0.85)
            - np.quantile(recent_detrended, 0.15)
        )
        recent_noise = max(
            1.0,
            float(np.median(np.abs(np.diff(recent_detrended)))) * 1.4826,
        )
        self.recent_activity = recent_span / recent_noise

        autocorrelation = np.correlate(filtered, filtered, mode="full")[len(filtered) - 1 :]
        autocorrelation /= max(float(autocorrelation[0]), 1e-9)
        min_lag = int(self.sample_rate * 1.25)  # 48 次/分
        max_lag = int(self.sample_rate * 10.0)  # 6 次/分
        segment = autocorrelation[min_lag : max_lag + 1]
        peaks, _ = find_peaks(segment, distance=int(self.sample_rate * 0.8))
        if not len(peaks):
            self._reject()
            return
        best_peak = int(peaks[np.argmax(segment[peaks])])
        lag = min_lag + best_peak
        acf_confidence = float(segment[best_peak])
        self.acf_rate = 60.0 * self.sample_rate / lag

        frequencies, power = welch(
            filtered,
            fs=self.sample_rate,
            window="hann",
            nperseg=len(filtered),
            noverlap=0,
            detrend="linear",
        )
        band = (frequencies >= 0.08) & (frequencies <= 0.8)
        band_frequencies = frequencies[band]
        band_power = power[band]
        total_power = float(np.sum(band_power))
        if not len(band_power) or total_power <= 0:
            self._reject()
            return
        peak = int(np.argmax(band_power))
        peak_frequency = float(band_frequencies[peak])
        if 0 < peak < len(band_power) - 1:
            left, center, right = np.log(band_power[peak - 1 : peak + 2] + 1e-12)
            denominator = left - 2 * center + right
            if abs(denominator) > 1e-12:
                peak_frequency += (
                    0.5
                    * (left - right)
                    / denominator
                    * (band_frequencies[1] - band_frequencies[0])
                )
        self.spectrum_rate = 60.0 * peak_frequency
        local_power = float(
            np.sum(band_power[max(0, peak - 1) : min(len(band_power), peak + 2)])
        )
        spectral_concentration = local_power / total_power
        agreement = abs(self.acf_rate - self.spectrum_rate)
        self.confidence = min(
            1.0,
            max(0.0, acf_confidence) * 0.65 + spectral_concentration * 0.35,
        )
        if (
            acf_confidence < 0.30
            or spectral_concentration < 0.42
            or agreement > 2.5
        ):
            self.rate = None
            return
        self.rate = (self.acf_rate + self.spectrum_rate) / 2.0

    def _reject(self):
        self.rate = None
        self.confidence = 0.0
        self.acf_rate = None
        self.spectrum_rate = None


class HybridBreathDetector:
    """阈值法负责及时性，共识法负责复核与拒绝不可靠结果。"""

    def __init__(self):
        self.threshold_detector = BreathDetector()
        self.consensus_detector = ConsensusBreathEstimator()
        self.output_rate = None
        self.state = "calibrating"

    def reset(self):
        self.threshold_detector.reset()
        self.consensus_detector.reset()
        self.output_rate = None
        self.state = "calibrating"

    def process(self, raw: int, timestamp: float) -> bool:
        detected = self.threshold_detector.process(raw, timestamp)
        self.consensus_detector.process(raw, timestamp)
        self._update_output(timestamp)
        return detected

    def _update_output(self, timestamp: float):
        threshold_rate = self.threshold_detector.rate(
            since=max(0.0, timestamp - 60.0), now=timestamp
        )
        consensus_rate = self.consensus_detector.rate
        last_breath = self.threshold_detector.last_breath_at

        if not self.threshold_detector.calibrated:
            self.output_rate = None
            self.state = "calibrating"
            return
        if last_breath is None or timestamp - last_breath > 12.0:
            if (
                consensus_rate is not None
                and self.consensus_detector.recent_activity >= 5.5
            ):
                self.output_rate = consensus_rate
                self.state = "consensus_only"
            else:
                self.output_rate = None
                self.state = "no_recent_breath"
            return
        if not self.consensus_detector.ready:
            self.output_rate = threshold_rate
            self.state = "verifier_warming" if threshold_rate is not None else "detecting"
            return
        if threshold_rate is not None and consensus_rate is not None:
            if abs(threshold_rate - consensus_rate) <= 2.5:
                self.output_rate = threshold_rate * 0.65 + consensus_rate * 0.35
                self.state = "agreement"
            else:
                self.output_rate = None
                self.state = "disagreement"
            return
        if threshold_rate is not None and self.threshold_detector.quality >= 40:
            self.output_rate = threshold_rate
            self.state = "threshold_only"
            return
        if consensus_rate is not None:
            self.output_rate = consensus_rate
            self.state = "consensus_only"
            return
        self.output_rate = None
        self.state = "low_quality"

    def rate(self, **_kwargs) -> float | None:
        return self.output_rate

    def __getattr__(self, name):
        # 保持绘图和 UI 对动态阈值属性的现有访问方式。
        return getattr(self.threshold_detector, name)


class MeasurementRecorder:
    """保存一次测量的原始压力数据和键盘标记。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.rows = []
        self.first_device_ms = None
        self.pending_marks = deque()

    def queue_mark(self, mark) -> bool:
        mark_text = str(mark)
        if len(mark_text) != 1 or mark_text not in "123456789":
            return False
        self.pending_marks.append(mark_text)
        return True

    def add(self, device_ms: int, raw: int):
        device_ms = int(device_ms) & 0xFFFFFFFF
        if self.first_device_ms is None:
            self.first_device_ms = device_ms
        elapsed_ms = (device_ms - self.first_device_ms) & 0xFFFFFFFF
        mark = self.pending_marks.popleft() if self.pending_marks else "NaN"
        row = (elapsed_ms, int(raw), mark)
        self.rows.append(row)
        return row

    def save_csv(self, path: str | Path):
        # utf-8-sig 让 Windows Excel 直接打开时也能正确识别编码。
        with Path(path).open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(("time_ms", "raw_pressure", "mark"))
            writer.writerows(self.rows)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str, unit: str = "", accent: str = "#38BDF8"):
        super().__init__()
        self.setObjectName("metricCard")

        title_label = QLabel(title)
        title_label.setObjectName("metricTitle")

        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.value_label.setStyleSheet(f"color: {accent};")

        unit_label = QLabel(unit)
        unit_label.setObjectName("metricUnit")

        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(7)
        value_row.addWidget(self.value_label)
        value_row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBottom)
        value_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(6)
        layout.addWidget(title_label)
        layout.addLayout(value_row)

    def set_value(self, value: str):
        self.value_label.setText(value)


class RespirationWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.reader = None
        self.connected = False
        self.detector = HybridBreathDetector()
        self.recorder = MeasurementRecorder()
        self.measurement_saved = True

        self.timestamps = deque(maxlen=4000)
        self.raw_values = deque(maxlen=4000)
        self.breath_markers = deque(maxlen=100)

        self.monitoring_active = False
        self.monitoring_started_at = 0.0

        self.setWindowTitle("呼吸监测")
        self.resize(1180, 760)
        self.setMinimumSize(940, 650)
        self._build_ui()
        self._apply_style()
        self._build_mark_shortcuts()

        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.update_plot)
        self.plot_timer.start(100)

        self.monitoring_timer = QTimer(self)
        self.monitoring_timer.timeout.connect(self.update_monitoring)
        self.monitoring_timer.start(250)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("呼吸监测")
        title.setObjectName("pageTitle")
        subtitle = QLabel("连接设备后自动校准并开始监测")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QLabel("设备未连接")
        self.status_label.setObjectName("statusText")
        status_box = QHBoxLayout()
        status_box.setSpacing(8)
        status_box.addWidget(self.status_dot)
        status_box.addWidget(self.status_label)
        header.addLayout(status_box)
        root.addLayout(header)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(14)
        self.rate_card = MetricCard("呼吸频率", "--", "次/分", "#167D70")
        self.raw_card = MetricCard("实时压力", "--", "ADC", "#334155")
        self.threshold_card = MetricCard("动态阈值", "--", "ADC", "#167D70")
        metrics.addWidget(self.rate_card, 0, 0)
        metrics.addWidget(self.raw_card, 0, 1)
        metrics.addWidget(self.threshold_card, 0, 2)
        metrics.setColumnStretch(0, 1)
        metrics.setColumnStretch(1, 1)
        metrics.setColumnStretch(2, 1)
        root.addLayout(metrics)

        content = QHBoxLayout()
        content.setSpacing(18)

        plot_panel = QFrame()
        plot_panel.setObjectName("panel")
        plot_layout = QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(18, 17, 18, 14)
        plot_layout.setSpacing(10)

        plot_header = QHBoxLayout()
        plot_title = QLabel("实时呼吸曲线")
        plot_title.setObjectName("panelTitle")
        self.plot_hint = QLabel("最近 30 秒 · 阈值自动适应")
        self.plot_hint.setObjectName("mutedText")
        plot_header.addWidget(plot_title)
        plot_header.addStretch()
        plot_header.addWidget(self.plot_hint)
        plot_layout.addLayout(plot_header)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#FFFFFF")
        self.plot.showGrid(x=True, y=True, alpha=0.11)
        self.plot.setXRange(-30, 0, padding=0)
        self.plot.setYRange(0, 3000, padding=0.03)
        self.plot.setLabel("left", "压力信号 (raw)")
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.getAxis("left").setTextPen("#64748B")
        self.plot.getAxis("bottom").setTextPen("#64748B")
        self.plot.getAxis("left").setPen("#CBD5E1")
        self.plot.getAxis("bottom").setPen("#CBD5E1")
        self.curve = self.plot.plot([], [], pen=pg.mkPen("#167D70", width=2.3))
        self.marker_plot = self.plot.plot(
            [], [], pen=None, symbol="o", symbolSize=8,
            symbolBrush=pg.mkBrush("#167D70"), symbolPen=pg.mkPen("#FFFFFF", width=1)
        )
        self.threshold_line = pg.InfiniteLine(
            pos=self.detector.threshold,
            angle=0,
            pen=pg.mkPen("#94A3B8", width=1, style=Qt.PenStyle.DashLine),
            label="自动阈值",
            labelOpts={"color": "#64748B", "position": 0.92},
        )
        self.plot.addItem(self.threshold_line)
        self.release_line = pg.InfiniteLine(
            pos=self.detector.release_level,
            angle=0,
            pen=pg.mkPen("#CBD5E1", width=1, style=Qt.PenStyle.DotLine),
        )
        self.plot.addItem(self.release_line)
        plot_layout.addWidget(self.plot, 1)
        content.addWidget(plot_panel, 1)

        controls = QFrame()
        controls.setObjectName("panel")
        controls.setFixedWidth(292)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(20, 18, 20, 20)
        controls_layout.setSpacing(13)

        self.control_title = QLabel("开始监测")
        self.control_title.setObjectName("panelTitle")
        controls_layout.addWidget(self.control_title)

        self.guide = QLabel("1  打开 ESP32 电源\n2  点击连接，自然呼吸约 8 秒")
        self.guide.setObjectName("guideText")
        self.guide.setWordWrap(True)
        controls_layout.addWidget(self.guide)

        self.connect_button = QPushButton("连接设备")
        self.connect_button.clicked.connect(self.toggle_connection)
        controls_layout.addWidget(self.connect_button)

        self.finish_button = QPushButton("结束并保存")
        self.finish_button.setObjectName("finishButton")
        self.finish_button.setEnabled(False)
        self.finish_button.clicked.connect(self.finish_or_save_measurement)
        controls_layout.addWidget(self.finish_button)

        self.mark_status = QLabel("键盘标记：按 1–9 写入下一条采样\n未标记样本保存为 NaN")
        self.mark_status.setObjectName("markBox")
        self.mark_status.setWordWrap(True)
        controls_layout.addWidget(self.mark_status)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("divider")
        controls_layout.addWidget(divider)

        auto_title = QLabel("自动阈值")
        auto_title.setObjectName("fieldLabel")
        controls_layout.addWidget(auto_title)

        self.auto_status = QLabel("连接后自动学习呼吸幅度\n无需手动设置")
        self.auto_status.setObjectName("autoBox")
        self.auto_status.setWordWrap(True)
        controls_layout.addWidget(self.auto_status)

        self.elapsed_label = QLabel("监测时长  00:00:00")
        self.elapsed_label.setObjectName("elapsedBox")
        controls_layout.addWidget(self.elapsed_label)

        self.monitoring_result = QLabel("等待连接 BreathSensor-ESP32")
        self.monitoring_result.setObjectName("resultBox")
        self.monitoring_result.setWordWrap(True)
        controls_layout.addWidget(self.monitoring_result)
        controls_layout.addStretch()

        content.addWidget(controls)
        root.addLayout(content, 1)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #F4F6F7;
                color: #172126;
                font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
                font-size: 14px;
            }
            QLabel { background: transparent; }
            QLabel#pageTitle { font-size: 25px; font-weight: 700; color: #172126; }
            QLabel#pageSubtitle { font-size: 13px; color: #66757C; }
            QLabel#statusDot { color: #94A3B8; font-size: 16px; }
            QLabel#statusText { color: #526168; font-weight: 600; }
            QFrame#metricCard, QFrame#panel {
                background: #FFFFFF;
                border: 1px solid #DCE3E5;
                border-radius: 11px;
            }
            QLabel#metricTitle, QLabel#mutedText { color: #718087; font-size: 12px; }
            QLabel#metricValue { font-size: 29px; font-weight: 700; }
            QLabel#metricUnit { color: #718087; font-size: 12px; padding-bottom: 4px; }
            QLabel#panelTitle { font-size: 16px; font-weight: 700; color: #172126; }
            QLabel#fieldLabel { color: #526168; font-size: 12px; font-weight: 650; }
            QLabel#guideText {
                color: #526168;
                font-size: 13px;
                line-height: 1.65;
                padding: 6px 1px 7px;
            }
            QLabel#autoBox {
                background: #E7F4F1;
                border: 1px solid #CDE6E1;
                border-radius: 8px;
                color: #10675D;
                font-size: 12px;
                padding: 11px;
            }
            QLabel#resultBox {
                background: #F8FAFA;
                border: 1px solid #E1E7E9;
                border-radius: 8px;
                color: #526168;
                padding: 12px;
            }
            QLabel#markBox {
                background: #F8FAFA;
                border: 1px solid #E1E7E9;
                border-radius: 8px;
                color: #526168;
                font-size: 12px;
                padding: 9px 11px;
            }
            QLabel#elapsedBox {
                background: #FFFFFF;
                border: 1px solid #DCE3E5;
                border-radius: 7px;
                color: #334155;
                font-size: 15px;
                font-weight: 650;
                padding: 10px;
            }
            QPushButton {
                background: #167D70;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 11px 12px;
                font-weight: 650;
            }
            QPushButton:hover { background: #10675D; }
            QPushButton:pressed { background: #0D5B52; }
            QPushButton:disabled { background: #D6DEDF; color: #829095; }
            QPushButton#finishButton {
                background: #FFFFFF;
                color: #167D70;
                border: 1px solid #9FCFC7;
            }
            QPushButton#finishButton:hover { background: #E7F4F1; }
            QPushButton#finishButton:disabled {
                background: #F1F4F4;
                color: #9AA6AA;
                border: 1px solid #DCE3E5;
            }
            QFrame#divider { color: #E1E7E9; background: #E1E7E9; max-height: 1px; }
            """
        )

    def _build_mark_shortcuts(self):
        self.mark_shortcuts = []
        for digit in "123456789":
            shortcut = QShortcut(QKeySequence(digit), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(
                lambda selected_digit=digit: self.queue_mark(selected_digit)
            )
            self.mark_shortcuts.append(shortcut)

    def queue_mark(self, digit: str):
        if not self.monitoring_active:
            return
        if self.recorder.queue_mark(digit):
            pending = len(self.recorder.pending_marks)
            self.mark_status.setText(
                f"已输入标记 {digit} · 将写入下一条采样"
                + (f"\n待写入标记：{pending} 个" if pending > 1 else "")
            )

    def toggle_connection(self):
        if self.connected or (self.reader and self.reader.isRunning()):
            self.disconnect_ble()
            return

        # 断线后如果用户取消了保存，先保护旧数据，再允许新测量覆盖内存记录。
        if self.recorder.rows and not self.measurement_saved:
            if not self.save_measurement():
                return

        self.connect_button.setEnabled(False)
        self.connect_button.setText("正在查找设备…")
        self.monitoring_result.setText("正在搜索附近的 BreathSensor-ESP32…")
        self.reader = BleReader(DEVICE_NAME)
        self.reader.sample_received.connect(self.handle_sample)
        self.reader.connection_opened.connect(self.on_connected)
        self.reader.connection_error.connect(self.on_connection_error)
        self.reader.connection_closed.connect(self.on_disconnected)
        self.reader.finished.connect(self.reader_finished)
        self.reader.start()

    def disconnect_ble(self):
        if self.reader and self.reader.isRunning():
            self.reader.stop()
            self.connect_button.setEnabled(False)
            self.connect_button.setText("正在断开…")

    def on_connected(self, device_name: str):
        self.connected = True
        self.status_dot.setStyleSheet("color: #167D70;")
        self.status_label.setText(f"已连接 · {device_name}")
        self.connect_button.setEnabled(True)
        self.connect_button.setText("断开设备")
        self.control_title.setText("监测进行中")
        self.guide.setText("设备已连接 · 已自动开始\n保持自然呼吸，按 1–9 可标记")
        self.start_monitoring()

    def on_connection_error(self, message: str):
        guidance = ""
        if sys.platform == "win32":
            guidance = (
                "\n\n请确认：\n"
                "1. Windows 蓝牙已打开；\n"
                "2. 电脑支持低功耗蓝牙 BLE；\n"
                "3. ESP32 已通电且没有连接到其他设备。"
            )
        QMessageBox.critical(
            self,
            "蓝牙连接失败",
            f"无法连接 BreathSensor-ESP32：\n{message}{guidance}",
        )

    def on_disconnected(self):
        self.connected = False
        self.status_dot.setStyleSheet("color: #94A3B8;")
        self.status_label.setText("设备未连接")
        self.connect_button.setEnabled(True)
        self.connect_button.setText("连接设备")
        self.control_title.setText("开始监测")
        self.guide.setText("1  打开 ESP32 电源\n2  点击连接，自然呼吸约 8 秒")
        if self.monitoring_active:
            self.stop_monitoring(interrupted=True)
            QTimer.singleShot(0, self.save_measurement)

    def reader_finished(self):
        if self.reader:
            self.reader.deleteLater()
            self.reader = None

    def handle_sample(self, device_ms: int, raw: int, _millivolts: int):
        now = time.monotonic()
        self.raw_card.set_value(str(raw))

        if self.monitoring_active:
            self.timestamps.append(now)
            self.raw_values.append(raw)
            self.recorder.add(device_ms, raw)
            if not self.recorder.pending_marks:
                self.mark_status.setText("键盘标记：按 1–9 写入下一条采样\n未标记样本保存为 NaN")
            if self.detector.process(raw, now):
                self.breath_markers.append((now, raw))

            rate = self.detector.rate(since=now - 60.0)
            self.rate_card.set_value(f"{rate:.1f}" if rate is not None else "--")
            self.threshold_card.set_value(
                str(int(round(self.detector.threshold))) if self.detector.threshold else "--"
            )

    def update_plot(self):
        if not self.timestamps:
            return
        now = time.monotonic()
        cutoff = now - 30.0

        points = [(stamp - now, raw) for stamp, raw in zip(self.timestamps, self.raw_values) if stamp >= cutoff]
        if points:
            x_values, y_values = zip(*points)
            self.curve.setData(x_values, y_values)
            visible_max = max(y_values)
            upper = max(900, min(4095, int(visible_max * 1.22 + 100)))
            self.plot.setYRange(0, upper, padding=0)
            if self.detector.threshold:
                self.threshold_line.setValue(self.detector.threshold)
                self.release_line.setValue(self.detector.release_level)

        markers = [(stamp - now, raw) for stamp, raw in self.breath_markers if stamp >= cutoff]
        if markers:
            marker_x, marker_y = zip(*markers)
            self.marker_plot.setData(marker_x, marker_y)
        else:
            self.marker_plot.setData([], [])

    def start_monitoring(self):
        now = time.monotonic()
        self.monitoring_active = True
        self.monitoring_started_at = now
        self.detector.reset()
        self.recorder.reset()
        self.measurement_saved = False
        self.timestamps.clear()
        self.raw_values.clear()
        self.breath_markers.clear()
        self.rate_card.set_value("--")
        self.raw_card.set_value("--")
        self.threshold_card.set_value("--")
        self.elapsed_label.setText("监测时长  00:00:00")
        self.auto_status.setText("正在自动校准 · 0%\n请保持自然呼吸")
        self.monitoring_result.setText("正在学习您的呼吸幅度，约需 8 秒。")
        self.finish_button.setText("结束并保存")
        self.finish_button.setEnabled(True)
        self.mark_status.setText("键盘标记：按 1–9 写入下一条采样\n未标记样本保存为 NaN")

    def update_monitoring(self):
        if not self.monitoring_active:
            return
        elapsed = time.monotonic() - self.monitoring_started_at
        self.elapsed_label.setText(f"监测时长  {self._format_elapsed(elapsed)}")
        if not self.detector.calibrated:
            progress = self.detector.calibration_progress
            self.auto_status.setText(f"正在自动校准 · {progress}%\n请保持自然呼吸")
            self.monitoring_result.setText("校准中：系统正在学习当前佩戴状态与呼吸幅度。")
        else:
            quality = self.detector.quality
            quality_text = "良好" if quality >= 65 else "一般" if quality >= 35 else "较弱"
            state = self.detector.state
            if state == "agreement":
                self.auto_status.setText(f"✓ 双算法结果一致\n信号质量：{quality_text}")
                self.monitoring_result.setText("监测正常：动态阈值与周期分析结果一致。")
            elif state == "verifier_warming":
                self.auto_status.setText("动态阈值已工作\n周期复核正在预热")
                self.monitoring_result.setText("监测中：约 24 秒后启用 ACF 与频谱复核。")
            elif state == "threshold_only":
                self.auto_status.setText(f"动态阈值暂时输出\n信号质量：{quality_text}")
                self.monitoring_result.setText("周期复核暂不可用，当前结果置信度较低。")
            elif state == "consensus_only":
                self.auto_status.setText("周期分析暂时输出\n逐次呼吸检测不稳定")
                self.monitoring_result.setText("请检查传感器贴合，当前结果仅由周期分析确认。")
            elif state == "disagreement":
                self.auto_status.setText("两种算法结果不一致\n已暂停显示频率")
                self.monitoring_result.setText("检测结果冲突，请保持静止并继续自然呼吸。")
            elif state == "no_recent_breath":
                self.auto_status.setText("未检测到近期呼吸\n已清空旧频率")
                self.monitoring_result.setText("超过 12 秒没有确认新呼吸，请检查佩戴或呼吸状态。")
            elif state == "low_quality" or quality < 35:
                self.auto_status.setText(f"信号质量：{quality_text}\n暂不输出频率")
                self.monitoring_result.setText("信号较弱或存在干扰，请确认传感器贴合。")
            else:
                self.auto_status.setText(f"正在识别呼吸周期\n信号质量：{quality_text}")
                self.monitoring_result.setText("监测中：正在等待足够的稳定呼吸周期。")

    def stop_monitoring(self, interrupted: bool = False):
        elapsed = max(0.0, time.monotonic() - self.monitoring_started_at)
        self.monitoring_active = False

        if interrupted:
            self.monitoring_result.setText(
                f"连接已断开，本次监测 {self._format_elapsed(elapsed)}。"
            )
        else:
            self.monitoring_result.setText(
                f"监测已停止，本次持续 {self._format_elapsed(elapsed)}。"
            )
        self.auto_status.setText("连接后自动学习呼吸幅度\n无需手动设置")
        self.finish_button.setText("保存本次数据")
        self.finish_button.setEnabled(bool(self.recorder.rows))
        self.mark_status.setText("测量已结束\n键盘标记已停用")

    def finish_or_save_measurement(self):
        if self.monitoring_active:
            self.stop_monitoring()
            self.save_measurement()
        elif self.recorder.rows and not self.measurement_saved:
            self.save_measurement()
        elif self.connected and self.measurement_saved:
            self.start_monitoring()

    def save_measurement(self) -> bool:
        if not self.recorder.rows or self.measurement_saved:
            return self.measurement_saved

        suggested_name = f"呼吸测量_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "保存测量数据",
            str(Path.home() / suggested_name),
            "CSV 文件 (*.csv)",
        )
        if not path:
            self.finish_button.setText("保存本次数据")
            self.finish_button.setEnabled(True)
            self.monitoring_result.setText(
                f"已取消保存，{len(self.recorder.rows)} 条数据仍保留在程序中。"
            )
            return False

        csv_path = Path(path)
        if csv_path.suffix.lower() != ".csv":
            csv_path = csv_path.with_suffix(".csv")
        try:
            self.recorder.save_csv(csv_path)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"无法保存 CSV：\n{exc}")
            self.finish_button.setText("保存本次数据")
            self.finish_button.setEnabled(True)
            return False

        self.measurement_saved = True
        self.monitoring_result.setText(
            f"已保存 {len(self.recorder.rows)} 条数据\n{csv_path}"
        )
        self.mark_status.setText("数据已保存\nmark 列仅包含 1–9 或 NaN")
        if self.connected:
            self.finish_button.setText("开始新测量")
            self.finish_button.setEnabled(True)
        else:
            self.finish_button.setText("数据已保存")
            self.finish_button.setEnabled(False)
        QMessageBox.information(
            self,
            "保存完成",
            f"已保存 {len(self.recorder.rows)} 条数据到：\n{csv_path}",
        )
        return True

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def closeEvent(self, event):
        if self.reader and self.reader.isRunning():
            self.reader.stop()
            self.reader.wait(1200)
        event.accept()


def run_self_test() -> int:
    """Packaged-build smoke test used by Windows CI without opening the UI."""
    detector = HybridBreathDetector()
    for index in range(20 * 50):
        timestamp = index / 20
        raw = 1450 + 220 * np.sin(timestamp / 4.0 * np.pi * 2)
        detector.process(int(round(raw)), timestamp)
    rate = detector.rate()
    if detector.state != "agreement" or rate is None or abs(rate - 15.0) > 0.8:
        return 2
    for index in range(20 * 16):
        detector.process(1450, 50 + index / 20)
    if detector.rate() is not None:
        return 3
    recorder = MeasurementRecorder()
    recorder.queue_mark("6")
    recorder.add(50_000, 1500)
    recorder.add(50_050, 1510)
    if recorder.rows != [(0, 1500, "6"), (50, 1510, "NaN")]:
        return 4
    return 0


def main():
    if "--self-test" in sys.argv:
        raise SystemExit(run_self_test())

    app = QApplication(sys.argv)
    app.setApplicationName("呼吸监测")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("BreathMonitor")
    app.setFont(QFont("Microsoft YaHei UI" if sys.platform == "win32" else "PingFang SC", 10))
    icon_path = resource_path("assets/app_icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = RespirationWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
