# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
利用者のレイヤーを選ぶ選択欄（ゾーン統計量のポリゴンレイヤー・印刷タブの背景）を、レイヤーパネルの並びで出す部品。

QGIS の QgsMapLayerComboBox は候補を名前の順に並べ、並べ方を変える設定が無い。利用者は自分のレイヤーを
レイヤーパネルの並びで覚えているので、同じ並びで選べるようにする。候補は開くたびにその時点のレイヤーパネルの
並びで作り直す（プロジェクト全体のレイヤーの増減は見張らない方針のため、開いたとき・画面を合わせ直すときに作る）。
各タブが使っている QgsMapLayerComboBox の使い方（setFilters・setAllowEmptyLayer・setExceptedLayerList・
currentLayer・setLayer・layerChanged）をそのまま持つ。
"""

from qgis.PyQt.QtCore import QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox
from qgis.core import Qgis, QgsMapLayerProxyModel, QgsProject, QgsRasterLayer, QgsVectorLayer

try:
    from qgis.core import QgsIconUtils
except ImportError:  # 古い QGIS では、アイコンなしで並べる
    QgsIconUtils = None


EXTRA_ITEM = "__extra__"


class TreeOrderLayerComboBox(QComboBox):
    layerChanged = pyqtSignal(object)  # 選んだレイヤー（未選択なら None）
    extraChosen = pyqtSignal()  # setExtraItem の項目が選ばれた（選択は前のレイヤーに戻す）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter = None
        self._allow_empty = False
        self._empty_text = ""
        self._excepted_ids = set()
        self._current_id = None
        self._extra_text = ""
        self._extra_shown = None
        self._extra_activated = False
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.currentIndexChanged.connect(self._on_index_changed)
        self.activated.connect(self._on_activated)

    # ── QgsMapLayerComboBox と同じ使い方 ──
    def setFilters(self, layer_filter):
        self._filter = layer_filter
        self.refresh_layers()

    def setAllowEmptyLayer(self, allow, text=""):
        self._allow_empty = bool(allow)
        self._empty_text = text
        self.refresh_layers()

    def setExtraItem(self, text, shown=None):
        """「未選択」の次に、レイヤーではない項目（例：現在地の樹種メッシュを取得する）を置く。
        利用者が選んだときだけ extraChosen を出し、選択は前のレイヤーに戻す（呼び出し側がレイヤーを作って setLayer する）。
        レイヤーが消えたときなど、プログラムから選択が移っただけでは出さない（手動のときだけ作るため）。
        shown：項目を出すかを返す関数（候補を作り直すたびに呼ぶ。例：作ったレイヤーが無いときだけ出す）"""
        self._extra_text = text
        self._extra_shown = shown
        self.refresh_layers()

    def setExceptedLayerList(self, layers):
        self._excepted_ids = {layer.id() for layer in layers if layer is not None}
        self.refresh_layers()

    def currentLayer(self):
        layer_id = self.currentData()
        return QgsProject.instance().mapLayer(layer_id) if layer_id and layer_id != EXTRA_ITEM else None

    def setLayer(self, layer):
        if layer is not None and self.findData(layer.id()) < 0:
            self.refresh_layers()
        if layer is None:
            index = 0 if self._allow_empty else -1
        else:
            index = self.findData(layer.id())
            if index < 0:
                return
        self.setCurrentIndex(index)

    def showPopup(self):
        # 開くたびに、その時点のレイヤーパネルの並びで候補を作り直す
        self.refresh_layers()
        super().showPopup()

    # ── 候補の作り直し ──
    def refresh_layers(self):
        """レイヤーパネルの並び（上から、グループの中もたどる）で候補を作り直す。
        今のレイヤーが候補に残っていればそのまま、無くなっていれば未選択（または先頭）にし、変わったら合図を出す"""
        previous_id = self._current_id
        self.blockSignals(True)
        try:
            self.clear()
            if self._allow_empty:
                self.addItem(self._empty_text, None)
            if self._extra_text and (self._extra_shown is None or self._extra_shown()):
                self.addItem(self._extra_text, EXTRA_ITEM)
            for layer in self._layers_in_tree_order():
                if QgsIconUtils is not None:
                    self.addItem(QgsIconUtils.iconForLayer(layer), layer.name(), layer.id())
                else:
                    self.addItem(layer.name(), layer.id())
            index = self.findData(previous_id) if previous_id else -1
            if index < 0:
                # 前のレイヤーが無くなったら未選択（未選択が無ければ最初のレイヤー）。setExtraItem の項目には移さない
                index = next((i for i in range(self.count()) if self.itemData(i) != EXTRA_ITEM), -1)
            self.setCurrentIndex(index)
        finally:
            self.blockSignals(False)
        self._on_index_changed()

    def _layers_in_tree_order(self):
        root = QgsProject.instance().layerTreeRoot()
        layers = []
        for node in root.findLayers():
            layer = node.layer()
            if layer is not None and layer.id() not in self._excepted_ids and self._accepts(layer):
                layers.append(layer)
        return layers

    def _accepts(self, layer) -> bool:
        layer_filter = self._filter
        if layer_filter is None:
            return True
        if layer_filter == QgsMapLayerProxyModel.Filter.RasterLayer:
            return isinstance(layer, QgsRasterLayer)
        if layer_filter == QgsMapLayerProxyModel.Filter.VectorLayer:
            return isinstance(layer, QgsVectorLayer)
        if layer_filter == QgsMapLayerProxyModel.Filter.PolygonLayer:
            return isinstance(layer, QgsVectorLayer) and layer.geometryType() == Qgis.GeometryType.Polygon
        return True

    def _on_index_changed(self, *_):
        if self.currentData() == EXTRA_ITEM:
            # 選択を前のレイヤーに戻すのは、処理が一巡した後にする。この合図の後に出る「利用者が選んだ」の合図
            # （activated）は、その時点の選択の番号を渡すので、ここで戻すと選ばれたことがわからなくなる
            QTimer.singleShot(0, self._revert_extra)
            return
        layer = self.currentLayer()
        layer_id = layer.id() if layer is not None else None
        if layer_id != self._current_id:
            self._current_id = layer_id
            self.layerChanged.emit(layer)

    def _on_activated(self, index):
        if self.itemData(index) == EXTRA_ITEM:
            self._extra_activated = True

    def _revert_extra(self):
        """setExtraItem の項目から、選択を前のレイヤーに戻す。利用者が選んでいたときだけ extraChosen を出す"""
        if self.currentData() == EXTRA_ITEM:
            index = self.findData(self._current_id) if self._current_id else -1
            self.blockSignals(True)
            self.setCurrentIndex(index if index >= 0 else (0 if self._allow_empty else -1))
            self.blockSignals(False)
        if self._extra_activated:
            self._extra_activated = False
            self.extraChosen.emit()


def replace_with_tree_order_combo(dialog, name):
    """画面定義（.ui）の QgsMapLayerComboBox を、同じ場所・同じ名前の TreeOrderLayerComboBox に差し替える"""
    old = getattr(dialog, name)
    new = TreeOrderLayerComboBox(old.parentWidget())
    new.setObjectName(name)
    new.setSizePolicy(old.sizePolicy())
    new.setMinimumSize(old.minimumSize())
    new.setMaximumSize(old.maximumSize())
    new.setToolTip(old.toolTip())
    layout = old.parentWidget().layout() if old.parentWidget() is not None else None
    if layout is not None:
        layout.replaceWidget(old, new)
    old.hide()
    old.setParent(None)
    old.deleteLater()
    setattr(dialog, name, new)
    return new
