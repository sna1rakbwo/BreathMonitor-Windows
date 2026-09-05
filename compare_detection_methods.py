"""动态阈值法与 ACF + 频谱共识法的可复现配对模拟。"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from scipy.signal import butter, find_peaks, sosfiltfilt, welch

from main import BreathDetector, HybridBreathDetector


SAMPLE_RATE = 20
DURATION_SECONDS = 180
SEEDS = 30
OUTPUT_DIR = Path(__file__).with_name("simulation_results")
METHODS = ("threshold", "consensus", "hybrid")
FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
if FONT_PATH.exists():
    font_manager.fontManager.addfont(str(FONT_PATH))
    plt.rcParams["font.family"] = "Arial Unicode MS"
plt.rcParams["axes.unicode_minus"] = False


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    amplitude: float = 220.0
    noise: float = 10.0
    drift: float = 0.0
    amplitude_modulation: float = 0.0
    motion_bursts: int = 0
    rate_mode: str = "constant"
    apnea: tuple[float, float] | None = None


SCENARIOS = (
    Scenario("clean", "稳定呼吸", noise=8.0),
    Scenario("low_amplitude", "低幅呼吸", amplitude=42.0, noise=13.0),
    Scenario("baseline_drift", "基线漂移", drift=520.0, noise=12.0),
    Scenario(
        "amplitude_change", "幅度变化", amplitude=150.0, noise=14.0,
        amplitude_modulation=0.75,
    ),
    Scenario(
        "motion", "体动干扰", amplitude=190.0, noise=18.0,
        drift=240.0, motion_bursts=8,
    ),
    Scenario(
        "rate_step", "频率突变", amplitude=190.0, noise=12.0,
        rate_mode="step",
    ),
    Scenario(
        "apnea", "屏息片段", amplitude=200.0, noise=10.0,
        apnea=(75.0, 115.0),
    ),
)


def generate_signal(scenario: Scenario, seed: int):
    rng = np.random.default_rng(seed)
    count = DURATION_SECONDS * SAMPLE_RATE
    times = np.arange(count) / SAMPLE_RATE

    if scenario.rate_mode == "step":
        truth_rate = np.where(times < 90.0, 10.0, 24.0)
    else:
        truth_rate = np.full(count, 15.0)

    amplitude = np.full(count, scenario.amplitude)
    if scenario.amplitude_modulation:
        modulation = 1.0 + scenario.amplitude_modulation * np.sin(
            2 * np.pi * times / 48.0
        )
        amplitude *= np.maximum(0.16, modulation)

    breathing_enabled = np.ones(count, dtype=bool)
    if scenario.apnea:
        start, stop = scenario.apnea
        breathing_enabled = ~((times >= start) & (times < stop))
        truth_rate = truth_rate.astype(float)
        truth_rate[~breathing_enabled] = np.nan

    phase = np.cumsum(2 * np.pi * truth_rate.clip(min=0, max=60) / 60 / SAMPLE_RATE)
    phase = np.nan_to_num(phase, nan=phase[np.flatnonzero(np.isfinite(phase))[0]])
    # 略带非对称和二次谐波，比纯正弦更接近压力带形态。
    respiratory = amplitude * (
        np.sin(phase) + 0.18 * np.sin(2 * phase + 0.35)
    ) * breathing_enabled

    baseline = 1450.0 + scenario.drift * times / DURATION_SECONDS
    baseline += 35.0 * np.sin(2 * np.pi * times / 70.0)

    white = rng.normal(0, scenario.noise, count)
    colored = np.zeros(count)
    for index in range(1, count):
        colored[index] = 0.88 * colored[index - 1] + white[index]
    signal = baseline + respiratory + 0.45 * colored

    if scenario.motion_bursts:
        centers = rng.choice(
            np.arange(20 * SAMPLE_RATE, (DURATION_SECONDS - 15) * SAMPLE_RATE),
            size=scenario.motion_bursts,
            replace=False,
        )
        for center in centers:
            width = int(rng.uniform(0.5, 2.2) * SAMPLE_RATE)
            end = min(count, center + width)
            pulse = rng.choice((-1, 1)) * rng.uniform(350, 850)
            signal[center:end] += pulse * np.hanning(max(2, 2 * (end - center)))[: end - center]
            signal[center:end] += rng.normal(0, 90, end - center)

    return times, np.clip(signal, 0, 4095), truth_rate


class ConsensusDetector:
    """滚动窗口 ACF 周期与 Welch 主频一致时才输出。"""

    def __init__(self, sample_rate: int = SAMPLE_RATE, window_seconds: float = 24.0):
        self.fs = sample_rate
        self.window_samples = int(window_seconds * sample_rate)
        self.values: list[float] = []
        self.sos = butter(3, (0.05, 0.8), btype="bandpass", fs=sample_rate, output="sos")

    def add(self, value: float, evaluate: bool = False) -> tuple[float | None, float]:
        self.values.append(float(value))
        if len(self.values) > self.window_samples:
            self.values.pop(0)
        if not evaluate or len(self.values) < self.window_samples:
            return None, 0.0

        raw = np.asarray(self.values)
        try:
            filtered = sosfiltfilt(self.sos, raw)
        except ValueError:
            return None, 0.0
        filtered -= np.mean(filtered)
        energy = float(np.dot(filtered, filtered))
        if energy < 1e-6:
            return None, 0.0

        autocorrelation = np.correlate(filtered, filtered, mode="full")[len(filtered) - 1 :]
        autocorrelation /= max(autocorrelation[0], 1e-9)
        min_lag = int(self.fs * 1.25)   # 48 次/分
        max_lag = int(self.fs * 10.0)   # 6 次/分
        segment = autocorrelation[min_lag : max_lag + 1]
        peak_indices, _ = find_peaks(segment, distance=int(self.fs * 0.8))
        if not len(peak_indices):
            return None, 0.0
        best_index = peak_indices[np.argmax(segment[peak_indices])]
        lag = min_lag + int(best_index)
        acf_confidence = float(segment[best_index])
        rr_acf = 60.0 * self.fs / lag

        frequencies, power = welch(
            filtered, fs=self.fs, window="hann", nperseg=len(filtered),
            noverlap=0, detrend="linear",
        )
        band = (frequencies >= 0.08) & (frequencies <= 0.8)
        band_frequencies = frequencies[band]
        band_power = power[band]
        if not len(band_power) or float(np.sum(band_power)) <= 0:
            return None, 0.0
        peak = int(np.argmax(band_power))
        peak_frequency = float(band_frequencies[peak])
        if 0 < peak < len(band_power) - 1:
            left, center, right = np.log(band_power[peak - 1 : peak + 2] + 1e-12)
            denominator = left - 2 * center + right
            if abs(denominator) > 1e-12:
                peak_frequency += 0.5 * (left - right) / denominator * (
                    band_frequencies[1] - band_frequencies[0]
                )
        rr_spectrum = 60.0 * peak_frequency
        lo = max(0, peak - 1)
        hi = min(len(band_power), peak + 2)
        spectral_concentration = float(np.sum(band_power[lo:hi]) / np.sum(band_power))

        agreement = abs(rr_acf - rr_spectrum)
        confidence = min(
            1.0,
            max(0.0, acf_confidence) * 0.65 + spectral_concentration * 0.35,
        )
        if acf_confidence < 0.30 or spectral_concentration < 0.42 or agreement > 2.5:
            return None, confidence
        return (rr_acf + rr_spectrum) / 2.0, confidence


def run_detectors(signal: np.ndarray):
    threshold = BreathDetector()
    consensus = ConsensusDetector()
    hybrid = HybridBreathDetector()
    threshold_outputs: dict[int, float | None] = {}
    consensus_outputs: dict[int, float | None] = {}
    hybrid_outputs: dict[int, float | None] = {}
    confidences: dict[int, float] = {}

    for index, value in enumerate(signal):
        timestamp = index / SAMPLE_RATE
        threshold.process(int(round(value)), timestamp)
        hybrid.process(int(round(value)), timestamp)
        evaluate_now = index % SAMPLE_RATE == SAMPLE_RATE - 1
        consensus_value, confidence = consensus.add(float(value), evaluate=evaluate_now)
        if evaluate_now:
            second = int(timestamp)
            threshold_outputs[second] = threshold.rate(since=max(0.0, timestamp - 60.0))
            consensus_outputs[second] = consensus_value
            hybrid_outputs[second] = hybrid.rate()
            confidences[second] = confidence
    return threshold_outputs, consensus_outputs, hybrid_outputs, confidences


def valid_evaluation_second(scenario: Scenario, second: int) -> bool:
    if second < 30:
        return False
    if scenario.rate_mode == "step" and 90 <= second < 115:
        return False
    if scenario.apnea:
        start, stop = scenario.apnea
        if start <= second < stop:
            return False
        if stop <= second < stop + 20:
            return False
    return True


def evaluate_output(scenario: Scenario, truth: np.ndarray, outputs: dict[int, float | None]):
    errors = []
    eligible = 0
    output_count = 0
    gross_errors = 0
    for second, estimate in outputs.items():
        if not valid_evaluation_second(scenario, second):
            continue
        target = float(truth[min(len(truth) - 1, second * SAMPLE_RATE)])
        if not math.isfinite(target):
            continue
        eligible += 1
        if estimate is None:
            continue
        output_count += 1
        error = abs(estimate - target)
        errors.append(error)
        gross_errors += error > 5.0
    return {
        "mae": float(np.mean(errors)) if errors else math.nan,
        "coverage": output_count / eligible if eligible else 0.0,
        "gross_error_rate": gross_errors / output_count if output_count else math.nan,
    }


def apnea_false_output(scenario: Scenario, outputs: dict[int, float | None]):
    if not scenario.apnea:
        return math.nan
    start, stop = scenario.apnea
    # 给算法 15 秒确认呼吸停止；统计此后仍错误报告 RR 的比例。
    seconds = range(math.ceil(start + 15), math.floor(stop))
    values = [outputs.get(second) for second in seconds]
    return sum(value is not None for value in values) / len(values)


def step_response_latency(scenario: Scenario, outputs: dict[int, float | None]):
    if scenario.rate_mode != "step":
        return math.nan
    # 频率从 10 跳到 24 次/分后，连续 3 秒进入 ±2 次/分视为完成跟踪。
    for second in range(90, DURATION_SECONDS - 2):
        estimates = [outputs.get(second + offset) for offset in range(3)]
        if all(value is not None and abs(value - 24.0) <= 2.0 for value in estimates):
            return float(second - 90)
    return float(DURATION_SECONDS - 90)


def paired_bootstrap_difference(
    rows, scenario_key: str, metric: str, method_a: str, method_b: str
):
    selected = [row for row in rows if row["scenario"] == scenario_key]
    by_seed: dict[int, dict[str, float]] = {}
    for row in selected:
        by_seed.setdefault(row["seed"], {})[row["method"]] = row[metric]
    differences = np.array([
        methods[method_a] - methods[method_b]
        for methods in by_seed.values()
        if math.isfinite(methods[method_a]) and math.isfinite(methods[method_b])
    ])
    if not len(differences):
        return math.nan, math.nan, math.nan
    rng = np.random.default_rng(20260901)
    samples = rng.choice(differences, size=(5000, len(differences)), replace=True).mean(axis=1)
    return float(np.mean(differences)), float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))


def summarize(rows):
    summary = []
    for scenario in SCENARIOS:
        for method in METHODS:
            selected = [
                row for row in rows
                if row["scenario"] == scenario.key and row["method"] == method
            ]
            summary.append({
                "scenario": scenario.key,
                "scenario_label": scenario.label,
                "method": method,
                "mae_mean": float(np.nanmean([row["mae"] for row in selected])),
                "coverage_mean": float(np.mean([row["coverage"] for row in selected])),
                "gross_error_rate_mean": float(np.nanmean([row["gross_error_rate"] for row in selected])),
                "apnea_false_output_mean": float(np.nanmean([row["apnea_false_output"] for row in selected]))
                if scenario.apnea else math.nan,
                "step_latency_mean": float(np.nanmean([row["step_latency"] for row in selected]))
                if scenario.rate_mode == "step" else math.nan,
            })
    return summary


def write_outputs(rows, summary):
    OUTPUT_DIR.mkdir(exist_ok=True)
    with (OUTPUT_DIR / "paired_trials.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "configuration": {
            "sample_rate_hz": SAMPLE_RATE,
            "duration_seconds": DURATION_SECONDS,
            "paired_seeds": SEEDS,
            "scenarios": [asdict(scenario) for scenario in SCENARIOS],
            "evaluation": "30 s warm-up; rate-step transition excludes 25 s; apnea recovery excludes 20 s",
        },
        "summary": summary,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=True), encoding="utf-8"
    )

    labels = [scenario.label for scenario in SCENARIOS]
    x = np.arange(len(labels))
    width = 0.25
    fig, axes = plt.subplots(2, 1, figsize=(11, 8), constrained_layout=True)
    for offset, method, name, color in (
        (-width, "threshold", "动态阈值", "#94A3B8"),
        (0, "consensus", "ACF+频谱共识", "#5B8E87"),
        (width, "hybrid", "主程序混合算法", "#0F5F56"),
    ):
        method_rows = [item for item in summary if item["method"] == method]
        axes[0].bar(x + offset, [item["mae_mean"] for item in method_rows], width, label=name, color=color)
        axes[1].bar(x + offset, [item["coverage_mean"] * 100 for item in method_rows], width, label=name, color=color)
    axes[0].set_ylabel("MAE（次/分，越低越好）")
    axes[1].set_ylabel("有效输出率（%，越高越好）")
    axes[1].set_ylim(0, 105)
    for axis in axes:
        axis.set_xticks(x, labels)
        axis.grid(axis="y", alpha=0.22)
        axis.spines[["top", "right"]].set_visible(False)
        axis.legend(frameon=False)
    fig.suptitle("呼吸频率检测配对模拟（30 seeds × 180 s）", fontsize=15)
    fig.savefig(OUTPUT_DIR / "method_comparison.png", dpi=180)
    plt.close(fig)

    lines = [
        "# 动态阈值、ACF+频谱和主程序混合算法配对模拟", "",
        f"- 配对设计：每种场景 {SEEDS} 个相同随机种子，每条 {DURATION_SECONDS} 秒，{SAMPLE_RATE} Hz。",
        "- MAE 只在算法实际输出且存在呼吸真值时计算；覆盖率单独报告。",
        "- 这是合成信号工程筛选，不代表真实人体准确率。", "",
        "| 场景 | 方法 | MAE 次/分 | 覆盖率 | >5 次/分错误率 |", "|---|---|---:|---:|---:|",
    ]
    for scenario in SCENARIOS:
        for method in METHODS:
            item = next(row for row in summary if row["scenario"] == scenario.key and row["method"] == method)
            method_name = {
                "threshold": "动态阈值",
                "consensus": "ACF+频谱共识",
                "hybrid": "主程序混合算法",
            }[method]
            lines.append(
                f"| {scenario.label} | {method_name} | {item['mae_mean']:.2f} | "
                f"{item['coverage_mean']:.1%} | {item['gross_error_rate_mean']:.1%} |"
            )
    lines.extend(["", "## 混合算法配对 MAE 差值", "", "差值为 `混合算法 - 动态阈值`，负数表示混合算法误差更低。", ""])
    for scenario in SCENARIOS:
        difference, low, high = paired_bootstrap_difference(
            rows, scenario.key, "mae", "hybrid", "threshold"
        )
        lines.append(f"- {scenario.label}：{difference:+.2f} 次/分，95% bootstrap CI [{low:+.2f}, {high:+.2f}]")
    apnea = [item for item in summary if item["scenario"] == "apnea"]
    lines.extend(["", "## 屏息后错误持续报数", ""])
    for item in apnea:
        method_name = {
            "threshold": "动态阈值",
            "consensus": "ACF+频谱共识",
            "hybrid": "主程序混合算法",
        }[item["method"]]
        lines.append(f"- {method_name}：{item['apnea_false_output_mean']:.1%}")
    rate_step = [item for item in summary if item["scenario"] == "rate_step"]
    lines.extend(["", "## 频率突变响应延迟", ""])
    for item in rate_step:
        method_name = {
            "threshold": "动态阈值",
            "consensus": "ACF+频谱共识",
            "hybrid": "主程序混合算法",
        }[item["method"]]
        lines.append(f"- {method_name}：{item['step_latency_mean']:.1f} 秒")
    (OUTPUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    rows = []
    for scenario in SCENARIOS:
        for seed in range(SEEDS):
            _times, signal, truth = generate_signal(scenario, seed)
            threshold, consensus, hybrid, _confidence = run_detectors(signal)
            for method, outputs in (
                ("threshold", threshold),
                ("consensus", consensus),
                ("hybrid", hybrid),
            ):
                metrics = evaluate_output(scenario, truth, outputs)
                rows.append({
                    "scenario": scenario.key,
                    "seed": seed,
                    "method": method,
                    **metrics,
                    "apnea_false_output": apnea_false_output(scenario, outputs),
                    "step_latency": step_response_latency(scenario, outputs),
                })
    summary = summarize(rows)
    write_outputs(rows, summary)
    print((OUTPUT_DIR / "REPORT.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
