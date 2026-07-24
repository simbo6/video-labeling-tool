import re
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import interp1d
from scipy.io import loadmat
from scipy.signal import butter, filtfilt, find_peaks


# ============================================================
# ==================== 配置部分 ==============================
# ============================================================
video_path = r'E:\pycharm\video\zzq.mp4'
wrist_file_path = r'E:\pycharm\video\202512021542L_crop_195200_201200.mat'

# 可选: "acc"、"gyr"、"acc_gyr"
IMU_MODE = "acc_gyr"

TARGET_FS = 10.0
FRAME_SCALE = 0.25

# 手动 ROI:
# 传原始视频分辨率下的 (x, y, w, h)
# 为 None 时才会走自动 ROI 搜索
MANUAL_ROI = None

# 自动 ROI 搜索范围与候选窗口
BED_BOX = (0.22, 0.02, 0.56, 0.96)  # 相对缩放后画面的比例框
ROI_SHAPES = (
    (0.34, 0.42),
    (0.28, 0.30),
    (0.22, 0.24),
)
ROI_GRID_SIZE = 4
HAND_ROI_SHAPES = (
    (0.14, 0.16),
    (0.18, 0.20),
    (0.22, 0.24),
    (0.16, 0.30),
)

# 对齐参数
BASELINE_SECONDS = 120
MAX_SHIFT_SECONDS = 30
PEAK_DISTANCE_SEC = 1.5
FEATURE_PEAK_TOP_N = 20
LOCAL_CLUSTER_SEC = 0.8
BLOCK_K_VALUES = (1.6, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0)

# 特征参数
SHORT_SMOOTH_SEC = 0.8
LONG_SMOOTH_SEC = 3.0
ENERGY_WINDOW_SEC = 1.5
ACTIVE_MASK_PERCENTILE = 78
PEAK_TRAIN_SMOOTH_SEC = 1.2

# 调试输出
SAVE_DEBUG_PLOT = True
DEBUG_PLOT_PATH = r'E:\pycharm\video\roi_offset_debug.png'
FIRST_FRAME_DEBUG_PATH = r'E:\pycharm\video\roi_alignment_first_frame.png'

VERBOSE = False
fs_imu = 100


# ============================================================
# ==================== 基础函数 ==============================
# ============================================================
def butter_highpass(cutoff, fs, order=6):
    nyq = fs / 2.0
    return butter(order, cutoff / nyq, btype='high')


def butter_lowpass(cutoff, fs, order=6):
    nyq = fs / 2.0
    return butter(order, cutoff / nyq, btype='low')


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


def moving_average(sig, window_size):
    sig = np.asarray(sig, dtype=np.float64)
    window_size = max(1, int(window_size))
    kernel = np.ones(window_size, dtype=np.float64) / window_size
    return np.convolve(sig, kernel, mode='same')


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
    std = np.std(sig)
    return (sig - np.mean(sig)) / max(std, 1e-9)


def resample_signal(sig, original_fs, target_fs):
    sig = np.asarray(sig, dtype=np.float64)
    if len(sig) == 0:
        return np.array([], dtype=np.float64)
    t_orig = np.arange(len(sig)) / original_fs
    t_new = np.arange(0, len(sig) / original_fs, 1.0 / target_fs)
    interp = interp1d(
        t_orig,
        sig,
        kind='linear',
        fill_value='extrapolate',
        bounds_error=False,
    )
    return interp(t_new)


def extract_watch_side(file_path):
    name = Path(file_path).stem.upper()
    match = re.search(r'\d{12}([LR])', name)
    return match.group(1) if match else None


