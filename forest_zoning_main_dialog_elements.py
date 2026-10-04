# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import gc

# QGIS-API
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPoint, QTimer, QUrl, Qt
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
)
from qgis.core import (
    QgsFillSymbol, QgsLayerTreeGroup, QgsLineSymbol, QgsProject, QgsRectangle, QgsSingleSymbolRenderer,
    QgsVectorLayer,
)

from .progress_dialog import run_with_progress
from .forest_zoning_dem_browser_dialog import DemBrowserDialog, DemResolutionDialog
from .dem_loader import GSITileDEMLoader
from .settings_manager import OutputLayerStyleManager, ShcMethodManager
from .forest_zoning_main_dialog_archive import COMPANION_EXTENSIONS, ForestZoningMainDialogArchive
from . import saga_check
from . import processes
from . import morizon_data
from . import layer_db
from . import utils
from .processes.raster_styler import apply_output_blend_mode
from .constants import (
    COSTCSV_TEMPLATE_FILE,
    COSTCSV_TEMPLATE_NAME,
    DIR_DATA,
    DIR_YOUSO,
    INPUT_DEM,
    INPUT_COSTCSV,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_SHC,
    OUTPUT_SLOPE,
    OUTPUT_SAVEAREA,
)
from . import zoningkit_fetcher
from . import fgd_fetcher
from .fgd_login_dialog import FgdLoginDialog
from .forest_zoning_main_dialog_costcsv_editor import CostCsvEditorWidget


# 公式の手引（G空間情報センターで公開されているPDF）。ダイアログには題名だけを出し、開くのはボタンから。
# 末尾の ?preview=1 はサイトがPDFをページ内に表示するときの指定で、付けないとダウンロードになる
MORIZON_GUIDE_URL = (
    "https://www.geospatial.jp/ckan/dataset/84476079-c049-4b8f-b46a-3280e9bc2cc4"
    "/resource/a4d0ae9c-36e8-48c6-8474-3d1d4312e061/download/shinrin_zoning_tebiki_r8.pdf?preview=1"
)
MORIZON_GUIDE_TITLE = "収益性と災害リスクを考慮した森林ゾーニングの手引き（令和8年3月）"
# 上の手引で、道路データと地利について書かれたページ
MORIZON_GUIDE_ROAD_PAGES = "p.42〜43/p.71〜72"

# 「道路と建物を出力」で出すレイヤーのグループと表示。建築物は国土地理院のタイル（地理院地図）に似せた表示
# （オレンジの塗り・外周線なし）、道路縁は細い黒線（ラスターの上でも見分けやすいように。
# 基盤地図情報の道路縁と同じ濃い緑にするなら "0,96,42,255"）
ROAD_BUILDING_GROUP_NAME = "道路・建物"
ROAD_LINE_COLOR = "0,0,0,255"
BUILDING_FILL_COLOR = "244,139,41,255"


class _AlignRightToPathField(QObject):
    """開始ボタンの並びの右端を、入力欄のパス表示部分（「…」ボタンの手前）に揃える。
    「…」の幅は表示環境のスタイルで変わるため固定値にせず、パス表示部分の位置・大きさが
    変わるたびに実際の位置から右側の余白を測り直す"""

    def __init__(self, file_widget, layout):
        super().__init__(file_widget)
        self._file_widget = file_widget
        self._layout = layout
        file_widget.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            path_right = obj.mapTo(self._file_widget, QPoint(obj.width(), 0)).x()
            margin = max(self._file_widget.width() - path_right, 0)
            current = self._layout.contentsMargins()
            if current.right() != margin:
                self._layout.setContentsMargins(current.left(), current.top(), margin, current.bottom())
        return False


