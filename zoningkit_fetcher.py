# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
もりぞんZoningKit（NPP/SRAD/VTEX等の地位指数算出用データ）取得ユーティリティ。

配布元：geospatial.jp（もりぞん本体と同じCKANデータセット、GPLv3・登録不要）。
座標系1〜13系（JGD2011 平面直角座標系I〜XIII）のみ提供。14〜19系（南西諸島・
小笠原等）は対象外。
"""

import functools
import json
import math
import os
import struct
import time
import zipfile
import zlib

import requests

CKAN_PACKAGE_SHOW_URL = "https://www.geospatial.jp/ckan/api/3/action/package_show?id=rinya-morizon-dateset"

# JGD2011 平面直角座標系 I〜XIII の (EPSGコード, 経度min, 緯度min, 経度max, 緯度max)
# QGISのCRSデータベース(area of use)から取得した値。ZoningKitが提供するのはこのうち1〜13系のみ
ZONE_BOUNDS = {
    1: (6669, 128.17, 26.96, 130.46, 34.74),
    2: (6670, 129.76, 30.18, 132.05, 33.99),
    3: (6671, 130.81, 33.72, 133.49, 36.38),
    4: (6672, 131.95, 32.69, 134.81, 34.45),
    5: (6673, 133.13, 34.13, 135.47, 35.71),
    6: (6674, 134.86, 33.40, 136.99, 36.33),
    7: (6675, 136.22, 34.51, 137.84, 37.58),
    8: (6676, 137.32, 34.54, 139.91, 38.58),
    9: (6677, 138.40, 29.31, 141.11, 37.98),
    10: (6678, 139.49, 37.73, 142.14, 41.58),
    11: (6679, 139.34, 41.34, 141.46, 43.42),
    12: (6680, 140.89, 42.15, 143.61, 45.54),
    13: (6681, 142.61, 41.87, 145.87, 44.40),
}


# 各系の原点（緯度, 経度）。法令（平成14年国土交通省告示第9号）による
ZONE_ORIGINS = {
    1: (33, 129.5), 2: (33, 131), 3: (36, 132 + 10 / 60), 4: (33, 133.5), 5: (36, 134 + 20 / 60),
    6: (36, 136), 7: (36, 137 + 10 / 60), 8: (36, 138.5), 9: (36, 139 + 50 / 60), 10: (40, 140 + 50 / 60),
    11: (44, 140.25), 12: (44, 142.25), 13: (44, 144.25),
}

# 都道府県コード → 系（法令は都道府県ごとに系を決めている）。
# 1つの道県に複数の系がある北海道・東京都・鹿児島県・沖縄県は _zone_in_split_prefecture で決める
PREFECTURE_ZONES = {
    2: 10, 3: 10, 4: 10, 5: 10, 6: 10,                  # 青森・岩手・宮城・秋田・山形
    7: 9, 8: 9, 9: 9, 10: 9, 11: 9, 12: 9, 14: 9,       # 福島・茨城・栃木・群馬・埼玉・千葉・神奈川
    15: 8, 19: 8, 20: 8, 22: 8,                         # 新潟・山梨・長野・静岡
    16: 7, 17: 7, 21: 7, 23: 7,                         # 富山・石川・岐阜・愛知
    18: 6, 24: 6, 25: 6, 26: 6, 27: 6, 29: 6, 30: 6,    # 福井・三重・滋賀・京都・大阪・奈良・和歌山
    28: 5, 31: 5, 33: 5,                                # 兵庫・鳥取・岡山
    32: 3, 34: 3, 35: 3,                                # 島根・広島・山口
    36: 4, 37: 4, 38: 4, 39: 4,                         # 徳島・香川・愛媛・高知
    40: 2, 41: 2, 43: 2, 44: 2, 45: 2,                  # 福岡・佐賀・熊本・大分・宮崎
    42: 1,                                              # 長崎
}

# 北海道の11系・13系の市町村コード（法令の区分。ほかは12系）。コードと名前は国土地理院の市区町村コードの表で確かめた
_HOKKAIDO_ZONE11 = frozenset(
    [1202, 1203, 1233, 1236]                    # 函館市・小樽市・伊達市・北斗市
    + list(range(1331, 1348))                   # 渡島総合振興局の所管区域
    + list(range(1361, 1372))                   # 檜山振興局の所管区域
    + list(range(1391, 1410))                   # 後志総合振興局の所管区域
    + [1571, 1575, 1584]                        # 胆振のうち豊浦町・壮瞥町・洞爺湖町
)
_HOKKAIDO_ZONE13 = frozenset(
    [1206, 1207, 1208, 1211, 1223]              # 釧路市・帯広市・北見市・網走市・根室市
    + list(range(1543, 1565))                   # オホーツク総合振興局の所管区域（紋別市は12系）
    + list(range(1631, 1650))                   # 十勝総合振興局の所管区域
    + list(range(1661, 1669))                   # 釧路総合振興局の所管区域
    + list(range(1691, 1701))                   # 根室振興局の所管区域
)

REVERSE_GEOCODER_URL = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
# 地点が海などで市区町村が返らないとき、まわりを探す距離（度）と方向
_SEARCH_RADII = (0.01, 0.02, 0.05, 0.1)
_SEARCH_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def find_zone_for_point(lon: float, lat: float):
    """
    経度緯度(WGS84)の地点の平面直角座標系の系(1-13)を返す。14-19系の地域（ZoningKitが無い）はNoneを返す。
    法令は都道府県ごとに系を決めているため、国土地理院の逆ジオコーディングで都道府県を調べて決める。
    調べられないとき（通信できない、近くに陸が無い）は、ZONE_BOUNDS の範囲と原点の近さで決める（県境付近でずれ得る）。
    DEMの座標系と、使うZoningKitの両方をこれで決める。同じ地点の結果は覚えておき、通信を繰り返さない。
    """
    fallback = _zone_by_bounds(lon, lat)
    if fallback is None:
        # 1〜13系のどの範囲にも入らない（国外・14〜19系の地域）。問い合わせるまでもない
        return None
    municipality = _municipality_code_near(round(lon, 4), round(lat, 4))
    if municipality is None:
        return fallback
    prefecture = municipality // 1000
    if prefecture in PREFECTURE_ZONES:
        return PREFECTURE_ZONES[prefecture]
    return _zone_in_split_prefecture(prefecture, municipality, lon, lat)


def _zone_in_split_prefecture(prefecture: int, municipality: int, lon: float, lat: float):
    """1つの道県に複数の系がある北海道・東京都・鹿児島県・沖縄県の系"""
    if prefecture == 1:
        # 北海道は市町村ごとに11・12・13系に分かれる
        if municipality in _HOKKAIDO_ZONE11:
            return 11
        if municipality in _HOKKAIDO_ZONE13:
            return 13
        return 12
    if prefecture == 13:
        # 東京都：北緯28度より南（小笠原諸島など）は14・18・19系で、ZoningKitが無い
        return 9 if lat >= 28 else None
    if prefecture == 46:
        # 鹿児島県：北緯27〜32度・東経128度18分〜130度の島（奄美群島は東経130度13分まで）はI系、ほかはII系
        if 27 <= lat <= 32 and 128.3 <= lon <= 130:
            return 1
        if 27 <= lat <= 28.6 and lon <= 130 + 13 / 60:
            return 1
        return 2
    # 沖縄県は15・16・17系で、ZoningKitが無い
    return None


@functools.lru_cache(maxsize=64)
def _municipality_code_near(lon: float, lat: float):
    """地点の市区町村コード（5桁の数）。地点が海などで市区町村が返らなければ、まわりを近い順に調べる。分からなければNone"""
    points = [(lon, lat)] + [(lon + dx * r, lat + dy * r) for r in _SEARCH_RADII for dx, dy in _SEARCH_DIRECTIONS]
    for x, y in points:
        try:
            resp = requests.get(REVERSE_GEOCODER_URL, params={"lat": f"{y:.6f}", "lon": f"{x:.6f}"}, timeout=5)
            resp.raise_for_status()
            code = ((resp.json() or {}).get("results") or {}).get("muniCd")
        except Exception:
            # 通信できない。まわりを調べても同じなので、すぐ予備の判定に回す
            return None
        if code and str(code).isdigit():
            return int(code)
    return None


def _nearest_origin_zone(lon: float, lat: float, zones):
    """候補の系のうち、原点が最も近い系"""
    scale = math.cos(math.radians(lat))
    return min(zones, key=lambda z: ((lon - ZONE_ORIGINS[z][1]) * scale) ** 2 + (lat - ZONE_ORIGINS[z][0]) ** 2)


def _zone_by_bounds(lon: float, lat: float):
    """予備の判定：ZONE_BOUNDS の範囲に入る系のうち、原点が最も近い系。どの範囲にも入らなければNone"""
    candidates = [zone for zone, (_, lon_min, lat_min, lon_max, lat_max) in ZONE_BOUNDS.items()
                  if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max]
    if not candidates:
        return None
    return _nearest_origin_zone(lon, lat, candidates)


def get_zoningkit_resources():
    """
    CKANのpackage_show APIからもりぞんデータセットのリソース一覧を取得し、
    {zone_number: download_url} の辞書を返す（ZoningKit_XX.zipのみ抽出）。
    """
    resp = requests.get(
        CKAN_PACKAGE_SHOW_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"},
        timeout=30,
    )
    resp.raise_for_status()
    data = json.loads(resp.content.decode("utf-8"))

    if not data.get("success"):
        raise RuntimeError("CKAN APIの応答が不正です")

    resources = {}
    for r in data["result"]["resources"]:
        name = r.get("name", "")
        m = None
        if name.startswith("ZoningKit_") and name.endswith(".zip"):
            try:
                m = int(name[len("ZoningKit_"):-len(".zip")])
            except ValueError:
                m = None
        if m is not None:
            resources[m] = r.get("url")

    return resources


# 地位データを受け取りながら書き出すときの1回の大きさと、進み具合を知らせる間隔（秒）
STREAM_CHUNK_BYTES = 256 * 1024
PROGRESS_INTERVAL = 0.2


class HttpRangeFile:
    """
    HTTP Rangeリクエストで必要なバイト範囲だけを都度取得する、zipfile.ZipFileに
    そのまま渡せるシークル可能なファイルオブジェクト。ZIP全体をダウンロードせず、
    中央ディレクトリと必要なエントリのみをネットワーク越しに読む。

    CKANのダウンロードURLはHEADリクエストを拒否するS3署名付きURLへリダイレクトする
    ため、GETリクエストでリダイレクト解決し、以降は同じ署名付きURLを使い回す
    （署名付きURLは同一URLへの複数回のGET Rangeリクエストを許可することを確認済み）。
    """

    def __init__(self, ckan_url: str, progress_cb=None, cancel_cb=None):
        self._ckan_url = ckan_url
        self._resolved_url = None
        self._size = None
        self._pos = 0
        self._bytes_fetched = 0
        self._progress_cb = progress_cb
        self._cancel_cb = cancel_cb
        # 少しずつ何度も読むので、配信元への接続を使い回す（1回ごとに接続し直す手間を省く）
        self._session = requests.Session()

    def _ensure_resolved(self):
        if self._resolved_url is not None:
            return
        # 範囲を指定して読むので、配信側で圧縮させない（Accept-Encoding: identity）
        with self._session.get(
            self._ckan_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)",
                "Range": "bytes=0-0",
                "Accept-Encoding": "identity",
            },
            timeout=30,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            self._resolved_url = resp.url
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                self._size = int(content_range.rsplit("/", 1)[1])
            else:
                self._size = int(resp.headers.get("Content-Length", 0))

    def _read_range(self, start: int, end: int) -> bytes:
        """[start, end) のバイト範囲を取得する"""
        if self._cancel_cb is not None and self._cancel_cb():
            raise InterruptedError("処理を中断しました。")
        self._ensure_resolved()
        if start >= end:
            return b""
        resp = self._session.get(
            self._resolved_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)",
                "Range": f"bytes={start}-{end - 1}",
                "Accept-Encoding": "identity",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.content
        self._bytes_fetched += len(data)
        if self._progress_cb is not None:
            self._progress_cb(self._bytes_fetched)
        return data

    # -- zipfile.ZipFileが要求するファイルオブジェクトインタフェース --

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        self._ensure_resolved()
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        return self._pos

    def read(self, size=-1):
        self._ensure_resolved()
        if size is None or size < 0:
            end = self._size
        else:
            end = min(self._pos + size, self._size)
        data = self._read_range(self._pos, end)
        self._pos += len(data)
        return data

    def stream_member(self, info: zipfile.ZipInfo, dst):
        """ZIP の中の1ファイルを、1回の問い合わせで受け取りながら展開して dst に書き出す。
        受け取った量（圧縮後）は PROGRESS_INTERVAL 秒おきに progress_cb へ知らせ、受け取るたびに中断を確かめる。
        小さく区切って何度も問い合わせると、この配信元では1回ごとの待ち時間がかさんで遅くなったため
        （2MB ずつで毎秒 0.7MB、1回で受け取ると毎秒 2.5MB 前後）"""
        header = self._read_range(info.header_offset, info.header_offset + 30)
        name_length, extra_length = struct.unpack("<HH", header[26:30])
        start = info.header_offset + 30 + name_length + extra_length
        if info.compress_type == zipfile.ZIP_DEFLATED:
            decompressor = zlib.decompressobj(-15)
        elif info.compress_type == zipfile.ZIP_STORED:
            decompressor = None
        else:
            raise RuntimeError(f"対応していない圧縮形式です: {info.filename}")
        crc = 0
        last_report = 0.0
        with self._session.get(
            self._resolved_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)",
                "Range": f"bytes={start}-{start + info.compress_size - 1}",
                "Accept-Encoding": "identity",
            },
            timeout=30,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(STREAM_CHUNK_BYTES):
                if self._cancel_cb is not None and self._cancel_cb():
                    raise InterruptedError("処理を中断しました。")
                if not chunk:
                    continue
                data = decompressor.decompress(chunk) if decompressor else chunk
                crc = zlib.crc32(data, crc)
                dst.write(data)
                self._bytes_fetched += len(chunk)
                now = time.monotonic()
                if self._progress_cb is not None and now - last_report >= PROGRESS_INTERVAL:
                    last_report = now
                    self._progress_cb(self._bytes_fetched)
        if decompressor:
            data = decompressor.flush()
            crc = zlib.crc32(data, crc)
            dst.write(data)
        if self._progress_cb is not None:
            self._progress_cb(self._bytes_fetched)
        if crc != info.CRC:
            raise RuntimeError(f"受け取ったデータが壊れています: {info.filename}")

    def close(self):
        self._session.close()


def zone_cache_paths(zone: int, cache_base_dir: str) -> dict:
    """座標系zone系のNPP/SRAD/VTEXを置く場所 {種別: パス}"""
    zone_dir = os.path.join(cache_base_dir, f"zone_{zone}")
    return {kind: os.path.join(zone_dir, f"{kind}.tif") for kind in ("NPP", "SRAD", "VTEX")}


def zone_cache_ready(zone: int, cache_base_dir: str) -> bool:
    """座標系zone系のデータを、すでに全部ダウンロードしてあるか"""
    return all(os.path.exists(path) for path in zone_cache_paths(zone, cache_base_dir).values())


def ensure_zone_cache(zone: int, cache_base_dir: str, progress_cb=None, cancel_cb=None) -> dict:
    """
    座標系zone系のNPP/SRAD/VTEX（ゾーン全体分、数十〜数百MB規模）を
    <cache_base_dir>/zone_{zone}/{NPP,SRAD,VTEX}.tifとしてキャッシュする。
    既に全て揃っていれば何もダウンロードせずそのパスを返す（プロジェクトをまたいで再利用）。
    ZIP全体はダウンロードせず、HTTP Rangeで該当ファイルだけをネットワーク越しに取得する。

    progress_cb(bytes_done, bytes_total)は取得中随時呼ばれる
    （bytes_totalは対象メンバーの圧縮サイズ合計から算出した既知の値）。

    Returns:
        {種別: キャッシュ済みファイルパス}
    """
    expected = zone_cache_paths(zone, cache_base_dir)
    zone_dir = os.path.dirname(expected["NPP"])

    if zone_cache_ready(zone, cache_base_dir):
        return expected

    resources = get_zoningkit_resources()
    if zone not in resources:
        raise RuntimeError(f"座標系{zone}系のZoningKitがデータセット内に見つかりません")

    os.makedirs(zone_dir, exist_ok=True)
    result = {}

    # HttpRangeFileの内部バイトカウンタとは別に、ここでは既知の合計サイズに対する
    # 割合として進捗を報告する（progress_cbには(bytes_done, bytes_total)を渡す）
    bytes_total_holder = {"value": 0}

    def on_range_bytes(bytes_fetched_by_httprangefile):
        if progress_cb is not None:
            progress_cb(bytes_fetched_by_httprangefile, bytes_total_holder["value"])

    range_file = HttpRangeFile(resources[zone], progress_cb=on_range_bytes, cancel_cb=cancel_cb)
    with zipfile.ZipFile(range_file) as zf:
        # 先に必要なメンバーとその圧縮サイズの合計を求め、進捗の分母として使う
        members_to_fetch = {}
        for kind in ("NPP", "SRAD", "VTEX"):
            if os.path.exists(expected[kind]):
                continue
            candidates = [
                name for name in zf.namelist()
                if f"/SiteIndex/{kind}/" in name.replace("\\", "/")
                and name.lower().endswith(".tif")
            ]
            if candidates:
                members_to_fetch[kind] = sorted(candidates)[0]
        bytes_total_holder["value"] = sum(
            zf.getinfo(m).compress_size for m in members_to_fetch.values()
        )

        for kind, member in members_to_fetch.items():
            out_path = expected[kind]
            tmp_path = out_path + ".part"
            # 受け取りながら書き出す。1つのファイル（数百MB）をまとめて受け取ると、受け取り終わるまで
            # 進み具合が増えず、中断も効かず、メモリにまるごと載ってしまうため
            with open(tmp_path, "wb") as dst:
                range_file.stream_member(zf.getinfo(member), dst)
            os.replace(tmp_path, out_path)
            result[kind] = out_path

        for kind in ("NPP", "SRAD", "VTEX"):
            if kind not in result and os.path.exists(expected[kind]):
                result[kind] = expected[kind]

    range_file.close()
    return result


def clip_siteindex_to_extent(zone_cache: dict, output_dir: str,
                             lon_min: float, lat_min: float,
                             lon_max: float, lat_max: float) -> dict:
    """
    ゾーン全体分のNPP/SRAD/VTEX（zone_cacheが指すキャッシュ済みファイル）から、
    指定範囲(WGS84)だけを切り出し、もりぞんのフォルダ規則に合わせて
    output_dir/SiteIndex/{NPP,SRAD,VTEX}/*.tifとして保存する（各ファイルは小さくなる）。
    """
    from osgeo import gdal

    result = {}
    for kind, src_path in zone_cache.items():
        dst_dir = os.path.join(output_dir, "SiteIndex", kind)
        os.makedirs(dst_dir, exist_ok=True)
        dst_path = os.path.join(dst_dir, f"{kind.lower()}_clipped.tif")

        # 元データのグリッド(セル境界・解像度)に揃えた範囲で切り出す。
        # 緯度経度の範囲だけを渡すと、GDALが解像度を決め直して再サンプリングになり、
        # セルが正方形でなくなる・グリッドがずれる・元のセルが抜けることがある
        bounds, res_x, res_y = _snap_bounds_to_grid(src_path, lon_min, lat_min, lon_max, lat_max)
        gdal.Warp(
            dst_path, src_path,
            outputBounds=bounds,
            xRes=res_x, yRes=res_y,
            dstSRS=None,  # 元のCRS(ゾーンの平面直角座標系)のまま保持
            resampleAlg="near",
            format="GTiff",
        )
        result[kind] = dst_path

    return result


def _snap_bounds_to_grid(src_path: str, lon_min: float, lat_min: float,
                         lon_max: float, lat_max: float):
    """
    WGS84の範囲を元データのCRSへ変換し、元データのセル境界まで外側へ広げた
    (xmin, ymin, xmax, ymax)と、元データの解像度(x, y)を返す。
    """
    from osgeo import gdal, osr

    ds = gdal.Open(src_path)
    gt = ds.GetGeoTransform()
    src_srs = osr.SpatialReference(wkt=ds.GetProjection())
    ds = None
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(wgs84, src_srs)
    xmin, ymin, xmax, ymax = transform.TransformBounds(lon_min, lat_min, lon_max, lat_max, 21)

    res_x, res_y = gt[1], abs(gt[5])
    xmin = gt[0] + math.floor((xmin - gt[0]) / res_x) * res_x
    xmax = gt[0] + math.ceil((xmax - gt[0]) / res_x) * res_x
    ymax = gt[3] - math.floor((gt[3] - ymax) / res_y) * res_y
    ymin = gt[3] - math.ceil((gt[3] - ymin) / res_y) * res_y
    return (xmin, ymin, xmax, ymax), res_x, res_y


def siteindex_has_data(dem_path: str, siteindex_paths) -> bool:
    """
    DEMの範囲に、NPP/SRAD/VTEXのすべてに値があるセルが1つでもあるかを返す。
    地位指数データ（もりぞんデータセット）は伊豆諸島などの離島を含まない。座標系としてはその系でも、
    データの外の場所では地位指数が全面データなしになり、スコアリング・ゾーニングに進めない。
    森林があっても解析できる場所だと誤解させないよう、それを先に見つけて止めるのに使う
    （市街地などはデータの中でも値が無いが、範囲内に森林が少しでもあれば値がある）。
    """
    import numpy as np
    from osgeo import gdal

    dem = gdal.Open(dem_path)
    gt = dem.GetGeoTransform()
    cols, rows = dem.RasterXSize, dem.RasterYSize
    bounds = (gt[0], gt[3] + rows * gt[5], gt[0] + cols * gt[1], gt[3])
    projection = dem.GetProjection()
    dem = None
    # 地位指数データ（約50m）に合わせた粗い格子で調べる
    options = dict(format="MEM", dstSRS=projection, outputBounds=bounds, xRes=50, yRes=50, dstNodata=np.nan,
                   outputType=gdal.GDT_Float32, resampleAlg="near")
    covered = None
    for path in siteindex_paths:
        ds = gdal.Warp("", path, **options)
        valid = ~np.isnan(ds.GetRasterBand(1).ReadAsArray())
        ds = None
        covered = valid if covered is None else covered & valid
    return bool(covered is not None and covered.any())


SITEINDEX_OUT_OF_RANGE_TITLE = "地位指数データの整備範囲外"


def siteindex_out_of_range_message(dem_fetched: bool) -> str:
    """地位指数データの整備範囲外で止めたときの知らせ。
    dem_fetched: DEMブラウザで取得したDEMを、この知らせの後で解放（削除）するとき True。手で指定したDEMには触らない"""
    message = (
        f"{'取得した' if dem_fetched else '指定した'}DEM範囲では、対になる地位指数データは存在しません。\n"
        "地位指数データ（林野庁「もりぞん」データセット）の整備範囲外です。\n\n"
        "この地域ではスコアリング・ゾーニングまで処理を進めることが出来ないため、ここで中止しました。"
    )
    if dem_fetched:
        message += "\n取得したDEMは使わないため、削除します。"
    return message


def download_zoningkit_zip(zone: int, dest_path: str, progress_cb=None, cancel_cb=None):
    """
    指定ゾーンのZoningKit_XX.zipをdest_pathへダウンロードする。
    既にdest_pathが存在する場合は再ダウンロードしない（キャッシュとして扱う）。
    progress_cb(bytes_done, bytes_total)は受信のたびに呼ばれる
    （サーバーがContent-Lengthを返さない場合、bytes_totalは0になる）。
    """
    if os.path.exists(dest_path):
        return dest_path

    resources = get_zoningkit_resources()
    if zone not in resources:
        raise RuntimeError(f"座標系{zone}系のZoningKitがデータセット内に見つかりません")
    url = resources[zone]

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".part"

    with requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)", "Accept-Encoding": "identity"},
        timeout=30,
        stream=True,
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):  # 1MBずつ
                if cancel_cb is not None and cancel_cb():
                    f.close()
                    os.remove(tmp_path)
                    return None
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)

    os.replace(tmp_path, dest_path)
    return dest_path


def extract_siteindex_rasters(zip_path: str, output_dir: str) -> dict:
    """
    ZoningKit_XX.zip内のDATA/SiteIndex/{NPP,SRAD,VTEX}配下のtifを取り出し、
    output_dir/{NPP,SRAD,VTEX}.tifとして保存する。
    見つかったものだけを{種別: 保存パス}の辞書で返す。
    """
    os.makedirs(output_dir, exist_ok=True)
    result = {}

    with zipfile.ZipFile(zip_path) as zf:
        for kind in ("NPP", "SRAD", "VTEX"):
            candidates = [
                name for name in zf.namelist()
                if f"/SiteIndex/{kind}/" in name.replace("\\", "/")
                and name.lower().endswith(".tif")
            ]
            if not candidates:
                continue
            # 同名フォルダに複数tifがある場合は最初の1つを採用
            member = sorted(candidates)[0]
            out_path = os.path.join(output_dir, f"{kind}.tif")
            with zf.open(member) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            result[kind] = out_path

    return result
