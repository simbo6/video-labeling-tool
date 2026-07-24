# -*- coding: utf-8 -*-
import json
import time

from .shared import *
from .widgets import SignalWaveWidget, EnergyAxisWidget, ModalityFilterDialog, TimelineWidget
from .offset_estimator import (
    OffsetEstimationCancelled,
    estimate_video_watch_offset,
    read_video_first_frame,
)
from .hospital_time_extractor import extract_hospital_times


class OffsetEstimateWorker(QObject):
    """在后台线程估计视频和腕表数据偏移量。"""

    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    cancelled = pyqtSignal()

    def __init__(self, video_path, watch_path, roi, eeg_start_sec, eeg_end_sec, acc_fs, gyro_fs, wrist_start_relative_sec=None):
        super().__init__()
        self.video_path = video_path
        self.watch_path = watch_path
        self.roi = tuple(roi)
        self.eeg_start_sec = float(eeg_start_sec)
        self.eeg_end_sec = float(eeg_end_sec)
        self.acc_fs = float(acc_fs)
        self.gyro_fs = float(gyro_fs)
        self.wrist_start_relative_sec = wrist_start_relative_sec
        self.cancel_requested = False

    def request_cancel(self):
        """请求停止当前自动偏移估计。"""
        self.cancel_requested = True

    def run(self):
        """执行自动偏移估计。"""
        try:
            def report_progress(percent, message):
                self.progress.emit(int(percent), str(message or ""))

            result = estimate_video_watch_offset(
                self.video_path,
                self.watch_path,
                roi=self.roi,
                eeg_start_sec=self.eeg_start_sec,
                eeg_end_sec=self.eeg_end_sec,
                imu_mode="acc_gyr",
                target_fs=5.0,
                acc_fs=self.acc_fs,
                gyro_fs=self.gyro_fs,
                wrist_start_relative_sec=self.wrist_start_relative_sec,
                progress_callback=report_progress,
                cancel_callback=lambda: self.cancel_requested,
            )
            self.finished.emit(result)
        except OffsetEstimationCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class VideoDecodeWorker(QObject):
    """在后台线程顺序解码视频，避免阻塞 Qt 主线程。"""

    frame_ready = pyqtSignal(object, int)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, video_path, start_frame, fps, frame_count, target_size):
        super().__init__()
        self.video_path = str(video_path)
        self.start_frame = max(0, int(start_frame))
        self.fps = max(1.0, float(fps))
        self.frame_count = max(0, int(frame_count))
        self.target_width = max(1, int(target_size[0]))
        self.target_height = max(1, int(target_size[1]))
        self.stop_requested = False

    def request_stop(self):
        """请求后台解码循环尽快退出。"""
        self.stop_requested = True

    def frame_to_image(self, frame):
        """把视频帧缩放并转换为可跨线程传递的 QImage。"""
        frame_height, frame_width = frame.shape[:2]
        scale = min(
            self.target_width / max(1, frame_width),
            self.target_height / max(1, frame_height),
        )
        if abs(scale - 1.0) > 0.01:
            resized_width = max(1, int(round(frame_width * scale)))
            resized_height = max(1, int(round(frame_height * scale)))
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            frame = cv2.resize(frame, (resized_width, resized_height), interpolation=interpolation)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        bytes_per_line = channels * width
        return QImage(rgb.data, width, height, bytes_per_line, QImage.Format_RGB888).copy()

    def run(self):
        """顺序读取并按视频帧率向界面发送图像。"""
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            self.failed.emit(f"无法打开视频文件：{self.video_path}")
            self.finished.emit()
            return
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, self.start_frame)
            frame_idx = self.start_frame - 1
            frame_interval = 1.0 / self.fps
            next_deadline = time.monotonic()
            while not self.stop_requested and frame_idx + 1 < self.frame_count:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_idx += 1
                self.frame_ready.emit(self.frame_to_image(frame), frame_idx)

                next_deadline += frame_interval
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    QThread.msleep(max(1, int(delay * 1000)))
                elif delay < -frame_interval:
                    skip_count = min(5, int((-delay) / frame_interval))
                    for _ in range(skip_count):
                        if self.stop_requested or frame_idx + 1 >= self.frame_count or not cap.grab():
                            break
                        frame_idx += 1
                    next_deadline = time.monotonic()
        except Exception as exc:
            if not self.stop_requested:
                self.failed.emit(str(exc))
        finally:
            cap.release()
            self.finished.emit()


class HospitalTimeWorker(QObject):
    """在后台线程读取医院导出目录中的时间信息。"""

    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, folder_path, hospital_type):
        super().__init__()
        self.folder_path = folder_path
        self.hospital_type = hospital_type

    def run(self):
        """执行医院时间信息提取。"""
        try:
            self.finished.emit(extract_hospital_times(self.folder_path, self.hospital_type))
        except Exception as exc:
            self.failed.emit(str(exc))


