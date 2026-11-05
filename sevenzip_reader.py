# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""中身が1ファイルの 7z を、Python 標準の lzma で取り出す。

全国森林資源メッシュの 7z は、GDAL の /vsi7z/ で中を読んでいる。ところが /vsi7z/ は libarchive 付きで作られた
GDAL にしか無く、Linux のディストリビューションの GDAL（Ubuntu 24.04 の 3.8.4 など）では使えない。
そのときだけ、ここで取り出す。配布されている形（中身は GeoPackage が1つ、LZMA2 か LZMA か無圧縮、
暗号化なし）だけを扱い、それ以外の形は ValueError にする。
大きな図郭は取り出すと数百MBになるので、少しずつ解凍してファイルに書く（全体をメモリに持たない）。
"""

import lzma
import os
import struct
import zlib

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
CHUNK = 1024 * 1024

# 7z の見出しの項目の番号
_END, _HEADER, _MAIN_STREAMS, _FILES, _PACK_INFO, _UNPACK_INFO, _SUBSTREAMS = 0x00, 0x01, 0x04, 0x05, 0x06, 0x07, 0x08
_SIZE, _CRC, _FOLDER, _UNPACK_SIZE, _NUM_UNPACK_STREAM, _NAME, _ENCODED_HEADER = (
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x11, 0x17)
_METHOD_COPY, _METHOD_LZMA2, _METHOD_LZMA = b"\x00", b"\x21", b"\x03\x01\x01"


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def byte(self):
        if self.pos >= len(self.data):
            raise ValueError("7z の見出しが途中で切れています")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def take(self, count):
        if self.pos + count > len(self.data):
            raise ValueError("7z の見出しが途中で切れています")
        value = self.data[self.pos:self.pos + count]
        self.pos += count
        return value

    def number(self):
        """7z の可変長の数（最初のバイトの上位ビットで、続くバイト数を表す）"""
        first = self.byte()
        mask = 0x80
        value = 0
        for index in range(8):
            if not first & mask:
                return value | ((first & (mask - 1)) << (8 * index))
            value |= self.byte() << (8 * index)
            mask >>= 1
        return value

    def defined_crcs(self, count):
        """「すべてあり」の印か、ビット列で示された分の CRC を読む。ある分だけ値、無い分は None"""
        if self.byte():
            defined = [True] * count
        else:
            defined = []
            current = mask = 0
            for _ in range(count):
                if mask == 0:
                    current, mask = self.byte(), 0x80
                defined.append(bool(current & mask))
                mask >>= 1
        return [struct.unpack("<I", self.take(4))[0] if flag else None for flag in defined]


def _pack_info(reader):
    pack_pos = reader.number()
    count = reader.number()
    sizes = []
    while True:
        item = reader.byte()
        if item == _END:
            return pack_pos, sizes
        if item == _SIZE:
            sizes = [reader.number() for _ in range(count)]
        elif item == _CRC:
            reader.defined_crcs(count)
        else:
            raise ValueError("対応していない 7z の形です（PackInfo）")


def _folder(reader):
    coders = []
    for _ in range(reader.number()):
        flag = reader.byte()
        if flag & 0x10:
            raise ValueError("対応していない 7z の形です（複数の入出力を持つ圧縮）")
        method = reader.take(flag & 0x0F)
        props = reader.take(reader.number()) if flag & 0x20 else b""
        coders.append((method, props))
    return coders


def _unpack_info(reader):
    if reader.byte() != _FOLDER:
        raise ValueError("対応していない 7z の形です（UnPackInfo）")
    count = reader.number()
    if reader.byte() != 0:
        raise ValueError("対応していない 7z の形です（外部のフォルダ情報）")
    folders = [_folder(reader) for _ in range(count)]
    if reader.byte() != _UNPACK_SIZE:
        raise ValueError("対応していない 7z の形です（UnPackInfo）")
    sizes = [[reader.number() for _ in coders] for coders in folders]
    crcs = [None] * count
    while True:
        item = reader.byte()
        if item == _END:
            return folders, sizes, crcs
        if item == _CRC:
            crcs = reader.defined_crcs(count)
        else:
            raise ValueError("対応していない 7z の形です（UnPackInfo）")


def _substreams_info(reader, folder_count):
    """1フォルダに1ファイルの形だけ扱う。ファイルの CRC があれば返す"""
    crcs = None
    while True:
        item = reader.byte()
        if item == _END:
            return crcs
        if item == _NUM_UNPACK_STREAM:
            if any(reader.number() != 1 for _ in range(folder_count)):
                raise ValueError("対応していない 7z の形です（1つの圧縮に複数のファイル）")
        elif item == _CRC:
            crcs = reader.defined_crcs(folder_count)
        else:
            raise ValueError("対応していない 7z の形です（SubStreamsInfo）")


def _streams_info(reader):
    pack = unpack = None
    file_crcs = None
    while True:
        item = reader.byte()
        if item == _END:
            break
        if item == _PACK_INFO:
            pack = _pack_info(reader)
        elif item == _UNPACK_INFO:
            unpack = _unpack_info(reader)
        elif item == _SUBSTREAMS:
            file_crcs = _substreams_info(reader, len(unpack[0]) if unpack else 1)
        else:
            raise ValueError("対応していない 7z の形です（StreamsInfo）")
    if pack is None or unpack is None:
        raise ValueError("対応していない 7z の形です（StreamsInfo）")
    pack_pos, pack_sizes = pack
    folders, sizes, crcs = unpack
    if len(folders) != 1 or len(pack_sizes) != 1:
        raise ValueError("対応していない 7z の形です（複数の圧縮のまとまり）")
    crc = (file_crcs or [None])[0]
    if crc is None:
        crc = crcs[0]
    return {"pack_pos": pack_pos, "pack_size": pack_sizes[0], "coders": folders[0],
            "unpack_size": sizes[0][-1], "crc": crc}


def _decompressor(coders):
    """None なら無圧縮"""
    if len(coders) != 1:
        raise ValueError("対応していない 7z の形です（複数の変換の組み合わせ）")
    method, props = coders[0]
    if method == _METHOD_COPY:
        return None
    if method == _METHOD_LZMA2:
        if not props or props[0] > 40:
            raise ValueError("7z の LZMA2 の設定を読めません")
        bits = props[0]
        dict_size = 0xFFFFFFFF if bits == 40 else (2 | (bits & 1)) << (bits // 2 + 11)
        filters = [{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}]
    elif method == _METHOD_LZMA:
        if len(props) < 5:
            raise ValueError("7z の LZMA の設定を読めません")
        first = props[0]
        filters = [{"id": lzma.FILTER_LZMA1, "lc": first % 9, "lp": (first // 9) % 5, "pb": first // 45,
                    "dict_size": struct.unpack("<I", props[1:5])[0]}]
    else:
        raise ValueError("対応していない 7z の圧縮方式です（LZMA2・LZMA・無圧縮だけ扱えます）")
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)


def _copy_stream(source, stream, write, cancel_cb=None, progress_cb=None):
    """stream（_streams_info の結果）を解凍して write に渡す。CRC を確かめる。中断したら False"""
    source.seek(32 + stream["pack_pos"])
    decompressor = _decompressor(stream["coders"])
    remaining_in = stream["pack_size"]
    total = stream["unpack_size"]
    written = 0
    crc = 0
    while written < total:
        if cancel_cb is not None and cancel_cb():
            return False
        if decompressor is None or decompressor.needs_input:
            chunk = source.read(min(CHUNK, remaining_in)) if remaining_in > 0 else b""
            if not chunk and (decompressor is None or decompressor.needs_input):
                raise ValueError("7z のデータが途中で切れています")
            remaining_in -= len(chunk)
        else:
            chunk = b""
        if decompressor is None:
            out = chunk[:total - written]
        else:
            try:
                out = decompressor.decompress(chunk, max_length=min(CHUNK, total - written))
            except lzma.LZMAError as e:
                raise ValueError(f"7z の中身が壊れています（{e}）") from e
            if not out and decompressor.eof:
                raise ValueError("7z のデータが途中で切れています")
        if out:
            write(out)
            crc = zlib.crc32(out, crc)
            written += len(out)
            if progress_cb is not None:
                progress_cb(written, total)
    if stream["crc"] is not None and crc != stream["crc"]:
        raise ValueError("7z の中身が壊れています（CRC が合いません）")
    return True


def _read_header(source):
    source.seek(0)
    start = source.read(32)
    if len(start) < 32 or start[:6] != SIGNATURE:
        raise ValueError("7z のファイルではありません")
    next_offset, next_size, next_crc = struct.unpack("<QQI", start[12:32])
    source.seek(32 + next_offset)
    header = source.read(next_size)
    if len(header) != next_size or zlib.crc32(header) != next_crc:
        raise ValueError("7z の見出しが壊れています")
    reader = _Reader(header)
    item = reader.byte()
    if item == _ENCODED_HEADER:
        # 見出しそのものが圧縮されている（小さいのでメモリで解凍する）
        parts = []
        _copy_stream(source, _streams_info(reader), parts.append)
        reader = _Reader(b"".join(parts))
        item = reader.byte()
    if item != _HEADER:
        raise ValueError("対応していない 7z の形です（見出し）")
    stream = None
    names = []
    while True:
        item = reader.byte()
        if item == _END:
            break
        if item == _MAIN_STREAMS:
            stream = _streams_info(reader)
        elif item == _FILES:
            names = _file_names(reader, reader.number())
        else:
            raise ValueError("対応していない 7z の形です（見出し）")
    if stream is None or len(names) != 1:
        raise ValueError("対応していない 7z の形です（中身が1ファイルではありません）")
    return names[0], stream


def _file_names(reader, count):
    names = []
    while True:
        item = reader.byte()
        if item == _END:
            return names
        body = reader.take(reader.number())
        if item == _NAME:
            if body[:1] != b"\x00":
                raise ValueError("対応していない 7z の形です（ファイル名）")
            names = body[1:].decode("utf-16-le").split("\x00")[:count]


def single_file_name(path):
    """中のファイルの名前"""
    with open(path, "rb") as source:
        return _read_header(source)[0]


def extract_single(path, dest_path, cancel_cb=None, progress_cb=None):
    """中の1ファイルを dest_path に取り出し、中のファイルの名前を返す。中断したら None（書きかけは消す）。
    扱えない形・壊れているときは ValueError"""
    with open(path, "rb") as source:
        name, stream = _read_header(source)
        part_path = dest_path + ".part"
        try:
            with open(part_path, "wb") as dest:
                done = _copy_stream(source, stream, dest.write, cancel_cb, progress_cb)
        except BaseException:
            if os.path.exists(part_path):
                os.remove(part_path)
            raise
    if not done:
        os.remove(part_path)
        return None
    os.replace(part_path, dest_path)
    return name
