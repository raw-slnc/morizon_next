"""
DEMブラウザで選べるDEM取得元。1ソース=1モジュールで、メイン機能（ブラウザ・取得スレッド）からは
base.DemSource のメソッド（coverage_problem / estimate / fetch）だけを使う。

ソースを追加するとき:
    1. base.DemSource を継承したクラスを新しいモジュールに作る（出典の説明・対象地域の判定・取得）
    2. 下の all_sources() に並べる（並び順がブラウザの表示順）
ブラウザと取得スレッドは変更不要。
"""

from .gsi import GsiSource, TerrariumSource
from .nagano_rinmu import NaganoRinmuSource
from .nagano_sabo import NaganoSaboSource
from .virtual_shizuoka import VirtualShizuokaSource


def all_sources():
    return [
        GsiSource(),
        VirtualShizuokaSource(),
        NaganoSaboSource(),
        NaganoRinmuSource(),
        TerrariumSource(),
    ]
