# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import platform
import shutil
import tempfile
import time

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog, QgsVectorLayer

from .. import fgd_fetcher, layer_db
from ..utils import get_ascii_safe_dir, get_tiff_info


# 作業フォルダ（DATA）の中の、取得した道路・建物の shp の場所
ROAD_SHP = os.path.join("ROAD", "road_edge.shp")
BUILDING_SHP = os.path.join("TATEMONO", "building.shp")


def fetch_area_key(lon_min, lat_min, lon_max, lat_max, dem_crs: str) -> str:
    """取得の範囲と保存する座標系を表す文字列。前回の取得と同じ範囲かを見分けるのに使う"""
    return ",".join(f"{value:.7f}" for value in (lon_min, lat_min, lon_max, lat_max)) + f"|{dem_crs}"


def dem_crs_of(dem_filepath: str) -> str:
    """取得したデータを保存する座標系（DEM の座標系）。読めなければ EPSG:4326"""
    if dem_filepath:
        try:
            crs = get_tiff_info(dem_filepath)["crs"]
            return crs.authid() or crs.toWkt()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"DEMの座標系を読めないため、建物・道路を EPSG:4326 で保存します（{e}）",
                "Morizon Next", Qgis.MessageLevel.Warning,
            )
    return "EPSG:4326"


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
                 dem_filepath: str = None, clip_to_extent=True, db_dir: str = "", keep_kinds=()):
        """db_dir は描画用の DB の場所（layer_db.py）。keep_kinds に入れた種類（layer_db.KIND_*）は取得せず、
        作業フォルダの編集した shp をそのまま使う（取得の前に利用者が選ぶ）"""
        super().__init__()
        self.keep_kinds = set(keep_kinds)
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
            self._dem_crs = dem_crs_of(self.dem_filepath)
        return self._dem_crs

    def _fetch_and_merge(self, type_code: str, label: str, output_path: str, kind: str,
                         include_layer_names=None):
        """基盤地図情報を取得し、描画用の DB（layer_db.py）に入れてから、作業フォルダへ互換用の shp を書き出す
        （DB → shp の順）。まとめる作業は一時フォルダの GPKG で行う（shp だと属性名が10バイトで切り詰められる）。
        (shp のパス, 地物が無かったか) を返す。地物が無いときは空のデータを作る"""
        # 容量の行：データサイズ（範囲にかかるファイル全部、固定）／ダウンロード（取得済みで使い回す分も含めて
        # そろえた量）／処理済（範囲でまとめ終えたファイルの量）。どれも一覧に載っている大きさで数える
        # 工程（取得中・まとめています）は状態の行に出し、詳しい欄には容量の行だけを出す
        status = {"needed": None, "downloaded": 0, "processed": 0}

        def post_detail():
            if status["needed"] is None:
                return
            mb = 1024 * 1024
            # データサイズのうちダウンロードした量、そのうち処理済の量、の順に読めるように並べる
            self.postDetail.emit(f"ダウンロード 約{status['downloaded'] / mb:.1f}MB：処理済 約{status['processed'] / mb:.1f}MB"
                                 f"／データサイズ 約{status['needed'] / mb:.1f}MB")

        def on_progress(done, total, _file_name):
            if done == 1:
                self.processStarted.emit(total + 1)
            self.addProgress.emit(1)

        def on_size(needed, downloaded):
            status["needed"] = needed
            status["downloaded"] = downloaded
            post_detail()

        listed_sizes = {}

        def on_zip_done(zip_path):
            status["processed"] += listed_sizes.get(zip_path, 0)
            post_detail()

        self.postMessage.emit(f"{label}を取得中")

        # 段階ごとにかかった時間を、ログメッセージパネルに出す（OS による差と PC の性能差を見分けるため。
        # どの段階も同じ比率で遅ければ性能差、特定の段階だけ遅ければ OS の差と見る）
        times = {}
        started = time.monotonic()

        db_path = layer_db.db_file(self.db_dir, kind)
        if kind in self.keep_kinds:
            # 編集した shp をそのまま使う（取得しない）。DB も shp に合わせる
            layer_db.import_saved(db_path, kind, output_path)
            QgsMessageLog.logMessage(
                f"{label}：編集したデータを使うため、取得しませんでした", "Morizon Next", Qgis.MessageLevel.Info
            )
            return output_path, QgsVectorLayer(output_path, kind, "ogr").featureCount() == 0

        zip_paths = fgd_fetcher.fetch_meshes(
            self.session, type_code, self.mesh_codes, self.cache_dir,
            progress_cb=on_progress, cancel_cb=lambda: self.abort_flag, size_cb=on_size,
            listed_sizes=listed_sizes,
        )
        if self.abort_flag:
            raise InterruptedError()
        times["download"] = time.monotonic() - started

        # 前回と同じ条件（範囲・座標系・使った ZIP）で取得していて、作業フォルダの shp もそのときのままなら、
        # まとめ直しても同じ結果になるので、そのまま使う
        area = fetch_area_key(self.lon_min, self.lat_min, self.lon_max, self.lat_max, self._get_dem_crs())
        zips = ",".join(sorted(os.path.basename(path) for path in zip_paths))
        record = layer_db.get_fetch_record(db_path, kind)
        if (record and record[0] == area and record[1] == zips and os.path.isfile(output_path)
                and layer_db.has_layer(db_path, kind) and layer_db.shp_fingerprint(output_path) == record[2]):
            self.addProgress.emit(1)
            status["processed"] = status["needed"] or 0
            post_detail()
            QgsMessageLog.logMessage(
                f"{label}：前回と同じ条件で取得済みのため、まとめ直しを省きました"
                f"（一覧の取得とダウンロード {times['download']:.1f}秒）",
                "Morizon Next", Qgis.MessageLevel.Info,
            )
            return output_path, QgsVectorLayer(output_path, kind, "ogr").featureCount() == 0

        # まとめる作業は半角の一時フォルダで行う（利用者の一時フォルダは、ユーザー名の全角文字を含み得るため）
        temp_dir = tempfile.mkdtemp(prefix=f"morizon_{kind}_", dir=get_ascii_safe_dir("morizon_next_tmp"))
        try:
            ok = False
            step = time.monotonic()
            if zip_paths:
                self.postMessage.emit(f"{label}を範囲でまとめています")
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
            times["merge"] = time.monotonic() - step
            if not ok:
                # 道路・建物が無いことを後続の処理へ明示的に渡すため、空のデータを作る
                work_path = fgd_fetcher.create_empty_shapefile(
                    os.path.join(temp_dir, f"{kind}.shp"), self._get_dem_crs(), kind,
                    polygon=(kind == layer_db.KIND_BUILDING),
                )
            work_layer = QgsVectorLayer(
                layer_db.layer_uri(work_path, kind) if work_path.endswith(".gpkg") else work_path, kind, "ogr"
            )
            step = time.monotonic()
            layer_db.store(db_path, kind, work_layer)
            del work_layer
            times["store"] = time.monotonic() - step
            step = time.monotonic()
            layer_db.export_shp(db_path, kind, output_path)
            times["export"] = time.monotonic() - step
            layer_db.set_fetch_record(db_path, kind, area, zips, layer_db.shp_fingerprint(output_path))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self._log_times(label, kind, len(zip_paths), output_path, times, time.monotonic() - started)
        return output_path, not ok

    @staticmethod
    def _log_times(label, kind, zip_count, output_path, times, total):
        converter = "ogr2ogr" if shutil.which("ogr2ogr") else "GDAL (in QGIS)"
        features = QgsVectorLayer(output_path, kind, "ogr").featureCount()
        QgsMessageLog.logMessage(
            f"{label}の取得時間（{platform.system()}）：合計 {total:.1f}秒 ＝ "
            f"一覧の取得とダウンロード {times.get('download', 0):.1f}秒、"
            f"範囲でまとめる {times.get('merge', 0):.1f}秒（{converter}、ZIP {zip_count}件）、"
            f"DBへ写す {times.get('store', 0):.1f}秒、shpへ書き出す {times.get('export', 0):.1f}秒"
            f"（{features}件）",
            "Morizon Next", Qgis.MessageLevel.Info,
        )

    def run(self):
        try:
            self.setAbortable.emit(True)
            result = {}

            road_path = os.path.join(self.output_dir, ROAD_SHP)
            result["road"], result["road_empty"] = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_ROAD_EDGE, "道路縁データ", road_path, layer_db.KIND_ROAD,
                include_layer_names=fgd_fetcher.ROAD_LAYERS,
            )
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            building_path = os.path.join(self.output_dir, BUILDING_SHP)
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
