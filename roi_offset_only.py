from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import interp1d
from scipy.io import loadmat
from scipy.signal import butter, filtfilt


# ============================================================
# ==================== 配置部分 ==============================
# ============================================================
video_path = r"E:\pycharm\video\zzq.mp4"
wrist_file_path = r"E:\pycharm\video\202512021542L_crop_195200_201200.mat"

# 可选: "acc"、"gyr"、"acc_gyr"
IMU_MODE = "acc_gyr"

# 脑电给出的发作参考时间窗，按“视频起点后的相对秒数”填写。
# 支持三种写法:
#   1. 620.0
#   2. "10:20"
#   3. "00:10:20"
EEG_EVENT_START = "10:20"
EEG_EVENT_END = "11:40"

# 偏移定义: offset = 腕表时间 - 视频时间
# 正值: 腕表比视频晚
# 负值: 腕表比视频早
MAX_OFFSET_SECONDS = 15 * 60

# 视频处理提速参数
TARGET_VIDEO_FS = 5.0
ROI_MAX_WIDTH = 320

# 围绕脑电时间窗的额外扩展，帮助包含完整发作模块
EEG_PAD_BEFORE_SEC = 25.0
EEG_PAD_AFTER_SEC = 35.0
TEMPLATE_CONTEXT_BEFORE_SEC = 12.0
TEMPLATE_CONTEXT_AFTER_SEC = 18.0

# 特征构建参数
SHORT_SMOOTH_SEC = 1.0
LONG_SMOOTH_SEC = 5.0
BURST_SMOOTH_SEC = 1.5
MASK_SMOOTH_SEC = 2.0
MIN_EVENT_DURATION_SEC = 8.0
MAX_TEMPLATE_DURATION_SEC = 140.0

# 调试输出
SAVE_DEBUG_PLOT = True
DEBUG_PLOT_PATH = str(Path(video_path).with_name("roi_offset_debug.png"))
ROI_DEBUG_PATH = str(Path(video_path).with_name("roi_selected.png"))
LAST_ROI_PATH = str(Path(video_path).with_name("last_roi.txt"))

# 仅调试时可直接填 (x, y, w, h)，正常使用请保持 None，让程序启动后手动框选
DEBUG_ROI_OVERRIDE = None

fs_imu = 100


# ============================================================
# ==================== 基础工具函数 ==========================
# ============================================================
def parse_time_value(value):
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        raise ValueError("时间配置不能为空")

    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value}") from exc

    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60.0 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]
    raise ValueError(f"无法解析时间: {value}")


def format_seconds(seconds):
    seconds = float(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hh = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = seconds % 60
    return f"{sign}{hh:02d}:{mm:02d}:{ss:05.2f}"


def moving_average(sig, window_size):
    sig = np.asarray(sig, dtype=np.float64)
    window_size = max(1, int(window_size))
    kernel = np.ones(window_size, dtype=np.float64) / window_size
    return np.convolve(sig, kernel, mode="same")


def robust_scale(sig):
    sig = np.asarray(sig, dtype=np.float64)
    if len(sig) == 0:
        return sig
    med = np.median(sig)
    q75 = np.percentile(sig, 75)
    q25 = np.percentile(sig, 25)
    iqr = max(q75 - q25, 1e-9)
    return (sig - med) / iqr


def zscore_normalize(sig):
    sig = np.asarray(sig, dtype=np.float64)
    if len(sig) == 0:
        return sig
    mu = np.mean(sig)
    std = max(np.std(sig), 1e-9)
    return (sig - mu) / std


def resample_signal(sig, original_fs, target_fs):
    sig = np.asarray(sig, dtype=np.float64)
    if len(sig) == 0:
        return np.array([], dtype=np.float64)

    t_orig = np.arange(len(sig), dtype=np.float64) / float(original_fs)
    t_new = np.arange(0.0, len(sig) / float(original_fs), 1.0 / float(target_fs))
    interp = interp1d(
        t_orig,
        sig,
        kind="linear",
        fill_value="extrapolate",
        bounds_error=False,
    )
    return interp(t_new)


def resample_by_time(times, values, target_fs, duration_sec):
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    if len(times) == 0 or len(values) == 0:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)

    t_new = np.arange(0.0, duration_sec, 1.0 / float(target_fs))
    y_new = np.interp(t_new, times, values, left=values[0], right=values[-1])
    return t_new, y_new


