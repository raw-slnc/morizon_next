# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import subprocess
import time

import requests

API_BASE = "https://service.gsi.go.jp/kiban/app/api"

TYPE_CODE_ROAD_EDGE = "06"
TYPE_CODE_BUILDING = "11"

# 道路縁ZIP内の実レイヤー名（実サンプルFG-GML-523807-06-*.zipで確認済み）
ROAD_LAYERS = ["RdEdg", "RdCompt"]
# 建物ポリゴンとして使うため、建築物の面データのみ採用する（BldLは線データ）。
BUILDING_LAYERS = ["BldA"]


def latlon_to_mesh2(lat: float, lon: float) -> str:
    """緯度経度から基盤地図情報の2次メッシュコード（6桁、約10km四方）を求める"""
    lat_min = lat * 60
    p = int(lat_min // 40)
    a = lat_min - p * 40
    q = int(a // 5)

    lon_deg = lon - 100
    u = int(lon_deg)
    d = lon_deg - u
    v = int(d // 0.125)

    return f"{p:02d}{u:02d}{q}{v}"


def mesh2_bounds(mesh: str):
    """2次メッシュコードから(lon_min, lat_min, lon_max, lat_max)を求める（検算・デバッグ用）"""
    p = int(mesh[0:2])
    u = int(mesh[2:4])
    q = int(mesh[4])
    v = int(mesh[5])
    lat_min = (p * 40 + q * 5) / 60
    lat_max = (p * 40 + (q + 1) * 5) / 60
    lon_min = 100 + u + v * 0.125
    lon_max = 100 + u + (v + 1) * 0.125
    return lon_min, lat_min, lon_max, lat_max


def mesh_codes_for_extent(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> list:
    """WGS84範囲を覆う2次メッシュコードを列挙する"""
    codes = set()
    lat_step = 5 / 60
    lon_step = 0.125
    lat = lat_min
    while lat <= lat_max:
        lon = lon_min
        while lon <= lon_max:
            codes.add(latlon_to_mesh2(lat, lon))
            lon += lon_step
        lat += lat_step
    # 右端・上端の取りこぼし対策
    codes.add(latlon_to_mesh2(lat_max, lon_max))
    return sorted(codes)


def build_session(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="service.gsi.go.jp")
    return session


def check_login(session: requests.Session) -> bool:
    resp = session.get(f"{API_BASE}/isLogin", timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return bool(data.get("results", {}).get("login"))


def query_latest(session: requests.Session, type_code: str, mesh_codes: list) -> list:
    """指定type_code・メッシュ群の最新データ一覧を取得する。要素は{id, file_name, place_code, ...}"""
    url = f"{API_BASE}/base/latest"
    params = {"type_codes": type_code, "mesh_codes": ",".join(mesh_codes)}
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("result", {}).get("status") != "success":
        raise RuntimeError(data.get("result", {}).get("message") or "APIエラー")
    return data.get("results", [])


def download_file(session: requests.Session, file_id: int, output_path: str, cancel_cb=None,
                  chunk_cb=None):
    """chunk_cb(受け取ったバイト数) は受け取るたびに呼ぶ（このファイル内の累計）"""
    url = f"{API_BASE}/download/file/{file_id}"
    with session.get(url, timeout=60, stream=True) as resp:
        resp.raise_for_status()
        received = 0
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=262144):
                if cancel_cb and cancel_cb():
                    raise InterruptedError()
                if chunk:
                    f.write(chunk)
                    received += len(chunk)
                    if chunk_cb:
                        chunk_cb(received)
    return output_path


def _item_size(item: dict) -> int:
    """一覧に載っている大きさ（KB単位）をバイトで返す。載っていなければ0"""
    try:
        return int(item.get("file_size_kbyte") or 0) * 1024
    except (TypeError, ValueError):
        return 0


def fetch_meshes(session: requests.Session, type_code: str, mesh_codes: list, cache_dir: str,
                 progress_cb=None, cancel_cb=None, size_cb=None, listed_sizes=None) -> list:
    """type_code・メッシュ群のZIPをcache_dirへダウンロードする（既存ファイルはスキップ）。ZIPパスのリストを返す。
    size_cb(全体の大きさ, そろえた量) はバイト単位で、一覧を取った直後と受け取りの途中
    （0.15秒間隔に間引く）、各ファイルの終わりに呼ぶ。そろえた量は、取得済みで使い回すファイルも
    ダウンロードしたファイルと同じく数える（共有フォルダから写してくる見え方にする）。
    どちらも一覧に載っている大きさで数え、最後は全体の大きさと同じになる。
    listed_sizes に辞書を渡すと、ZIPのパスごとに一覧に載っている大きさ（バイト）を入れる"""
    os.makedirs(cache_dir, exist_ok=True)
    items = query_latest(session, type_code, mesh_codes)
    zip_paths = []
    total = len(items)
    needed_bytes = sum(_item_size(item) for item in items)
    obtained_bytes = 0
    if size_cb:
        size_cb(needed_bytes, obtained_bytes)
    for i, item in enumerate(items):
        if cancel_cb and cancel_cb():
            raise InterruptedError()
        file_name = item["file_name"]
        out_path = os.path.join(cache_dir, file_name)
        if listed_sizes is not None:
            listed_sizes[out_path] = _item_size(item)
        if not os.path.exists(out_path):
            last_report = [0.0]

            def on_chunk(received, size=_item_size(item)):
                now = time.monotonic()
                if size_cb and now - last_report[0] >= 0.15:
                    last_report[0] = now
                    size_cb(needed_bytes, obtained_bytes + min(received, size))

            download_file(session, item["id"], out_path, cancel_cb=cancel_cb, chunk_cb=on_chunk)
        obtained_bytes += _item_size(item)
        zip_paths.append(out_path)
        if size_cb:
            size_cb(needed_bytes, obtained_bytes)
        if progress_cb:
            progress_cb(i + 1, total, file_name)
    return zip_paths


def merge_layers(zip_paths: list, output_path: str,
                 lon_min: float, lat_min: float, lon_max: float, lat_max: float,
                 include_layer_names=None, driver_format="ESRI Shapefile",
                 output_layer=None, dst_crs="EPSG:4326",
                 clip_to_extent=True, zip_done_cb=None) -> bool:
    """複数ZIP内のGMLレイヤーを1つのベクタファイルへ範囲クリップしつつ統合する。
    zip_done_cb(ZIPのパス) は、ZIPを1つまとめ終えるごとに呼ぶ。
    include_layer_names未指定の場合はZIP内の全レイヤーを対象にする。
    driver_formatは"ESRI Shapefile"（既存のフォルダ読み込み規約=INPUT_BUILDING/INPUT_NETWORKに合わせる）
    または"GPKG"。1件以上書き込めた場合はTrueを返す。

    dst_crsは出力のCRS。DEMと異なるCRS（既定はWGS84度単位）のまま保存すると、
    distance.py等の後続処理がDEMの解像度(メートル)をそのまま度単位の範囲に適用してしまい、
    0x0ピクセルのラスターを作ろうとして失敗する不具合が実際に発生したため、
    呼び出し側でDEMのCRSを渡して合わせられるようにしている。"""
    from osgeo import gdal

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _remove_shapefile_dataset(output_path)
    first = True
    for zip_path in zip_paths:
        for member in _zip_member_names(zip_path):
            if not _is_feature_xml(member):
                continue
            src = f"/vsizip/{zip_path}/{member}"
            ds = gdal.OpenEx(src, gdal.OF_VECTOR)
            if ds is None:
                continue
            layer_count = ds.GetLayerCount()
            ds = None
            for i in range(layer_count):
                ds = gdal.OpenEx(src, gdal.OF_VECTOR)
                layer = ds.GetLayer(i)
                name = layer.GetName()
                if include_layer_names and name not in include_layer_names:
                    ds = None
                    continue
                feature_count = layer.GetFeatureCount()
                ds = None
                if feature_count == 0:
                    continue
                kwargs = dict(
                    format=driver_format,
                    layers=[name],
                    accessMode="overwrite" if first else "append",
                    dstSRS=dst_crs,
                    reproject=True,
                    skipFailures=True,
                )
                if clip_to_extent:
                    kwargs["spatFilter"] = (lon_min, lat_min, lon_max, lat_max)
                    kwargs["spatSRS"] = "EPSG:4326"
                if driver_format == "GPKG" and output_layer:
                    kwargs["layerName"] = output_layer
                if driver_format == "ESRI Shapefile":
                    # DBFの既定エンコーディング(ISO-8859-1)では日本語属性が文字化けするため明示指定
                    kwargs["layerCreationOptions"] = ["ENCODING=UTF-8"]
                translated = _translate_layer(
                    src,
                    output_path,
                    name,
                    kwargs,
                    driver_format,
                    output_layer,
                    first,
                    clip_to_extent,
                    (lon_min, lat_min, lon_max, lat_max),
                    dst_crs,
                )
                if translated is not None:
                    translated = None
                    first = False
        if zip_done_cb:
            zip_done_cb(zip_path)

    if first:
        return False

    valid_count = _count_non_empty_geometries(output_path)
    if valid_count == 0:
        _remove_shapefile_dataset(output_path)
        return False
    return True


def create_empty_line_shapefile(output_path: str, dst_crs="EPSG:4326",
                                layer_name=None) -> str:
    """後続処理へ「道路地物なし」を明示的に渡すための空ラインShapefileを作る。"""
    from osgeo import ogr, osr

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    _remove_shapefile_dataset(output_path)
    driver = ogr.GetDriverByName("ESRI Shapefile")
    ds = driver.CreateDataSource(output_path)
    if ds is None:
        raise RuntimeError(f"空の道路データを作成できませんでした: {output_path}")
    srs = osr.SpatialReference()
    if dst_crs:
        srs.SetFromUserInput(dst_crs)
    layer = ds.CreateLayer(
        layer_name or os.path.splitext(os.path.basename(output_path))[0],
        srs,
        ogr.wkbLineString,
    )
    if layer is None:
        ds = None
        raise RuntimeError(f"空の道路レイヤーを作成できませんでした: {output_path}")
    field = ogr.FieldDefn("gml_id", ogr.OFTString)
    field.SetWidth(80)
    layer.CreateField(field)
    ds = None
    return output_path


def _zip_member_names(zip_path: str) -> list:
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def _is_feature_xml(member_name: str) -> bool:
    basename = os.path.basename(member_name)
    return basename.startswith("FG-GML-") and basename.lower().endswith(".xml")


def _remove_shapefile_dataset(output_path: str):
    if not output_path.lower().endswith(".shp"):
        if os.path.exists(output_path):
            os.remove(output_path)
        return

    stem, _ = os.path.splitext(output_path)
    for ext in (
        ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx",
        ".fix", ".idm", ".ind", ".ain", ".aih", ".atx", ".ixs", ".mxs",
        ".xml",
    ):
        path = stem + ext
        if os.path.exists(path):
            os.remove(path)


def _translate_layer(src: str, output_path: str, layer_name: str, kwargs: dict,
                     driver_format: str, output_layer: str, first: bool,
                     clip_to_extent: bool, extent_wgs84: tuple, dst_crs: str):
    """GPKG へ書くときは、ディスクへの書き込みを待たない設定にする。壊れた地物を飛ばす指定
    （skipfailures）をすると1件ごとに保存を確定するようになり、待つ設定のままでは建物4.5万件で
    約3分かかった（待たない設定で約5秒）。書き先は呼び出し側の一時ファイルで、途中で止まって
    壊れても捨てるだけなので、待たなくてよい"""
    ogr2ogr = shutil.which("ogr2ogr")
    if ogr2ogr:
        return _translate_layer_with_ogr2ogr(
            ogr2ogr, src, output_path, layer_name, driver_format, output_layer,
            first, clip_to_extent, extent_wgs84, dst_crs,
        )

    from osgeo import gdal
    if driver_format != "GPKG":
        return gdal.VectorTranslate(output_path, src, **kwargs)
    previous = gdal.GetConfigOption("OGR_SQLITE_SYNCHRONOUS")
    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    try:
        return gdal.VectorTranslate(output_path, src, **kwargs)
    finally:
        gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", previous)


def _translate_layer_with_ogr2ogr(ogr2ogr: str, src: str, output_path: str,
                                  layer_name: str, driver_format: str,
                                  output_layer: str, first: bool,
                                  clip_to_extent: bool, extent_wgs84: tuple,
                                  dst_crs: str):
    target_layer = output_layer or os.path.splitext(os.path.basename(output_path))[0]
    cmd = [ogr2ogr, "-f", driver_format]
    if not first:
        cmd.extend(["-update", "-append"])
    if clip_to_extent:
        lon_min, lat_min, lon_max, lat_max = extent_wgs84
        cmd.extend([
            "-spat", str(lon_min), str(lat_min), str(lon_max), str(lat_max),
            "-spat_srs", "EPSG:4326",
        ])
    if dst_crs:
        cmd.extend(["-t_srs", dst_crs])
    cmd.append("-skipfailures")
    if target_layer:
        cmd.extend(["-nln", target_layer])
    if driver_format == "ESRI Shapefile":
        cmd.extend(["-lco", "ENCODING=UTF-8"])
    if driver_format == "GPKG":
        cmd.extend(["--config", "OGR_SQLITE_SYNCHRONOUS", "OFF"])
    cmd.extend([output_path, src, layer_name])

    completed = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "基盤地図情報の変換に失敗しました。\n"
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return True


def _count_non_empty_geometries(vector_path: str) -> int:
    from osgeo import ogr

    ds = ogr.Open(vector_path)
    if ds is None:
        return 0
    count = 0
    for layer_idx in range(ds.GetLayerCount()):
        layer = ds.GetLayer(layer_idx)
        if layer is None:
            continue
        layer.ResetReading()
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None and not geometry.IsEmpty():
                count += 1
    ds = None
    return count
