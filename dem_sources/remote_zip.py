"""
ZIP全体をダウンロードせず、central directory と必要なエントリだけを
HTTP Range リクエストで読むための部品（forestry_operations_lite の terrain/remote_zip.py より移植）。

1つのZIPに大量のタイルが入っている配布形式（長野県のDEM）で、必要なタイルだけを取り出すのに使う。
データソース固有の事情（タイル座標・URL索引）は呼び出し側（nagano_*.py）が持つ。

前提: 配布サーバが HTTP Range（206 Partial Content）に対応していること。
2026-09-23、geospatial.jp 経由の S3 配信（CKAN リダイレクト／直リンクとも）で確認済み。
HEAD はこの配信元で 403 になるため使わず、1バイトの Range GET でサイズを取る。
"""

import io
import time
import urllib.error
import urllib.request
import zipfile

from .base import Cancelled

_USER_AGENT = "Mozilla/5.0 (compatible; QGIS plugin MORIZON NEXT)"
_CHUNK_SIZE = 65536


class RemoteZipFile:
    """HTTP Range 経由でランダムアクセスできる file-like オブジェクト。
    zipfile.ZipFile にそのまま渡せる（read/seek/tell のダックタイピング）。

    CKAN 経由の URL は S3 プレサイン URL への 302 リダイレクトを挟むため、
    初回に解決した URL を使い回し、失効したら元の URL から解決し直す。
    読み込みはチャンク単位で cancel_cb を確認する（タイル1枚が百MBを超えることがあるため）。"""

    def __init__(self, url, cancel_cb=None):
        self.url = url
        self._cancel_cb = cancel_cb
        self._resolved_url = url
        self._pos = 0
        self.chunk_cb = None  # エントリ本体を読む直前に呼び出し側が設定する（進捗通知用）
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            self._resolved_url = resp.geturl() or url
            content_range = resp.headers.get("Content-Range")
            if content_range and "/" in content_range:
                self._size = int(content_range.rsplit("/", 1)[-1])
            else:
                size = resp.headers.get("Content-Length")
                if size is None:
                    raise RuntimeError(f"サイズを取得できません: {url}")
                self._size = int(size)

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        return self._pos

    def tell(self):
        return self._pos

    def _open_range(self, url, start, end):
        req = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, "Range": f"bytes={start}-{end}"}
        )
        return urllib.request.urlopen(req, timeout=30)  # nosec B310

    def read(self, n=-1):
        end = (self._size - 1) if (n is None or n < 0) else min(self._pos + n, self._size) - 1
        if end < self._pos:
            return b""
        try:
            resp = self._open_range(self._resolved_url, self._pos, end)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and self._resolved_url != self.url:
                resp = self._open_range(self.url, self._pos, end)
                self._resolved_url = resp.geturl() or self.url
            else:
                raise

        chunks = []
        with resp:
            while True:
                if self._cancel_cb and self._cancel_cb():
                    raise Cancelled()
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                if self.chunk_cb:
                    self.chunk_cb(len(chunk))
        data = b"".join(chunks)
        self._pos += len(data)
        return data


class RemoteZipCatalog:
    """複数のリモートZIPの central directory を1回ずつだけ読んで保持し、
    エントリを探して取り出す。同じZIPに何度もタイルを探しに行く場合に
    central directory の再取得を省く（1回の取得処理の間だけ使う）。"""

    def __init__(self, cancel_cb=None):
        self._cancel_cb = cancel_cb
        self._zips = {}  # url -> (RemoteZipFile, ZipFile) または None（開けなかった）

    def _open(self, url):
        if url not in self._zips:
            try:
                remote = RemoteZipFile(url, cancel_cb=self._cancel_cb)
                self._zips[url] = (remote, zipfile.ZipFile(remote))
            except Cancelled:
                raise
            except Exception:
                self._zips[url] = None
        return self._zips[url]

    def find(self, url, name_suffix):
        """url のZIP内で末尾が name_suffix に一致するエントリ名。無ければ None。"""
        opened = self._open(url)
        if opened is None:
            return None
        for name in opened[1].namelist():
            if name.endswith(name_suffix):
                return name
        return None

    def read(self, url, name, progress_cb=None):
        """エントリを読み込んで bytes を返す。
        progress_cb(downloaded, total) は実転送バイト数（圧縮後サイズ）で、0.15秒間隔に間引いて通知する。"""
        remote, archive = self._open(url)
        total = archive.getinfo(name).compress_size
        downloaded = 0
        last_report = 0.0

        def on_chunk(n):
            nonlocal downloaded, last_report
            downloaded += n
            now = time.monotonic()
            if progress_cb and (now - last_report >= 0.15 or downloaded >= total):
                last_report = now
                progress_cb(downloaded, total)

        remote.chunk_cb = on_chunk
        try:
            return archive.read(name)
        finally:
            remote.chunk_cb = None

    def close(self):
        for opened in self._zips.values():
            if opened is not None:
                opened[1].close()
        self._zips.clear()
