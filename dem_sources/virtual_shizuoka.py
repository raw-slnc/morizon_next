# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
静岡県「VIRTUAL SHIZUOKA」航空レーザ測量 LP/Grid（0.5m DTM）
（forestry_operations_lite の vs_lp.py のうち DTM 取得部分を移植。点群・DSMは扱わない。
FOL本体とは実行時の依存を持たない）

配信: https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com/{年度}/LP/Grid/08/{フォルダ}/{XX}/{コード}.zip
    1タイル=1ZIP。中身は GeoTIFF か XYZ テキスト（年度により異なる）。

タイルコード "08{フォルダ2文字}{XX:02d}{AA:02d}"（EPSG:6676、平面直角第8系）:
    フォルダ原点 x0 = (フォルダ2文字目 - 'E')×40000、y0 = -(フォルダ1文字目 - 'M' + 2)×30000
    XX: 4km×3km ブロック（十の位=行、一の位=列）
    AA: 方式A 400m×300m の単純行列（2019〜2021, 2025）
        方式B 1000m×750m の2階層Z曲線（2022年、LD/MD フォルダで確認）
    同じコードでも方式が違えば別の場所を指す。方式は年度で決まる（YEAR_SCHEME）。
    YEAR_SCHEME に無い新しい年度は両方式の候補を作り、取得したファイル自身の位置で範囲と重なるかを確かめる。

取得の流れ（整備範囲の索引は無い）:
    1. 配信サーバの年度を一覧し、新しい年度から順に、まだ値の無い場所に掛かるタイルだけを取得して重ねる
       （新しい年度が上。新しい年度のタイルの欠けは古い年度で埋まる）
    2. それでも値の無い陸地は、国土地理院 DEM（1m → 5m → 10m）で補う。海・水面は NoData のまま
       （地理院の水域で判定。どこを何で補ったかはマスクに残す）
    3. 通信の失敗は「タイルが無い」と区別し、取り直しても失敗したら知らせて止める
