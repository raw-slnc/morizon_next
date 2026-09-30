"""
もりぞんZoningKit（NPP/SRAD/VTEX等の地位指数算出用データ）取得ユーティリティ。

配布元：geospatial.jp（もりぞん本体と同じCKANデータセット、GPLv3・登録不要）。
座標系1〜13系（JGD2011 平面直角座標系I〜XIII）のみ提供。14〜19系（南西諸島・
小笠原等）は対象外。
"""

import json
import os
import urllib.request
import zipfile

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


def find_zone_for_point(lon: float, lat: float):
    """
    経度緯度(WGS84)が属する座標系ゾーン番号(1-13)を返す。
    どのゾーンにも属さない場合（14-19系相当の地域等）はNoneを返す。
    複数ゾーンの範囲が重なる場合は番号が若い方を優先する。
    """
    for zone, (_, lon_min, lat_min, lon_max, lat_max) in ZONE_BOUNDS.items():
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return zone
    return None


def get_zoningkit_resources():
    """
    CKANのpackage_show APIからもりぞんデータセットのリソース一覧を取得し、
    {zone_number: download_url} の辞書を返す（ZoningKit_XX.zipのみ抽出）。
    """
    req = urllib.request.Request(
        CKAN_PACKAGE_SHOW_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        data = json.loads(resp.read().decode("utf-8"))

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

    def _ensure_resolved(self):
        if self._resolved_url is not None:
            return
        req = urllib.request.Request(
            self._ckan_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)",
                "Range": "bytes=0-0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            self._resolved_url = resp.geturl()
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
        req = urllib.request.Request(
            self._resolved_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)",
                "Range": f"bytes={start}-{end - 1}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            data = resp.read()
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

    def close(self):
        pass


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
    zone_dir = os.path.join(cache_base_dir, f"zone_{zone}")
    expected = {kind: os.path.join(zone_dir, f"{kind}.tif") for kind in ("NPP", "SRAD", "VTEX")}

    if all(os.path.exists(p) for p in expected.values()):
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
            with zf.open(member) as src, open(tmp_path, "wb") as dst:
                dst.write(src.read())
            os.replace(tmp_path, out_path)
            result[kind] = out_path

        for kind in ("NPP", "SRAD", "VTEX"):
            if kind not in result and os.path.exists(expected[kind]):
                result[kind] = expected[kind]

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

        gdal.Warp(
            dst_path, src_path,
            outputBounds=(lon_min, lat_min, lon_max, lat_max),
            outputBoundsSRS="EPSG:4326",
            dstSRS=None,  # 元のCRS(ゾーンの平面直角座標系)のまま保持
            format="GTiff",
        )
        result[kind] = dst_path

    return result


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

    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(tmp_path, "wb") as f:
            while True:
                if cancel_cb is not None and cancel_cb():
                    f.close()
                    os.remove(tmp_path)
                    return None
                chunk = resp.read(1024 * 1024)  # 1MBずつ
                if not chunk:
                    break
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
