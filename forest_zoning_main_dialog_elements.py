import os
import glob
import re

# QGIS-API
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *


from .progress_dialog import ProgressDialog
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
from .utils import is_tmpdir_valid


class ForestZoningMainDialogElements:
    """
    メイン画面の「要素計算」タブの処理を実装するクラス
    """

    def __init__(self, main):
        self.main = main
        self.init_elements_ui()

    def init_elements_ui(self):
        # connect signals
        self.main.elementsRunPushButton.clicked.connect(self.run_elements)
        self.main.elementsLoadFromDirPushButton.clicked.connect(
            self.load_elements_files_from_dir
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

        self.refresh_elements_ui()

    def refresh_elements_ui(self):
        self.set_elements_filewidgets_enabled()
        self.main.elementsErrorLabel.setText("\n".join(self.get_elements_error_texts()))
        self.main.elementsRunPushButton.setEnabled(
            len(self.get_elements_error_texts()) == 0
        )

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

        def validate_input(filepath: str, input_name: str):
            """
            入力データのバリデーション
            """
            if filepath == "":
                error_texts.append(f"{input_name}を設定してください")
            else:
                if re.search("[^\x01-\x7E]", filepath):
                    error_texts.append(f"{input_name}のファイルパスに全角文字列が含まれています")

        # 必須データをバリデーション
        mondatory_files_dict = self.get_elements_mandatory_files_dict()
        if mondatory_files_dict["dem"]:
            validate_input(
                self.main.elementsDemFileWidget.filePath(), INPUT_DEM["DISPLAY_NAME"]
            )
        if mondatory_files_dict["npp"]:
            validate_input(
                self.main.elementsNppFileWidget.filePath(), INPUT_NPP["DISPLAY_NAME"]
            )
        if mondatory_files_dict["srad"]:
            validate_input(
                self.main.elementsSradFileWidget.filePath(), INPUT_SRAD["DISPLAY_NAME"]
            )
        if mondatory_files_dict["vtex"]:
            validate_input(
                self.main.elementsVtexFileWidget.filePath(), INPUT_VTEX["DISPLAY_NAME"]
            )
        if mondatory_files_dict["building"]:
            validate_input(
                self.main.elementsBuildingFileWidget.filePath(),
                INPUT_BUILDING["DISPLAY_NAME"],
            )
        if mondatory_files_dict["network"]:
            validate_input(
                self.main.elementsNetworkFileWidget.filePath(),
                INPUT_NETWORK["DISPLAY_NAME"],
            )
        if mondatory_files_dict["costcsv"]:
            validate_input(
                self.main.elementsCostCsvFileWidget.filePath(),
                INPUT_COSTCSV["DISPLAY_NAME"],
            )

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

        # SAGA・GRASSエラーを回避するために環境変数に不正な文字がないか確認
        if (
            target_elements_dict["distance"]
            or target_elements_dict["shc"]
            or target_elements_dict["savearea"]
        ):
            if not is_tmpdir_valid():
                QMessageBox.information(
                    self.main,
                    "エラー",
                    f"TEMPディレクトリーに不正な文字があります。\nマニュアルに従い、システム環境変数を設定していください。",
                )
                self.main.show()
                return

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
        要素計算の処理結果を受け取って各要素ごとの2レイヤーを1つのグループとしてプロジェクトに追加
        """
        for display_name, rlayers in reversed(list(output_rlayers_dict.items())):
            root = QgsProject().instance().layerTreeRoot()
            group_node = root.insertGroup(0, display_name)
            group_node.setExpanded(False)

            for rlayer in rlayers:
                # QMLでの定義がQGISの不具合で反映されないのでコードでも設定する
                rlayer.setBlendMode(QPainter.CompositionMode_Multiply)

                QgsProject.instance().addMapLayer(rlayer, False)
                group_node.addLayer(rlayer)