# ============================================================
# ==================== 自动 ROI ==============================
# ============================================================
def generate_candidate_rois(frame_shape, watch_side=None):
    h, w = frame_shape[:2]
    bx = int(round(w * BED_BOX[0]))
    by = int(round(h * BED_BOX[1]))
    bw = int(round(w * BED_BOX[2]))
    bh = int(round(h * BED_BOX[3]))

    candidates = []
    seen = set()

    for rel_w, rel_h in ROI_SHAPES:
        rw = int(round(bw * rel_w))
        rh = int(round(bh * rel_h))
        xs = np.linspace(bx, bx + bw - rw, ROI_GRID_SIZE)
        ys = np.linspace(by, by + bh - rh, ROI_GRID_SIZE)
        for x in xs:
            for y in ys:
                roi = (int(round(x)), int(round(y)), rw, rh)
                if roi not in seen:
                    seen.add(roi)
                    candidates.append(roi)

    # 额外加一些更贴近上半身的候选框
    extra = [
        (int(w * 0.28), int(h * 0.02), int(w * 0.34), int(h * 0.42)),
        (int(w * 0.33), int(h * 0.18), int(w * 0.24), int(h * 0.28)),
        (int(w * 0.22), int(h * 0.02), int(w * 0.56), int(h * 0.96)),
    ]
    for roi in extra:
        if roi not in seen:
            seen.add(roi)
            candidates.append(roi)

    # 针对“腕表戴在手上”的任务，补充左右上肢/前臂/手腕附近的小框。
    # 注意这里只是软候选，不强行限制某一侧，避免不同相机视角把左右映射反掉。
    side_anchor_map = {
        "L": [
            (0.60, 0.24, 1.00),  # 常见情况下，左手在画面右侧偏上
            (0.64, 0.38, 0.92),
            (0.40, 0.24, 0.72),  # 镜像兜底
            (0.36, 0.38, 0.64),
            (0.50, 0.30, 0.58),  # 躯干/肩带兜底
        ],
        "R": [
            (0.40, 0.24, 1.00),  # 常见情况下，右手在画面左侧偏上
            (0.36, 0.38, 0.92),
            (0.60, 0.24, 0.72),  # 镜像兜底
            (0.64, 0.38, 0.64),
            (0.50, 0.30, 0.58),
        ],
        None: [
            (0.40, 0.24, 0.88),
            (0.60, 0.24, 0.88),
            (0.36, 0.38, 0.78),
            (0.64, 0.38, 0.78),
            (0.50, 0.30, 0.58),
        ],
    }
    anchors = side_anchor_map.get(watch_side, side_anchor_map[None])

    for cx_rel, cy_rel, _ in anchors:
        cx = int(round(w * cx_rel))
        cy = int(round(h * cy_rel))
        for rel_w, rel_h in HAND_ROI_SHAPES:
            rw = int(round(bw * rel_w))
            rh = int(round(bh * rel_h))
            for dx_rel, dy_rel in ((0.0, 0.0), (-0.03, 0.0), (0.03, 0.0), (0.0, -0.03), (0.0, 0.03)):
                x = int(round(cx + w * dx_rel - rw / 2))
                y = int(round(cy + h * dy_rel - rh / 2))
                x = max(bx, min(x, bx + bw - rw))
                y = max(by, min(y, by + bh - rh))
                roi = (x, y, rw, rh)
                if roi not in seen:
                    seen.add(roi)
                    candidates.append(roi)

    priors = []
    for x, y, rw, rh in candidates:
        cx = (x + rw / 2) / w
        cy = (y + rh / 2) / h
        roi_area = (rw * rh) / max(w * h, 1)

        anchor_scores = []
        for ax, ay, weight in anchors:
            px = np.exp(-((cx - ax) ** 2) / (2 * 0.10 ** 2))
            py = np.exp(-((cy - ay) ** 2) / (2 * 0.12 ** 2))
            anchor_scores.append(weight * px * py)

        side_prior = max(anchor_scores) if anchor_scores else 0.0

        # 更偏爱“手/前臂”大小的框，弱化太大的全床框和太小的噪声框。
        area_pref = np.exp(-((roi_area - 0.035) ** 2) / (2 * 0.020 ** 2))
        aspect = rw / max(rh, 1)
        aspect_pref = np.exp(-((aspect - 0.85) ** 2) / (2 * 0.65 ** 2))

        prior = float(0.70 * side_prior + 0.20 * area_pref + 0.10 * aspect_pref)
        priors.append(prior)

    return candidates, np.asarray(priors, dtype=np.float64)


def scale_roi_to_small(roi, scale, frame_shape):
    x, y, w, h = roi
    fh, fw = frame_shape[:2]
    sx = int(round(x * scale))
    sy = int(round(y * scale))
    sw = int(round(w * scale))
    sh = int(round(h * scale))
    sx = max(0, min(sx, fw - 1))
    sy = max(0, min(sy, fh - 1))
    sw = max(1, min(sw, fw - sx))
    sh = max(1, min(sh, fh - sy))
    return sx, sy, sw, sh


def compute_video_roi_series(path, target_fs, scale, rois):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0:
        cap.release()
        raise RuntimeError("无法读取视频 FPS")

    step = max(1, int(round(fps / target_fs)))
    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("无法读取视频第一帧")

    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    first_frame = small.copy()
    prev_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    series = [[] for _ in rois]
    frame_idx = 1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step != 0:
            frame_idx += 1
            continue

        small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray, prev_gray).astype(np.float32)

        for idx, (x, y, rw, rh) in enumerate(rois):
            roi_diff = diff[y:y + rh, x:x + rw]
            series[idx].append(float(np.mean(roi_diff)))

        prev_gray = gray
        frame_idx += 1

    cap.release()

    if frame_count <= 0 or not series or len(series[0]) == 0:
        raise RuntimeError("视频运动序列为空")

    return first_frame, [np.asarray(sig, dtype=np.float64) for sig in series]


