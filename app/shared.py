# -*- coding: utf-8 -*-
import os
import re
import sys
from datetime import datetime, timedelta
from PyQt5.QtCore import Qt, QSize, QTimer, QUrl, QElapsedTimer
from PyQt5.QtGui import QFont, QPainter, QColor, QPen, QBrush, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QMainWindow, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QListWidget,
    QListWidgetItem, QComboBox, QTextEdit, QFileDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QStackedWidget, QMessageBox, QTabWidget,
    QSlider, QSizePolicy, QCheckBox, QDialog, QInputDialog, QScrollArea, QSplitter,
    QAbstractItemView
)

try:
    from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
    from PyQt5.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QMediaContent = None
    QMediaPlayer = None
    QVideoWidget = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
    from scipy.io import loadmat
    from scipy.signal import butter, filtfilt
except ImportError:
    np = None
    loadmat = None
    butter = None
    filtfilt = None


# =========================
# 全局样式
# =========================

APP_QSS = """
QMainWindow {
    background-color: #E8F7FF;
}

QWidget {
    font-family: "Microsoft YaHei";
    font-size: 10pt;
    color: #234;
}

QFrame#MainCard {
    background-color: rgba(255, 255, 255, 0.82);
    border: 1px solid #B7DCEA;
    border-radius: 18px;
}

QFrame#SideBar {
    background-color: #DDF3FA;
    border-right: 1px solid #A6C9D8;
}

QLabel#TitleLabel {
    font-size: 18pt;
    font-weight: bold;
    color: #236B8E;
}

QLabel#SubTitleLabel {
    font-size: 11pt;
    color: #5F7D8A;
}

QLabel#LogoLabel {
    font-size: 12pt;
    font-weight: bold;
    color: #266B8C;
}

QLabel#SectionTitle {
    font-size: 11pt;
    font-weight: bold;
    color: #266B8C;
}

QLineEdit, QTextEdit, QComboBox {
    background-color: white;
    border: 1px solid #A8CBD9;
    border-radius: 8px;
    padding: 6px 9px;
    min-height: 22px;
    selection-background-color: #36A9E1;
}

QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border: 1px solid #29A8DF;
}

QPushButton {
    background-color: #2FA8E1;
    color: white;
    border: none;
    border-radius: 9px;
    padding: 7px 12px;
    font-weight: bold;
    min-height: 24px;
}

QPushButton:hover {
    background-color: #158FC8;
}

QPushButton:pressed {
    background-color: #0877AA;
}

QPushButton#WhiteButton {
    background-color: white;
    color: #235;
    border: 1px solid #A8CBD9;
}

QPushButton#WhiteButton:hover {
    background-color: #F1FBFF;
    border: 1px solid #2FA8E1;
}

QPushButton#DangerButton {
    background-color: #F56C6C;
    color: white;
}

QPushButton#DangerButton:hover {
    background-color: #E45656;
}

QListWidget {
    background-color: white;
    border: 1px solid #A8CBD9;
    border-radius: 10px;
    padding: 8px;
}

QListWidget::item {
    padding: 10px;
    border-radius: 7px;
}

QListWidget::item:selected {
    background-color: #DFF4FF;
    color: #12638B;
}

QTableWidget {
    background-color: white;
    border: 1px solid #A8CBD9;
    border-radius: 10px;
    gridline-color: #D5EAF2;
    selection-background-color: #DFF4FF;
    selection-color: #123;
}

QTableWidget::item {
    padding: 6px;
}

QHeaderView::section {
    background-color: #CDEDF8;
    color: #235;
    border: none;
    border-right: 1px solid #B5D8E5;
    padding: 8px;
    font-weight: bold;
}

QScrollBar:vertical {
    background: #EAF8FD;
    width: 10px;
    border-radius: 5px;
}

QScrollBar::handle:vertical {
    background: #9BD2E7;
    border-radius: 5px;
}

QSplitter::handle {
    background-color: #B7DCEA;
    border-radius: 3px;
}

QSplitter::handle:hover {
    background-color: #36A9E1;
}
"""


