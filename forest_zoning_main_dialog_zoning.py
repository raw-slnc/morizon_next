import os

# QGIS-API
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.utils import iface

from .processes.raster_styler import (
    write_qml_deviding_by_threshold,
)
from . import processes
from . import utils
from .progress_dialog import ProgressDialog
from .constants import (
    OUTPUT_PROFIT,
    OUTPUT_RISK,
    OUTPUT_ZONING,
    OUTPUT_ZONING_THRESHOLDS_JSON,
    SCORING_COLORS_PROFIT,
    SCORING_COLORS_RISK,
)


class ForestZoningMainDialogZoning:
    """
    メイン画面の「ゾーニング」タブの処理を実装するクラス
    """

    def __init__(self, main):
        self.main = main
        self.init_zoning_ui()

    def init_zoning_ui(self):
        """
        初回にのみ発火してUIと関数の紐付けなどの初期化処理を行う関数
        """
        self.main.zoningRunButton.clicked.connect(self.run_zoning)
        self.main.zoningSetLayersButton.clicked.connect(self.set_zoning_layer_combobox)
        self.main.zoningProfitUpdateButton.clicked.connect(
            lambda: self.set_zoning_raster_style("profit")
        )
        self.main.zoningRiskUpdateButton.clicked.connect(
            lambda: self.set_zoning_raster_style("risk")
        )

        # ラスターレイヤーだけを選択可能に
        for combobox in (
            self.main.zoningProfitLayerCombobox,
            self.main.zoningRiskLayerCombobox,
        ):
            combobox.setFilters(QgsMapLayerProxyModel.RasterLayer)

        # UI入力時にステート更新
        self.main.zoningProfitLayerCombobox.layerChanged.connect(
            self.refresh_zoning_ui
        )  # nopep8
        self.main.zoningProfitLayerCombobox.layerChanged.connect(
            self.set_zoning_thresholds
        )  # nopep8
        self.main.zoningRiskLayerCombobox.layerChanged.connect(
            self.refresh_zoning_ui
        )  # nopep8
        self.main.zoningRiskLayerCombobox.layerChanged.connect(
            self.set_zoning_thresholds
        )  # nopep8
        self.main.zoningOutputDirFileWidget.fileChanged.connect(
            self.refresh_zoning_ui
        )  # nopep8

        self.refresh_zoning_ui()
        self.set_zoning_thresholds()

    def refresh_zoning_ui(self):
        """
        UIの変更の都度発火してUIの状態を更新する関数
        """

        error_texts = self.get_zoning_error_texts()
        has_no_error = len(error_texts) == 0
        self.main.zoningErrorLabel.setText("\n".join(error_texts))
        self.main.zoningRunButton.setEnabled(has_no_error)

        for combobox, reload_button in (
            (self.main.zoningProfitLayerCombobox, self.main.zoningProfitUpdateButton),
            (self.main.zoningRiskLayerCombobox, self.main.zoningRiskUpdateButton),
        ):
            combobox_has_layer = combobox.currentLayer() is not None
            reload_button.setEnabled(combobox_has_layer)

    def get_zoning_error_texts(self) -> list:
        """
        UIの入力をチェックしてエラーを文字列の配列で返す
        要素数0=エラー無し
        Returns:
            list: 要素数が0以上のstrの配列
        """
        error_texts = []
        for name, combobox in (
            (OUTPUT_PROFIT["DISPLAY_NAME"], self.main.zoningProfitLayerCombobox),
            (OUTPUT_RISK["DISPLAY_NAME"], self.main.zoningRiskLayerCombobox),
        ):
            if combobox.currentLayer() is None:
                error_texts.append(f"{name}ラスターを指定してください")
                continue

            if not utils.is_valid_scoring_layer(combobox.currentLayer()):
                error_texts.append(f"有効な{name}ラスターを指定してください")
                continue

        # 出力レイヤーの存在確認
        layers = QgsProject().instance().layerTreeRoot().findLayers()
        layer_names = list(map(lambda l: l.name(), layers))
        if OUTPUT_ZONING["DISPLAY_NAME"] in layer_names:
            error_texts.append(f"プロジェクトにすでに「{OUTPUT_ZONING['DISPLAY_NAME']}」が存在します")

        if self.main.zoningOutputDirFileWidget.filePath() == "":
            error_texts.append("出力先フォルダを指定してください")

        return error_texts

    def set_zoning_layer_combobox(self):
        """
        プロジェクトのレイヤー名を検索し、ゾーニングタブの各コンボボックスに対応するレイヤーをセットする
        """
        layers = list(QgsProject.instance().mapLayers().values())
        layer_names = list(map(lambda layer: layer.name(), layers))

        for name, combobox in (
            (OUTPUT_PROFIT["DISPLAY_NAME"], self.main.zoningProfitLayerCombobox),
            (OUTPUT_RISK["DISPLAY_NAME"], self.main.zoningRiskLayerCombobox),
        ):
            idx = utils.find(layer_names, name)
            if idx > -1:
                combobox.setLayer(layers[idx])

    def set_zoning_thresholds(self):
        """
        収益性・災害リスクのしきい値を、入力ラスターの値域から計算してセットする
        """
        for combobox, spinbox in (
            (self.main.zoningProfitLayerCombobox, self.main.zoningProfitSpinbox),
            (self.main.zoningRiskLayerCombobox, self.main.zoningRiskSpinbox),
        ):
            spinbox.setValue(0)  # 初期化
            if combobox.currentLayer() is not None:
                if utils.is_valid_scoring_layer(combobox.currentLayer()):
                    threshold = utils.get_initial_thresholds(
                        combobox.currentLayer(), classes_count=2
                    )[0]
                    spinbox.setValue(threshold)

    def set_zoning_raster_style(self, layer_name: str):
        if layer_name == "profit":
            qml_filepath = write_qml_deviding_by_threshold(
                self.main.zoningProfitSpinbox.value(),
                SCORING_COLORS_PROFIT[0],
                SCORING_COLORS_PROFIT[1],
            )
            target_layer = self.main.zoningProfitLayerCombobox.currentLayer()
            opacity = 0.5
        elif layer_name == "risk":
            qml_filepath = write_qml_deviding_by_threshold(
                self.main.zoningRiskSpinbox.value(),
                SCORING_COLORS_RISK[0],
                SCORING_COLORS_RISK[1],
            )
            target_layer = self.main.zoningRiskLayerCombobox.currentLayer()
            opacity = 0.8

        target_layer.loadNamedStyle(qml_filepath)
        target_layer.renderer().setOpacity(opacity)
        iface.layerTreeView().refreshLayerSymbology(target_layer.id())  # レイヤー一覧の凡例を更新
        target_layer.triggerRepaint()  # キャンバス上の見た目を更新

    def get_existing_filenames(self):
        """
        出力先フォルダに同名ファイルが存在するかチェック
        存在するファイルの配列を返す

        Returns:
            list
        """
        output_dir = self.main.zoningOutputDirFileWidget.filePath()
        existing_filenames = []

        for file_info in (OUTPUT_ZONING, OUTPUT_ZONING_THRESHOLDS_JSON):
            filename = f"{file_info['FILE_NAME']}.{file_info['EXTENSION']}"
            if os.path.exists(os.path.join(output_dir, filename)):
                existing_filenames.append(filename)

        return existing_filenames

    def run_zoning(self):
        existing_filenames = self.get_existing_filenames()
        if len(existing_filenames) > 0:
            if QMessageBox.No == QMessageBox.question(
                self.main,
                "上書き確認",
                "出力先フォルダに同名ファイルが存在します、上書きしますか？\n" + "\n".join(existing_filenames),
                QMessageBox.Yes,
                QMessageBox.No,
            ):
                QMessageBox.information(self.main, "処理中断", "処理を中断しました。")
                return

        input_layers_dict = {
            "profit": self.main.zoningProfitLayerCombobox.currentLayer(),
            "risk": self.main.zoningRiskLayerCombobox.currentLayer(),
        }
        input_thresholds_dict = {
            "profit": self.main.zoningProfitSpinbox.value(),
            "risk": self.main.zoningRiskSpinbox.value(),
        }
        thread = processes.zoning.ProcessingThread(
            input_layers_dict,
            input_thresholds_dict,
            self.main.zoningOutputDirFileWidget.filePath(),
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        progress_dialog.set_abortable(False)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFinished.connect(self.add_layers_to_project)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"エラーが発生しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec_()

        if thread.abort_flag:
            QMessageBox.information(self.main, "中断", "処理を中断しました。")
        else:
            QMessageBox.information(self.main, "終了", "処理が終了しました。")

    @staticmethod
    def add_layers_to_project(rlayers_dict):
        """
        処理結果をプロジェクトに追加
        """
        for rlayer in rlayers_dict.values():
            # プロジェクトのレイヤー一覧の一番上にレイヤーを追加
            QgsProject.instance().addMapLayer(rlayer, False)
            root = QgsProject().instance().layerTreeRoot()
            root.insertLayer(0, rlayer)