# ============================================================
# ==================== 特征与对齐 ============================
# ============================================================
def build_activity_feature(raw_signal, fs):
    raw_signal = np.asarray(raw_signal, dtype=np.float64)
    raw_signal = np.maximum(raw_signal - np.median(raw_signal), 0.0)
    raw_signal = np.log1p(raw_signal)

    short = moving_average(raw_signal, round(fs * SHORT_SMOOTH_SEC))
    long = moving_average(raw_signal, round(fs * LONG_SMOOTH_SEC))
    dynamic = np.abs(short - long)
    gradient = np.abs(np.diff(short, prepend=short[0]))
    feature = 0.7 * dynamic + 0.3 * gradient
    feature = moving_average(feature, round(fs * ENERGY_WINDOW_SEC))
    return robust_scale(feature)


def build_imu_feature(mat_path, mode, target_fs):
    mat = loadmat(mat_path)
    data = mat['data']
    acc_raw = np.asarray(data[1, 0], dtype=np.float64)
    gyr_raw = np.asarray(data[2, 0], dtype=np.float64)

    acc_filtered = np.empty_like(acc_raw, dtype=np.float64)
    gyr_filtered = np.empty_like(gyr_raw, dtype=np.float64)

    for axis in range(3):
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
        raw = 0.5 * acc_rs[:n] + 0.5 * gyr_rs[:n]
    else:
        raise ValueError("IMU_MODE 只能是 'acc'、'gyr' 或 'acc_gyr'")

    return build_activity_feature(raw, target_fs)


def build_peak_train(signal, fs):
    signal = np.asarray(signal, dtype=np.float64)
    base_n = min(len(signal), int(round(BASELINE_SECONDS * fs)))
    baseline = signal[:base_n] if base_n > 0 else signal
    med = np.median(baseline)
    mad = max(np.median(np.abs(baseline - med)), 1e-3)

    peaks = np.array([], dtype=int)
    for height_mul, prom_mul in ((3.0, 1.5), (2.3, 1.0), (1.8, 0.8)):
        peaks, _ = find_peaks(
            signal,
            height=med + height_mul * mad,
            prominence=max(0.08, prom_mul * mad),
            distance=max(1, int(round(PEAK_DISTANCE_SEC * fs))),
        )
        if len(peaks) >= 3:
            break

    if len(peaks) == 0:
        peaks, _ = find_peaks(signal, distance=max(1, int(round(PEAK_DISTANCE_SEC * fs))))

    weights = np.zeros(len(signal), dtype=np.float64)
    if len(peaks) > 0:
        peak_vals = signal[peaks]
        norm_vals = peak_vals - np.median(signal)
        norm_vals = norm_vals / max(np.max(np.abs(norm_vals)), 1e-9)
        weights[peaks] = np.maximum(norm_vals, 0.2)

    peak_train = moving_average(weights, round(fs * PEAK_TRAIN_SMOOTH_SEC))
    return peak_train, peaks


def build_active_mask(signal):
    threshold = np.percentile(signal, ACTIVE_MASK_PERCENTILE)
    mask = (signal >= threshold).astype(np.float64)
    return moving_average(mask, round(TARGET_FS * 2.0))


def limited_xcorr(sig_a, sig_b, max_shift_samples):
    sig_a = zscore_normalize(sig_a)
    sig_b = zscore_normalize(sig_b)
    corr_full = np.correlate(sig_b, sig_a, mode='full')
    lag_full = np.arange(-(len(sig_a) - 1), len(sig_b))
    valid = np.abs(lag_full) <= max_shift_samples
    return lag_full[valid], corr_full[valid]


def parabolic_refine(curve, index):
    if index <= 0 or index >= len(curve) - 1:
        return 0.0
    y1, y2, y3 = curve[index - 1], curve[index], curve[index + 1]
    denom = y1 - 2 * y2 + y3
    if abs(denom) < 1e-12:
        return 0.0
    delta = 0.5 * (y1 - y3) / denom
    return float(np.clip(delta, -1.0, 1.0))


