# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import layer_db, morizon_restore
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

    def __init__(self, youso_dir, costcsv_path, zoning_dir, aggregate_shp, aggregate_threshold,
                 db_dir, road_shp=None, building_shp=None):
        """db_dir は描画用の DB の場所（layer_db.py）。道路・建物は DB を作り直すだけで、レイヤーは作らない"""
        super().__init__()
        self.youso_dir = youso_dir
        self.costcsv_path = costcsv_path
        self.zoning_dir = zoning_dir
        self.aggregate_shp = aggregate_shp
        self.aggregate_threshold = aggregate_threshold
        self.db_dir = db_dir
        self.road_shp = road_shp
        self.building_shp = building_shp
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
                 self.aggregate_shp, layer_db.db_file(self.db_dir, layer_db.KIND_AGGREGATE),
                 self.aggregate_threshold, problems)),
            ("road", "道路縁のデータを取り込み中",
             lambda: self._import_to_db(layer_db.KIND_ROAD, self.road_shp, "道路縁", problems)),
            ("building", "建物ポリゴンのデータを取り込み中",
             lambda: self._import_to_db(layer_db.KIND_BUILDING, self.building_shp, "建物ポリゴン", problems)),
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

    def _import_to_db(self, kind, shp_path, label, problems):
        """道路・建物の入力（保存データの shp と、あれば同じ名前の GPKG）を描画用の DB に取り込む"""
        try:
            layer_db.import_saved(layer_db.db_file(self.db_dir, kind), kind, shp_path)
        except Exception as e:
            problems.append(f"{label}：データを取り込めませんでした（{e}）")
        return None
