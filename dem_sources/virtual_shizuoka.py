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
    どちらの方式かは年度で決まり事前に分からないため、両方の候補を作り S3 で実在を確認する。
    新しい年度が追加されたら、実タイルを取得してサイズと AA の符号化を必ず確かめること。

整備範囲の索引は無い。ブラウザでは静岡県の外接矩形で判定し、取得後に表示範囲の欠けを
確かめる（base.build_dem_from_tiles）。
"""

import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile

from .base import (
    DemSource, Cancelled, build_dem_from_tiles, check_cancel, extent_in_epsg, extent_within,
    format_info, NODATA,
)

EPSG = 6676
RESOLUTION = 1  # 0.5m配布。解析側の解像度判定（1/5/10m）に合わせて1mで保存する
BUCKET_URL = "https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com"
TILE_W = 400
TILE_H = 300
YEAR_PRIORITY = (2025, 2022, 2021, 2020, 2019)  # 新しい年度を優先
SHIZUOKA_BBOX_WGS84 = (137.47410694, 34.57213583, 139.17655861, 35.64595651)

_KEY_PATTERN = re.compile(r"<Key>([^<]+)</Key>")


# ── タイル座標 ───────────────────────────────────────────────────────

def _folder_origin(folder):
    return (ord(folder[1]) - ord("E")) * 40000, -(ord(folder[0]) - ord("M") + 2) * 30000


def tile_bbox(code):
    """タイルコード → (xmin, ymin, xmax, ymax)（方式Aの座標）"""
    x0, y0 = _folder_origin(code[2:4])
    xx, aa = int(code[4:6]), int(code[6:8])
    xc = x0 + (xx % 10) * 4000 + (aa % 10) * TILE_W + TILE_W // 2
    yc = y0 - (xx // 10) * 3000 - (aa // 10) * TILE_H - TILE_H // 2
    return xc - TILE_W // 2, yc - TILE_H // 2, xc + TILE_W // 2, yc + TILE_H // 2


def tiles_for_extent(xmin, ymin, xmax, ymax):
    """EPSG:6676 の範囲に重なる候補コード（方式A・Bの両方、実在は未確認）"""
    codes = set()
    for c1 in "LMNOP":
        for c2 in "BCDEF":
            folder = c1 + c2
            x0, y0 = _folder_origin(folder)
            if xmax < x0 or xmin > x0 + 40000 or ymax < y0 - 30000 or ymin > y0:
                continue
            for yr in range(max(0, int((y0 - ymax) / 3000)), min(9, int((y0 - ymin) / 3000)) + 1):
                for xc in range(max(0, int((xmin - x0) / 4000)), min(9, int((xmax - x0) / 4000)) + 1):
                    bx, by = x0 + xc * 4000, y0 - yr * 3000
                    xx = yr * 10 + xc
                    # 方式A: 400m×300m 単純行列
                    for ar in range(max(0, int((by - ymax) / TILE_H)), min(9, int((by - ymin) / TILE_H)) + 1):
                        for ac in range(max(0, int((xmin - bx) / TILE_W)), min(9, int((xmax - bx) / TILE_W)) + 1):
                            codes.add(f"08{folder}{xx:02d}{ar * 10 + ac:02d}")
                    # 方式B: 1000m×750m Z曲線
                    for gr in range(max(0, int((by - ymax) / 750)), min(3, int((by - ymin) / 750)) + 1):
                        for gc in range(max(0, int((xmin - bx) / 1000)), min(3, int((xmax - bx) / 1000)) + 1):
                            t = (gr // 2) * 2 + (gc // 2) + 1
                            u = (gr % 2) * 2 + (gc % 2) + 1
                            codes.add(f"08{folder}{xx:02d}{t * 10 + u:02d}")
    # 方式Bの候補が方式Aのタイルとして範囲外にあると誤って取得してしまうため、方式Aの座標で絞る
    return {
        code for code in codes
        if (lambda b: b[2] > xmin and b[0] < xmax and b[3] > ymin and b[1] < ymax)(tile_bbox(code))
    }


# ── S3 ───────────────────────────────────────────────────────────────

def _tile_url(year, code):
    return f"{BUCKET_URL}/{year}/LP/Grid/08/{code[2:4]}/{code[4:6]}/{code}.zip"


def _s3_list(year, folder, xx):
    """指定ディレクトリにあるタイルコードの集合。一覧が取れなければ None。
    応答は S3 ListObjectsV2 の XML で、必要なのは <Key> だけなので正規表現で取り出す
    （XMLパーサを使わない）。"""
    prefix = f"{year}/LP/Grid/08/{folder}/{xx}/"
    url = f"{BUCKET_URL}/?list-type=2&prefix={prefix}&delimiter=/"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    return {
        key.rstrip("/").split("/")[-1][:-4]
        for key in _KEY_PATTERN.findall(body) if key.endswith(".zip")
    }


def _s3_exists(year, code):
    """一覧が取れない場合の個別確認（HEAD）"""
    req = urllib.request.Request(_tile_url(year, code), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10):  # nosec B310
            return True
    except Exception:
        return False


def resolve_years(codes, cancel_cb=None, progress_cb=None):
    """候補コード → {実在するコード: 年度}（新しい年度を優先）"""
    groups = {}
    for code in codes:
        groups.setdefault((code[2:4], code[4:6]), set()).add(code)

    result = {}
    for done, ((folder, xx), batch) in enumerate(groups.items(), start=1):
        left = set(batch)
        for year in YEAR_PRIORITY:
            if not left:
                break
            check_cancel(cancel_cb)
            listed = _s3_list(year, folder, xx)
            if listed is None:
                for code in list(left):
                    check_cancel(cancel_cb)
                    if _s3_exists(year, code):
                        result[code] = year
                        left.discard(code)
                continue
            for code in left & listed:
                result[code] = year
            left -= listed
        if progress_cb:
            progress_cb(done, len(groups))
    return result


def _download(url, dest, cancel_cb=None, progress_cb=None):
    with urllib.request.urlopen(url, timeout=600) as resp, open(dest, "wb") as fh:  # nosec B310
        total = resp.headers.get("Content-Length")
        total = int(total) if total is not None else -1
        downloaded = 0
        last_report = 0.0
        while True:
            if cancel_cb and cancel_cb():
                raise Cancelled()
            chunk = resp.read(65536)
            if not chunk:
                break
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


class VirtualShizuokaSource(DemSource):
    key = "virtual_shizuoka"
    label = "VIRTUAL SHIZUOKA 0.5m DTM（静岡県）"
    description = (
        "出典：静岡県「VIRTUAL SHIZUOKA」航空レーザ測量データ LP/Grid（0.5m DTM、"
        "G空間情報センター）。ライセンス CC BY 4.0。\n"
        "・静岡県内のみ。表示範囲が静岡県の範囲に収まるときだけ選べます\n"
        "・計測年度（2019〜2025）は場所により異なり、新しい年度を優先して取得します\n"
        "・整備範囲の索引が無いため、取得後に表示範囲の欠けを確認します。"
        "欠けが大きい場合は取得を中止します\n"
        "・0.5mで配布されていますが、1mに変換して保存します\n"
        "利用条件は G空間情報センターの VIRTUAL SHIZUOKA 各データセットのページでご確認ください。"
    )

    def coverage_problem(self, extent):
        if not extent_within(extent, SHIZUOKA_BBOX_WGS84):
            return "表示範囲が静岡県の範囲の外です"
        return None

    def estimate(self, extent):
        # 実在するタイル数は S3 に問い合わせるまで分からない（1タイル 400m×300m 程度、年度により異なる）
        return ""

    def fetch(self, extent, output_path, cancel_cb, reporter):
        bbox = extent_in_epsg(extent, EPSG)
        candidates = tiles_for_extent(*bbox)

        reporter.message("配信サーバでタイルの有無を確認中")
        reporter.start(0)
        resolved = resolve_years(
            candidates, cancel_cb=cancel_cb,
            progress_cb=lambda done, total: reporter.detail(f"{done}/{total}区画を確認済み"),
        )
        if not resolved:
            raise RuntimeError("表示範囲にVIRTUAL SHIZUOKAのタイルがありません。")

        reporter.start(len(resolved) + 1)
        work_dir = tempfile.mkdtemp(prefix=".virtual_shizuoka_", dir=os.path.dirname(output_path))
        try:
            tile_paths = []
            for i, (code, year) in enumerate(sorted(resolved.items())):
                check_cancel(cancel_cb)
                reporter.message(f"タイルを取得中（{i + 1}/{len(resolved)}枚目）")

                def on_progress(done, total, _code=code, _year=year):
                    size = f"{done / 1e6:.1f}/{total / 1e6:.1f}MB" if total > 0 else f"{done / 1e6:.1f}MB"
                    reporter.detail(f"{_code}（{_year}年度）：{size}")

                zip_path = os.path.join(work_dir, f"{code}.zip")
                _download(_tile_url(year, code), zip_path, cancel_cb=cancel_cb, progress_cb=on_progress)
                tile_dir = os.path.join(work_dir, code)
                os.makedirs(tile_dir)
                tif_path = _extract_tif(zip_path, tile_dir)
                os.remove(zip_path)
                _ensure_crs(tif_path)
                tile_paths.append(tif_path)
                reporter.step()

            check_cancel(cancel_cb)
            reporter.message("タイルを結合して保存中")
            reporter.detail(f"EPSG:{EPSG}・{RESOLUTION}m")
            cols, rows, filled = build_dem_from_tiles(tile_paths, bbox, EPSG, output_path, RESOLUTION)
            reporter.step()
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        years = "・".join(str(y) for y in sorted(set(resolved.values())))
        extra = f"{len(tile_paths)}タイル（{years}年度）"
        if filled:
            extra += f"  |  NoData補間 {filled} px"
        return {"path": output_path, "info": format_info(cols, rows, self.label, EPSG, RESOLUTION, extra)}