def analyze_xcorr(signal_a, signal_b, max_shift_samples):
    lags, corr = limited_xcorr(signal_a, signal_b, max_shift_samples)
    best_idx = int(np.argmax(corr))
    delta = parabolic_refine(corr, best_idx)
    refined_lag = float(lags[best_idx] + delta)

    corr_z = (corr - np.mean(corr)) / max(np.std(corr), 1e-9)
    peak_z = float(corr_z[best_idx])

    exclusion = int(round(TARGET_FS * 1.0))
    mask = np.ones(len(corr), dtype=bool)
    left = max(0, best_idx - exclusion)
    right = min(len(corr), best_idx + exclusion + 1)
    mask[left:right] = False
    second_z = float(np.max(corr_z[mask])) if np.any(mask) else peak_z
    margin_z = peak_z - second_z

    return {
        "lags": lags,
        "corr": corr,
        "best_idx": best_idx,
        "refined_lag": refined_lag,
        "peak_z": peak_z,
        "margin_z": margin_z,
    }


def overlap_segments(video_signal, imu_signal, lag_samples):
    lag_int = int(np.floor(lag_samples))
    if lag_int >= 0:
        overlap = min(len(video_signal), len(imu_signal) - lag_int)
        if overlap <= 0:
            return np.array([]), np.array([])
        return video_signal[:overlap], imu_signal[lag_int:lag_int + overlap]

    start_v = -lag_int
    overlap = min(len(video_signal) - start_v, len(imu_signal))
    if overlap <= 0:
        return np.array([]), np.array([])
    return video_signal[start_v:start_v + overlap], imu_signal[:overlap]


def active_overlap_score(video_feature, imu_feature, lag_samples):
    v, i = overlap_segments(video_feature, imu_feature, lag_samples)
    if len(v) < int(4 * TARGET_FS):
        return 0.0
    v_mask = v >= np.percentile(v, ACTIVE_MASK_PERCENTILE)
    i_mask = i >= np.percentile(i, ACTIVE_MASK_PERCENTILE)
    inter = np.sum(v_mask & i_mask)
    union = np.sum(v_mask | i_mask)
    if union == 0:
        return 0.0
    return float(inter / union)


def build_roi_result(video_feature, imu_feature, roi, prior):
    max_shift_samples = int(round(MAX_SHIFT_SECONDS * TARGET_FS))

    feature_xcorr = analyze_xcorr(video_feature, imu_feature, max_shift_samples)
    peak_train_video, peaks = build_peak_train(video_feature, TARGET_FS)
    peak_train_imu, _ = build_peak_train(imu_feature, TARGET_FS)
    peak_xcorr = analyze_xcorr(peak_train_video, peak_train_imu, max_shift_samples)

    mask_video = build_active_mask(video_feature)
    mask_imu = build_active_mask(imu_feature)
    mask_xcorr = analyze_xcorr(mask_video, mask_imu, max_shift_samples)

    feat_lag = feature_xcorr["refined_lag"] / TARGET_FS
    peak_lag = peak_xcorr["refined_lag"] / TARGET_FS
    mask_lag = mask_xcorr["refined_lag"] / TARGET_FS

    agree_peak = np.exp(-abs(feat_lag - peak_lag) / 1.0)
    agree_mask = np.exp(-abs(feat_lag - mask_lag) / 1.0)
    active_score = active_overlap_score(video_feature, imu_feature, feature_xcorr["refined_lag"])

    peak_score = np.clip((feature_xcorr["peak_z"] - 1.5) / 2.0, 0.0, 1.0)
    margin_score = np.clip(feature_xcorr["margin_z"] / 0.35, 0.0, 1.0)
    prior_score = float(np.clip(prior, 0.0, 1.0))

    final_score = (
        0.40 * peak_score +
        0.25 * agree_peak +
        0.15 * agree_mask +
        0.10 * active_score +
        0.10 * prior_score
    )

    return {
        "roi": roi,
        "prior": prior_score,
        "feature_lag_sec": feat_lag,
        "peak_lag_sec": peak_lag,
        "mask_lag_sec": mask_lag,
        "peak_z": feature_xcorr["peak_z"],
        "margin_z": feature_xcorr["margin_z"],
        "active_score": active_score,
        "agree_peak": float(agree_peak),
        "agree_mask": float(agree_mask),
        "score": float(final_score),
        "video_feature": video_feature,
        "feature_xcorr": feature_xcorr,
        "peaks": peaks,
    }


