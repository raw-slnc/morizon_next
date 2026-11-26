# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# QGIS-API
import os
import shutil
import tempfile
import time

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsRasterLayer

from . import raster_writer
from . import raster_styler
from .processing_feedback import LogForwardingFeedback, set_stage
from ..settings_manager import CustomShcManager
from ..utils import (
    get_tiff_info, is_resampling_needed, get_morizon_log_dir, AsciiSafeProcessingTmpdir, get_ascii_safe_alias,
    remove_files, move_output_layers_to_main_thread,
)
from ..constants import (
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_SAVEAREA,
    OUTPUT_SAVEAREA_CUSTOM,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_DOKUJI,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_SHC,
    OUTPUT_SHC_CUSTOM,
    OUTPUT_SLOPE,
    SAVEAREA_CONTOUR_BONUS,
)


# 取り込んだ地位指数マップを DEM のマス目にそろえるときの NoData の値
SITEIDX_DOKUJI_NODATA = -9999.0

# 処理時間の記録（毎回上書き。溜めない）。個人情報（パス・プロジェクト名など）は書かない
ONETIME_LOG_DIR_NAME = "log"
ONETIME_LOG_FILE_NAME = "onetimelog_except_private_data.log"


class ProcessingThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
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
        # 処理時間の記録の置き場所。morizon_next/log/<プロジェクト名>（メインスレッドで決める）。未保存なら出力先の log
        log_dir = get_morizon_log_dir() or (os.path.join(output_dir, ONETIME_LOG_DIR_NAME) if output_dir else "")
        self._onetime_log_path = os.path.join(log_dir, ONETIME_LOG_FILE_NAME) if log_dir else ""
        self._stage_marks = []
        self._log_started = time.monotonic()
        self._log_dem_info = "不明"
        self._log_finished = False
        self.custom_shc_enabled = CustomShcManager().load_enabled()
        self.no_road_distance_created = False
        self.no_building_savearea_created = False
        self.no_overlap_savearea_created = False

        self.abort_flag = False
        self.feedback = None
        # DEMにNoDataがあるとき、その元のDEM（成果物のNoDataを戻すのに使う）。無ければ None
        self._dem_with_nodata = None

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag
        if flag and self.feedback is not None:
            self.feedback.cancel()

    def _clip_final_output(self, filepath: str) -> str:
        """最終成果物を仕上げる：元のDEMのNoData（海・水面など）を戻し、表示・解析範囲で切り出す"""
        set_stage(self.feedback, "結果の仕上げ")
        if filepath and self._dem_with_nodata is not None:
            raster_writer.restore_dem_nodata(filepath, self._dem_with_nodata, feedback=self.feedback)
        if filepath and self.final_extent_wgs84 is not None:
            return raster_writer.replace_with_clipped_wgs84_extent(
                filepath, self.final_extent_wgs84, feedback=self.feedback
            )
        return filepath

    def _generate_siteidx_dokuji(self, dem_filepath):
        """取り込んだ地位指数マップを、値を変えないよう最近傍法で DEM のマス目にそろえて出力する。
        マップが無ければ、前に作った「地位（独自）」の出力を消して None を返す（外したマップの結果を残さない）"""
        output_filepath = os.path.join(self.output_dir, OUTPUT_SITEIDX_DOKUJI["FILE_NAME"] + ".tif")
        map_filepath = self.input_files_dict.get("siteidx_map") or ""
        if not map_filepath or not os.path.isfile(map_filepath):
            prefix = OUTPUT_SITEIDX_DOKUJI["FILE_NAME"]
            if os.path.isdir(self.output_dir):
                for name in os.listdir(self.output_dir):
                    stem = os.path.splitext(name)[0]
                    if stem == prefix or stem.startswith(prefix + "_") or name.startswith(prefix + ".tif."):
                        os.remove(os.path.join(self.output_dir, name))
            return None
        set_stage(self.feedback, "地位指数マップをDEMのマス目にそろえる")
        shutil.copyfile(map_filepath, output_filepath)
        # マップが DEM を覆わない所は NoData にする（マップに NoData の値が無いと 0 になり、地位指数 0 として
        # 最低点が付き、しきい値の初期値もずれるため）。マップの NoData も同じ値にそろう
        raster_writer.replace_with_adjusted_extent_and_resolution(
            dem_filepath, output_filepath, resampling_alg_name="nearest", feedback=self.feedback,
            nodata=SITEIDX_DOKUJI_NODATA)
        return self._clip_final_output(output_filepath)

    def _mark_stage(self, message):
        """工程の始まりの時刻を控える（処理時間の記録用）。postMessage と同じスレッドで呼ばれる"""
        if self._log_finished or message == '終了処理中':
            return  # 終わりの書き出しの後は、上書きしない
        self._stage_marks.append((time.monotonic(), message))
        # 途中で異常終了しても、そこまでの時間が残るよう、工程の始まりごとに書く
        self._write_onetime_log(self._log_started, "実行中（この表示のまま残っていれば、異常終了した可能性があります）",
                                self._log_dem_info)

    def _write_onetime_log(self, started, status, dem_info):
        """工程ごとの所要時間を onetimelog_except_private_data.log に書く。実行のたびに上書きし、溜めない。
        ファイルのパス・プロジェクト名などは書かない。書けなくても処理は止めない"""
        if not self._onetime_log_path:
            return
        try:
            ended = time.monotonic()
            marks = [m for m in self._stage_marks if m[1] != '終了処理中']
            lines = [
                "MORIZON NEXT 要素計算の処理時間（毎回上書き。パス・プロジェクト名などは含みません）",
                f"記録日時: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"結果: {status}",
                f"DEM: {dem_info}",
                "等高線法: " + ("オン（CS立体図の青で補正）" if self.custom_shc_enabled else "オフ"),
                "",
                "工程ごとの所要時間",
            ]
            for index, (start, message) in enumerate(marks):
                end = marks[index + 1][0] if index + 1 < len(marks) else ended
                lines.append(f"  {end - start:8.1f} 秒  {message}")
            lines.append(f"  {ended - started:8.1f} 秒  合計")
            os.makedirs(os.path.dirname(self._onetime_log_path), exist_ok=True)
            # 一時ファイルに書いてから置き換える（書いている途中に落ちても、半端なファイルを残さない）
            temp_path = self._onetime_log_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(temp_path, self._onetime_log_path)
        except Exception:  # nosec B110 - 記録に失敗しても計算は続ける
            pass

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
        self.feedback = LogForwardingFeedback(self.postLog.emit, self.postDetail.emit)
        # 次の工程に移ったら、前の工程の段階名を消す
        self.postMessage.connect(lambda _message: set_stage(self.feedback, None))
        self._stage_marks = []
        self.postMessage.connect(lambda message: self._mark_stage(message))
        run_started = self._log_started = time.monotonic()
        self._log_finished = False
        run_status = "中断（または途中で終了）"
        dem_info = "不明"

        input_aliases = []
        self.input_files_dict = {
            key: (get_ascii_safe_alias(path, created=input_aliases) if path else path)
            for key, path in self.input_files_dict.items()
        }

        try:
            dem_tiff_info = get_tiff_info(self.input_files_dict["dem"], feedback=self.feedback)
            is_resampling = is_resampling_needed(dem_tiff_info)
            size = dem_tiff_info.get("size") or [0, 0]
            dem_info = (f"{size[0]}×{size[1]} セル（{size[0] * size[1]:,} セル）、"
                        f"{dem_tiff_info.get('resolution'):g} m、"
                        + ("10 m にまとめ直した" if is_resampling else "まとめ直しなし"))
            self._log_dem_info = dem_info

            sum_of_processes = (sum(self.target_elements_dict.values()) + int(is_resampling)
                                + int(self.custom_shc_enabled and self.target_elements_dict["shc"])
                                + int(self.custom_shc_enabled and self.target_elements_dict["savearea"]))
            self.processStarted.emit(sum_of_processes)
            progress_counter = 0

            # DEMのNoData（海・水面など）は埋めずにそのまま計算する（原版と同じ）。
            # DEMを使わない要素（地位・地利）の成果物も、最終成果物で同じ場所をNoDataにそろえる（_clip_final_output）
            dem_for_processes = self.input_files_dict["dem"]
            if raster_writer.dem_has_nodata(dem_for_processes):
                self._dem_with_nodata = dem_for_processes

            # 必要ならDEMをリサンプリング
            if is_resampling:
                self.addProgress.emit(1)
                progress_counter += 1
                self.postMessage.emit('DEMをリサンプリング中')
                resampled_dem_filepath = os.path.join(intermediate_dir, "dem_resampled_10m.tif")
                set_stage(self.feedback, "10m のマス目にまとめ直す")
                dem_for_processes = raster_writer.resampling(
                    dem_for_processes, 10,
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

                # 取り込んだ地位指数マップ（手引 4.2）があれば、DEM のマス目にそろえて「地位（独自）」を作る
                dokuji_filepath = self._generate_siteidx_dokuji(dem_for_processes)
                if dokuji_filepath:
                    dokuji_rawdata_rlayer = QgsRasterLayer(dokuji_filepath, OUTPUT_SITEIDX_DOKUJI["DISPLAY_NAME"])
                    dokuji_scoring_rlayer = QgsRasterLayer(
                        dokuji_filepath, OUTPUT_SITEIDX_DOKUJI["DISPLAY_NAME"] + "[スコアリング]")
                    dokuji_rawdata_rlayer.loadNamedStyle(
                        raster_styler.siteidx.write_rawdata_qml(dokuji_filepath, wood_type="dokuji"))
                    dokuji_scoring_rlayer.loadNamedStyle(
                        raster_styler.siteidx.write_scoring_qml(dokuji_filepath))
                    output_rlayers_dict[OUTPUT_SITEIDX_DOKUJI["DISPLAY_NAME"]] = [
                        dokuji_rawdata_rlayer, dokuji_scoring_rlayer]

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

                if self.custom_shc_enabled:
                    from .raster_writer import custom_shc
                    self.postMessage.emit(f'{OUTPUT_SHC_CUSTOM["DISPLAY_NAME"]}を計算中')
                    self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                    self.addProgress.emit(1)
                    progress_counter += 1
                    custom_path = custom_shc.generate(
                        self.input_files_dict["dem"], self.output_dir,
                        target_dem_filepath=dem_for_processes, feedback=self.feedback)
                    custom_path = self._clip_final_output(custom_path)
                    filename_stem = OUTPUT_SHC_CUSTOM["FILE_NAME"]
                    raw_qml = raster_styler.shc.write_rawdata_qml(
                        custom_path, self.output_dir, filename_stem)
                    score_qml = raster_styler.shc.write_scoring_qml(
                        custom_path, self.output_dir, filename_stem)
                    raw_layer = QgsRasterLayer(custom_path, OUTPUT_SHC_CUSTOM["DISPLAY_NAME"])
                    score_layer = QgsRasterLayer(
                        custom_path, OUTPUT_SHC_CUSTOM["DISPLAY_NAME"] + "[スコアリング]")
                    raw_layer.loadNamedStyle(raw_qml)
                    score_layer.loadNamedStyle(score_qml)
                    output_rlayers_dict[OUTPUT_SHC_CUSTOM["DISPLAY_NAME"]] = [raw_layer, score_layer]
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

                savearea_filepath, savearea_zero_reason = raster_writer.savearea.generate(
                    dem_for_processes,
                    self.input_files_dict["building"],
                    self.output_dir,
                    feedback=self.feedback,
                )
                self.no_overlap_savearea_created = savearea_zero_reason == "no_overlap"
                if savearea_zero_reason == "no_building":
                    self.no_building_savearea_created = True
                    self.postMessage.emit(
                        f'{OUTPUT_SAVEAREA["DISPLAY_NAME"]}: 建物が無いため全域を保全対象なしとして作成しました')
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

                if self.custom_shc_enabled:
                    if self.abort_flag:
                        self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
                        return
                    # 等高線法の複雑さと組にする、流域のしきい値に10mに対する係数を掛けた保全対象を含む流域。従来のものは変えず、並べて作る
                    self.postMessage.emit(f'{OUTPUT_SAVEAREA_CUSTOM["DISPLAY_NAME"]}を計算中')
                    self.setAbortable.emit(sum_of_processes - progress_counter > 1)
                    self.addProgress.emit(1)
                    progress_counter += 1
                    custom_savearea_filepath, _unused = raster_writer.savearea.generate(
                        dem_for_processes,
                        self.input_files_dict["building"],
                        self.output_dir,
                        feedback=self.feedback,
                        area_scaled_threshold=True,
                        output_file_name=OUTPUT_SAVEAREA_CUSTOM["FILE_NAME"],
                    )
                    custom_savearea_filepath = self._clip_final_output(custom_savearea_filepath)
                    custom_stem = OUTPUT_SAVEAREA_CUSTOM["FILE_NAME"]
                    custom_raw_qml = raster_styler.savearea.write_rawdata_qml(self.output_dir, custom_stem)
                    custom_score_qml = raster_styler.savearea.write_scoring_qml(
                        self.output_dir, custom_stem, bonus=SAVEAREA_CONTOUR_BONUS)
                    custom_raw_layer = QgsRasterLayer(custom_savearea_filepath, OUTPUT_SAVEAREA_CUSTOM["DISPLAY_NAME"])
                    custom_score_layer = QgsRasterLayer(
                        custom_savearea_filepath, OUTPUT_SAVEAREA_CUSTOM["DISPLAY_NAME"] + "[スコアリング]")
                    custom_raw_layer.loadNamedStyle(custom_raw_qml)
                    custom_score_layer.loadNamedStyle(custom_score_qml)
                    output_rlayers_dict[OUTPUT_SAVEAREA_CUSTOM["DISPLAY_NAME"]] = [custom_raw_layer, custom_score_layer]
        except Exception as e:
            # エラーはまとめてキャッチして呼び出し元に報告・処理を中断
            run_status = "失敗"
            self.processFailed.emit(str(e))
            self.abort_flag = True
            self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
            return
        else:
            if not self.abort_flag:
                run_status = "完了"
        finally:
            self._write_onetime_log(run_started, run_status, dem_info)
            self._log_finished = True
            ascii_tmpdir.restore()
            shutil.rmtree(intermediate_dir, ignore_errors=True)
            remove_files(input_aliases)

        self.postMessage.emit('終了処理中')

        # 本当はここでプロジェクトにレイヤーを追加したい
        # しかし別スレッドでプロジェクトに追加されたレイヤーはUIで認識できない
        # なのでメインスレッドでレイヤーを追加するため、処理結果をメインスレッドに渡す
        self.processFinished.emit(move_output_layers_to_main_thread(output_rlayers_dict))
