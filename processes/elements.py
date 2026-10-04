# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# QGIS-API
import os
import shutil
import tempfile

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsRasterLayer

from . import raster_writer
from . import raster_styler
from .processing_feedback import LogForwardingFeedback
from ..utils import (
    get_tiff_info, is_resampling_needed, AsciiSafeProcessingTmpdir, get_ascii_safe_alias,
    move_output_layers_to_main_thread,
)
from ..constants import (
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_SAVEAREA,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_SHC,
    OUTPUT_SLOPE
)


class ProcessingThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postLog = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, input_files_dict: dict, target_elements_dict: dict, output_dir: str,
                 final_extent_wgs84=None):
        super().__init__()
        self.input_files_dict = input_files_dict
        self.target_elements_dict = target_elements_dict
        self.output_dir = output_dir
        self.final_extent_wgs84 = final_extent_wgs84
        self.no_road_distance_created = False

        self.abort_flag = False
        self.feedback = None

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag
        if flag and self.feedback is not None:
            self.feedback.cancel()

    def _clip_final_output(self, filepath: str) -> str:
        if filepath and self.final_extent_wgs84 is not None:
            return raster_writer.replace_with_clipped_wgs84_extent(
                filepath, self.final_extent_wgs84, feedback=self.feedback
            )
        return filepath

    def run(self):
        """
        「要素計算」処理を実行する
        最大6種類8ラスターが生成され、1ラスターにつき2レイヤーがプロジェクトに追加される

        Args:
            input_files_dict (dict): 入力ファイルに関するUIの入力状態をまとめた辞書
            target_elements_dict (dict): どの要素が処理対象かまとめた辞書 - {レイヤー名: bool}
            output_dir (str): ファイル出力先
        """

        # 処理に成功したレイヤーの名前とインスタンスを保持する辞書
        output_rlayers_dict = {}

        # SAGA/GRASS等がTMP/TEMPや入力ファイルパスの全角文字でエラーを起こす問題への対処。
        # ユーザーのフォルダ名・プロジェクトの保存場所は一切変えさせず、
        # 処理の内部でだけ半角安全な別名パス・一時フォルダに差し替える
        ascii_tmpdir = AsciiSafeProcessingTmpdir()
        ascii_tmpdir.enter()
        intermediate_dir = tempfile.mkdtemp()
        # Processing の外部コマンド出力を、OS共通の経路で進捗ダイアログへ渡す。
        # 各 raster_writer まで同じ feedback を引き回すことが重要。
        self.feedback = LogForwardingFeedback(self.postLog.emit)

        self.input_files_dict = {
            key: (get_ascii_safe_alias(path) if path else path)
            for key, path in self.input_files_dict.items()
        }

        try:
            is_resampling = is_resampling_needed(
                get_tiff_info(self.input_files_dict["dem"], feedback=self.feedback))

            sum_of_processes = len(list(filter(
                lambda val: val, self.target_elements_dict.values()))) + int(is_resampling)
            self.processStarted.emit(sum_of_processes)
            progress_counter = 0

            # 必要ならDEMをリサンプリング
            dem_for_processes = self.input_files_dict["dem"]
            if is_resampling:
                self.addProgress.emit(1)
                progress_counter += 1
                self.postMessage.emit('DEMをリサンプリング中')
                resampled_dem_filepath = os.path.join(intermediate_dir, "dem_resampled_10m.tif")
                dem_for_processes = raster_writer.resampling(
                    self.input_files_dict["dem"], 10,
                    output_filepath=resampled_dem_filepath, feedback=self.feedback)
                if not os.path.exists(dem_for_processes):
                    raise RuntimeError(f"リサンプリング後のDEMを作成できませんでした: {dem_for_processes}")

                if self.abort_flag:
                    self.processFinished.emit({})
                    return

            if self.target_elements_dict["siteidx"]:
                self.postMessage.emit('地位指数を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                siteidx_filepaths = raster_writer.siteidx.generate(dem_for_processes,
                                                                   self.input_files_dict["npp"],
                                                                   self.input_files_dict["srad"],
                                                                   self.input_files_dict["vtex"],
                                                                   self.output_dir,
                                                                   feedback=self.feedback)
                siteidx_filepaths = [
                    self._clip_final_output(path) for path in siteidx_filepaths
                ]
                display_names = [
                    OUTPUT_SITEIDX_SUGI["DISPLAY_NAME"],
                    OUTPUT_SITEIDX_HINOKI["DISPLAY_NAME"],
                    OUTPUT_SITEIDX_KARAMATSU["DISPLAY_NAME"]
                ]
                siteidx_suffixes = ["sugi", "hinoki", "karamatsu"]

                for idx, path in enumerate(siteidx_filepaths):
                    siteidx_rawdata_qml_filepath = raster_styler.siteidx.write_rawdata_qml(
                        path, wood_type=siteidx_suffixes[idx])
                    siteidx_scoring_qml_filepath = raster_styler.siteidx.write_scoring_qml(
                        path)
                    siteidx_rawdata_rlayer = QgsRasterLayer(path,
                                                            display_names[idx])
                    siteidx_scoring_rlayer = QgsRasterLayer(path,
                                                            display_names[idx] + "[スコアリング]")
                    siteidx_rawdata_rlayer.loadNamedStyle(
                        siteidx_rawdata_qml_filepath)
                    siteidx_scoring_rlayer.loadNamedStyle(
                        siteidx_scoring_qml_filepath)
                    output_rlayers_dict[display_names[idx]] = [
                        siteidx_rawdata_rlayer, siteidx_scoring_rlayer]

                if self.abort_flag:
                    self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                    return

            if self.target_elements_dict["cost"]:
                self.postMessage.emit(f'{OUTPUT_COST["DISPLAY_NAME"]}を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                cost_filepath = raster_writer.cost.generate(dem_for_processes,
                                                            self.input_files_dict["costcsv"],
                                                            self.output_dir,
                                                            feedback=self.feedback)
                cost_filepath = self._clip_final_output(cost_filepath)
                cost_rawdata_qml_filepath = raster_styler.cost.write_rawdata_qml(self.input_files_dict["costcsv"],
                                                                                 self.output_dir)
                cost_scoring_qml_filepath = raster_styler.cost.write_scoring_qml(cost_filepath,
                                                                                 self.output_dir)
                cost_rawdata_rlayer = QgsRasterLayer(cost_filepath,
                                                     OUTPUT_COST["DISPLAY_NAME"])
                cost_scoring_rlayer = QgsRasterLayer(cost_filepath,
                                                     OUTPUT_COST["DISPLAY_NAME"] + "[スコアリング]")
                cost_rawdata_rlayer.loadNamedStyle(cost_rawdata_qml_filepath)
                cost_scoring_rlayer.loadNamedStyle(cost_scoring_qml_filepath)

                output_rlayers_dict[OUTPUT_COST["DISPLAY_NAME"]] = [
                    cost_rawdata_rlayer, cost_scoring_rlayer]

                if self.abort_flag:
                    self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                    return

            if self.target_elements_dict["distance"]:
                self.postMessage.emit(f'{OUTPUT_DISTANCE["DISPLAY_NAME"]}を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                distance_filepath, no_road_distance_created = raster_writer.distance.generate(
                    dem_for_processes,
                    self.input_files_dict["network"],
                    self.output_dir,
                    feedback=self.feedback,
                )
                if no_road_distance_created:
                    self.no_road_distance_created = True
                    self.postMessage.emit(
                        f'{OUTPUT_DISTANCE["DISPLAY_NAME"]}: 道路地物が無いため全域1点相当として作成しました')
                if distance_filepath is None:
                    self.postMessage.emit(
                        f'{OUTPUT_DISTANCE["DISPLAY_NAME"]}: 対象範囲に道路データが無いためスキップしました')
                else:
                    distance_filepath = self._clip_final_output(distance_filepath)
                    distance_rawdata_qml_filepath = raster_styler.distance.write_rawdata_qml(
                        self.output_dir)
                    distance_scoring_qml_filepath = raster_styler.distance.write_scoring_qml(distance_filepath,
                                                                                             self.output_dir)
                    distance_rawdata_rlayer = QgsRasterLayer(distance_filepath,
                                                             OUTPUT_DISTANCE["DISPLAY_NAME"])
                    distance_scoring_rlayer = QgsRasterLayer(distance_filepath,
                                                             OUTPUT_DISTANCE["DISPLAY_NAME"] + "[スコアリング]")
                    distance_rawdata_rlayer.loadNamedStyle(
                        distance_rawdata_qml_filepath)
                    distance_scoring_rlayer.loadNamedStyle(
                        distance_scoring_qml_filepath)
                    output_rlayers_dict[OUTPUT_DISTANCE["DISPLAY_NAME"]] = [
                        distance_rawdata_rlayer, distance_scoring_rlayer]

                if self.abort_flag:
                    self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                    return

            if self.target_elements_dict["shc"]:
                self.postMessage.emit(f'{OUTPUT_SHC["DISPLAY_NAME"]}を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                shc_filepath = raster_writer.shc.generate(dem_for_processes,
                                                          self.output_dir,
                                                          feedback=self.feedback)
                shc_filepath = self._clip_final_output(shc_filepath)
                shc_rawdata_qml_filepath = raster_styler.shc.write_rawdata_qml(shc_filepath,
                                                                               self.output_dir)
                shc_scoring_qml_filepath = raster_styler.shc.write_scoring_qml(shc_filepath,
                                                                               self.output_dir)
                shc_rawdata_rlayer = QgsRasterLayer(shc_filepath,
                                                    OUTPUT_SHC["DISPLAY_NAME"])
                shc_scoring_rlayer = QgsRasterLayer(shc_filepath,
                                                    OUTPUT_SHC["DISPLAY_NAME"] + "[スコアリング]")
                shc_rawdata_rlayer.loadNamedStyle(shc_rawdata_qml_filepath)
                shc_scoring_rlayer.loadNamedStyle(shc_scoring_qml_filepath)
                output_rlayers_dict[OUTPUT_SHC["DISPLAY_NAME"]] = [
                    shc_rawdata_rlayer, shc_scoring_rlayer]

                if self.abort_flag:
                    self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                    return

            if self.target_elements_dict["slope"]:
                self.postMessage.emit(f'{OUTPUT_SLOPE["DISPLAY_NAME"]}を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                slope_filepath = raster_writer.slope.generate(dem_for_processes,
                                                              self.output_dir,
                                                              feedback=self.feedback)
                slope_filepath = self._clip_final_output(slope_filepath)
                slope_rawdata_qml_filepath = raster_styler.slope.write_rawdata_qml(
                    self.output_dir)
                slope_scoring_qml_filepath = raster_styler.slope.write_scoring_qml(
                    self.output_dir)
                slope_rawdata_rlayer = QgsRasterLayer(slope_filepath,
                                                      OUTPUT_SLOPE["DISPLAY_NAME"])
                slope_scoring_rlayer = QgsRasterLayer(slope_filepath,
                                                      OUTPUT_SLOPE["DISPLAY_NAME"] + "[スコアリング]")
                slope_rawdata_rlayer.loadNamedStyle(slope_rawdata_qml_filepath)
                slope_scoring_rlayer.loadNamedStyle(slope_scoring_qml_filepath)
                output_rlayers_dict[OUTPUT_SLOPE["DISPLAY_NAME"]] = [
                    slope_rawdata_rlayer, slope_scoring_rlayer]

                if self.abort_flag:
                    self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                    return

            if self.target_elements_dict["savearea"]:
                self.postMessage.emit(f'{OUTPUT_SAVEAREA["DISPLAY_NAME"]}を計算中')
                self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                self.addProgress.emit(1)
                progress_counter += 1

                savearea_filepath = raster_writer.savearea.generate(dem_for_processes,
                                                                    self.input_files_dict["building"],
                                                                    self.output_dir,
                                                                    feedback=self.feedback)
                if savearea_filepath is None:
                    self.postMessage.emit(
                        f'{OUTPUT_SAVEAREA["DISPLAY_NAME"]}: 対象範囲に建物データが無いためスキップしました')
                else:
                    savearea_filepath = self._clip_final_output(savearea_filepath)
                    savearea_rawdata_qml_filepath = raster_styler.savearea.write_rawdata_qml(
                        self.output_dir)
                    savearea_scoring_qml_filepath = raster_styler.savearea.write_scoring_qml(
                        self.output_dir)
                    savearea_rawdata_rlayer = QgsRasterLayer(savearea_filepath,
                                                             OUTPUT_SAVEAREA["DISPLAY_NAME"])
                    savearea_scoring_rlayer = QgsRasterLayer(savearea_filepath,
                                                             OUTPUT_SAVEAREA["DISPLAY_NAME"] + "[スコアリング]")
                    savearea_rawdata_rlayer.loadNamedStyle(
                        savearea_rawdata_qml_filepath)
                    savearea_scoring_rlayer.loadNamedStyle(
                        savearea_scoring_qml_filepath)
                    output_rlayers_dict[OUTPUT_SAVEAREA["DISPLAY_NAME"]] = [
                        savearea_rawdata_rlayer, savearea_scoring_rlayer]
        except Exception as e:
            # エラーはまとめてキャッチして呼び出し元に報告・処理を中断
            self.processFailed.emit(str(e))
            self.abort_flag = True
            self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
            return
        finally:
            ascii_tmpdir.restore()
            shutil.rmtree(intermediate_dir, ignore_errors=True)

        self.postMessage.emit('終了処理中')

        # 本当はここでプロジェクトにレイヤーを追加したい
        # しかし別スレッドでプロジェクトに追加されたレイヤーはUIで認識できない
        # なのでメインスレッドでレイヤーを追加するため、処理結果をメインスレッドに渡す
        self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