def strongest_block(signal, fs, k):
    signal = np.asarray(signal, dtype=np.float64)
    base_n = min(len(signal), int(round(BASELINE_SECONDS * fs)))
    baseline = signal[:base_n] if base_n > 0 else signal
    med = np.median(baseline)
    mad = max(np.median(np.abs(baseline - med)), 1e-3)
    threshold = med + k * mad
    mask = signal > threshold

    blocks = []
    start = None
    for idx, flag in enumerate(mask):
        if flag and start is None:
            start = idx
        if not flag and start is not None:
            blocks.append((start, idx))
            start = None
    if start is not None:
        blocks.append((start, len(signal)))

    best = None
    best_score = -1.0
    for start, end in blocks:
        duration_sec = (end - start) / fs
        if duration_sec < 3.0:
            continue
        score = float(np.sum(signal[start:end] - threshold))
        if score > best_score:
            best_score = score
            best = {
                "start": start,
                "end": end,
                "score": score,
                "threshold": threshold,
            }
    return best


def estimate_block_offset(video_feature, imu_feature, fs):
    offsets = []
    for k in BLOCK_K_VALUES:
        v_block = strongest_block(video_feature, fs, k)
        i_block = strongest_block(imu_feature, fs, k)
        if v_block is None or i_block is None:
            continue
        offsets.append((i_block["start"] - v_block["start"]) / fs)

    if len(offsets) < 3:
        return None

    offsets = np.asarray(offsets, dtype=np.float64)
    median_offset = float(np.median(offsets))
    dispersion = float(np.std(offsets))
    return {
        "offset": median_offset,
        "dispersion": dispersion,
        "count": len(offsets),
        "offsets": offsets,
    }


def estimate_primary_event_range(signal, fs):
    candidates = []
    for k in BLOCK_K_VALUES:
        block = strongest_block(signal, fs, k)
        if block is None:
            continue
        candidates.append({
            "start": int(block["start"]),
            "end": int(block["end"]),
            "duration_sec": (block["end"] - block["start"]) / fs,
            "score": float(block["score"]),
            "threshold": float(block["threshold"]),
            "k": float(k),
        })

    if not candidates:
        return None

    clusters = []
    for cand in candidates:
        center = 0.5 * (cand["start"] + cand["end"])
        matched = None
        for cluster in clusters:
            if abs(center - cluster["center"]) <= int(round(12.0 * fs)):
                matched = cluster
                break
        if matched is None:
            clusters.append({
                "items": [cand],
                "center": center,
            })
        else:
            matched["items"].append(cand)
            centers = [0.5 * (item["start"] + item["end"]) for item in matched["items"]]
            matched["center"] = float(np.mean(centers))

    best_cluster = None
    best_value = -1e18
    for cluster in clusters:
        items = cluster["items"]
        durations = np.asarray([item["duration_sec"] for item in items], dtype=np.float64)
        scores = np.asarray([item["score"] for item in items], dtype=np.float64)
        support = len(items)
        median_duration = float(np.median(durations))
        mean_score = float(np.mean(scores))
        cluster_value = 1.2 * support + 0.35 * median_duration + 0.02 * mean_score
        if cluster_value > best_value:
            best_value = cluster_value
            best_cluster = cluster

    items = best_cluster["items"]
    starts = np.asarray([item["start"] for item in items], dtype=np.float64)
    ends = np.asarray([item["end"] for item in items], dtype=np.float64)
    durations = np.asarray([item["duration_sec"] for item in items], dtype=np.float64)
    scores = np.asarray([item["score"] for item in items], dtype=np.float64)

    start_idx = int(round(np.median(starts)))
    end_idx = int(round(np.median(ends)))
    if end_idx <= start_idx:
        end_idx = start_idx + max(1, int(round(np.median(durations) * fs)))

    return {
        "start_idx": start_idx,
        "end_idx": end_idx,
        "start_sec": start_idx / fs,
        "end_sec": end_idx / fs,
        "duration_sec": (end_idx - start_idx) / fs,
        "support_count": len(items),
        "median_score": float(np.median(scores)),
        "used_k_values": [item["k"] for item in items],
    }


def is_valid_block_info(block_info):
    if block_info is None:
        return False
    return (
        abs(float(block_info["offset"])) <= MAX_SHIFT_SECONDS and
        float(block_info["dispersion"]) <= 0.40 and
        int(block_info["count"]) >= 5
    )


def gather_offset_hypotheses(item):
    hypotheses = [("feature", float(item["feature_lag_sec"]), float(item["score"]))]

    if abs(item["mask_lag_sec"] - item["feature_lag_sec"]) <= 1.5:
        hypotheses.append(("mask", float(item["mask_lag_sec"]), 0.45 * float(item["score"])))

    if abs(item["peak_lag_sec"] - item["feature_lag_sec"]) <= 1.0:
        hypotheses.append(("peak", float(item["peak_lag_sec"]), 0.15 * float(item["score"])))

    block_info = item.get("block_info")
    if is_valid_block_info(block_info):
        hypotheses.append(("block", float(block_info["offset"]), 0.80 * float(item["score"])))

    return hypotheses