# =========================
# 通用组件
# =========================

def make_card():
    """创建应用内统一样式的卡片容器。"""
    card = QFrame()
    card.setObjectName("MainCard")
    return card


def make_white_button(text):
    """创建白色次要按钮。"""
    btn = QPushButton(text)
    btn.setObjectName("WhiteButton")
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def make_blue_button(text):
    """创建蓝色主按钮。"""
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def make_danger_button(text):
    """创建危险操作按钮。"""
    btn = QPushButton(text)
    btn.setObjectName("DangerButton")
    btn.setCursor(Qt.PointingHandCursor)
    return btn


WATCH_TIME_FORMATS = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%dT%H:%M:%S", True),
    ("%H:%M:%S", False),
)


def parse_watch_start_datetime(start_time_text):
    """解析腕表起始时间文本，返回 datetime 和是否包含日期。"""
    text = str(start_time_text or "").strip()
    if not text:
        return None, False
    for fmt, has_date in WATCH_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), has_date
        except ValueError:
            continue
    return None, False


def format_relative_ms(value_ms):
    """把毫秒格式化为相对时间，优先显示 HH:MM:SS。"""
    total_seconds = max(0, int(value_ms) // 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_watch_time(start_time_text, value_ms, offset_ms=0, clock_only=False):
    """按腕表真实起始时间把毫秒时间格式化为真实时钟时间。"""
    base_time, has_date = parse_watch_start_datetime(start_time_text)
    target_ms = max(0, int(value_ms) + int(offset_ms))
    if base_time is None:
        return format_relative_ms(target_ms)
    current = base_time + timedelta(milliseconds=target_ms)
    if clock_only or not has_date:
        return current.strftime("%H:%M:%S")
    return current.strftime("%Y-%m-%d %H:%M:%S")


def parse_clock_time_text(time_text):
    """解析用户输入的日期时间或时分秒文本。"""
    text = str(time_text or "").strip()
    if not text:
        return None, False
    for fmt, has_date in WATCH_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt), has_date
        except ValueError:
            continue
    for fmt in ("%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed, "%Y" in fmt
        except ValueError:
            continue
    return None, False


def format_clock_time_value(value, has_date=False):
    """按是否包含日期格式化时间刻度。"""
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S" if has_date else "%H:%M:%S")


def ms_between_clock_times(start_text, target_text):
    """计算目标时间相对开始时间的毫秒偏移。"""
    start, start_has_date = parse_clock_time_text(start_text)
    target, target_has_date = parse_clock_time_text(target_text)
    if start is None or target is None:
        return None
    if not start_has_date and not target_has_date and target < start:
        target = target + timedelta(days=1)
    return int((target - start).total_seconds() * 1000)


def extract_watch_start_time(mat_data, data=None):
    """优先按可靠的 workspace 方式读取 MAT 腕表起始时间。"""
    workspace = mat_data.get("__function_workspace__")
    if workspace is not None:
        try:
            ws_bytes = workspace[0].tobytes()
            matches = re.findall(
                rb"\d\x00\d\x00:\x00\d\x00\d\x00:\x00\d\x00\d\x00",
                ws_bytes
            )
            if matches:
                return matches[0].decode("utf-16-le")
        except Exception:
            pass

    data = mat_data.get("data") if data is None else data
    if data is None or getattr(data, "shape", (0,))[0] == 0:
        return ""

    raw_time = np.asarray(data[0, 0]).flatten()
    if len(raw_time) == 0:
        return ""

    first = raw_time[0]
    text = str(first)
    full_match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}", text)
    if full_match:
        return full_match.group().replace("/", "-")

    time_match = re.search(r"\d{1,2}:\d{2}:\d{2}", text)
    if time_match:
        return time_match.group()

    try:
        value = float(np.asarray(first).ravel()[0])
        if value > 100000:
            day = datetime.fromordinal(int(value) - 366)
            fraction = timedelta(days=value % 1)
            return (day + fraction).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""