def butter_highpass(cutoff, fs, order=6):
    nyq = fs / 2.0
    return butter(order, cutoff / nyq, btype="high")


def butter_lowpass(cutoff, fs, order=6):
    nyq = fs / 2.0
    return butter(order, cutoff / nyq, btype="low")


def filter_acc(signal_1d, fs=100):
    b_h, a_h = butter_highpass(0.5, fs, order=6)
    high_passed = filtfilt(b_h, a_h, signal_1d)
    b_l, a_l = butter_lowpass(15.0, fs, order=10)
    return filtfilt(b_l, a_l, high_passed)


def filter_gyr(signal_1d, fs=100):
    b_h, a_h = butter_highpass(0.5, fs, order=6)
    high_passed = filtfilt(b_h, a_h, signal_1d)
    b_l, a_l = butter_lowpass(15.0, fs, order=10)
    return filtfilt(b_l, a_l, high_passed)


def build_activity_feature(raw_signal, fs):
    raw_signal = np.asarray(raw_signal, dtype=np.float64)
    raw_signal = np.maximum(raw_signal, 0.0)
    raw_signal = np.log1p(raw_signal)

    short = moving_average(raw_signal, round(fs * SHORT_SMOOTH_SEC))
    long = moving_average(raw_signal, round(fs * LONG_SMOOTH_SEC))
    burst = np.maximum(short - long, 0.0)
    gradient = np.abs(np.diff(short, prepend=short[0]))

    feature = 0.65 * burst + 0.25 * np.maximum(short, 0.0) + 0.10 * gradient
    feature = moving_average(feature, round(fs * BURST_SMOOTH_SEC))
    return robust_scale(feature)


def build_soft_mask(signal, fs):
    signal = np.asarray(signal, dtype=np.float64)
    threshold = np.percentile(signal, 70)
    mask = (signal >= threshold).astype(np.float64)
    return moving_average(mask, round(fs * MASK_SMOOTH_SEC))


def build_peak_train(signal, fs):
    signal = np.asarray(signal, dtype=np.float64)
    smooth = moving_average(signal, round(fs * 1.2))
    baseline = np.percentile(smooth, 60)
    active = np.maximum(smooth - baseline, 0.0)
    if np.max(active) <= 1e-9:
        return np.zeros_like(signal, dtype=np.float64)

    peak_mask = np.zeros_like(active, dtype=np.float64)
    min_distance = max(1, int(round(fs * 1.5)))
    last_idx = -10**9
    for idx in range(1, len(active) - 1):
        if active[idx] >= active[idx - 1] and active[idx] > active[idx + 1] and active[idx] > 0:
            if idx - last_idx >= min_distance:
                peak_mask[idx] = active[idx]
                last_idx = idx
            elif active[idx] > peak_mask[last_idx]:
                peak_mask[last_idx] = 0.0
                peak_mask[idx] = active[idx]
                last_idx = idx
    return moving_average(peak_mask, round(fs * 1.6))


def parabolic_refine(curve, index):
    if index <= 0 or index >= len(curve) - 1:
        return 0.0
    y1, y2, y3 = curve[index - 1], curve[index], curve[index + 1]
    denom = y1 - 2 * y2 + y3
    if abs(denom) < 1e-12:
        return 0.0
    delta = 0.5 * (y1 - y3) / denom
    return float(np.clip(delta, -1.0, 1.0))


def save_roi_rect_image(frame_bgr, roi, output_path):
    vis = frame_bgr.copy()
    x, y, w, h = roi
    cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
    cv2.imwrite(output_path, vis)