"""

import math
import os
import re
import shutil
import tempfile
import time
import zipfile

import requests

from .base import DemSource, Cancelled, check_cancel, extent_in_epsg, format_info, NODATA

EPSG = 6676
RESOLUTION = 1  # 0.5m配布。解析側の解像度判定（1/5/10m）に合わせて1mで保存する
BUCKET_URL = "https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com"
TILE_W = 400
TILE_H = 300
# 年度ごとのタイルコードの方式（実タイルで確かめたもの）。ここに無い年度は両方式の候補を作る
YEAR_SCHEME = {2019: "A", 2020: "A", 2021: "A", 2022: "B", 2025: "A"}
# 年度の一覧が取れないとき（一覧の機能が閉じられたときなど）に使う年度
FALLBACK_YEARS = (2025, 2022, 2021, 2020, 2019)
SHIZUOKA_BBOX_WGS84 = (137.47410694, 34.57213583, 139.17655861, 35.64595651)
# 配信のタイルは県境の外まではみ出している（湖西の西で確認）。選べるかの判定は県の長方形をこれだけ広げて行う
SELECT_MARGIN_DEG = 0.05
# 通信の取り直し（回数と、回ごとの待ち秒）
RETRY_WAITS = (1, 2, 4)

_KEY_PATTERN = re.compile(r"<Key>([^<]+)</Key>")
_YEAR_PREFIX_PATTERN = re.compile(r"<Prefix>(\d{4})/</Prefix>")


class NetworkError(RuntimeError):
    """配信サーバとの通信が、取り直しても失敗した（タイルが無いのとは別）"""


# ── タイル座標 ───────────────────────────────────────────────────────

def _folder_origin(folder):
    return (ord(folder[1]) - ord("E")) * 40000, -(ord(folder[0]) - ord("M") + 2) * 30000


def tile_bbox(code, scheme="A"):
    """タイルコード → (xmin, ymin, xmax, ymax)。方式Bで符号として成り立たないコードは None"""
    x0, y0 = _folder_origin(code[2:4])
    xx, aa = int(code[4:6]), int(code[6:8])
    bx, by = x0 + (xx % 10) * 4000, y0 - (xx // 10) * 3000
    if scheme == "A":
        left, top = bx + (aa % 10) * TILE_W, by - (aa // 10) * TILE_H
        return left, top - TILE_H, left + TILE_W, top
    t, u = aa // 10, aa % 10
    if not (1 <= t <= 4 and 1 <= u <= 4):
        return None
    gr = ((t - 1) // 2) * 2 + (u - 1) // 2
    gc = ((t - 1) % 2) * 2 + (u - 1) % 2
    left, top = bx + gc * 1000, by - gr * 750
    return left, top - 750, left + 1000, top


def candidate_tiles(xmin, ymin, xmax, ymax, scheme):
    """EPSG:6676 の範囲に重なる、その方式のタイルコード → 範囲（実在は未確認）"""
    tiles = {}
    for c1 in "LMNOP":
        for c2 in "BCDEF":
            folder = c1 + c2
            x0, y0 = _folder_origin(folder)
            if xmax < x0 or xmin > x0 + 40000 or ymax < y0 - 30000 or ymin > y0:
                continue
            for yr in range(max(0, int((y0 - ymax) / 3000)), min(9, int((y0 - ymin) / 3000)) + 1):
                for xc in range(max(0, int((xmin - x0) / 4000)), min(9, int((xmax - x0) / 4000)) + 1):
                    xx = yr * 10 + xc
                    if scheme == "A":
                        codes = [f"08{folder}{xx:02d}{ar * 10 + ac:02d}" for ar in range(10) for ac in range(10)]
                    else:
                        codes = [f"08{folder}{xx:02d}{t * 10 + u:02d}" for t in range(1, 5) for u in range(1, 5)]
                    for code in codes:
                        box = tile_bbox(code, scheme)
                        if box and box[2] > xmin and box[0] < xmax and box[3] > ymin and box[1] < ymax:
                            tiles[code] = box
    return tiles


# ── 通信 ─────────────────────────────────────────────────────────────

def _request(method, url, cancel_cb=None, **kwargs):
    """取り直し付きの通信。応答（404 なども含む）を返す。通信できない・サーバの不調（5xx）が続いたら NetworkError"""
    last = None
    for wait in (0,) + RETRY_WAITS:
        if wait:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                check_cancel(cancel_cb)
                time.sleep(0.1)
        check_cancel(cancel_cb)
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last = str(e)
            continue
        if resp.status_code >= 500:
            last = f"HTTP {resp.status_code}"
            resp.close()
            continue
        return resp
    raise NetworkError(
        "VIRTUAL SHIZUOKA の配信サーバと通信できませんでした（取り直しても失敗しました）。"
        f"時間をおいてもう一度お試しください。\n（{last}）"
    )


def available_years(cancel_cb=None):
    """配信サーバにある年度（新しい順）。一覧の機能が使えなければ FALLBACK_YEARS"""
    resp = _request("GET", f"{BUCKET_URL}/?list-type=2&delimiter=/", cancel_cb, timeout=15)
    if resp.status_code != 200:
        return list(FALLBACK_YEARS)
    years = sorted({int(y) for y in _YEAR_PREFIX_PATTERN.findall(resp.text)}, reverse=True)
    return years or list(FALLBACK_YEARS)


def _tile_url(year, code):
    return f"{BUCKET_URL}/{year}/LP/Grid/08/{code[2:4]}/{code[4:6]}/{code}.zip"


def _s3_list(year, folder, xx, cancel_cb=None):
    """指定ディレクトリにあるタイルコードの集合。一覧の機能が使えなければ None。
    応答は S3 ListObjectsV2 の XML で、必要なのは <Key> だけなので正規表現で取り出す（XMLパーサを使わない）。
    1ブロックのタイルは最大100枚で、一覧の上限（1000件）に収まる"""
    prefix = f"{year}/LP/Grid/08/{folder}/{xx}/"
    resp = _request("GET", f"{BUCKET_URL}/?list-type=2&prefix={prefix}&delimiter=/", cancel_cb, timeout=15)
    if resp.status_code != 200:
        return None
    body = resp.content.decode("utf-8", errors="replace")
    return {key.rstrip("/").split("/")[-1][:-4] for key in _KEY_PATTERN.findall(body) if key.endswith(".zip")}


def _s3_exists(year, code, cancel_cb=None):
    """一覧が使えない場合の個別確認（HEAD）。404・403 は「無い」"""
    resp = _request("HEAD", _tile_url(year, code), cancel_cb, timeout=15, allow_redirects=True)
    return resp.status_code == 200


def _existing_codes(year, codes, cancel_cb=None):
    """その年度に実在するコード"""
    groups = {}
    for code in codes:
        groups.setdefault((code[2:4], code[4:6]), set()).add(code)
    found = set()
    for (folder, xx), batch in groups.items():
        listed = _s3_list(year, folder, xx, cancel_cb)
        if listed is None:
            found |= {code for code in batch if _s3_exists(year, code, cancel_cb)}
        else:
            found |= batch & listed
    return found


def _download(url, dest, cancel_cb=None, progress_cb=None):
    """取り直し付きのダウンロード（途中で切れたときも最初から取り直す）"""
    last = ""
    for wait in (0,) + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        check_cancel(cancel_cb)
        try:
            with requests.get(url, timeout=600, stream=True, headers={"Accept-Encoding": "identity"}) as resp:
                if resp.status_code >= 500:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                _write_response(resp, dest, cancel_cb, progress_cb)
            return
        except requests.HTTPError:
            raise
        except requests.RequestException as e:
            last = str(e)
    raise NetworkError(
        f"VIRTUAL SHIZUOKA のタイル（{os.path.basename(url)}）を取得できませんでした（取り直しても失敗しました）。"
        f"時間をおいてもう一度お試しください。\n（{last}）"
    )


def _write_response(resp, dest, cancel_cb, progress_cb):
    with open(dest, "wb") as fh:
        total = resp.headers.get("Content-Length")
        total = int(total) if total is not None else -1
        downloaded = 0
        last_report = 0.0
        for chunk in resp.iter_content(65536):
            if cancel_cb and cancel_cb():
                raise Cancelled()
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            now = time.monotonic()
            if progress_cb and (now - last_report >= 0.15 or downloaded == total):
                last_report = now
                progress_cb(downloaded, total)


# ── ZIP の中身 → GeoTIFF ──────────────────────────────────────────────

def _xyz_to_tif(txt_path, cell_size=0.5):
    """XYZ テキストを GeoTIFF（EPSG:6676）にする。
    旧形式（2019〜2020）: スペース区切り「X Y Z」
    新形式（2021〜）    : カンマ区切り「ID,X,Y,Z[,flag]」
    形式は先頭の有効行で判定する。"""
    import numpy as np
    from osgeo import gdal, osr

    xs, ys, zs = [], [], []
    fmt = None
    with open(txt_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if fmt is None:
                fmt = "new" if "," in line else "old"
            try:
                if fmt == "new":
                    parts = line.split(",")
                    if len(parts) >= 4:
                        xs.append(float(parts[1]))
                        ys.append(float(parts[2]))
                        zs.append(float(parts[3]))
                else:
                    parts = line.split()
                    if len(parts) == 3:
                        xs.append(float(parts[0]))
                        ys.append(float(parts[1]))
                        zs.append(float(parts[2]))
            except ValueError:
                continue

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    z = np.asarray(zs, dtype=np.float32)
    xmin, xmax = float(x.min()) - cell_size / 2, float(x.max()) + cell_size / 2
    ymin, ymax = float(y.min()) - cell_size / 2, float(y.max()) + cell_size / 2
    cols = int(round((xmax - xmin) / cell_size))
    rows = int(round((ymax - ymin) / cell_size))

    grid = np.full((rows, cols), np.float32(NODATA))
    ci = np.round((x - xmin - cell_size / 2) / cell_size).astype(np.int32)
    ri = (rows - 1) - np.round((y - ymin - cell_size / 2) / cell_size).astype(np.int32)
    ok = (ci >= 0) & (ci < cols) & (ri >= 0) & (ri < rows)
    grid[ri[ok], ci[ok]] = z[ok]

    out_path = os.path.splitext(txt_path)[0] + ".tif"
    ds = gdal.GetDriverByName("GTiff").Create(
        out_path, cols, rows, 1, gdal.GDT_Float32, ["COMPRESS=LZW", "TILED=YES"]
    )
    ds.SetGeoTransform([xmin, cell_size, 0, ymax, 0, -cell_size])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(EPSG)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(grid)
    ds = None
    os.remove(txt_path)
    return out_path


def _extract_tif(zip_path, out_dir):
    """ZIP 内の TIF（同名の TFW があれば一緒に）または XYZ テキストを取り出し、GeoTIFF のパスを返す。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        tif_name = next((n for n in names if n.lower().endswith((".tif", ".tiff"))), None)
        txt_name = next((n for n in names if n.lower().endswith(".txt")), None)

        def extract(name):
            dest = os.path.join(out_dir, os.path.basename(name))
            with zf.open(name) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return dest

        if tif_name:
            tfw = os.path.splitext(tif_name)[0].lower() + ".tfw"
            for name in names:
                if name.lower() == tfw:
                    extract(name)
            return extract(tif_name)
        if txt_name:
            return _xyz_to_tif(extract(txt_name))
    raise ValueError(f"ZIPにTIF/TXTがありません: {os.path.basename(zip_path)}")


