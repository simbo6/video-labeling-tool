# -*- coding: utf-8 -*-
import sys

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from app import APP_QSS, MainWindow


def run():
    """函数说明：执行当前函数对应的业务逻辑。"""
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    app.setFont(QFont("Microsoft YaHei", 10))

    window = MainWindow()
    window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(run())
