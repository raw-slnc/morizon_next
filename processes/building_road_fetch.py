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
    processFinished = pyqtSignal(dict)  # {"building": path|None, "road": path|None}
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
                         include_layer_names=None, allow_empty_output=False):
        """基盤地図情報を取得し、描画用の DB（layer_db.py）に入れてから、作業フォルダへ互換用の shp を書き出す
        （DB → shp の順）。まとめる作業は一時フォルダの GPKG で行う（shp だと属性名が10バイトで切り詰められる）"""
        def on_progress(done, total, file_name):
            if done == 1:
                self.processStarted.emit(total + 1)
            self.addProgress.emit(1)
            self.postDetail.emit(f"{label} {done}/{total}件取得済み（{file_name}）")

        db_path = layer_db.db_file(self.db_dir, kind)
        zip_paths = fgd_fetcher.fetch_meshes(
            self.session, type_code, self.mesh_codes, self.cache_dir,
            progress_cb=on_progress, cancel_cb=lambda: self.abort_flag,
        )
        if self.abort_flag:
            raise InterruptedError()

        temp_dir = tempfile.mkdtemp(prefix=f"morizon_{kind}_")
        try:
            ok = False
            if zip_paths:
                self.postDetail.emit(f"{label} を範囲でまとめています…")
                work_path = os.path.join(temp_dir, f"{kind}.gpkg")
                ok = fgd_fetcher.merge_layers(
                    zip_paths, work_path,
                    self.lon_min, self.lat_min, self.lon_max, self.lat_max,
                    include_layer_names=include_layer_names,
                    driver_format="GPKG",
                    output_layer=kind,
                    dst_crs=self._get_dem_crs(),
                    clip_to_extent=self.clip_to_extent,
                )
                self.addProgress.emit(1)
            if not ok:
                if not allow_empty_output:
                    layer_db.clear(db_path, kind)
                    return None
                # 道路地物が無いことを後続の処理へ明示的に渡すため、空のデータを作る
                work_path = fgd_fetcher.create_empty_line_shapefile(
                    os.path.join(temp_dir, f"{kind}.shp"), self._get_dem_crs(), kind
                )
            work_layer = QgsVectorLayer(
                layer_db.layer_uri(work_path, kind) if work_path.endswith(".gpkg") else work_path, kind, "ogr"
            )
            layer_db.store(db_path, kind, work_layer)
            del work_layer
            layer_db.export_shp(db_path, kind, output_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if allow_empty_output:
            return output_path, not ok
        return output_path

    def run(self):
        try:
            self.setAbortable.emit(True)
            result = {}

            self.postMessage.emit("道路縁データを取得中…")
            road_path = os.path.join(self.output_dir, "ROAD", "road_edge.shp")
            road_result = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_ROAD_EDGE, "道路縁", road_path, layer_db.KIND_ROAD,
                include_layer_names=fgd_fetcher.ROAD_LAYERS,
                allow_empty_output=True,
            )
            if isinstance(road_result, tuple):
                result["road"], result["road_empty"] = road_result
            else:
                result["road"] = road_result
                result["road_empty"] = False
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            self.postMessage.emit("建物ポリゴンデータを取得中…")
            building_path = os.path.join(self.output_dir, "TATEMONO", "building.shp")
            result["building"] = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_BUILDING, "建物", building_path, layer_db.KIND_BUILDING,
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