def _ensure_crs(tif_path):
    from osgeo import gdal, osr
    ds = gdal.Open(tif_path, gdal.GA_Update)
    if not ds.GetProjection():
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(EPSG)
        ds.SetProjection(srs.ExportToWkt())
    ds = None


class _Grid:
    """保存するDEMのグリッド（EPSG:6676、RESOLUTION m）"""

    def __init__(self, bbox):
        self.xmin = math.floor(bbox[0] / RESOLUTION) * RESOLUTION
        self.ymin = math.floor(bbox[1] / RESOLUTION) * RESOLUTION
        self.xmax = math.ceil(bbox[2] / RESOLUTION) * RESOLUTION
        self.ymax = math.ceil(bbox[3] / RESOLUTION) * RESOLUTION
        self.cols = int(round((self.xmax - self.xmin) / RESOLUTION))
        self.rows = int(round((self.ymax - self.ymin) / RESOLUTION))
        self.gt = (self.xmin, RESOLUTION, 0.0, self.ymax, 0.0, -RESOLUTION)

    def window(self, box):
        """範囲 box が掛かる行・列のスライス（掛からなければ None）"""
        c0 = max(0, int(math.floor((box[0] - self.xmin) / RESOLUTION)))
        c1 = min(self.cols, int(math.ceil((box[2] - self.xmin) / RESOLUTION)))
        r0 = max(0, int(math.floor((self.ymax - box[3]) / RESOLUTION)))
        r1 = min(self.rows, int(math.ceil((self.ymax - box[1]) / RESOLUTION)))
        if c0 >= c1 or r0 >= r1:
            return None
        return slice(r0, r1), slice(c0, c1)


