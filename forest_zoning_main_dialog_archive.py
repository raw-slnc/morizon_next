import os
import zipfile
from datetime import datetime

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFileDialog, QLabel, QMenu, QMessageBox
from qgis.core import QgsProject

from . import morizon_data, morizon_restore, processes, utils
from .progress_dialog import ProgressDialog
from .constants import (
    DIR_AGGREGATE,
    DIR_DATA,
    DIR_SHARED,
    DIR_YOUSO,
    DIR_ZONING,
    INPUT_DEM,
    INPUT_NPP,
    INPUT_SRAD,
    INPUT_VTEX,
    INPUT_BUILDING,
    INPUT_NETWORK,
    INPUT_COSTCSV,
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_PARAMS_JSON,
    OUTPUT_PROFIT,
    OUTPUT_RISK,
    OUTPUT_SAVEAREA,
    OUTPUT_SHC,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_SLOPE,
    OUTPUT_ZONING,
    OUTPUT_ZONING_THRESHOLDS_JSON,
)

ARCHIVE_DIR_NAME = "morizon_next_archive"
ARCHIVE_FILE_PREFIX = "morizon_next"

# ZIPに同梱する付随ファイルの拡張子
# （読み込み時の検索で本体と取り違えないよう、.aux.xml等は入れない）
COMPANION_EXTENSIONS = {
    "tif": {".tif", ".tiff", ".tfw"},
    "shp": {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"},
    "csv": {".csv"},
}
# 出力ラスターと一緒に保存するファイル（本体と、名前が「本体名_」で始まるスタイル）
OUTPUT_RASTER_EXTENSIONS = {".tif", ".tiff", ".tfw", ".qml"}

ELEMENT_OUTPUTS = (
    OUTPUT_SITEIDX_SUGI, OUTPUT_SITEIDX_HINOKI, OUTPUT_SITEIDX_KARAMATSU, OUTPUT_COST,
    OUTPUT_DISTANCE, OUTPUT_SHC, OUTPUT_SLOPE, OUTPUT_SAVEAREA,
)


def _json_name(output_def):
    return f"{output_def['FILE_NAME']}.{output_def['EXTENSION']}"


class ForestZoningMainDialogArchive:
    """
    要素計算タブの「保存ファイルを読み込む」とタブ行右端の「保存ファイル出力」を実装するクラス。

    保存ファイル出力：入力（DATA/）と各タブの出力（YOUSO/・ZONING/・AGGREGATE/）を、
        原版の ZoningKit と同じ構成のZIPに保存し、MORIZON管理フォルダの個別データ
        （共有キャッシュを除く）をクリアする
    保存ファイル読み込み：管理フォルダのデータ、または保存ZIP・ZoningKit形式のフォルダを取り込み、
        各タブの入力・出力先と出力レイヤーを処理直後と同じ状態に戻す
    構成の判定や取り込みは morizon_data、レイヤーの作り直しは morizon_restore が受け持つ
    """

    def __init__(self, main):
        self.main = main

        # 読み込みは主要な入口のひとつなので、要素計算タブの開始ボタンの並びに置く
        self.main.elementsLoadSavedPushButton.setToolTip(
            "保存したデータを読み込み、各タブとレイヤーを保存したときの状態に戻します"
        )
        self.main.elementsLoadSavedPushButton.clicked.connect(self.show_load_menu)

        save_link = QLabel('<a href="#">保存ファイル出力</a>')
        save_link.setContentsMargins(0, 0, 8, 0)
        save_link.setToolTip(
            "入力データと出力結果をZIPに保存し、プロジェクト内の個別データをクリアします"
        )
        save_link.linkActivated.connect(self.run_archive)
        self.main.tabWidget.setCornerWidget(save_link, Qt.Corner.TopRightCorner)

    def _run_thread(self, thread) -> dict:
        """処理スレッドを進捗ダイアログ付きで実行し、結果を返す。
        成功・中断時は processFinished の辞書、失敗時は {"status": "failed", "message": ...}"""
        outcome = {}
        progress_dialog = ProgressDialog(thread.set_abort_flag)
        thread.processStarted.connect(progress_dialog.set_sum_of_processes)
        thread.addProgress.connect(progress_dialog.add_progress)
        thread.postMessage.connect(progress_dialog.set_messsage)
        thread.postDetail.connect(progress_dialog.set_detail)
        thread.setAbortable.connect(progress_dialog.set_abortable)
        thread.processFinished.connect(outcome.update)
        thread.processFinished.connect(progress_dialog.close)
        thread.processFailed.connect(lambda message: outcome.update(status="failed", message=message))
        thread.processFailed.connect(progress_dialog.close)
        thread.start()
        progress_dialog.exec()
        thread.wait()
        return outcome

    # ── 保存 ─────────────────────────────────────────────────────────

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

    @staticmethod
    def _output_raster_files(directory, file_name):
        if not directory or not os.path.isdir(directory):
            return []
        return [
            os.path.join(directory, name)
            for name in sorted(os.listdir(directory))
            if os.path.splitext(name)[1].lower() in OUTPUT_RASTER_EXTENSIONS
            and (os.path.splitext(name)[0] == file_name
                 or os.path.splitext(name)[0].startswith(file_name + "_"))
        ]

    @staticmethod
    def _existing(directory, name):
        path = os.path.join(directory, name) if directory else ""
        return [path] if path and os.path.isfile(path) else []

    def get_archive_entries(self) -> list:
        """ZIPに入れる(ZIP内のパス, ファイル)の配列。入力はDATA/、出力はタブごとのフォルダへ。
        出力はプラグインが書き出す名前のファイルだけを拾う（出力先に無関係なファイルがあっても巻き込まない）"""
        entries = []
        for input_def, path in self.get_input_entries():
            for filepath in self.get_companion_files(path, input_def["EXT"]):
                entries.append(("/".join([DIR_DATA, *input_def["PATH"], os.path.basename(filepath)]), filepath))

        # 作業システムのExcel（「デフォルトExcelを開く」で置いたもの・原版キットに入っていたもの）も
        # クリアで消えないよう、CSVと同じフォルダに入れて残す
        costcsv_dir = utils.get_workspace_dir(DIR_DATA, *INPUT_COSTCSV["PATH"])
        if os.path.isdir(costcsv_dir):
            for name in sorted(os.listdir(costcsv_dir)):
                if os.path.splitext(name)[1].lower() in (".xlsx", ".xls"):
                    entries.append((
                        "/".join([DIR_DATA, *INPUT_COSTCSV["PATH"], name]), os.path.join(costcsv_dir, name)
                    ))

        elements_dir = self.main.elementsOutputDirFileWidget.filePath()
        scoring_dir = self.main.scoringOutputDirFileWidget.filePath()
        zoning_dir = self.main.zoningOutputDirFileWidget.filePath()
        outputs = []
        for output_def in ELEMENT_OUTPUTS:
            outputs += [(DIR_YOUSO, f) for f in self._output_raster_files(elements_dir, output_def["FILE_NAME"])]
        for output_def in (OUTPUT_PROFIT, OUTPUT_RISK):
            outputs += [(DIR_ZONING, f) for f in self._output_raster_files(scoring_dir, output_def["FILE_NAME"])]
        outputs += [(DIR_ZONING, f) for f in self._existing(scoring_dir, _json_name(OUTPUT_PARAMS_JSON))]
        outputs += [(DIR_ZONING, f) for f in self._output_raster_files(zoning_dir, OUTPUT_ZONING["FILE_NAME"])]
        outputs += [(DIR_ZONING, f) for f in self._existing(zoning_dir, _json_name(OUTPUT_ZONING_THRESHOLDS_JSON))]

        aggregate_path = self.main.aggregateOutputDirFileWidget.filePath()
        if aggregate_path and os.path.isfile(aggregate_path):
            outputs += [(DIR_AGGREGATE, f) for f in self.get_companion_files(aggregate_path, "shp")]
            qml = os.path.splitext(aggregate_path)[0] + ".qml"
            if os.path.isfile(qml):
                outputs.append((DIR_AGGREGATE, qml))

        seen = {arcname for arcname, _ in entries}
        for folder, filepath in outputs:
            arcname = f"{folder}/{os.path.basename(filepath)}"
            if arcname not in seen:
                seen.add(arcname)
                entries.append((arcname, filepath))
        return entries

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

    def _clear_managed_data(self, managed_dir: str) -> list:
        # ファイルを削除できるよう、先にレイヤーを外して参照を解放する
        utils.remove_project_layers_under_dir(
            managed_dir, excluded_dirs=[os.path.join(managed_dir, DIR_SHARED)]
        )
        return morizon_data.clear_managed_dir(managed_dir)

    def run_archive(self, *args):
        project_home = QgsProject.instance().homePath()
        if project_home == "":
            QMessageBox.information(
                self.main, "エラー", "先にQGISプロジェクトを保存してください。"
            )
            return

        inputs = self.get_input_entries()
        if not inputs:
            QMessageBox.information(
                self.main, "エラー", "要素計算タブに入力データが設定されていません。"
            )
            return
        missing = [path for _, path in inputs if not os.path.exists(path)]
        if missing:
            QMessageBox.information(
                self.main, "エラー", "入力データが見つかりません。\n" + "\n".join(missing)
            )
            return

        entries = self.get_archive_entries()
        managed_dir = utils.get_morizon_managed_dir()
        archive_dir = os.path.join(project_home, ARCHIVE_DIR_NAME)
        archive_name = f"{ARCHIVE_FILE_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        archive_path = os.path.join(archive_dir, archive_name)

        counts = {}
        for arcname, _ in entries:
            counts[arcname.split("/")[0]] = counts.get(arcname.split("/")[0], 0) + 1
        content_lines = "\n".join(
            f"・{folder}/  {counts.get(folder, 0)}ファイル"
            for folder in (DIR_DATA, DIR_YOUSO, DIR_ZONING, DIR_AGGREGATE)
        )
        # 作業場が外部のフォルダのときは、そのフォルダを消さない（保存だけ行う）
        external = utils.get_external_workspace()
        if external:
            clear_text = f"作業場は外部のフォルダ（{os.path.basename(external)}）のため、保存後のクリアは行いません。\n\n"
        else:
            clear_text = (
                "保存後、次の個別データをクリアします。\n"
                f"・{managed_dir} 内のファイル（{DIR_SHARED} フォルダを除く）\n"
                "・上記を読み込んでいるプロジェクト上のレイヤー\n"
                "・各タブの入力欄\n\n"
            )
        answer = QMessageBox.question(
            self.main,
            "保存ファイル出力",
            f"入力データと出力結果を次のZIPに保存します。\n{archive_path}\n\n{content_lines}\n\n"
            + clear_text + "実行してよろしいですか？",
            QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # 大きなDEMの圧縮は時間がかかるため、ZIPの書き出しはワーカースレッドに任せる
        os.makedirs(archive_dir, exist_ok=True)
        outcome = self._run_thread(processes.data_archive.DataArchiveThread(entries, archive_path))
        if outcome.get("status") != "done":
            # ZIPが確実に保存できなかった場合はクリアしない
            if outcome.get("status") == "cancelled":
                text = "保存を中断しました。個別データはクリアしていません。"
            else:
                text = f"ZIPの保存に失敗したため、クリアは行いません。\n\n{outcome.get('message', '')}"
            QMessageBox.warning(self.main, "保存ファイル出力", text)
            return

        if external:
            QMessageBox.information(self.main, "保存ファイル出力", f"ZIPを保存しました。\n{archive_path}")
            return

        failed = self._clear_managed_data(managed_dir)
        with self.main.suppress_input_import():
            self.main.elements.reset_elements_inputs()
            self.main.aggregateDemFileWidget.setFilePath("")
        self.recreate_output_dirs(managed_dir)

        message = f"ZIPを保存しました。\n{archive_path}\n\n個別データをクリアしました。"
        if failed:
            message += "\n\n次のものは削除できませんでした（使用中の可能性があります）。\n" + "\n".join(failed)
        QMessageBox.information(self.main, "保存ファイル出力", message)

    # ── 読み込み ─────────────────────────────────────────────────────

    def show_load_menu(self, *args):
        menu = QMenu(self.main)
        menu.addAction("プロジェクト内のデータを読み直す", self.reload_managed_data)
        menu.addSeparator()
        menu.addAction("ZIPを選んで読み込む…", self.load_from_zip)
        menu.addAction("フォルダを選んで読み込む…", self.load_from_dir)
        button = self.main.elementsLoadSavedPushButton
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _require_project(self) -> bool:
        if QgsProject.instance().homePath() == "":
            QMessageBox.information(self.main, "エラー", "先にQGISプロジェクトを保存してください。")
            return False
        return True

    def reload_managed_data(self):
        """管理フォルダにあるデータをそのまま読み直す（ファイルは消さない）"""
        if not self._require_project():
            return
        managed_dir = utils.get_morizon_managed_dir()
        if not os.path.isdir(os.path.join(managed_dir, DIR_DATA)):
            QMessageBox.information(
                self.main, "保存ファイル読み込み",
                f"読み直すデータがありません。\n{os.path.join(managed_dir, DIR_DATA)}"
            )
            return
        self.load_managed_data()

    def load_from_zip(self):
        if not self._require_project():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.main, "読み込むZIPを選択", "", "ZIPファイル (*.zip *.ZIP)"
        )
        if path:
            self._import_and_load(path)

    def load_from_dir(self):
        if not self._require_project():
            return
        path = QFileDialog.getExistingDirectory(self.main, "読み込むフォルダを選択")
        if path:
            self._import_and_load(path)

    def _import_and_load(self, source: str):
        managed_dir = utils.get_morizon_managed_dir()
        source = os.path.normpath(source)
        if os.path.normcase(source) == os.path.normcase(managed_dir):
            self.reload_managed_data()
            return
        if utils.is_under_dir(os.path.abspath(source), managed_dir):
            # 取り込み前に管理フォルダを空にするため、中にあるものは取り込み元にできない
            QMessageBox.information(
                self.main, "保存ファイル読み込み",
                f"{managed_dir} の中にあるZIP・フォルダは読み込めません。\n"
                "別の場所に移してから選んでください。"
            )
            return
        try:
            morizon_data.inspect_source(source)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            QMessageBox.information(self.main, "保存ファイル読み込み", str(e))
            return

        answer = QMessageBox.question(
            self.main, "保存ファイル読み込み", "保存されていないデータは破棄されます。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return

        # ファイルを消せるよう、レイヤーの取り外しだけは先に画面側（メインスレッド）で行う。
        # 削除とコピー・展開は大きなDEMで時間がかかるため、ワーカースレッドに任せる
        utils.remove_project_layers_under_dir(
            managed_dir, excluded_dirs=[os.path.join(managed_dir, DIR_SHARED)]
        )
        outcome = self._run_thread(processes.data_import.DataImportThread(source, managed_dir))

        status = outcome.get("status")
        if status == "failed":
            QMessageBox.warning(self.main, "エラー", f"読み込みに失敗しました。\n\n{outcome.get('message', '')}")
            return
        if status == "cancelled":
            QMessageBox.information(
                self.main, "保存ファイル読み込み",
                "読み込みを中断しました。途中まで読み込んだデータは削除しました。"
            )
            # 入力欄・出力先が消えたファイルを指したままにならないよう、空の状態を反映する
            self.main.elements.reset_elements_inputs()
            self.main.aggregateDemFileWidget.setFilePath("")
            self.recreate_output_dirs(managed_dir)
            return
        self.load_managed_data()

    def load_managed_data(self):
        """管理フォルダ（DATA/・YOUSO/・ZONING/・AGGREGATE/）を各タブとレイヤーに反映する。
        保存データはプロジェクト内に取り込むので、作業場はプロジェクト内になる"""
        self.main.use_project_workspace(refill_inputs=False)
        managed_dir = utils.get_morizon_managed_dir()
        data_dir = os.path.join(managed_dir, DIR_DATA)
        youso_dir = os.path.join(managed_dir, DIR_YOUSO)
        zoning_dir = os.path.join(managed_dir, DIR_ZONING)
        aggregate_dir = os.path.join(managed_dir, DIR_AGGREGATE)
        for directory in (youso_dir, zoning_dir, aggregate_dir):
            os.makedirs(directory, exist_ok=True)

        # 同じファイルのレイヤーが二重にならないよう、管理フォルダのレイヤーは外してから作り直す
        utils.remove_project_layers_under_dir(
            managed_dir, excluded_dirs=[os.path.join(managed_dir, DIR_SHARED)]
        )

        # 入力
        with self.main.suppress_input_import():
            self.main.elements.reset_elements_inputs()
        found = self.main.set_inputs_from_data_dir(data_dir)
        notes = []
        for key, candidates in found.items():
            if len(candidates) > 1:
                notes.append(
                    f"{morizon_data.INPUT_DEFS[key]['DISPLAY_NAME']}：候補が{len(candidates)}個あるため"
                    f"「{os.path.basename(candidates[0])}」を使います"
                )

        # 出力先（作業場の中に決まる）
        self.main.apply_workspace_output_dirs()
        aggregate_shp = morizon_restore.find_aggregate_shp(aggregate_dir)

        # スコアリングのしきい値
        params_path = os.path.join(zoning_dir, _json_name(OUTPUT_PARAMS_JSON))
        if os.path.isfile(params_path):
            self.main.scoring.apply_params_file(params_path)

        # 出力レイヤー（処理の順に追加し、後の工程ほど上に来るようにする）
        # スタイルの作り直し（統計計算）は大きなラスターで時間がかかるため、ワーカースレッドで作る
        outcome = self._run_thread(processes.data_restore.DataRestoreThread(
            youso_dir, (found["costcsv"] or [None])[0], zoning_dir,
            aggregate_shp, self.main.aggregateStyleThresholdspinBox.value(),
        ))
        problems = list(outcome.get("problems", []))
        if outcome.get("status") == "failed":
            problems.append(f"出力レイヤーの作成に失敗しました：{outcome.get('message', '')}")
        elif outcome.get("status") == "cancelled":
            problems.append("出力レイヤーの作成を中断しました（作成済みの分だけ追加しています）")
        element_layers = outcome.get("elements", {})
        scoring_layers = outcome.get("scoring", {})
        zoning_layers = outcome.get("zoning", {})
        aggregate_layers = outcome.get("aggregate", {})
        counts = [len(element_layers), len(scoring_layers), len(zoning_layers), len(aggregate_layers)]
        if element_layers:
            self.main.elements.add_elements_layer_to_project(element_layers)
        if scoring_layers:
            self.main.scoring.add_layers_to_project(scoring_layers)
        if zoning_layers:
            self.main.zoning.add_layers_to_project(zoning_layers)
        if aggregate_layers:
            self.main.aggregate.add_layers_to_project(aggregate_layers)

        # 各タブの選択欄を、作り直したレイヤーに合わせる
        self.main.scoring.set_scoring_layer_combobox()
        self.main.zoning.select_restored_layers()
        self.main.aggregate.select_restored_layers()
        self.main.printlayout.update_printlayout_layer_scope()

        input_lines = "\n".join(
            f"・{defn['DISPLAY_NAME']}：{os.path.basename(found[key][0]) if found[key] else '（なし）'}"
            for key, defn in morizon_data.INPUT_DEFS.items()
        )
        message = (
            f"読み込みました。\n{managed_dir}\n\n入力\n{input_lines}\n\n"
            f"出力レイヤー\n・要素計算：{counts[0]}要素\n・スコアリング：{counts[1]}件\n"
            f"・ゾーニング：{counts[2]}件\n・集計：{counts[3]}件"
        )
        if not found["dem"]:
            message += "\n\nDEMがありません。要素計算にはDEMが必要です。"
        if notes or problems:
            message += "\n\n" + "\n".join(notes + problems)
        QMessageBox.information(self.main, "保存ファイル読み込み", message)
