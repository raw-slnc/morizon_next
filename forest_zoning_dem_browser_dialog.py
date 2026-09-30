from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDialogButtonBox
)
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
from qgis.utils import iface

from .dem_loader import GSITileDEMLoader


class DemBrowserDialog(QDialog):
    """
    DEM取得元を選択して取得するダイアログ。

    現状は国土地理院(GSI)・AWS Terrariumのみに対応。静岡・長野等の地域別高解像度ソースは
    forestry_operations_lite側(vs_lp.py/nagano_sabo.py/nagano_rinmu.py/nagano_dchm.py)に
    実装があるが、まだ本プラグインには移植していない（別途追加予定）。

    出典・ライセンス表記はforestry_operations_liteのDemBrowserDialog記載内容を踏襲。
    """

    # (表示名, 出典・ライセンス・カバレッジの説明)
    SOURCES = [
        (
            "国土地理院 DEM1A/5A/10B（1m→5m→10mの順で自動フォールバック）",
            "出典：国土地理院（地理院タイル DEM1A/DEM5A/DEM10B）。利用の際は出典の明示が必要です。\n"
            "・DEM1A 1m：航空レーザ測量。測量実施エリアのみ（伊豆半島・山間部等）\n"
            "・DEM5A 5m：標準解像度、全国で利用可能\n"
            "・DEM10B 10m：広域解析向け、全国で利用可能\n"
            "取得範囲内でDEM1Aが無ければ自動的にDEM5A、それも無ければDEM10Bにフォールバックします。\n"
            "実際どの解像度が取得されたかは処理完了後に表示します。\n"
            "正式な利用規約は国土地理院の該当ページでご確認ください。"
        ),
        (
            "AWS Terrarium（全球、登録不要）",
            "出典：AWS Terrain Tiles (Mapzen Terrarium)。全球カバー、無料・登録不要。\n"
            "国土地理院データが取得できない場合のフォールバック用途を想定。\n"
            "正式な利用条件はAWS Terrain Tilesの配布元でご確認ください。"
        ),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DEMブラウザ")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("取得元:"))
        self.sourceCombobox = QComboBox()
        self.sourceCombobox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sourceCombobox.addItems([label for label, _ in self.SOURCES])
        self.sourceCombobox.currentIndexChanged.connect(self._update_info_label)
        source_row.addWidget(self.sourceCombobox)
        layout.addLayout(source_row)

        self.infoLabel = QLabel()
        self.infoLabel.setWordWrap(True)
        self.infoLabel.setStyleSheet("color: #444; padding: 4px;")
        layout.addWidget(self.infoLabel)
        self._update_info_label()

        self.extentLabel = QLabel()
        self.extentLabel.setWordWrap(True)
        layout.addWidget(self.extentLabel)
        self._update_extent_label()

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
        self._focus_order = [self.sourceCombobox, ok_button, cancel_button]

    def showEvent(self, event):
        super().showEvent(event)
        # showEvent直後にQDialogButtonBox側がOKをdefault扱いへ戻すことがあるため、
        # 表示後のイベント処理に回してから初期フォーカスを確定させる。
        QTimer.singleShot(0, self._set_initial_focus)
        QTimer.singleShot(50, self._set_initial_focus)

    def _set_initial_focus(self):
        self._clear_button_defaults()
        self.sourceCombobox.setFocus()

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
            if current == self.sourceCombobox and not self.sourceCombobox.view().isVisible():
                self.sourceCombobox.showPopup()
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

    def _update_info_label(self):
        _, info_text = self.SOURCES[self.sourceCombobox.currentIndex()]
        self.infoLabel.setText(info_text)

    def _update_extent_label(self):
        extent = self.get_extent_wgs84()
        self.extentLabel.setText(
            "取得範囲（現在のQGIS地図表示範囲）:\n"
            f"経度 {extent.xMinimum():.5f} 〜 {extent.xMaximum():.5f}\n"
            f"緯度 {extent.yMinimum():.5f} 〜 {extent.yMaximum():.5f}"
        )

    def get_extent_wgs84(self):
        canvas = iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(canvas_crs, wgs84_crs, QgsProject.instance())
        return transform.transformBoundingBox(canvas.extent())

    def get_selected_sources(self):
        """
        選択された取得元に対応するタイルソースリスト
        （dem_loader.GSITileDEMLoader.fetch_for_extentのsources引数にそのまま渡せる形式）を返す
        """
        if self.sourceCombobox.currentIndex() == 0:
            return GSITileDEMLoader.TILE_SOURCES
        return GSITileDEMLoader.TERRARIUM_SOURCES