def build_offset_vote_table(results):
    bin_scores = {}
    bin_feature_scores = {}
    bin_block_scores = {}
    vote_rows = []

    for item in results:
        for source, offset_sec, weight in gather_offset_hypotheses(item):
            bin_key = round(offset_sec * 2.0) / 2.0
            bin_scores[bin_key] = bin_scores.get(bin_key, 0.0) + float(weight)
            if source == "feature":
                bin_feature_scores.setdefault(bin_key, []).append(float(item["score"]))
            if source == "block":
                bin_block_scores[bin_key] = bin_block_scores.get(bin_key, 0.0) + float(weight)
            vote_rows.append({
                "source": source,
                "offset_sec": float(offset_sec),
                "weight": float(weight),
                "bin_key": float(bin_key),
                "roi": item["roi"],
                "item": item,
            })

    if not vote_rows:
        raise RuntimeError("没有可用的偏移候选")

    cluster_rows = []
    for bin_key, total_score in bin_scores.items():
        feature_scores = sorted(bin_feature_scores.get(bin_key, []), reverse=True)
        feature_top3_sum = float(sum(feature_scores[:3]))
        block_sum = float(bin_block_scores.get(bin_key, 0.0))
        cluster_score = feature_top3_sum + 0.20 * float(total_score) + 0.20 * block_sum
        cluster_rows.append({
            "center": float(bin_key),
            "cluster_score": float(cluster_score),
            "total_score": float(total_score),
            "feature_top3_sum": feature_top3_sum,
            "feature_count": len(feature_scores),
            "block_sum": block_sum,
        })

    cluster_rows.sort(key=lambda row: row["cluster_score"], reverse=True)
    best_center = float(cluster_rows[0]["center"])
    second_score = float(cluster_rows[1]["cluster_score"]) if len(cluster_rows) > 1 else float(cluster_rows[0]["cluster_score"])

    support_rows = [
        row for row in vote_rows
        if abs(row["offset_sec"] - best_center) <= 1.0
    ]
    support_items = []
    seen = set()
    for row in sorted(support_rows, key=lambda r: r["weight"], reverse=True):
        key = row["roi"]
        if key not in seen:
            seen.add(key)
            support_items.append(row["item"])

    return {
        "best_center": best_center,
        "best_score": float(cluster_rows[0]["cluster_score"]),
        "second_score": second_score,
        "bins": [(row["center"], row["cluster_score"]) for row in cluster_rows],
        "cluster_rows": cluster_rows,
        "rows": vote_rows,
        "support_rows": support_rows,
        "support_items": support_items,
    }


def choose_final_result(results):
    if not results:
        raise RuntimeError("没有 ROI 对齐结果")

    results_sorted = sorted(results, key=lambda x: x["score"], reverse=True)
    vote_table = build_offset_vote_table(results_sorted)
    best_center = vote_table["best_center"]

    peer_items = vote_table["support_items"][: min(12, len(vote_table["support_items"]))]
    if not peer_items:
        peer_items = [results_sorted[0]]

    def representative_score(item):
        score = float(item["score"])
        score += 0.20 * np.exp(-abs(float(item["feature_lag_sec"]) - best_center) / 0.75)
        score += 0.10 * np.exp(-abs(float(item["mask_lag_sec"]) - best_center) / 0.75)
        if is_valid_block_info(item.get("block_info")):
            score += 0.08 * np.exp(-abs(float(item["block_info"]["offset"]) - best_center) / 0.75)
        return score

    best_item = max(peer_items, key=representative_score)

    strong_rows = [
        row for row in vote_table["support_rows"]
        if row["source"] == "feature"
    ]
    if strong_rows:
        strong_rows = sorted(strong_rows, key=lambda row: row["weight"], reverse=True)[:5]
        weights = np.array([row["weight"] for row in strong_rows], dtype=np.float64)
        offsets = np.array([row["offset_sec"] for row in strong_rows], dtype=np.float64)
        final_offset = float(np.sum(offsets * weights) / np.sum(weights))
    else:
        final_offset = float(best_item["feature_lag_sec"])

    if abs(final_offset - best_center) > 1.25:
        final_offset = 0.65 * final_offset + 0.35 * best_center

    if abs(best_item["mask_lag_sec"] - final_offset) <= 1.0:
        final_offset = 0.8 * final_offset + 0.2 * float(best_item["mask_lag_sec"])

    if is_valid_block_info(best_item.get("block_info")):
        block_offset = float(best_item["block_info"]["offset"])
        if abs(block_offset - final_offset) <= 1.5:
            final_offset = 0.75 * final_offset + 0.25 * block_offset

    return float(final_offset), best_item, peer_items, results_sorted, vote_table


