# -*- coding: utf-8 -*-
from .shared import *
from .database import AppDatabase
from .pages import LoginPage, ProjectListPage, ImportPage, VisualProjectPage, RecordPage, AnnotationPage


class MainWindow(QMainWindow):
    def __init__(self):
        """初始化主窗口、数据库和各业务页面。"""
        super().__init__()
        self.setWindowTitle("癫痫腕表数据标注平台")
        self.setMinimumSize(QSize(900, 620))
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(1180, int(available.width() * 0.92)), min(760, int(available.height() * 0.88)))
        else:
            self.resize(1180, 760)
        self.db = AppDatabase()
        self.app_state = {}
        self.load_current_project_state()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_page = LoginPage(self.show_project_page, self.db)
        self.project_page = ProjectListPage(
            self.show_import_page,
            self.show_record_page,
            self.show_annotation_page,
            self.db,
            self.open_project,
            self.open_project_for_import,
            self.open_project_for_record,
        )
        self.import_page = ImportPage(self.show_project_page, self.show_annotation_page, self.app_state, self.db)
        self.visual_page = VisualProjectPage(self.show_project_page, self.show_annotation_page)
        self.record_page = RecordPage(self.show_project_page, self.db, self.app_state)
        self.annotation_page = AnnotationPage(self.show_project_page, self.app_state, self.db)

        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.project_page)
        self.stack.addWidget(self.import_page)
        self.stack.addWidget(self.visual_page)
        self.stack.addWidget(self.record_page)
        self.stack.addWidget(self.annotation_page)
        self.stack.setCurrentIndex(0)

    def load_current_project_state(self):
        """从数据库读取当前项目，并同步到页面共享状态。"""
        project = self.db.get_current_project()
        self.app_state["project_id"] = project["id"]
        self.app_state["project"] = project
        self.app_state["video_path"] = project.get("video_path") or ""
        self.app_state["watch_path"] = project.get("watch_path") or ""
        self.app_state["signal_offset_ms"] = int(project.get("signal_offset_ms") or 0)

    def open_project(self, project_id):
        """把指定项目设为当前项目，并打开该项目的标注页。"""
        self.db.set_current_project(project_id)
        self.load_current_project_state()
        self.show_annotation_page()

    def open_project_for_import(self, project_id):
        """把指定项目设为当前项目，并打开该项目的数据导入页。"""
        self.db.set_current_project(project_id)
        self.load_current_project_state()
        self.import_page.restore_import_paths()
        self.stack.setCurrentIndex(2)

    def open_project_for_record(self, project_id):
        """把指定项目设为当前项目，并打开该项目的标注记录页。"""
        self.db.set_current_project(project_id)
        self.load_current_project_state()
        self.record_page.refresh_records()
        self.stack.setCurrentIndex(4)

    def show_project_page(self):
        """显示项目列表页。"""
        self.project_page.refresh_projects()
        self.stack.setCurrentIndex(1)

    def show_import_page(self):
        """显示当前项目的数据导入页。"""
        self.import_page.restore_import_paths()
        self.stack.setCurrentIndex(2)

    def show_visual_page(self):
        """显示可视化项目页。"""
        self.stack.setCurrentIndex(3)

    def show_record_page(self):
        """显示当前项目的标注记录页。"""
        self.record_page.refresh_records()
        self.stack.setCurrentIndex(4)

    def show_annotation_page(self):
        """显示当前项目的标注工作页。"""
        self.stack.setCurrentIndex(5)
        QTimer.singleShot(0, self.annotation_page.refresh_loaded_data)

    def closeEvent(self, event):
        """关闭窗口时释放数据库连接。"""
        if self.db is not None:
            self.db.close()
        super().closeEvent(event)
