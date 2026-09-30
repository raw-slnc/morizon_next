"""
DEM取得ユーティリティ（forestry_operations_lite の terrain/dem_loader.py より移植）

GSITileDEMLoader: 国土地理院DEMタイル(1m/5m/10m)とAWS Terrarium(全球)を、
緯度経度範囲を指定して取得しnumpy配列に組み立てるクラス。
FOL本体とは実行時の連携・依存を持たない独立実装。
"""

import math
import numpy as np

try:
    from osgeo import gdal
    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False


class GSITileDEMLoader:
    """国土地理院 DEM5A等 PNGタイルを緯度経度範囲で取得する。

    使い方:
        loader = GSITileDEMLoader()
        loader.fetch_for_extent(lon_min, lat_min, lon_max, lat_max)
        # 以降 data / gt / crs_wkt / cell_size が使える
    """

    TILE_SIZE = 256
    WEB_MERCATOR_LIMIT = 20037508.3428
    MAX_MERCATOR_LAT = 85.05112878

    # 解像度優先順: 1m → 5m → 10m
    # (url_template, zoom, label, encoding)
    TILE_SOURCES = [
        ("https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png", 17, "DEM1A 1m",   "gsi"),
        ("https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png", 15, "DEM5A 5m",   "gsi"),
        ("https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",   14, "DEM10B 10m", "gsi"),
    ]

    # AWS Terrain Tiles (Mapzen/Terrarium) — 全球、登録不要
    TERRARIUM_SOURCES = [
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 14, "Terrarium ~2m",  "terrarium"),
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 13, "Terrarium ~5m",  "terrarium"),
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 12, "Terrarium ~10m", "terrarium"),
    ]

    def __init__(self):
        self.data = None
        self.gt = None
        self.crs_wkt = None
        self.cell_size = None
        self.nodata = None
        self.last_errors = []
        self._used_source_label = None
        self._filled_nodata_count = 0
        self._cancelled = False

    @staticmethod
    def _lonlat_to_tile(lon, lat, z):
        n = 2 ** z
        tx = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        ty = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
        return tx, ty

    @classmethod
    def _tile_range(cls, lon_min, lat_min, lon_max, lat_max, zoom):
        max_tile = 2 ** zoom - 1
        x0, y0 = cls._lonlat_to_tile(lon_min, lat_max, zoom)
        x1, y1 = cls._lonlat_to_tile(lon_max, lat_min, zoom)
        x0 = max(0, min(x0, max_tile))
        y0 = max(0, min(y0, max_tile))
        x1 = max(0, min(x1, max_tile))
        y1 = max(0, min(y1, max_tile))
        return x0, y0, x1, y1

    @classmethod
    def check_source_coverage(cls, lon_min, lat_min, lon_max, lat_max, source,
                              cancel_cb=None):
        """指定範囲の全タイルがPNGとして取得できるかを確認する。標高NoDataは判定しない。"""
        import urllib.request
        from qgis.PyQt.QtGui import QImage

        tile_url, tile_zoom, label, _ = source
        x0, y0, x1, y1 = cls._tile_range(lon_min, lat_min, lon_max, lat_max, tile_zoom)
        total = (x1 - x0 + 1) * (y1 - y0 + 1)
        missing = 0
        errors = []
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                if cancel_cb is not None and cancel_cb():
                    return {
                        "source": source,
                        "label": label,
                        "total": total,
                        "missing": total,
                        "cancelled": True,
                        "errors": errors,
                    }
                url = tile_url.format(z=tile_zoom, x=tx, y=ty)
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"},
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                        raw = resp.read()
                    img = QImage()
                    if not img.loadFromData(raw):
                        missing += 1
                        errors.append(f"{label} z={tile_zoom} x={tx} y={ty}: PNG decode failed")
                except Exception as e:
                    missing += 1
                    errors.append(f"{label} z={tile_zoom} x={tx} y={ty}: {e}")
        return {
            "source": source,
            "label": label,
            "total": total,
            "missing": missing,
            "cancelled": False,
            "errors": errors,
        }

    @classmethod
    def check_sources_coverage(cls, lon_min, lat_min, lon_max, lat_max, sources,
                               cancel_cb=None):
        return [
            cls.check_source_coverage(lon_min, lat_min, lon_max, lat_max, source, cancel_cb)
            for source in sources
        ]

    @classmethod
    def _lonlat_to_webmercator(cls, lon, lat):
        lat = max(-cls.MAX_MERCATOR_LAT, min(cls.MAX_MERCATOR_LAT, lat))
        x = lon * cls.WEB_MERCATOR_LIMIT / 180.0
        y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * cls.WEB_MERCATOR_LIMIT / math.pi
        return x, y

    @classmethod
    def _crop_to_requested_extent(cls, data, gt, lon_min, lat_min, lon_max, lat_max):
        """結合済みタイルDEMから、取得要求範囲外のピクセルを落とす。"""
        if data is None or gt is None:
            return data, gt

        x_min, y_min = cls._lonlat_to_webmercator(lon_min, lat_min)
        x_max, y_max = cls._lonlat_to_webmercator(lon_max, lat_max)
        req_x_min, req_x_max = sorted((x_min, x_max))
        req_y_min, req_y_max = sorted((y_min, y_max))

        x_origin, px_w, rot_x, y_origin, rot_y, px_h = gt
        px = abs(px_w)
        rows, cols = data.shape

        col_start = max(0, min(cols, math.ceil((req_x_min - x_origin) / px)))
        col_end = max(0, min(cols, math.floor((req_x_max - x_origin) / px)))
        row_start = max(0, min(rows, math.ceil((y_origin - req_y_max) / px)))
        row_end = max(0, min(rows, math.floor((y_origin - req_y_min) / px)))

        if col_end <= col_start or row_end <= row_start:
            return data, gt

        cropped = data[row_start:row_end, col_start:col_end]
        cropped_gt = (
            x_origin + col_start * px_w,
            px_w,
            rot_x,
            y_origin + row_start * px_h,
            rot_y,
            px_h,
        )
        return cropped, cropped_gt

    @staticmethod
    def _fill_nodata_with_gdal(data):
        if not HAS_GDAL:
            return data, 0
        nodata_mask = np.isnan(data)
        nodata_count = int(nodata_mask.sum())
        if nodata_count == 0:
            return data, 0

        rows, cols = data.shape
        driver = gdal.GetDriverByName("MEM")
        ds = driver.Create("", cols, rows, 1, gdal.GDT_Float32)
        band = ds.GetRasterBand(1)
        working = data.astype(np.float32)
        working[nodata_mask] = -9999.0
        band.SetNoDataValue(-9999.0)
        band.WriteArray(working)

        mask_ds = driver.Create("", cols, rows, 1, gdal.GDT_Byte)
        mask_band = mask_ds.GetRasterBand(1)
        mask_band.WriteArray((~nodata_mask).astype(np.uint8) * 255)
        gdal.FillNodata(band, mask_band, 1000, 0)
        filled = band.ReadAsArray().astype(np.float64)
        filled[filled == -9999.0] = np.nan
        return filled, nodata_count - int(np.isnan(filled).sum())

    @classmethod
    def _fill_nodata(cls, data):
        filled, filled_count = cls._fill_nodata_with_gdal(data)
        if int(np.isnan(filled).sum()) == 0:
            return filled, filled_count

        # GDALで残った場合の保険。隣接有効値の平均を広げる。
        data = filled.copy()
        original_nodata_count = int(np.isnan(data).sum())
        for _ in range(max(data.shape)):
            nodata = np.isnan(data)
            if not nodata.any():
                break
            sums = np.zeros(data.shape, dtype=np.float64)
            counts = np.zeros(data.shape, dtype=np.int16)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    src_r0 = max(0, -dr)
                    src_r1 = data.shape[0] - max(0, dr)
                    src_c0 = max(0, -dc)
                    src_c1 = data.shape[1] - max(0, dc)
                    dst_r0 = max(0, dr)
                    dst_r1 = data.shape[0] - max(0, -dr)
                    dst_c0 = max(0, dc)
                    dst_c1 = data.shape[1] - max(0, -dc)
                    neighbor = data[src_r0:src_r1, src_c0:src_c1]
                    valid = ~np.isnan(neighbor)
                    sums[dst_r0:dst_r1, dst_c0:dst_c1] += np.where(valid, neighbor, 0)
                    counts[dst_r0:dst_r1, dst_c0:dst_c1] += valid
            fillable = nodata & (counts > 0)
            if not fillable.any():
                break
            data[fillable] = sums[fillable] / counts[fillable]
        return data, filled_count + original_nodata_count - int(np.isnan(data).sum())

    @staticmethod
    def _fetch_tile_array(url, encoding="gsi"):
        """URLのPNGタイルを取得し(256, 256)の標高numpy配列を返す。
        失敗時は(None, エラー文字列, 0)を返す。3つ目の戻り値はダウンロードしたバイト数。"""
        import urllib.request
        from qgis.PyQt.QtGui import QImage

        if not url.startswith(("https://", "http://")):
            return None, f"Invalid URL scheme: {url}", 0
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                raw = resp.read()
        except Exception as e:
            return None, f"Connection error: {e}", 0

        try:
            img = QImage()
            if not img.loadFromData(raw):
                return None, "PNG decode failed"
            img = img.convertToFormat(QImage.Format.Format_ARGB32)

            ptr = img.bits()
            nbytes = img.sizeInBytes() if hasattr(img, 'sizeInBytes') else img.byteCount()
            if hasattr(ptr, 'setsize'):
                ptr.setsize(nbytes)
            buf = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((256, 256, 4))
            # ARGB32リトルエンディアン: byte0=B, byte1=G, byte2=R, byte3=A
            r = buf[:, :, 2].astype(np.float64)
            g = buf[:, :, 1].astype(np.float64)
            b = buf[:, :, 0].astype(np.float64)

            if encoding == "terrarium":
                arr = r * 256.0 + g + b / 256.0 - 32768.0
            else:
                u = (r.astype(np.uint32) * 65536
                     + g.astype(np.uint32) * 256
                     + b.astype(np.uint32))
                arr = np.where(u == 0x800000, np.nan,
                               np.where(u < 0x800000, u.astype(np.float64) * 0.01,
                                        (u.astype(np.int32) - 0x1000000).astype(np.float64) * 0.01))

            return arr.astype(np.float64), None, len(raw)
        except Exception as e:
            return None, f"Image processing error: {e}", len(raw)

    def fetch_for_extent(self, lon_min, lat_min, lon_max, lat_max, sources=None, cancel_cb=None,
                          progress_cb=None):
        """WGS84経緯度範囲のタイルをダウンロードして取得する。
        sources省略時はTILE_SOURCES(1m→5m→10m)の順で自動フォールバックする。
        progress_cb(tiles_done, tiles_total, bytes_done)は各タイル取得後に呼ばれる。"""
        self.last_errors = []
        self._used_source_label = None
        self._filled_nodata_count = 0
        self._cancelled = False
        result = None
        source_list = sources or self.TILE_SOURCES
        for i, item in enumerate(source_list):
            tile_url, tile_zoom, label, encoding = item
            candidate = self._fetch_tiles(lon_min, lat_min, lon_max, lat_max,
                                           tile_url, tile_zoom, encoding, cancel_cb=cancel_cb,
                                           progress_cb=progress_cb)
            if candidate is None:
                self._cancelled = True
                return self

            _, _, _, _, missing_tiles, tiles_total = candidate
            if missing_tiles > 0:
                self.last_errors.append(
                    f"{label}: {missing_tiles}/{tiles_total}枚のタイルが取得できません → 次の解像度へ"
                )
                continue

            result = candidate
            self._used_source_label = label
            break
        if result is None:
            return self
        total_arr, zoom_used, x0, y0, _, _ = result
        TS = self.TILE_SIZE

        # EPSG:3857 (Web Mercator) でジオトランスフォームを設定
        WORLD_M = self.WEB_MERCATOR_LIMIT
        tile_m = 2.0 * WORLD_M / (2 ** zoom_used)
        px_m = tile_m / TS

        x_origin = -WORLD_M + x0 * tile_m
        y_origin = WORLD_M - y0 * tile_m
        gt = (x_origin, px_m, 0.0, y_origin, 0.0, -px_m)
        total_arr, gt = self._crop_to_requested_extent(
            total_arr, gt, lon_min, lat_min, lon_max, lat_max
        )
        total_arr, self._filled_nodata_count = self._fill_nodata(total_arr)

        try:
            from osgeo import osr
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(3857)
            crs_wkt = srs.ExportToWkt()
        except Exception:
            crs_wkt = 'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",' \
                      'DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],' \
                      'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],' \
                      'PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],' \
                      'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],' \
                      'PARAMETER["false_northing",0],UNIT["metre",1],' \
                      'AUTHORITY["EPSG","3857"]]'

        center_lat = (lat_min + lat_max) / 2
        corrected_cell_size = px_m * math.cos(math.radians(abs(center_lat)))

        self.data = total_arr
        self.gt = gt
        self.crs_wkt = crs_wkt
        self.cell_size = corrected_cell_size
        self.nodata = None
        return self

    def _fetch_tiles(self, lon_min, lat_min, lon_max, lat_max, tile_url, zoom, encoding="gsi",
                      cancel_cb=None, progress_cb=None):
        max_tile = 2 ** zoom - 1
        TS = self.TILE_SIZE

        x0, y0 = self._lonlat_to_tile(lon_min, lat_max, zoom)
        x1, y1 = self._lonlat_to_tile(lon_max, lat_min, zoom)
        x0 = max(0, min(x0, max_tile))
        y0 = max(0, min(y0, max_tile))
        x1 = max(0, min(x1, max_tile))
        y1 = max(0, min(y1, max_tile))

        nx = x1 - x0 + 1
        ny = y1 - y0 + 1

        MAX_PIXELS = 100_000_000
        if nx * ny * TS * TS > MAX_PIXELS:
            raise MemoryError(
                f"Extent too large (tiles: {nx * ny}, "
                f"pixels: {nx * TS:,} × {ny * TS:,}).\n"
                f"Zoom in or reduce the canvas extent and try again."
            )

        total_arr = np.full((ny * TS, nx * TS), np.nan, dtype=np.float64)

        tiles_total = nx * ny
        tiles_done = 0
        bytes_done = 0
        missing_tiles = 0

        for iy, ty in enumerate(range(y0, y1 + 1)):
            for ix, tx in enumerate(range(x0, x1 + 1)):
                if cancel_cb is not None and cancel_cb():
                    return None
                url = tile_url.format(z=zoom, x=tx, y=ty)
                tile, err, tile_bytes = self._fetch_tile_array(url, encoding)
                tiles_done += 1
                bytes_done += tile_bytes
                if progress_cb is not None:
                    progress_cb(tiles_done, tiles_total, bytes_done)
                if tile is not None:
                    total_arr[iy * TS:(iy + 1) * TS,
                              ix * TS:(ix + 1) * TS] = tile
                elif err:
                    missing_tiles += 1
                    self.last_errors.append(f"z={zoom} x={tx} y={ty}: {err}")

        return (total_arr, zoom, x0, y0, missing_tiles, tiles_total)

    def info_text(self):
        if self.data is None:
            return "Not fetched"
        rows, cols = self.data.shape
        px_m = abs(self.gt[1]) if self.gt else 0
        src = self._used_source_label or "Elevation Tile"
        fill = f"  |  NoData補間 {self._filled_nodata_count} px" if self._filled_nodata_count else ""
        return f"{cols}×{rows} px  |  {src}  |  EPSG:3857  |  {px_m:.1f} m/px{fill}"


def save_as_geotiff(loader, output_path):
    """GSITileDEMLoaderのdata/gt/crs_wktをGeoTIFFに保存する。
    出力先ディレクトリは事前に作成しておくこと。"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if loader.data is None or loader.gt is None:
        raise ValueError("loader has no data or gt set")

    from osgeo import osr

    data = loader.data
    rows, cols = data.shape

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        output_path, cols, rows, 1, gdal.GDT_Float32,
        ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"]
    )
    ds.SetGeoTransform(loader.gt)

    if loader.crs_wkt:
        ds.SetProjection(loader.crs_wkt)
    else:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3857)
        ds.SetProjection(srs.ExportToWkt())

    NODATA = -9999.0
    out_data = data.astype(np.float32)
    out_data[np.isnan(out_data)] = NODATA

    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(out_data)
    band.FlushCache()
    ds.FlushCache()
    ds = None

    return output_path
