# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
/***************************************************************************
 ForestZoningPlugin

                              -------------------
        begin                : 2021-06-30
        git sha              : $Format:%H$
        copyright            : (C) 2021 by MIERUNE Inc.
        email                : info@mierune.co.jp
        license              : GNU General Public License v2.0
 ***************************************************************************/
"""

import os

# QGIS-API
from qgis.PyQt import uic
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *


class ProgressDialog(QDialog):
    def __init__(self, set_abort_flag_callback):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "progress_dialog.ui"), self
        )
        self.setWindowIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "imgs", "icon.png"))
        )

        self.set_abort_flag_callback = set_abort_flag_callback
        self.init_ui()

    def init_ui(self):
        self.label.setText("処理開始中...")
        self.detailLabel.setText("")
        self.progressBar.setValue(0)
        self.progressBar.setMaximum(0)
        self.abortButton.setEnabled(True)
        self.abortButton.setText("中断")
        self.abortButton.clicked.connect(self.on_abort_click)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            return
        super().keyPressEvent(event)

    def on_abort_click(self):
        if QMessageBox.StandardButton.Yes == QMessageBox.question(
            self, "確認", "処理を中断し、以降の処理をスキップしてよろしいですか？", QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
        ):
            if self.abortButton.isEnabled():  # 中断可能な場合のみ中断イベントを発火させる
                self.set_abort_flag_callback(True)
                self.abortButton.setEnabled(False)
                self.abortButton.setText("中断待機中...")

    def set_sum_of_processes(self, value: int):
        self.progressBar.setMaximum(value)

    def add_progress(self, value: int):
        self.progressBar.setValue(self.progressBar.value() + value)

    def set_progress(self, value: int):
        self.progressBar.setValue(value)

    def set_messsage(self, message: str):
        self.label.setText(message + "...")
        self.adjustSize()

    def set_detail(self, detail: str):
        self.detailLabel.setText(detail)
        self.adjustSize()

    def set_abortable(self, abortable=True):
        self.abortButton.setEnabled(abortable)
