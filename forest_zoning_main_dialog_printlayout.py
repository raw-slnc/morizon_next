# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# QGIS-API
from qgis.PyQt.QtCore import Qt
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
        self._tighten_printlayout_background_controls()

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
        self.main.printlayoutBackgroundSubLayerCombobox.setFilters(
            QgsMapLayerProxyModel.Filter.VectorLayer
        )
        self.main.printlayoutBackgroundSubLayerCombobox.setAllowEmptyLayer(True, "未選択")
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
        self.main.printlayoutBackgroundSubLayerCombobox.layerChanged.connect(
            self.refresh_create_zoning_printlayout_ui
        )
        self.main.printlayoutBackgroundSubLayerCombobox.layerChanged.connect(
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
        self.update_printlayout_layer_scope()
        self._load_background_layer_settings()
        self._load_background_option_settings()
        self.main.printlayoutBackgroundLayerCombobox.layerChanged.connect(
            lambda layer: self.background_manager.store_layer_ref(
                PrintlayoutBackgroundManager.BACKGROUND_MAIN_LAYER_KEY,
                layer,
                store_none=False,
            )
        )
        self.main.printlayoutBackgroundSubLayerCombobox.layerChanged.connect(
            lambda layer: self.background_manager.store_layer_ref(
                PrintlayoutBackgroundManager.BACKGROUND_SUB_LAYER_KEY, layer
            )
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.background_manager.store_network_only
        )
        # チェックを変えたら、背景の除外を作り直す（利用者の操作なので、その場で作り直してよい）
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            lambda _checked: self.update_printlayout_layer_scope()
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.refresh_create_zoning_printlayout_ui
        )
        self.main.printlayoutBackgroundNetworkOnlyCheckBox.toggled.connect(
            self.refresh_create_aggregate_printlayout_ui
        )
        for checkbox in (
            self.main.zoningPrintlayoutUseSubBackgroundCheckBox,
        ):
            checkbox.toggled.connect(self.refresh_create_zoning_printlayout_ui)
            checkbox.toggled.connect(
                lambda checked: self.background_manager.store_bool(
                    PrintlayoutBackgroundManager.ZONING_USE_SUB_KEY, checked
                )
            )
        for checkbox in (
            self.main.aggregatePrintlayoutUseSubBackgroundCheckBox,
        ):
            checkbox.toggled.connect(self.refresh_create_aggregate_printlayout_ui)
            checkbox.toggled.connect(
                lambda checked: self.background_manager.store_bool(
                    PrintlayoutBackgroundManager.AGGREGATE_USE_SUB_KEY, checked
                )
            )
        self.main.zoningPrintlayoutSubOpacitySpinBox.valueChanged.connect(
            lambda value: self.background_manager.store_int(
                PrintlayoutBackgroundManager.ZONING_SUB_OPACITY_KEY, value
            )
        )
        self.main.zoningPrintlayoutMainOpacitySpinBox.valueChanged.connect(
            lambda value: self.background_manager.store_int(
                PrintlayoutBackgroundManager.ZONING_MAIN_OPACITY_KEY, value
            )
        )
        self.main.aggregatePrintlayoutSubOpacitySpinBox.valueChanged.connect(
            lambda value: self.background_manager.store_int(
                PrintlayoutBackgroundManager.AGGREGATE_SUB_OPACITY_KEY, value
            )
        )
        self.main.aggregatePrintlayoutMainOpacitySpinBox.valueChanged.connect(
            lambda value: self.background_manager.store_int(
                PrintlayoutBackgroundManager.AGGREGATE_MAIN_OPACITY_KEY, value
            )
        )
        self.refresh_create_zoning_printlayout_ui()
        self.refresh_create_aggregate_printlayout_ui()

    def _load_background_layer_settings(self):
        self._restore_background_layer(
            self.main.printlayoutBackgroundLayerCombobox,
            PrintlayoutBackgroundManager.BACKGROUND_MAIN_LAYER_KEY,
        )
        self._restore_background_layer(
            self.main.printlayoutBackgroundSubLayerCombobox,
            PrintlayoutBackgroundManager.BACKGROUND_SUB_LAYER_KEY,
        )

    def _restore_background_layer(self, combobox, key: str):
        layer_ref = self.background_manager.load_layer_ref(key)
        if not layer_ref:
            return
        layer = self._find_project_layer(layer_ref)
        if layer is not None:
            combobox.setLayer(layer)

    @staticmethod
    def _find_project_layer(layer_ref: dict):
        layers = list(QgsProject.instance().mapLayers().values())
        layer_id = layer_ref.get('id')
        if layer_id:
            for layer in layers:
                if layer.id() == layer_id:
                    return layer

        name = layer_ref.get('name')
        source = layer_ref.get('source')
        provider = layer_ref.get('provider')
        for layer in layers:
            if (
                layer.name() == name
                and layer.source() == source
                and layer.providerType() == provider
            ):
                return layer
        for layer in layers:
            if layer.name() == name and layer.providerType() == provider:
                return layer
        return None

    def _load_background_option_settings(self):
        self.main.zoningPrintlayoutUseSubBackgroundCheckBox.setChecked(
            self.background_manager.load_bool(
                PrintlayoutBackgroundManager.ZONING_USE_SUB_KEY, False
            )
        )
        self.main.zoningPrintlayoutSubOpacitySpinBox.setValue(
            self.background_manager.load_int(
                PrintlayoutBackgroundManager.ZONING_SUB_OPACITY_KEY, 100
            )
        )
        self.main.zoningPrintlayoutMainOpacitySpinBox.setValue(
            self.background_manager.load_int(
                PrintlayoutBackgroundManager.ZONING_MAIN_OPACITY_KEY, 100
            )
        )
        self.main.aggregatePrintlayoutUseSubBackgroundCheckBox.setChecked(
            self.background_manager.load_bool(
                PrintlayoutBackgroundManager.AGGREGATE_USE_SUB_KEY, False
            )
        )
        self.main.aggregatePrintlayoutSubOpacitySpinBox.setValue(
            self.background_manager.load_int(
                PrintlayoutBackgroundManager.AGGREGATE_SUB_OPACITY_KEY, 100
            )
        )
        self.main.aggregatePrintlayoutMainOpacitySpinBox.setValue(
            self.background_manager.load_int(
                PrintlayoutBackgroundManager.AGGREGATE_MAIN_OPACITY_KEY, 100
            )
        )

    def _tighten_printlayout_background_controls(self):
        placeholder = getattr(self.main, "printlayoutBackgroundSubNetworkPlaceholder", None)
        if placeholder is not None:
            placeholder.setFixedWidth(
                self.main.printlayoutBackgroundNetworkOnlyCheckBox.sizeHint().width()
            )

        align_center = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignHCenter")
        for layout_name in ("gridLayout_10", "gridLayout_8"):
            layout = getattr(self.main, layout_name, None)
            if layout is not None:
                layout.setAlignment(align_center)

        for checkbox in (
            self.main.zoningPrintlayoutUseMainBackgroundCheckBox,
            self.main.aggregatePrintlayoutUseMainBackgroundCheckBox,
        ):
            checkbox.setChecked(True)
            checkbox.setEnabled(False)

        for widget in (
            self.main.zoningPrintlayoutBackgroundOptionsWidget,
            self.main.aggregatePrintlayoutBackgroundOptionsWidget,
        ):
            layout = widget.layout()
            if layout is None:
                continue
            layout.activate()
            widget.setFixedWidth(layout.sizeHint().width())

    @staticmethod
    def _set_layers_button_enabled(button, combobox, default_layer):
        """既定のレイヤーが入っているとき（押しても変わらないとき）は、再読込ボタンをグレーアウトする"""
        button.setEnabled(default_layer is not None and combobox.currentLayer() is not default_layer)

    def refresh_create_zoning_printlayout_ui(self):
        # 候補の作り直しはしない（選択欄の合図の中から呼ばれるため。合わせ直しはメイン画面の refresh_all_tabs）
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
            error_texts.append("基本背景を指定してください")
        if (
            self.main.zoningPrintlayoutUseSubBackgroundCheckBox.isChecked()
            and self.main.printlayoutBackgroundSubLayerCombobox.currentLayer() is None
        ):
            error_texts.append("背景補助を指定してください")
        if self.main.printlayoutZoningLayerCombobox.currentLayer() is None:
            error_texts.append("ゾーニング図を指定してください")
        return error_texts

    def refresh_create_aggregate_printlayout_ui(self):
        # 候補の作り直しはしない（選択欄の合図の中から呼ばれるため。合わせ直しはメイン画面の refresh_all_tabs）
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
            error_texts.append("基本背景を指定してください")
        if (
            self.main.aggregatePrintlayoutUseSubBackgroundCheckBox.isChecked()
            and self.main.printlayoutBackgroundSubLayerCombobox.currentLayer() is None
        ):
            error_texts.append("背景補助を指定してください")
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
        background_main_layer = self.main.printlayoutBackgroundLayerCombobox.currentLayer()
        background_sub_layer = self.main.printlayoutBackgroundSubLayerCombobox.currentLayer()

        if target_name == "zoning":
            target_layer = self.main.printlayoutZoningLayerCombobox.currentLayer()
            printlayout_name = "ゾーニング図"
            background_layers = self._background_layers(
                use_sub=self.main.zoningPrintlayoutUseSubBackgroundCheckBox.isChecked(),
                sub_layer=background_sub_layer,
                sub_opacity=self.main.zoningPrintlayoutSubOpacitySpinBox.value(),
                use_main=True,
                main_layer=background_main_layer,
                main_opacity=self.main.zoningPrintlayoutMainOpacitySpinBox.value(),
            )
        else:
            target_layer = self.main.printlayoutAggregateLayerCombobox.currentLayer()
            printlayout_name = "ゾーン統計量"
            background_layers = self._background_layers(
                use_sub=self.main.aggregatePrintlayoutUseSubBackgroundCheckBox.isChecked(),
                sub_layer=background_sub_layer,
                sub_opacity=self.main.aggregatePrintlayoutSubOpacitySpinBox.value(),
                use_main=True,
                main_layer=background_main_layer,
                main_opacity=self.main.aggregatePrintlayoutMainOpacitySpinBox.value(),
            )

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
            target_name, background_layers, target_layer
        )

    @staticmethod
    def _background_layers(use_sub, sub_layer, sub_opacity, use_main, main_layer, main_opacity):
        layers = []
        if use_sub and sub_layer is not None:
            layers.append({"layer": sub_layer, "opacity": sub_opacity / 100.0})
        if use_main and main_layer is not None:
            layers.append({"layer": main_layer, "opacity": main_opacity / 100.0})
        return layers
