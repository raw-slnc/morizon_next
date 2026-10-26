# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import json
import gc

# QGIS-API
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QDialog, QDoubleSpinBox, QFileDialog, QMessageBox
from qgis.core import QgsLayerTreeGroup, QgsMapLayer, QgsMapLayerProxyModel, QgsProject
from qgis.gui import QgsMapLayerComboBox
from qgis.utils import iface

from .settings_manager import SettingsManager
from .progress_dialog import run_with_progress

from .forest_zoning_scoring_stats_dialog import ForestZoningScoringStatsDialog
from .processes.raster_styler import (
    apply_output_blend_mode,
    write_qml_by_thresholds_and_colors,
)
from . import processes
from . import utils
from .constants import (
    DIR_ZONING,
    OUTPUT_GROUP_NAME,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_DOKUJI,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_SHC,
    OUTPUT_SLOPE,
    OUTPUT_SAVEAREA,
    OUTPUT_PARAMS_JSON,
    OUTPUT_PROFIT,
    OUTPUT_RISK,
    SCORING_COLORS_SITEIDX,
    SCORING_COLORS_DISTANCE,
    SCORING_COLORS_COST,
    SCORING_COLORS_SLOPE,
    SCORING_COLORS_SHC,
)


class ScoringObject:
    """
    スコアリングUIの各パラメタのcombobox、threshold1_spinbox、threshold2_spinboxを統合するクラス
    """

    def __init__(
        self,
        combobox: QgsMapLayerComboBox,
        threshold1_spinbox: QDoubleSpinBox,
        threshold2_spinbox: QDoubleSpinBox,
        laye_name: str,
    ):
        self.combobox = combobox
        self.threshold1_spinbox = threshold1_spinbox
        self.threshold2_spinbox = threshold2_spinbox
        self.layer_name = laye_name
        # しきい値の欄を動かしたとき、指が止まってから1回だけ塗り直すためのタイマー（ForestZoningMainDialogScoring が作る）
        self.style_timer = None

    def get_scoring_colors(self):
        if self.layer_name == "siteidx":
            scoring_colors = SCORING_COLORS_SITEIDX
        if self.layer_name == "distance":
            scoring_colors = SCORING_COLORS_DISTANCE
        if self.layer_name == "cost":
            scoring_colors = SCORING_COLORS_COST
        if self.layer_name == "slope":
            scoring_colors = SCORING_COLORS_SLOPE
        if self.layer_name == "shc":
            scoring_colors = SCORING_COLORS_SHC
        return scoring_colors

    def get_scores(self):
        settings_manager = SettingsManager()
        scores = settings_manager.get_setting(f"scores_{self.layer_name}")

        return scores


def get_initial_thresholds_of(obj: ScoringObject):
    if obj.layer_name == "slope":  # 「傾斜」だけはしきい値が固定値
        thresholds = (35, 45)
    elif obj.combobox.currentLayer() is not None and utils.is_valid_elements_layer(
        obj.combobox.currentLayer()
    ):
        thresholds = utils.get_initial_thresholds(obj.combobox.currentLayer())
    else:
        thresholds = (0, 0)

    return thresholds