def load_last_roi(path):
    roi_path = Path(path)
    if not roi_path.exists():
        return None
    text = roi_path.read_text(encoding="utf-8").strip()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        return None
    try:
        roi = tuple(int(float(p)) for p in parts)
    except ValueError:
        return None
    return roi


def save_last_roi(path, roi):
    Path(path).write_text(",".join(str(int(v)) for v in roi), encoding="utf-8")


# ============================================================
# ==================== ROI 与视频特征 ========================
# ============================================================
def select_manual_roi(video_file):
    cap = cv2.VideoCapture(video_file)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_file}")

    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("无法读取视频第一帧")

    if DEBUG_ROI_OVERRIDE is not None:
        roi = tuple(int(v) for v in DEBUG_ROI_OVERRIDE)
        print(f"  调试模式直接使用 ROI: {roi}")
        save_last_roi(LAST_ROI_PATH, roi)
        save_roi_rect_image(frame, roi, ROI_DEBUG_PATH)
        return frame, roi

    last_roi = load_last_roi(LAST_ROI_PATH)
    if last_roi is not None:
        print(f"  上次 ROI: {last_roi}")

    print("  请在弹出的窗口中框选病床/病人区域，按 Enter 确认，按 Esc 取消。")
    roi = cv2.selectROI("Select Patient ROI", frame, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()

    x, y, w, h = [int(v) for v in roi]
    if w <= 0 or h <= 0:
        raise RuntimeError("未选择有效 ROI")

    roi = (x, y, w, h)
    save_last_roi(LAST_ROI_PATH, roi)
    save_roi_rect_image(frame, roi, ROI_DEBUG_PATH)
    return frame, roi


def preprocess_roi_gray(frame, roi, max_width):
    x, y, w, h = roi
    crop = frame[y:y + h, x:x + w]
    if crop.size == 0:
        raise RuntimeError("ROI 超出图像范围，请重新选择")

    if crop.shape[1] > max_width:
        scale = max_width / float(crop.shape[1])
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    return gray


def detect_tracking_points(gray):
    return cv2.goodFeaturesToTrack(
        gray,
        maxCorners=150,
        qualityLevel=0.01,
        minDistance=6,
        blockSize=7,
    )


def compute_video_motion_series(video_file, roi, target_fs, roi_max_width):
    cap = cv2.VideoCapture(video_file)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_file}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0:
        cap.release()
        raise RuntimeError("无法读取视频 FPS")

    duration_sec = frame_count / fps
    step = max(1, int(round(fps / target_fs)))

    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("无法读取视频第一帧")

    prev_gray = preprocess_roi_gray(frame, roi, roi_max_width)
    prev_pts = detect_tracking_points(prev_gray)

    sampled_times = []
    flow_values = []
    diff_values = []
    point_counts = []

    next_progress = 10
    frame_idx = 0

    print(f"  视频 FPS: {fps:.2f}, 总帧数: {frame_count}, 总时长: {duration_sec:.2f}s")
    print(f"  采样后目标频率: {target_fs:.2f}Hz, 约每 {step} 帧取一次")
    print(f"  ROI: x={roi[0]}, y={roi[1]}, w={roi[2]}, h={roi[3]}")

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        frame_idx += 1
        if frame_idx % step != 0:
            continue

        gray = preprocess_roi_gray(frame, roi, roi_max_width)
        diff = cv2.absdiff(gray, prev_gray).astype(np.float32)
        diff_score = float(np.mean(diff) / 255.0)

        flow_score = 0.0
        valid_count = 0

        if prev_pts is None or len(prev_pts) < 8:
            prev_pts = detect_tracking_points(prev_gray)

        if prev_pts is not None and len(prev_pts) >= 6:
            next_pts, status, _ = cv2.calcOpticalFlowPyrLK(
                prev_gray,
                gray,
                prev_pts,
                None,
                winSize=(21, 21),
                maxLevel=2,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
            )
            if next_pts is not None and status is not None:
                keep = status.reshape(-1) == 1
                old_pts = prev_pts[keep].reshape(-1, 2)
                new_pts = next_pts[keep].reshape(-1, 2)
                valid_count = len(old_pts)
                if valid_count >= 4:
                    disp = np.linalg.norm(new_pts - old_pts, axis=1)
                    flow_score = float(np.median(disp) + 0.35 * np.percentile(disp, 80))
                    prev_pts = new_pts.reshape(-1, 1, 2)
                else:
                    prev_pts = detect_tracking_points(gray)
            else:
                prev_pts = detect_tracking_points(gray)
        else:
            prev_pts = detect_tracking_points(gray)

        sampled_times.append(frame_idx / fps)
        flow_values.append(flow_score)
        diff_values.append(diff_score)
        point_counts.append(valid_count)

        prev_gray = gray

        progress = int((frame_idx / max(frame_count - 1, 1)) * 100)
        if progress >= next_progress:
            print(f"    视频处理进度: {progress}%")
            next_progress += 10

    cap.release()

    if len(sampled_times) == 0:
        raise RuntimeError("视频运动序列为空")

    sampled_times = np.asarray(sampled_times, dtype=np.float64)
    flow_values = np.asarray(flow_values, dtype=np.float64)
    diff_values = np.asarray(diff_values, dtype=np.float64)
    point_counts = np.asarray(point_counts, dtype=np.float64)

    t_uniform, flow_uniform = resample_by_time(sampled_times, flow_values, target_fs, duration_sec)
    _, diff_uniform = resample_by_time(sampled_times, diff_values, target_fs, duration_sec)
    _, points_uniform = resample_by_time(sampled_times, point_counts, target_fs, duration_sec)

    actual_fs = 1.0 / np.median(np.diff(sampled_times)) if len(sampled_times) > 1 else target_fs
    print(f"  实际抽样频率约: {actual_fs:.2f}Hz")
    print(f"  重采样后视频序列长度: {len(t_uniform)}")

    return {
        "time": t_uniform,
        "flow": flow_uniform,
        "diff": diff_uniform,
        "points": points_uniform,
        "fps": fps,
        "duration_sec": duration_sec,
    }


