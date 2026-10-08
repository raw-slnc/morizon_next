# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import gc

# QGIS-API
from qgis.PyQt.QtWidgets import QMessageBox, QVBoxLayout
from qgis.core import QgsMapLayerProxyModel, QgsProject
from qgis.utils import iface

from .processes.raster_styler import (
    apply_output_blend_mode,
    write_qml_deviding_by_threshold,
)
from . import processes
from . import utils
from .progress_dialog import run_with_progress
from .zoning_threshold_view import ZoningThresholdPanel
from .constants import (
    DIR_ZONING,
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
        # しきい値を変えたら地図の色分けをすぐ描き直す（原版の「更新」ボタンは不要になったので、
        # 同じボタンを「初期値」に戻すボタンとして使う）
        self._suspend_style = False
        for button, name in (
            (self.main.zoningProfitUpdateButton, "profit"),
            (self.main.zoningRiskUpdateButton, "risk"),
        ):
            button.setText("初期値")
            # 画面定義では「更新」の幅（最大40px）なので、3文字が収まるよう広げる
            button.setMaximumWidth(button.fontMetrics().horizontalAdvance("初期値") + 24)
            button.setToolTip("しきい値を初期値（面積を半々に分ける位置をもとに求めた値）に戻す")
            button.clicked.connect(lambda _=False, n=name: self.reset_zoning_threshold(n))
        self.main.zoningProfitSpinbox.valueChanged.connect(lambda _: self.on_zoning_threshold_changed("profit"))
        self.main.zoningRiskSpinbox.valueChanged.connect(lambda _: self.on_zoning_threshold_changed("risk"))

        # ラスターレイヤーだけを選択可能に
        for combobox in (
            self.main.zoningProfitLayerCombobox,
            self.main.zoningRiskLayerCombobox,
        ):
            combobox.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.update_zoning_layer_scope()

        # 出力先未指定時は、プロジェクト内蔵のプラグイン管理フォルダをデフォルトにする
        # (set_morizon_layer_scopeがこの管理フォルダ配下しか候補にしないため、
        # 他タブと出力先を揃えておく必要がある)
        if self.main.zoningOutputDirFileWidget.filePath() == "":
            project_home = QgsProject.instance().homePath()
            if project_home != "":
                default_output_dir = utils.get_morizon_managed_dir(DIR_ZONING)
                os.makedirs(default_output_dir, exist_ok=True)
                self.main.zoningOutputDirFileWidget.setFilePath(default_output_dir)

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

        self.init_threshold_panel()
        self.refresh_zoning_ui()
        self.set_zoning_thresholds()

    def init_threshold_panel(self):
        """収益性・災害リスクの値の段階と面積の割合を、しきい値の位置と一緒に見せる図を、
        「既定のレイヤーを再読込する」の下（タブの空いている場所）に置く。表示だけで計算には関わらない"""
        self.threshold_panel = ZoningThresholdPanel(SCORING_COLORS_PROFIT, SCORING_COLORS_RISK)
        layout = self.main.findChild(QVBoxLayout, "verticalLayout_3")
        layout.insertWidget(2, self.threshold_panel)
        for signal in (
            self.main.zoningProfitLayerCombobox.layerChanged,
            self.main.zoningRiskLayerCombobox.layerChanged,
            self.main.zoningProfitSpinbox.valueChanged,
            self.main.zoningRiskSpinbox.valueChanged,
        ):
            signal.connect(self.refresh_threshold_panel)
        # ▲を動かしたら、しきい値の欄に入れる（欄の変更で図も描き直される）
        self.threshold_panel.profit.thresholdChanged.connect(self.main.zoningProfitSpinbox.setValue)
        self.threshold_panel.risk.thresholdChanged.connect(self.main.zoningRiskSpinbox.setValue)
        # カラーバーの下のボタンで、レイヤーの地図での表示を切り替える
        self.threshold_panel.profit.visibilityToggled.connect(
            lambda checked: self.set_axis_layer_visible(self.main.zoningProfitLayerCombobox, checked))
        self.threshold_panel.risk.visibilityToggled.connect(
            lambda checked: self.set_axis_layer_visible(self.main.zoningRiskLayerCombobox, checked))
        self.refresh_threshold_panel()

    @staticmethod
    def _layer_node(layer):
        if layer is None:
            return None
        return QgsProject.instance().layerTreeRoot().findLayer(layer.id())

    def set_axis_layer_visible(self, combobox, checked):
        node = self._layer_node(combobox.currentLayer())
        if node is None:
            return
        node.setItemVisibilityChecked(checked)
        if checked:
            # 親のグループ（スコアリング・Morizon Next）が OFF だと地図に出ないので、親も ON にする
            parent = node.parent()
            while parent is not None and parent.parent() is not None:
                parent.setItemVisibilityChecked(True)
                parent = parent.parent()

    def refresh_threshold_panel(self, *_):
        for view, combobox, spinbox in (
            (self.threshold_panel.profit, self.main.zoningProfitLayerCombobox, self.main.zoningProfitSpinbox),
            (self.threshold_panel.risk, self.main.zoningRiskLayerCombobox, self.main.zoningRiskSpinbox),
        ):
            layer = combobox.currentLayer()
            view.set_layer(layer, layer is not None and utils.is_valid_scoring_layer(layer),
                           self._initial_zoning_threshold)
            view.set_threshold(spinbox.value())
        self.sync_visible_buttons()

    def sync_visible_buttons(self):
        """表示ボタンを、レイヤーパネルのチェックの状態に合わせる"""
        if not hasattr(self, "threshold_panel"):
            return
        for view, combobox in (
            (self.threshold_panel.profit, self.main.zoningProfitLayerCombobox),
            (self.threshold_panel.risk, self.main.zoningRiskLayerCombobox),
        ):
            node = self._layer_node(combobox.currentLayer())
            view.set_visible_state(node is not None, node is not None and node.isVisible())

    def on_zoning_threshold_changed(self, layer_name):
        if not self._suspend_style:
            self.set_zoning_raster_style(layer_name)

    def reset_zoning_threshold(self, layer_name):
        """「初期値」ボタン：しきい値を初期値に戻す（欄の変更で地図と図も描き直される）"""
        combobox, spinbox = (
            (self.main.zoningProfitLayerCombobox, self.main.zoningProfitSpinbox) if layer_name == "profit"
            else (self.main.zoningRiskLayerCombobox, self.main.zoningRiskSpinbox))
        layer = combobox.currentLayer()
        if layer is not None and utils.is_valid_scoring_layer(layer):
            spinbox.setValue(self._initial_zoning_threshold(layer))

    @staticmethod
    def _initial_zoning_threshold(layer):
        """しきい値の初期値（set_zoning_thresholds と同じ求め方。面積がおおよそ半々になる値を整数に切り捨て）"""
        return int(utils.get_initial_thresholds(layer, classes_count=2)[0])

    def refresh_zoning_ui(self):
        """
        UIの変更の都度発火してUIの状態を更新する関数
        （候補の作り直しはしない。選択欄の合図の中から呼ばれるため。合わせ直しはメイン画面の refresh_all_tabs）
        """
        error_texts = self.get_zoning_error_texts()
        has_no_error = len(error_texts) == 0
        self.main.zoningErrorLabel.setText("\n".join(error_texts))
        self.main.zoningRunButton.setEnabled(has_no_error)
        # レイヤーパネルで表示を切り替えた場合も、タブを開き直したときなどに合わせる
        self.sync_visible_buttons()

        # 既定のレイヤーがすべて入っているとき（押しても変わらないとき）は、再読込ボタンをグレーアウトする
        self.main.zoningSetLayersButton.setEnabled(any(
            combobox.currentLayer() is not layer
            for combobox, layer in self.default_zoning_layers().items()
        ))

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

        # 既存の出力は実行時の上書き確認で置き換えるため、ここでは止めない

        if self.main.zoningOutputDirFileWidget.filePath() == "":
            error_texts.append("QGISプロジェクトを保存してください（出力先はプロジェクトと同じフォルダの morizon_next/<プロジェクトのファイル名> の中に決まります）")

        return error_texts

    def set_zoning_layer_combobox(self):
        """
        MORIZON管理フォルダ内のレイヤーを検索し、ゾーニングタブの各コンボボックスに対応するレイヤーをセットする
        """
        self.update_zoning_layer_scope()
        for combobox, layer in self.default_zoning_layers().items():
            combobox.setLayer(layer)

    def default_zoning_layers(self) -> dict:
        """各コンボボックスに入る既定のレイヤー（コンボボックス → レイヤー。見つからない欄は含めない）"""
        result = {}
        for name, combobox in (
            (OUTPUT_PROFIT["DISPLAY_NAME"], self.main.zoningProfitLayerCombobox),
            (OUTPUT_RISK["DISPLAY_NAME"], self.main.zoningRiskLayerCombobox),
        ):
            layer = utils.find_morizon_layer_by_name(name, allowed_extensions={".tif", ".tiff"})
            if layer is not None:
                result[combobox] = layer
        return result

    def select_restored_layers(self):
        """保存データの読み込み後、収益性・災害リスクの選択欄を作り直したレイヤーに合わせる"""
        self.update_zoning_layer_scope()
        for output_def, combobox in (
            (OUTPUT_PROFIT, self.main.zoningProfitLayerCombobox),
            (OUTPUT_RISK, self.main.zoningRiskLayerCombobox),
        ):
            layer = utils.find_morizon_layer_by_name(
                output_def["DISPLAY_NAME"], allowed_extensions={".tif", ".tiff"}
            )
            if layer is not None:
                combobox.setLayer(layer)

    def update_zoning_layer_scope(self):
        # 欄ごとに、入るべきレイヤーだけを候補にする。両方の欄に両方を許すと、スコアリングが
        # 収益性 → 災害リスクの順にレイヤーを追加したとき、空だった両方の欄が先に現れた収益性を
        # 自動で選んでしまい、災害リスクの欄にも収益性が入る
        for output_def, combobox in (
            (OUTPUT_PROFIT, self.main.zoningProfitLayerCombobox),
            (OUTPUT_RISK, self.main.zoningRiskLayerCombobox),
        ):
            utils.set_morizon_layer_scope(
                combobox,
                allowed_names={output_def["DISPLAY_NAME"]},
                allowed_extensions={".tif", ".tiff"},
            )

    def set_zoning_thresholds(self):
        """
        収益性・災害リスクのしきい値を、入力ラスターの値域から計算してセットする
        （途中の 0 で地図を描き直さないよう、入れ終えてから1回だけ描き直す）
        """
        self._suspend_style = True
        try:
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
                        spinbox.setValue(int(threshold))
        finally:
            self._suspend_style = False
        self.set_zoning_raster_style("profit")
        self.set_zoning_raster_style("risk")

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

        if not utils.is_usable_raster_layer(target_layer):
            return
        target_layer.loadNamedStyle(qml_filepath)
        target_layer.renderer().setOpacity(opacity)
        apply_output_blend_mode(target_layer)
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
        # ゾーニング図のレイヤーを片付ける（指している場所に関係なく。ファイルを上書きするかどうかとは別の話）
        # 片付けたレイヤーは削除せずに取り外して持っておき、上書きをキャンセルしたら元の位置に戻す。
        # 実行するなら、ファイルの掴みを解放するため、ここで削除する
        selections = self.main.snapshot_selections()
        detached = utils.detach_output_layers(utils.STAGE_ZONING)

        existing_filenames = self.get_existing_filenames()
        if len(existing_filenames) > 0:
            if QMessageBox.StandardButton.No == QMessageBox.question(
                self.main,
                "上書き確認",
                "出力先フォルダに同名ファイルが存在します、上書きしますか？\n" + "\n".join(existing_filenames),
                QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.No,
            ):
                utils.restore_detached_layers(detached)
                self.main.restore_selections(selections)
                QMessageBox.information(self.main, "処理中断", "処理を中断しました。")
                return
        utils.discard_detached_layers(detached)

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
        # 結果のレイヤー追加と知らせは、進捗の窓を消してから行う（run_with_progress）
        # 中断したときも、それまでに作れたレイヤーは追加する（前のレイヤーは実行前に外してあるため）
        outcome = run_with_progress(thread, abortable=False)
        if "result" in outcome:
            self.add_layers_to_project(outcome["result"])
        if "error" in outcome:
            QMessageBox.information(self.main, "エラー", f"エラーが発生しました。\n\n{outcome['error']}")
        elif thread.abort_flag:
            QMessageBox.information(self.main, "中断", "処理を中断しました。")
        else:
            QMessageBox.information(self.main, "終了", "処理が終了しました。")

    @staticmethod
    def add_layers_to_project(rlayers_dict):
        """
        処理結果をプロジェクトに追加
        """
        for key, rlayer in rlayers_dict.items():
            apply_output_blend_mode(rlayer)
            utils.tag_output_layer(rlayer, utils.STAGE_ZONING, key)
            QgsProject.instance().addMapLayer(rlayer, False)
            # 出力レイヤーは「Morizon Next」グループの中の一番上に追加する
            utils.get_morizon_output_group().insertLayer(0, rlayer)
        rlayers_dict.clear()
        gc.collect()
