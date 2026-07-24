# -*- coding: utf-8 -*-
from .shared import *

class SignalWaveWidget(QWidget):
    """函数说明：执行当前函数对应的业务逻辑。"""

    def __init__(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self.setToolTip("按住 Ctrl 并滚动鼠标滚轮可缩放腕表信号时间窗口")
        self.setMinimumHeight(220)
        self.duration_ms = 60000
        self.data_duration_ms = 60000
        self.position_ms = 0
        self.window_steps_ms = [30 * 1000, 45 * 1000, 60 * 1000, 90 * 1000, 120 * 1000, 180 * 1000, 240 * 1000, 300 * 1000]
        self.window_step_index = 2
        self.window_ms = 60 * 1000
        self.signal_offset_ms = 0
        self.min_signal_offset_ms = -30 * 60 * 1000
        self.max_signal_offset_ms = 30 * 60 * 1000
        self.align_mode = False
        self.offset_locked = True
        self.drag_start_x = None
        self.drag_start_offset_ms = 0
        self.annotation_dragging = False
        self.annotation_drag_start_ms = 0
        self.annotation_active_range = None
        self.annotation_ranges = []
        self.on_annotation_range = None
        self.start_time_text = ""
        self.on_offset_changed = None
        self.signals = []
        self.visible_signal_names = {"ACC", "GYRO", "EMG", "PPG"}

    def set_signals(self, signals, duration_ms=0, data_duration_ms=0, start_time_text=""):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.signals = signals
        self.duration_ms = max(1, int(duration_ms or self.duration_ms))
        self.data_duration_ms = max(1, int(data_duration_ms or self.data_duration_ms))
        self.position_ms = 0
        self.start_time_text = start_time_text
        self.update()

    def set_visible_signals(self, names):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.visible_signal_names = set(names)
        self.update()

    def set_position(self, position_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.position_ms = max(0, int(position_ms))
        self.update()

    def set_align_mode(self, enabled):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.align_mode = enabled
        self.setCursor(Qt.OpenHandCursor if enabled and not self.offset_locked else Qt.ArrowCursor)

    def set_offset_locked(self, locked):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.offset_locked = locked
        self.setCursor(Qt.OpenHandCursor if self.align_mode and not locked else Qt.ArrowCursor)

    def set_signal_offset(self, offset_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.signal_offset_ms = self.clamp_signal_offset(offset_ms)
        self.update()
        if self.on_offset_changed:
            self.on_offset_changed()

    def get_signal_offset(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        return self.signal_offset_ms

    def set_offset_limit(self, min_offset_ms, max_offset_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.min_signal_offset_ms = int(min_offset_ms)
        self.max_signal_offset_ms = int(max_offset_ms)
        self.signal_offset_ms = self.clamp_signal_offset(self.signal_offset_ms)
        self.update()

    def clamp_signal_offset(self, offset_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        return max(self.min_signal_offset_ms, min(int(offset_ms), self.max_signal_offset_ms))

    def paintEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        painter.fillRect(self.rect(), QColor("#FFFFFF"))

        display_signals = [item for item in self.signals if item[0] in self.visible_signal_names]
        names = [item[0] for item in display_signals] if display_signals else []
        colors = [QColor("#2FA8E1"), QColor("#8E6BE8"), QColor("#34B27A")]

        w = self.width()
        lane_height = max(48, min(66, int((self.height() - 70) / max(len(names), 1))))
        start_y = 30
        amplitude = max(18, min(28, lane_height // 2 - 4))
        plot_x1 = 90
        plot_x2 = max(plot_x1 + 1, w - 30)
        visible_start_ms, visible_end_ms = self.visible_data_range()

        if not names:
            painter.setPen(QColor("#6E8FA0"))
            painter.drawText(plot_x1, 40, "未选择腕表信号")

        for i, name in enumerate(names):
            y = start_y + i * lane_height

            painter.setPen(QPen(QColor("#24677F"), 2))
            painter.drawText(15, y - 12, name)

            painter.setPen(QPen(QColor("#B8D4DE"), 1))
            painter.drawLine(plot_x1, y, plot_x2, y)

            painter.setPen(QPen(colors[i % len(colors)], 2))
            values = display_signals[i][1] if i < len(display_signals) else None
            sample_rate = display_signals[i][2] if i < len(display_signals) and len(display_signals[i]) > 2 else 1.0
            if values is None or len(values) < 2:
                self.draw_demo_signal(painter, y, plot_x1, plot_x2, amplitude)
            else:
                self.draw_raw_signal(painter, values, sample_rate, visible_start_ms, visible_end_ms, y, plot_x1, plot_x2, amplitude)

        self.draw_annotation_ranges(painter, plot_x1, plot_x2, visible_start_ms, visible_end_ms)
        cursor_x = self.cursor_x_in_minute(plot_x1, plot_x2)
        painter.setPen(QPen(QColor("#E54B4B"), 2))
        painter.drawLine(cursor_x, 8, cursor_x, self.height() - 8)
        self.draw_time_axis(painter, plot_x1, plot_x2, visible_start_ms, visible_end_ms)

    def set_annotation_ranges(self, ranges):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.annotation_ranges = list(ranges or [])
        self.update()

    def set_active_annotation_range(self, start_ms, end_ms=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if start_ms is None:
            self.annotation_active_range = None
        else:
            end_ms = start_ms if end_ms is None else end_ms
            self.annotation_active_range = (int(start_ms), int(end_ms))
        self.update()

    def draw_annotation_ranges(self, painter, x1, x2, visible_start_ms, visible_end_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        ranges = list(self.annotation_ranges)
        if self.annotation_active_range is not None:
            ranges.append(self.annotation_active_range)
        if not ranges:
            return

        span = max(1, visible_end_ms - visible_start_ms)
        top = 8
        height = max(1, self.height() - 42)
        for start_ms, end_ms in ranges:
            left_ms, right_ms = sorted([int(start_ms), int(end_ms)])
            if right_ms < visible_start_ms or left_ms > visible_end_ms:
                continue
            left_ms = max(left_ms, visible_start_ms)
            right_ms = min(right_ms, visible_end_ms)
            left_x = int(x1 + (left_ms - visible_start_ms) / span * (x2 - x1))
            right_x = int(x1 + (right_ms - visible_start_ms) / span * (x2 - x1))
            width = max(2, right_x - left_x)
            painter.fillRect(left_x, top, width, height, QColor(255, 206, 84, 72))
            painter.setPen(QPen(QColor("#D99821"), 1))
            painter.drawRect(left_x, top, width, height)

    def draw_demo_signal(self, painter, y, x1, x2, amplitude=18):
        """函数说明：执行当前函数对应的业务逻辑。"""
        last_x = x1
        last_y = y
        for k in range(1, 35):
            x = x1 + k * ((x2 - x1) / 35)
            val = 0
            if 4 < k < 10:
                val = (k % 3 + 1) * max(3, amplitude // 6)
            elif 22 < k < 29:
                val = (k % 4 + 1) * max(3, amplitude // 7)
            new_y = y - val
            painter.drawLine(int(last_x), int(last_y), int(x), int(new_y))
            last_x, last_y = x, new_y

    def draw_raw_signal(self, painter, values, sample_rate, visible_start_ms, visible_end_ms, y, x1, x2, amplitude=18):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if np is None:
            self.draw_demo_signal(painter, y, x1, x2, amplitude)
            return

        arr = np.asarray(values, dtype=float)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        if arr.shape[0] < 2:
            self.draw_demo_signal(painter, y, x1, x2, amplitude)
            return

        start_idx = max(0, int(np.floor(visible_start_ms / 1000 * sample_rate)))
        end_idx = min(arr.shape[0], int(np.ceil(visible_end_ms / 1000 * sample_rate)))
        if end_idx - start_idx < 2:
            return

        source_idx = np.arange(start_idx, end_idx)
        if len(source_idx) > max(20, x2 - x1):
            keep = np.linspace(0, len(source_idx) - 1, max(20, x2 - x1)).astype(int)
            source_idx = source_idx[keep]

        preview = arr[source_idx, :]
        finite_rows = np.any(np.isfinite(preview), axis=1)
        if not np.any(finite_rows):
            return
        preview = preview[finite_rows]
        source_idx = source_idx[finite_rows]

        visible_span_ms = max(1, visible_end_ms - visible_start_ms)
        x_values = x1 + ((source_idx / sample_rate * 1000 - visible_start_ms) / visible_span_ms) * (x2 - x1)
        valid_x = (x_values >= x1) & (x_values <= x2)
        if not np.any(valid_x):
            return
        x_values = x_values[valid_x]
        preview = preview[valid_x]

        axis_colors = [
            QColor("#2FA8E1"),
            QColor("#F6A623"),
            QColor("#34B27A"),
            QColor("#E456D7"),
        ]
        axis_labels = ["X", "Y", "Z", "W"]
        channel_count = min(preview.shape[1], 4)
        for channel in range(channel_count):
            channel_values = preview[:, channel]
            channel_finite = np.isfinite(channel_values)
            if not np.any(channel_finite):
                continue
            channel_x = x_values[channel_finite]
            full_channel = arr[:, channel]
            full_channel = full_channel[np.isfinite(full_channel)]
            if full_channel.size == 0:
                continue
            channel_min = float(np.min(full_channel))
            channel_max = float(np.max(full_channel))
            channel_span = max(channel_max - channel_min, 1e-8)
            norm = ((channel_values[channel_finite] - channel_min) / channel_span) * 2.0 - 1.0
            norm = np.clip(norm, -1.0, 1.0)
            painter.setPen(QPen(axis_colors[channel], 2))
            last_x = channel_x[0]
            last_y = y - int(norm[0] * amplitude)
            for x, value in zip(channel_x[1:], norm[1:]):
                new_y = y - int(value * amplitude)
                painter.drawLine(int(last_x), int(last_y), int(x), int(new_y))
                last_x, last_y = x, new_y

        if channel_count > 1:
            legend_x = x2 - 26 * channel_count
            for channel in range(channel_count):
                painter.setPen(axis_colors[channel])
                painter.drawText(legend_x + channel * 24, y - 12, axis_labels[channel])

    def visible_data_range(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        raw_start_ms = self.position_ms + self.signal_offset_ms
        start_ms = (raw_start_ms // self.window_ms) * self.window_ms
        end_ms = start_ms + self.window_ms
        return int(start_ms), int(end_ms)

    def cursor_x_in_minute(self, x1, x2):
        """函数说明：执行当前函数对应的业务逻辑。"""
        data_ms = self.position_ms + self.signal_offset_ms
        visible_start_ms, _ = self.visible_data_range()
        ratio = (data_ms - visible_start_ms) / max(1, self.window_ms)
        ratio = max(0.0, min(1.0, ratio))
        return int(x1 + ratio * (x2 - x1))

    def x_to_data_ms(self, x):
        """函数说明：执行当前函数对应的业务逻辑。"""
        plot_x1 = 90
        plot_x2 = max(plot_x1 + 1, self.width() - 30)
        ratio = (x - plot_x1) / max(1, plot_x2 - plot_x1)
        ratio = max(0.0, min(1.0, ratio))
        visible_start_ms, visible_end_ms = self.visible_data_range()
        return int(visible_start_ms + ratio * (visible_end_ms - visible_start_ms))

    def draw_time_axis(self, painter, x1, x2, start_ms, end_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        y = self.height() - 28
        painter.setPen(QPen(QColor("#6E8FA0"), 1))
        painter.drawLine(x1, y, x2, y)

        tick_count = 7
        for i in range(tick_count):
            ratio = i / max(1, tick_count - 1)
            x = int(x1 + ratio * (x2 - x1))
            data_ms = int(start_ms + ratio * (end_ms - start_ms))
            painter.drawLine(x, y - 5, x, y + 5)
            painter.setPen(QColor("#456"))
            painter.drawText(x - 34, y + 22, self.format_data_time(data_ms))
            painter.setPen(QPen(QColor("#6E8FA0"), 1))

        painter.setPen(QColor("#E54B4B"))
        painter.drawText(x1 + 8, 16, f"当前数据时间 {self.format_data_time(self.position_ms + self.signal_offset_ms)}")
        painter.drawText(max(x1 + 180, x2 - 100), 16, f"{int(self.window_ms / 1000)}s")
        if self.align_mode and not self.offset_locked:
            painter.setPen(QColor("#12638B"))
            painter.drawText(x1 + 8, 36, "对齐模式：按住下面波形左右拖动；完成后点固定")

    def format_data_time(self, data_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        seconds = int(max(0, data_ms) / 1000)
        if self.start_time_text:
            for fmt, output_fmt in [
                ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"),
                ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"),
                ("%H:%M:%S", "%H:%M:%S"),
            ]:
                try:
                    base = datetime.strptime(self.start_time_text, fmt)
                    return (base + timedelta(seconds=seconds)).strftime(output_fmt)
                except ValueError:
                    continue
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def mousePressEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if event.button() == Qt.LeftButton and self.align_mode and not self.offset_locked:
            self.drag_start_x = event.x()
            self.drag_start_offset_ms = self.signal_offset_ms
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.annotation_dragging = True
            self.annotation_drag_start_ms = self.x_to_data_ms(event.x())
            self.set_active_annotation_range(self.annotation_drag_start_ms, self.annotation_drag_start_ms)
            event.accept()

    def mouseMoveEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_dragging:
            current_ms = self.x_to_data_ms(event.x())
            self.set_active_annotation_range(self.annotation_drag_start_ms, current_ms)
            event.accept()
            return
        if self.drag_start_x is None or not self.align_mode or self.offset_locked:
            return
        plot_width = max(1, self.width() - 120)
        delta_ratio = (event.x() - self.drag_start_x) / plot_width
        delta_ms = int(delta_ratio * self.window_ms)
        self.signal_offset_ms = self.clamp_signal_offset(self.drag_start_offset_ms - delta_ms)
        self.update()
        if self.on_offset_changed:
            self.on_offset_changed()
        event.accept()

    def mouseReleaseEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_dragging:
            self.annotation_dragging = False
            end_ms = self.x_to_data_ms(event.x())
            start_ms, end_ms = sorted([self.annotation_drag_start_ms, end_ms])
            if end_ms - start_ms >= 100:
                if self.on_annotation_range:
                    self.on_annotation_range(start_ms, end_ms)
            self.annotation_active_range = None
            self.update()
            event.accept()
            return
        if self.drag_start_x is not None:
            self.drag_start_x = None
            self.setCursor(Qt.OpenHandCursor if self.align_mode and not self.offset_locked else Qt.ArrowCursor)

    def wheelEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if not (event.modifiers() & Qt.ControlModifier):
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return

        old_index = self.window_step_index
        if delta > 0:
            self.window_step_index = max(0, self.window_step_index - 1)
        else:
            self.window_step_index = min(len(self.window_steps_ms) - 1, self.window_step_index + 1)

        if self.window_step_index != old_index:
            self.window_ms = self.window_steps_ms[self.window_step_index]
            self.update()
        event.accept()


class EnergyAxisWidget(QWidget):
    """函数说明：执行当前函数对应的业务逻辑。"""

    def __init__(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.setMinimumHeight(78)
        self.duration_ms = 60000
        self.data_duration_ms = 60000
        self.position_ms = 0
        self.signal_offset_ms = 0
        self.energy = np.array([]) if np is not None else []
        self.energy_fs = 1.0
        self.threshold = None
        self.on_seek = None
        self.setCursor(Qt.PointingHandCursor)

    def set_energy(self, energy, fs, data_duration_ms, display_duration_ms=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if np is None:
            self.energy = []
            return
        arr = np.asarray(energy, dtype=float)
        arr = arr[np.isfinite(arr)]
        self.energy = arr
        self.energy_fs = max(1e-6, float(fs))
        self.data_duration_ms = max(1, int(data_duration_ms or len(arr) / self.energy_fs * 1000))
        if display_duration_ms:
            self.duration_ms = max(1, int(display_duration_ms))
        self.threshold = float(np.percentile(arr, 95)) if len(arr) else None
        self.update()

    def set_display_duration(self, duration_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.duration_ms = max(1, int(duration_ms or self.duration_ms))
        self.update()

    def set_signal_offset(self, offset_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.signal_offset_ms = int(offset_ms)
        self.update()

    def set_position(self, position_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.position_ms = max(0, min(int(position_ms), self.duration_ms))
        self.update()

    def paintEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))

        x1 = 90
        x2 = max(x1 + 1, self.width() - 30)
        y_top = 18
        y_bottom = self.height() - 28
        axis_y = y_bottom

        painter.setPen(QPen(QColor("#24677F"), 2))
        painter.drawText(15, y_top + 8, "ENERGY")

        painter.setPen(QPen(QColor("#B8D4DE"), 1))
        painter.drawLine(x1, axis_y, x2, axis_y)

        if np is not None and len(self.energy) > 1:
            points_x, values = self.sample_display_energy(x1, x2)
            valid = np.isfinite(values)
            if np.any(valid):
                max_value = max(float(np.nanmax(values)), 1e-8)
                points_y = axis_y - (values / max_value) * max(8, y_bottom - y_top - 6)

                if self.threshold is not None:
                    threshold_y = axis_y - (self.threshold / max_value) * max(8, y_bottom - y_top - 6)
                    painter.setPen(QPen(QColor("#E45656"), 1, Qt.DashLine))
                    painter.drawLine(x1, int(threshold_y), x2, int(threshold_y))
                    painter.setBrush(QBrush(QColor(255, 80, 80, 70)))
                    painter.setPen(Qt.NoPen)
                    for i in range(len(values) - 1):
                        if valid[i] and values[i] > self.threshold:
                            painter.drawRect(int(points_x[i]), int(points_y[i]), max(1, int(points_x[i + 1] - points_x[i])), int(axis_y - points_y[i]))

                painter.setPen(QPen(QColor("#0055FF"), 1))
                last_x = None
                last_y = None
                for x, y, ok in zip(points_x, points_y, valid):
                    if not ok:
                        last_x = None
                        last_y = None
                        continue
                    if last_x is not None:
                        painter.drawLine(int(last_x), int(last_y), int(x), int(y))
                    last_x, last_y = x, y

        duration_s = max(1, int(self.duration_ms / 1000))
        ticks = [0, duration_s // 4, duration_s // 2, duration_s * 3 // 4, duration_s]
        painter.setPen(QPen(QColor("#6E8FA0"), 1))
        for i, seconds in enumerate(ticks):
            x = int(x1 + i * ((x2 - x1) / max(1, len(ticks) - 1)))
            painter.drawLine(x, axis_y - 5, x, axis_y + 5)
            painter.drawText(x - 22, axis_y + 22, f"{seconds}s")

        cursor_ratio = min(1.0, self.position_ms / self.duration_ms)
        cursor_x = int(x1 + cursor_ratio * (x2 - x1))
        painter.setPen(QPen(QColor("#E54B4B"), 2))
        painter.drawLine(cursor_x, 6, cursor_x, self.height() - 8)

    def sample_display_energy(self, x1, x2):
        """函数说明：执行当前函数对应的业务逻辑。"""
        width = max(20, x2 - x1)
        points_x = np.linspace(x1, x2, width)
        video_ms = np.linspace(0, self.duration_ms, width)
        data_ms = video_ms + self.signal_offset_ms
        idx = (data_ms / 1000 * self.energy_fs).astype(int)
        values = np.full(width, np.nan)
        valid = (idx >= 0) & (idx < len(self.energy))
        values[valid] = self.energy[idx[valid]]
        return points_x, values

    def mousePressEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if event.button() != Qt.LeftButton:
            return
        x1 = 90
        x2 = max(x1 + 1, self.width() - 30)
        ratio = (event.x() - x1) / (x2 - x1)
        ratio = max(0.0, min(1.0, ratio))
        target_ms = int(ratio * self.duration_ms)
        self.set_position(target_ms)
        if self.on_seek:
            self.on_seek(target_ms)
        event.accept()


class ModalityFilterDialog(QDialog):
    def __init__(self, modalities, settings, parent=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__(parent)
        self.setWindowTitle("Filtering")
        self.resize(560, 260)
        self.modalities = modalities
        self.settings = {
            name: dict(settings.get(name, {}))
            for name in modalities
        }
        self.table = QTableWidget()
        self.init_ui()

    def init_ui(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        root = QVBoxLayout(self)
        title = QLabel("Channels")
        title.setObjectName("SectionTitle")

        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["", "Type", "HP (Hz)", "LP (Hz)", "Notch (Hz)"])
        self.table.setRowCount(len(self.modalities))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        for row, name in enumerate(self.modalities):
            cfg = self.settings.get(name, {})
            check = QCheckBox()
            check.setChecked(cfg.get("visible", True))
            self.table.setCellWidget(row, 0, check)

            type_item = QTableWidgetItem(name)
            type_item.setFlags(type_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, type_item)

            for col, key in [(2, "hp"), (3, "lp"), (4, "notch")]:
                edit = QLineEdit()
                edit.setPlaceholderText("None")
                value = cfg.get(key)
                if value not in [None, ""]:
                    edit.setText(str(value))
                self.table.setCellWidget(row, col, edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = make_white_button("取消")
        apply_btn = make_blue_button("确定")
        cancel_btn.clicked.connect(self.reject)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)

        root.addWidget(title)
        root.addWidget(self.table)
        root.addLayout(btn_row)

    def result_settings(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        result = {}
        for row, name in enumerate(self.modalities):
            check = self.table.cellWidget(row, 0)
            cfg = {"visible": check.isChecked() if check is not None else True}
            for col, key in [(2, "hp"), (3, "lp"), (4, "notch")]:
                edit = self.table.cellWidget(row, col)
                cfg[key] = edit.text().strip() if edit is not None else ""
            result[name] = cfg
        return result


class TimelineWidget(QWidget):
    """函数说明：执行当前函数对应的业务逻辑。"""

    def __init__(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.setMinimumHeight(64)
        self.duration_ms = 60000
        self.position_ms = 0
        self.cursor_duration_ms = 60000
        self.video_start_text = ""
        self.video_end_text = ""
        self.eeg_marker_start_text = ""
        self.eeg_marker_end_text = ""

    def set_project_times(self, project):
        """设置整体时间轴使用的视频和脑电标记时间。"""
        project = project or {}
        self.video_start_text = str(project.get("video_start") or "").strip()
        self.video_end_text = str(project.get("video_end") or "").strip()
        self.eeg_marker_start_text = str(project.get("eeg_marker_start") or "").strip()
        self.eeg_marker_end_text = str(project.get("eeg_marker_end") or "").strip()
        self.update()

    def set_duration(self, duration_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.duration_ms = max(1, int(duration_ms or 60000))
        self.cursor_duration_ms = self.duration_ms
        self.update()

    def set_position(self, position_ms, cursor_duration_ms=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.position_ms = max(0, int(position_ms))
        self.cursor_duration_ms = max(1, int(cursor_duration_ms or self.duration_ms))
        self.update()

    def paintEvent(self, event):
        """函数说明：执行当前函数对应的业务逻辑。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.fillRect(self.rect(), QColor("#FFFFFF"))

        w = self.width()
        h = self.height()

        x1 = 25
        x2 = w - 25
        y = h // 2

        painter.setPen(QPen(QColor("#6E8FA0"), 2))
        painter.drawLine(x1, y, x2, y)

        duration_s = max(1, int(self.duration_ms / 1000))
        use_project_times = bool(self.video_start_text and self.video_end_text)
        video_start_dt, video_has_date = parse_clock_time_text(self.video_start_text)
        video_end_dt, end_has_date = parse_clock_time_text(self.video_end_text)
        if video_start_dt is not None and video_end_dt is not None and not video_has_date and not end_has_date and video_end_dt < video_start_dt:
            video_end_dt = video_end_dt + timedelta(days=1)
        use_project_times = use_project_times and video_start_dt is not None and video_end_dt is not None and video_end_dt > video_start_dt
        project_duration_ms = 1
        if use_project_times:
            project_duration_ms = max(1, int((video_end_dt - video_start_dt).total_seconds() * 1000))

        ticks = [0, duration_s // 4, duration_s // 2, duration_s * 3 // 4, duration_s]
        for i, seconds in enumerate(ticks):
            ratio = i / max(1, len(ticks) - 1)
            x = x1 + ratio * (x2 - x1)
            painter.setPen(QPen(QColor("#6E8FA0"), 2))
            painter.drawLine(int(x), y - 6, int(x), y + 6)
            painter.setPen(QColor("#456"))
            if use_project_times:
                tick_time = video_start_dt + timedelta(milliseconds=int(project_duration_ms * ratio))
                label = format_clock_time_value(tick_time, video_has_date or end_has_date)
                painter.drawText(int(x) - 34, y + 24, label)
            else:
                painter.drawText(int(x) - 18, y + 24, f"{seconds}s")

        marker_start_ms = ms_between_clock_times(self.video_start_text, self.eeg_marker_start_text)
        marker_end_ms = ms_between_clock_times(self.video_start_text, self.eeg_marker_end_text)
        if use_project_times and marker_start_ms is not None and marker_end_ms is not None:
            marker_start_ms, marker_end_ms = sorted([marker_start_ms, marker_end_ms])
            marker_start_ms = max(0, min(marker_start_ms, project_duration_ms))
            marker_end_ms = max(0, min(marker_end_ms, project_duration_ms))
            if marker_end_ms > marker_start_ms:
                marker_x1 = int(x1 + marker_start_ms / project_duration_ms * (x2 - x1))
                marker_x2 = int(x1 + marker_end_ms / project_duration_ms * (x2 - x1))
                painter.setBrush(QBrush(QColor("#3A3A3A")))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(marker_x1, y - 12, max(4, marker_x2 - marker_x1), 24, 4, 4)
                painter.setPen(QPen(QColor("#222"), 1))
                painter.drawText(marker_x1, y - 16, "EEG")

        cursor_ratio = min(1.0, max(0.0, self.position_ms / max(1, self.cursor_duration_ms)))
        cursor_x = int(x1 + cursor_ratio * (x2 - x1))
        painter.setPen(QPen(QColor("#E54B4B"), 2))
        painter.drawLine(cursor_x, y - 24, cursor_x, y + 24)


# =========================
# 1 登录界面
# =========================
