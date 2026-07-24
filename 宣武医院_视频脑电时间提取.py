import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:  # pragma: no cover
    tk = None
    filedialog = None


TZ = timezone(timedelta(hours=8))


def choose_root() -> Path:
    if tk is None or filedialog is None:
        raise RuntimeError("当前 Python 环境无法打开目录选择框，请直接在命令行后面跟路径。")
    root = tk.Tk()
    root.withdraw()
    selected = filedialog.askdirectory(title="选择宣武医院视频脑电数据根目录")
    root.destroy()
    if not selected:
        raise RuntimeError("未选择目录。")
    return Path(selected)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取宣武医院导出的视频脑电时间和标记信息")
    parser.add_argument("root", nargs="?", help="数据根目录，不传则弹窗选择")
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))


def parse_exam_info(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig", errors="ignore")
    result = {}
    for field in ("PatientID", "ExamID", "ExamNote", "ExamTime", "ExamEndTime"):
        match = re.search(rf'"{field}"\s*:\s*"([^"]*)"', raw)
        if match:
            result[field] = match.group(1)
    return result


def iter_exam_dirs(root: Path):
    seen = set()
    for path in root.rglob("datainfo.json"):
        exam_dir = path.parent.resolve()
        if exam_dir not in seen:
            seen.add(exam_dir)
            yield exam_dir


def parse_events(record_info: dict, start_dt: datetime) -> list[dict]:
    events = []
    for item in record_info.get("RecordEvents", []) or []:
        offset_ms = int(item.get("TimeStampInMillisecond", 0))
        absolute_dt = start_dt + timedelta(milliseconds=offset_ms)
        events.append(
            {
                "annotation": item.get("Annotation", ""),
                "offset_milliseconds": offset_ms,
                "absolute_time": absolute_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "duration": item.get("Duration", 0),
                "remark": item.get("Remark", ""),
            }
        )
    events.sort(key=lambda x: x["offset_milliseconds"])
    return events


def parse_exam_dir(exam_dir: Path) -> dict:
    data_info = load_json(exam_dir / "datainfo.json")
    video_info = load_json(exam_dir / "videoinfo.json") if (exam_dir / "videoinfo.json").exists() else {}
    record_info = load_json(exam_dir / "RecordInfo.json") if (exam_dir / "RecordInfo.json").exists() else {}
    exam_info = parse_exam_info(exam_dir / "ExamInfo.json")

    start_dt = datetime.fromtimestamp(int(data_info["startTime"]), tz=TZ)
    end_dt = start_dt + timedelta(seconds=float(data_info["duration"]))
    events = parse_events(record_info, start_dt)

    return {
        "folder": str(exam_dir),
        "patient_id": exam_info.get("PatientID", ""),
        "exam_id": exam_info.get("ExamID", ""),
        "exam_note": exam_info.get("ExamNote", ""),
        "exam_time_from_examinfo": exam_info.get("ExamTime", ""),
        "exam_end_time_from_examinfo": exam_info.get("ExamEndTime", ""),
        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_duration_seconds": data_info.get("duration"),
        "sample_rate": data_info.get("sampleRate"),
        "channel_count": data_info.get("channelCount"),
        "video_duration_ms": ((video_info.get("section1") or {}).get("durationForVideo1")),
        "events": events,
    }


def dedupe_records(records: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for record in records:
        signature = (
            record["patient_id"],
            record["exam_id"],
            record["exam_note"],
            record["start_time"],
            record["end_time"],
            tuple((evt["annotation"], evt["offset_milliseconds"]) for evt in record["events"]),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(record)
    return unique


def print_record(record: dict) -> None:
    print(f"目录: {record['folder']}")
    if record["patient_id"]:
        print(f"患者ID: {record['patient_id']}")
    if record["exam_id"]:
        print(f"检查ID: {record['exam_id']}")
    if record["exam_note"]:
        print(f"检查备注: {record['exam_note']}")
    print(f"开始时间: {record['start_time']}")
    print(f"结束时间: {record['end_time']}")
    print(f"信号时长(秒): {record['signal_duration_seconds']}")
    print(f"视频时长(ms): {record['video_duration_ms']}")
    print("标记:")
    if not record["events"]:
        print("  无")
    for event in record["events"]:
        print(
            "  - "
            f"{event['annotation']} | 偏移 {event['offset_milliseconds']} ms | "
            f"绝对时间 {event['absolute_time']}"
        )
    print()


def main() -> int:
    args = parse_args()
    root = Path(args.root) if args.root else choose_root()
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {root}")

    records = [parse_exam_dir(exam_dir) for exam_dir in iter_exam_dirs(root)]
    records = dedupe_records(records)
    records.sort(key=lambda x: (x["start_time"], x["folder"]))

    if not records:
        print("未找到 datainfo.json，像是没有检测到宣武医院这一类导出结构。", file=sys.stderr)
        return 1

    for record in records:
        print_record(record)

    print("JSON结果:")
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
