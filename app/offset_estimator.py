# -*- coding: utf-8 -*-
"""视频和腕表信号自动对齐估计。"""

from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
    from scipy.interpolate import interp1d
    from scipy.io import loadmat
    from scipy.signal import butter, filtfilt
except ImportError:
    np = None
    interp1d = None
    loadmat = None
    butter = None
    filtfilt = None


EEG_PAD_BEFORE_SEC = 25.0
EEG_PAD_AFTER_SEC = 35.0
TEMPLATE_CONTEXT_BEFORE_SEC = 12.0
TEMPLATE_CONTEXT_AFTER_SEC = 18.0
SHORT_SMOOTH_SEC = 1.0
LONG_SMOOTH_SEC = 5.0
BURST_SMOOTH_SEC = 1.5
MASK_SMOOTH_SEC = 2.0
MIN_EVENT_DURATION_SEC = 8.0
MAX_TEMPLATE_DURATION_SEC = 140.0
DEFAULT_MAX_OFFSET_SECONDS = 15 * 60
DEFAULT_TARGET_FS = 5.0
DEFAULT_ROI_MAX_WIDTH = 240


def _report_progress(progress_callback, percent, message):
    """向外报告当前计算进度。"""
    if progress_callback is not None:
        progress_callback(int(max(0, min(100, percent))), str(message or ""))


def read_video_first_frame(video_path):
    """读取视频第一帧，供 ROI 选择界面预览。"""
    video_path = str(video_path or "")
    if not Path(video_path).exists():
        raise FileNotFoundError(f"找不到视频文件：{video_path}")
    if cv2 is None:
        raise RuntimeError("未安装 opencv-python，无法读取视频图像")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"无法打开视频：{video_path}")
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError("无法读取视频第一帧")
    return frame


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


def compute_video_motion_series(video_file, roi, target_fs, roi_max_width, progress_callback=None):
    cap = cv2.VideoCapture(video_file)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_file}")

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
    frame_idx = 0
    last_progress = -1

    _report_progress(progress_callback, 25, "正在分析视频 ROI 运动特征...")

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

        current_progress = 25 + int(45 * frame_idx / max(frame_count - 1, 1))
        if current_progress != last_progress:
            last_progress = current_progress
            _report_progress(progress_callback, current_progress, f"正在分析视频 ROI... {current_progress}%")

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


def build_imu_feature(mat_path, mode, target_fs, acc_fs=100.0, gyro_fs=100.0, progress_callback=None):
    _report_progress(progress_callback, 10, "正在读取腕表数据...")
    mat = loadmat(mat_path)
    if "data" not in mat:
        raise RuntimeError("腕表 MAT 文件中未找到 data 变量")
    data = mat["data"]
    if data.shape[0] <= 2:
        raise RuntimeError("腕表 MAT 数据缺少 ACC 或 GYRO 通道")

    acc_raw = np.asarray(data[1, 0], dtype=np.float64)
    gyr_raw = np.asarray(data[2, 0], dtype=np.float64)
    if acc_raw.ndim != 2 or acc_raw.shape[1] < 3:
        raise RuntimeError("ACC 数据格式不正确，至少需要 3 轴")
    if gyr_raw.ndim != 2 or gyr_raw.shape[1] < 3:
        raise RuntimeError("GYRO 数据格式不正确，至少需要 3 轴")

    acc_filtered = np.empty_like(acc_raw, dtype=np.float64)
    gyr_filtered = np.empty_like(gyr_raw, dtype=np.float64)

    for axis in range(3):
        _report_progress(progress_callback, 12 + axis * 3, f"正在滤波腕表加速度/角速度... {axis + 1}/3")
        acc_filtered[:, axis] = filter_acc(acc_raw[:, axis], fs=acc_fs)
        gyr_filtered[:, axis] = filter_gyr(gyr_raw[:, axis], fs=gyro_fs)

    acc_mag = np.sqrt(np.sum(acc_filtered ** 2, axis=1))
    gyr_mag = np.sqrt(np.sum(gyr_filtered ** 2, axis=1))
    acc_rs = resample_signal(acc_mag, acc_fs, target_fs)
    gyr_rs = resample_signal(gyr_mag, gyro_fs, target_fs)

    if mode == "acc":
        raw = acc_rs
    elif mode == "gyr":
        raw = gyr_rs
    elif mode == "acc_gyr":
        n = min(len(acc_rs), len(gyr_rs))
        raw = 0.55 * acc_rs[:n] + 0.45 * gyr_rs[:n]
    else:
        raise ValueError("imu_mode 只能是 'acc'、'gyr' 或 'acc_gyr'")

    feature = build_activity_feature(np.maximum(raw, 0.0), target_fs)
    _report_progress(progress_callback, 20, "腕表特征构建完成")
    return {
        "time": np.arange(len(feature), dtype=np.float64) / target_fs,
        "raw": raw[: len(feature)],
        "feature": feature,
    }


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
        raise RuntimeError("脑电参考时间窗太短，无法稳定定位主异常块")

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
        core_best = (
            max(0, peak_rel - int(round(10 * fs))),
            min(len(segment), peak_rel + int(round(10 * fs))),
        )

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

    return {
        "core_start": core_start_abs,
        "core_end": core_end_abs,
        "main_start": main_start_abs,
        "main_end": main_end_abs,
        "peak_time_sec": (eeg_lo + peak_rel) / fs,
        "template_start": tmpl_start,
        "template_end": tmpl_end,
    }


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
        raise RuntimeError("限定偏移范围后没有可搜索区间，请检查最大偏移范围")

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
    best_offset = float(valid_offsets[best_idx] + delta * (1.0 / fs))

    exclusion = max(1, int(round(3.0 * fs)))
    left = max(0, best_idx - exclusion)
    right = min(len(combined), best_idx + exclusion + 1)
    keep_mask = np.ones(len(combined), dtype=bool)
    keep_mask[left:right] = False
    second_best = float(np.max(combined[keep_mask])) if np.any(keep_mask) else float(combined[best_idx])
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
    return {
        "offset_seconds": best_offset,
        "confidence": float(confidence),
        "template_start": tmpl_start,
        "template_end": tmpl_end,
        "raw_peak_offset": raw_peak_offset,
        "peak_peak_offset": peak_peak_offset,
        "grad_peak_offset": grad_peak_offset,
        "mask_peak_offset": mask_peak_offset,
    }