def _raster_bbox(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    cols, rows = ds.RasterXSize, ds.RasterYSize
    ds = None
    xs = (gt[0], gt[0] + cols * gt[1])
    ys = (gt[3], gt[3] + rows * gt[5])
    return min(xs), min(ys), max(xs), max(ys)


def _mosaic(layers, grid, work_dir):
    """取得したタイル（新しい年度が先）を重ねてグリッドに載せた配列（値の無い所は NaN）。新しい年度が上"""
    import numpy as np
    from osgeo import gdal
    vrt_path = os.path.join(work_dir, "mosaic.vrt")
    # gdalbuildvrt は後に並べたものが上になる。新しい年度を上にするため、古い順に並べる
    vrt = gdal.BuildVRT(vrt_path, [path for _year, path in reversed(layers)], srcNodata=NODATA, VRTNodata=NODATA)
    if vrt is None:
        raise RuntimeError("タイルを結合できませんでした")
    vrt = None
    dst = gdal.GetDriverByName("MEM").Create("", grid.cols, grid.rows, 1, gdal.GDT_Float32)
    dst.SetGeoTransform(grid.gt)
    dst.SetProjection(_epsg_wkt())
    band = dst.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.Fill(NODATA)
    gdal.Warp(dst, vrt_path, resampleAlg="average", srcNodata=NODATA, dstNodata=NODATA)
    data = band.ReadAsArray().astype(np.float32)
    dst = None
    os.remove(vrt_path)
    data[~np.isfinite(data) | (data == NODATA)] = np.nan
    return data


def _epsg_wkt(epsg=EPSG):
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    return srs.ExportToWkt()


def _grid_extent_wgs84(grid, rows, cols, margin=50):
    """グリッドの一部（行・列のスライス）の経緯度の範囲（少し広げる）"""
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromEPSG(EPSG)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    for srs in (src, dst):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)
    x0 = grid.xmin + cols.start * RESOLUTION - margin
    x1 = grid.xmin + cols.stop * RESOLUTION + margin
    y1 = grid.ymax - rows.start * RESOLUTION + margin
    y0 = grid.ymax - rows.stop * RESOLUTION - margin
    lons, lats = [], []
    for x in (x0, x1):
        for y in (y0, y1):
            lon, lat = transform.TransformPoint(x, y)[:2]
            lons.append(lon)
            lats.append(lat)
    return min(lons), min(lats), max(lons), max(lats)


