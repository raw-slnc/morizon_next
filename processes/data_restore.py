# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import morizon_restore
from ..utils import move_output_layers_to_main_thread


class DataRestoreThread(QThread):
    """保存データの出力ファイルから、出力レイヤーとスタイルを作り直すスレッド。
    qmlが無い場合のスタイル作成（ラスターの統計計算を含む）は大きなDEMで時間がかかるため、ここで行う。
    レイヤーは他の処理スレッドと同じく、emitの直前にメインスレッドへ移す。プロジェクトへの追加は呼び出し側。

    結果は processFinished({"elements", "scoring", "zoning", "aggregate": {表示名: レイヤー},
    "problems": [...], "status": "done" | "cancelled"}) で返す。中断は工程（タブ）の区切りで確認する。"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, youso_dir, costcsv_path, zoning_dir, aggregate_shp, aggregate_threshold):
        super().__init__()
        self.youso_dir = youso_dir
        self.costcsv_path = costcsv_path
        self.zoning_dir = zoning_dir
        self.aggregate_shp = aggregate_shp
        self.aggregate_threshold = aggregate_threshold
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        problems = []
        result = {"elements": {}, "scoring": {}, "zoning": {}, "aggregate": {}, "problems": problems}
        steps = (
            ("elements", "要素計算の出力レイヤーを作成中",
             lambda: morizon_restore.build_element_layers(self.youso_dir, self.costcsv_path, problems)),
            ("scoring", "スコアリングの出力レイヤーを作成中",
             lambda: morizon_restore.build_scoring_layers(self.zoning_dir, problems)),
            ("zoning", "ゾーニングの出力レイヤーを作成中",
             lambda: morizon_restore.build_zoning_layers(self.zoning_dir, problems)),
            ("aggregate", "集計の出力レイヤーを作成中",
             lambda: morizon_restore.build_aggregate_layers(
                 self.aggregate_shp, self.aggregate_threshold, problems)),
        )
        try:
            self.setAbortable.emit(True)
            self.processStarted.emit(len(steps))
            for key, message, build in steps:
                if self.abort_flag:
                    break
                self.postMessage.emit(message)
                result[key] = build()
                self.addProgress.emit(1)
            result["status"] = "cancelled" if self.abort_flag else "done"
            for key in ("elements", "scoring", "zoning", "aggregate"):
                move_output_layers_to_main_thread(result[key])
            self.processFinished.emit(result)
        except Exception as e:
            for key in ("elements", "scoring", "zoning", "aggregate"):
                move_output_layers_to_main_thread(result[key])
            self.processFailed.emit(str(e))