def build_video_feature(video_motion, fs):
    flow_part = robust_scale(np.log1p(np.maximum(video_motion["flow"], 0.0)))
    diff_part = robust_scale(np.log1p(np.maximum(video_motion["diff"], 0.0)))
    raw_mix = 0.72 * flow_part + 0.28 * diff_part
    feature = build_activity_feature(np.maximum(raw_mix - np.percentile(raw_mix, 15), 0.0), fs)
    return raw_mix, feature


# ============================================================
# ==================== 腕表特征 ==============================
# ============================================================
def build_imu_feature(mat_path, mode, target_fs):
    print("  正在读取 .mat 并构建腕表特征...")
    mat = loadmat(mat_path)
    data = mat["data"]

    acc_raw = np.asarray(data[1, 0], dtype=np.float64)
    gyr_raw = np.asarray(data[2, 0], dtype=np.float64)

    print(f"  ACC 形状: {acc_raw.shape}")
    print(f"  GYR 形状: {gyr_raw.shape}")

    acc_filtered = np.empty_like(acc_raw, dtype=np.float64)
    gyr_filtered = np.empty_like(gyr_raw, dtype=np.float64)

    for axis in range(3):
        print(f"    腕表滤波轴进度: axis={axis + 1}/3")
        acc_filtered[:, axis] = filter_acc(acc_raw[:, axis], fs=fs_imu)
        gyr_filtered[:, axis] = filter_gyr(gyr_raw[:, axis], fs=fs_imu)

    acc_mag = np.sqrt(np.sum(acc_filtered ** 2, axis=1))
    gyr_mag = np.sqrt(np.sum(gyr_filtered ** 2, axis=1))

    acc_rs = resample_signal(acc_mag, fs_imu, target_fs)
    gyr_rs = resample_signal(gyr_mag, fs_imu, target_fs)

    if mode == "acc":
        raw = acc_rs
    elif mode == "gyr":
        raw = gyr_rs
    elif mode == "acc_gyr":
        n = min(len(acc_rs), len(gyr_rs))
        raw = 0.55 * acc_rs[:n] + 0.45 * gyr_rs[:n]
    else:
        raise ValueError("IMU_MODE 只能是 'acc'、'gyr' 或 'acc_gyr'")

    feature = build_activity_feature(np.maximum(raw, 0.0), target_fs)
    print(f"  腕表重采样后长度: {len(feature)}")

    return {
        "time": np.arange(len(feature), dtype=np.float64) / target_fs,
        "raw": raw[: len(feature)],
        "feature": feature,
    }


