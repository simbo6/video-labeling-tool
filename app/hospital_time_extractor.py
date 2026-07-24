# -*- coding: utf-8 -*-
"""医院视频脑电导出目录的时间信息提取。"""

import importlib.util
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
TIANTAN_SCRIPT = ROOT_DIR / "天坛医院_视频脑电时间提取.py"
XUANWU_SCRIPT = ROOT_DIR / "宣武医院_视频脑电时间提取.py"


def _load_script(script_path):
    """按文件路径加载现有医院解析脚本。"""
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载解析脚本：{script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _detect_hospital(root):
    """根据目录结构自动判断医院导出类型。"""
    if any(root.rglob("datainfo.json")):
        return "宣武医院"
    if any(root.rglob("*.EGF")) or any(root.rglob("*.egf")):
        return "天坛医院"
    return ""


def _pick_event_time(events, names):
    """按候选名称顺序返回第一个匹配到的绝对时间。"""
    for name in names:
        target = str(name or "").strip().lower()
        for event in events:
            label = str(event.get("annotation") or event.get("label") or "").strip().lower()
            if label == target:
                return str(event.get("absolute_time") or "")
    return ""


def _extract_date_and_time(value):
    """把完整时间拆成日期和时分秒，忽略毫秒。"""
    text = str(value or "").strip()
    if not text:
        return "", ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^(\d{2}:\d{2}:\d{2})", text)
    if match:
        return "", match.group(1)
    return "", text


def _pick_seizure_date(*values):
    """从多个时间字段里挑选一个可用日期。"""
    for value in values:
        date_text, _ = _extract_date_and_time(value)
        if date_text:
            return date_text
    return ""


def _normalize_event_times(events):
    """事件摘要里只保留到秒，避免重复显示年月日。"""
    normalized = []
    for event in events or []:
        copied = dict(event)
        _, absolute_time = _extract_date_and_time(copied.get("absolute_time"))
        copied["absolute_time"] = absolute_time
        normalized.append(copied)
    return normalized


def _normalize_tiantan_record(record, root):
    """把天坛脚本记录转换为项目可直接保存的字段。"""
    events = _normalize_event_times(record.get("events") or [])
    raw_video_start = record.get("video_start_time") or ""
    raw_video_end = record.get("video_end_time") or ""
    raw_marker_start = _pick_event_time(record.get("events") or [], ["EEG onset"])
    raw_marker_end = _pick_event_time(record.get("events") or [], ["EEG stop"])
    seizure_date = _pick_seizure_date(raw_marker_start, raw_video_start, raw_marker_end, raw_video_end)
    _, video_start = _extract_date_and_time(raw_video_start)
    _, video_end = _extract_date_and_time(raw_video_end)
    _, marker_start = _extract_date_and_time(raw_marker_start)
    _, marker_end = _extract_date_and_time(raw_marker_end)
    return {
        "hospital_type": "天坛医院",
        "hospital_data_path": str(root),
        "hospital": "天坛医院",
        "seizure_date": seizure_date,
        "video_start": video_start,
        "video_end": video_end,
        "eeg_marker_start": marker_start,
        "eeg_marker_end": marker_end,
        "record_name": record.get("exam_base_name") or "",
        "record_path": record.get("egf_path") or "",
        "events": events,
        "summary": _build_summary("天坛医院", seizure_date, video_start, video_end, marker_start, marker_end, events),
    }


def _normalize_xuanwu_record(record, root):
    """把宣武脚本记录转换为项目可直接保存的字段。"""
    events = _normalize_event_times(record.get("events") or [])
    raw_video_start = record.get("start_time") or ""
    raw_video_end = record.get("end_time") or ""
    raw_marker_start = _pick_event_time(record.get("events") or [], ["pre", "start", "onset", "发作开始"])
    raw_marker_end = _pick_event_time(record.get("events") or [], ["end", "stop", "offset", "发作结束"])
    seizure_date = _pick_seizure_date(raw_marker_start, raw_video_start, raw_marker_end, raw_video_end)
    _, video_start = _extract_date_and_time(raw_video_start)
    _, video_end = _extract_date_and_time(raw_video_end)
    _, marker_start = _extract_date_and_time(raw_marker_start)
    _, marker_end = _extract_date_and_time(raw_marker_end)
    return {
        "hospital_type": "宣武医院",
        "hospital_data_path": str(root),
        "hospital": "宣武医院",
        "seizure_date": seizure_date,
        "video_start": video_start,
        "video_end": video_end,
        "eeg_marker_start": marker_start,
        "eeg_marker_end": marker_end,
        "record_name": record.get("exam_note") or record.get("exam_id") or "",
        "record_path": record.get("folder") or "",
        "events": events,
        "summary": _build_summary("宣武医院", seizure_date, video_start, video_end, marker_start, marker_end, events),
    }


def _build_summary(hospital_type, seizure_date, video_start, video_end, marker_start, marker_end, events):
    """生成界面上展示的提取摘要。"""
    lines = [
        f"医院类型：{hospital_type}",
        f"发作日期：{seizure_date or '未读取到'}",
        f"视频开始：{video_start or '未读取到'}",
        f"视频结束：{video_end or '未读取到'}",
        f"发作开始：{marker_start or '未读取到'}",
        f"发作结束：{marker_end or '未读取到'}",
        f"标记数量：{len(events or [])}",
    ]
    for event in (events or [])[:8]:
        label = event.get("annotation") or event.get("label") or "标记"
        time_text = event.get("absolute_time") or ""
        lines.append(f"- {label}: {time_text}")
    return "\n".join(lines)


def extract_hospital_times(path, hospital_type="自动识别"):
    """读取医院导出目录，返回视频时间和发作标记时间。"""
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")

    detected = _detect_hospital(root) if hospital_type in ("", "自动识别") else hospital_type
    if detected == "天坛医院":
        module = _load_script(TIANTAN_SCRIPT)
        egf_files = sorted(root.rglob("*.EGF")) or sorted(root.rglob("*.egf"))
        if not egf_files:
            raise RuntimeError("未找到 .EGF 文件，无法按天坛医院格式读取。")
        records = [module.parse_exam(path) for path in egf_files]
        records.sort(key=lambda item: (item.get("video_start_time") or "", item.get("exam_base_name") or ""))
        return _normalize_tiantan_record(records[0], root)

    if detected == "宣武医院":
        module = _load_script(XUANWU_SCRIPT)
        records = [module.parse_exam_dir(exam_dir) for exam_dir in module.iter_exam_dirs(root)]
        records = module.dedupe_records(records)
        records.sort(key=lambda item: (item.get("start_time") or "", item.get("folder") or ""))
        if not records:
            raise RuntimeError("未找到 datainfo.json，无法按宣武医院格式读取。")
        return _normalize_xuanwu_record(records[0], root)

    raise RuntimeError("未识别到天坛医院或宣武医院的数据结构。")
