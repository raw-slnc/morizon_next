import os
import shutil
import zipfile
from datetime import datetime

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QLabel, QMessageBox
from qgis.core import QgsProject

from . import utils
from .constants import (
    INPUT_DEM,
    INPUT_NPP,
    INPUT_SRAD,
    INPUT_VTEX,
    INPUT_BUILDING,
    INPUT_NETWORK,
    INPUT_COSTCSV,
)

ARCHIVE_DIR_NAME = "morizon_next_archive"
ARCHIVE_FILE_PREFIX = "morizon_next"
# 解析をまたいで使い回すキャッシュ。個別データのクリア対象から外す
SHARED_DIR_NAME = "shared"

# ZIPに同梱する付随ファイルの拡張子
# （「フォルダから読み込み」の検索で本体と取り違えないよう、.aux.xml等は入れない）
COMPANION_EXTENSIONS = {
    "tif": {".tif", ".tiff", ".tfw"},
    "shp": {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"},
    "csv": {".csv"},
}


class ForestZoningMainDialogArchive:
    """
    タブ行右端の「保存ファイル出力」の処理を実装するクラス。
    要素計算タブの入力データを「フォルダから読み込み」で読める階層のZIPに保存し、
    その後MORIZON管理フォルダの個別データ（共有キャッシュを除く）をクリアする
    """

    def __init__(self, main):
        self.main = main
        link = QLabel('<a href="#">保存ファイル出力</a>')
        link.setContentsMargins(0, 0, 8, 0)
        link.setToolTip(
            "入力データをZIPに保存し、プロジェクト内の個別データをクリアします"
        )
        link.linkActivated.connect(self.run_archive)
        self.main.tabWidget.setCornerWidget(link, Qt.Corner.TopRightCorner)

    def get_input_entries(self) -> list:
        """要素計算タブで設定されている入力ファイルを(入力定義, パス)の配列で返す"""
        entries = []
        for input_def, filewidget in (
            (INPUT_DEM, self.main.elementsDemFileWidget),
            (INPUT_NPP, self.main.elementsNppFileWidget),
            (INPUT_SRAD, self.main.elementsSradFileWidget),
            (INPUT_VTEX, self.main.elementsVtexFileWidget),
            (INPUT_BUILDING, self.main.elementsBuildingFileWidget),
            (INPUT_NETWORK, self.main.elementsNetworkFileWidget),
            (INPUT_COSTCSV, self.main.elementsCostCsvFileWidget),
        ):
            path = filewidget.filePath()
            if path:
                entries.append((input_def, path))
        return entries

    @staticmethod
    def get_companion_files(filepath: str, ext: str) -> list:
        """本体ファイルと同じ名前の付随ファイル（shpの.dbf等）を含めて返す"""
        directory = os.path.dirname(filepath)
        stem = os.path.splitext(os.path.basename(filepath))[0]
        allowed = COMPANION_EXTENSIONS.get(ext.lower(), {"." + ext.lower()})
        files = []
        for name in sorted(os.listdir(directory)):
            name_stem, name_ext = os.path.splitext(name)
            if name_stem == stem and name_ext.lower() in allowed:
                files.append(os.path.join(directory, name))
        return files

    def write_archive(self, entries: list, archive_path: str):
        """入力データを「フォルダから読み込み」の階層（DEM/、SiteIndex/NPP/ 等）でZIPに書き出す"""
        temp_path = archive_path + ".part"
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for input_def, path in entries:
                    for filepath in self.get_companion_files(path, input_def["EXT"]):
                        arcname = "/".join(input_def["PATH"] + [os.path.basename(filepath)])
                        zf.write(filepath, arcname)
            with zipfile.ZipFile(temp_path) as zf:
                broken = zf.testzip()
            if broken is not None:
                raise RuntimeError(f"ZIPの検証に失敗しました: {broken}")
            os.replace(temp_path, archive_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @staticmethod
    def delete_individual_data(managed_dir: str) -> list:
        """MORIZON管理フォルダ内の共有キャッシュ以外を削除し、削除できなかったものを返す"""
        failed = []
        if not os.path.isdir(managed_dir):
            return failed
        for name in os.listdir(managed_dir):
            if name == SHARED_DIR_NAME:
                continue
            path = os.path.join(managed_dir, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError as e:
                failed.append(f"{name}: {e}")
        return failed

    def recreate_output_dirs(self, managed_dir: str):
        """各タブの出力先がMORIZON管理フォルダ内なら、削除したフォルダを作り直す"""
        for filewidget, is_file in (
            (self.main.elementsOutputDirFileWidget, False),
            (self.main.scoringOutputDirFileWidget, False),
            (self.main.zoningOutputDirFileWidget, False),
            (self.main.aggregateOutputDirFileWidget, True),
        ):
            path = filewidget.filePath()
            if not path:
                continue
            directory = os.path.dirname(path) if is_file else path
            if utils.is_under_dir(os.path.abspath(directory), managed_dir):
                os.makedirs(directory, exist_ok=True)

    def run_archive(self, *args):
        project_home = QgsProject.instance().homePath()
        if project_home == "":
            QMessageBox.information(
                self.main, "エラー", "先にQGISプロジェクトを保存してください。"
            )
            return

        entries = self.get_input_entries()
        if not entries:
            QMessageBox.information(
                self.main, "エラー", "要素計算タブに入力データが設定されていません。"
            )
            return
        missing = [path for _, path in entries if not os.path.exists(path)]
        if missing:
            QMessageBox.information(
                self.main, "エラー", "入力データが見つかりません。\n" + "\n".join(missing)
            )
            return

        managed_dir = utils.get_morizon_managed_dir()
        archive_dir = os.path.join(project_home, ARCHIVE_DIR_NAME)
        archive_name = f"{ARCHIVE_FILE_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        archive_path = os.path.join(archive_dir, archive_name)

        input_lines = "\n".join(
            "・" + "/".join(input_def["PATH"]) + "/" + os.path.basename(path)
            for input_def, path in entries
        )
        answer = QMessageBox.question(
            self.main,
            "保存ファイル出力",
            f"入力データを次のZIPに保存します。\n{archive_path}\n\n{input_lines}\n\n"
            "保存後、次の個別データをクリアします。\n"
            f"・{managed_dir} 内のファイル（{SHARED_DIR_NAME} フォルダを除く）\n"
            "・上記を読み込んでいるプロジェクト上のレイヤー\n"
            "・各タブの入力欄\n\n"
            "実行してよろしいですか？",
            QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            os.makedirs(archive_dir, exist_ok=True)
            self.write_archive(entries, archive_path)
        except (OSError, RuntimeError, zipfile.BadZipFile) as e:
            # ZIPが確実に保存できなかった場合はクリアしない
            QMessageBox.warning(
                self.main, "エラー", f"ZIPの保存に失敗したため、クリアは行いません。\n\n{e}"
            )
            return

        # ファイルを削除できるよう、先にレイヤーを外して参照を解放する
        utils.remove_project_layers_under_dir(
            managed_dir, excluded_dirs=[os.path.join(managed_dir, SHARED_DIR_NAME)]
        )
        failed = self.delete_individual_data(managed_dir)

        self.main.elements.reset_elements_inputs()
        self.main.aggregateDemFileWidget.setFilePath("")
        self.recreate_output_dirs(managed_dir)

        message = f"ZIPを保存しました。\n{archive_path}\n\n個別データをクリアしました。"
        if failed:
            message += "\n\n次のものは削除できませんでした（使用中の可能性があります）。\n" + "\n".join(failed)
        QMessageBox.information(self.main, "保存ファイル出力", message)