# ============================================================
# ==================== 主异常块与对齐 ========================
# ============================================================
def merge_small_gaps(mask, max_gap_samples):
    mask = np.asarray(mask, dtype=bool).copy()
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        j = i
        while j < n and not mask[j]:
            j += 1
        if i > 0 and j < n and (j - i) <= max_gap_samples:
            mask[i:j] = True
        i = j
    return mask


def find_segments(mask):
    segments = []
    start = None
    for idx, flag in enumerate(mask):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            segments.append((start, idx))
            start = None
    if start is not None:
        segments.append((start, len(mask)))
    return segments


def detect_main_event_block(feature, fs, eeg_start_sec, eeg_end_sec):
    eeg_lo = max(0, int(round((eeg_start_sec - EEG_PAD_BEFORE_SEC) * fs)))
    eeg_hi = min(len(feature), int(round((eeg_end_sec + EEG_PAD_AFTER_SEC) * fs)))
    if eeg_hi - eeg_lo < int(10 * fs):
        raise RuntimeError("脑电时间窗太短，无法稳定定位主异常块")

    segment = feature[eeg_lo:eeg_hi]
    smooth_core = moving_average(segment, round(fs * 2.0))
    smooth_main = moving_average(segment, round(fs * 4.0))
    peak_rel = int(np.argmax(smooth_core))
    peak_value = float(smooth_core[peak_rel])
    baseline = float(np.percentile(smooth_main, 35))
    high_threshold = baseline + 0.32 * max(peak_value - baseline, 1e-9)
    low_threshold = baseline + 0.12 * max(peak_value - baseline, 1e-9)

    core_active = merge_small_gaps(smooth_core >= high_threshold, max_gap_samples=int(round(fs * 2.0)))
    main_active = merge_small_gaps(smooth_main >= low_threshold, max_gap_samples=int(round(fs * 4.0)))

    core_segments = find_segments(core_active)
    main_segments = find_segments(main_active)

    min_len = int(round(MIN_EVENT_DURATION_SEC * fs))
    core_best = None
    core_best_score = -1e18
    for start, end in core_segments:
        if end - start < min_len:
            continue
        excess = float(np.sum(smooth_core[start:end] - high_threshold))
        contains_peak = start <= peak_rel < end
        score = excess + (10.0 if contains_peak else 0.0)
        if score > core_best_score:
            core_best_score = score
            core_best = (start, end)

    if core_best is None:
        core_best = (max(0, peak_rel - int(round(10 * fs))), min(len(segment), peak_rel + int(round(10 * fs))))

    main_best = None
    for start, end in main_segments:
        if start <= peak_rel < end:
            main_best = (start, end)
            break

    if main_best is None:
        main_best = core_best

    max_template_len = int(round(MAX_TEMPLATE_DURATION_SEC * fs))
    core_start_abs = eeg_lo + core_best[0]
    core_end_abs = eeg_lo + core_best[1]
    main_start_abs = eeg_lo + main_best[0]
    main_end_abs = eeg_lo + main_best[1]

    tmpl_start = max(0, main_start_abs - int(round(TEMPLATE_CONTEXT_BEFORE_SEC * fs)))
    tmpl_end = min(len(feature), main_end_abs + int(round(TEMPLATE_CONTEXT_AFTER_SEC * fs)))

    if tmpl_end - tmpl_start > max_template_len:
        peak_abs = eeg_lo + peak_rel
        half = max_template_len // 2
        tmpl_start = max(0, peak_abs - half)
        tmpl_end = min(len(feature), tmpl_start + max_template_len)
        tmpl_start = max(0, tmpl_end - max_template_len)

    best = {
        "core_start": core_start_abs,
        "core_end": core_end_abs,
        "main_start": main_start_abs,
        "main_end": main_end_abs,
        "threshold": low_threshold,
        "peak_time_sec": (eeg_lo + peak_rel) / fs,
        "template_start": tmpl_start,
        "template_end": tmpl_end,
    }
    return best