def _gsi_fill(grid, need, work_dir, cancel_cb, reporter):
    """need（値の無いセル）の範囲で国土地理院DEM（1m→5m→10m、海・水面はNaN）を取り、グリッドに載せる。
    戻り値は (値の配列, マスクの配列)。マスクは dem_gaps.MASK_*（陸の値は使った解像度、海・水面）"""
    import numpy as np
    from .. import dem_gaps
    from ..dem_loader import GSITileDEMLoader, save_as_geotiff

    rows_any = np.where(need.any(axis=1))[0]
    cols_any = np.where(need.any(axis=0))[0]
    rows = slice(int(rows_any[0]), int(rows_any[-1]) + 1)
    cols = slice(int(cols_any[0]), int(cols_any[-1]) + 1)
    lon_min, lat_min, lon_max, lat_max = _grid_extent_wgs84(grid, rows, cols)

    current = {"message": None}

    def on_stage(message, done, total):
        if message != current["message"]:
            current["message"] = message
            reporter.start(total)
            reporter.message(f"VIRTUAL SHIZUOKA の無い所：{message}")
        reporter.step()
        reporter.detail(f"{done}/{total}枚")

    def on_tiles(done, total, bytes_done):
        on_stage("国土地理院DEMを取得中", done, total)

    loader = GSITileDEMLoader()
    sources = list(GSITileDEMLoader.TILE_SOURCES)
    while True:
        try:
            loader.fetch_for_extent(lon_min, lat_min, lon_max, lat_max, sources=sources, cancel_cb=cancel_cb,
                                    progress_cb=on_tiles, allow_partial_tiles=True, classify_gaps=True,
                                    stage_cb=on_stage)
            break
        except MemoryError:
            # 範囲が広く 1m のタイルが多すぎるときは、次の解像度から
            sources = sources[1:]
            if not sources:
                raise
    if loader._cancelled:
        raise Cancelled()
    if loader.data is None:
        raise RuntimeError(
            "VIRTUAL SHIZUOKA の無い陸地を補う国土地理院DEMを取得できませんでした。\n"
            + "\n".join(loader.last_errors[-3:])
        )

    base_code = {"DEM1A 1m": dem_gaps.MASK_FILLED_1A, "DEM5A 5m": dem_gaps.MASK_FILLED_5A,
                 "DEM10B 10m": dem_gaps.MASK_FILLED_10B}.get(loader._used_source_label, dem_gaps.MASK_FILLED_10B)
    with tempfile.TemporaryDirectory(prefix=".vs_gsi_", dir=work_dir) as tmp:
        dem_path = os.path.join(tmp, "gsi.tif")
        save_as_geotiff(loader, dem_path)
        values = _warp_to_grid(dem_path, grid, "bilinear", np.float32, NODATA)
        mask = np.full((grid.rows, grid.cols), dem_gaps.MASK_NODATA, dtype=np.uint8)
        if loader.gap_mask is not None:
            mask_path = os.path.join(tmp, "gsi_mask.tif")
            dem_gaps.save_mask_geotiff(loader.gap_mask, loader.gt, loader.crs_wkt, mask_path)
            mask = _warp_to_grid(mask_path, grid, "near", np.uint8, dem_gaps.MASK_NODATA)
    values[values == NODATA] = np.nan
    # 地理院DEMの「選んだ解像度の値」は、実際に使った解像度の補完として記録する
    mask[mask == dem_gaps.MASK_VALID] = base_code
    return values, mask


def _warp_to_grid(path, grid, resample_alg, dtype, nodata):
    import numpy as np
    from osgeo import gdal
    gdal_type = gdal.GDT_Byte if dtype == np.uint8 else gdal.GDT_Float32
    dst = gdal.GetDriverByName("MEM").Create("", grid.cols, grid.rows, 1, gdal_type)
    dst.SetGeoTransform(grid.gt)
    dst.SetProjection(_epsg_wkt())
    band = dst.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.Fill(nodata)
    gdal.Warp(dst, path, resampleAlg=resample_alg, dstNodata=nodata)
    data = band.ReadAsArray().astype(dtype)
    dst = None
    return data


