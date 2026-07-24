import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:  # pragma: no cover
    tk = None
    filedialog = None


KNOWN_EVENT_LABELS = [
    "REC START",
    "EEG onset",
    "MARK ON",
    "MARK OFF",
    "EEG stop",
]


def choose_root() -> Path:
    if tk is None or filedialog is None:
        raise RuntimeError("当前 Python 环境无法打开目录选择框，请直接在命令行后面跟路径。")
    root = tk.Tk()
    root.withdraw()
    selected = filedialog.askdirectory(title="选择天坛医院视频脑电数据根目录")
    root.destroy()
    if not selected:
        raise RuntimeError("未选择目录。")
    return Path(selected)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取天坛医院日本光电视频脑电时间和标记信息")
    parser.add_argument("root", nargs="?", help="数据根目录，不传则弹窗选择")
    return parser.parse_args()


def parse_compact_timestamp(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if len(text) < 14:
        return text
    base = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    if len(text) == 14:
        return base.strftime("%Y-%m-%d %H:%M:%S")
    fraction = text[14:]
    microsecond = int((fraction + "000000")[:6])
    base = base.replace(microsecond=microsecond)
    return base.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def parse_yy_timestamp(text: str) -> str:
    base = datetime.strptime(text, "%y%m%d%H%M%S")
    return base.strftime("%Y-%m-%d %H:%M:%S")


def parse_egf(path: Path) -> dict:
    raw = path.read_bytes().decode("latin1", errors="ignore").replace("\x00", "")
    info = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip()
    return info


def parse_video_segments(egf_info: dict) -> list[dict]:
    segments = []
    for key, value in egf_info.items():
        if not key.lower().startswith("mpeg"):
            continue
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 3:
            continue
        start_raw, end_raw, relative_path = parts
        segments.append(
            {
                "segment": key,
                "start_raw": start_raw,
                "end_raw": end_raw,
                "start_time": parse_compact_timestamp(start_raw),
                "end_time": parse_compact_timestamp(end_raw),
                "relative_path": relative_path,
            }
        )
    segments.sort(key=lambda x: x["start_raw"])
    return segments


def find_case_insensitive(base: Path, suffix: str) -> Path | None:
    suffix = suffix.lower()
    for item in base.parent.iterdir():
        if item.stem.lower() == base.stem.lower() and item.suffix.lower() == suffix:
            return item
    return None


def extract_events_from_log(path: Path) -> list[dict]:
    text = path.read_bytes().decode("latin1", errors="ignore").replace("\x00", "")
    events = []
    for label in KNOWN_EVENT_LABELS:
        start = 0
        while True:
            index = text.find(label, start)
            if index < 0:
                break
            window = text[index : index + 160]
            match = re.search(r"(\d{6})\((\d{12})\)", window)
            if match:
                offset_code = match.group(1)
                raw_ts = match.group(2)
                events.append(
                    {
                        "label": label,
                        "offset_code": offset_code,
                        "absolute_time": parse_yy_timestamp(raw_ts),
                        "raw_timestamp": raw_ts,
                    }
                )
            start = index + len(label)
    order = {label: i for i, label in enumerate(KNOWN_EVENT_LABELS)}
    events.sort(key=lambda x: order.get(x["label"], 999))
    return events


def extract_events_from_pnt(path: Path) -> list[dict]:
    # Some exports keep event text in .LOG instead of .pnt.
    # This fallback only handles plain-text .pnt files.
    text = path.read_bytes().decode("latin1", errors="ignore").replace("\x00", "")
    events = []
    for label in KNOWN_EVENT_LABELS:
        start = 0
        while True:
            index = text.find(label, start)
            if index < 0:
                break
            window = text[index : index + 160]
            match = re.search(r"(\d{6})\((\d{12})\)", window)
            if match:
                offset_code = match.group(1)
                raw_ts = match.group(2)
                events.append(
                    {
                        "label": label,
                        "offset_code": offset_code,
                        "absolute_time": parse_yy_timestamp(raw_ts),
                        "raw_timestamp": raw_ts,
                    }
                )
            start = index + len(label)
    order = {label: i for i, label in enumerate(KNOWN_EVENT_LABELS)}
    events.sort(key=lambda x: order.get(x["label"], 999))
    return events


def parse_exam(base_egf: Path) -> dict:
    egf_info = parse_egf(base_egf)
    segments = parse_video_segments(egf_info)
    pnt_path = find_case_insensitive(base_egf, ".pnt")
    log_path = find_case_insensitive(base_egf, ".LOG")
    events = []
    if log_path and log_path.exists():
        events = extract_events_from_log(log_path)
    if not events and pnt_path and pnt_path.exists():
        events = extract_events_from_pnt(pnt_path)

    video_start = segments[0]["start_time"] if segments else ""
    video_end = segments[-1]["end_time"] if segments else ""

    return {
        "exam_base_name": base_egf.stem,
        "egf_path": str(base_egf),
        "pnt_path": str(pnt_path) if pnt_path else "",
        "log_path": str(log_path) if log_path else "",
        "comment_clip": egf_info.get("COMMENT_CLIP", ""),
        "video_directory": egf_info.get("IMAGEDIR", ""),
        "video_rectime_correction_ms": egf_info.get("RECTIMECORRECTION", ""),
        "video_start_time": video_start,
        "video_end_time": video_end,
        "video_segments": segments,
        "events": events,
    }


def print_record(record: dict) -> None:
    print(f"检查名: {record['exam_base_name']}")
    print(f"EGF文件: {record['egf_path']}")
    if record["pnt_path"]:
        print(f"PNT文件: {record['pnt_path']}")
    if record["log_path"]:
        print(f"LOG文件: {record['log_path']}")
    if record["comment_clip"]:
        print(f"备注: {record['comment_clip']}")
    print(f"视频开始时间: {record['video_start_time']}")
    print(f"视频结束时间: {record['video_end_time']}")
    print("视频分段:")
    if not record["video_segments"]:
        print("  无")
    for segment in record["video_segments"]:
        print(
            "  - "
            f"{segment['segment']} | {segment['start_time']} -> {segment['end_time']} | "
            f"{segment['relative_path']}"
        )
    print("事件/标记:")
    if not record["events"]:
        print("  无")
    for event in record["events"]:
        print(
            "  - "
            f"{event['label']} | 相对码 {event['offset_code']} | 绝对时间 {event['absolute_time']}"
        )
    print()


def main() -> int:
    args = parse_args()
    root = Path(args.root) if args.root else choose_root()
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {root}")

    egf_files = sorted(root.rglob("*.EGF"))
    if not egf_files:
        egf_files = sorted(root.rglob("*.egf"))
    if not egf_files:
        print("未找到 .EGF 文件，像是没有检测到天坛医院这一类日本光电导出结构。", file=sys.stderr)
        return 1

    records = [parse_exam(path) for path in egf_files]
    records.sort(key=lambda x: (x["video_start_time"], x["exam_base_name"]))

    for record in records:
        print_record(record)

    print("JSON结果:")
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