class ForestZoningMainDialogElements:
    """
    メイン画面の「要素計算」タブの処理を実装するクラス
    """

    def __init__(self, main):
        self.main = main
        self._calc_checkbox_was_ready = {}
        self.init_elements_ui()

    def init_elements_ui(self):
        # connect signals
        self.main.elementsRunPushButton.clicked.connect(self.run_elements)
        self.main.elementsLoadFromDirPushButton.clicked.connect(
            self.load_elements_files_from_dir
        )
        self.main.elementsStartFromDemBrowserPushButton.clicked.connect(
            self.start_from_dem_browser
        )
        self.main.elementsClearSettingsPushButton.clicked.connect(
            self.clear_elements_settings
        )
        self._init_output_blend_option()
        # 「…」で選んだファイルを作業フォルダへ取り込む
        self._input_paths = {}
        for key, filewidget in self.input_filewidgets().items():
            filewidget.fileChanged.connect(lambda _path, k=key: self._on_input_file_changed(k))
        self._button_row_alignment = _AlignRightToPathField(
            self.main.elementsDemFileWidget, self.main.horizontalLayout
        )
        # UIの変更を検知しUI全体を更新する
        for signal in (
            self.main.elementsDemFileWidget.fileChanged,
            self.main.elementsNppFileWidget.fileChanged,
            self.main.elementsSradFileWidget.fileChanged,
            self.main.elementsVtexFileWidget.fileChanged,
            self.main.elementsBuildingFileWidget.fileChanged,
            self.main.elementsNetworkFileWidget.fileChanged,
            self.main.elementsCostCsvFileWidget.fileChanged,
            self.main.elementsSiteIdxCheckbox.stateChanged,
            self.main.elementsCostCheckbox.stateChanged,
            self.main.elementsDistanceCheckbox.stateChanged,
            self.main.elementsShcCheckbox.stateChanged,
            self.main.elementsSlopeCheckbox.stateChanged,
            self.main.elementsSaveareaCheckbox.stateChanged,
            self.main.elementsOutputDirFileWidget.fileChanged,
        ):
            signal.connect(self.refresh_elements_ui)

        # 出力先未指定時は、プロジェクト内蔵のプラグイン管理フォルダをデフォルトにする
        if self.main.elementsOutputDirFileWidget.filePath() == "":
            project_home = QgsProject.instance().homePath()
            if project_home != "":
                default_output_dir = utils.get_morizon_managed_dir(DIR_YOUSO)
                os.makedirs(default_output_dir, exist_ok=True)
                self.main.elementsOutputDirFileWidget.setFilePath(default_output_dir)

        self.costcsv_editor = CostCsvEditorWidget(
            self.main,
            on_export=self.handle_costcsv_export,
            on_open_template=self.open_costcsv_template,
            on_import=self.import_costcsv,
        )
        self.main.costCsvGroupBoxLayout.addWidget(self.costcsv_editor)

        self.refresh_elements_ui()

    def _init_output_blend_option(self):
        manager = OutputLayerStyleManager()
        self.main.morizonMultiplyOutputCheckbox.blockSignals(True)
        self.main.morizonMultiplyOutputCheckbox.setChecked(
            manager.load_apply_multiply()
        )
        self.main.morizonMultiplyOutputCheckbox.blockSignals(False)
        self.main.morizonMultiplyOutputCheckbox.setToolTip(self.MULTIPLY_OFF_NOTICE)
        self.main.morizonMultiplyOutputCheckbox.toggled.connect(
            self.handle_output_blend_option_toggled
        )

    # 乗算をオフにするときの案内（チェックボックスのツールチップにも同じ文章を出す）
    MULTIPLY_OFF_NOTICE = (
        "乗算をオフにすると、表示が軽くなる場合があります。<br>"
        "その代わり、上に重ねたレイヤーが下のレイヤーを隠すため、"
        "重ねて見るときはレイヤーの順番と不透明度の調整が必要になります。<br><br>"
        "PCの性能に余裕があるなら、乗算での出力が MORIZON 本来の使い方に忠実です。"
    )

    def handle_output_blend_option_toggled(self, checked: bool):
        OutputLayerStyleManager().store_apply_multiply(checked)
        if not checked:
            box = QMessageBox(self.main)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("乗算出力をオフにします")
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(self.MULTIPLY_OFF_NOTICE)
            box.exec()

    def open_costcsv_template(self):
        """原版の作業システムExcelを DATA/SAGYO-SYSTEM_CSV に置き、既定のアプリで開く。
        すでに置いてある場合は、編集途中の内容を消さないようそのファイルを開く"""
        if not self.main.has_workspace():
            QMessageBox.information(
                self.main, "エラー", "先にQGISプロジェクトを保存してください（Excelを作業フォルダに置きます）。"
            )
            return
        target = utils.get_workspace_dir(DIR_DATA, *INPUT_COSTCSV["PATH"], COSTCSV_TEMPLATE_NAME)
        if not os.path.isfile(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(os.path.dirname(__file__), COSTCSV_TEMPLATE_FILE), target)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(target)):
            QMessageBox.information(
                self.main, "Excelを開けません",
                "xlsxを開けるアプリが見つかりませんでした。次のファイルを表計算ソフトで開いてください。\n"
                f"{target}"
            )

    def import_costcsv(self):
        """作業システムCSVを選び、パターンと機材名を作業システム設定の表示に読み込む"""
        start_dir = utils.get_workspace_dir(DIR_DATA, *INPUT_COSTCSV["PATH"])
        if not os.path.isdir(start_dir):
            start_dir = ""
        path, _ = QFileDialog.getOpenFileName(
            self.main, "作業システムCSVを選択", start_dir, "CSVファイル (*.csv *.CSV)"
        )
        if not path:
            return
        try:
            self.costcsv_editor.load_from_csv(path)
        except (ValueError, OSError) as e:
            QMessageBox.information(self.main, "CSVインポート", f"読み込めませんでした。\n\n{e}")

    def handle_costcsv_export(self, editor_widget):
        if not self.main.has_workspace():
            QMessageBox.information(
                self.main, "エラー", "先にQGISプロジェクトを保存してください。"
            )
            return
        target_dir = utils.get_workspace_dir(DIR_DATA, *INPUT_COSTCSV["PATH"])
        output_path = os.path.join(target_dir, "costcsv.csv")
        # 1種類1ファイルにそろえる（Excel など CSV 以外は残す）
        others = [path for path in self._files_of_type(target_dir, INPUT_COSTCSV["EXT"])
                  if os.path.normcase(path) != os.path.normcase(output_path)]
        if not self._confirm_replace_external(target_dir, others):
            return
        try:
            editor_widget.export_to_csv(output_path)
        except ValueError as e:
            QMessageBox.warning(self.main, "入力エラー", str(e))
            return
        for path in others:
            try:
                os.remove(path)
            except OSError:
                pass
        self._set_input_path("costcsv", output_path)
        QMessageBox.information(
            self.main, "完了", f"作業システムCSVを書き出しました。\n{output_path}"
        )

    def refresh_elements_ui(self):
        self.update_elements_calculate_checkboxes()
        self.set_elements_filewidgets_enabled()
        self.update_elements_status_labels()
        other_errors = self.get_elements_error_texts()
        self.main.elementsErrorLabel.setText("\n".join(other_errors))
        missing_inputs = self.get_missing_mandatory_inputs()
        self.main.elementsRunPushButton.setEnabled(
            len(other_errors) == 0 and not any(missing_inputs.values())
        )

    def get_elements_filewidgets(self) -> dict:
        return {
            "dem": self.main.elementsDemFileWidget,
            "npp": self.main.elementsNppFileWidget,
            "srad": self.main.elementsSradFileWidget,
            "vtex": self.main.elementsVtexFileWidget,
            "building": self.main.elementsBuildingFileWidget,
            "network": self.main.elementsNetworkFileWidget,
            "costcsv": self.main.elementsCostCsvFileWidget,
        }

    def get_missing_mandatory_inputs(self) -> dict:
        """未設定の入力項目を{key: bool}で返す(Trueが未設定)。
        チェックされている計算項目に必要な入力だけを対象にする"""
        mandatory_files_dict = self.get_elements_mandatory_files_dict()
        return {
            key: mandatory_files_dict[key] and widget.filePath() == ""
            for key, widget in self.get_elements_filewidgets().items()
        }

    def update_elements_status_labels(self):
        """各入力行の右端に「未設定」「設定済」を表示する"""
        missing_inputs = self.get_missing_mandatory_inputs()
        mandatory_files_dict = self.get_elements_mandatory_files_dict()
        status_labels = {
            "dem": self.main.elementsDemStatusLabel,
            "npp": self.main.elementsNppStatusLabel,
            "srad": self.main.elementsSradStatusLabel,
            "vtex": self.main.elementsVtexStatusLabel,
            "building": self.main.elementsBuildingStatusLabel,
            "network": self.main.elementsNetworkStatusLabel,
            "costcsv": self.main.elementsCostCsvStatusLabel,
        }
        for key, label in status_labels.items():
            if missing_inputs[key]:
                label.setText("未設定")
                label.setStyleSheet("color:#ff0000;")
            elif not mandatory_files_dict[key]:
                label.setText("任意")
                label.setStyleSheet("color:#666666;")
            else:
                label.setText("設定済")
                label.setStyleSheet("color:#188038;")

    def update_elements_calculate_checkboxes(self):
        """
        「計算する要素を選択」のチェックボックスを、対応するデータの設定状況に連動させる。
        データ未設定の間は未チェック・操作不可（グレーアウト）にし、データが揃った直後に
        自動でチェックを入れる（以降は手動でオフにできる。ユーザーの選択を毎回上書きしない
        よう、揃った瞬間の一回だけ自動チェックする）
        """
        w = self.get_elements_filewidgets()
        dem_set = w["dem"].filePath() != ""
        checkbox_conditions = {
            self.main.elementsSiteIdxCheckbox: (
                dem_set and w["npp"].filePath() != "" and w["srad"].filePath() != ""
                and w["vtex"].filePath() != ""
            ),
            self.main.elementsSaveareaCheckbox: dem_set and w["building"].filePath() != "",
            self.main.elementsDistanceCheckbox: dem_set and w["network"].filePath() != "",
            self.main.elementsCostCheckbox: dem_set and w["costcsv"].filePath() != "",
            self.main.elementsShcCheckbox: dem_set,
            self.main.elementsSlopeCheckbox: dem_set,
        }
        for checkbox, ready in checkbox_conditions.items():
            checkbox.setEnabled(ready)
            was_ready = self._calc_checkbox_was_ready.get(checkbox, False)
            if not ready:
                checkbox.setChecked(False)
            elif not was_ready:
                checkbox.setChecked(True)
            self._calc_checkbox_was_ready[checkbox] = ready

    def get_elements_mandatory_files_dict(self) -> dict:
        """
        チェックボックスの状態をもとに各データが必須か否かの辞書を取得する

        Returns:
            dict: {[key:str]: bool}
        """
        return {
            "dem": (
                self.main.elementsSiteIdxCheckbox.isChecked()
                or self.main.elementsCostCheckbox.isChecked()
                or self.main.elementsDistanceCheckbox.isChecked()
                or self.main.elementsShcCheckbox.isChecked()
                or self.main.elementsSlopeCheckbox.isChecked()
                or self.main.elementsSaveareaCheckbox.isChecked()
            ),
            "npp": self.main.elementsSiteIdxCheckbox.isChecked(),
            "srad": self.main.elementsSiteIdxCheckbox.isChecked(),
            "vtex": self.main.elementsSiteIdxCheckbox.isChecked(),
            "building": self.main.elementsSaveareaCheckbox.isChecked(),
            "network": self.main.elementsDistanceCheckbox.isChecked(),
            "costcsv": self.main.elementsCostCheckbox.isChecked(),
        }

    def set_elements_filewidgets_enabled(self):
        mondatory_files_dict = self.get_elements_mandatory_files_dict()
        self.main.elementsDemFileWidget.setEnabled(mondatory_files_dict["dem"])
        self.main.elementsNppFileWidget.setEnabled(mondatory_files_dict["npp"])
        self.main.elementsSradFileWidget.setEnabled(mondatory_files_dict["srad"])
        self.main.elementsVtexFileWidget.setEnabled(mondatory_files_dict["vtex"])
        self.main.elementsBuildingFileWidget.setEnabled(
            mondatory_files_dict["building"]
        )
        self.main.elementsNetworkFileWidget.setEnabled(mondatory_files_dict["network"])
        self.main.elementsCostCsvFileWidget.setEnabled(mondatory_files_dict["costcsv"])

    def get_elements_error_texts(self) -> list:
        """
        要素計算タブのUIの状態が不正な場合、全ての不正項目についてエラー文の配列を返す

        Returns:
            [list[str]]
        """
        error_texts = []
        mondatory_files_dict = self.get_elements_mandatory_files_dict()

        # 既存の出力は実行時の上書き確認で置き換えるため、ここでは止めない
        if not mondatory_files_dict["dem"]:
            error_texts.append("計算する要素をひとつ以上選択してください")
        if self.main.elementsOutputDirFileWidget.filePath() == "":
            error_texts.append("QGISプロジェクトを保存してください（出力先はプロジェクトと同じフォルダの morizon_next/<プロジェクトのファイル名> の中に決まります）")

        return error_texts

    def _confirm_shc_method(self) -> bool:
        """地形の複雑さを SAGA で計算する設定（SAGA ON）なのに SAGA が使えない場合、計算を始める前に知らせる。
        OFF（プラグイン内で計算）に切り替えて続けるなら True、やめるなら False"""
        if not (self.main.elementsShcCheckbox.isChecked() and ShcMethodManager().load_use_saga()):
            return True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            check = saga_check.check_saga()
        finally:
            QApplication.restoreOverrideCursor()
        if check["ok"]:
            return True
        answer = QMessageBox.question(
            self.main, "SAGA を使えません",
            "地形の複雑さを SAGA で計算する設定（SAGA ON）になっていますが、SAGA を使えません。\n\n"
            + check["problem"]
            + "\n\nOFF（プラグイン内で計算）に切り替えて実行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        ShcMethodManager().store_use_saga(False)
        self.main.settings.update_shc_method_button()
        return True

    def input_filewidgets(self) -> dict:
        """入力欄（キーは morizon_data.INPUT_DEFS と同じ）"""
        return {
            "dem": self.main.elementsDemFileWidget,
            "npp": self.main.elementsNppFileWidget,
            "srad": self.main.elementsSradFileWidget,
            "vtex": self.main.elementsVtexFileWidget,
            "building": self.main.elementsBuildingFileWidget,
            "network": self.main.elementsNetworkFileWidget,
            "costcsv": self.main.elementsCostCsvFileWidget,
        }

    def load_elements_files_from_dir(self):
        """
        「フォルダ選択から開始する」：選んだフォルダを作業フォルダ（外部）にして、その場で使う。
        入力は DATA の中から探し、出力はそのフォルダの YOUSO/・ZONING/・AGGREGATE/ に書く（原版のキットと同じ使い方）。
        フォルダの中身は人がそろえたものなので、使うファイルの一覧を確かめてから始める
        """
        # 初期状態から始める操作なので、最初にレイヤーを初期化する（途中で取りやめたら元に戻す）
        detached = self.main.detach_layers_to_initialize()
        if detached is None:
            return
        selected_dir = QFileDialog.getExistingDirectory(self.main, "フォルダを選択")
        if not selected_dir:
            self.main.restore_initialized_layers(detached)
            return
        # DATA だけが選ばれた場合、出力（YOUSO/ 等）はその隣（DATA の親）に作る
        workspace_root, data_dir = morizon_data.resolve_workspace(selected_dir)
        if workspace_root is None:
            QMessageBox.information(
                self.main, "フォルダ選択",
                "入力データが見つかりませんでした。\n"
                "DATA フォルダ（DEM・SiteIndex などを含むフォルダ）か、それを含むフォルダを選んでください。"
            )
            self.main.restore_initialized_layers(detached)
            return
        if morizon_data.has_outputs(workspace_root):
            # 出力がある＝途中まで進んだフォルダなので、保存ファイルの読み込み（フォルダ）と同じく読み込んで反映する
            QMessageBox.information(
                self.main, "フォルダ選択から開始する",
                f"既存データがあります。読み込んで反映されます。\n{os.path.basename(workspace_root)}"
            )
            self.main.discard_initialized_layers(detached)
            self.main.archive.load_workspace_data(workspace_root)
            return
        found = morizon_data.find_inputs(data_dir)

        lines = []
        for key, defn in morizon_data.INPUT_DEFS.items():
            candidates = found[key]
            if not candidates:
                lines.append(f"・{defn['DISPLAY_NAME']}：（なし）")
            elif len(candidates) == 1:
                lines.append(f"・{defn['DISPLAY_NAME']}：{os.path.basename(candidates[0])}")
            else:
                lines.append(
                    f"・{defn['DISPLAY_NAME']}：{os.path.basename(candidates[0])}"
                    f"（候補{len(candidates)}件のうち名前順で最初）"
                )
        warning = "" if found["dem"] else "\n\nDEMがありません。要素計算にはDEMが必要です。"
        answer = QMessageBox.question(
            self.main, "フォルダ選択から開始する",
            f"次のフォルダを作業フォルダ（外部）にします。\n{os.path.basename(workspace_root)}\n\n"
            "使う入力：\n" + "\n".join(lines) + warning
            + "\n\n出力は、このフォルダの YOUSO・ZONING・AGGREGATE に書き込みます。\n開始しますか？",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if answer != QMessageBox.StandardButton.Ok:
            self.main.restore_initialized_layers(detached)
            return
        self.main.discard_initialized_layers(detached)
        self.main.use_external_workspace(workspace_root)
        self.main.set_inputs_from_data_dir(data_dir)

    # ── 「…」で選んだファイルを作業フォルダへ取り込む ─────────────────────────
    # 個別に選んだファイルは、今の作業フォルダの DATA/<種類>/ にコピーして使う（1種類1ファイル。前のファイルは置き換える）。
    # プロジェクト内の作業フォルダは取り込みでそろうので確認しない。外部のフォルダの中を置き換えるときだけ確認する

    @staticmethod
    def _files_of_type(directory, ext):
        """作業フォルダの種類フォルダにある、その種類のファイル（付随ファイルを含む。Excel 等ほかの形式は含めない）"""
        if not os.path.isdir(directory):
            return []
        allowed = COMPANION_EXTENSIONS.get(ext.lower(), {"." + ext.lower()})
        return [
            os.path.join(directory, name) for name in sorted(os.listdir(directory))
            if os.path.isfile(os.path.join(directory, name))
            and (os.path.splitext(name)[1].lower() in allowed or name.lower().endswith(".aux.xml"))
        ]

    def _confirm_replace_external(self, target_dir, old_files) -> bool:
        external = utils.get_external_workspace()
        if not external or not old_files:
            return True
        names = "\n".join("・" + os.path.basename(path) for path in old_files)
        answer = QMessageBox.question(
            self.main, "外部のフォルダのファイルを置き換えます",
            f"作業フォルダ（外部 {os.path.basename(external)}）の {os.path.relpath(target_dir, external)} にある"
            f"次のファイルを削除し、選んだファイルに置き換えます。\n\n{names}\n\nよろしいですか？",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Ok

    def _set_input_path(self, key, path):
        with self.main.suppress_input_import():
            self.input_filewidgets()[key].setFilePath(path)
        self._input_paths[key] = path

    def _on_input_file_changed(self, key):
        path = self.input_filewidgets()[key].filePath()
        previous = self._input_paths.get(key, "")
        if self.main.is_input_import_suppressed() or not path or not os.path.isfile(path) \
                or not self.main.has_workspace():
            # プラグインが設定したとき・空欄・入力途中・作業フォルダが無い（未保存のプロジェクト）ときはそのまま
            self._input_paths[key] = path
            return
        input_def = morizon_data.INPUT_DEFS[key]
        target_dir = utils.get_workspace_dir(DIR_DATA, *input_def["PATH"])
        if utils.is_under_dir(os.path.abspath(path), target_dir):
            self._input_paths[key] = path
            return

        old_files = self._files_of_type(target_dir, input_def["EXT"])
        if not self._confirm_replace_external(target_dir, old_files):
            self._set_input_path(key, previous)
            return
        sources = ForestZoningMainDialogArchive.get_companion_files(path, input_def["EXT"]) or [path]
        os.makedirs(target_dir, exist_ok=True)
        # いったん .part の名前でコピーし、そろってから前のファイルと入れ替える（中断・失敗しても前のファイルは残る）
        tasks = [(src, os.path.join(target_dir, os.path.basename(src) + ".part")) for src in sources]
        outcome = self.main.archive._run_thread(processes.data_import.FileCopyThread(tasks))
        if outcome.get("status") != "done":
            for _, part in tasks:
                if os.path.exists(part):
                    os.remove(part)
            if outcome.get("status") == "failed":
                QMessageBox.warning(self.main, "エラー", f"ファイルを取り込めませんでした。\n\n{outcome.get('message', '')}")
            self._set_input_path(key, previous)
            return
        for old in old_files:
            try:
                os.remove(old)
            except OSError:
                pass
        for _, part in tasks:
            os.replace(part, part[:-len(".part")])
        self._set_input_path(key, os.path.join(target_dir, os.path.basename(path)))

    def start_from_dem_browser(self):
        """
        「DEMブラウザから開始する」ボタンの処理
        現在のQGISキャンバス範囲でDEMを取得し、プラグイン管理フォルダに保存してDEMウィジェットに反映する
        """
        project_home = QgsProject.instance().homePath()
        if project_home == "":
            QMessageBox.information(
                self.main, "エラー",
                "先にQGISプロジェクトを保存してください（保存先フォルダにDEM等を格納します）。"
            )
            return
        # 初期状態から始める操作なので、最初にレイヤーを初期化する（実行の前に取りやめたら元に戻す）
        detached = self.main.detach_layers_to_initialize()
        if detached is None:
            return
        # 取得したデータはプロジェクト内（morizon_next）に置くので、作業フォルダをプロジェクト内にする。
        # 作業フォルダの切り替えと前の解析の破棄（データの判断）は、押してすぐ1回にまとめて確かめ、
        # 実行はDEMブラウザで範囲を決めてから行う（ブラウザで取りやめたときはデータを変えない）
        external = utils.get_external_workspace()
        switch_to_internal = not utils.is_internal_workspace()
        discard = self.main.archive.has_previous_analysis()
        notes = []
        if external:
            notes.append(f"作業フォルダを外部（{os.path.basename(external) or external}）からプロジェクト内に切り替えます。")
        if discard:
            notes.append(self.main.archive.DISCARD_MESSAGE)
        if notes:
            answer = QMessageBox.question(
                self.main, "DEMブラウザから開始する",
                "\n\n".join(notes) + "\n\n実行は、DEMブラウザで範囲を決めてから行います。",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                self.main.restore_initialized_layers(detached)
                return

        dlg = DemBrowserDialog(self.main)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.main.restore_initialized_layers(detached)
            return
        extent_wgs84 = dlg.get_extent_wgs84()
        source = dlg.get_selected_source()
        if source.key == "gsi":
            # 国土地理院は解像度の混在を避けるため、範囲全体をカバーできる解像度に先に絞る
            tile_sources = self._resolve_gsi_tile_sources(extent_wgs84)
            if tile_sources is None:
                self.main.restore_initialized_layers(detached)
                return
            source.tile_sources = tile_sources

        # ここから実行（確かめた内容）
        self.main.discard_initialized_layers(detached)
        if switch_to_internal:
            self.main.use_project_workspace(refill_inputs=True)
        if discard:
            self.main.archive.discard_previous_analysis()
        # DEMブラウザで指定した可視範囲は、最終成果物の表示・解析範囲として保持する。
        # DEMや道路などの途中データは周辺情報を失わないよう、ここでは切り捨てない。
        self._pending_extent_wgs84 = extent_wgs84
        self._pending_final_extent_wgs84 = extent_wgs84

        # データの実体は必ずプロジェクトフォルダ配下に保存する（ユーザーの保存場所を変えない）。
        # 全角パスでも構わない。ASCII安全な別名への変換は、実際にSAGA/GRASS等へ渡す直前
        # （processes/elements.pyのProcessingThread.run）でだけ行う
        output_dir = utils.get_morizon_managed_dir(DIR_DATA, *INPUT_DEM["PATH"])
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "dem_fetched.tif")

        thread = processes.dem_fetch.DemFetchThread(
            source,
            (extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
             extent_wgs84.xMaximum(), extent_wgs84.yMaximum()),
            output_path,
        )
        result = self._run_fetch(thread, "DEMの取得に失敗しました。", show_set_progress=False)
        if result is not None:
            self.set_dem_filepath(result)

    def _run_fetch(self, thread, failure_message, show_set_progress=False):
        """取得の処理スレッドを進捗ダイアログ付きで実行し、成功なら結果の辞書、失敗なら None を返す（失敗は知らせる）。
        結果の反映や次の知らせは、進捗の窓を消して下の画面を描き直してから行う（run_with_progress）"""
        outcome = run_with_progress(thread, show_detail=True, show_set_progress=show_set_progress)
        if "error" in outcome:
            QMessageBox.information(self.main, "エラー", f"{failure_message}\n\n{outcome['error']}")
            return None
        return outcome.get("result")

    def _resolve_gsi_tile_sources(self, extent_wgs84):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            coverage_results = GSITileDEMLoader.check_sources_coverage(
                extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
                extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
                GSITileDEMLoader.TILE_SOURCES,
            )
        finally:
            QApplication.restoreOverrideCursor()

        available = [
            result for result in coverage_results
            if not result["cancelled"] and result["missing"] == 0
        ]
        if not available:
            details = "\n".join(
                f"{result['label']}: {result['missing']}/{result['total']}枚が取得不可"
                for result in coverage_results
            )
            QMessageBox.information(
                self.main, "DEMタイルなし",
                "取得範囲全体を同一解像度でカバーできる国土地理院DEMタイルがありません。\n"
                f"{details}"
            )
            return None

        if len(available) == 1:
            selected = available[0]
            skipped = [
                f"{result['label']}（{result['missing']}/{result['total']}枚不足）"
                for result in coverage_results
                if result["missing"] > 0
            ]
            if skipped:
                message = (
                    "取得範囲内で利用可能な解像度が混在しています。\n"
                    + "\n".join(skipped)
                    + f"\n\n{selected['label']}で統一します。"
                )
            else:
                message = (
                    "取得範囲全体をカバーできる国土地理院DEMタイルは"
                    f"{selected['label']}のみです。\n\n"
                    f"{selected['label']}で統一します。"
                )
            QMessageBox.information(self.main, "DEM解像度の統一", message)
            return [selected["source"]]

        labels = [result["label"] for result in available]
        dialog = DemResolutionDialog(labels, self.main)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected_label = dialog.selected_label()
        for result in available:
            if result["label"] == selected_label:
                return [result["source"]]
        return None

    def set_dem_filepath(self, result: dict):
        # ウィジェットには常に実パス（プロジェクトフォルダ内）を表示する。
        # ASCII安全な別名への変換は、実際にSAGA/GRASS等へ渡す直前
        # （processes/elements.pyのProcessingThread.run）でだけ行う
        dem_path = result["path"]
        self.main.elementsDemFileWidget.setFilePath(dem_path)
        self._dem_browser_dem_path = dem_path
        QMessageBox.information(
            self.main, "完了",
            f"DEMを取得しプロジェクトフォルダに保存しました。\n{result['info']}\n"
            f"保存先：{dem_path}"
        )

        # 続けて可視範囲で地位指数データ(NPP/SRAD/VTEX)を取得する。
        # 地位指数は距離計算のような範囲外依存を持たないため、最終範囲で十分。
        extent = getattr(self, "_pending_extent_wgs84", None)
        if extent is None:
            return
        self.start_siteindex_fetch(extent)

    def start_siteindex_fetch(self, extent_wgs84):
        center_lon = (extent_wgs84.xMinimum() + extent_wgs84.xMaximum()) / 2
        center_lat = (extent_wgs84.yMinimum() + extent_wgs84.yMaximum()) / 2
        zone = zoningkit_fetcher.find_zone_for_point(center_lon, center_lat)
        if zone is None:
            QMessageBox.information(
                self.main, "地位指数データなし",
                "この範囲は座標系1〜13系のいずれにも該当しないため、"
                "地位指数データ(NPP/SRAD/VTEX)の自動取得には対応していません。\n"
                "手動でファイルを指定してください。"
            )
            return

        output_dir = utils.get_morizon_managed_dir(DIR_DATA)
        # ゾーン全体データはプロジェクト内蔵の共有領域に置く（ドライブ直下キャッシュは廃止）。
        # プロジェクトを他PCへ移動しても一緒に運ばれ、同一プロジェクト内での再取得を避けられる。
        # 「フォルダ一式」（DATA/等）には含めない、あくまで内部支援用
        cache_base_dir = utils.get_morizon_shared_dir()

        thread = processes.siteindex_fetch.SiteIndexFetchThread(
            zone, cache_base_dir, output_dir,
            extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
            extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
        )
        result = self._run_fetch(thread, "地位指数データの取得に失敗しました。", show_set_progress=True)
        if result is not None:
            self.set_siteindex_filepaths(result)

    def set_siteindex_filepaths(self, result: dict):
        if "NPP" in result:
            self.main.elementsNppFileWidget.setFilePath(result["NPP"])
        if "SRAD" in result:
            self.main.elementsSradFileWidget.setFilePath(result["SRAD"])
        if "VTEX" in result:
            self.main.elementsVtexFileWidget.setFilePath(result["VTEX"])
        QMessageBox.information(
            self.main, "完了", "地位指数データ(NPP/SRAD/VTEX)を取得しました。"
        )

        extent = getattr(self, "_pending_extent_wgs84", None)
        if extent is None:
            return
        answer = QMessageBox.question(
            self.main, "確認",
            "続けて建物ポリゴン・道路縁データも自動取得しますか？\n"
            "（基盤地図情報ダウンロードサービスへのログインが必要です）",
            QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_building_road_fetch(extent)

    def start_building_road_fetch(self, extent_wgs84):
        session = getattr(self, "_fgd_session", None)
        if session is None:
            login_dlg = FgdLoginDialog(self.main)
            if login_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            session = login_dlg.get_session()
            self._fgd_session = session

        fetch_extent_wgs84 = self._expand_extent_for_fgd_fetch(extent_wgs84)
        mesh_codes = fgd_fetcher.mesh_codes_for_extent(
            fetch_extent_wgs84.xMinimum(), fetch_extent_wgs84.yMinimum(),
            fetch_extent_wgs84.xMaximum(), fetch_extent_wgs84.yMaximum(),
        )

        output_dir = utils.get_morizon_managed_dir(DIR_DATA)
        # 基盤地図情報のZIP自体は地位指数と同様、プロジェクト内蔵の共有領域にキャッシュする
        cache_dir = utils.get_morizon_shared_dir("fgd")

        thread = processes.building_road_fetch.BuildingRoadFetchThread(
            session, mesh_codes, cache_dir, output_dir,
            fetch_extent_wgs84.xMinimum(), fetch_extent_wgs84.yMinimum(),
            fetch_extent_wgs84.xMaximum(), fetch_extent_wgs84.yMaximum(),
            dem_filepath=self.main.elementsDemFileWidget.filePath(),
            clip_to_extent=True,
            db_dir=utils.get_morizon_layer_db_dir(),
        )
        result = self._run_fetch(thread, "建物・道路データの取得に失敗しました。")
        if result is not None:
            self.set_building_road_filepaths(result)

    @staticmethod
    def _expand_extent_for_fgd_fetch(extent_wgs84):
        # 基盤地図情報は2次メッシュ単位。可視範囲外すぐの道路・建物が距離/流域に効くため、
        # 1メッシュ分だけ周辺も取得し、切り捨ては最終ラスター側で行う。
        lon_margin = 0.125
        lat_margin = 5 / 60
        return QgsRectangle(
            extent_wgs84.xMinimum() - lon_margin,
            extent_wgs84.yMinimum() - lat_margin,
            extent_wgs84.xMaximum() + lon_margin,
            extent_wgs84.yMaximum() + lat_margin,
        )

    def set_building_road_filepaths(self, result: dict):
        missing = []
        if result.get("building"):
            self.main.elementsBuildingFileWidget.setFilePath(result["building"])
        else:
            missing.append("建物ポリゴン")
        if result.get("road"):
            self.main.elementsNetworkFileWidget.setFilePath(result["road"])
            if result.get("road_empty"):
                self._show_no_road_warning(
                    "道路地物がありません",
                    "基盤地図情報から取得した道路データに、地利計算に使える有効な道路地物がありませんでした。\n"
                    "要素計算の地利は、DEM全域を道路から遠い条件（1点相当）として作成します。\n\n"
                    "実際の林道・作業道を反映する場合は、独自の道路データを作成して読み込んでください。",
                )
        else:
            missing.append("道路縁")
        self.refresh_elements_ui()
        if missing:
            QMessageBox.warning(
                self.main,
                "一部取得できませんでした",
                "次のデータを取得・反映できませんでした。\n"
                + "\n".join(missing)
                + "\n\n対象範囲に有効なジオメトリが無いか、基盤地図情報の変換に失敗しています。",
            )
            return
        if result.get("road_empty"):
            QMessageBox.information(
                self.main, "完了", "建物ポリゴンを取得し、道路縁は空データとして設定しました。"
            )
        else:
            QMessageBox.information(
                self.main, "完了", "建物ポリゴン・道路縁データを取得しました。"
            )

    def _show_no_road_warning(self, title: str, message: str):
        box = QMessageBox(self.main)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        text = f"{message}\n\n- 公式の手引 -\n{MORIZON_GUIDE_TITLE}\n参照ページ：{MORIZON_GUIDE_ROAD_PAGES}"
        box.setText(text)
        # どの行も途中で折り返されない幅にする。フォントや大きさは OS・環境で違うため、
        # 実際に表示するフォントで各行の幅を測り、一番長い行に合わせる
        label = box.findChild(QLabel, "qt_msgbox_label")
        if label is not None:
            metrics = label.fontMetrics()
            widest = max(metrics.horizontalAdvance(line) for line in text.split("\n"))
            label.setMinimumWidth(widest + metrics.averageCharWidth())
        guide_button = box.addButton("公式の手引を開く", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() == guide_button:
            QDesktopServices.openUrl(QUrl(MORIZON_GUIDE_URL))

    def show_road_building_layers(self):
        """描画用の DB（layer_db.py）の道路縁・建築物を、「Morizon Next」グループの「災害リスク」の下の
        「道路・建物」グループに表示する。前に出したものは外してから出し直す。
        表示の前に、要素計算タブに設定されている道路・建物の shp と DB を照らし合わせ、shp が編集されていれば
        （計画路網を描き足したなど）DB に取り込んでから表示し、要素計算をもう一度実行するよう案内する"""
        db_dir = utils.get_morizon_layer_db_dir()
        targets = [
            (layer_db.KIND_ROAD, "道路縁", "地利", self.main.elementsNetworkFileWidget.filePath(),
             lambda: QgsLineSymbol.createSimple({
                 "line_color": ROAD_LINE_COLOR, "line_width": "0.1", "line_width_unit": "MM",
                 "capstyle": "square", "joinstyle": "bevel",
             })),
            (layer_db.KIND_BUILDING, "建築物", "保全対象を含む流域", self.main.elementsBuildingFileWidget.filePath(),
             lambda: QgsFillSymbol.createSimple({
                 "color": BUILDING_FILL_COLOR, "outline_style": "no", "style": "solid",
             })),
        ]
        # 照らし合わせで DB を書き換えることがあるので、前に出したレイヤーは先に外す
        utils.remove_output_layers(utils.STAGE_ROAD_BUILDING)
        edited = []
        for kind, name, element, shp_path, _ in targets:
            if not shp_path or not os.path.isfile(shp_path):
                continue  # 入力が設定されていなければ、DB はそのまま
            try:
                if layer_db.import_saved(layer_db.db_file(db_dir, kind), kind, shp_path) == "shp":
                    edited.append((name, element))
            except Exception as e:
                QMessageBox.warning(self.main, "道路と建物を出力", f"{name}のデータを取り込めませんでした。\n\n{e}")
        available = [target for target in targets
                     if layer_db.has_layer(layer_db.db_file(db_dir, target[0]), target[0])]
        if not available:
            QMessageBox.information(
                self.main, "道路と建物を出力",
                "表示できる道路縁・建築物のデータがありません。\n"
                "DEMブラウザからの取得、または保存ファイルの読み込みで作成されます。",
            )
            return

        root = utils.get_morizon_output_group()
        children = root.children()
        risk_index = next((i for i, child in enumerate(children)
                           if isinstance(child, QgsLayerTreeGroup) and child.name() == "災害リスク"), None)
        group = root.insertGroup(len(children) if risk_index is None else risk_index + 1, ROAD_BUILDING_GROUP_NAME)
        group.setExpanded(True)
        for kind, name, _, _, make_symbol in available:
            layer = QgsVectorLayer(layer_db.layer_uri(layer_db.db_file(db_dir, kind), kind), name, "ogr")
            if not layer.isValid():
                continue
            layer.setRenderer(QgsSingleSymbolRenderer(make_symbol()))
            utils.tag_output_layer(layer, utils.STAGE_ROAD_BUILDING, kind)
            QgsProject.instance().addMapLayer(layer, False)
            group.addLayer(layer).setExpanded(False)
        if edited:
            QMessageBox.information(
                self.main, "道路と建物を出力",
                "次のデータが編集されていたため、表示を新しい内容に更新しました。\n"
                + "\n".join(f"・{name}" for name, _ in edited)
                + "\n\n計算結果に反映するには、要素計算をもう一度実行してください（"
                + "、".join(f"{name}は「{element}」" for name, element in edited)
                + "に使われます）。",
            )

    def clear_elements_settings(self):
        """
        「設定をクリアする」ボタンの処理：初期状態（作業フォルダ：なし）に戻す。
        レイヤーを初期化し、選べばプロジェクト内（morizon_next）の個別データも削除する。プロジェクトの作業フォルダの記録も消す
        """
        layers = self.main.layers_to_initialize()
        box = QMessageBox(self.main)
        box.setWindowTitle("設定をクリアする")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            (self.main.initialize_message(layers) + "\n") if layers else "レイヤーを初期化します。\n"
        )
        box.setInformativeText("要素計算タブの入力欄を空にし、初期状態（作業フォルダ：なし）に戻します。")
        delete_checkbox = QCheckBox(
            f"プラグインフォルダ内の個別データを削除（[Project Folder]/morizon_next/{os.path.basename(utils.get_morizon_managed_dir())}）"
        )
        delete_checkbox.setEnabled(QgsProject.instance().homePath() != "")
        box.setCheckBox(delete_checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        utils.remove_project_layers(layers)
        if delete_checkbox.isChecked():
            failed = self.main.archive.clear_managed_data()
            if failed:
                QMessageBox.warning(
                    self.main, "設定をクリアする",
                    "次のものは削除できませんでした（使用中の可能性があります）。\n" + "\n".join(failed),
                )
        self.reset_elements_inputs()
        self.main.use_no_workspace(persist=True)

    def reset_elements_inputs(self):
        """要素計算タブの入力ファイル・チェックボックスを初期状態に戻す（確認なし）"""
        for filewidget in (
            self.main.elementsDemFileWidget,
            self.main.elementsNppFileWidget,
            self.main.elementsSradFileWidget,
            self.main.elementsVtexFileWidget,
            self.main.elementsBuildingFileWidget,
            self.main.elementsNetworkFileWidget,
            self.main.elementsCostCsvFileWidget,
        ):
            filewidget.setFilePath("")

        for checkbox in (
            self.main.elementsSiteIdxCheckbox,
            self.main.elementsCostCheckbox,
            self.main.elementsDistanceCheckbox,
            self.main.elementsShcCheckbox,
            self.main.elementsSlopeCheckbox,
            self.main.elementsSaveareaCheckbox,
        ):
            checkbox.setChecked(False)
        self.forget_dem_browser_state()

    def forget_dem_browser_state(self):
        """DEMブラウザで取得したときの範囲などの記憶を消す（入力を消したとき・プロジェクトを切り替えたとき）"""
        self._dem_browser_dem_path = None
        self._pending_extent_wgs84 = None
        self._pending_final_extent_wgs84 = None

    def elements_get_existing_filenames(self) -> list:
        """
        「要素計算」で、出力先フォルダに同名ファイルが存在するかチェック
        存在する場合そのすべてのファイル名の配列を返す

        Returns:
            list
        """
        output_dir = self.main.elementsOutputDirFileWidget.filePath()
        existing_filenames = []

        def append_filename_if_exist(filename: str):
            if os.path.exists(os.path.join(output_dir, filename + ".tif")):
                existing_filenames.append(filename + ".tif")

        if self.main.elementsSiteIdxCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_SITEIDX_SUGI["FILE_NAME"])
            append_filename_if_exist(OUTPUT_SITEIDX_HINOKI["FILE_NAME"])
            append_filename_if_exist(OUTPUT_SITEIDX_KARAMATSU["FILE_NAME"])
        if self.main.elementsCostCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_COST["FILE_NAME"])
        if self.main.elementsDistanceCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_DISTANCE["FILE_NAME"])
        if self.main.elementsShcCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_SHC["FILE_NAME"])
        if self.main.elementsSlopeCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_SLOPE["FILE_NAME"])
        if self.main.elementsSaveareaCheckbox.isChecked():
            append_filename_if_exist(OUTPUT_SAVEAREA["FILE_NAME"])

        return existing_filenames

    def selected_element_names(self) -> set:
        """計算する要素（チェックの入った要素）の出力レイヤーの区別（表示名）"""
        names = set()
        if self.main.elementsSiteIdxCheckbox.isChecked():
            names |= {OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"], OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"],
                      OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"]}
        for checkbox, output_def in (
            (self.main.elementsCostCheckbox, OUTPUT_COST),
            (self.main.elementsDistanceCheckbox, OUTPUT_DISTANCE),
            (self.main.elementsShcCheckbox, OUTPUT_SHC),
            (self.main.elementsSlopeCheckbox, OUTPUT_SLOPE),
            (self.main.elementsSaveareaCheckbox, OUTPUT_SAVEAREA),
        ):
            if checkbox.isChecked():
                names.add(output_def["DISPLAY_NAME"])
        return names

    def run_elements(self):
        if not self._confirm_shc_method():
            return
        self.main.hide()

        # 計算する要素のレイヤーを片付ける（指している場所に関係なく。ファイルを上書きするかどうかとは別の話）
        # 片付けたレイヤーは削除せずに取り外して持っておき、上書きをキャンセルしたら元の位置に戻す。
        # 実行するなら、ファイルの掴みを解放するため、ここで削除する
        selections = self.main.snapshot_selections()
        detached = utils.detach_output_layers(utils.STAGE_ELEMENTS, self.selected_element_names())

        existing_filenames = self.elements_get_existing_filenames()
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
                self.main.show()
                return
        utils.discard_detached_layers(detached)

        input_files_dict = {
            "dem": self.main.elementsDemFileWidget.filePath(),
            "npp": self.main.elementsNppFileWidget.filePath(),
            "srad": self.main.elementsSradFileWidget.filePath(),
            "vtex": self.main.elementsVtexFileWidget.filePath(),
            "building": self.main.elementsBuildingFileWidget.filePath(),
            "network": self.main.elementsNetworkFileWidget.filePath(),
            "costcsv": self.main.elementsCostCsvFileWidget.filePath(),
        }

        target_elements_dict = {
            "siteidx": self.main.elementsSiteIdxCheckbox.isChecked(),
            "cost": self.main.elementsCostCheckbox.isChecked(),
            "distance": self.main.elementsDistanceCheckbox.isChecked(),
            "shc": self.main.elementsShcCheckbox.isChecked(),
            "slope": self.main.elementsSlopeCheckbox.isChecked(),
            "savearea": self.main.elementsSaveareaCheckbox.isChecked(),
        }

        # UIをブロックしないように別スレッドで処理を動かす
        thread = processes.elements.ProcessingThread(
            input_files_dict,
            target_elements_dict,
            self.main.elementsOutputDirFileWidget.filePath(),
            final_extent_wgs84=self._get_final_extent_for_current_dem(),
        )
        # 結果のレイヤー追加と知らせは、進捗の窓を消してから行う（run_with_progress）
        # 中断したときも、それまでに作れたレイヤーは追加する（前のレイヤーは実行前に外してあるため）
        outcome = run_with_progress(thread)
        if "result" in outcome:
            self.add_elements_layer_to_project(outcome["result"])
        if "error" in outcome:
            QMessageBox.information(self.main, "エラー", f"エラーが発生しました。\n\n{outcome['error']}")
        elif thread.abort_flag:
            QMessageBox.information(self.main, "中断", "処理を中断しました。")
        else:
            if getattr(thread, "no_road_distance_created", False):
                self._show_no_road_warning(
                    "地利を全域1点相当で作成しました",
                    "設定された道路データには、地利計算に使える有効な道路地物がありませんでした。\n"
                    "地利はDEM全域を道路から遠い条件（1点相当）として作成しました。\n\n"
                    "実際の林道・作業道を反映する場合は、独自の道路データの作成をおすすめします。",
                )
            QMessageBox.information(self.main, "終了", "処理が終了しました。")

        self.main.show()

    def _get_final_extent_for_current_dem(self):
        dem_path = self.main.elementsDemFileWidget.filePath()
        if dem_path != getattr(self, "_dem_browser_dem_path", None):
            return None
        extent = getattr(self, "_pending_final_extent_wgs84", None)
        if extent is None:
            return None
        return (
            extent.xMinimum(), extent.yMinimum(),
            extent.xMaximum(), extent.yMaximum(),
        )

    # グループをONにしたとき最初にスコアリング表示にする要素（地利のみ）。
    # それ以外の要素（災害リスクの3要素を含む）は生データ表示にする
    SCORING_CHECKED_ELEMENTS = {
        OUTPUT_DISTANCE["DISPLAY_NAME"],
    }

    @staticmethod
    def add_elements_layer_to_project(output_rlayers_dict):
        """
        要素計算の処理結果を受け取って各要素ごとの2レイヤーを1つのグループとしてプロジェクトに追加。

        DISPLAY_NAMEはスラッシュ区切りでグループにネストできる。
        2階層（例：「収益性/地位（スギ）」「収益性/地利」）の場合は、
        最後のセグメント専用の葉グループを作り、その中に2レイヤーを格納する。
        3階層以上の場合は最後のセグメントは葉グループを作らず、
        1つ手前のグループへレイヤーをまとめて直接追加する。
        """
        # 出力レイヤーは「Morizon Next」グループの中にまとめる
        root = utils.get_morizon_output_group()
        group_cache = {}
        layer_items = list(reversed(list(output_rlayers_dict.items())))
        if not layer_items:
            return

        progress_dialog = QProgressDialog("出力レイヤーを追加します...", None, 0, len(layer_items))
        progress_dialog.setWindowTitle("レイヤー追加中...")
        progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress_dialog.setCancelButton(None)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setValue(0)
        progress_dialog.show()

        for step, (display_name, rlayers) in enumerate(layer_items, start=1):
            progress_dialog.setLabelText(f"{display_name}を追加します")
            progress_dialog.setValue(step - 1)
            QCoreApplication.processEvents()

            parts = display_name.split("/")
            *category_parts, short_name = parts

            parent_group = root
            path_key = ()
            for part in category_parts:
                path_key += (part,)
                if path_key not in group_cache:
                    existing = parent_group.findGroup(part)
                    group_cache[path_key] = (
                        existing if existing is not None else parent_group.insertGroup(0, part)
                    )
                parent_group = group_cache[path_key]
                parent_group.setExpanded(True)

            if len(parts) <= 2:
                # 2階層まで: short_name専用の葉グループを作りその中にレイヤーを格納する
                target_group = parent_group.insertGroup(0, short_name)
                group_cache[path_key + (short_name,)] = target_group
                # 要素ごとの子グループ（生データ／スコアリング）は閉じておく
                target_group.setExpanded(False)
                target_group.setItemVisibilityChecked(False)
            else:
                # 3階層以上: 葉グループを作らず、手前のグループへ直接レイヤーをまとめる
                target_group = parent_group
                target_group.setItemVisibilityChecked(False)

            # 要素ごとに最初に見せたい方（生データ／スコアリング）にチェックを入れておく。
            # グループはOFFのままなので、グループをONにしたときにその表示になる
            checked_index = (
                1 if display_name in ForestZoningMainDialogElements.SCORING_CHECKED_ELEMENTS else 0
            ) if len(parts) <= 2 else None
            for index, rlayer in enumerate(rlayers):
                # QML側の指定が環境によって反映されない場合があるため、追加時にも明示する
                apply_output_blend_mode(rlayer)
                utils.tag_output_layer(rlayer, utils.STAGE_ELEMENTS, display_name)

                QgsProject.instance().addMapLayer(rlayer, False)
                layer_node = target_group.addLayer(rlayer)
                layer_node.setItemVisibilityChecked(index == checked_index)
                # シンボロジ（凡例の展開）は閉じておく
                layer_node.setExpanded(False)

            progress_dialog.setValue(step)
            QCoreApplication.processEvents()
            ForestZoningMainDialogElements._wait_for_layer_add_step()

        ForestZoningMainDialogElements.setup_output_layer_tree_visibility(root)
        progress_dialog.setValue(len(layer_items))
        progress_dialog.close()
        output_rlayers_dict.clear()
        gc.collect()

    @staticmethod
    def _wait_for_layer_add_step(milliseconds=250):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    @staticmethod
    def setup_output_layer_tree_visibility(root=None):
        """
        要素計算の出力グループにQGIS標準の排他的表示（Mutually Exclusive Group）を設定する。
        設定はプロジェクトに保存されるため、プラグインの起動有無に関係なく有効。
        """
        root = root or utils.get_morizon_output_group()
        # 災害リスクは傾斜をベースに地形の複雑さ・保全対象を含む流域を重ねて見るため、
        # 要素どうしは排他にしない（各要素内の生データ／スコアリングだけ排他）。収益性は要素どうしも排他
        for axis_name, exclusive_between_elements in (("収益性", True), ("災害リスク", False)):
            axis_group = ForestZoningMainDialogElements._find_direct_group(root, axis_name)
            if axis_group is None:
                continue
            if exclusive_between_elements:
                ForestZoningMainDialogElements._set_mutually_exclusive_group(axis_group)
            else:
                axis_group.setIsMutuallyExclusive(False)
            for child in axis_group.children():
                if isinstance(child, QgsLayerTreeGroup):
                    ForestZoningMainDialogElements._set_mutually_exclusive_group(child)

    @staticmethod
    def _find_direct_group(parent, name):
        for child in parent.children():
            if isinstance(child, QgsLayerTreeGroup) and child.name() == name:
                return child
        return None

    @staticmethod
    def _set_mutually_exclusive_group(group):
        if not group.children():
            return
        group.setIsMutuallyExclusive(True)
