# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import glob
import gc

# QGIS-API
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)
from qgis.core import QgsMapLayerProxyModel, QgsProject
from qgis.gui import QgsFieldComboBox

from . import processes
from . import layer_db
from . import morizon_data
from . import utils
from .constants import DIR_AGGREGATE, OUTPUT_ZONING, OUTPUT_AGGREGATE, INPUT_DEM
from .progress_dialog import run_with_progress
from .settings_manager import AggregateConiferManager, AggregateStyleManager


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

        self.init_conifer_ui()
        self.refresh_aggregate_ui()

    # ── 針葉樹のフィーチャーだけで収益性を判定する（任意。ポリゴンに針葉樹を区別する列があるとき） ──
    def init_conifer_ui(self):
        """ポリゴンレイヤーの欄の下（ジオメトリの注意書きと同じ行）に、チェック・列の選択・設定ボタンを足す。
        チェックを外していれば、集計は原版と同じ"""
        self.conifer_values = set()  # 針葉樹とみなす値（文字列）
        self._conifer_restoring = False  # 保存した設定を戻している間は、選択の変更で値を解かない
        self.conifer_check = QCheckBox("針葉樹のフィーチャーだけで収益性を判定する")
        self.conifer_check.setToolTip(
            "収益性（林業経営適地・要収益性向上）は針葉樹の人工林を前提にした指標のため、\n"
            "針葉樹でないポリゴンは4象限の区分を付けない（灰色で塗る。災害リスクの斜線は付く）")
        self.conifer_field_combo = QgsFieldComboBox()
        self.conifer_field_combo.setToolTip("針葉樹かどうかを見分ける列")
        self.conifer_values_button = QPushButton("設定")
        self.conifer_values_button.setToolTip("選んだ列の値から、針葉樹とみなす値を選ぶ")
        self.conifer_values_label = QLabel()

        row = QHBoxLayout()
        row.addWidget(self.conifer_check)
        row.addWidget(QLabel("照合する列"))
        row.addWidget(self.conifer_field_combo, 1)
        row.addWidget(self.conifer_values_button)
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        grid = self.main.findChild(QGridLayout, "gridLayout_5")
        note = self.main.label_8  # ジオメトリの注意書き（4行目・2列目）
        grid.removeWidget(note)
        box.addWidget(note)
        # 「外周線を出力しない」（針葉樹のオプションの上。表示だけで、集計の値は変わらない）
        self.no_outline_check = QCheckBox("外周線を出力しない")
        self.no_outline_check.setToolTip(
            "区分の塗りの外周線を描かず、災害リスクの斜線を薄く（不透明度30%）描く。\n"
            "森林計画図の小班線など、ほかのレイヤーを重ねて見るときに使う（表示だけで、集計の値は変わらない）")
        self.no_outline_check.setChecked(AggregateStyleManager().load_no_outline())
        self.no_outline_check.toggled.connect(lambda checked: AggregateStyleManager().store_no_outline(checked))
        box.addWidget(self.no_outline_check)
        box.addLayout(row)
        box.addWidget(self.conifer_values_label)
        grid.addLayout(box, 4, 1, 1, 2)

        self.conifer_field_combo.setLayer(self.main.aggregatePolygonLayerCommbobox.currentLayer())
        self.main.aggregatePolygonLayerCommbobox.layerChanged.connect(self.on_conifer_layer_changed)
        self.conifer_field_combo.fieldChanged.connect(self.on_conifer_field_changed)
        self.conifer_check.toggled.connect(self.on_conifer_check_toggled)
        self.conifer_values_button.clicked.connect(self.choose_conifer_values)
        self.restore_conifer_settings()

    # 選んだポリゴンレイヤーとチェック項目は、プラグインの設定に保存して次に開いたときに戻す
    def restore_conifer_settings(self):
        """保存したポリゴンレイヤーがプロジェクトにあれば選び直す（列・値・チェックは選び直しの中で戻す）"""
        saved = AggregateConiferManager().load()
        if not saved['layer_source'] or self.main.aggregatePolygonLayerCommbobox.currentLayer() is not None:
            return
        for layer in QgsProject.instance().mapLayers().values():
            if layer.source() == saved['layer_source']:
                self.main.aggregatePolygonLayerCommbobox.setLayer(layer)
                return

    def store_conifer_settings(self):
        if self._conifer_restoring:
            return
        layer = self.main.aggregatePolygonLayerCommbobox.currentLayer()
        AggregateConiferManager().store(
            layer.source() if layer is not None else "",
            self.conifer_check.isChecked(),
            self.conifer_field_combo.currentField(),
            self.conifer_values,
        )

    def on_conifer_layer_changed(self, layer):
        saved = AggregateConiferManager().load()
        self._conifer_restoring = True
        try:
            self.conifer_field_combo.setLayer(layer)
            if layer is not None and layer.source() == saved['layer_source']:
                # 前に使ったレイヤーなら、列・値・チェックを戻す（列が無くなっていれば戻さない）
                if saved['field'] and layer.fields().indexOf(saved['field']) >= 0:
                    self.conifer_field_combo.setField(saved['field'])
                    self.conifer_values = set(saved['values'])
                    self.conifer_check.setChecked(saved['enabled'])
                else:
                    self.conifer_values = set()
            else:
                self.conifer_values = set()
        finally:
            self._conifer_restoring = False
        self.store_conifer_settings()
        self.refresh_aggregate_ui()

    def on_conifer_field_changed(self, *_):
        if self._conifer_restoring:
            return
        # 列が変われば値の意味も変わるので、選んだ値は解く
        self.conifer_values = set()
        self.store_conifer_settings()
        self.refresh_aggregate_ui()

    def on_conifer_check_toggled(self, *_):
        self.store_conifer_settings()
        self.refresh_aggregate_ui()

    def choose_conifer_values(self):
        """選んだ列の値（重複なし）をチェックの一覧で出し、針葉樹とみなす値を選ぶ"""
        layer = self.main.aggregatePolygonLayerCommbobox.currentLayer()
        field = self.conifer_field_combo.currentField()
        if layer is None or not field:
            return
        index = layer.fields().indexOf(field)
        values = sorted(str(v) for v in layer.uniqueValues(index) if v is not None and str(v) != "NULL")

        dialog = QDialog(self.main)
        dialog.setWindowTitle("針葉樹とみなす値")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"列「{field}」の値から、針葉樹とみなす値にチェックを入れてください。"))
        value_list = QListWidget()
        for value in values:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if value in self.conifer_values
                               else Qt.CheckState.Unchecked)
            value_list.addItem(item)
        layout.addWidget(value_list)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("確定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(360, 420)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.conifer_values = {
            value_list.item(i).text() for i in range(value_list.count())
            if value_list.item(i).checkState() == Qt.CheckState.Checked
        }
        self.store_conifer_settings()
        self.refresh_aggregate_ui()

    def get_conifer_setting(self):
        """集計に渡す (列名, 針葉樹とみなす値の集合)。使わないときは None"""
        if not (self.main.radioButtonPolygon.isChecked() and self.conifer_check.isChecked()):
            return None
        return self.conifer_field_combo.currentField(), set(self.conifer_values)

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
            except Exception:
                return True

        # ゾーン統計量のレイヤーを片付ける（指している場所に関係なく。ファイルを上書きするかどうかとは別の話）
        utils.remove_output_layers(utils.STAGE_AGGREGATE)
        db_path = layer_db.db_file(utils.get_morizon_layer_db_dir(), layer_db.KIND_AGGREGATE)

        # .shpがすでに存在している場合、同名の.shp/.dbf/.shx/.prjファイルを削除する
        if os.path.exists(output_path):
            folderpath = os.path.dirname(output_path)
            filename_no_extension = os.path.splitext(os.path.basename(output_path))[0]
            file_list = glob.glob(f"{folderpath}/{filename_no_extension}.*")

            # deletableを初期化
            deletable = True
            for file in file_list:
                if is_file_used(file):
                    deletable = False
                    break

            if deletable:
                for file in file_list:
                    os.remove(file)
            else:
                QMessageBox.information(self.main, "エラー", "指定したファイルが使用中のため、上書きできません。")
                return
        # 描画用の DB の前の結果も空にする（shp と DB の両方を消し込む）
        layer_db.clear(db_path, layer_db.KIND_AGGREGATE)

        self.main.hide()

        mode = "polygon" if self.main.radioButtonPolygon.isChecked() else "dem"
        if mode == "polygon":
            input_layer = self.main.aggregatePolygonLayerCommbobox.currentLayer()
        else:
            input_layer = self.main.aggregateDemFileWidget.filePath()
        thread = processes.aggregate.ProcessingThread(
            mode=mode,
            zoning_layer_path=zoning_rlayer,
            input_layer=input_layer,
            output_path=output_path,
            style_threshold=self.main.aggregateStyleThresholdspinBox.value(),
            db_path=db_path,
            conifer=self.get_conifer_setting(),
            no_outline=self.no_outline_check.isChecked(),
        )
        # 結果のレイヤー追加と知らせは、進捗の窓を消してから行う（run_with_progress）
        outcome = run_with_progress(thread, show_detail=True)
        if "result" in outcome:
            self.add_layers_to_project(outcome["result"])

        self.main.show()

        if "error" in outcome:
            QMessageBox.information(self.main, "エラー", f"集計を完了できませんでした。\n\n{outcome['error']}")
        else:
            QMessageBox.information(self.main, "完了", f"処理が完了しました。\n{thread.summary}")

    def refresh_aggregate_ui(self):
        # 候補の作り直しはしない（選択欄の合図の中から呼ばれるため。合わせ直しはメイン画面の refresh_all_tabs）

        # 既定のレイヤーが入っているとき（押しても変わらないとき）は、再読込ボタンをグレーアウトする
        default_layer = utils.find_morizon_layer_by_name(
            OUTPUT_ZONING["DISPLAY_NAME"], allowed_extensions={".tif", ".tiff"}
        )
        self.main.aggregateSetLayersButton.setEnabled(
            default_layer is not None
            and self.main.aggregateZoningLayerCombobox.currentLayer() is not default_layer
        )

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

        # 針葉樹の判定はポリゴンで集計するときだけ。チェックを入れたときに列と値を選べる
        if hasattr(self, "conifer_check"):
            polygon_mode = self.main.radioButtonPolygon.isChecked()
            self.conifer_check.setEnabled(polygon_mode)
            use = polygon_mode and self.conifer_check.isChecked()
            self.conifer_field_combo.setEnabled(use)
            self.conifer_values_button.setEnabled(use and bool(self.conifer_field_combo.currentField()))
            if use and self.conifer_values:
                shown = "、".join(sorted(self.conifer_values))
                self.conifer_values_label.setText(f"針葉樹とみなす値：{shown}")
            elif use:
                self.conifer_values_label.setText("針葉樹とみなす値：未設定（「設定」で選んでください）")
            else:
                self.conifer_values_label.setText("")
            self.conifer_values_label.setVisible(use)

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
        if (
                hasattr(self, "conifer_check")
                and self.main.radioButtonPolygon.isChecked()
                and self.conifer_check.isChecked()
        ):
            if not self.conifer_field_combo.currentField():
                error_texts.append("針葉樹を照合する列を指定してください")
            elif not self.conifer_values:
                error_texts.append("針葉樹とみなす値を「設定」で選んでください")
        if self.main.aggregateOutputDirFileWidget.filePath() == "":
            error_texts.append("QGISプロジェクトを保存してください（出力先はプロジェクトと同じフォルダの morizon_next/<プロジェクトのファイル名> の中に決まります）")

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