class RoiSelectionLabel(QLabel):
    """用于在第一帧上手动框选 ROI。"""

    selectionChanged = pyqtSignal()

    def __init__(self, pixmap, original_size, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self.original_width = int(original_size[0])
        self.original_height = int(original_size[1])
        self.drag_start = None
        self.drag_end = None
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        point = self._clamp_point(event.pos())
        self.drag_start = point
        self.drag_end = point
        self.update()
        self.selectionChanged.emit()

    def mouseMoveEvent(self, event):
        if self.drag_start is None:
            return
        self.drag_end = self._clamp_point(event.pos())
        self.update()
        self.selectionChanged.emit()

    def mouseReleaseEvent(self, event):
        if self.drag_start is None:
            return
        self.drag_end = self._clamp_point(event.pos())
        self.update()
        self.selectionChanged.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.selected_rect()
        if rect is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#FF3B30"), 2))
        painter.setBrush(QBrush(QColor(255, 59, 48, 40)))
        painter.drawRect(rect)

    def _clamp_point(self, point):
        return QPoint(
            max(0, min(int(point.x()), self.width() - 1)),
            max(0, min(int(point.y()), self.height() - 1)),
        )

    def selected_rect(self):
        if self.drag_start is None or self.drag_end is None:
            return None
        rect = QRect(self.drag_start, self.drag_end).normalized()
        if rect.width() < 4 or rect.height() < 4:
            return None
        return rect

    def selected_roi(self):
        rect = self.selected_rect()
        if rect is None:
            return None
        scale_x = self.original_width / max(1, self.width())
        scale_y = self.original_height / max(1, self.height())
        x = max(0, min(int(round(rect.x() * scale_x)), self.original_width - 1))
        y = max(0, min(int(round(rect.y() * scale_y)), self.original_height - 1))
        right = max(x + 1, min(int(round((rect.x() + rect.width()) * scale_x)), self.original_width))
        bottom = max(y + 1, min(int(round((rect.y() + rect.height()) * scale_y)), self.original_height))
        return x, y, max(1, right - x), max(1, bottom - y)


class RoiSelectionDialog(QDialog):
    """弹出第一帧预览，让用户手动框选 ROI。"""

    def __init__(self, frame_bgr, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择 ROI")
        self.selected_roi_value = None

        height, width = frame_bgr.shape[:2]
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        bytes_per_line = frame_rgb.shape[1] * frame_rgb.shape[2]
        image = QImage(frame_rgb.data, frame_rgb.shape[1], frame_rgb.shape[0], bytes_per_line, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)

        max_width = 1100
        max_height = 700
        scale = min(max_width / max(1, width), max_height / max(1, height), 1.0)
        display_width = max(1, int(round(width * scale)))
        display_height = max(1, int(round(height * scale)))
        display_pixmap = pixmap.scaled(display_width, display_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        tip = QLabel("请在第一帧上框出病床/病人区域，然后点击“确认 ROI”。")
        tip.setWordWrap(True)
        tip.setObjectName("SubTitleLabel")

        self.roi_label = RoiSelectionLabel(display_pixmap, (width, height), self)
        self.status_label = QLabel("尚未选择 ROI")
        self.status_label.setObjectName("SubTitleLabel")

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = make_white_button("取消")
        self.confirm_btn = make_blue_button("确认 ROI")
        self.confirm_btn.setEnabled(False)
        cancel_btn.clicked.connect(self.reject)
        self.confirm_btn.clicked.connect(self.accept_selection)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self.confirm_btn)

        self.roi_label.selectionChanged.connect(self.refresh_status)

        layout.addWidget(tip)
        layout.addWidget(self.roi_label, alignment=Qt.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)

    def refresh_status(self):
        roi = self.roi_label.selected_roi()
        if roi is None:
            self.status_label.setText("尚未选择 ROI")
            self.confirm_btn.setEnabled(False)
            return
        x, y, w, h = roi
        self.status_label.setText(f"当前 ROI：x={x}, y={y}, w={w}, h={h}")
        self.confirm_btn.setEnabled(True)

    def accept_selection(self):
        roi = self.roi_label.selected_roi()
        if roi is None:
            QMessageBox.information(self, "提示", "请先框选一个有效 ROI")
            return
        self.selected_roi_value = roi
        self.accept()

    def selected_roi(self):
        return self.selected_roi_value

class LoginPage(QWidget):
    def __init__(self, switch_to_main, db=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.switch_to_main = switch_to_main
        self.db = db
        self.user_edit = None
        self.pwd_edit = None
        self.init_ui()

    def init_ui(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)
        root.setAlignment(Qt.AlignCenter)

        title = QLabel("癫痫腕表数据标注平台")
        title.setObjectName("TitleLabel")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("Epilepsy Watch Data Labeling Platform")
        subtitle.setObjectName("SubTitleLabel")
        subtitle.setAlignment(Qt.AlignCenter)

        card = make_card()
        card.setMinimumSize(320, 300)
        card.setMaximumSize(430, 330)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(32, 28, 32, 28)
        card_layout.setSpacing(14)

        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("请输入用户名")
        self.user_edit.setText("admin")

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("请输入密码")
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setText("admin")

        register_btn = make_white_button("注册")
        login_btn = make_blue_button("登录")

        card_layout.addWidget(QLabel("用户名"))
        card_layout.addWidget(self.user_edit)
        card_layout.addWidget(QLabel("密码"))
        card_layout.addWidget(self.pwd_edit)
        card_layout.addWidget(register_btn)
        card_layout.addWidget(login_btn)

        login_btn.clicked.connect(self.login)
        register_btn.clicked.connect(self.register_user)
        self.pwd_edit.returnPressed.connect(self.login)

        root.addWidget(title)
        root.addWidget(subtitle)
        root.addWidget(card, alignment=Qt.AlignCenter)

    def login(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        username = self.user_edit.text().strip() if self.user_edit is not None else ""
        password = self.pwd_edit.text() if self.pwd_edit is not None else ""
        if self.db is None:
            self.switch_to_main()
            return
        if self.db.verify_user(username, password):
            self.switch_to_main()
            return
        QMessageBox.warning(self, "登录失败", "用户名或密码错误")

    def register_user(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        username = self.user_edit.text().strip() if self.user_edit is not None else ""
        password = self.pwd_edit.text() if self.pwd_edit is not None else ""
        if self.db is None:
            QMessageBox.warning(self, "注册失败", "数据库未初始化")
            return
        ok, message = self.db.create_user(username, password)
        if ok:
            QMessageBox.information(self, "注册成功", message)
        else:
            QMessageBox.warning(self, "注册失败", message)

class ProjectListPage(QWidget):
    def __init__(
        self,
        open_import_page,
        open_record_page,
        open_annotation_page,
        db=None,
        open_project=None,
        open_import_project=None,
        open_record_project=None,
    ):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.open_import_page = open_import_page
        self.open_record_page = open_record_page
        self.open_annotation_page = open_annotation_page
        self.db = db
        self.open_project_callback = open_project
        self.open_import_project_callback = open_import_project
        self.open_record_project_callback = open_record_project
        self.project_list = None
        self.project_count_label = None
        self.metric_labels = {}
        self.init_ui()

    def init_ui(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QFrame()
        side.setObjectName("SideBar")
        side.setFixedWidth(180)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(16, 22, 16, 18)
        side_layout.setSpacing(12)

        logo = QLabel("数据标注平台")
        logo.setObjectName("LogoLabel")
        logo.setAlignment(Qt.AlignCenter)
        import_nav_btn = make_white_button("资料导入")
        anno_btn = make_white_button("标注工作台")
        record_btn = make_white_button("标注记录")
        side_layout.addWidget(logo)
        side_layout.addSpacing(20)
        side_layout.addWidget(import_nav_btn)
        side_layout.addWidget(anno_btn)
        side_layout.addWidget(record_btn)
        side_layout.addStretch()

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(28, 24, 28, 24)
        main_layout.setSpacing(16)

        header = QHBoxLayout()
        title_group = QVBoxLayout()
        title = QLabel("项目管理")
        title.setObjectName("TitleLabel")
        subtitle = QLabel("按项目管理患者资料、视频、腕表数据、对齐偏移和标注记录")
        subtitle.setObjectName("SubTitleLabel")
        title_group.addWidget(title)
        title_group.addWidget(subtitle)
        self.project_count_label = QLabel("0 个项目")
        self.project_count_label.setObjectName("SubTitleLabel")
        self.project_count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addLayout(title_group, 1)
        header.addWidget(self.project_count_label)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        for key, title_text in [
            ("projects", "项目总数"),
            ("videos", "已导入视频"),
            ("watch", "已导入腕表"),
            ("annotations", "标注记录"),
        ]:
            card, value_label = self.make_metric_card(title_text, "0")
            self.metric_labels[key] = value_label
            metrics.addWidget(card)

        toolbar_card = QFrame()
        toolbar_card.setObjectName("ToolbarCard")
        top = QHBoxLayout(toolbar_card)
        top.setContentsMargins(14, 12, 14, 12)
        top.setSpacing(10)
        add_btn = make_blue_button("新建项目")
        import_btn = make_white_button("编辑/导入资料")
        open_btn = make_blue_button("开始标注")
        record_btn_top = make_white_button("查看记录")
        delete_btn = make_danger_button("删除项目")
        top.addWidget(add_btn)
        top.addWidget(import_btn)
        top.addWidget(open_btn)
        top.addWidget(record_btn_top)
        top.addStretch()
        top.addWidget(delete_btn)

        self.project_list = QTableWidget()
        self.project_list.setColumnCount(8)
        self.project_list.setHorizontalHeaderLabels([
            "ID", "项目名称", "患者", "视频文件", "腕表数据", "偏移", "标注数", "更新时间"
        ])
        self.project_list.verticalHeader().setVisible(False)
        self.project_list.setAlternatingRowColors(True)
        self.project_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.project_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.project_list.setEditTriggers(QTableWidget.NoEditTriggers)
        self.project_list.setSortingEnabled(False)
        self.project_list.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.project_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.project_list.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.project_list.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.project_list.setMinimumHeight(360)
        self.project_list.cellDoubleClicked.connect(lambda *_: self.open_selected_project())

        add_btn.clicked.connect(self.create_project)
        open_btn.clicked.connect(self.open_selected_project)
        delete_btn.clicked.connect(self.delete_selected_project)
        import_btn.clicked.connect(self.import_selected_project)
        import_nav_btn.clicked.connect(self.import_selected_project)
        record_btn_top.clicked.connect(self.open_selected_record)
        record_btn.clicked.connect(self.open_selected_record)
        anno_btn.clicked.connect(self.open_selected_project)

        main_layout.addLayout(header)
        main_layout.addLayout(metrics)
        main_layout.addWidget(toolbar_card)
        main_layout.addWidget(self.project_list)
        root.addWidget(side)
        root.addWidget(main)
        self.refresh_projects()

    def make_metric_card(self, title, value):
        """创建首页统计卡片。"""
        card = QFrame()
        card.setObjectName("MetricCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        value_label = QLabel(value)
        value_label.setObjectName("MetricValue")
        title_label = QLabel(title)
        title_label.setObjectName("MetricLabel")
        layout.addWidget(value_label)
        layout.addWidget(title_label)
        return card, value_label

    def refresh_projects(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.project_list is None:
            return
        projects = self.db.list_projects() if self.db is not None else []
        self.project_list.setRowCount(len(projects))
        total_annotations = 0
        video_count = 0
        watch_count = 0
        if self.project_count_label is not None:
            self.project_count_label.setText(f"{len(projects)} 个项目")
        for row, project in enumerate(projects):
            patient = project.get("patient_name") or "未填写患者"
            offset = int(project.get("signal_offset_ms") or 0) / 1000
            has_video = bool(project.get("video_path"))
            has_watch = bool(project.get("watch_path"))
            video_count += 1 if has_video else 0
            watch_count += 1 if has_watch else 0
            video_name = os.path.basename(project.get("video_path") or "") or "未导入"
            watch_name = os.path.basename(project.get("watch_path") or "") or "未导入"
            annotation_count = len(self.db.list_annotations(project["id"])) if self.db is not None else 0
            total_annotations += annotation_count
            values = [
                str(project["id"]),
                project.get("name") or "未命名项目",
                patient,
                video_name,
                watch_name,
                f"{offset:.1f}s",
                str(annotation_count),
                project.get("updated_at") or "",
            ]
            for col, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setData(Qt.UserRole, project["id"])
                self.project_list.setItem(row, col, table_item)
            self.project_list.setRowHeight(row, 42)
        if projects and self.project_list.currentRow() < 0:
            self.project_list.selectRow(0)
        if self.metric_labels:
            self.metric_labels["projects"].setText(str(len(projects)))
            self.metric_labels["videos"].setText(str(video_count))
            self.metric_labels["watch"].setText(str(watch_count))
            self.metric_labels["annotations"].setText(str(total_annotations))

    def current_project_id(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.project_list is None:
            return None
        row = self.project_list.currentRow()
        if row < 0:
            return None
        item = self.project_list.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def create_project(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.db is None:
            return
        name, ok = QInputDialog.getText(self, "新建项目", "项目名称：")
        if not ok:
            return
        project = self.db.create_project(name.strip() or None)
        self.refresh_projects()
        if self.open_import_project_callback is not None:
            self.open_import_project_callback(project["id"])
        elif self.open_project_callback is not None:
            self.open_project_callback(project["id"])

    def open_selected_project(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        project_id = self.current_project_id()
        if project_id is None:
            QMessageBox.information(self, "提示", "请先选择项目")
            return
        if self.open_project_callback is not None:
            self.open_project_callback(project_id)

    def import_selected_project(self):
        """打开所选项目的数据导入页，确保文件路径写入该项目。"""
        project_id = self.current_project_id()
        if project_id is None:
            QMessageBox.information(self, "提示", "请先选择项目")
            return
        if self.open_import_project_callback is not None:
            self.open_import_project_callback(project_id)
        else:
            self.open_import_page()

    def open_selected_record(self):
        """打开所选项目的标注记录页。"""
        project_id = self.current_project_id()
        if project_id is None:
            QMessageBox.information(self, "提示", "请先选择项目")
            return
        if self.open_record_project_callback is not None:
            self.open_record_project_callback(project_id)
        else:
            self.open_record_page()

    def delete_selected_project(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        project_id = self.current_project_id()
        if project_id is None or self.db is None:
            QMessageBox.information(self, "提示", "请先选择项目")
            return
        reply = QMessageBox.question(self, "删除项目", "确认删除该项目及其标注记录？")
        if reply != QMessageBox.Yes:
            return
        self.db.delete_project(project_id)
        self.refresh_projects()

class ImportPage(QWidget):
    def __init__(self, back, open_annotation, app_state, db=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.back = back
        self.open_annotation = open_annotation
        self.app_state = app_state
        self.db = db
        self.video_path_edit = None
        self.watch_path_edit = None
        self.hospital_path_edit = None
        self.hospital_type_box = None
        self.hospital_summary = None
        self.hospital_thread = None
        self.hospital_worker = None
        self.video_summary = None
        self.watch_summary = None
        self.raw_tabs = None
        self.project_fields = {}
        self.sample_rate_fields = {}
        self.init_ui()

    def init_ui(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        outer.addWidget(scroll)

        top_card = make_card()
        top_layout = QGridLayout(top_card)
        top_layout.setContentsMargins(25, 18, 25, 18)
        top_layout.setHorizontalSpacing(15)
        top_layout.setVerticalSpacing(12)

        labels = [
            ("patient_name", "患者姓名"), ("patient_age", "年龄"),
            ("patient_gender", "性别"), ("hospital", "医院"),
            ("dominant_hand", "惯用手"), ("birth_date", "出生日期"),
            ("seizure_date", "发作日期"), ("wearing_hand", "佩戴手"),
        ]
        for i, (key, text) in enumerate(labels):
            lab = QLabel(text)
            edit = QLineEdit()
            edit.setMinimumHeight(34)
            edit.setMinimumWidth(120)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.project_fields[key] = edit
            top_layout.addWidget(lab, i // 4, (i % 4) * 2)
            top_layout.addWidget(edit, i // 4, (i % 4) * 2 + 1)

        video_card = make_card()
        video_layout = QGridLayout(video_card)
        video_layout.setContentsMargins(25, 22, 25, 22)
        video_layout.setSpacing(14)

        video_btn = make_blue_button("导入视频文件")
        video_path = QLineEdit()
        video_path.setPlaceholderText("请选择视频文件路径")
        video_path.setMinimumHeight(34)
        video_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.video_path_edit = video_path

        video_layout.addWidget(video_btn, 0, 0)
        video_layout.addWidget(video_path, 0, 1, 1, 5)

        video_fields = [
            ("video_start", "视频开始时间"),
            ("video_end", "视频结束时间"),
            ("video_note", "视频备注"),
            ("eeg_marker_start", "脑电标记开始"),
            ("eeg_marker_end", "脑电标记结束"),
        ]
        for i, (key, name) in enumerate(video_fields):
            edit = QLineEdit()
            edit.setMinimumHeight(34)
            edit.setMinimumWidth(120)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.project_fields[key] = edit
            video_layout.addWidget(QLabel(name), 1 + i // 3, (i % 3) * 2)
            video_layout.addWidget(edit, 1 + i // 3, (i % 3) * 2 + 1)

        hospital_card = make_card()
        hospital_layout = QGridLayout(hospital_card)
        hospital_layout.setContentsMargins(25, 22, 25, 22)
        hospital_layout.setSpacing(14)

        self.hospital_type_box = QComboBox()
        self.hospital_type_box.addItems(["自动识别", "天坛医院", "宣武医院"])
        self.hospital_type_box.setMinimumHeight(34)
        self.hospital_type_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.hospital_path_edit = QLineEdit()
        self.hospital_path_edit.setPlaceholderText("请选择天坛医院或宣武医院导出的数据目录")
        self.hospital_path_edit.setMinimumHeight(34)
        self.hospital_path_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        hospital_dir_btn = make_white_button("选择目录")
        hospital_read_btn = make_blue_button("读取时间信息")
        hospital_dir_btn.clicked.connect(self.choose_hospital_folder)
        hospital_read_btn.clicked.connect(self.start_hospital_time_extract)

        self.hospital_summary = QTextEdit()
        self.hospital_summary.setReadOnly(True)
        self.hospital_summary.setFixedHeight(86)
        self.hospital_summary.setPlaceholderText("医院数据目录读取结果将在这里显示")

        hospital_layout.addWidget(QLabel("医院类型"), 0, 0)
        hospital_layout.addWidget(self.hospital_type_box, 0, 1)
        hospital_layout.addWidget(QLabel("医院数据目录"), 0, 2)
        hospital_layout.addWidget(self.hospital_path_edit, 0, 3, 1, 3)
        hospital_layout.addWidget(hospital_dir_btn, 0, 6)
        hospital_layout.addWidget(hospital_read_btn, 0, 7)
        hospital_layout.addWidget(self.hospital_summary, 1, 0, 1, 8)

        watch_card = make_card()
        watch_layout = QGridLayout(watch_card)
        watch_layout.setContentsMargins(25, 22, 25, 22)
        watch_layout.setSpacing(14)

        watch_btn = make_blue_button("导入腕表数据")
        watch_path = QLineEdit()
        watch_path.setPlaceholderText("请选择 MAT/CSV/XLSX 腕表数据文件")
        watch_path.setMinimumHeight(34)
        watch_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.watch_path_edit = watch_path

        watch_layout.addWidget(watch_btn, 0, 0)
        watch_layout.addWidget(watch_path, 0, 1, 1, 5)

        fields2 = [
            ("signal_offset_ms", "信号偏移(ms)"), ("watch_start", "腕表开始时间"),
            ("watch_end", "腕表结束时间"), ("acc_fs", "加速度采样率"),
            ("gyro_fs", "角速度采样率"), ("emg_fs", "肌电采样率"),
            ("ppg_fs", "PPG采样率"),
        ]
        for i, (key, name) in enumerate(fields2):
            edit = QLineEdit()
            edit.setMinimumHeight(34)
            edit.setMinimumWidth(120)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if key in ["acc_fs", "gyro_fs", "emg_fs", "ppg_fs"]:
                self.sample_rate_fields[key] = edit
            else:
                self.project_fields[key] = edit
            watch_layout.addWidget(QLabel(name), 1 + i // 3, (i % 3) * 2)
            watch_layout.addWidget(edit, 1 + i // 3, (i % 3) * 2 + 1)

        raw_card = make_card()
        raw_layout = QVBoxLayout(raw_card)
        raw_layout.setContentsMargins(25, 22, 25, 22)
        raw_layout.setSpacing(12)

        raw_title = QLabel("原始数据预览")
        raw_title.setObjectName("SectionTitle")

        summary_row = QHBoxLayout()
        self.video_summary = QTextEdit()
        self.video_summary.setReadOnly(True)
        self.video_summary.setFixedHeight(90)
        self.video_summary.setPlaceholderText("视频文件信息将在这里显示")

        self.watch_summary = QTextEdit()
        self.watch_summary.setReadOnly(True)
        self.watch_summary.setFixedHeight(90)
        self.watch_summary.setPlaceholderText("腕表数据文件信息将在这里显示")

        summary_row.addWidget(self.video_summary)
        summary_row.addWidget(self.watch_summary)

        self.raw_tabs = QTabWidget()
        self.raw_tabs.setMinimumHeight(190)

        raw_layout.addWidget(raw_title)
        raw_layout.addLayout(summary_row)
        raw_layout.addWidget(self.raw_tabs)

        bottom = QHBoxLayout()
        back_btn = make_white_button("返回")
        start_btn = make_blue_button("开始标注")
        back_btn.clicked.connect(self.back)
        start_btn.clicked.connect(self.save_and_open_annotation)

        bottom.addWidget(back_btn)
        bottom.addStretch()
        bottom.addWidget(start_btn)

        root.addWidget(top_card)
        root.addWidget(video_card)
        root.addWidget(hospital_card)
        root.addWidget(watch_card)
        root.addWidget(raw_card)
        root.addLayout(bottom)
        scroll.setWidget(content)

        video_btn.clicked.connect(self.choose_video_file)
        watch_btn.clicked.connect(self.choose_watch_file)
        self.restore_import_paths()

    def restore_import_paths(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        project = self.app_state.get("project") or {}
        for key, edit in self.project_fields.items():
            if key == "signal_offset_ms":
                edit.setText(str(project.get(key) or self.app_state.get("signal_offset_ms", 0)))
            else:
                edit.setText(str(project.get(key) or ""))
        for key, edit in self.sample_rate_fields.items():
            default_value = {"acc_fs": 100, "gyro_fs": 100, "emg_fs": 200, "ppg_fs": 25}.get(key, "")
            edit.setText(str(project.get(key) or default_value))
        video_path = self.app_state.get("video_path", "")
        watch_path = self.app_state.get("watch_path", "")
        hospital_path = project.get("hospital_data_path") or ""
        hospital_type = project.get("hospital_type") or "自动识别"
        if self.video_path_edit is not None:
            self.video_path_edit.setText(video_path)
        if self.watch_path_edit is not None:
            self.watch_path_edit.setText(watch_path)
        if self.hospital_path_edit is not None:
            self.hospital_path_edit.setText(hospital_path)
        if self.hospital_type_box is not None:
            index = self.hospital_type_box.findText(hospital_type)
            self.hospital_type_box.setCurrentIndex(index if index >= 0 else 0)
        if self.video_summary is not None:
            self.video_summary.clear()
        if self.watch_summary is not None:
            self.watch_summary.clear()
        if self.hospital_summary is not None:
            if hospital_path:
                self.hospital_summary.setText(
                    "\n".join([
                        f"医院类型：{hospital_type}",
                        f"数据目录：{hospital_path}",
                        f"发作日期：{project.get('seizure_date') or '未填写'}",
                        f"视频开始：{project.get('video_start') or '未填写'}",
                        f"视频结束：{project.get('video_end') or '未填写'}",
                        f"发作开始：{project.get('eeg_marker_start') or '未填写'}",
                        f"发作结束：{project.get('eeg_marker_end') or '未填写'}",
                    ])
                )
            else:
                self.hospital_summary.clear()
        if self.raw_tabs is not None:
            self.raw_tabs.clear()
        if video_path and os.path.exists(video_path):
            self.load_video_raw(video_path)
        if watch_path and os.path.exists(watch_path):
            self.load_watch_raw(watch_path)

    def collect_project_fields(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        values = {key: edit.text().strip() for key, edit in self.project_fields.items()}
        for key, edit in self.sample_rate_fields.items():
            try:
                values[key] = float(edit.text().strip())
            except ValueError:
                values[key] = {"acc_fs": 100, "gyro_fs": 100, "emg_fs": 200, "ppg_fs": 25}.get(key, 0)
        try:
            values["signal_offset_ms"] = int(float(values.get("signal_offset_ms") or 0))
        except ValueError:
            values["signal_offset_ms"] = 0
        values["video_path"] = self.video_path_edit.text().strip() if self.video_path_edit is not None else ""
        values["watch_path"] = self.watch_path_edit.text().strip() if self.watch_path_edit is not None else ""
        values["hospital_data_path"] = self.hospital_path_edit.text().strip() if self.hospital_path_edit is not None else ""
        values["hospital_type"] = self.hospital_type_box.currentText() if self.hospital_type_box is not None else ""
        if values.get("patient_name"):
            values["name"] = values["patient_name"]
        return values

    def save_current_project(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.db is None:
            return
        project_id = self.app_state.get("project_id")
        if not project_id:
            project_id = self.db.get_current_project()["id"]
        project = self.db.update_project(project_id, **self.collect_project_fields())
        self.app_state["project_id"] = project["id"]
        self.app_state["project"] = project
        self.app_state["video_path"] = project.get("video_path") or ""
        self.app_state["watch_path"] = project.get("watch_path") or ""
        self.app_state["signal_offset_ms"] = int(project.get("signal_offset_ms") or 0)

    def save_and_open_annotation(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.save_current_project()
        self.open_annotation()

    def choose_video_file(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择视频文件",
            "",
            "Video Files (*.mp4 *.avi *.mov *.mkv *.MP4);;All Files (*)"
        )
        if file_path:
            self.video_path_edit.setText(file_path)
            self.app_state["video_path"] = file_path
            if self.db is not None:
                self.db.save_media_file("video", file_path)
                self.save_current_project()
            self.load_video_raw(file_path)

    def choose_watch_file(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择腕表数据文件",
            "",
            "MAT Files (*.mat);;CSV Files (*.csv);;Excel Files (*.xlsx *.xls);;All Files (*)"
        )
        if file_path:
            self.watch_path_edit.setText(file_path)
            self.app_state["watch_path"] = file_path
            if self.db is not None:
                self.db.save_media_file("watch", file_path)
                self.save_current_project()
            self.load_watch_raw(file_path)

    def choose_hospital_folder(self):
        """选择医院视频脑电导出目录。"""
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "选择医院数据目录",
            self.hospital_path_edit.text().strip() if self.hospital_path_edit is not None else "",
        )
        if not folder_path:
            return
        self.hospital_path_edit.setText(folder_path)
        if self.db is not None:
            self.db.save_media_file("hospital", folder_path)
            self.save_current_project()

    def start_hospital_time_extract(self):
        """启动医院目录时间信息读取。"""
        folder_path = self.hospital_path_edit.text().strip() if self.hospital_path_edit is not None else ""
        if not folder_path:
            QMessageBox.information(self, "提示", "请先选择医院数据目录")
            return
        if self.hospital_thread is not None:
            QMessageBox.information(self, "提示", "正在读取医院时间信息，请稍候")
            return

        hospital_type = self.hospital_type_box.currentText() if self.hospital_type_box is not None else "自动识别"
        if self.hospital_summary is not None:
            self.hospital_summary.setText("正在读取医院数据目录，请稍候...")

        self.hospital_thread = QThread(self)
        self.hospital_worker = HospitalTimeWorker(folder_path, hospital_type)
        self.hospital_worker.moveToThread(self.hospital_thread)
        self.hospital_thread.started.connect(self.hospital_worker.run)
        self.hospital_worker.finished.connect(self.apply_hospital_time_result)
        self.hospital_worker.failed.connect(self.handle_hospital_time_error)
        self.hospital_worker.finished.connect(self.hospital_thread.quit)
        self.hospital_worker.failed.connect(self.hospital_thread.quit)
        self.hospital_thread.finished.connect(self.hospital_worker.deleteLater)
        self.hospital_thread.finished.connect(self.hospital_thread.deleteLater)
        self.hospital_thread.finished.connect(self.clear_hospital_worker)
        self.hospital_thread.start()

    def apply_hospital_time_result(self, result):
        """把医院目录读取结果写入当前项目字段。"""
        for key in ["seizure_date", "video_start", "video_end", "eeg_marker_start", "eeg_marker_end"]:
            edit = self.project_fields.get(key)
            if edit is not None:
                edit.setText(str(result.get(key) or ""))

        hospital_type = result.get("hospital_type") or "自动识别"
        if self.hospital_type_box is not None:
            index = self.hospital_type_box.findText(hospital_type)
            if index >= 0:
                self.hospital_type_box.setCurrentIndex(index)
        if self.hospital_path_edit is not None:
            self.hospital_path_edit.setText(result.get("hospital_data_path") or self.hospital_path_edit.text().strip())
        hospital_edit = self.project_fields.get("hospital")
        hospital_name = result.get("hospital") or (hospital_type if hospital_type != "自动识别" else "")
        if hospital_edit is not None and hospital_name:
            hospital_edit.setText(str(hospital_name))
        if self.hospital_summary is not None:
            self.hospital_summary.setText(result.get("summary") or "读取完成")

        self.save_current_project()
        QMessageBox.information(self, "读取完成", "医院时间信息已写入当前项目")

    def handle_hospital_time_error(self, message):
        """展示医院目录读取失败信息。"""
        if self.hospital_summary is not None:
            self.hospital_summary.setText(f"读取失败：{message}")
        QMessageBox.warning(self, "读取失败", message)

    def clear_hospital_worker(self):
        """清理医院时间读取线程引用。"""
        self.hospital_thread = None
        self.hospital_worker = None

    def load_video_raw(self, file_path):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if cv2 is None:
            QMessageBox.warning(self, "缺少依赖", "未安装 opencv-python，无法读取视频信息")
            return

        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            QMessageBox.warning(self, "读取失败", "无法打开视频文件")
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = frame_count / fps if fps else 0
        cap.release()

        self.video_summary.setText(
            "\n".join([
                f"文件名：{os.path.basename(file_path)}",
                f"路径：{file_path}",
                f"分辨率：{width} x {height}",
                f"帧率：{fps:.2f} FPS",
                f"总帧数：{frame_count}",
                f"时长：{duration:.2f} 秒",
            ])
        )

        rows = [
            ["文件名", os.path.basename(file_path)],
            ["分辨率", f"{width} x {height}"],
            ["帧率", f"{fps:.2f} FPS"],
            ["总帧数", str(frame_count)],
            ["时长", f"{duration:.2f} 秒"],
        ]
        self.add_table_tab("视频信息", ["字段", "值"], rows)

    def load_watch_raw(self, path):
        """根据文件类型读取腕表原始数据并显示预览。"""
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".mat":
                self.load_mat_raw(path)
            elif ext == ".csv":
                self.load_table_file_raw(path, "csv")
            elif ext in [".xlsx", ".xls"]:
                self.load_table_file_raw(path, "excel")
            else:
                QMessageBox.information(self, "格式不支持", "当前只支持 .mat、.csv、.xlsx、.xls 文件")
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", f"文件读取失败：{exc}")

    def load_mat_raw(self, path):
        """读取 MAT 文件中的 data 和 label，并展示关键数组。"""
        if loadmat is None or np is None:
            QMessageBox.warning(self, "缺少依赖", "未安装 scipy/numpy，无法读取 .mat 文件")
            return

        mat_data = loadmat(path)
        keys = [k for k in mat_data.keys() if not k.startswith("__")]
        summary_lines = [
            f"文件名：{os.path.basename(path)}",
            f"变量：{', '.join(keys)}",
        ]

        if "data" in mat_data:
            data = mat_data["data"]
            summary_lines.append(f"data 形状：{data.shape}")
            raw_map = [
                ("time_raw", data[0, 0] if data.shape[0] > 0 else None),
                ("acc_raw", data[1, 0] if data.shape[0] > 1 else None),
                ("gyr_raw", data[2, 0] if data.shape[0] > 2 else None),
                ("emg_raw", data[4, 0] if data.shape[0] > 4 else None),
                ("ppg_raw", data[7, 0] if data.shape[0] > 7 else None),
            ]
            for name, arr in raw_map:
                if arr is not None:
                    summary_lines.append(f"{name} 形状：{np.asarray(arr).shape}")
                    self.add_array_tab(name, arr)
        else:
            for key in keys:
                arr = mat_data[key]
                summary_lines.append(f"{key} 形状：{np.asarray(arr).shape}")
                self.add_array_tab(key, arr)

        if "label" in mat_data:
            label = mat_data["label"]
            summary_lines.append(f"label 形状：{np.asarray(label).shape}")
            self.add_array_tab("label", label)

        self.watch_summary.setText("\n".join(summary_lines))

    def load_table_file_raw(self, path, file_type):
        """读取 CSV 或 Excel 文件并展示前 200 行。"""
        import pandas as pd

        if file_type == "csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)

        self.watch_summary.setText(
            "\n".join([
                f"文件名：{os.path.basename(path)}",
                f"行数：{len(df)}",
                f"列数：{len(df.columns)}",
                f"列名：{', '.join(map(str, df.columns))}",
            ])
        )
        rows = df.head(200).astype(str).values.tolist()
        self.add_table_tab(os.path.basename(path), [str(c) for c in df.columns], rows)

    def add_array_tab(self, title, array):
        """把数组压缩成表格形式，并限制预览行列数量。"""
        arr = np.asarray(array)
        arr = np.squeeze(arr)

        if arr.ndim == 0:
            rows = [["0", str(arr.item())]]
            self.add_table_tab(title, ["索引", "数值"], rows)
            return

        if arr.ndim == 1:
            preview = arr[:200]
            rows = [[str(i), self.format_raw_value(v)] for i, v in enumerate(preview)]
            self.add_table_tab(title, ["索引", "数值"], rows)
            return

        flat = arr.reshape(arr.shape[0], -1)
        preview = flat[:200, :12]
        headers = ["索引"] + [f"列{c + 1}" for c in range(preview.shape[1])]
        rows = []
        for r, values in enumerate(preview):
            rows.append([str(r)] + [self.format_raw_value(v) for v in values])
        self.add_table_tab(title, headers, rows)

    def add_table_tab(self, title, headers, rows):
        """函数说明：执行当前函数对应的业务逻辑。"""
        table = QTableWidget()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)

        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))

        self.raw_tabs.addTab(table, title[:18])

    def format_raw_value(self, value):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        if hasattr(value, "shape") and getattr(value, "shape", None):
            return str(np.asarray(value).tolist())
        return str(value)


# =========================
# 4 可视化项目页
# =========================

class VisualProjectPage(QWidget):
    def __init__(self, back, open_annotation):
        """初始化历史可视化项目占位页。"""
        super().__init__()
        self.back = back
        self.open_annotation = open_annotation
        self.init_ui()

    def init_ui(self):
        """搭建历史项目列表和进入标注页面的按钮。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(35, 30, 35, 30)

        title = QLabel("可视化项目")
        title.setObjectName("TitleLabel")
        title.setAlignment(Qt.AlignCenter)

        card = make_card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 30, 30, 30)
        card_layout.setSpacing(20)

        tip = QLabel("请选择历史项目继续查看或标注")
        tip.setObjectName("SubTitleLabel")

        list_widget = QListWidget()
        for item in [
            "示例项目 01    未完成    已保存 2 条标注",
            "示例项目 02    已完成    已保存 5 条标注",
            "示例项目 03    未完成    已保存 1 条标注",
        ]:
            QListWidgetItem(item, list_widget)

        btn_layout = QHBoxLayout()
        back_btn = make_white_button("返回")
        open_btn = make_blue_button("进入标注")

        back_btn.clicked.connect(self.back)
        open_btn.clicked.connect(self.open_annotation)

        btn_layout.addWidget(back_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(open_btn)

        card_layout.addWidget(tip)
        card_layout.addWidget(list_widget)
        card_layout.addLayout(btn_layout)

        root.addWidget(title)
        root.addWidget(card)


# =========================
# 5 标注记录页
# =========================

class RecordPage(QWidget):
    def __init__(self, back, db=None, app_state=None):
        """初始化标注记录页面。"""
        super().__init__()
        self.back = back
        self.db = db
        self.app_state = app_state or {}
        self.table = None
        self.init_ui()

    def init_ui(self):
        """搭建标注记录表格。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(35, 30, 35, 30)
        root.setSpacing(18)

        top = QHBoxLayout()
        logo = QLabel("Labeling Project")
        logo.setObjectName("LogoLabel")

        back_btn = make_white_button("返回")
        back_btn.clicked.connect(self.back)

        top.addWidget(logo)
        top.addStretch()
        top.addWidget(back_btn)

        self.table = QTableWidget()
        self.table.setColumnCount(15)
        self.table.setHorizontalHeaderLabels([
            "ID", "项目ID", "标注类型", "患者姓名", "年龄", "性别", "医院", "惯用手",
            "出生日期", "发作日期", "佩戴手", "开始时间", "结束时间", "持续时间", "文件"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        root.addLayout(top)
        root.addWidget(self.table)
        self.refresh_records()

    def refresh_records(self):
        """从数据库重新加载全部未删除标注。"""
        if self.table is None:
            return
        project_id = self.app_state.get("project_id")
        records = self.db.list_annotations(project_id) if self.db is not None and project_id else []
        self.table.setRowCount(len(records))
        for row, item in enumerate(records):
            start_ms = int(item.get("start_ms", 0))
            end_ms = int(item.get("end_ms", 0))
            duration_s = max(0, end_ms - start_ms) / 1000
            values = [
                str(item.get("id", "")),
                str(item.get("project_id", "")),
                item.get("label", ""),
                item.get("patient_name", ""),
                item.get("patient_age", ""),
                item.get("patient_gender", ""),
                item.get("hospital", ""),
                item.get("dominant_hand", ""),
                item.get("birth_date", ""),
                item.get("seizure_date", ""),
                item.get("wearing_hand", ""),
                self.format_ms(start_ms, item.get("watch_start")),
                self.format_ms(end_ms, item.get("watch_start")),
                f"{duration_s:.1f}s",
                os.path.basename(item.get("video_path", "")) or os.path.basename(item.get("watch_path", "")),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

    def format_ms(self, value, watch_start=None):
        """把毫秒时间格式化为腕表真实时间或相对分秒。"""
        return format_watch_time(watch_start, value)

# =========================
# 6 标注工作页
# =========================

class AnnotationPage(QWidget):
    def __init__(self, back, app_state, db=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        super().__init__()
        self.back = back
        self.app_state = app_state
        self.db = db
        self.media_player = None
        self.video_widget = None
        self.video_area = None
        self.video_cap = None
        self.video_decode_thread = None
        self.video_decode_worker = None
        self.video_fps = 25.0
        self.video_frame_count = 0
        self.current_frame_idx = -1
        self.video_path = ""
        self.playback_clock = QElapsedTimer()
        self.playback_start_ms = 0
        self.playback_position_ms = 0
        self.video_playing = False
        self.timeline = None
        self.wave = None
        self.energy_axis = None
        self.raw_signals = {}
        self.current_start_time_text = ""
        self.current_data_duration_ms = 60000
        self.loaded_project_id = None
        self.loaded_video_path = ""
        self.loaded_watch_path = ""
        self.loaded_watch_mtime = None
        self.loaded_watch_signature = None
        self.filtered_signal_cache = {}
        self.modality_order = ["ACC", "GYRO", "EMG", "PPG"]
        self.modality_settings = {
            name: {"visible": True, "hp": "", "lp": "", "notch": ""}
            for name in self.modality_order
        }
        self.modality_select_check = None
        self.position_slider = None
        self.time_text = None
        self.play_btn = None
        self.align_btn = None
        self.lock_btn = None
        self.offset_label = None
        self.last_saved_signal_offset_ms = None
        self.annotation_info = None
        self.annotation_type_box = None
        self.auto_offset_btn = None
        self.cancel_offset_btn = None
        self.confirm_auto_offset_btn = None
        self.reject_auto_offset_btn = None
        self.offset_progress_bar = None
        self.offset_status_label = None
        self.offset_thread = None
        self.offset_worker = None
        self.pending_auto_offset_result = None
        self.annotation_types = self.db.list_annotation_types() if self.db is not None else ["癫痫"]
        project_id = self.app_state.get("project_id")
        self.annotation_records = self.db.list_annotations(project_id) if self.db is not None and project_id else []
        self.pending_annotation_start_ms = None
        self.pending_annotation_end_ms = None
        self.align_mode = False
        self.offset_locked = True
        self.duration_ms = 60000
        self.last_wave_update_ms = -1000
        self.last_ui_update_ms = -1000
        self.last_displayed_offset_ms = None
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(30)
        self.play_timer.timeout.connect(self.advance_open_cv_frame)
        self.init_ui()

    def init_ui(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(10)
        outer.addWidget(scroll)

        top = QHBoxLayout()
        back_btn = make_white_button("返回")
        self.modality_select_check = QCheckBox("模态选择")
        patient_edit = QLineEdit()
        patient_edit.setPlaceholderText("患者编号/姓名")
        patient_edit.setMinimumWidth(120)
        event_edit = QLineEdit()
        event_edit.setPlaceholderText("事件备注")
        event_edit.setMinimumWidth(120)
        save_btn = make_blue_button("保存项目")

        back_btn.clicked.connect(self.back)
        self.modality_select_check.clicked.connect(self.open_modality_dialog)

        top.addWidget(back_btn)
        top.addWidget(self.modality_select_check)
        top.addSpacing(8)
        top.addWidget(patient_edit)
        top.addWidget(event_edit)
        top.addStretch()
        top.addWidget(save_btn)

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(8)
        media_splitter = QSplitter(Qt.Horizontal)
        media_splitter.setChildrenCollapsible(False)
        media_splitter.setHandleWidth(8)

        video_card = make_card()
        video_layout = QVBoxLayout(video_card)
        video_layout.setContentsMargins(14, 14, 14, 14)
        video_layout.setSpacing(10)

        video_label = QLabel("视频播放区")
        video_label.setObjectName("SectionTitle")

        self.video_area = QLabel("[ 请导入视频文件 ]")
        self.video_area.setAlignment(Qt.AlignCenter)
        self.video_area.setMinimumSize(320, 180)
        self.video_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_area.setScaledContents(False)
        self.video_area.setStyleSheet("""
            background-color: white;
            border: 1px solid #A8CBD9;
            border-radius: 12px;
            font-size: 12pt;
            color: #345;
        """)

        control_row = QHBoxLayout()
        control_row.setSpacing(6)
        self.play_btn = make_blue_button("播放")
        pause_btn = make_white_button("暂停")
        self.play_btn.setMinimumHeight(36)
        pause_btn.setMinimumHeight(36)
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, self.duration_ms)
        self.position_slider.setMinimumWidth(120)
        self.time_text = QLabel("00:00 / 00:00")
        self.time_text.setMinimumWidth(96)
        self.align_btn = make_white_button("对齐")
        self.align_btn.setCheckable(True)
        self.align_btn.setMinimumHeight(36)
        self.lock_btn = make_white_button("固定")
        self.lock_btn.setCheckable(True)
        self.lock_btn.setChecked(True)
        self.lock_btn.setMinimumHeight(36)
        self.offset_label = QLabel("偏移 0.0s")
        self.offset_label.setMinimumWidth(90)

        self.play_btn.clicked.connect(self.play)
        pause_btn.clicked.connect(self.pause)
        self.position_slider.sliderMoved.connect(self.seek)
        self.align_btn.clicked.connect(self.toggle_align_mode)
        self.lock_btn.clicked.connect(self.toggle_offset_lock)

        control_row.addWidget(self.play_btn)
        control_row.addWidget(pause_btn)
        control_row.addWidget(self.position_slider)
        control_row.addWidget(self.time_text)
        control_row.addWidget(self.align_btn)
        control_row.addWidget(self.lock_btn)
        control_row.addWidget(self.offset_label)

        video_layout.addWidget(video_label)
        video_layout.addWidget(self.video_area, 1)
        video_layout.addLayout(control_row)

        right_card = make_card()
        right_card.setMinimumWidth(200)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(14, 14, 14, 14)
        right_layout.setSpacing(10)

        pre_btn = make_blue_button("预标注")
        pre_btn.setMinimumHeight(36)
        self.auto_offset_btn = make_white_button("自动获取偏移量")
        self.auto_offset_btn.setMinimumHeight(36)
        self.auto_offset_btn.clicked.connect(self.start_auto_offset_estimation)
        self.cancel_offset_btn = make_white_button("取消计算")
        self.cancel_offset_btn.setMinimumHeight(36)
        self.cancel_offset_btn.setVisible(False)
        self.cancel_offset_btn.clicked.connect(self.cancel_auto_offset_estimation)
        self.confirm_auto_offset_btn = make_blue_button("确认自动偏移")
        self.confirm_auto_offset_btn.setMinimumHeight(36)
        self.confirm_auto_offset_btn.setVisible(False)
        self.confirm_auto_offset_btn.clicked.connect(self.confirm_auto_offset)
        self.reject_auto_offset_btn = make_white_button("放弃自动偏移")
        self.reject_auto_offset_btn.setMinimumHeight(36)
        self.reject_auto_offset_btn.setVisible(False)
        self.reject_auto_offset_btn.clicked.connect(self.reject_auto_offset)
        self.offset_progress_bar = QProgressBar()
        self.offset_progress_bar.setRange(0, 100)
        self.offset_progress_bar.setValue(0)
        self.offset_progress_bar.setTextVisible(True)
        self.offset_progress_bar.setVisible(False)
        self.offset_status_label = QLabel("")
        self.offset_status_label.setObjectName("SubTitleLabel")
        self.offset_status_label.setWordWrap(True)
        self.offset_status_label.setVisible(False)
        self.annotation_type_box = QComboBox()
        self.annotation_type_box.addItems(self.annotation_types)
        self.annotation_type_box.setMinimumHeight(34)
        add_type_btn = make_white_button("新增")
        edit_type_btn = make_white_button("修改")
        remove_type_btn = make_white_button("删除")
        add_type_btn.setMinimumHeight(32)
        edit_type_btn.setMinimumHeight(32)
        remove_type_btn.setMinimumHeight(32)
        add_type_btn.clicked.connect(self.add_annotation_type)
        edit_type_btn.clicked.connect(self.rename_annotation_type)
        remove_type_btn.clicked.connect(self.delete_annotation_type)

        info = QTextEdit()
        info.setReadOnly(True)
        info.setMinimumHeight(110)
        info.clear()
        self.annotation_info = info

        start_mark_btn = make_white_button("标注开始")
        end_mark_btn = make_white_button("标注结束")
        start_mark_btn.setMinimumHeight(36)
        end_mark_btn.setMinimumHeight(36)

        confirm_btn = make_blue_button("保存标注")
        delete_btn = make_danger_button("删除上一条")
        confirm_btn.setMinimumHeight(36)
        delete_btn.setMinimumHeight(36)
        start_mark_btn.clicked.connect(self.mark_annotation_start)
        end_mark_btn.clicked.connect(self.mark_annotation_end)
        confirm_btn.clicked.connect(self.save_pending_annotation)
        delete_btn.clicked.connect(self.delete_last_annotation)

        prev_btn = make_white_button("上一条")
        next_btn = make_white_button("下一条")
        prev_btn.setMinimumHeight(36)
        next_btn.setMinimumHeight(36)

        type_row = QHBoxLayout()
        type_row.addWidget(self.annotation_type_box, 1)
        type_row.addWidget(add_type_btn)
        type_row.addWidget(edit_type_btn)
        type_row.addWidget(remove_type_btn)

        mark_row = QHBoxLayout()
        mark_row.addWidget(start_mark_btn)
        mark_row.addWidget(end_mark_btn)

        action_row = QHBoxLayout()
        action_row.addWidget(confirm_btn)
        action_row.addWidget(delete_btn)

        nav_row = QHBoxLayout()
        nav_row.addWidget(prev_btn)
        nav_row.addWidget(next_btn)

        right_layout.addWidget(pre_btn)
        right_layout.addWidget(self.auto_offset_btn)
        right_layout.addWidget(self.cancel_offset_btn)
        right_layout.addWidget(self.confirm_auto_offset_btn)
        right_layout.addWidget(self.reject_auto_offset_btn)
        right_layout.addWidget(self.offset_progress_bar)
        right_layout.addWidget(self.offset_status_label)
        right_layout.addLayout(type_row)
        right_layout.addLayout(mark_row)
        right_layout.addWidget(info)
        right_layout.addLayout(action_row)
        right_layout.addSpacing(8)
        right_layout.addLayout(nav_row)
        right_layout.addStretch()

        media_splitter.addWidget(video_card)
        media_splitter.addWidget(right_card)
        media_splitter.setStretchFactor(0, 4)
        media_splitter.setStretchFactor(1, 1)
        media_splitter.setSizes([820, 260])

        signal_card = make_card()
        signal_card.setMinimumHeight(280)
        signal_layout = QVBoxLayout(signal_card)
        signal_layout.setContentsMargins(14, 10, 14, 12)
        signal_layout.setSpacing(6)

        time_label = QLabel("整体时间轴")
        time_label.setObjectName("SectionTitle")
        self.timeline = TimelineWidget()

        watch_label = QLabel("腕表信号区")
        watch_label.setObjectName("SectionTitle")
        signal_filter_row = QHBoxLayout()
        signal_filter_row.setSpacing(14)
        signal_filter_row.addWidget(watch_label)
        modal_tip = QLabel("按住 Ctrl 滚动可缩放显示窗口，拖拽波形可人工对齐")
        modal_tip.setObjectName("SubTitleLabel")
        signal_filter_row.addWidget(modal_tip)
        signal_filter_row.addStretch()

        self.wave = SignalWaveWidget()
        self.wave.on_offset_changed = self.update_offset_label
        self.wave.on_annotation_range = self.add_annotation_from_drag
        self.energy_axis = EnergyAxisWidget()
        self.energy_axis.on_seek = self.seek

        signal_layout.addWidget(time_label)
        signal_layout.addWidget(self.timeline)
        signal_layout.addLayout(signal_filter_row)
        signal_layout.addWidget(self.wave, 1)
        signal_layout.addWidget(self.energy_axis)

        main_splitter.addWidget(media_splitter)
        main_splitter.addWidget(signal_card)
        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 2)
        main_splitter.setSizes([400, 340])

        root.addLayout(top)
        root.addWidget(main_splitter, 1)
        scroll.setWidget(content)
        self.sync_annotation_overlay()
        self.update_annotation_display()

    def refresh_loaded_data(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        project_id = self.app_state.get("project_id")
        project_changed = project_id != self.loaded_project_id
        if self.db is not None:
            project = self.db.get_project(project_id) if project_id else self.db.get_current_project()
            if project:
                project_id = project["id"]
                project_changed = project_id != self.loaded_project_id
                self.app_state["project_id"] = project_id
                self.app_state["project"] = project
                self.app_state["video_path"] = project.get("video_path") or ""
                self.app_state["watch_path"] = project.get("watch_path") or ""
                self.app_state["signal_offset_ms"] = int(project.get("signal_offset_ms") or 0)
                self.current_start_time_text = str(project.get("watch_start") or "").strip()
                if self.timeline is not None:
                    self.timeline.set_project_times(project)
            self.annotation_types = self.db.list_annotation_types()
            self.annotation_records = self.db.list_annotations(project_id) if project_id else []
            self.refresh_annotation_type_box()
            if self.wave is not None:
                self.wave.set_signal_offset(self.app_state.get("signal_offset_ms", 0))
            self.sync_annotation_overlay()
            self.update_annotation_display()
        video_path = self.app_state.get("video_path")
        watch_path = self.app_state.get("watch_path")

        if video_path and os.path.exists(video_path):
            if project_changed or video_path != self.loaded_video_path or self.video_cap is None:
                self.load_video(video_path)
        else:
            self.close_video_capture()
            self.loaded_video_path = ""
            if self.video_area is not None:
                self.video_area.clear()
                self.video_area.setText("[ 请导入当前项目的视频文件 ]")
                self.video_area.setToolTip("当前项目还没有可用视频")

        if watch_path and os.path.exists(watch_path):
            if project_changed or self.should_reload_watch_data(watch_path):
                self.load_watch_signals(watch_path)
            else:
                if self.wave is not None:
                    self.wave.set_signal_offset(self.app_state.get("signal_offset_ms", 0))
                self.update_offset_label()
        else:
            self.raw_signals = {}
            self.loaded_watch_path = ""
            self.loaded_watch_mtime = None
            self.loaded_watch_signature = None
            self.filtered_signal_cache.clear()
            self.current_start_time_text = ""
            self.current_data_duration_ms = 60000
            if self.wave is not None:
                self.wave.set_signals([], self.duration_ms, self.current_data_duration_ms, "")
                self.wave.set_signal_offset(self.app_state.get("signal_offset_ms", 0))
            if self.timeline is not None:
                self.timeline.set_project_times(self.app_state.get("project") or {})
            if self.energy_axis is not None:
                self.energy_axis.set_energy([], 1.0, self.current_data_duration_ms, self.duration_ms)
            self.update_offset_label()
        self.loaded_project_id = project_id

    def should_reload_watch_data(self, watch_path):
        """判断当前腕表数据是否需要重新读取。"""
        project = self.app_state.get("project") or {}
        signature = (
            watch_path,
            float(project.get("acc_fs") or 100.0),
            float(project.get("gyro_fs") or 100.0),
            float(project.get("emg_fs") or 200.0),
            float(project.get("ppg_fs") or 25.0),
        )
        try:
            mtime = os.path.getmtime(watch_path)
        except OSError:
            mtime = None
        return (
            watch_path != self.loaded_watch_path
            or mtime != self.loaded_watch_mtime
            or signature != self.loaded_watch_signature
            or not self.raw_signals
        )

    def current_position_ms(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.position_slider is None:
            return 0
        return int(self.position_slider.value())

    def current_watch_position_ms(self):
        """把当前视频播放位置换算到腕表轴位置。"""
        offset_ms = self.wave.get_signal_offset() if self.wave is not None else 0
        return max(0, min(self.current_position_ms() + int(offset_ms), self.current_data_duration_ms))

    def refresh_annotation_type_box(self, selected_text=None):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_type_box is None:
            return
        selected_text = selected_text or self.annotation_type_box.currentText()
        self.annotation_type_box.blockSignals(True)
        self.annotation_type_box.clear()
        self.annotation_type_box.addItems(self.annotation_types)
        index = self.annotation_type_box.findText(selected_text)
        if index < 0:
            index = 0
        self.annotation_type_box.setCurrentIndex(index)
        self.annotation_type_box.blockSignals(False)

    def add_annotation_type(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        text, ok = QInputDialog.getText(self, "新增标注类型", "标注类型名称：")
        text = text.strip() if ok else ""
        if not text:
            return
        if text in self.annotation_types:
            QMessageBox.information(self, "提示", "该标注类型已存在")
            self.refresh_annotation_type_box(text)
            return
        self.annotation_types.append(text)
        if self.db is not None:
            self.db.add_annotation_type(text)
        self.refresh_annotation_type_box(text)

    def rename_annotation_type(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_type_box is None or not self.annotation_types:
            return
        old_text = self.annotation_type_box.currentText()
        text, ok = QInputDialog.getText(self, "修改标注类型", "标注类型名称：", text=old_text)
        text = text.strip() if ok else ""
        if not text or text == old_text:
            return
        if text in self.annotation_types:
            QMessageBox.information(self, "提示", "该标注类型已存在")
            self.refresh_annotation_type_box(text)
            return
        index = self.annotation_types.index(old_text)
        self.annotation_types[index] = text
        for item in self.annotation_records:
            if item.get("label") == old_text:
                item["label"] = text
        if self.db is not None:
            self.db.rename_annotation_type(old_text, text)
        self.refresh_annotation_type_box(text)
        self.update_annotation_display()

    def delete_annotation_type(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_type_box is None or not self.annotation_types:
            return
        if len(self.annotation_types) <= 1:
            QMessageBox.information(self, "提示", "至少保留一个标注类型")
            return
        text = self.annotation_type_box.currentText()
        reply = QMessageBox.question(self, "删除标注类型", f"确认删除 {text}？")
        if reply != QMessageBox.Yes:
            return
        self.annotation_types = [item for item in self.annotation_types if item != text]
        if self.db is not None:
            self.db.delete_annotation_type(text)
        self.refresh_annotation_type_box()
        self.update_annotation_display()

    def mark_annotation_start(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.pending_annotation_start_ms = self.current_watch_position_ms()
        self.pending_annotation_end_ms = None
        self.sync_annotation_overlay()
        self.update_annotation_display()

    def mark_annotation_end(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.pending_annotation_end_ms = self.current_watch_position_ms()
        self.save_pending_annotation()

    def save_pending_annotation(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.pending_annotation_start_ms is None or self.pending_annotation_end_ms is None:
            self.update_annotation_display()
            return
        start_ms, end_ms = sorted([self.pending_annotation_start_ms, self.pending_annotation_end_ms])
        if end_ms - start_ms < 100:
            self.pending_annotation_end_ms = None
            self.update_annotation_display()
            return
        label = self.annotation_type_box.currentText() if self.annotation_type_box is not None else "癫痫"
        annotation_id = None
        project = self.app_state.get("project") or {}
        if self.db is not None:
            annotation_id = self.db.save_annotation(
                label,
                start_ms,
                end_ms,
                self.app_state.get("video_path", ""),
                self.app_state.get("watch_path", ""),
                self.app_state.get("project_id"),
                project
            )
        record = {
            "id": annotation_id,
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "label": label,
            "video_path": self.app_state.get("video_path", ""),
            "watch_path": self.app_state.get("watch_path", ""),
            "project_id": self.app_state.get("project_id"),
        }
        for key in ["patient_name", "patient_age", "patient_gender", "hospital", "dominant_hand", "birth_date", "seizure_date", "wearing_hand"]:
            record[key] = project.get(key, "")
        self.annotation_records.append(record)
        self.pending_annotation_start_ms = None
        self.pending_annotation_end_ms = None
        self.sync_annotation_overlay()
        self.update_annotation_display()

    def delete_last_annotation(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.pending_annotation_start_ms is not None:
            self.pending_annotation_start_ms = None
            self.pending_annotation_end_ms = None
        elif self.annotation_records:
            removed = self.annotation_records.pop()
            if self.db is not None and removed.get("id") is not None:
                self.db.delete_annotation(removed.get("id"))
        self.sync_annotation_overlay()
        self.update_annotation_display()

    def add_annotation_from_drag(self, start_data_ms, end_data_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        start_ms = max(0, min(int(start_data_ms), self.current_data_duration_ms))
        end_ms = max(0, min(int(end_data_ms), self.current_data_duration_ms))
        self.pending_annotation_start_ms = start_ms
        self.pending_annotation_end_ms = end_ms
        self.save_pending_annotation()

    def sync_annotation_overlay(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.wave is None:
            return
        offset_ms = self.wave.get_signal_offset()
        ranges = [
            (item["start_ms"], item["end_ms"])
            for item in self.annotation_records
        ]
        self.wave.set_annotation_ranges(ranges)
        if self.pending_annotation_start_ms is not None:
            end_ms = self.pending_annotation_end_ms
            if end_ms is None:
                end_ms = self.pending_annotation_start_ms
            self.wave.set_active_annotation_range(
                self.pending_annotation_start_ms,
                end_ms
            )
        else:
            self.wave.set_active_annotation_range(None)

    def update_annotation_display(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.annotation_info is None:
            return
        lines = []
        project = self.app_state.get("project") or {}
        info_parts = [
            project.get("patient_name"), project.get("patient_age"), project.get("patient_gender"),
            project.get("hospital"), project.get("dominant_hand"), project.get("wearing_hand"),
        ]
        info_text = "  ".join([str(item) for item in info_parts if item])
        if info_text:
            lines.append(f"项目信息：{info_text}")
            lines.append("")
        seizure_date = str(project.get("seizure_date") or "").strip()
        video_start = str(project.get("video_start") or "").strip()
        eeg_start = str(project.get("eeg_marker_start") or "").strip()
        eeg_end = str(project.get("eeg_marker_end") or "").strip()
        if seizure_date or eeg_start or eeg_end:
            date_text = seizure_date or "--"
            ref_start = eeg_start or "--"
            ref_end = eeg_end or "--"
            lines.append(f"脑电参考：{date_text}  {ref_start} - {ref_end}")
            if video_start:
                lines.append(f"视频起点：{video_start}")
            lines.append("")
        if self.pending_annotation_start_ms is not None:
            end_text = "--:--" if self.pending_annotation_end_ms is None else self.format_ms(self.pending_annotation_end_ms)
            lines.append(f"待保存标注：{self.format_ms(self.pending_annotation_start_ms)} - {end_text}")
            lines.append("")
        if not self.annotation_records:
            lines.append("暂无标注记录")
        else:
            for index, item in enumerate(self.annotation_records, 1):
                duration_s = (item["end_ms"] - item["start_ms"]) / 1000
                lines.append(
                    f"{index}. {item['label']}  {self.format_ms(item['start_ms'])} - {self.format_ms(item['end_ms'])}  ({duration_s:.1f}s)"
                )
        self.annotation_info.setPlainText("\n".join(lines))

    def load_video(self, path):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if path == self.loaded_video_path and self.video_cap is not None:
            return
        self.close_video_capture()
        self.video_path = path

        if cv2 is None:
            QMessageBox.warning(self, "缺少依赖", "未安装 opencv-python，无法播放视频")
            return

        self.video_cap = cv2.VideoCapture(path)
        if not self.video_cap.isOpened():
            QMessageBox.warning(self, "读取失败", "无法打开视频文件")
            self.video_cap = None
            return

        self.video_fps = self.video_cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.video_frame_count = int(self.video_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.current_frame_idx = -1
        self.play_timer.setInterval(max(10, int(round(1000.0 / max(self.video_fps, 1.0)))))
        if self.video_fps > 0 and self.video_frame_count > 0:
            self.set_duration(max(1, int(self.video_frame_count / self.video_fps * 1000)))
        else:
            self.set_duration(self.get_video_duration_ms(path))
        self.loaded_video_path = path
        self.show_frame_at(0)

    def get_video_duration_ms(self, path):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if cv2 is None:
            return self.duration_ms
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return self.duration_ms
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        if fps <= 0:
            return self.duration_ms
        return max(1, int(frame_count / fps * 1000))

    def load_watch_signals(self, path):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if loadmat is None or np is None:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext != ".mat":
            return

        mat_data = loadmat(path)
        if "data" not in mat_data:
            return

        data = mat_data["data"]
        raw_signals = {}
        start_time_text = extract_watch_start_time(mat_data, data)
        if start_time_text and self.db is not None and self.app_state.get("project_id"):
            project = self.db.update_project(self.app_state["project_id"], watch_start=start_time_text)
            self.app_state["project"] = project
        data_duration_ms = 0
        project = self.app_state.get("project") or {}
        acc_fs = float(project.get("acc_fs") or 100.0)
        gyro_fs = float(project.get("gyro_fs") or 100.0)
        emg_fs = float(project.get("emg_fs") or 200.0)
        ppg_fs = float(project.get("ppg_fs") or 25.0)
        if data.shape[0] > 1:
            acc = np.asarray(data[1, 0])
            if acc.ndim == 2 and acc.shape[1] >= 3:
                acc_value = acc[:, :3].astype(float)
            else:
                acc_value = np.ravel(acc)
            raw_signals["ACC"] = (acc_value, acc_fs)
            data_duration_ms = max(data_duration_ms, int(len(acc_value) / acc_fs * 1000))
        if data.shape[0] > 2:
            gyr = np.asarray(data[2, 0])
            if gyr.ndim == 2 and gyr.shape[1] >= 3:
                gyr_value = gyr[:, :3].astype(float)
            else:
                gyr_value = np.ravel(gyr)
            raw_signals["GYRO"] = (gyr_value, gyro_fs)
            data_duration_ms = max(data_duration_ms, int(len(gyr_value) / gyro_fs * 1000))
        if data.shape[0] > 4:
            emg_value = np.ravel(np.asarray(data[4, 0]))
            raw_signals["EMG"] = (emg_value, emg_fs)
            data_duration_ms = max(data_duration_ms, int(len(emg_value) / emg_fs * 1000))
        if data.shape[0] > 7:
            ppg = np.asarray(data[7, 0])
            if ppg.ndim == 2 and ppg.shape[1] >= 4:
                ppg_value = ppg[:, :4].astype(float)
            else:
                ppg_value = np.ravel(ppg)
            raw_signals["PPG"] = (ppg_value, ppg_fs)
            data_duration_ms = max(data_duration_ms, int(len(ppg_value) / ppg_fs * 1000))

        self.raw_signals = raw_signals
        self.filtered_signal_cache.clear()
        self.current_start_time_text = start_time_text
        self.current_data_duration_ms = max(1, data_duration_ms)
        self.modality_order = [name for name in ["ACC", "GYRO", "EMG", "PPG"] if name in raw_signals]
        for name in self.modality_order:
            self.modality_settings.setdefault(name, {"visible": True, "hp": "", "lp": "", "notch": ""})

        self.refresh_filtered_signals()
        max_offset = max(30 * 60 * 1000, data_duration_ms)
        self.wave.set_offset_limit(-max_offset, max_offset)
        self.wave.set_signal_offset(self.app_state.get("signal_offset_ms", 0))
        energy_source = raw_signals.get("ACC")
        if energy_source is None and len(raw_signals) > 0:
            energy_source = next(iter(raw_signals.values()))
        if energy_source is not None:
            energy, energy_fs = self.calculate_energy_axis(energy_source[0], energy_source[1])
            self.energy_axis.set_energy(energy, energy_fs, data_duration_ms, self.duration_ms)
            self.energy_axis.setVisible(True)
        project = self.app_state.get("project") or {}
        self.loaded_watch_path = path
        try:
            self.loaded_watch_mtime = os.path.getmtime(path)
        except OSError:
            self.loaded_watch_mtime = None
        self.loaded_watch_signature = (
            path,
            float(project.get("acc_fs") or 100.0),
            float(project.get("gyro_fs") or 100.0),
            float(project.get("emg_fs") or 200.0),
            float(project.get("ppg_fs") or 25.0),
        )
        self.update_offset_label()

    def open_modality_dialog(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if not self.raw_signals:
            if self.modality_select_check is not None:
                self.modality_select_check.setChecked(False)
            QMessageBox.information(self, "提示", "请先导入腕表数据")
            return

        dialog = ModalityFilterDialog(self.modality_order, self.modality_settings, self)
        if dialog.exec_() == QDialog.Accepted:
            self.modality_settings.update(dialog.result_settings())
            self.refresh_filtered_signals()
        if self.modality_select_check is not None:
            self.modality_select_check.setChecked(False)

    def select_manual_roi(self, video_path):
        """弹出 ROI 选择窗口并返回框选结果。"""
        frame = read_video_first_frame(video_path)
        dialog = RoiSelectionDialog(frame, self)
        if dialog.exec_() != QDialog.Accepted:
            return None
        return dialog.selected_roi()

    def get_eeg_reference_window_seconds(self, project):
        """把项目里的视频开始/脑电开始结束时间换算成相对秒数。"""
        video_start = str(project.get("video_start") or "").strip()
        eeg_start = str(project.get("eeg_marker_start") or "").strip()
        eeg_end = str(project.get("eeg_marker_end") or "").strip()
        if not video_start or not eeg_start or not eeg_end:
            raise RuntimeError("请先读取医院时间信息，确保已获得视频开始、脑电标记开始和脑电标记结束时间")
        start_ms = ms_between_clock_times(video_start, eeg_start)
        end_ms = ms_between_clock_times(video_start, eeg_end)
        if start_ms is None or end_ms is None:
            raise RuntimeError("无法把脑电参考时间换算为相对视频时间，请检查时间格式")
        if end_ms <= start_ms:
            raise RuntimeError("脑电标记结束时间必须晚于开始时间")
        return start_ms / 1000.0, end_ms / 1000.0

    def update_auto_offset_progress(self, percent, message):
        """刷新自动偏移进度条和提示文本。"""
        if self.offset_progress_bar is not None:
            self.offset_progress_bar.setVisible(True)
            self.offset_progress_bar.setValue(int(max(0, min(100, percent))))
        if self.offset_status_label is not None:
            self.offset_status_label.setVisible(True)
            self.offset_status_label.setText(str(message or ""))

    def start_auto_offset_estimation(self):
        """启动视频和腕表数据的自动偏移估计。"""
        if self.offset_thread is not None:
            QMessageBox.information(self, "提示", "正在计算偏移量，请稍候")
            return
        video_path = self.app_state.get("video_path", "")
        watch_path = self.app_state.get("watch_path", "")
        if not video_path or not os.path.exists(video_path):
            QMessageBox.warning(self, "缺少视频", "请先在当前项目中导入视频文件")
            return
        if not watch_path or not os.path.exists(watch_path):
            QMessageBox.warning(self, "缺少腕表数据", "请先在当前项目中导入腕表数据文件")
            return
        project = self.app_state.get("project") or {}
        try:
            eeg_start_sec, eeg_end_sec = self.get_eeg_reference_window_seconds(project)
        except Exception as exc:
            QMessageBox.warning(self, "缺少脑电参考时间", str(exc))
            return

        try:
            roi = self.select_manual_roi(video_path)
        except Exception as exc:
            QMessageBox.warning(self, "ROI 选择失败", str(exc))
            return
        if roi is None:
            return

        acc_fs = float(project.get("acc_fs") or 100.0)
        gyro_fs = float(project.get("gyro_fs") or 100.0)
        wrist_start_relative_sec = None
        video_start_text = str(project.get("video_start") or "").strip()
        watch_start_text = str(project.get("watch_start") or "").strip()
        if video_start_text and watch_start_text:
            watch_offset_ms = ms_between_clock_times(
                video_start_text,
                watch_start_text,
                rollover=False,
            )
            if watch_offset_ms is not None:
                wrist_start_relative_sec = watch_offset_ms / 1000.0

        self.auto_offset_btn.setEnabled(False)
        self.auto_offset_btn.setText("计算偏移中...")
        self.cancel_offset_btn.setVisible(True)
        self.cancel_offset_btn.setEnabled(True)
        self.confirm_auto_offset_btn.setVisible(False)
        self.confirm_auto_offset_btn.setEnabled(True)
        self.reject_auto_offset_btn.setVisible(False)
        self.pending_auto_offset_result = None
        self.update_auto_offset_progress(0, "已确认 ROI，准备开始计算...")
        self.offset_thread = QThread(self)
        self.offset_worker = OffsetEstimateWorker(
            video_path,
            watch_path,
            roi,
            eeg_start_sec,
            eeg_end_sec,
            acc_fs,
            gyro_fs,
            wrist_start_relative_sec=wrist_start_relative_sec,
        )
        self.offset_worker.moveToThread(self.offset_thread)
        self.offset_thread.started.connect(self.offset_worker.run)
        self.offset_worker.progress.connect(self.update_auto_offset_progress)
        self.offset_worker.finished.connect(self.apply_auto_offset_result)
        self.offset_worker.failed.connect(self.handle_auto_offset_error)
        self.offset_worker.cancelled.connect(self.handle_auto_offset_cancelled)
        self.offset_worker.finished.connect(self.offset_thread.quit)
        self.offset_worker.failed.connect(self.offset_thread.quit)
        self.offset_worker.cancelled.connect(self.offset_thread.quit)
        self.offset_thread.finished.connect(self.cleanup_auto_offset_worker)
        self.offset_thread.start()

    def cleanup_auto_offset_worker(self):
        """释放自动偏移计算线程对象。"""
        if self.offset_worker is not None:
            self.offset_worker.deleteLater()
        if self.offset_thread is not None:
            self.offset_thread.deleteLater()
        self.offset_worker = None
        self.offset_thread = None
        if self.auto_offset_btn is not None:
            self.auto_offset_btn.setEnabled(True)
            self.auto_offset_btn.setText("自动获取偏移量")
        if self.cancel_offset_btn is not None:
            self.cancel_offset_btn.setVisible(False)
            self.cancel_offset_btn.setEnabled(True)

    def cancel_auto_offset_estimation(self):
        """请求取消后台自动偏移估计。"""
        if self.offset_worker is not None:
            self.cancel_offset_btn.setEnabled(False)
            self.update_auto_offset_progress(0, "正在取消自动偏移计算...")
            self.offset_worker.request_cancel()

    def handle_auto_offset_cancelled(self):
        """处理用户主动取消自动偏移估计。"""
        self.update_auto_offset_progress(0, "自动偏移计算已取消")

    def handle_auto_offset_error(self, message):
        """显示自动偏移计算失败信息。"""
        self.update_auto_offset_progress(0, f"自动偏移失败：{message}")
        QMessageBox.warning(self, "自动偏移失败", message)

    def apply_auto_offset_result(self, result):
        """预览自动偏移结果，等待用户确认后再写入项目。"""
        offset_ms = int(result.get("offset_ms", 0))
        self.pending_auto_offset_result = dict(result)
        if self.wave is not None:
            self.wave.set_signal_offset(offset_ms)
        self.update_offset_label()
        self.sync_annotation_overlay()
        self.confirm_auto_offset_btn.setVisible(True)
        self.reject_auto_offset_btn.setVisible(True)
        confidence = float(result.get("confidence", 0.0))
        offset_s = offset_ms / 1000
        if confidence >= 0.75:
            level = "置信度较高，建议确认后使用"
            self.confirm_auto_offset_btn.setEnabled(True)
        elif confidence >= 0.50:
            level = "置信度一般，建议先人工复核"
            self.confirm_auto_offset_btn.setEnabled(True)
        else:
            level = "置信度较低，不建议直接使用"
            self.confirm_auto_offset_btn.setEnabled(False)
        self.update_auto_offset_progress(100, f"自动偏移已完成：{offset_s:.2f}s，{level}")

    def confirm_auto_offset(self):
        """确认当前自动偏移预览，并保存质量信息。"""
        if self.pending_auto_offset_result is None or self.wave is None:
            return
        result = dict(self.pending_auto_offset_result)
        self.save_signal_offset(
            int(self.wave.get_signal_offset()),
            source="auto_confirmed",
            result=result,
        )
        self.pending_auto_offset_result = None
        self.confirm_auto_offset_btn.setVisible(False)
        self.confirm_auto_offset_btn.setEnabled(True)
        self.reject_auto_offset_btn.setVisible(False)
        self.update_auto_offset_progress(100, "自动偏移已确认并保存")

    def reject_auto_offset(self):
        """放弃当前自动偏移预览，恢复确认前的项目偏移。"""
        self.pending_auto_offset_result = None
        saved_offset = int(self.app_state.get("signal_offset_ms", 0) or 0)
        if self.wave is not None:
            self.wave.set_signal_offset(saved_offset)
        self.confirm_auto_offset_btn.setVisible(False)
        self.reject_auto_offset_btn.setVisible(False)
        self.update_auto_offset_progress(0, "已放弃自动偏移结果")

    def refresh_filtered_signals(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        signals = []
        visible_names = []
        for name in self.modality_order:
            if name not in self.raw_signals:
                continue
            cfg = self.modality_settings.get(name, {})
            if not cfg.get("visible", True):
                continue
            values, sample_rate = self.raw_signals[name]
            cache_key = (
                name,
                float(sample_rate),
                str(cfg.get("hp", "")),
                str(cfg.get("lp", "")),
                str(cfg.get("notch", "")),
            )
            if cache_key in self.filtered_signal_cache:
                filtered_values = self.filtered_signal_cache[cache_key]
            else:
                filtered_values = self.apply_filters_to_signal(name, values, sample_rate, cfg)
                self.filtered_signal_cache[cache_key] = filtered_values
            signals.append((name, filtered_values, sample_rate))
            visible_names.append(name)

        if self.wave is not None:
            position_ms = self.position_slider.value() if self.position_slider is not None else 0
            self.wave.set_visible_signals(visible_names)
            self.wave.set_signals(
                signals,
                self.duration_ms,
                self.current_data_duration_ms,
                self.current_start_time_text
            )
            self.wave.set_position(position_ms)

    def parse_filter_value(self, text):
        """函数说明：执行当前函数对应的业务逻辑。"""
        text = str(text or "").strip()
        if text == "" or text.lower() == "none":
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def parse_notch_value(self, text, sample_rate):
        """函数说明：执行当前函数对应的业务逻辑。"""
        text = str(text or "").strip()
        if text == "" or text.lower() == "none":
            return None
        numbers = re.findall(r"\d+(?:\.\d+)?", text)
        if len(numbers) >= 2:
            low, high = float(numbers[0]), float(numbers[1])
        elif len(numbers) == 1:
            center = float(numbers[0])
            low, high = center - 1.0, center + 1.0
        else:
            return None
        nyquist = float(sample_rate) / 2.0
        low = max(0.01, min(low, nyquist - 0.01))
        high = max(low + 0.01, min(high, nyquist - 0.01))
        return (low, high) if low < high else None

    def apply_filters_to_signal(self, name, values, sample_rate, cfg):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if butter is None or filtfilt is None or np is None:
            return values

        hp = self.parse_filter_value(cfg.get("hp", ""))
        lp = self.parse_filter_value(cfg.get("lp", ""))
        notch = self.parse_notch_value(cfg.get("notch", ""), sample_rate)

        stages = []
        if name in ["ACC", "GYRO"]:
            if hp is not None:
                stages.append(("high", hp, 6))
            if lp is not None:
                stages.append(("low", lp, 10))
        elif name == "EMG":
            if hp is not None:
                stages.append(("high", hp, 10))
            if notch is not None:
                stages.append(("stop", notch, 10))
        else:
            if hp is not None:
                stages.append(("high", hp, 6))
            if lp is not None:
                stages.append(("low", lp, 6))
            if notch is not None:
                stages.append(("stop", notch, 6))

        if not stages:
            return values
        return self.filter_array(values, sample_rate, stages)

    def filter_array(self, values, sample_rate, stages):
        """函数说明：执行当前函数对应的业务逻辑。"""
        arr = np.asarray(values, dtype=float)
        original_ndim = arr.ndim
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        if arr.shape[0] < 4:
            return values

        filtered = np.empty_like(arr, dtype=float)
        for col in range(arr.shape[1]):
            column = arr[:, col]
            if not np.any(np.isfinite(column)):
                filtered[:, col] = column
                continue
            filled = column.astype(float).copy()
            finite = np.isfinite(filled)
            if not np.all(finite):
                x = np.arange(len(filled))
                filled[~finite] = np.interp(x[~finite], x[finite], filled[finite])
            for filter_type, cutoff, order in stages:
                try:
                    nyquist = float(sample_rate) / 2.0
                    if filter_type == "stop":
                        low, high = cutoff
                        wn = [low / nyquist, high / nyquist]
                    else:
                        if cutoff <= 0 or cutoff >= nyquist:
                            continue
                        wn = cutoff / nyquist
                    b, a = butter(order, wn, btype=filter_type)
                    filled = filtfilt(b, a, filled)
                except Exception:
                    continue
            filtered[:, col] = filled

        if original_ndim == 1:
            return filtered[:, 0]
        return filtered

    def calculate_energy_axis(self, acc_value, sample_rate):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if np is None:
            return [], 1.0
        arr = np.asarray(acc_value, dtype=float)
        if arr.ndim == 2:
            arr = np.sqrt(np.sum(arr[:, :min(3, arr.shape[1])] ** 2, axis=1))
        arr = arr[np.isfinite(arr)]
        if len(arr) < 2:
            return arr, sample_rate

        target_fs = min(30.0, float(sample_rate))
        step = max(1, int(round(float(sample_rate) / target_fs)))
        downsampled = arr[::step]
        actual_fs = float(sample_rate) / step
        window_size = max(1, int(2.0 * actual_fs))
        kernel = np.ones(window_size) / window_size
        energy = np.sqrt(np.convolve(downsampled ** 2, kernel, mode="same"))
        return energy, actual_fs

    def set_duration(self, duration_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.duration_ms = max(1, int(duration_ms))
        self.position_slider.setRange(0, self.duration_ms)
        self.timeline.set_duration(self.duration_ms)
        self.wave.duration_ms = self.duration_ms
        if self.energy_axis is not None:
            self.energy_axis.set_display_duration(self.duration_ms)
        self.update_sync_position(0)

    def play(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.video_cap is None:
            video_path = self.app_state.get("video_path")
            if video_path and os.path.exists(video_path):
                self.load_video(video_path)
            else:
                QMessageBox.information(self, "提示", "当前已固定偏移，请先取消固定再拖拽对齐")
                return
        if self.video_decode_thread is not None and self.video_decode_thread.isRunning():
            return
        self.playback_start_ms = self.playback_position_ms or self.position_slider.value()
        self.playback_clock.restart()
        self.video_playing = True
        self.start_video_decoder(self.playback_start_ms)
        if self.video_decode_thread is None:
            self.play_timer.start()

    def pause(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.play_timer.stop()
        self.video_playing = False
        self.stop_video_decoder(wait=True)

    def seek(self, position_ms):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.pause()
        self.show_frame_at(position_ms)
        self.update_sync_position(position_ms, force_wave=True)

    def toggle_align_mode(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.align_mode = self.align_btn.isChecked()
        if self.align_mode:
            self.pause()
            self.lock_btn.setChecked(False)
            self.offset_locked = False
        self.wave.set_align_mode(self.align_mode)
        self.wave.set_offset_locked(self.offset_locked)
        self.update_offset_label()

    def toggle_offset_lock(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.offset_locked = self.lock_btn.isChecked()
        if self.offset_locked:
            self.align_btn.setChecked(False)
            self.align_mode = False
        self.wave.set_align_mode(self.align_mode)
        self.wave.set_offset_locked(self.offset_locked)
        if self.offset_locked and self.wave is not None:
            self.save_signal_offset(int(self.wave.get_signal_offset()), source="manual")
        self.update_offset_label()

    def update_signal_visibility(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        selected = [
            name for name in self.modality_order
            if self.modality_settings.get(name, {}).get("visible", True)
        ]
        if self.wave is not None:
            self.wave.set_visible_signals(selected)
        if self.energy_axis is not None:
            self.energy_axis.setVisible(True)

    def advance_open_cv_frame(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.video_cap is None:
            self.play_timer.stop()
            return

        position_ms = self.playback_start_ms + self.playback_clock.elapsed()
        if position_ms >= self.duration_ms:
            position_ms = self.duration_ms
            self.play_timer.stop()
            self.stop_video_decoder(wait=False)
        if self.video_decode_thread is None:
            self.show_frame_at(position_ms, prefer_sequential=True)
        self.update_sync_position(position_ms)
        self.playback_position_ms = position_ms

    def start_video_decoder(self, position_ms):
        """启动后台视频解码线程，界面线程只负责接收和显示图像。"""
        if self.video_decode_thread is not None:
            return
        target_size = self.video_area.contentsRect().size()
        target_width = target_size.width() if target_size.width() > 0 else 480
        target_height = target_size.height() if target_size.height() > 0 else 260
        start_frame = int(max(0, position_ms) / 1000.0 * self.video_fps)
        self.video_decode_thread = QThread(self)
        self.video_decode_worker = VideoDecodeWorker(
            self.loaded_video_path or self.video_path,
            start_frame,
            self.video_fps,
            self.video_frame_count,
            (target_width, target_height),
        )
        self.video_decode_worker.moveToThread(self.video_decode_thread)
        self.video_decode_thread.started.connect(self.video_decode_worker.run)
        self.video_decode_worker.frame_ready.connect(self.display_decoded_frame)
        self.video_decode_worker.failed.connect(self.handle_video_decode_error)
        self.video_decode_worker.finished.connect(self.video_decode_worker.deleteLater)
        self.video_decode_worker.finished.connect(self.video_decode_thread.quit, Qt.DirectConnection)
        self.video_decode_thread.finished.connect(self.cleanup_video_decoder)
        self.video_decode_thread.start()

    def stop_video_decoder(self, wait=False):
        """请求后台解码线程停止。"""
        worker = self.video_decode_worker
        thread = self.video_decode_thread
        if worker is None or thread is None:
            return
        worker.request_stop()
        if wait and thread.isRunning():
            thread.wait(500)

    def cleanup_video_decoder(self):
        """释放后台视频解码线程和 worker。"""
        if self.video_decode_thread is not None:
            self.video_decode_thread.deleteLater()
        self.video_decode_worker = None
        self.video_decode_thread = None
        self.video_playing = False

    def display_decoded_frame(self, image, frame_idx):
        """在主线程显示后台解码完成的图像。"""
        if image is None:
            return
        if not self.video_playing:
            return
        self.current_frame_idx = int(frame_idx)
        self.video_area.setPixmap(QPixmap.fromImage(image))
        position_ms = int(self.current_frame_idx / max(self.video_fps, 1.0) * 1000)
        self.update_sync_position(position_ms)

    def handle_video_decode_error(self, message):
        """显示后台视频解码错误。"""
        self.play_timer.stop()
        self.video_playing = False
        QMessageBox.warning(self, "视频播放失败", str(message))

    def show_frame_at(self, position_ms, prefer_sequential=False):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.video_cap is None:
            return

        frame_idx = int(position_ms / 1000 * self.video_fps)
        frame_idx = max(0, min(frame_idx, max(0, self.video_frame_count - 1)))

        if prefer_sequential and self.current_frame_idx >= 0:
            diff = frame_idx - self.current_frame_idx
            if diff == 0:
                return
            if 0 < diff <= 5:
                frame = None
                ret = False
                for _ in range(diff):
                    ret, frame = self.video_cap.read()
                    if not ret:
                        break
                if ret:
                    self.current_frame_idx = frame_idx
                    self.render_frame(frame)
                return

        self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = self.video_cap.read()
        if ret:
            self.current_frame_idx = frame_idx
            self.render_frame(frame)

    def render_frame(self, frame):
        """函数说明：执行当前函数对应的业务逻辑。"""
        target_size = self.video_area.contentsRect().size()
        target_width = target_size.width()
        target_height = target_size.height()
        if target_width <= 0 or target_height <= 0:
            target_width, target_height = 480, 260

        frame_height, frame_width = frame.shape[:2]
        scale = min(target_width / max(1, frame_width), target_height / max(1, frame_height))
        if scale < 1.0:
            resized_width = max(1, int(round(frame_width * scale)))
            resized_height = max(1, int(round(frame_height * scale)))
            frame = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_AREA)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, channels = rgb.shape
        bytes_per_line = channels * w
        image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap if scale < 1.0 else pixmap.scaled(
            QSize(target_width, target_height),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.video_area.setPixmap(scaled)

    def close_video_capture(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        self.play_timer.stop()
        self.video_playing = False
        self.stop_video_decoder(wait=True)
        if self.video_cap is not None:
            self.video_cap.release()
        self.video_cap = None
        self.loaded_video_path = ""

    def update_sync_position(self, position_ms, force_wave=False):
        """函数说明：执行当前函数对应的业务逻辑。"""
        position_ms = max(0, min(int(position_ms), self.duration_ms))
        self.playback_position_ms = position_ms
        self.update_timeline_cursor(position_ms)
        if force_wave or abs(position_ms - self.last_wave_update_ms) >= 100:
            self.wave.set_position(position_ms)
            if self.energy_axis is not None:
                self.energy_axis.set_position(position_ms)
            self.last_wave_update_ms = position_ms
        if force_wave or abs(position_ms - self.last_ui_update_ms) >= 100:
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(position_ms)
            self.position_slider.blockSignals(False)
            self.time_text.setText(f"{format_relative_ms(position_ms)} / {format_relative_ms(self.duration_ms)}")
            self.last_ui_update_ms = position_ms

    def update_timeline_cursor(self, position_ms=None):
        """让整体时间轴光标跟随视频和腕表的对齐位置。"""
        if self.timeline is None:
            return
        video_position_ms = self.current_position_ms() if position_ms is None else int(position_ms)
        offset_ms = self.wave.get_signal_offset() if self.wave is not None else 0
        cursor_duration_ms = self.current_data_duration_ms if self.raw_signals else self.duration_ms
        cursor_position_ms = video_position_ms + int(offset_ms)
        cursor_position_ms = max(0, min(cursor_position_ms, cursor_duration_ms))
        self.timeline.set_position(cursor_position_ms, cursor_duration_ms)

    def update_offset_label(self):
        """函数说明：执行当前函数对应的业务逻辑。"""
        if self.offset_label is None or self.wave is None:
            return
        current_offset_ms = int(self.wave.get_signal_offset())
        if current_offset_ms == self.last_displayed_offset_ms:
            return
        offset_s = current_offset_ms / 1000
        self.offset_label.setText(f"腕表相对视频偏移 {offset_s:+.1f}s")
        if self.energy_axis is not None:
            self.energy_axis.set_signal_offset(current_offset_ms)
        self.last_displayed_offset_ms = current_offset_ms
        self.update_timeline_cursor()

    def save_signal_offset(self, offset_ms, source="manual", result=None):
        """保存偏移量及其来源、置信度和调试窗口信息。"""
        if self.db is None or not self.app_state.get("project_id"):
            return
        result = result or {}
        project = self.db.update_project(
            self.app_state["project_id"],
            signal_offset_ms=int(offset_ms),
            signal_offset_source=str(source or "manual"),
            signal_offset_confidence=float(result.get("confidence", 0.0) or 0.0),
            signal_offset_reference_start=float(result.get("reference_start_seconds", 0.0) or 0.0),
            signal_offset_reference_end=float(result.get("reference_end_seconds", 0.0) or 0.0),
            signal_offset_search_start=float(result.get("search_start_seconds", 0.0) or 0.0),
            signal_offset_search_end=float(result.get("search_end_seconds", 0.0) or 0.0),
            signal_offset_template_start=float(result.get("template_start_seconds", 0.0) or 0.0),
            signal_offset_template_end=float(result.get("template_end_seconds", 0.0) or 0.0),
            signal_offset_algorithm_version="local-window-v1" if result else "manual",
            signal_offset_debug_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
        )
        self.app_state["project"] = project
        self.app_state["signal_offset_ms"] = int(project.get("signal_offset_ms") or 0)
        self.last_saved_signal_offset_ms = int(offset_ms)

    def format_ms(self, value):
        """函数说明：执行当前函数对应的业务逻辑。"""
        return format_watch_time(self.current_start_time_text, value)