class VirtualShizuokaSource(DemSource):
    key = "virtual_shizuoka"
    label = "VIRTUAL SHIZUOKA 0.5m DTM（静岡県）"
    description = (
        "出典：静岡県「VIRTUAL SHIZUOKA」航空レーザ測量データ LP/Grid（0.5m DTM、"
        "G空間情報センター）。ライセンス CC BY 4.0。\n"
        "・表示範囲が静岡県に掛かるときに選べます\n"
        "・計測年度は場所により異なり、新しい年度を優先し、その欠けは古い年度で補います\n"
        "・VIRTUAL SHIZUOKA の無い陸地（整備範囲の外・県外など）は国土地理院DEM（1m→5m→10m）で補い、"
        "どこを補ったかを知らせます。海・水面は解析の対象外にします\n"
        "・0.5mで配布されていますが、1mに変換して保存します\n"
        "利用条件は G空間情報センターの VIRTUAL SHIZUOKA 各データセットのページでご確認ください。"
    )

    def coverage_problem(self, extent):
        # 県を囲む長方形に少しでも掛かれば選べる（県外や整備範囲の外の陸は国土地理院DEMで補う）
        m = SELECT_MARGIN_DEG
        bbox = SHIZUOKA_BBOX_WGS84
        if (extent[2] <= bbox[0] - m or extent[0] >= bbox[2] + m
                or extent[3] <= bbox[1] - m or extent[1] >= bbox[3] + m):
            return "表示範囲が静岡県に掛かっていません"
        return None

    def estimate(self, extent):
        # 実在するタイル数は配信サーバに問い合わせるまで分からない（1タイル 400m×300m 程度、年度により異なる）
        return ""

    def fetch(self, extent, output_path, cancel_cb, reporter):
        import numpy as np
        from osgeo import gdal
        from .. import dem_gaps

        bbox = extent_in_epsg(extent, EPSG)
        grid = _Grid(bbox)
        reporter.message("配信サーバの年度を確認中")
        reporter.start(0)
        years = available_years(cancel_cb)

        work_dir = tempfile.mkdtemp(prefix=".virtual_shizuoka_", dir=os.path.dirname(output_path))
        try:
            layers = []  # (年度, GeoTIFF) 新しい年度が先
            data = np.full((grid.rows, grid.cols), np.nan, dtype=np.float32)
            for year in years:
                need = np.isnan(data)
                if not need.any():
                    break
                schemes = [YEAR_SCHEME[year]] if year in YEAR_SCHEME else ["A", "B"]
                candidates = set()
                for scheme in schemes:
                    for code, box in candidate_tiles(grid.xmin, grid.ymin, grid.xmax, grid.ymax, scheme).items():
                        window = grid.window(box)
                        if window is not None and need[window].any():
                            candidates.add(code)
                if not candidates:
                    continue
                reporter.message(f"{year}年度のタイルの有無を確認中")
                reporter.detail(f"候補 {len(candidates)}枚")
                codes = sorted(_existing_codes(year, candidates, cancel_cb))
                if not codes:
                    continue
                reporter.start(len(codes))
                added = 0
                for i, code in enumerate(codes):
                    check_cancel(cancel_cb)
                    reporter.message(f"{year}年度のタイルを取得中（{i + 1}/{len(codes)}枚目）")

                    def on_progress(done, total, _code=code):
                        size = f"{done / 1e6:.1f}/{total / 1e6:.1f}MB" if total > 0 else f"{done / 1e6:.1f}MB"
                        reporter.detail(f"{_code}：{size}")

                    zip_path = os.path.join(work_dir, f"{year}_{code}.zip")
                    _download(_tile_url(year, code), zip_path, cancel_cb=cancel_cb, progress_cb=on_progress)
                    tile_dir = os.path.join(work_dir, f"{year}_{code}")
                    os.makedirs(tile_dir)
                    tif_path = _extract_tif(zip_path, tile_dir)
                    os.remove(zip_path)
                    _ensure_crs(tif_path)
                    reporter.step()
                    if year not in YEAR_SCHEME:
                        # 方式の分からない年度は、ファイル自身の位置で範囲と重なるかを確かめる
                        window = grid.window(_raster_bbox(tif_path))
                        if window is None or not need[window].any():
                            continue
                    layers.append((year, tif_path))
                    added += 1
                if added:
                    reporter.message("タイルを重ねています")
                    data = _mosaic(layers, grid, work_dir)

            if not layers:
                raise RuntimeError(
                    "表示範囲にVIRTUAL SHIZUOKAのタイルがありません。国土地理院のDEMを選んでください。")

            mask = np.where(np.isnan(data), dem_gaps.MASK_NODATA, dem_gaps.MASK_VALID).astype(np.uint8)
            need = np.isnan(data)
            notes = []
            if need.any():
                values, gsi_mask = _gsi_fill(grid, need, work_dir, cancel_cb, reporter)
                use = need & np.isfinite(values)
                data[use] = values[use]
                mask[use] = gsi_mask[use]
                water = need & np.isin(gsi_mask, (dem_gaps.MASK_SEA, dem_gaps.MASK_WATER))
                mask[water] = gsi_mask[water]
                # 地理院DEMでも値が無く、海・水面でもない所（範囲の端など）は周囲から補う
                rest = np.isnan(data) & ~water
                if rest.any():
                    notes.append("補う値が無かった陸の欠けは、周囲の値から補間しました。")
                    mask[rest] = dem_gaps.MASK_INTERPOLATED

            check_cancel(cancel_cb)
            reporter.message("保存中")
            reporter.detail(f"座標系 EPSG:{EPSG}・{RESOLUTION}m として保存")
            out = data.copy()
            out[np.isnan(out)] = NODATA
            ds = gdal.GetDriverByName("GTiff").Create(
                output_path, grid.cols, grid.rows, 1, gdal.GDT_Float32,
                ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
            ds.SetGeoTransform(grid.gt)
            ds.SetProjection(_epsg_wkt())
            band = ds.GetRasterBand(1)
            band.SetNoDataValue(NODATA)
            band.WriteArray(out)
            interpolate = mask == dem_gaps.MASK_INTERPOLATED
            if interpolate.any():
                # 補間する所だけを埋める（海・水面は NoData のまま残す）
                keep = (mask == dem_gaps.MASK_SEA) | (mask == dem_gaps.MASK_WATER)
                gdal.FillNodata(band, None, 100, 0)
                filled = band.ReadAsArray()
                filled[keep] = NODATA
                band.WriteArray(filled)
            band.FlushCache()
            ds = None
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        mask_path = None
        mask_write_failed = False
        if (mask != dem_gaps.MASK_VALID).any():
            try:
                mask_path = dem_gaps.save_mask_geotiff(mask, grid.gt, _epsg_wkt(), dem_gaps.mask_path_for(output_path))
            except Exception:
                mask_write_failed = True
        summary = dem_gaps.summarize_mask(mask)
        years_used = "・".join(str(y) for y in sorted({y for y, _p in layers}))
        extra = f"{len(layers)}タイル（{years_used}年度）"
        land = max(1.0 - summary["sea"] - summary["water"], 1e-9)
        fills = [(summary["filled_1a"], "1m"), (summary["filled_5a"], "5m"), (summary["filled_10b"], "10m")]
        fill_total = sum(ratio for ratio, _name in fills) / land
        warnings = []
        if fill_total > 0:
            parts = "・".join(f"{name} {_percent(ratio / land)}" for ratio, name in fills if ratio > 0)
            extra += f"  |  国土地理院DEMで補完 {_percent(fill_total)}（{parts}）"
            warnings.append(
                f"陸地の{_percent(fill_total)}は VIRTUAL SHIZUOKA が無いため、国土地理院DEMで補いました（{parts}）。")
            if summary["land_fill_ratio"] >= dem_gaps.LAND_FILL_WARN_RATIO:
                warnings.append("5m・10mで補った部分は、傾斜などがなだらかに出ます。")
        warnings.extend(notes)
        if mask_write_failed:
            warnings.append(
                "どこを補ったか・対象外にしたかを示すファイル（dem_fetched_mask.tif）を書き込めませんでした。"
                "QGISで開いている場合は、レイヤーを削除してから取得し直してください。")
        elif mask_path and fill_total > 0:
            warnings.append(f"どこを補ったか・対象外にしたかは次のファイルで確認できます：\n{mask_path}")
        result = {"path": output_path,
                  "info": format_info(grid.cols, grid.rows, self.label, EPSG, RESOLUTION, extra),
                  "warning": "\n".join(warnings)}
        if mask_path:
            result["mask_path"] = mask_path
        return result


def _percent(ratio):
    if ratio < 0.001:
        return "0.1%未満"
    return f"{ratio * 100:.1f}%"