def normalized_corr_valid(target, template):
    target_z = zscore_normalize(target)
    template_z = zscore_normalize(template)
    return np.correlate(target_z, template_z, mode="valid") / max(len(template_z), 1)


def bounded_offset_search(video_feature, wrist_feature, fs, block_info, max_offset_sec):
    tmpl_start = int(block_info["template_start"])
    tmpl_end = int(block_info["template_end"])
    template = video_feature[tmpl_start:tmpl_end]
    if len(template) < int(round(12 * fs)):
        raise RuntimeError("主异常模板太短，无法稳定搜索偏移")

    video_grad = np.abs(np.diff(video_feature, prepend=video_feature[0]))
    wrist_grad = np.abs(np.diff(wrist_feature, prepend=wrist_feature[0]))
    video_mask = build_soft_mask(video_feature, fs)
    wrist_mask = build_soft_mask(wrist_feature, fs)
    video_peak = build_peak_train(video_feature, fs)
    wrist_peak = build_peak_train(wrist_feature, fs)

    raw_corr = normalized_corr_valid(wrist_feature, template)
    grad_corr = normalized_corr_valid(wrist_grad, video_grad[tmpl_start:tmpl_end])
    mask_corr = normalized_corr_valid(wrist_mask, video_mask[tmpl_start:tmpl_end])
    peak_corr = normalized_corr_valid(wrist_peak, video_peak[tmpl_start:tmpl_end])

    positions = np.arange(len(raw_corr), dtype=np.float64)
    offsets = (positions - tmpl_start) / fs

    valid = np.abs(offsets) <= max_offset_sec
    if not np.any(valid):
        raise RuntimeError("限定偏移范围后没有可搜索区间，请检查 MAX_OFFSET_SECONDS")

    valid_offsets = offsets[valid]
    raw_valid = raw_corr[valid]
    grad_valid = grad_corr[valid]
    mask_valid = mask_corr[valid]
    peak_valid = peak_corr[valid]

    combined = (
        0.55 * zscore_normalize(raw_valid) +
        0.25 * zscore_normalize(peak_valid) +
        0.10 * zscore_normalize(grad_valid) +
        0.10 * zscore_normalize(mask_valid)
    )

    best_idx = int(np.argmax(combined))
    delta = parabolic_refine(combined, best_idx)
    offset_step = 1.0 / fs
    best_offset = float(valid_offsets[best_idx] + delta * offset_step)

    exclusion = max(1, int(round(3.0 * fs)))
    left = max(0, best_idx - exclusion)
    right = min(len(combined), best_idx + exclusion + 1)
    mask = np.ones(len(combined), dtype=bool)
    mask[left:right] = False
    second_best = float(np.max(combined[mask])) if np.any(mask) else float(combined[best_idx])
    margin = float(combined[best_idx] - second_best)
    level = float((combined[best_idx] - np.mean(combined)) / max(np.std(combined), 1e-9))

    raw_peak_offset = float(valid_offsets[int(np.argmax(raw_valid))])
    peak_peak_offset = float(valid_offsets[int(np.argmax(peak_valid))])
    grad_peak_offset = float(valid_offsets[int(np.argmax(grad_valid))])
    mask_peak_offset = float(valid_offsets[int(np.argmax(mask_valid))])
    agreement = np.mean([
        np.exp(-abs(best_offset - raw_peak_offset) / 4.0),
        np.exp(-abs(best_offset - peak_peak_offset) / 4.0),
        np.exp(-abs(best_offset - grad_peak_offset) / 4.0),
        np.exp(-abs(best_offset - mask_peak_offset) / 4.0),
    ])

    confidence = (
        0.45 * np.clip((level - 0.8) / 2.5, 0.0, 1.0) +
        0.35 * np.clip(margin / 1.8, 0.0, 1.0) +
        0.20 * np.clip(agreement, 0.0, 1.0)
    )

    best_wrist_start = int(round(tmpl_start + best_offset * fs))
    best_wrist_start = max(0, min(best_wrist_start, len(wrist_feature) - len(template)))
    best_wrist_end = best_wrist_start + len(template)

    return {
        "offset_seconds": best_offset,
        "confidence": float(confidence),
        "candidate_offsets": valid_offsets,
        "combined_scores": combined,
        "raw_scores": raw_valid,
        "peak_scores": peak_valid,
        "grad_scores": grad_valid,
        "mask_scores": mask_valid,
        "template_start": tmpl_start,
        "template_end": tmpl_end,
        "best_wrist_start": best_wrist_start,
        "best_wrist_end": best_wrist_end,
        "raw_peak_offset": raw_peak_offset,
        "peak_peak_offset": peak_peak_offset,
        "grad_peak_offset": grad_peak_offset,
        "mask_peak_offset": mask_peak_offset,
    }


