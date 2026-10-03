# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# QGIS-API
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import QgsMapLayerProxyModel, QgsProject

from . import processes
from . import utils
from .settings_manager import PrintlayoutBackgroundManager
from .constants import (
    OUTPUT_ZONING,
    OUTPUT_AGGREGATE,
)


class ForestZoningMainDialogPrintlayout:
    """
    メイン画面の「印刷」タブの処理を実装するクラス
    """

    def __init__(self, main):
        self.main = main
        self.init_printlayout_ui()

    def init_printlayout_ui(self):
        # connect signals
        self.main.zoningPrintlayoutSetLayersButton.clicked.connect(
            self.set_zoning_layer_printlayout_combobox
        )
        self.main.aggregatePrintlayoutSetLayersButton.clicked.connect(
            self.set_aggregate_layer_printlayout_combobox
        )
        self.main.createZoningPrintlayoutPushButton.clicked.connect(
            lambda: self.run_printlayout("zoning")
        )
        self.main.createAggregatePrintlayoutPushButton.clicked.connect(
            lambda: self.run_printlayout("aggregate")
        )

        self.main.printlayoutBackgroundLayerCombobox.setFilters(
            QgsMapLayerProxyModel.Filter.RasterLayer
        )
        self.main.printlayoutZoningLayerCombobox.setFilters(
            QgsMapLayerProxyModel.Filter.RasterLayer
        )
        self.main.printlayoutAggregateLayerCombobox.setFilters(
            QgsMapLayerProxyModel.Filter.VectorLayer
        )
        self.update_printlayout_layer_scope()

        self.main.printlayoutBackgroundLayerCombobox.layerChanged.connect(
            self.refresh_create_zoning_printlayout_ui
        )
        self.main.printlayoutBackgroundLayerCombobox.layerChanged.connect(
            self.refresh_create_aggregate_printlayout_ui
        )
        self.main.printlayoutZoningLayerCombobox.layerChanged.connect(
            self.refresh_create_zoning_printlayout_ui
        )
        self.main.printlayoutAggregateLayerCombobox.layerChanged.connect(
            self.refresh_create_aggregate_printlayout_ui
        )
        self.background_manager = PrintlayoutBackgroundManager()
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.setChecked(
            self.background_manager.load_network_only()
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.background_manager.store_network_only
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.refresh_create_zoning_printlayout_ui
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.refresh_create_aggregate_printlayout_ui
        )
        self.refresh_create_zoning_printlayout_ui()
        self.refresh_create_aggregate_printlayout_ui()

    @staticmethod
    def _set_layers_button_enabled(button, combobox, default_layer):
        """既定のレイヤーが入っているとき（押しても変わらないとき）は、再読込ボタンをグレーアウトする"""
        button.setEnabled(default_layer is not None and combobox.currentLayer() is not default_layer)

    def refresh_create_zoning_printlayout_ui(self):
        self.update_printlayout_layer_scope()
        self._set_layers_button_enabled(
            self.main.zoningPrintlayoutSetLayersButton,
            self.main.printlayoutZoningLayerCombobox,
            self._default_zoning_layer(),
        )
        error_texts = self.get_create_zoning_printlayout_error()
        has_no_error = len(error_texts) == 0
        self.main.createZoningPrintlayoutErrorLabel.setText("\n".join(error_texts))
        self.main.createZoningPrintlayoutPushButton.setEnabled(has_no_error)

    def get_create_zoning_printlayout_error(self) -> list:
        error_texts = []
        if self.main.printlayoutBackgroundLayerCombobox.currentLayer() is None:
            error_texts.append("背景レイヤを指定してください")
        if self.main.printlayoutZoningLayerCombobox.currentLayer() is None:
            error_texts.append("ゾーニング図を指定してください")
        return error_texts

    def refresh_create_aggregate_printlayout_ui(self):
        self.update_printlayout_layer_scope()
        self._set_layers_button_enabled(
            self.main.aggregatePrintlayoutSetLayersButton,
            self.main.printlayoutAggregateLayerCombobox,
            self._default_aggregate_layer(),
        )
        error_texts = self.get_create_aggregate_printlayout_error()
        has_no_error = len(error_texts) == 0
        self.main.createAggregatePrintlayoutErrorLabel.setText("\n".join(error_texts))
        self.main.createAggregatePrintlayoutPushButton.setEnabled(has_no_error)

    def get_create_aggregate_printlayout_error(self) -> list:
        error_texts = []
        if self.main.printlayoutBackgroundLayerCombobox.currentLayer() is None:
            error_texts.append("背景レイヤを指定してください")
        if self.main.printlayoutAggregateLayerCombobox.currentLayer() is None:
            error_texts.append("ゾーン統計量を指定してください")
        return error_texts

    @staticmethod
    def _default_zoning_layer():
        return utils.find_morizon_layer_by_name(
            OUTPUT_ZONING.get("DISPLAY_NAME"),
            allowed_extensions={".tif", ".tiff"},
        )

    @staticmethod
    def _default_aggregate_layer():
        return utils.find_morizon_layer_by_name(
            OUTPUT_AGGREGATE.get("DISPLAY_NAME"),
            allowed_extensions={".shp", ".gpkg"},
        )

    def set_zoning_layer_printlayout_combobox(self):
        self.update_printlayout_layer_scope()
        zoning_layer = self._default_zoning_layer()
        if zoning_layer is not None:
            self.main.printlayoutZoningLayerCombobox.setLayer(zoning_layer)
        else:
            QMessageBox.information(self.main, "エラー", "ゾーニング図を作成してください。")
            return

    def set_aggregate_layer_printlayout_combobox(self):
        self.update_printlayout_layer_scope()
        aggregate_layer = self._default_aggregate_layer()
        if aggregate_layer is not None:
            self.main.printlayoutAggregateLayerCombobox.setLayer(aggregate_layer)
        else:
            QMessageBox.information(self.main, "エラー", "ゾーン統計量を作成してください。")
            return

    def update_printlayout_layer_scope(self):
        utils.set_morizon_layer_scope(
            self.main.printlayoutZoningLayerCombobox,
            allowed_names={OUTPUT_ZONING.get("DISPLAY_NAME")},
            allowed_extensions={".tif", ".tiff"},
        )
        utils.set_morizon_layer_scope(
            self.main.printlayoutAggregateLayerCombobox,
            allowed_names={OUTPUT_AGGREGATE.get("DISPLAY_NAME")},
            allowed_extensions={".shp", ".gpkg"},
        )
        # 背景：「ネットワーク経由のレイヤーに候補を絞る」なら、ファイル等の手元のレイヤーを候補から外す
        excepted = []
        if self.main.printlayoutBackgroundNetworkOnlyCheckBox.isChecked():
            excepted = [
                layer for layer in QgsProject.instance().mapLayers().values()
                if not self._is_network_layer(layer)
            ]
        self.main.printlayoutBackgroundLayerCombobox.setExceptedLayerList(excepted)

    @staticmethod
    def _is_network_layer(layer) -> bool:
        """WMS/WMTS/XYZタイル・WCS・ArcGIS等のサービスや、URLを直接読んでいるレイヤーか"""
        if layer.providerType() in ("wms", "wcs", "arcgismapserver"):
            return True
        source = layer.source().lower()
        return source.startswith(("http://", "https://", "/vsicurl/")) or "url=http" in source

    def run_printlayout(self, target_name):
        project = QgsProject.instance()
        manager = project.layoutManager()
        printlayout_list = [layout.name() for layout in manager.printLayouts()]
        background_layer = self.main.printlayoutBackgroundLayerCombobox.currentLayer()

        if target_name == "zoning":
            target_layer = self.main.printlayoutZoningLayerCombobox.currentLayer()
            printlayout_name = "ゾーニング図"
        else:
            target_layer = self.main.printlayoutAggregateLayerCombobox.currentLayer()
            printlayout_name = "ゾーン統計量"

        if printlayout_name in printlayout_list:
            if QMessageBox.StandardButton.No == QMessageBox.question(
                self.main,
                "上書き確認",
                f'出力先フォルダに"{printlayout_name}"のレイアウトが存在します、上書きしますか？',
                QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.No,
            ):
                QMessageBox.information(self.main, "処理中断", "処理を中断しました。")
                return
            project.layoutManager().removeLayout(manager.layoutByName(printlayout_name))

        processes.printlayout.create_printlayout.generate(
            target_name, background_layer, target_layer
        )
