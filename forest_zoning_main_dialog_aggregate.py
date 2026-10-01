# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import glob
import gc

# QGIS-API
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *

from . import processes
from . import morizon_data
from . import utils
from .constants import DIR_AGGREGATE, OUTPUT_ZONING, OUTPUT_AGGREGATE, INPUT_DEM
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
            QgsMapLayerProxyModel.Filter.RasterLayer
        )
        self.update_aggregate_layer_scope()
        self.main.aggregatePolygonLayerCommbobox.setFilters(
            QgsMapLayerProxyModel.Filter.PolygonLayer
        )
        self.main.aggregatePolygonLayerCommbobox.setAllowEmptyLayer(True, "未選択")
        self.main.aggregatePolygonLayerCommbobox.setCurrentIndex(0)
        self.main.aggregateRunButton.clicked.connect(self.run_aggregate)
        self.main.aggregateOutputDirFileWidget.setFilter("*.shp")
        filename = OUTPUT_AGGREGATE.get("FILE_NAME")
        self.main.aggregateOutputDirFileWidget.setDefaultRoot(f"{filename}.shp")

        # 出力先未指定時は、プロジェクト内蔵のプラグイン管理フォルダをデフォルトにする
        # (set_morizon_layer_scopeがこの管理フォルダ配下しか候補にしないため、
        # 他タブと出力先を揃えておく必要がある)
        if self.main.aggregateOutputDirFileWidget.filePath() == "":
            project_home = QgsProject.instance().homePath()
            if project_home != "":
                default_output_dir = utils.get_morizon_managed_dir(DIR_AGGREGATE)
                os.makedirs(default_output_dir, exist_ok=True)
                self.main.aggregateOutputDirFileWidget.setFilePath(
                    os.path.join(default_output_dir, f"{filename}.shp")
                )

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
        self.update_aggregate_layer_scope()
        zoning_layer = utils.find_morizon_layer_by_name(
            OUTPUT_ZONING.get("DISPLAY_NAME"),
            allowed_extensions={".tif", ".tiff"},
        )
        if zoning_layer is not None:
            self.main.aggregateZoningLayerCombobox.setLayer(zoning_layer)
        else:
            QMessageBox.information(self.main, "エラー", "ゾーニング図を作成してください。")
            return

    def select_restored_layers(self):
        """保存データの読み込み後、ゾーニング図の選択欄を作り直したレイヤーに合わせる"""
        self.update_aggregate_layer_scope()
        layer = utils.find_morizon_layer_by_name(
            OUTPUT_ZONING["DISPLAY_NAME"], allowed_extensions={".tif", ".tiff"}
        )
        if layer is not None:
            self.main.aggregateZoningLayerCombobox.setLayer(layer)

    def update_aggregate_layer_scope(self):
        utils.set_morizon_layer_scope(
            self.main.aggregateZoningLayerCombobox,
            allowed_names={OUTPUT_ZONING.get("DISPLAY_NAME")},
            allowed_extensions={".tif", ".tiff"},
        )

    def load_aggregate_dem_path(self):
        selected_dir = QFileDialog.getExistingDirectory(self.main, "データフォルダを選択")

        if not selected_dir:
            return
        # ZoningKit の最上位・DATA フォルダのどちらでもよい。既定階層にDEMが無ければ空にする
        data_dir = morizon_data.resolve_data_dir(selected_dir)
        dem_filenames = morizon_data.find_input_files(data_dir, INPUT_DEM) if data_dir else []
        dem_path = dem_filenames[0] if dem_filenames else ""
        self.main.aggregateDemFileWidget.setFilePath(dem_path)

    def run_aggregate(self):
        output_path = self.main.aggregateOutputDirFileWidget.filePath()
        zoning_rlayer = self.main.aggregateZoningLayerCombobox.currentLayer()

        def is_file_used(file_name):
            """ファイルが使用されているかをチェックする関数"""
            try:
                os.rename(file_name, file_name)
                return False
            except:
                return True

        # ゾーン統計量のレイヤーを片付ける（指している場所に関係なく。ファイルを上書きするかどうかとは別の話）
        utils.remove_output_layers(utils.STAGE_AGGREGATE)

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
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.postDetail.connect(progress_dialog.set_detail)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(self.add_layers_to_project)
        thread.processFinished.connect(progress_dialog.close)
        failures = []
        thread.processFailed.connect(failures.append)
        thread.processFailed.connect(progress_dialog.close)
        thread.start()
        progress_dialog.exec()
        thread.wait()

        self.main.show()

        if failures:
            QMessageBox.information(self.main, "エラー", f"集計を完了できませんでした。\n\n{failures[0]}")
        else:
            QMessageBox.information(self.main, "完了", f"処理が完了しました。\n{thread.summary}")

    def refresh_aggregate_ui(self):
        self.update_aggregate_layer_scope()

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
            error_texts.append("QGISプロジェクトを保存してください（出力先はプロジェクトと同じフォルダの morizon_next の中に決まります）")

        return error_texts

    @staticmethod
    def add_layers_to_project(rlayers_dict):
        """
        処理結果をプロジェクトに追加
        """
        for key, rlayer in rlayers_dict.items():
            utils.tag_output_layer(rlayer, utils.STAGE_AGGREGATE, key)
            QgsProject.instance().addMapLayer(rlayer, False)
            # 出力レイヤーは「Morizon Next」グループの中の一番上に追加する
            utils.get_morizon_output_group().insertLayer(0, rlayer)
        rlayers_dict.clear()
        gc.collect()
