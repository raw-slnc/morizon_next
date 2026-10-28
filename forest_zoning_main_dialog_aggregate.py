# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import glob
import gc
import re

# QGIS-API
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)
from qgis.core import QgsFields, QgsFillSymbol, QgsMapLayerProxyModel, QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldComboBox

from . import processes
from . import layer_db
from . import morizon_data
from . import utils
from .constants import DIR_AGGREGATE, OUTPUT_ZONING, OUTPUT_AGGREGATE, INPUT_DEM
from .processes.species_mesh import OUTPUT_KEY as SPECIES_MESH_KEY
from .progress_dialog import run_with_progress
from .settings_manager import AggregateConiferManager, AggregateStyleManager, SettingsManager


SPECIES_MESH_ITEM = "現在地の樹種メッシュ（全国森林資源メッシュを取得）"


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
        # ゾーニングは原版の手順（ZoningKit の地位）のまま進め、最後に全国の樹種メッシュで集計して樹種を絞って見るための選択肢
        # 一度作ったら、そのレイヤーを選べば足りるので、項目はレイヤーが無いときだけ出す
        # （集計の後・ゾーニングのやり直し・初期化で外れたら、また出る）
        self.main.aggregatePolygonLayerCommbobox.setExtraItem(
            SPECIES_MESH_ITEM, shown=lambda: not utils.output_layers(utils.STAGE_ZONING, keys=[SPECIES_MESH_KEY]))
        self.main.aggregatePolygonLayerCommbobox.extraChosen.connect(self.load_species_mesh)
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

        # 「流域で集計」のDEMは、要素計算のDEM（取得・調整済みで、海・水面を対象外にしたもの）を既定にする。
        # ゾーニング図と同じDEMで流域を作るため。要素計算のDEMが変わったら合わせる（流域用に別のDEMを選び直すことはできる）
        self.main.elementsDemFileWidget.fileChanged.connect(self.main.aggregateDemFileWidget.setFilePath)
        if self.main.aggregateDemFileWidget.filePath() == "":
            self.main.aggregateDemFileWidget.setFilePath(self.main.elementsDemFileWidget.filePath())
        self.main.aggregateDemFileWidget.setToolTip(
            "要素計算のDEMが自動で入ります（ゾーニング図と同じDEMで流域を作るため）。\n"
            "流域用に別のDEMを使うときだけ選び直してください")

        # ラジオボタンの変更時にUI更新
        # 一方のラジオボタンの変更が発火するともう一方も発火するので一方だけconnect
        self.main.radioButtonPolygon.toggled.connect(self.refresh_aggregate_ui)

        # 災害リスク高の割合（%）のしきい値。原版は範囲を指定しておらず Qt の既定（0〜99）のままで、
        # 「ポリゴン全体が災害リスク高（100%）」を選べなかった（細かいメッシュで集計すると、多くが 0% か 100% になる）。
        # 0% はすべてのポリゴンが当てはまり意味が無いので、1〜100 にする
        self.main.aggregateStyleThresholdspinBox.setRange(1, 100)
        self.apply_initial_risk_threshold()

        self.init_conifer_ui()
        self.refresh_aggregate_ui()

    # ── 針葉樹のフィーチャーだけで収益性を判定する（任意。ポリゴンに針葉樹を区別する列があるとき） ──
    def result_risk_threshold(self):
        """作業フォルダにゾーン統計量の結果があれば、それを作ったときの「災害リスク高」の割合（%）を、
        結果のスタイル（qml）の凡例（「災害リスク高≧30%」）から読む。結果が無い・読めなければ None"""
        shp_path = self.main.aggregateOutputDirFileWidget.filePath()
        qml_path = os.path.splitext(shp_path)[0] + ".qml" if shp_path else ""
        if not (shp_path and os.path.isfile(shp_path) and os.path.isfile(qml_path)):
            return None
        try:
            with open(qml_path, encoding="utf-8") as f:
                match = re.search(r"災害リスク高≧(\d+)%", f.read())
        except (OSError, UnicodeDecodeError):
            return None
        value = int(match.group(1)) if match else None
        return value if value is not None and 1 <= value <= 100 else None

    def apply_initial_risk_threshold(self):
        """「災害リスク高」の欄に値を入れる：結果があればそれを作ったときの値、無ければ事業者の基準（設定タブ）。
        画面を作ったとき・作業フォルダやプロジェクトが変わったとき・再開したとき・基準を保存したときに呼ぶ
        （手で入れた値を、タブの切り替えなどのたびに上書きしないよう、それ以外では呼ばない）"""
        value = self.result_risk_threshold()
        if value is None:
            try:
                value = int(float(SettingsManager().get_setting("aggregate_risk_threshold")))
            except (TypeError, ValueError):
                value = 30
        self.main.aggregateStyleThresholdspinBox.setValue(value)

    def init_conifer_ui(self):
        """ポリゴンレイヤーの欄の下（ジオメトリの注意書きと同じ行）に、チェック・列の選択・設定ボタンを足す。
        チェックを外していれば、集計は原版と同じ"""
        self.conifer_values = set()  # 針葉樹とみなす値（文字列）
        self._conifer_restoring = False  # 保存した設定を戻している間は、選択の変更で値を解かない
        self._conifer_watched_layer = None  # 選んでいるポリゴンレイヤー（消されたら設定を作り直すため見張る）
        self.conifer_check = QCheckBox("針葉樹のフィーチャーだけで収益性を判定する")
        self.conifer_check.setToolTip(
            "収益性（林業経営適地・要収益性向上）は針葉樹の人工林を前提にした指標のため、\n"
            "針葉樹とみなす値に当てはまらないポリゴンは4象限の区分を付けない（淡い黄色で塗る。災害リスクの斜線は付く）")
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
        # 「外周線を出力しない」（ゾーニング図の選択欄の下。表示だけで、集計の値は変わらない）
        self.no_outline_check = QCheckBox("外周線を出力しない")
        self.no_outline_check.setToolTip(
            "区分の塗りの外周線を描かず、災害リスクの斜線を薄く（不透明度30%）描く。\n"
            "森林計画図の小班線など、ほかのレイヤーを重ねて見るときに使う（表示だけで、集計の値は変わらない）")
        self.no_outline_check.setChecked(AggregateStyleManager().load_no_outline())
        self.no_outline_check.toggled.connect(lambda checked: AggregateStyleManager().store_no_outline(checked))
        # 「不透明度を100％設定」（「外周線を出力しない」の横。表示だけで、集計の値は変わらない）
        self.full_opacity_check = QCheckBox("不透明度を100％設定")
        self.full_opacity_check.setToolTip(
            "区分の塗りを不透明度100%・描画モード通常で描く（原版と同じ）。\n"
            "既定は塗り40%・描画モード乗算で、背景の地図が透けて見える。\n"
            "表示だけで、集計の値は変わらない")
        self.full_opacity_check.setChecked(AggregateStyleManager().load_full_opacity())
        self.full_opacity_check.toggled.connect(
            lambda checked: AggregateStyleManager().store_full_opacity(checked))
        style_row = QHBoxLayout()
        style_row.addWidget(self.no_outline_check)
        style_row.addWidget(self.full_opacity_check)
        style_row.addStretch(1)
        # 表示の設定は「任意のポリゴン」「流域」のどちらでも使うため、ゾーニング図の選択欄（1行目・2列目）の下に置く。
        # 同じ行の「ゾーニング図」の文字と再読込ボタンは、選択欄の横にそろうよう上寄せにする
        zoning_combo = self.main.aggregateZoningLayerCombobox
        grid.removeWidget(zoning_combo)
        zoning_box = QVBoxLayout()
        zoning_box.setContentsMargins(0, 0, 0, 0)
        zoning_box.addWidget(zoning_combo)
        zoning_box.addLayout(style_row)
        grid.addLayout(zoning_box, 0, 1)
        for widget in (self.main.aggregateVectorLayerText_2, self.main.aggregateSetLayersButton):
            grid.setAlignment(widget, Qt.AlignmentFlag.AlignTop)
        box.addLayout(row)
        box.addWidget(self.conifer_values_label)
        grid.addLayout(box, 4, 1, 1, 2)

        self._set_conifer_fields(self.main.aggregatePolygonLayerCommbobox.currentLayer())
        self._watch_conifer_layer(self.main.aggregatePolygonLayerCommbobox.currentLayer())
        self.main.aggregatePolygonLayerCommbobox.layerChanged.connect(self.on_conifer_layer_changed)
        self.conifer_field_combo.fieldChanged.connect(self.on_conifer_field_changed)
        self.conifer_check.toggled.connect(self.on_conifer_check_toggled)
        self.conifer_values_button.clicked.connect(self.choose_conifer_values)
        self.restore_conifer_settings()

    # ── 現在地の樹種メッシュ（全国森林資源メッシュ） ──
    def load_species_mesh(self):
        """ゾーニング図の範囲の全国森林資源メッシュを取得して、ポリゴンレイヤーとして選び、針葉樹フィルターの列と値を樹種で入れる（チェックは入れない）"""
        from .processes.species_mesh import CONIFER_VALUES, LAYER_NAME, OUTPUT_KEY, SPECIES_FIELD, SpeciesMeshThread
        zoning = self.main.aggregateZoningLayerCombobox.currentLayer()
        if zoning is None:
            QMessageBox.information(self.main, "樹種メッシュ", "先にゾーニング図を選んでください（範囲はゾーニング図で決めます）。")
            return
        if not self.main.has_workspace():
            QMessageBox.information(self.main, "樹種メッシュ", "作業フォルダが必要です（「表示範囲から開始する」などで始めてください）。")
            return
        output = utils.get_workspace_dir(DIR_AGGREGATE, "species_mesh.gpkg")
        # 前に作ったメッシュを読み込んでいれば、ファイルを書き直す前にプロジェクトから外す
        self.remove_species_mesh(delete_file=False)
        thread = SpeciesMeshThread(zoning.source(), utils.get_morizon_shared_dir(), output)
        outcome = run_with_progress(thread, show_detail=True)
        if "error" in outcome:
            QMessageBox.information(self.main, "樹種メッシュ", f"樹種メッシュを用意できませんでした。\n\n{outcome['error']}")
            return
        result = outcome.get("result") or {}
        if thread.abort_flag or not result:
            return
        layer = QgsVectorLayer(f"{result['path']}|layername={LAYER_NAME}", "全国森林資源メッシュ（林野庁）", "ogr")
        if not layer.isValid():
            QMessageBox.information(self.main, "樹種メッシュ", "樹種メッシュを読み込めませんでした。")
            return
        # 塗りは透明のまま（下の地図を隠さない）。外周線は fcloud_shizuoka のメッシュと同じ（地図上 0.13m の濃い灰色）
        layer.renderer().setSymbol(QgsFillSymbol.createSimple({
            "color": "0,0,0,0", "outline_color": "35,35,35,255", "outline_style": "solid",
            "outline_width": "0.13", "outline_width_unit": "RenderMetersInMapUnits", "joinstyle": "bevel",
        }))
        # 「Morizon Next」の中の、ゾーニング図の直下に置く（ゾーニング図が無ければ「Morizon Next」の一番上）
        utils.tag_output_layer(layer, utils.STAGE_ZONING, OUTPUT_KEY)
        QgsProject.instance().addMapLayer(layer, False)
        zoning_node = QgsProject.instance().layerTreeRoot().findLayer(zoning.id())
        if zoning_node is not None and zoning_node.parent() is not None:
            parent = zoning_node.parent()
            parent.insertLayer(parent.children().index(zoning_node) + 1, layer)
        else:
            utils.get_morizon_output_group().insertLayer(0, layer)
        self.main.aggregatePolygonLayerCommbobox.setLayer(layer)
        # 針葉樹フィルター：樹種の列で、針葉樹の値を入れてオンにする（値は「設定」で選び直せる）
        values = {str(v) for v in layer.uniqueValues(layer.fields().indexOf(SPECIES_FIELD)) if v}
        self._conifer_restoring = True
        try:
            self.conifer_field_combo.setField(SPECIES_FIELD)
            self.conifer_values = values & set(CONIFER_VALUES)
            # 列と値は入れておくが、フィルターを使うかは利用者が決める（チェックは入れない）
            self.conifer_check.setChecked(False)
        finally:
            self._conifer_restoring = False
        self.main.radioButtonPolygon.setChecked(True)  # 樹種メッシュは「任意のポリゴン」で集計する
        self.store_conifer_settings()
        self.refresh_aggregate_ui()
        missing = result["total"] - result["with_species"]
        message = (f"樹種メッシュを用意しました（{result['total']:,} マス）。"
                   "針葉樹フィルターの列と値を樹種で入れました（使うときはチェックを入れてください）。")
        if missing > result["total"] * 0.5:
            message += (f"\n\n樹種の入っていないマスが {missing:,} あります（森林簿の情報が無い地域など）。"
                        "樹種の無いマスは、針葉樹でないものとして扱われます。")
        QMessageBox.information(self.main, "樹種メッシュ", message)

    def remove_species_mesh(self, delete_file=True):
        """樹種メッシュのレイヤーをプロジェクトから外す（delete_file ならファイルも消す。
        取得した図郭は共有データに残るので、次に作るときは取得し直さない）"""
        from .processes.species_mesh import OUTPUT_KEY
        utils.remove_output_layers(utils.STAGE_ZONING, keys=[OUTPUT_KEY])
        output = utils.get_workspace_dir(DIR_AGGREGATE, "species_mesh.gpkg") if self.main.has_workspace() else ""
        for layer in list(QgsProject.instance().mapLayers().values()):
            if output and layer.source().split("|")[0] == output:
                QgsProject.instance().removeMapLayer(layer)
        if delete_file and output and os.path.exists(output):
            gc.collect()
            try:
                os.remove(output)
            except OSError:
                pass  # 掴まれていて消せなければ残す（次に作るときに書き直す）

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

    def _watch_conifer_layer(self, layer):
        """選んでいるポリゴンレイヤー1つだけを見張る（プロジェクト全体のレイヤーの増減は見張らない方針）。
        選択欄は、選んでいたレイヤーが消されると、ほかにポリゴンレイヤーがあれば「選択が変わった」の合図を
        出さずに別のレイヤーへ切り替わる。そのままでは針葉樹の値が前のレイヤーのまま残るため、
        消される合図を受けたら、削除が終わってから（処理が一巡した後に）選択欄に合わせて作り直す"""
        previous = self._conifer_watched_layer
        if previous is layer:
            return
        if previous is not None:
            try:
                previous.willBeDeleted.disconnect(self._on_conifer_layer_will_be_deleted)
            except (TypeError, RuntimeError):
                pass
        self._conifer_watched_layer = layer
        if layer is not None:
            layer.willBeDeleted.connect(self._on_conifer_layer_will_be_deleted)

    def stop_conifer_watch(self):
        """プラグインを外すときに呼ぶ。見張っているレイヤーは画面より長く残るので、合図の受け先から外す
        （外さないと、破棄した画面の選択欄に触れてエラーになる）"""
        self._watch_conifer_layer(None)

    def _on_conifer_layer_will_be_deleted(self):
        # 削除の途中で選択欄に触れると QGIS ごと落ちることがあるため、ここでは何もせず後回しにする
        self._conifer_watched_layer = None
        QTimer.singleShot(0, self.sync_conifer_layer)

    def sync_conifer_layer(self):
        """針葉樹の列の欄が、ポリゴンの選択欄と同じレイヤーを指しているかを確かめ、違えば作り直す。
        選択欄が合図を出さずに切り替わった場合（選んでいたレイヤーが消された等）の食い違いを直す"""
        combo = self.main.aggregatePolygonLayerCommbobox
        if sip.isdeleted(combo):
            return  # 後回しにしている間に画面が破棄された
        if hasattr(combo, "refresh_layers"):
            # レイヤーパネルの並びの選択欄：消えたレイヤーを候補から外す（選んでいたなら未選択に戻り、合図が出る）
            combo.refresh_layers()
        current = combo.currentLayer()
        if self._conifer_watched_layer is not current:
            self.on_conifer_layer_changed(current)

    def _set_conifer_fields(self, layer):
        """針葉樹を照合する列の候補を、レイヤーの列から fid（GeoPackage の行番号など、地物の番号）を除いて入れる。
        列の選択欄にはレイヤーを渡さず、列の一覧だけを渡す（特定の列を除く設定が選択欄に無いため）"""
        fields = QgsFields()
        if layer is not None:
            key_indexes = set(layer.primaryKeyAttributes()) if hasattr(layer, "primaryKeyAttributes") else set()
            for index, field in enumerate(layer.fields()):
                if field.name().lower() == "fid" or index in key_indexes:
                    continue
                fields.append(field)
        self.conifer_field_combo.setFields(fields)

    def on_conifer_layer_changed(self, layer):
        self._watch_conifer_layer(layer)
        saved = AggregateConiferManager().load()
        self._conifer_restoring = True
        try:
            self._set_conifer_fields(layer)
            if layer is not None and layer.source() == saved['layer_source']:
                # 前に使ったレイヤーなら、列・値・チェックを戻す（列が無くなった・候補から外した fid なら戻さない）
                if saved['field'] and self.conifer_field_combo.fields().indexOf(saved['field']) >= 0:
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
        # 針葉樹の設定が、いま選んでいるポリゴンレイヤーのものかを確かめる（食い違っていれば作り直してから判断する）
        self.sync_conifer_layer()
        if self.get_aggregate_error():
            self.refresh_aggregate_ui()
            return
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
            full_opacity=self.full_opacity_check.isChecked(),
        )
        # 結果のレイヤー追加と知らせは、進捗の窓を消してから行う（run_with_progress）
        outcome = run_with_progress(thread, show_detail=True)
        if "result" in outcome:
            self.add_layers_to_project(outcome["result"])
            # 樹種メッシュは集計に使うためだけのものなので、集計を終えたら外す（作り直しは選択欄から手動で）
            self.remove_species_mesh()

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
        # ゾーン統計量を見やすくするため、前の工程（ゾーニング図・スコアリング・要素の収益性・災害リスク）を OFF にして閉じる
        # （ゾーニングタブの操作でスコアリングが ON に戻っていることがあるため、ゾーニング図だけでなく前の工程すべて）
        utils.hide_output_stages((utils.STAGE_ZONING, utils.STAGE_SCORING, utils.STAGE_ELEMENTS))
