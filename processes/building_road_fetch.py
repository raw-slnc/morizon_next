# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import tempfile

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsVectorLayer

from .. import fgd_fetcher, layer_db
from ..utils import get_tiff_info


class BuildingRoadFetchThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)  # {"road": path, "road_empty": bool, "building": path, "building_empty": bool}
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, session, mesh_codes: list, cache_dir: str, output_dir: str,
                 lon_min: float, lat_min: float, lon_max: float, lat_max: float,
                 dem_filepath: str = None, clip_to_extent=True, db_dir: str = ""):
        """db_dir は描画用の DB の場所（layer_db.py）"""
        super().__init__()
        self.db_dir = db_dir
        self.session = session
        self.mesh_codes = mesh_codes
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self.lon_min = lon_min
        self.lat_min = lat_min
        self.lon_max = lon_max
        self.lat_max = lat_max
        self.dem_filepath = dem_filepath
        self.clip_to_extent = clip_to_extent
        self._dem_crs = None
        self._dem_crs_resolved = False

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def _get_dem_crs(self):
        """DEMのCRSを返す(WGS84度単位のまま保存すると、DEMの解像度(メートル)が
        度単位の範囲に誤って適用され0x0ピクセルのラスター生成に失敗するバグを踏んだため、
        取得したベクタデータはDEMのCRSに合わせて保存する)。取得できない場合はEPSG:4326"""
        if not self._dem_crs_resolved:
            self._dem_crs_resolved = True
            self._dem_crs = "EPSG:4326"
            if self.dem_filepath:
                try:
                    crs = get_tiff_info(self.dem_filepath)["crs"]
                    self._dem_crs = crs.authid() or crs.toWkt()
                except Exception:
                    pass
        return self._dem_crs

    def _fetch_and_merge(self, type_code: str, label: str, output_path: str, kind: str,
                         include_layer_names=None):
        """基盤地図情報を取得し、描画用の DB（layer_db.py）に入れてから、作業フォルダへ互換用の shp を書き出す
        （DB → shp の順）。まとめる作業は一時フォルダの GPKG で行う（shp だと属性名が10バイトで切り詰められる）。
        (shp のパス, 地物が無かったか) を返す。地物が無いときは空のデータを作る"""
        # 容量の行：データサイズ（範囲にかかるファイル全部、固定）／ダウンロード（取得済みで使い回す分も含めて
        # そろえた量）／処理済（範囲でまとめ終えたファイルの量）。どれも一覧に載っている大きさで数える
        # 工程（取得中・まとめています）は状態の行に出し、詳しい欄には件数と容量だけを出す
        status = {"files": "", "needed": None, "downloaded": 0, "processed": 0}

        def post_detail():
            lines = [status["files"]] if status["files"] else []
            if status["needed"] is not None:
                mb = 1024 * 1024
                # データサイズのうちダウンロードした量、そのうち処理済の量、の順に読めるように並べる
                lines.append(f"ダウンロード 約{status['downloaded'] / mb:.1f}MB：処理済 約{status['processed'] / mb:.1f}MB"
                             f"／データサイズ 約{status['needed'] / mb:.1f}MB")
            self.postDetail.emit("\n".join(lines))

        def on_progress(done, total, file_name):
            if done == 1:
                self.processStarted.emit(total + 1)
            self.addProgress.emit(1)
            status["files"] = f"{done}/{total}件取得済み（{file_name}）"
            post_detail()

        def on_size(needed, downloaded):
            status["needed"] = needed
            status["downloaded"] = downloaded
            post_detail()

        listed_sizes = {}

        def on_zip_done(zip_path):
            status["processed"] += listed_sizes.get(zip_path, 0)
            post_detail()

        self.postMessage.emit(f"{label}を取得中")

        db_path = layer_db.db_file(self.db_dir, kind)
        zip_paths = fgd_fetcher.fetch_meshes(
            self.session, type_code, self.mesh_codes, self.cache_dir,
            progress_cb=on_progress, cancel_cb=lambda: self.abort_flag, size_cb=on_size,
            listed_sizes=listed_sizes,
        )
        if self.abort_flag:
            raise InterruptedError()

        temp_dir = tempfile.mkdtemp(prefix=f"morizon_{kind}_")
        try:
            ok = False
            if zip_paths:
                self.postMessage.emit(f"{label}を範囲でまとめています")
                status["files"] = ""
                post_detail()
                work_path = os.path.join(temp_dir, f"{kind}.gpkg")
                ok = fgd_fetcher.merge_layers(
                    zip_paths, work_path,
                    self.lon_min, self.lat_min, self.lon_max, self.lat_max,
                    include_layer_names=include_layer_names,
                    driver_format="GPKG",
                    output_layer=kind,
                    dst_crs=self._get_dem_crs(),
                    clip_to_extent=self.clip_to_extent,
                    zip_done_cb=on_zip_done,
                )
                self.addProgress.emit(1)
            if not ok:
                # 道路・建物が無いことを後続の処理へ明示的に渡すため、空のデータを作る
                work_path = fgd_fetcher.create_empty_shapefile(
                    os.path.join(temp_dir, f"{kind}.shp"), self._get_dem_crs(), kind,
                    polygon=(kind == layer_db.KIND_BUILDING),
                )
            work_layer = QgsVectorLayer(
                layer_db.layer_uri(work_path, kind) if work_path.endswith(".gpkg") else work_path, kind, "ogr"
            )
            layer_db.store(db_path, kind, work_layer)
            del work_layer
            layer_db.export_shp(db_path, kind, output_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return output_path, not ok

    def run(self):
        try:
            self.setAbortable.emit(True)
            result = {}

            road_path = os.path.join(self.output_dir, "ROAD", "road_edge.shp")
            result["road"], result["road_empty"] = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_ROAD_EDGE, "道路縁データ", road_path, layer_db.KIND_ROAD,
                include_layer_names=fgd_fetcher.ROAD_LAYERS,
            )
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            building_path = os.path.join(self.output_dir, "TATEMONO", "building.shp")
            result["building"], result["building_empty"] = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_BUILDING, "建物ポリゴンデータ", building_path, layer_db.KIND_BUILDING,
                include_layer_names=fgd_fetcher.BUILDING_LAYERS,
            )
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            self.processFinished.emit(result)
        except InterruptedError:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))