# ============================================================
# ==================== 调试图 ================================
# ============================================================
def save_debug_plot(
    first_frame,
    roi,
    video_feature,
    wrist_feature,
    fs,
    eeg_start_sec,
    eeg_end_sec,
    block_info,
    search_result,
    output_path,
):
    plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    frame_rgb = cv2.cvtColor(first_frame, cv2.COLOR_BGR2RGB)
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))

    ax = axes[0, 0]
    ax.imshow(frame_rgb)
    x, y, w, h = roi
    rect = plt.Rectangle((x, y), w, h, fill=False, edgecolor="red", linewidth=2)
    ax.add_patch(rect)
    ax.set_title("手动选择的 ROI")
    ax.axis("off")

    ax = axes[0, 1]
    t_video = np.arange(len(video_feature)) / fs
    ax.plot(t_video, zscore_normalize(video_feature), color="crimson", lw=1.0)
    ax.axvspan(eeg_start_sec, eeg_end_sec, color="gold", alpha=0.25, label="脑电参考窗")
    ax.axvspan(
        block_info["template_start"] / fs,
        block_info["template_end"] / fs,
        color="royalblue",
        alpha=0.18,
        label="视频主异常模板",
    )
    ax.axvline(block_info["peak_time_sec"], color="black", linestyle="--", lw=1.0, label="模板主峰")
    ax.set_title("视频特征与主异常块")
    ax.set_xlabel("视频时间 (秒)")
    ax.legend(loc="upper right")

    ax = axes[1, 0]
    ax.plot(search_result["candidate_offsets"], search_result["combined_scores"], color="darkorange", lw=1.1, label="综合得分")
    ax.plot(search_result["candidate_offsets"], zscore_normalize(search_result["raw_scores"]), color="steelblue", lw=0.9, alpha=0.8, label="原始相关")
    ax.axvline(search_result["offset_seconds"], color="red", linestyle="--", lw=1.2, label="最佳偏移")
    ax.set_title("限定偏移范围内的搜索结果")
    ax.set_xlabel("offset = 腕表 - 视频 (秒)")
    ax.legend(loc="upper right")

    ax = axes[1, 1]
    v0 = search_result["template_start"]
    v1 = search_result["template_end"]
    w0 = search_result["best_wrist_start"]
    w1 = search_result["best_wrist_end"]
    t_overlay = np.arange(v1 - v0) / fs
    ax.plot(t_overlay, zscore_normalize(video_feature[v0:v1]), color="crimson", lw=1.0, label="视频模板")
    ax.plot(t_overlay, zscore_normalize(wrist_feature[w0:w1]), color="steelblue", lw=1.0, label="腕表对应片段")
    ax.set_title(
        f"对齐后重叠  offset={search_result['offset_seconds']:.2f}s  "
        f"conf={search_result['confidence']:.3f}"
    )
    ax.set_xlabel("模板内部相对时间 (秒)")
    ax.legend(loc="upper right")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ============================================================
