import os
import glob

# QGIS-API
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *

from . import processes
from .constants import OUTPUT_ZONING, OUTPUT_AGGREGATE, INPUT_DEM
from .utils import is_tmpdir_valid
from .progress_dialog import ProgressDialog



class ForestZoningMainDialogAggregate:
    """
    メイン画面の「集計」タブの処理を実装するクラス
    """
    def __init__(self, main):
        self.main = main
        self.init_aggregate_ui()

    def init_aggregate_ui(self):
        self.main.aggregateSetLayersButton.clicked.connect(
            self.set_aggregate_layer_combobox
        )
        self.main.aggregateSetDemButton.clicked.connect(self.load_aggregate_dem_path)
        self.main.aggregateZoningLayerCombobox.setFilters(
            QgsMapLayerProxyModel.RasterLayer
        )
        self.main.aggregatePolygonLayerCommbobox.setFilters(
            QgsMapLayerProxyModel.VectorLayer
        )
        self.main.aggregateRunButton.clicked.connect(self.run_aggregate)
        self.main.aggregateOutputDirFileWidget.setFilter("*.shp")
        filename = OUTPUT_AGGREGATE.get("FILE_NAME")
        self.main.aggregateOutputDirFileWidget.setDefaultRoot(f"{filename}.shp")

        self.main.aggregateZoningLayerCombobox.layerChanged.connect(
            self.refresh_aggregate_ui
        )
        self.main.aggregatePolygonLayerCommbobox.layerChanged.connect(
            self.refresh_aggregate_ui
        )
        self.main.aggregateOutputDirFileWidget.fileChanged.connect(
            self.refresh_aggregate_ui
        )
        self.main.aggregateDemFileWidget.fileChanged.connect(self.refresh_aggregate_ui)

        # ラジオボタンの変更時にUI更新
        # 一方のラジオボタンの変更が発火するともう一方も発火するので一方だけconnect
        self.main.radioButtonPolygon.toggled.connect(self.refresh_aggregate_ui)

        self.main.aggregateStyleThresholdspinBox.setValue(30)

        self.refresh_aggregate_ui()

    def set_aggregate_layer_combobox(self):
        zoning_rlayers = QgsProject.instance().mapLayersByName(
            OUTPUT_ZONING.get("DISPLAY_NAME")
        )
        if len(zoning_rlayers) > 0:
            zoning_layer = zoning_rlayers[0]
            self.main.aggregateZoningLayerCombobox.setLayer(zoning_layer)
        else:
            QMessageBox.information(self.main, "エラー", "ゾーニング図を作成してください。")
            return

    def load_aggregate_dem_path(self):
        selected_dir = QFileDialog.getExistingDirectory(self.main, "データフォルダを選択")

        dem_filenames = glob.glob(
            os.path.join(
                selected_dir, *INPUT_DEM["PATH"], "*." + INPUT_DEM["EXT"] + "*"
            )
        )

        # 既定階層にあったDEMファイルがない場合空文字列をセットする
        dem_path = (
            os.path.join(selected_dir, dem_filenames[0])
            if len(dem_filenames) > 0
            else ""
        )
        self.main.aggregateDemFileWidget.setFilePath(dem_path)

    def run_aggregate(self):
        # GRASSエラーを回避するために環境変数に不正な文字がないか確認
        if not is_tmpdir_valid():
            QMessageBox.information(
                self.main,
                "エラー",
                f"TEMPディレクトリーに不正な文字があります。\nマニュアルに従い、システム環境変数を設定していください。",
            )
            return

        output_path = self.main.aggregateOutputDirFileWidget.filePath()
        zoning_rlayer = self.main.aggregateZoningLayerCombobox.currentLayer()

        def is_file_used(file_name):
            """ファイルが使用されているかをチェックする関数"""
            try:
                os.rename(file_name, file_name)
                return False
            except:
                return True

        # .shpがすでに存在している場合、同名の.shp/.dbf/.shx/.prjファイルを削除する
        if os.path.exists(output_path):
            folderpath = os.path.dirname(output_path)
            filename_no_extension = os.path.splitext(os.path.basename(output_path))[0]
            file_list = glob.glob(f"{folderpath}/{filename_no_extension}.*")

            # deletableを初期化
            deletable = True
            for file in file_list:
                if is_file_used(file) == True:
                    deletable = False
                    break

            if deletable == True:
                for file in file_list:
                    os.remove(file)
            else:
                QMessageBox.information(self.main, "エラー", "指定したファイルが使用中のため、上書きできません。")
                return

        self.main.hide()

        mode = "polygon" if self.main.radioButtonPolygon.isChecked() else "dem"
        input_layer = self.main.aggregatePolygonLayerCommbobox.currentLayer() if mode=="polygon" else self.main.aggregateDemFileWidget.filePath()
        thread = processes.aggregate.ProcessingThread(
            mode=mode,
            zoning_layer_path=zoning_rlayer,
            input_layer=input_layer,
            output_path=output_path,
            style_threshold=self.main.aggregateStyleThresholdspinBox.value()
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        progress_dialog.set_abortable(False)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.processFinished.connect(self.add_layers_to_project)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"エラーが発生しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec()

        self.main.show()

        QMessageBox.information(self.main, "完了", "処理が完了しました。")

    def refresh_aggregate_ui(self):
        # ラジオボタンの状態に応じてUIを有効化・無効化
        self.main.aggregatePolygonLayerCommbobox.setEnabled(
            self.main.radioButtonPolygon.isChecked()
        )
        self.main.aggregateDemFileWidget.setEnabled(
            self.main.radioButtonWatershed.isChecked()
        )
        self.main.aggregateSetDemButton.setEnabled(
            self.main.radioButtonWatershed.isChecked()
        )

        error_texts = self.get_aggregate_error()
        has_no_error = len(error_texts) == 0
        self.main.aggregateErrorLabel.setText("\n".join(error_texts))
        self.main.aggregateRunButton.setEnabled(has_no_error)

    def get_aggregate_error(self) -> list:
        error_texts = []
        if self.main.aggregateZoningLayerCombobox.currentLayer() is None:
            error_texts.append("ゾーニング図を指定してください")
        if (
                self.main.radioButtonPolygon.isChecked()
                and self.main.aggregatePolygonLayerCommbobox.currentLayer() is None
        ):
            error_texts.append("ポリゴンレイヤを指定してください")
        if (
                self.main.radioButtonWatershed.isChecked()
                and self.main.aggregateDemFileWidget.filePath() == ""
        ):
            error_texts.append("DEMファイルを指定してください")
        if self.main.aggregateOutputDirFileWidget.filePath() == "":
            error_texts.append("出力先フォルダを指定してください")

        return error_texts

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
