# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDialogButtonBox
)
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
from qgis.utils import iface

from . import dem_sources


class _ComboOkCancelFocusMixin:
    """選択欄1つと OK／キャンセルだけのダイアログで、キーボードのフォーカスを確実に動かす共通処理。
    （DEMブラウザで直した処理を、DEM解像度の選択でも使うため切り出した）
    使う側は self._focus_combo（選択欄）・self.button_box（OK／キャンセル）・self._focus_order（Tab で回る順）を用意する"""

    def showEvent(self, event):
        super().showEvent(event)
        # showEvent直後にQDialogButtonBox側がOKをdefault扱いへ戻すことがあるため、
        # 表示後のイベント処理に回してから初期フォーカスを確定させる。
        QTimer.singleShot(0, self._set_initial_focus)
        QTimer.singleShot(50, self._set_initial_focus)

    def _set_initial_focus(self):
        self._clear_button_defaults()
        self._focus_combo.setFocus()

    def _clear_button_defaults(self):
        for standard_button in (
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Cancel,
        ):
            button = self.button_box.button(standard_button)
            button.setAutoDefault(False)
            button.setDefault(False)

    def keyPressEvent(self, event):
        current = QApplication.focusWidget()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
            cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
            if current == ok_button:
                ok_button.click()
                return
            if current == cancel_button:
                cancel_button.click()
                return
            if current == self._focus_combo and not self._focus_combo.view().isVisible():
                self._focus_combo.showPopup()
                return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, next):
        current = QApplication.focusWidget()
        if current in self._focus_order:
            self._clear_button_defaults()
            step = 1 if next else -1
            idx = (self._focus_order.index(current) + step) % len(self._focus_order)
            reason = Qt.FocusReason.TabFocusReason if next else Qt.FocusReason.BacktabFocusReason
            self._focus_order[idx].setFocus(reason)
            return True
        return super().focusNextPrevChild(next)


