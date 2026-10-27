# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""林野庁「全国森林資源メッシュ」（G空間情報センター）の取得。

平面直角座標系の系ごとのデータセット（mesh_1〜mesh_19）に、国土基本図の図郭ごとの 7z（中は GeoPackage）と、
図郭割データ（図郭名の入った GeoPackage）が置かれている。DEM の範囲にかかる図郭だけを取得し、共有データに保存する
（同じ図郭は2回目から取得しない）。7z は解凍せず、GDAL の /vsi7z/ で中の GeoPackage を読む。
"""

import json
import os

import requests

PACKAGE_SHOW_URL = "https://www.geospatial.jp/ckan/api/3/action/package_show"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)", "Accept-Encoding": "identity"}
TIMEOUT = (30, 120)  # （つなぐまで, 受け取りの合間）。混み合っているときは受け取りが遅いので長めにする
CACHE_SUBDIR = "national_mesh"

UNREADABLE_7Z = (
    "このQGISでは、全国森林資源メッシュ（7z）の中を読めませんでした。"
    "G空間情報センターから図郭のファイルをダウンロードして解凍し、ほかの方法で地位指数マップを作って取り込んでください。"
)


def zone_from_epsg(epsg):
    """平面直角座標系の EPSG（JGD2011 6669〜6687、JGD2000 2443〜2461）から系の番号。違えば None"""
    if 6669 <= epsg <= 6687:
        return epsg - 6668
    if 2443 <= epsg <= 2461:
        return epsg - 2442
    return None


def _resources(zone, cache_dir):
    """系のデータセットのファイルの一覧。取れたら保存し、つながらないとき（混み合いなど）は保存したものを使う"""
    saved = os.path.join(cache_dir, "resources.json")
    try:
        resp = requests.get(PACKAGE_SHOW_URL, params={"id": f"mesh_{zone}"}, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        resources = resp.json()["result"]["resources"]
    except (requests.RequestException, ValueError, KeyError) as e:
        if os.path.isfile(saved):
            with open(saved, encoding="utf-8") as f:
                return json.load(f)
        raise RuntimeError(
            "G空間情報センターにつながりませんでした（混み合っている可能性があります）。時間をおいてもう一度お試しください。"
            f"\n（{e}）"
        ) from e
    os.makedirs(cache_dir, exist_ok=True)
    with open(saved, "w", encoding="utf-8") as f:
        json.dump(resources, f, ensure_ascii=False)
    return resources


def _download(url, dest_path, progress_cb=None, cancel_cb=None):
    """dest_path が既にあれば取得しない。中断したら None"""
    if os.path.exists(dest_path):
        return dest_path
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".part"
    with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                if cancel_cb is not None and cancel_cb():
                    f.close()
                    os.remove(tmp_path)
                    return None
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(done, total)
    os.replace(tmp_path, dest_path)
    return dest_path


def tiles_for_bounds(index_path, bounds):
    """図郭割データから、bounds（xmin, ymin, xmax, ymax、系の座標）にかかる図郭名の一覧"""
    from osgeo import ogr
    dataset = ogr.Open(index_path)
    if dataset is None:
        raise RuntimeError("全国森林資源メッシュの図郭割データを読み込めませんでした。")
    layer = dataset.GetLayer(0)
    layer.SetSpatialFilterRect(*bounds)
    return sorted({str(feature.GetField("図郭名")) for feature in layer if feature.GetField("図郭名")})


def gpkg_in_7z(path):
    """7z の中の GeoPackage を、GDAL で読めるパス（/vsi7z/…）で返す。読めない環境なら RuntimeError"""
    from osgeo import gdal
    entries = gdal.ReadDir(f"/vsi7z/{path}")
    names = [name for name in (entries or []) if name.lower().endswith(".gpkg")]
    if not names:
        raise RuntimeError(UNREADABLE_7Z)
    return f"/vsi7z/{path}/{names[0]}"


def fetch_tiles(zone, bounds, cache_base_dir, message_cb=None, progress_cb=None, cancel_cb=None):
    """DEM の範囲にかかる図郭の 7z を取得し（共有データに保存）、中の GeoPackage を読むパスの一覧を返す。
    中断したら None"""
    cache_dir = os.path.join(cache_base_dir, CACHE_SUBDIR, f"zone_{zone}")
    resources = _resources(zone, cache_dir)
    index = next((r for r in resources if r["url"].lower().endswith(".gpkg") and "index" in r["url"].lower()), None)
    if index is None:
        raise RuntimeError(f"第{zone}系の全国森林資源メッシュの図郭割データが見つかりません。")
    index_path = _download(index["url"], os.path.join(cache_dir, "index.gpkg"), cancel_cb=cancel_cb)
    if index_path is None:
        return None
    tiles = tiles_for_bounds(index_path, bounds)
    by_name = {r["name"].lower(): r["url"] for r in resources if r["url"].lower().endswith(".7z")}
    paths = []
    for number, tile in enumerate(tiles, start=1):
        key = next((name for name in by_name if name.startswith(f"fr_mesh20m_{tile.lower()}_")), None)
        if key is None:
            continue  # 森林の無い図郭などは配布されていない
        if message_cb is not None:
            message_cb(f"図郭 {tile} を取得中（{number}/{len(tiles)}）")
        try:
            path = _download(by_name[key], os.path.join(cache_dir, os.path.basename(by_name[key])),
                             progress_cb=progress_cb, cancel_cb=cancel_cb)
        except requests.RequestException as e:
            raise RuntimeError(
                f"図郭 {tile} を取得できませんでした（G空間情報センターが混み合っている可能性があります）。"
                f"時間をおいてもう一度お試しください。取得済みの図郭は保存してあります。\n（{e}）"
            ) from e
        if path is None:
            return None
        paths.append(gpkg_in_7z(path))
    if not paths:
        raise RuntimeError("DEM の範囲にかかる全国森林資源メッシュの図郭が見つかりませんでした。")
    return paths
