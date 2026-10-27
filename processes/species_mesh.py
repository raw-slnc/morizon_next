# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""ゾーン統計量の「現在地の樹種メッシュ」：林野庁「全国森林資源メッシュ」（20m）を、ゾーニング図の範囲で取得して
1つの GeoPackage にまとめる。ゾーニングは ZoningKit の地位のまま（原版の手順のまま）進め、最後にこのメッシュで
集計して、針葉樹フィルターで樹種を絞って見るために使う。

樹種の列「樹種」は、航空レーザの判定（レーザ解析樹種）があればそれを、無ければ森林簿の樹種（森林簿樹種1）を入れる。
"""

import os
import shutil
import tempfile

from qgis.PyQt.QtCore import QThread, pyqtSignal

LAYER_NAME = "樹種メッシュ"
# 出力レイヤーの印の区別。工程はゾーニング図と同じにする（タブの移動や操作での表示の排他を、ゾーニング図と同じに扱う）
OUTPUT_KEY = "species_mesh"
SPECIES_FIELD = "樹種"
KEEP_FIELDS = ("林種", "森林簿樹種1", "レーザ解析樹種", "林齢", "樹冠高")
# 針葉樹フィルターで「針葉樹」とみなす値の初期値（森林簿の樹種・レーザ解析樹種の書き方。その他N は針葉樹）
CONIFER_VALUES = ("スギ", "ヒノキ類", "カラマツ", "マツ類", "その他針葉樹", "その他N")


class SpeciesMeshThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, reference_raster, cache_base_dir, output_path):
        """reference_raster：範囲と座標系を決めるラスター（ゾーニング図）"""
        super().__init__()
        self.reference_raster = reference_raster
        self.cache_base_dir = cache_base_dir
        self.output_path = output_path
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.processStarted.emit(3)
            self.setAbortable.emit(True)
            result = self._run()
            self.processFinished.emit(result or {})
        except Exception as e:  # noqa: BLE001  画面に理由を出すため、まとめて受ける
            self.processFailed.emit(str(e))

    def _run(self):
        from osgeo import gdal, ogr, osr
        from .. import national_mesh_fetcher
        gdal.UseExceptions()
        raster = gdal.Open(self.reference_raster)
        if raster is None:
            raise RuntimeError("ゾーニング図を読み込めませんでした。")
        transform = raster.GetGeoTransform()
        wkt = raster.GetProjection()
        bounds = (transform[0], transform[3] + raster.RasterYSize * transform[5],
                  transform[0] + raster.RasterXSize * transform[1], transform[3])
        raster = None
        srs = osr.SpatialReference(wkt=wkt)
        srs.AutoIdentifyEPSG()
        code = srs.GetAuthorityCode(None)
        zone = national_mesh_fetcher.zone_from_epsg(int(code)) if code and code.isdigit() else None
        if zone is None:
            raise RuntimeError("ゾーニング図が平面直角座標系ではないため、全国森林資源メッシュの系を決められません。")

        self.postMessage.emit("全国森林資源メッシュを取得中")
        self.addProgress.emit(1)

        def progress(done, total):
            self.postDetail.emit(f"{done / total * 100:.0f}%" if total else f"{done / 1048576:.0f} MB")
        paths = national_mesh_fetcher.fetch_tiles(
            zone, bounds, self.cache_base_dir, message_cb=self.postDetail.emit, progress_cb=progress,
            cancel_cb=lambda: self.abort_flag)
        if paths is None:
            return None
        self.postDetail.emit("")

        self.postMessage.emit("ゾーニング図の範囲を切り出し中")
        self.addProgress.emit(1)
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        if os.path.exists(self.output_path):
            os.remove(self.output_path)
        # 7z の中を範囲で絞って読むと非常に遅いので、いったん一時フォルダに取り出してから使う
        temp_dir = tempfile.mkdtemp(prefix="morizon_next_mesh_")
        try:
            for index, path in enumerate(paths):
                plain = os.path.join(temp_dir, f"mesh_{index}.gpkg")
                gdal.CopyFile(path, plain)
                source_layer = os.path.basename(path)[:-len(".gpkg")]
                # 列を選んで切り出すのと、既存のファイルへの追加は同時にできないので、2図郭目からは一度別のファイルに切り出す
                target = self.output_path if index == 0 else os.path.join(temp_dir, f"part_{index}.gpkg")
                gdal.VectorTranslate(
                    target, plain, format="GPKG", layers=[source_layer], layerName=LAYER_NAME,
                    dstSRS=wkt, spatFilter=bounds, spatSRS=wkt, selectFields=list(KEEP_FIELDS),
                    geometryType="PROMOTE_TO_MULTI")
                if index:
                    gdal.VectorTranslate(self.output_path, target, format="GPKG", layers=[LAYER_NAME],
                                         layerName=LAYER_NAME, accessMode="append")
                if self.abort_flag:
                    return None
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.postMessage.emit("樹種の列を作成中")
        self.addProgress.emit(1)
        dataset = ogr.Open(self.output_path, 1)
        layer = dataset.GetLayerByName(LAYER_NAME)
        layer.CreateField(ogr.FieldDefn(SPECIES_FIELD, ogr.OFTString))
        total = with_species = 0
        layer.StartTransaction()
        for feature in layer:
            laser = (feature.GetField("レーザ解析樹種") or "").strip()
            register = (feature.GetField("森林簿樹種1") or "").strip()
            species = laser or register
            total += 1
            if species:
                with_species += 1
                feature.SetField(SPECIES_FIELD, species)
                layer.SetFeature(feature)
        layer.CommitTransaction()
        dataset = None
        if total == 0:
            raise RuntimeError("ゾーニング図の範囲に、全国森林資源メッシュのマスがありませんでした。")
        return {"path": self.output_path, "layer": LAYER_NAME, "total": total, "with_species": with_species}