class ForestZoningMainDialogScoring:
    """
    メイン画面の「スコアリング」タブの処理を実装するクラス

    要素の列（プルダウン・しきい値の欄・統計値・初期値）をユーザーが操作すると、その要素のスコアリングレイヤーを
    今のしきい値で塗り直し、地図に表示する（親のグループも ON にする）。表示は元に戻さない
    （プラグインからの切り替えも、レイヤーパネルでの操作も、ユーザーの操作として後から上書きしない）。
    プログラムが値を入れたとき（レイヤーの切り替わりに伴う初期値、params.json の読み込み、選択の復元）は表示を切り替えない
    """

    # しきい値の欄を動かしてから塗り直すまでの待ち時間（1段ごとに地図を描き直さないため）
    STYLE_DELAY_MS = 400

    def __init__(self, main):
        self.main = main
        self.init_scoring_ui()

    def init_scoring_ui(self):
        self.main.scoringRunPushButton.clicked.connect(self.run_scoring)
        self.set_scoring_score_labels_from_settings()
        self.main.scoringSetLayersPushbutton.clicked.connect(
            self.set_scoring_layer_combobox
        )

        self.scoring_objs_dict = {
            "siteidx": ScoringObject(
                self.main.scoringSiteidxLayerCombobox,
                self.main.scoringSiteidxThreshold1Spinbox,
                self.main.scoringSiteidxThreshold2Spinbox,
                "siteidx",
            ),
            "distance": ScoringObject(
                self.main.scoringDistanceLayerCombobox,
                self.main.scoringDistanceThreshold1Spinbox,
                self.main.scoringDistanceThreshold2Spinbox,
                "distance",
            ),
            "cost": ScoringObject(
                self.main.scoringCostLayerCombobox,
                self.main.scoringCostThreshold1Spinbox,
                self.main.scoringCostThreshold2Spinbox,
                "cost",
            ),
            "shc": ScoringObject(
                self.main.scoringShcLayerCombobox,
                self.main.scoringShcThreshold1Spinbox,
                self.main.scoringShcThreshold2Spinbox,
                "shc",
            ),
            "slope": ScoringObject(
                self.main.scoringSlopeLayerCombobox,
                self.main.scoringSlopeThreshold1Spinbox,
                self.main.scoringSlopeThreshold2Spinbox,
                "slope",
            ),
        }

        # レイヤー選択プルダウンをラスター限定に
        list(
            map(
                lambda obj: obj.combobox.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer),
                self.scoring_objs_dict.values(),
            )
        )
        self.main.scoringSaveareaLayerCombobox.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.update_scoring_layer_scope()

        # 出力先未指定時は、プロジェクト内蔵のプラグイン管理フォルダをデフォルトにする
        # (set_morizon_layer_scopeがこの管理フォルダ配下しか候補にしないため、
        # 他タブと出力先を揃えておく必要がある)
        if self.main.scoringOutputDirFileWidget.filePath() == "":
            project_home = QgsProject.instance().homePath()
            if project_home != "":
                default_output_dir = utils.get_morizon_managed_dir(DIR_ZONING)
                os.makedirs(default_output_dir, exist_ok=True)
                self.main.scoringOutputDirFileWidget.setFilePath(default_output_dir)

        # 初期値セット
        list(
            map(
                self.init_scoring_rlayer_stats,
                self.scoring_objs_dict.values(),
            )
        )
        # comboboxのcurrentlayerが変わると、そのパラメタの閾値だけリセットされる
        list(
            map(
                lambda obj: obj.combobox.layerChanged.connect(
                    lambda _layer=None, scoring_obj=obj: self.init_scoring_rlayer_stats(
                        scoring_obj
                    )
                ),
                self.scoring_objs_dict.values(),
            )
        )
        for obj in self.scoring_objs_dict.values():
            obj.combobox.layerChanged.connect(self.refresh_set_layers_button)
        self.main.scoringSaveareaLayerCombobox.layerChanged.connect(self.refresh_set_layers_button)

        # 列の操作で、その要素のスコアリングレイヤーを塗り直して表示する
        for obj in self.scoring_objs_dict.values():
            obj.style_timer = QTimer(self.main)
            obj.style_timer.setSingleShot(True)
            obj.style_timer.setInterval(self.STYLE_DELAY_MS)
            obj.style_timer.timeout.connect(
                lambda scoring_obj=obj: self.apply_row_operation(scoring_obj)
            )
            for spinbox in (obj.threshold1_spinbox, obj.threshold2_spinbox):
                spinbox.valueChanged.connect(
                    lambda _value, scoring_obj=obj, box=spinbox: self.on_threshold_value_changed(
                        scoring_obj, box
                    )
                )
            # activated はユーザーが選んだときだけ出る（setLayer やレイヤーの追加・削除では出ない）。
            # 初期値の設定（layerChanged）が済んでから塗り直すため、処理を後回しにする
            obj.combobox.activated.connect(
                lambda _index, scoring_obj=obj: QTimer.singleShot(
                    0, lambda: self.apply_row_operation(scoring_obj)
                )
            )

        # 統計値表示ボタン
        self.main.scoringSiteidxStatsButton.clicked.connect(
            lambda: self.show_scoring_rlayer_stats(
                self.scoring_objs_dict.get("siteidx")
            )
        )
        self.main.scoringCostStatsButton.clicked.connect(
            lambda: self.show_scoring_rlayer_stats(self.scoring_objs_dict.get("cost"))
        )
        self.main.scoringDistanceStatsButton.clicked.connect(
            lambda: self.show_scoring_rlayer_stats(
                self.scoring_objs_dict.get("distance")
            )
        )
        self.main.scoringSlopeStatsButton.clicked.connect(
            lambda: self.show_scoring_rlayer_stats(self.scoring_objs_dict.get("slope"))
        )
        self.main.scoringShcStatsButton.clicked.connect(
            lambda: self.show_scoring_rlayer_stats(self.scoring_objs_dict.get("shc"))
        )

        # 初期値に戻すボタン
        self.main.scoringSiteidxStyleInitPushbutton.clicked.connect(
            lambda: self.back_to_initial_state(self.scoring_objs_dict.get("siteidx"))
        )
        self.main.scoringCostStyleInitPushbutton.clicked.connect(
            lambda: self.back_to_initial_state(self.scoring_objs_dict.get("cost"))
        )
        self.main.scoringDistanceStyleInitPushbutton.clicked.connect(
            lambda: self.back_to_initial_state(self.scoring_objs_dict.get("distance"))
        )
        self.main.scoringShcStyleInitPushbutton.clicked.connect(
            lambda: self.back_to_initial_state(self.scoring_objs_dict.get("shc"))
        )
        self.main.scoringSlopeStyleInitPushbutton.clicked.connect(
            lambda: self.back_to_initial_state(self.scoring_objs_dict.get("slope"))
        )

        # パラメタ読み込みボタン
        self.main.scoringReadParamsPushbutton.clicked.connect(self.readin_params_json)

        # UIの変更を検知しUI全体を更新する
        for signal in (
            self.main.scoringSiteidxLayerCombobox.layerChanged,
            self.main.scoringCostLayerCombobox.layerChanged,
            self.main.scoringDistanceLayerCombobox.layerChanged,
            self.main.scoringSlopeLayerCombobox.layerChanged,
            self.main.scoringShcLayerCombobox.layerChanged,
            self.main.scoringSaveareaLayerCombobox.layerChanged,
            self.main.scoringProfitGroupbox.toggled,
            self.main.scoringDisasterGroupbox.toggled,
            self.main.scoringOutputDirFileWidget.fileChanged,
        ):
            signal.connect(self.refresh_scoring_ui)

        self.refresh_scoring_ui()

    def readin_params_json(self):
        result = QFileDialog.getOpenFileNames(self.main, "ファイル選択", "", "params.json")
        if len(result[0]) == 0:
            # 未選択なら処理を終了
            return

        self.apply_params_file(result[0][0])

    def apply_params_file(self, filepath):
        """params.json（スコアリング実行時に出力先へ書き出すしきい値）を各スピンボックスに反映し、
        各要素のスコアリングレイヤーをその値で塗り直す（表示の切り替えはしない）"""
        thresholds_dict = {}
        with open(filepath, encoding="utf-8") as f:
            thresholds_dict = json.load(f)
        # 実行したときの地位のレイヤー（スギ・ヒノキ・カラマツ・独自）に先に戻す。
        # 選択欄にレイヤーを入れるとしきい値が初期値に入れ直されるので、しきい値より前に行う
        # （記録の無い params.json（以前の版）では、今の選択のままにする）
        siteidx_name = thresholds_dict.get("siteidx_layer")
        if siteidx_name:
            layer = utils.find_morizon_layer_by_name(siteidx_name, allowed_extensions={".tif", ".tiff"})
            if layer is not None:
                self.main.scoringSiteidxLayerCombobox.setLayer(layer)
        try:
            self.main.scoringSiteidxThreshold1Spinbox.setValue(
                thresholds_dict.get("siteidx")[0]
            )
            self.main.scoringSiteidxThreshold2Spinbox.setValue(
                thresholds_dict.get("siteidx")[1]
            )
            self.main.scoringCostThreshold1Spinbox.setValue(
                thresholds_dict.get("cost")[0]
            )
            self.main.scoringCostThreshold2Spinbox.setValue(
                thresholds_dict.get("cost")[1]
            )
            self.main.scoringDistanceThreshold1Spinbox.setValue(
                thresholds_dict.get("distance")[0]
            )
            self.main.scoringDistanceThreshold2Spinbox.setValue(
                thresholds_dict.get("distance")[1]
            )
            self.main.scoringShcThreshold1Spinbox.setValue(
                thresholds_dict.get("shc")[0]
            )
            self.main.scoringShcThreshold2Spinbox.setValue(
                thresholds_dict.get("shc")[1]
            )
            self.main.scoringSlopeThreshold1Spinbox.setValue(
                thresholds_dict.get("slope")[0]
            )
            self.main.scoringSlopeThreshold2Spinbox.setValue(
                thresholds_dict.get("slope")[1]
            )
        except (TypeError, IndexError):
            QMessageBox.information(
                self.main, "エラー", "本プロクラムで生成したパラメータJSONファイルを選択してください。"
            )
            return
        for scoring_obj in self.scoring_objs_dict.values():
            self.set_scoring_raster_style(scoring_obj)

    def set_scoring_layer_combobox(self):
        """
        MORIZON管理フォルダ内のレイヤーを検索し、スコアリングタブの各コンボボックスに対応するレイヤーをセットする
        """
        self.update_scoring_layer_scope()
        for combobox, layer in self.default_scoring_layers().items():
            combobox.setLayer(layer)

    def default_scoring_layers(self) -> dict:
        """各コンボボックスに入る既定のレイヤー（コンボボックス → レイヤー。見つからない欄は含めない）"""
        name_suffix = "[スコアリング]"
        result = {}

        # "<DISPLAY_NAME>name_suffix"と一致するレイヤー名が存在する場合対応するコンボボックスに入れる
        # （地位は カラマツ → ヒノキ → スギ の順に探し、後に見つかったものを使う）
        for name, combobox in (
            (
                OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"] + name_suffix,
                self.main.scoringSiteidxLayerCombobox,
            ),
            (
                OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"] + name_suffix,
                self.main.scoringSiteidxLayerCombobox,
            ),
            (
                OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"] + name_suffix,
                self.main.scoringSiteidxLayerCombobox,
            ),
            (
                OUTPUT_COST["DISPLAY_NAME"] + name_suffix,
                self.main.scoringCostLayerCombobox,
            ),
            (
                OUTPUT_DISTANCE["DISPLAY_NAME"] + name_suffix,
                self.main.scoringDistanceLayerCombobox,
            ),
            (
                OUTPUT_SLOPE["DISPLAY_NAME"] + name_suffix,
                self.main.scoringSlopeLayerCombobox,
            ),
            (
                OUTPUT_SHC["DISPLAY_NAME"] + name_suffix,
                self.main.scoringShcLayerCombobox,
            ),
            (
                OUTPUT_SAVEAREA["DISPLAY_NAME"] + name_suffix,
                self.main.scoringSaveareaLayerCombobox,
            ),
        ):
            layer = utils.find_morizon_layer_by_name(name, allowed_extensions={".tif", ".tiff"})
            if layer is not None:
                result[combobox] = layer
        return result

    def refresh_set_layers_button(self):
        """既定のレイヤーがすべて入っているとき（押しても変わらないとき）は、再読込ボタンをグレーアウトする"""
        self.main.scoringSetLayersPushbutton.setEnabled(any(
            combobox.currentLayer() is not layer
            for combobox, layer in self.default_scoring_layers().items()
        ))

    def refresh_scoring_ui(self):
        # 候補の作り直しはしない（選択欄の合図の中から呼ばれるため。合わせ直しはメイン画面の refresh_all_tabs）
        self.refresh_set_layers_button()

        # 入力内容のエラーチェック
        error_texts = self.get_scoring_error_texts()
        self.main.scoringErrorLabel.setText("\n".join(error_texts))
        self.main.scoringRunPushButton.setEnabled(len(error_texts) == 0)

        # 各要素ごとのボタンの有効化チェック
        for combobox, stats_button, init_button in (
            (
                self.main.scoringSiteidxLayerCombobox,
                self.main.scoringSiteidxStatsButton,
                self.main.scoringSiteidxStyleInitPushbutton,
            ),
            (
                self.main.scoringCostLayerCombobox,
                self.main.scoringCostStatsButton,
                self.main.scoringCostStyleInitPushbutton,
            ),
            (
                self.main.scoringDistanceLayerCombobox,
                self.main.scoringDistanceStatsButton,
                self.main.scoringDistanceStyleInitPushbutton,
            ),
            (
                self.main.scoringSlopeLayerCombobox,
                self.main.scoringSlopeStatsButton,
                self.main.scoringSlopeStyleInitPushbutton,
            ),
            (
                self.main.scoringShcLayerCombobox,
                self.main.scoringShcStatsButton,
                self.main.scoringShcStyleInitPushbutton,
            ),
        ):
            # まずすべてのUIを無効化する
            stats_button.setEnabled(False)
            init_button.setEnabled(False)

            # 使用可能なボタンのみ有効化する
            if combobox.currentLayer() is not None:
                is_valid = utils.is_valid_elements_layer(combobox.currentLayer())
                stats_button.setEnabled(is_valid)
                init_button.setEnabled(is_valid)

    def update_scoring_layer_scope(self):
        name_suffix = "[スコアリング]"
        combobox_allowed_names = (
            (
                self.main.scoringSiteidxLayerCombobox,
                {
                    OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_DOKUJI["DISPLAY_NAME"] + name_suffix,
                },
            ),
            (
                self.main.scoringCostLayerCombobox,
                {OUTPUT_COST["DISPLAY_NAME"] + name_suffix},
            ),
            (
                self.main.scoringDistanceLayerCombobox,
                {OUTPUT_DISTANCE["DISPLAY_NAME"] + name_suffix},
            ),
            (
                self.main.scoringSlopeLayerCombobox,
                {OUTPUT_SLOPE["DISPLAY_NAME"] + name_suffix},
            ),
            (
                self.main.scoringShcLayerCombobox,
                {OUTPUT_SHC["DISPLAY_NAME"] + name_suffix},
            ),
            (
                self.main.scoringSaveareaLayerCombobox,
                {OUTPUT_SAVEAREA["DISPLAY_NAME"] + name_suffix},
            ),
        )
        for combobox, allowed_names in combobox_allowed_names:
            utils.set_morizon_layer_scope(
                combobox,
                allowed_names=allowed_names,
                allowed_extensions={".tif", ".tiff"},
            )

    def set_scoring_raster_style(self, scoring_obj: ScoringObject):
        target_layer = scoring_obj.combobox.currentLayer()
        if not utils.is_usable_raster_layer(target_layer):
            return False

        scores = scoring_obj.get_scores()
        scoring_colors = scoring_obj.get_scoring_colors()
        threshold1 = scoring_obj.threshold1_spinbox.value()
        threshold2 = scoring_obj.threshold2_spinbox.value()

        qml_filepath = write_qml_by_thresholds_and_colors(
            (threshold1, threshold2),
            scoring_colors,
            scores,
        )

        # 不透明度は今のレイヤーのものを引き継ぐ（塗り直すたびに不透明度が変わらないように）
        opacity = target_layer.renderer().opacity() if target_layer.renderer() is not None else None
        target_layer.loadNamedStyle(qml_filepath)
        if opacity is not None and target_layer.renderer() is not None:
            target_layer.renderer().setOpacity(opacity)
        iface.layerTreeView().refreshLayerSymbology(target_layer.id())  # レイヤー一覧の凡例を更新
        apply_output_blend_mode(target_layer)
        target_layer.triggerRepaint()  # キャンバス上の見た目を更新
        return True

    def on_threshold_value_changed(self, scoring_obj: ScoringObject, spinbox: QDoubleSpinBox):
        """しきい値の欄の値が変わったとき。ユーザーが欄を操作しているとき（欄にフォーカスがあるとき）だけ、
        指が止まってから塗り直す。プログラムが値を入れたときは何もしない（入れた側が必要なら塗り直す）"""
        if not spinbox.hasFocus():
            return
        scoring_obj.style_timer.start()

    def apply_row_operation(self, scoring_obj: ScoringObject):
        """要素の列をユーザーが操作したとき：今のしきい値で塗り直し、そのスコアリングレイヤーを地図に表示する"""
        scoring_obj.style_timer.stop()
        if self.set_scoring_raster_style(scoring_obj):
            self.show_layer_on_map(scoring_obj.combobox.currentLayer())

    @staticmethod
    def show_layer_on_map(layer):
        """レイヤーにチェックを入れ、親のグループ（要素・収益性／災害リスク・Morizon Next）も ON にする。
        最上位のグループ（Morizon Next）の中は、このレイヤーだけを表示する（災害リスクの要素どうしも、操作中は重ねない）。
        レイヤーパネルでは、このレイヤーまでの道筋とこのレイヤーの凡例だけを開き、ほかは閉じる（操作中の凡例を見るため）。
        最上位のグループの外は触らない"""
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is None:
            return
        node.setItemVisibilityChecked(True)
        path = []
        parent = node.parent()
        while parent is not None and parent.parent() is not None:
            parent.setItemVisibilityChecked(True)
            path.append(parent)
            parent = parent.parent()
        if not path:
            return

        def focus(group):
            """道筋の上にないものは、表示を外して閉じる（外すのはその項目のチェックだけで、中のチェックは残す）"""
            for child in group.children():
                on_path = child is node or child in path
                if not on_path and child.itemVisibilityChecked():
                    child.setItemVisibilityChecked(False)
                if isinstance(child, QgsLayerTreeGroup) and on_path:
                    focus(child)
                if child.isExpanded() != on_path:
                    child.setExpanded(on_path)

        top_group = path[-1]
        if top_group.name() != OUTPUT_GROUP_NAME:
            return  # ユーザーが Morizon Next の外へ動かしたレイヤーなら、ほかの表示と開閉はしない
        top_group.setExpanded(True)
        focus(top_group)

    def show_scoring_rlayer_stats(self, scoring_obj: ScoringObject):
        self.main.hide()

        stats_dialog = ForestZoningScoringStatsDialog(
            scoring_obj.layer_name,
            scoring_obj.combobox.currentLayer(),
            scoring_obj.threshold1_spinbox.value(),
            scoring_obj.threshold2_spinbox.value(),
        )
        result = stats_dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            # しきい値をメイン画面で反映
            threshold1, threshold2 = stats_dialog.get_thresholds()
            scoring_obj.threshold1_spinbox.setValue(threshold1)
            scoring_obj.threshold2_spinbox.setValue(threshold2)
            self.apply_row_operation(scoring_obj)

        self.main.show()
        self.main.raise_()
        self.main.activateWindow()

    def init_scoring_rlayer_stats(self, scoring_obj: ScoringObject):
        """レイヤがリセットされた時の挙動（しきい値を初期値にする。レイヤーは初期値の色分けで作られているので塗り直さない）"""

        thresholds = get_initial_thresholds_of(scoring_obj)

        scoring_obj.threshold1_spinbox.setValue(thresholds[0])
        scoring_obj.threshold2_spinbox.setValue(thresholds[1])

    def back_to_initial_state(self, scoring_obj: ScoringObject):
        """「初期値」ボタンがクリックした時の挙動"""
        thresholds = get_initial_thresholds_of(scoring_obj)

        scoring_obj.threshold1_spinbox.setValue(thresholds[0])
        scoring_obj.threshold2_spinbox.setValue(thresholds[1])
        self.apply_row_operation(scoring_obj)

    def set_scoring_score_labels_from_settings(self):
        smanager = SettingsManager()
        settings = smanager.get_settings()

        for scores, target_labels in (
            (
                settings["scores_siteidx"],
                (
                    self.main.scoringSiteidxScore1Label,
                    self.main.scoringSiteidxScore2Label,
                    self.main.scoringSiteidxScore3Label,
                ),
            ),
            (
                settings["scores_cost"],
                (
                    self.main.scoringCostScore1Label,
                    self.main.scoringCostScore2Label,
                    self.main.scoringCostScore3Label,
                ),
            ),
            (
                settings["scores_distance"],
                (
                    self.main.scoringDistanceScore1Label,
                    self.main.scoringDistanceScore2Label,
                    self.main.scoringDistanceScore3Label,
                ),
            ),
            (
                settings["scores_slope"],
                (
                    self.main.scoringSlopeScore1Label,
                    self.main.scoringSlopeScore2Label,
                    self.main.scoringSlopeScore3Label,
                ),
            ),
            (
                settings["scores_shc"],
                (
                    self.main.scoringShcScore1Label,
                    self.main.scoringShcScore2Label,
                    self.main.scoringShcScore3Label,
                ),
            ),
            (
                settings["scores_savearea"],
                (
                    self.main.scoringSaveareaScore1Label,
                    self.main.scoringSaveareaScore2Label,
                ),
            ),
        ):
            for idx, score in enumerate(scores):
                target_labels[idx].setText(str(score) + "点")

    def get_scoring_error_texts(self):
        error_texts = []
        name_suffix = "[スコアリング]"

        for name, combobox, allowed_names in (
            (
                "地位",
                self.main.scoringSiteidxLayerCombobox,
                {
                    OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"] + name_suffix,
                    OUTPUT_SITEIDX_DOKUJI["DISPLAY_NAME"] + name_suffix,
                },
            ),
            (
                OUTPUT_COST["DISPLAY_NAME"],
                self.main.scoringCostLayerCombobox,
                {OUTPUT_COST["DISPLAY_NAME"] + name_suffix},
            ),
            (
                OUTPUT_DISTANCE["DISPLAY_NAME"],
                self.main.scoringDistanceLayerCombobox,
                {OUTPUT_DISTANCE["DISPLAY_NAME"] + name_suffix},
            ),  # nopep8
            (
                OUTPUT_SLOPE["DISPLAY_NAME"],
                self.main.scoringSlopeLayerCombobox,
                {OUTPUT_SLOPE["DISPLAY_NAME"] + name_suffix},
            ),
            (
                OUTPUT_SHC["DISPLAY_NAME"],
                self.main.scoringShcLayerCombobox,
                {OUTPUT_SHC["DISPLAY_NAME"] + name_suffix},
            ),
            (
                OUTPUT_SAVEAREA["DISPLAY_NAME"],
                self.main.scoringSaveareaLayerCombobox,
                {OUTPUT_SAVEAREA["DISPLAY_NAME"] + name_suffix},
            ),  # nopep8
        ):

            if not combobox.parent().isChecked():
                # 親カテゴリにチェックがないならエラー判定を行わない
                continue

            if combobox.currentLayer() is None:
                error_texts.append(f"{name}ラスターを指定してください")
                continue

            if combobox.currentLayer().type() != QgsMapLayer.LayerType.RasterLayer:
                error_texts.append(f"{name}ラスターを指定してください")
                continue

            if not utils.is_morizon_managed_layer(
                combobox.currentLayer(),
                allowed_names=allowed_names,
                allowed_extensions={".tif", ".tiff"},
            ):
                error_texts.append(f"{name}ラスターを指定してください")
                continue

            # 統計量をもとにした妥当性チェック
            if not utils.is_valid_elements_layer(combobox.currentLayer()):
                # ラスタータイルはここで引っかかる: MIN=1000000 MEAN=1000000 MAX=-10000 となるため
                error_texts.append(f"有効な{name}ラスターを指定してください")

        # 既存の出力は実行時の上書き確認で置き換えるため、ここでは止めない

        if (
            not self.main.scoringSiteidxLayerCombobox.parent().isChecked()
            and not self.main.scoringSlopeLayerCombobox.parent().isChecked()
        ):
            error_texts.append("「収益性軸」「災害リスク軸」のいずれかにひとつ以上にチェックしてください")

        if self.main.scoringOutputDirFileWidget.filePath() == "":
            error_texts.append("QGISプロジェクトを保存してください（出力先はプロジェクトと同じフォルダの morizon_next/<プロジェクトのファイル名> の中に決まります）")

        return error_texts

    def scoring_get_existing_filenames(self):
        """
        「スコアリング」で、出力先フォルダに同名ファイルが存在するかチェック
        存在する場合そのすべてのファイル名の配列を返す

        Returns:
            list
        """
        output_dir = self.main.scoringOutputDirFileWidget.filePath()
        existing_filenames = []

        def append_filename_if_exist(file_info: dict):
            if os.path.exists(
                os.path.join(
                    output_dir, f"{file_info['FILE_NAME']}.{file_info['EXTENSION']}"
                )
            ):
                existing_filenames.append(
                    f"{file_info['FILE_NAME']}.{file_info['EXTENSION']}"
                )

        append_filename_if_exist(OUTPUT_PARAMS_JSON)
        if self.main.scoringSiteidxLayerCombobox.parent().isChecked():
            append_filename_if_exist(OUTPUT_PROFIT)
        if self.main.scoringSlopeLayerCombobox.parent().isChecked():
            append_filename_if_exist(OUTPUT_RISK)

        return existing_filenames

    def run_scoring(self):
        # 作る軸のレイヤーを片付ける（指している場所に関係なく。ファイルを上書きするかどうかとは別の話）
        axis_names = set()
        if self.main.scoringSiteidxLayerCombobox.parent().isChecked():
            axis_names.add(OUTPUT_PROFIT["DISPLAY_NAME"])
        if self.main.scoringSlopeLayerCombobox.parent().isChecked():
            axis_names.add(OUTPUT_RISK["DISPLAY_NAME"])
        # 片付けたレイヤーは削除せずに取り外して持っておき、上書きをキャンセルしたら元の位置に戻す。
        # 実行するなら、ファイルの掴みを解放するため、ここで削除する
        selections = self.main.snapshot_selections()
        detached = utils.detach_output_layers(utils.STAGE_SCORING, axis_names)

        existing_filenames = self.scoring_get_existing_filenames()
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
            "siteidx": self.main.scoringSiteidxLayerCombobox.currentLayer(),
            "cost": self.main.scoringCostLayerCombobox.currentLayer(),
            "distance": self.main.scoringDistanceLayerCombobox.currentLayer(),
            "slope": self.main.scoringSlopeLayerCombobox.currentLayer(),
            "shc": self.main.scoringShcLayerCombobox.currentLayer(),
            "savearea": self.main.scoringSaveareaLayerCombobox.currentLayer(),
        }

        input_thresholds_dict = {
            "siteidx": (
                self.main.scoringSiteidxThreshold1Spinbox.value(),
                self.main.scoringSiteidxThreshold2Spinbox.value(),
            ),
            "cost": (
                self.main.scoringCostThreshold1Spinbox.value(),
                self.main.scoringCostThreshold2Spinbox.value(),
            ),
            "distance": (
                self.main.scoringDistanceThreshold1Spinbox.value(),
                self.main.scoringDistanceThreshold2Spinbox.value(),
            ),
            "slope": (
                self.main.scoringSlopeThreshold1Spinbox.value(),
                self.main.scoringSlopeThreshold2Spinbox.value(),
            ),
            "shc": (
                self.main.scoringShcThreshold1Spinbox.value(),
                self.main.scoringShcThreshold2Spinbox.value(),
            ),
        }

        # processingを実行する前にパラメタを書き出す
        params_filepath = os.path.join(
            self.main.scoringOutputDirFileWidget.filePath(), "params.json"
        )
        # 地位はスギ・ヒノキ・カラマツ・独自から選ぶので、どれを使ったかも残す（再開のとき、しきい値と同じ地位に戻すため）
        siteidx_layer = self.main.scoringSiteidxLayerCombobox.currentLayer()
        params = dict(input_thresholds_dict)
        if siteidx_layer is not None:
            params["siteidx_layer"] = siteidx_layer.name()
        # レイヤー名（日本語）は \u の形で書き、ファイルは ASCII だけにする。原版や他の派生版が、
        # 文字コードを指定せずに読んでも（Windows では cp932）読めるようにするため
        with open(params_filepath, "wt", encoding="utf-8") as file:
            json.dump(params, file, ensure_ascii=True, indent=2)

        target_score_dict = {
            "profit": self.main.scoringSiteidxLayerCombobox.parent().isChecked(),
            "risk": self.main.scoringSlopeLayerCombobox.parent().isChecked(),
        }

        thread = processes.scoring.ProcessingThread(
            input_layers_dict,
            input_thresholds_dict,
            target_score_dict,
            self.main.scoringOutputDirFileWidget.filePath(),
        )
        # 結果のレイヤー追加と知らせは、進捗の窓を消してから行う（run_with_progress）
        # 中断したときも、それまでに作れたレイヤーは追加する（前のレイヤーは実行前に外してあるため）
        outcome = run_with_progress(thread)
        if "result" in outcome:
            self.add_layers_to_project(outcome["result"])
            # 作った「スコアリング」グループを表示して凡例まで開き、ほかの工程は OFF にして閉じる
            # （前の工程の要素の収益性・災害リスクと、やり直しのときに残っている後の工程のゾーニング図・ゾーン統計量）
            utils.activate_output_stage(utils.STAGE_SCORING, utils.SCORING_OTHER_STAGES)
        if "error" in outcome:
            QMessageBox.information(self.main, "エラー", f"エラーが発生しました。\n\n{outcome['error']}")
        elif thread.abort_flag:
            QMessageBox.information(self.main, "中断", "処理を中断しました。")
        else:
            QMessageBox.information(self.main, "終了", "処理が終了しました。")

    @staticmethod
    def add_layers_to_project(rlayers_dict):
        """
        処理結果を受け取ってレイヤー群を1つのグループとしてプロジェクトに追加
        """
        # 出力レイヤーは「Morizon Next」グループの中にまとめる
        root = utils.get_morizon_output_group()
        group_node = root.insertGroup(0, "スコアリング")
        group_node.setExpanded(False)

        # 収益性と災害リスクを重ねて見られるよう、グループ内は排他にしない。作ったときはグループ・レイヤーとも表示する
        # （続くゾーニングタブで、しきい値による色分けを地図で見ながら進めるため）
        group_node.setItemVisibilityChecked(True)
        root.setItemVisibilityChecked(True)  # 親（Morizon Next）が OFF だと地図に出ないため。ほかの子の状態は変わらない
        for key, rlayer in rlayers_dict.items():
            apply_output_blend_mode(rlayer)
            utils.tag_output_layer(rlayer, utils.STAGE_SCORING, key)
            QgsProject.instance().addMapLayer(rlayer, False)
            group_node.addLayer(rlayer).setItemVisibilityChecked(True)
        rlayers_dict.clear()
        gc.collect()
