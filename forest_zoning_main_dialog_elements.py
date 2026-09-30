import os
import glob
import re

# QGIS-API
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *


from qgis.utils import iface

from .progress_dialog import ProgressDialog
from .forest_zoning_dem_browser_dialog import DemBrowserDialog
from .dem_loader import GSITileDEMLoader
from . import processes
from .constants import (
    INPUT_DEM,
    INPUT_NPP,
    INPUT_SRAD,
    INPUT_VTEX,
    INPUT_BUILDING,
    INPUT_NETWORK,
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
from .utils import get_tiff_info


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
                default_output_dir = os.path.join(project_home, "morizon_next", "output")
                os.makedirs(default_output_dir, exist_ok=True)
                self.main.elementsOutputDirFileWidget.setFilePath(default_output_dir)

        self.costcsv_editor = CostCsvEditorWidget(
            self.main, on_export=self.handle_costcsv_export
        )
        self.main.costCsvGroupBoxLayout.addWidget(self.costcsv_editor)

        self.refresh_elements_ui()

    def handle_costcsv_export(self, editor_widget):
        project_home = QgsProject.instance().homePath()
        if project_home == "":
            QMessageBox.information(
                self.main, "エラー", "先にQGISプロジェクトを保存してください。"
            )
            return
        output_path = os.path.join(
            project_home, "morizon_next", "SAGYO-SYSTEM_CSV", "costcsv.csv"
        )
        try:
            editor_widget.export_to_csv(output_path)
        except ValueError as e:
            QMessageBox.warning(self.main, "入力エラー", str(e))
            return
        self.main.elementsCostCsvFileWidget.setFilePath(output_path)
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

        def validate_output(output_name: str):
            """
            出力データのバリデーション
            """
            groups = QgsProject().instance().layerTreeRoot().findGroups()
            group_names = list(map(lambda g: g.name(), groups))
            if output_name in group_names:
                error_texts.append(f"プロジェクトにすでに「{output_name}」が存在します")

        if self.main.elementsSiteIdxCheckbox.isChecked():
            validate_output(OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"])
            validate_output(OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"])
            validate_output(OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"])
        if self.main.elementsCostCheckbox.isChecked():
            validate_output(OUTPUT_COST["DISPLAY_NAME"])
        if self.main.elementsDistanceCheckbox.isChecked():
            validate_output(OUTPUT_DISTANCE["DISPLAY_NAME"])
        if self.main.elementsShcCheckbox.isChecked():
            validate_output(OUTPUT_SHC["DISPLAY_NAME"])
        if self.main.elementsSlopeCheckbox.isChecked():
            validate_output(OUTPUT_SLOPE["DISPLAY_NAME"])
        if self.main.elementsSaveareaCheckbox.isChecked():
            validate_output(OUTPUT_SAVEAREA["DISPLAY_NAME"])

        # その他
        if not mondatory_files_dict["dem"]:
            error_texts.append("計算する要素をひとつ以上選択してください")
        if self.main.elementsOutputDirFileWidget.filePath() == "":
            error_texts.append("出力先フォルダを指定してください")

        return error_texts

    def load_elements_files_from_dir(self):
        """
        指定されたフォルダから複数のファイルを探し見つけたらFileWidgetに反映する
        """
        selected_dir = QFileDialog.getExistingDirectory(self.main, "フォルダを選択")

        for INPUT_FILE, filewidget in (
            (INPUT_DEM, self.main.elementsDemFileWidget),
            (INPUT_NPP, self.main.elementsNppFileWidget),
            (INPUT_SRAD, self.main.elementsSradFileWidget),
            (INPUT_VTEX, self.main.elementsVtexFileWidget),
            (INPUT_BUILDING, self.main.elementsBuildingFileWidget),
            (INPUT_NETWORK, self.main.elementsNetworkFileWidget),
            (INPUT_COSTCSV, self.main.elementsCostCsvFileWidget),
        ):
            # 拡張子の大文字小文字を区別せず探す:tif->[tT][iI][fF]
            ext_case = "".join(
                list(
                    map(
                        lambda char: "[" + char.lower() + char.upper() + "]",
                        list(INPUT_FILE["EXT"]),
                    )
                )
            )
            files = glob.glob(
                os.path.join(selected_dir, *INPUT_FILE["PATH"], "*." + ext_case + "*")
            )

            # ファイルが見つかったならウィジェットに反映する
            if len(files) > 0:
                filewidget.setFilePath(files[0])

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

        dlg = DemBrowserDialog(self.main)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        extent_wgs84 = dlg.get_extent_wgs84()
        sources = self._resolve_dem_sources(dlg, extent_wgs84)
        if sources is None:
            return
        # DEM取得完了後、続けて同じ範囲で地位指数データも取得するため保持しておく
        self._pending_extent_wgs84 = extent_wgs84

        # データの実体は必ずプロジェクトフォルダ配下に保存する（ユーザーの保存場所を変えない）。
        # 全角パスでも構わない。ASCII安全な別名への変換は、実際にSAGA/GRASS等へ渡す直前
        # （processes/elements.pyのProcessingThread.run）でだけ行う
        output_dir = os.path.join(project_home, "morizon_next", "DEM")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "dem_fetched.tif")

        thread = processes.dem_fetch.DemFetchThread(
            extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
            extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
            output_path,
            sources=sources,
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.postDetail.connect(progress_dialog.set_detail)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFinished.connect(self.set_dem_filepath)
        thread.processFailed.connect(progress_dialog.close)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"DEMの取得に失敗しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec()

    def _resolve_dem_sources(self, dlg, extent_wgs84):
        selected_sources = dlg.get_selected_sources()
        if selected_sources is not GSITileDEMLoader.TILE_SOURCES:
            return selected_sources

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
        selected_label, ok = QInputDialog.getItem(
            self.main, "DEM解像度の選択",
            "複数のDEMタイル方式が取得範囲全体をカバーしています。\n"
            "解析範囲内で統一して使用する解像度を選択してください。",
            labels, 0, False,
        )
        if not ok:
            return None
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
        dem_extent = self._get_dem_extent_wgs84(dem_path)
        if dem_extent is not None:
            self._pending_extent_wgs84 = dem_extent
        QMessageBox.information(
            self.main, "完了",
            f"DEMを取得しプロジェクトフォルダに保存しました。\n{result['info']}\n"
            f"保存先：{dem_path}"
        )

        # 続けてDEM実範囲で地位指数データ(NPP/SRAD/VTEX)を取得する。
        # DEM実範囲を読めなかった場合だけ、取得要求時のキャンバス範囲へフォールバックする。
        extent = getattr(self, "_pending_extent_wgs84", None)
        if extent is None:
            return
        self.start_siteindex_fetch(extent)

    @staticmethod
    def _get_dem_extent_wgs84(dem_path):
        try:
            dem_info = get_tiff_info(dem_path)
            dem_extent = dem_info["extent"]
            dem_crs = dem_info["crs"]
            extent_rect = QgsRectangle(
                dem_extent[0], dem_extent[2],
                dem_extent[1], dem_extent[3],
            )
            wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(dem_crs, wgs84_crs, QgsProject.instance())
            return transform.transformBoundingBox(extent_rect)
        except Exception:
            return None

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

        project_home = QgsProject.instance().homePath()
        output_dir = os.path.join(project_home, "morizon_next")
        # ゾーン全体データはプロジェクト内蔵の共有領域に置く（ドライブ直下キャッシュは廃止）。
        # プロジェクトを他PCへ移動しても一緒に運ばれ、同一プロジェクト内での再取得を避けられる。
        # 「フォルダ一式」（DEM/SiteIndex等）には含めない、あくまで内部支援用
        cache_base_dir = os.path.join(project_home, "morizon_next", "shared")

        thread = processes.siteindex_fetch.SiteIndexFetchThread(
            zone, cache_base_dir, output_dir,
            extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
            extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.setProgress.connect(progress_dialog.set_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.postDetail.connect(progress_dialog.set_detail)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFinished.connect(self.set_siteindex_filepaths)
        thread.processFailed.connect(progress_dialog.close)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"地位指数データの取得に失敗しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec()

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

        mesh_codes = fgd_fetcher.mesh_codes_for_extent(
            extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
            extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
        )

        project_home = QgsProject.instance().homePath()
        output_dir = os.path.join(project_home, "morizon_next")
        # 基盤地図情報のZIP自体は地位指数と同様、プロジェクト内蔵の共有領域にキャッシュする
        cache_dir = os.path.join(project_home, "morizon_next", "shared", "fgd")

        thread = processes.building_road_fetch.BuildingRoadFetchThread(
            session, mesh_codes, cache_dir, output_dir,
            extent_wgs84.xMinimum(), extent_wgs84.yMinimum(),
            extent_wgs84.xMaximum(), extent_wgs84.yMaximum(),
            dem_filepath=self.main.elementsDemFileWidget.filePath(),
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.postDetail.connect(progress_dialog.set_detail)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFinished.connect(self.set_building_road_filepaths)
        thread.processFailed.connect(progress_dialog.close)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"建物・道路データの取得に失敗しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec()

    def set_building_road_filepaths(self, result: dict):
        if result.get("building"):
            self.main.elementsBuildingFileWidget.setFilePath(result["building"])
        if result.get("road"):
            self.main.elementsNetworkFileWidget.setFilePath(result["road"])
        QMessageBox.information(
            self.main, "完了", "建物ポリゴン・道路縁データを取得しました。"
        )

    def clear_elements_settings(self):
        """
        「設定をクリアする」ボタンの処理
        要素計算タブの入力ファイル・チェックボックスを初期状態に戻す
        """
        answer = QMessageBox.question(
            self.main, "確認", "要素計算タブの入力設定をクリアしてよろしいですか？",
            QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.No:
            return

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

    def run_elements(self):
        self.main.hide()

        existing_filenames = self.elements_get_existing_filenames()
        if len(existing_filenames) > 0:
            if QMessageBox.StandardButton.No == QMessageBox.question(
                self.main,
                "上書き確認",
                "出力先フォルダに同名ファイルが存在します、上書きしますか？\n" + "\n".join(existing_filenames),
                QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.No,
            ):
                QMessageBox.information(self.main, "処理中断", "処理を中断しました。")
                self.main.show()
                return

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
        )
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFinished.connect(self.add_elements_layer_to_project)
        thread.processFailed.connect(progress_dialog.close)
        thread.processFailed.connect(
            lambda error_message: QMessageBox.information(
                self.main, "エラー", f"エラーが発生しました。\n\n{error_message}"
            )
        )
        thread.start()
        progress_dialog.exec()

        if thread.abort_flag:
            QMessageBox.information(self.main, "中断", "処理を中断しました。")
        else:
            QMessageBox.information(self.main, "終了", "処理が終了しました。")

        self.main.show()

    @staticmethod
    def add_elements_layer_to_project(output_rlayers_dict):
        """
        要素計算の処理結果を受け取って各要素ごとの2レイヤーを1つのグループとしてプロジェクトに追加。

        DISPLAY_NAME（例：「収益性/地位/スギ」）はスラッシュ区切りで任意の階層数の
        グループにネストできる。2階層（例：「収益性/地利」）の場合は最後のセグメント
        専用の葉グループを作りその中に2レイヤーを格納する。3階層以上（例：
        「収益性/地位/スギ」）の場合は最後のセグメントは葉グループを作らず、
        1つ手前のグループ（「地位」）へ樹種ごとのレイヤーをまとめて直接追加する
        （2026-09-30、ユーザー指示による設計）。
        レイヤー実体の名前（rlayer.name()）はスコアリングタブ等が
        プロジェクト全体をフラットに名前検索する際に使うため変更せず、
        レイヤーツリー上の表示名だけ最後のセグメント（例：「スギ」）に短縮する。
        """
        root = QgsProject().instance().layerTreeRoot()
        group_cache = {}
        exclusive_groups = []

        def append_exclusive_group(group):
            if not any(group is existing for existing in exclusive_groups):
                exclusive_groups.append(group)

        for display_name, rlayers in reversed(list(output_rlayers_dict.items())):
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
                target_group.setExpanded(True)
                append_exclusive_group(target_group)
            else:
                # 3階層以上: 葉グループを作らず、手前のグループへ直接レイヤーをまとめる
                target_group = parent_group
                append_exclusive_group(target_group)

            for i, rlayer in enumerate(rlayers):
                # QMLでの定義がQGISの不具合で反映されないのでコードでも設定する
                rlayer.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)

                QgsProject.instance().addMapLayer(rlayer, False)
                layer_node = target_group.addLayer(rlayer)
                # シンボロジ（凡例の展開）は閉じておく
                layer_node.setExpanded(False)
                if i == 0:
                    # 生データ側は表示名だけ短縮する(rlayer.name()自体は変えない)
                    layer_node.setName(short_name)
                # スコアリング側はタブ間検索に使われる名前そのままを表示する

        profit_group = group_cache.get(("収益性",))
        if profit_group is not None:
            append_exclusive_group(profit_group)

        for group in exclusive_groups:
            ForestZoningMainDialogElements._set_mutually_exclusive_group(group, initial_child_index=None)

        risk_group = group_cache.get(("災害リスク",))
        if profit_group is not None and risk_group is not None:
            ForestZoningMainDialogElements._connect_axis_exclusive_groups(
                profit_group, risk_group
            )
        slope_group = group_cache.get(("災害リスク", "傾斜"))
        shc_group = group_cache.get(("災害リスク", "地形の複雑さ"))
        if slope_group is not None and shc_group is not None:
            ForestZoningMainDialogElements._connect_axis_exclusive_groups(
                slope_group, shc_group
            )

    @staticmethod
    def _set_mutually_exclusive_group(group, initial_child_index=None):
        children = group.children()
        if not children:
            return
        group.setItemVisibilityChecked(False)
        for idx, child in enumerate(children):
            child.setItemVisibilityChecked(
                initial_child_index is not None and idx == initial_child_index
            )
        if hasattr(group, "setIsMutuallyExclusive"):
            try:
                if initial_child_index is None:
                    group.setIsMutuallyExclusive(True)
                else:
                    initial_child_index = max(0, min(initial_child_index, len(children) - 1))
                    group.setIsMutuallyExclusive(True, initial_child_index)
            except TypeError:
                group.setIsMutuallyExclusive(True)

    @staticmethod
    def _connect_axis_exclusive_groups(profit_group, risk_group):
        syncing = {"active": False}

        def group_for_node(node):
            current = node
            while current is not None:
                if current is profit_group:
                    return profit_group
                if current is risk_group:
                    return risk_group
                current = current.parent()
            return None

        def set_ancestors_checked(node, stop_group):
            current = node
            while current is not None:
                current.setItemVisibilityChecked(True)
                if current is stop_group:
                    break
                current = current.parent()

        def set_group_unchecked(group):
            if hasattr(group, "setItemVisibilityCheckedRecursive"):
                group.setItemVisibilityCheckedRecursive(False)
            else:
                group.setItemVisibilityChecked(False)
                for child in group.children():
                    child.setItemVisibilityChecked(False)

        def on_visibility_changed(node):
            if syncing["active"] or not node.itemVisibilityChecked():
                return
            active_group = group_for_node(node)
            if active_group is None:
                return
            inactive_group = risk_group if active_group is profit_group else profit_group
            syncing["active"] = True
            try:
                set_ancestors_checked(node, active_group)
                set_group_unchecked(inactive_group)
            finally:
                syncing["active"] = False

        def connect_node(node):
            try:
                node.visibilityChanged.connect(on_visibility_changed)
            except (AttributeError, TypeError):
                pass
            for child in node.children():
                connect_node(child)

        connect_node(profit_group)
        connect_node(risk_group)