# ==================== 主流程 ================================
# ============================================================
def main():
    video_file = Path(video_path)
    wrist_file = Path(wrist_file_path)
    if not video_file.exists():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")
    if not wrist_file.exists():
        raise FileNotFoundError(f"找不到腕表文件: {wrist_file_path}")

    eeg_start_sec = parse_time_value(EEG_EVENT_START)
    eeg_end_sec = parse_time_value(EEG_EVENT_END)
    if eeg_end_sec <= eeg_start_sec:
        raise ValueError("EEG_EVENT_END 必须大于 EEG_EVENT_START")

    print("=" * 60)
    print("手动 ROI + 脑电时间窗约束的视频/腕表偏移计算")
    print("偏移定义: offset = 腕表时间 - 视频时间")
    print("=" * 60)

    print("\n[1/6] 读取腕表数据并构建特征...")
    imu_info = build_imu_feature(str(wrist_file), IMU_MODE, TARGET_VIDEO_FS)

    print("\n[2/6] 手动选择 ROI...")
    first_frame, roi = select_manual_roi(str(video_file))
    print(f"  已选择 ROI: {roi}")
    print(f"  ROI 预览图已保存: {ROI_DEBUG_PATH}")

    print("\n[3/6] 提取视频 ROI 运动序列...")
    video_motion = compute_video_motion_series(
        str(video_file),
        roi=roi,
        target_fs=TARGET_VIDEO_FS,
        roi_max_width=ROI_MAX_WIDTH,
    )

    print("\n[4/6] 构建视频特征，并在脑电时间窗内定位主异常块...")
    _, video_feature = build_video_feature(video_motion, TARGET_VIDEO_FS)
    block_info = detect_main_event_block(video_feature, TARGET_VIDEO_FS, eeg_start_sec, eeg_end_sec)
    print(f"  脑电参考窗: {format_seconds(eeg_start_sec)} ~ {format_seconds(eeg_end_sec)}")
    print(
        "  视频主异常模板: "
        f"{format_seconds(block_info['template_start'] / TARGET_VIDEO_FS)} ~ "
        f"{format_seconds(block_info['template_end'] / TARGET_VIDEO_FS)}"
    )
    print(
        "  核心异常块: "
        f"{format_seconds(block_info['core_start'] / TARGET_VIDEO_FS)} ~ "
        f"{format_seconds(block_info['core_end'] / TARGET_VIDEO_FS)}"
    )

    print("\n[5/6] 在限定偏移范围内搜索最佳对齐...")
    print(f"  搜索范围: ±{MAX_OFFSET_SECONDS:.0f} 秒")
    search_result = bounded_offset_search(
        video_feature=video_feature,
        wrist_feature=imu_info["feature"],
        fs=TARGET_VIDEO_FS,
        block_info=block_info,
        max_offset_sec=MAX_OFFSET_SECONDS,
    )
    print(
        "  候选峰值偏移: "
        f"raw={search_result['raw_peak_offset']:.2f}s, "
        f"grad={search_result['grad_peak_offset']:.2f}s, "
        f"mask={search_result['mask_peak_offset']:.2f}s"
    )

    if SAVE_DEBUG_PLOT:
        print("\n[6/6] 保存调试图...")
        save_debug_plot(
            first_frame=first_frame,
            roi=roi,
            video_feature=video_feature,
            wrist_feature=imu_info["feature"],
            fs=TARGET_VIDEO_FS,
            eeg_start_sec=eeg_start_sec,
            eeg_end_sec=eeg_end_sec,
            block_info=block_info,
            search_result=search_result,
            output_path=DEBUG_PLOT_PATH,
        )
        print(f"  调试图已保存: {DEBUG_PLOT_PATH}")

    print("\n" + "=" * 60)
    print("结果")
    print("=" * 60)
    print(f"offset_seconds={search_result['offset_seconds']:.2f}")
    print(f"confidence={search_result['confidence']:.3f}")


if __name__ == "__main__":
    main()
