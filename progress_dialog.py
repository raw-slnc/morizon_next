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
import sys
from datetime import datetime

# QGIS-API
from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon, QKeyEvent
from qgis.PyQt.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QSizePolicy


class _StatusLine(QLabel):
    """1行だけのステータス表示。幅に収まらない分は末尾を「…」で省略する（窓の幅は広げない）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setStyleSheet("color:#666;")

    def setText(self, text):
        self._full_text = text or ""
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(self.width(), 0)
        ))


class ProgressDialog(QDialog):
    def __init__(self, set_abort_flag_callback):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        if sys.platform.startswith("win"):
            # Windows では、処理の途中で外部のプログラム（GRASS・GDAL など）のコマンドプロンプトが手前に開き、
            # この窓を隠してしまう。常に手前に出して、コマンドプロンプトは後ろに回す
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "progress_dialog.ui"), self
        )
        self._init_log_line()
        self.setWindowIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "imgs", "icon.png"))
        )

        self.set_abort_flag_callback = set_abort_flag_callback
        self.init_ui()

    def init_ui(self):
        self.label.setText("処理開始中...")
        self.detailLabel.setText("")
        self.detailLabel.setVisible(False)  # 中身が空のときは隠す（空でも1行分の高さを取り、行間が空いて見えるため）
        self.progressBar.setValue(0)
        self.progressBar.setMaximum(0)
        self.abortButton.setEnabled(True)
        self.abortButton.setText("中断")
        self.abortButton.clicked.connect(self.on_abort_click)

    def exec(self):
        """閉じたあと、窓を消して下の画面を描き直してから戻る。
        戻ってすぐ次の知らせ（終了・エラーなど）を出すと、閉じた窓の絵が描き直されずに残像として残るため"""
        result = super().exec()
        self.hide()
        QApplication.processEvents()
        return result

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
        self.append_log(message)
        self.adjustSize()

    # ── ログのステータス表示 ──────────────────────────────────────────────
    # 動いていることを見せるため、工程の開始メッセージと、Processing・外部プログラムの
    # 記録のうち最新の1行だけを時刻付きで出す（読ませるためではないので、積み上げない）。
    # 詳細ログは、実行中の処理スレッドの postLog から受け取る。
    #
    # 保守メモ:
    # 以前は QgsApplication.messageLog().messageReceived を監視していたが、外部コマンドの
    # stdout は Linux では届いても Windows では届かない場合があった。また、全体ログの
    # 監視では他プラグインのメッセージまで表示する。このため、processing.run() に渡した
    # QgsProcessingFeedback から、このダイアログ専用の postLog で直接受け取る形にしている。

    def _init_log_line(self):
        self.logLine = _StatusLine()
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.detailLabel) + 1, self.logLine)

    def append_log(self, text: str):
        self.logLine.setText(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    def set_detail(self, detail: str):
        self.detailLabel.setText(detail)
        self.detailLabel.setVisible(bool(detail))
        self.adjustSize()

    def set_abortable(self, abortable=True):
        self.abortButton.setEnabled(abortable)


def run_with_progress(thread, show_detail=False, show_set_progress=False, abortable=True) -> dict:
    """処理スレッドを進捗ダイアログ付きで実行し、終わったら {"result": 結果} か {"error": メッセージ} を返す。
    結果の反映（レイヤーの追加）や知らせは、呼び出し側がこの関数から戻った後に行うこと。
    進捗の窓が開いている間に行うと、閉じた窓の絵が描き直されずに残像として残るため、
    ここで窓を消して下の画面を描き直してから戻る"""
    outcome = {}
    progress_dialog = ProgressDialog(thread.set_abort_flag)
    if not abortable:
        progress_dialog.set_abortable(False)
    thread.processStarted.connect(progress_dialog.set_sum_of_processes)
    thread.addProgress.connect(progress_dialog.add_progress)
    if show_set_progress:
        thread.setProgress.connect(progress_dialog.set_progress)
    thread.postMessage.connect(progress_dialog.set_messsage)
    # postLog は Processing の詳細ログを持つ計算スレッドだけが実装する。
    # 取得・保存など従来のスレッドには無いので、後方互換のため存在確認して接続する。
    if hasattr(thread, "postLog"):
        thread.postLog.connect(progress_dialog.append_log)
    if show_detail:
        thread.postDetail.connect(progress_dialog.set_detail)
    thread.setAbortable.connect(progress_dialog.set_abortable)
    thread.processFinished.connect(lambda result: outcome.update(result=result))
    thread.processFinished.connect(progress_dialog.close)
    thread.processFailed.connect(lambda message: outcome.update(error=message))
    thread.processFailed.connect(progress_dialog.close)
    thread.start()
    progress_dialog.exec()
    thread.wait()
    progress_dialog.hide()
    progress_dialog.deleteLater()
    QApplication.processEvents()
    return outcome