def estimate_confidence(best_item, peer_items, all_results, vote_table):
    peer_scores = np.array([item["score"] for item in peer_items], dtype=np.float64)
    peer_mean = float(np.mean(peer_scores))

    cluster_score = float(vote_table["best_score"])
    second_cluster_score = float(vote_table["second_score"])
    cluster_margin = cluster_score - second_cluster_score

    support_offsets = np.array(
        [row["offset_sec"] for row in vote_table["support_rows"]],
        dtype=np.float64,
    )
    dispersion = float(np.std(support_offsets)) if len(support_offsets) > 1 else 0.0

    agreement = 0.6 * best_item["agree_mask"] + 0.4 * best_item["agree_peak"]
    if is_valid_block_info(best_item.get("block_info")):
        agreement = max(
            agreement,
            float(np.exp(-abs(best_item["block_info"]["offset"] - best_item["feature_lag_sec"]) / 1.2)),
        )

    score_level = np.clip(peer_mean / 0.75, 0.0, 1.0)
    score_margin = np.clip(cluster_margin / 0.30, 0.0, 1.0)
    score_dispersion = np.clip(1.0 - dispersion / 1.0, 0.0, 1.0)
    score_agreement = np.clip(agreement, 0.0, 1.0)
    confidence = (
        0.30 * score_level +
        0.25 * score_margin +
        0.20 * score_dispersion +
        0.25 * score_agreement
    )
    return float(confidence)


# ============================================================
# ==================== 调试图 ================================
# ============================================================
def save_debug_plots(first_frame, all_results, best_item, peer_items, final_offset, confidence, vote_table, output_path):
    frame_bgr = first_frame.copy()
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

    top_show = all_results[: min(8, len(all_results))]
    selected_roi = best_item["roi"]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    ax = axes[0, 0]
    ax.imshow(frame_rgb)
    for item in top_show:
        x, y, w, h = item["roi"]
        color = 'yellow'
        lw = 1.0
        if item["roi"] == selected_roi:
            color = 'red'
            lw = 2.0
        rect = plt.Rectangle((x, y), w, h, fill=False, edgecolor=color, linewidth=lw)
        ax.add_patch(rect)
        ax.text(x, max(0, y - 3), f'{item["feature_lag_sec"]:.1f}s', color=color, fontsize=8)
    ax.set_title('自动搜索到的高分 ROI')
    ax.axis('off')

    ax = axes[0, 1]
    roi_offsets = [item["feature_lag_sec"] for item in all_results[:20]]
    roi_scores = [item["score"] for item in all_results[:20]]
    ax.scatter(roi_offsets, roi_scores, c=roi_scores, cmap='viridis', s=40)
    ax.axvline(final_offset, color='red', linestyle='--', lw=1.2)
    if vote_table["bins"]:
        top_bins = vote_table["bins"][: min(5, len(vote_table["bins"]))]
        for bin_center, bin_score in top_bins:
            ax.text(bin_center, max(roi_scores) if roi_scores else 0, f'{bin_center:.1f}', fontsize=8, color='black')
    ax.set_title('ROI 候选分数与偏移')
    ax.set_xlabel('偏移量 (秒)')
    ax.set_ylabel('ROI 分数')

    xcorr_info = best_item["feature_xcorr"]
    ax = axes[1, 0]
    lag_axis = xcorr_info["lags"] / TARGET_FS
    corr_z = (xcorr_info["corr"] - np.mean(xcorr_info["corr"])) / max(np.std(xcorr_info["corr"]), 1e-9)
    ax.plot(lag_axis, corr_z, color='darkorange', lw=1.2)
    ax.axvline(best_item["feature_lag_sec"], color='red', linestyle='--', lw=1.2)
    ax.set_title('最佳 ROI 的互相关曲线')
    ax.set_xlabel('偏移量 (秒)')
    ax.set_ylabel('相关峰 Z 分数')

    ax = axes[1, 1]
    video_feature = best_item["video_feature"]
    imu_feature = build_imu_feature(wrist_file_path, IMU_MODE, TARGET_FS)
    v_seg, i_seg = overlap_segments(video_feature, imu_feature, best_item["feature_xcorr"]["refined_lag"])
    t = np.arange(len(v_seg)) / TARGET_FS
    ax.plot(t, zscore_normalize(v_seg), color='crimson', lw=1.0, label='视频特征')
    ax.plot(t, zscore_normalize(i_seg), color='steelblue', lw=1.0, label='腕表特征')
    ax.set_title(f'最佳对齐效果  offset={final_offset:.2f}s  conf={confidence:.3f}')
    ax.set_xlabel('相对时间 (秒)')
    ax.legend(loc='upper right')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close(fig)

    first_fig, first_ax = plt.subplots(figsize=(8, 6))
    first_ax.imshow(frame_rgb)
    x, y, w, h = selected_roi
    rect = plt.Rectangle((x, y), w, h, fill=False, edgecolor='red', linewidth=2.0)
    first_ax.add_patch(rect)
    first_ax.set_title('最终选中的 ROI')
    first_ax.axis('off')
    plt.tight_layout()
    plt.savefig(FIRST_FRAME_DEBUG_PATH, dpi=150, bbox_inches='tight')
    plt.close(first_fig)


