from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *
import processing

from ..utils import is_resampling_needed, get_tiff_info, move_output_layers_to_main_thread
from . import raster_writer
from . import raster_styler
from ..constants import OUTPUT_AGGREGATE



class _StepFeedback(QgsProcessingFeedback):
    """処理ツールの進捗（工程内の%）を、全体の進捗バー（0〜100）と詳細表示に中継する"""

    def __init__(self, thread):
        super().__init__()
        self._thread = thread
        self._start = 0.0
        self._weight = 0.0
        self._reported = 0
        self.progressChanged.connect(self._on_progress)

    def begin(self, start, weight, message):
        self._start, self._weight = start, weight
        self._thread.postMessage.emit(message)
        self._thread.postDetail.emit("")
        self._on_progress(0)

    def _on_progress(self, percent):
        overall = int(self._start + self._weight * percent / 100.0)
        if overall > self._reported:
            self._thread.addProgress.emit(overall - self._reported)
            self._reported = overall
        self._thread.postDetail.emit(f"{percent:.0f}%")


class ProcessingThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, mode: str, zoning_layer_path: str, input_layer, output_path: str,
                 style_threshold: int):
        super().__init__()
        self.mode = mode
        self.zoning_layer_path = zoning_layer_path
        self.input_layer = input_layer
        self.output_path = output_path
        self.style_threshold = style_threshold

        self.abort_flag = False
        self.feedback = None
        self.summary = ""

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag
        if flag and self.feedback is not None:
            self.feedback.cancel()

    def _check_abort(self):
        if self.abort_flag or (self.feedback is not None and self.feedback.isCanceled()):
            raise QgsProcessingException("処理を中断しました。")

    def run(self):
        vlayer_dict = {}
        self.feedback = _StepFeedback(self)
        context = QgsProcessingContext()
        zoning_layer = self.zoning_layer_path

        try:
            # 工程ごとの進捗バー上の割合（合計100）。進捗は工程の中の%まで表示する
            self.processStarted.emit(100)
            self.setAbortable.emit(True)

            if self.mode == "polygon":
                # ゾーニング図の範囲に重なるポリゴンだけを集計する（範囲外の数万件を照合しない）
                self.feedback.begin(0, 10, "ゾーニング図の範囲のポリゴンを抽出中")
                polygon_vlayer = raster_writer.aggregate.extract_overlapping(
                    self.input_layer, zoning_layer, context, self.feedback)
                self._check_abort()
                if polygon_vlayer.featureCount() == 0:
                    raise RuntimeError("ゾーニング図の範囲に重なるポリゴンがありません。")
                self.feedback.begin(10, 10, f"ジオメトリを修復中（{polygon_vlayer.featureCount()}件）")
                polygon_vlayer = fix_geometry(polygon_vlayer, context, self.feedback)
                self._check_abort()
                polygon_vlayer = raster_writer.aggregate.to_raster_crs(
                    polygon_vlayer, zoning_layer, context, self.feedback)
            else:
                # DEMから流域ポリゴンを生成して集計する場合（解析範囲の中だけなので抽出しない）
                self.feedback.begin(0, 20, "流域ポリゴンを作成中")
                is_resampling = is_resampling_needed(get_tiff_info(self.input_layer))
                if is_resampling:
                    self.input_layer = raster_writer.resampling(self.input_layer, 10)
                basin_polygon = raster_writer.savearea.create_basin_polygon(self.input_layer)
                polygon_vlayer = raster_writer.savearea.dissolve_basin_vlayer(basin_polygon)
            self._check_abort()

            self.feedback.begin(20, 65, f"区分ごとのセル数を集計中（{polygon_vlayer.featureCount()}件）")
            aggregate_filepath = raster_writer.aggregate.zonal_histogram(
                zoning_layer, polygon_vlayer, self.output_path, context, self.feedback)
            self._check_abort()

            self.feedback.begin(85, 15, "統計と区分ごとの割合を計算中")
            kept, removed = raster_writer.aggregate.add_statistics(aggregate_filepath, self.feedback)
            self._check_abort()
            self.summary = f"集計したポリゴン：{kept}件"
            if removed:
                self.summary += f"（ゾーニング図のデータが無い {removed}件 は除外）"

            # スタイルを適用する
            self.feedback.begin(100, 0, "終了処理中")
            vlayer = QgsVectorLayer(aggregate_filepath, OUTPUT_AGGREGATE["DISPLAY_NAME"])
            qml_filepath = raster_styler.aggregate.write_qml(self.output_path, self.style_threshold)
            vlayer.loadNamedStyle(qml_filepath)
            vlayer_dict[OUTPUT_AGGREGATE["DISPLAY_NAME"]] = vlayer
        except Exception as e:
            # エラーはまとめてキャッチして呼び出し元に報告・処理を中断
            self.abort_flag = True
            self.processFailed.emit(str(e))
            self.processFinished.emit(move_output_layers_to_main_thread(vlayer_dict))
            return

        self.processFinished.emit(move_output_layers_to_main_thread(vlayer_dict))


def fix_geometry(polygon_layer: QgsVectorLayer, context=None, feedback=None):
    """任意ポリゴンによる集計をする場合、事前にジオメトリ修復を行う関数"""
    return processing.run("native:fixgeometries", {'INPUT': polygon_layer, 'OUTPUT': 'memory:'},
                          context=context, feedback=feedback)['OUTPUT']