class DemBrowserDialog(_ComboOkCancelFocusMixin, QDialog):
    """
    DEM取得元を選択して取得するダイアログ。

    取得元の一覧・出典の説明・対象地域の判定・取得処理は dem_sources パッケージ側にあり、
    このダイアログは表示と選択だけを受け持つ。取得元を増やすときは dem_sources を参照。
    表示範囲が対象地域に収まらない取得元は、一覧に出したうえで選べない状態にする。
    """

    # 詳細なDEMを選べば結果が良くなる、と受け取られないための常設の注意書き。
    # 10mへのリサンプリング条件は utils.is_resampling_needed / constants.PIXELS_THRESHOLD_RESAMPLING
    NOTE_TEXT = (
        "<b>細かいDEMはゾーニングに有利とは限りません</b><br>"
        "要素計算は原版と同じく10m格子を基本にしています。5mのDEMは常に、"
        "1mのDEMも範囲が約25km²（5km四方）を超えると、計算前に10mへリサンプリングされます。"
        "リサンプリングされない狭い範囲では、細かいDEMは小さな凹凸まで拾うため、"
        "傾斜や地形の複雑さの値が10mのDEMとは変わり、スコアのしきい値との関係がずれることがあります。"
        "取得と計算の時間も大きく増えます。迷ったら国土地理院のDEMを選んでください。"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DEMブラウザ")
        self.setMinimumWidth(520)

        # モーダル表示中はキャンバスを動かせないため、範囲は開いた時点で確定させる
        self._extent = self._canvas_extent_wgs84()
        extent_tuple = (
            self._extent.xMinimum(), self._extent.yMinimum(),
            self._extent.xMaximum(), self._extent.yMaximum(),
        )
        self._sources = dem_sources.all_sources()
        self._problems = [source.coverage_problem(extent_tuple) for source in self._sources]
        self._extent_tuple = extent_tuple

        layout = QVBoxLayout(self)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("取得元:"))
        self.sourceCombobox = QComboBox()
        self.sourceCombobox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for source, problem in zip(self._sources, self._problems):
            self.sourceCombobox.addItem(
                source.label if problem is None else f"{source.label}（{problem}）"
            )
            if problem is not None:
                # 既定モデル(QStandardItemModel)の項目を無効化して選べないようにする
                self.sourceCombobox.model().item(self.sourceCombobox.count() - 1).setEnabled(False)
        self.sourceCombobox.currentIndexChanged.connect(self._update_info_label)
        source_row.addWidget(self.sourceCombobox, 1)
        layout.addLayout(source_row)

        self.infoLabel = QLabel()
        self.infoLabel.setWordWrap(True)
        self.infoLabel.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.infoLabel.setStyleSheet("color: #444; padding: 4px;")
        layout.addWidget(self.infoLabel)

        self.noteLabel = QLabel(self.NOTE_TEXT)
        self.noteLabel.setWordWrap(True)
        self.noteLabel.setTextFormat(Qt.TextFormat.RichText)
        self.noteLabel.setStyleSheet(
            "color: #5a4500; background: #fff6d6; border: 1px solid #e0c870; padding: 6px;"
        )
        layout.addWidget(self.noteLabel)

        self.extentLabel = QLabel()
        self.extentLabel.setWordWrap(True)
        layout.addWidget(self.extentLabel)
        self._update_extent_label()

        # 先頭（国土地理院）は常に選べるが、念のため選べる最初の項目に合わせる
        first_available = next(i for i, problem in enumerate(self._problems) if problem is None)
        self.sourceCombobox.setCurrentIndex(first_available)
        self._update_info_label()

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        # default/autoDefaultの強調は実フォーカスとは別に残ることがあり紛らわしい。
        # フォーカス枠そのものは「いまキーボードで操作される対象」として残す。
        for button in (ok_button, cancel_button):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._focus_combo = self.sourceCombobox
        self._focus_order = [self.sourceCombobox, ok_button, cancel_button]

    def _update_info_label(self):
        source = self.get_selected_source()
        text = source.description
        estimate = source.estimate(self._extent_tuple)
        if estimate:
            text += f"\n\n{estimate}"
        self.infoLabel.setText(text)

    def _update_extent_label(self):
        extent = self._extent
        lines = [
            "取得範囲（現在のQGIS地図表示範囲）:",
            f"経度 {extent.xMinimum():.5f} 〜 {extent.xMaximum():.5f}",
            f"緯度 {extent.yMinimum():.5f} 〜 {extent.yMaximum():.5f}",
        ]
        if any(problem is not None for problem in self._problems):
            lines.append("※ 表示範囲がデータの対象地域に収まらない取得元は選べません。")
        self.extentLabel.setText("\n".join(lines))

    @staticmethod
    def _canvas_extent_wgs84():
        canvas = iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(canvas_crs, wgs84_crs, QgsProject.instance())
        return transform.transformBoundingBox(canvas.extent())

    def get_extent_wgs84(self):
        """ダイアログを開いた時点の表示範囲（WGS84、QgsRectangle）"""
        return self._extent

    def get_selected_source(self):
        """選択された取得元（dem_sources.base.DemSource）"""
        return self._sources[self.sourceCombobox.currentIndex()]


class DemResolutionDialog(_ComboOkCancelFocusMixin, QDialog):
    """国土地理院DEMで、取得範囲全体をカバーできる解像度が複数あるときに1つを選ぶダイアログ。
    Qt 標準の入力ダイアログ（QInputDialog.getItem）ではキーボードのフォーカスが動かなかったため、
    DEMブラウザと同じ作りにしている"""

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DEM解像度の選択")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "複数のDEMタイル方式が取得範囲全体をカバーしています。\n"
            "解析範囲内で統一して使用する解像度を選択してください。"
        ))
        self.resolutionCombobox = QComboBox()
        self.resolutionCombobox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resolutionCombobox.addItems(labels)
        layout.addWidget(self.resolutionCombobox)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        ok_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        for button in (ok_button, cancel_button):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._focus_combo = self.resolutionCombobox
        self._focus_order = [self.resolutionCombobox, ok_button, cancel_button]

    def selected_label(self) -> str:
        return self.resolutionCombobox.currentText()