def estimate_video_watch_offset(
    video_path,
    wrist_file_path,
    roi,
    eeg_start_sec,
    eeg_end_sec,
    imu_mode="acc_gyr",
    target_fs=DEFAULT_TARGET_FS,
    frame_scale=0.25,
    imu_fs=100.0,
    acc_fs=None,
    gyro_fs=None,
    max_offset_seconds=DEFAULT_MAX_OFFSET_SECONDS,
    roi_max_width=DEFAULT_ROI_MAX_WIDTH,
    progress_callback=None,
):
    """基于手动 ROI 和脑电参考时间窗估计视频到腕表数据的偏移量。"""
    del frame_scale
    video_path = str(video_path or "")
    wrist_file_path = str(wrist_file_path or "")
    roi = tuple(int(v) for v in (roi or ()))
    if len(roi) != 4:
        raise RuntimeError("缺少有效 ROI，请先手动框选病人区域")
    if eeg_end_sec <= eeg_start_sec:
        raise RuntimeError("脑电参考结束时间必须晚于开始时间")
    if not Path(video_path).exists():
        raise FileNotFoundError(f"找不到视频文件：{video_path}")
    if not Path(wrist_file_path).exists():
        raise FileNotFoundError(f"找不到腕表数据文件：{wrist_file_path}")
    if cv2 is None:
        raise RuntimeError("未安装 opencv-python，无法自动估计偏移量")
    if np is None or interp1d is None or loadmat is None or butter is None or filtfilt is None:
        raise RuntimeError("缺少 numpy/scipy 依赖，无法自动估计偏移量")

    acc_fs = float(acc_fs or imu_fs or 100.0)
    gyro_fs = float(gyro_fs or imu_fs or 100.0)
    target_fs = float(target_fs or DEFAULT_TARGET_FS)

    _report_progress(progress_callback, 0, "准备开始自动估计偏移量...")
    imu_info = build_imu_feature(
        wrist_file_path,
        imu_mode,
        target_fs,
        acc_fs=acc_fs,
        gyro_fs=gyro_fs,
        progress_callback=progress_callback,
    )

    _report_progress(progress_callback, 22, "正在提取视频 ROI 序列...")
    video_motion = compute_video_motion_series(
        video_path,
        roi=roi,
        target_fs=target_fs,
        roi_max_width=int(roi_max_width),
        progress_callback=progress_callback,
    )

    _report_progress(progress_callback, 72, "正在构建视频特征...")
    _, video_feature = build_video_feature(video_motion, target_fs)

    _report_progress(progress_callback, 82, "正在根据脑电参考时间定位视频异常块...")
    block_info = detect_main_event_block(video_feature, target_fs, float(eeg_start_sec), float(eeg_end_sec))

    _report_progress(progress_callback, 92, "正在搜索最佳偏移量...")
    search_result = bounded_offset_search(
        video_feature=video_feature,
        wrist_feature=imu_info["feature"],
        fs=target_fs,
        block_info=block_info,
        max_offset_sec=float(max_offset_seconds),
    )

    _report_progress(progress_callback, 100, "偏移量计算完成")
    return {
        "offset_seconds": float(search_result["offset_seconds"]),
        "offset_ms": int(round(float(search_result["offset_seconds"]) * 1000)),
        "confidence": float(search_result["confidence"]),
        "roi": roi,
        "reference_start_seconds": float(eeg_start_sec),
        "reference_end_seconds": float(eeg_end_sec),
        "template_start_seconds": float(block_info["template_start"] / target_fs),
        "template_end_seconds": float(block_info["template_end"] / target_fs),
    }