# ============================================================
# ==================== 主流程 ================================
# ============================================================
def main():
    if not Path(video_path).exists():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")
    if not Path(wrist_file_path).exists():
        raise FileNotFoundError(f"找不到腕表文件: {wrist_file_path}")

    watch_side = extract_watch_side(wrist_file_path)
    imu_feature = build_imu_feature(wrist_file_path, IMU_MODE, TARGET_FS)

    cap = cv2.VideoCapture(video_path)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("无法读取视频第一帧")
    first_small = cv2.resize(frame, None, fx=FRAME_SCALE, fy=FRAME_SCALE, interpolation=cv2.INTER_AREA)

    if MANUAL_ROI is not None:
        rois = [scale_roi_to_small(MANUAL_ROI, FRAME_SCALE, first_small.shape)]
        roi_priors = np.asarray([1.0], dtype=np.float64)
    else:
        rois, roi_priors = generate_candidate_rois(first_small.shape, watch_side=watch_side)
    first_frame, roi_series = compute_video_roi_series(
        video_path,
        target_fs=FLOW_TARGET_FPS if 'FLOW_TARGET_FPS' in globals() else TARGET_FS,
        scale=FRAME_SCALE,
        rois=rois,
    )

    results = []
    for roi, prior, motion_signal in zip(rois, roi_priors, roi_series):
        video_feature = build_activity_feature(motion_signal, TARGET_FS)
        result = build_roi_result(video_feature, imu_feature, roi, prior)
        results.append(result)

    final_offset, best_item, peer_items, all_results, vote_table = choose_final_result(results)
    confidence = estimate_confidence(best_item, peer_items, all_results, vote_table)
    primary_event = estimate_primary_event_range(best_item["video_feature"], TARGET_FS)

    if SAVE_DEBUG_PLOT:
        save_debug_plots(
            first_frame,
            all_results,
            best_item,
            peer_items,
            final_offset,
            confidence,
            vote_table,
            DEBUG_PLOT_PATH,
        )

    print(f"offset_seconds={final_offset:.2f}")
    print(f"confidence={confidence:.3f}")
    if primary_event is not None:
        print(f"video_event_start_sec={primary_event['start_sec']:.2f}")
        print(f"video_event_end_sec={primary_event['end_sec']:.2f}")
        print(f"video_event_duration_sec={primary_event['duration_sec']:.2f}")

    if VERBOSE:
        print(f"watch_side={watch_side}")
        print(f"selected_roi={best_item['roi']}")
        print(f"selected_peak_lag={best_item['peak_lag_sec']:.2f}")
        print(f"selected_mask_lag={best_item['mask_lag_sec']:.2f}")
        block_info = best_item.get("block_info")
        print(f"vote_center={vote_table['best_center']:.2f}")
        if block_info is not None:
            print(f"block_offset={block_info['offset']:.2f}")
            print(f"block_dispersion={block_info['dispersion']:.2f}")
            print(f"block_count={block_info['count']}")
        if primary_event is not None:
            print(
                f"primary_event={primary_event['start_sec']:.2f}s~"
                f"{primary_event['end_sec']:.2f}s "
                f"dur={primary_event['duration_sec']:.2f}s "
                f"support={primary_event['support_count']}"
            )
        print(f"vote_top_bins={[(round(k,2), round(v,3)) for k, v in vote_table['bins'][:5]]}")
        print(f"top_scores={[round(item['score'], 3) for item in all_results[:5]]}")


if __name__ == '__main__':
    main()
