# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
国土地理院DEMタイルの欠け（標高の無い画素・取れなかったタイル）を分類して扱う。

地理院DEMは、海・湖や川の水面・測量範囲外のどれでも同じように欠ける（タイルが404か、画素が欠測）。
これをまとめて周囲から補間すると、海や水面に実在しない斜面ができ、測量範囲外の陸地は尾根や谷の
消えた面になる。ここでは欠けを次のように分ける。

・海／水面：地理院ベクトルタイルの水域（WA）の内側。DEMはNoDataのままにする（解析対象外）
・小さな穴：水域に接しない、ごく小さな欠け。周囲の値から補間する
・陸の欠け：上のどれでもない欠け。粗い解像度（5A→10B）の値を引き伸ばして埋める
・10Bにも値が無い欠けは海とみなす。水域の外でも、水域に接して平らな欠けは水面とみなす
  （河道の移動などで水域ポリゴンに入っていない川の水面。5Aも水面は欠測になる）

どこをどう扱ったかはマスク（MASK_* の値）で返す。QGIS API には依存しない（ワーカースレッドから呼ぶ）。

取得元を問わず、保存したDEMの水域（WA）の内側は、値があっても exclude_water() で NoData にする
（航空レーザのDTMなどは水面にも高さの値を持つため）。
"""

import math
import os

import numpy as np

from osgeo import gdal, ogr, osr

gdal.UseExceptions()

VECTOR_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf"
VECTOR_TILE_MAX_ZOOM = 16
# 水域（WA）の vt_code のうち海を表す値。湖・池・川などはそれ以外の値になる
SEA_VT_CODE = 5100

# マスクの値
MASK_VALID = 0          # 選んだ解像度の値
MASK_FILLED_5A = 1      # 陸の欠けをDEM5Aで埋めた
MASK_FILLED_10B = 2     # 陸の欠けをDEM10Bで埋めた
MASK_INTERPOLATED = 3   # 周囲の値から補間した
MASK_SEA = 10           # 海（DEMはNoData）
MASK_WATER = 11         # 湖・川などの水面（DEMはNoData）
MASK_NODATA = 255       # 座標変換で生じた範囲外

# 水域に接しない欠けのうち、この画素数以下は周囲から補間する
SMALL_HOLE_MAX_PX = 100
SMALL_HOLE_SEARCH_PX = 20
# 水域に接する欠けのうち、10Bの高低差がこれ以下なら水面とみなす
WATER_MAX_RELIEF_M = 5.0
# 陸地のうち粗い解像度で埋めた割合がこれ以上なら、その解像度での解析をすすめない（仮の値）
LAND_FILL_WARN_RATIO = 0.05

TILE_SIZE = 256
WEB_MERCATOR_LIMIT = 20037508.3428
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"}


class _Cancelled(Exception):
    pass


def _check_cancel(cancel_cb):
    if cancel_cb is not None and cancel_cb():
        raise _Cancelled()


def tile_geotransform(zoom, x, y):
    """Webメルカトルのタイル(z, x, y)1枚(256画素)のジオトランスフォーム"""
    tile_m = 2.0 * WEB_MERCATOR_LIMIT / (2 ** zoom)
    px = tile_m / TILE_SIZE
    return (-WEB_MERCATOR_LIMIT + x * tile_m, px, 0.0, WEB_MERCATOR_LIMIT - y * tile_m, 0.0, -px)


def _mercator_srs_wkt():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    return srs.ExportToWkt()


def _mem_raster(shape, gt, data_type):
    rows, cols = shape
    ds = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, data_type)
    ds.SetGeoTransform(gt)
    ds.SetProjection(_mercator_srs_wkt())
    return ds


def _tile_range_for_grid(gt, shape, zoom):
    """グリッド(gt, shape)を覆う、ズーム zoom のタイル番号の範囲 (x0, y0, x1, y1)"""
    rows, cols = shape
    tile_m = 2.0 * WEB_MERCATOR_LIMIT / (2 ** zoom)
    eps = gt[1] * 0.5
    x_min, y_max = gt[0], gt[3]
    x_max, y_min = gt[0] + cols * gt[1], gt[3] + rows * gt[5]
    x0 = int(math.floor((x_min + eps + WEB_MERCATOR_LIMIT) / tile_m))
    x1 = int(math.floor((x_max - eps + WEB_MERCATOR_LIMIT) / tile_m))
    y0 = int(math.floor((WEB_MERCATOR_LIMIT - y_max + eps) / tile_m))
    y1 = int(math.floor((WEB_MERCATOR_LIMIT - y_min - eps) / tile_m))
    return x0, y0, x1, y1


def water_codes(gt, shape, zoom, cancel_cb=None, progress=None):
    """グリッド(gt, shape)の各画素に、地理院ベクトルタイルの水域（WA）の vt_code を焼き込んだ配列を返す
    （水域外は0）。戻り値は (配列, 全タイルを取得できたか)。GDALにMVTドライバが無ければ (None, False)。"""
    import requests

    if gdal.GetDriverByName("MVT") is None:
        return None, False
    vz = min(VECTOR_TILE_MAX_ZOOM, zoom)
    x0, y0, x1, y1 = _tile_range_for_grid(gt, shape, vz)
    out = _mem_raster(shape, gt, gdal.GDT_UInt16)
    complete = True
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    done = 0
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            _check_cancel(cancel_cb)
            done += 1
            if progress is not None:
                progress("水域データを取得中", done, total)
            try:
                resp = requests.get(VECTOR_TILE_URL.format(z=vz, x=tx, y=ty), headers=_HEADERS, timeout=15)
            except Exception:
                complete = False
                continue
            if resp.status_code == 404:
                # 何も描かれていないタイル（陸の水域なし・遠い沖など）
                continue
            if resp.status_code != 200:
                complete = False
                continue
            path = f"/vsimem/morizon_next_wa_{vz}_{tx}_{ty}.pbf"
            gdal.FileFromMemBuffer(path, resp.content)
            try:
                ds = gdal.OpenEx(path, gdal.OF_VECTOR, open_options=[f"X={tx}", f"Y={ty}", f"Z={vz}"])
                layer = ds.GetLayerByName("WA") if ds is not None else None
                if layer is not None:
                    gdal.RasterizeLayer(out, [1], layer, options=["ATTRIBUTE=vt_code"])
                ds = None
            except Exception:
                complete = False
            finally:
                gdal.Unlink(path)
    return out.GetRasterBand(1).ReadAsArray(), complete


def _dilate8(mask):
    """8近傍に1画素ふくらませる"""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    rows_done = out.copy()
    out[:, 1:] |= rows_done[:, :-1]
    out[:, :-1] |= rows_done[:, 1:]
    return out


def _memory_vector_datasource():
    # GDAL 3.11 以降はラスターと同じ MEM ドライバでベクターも扱う（Memory ドライバは非推奨）
    if int(gdal.VersionInfo()) >= 3110000:
        return gdal.GetDriverByName("MEM").Create("", 0, 0, 0, gdal.GDT_Unknown)
    return ogr.GetDriverByName("Memory").CreateDataSource("")


def _label(mask):
    """8近傍でつながった True の画素のかたまりに 1 から番号を振る。(番号の配列, かたまりの数)
    つながった画素をGDALでポリゴン化し、番号を焼き戻して番号付けする。scipy は QGIS の環境によって
    入っていないことがあるため使わない（1m・約7,000万画素でも数秒で、取得全体の時間にはほぼ影響しない）"""
    rows, cols = mask.shape
    gt = (0.0, 1.0, 0.0, float(rows), 0.0, -1.0)
    driver = gdal.GetDriverByName("MEM")
    src = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    src.SetGeoTransform(gt)
    band = src.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    vds = _memory_vector_datasource()
    layer = vds.CreateLayer("components", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, ["8CONNECTED=8"])
    fids = [feature.GetFID() for feature in layer]
    for number, fid in enumerate(fids, start=1):
        feature = layer.GetFeature(fid)
        feature.SetField("v", number)
        layer.SetFeature(feature)
    dst = driver.Create("", cols, rows, 1, gdal.GDT_Int32)
    dst.SetGeoTransform(gt)
    gdal.RasterizeLayer(dst, [1], layer, options=["ATTRIBUTE=v"])
    labels = dst.GetRasterBand(1).ReadAsArray().astype(np.int32)
    labels[~mask] = 0
    return labels, len(fids)


def _touching(labels, count, seeds):
    """かたまりごとに、seeds の画素と接しているか（添字0は未使用）"""
    touching = np.zeros(count + 1, dtype=bool)
    touched = labels[_dilate8(seeds) & (labels > 0)]
    touching[np.unique(touched)] = True
    touching[0] = False
    return touching


def _fill_from_neighbors(data, max_search_px):
    """欠測(NaN)を GDAL の FillNodata で周囲から補間した配列を返す（届かない画素は NaN のまま）"""
    nodata = np.isnan(data)
    rows, cols = data.shape
    driver = gdal.GetDriverByName("MEM")
    ds = driver.Create("", cols, rows, 1, gdal.GDT_Float32)
    band = ds.GetRasterBand(1)
    work = data.astype(np.float32)
    work[nodata] = -9999.0
    band.SetNoDataValue(-9999.0)
    band.WriteArray(work)
    mask_ds = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
    mask_band = mask_ds.GetRasterBand(1)
    mask_band.WriteArray((~nodata).astype(np.uint8) * 255)
    gdal.FillNodata(band, mask_band, max_search_px, 0)
    filled = band.ReadAsArray().astype(np.float64)
    filled[filled == -9999.0] = np.nan
    return filled


def _coarse_on_grid(source, need, gt, shape, zoom, cancel_cb=None, progress=None):
    """need の画素を含む粗いDEMタイルだけを取得し、グリッド(gt, shape)へ双線形で引き伸ばす。
    戻り値は (値の配列, 取得に失敗したタイルの画素)。404のタイルは値が無い（NaN）として扱う。"""
    from .dem_loader import GSITileDEMLoader

    tile_url, coarse_zoom, label, encoding = source
    rows, cols = shape
    ny, nx = rows // TILE_SIZE, cols // TILE_SIZE
    fine_x0, fine_y0, _, _ = _tile_range_for_grid(gt, shape, zoom)
    dz = zoom - coarse_zoom
    tile_need = need.reshape(ny, TILE_SIZE, nx, TILE_SIZE).any(axis=(1, 3))
    wanted = sorted({((fine_x0 + ix) >> dz, (fine_y0 + iy) >> dz)
                     for iy, ix in zip(*np.nonzero(tile_need))})
    cx0, cy0, cx1, cy1 = _tile_range_for_grid(gt, shape, coarse_zoom)
    coarse = np.full(((cy1 - cy0 + 1) * TILE_SIZE, (cx1 - cx0 + 1) * TILE_SIZE), np.nan)
    failed_tiles = np.zeros((cy1 - cy0 + 1, cx1 - cx0 + 1), dtype=bool)
    for done, (tx, ty) in enumerate(wanted, start=1):
        _check_cancel(cancel_cb)
        if progress is not None:
            progress(f"{label}で陸の欠けを補う値を取得中", done, len(wanted))
        tile, err, _ = GSITileDEMLoader._fetch_tile_array(tile_url.format(z=coarse_zoom, x=tx, y=ty), encoding)
        iy, ix = ty - cy0, tx - cx0
        if tile is not None:
            coarse[iy * TILE_SIZE:(iy + 1) * TILE_SIZE, ix * TILE_SIZE:(ix + 1) * TILE_SIZE] = tile
        elif err and "404" not in err:
            # 404は「値が無い」（海など）。それ以外の失敗は値の有無が分からない
            failed_tiles[iy, ix] = True

    src = _mem_raster(coarse.shape, tile_geotransform(coarse_zoom, cx0, cy0), gdal.GDT_Float32)
    src_band = src.GetRasterBand(1)
    src_band.SetNoDataValue(float("nan"))
    src_band.WriteArray(coarse.astype(np.float32))
    dst = _mem_raster(shape, gt, gdal.GDT_Float32)
    dst_band = dst.GetRasterBand(1)
    dst_band.SetNoDataValue(float("nan"))
    dst_band.Fill(float("nan"))
    gdal.Warp(dst, src, resampleAlg="bilinear", srcNodata="nan", dstNodata="nan")
    values = dst_band.ReadAsArray().astype(np.float64)

    # 取得に失敗した粗いタイルが覆う画素
    failed = np.zeros(shape, dtype=bool)
    if failed_tiles.any():
        for iy, ix in zip(*np.nonzero(tile_need)):
            if failed_tiles[((fine_y0 + iy) >> dz) - cy0, ((fine_x0 + ix) >> dz) - cx0]:
                failed[iy * TILE_SIZE:(iy + 1) * TILE_SIZE, ix * TILE_SIZE:(ix + 1) * TILE_SIZE] = True
    return values, failed


def classify_and_fill(data, gt, zoom, fill_sources, base_source, cancel_cb=None, progress=None):
    """タイルを並べたDEM配列(data、欠けはNaN)の欠けを分類し、陸の欠けと小さな穴を埋める。

    fill_sources: 陸の欠けを埋めるのに使う、選んだ解像度より粗いDEM（例：1Aなら[5A]）
    base_source: 海か陸かの最終判定と最後の埋め合わせに使うDEM10B（10Bを選んだときは None）
    戻り値は (data, mask, notes)。海・水面はNaNのまま残す。中断されたら None。
    notes: {"water_data": 水域データを全部取得できたか, "fallback_interpolated": 補う値が取れず補間した画素があるか}
    """
    try:
        return _classify_and_fill(data, gt, zoom, fill_sources, base_source, cancel_cb, progress)
    except _Cancelled:
        return None


def _classify_and_fill(data, gt, zoom, fill_sources, base_source, cancel_cb, progress):
    data = data.copy()
    mask = np.zeros(data.shape, dtype=np.uint8)
    notes = {"water_data": True, "fallback_interpolated": False}
    missing = np.isnan(data)
    # 10Bは湖などの水面にも平らな値を持つため、10Bを選んだときは欠けが無くても水域を調べる
    if not missing.any() and base_source is not None:
        return data, mask, notes

    codes, complete = water_codes(gt, data.shape, zoom, cancel_cb, progress)
    if codes is None:
        codes = np.zeros(data.shape, dtype=np.uint16)
        complete = False
    notes["water_data"] = complete
    water = codes > 0
    if base_source is None:
        # 10Bの水域内の値（湖面・海岸の水際）は水面の高さで、地形ではないので対象外にする。
        # 1A/5Aの水域内の値は、保存後の exclude_water() で取得元をそろえて対象外にする
        missing |= water
        data[water] = np.nan
    mask[missing & (codes == SEA_VT_CODE)] = MASK_SEA
    mask[missing & water & (codes != SEA_VT_CODE)] = MASK_WATER
    unknown = missing & ~water

    # 水域に接しない小さな穴は、選んだ解像度の周囲の値から補間する
    if unknown.any():
        labels, count = _label(unknown)
        sizes = np.bincount(labels.ravel(), minlength=count + 1)
        small = (sizes <= SMALL_HOLE_MAX_PX) & ~_touching(labels, count, water)
        small[0] = False
        small_px = small[labels]
        del labels
        if small_px.any():
            filled = _fill_from_neighbors(data, SMALL_HOLE_SEARCH_PX)
            ok = small_px & ~np.isnan(filled)
            data[ok] = filled[ok]
            mask[ok] = MASK_INTERPOLATED
            unknown &= ~ok

    # 陸の欠けを粗い解像度で埋める（5Aは水面も欠測なので、ここで埋まるのは陸）
    for source in fill_sources:
        if not unknown.any():
            break
        values, _ = _coarse_on_grid(source, unknown, gt, data.shape, zoom, cancel_cb, progress)
        fill = unknown & ~np.isnan(values)
        data[fill] = values[fill]
        mask[fill] = MASK_FILLED_5A
        unknown &= ~fill

    if base_source is None:
        # 10Bを選んだとき：10Bの欠けで、水域でも小さな穴でもないものは海
        mask[unknown] = MASK_SEA
        unknown[:] = False
    elif unknown.any():
        values, failed = _coarse_on_grid(base_source, unknown, gt, data.shape, zoom, cancel_cb, progress)
        sea = unknown & np.isnan(values) & ~failed
        mask[sea] = MASK_SEA
        unknown &= ~sea
        candidates = unknown & ~np.isnan(values)
        if candidates.any():
            labels, count = _label(candidates)
            seeds = water | (mask == MASK_SEA)
            touching = _touching(labels, count, seeds)
            lows = np.full(count + 1, np.inf)
            highs = np.full(count + 1, -np.inf)
            np.minimum.at(lows, labels[candidates], values[candidates])
            np.maximum.at(highs, labels[candidates], values[candidates])
            is_water = touching & ((highs - lows) <= WATER_MAX_RELIEF_M)
            is_water[0] = False
            water_px = candidates & is_water[labels]
            del labels
            mask[water_px] = MASK_WATER
            land_px = candidates & ~water_px
            data[land_px] = values[land_px]
            mask[land_px] = MASK_FILLED_10B
            unknown &= ~candidates

    # 補う値も取れなかった陸の欠けは、従来どおり周囲から補間する
    if unknown.any():
        filled = _fill_from_neighbors(data, 1000)
        ok = unknown & ~np.isnan(filled)
        data[ok] = filled[ok]
        mask[ok] = MASK_INTERPOLATED
        notes["fallback_interpolated"] = True

    return data, mask, notes


def exclude_water(dem_path, cancel_cb=None, progress=None):
    """保存したDEM（取得元を問わない）のうち、地理院ベクトルタイルの水域（WA）の内側を NoData にし、
    どこを対象外にしたかをマスク（mask_path_for）に書く。マスクが既にあれば（国土地理院DEM）書き足す。

    取得元によっては湖や川の水面にも高さの値がある（航空レーザのDTMなど）。そのままでは水面が地形として
    解析されるため、どの取得元でも水面は解析対象外にそろえる。
    戻り値は {"available": 水域データを使えたか, "complete": 全タイルを取得できたか,
    "water_px": 対象外にした画素数, "total_px": 画素数, "mask_path": マスクのパス（書かなければ None）,
    "mask_write_failed": マスクを書けなかったか}。中断されたら None。"""
    try:
        return _exclude_water(dem_path, cancel_cb, progress)
    except _Cancelled:
        return None


def _exclude_water(dem_path, cancel_cb, progress):
    ds = gdal.Open(dem_path)
    gt = ds.GetGeoTransform()
    cols, rows = ds.RasterXSize, ds.RasterYSize
    wkt = ds.GetProjection()
    ds = None
    result = {"available": True, "complete": True, "water_px": 0, "total_px": cols * rows,
              "mask_path": None, "mask_write_failed": False}

    # DEMの範囲を覆う Webメルカトルのグリッド（画素の大きさはDEMの解像度にそろえる）に水域を焼き込む
    local = osr.SpatialReference()
    local.ImportFromWkt(wkt)
    mercator = osr.SpatialReference()
    mercator.ImportFromEPSG(3857)
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    for srs in (local, mercator, wgs84):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_mercator = osr.CoordinateTransformation(local, mercator)
    x_min, y_max = gt[0], gt[3]
    x_max, y_min = gt[0] + cols * gt[1], gt[3] + rows * gt[5]
    steps = 16
    xs, ys = [], []
    for i in range(steps + 1):
        t = i / steps
        x = x_min + (x_max - x_min) * t
        y = y_min + (y_max - y_min) * t
        for px, py in ((x, y_min), (x, y_max), (x_min, y), (x_max, y)):
            mx, my = to_mercator.TransformPoint(px, py)[:2]
            xs.append(mx)
            ys.append(my)
    center_lat = osr.CoordinateTransformation(local, wgs84).TransformPoint(
        (x_min + x_max) / 2, (y_min + y_max) / 2)[1]
    pixel = abs(gt[1]) / math.cos(math.radians(center_lat))
    m_cols = int(math.ceil((max(xs) - min(xs)) / pixel)) + 1
    m_rows = int(math.ceil((max(ys) - min(ys)) / pixel)) + 1
    m_gt = (min(xs), pixel, 0.0, max(ys), 0.0, -pixel)
    codes, complete = water_codes(m_gt, (m_rows, m_cols), VECTOR_TILE_MAX_ZOOM, cancel_cb, progress)
    if codes is None:
        result["available"] = False
        result["complete"] = False
        return result
    result["complete"] = complete
    _check_cancel(cancel_cb)

    src = _mem_raster((m_rows, m_cols), m_gt, gdal.GDT_UInt16)
    src.GetRasterBand(1).WriteArray(codes)
    del codes
    dst = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_UInt16)
    dst.SetGeoTransform(gt)
    dst.SetProjection(wkt)
    gdal.Warp(dst, src, resampleAlg="near")
    src = None
    local_codes = dst.GetRasterBand(1).ReadAsArray()
    dst = None

    ds = gdal.Open(dem_path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        nodata = -9999.0
        band.SetNoDataValue(nodata)
    data = band.ReadAsArray()
    valid = np.isfinite(data) & (data != nodata)
    water = (local_codes > 0) & valid
    result["water_px"] = int(water.sum())
    if result["water_px"]:
        data[water] = nodata
        band.WriteArray(data)
        band.FlushCache()
    ds = None
    del data

    mask_path = mask_path_for(dem_path)
    has_mask = os.path.exists(mask_path)
    if not result["water_px"] and not has_mask:
        return result
    try:
        if has_mask:
            mask_ds = gdal.Open(mask_path, gdal.GA_Update)
            mask_band = mask_ds.GetRasterBand(1)
            mask = mask_band.ReadAsArray()
        else:
            mask = np.full((rows, cols), MASK_VALID, dtype=np.uint8)
            mask[~valid] = MASK_NODATA
        mask[water & (local_codes == SEA_VT_CODE)] = MASK_SEA
        mask[water & (local_codes != SEA_VT_CODE)] = MASK_WATER
        if has_mask:
            mask_band.WriteArray(mask)
            mask_band.FlushCache()
            mask_ds = None
        else:
            save_mask_geotiff(mask, gt, wkt, mask_path)
        result["mask_path"] = mask_path
    except Exception:
        # 前回のマスクをQGISで開いているなど（Windowsでは上書きできない）。DEMの水面は対象外にできている
        result["mask_write_failed"] = True
    return result


def sample_land_gap(tile_array, zoom, x, y):
    """取得前の確認用。DEMタイル1枚(404なら None)について、(陸の画素数, 陸で欠けている画素数) を返す。
    水域データを取れなければ None。"""
    codes, complete = water_codes(tile_geotransform(zoom, x, y), (TILE_SIZE, TILE_SIZE), zoom)
    if codes is None or not complete:
        return None
    land = codes == 0
    missing = np.ones(land.shape, dtype=bool) if tile_array is None else np.isnan(tile_array)
    return int(land.sum()), int((missing & land).sum())


def mask_path_for(dem_path):
    """DEMの隣に置くマスクのパス"""
    return os.path.splitext(dem_path)[0] + "_mask.tif"


def save_mask_geotiff(mask, gt, crs_wkt, output_path):
    """マスクを GeoTIFF(Byte) で保存する"""
    rows, cols = mask.shape
    ds = gdal.GetDriverByName("GTiff").Create(
        output_path, cols, rows, 1, gdal.GDT_Byte, ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"]
    )
    ds.SetGeoTransform(gt)
    ds.SetProjection(crs_wkt)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(MASK_NODATA)
    band.WriteArray(mask)
    band.FlushCache()
    ds = None
    return output_path


def mask_summary(mask_path):
    """保存したマスクから、範囲に占める割合などをまとめる"""
    ds = gdal.Open(mask_path)
    mask = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return summarize_mask(mask)


def summarize_mask(mask):
    """マスクの配列から、範囲に占める割合などをまとめる"""
    inside = mask != MASK_NODATA
    total = max(int(inside.sum()), 1)
    count = {value: int((mask == value).sum()) for value in (
        MASK_FILLED_5A, MASK_FILLED_10B, MASK_INTERPOLATED, MASK_SEA, MASK_WATER)}
    land = total - count[MASK_SEA] - count[MASK_WATER]
    filled = count[MASK_FILLED_5A] + count[MASK_FILLED_10B]
    return {
        "sea": count[MASK_SEA] / total,
        "water": count[MASK_WATER] / total,
        "filled_5a": count[MASK_FILLED_5A] / total,
        "filled_10b": count[MASK_FILLED_10B] / total,
        "interpolated_px": count[MASK_INTERPOLATED],
        "land_fill_ratio": filled / land if land > 0 else 0.0,
    }
