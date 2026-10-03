# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# MORIZON管理フォルダの構成
# プロジェクトフォルダ内の作業場所。原版の『ZoningKit_○○』（手引 図3-13）と同じ構成にし、
# 「保存ファイル出力」のZIPも同じ構成で書き出す（そのまま展開・読み直しできるように）
MANAGED_DIR_NAME = "morizon_next"
DIR_DATA = "DATA"            # 入力（DEM/・SiteIndex/・ROAD/・TATEMONO/・SAGYO-SYSTEM_CSV/）
DIR_YOUSO = "YOUSO"          # 要素計算の出力
DIR_ZONING = "ZONING"        # スコアリング（収益性・災害リスク）とゾーニングの出力
DIR_AGGREGATE = "AGGREGATE"  # 集計の出力（原版には無い。MORIZON NEXTで追加）
DIR_SHARED = "shared"        # 解析をまたいで使い回すキャッシュ。保存・破棄の対象外
DATA_INFO_FILE_NAME = "MORIZON_NEXT.txt"  # 保存データの最上位に置く説明
# 出力レイヤーをまとめるレイヤーツリーのグループ（プロジェクトの最上位）
OUTPUT_GROUP_NAME = "Morizon Next"

# 作業システムのExcelひな形
# 原版 ZoningKit 同梱の「集材作業効率の設定220113.xlsx」（ZoningKit_03〜13・サンプルとも同一の配布原本）。
# プラグイン内はASCII名で持ち、プロジェクトへは原版と同じ名前で置く
COSTCSV_TEMPLATE_FILE = "templates/sagyo_system_template_220113.xlsx"
COSTCSV_TEMPLATE_NAME = "集材作業効率の設定220113.xlsx"

# 入力ファイル定義
INPUT_DEM = {
    "DISPLAY_NAME": "DEM",
    "EXT": "tif",
    "PATH": ["DEM"]
}
INPUT_NPP = {
    "DISPLAY_NAME": "NPP",
    "EXT": "tif",
    "PATH": ["SiteIndex", "NPP"]
}
INPUT_SRAD = {
    "DISPLAY_NAME": "SRAD",
    "EXT": "tif",
    "PATH": ["SiteIndex", "SRAD"]
}
INPUT_VTEX = {
    "DISPLAY_NAME": "VTEX",
    "EXT": "tif",
    "PATH": ["SiteIndex", "VTEX"]
}
INPUT_BUILDING = {
    "DISPLAY_NAME": "建物ポリゴン",
    "EXT": "shp",
    "PATH": ["TATEMONO"]
}
INPUT_NETWORK = {
    "DISPLAY_NAME": "既設路網ライン",
    "EXT": "shp",
    "PATH": ["ROAD"]
}
INPUT_COSTCSV = {
    "DISPLAY_NAME": "作業システムCSV",
    "EXT": "csv",
    "PATH": ["SAGYO-SYSTEM_CSV"]
}

# 出力ファイル定義
OUTPUT_SLOPE = {
    "DISPLAY_NAME": "災害リスク/傾斜",
    "FILE_NAME": "Y_12_keisha",
}
OUTPUT_DISTANCE = {
    "DISPLAY_NAME": "収益性/地利",
    "FILE_NAME": "Y_03_chiri",
}
OUTPUT_SITEIDX_SUGI = {
    "DISPLAY_NAME": "収益性/地位（スギ）",
    "FILE_NAME": "Y_01_chii_sugi",
}
OUTPUT_SITEIDX_HINOKI = {
    "DISPLAY_NAME": "収益性/地位（ヒノキ）",
    "FILE_NAME": "Y_01_chii_hinoki",
}
OUTPUT_SITEIDX_KARAMATSU = {
    "DISPLAY_NAME": "収益性/地位（カラマツ）",
    "FILE_NAME": "Y_01_chii_karamatsu",
}
OUTPUT_SAVEAREA = {
    "DISPLAY_NAME": "災害リスク/保全対象を含む流域",
    "FILE_NAME": "Y_13_hozen",
}
OUTPUT_COST = {
    "DISPLAY_NAME": "収益性/集材作業効率",
    "FILE_NAME": "Y_02_shuzai",
}
OUTPUT_SHC = {
    "DISPLAY_NAME": "災害リスク/地形の複雑さ",
    "FILE_NAME": "Y_11_chikei",
}
OUTPUT_PARAMS_JSON = {
    "FILE_NAME": "params",
    "EXTENSION": "json"
}
OUTPUT_PROFIT = {
    "DISPLAY_NAME": "収益性",
    "FILE_NAME": "shuekisei",
    "EXTENSION": "tif"
}
OUTPUT_RISK = {
    "DISPLAY_NAME": "災害リスク",
    "FILE_NAME": "saigairisk",
    "EXTENSION": "tif"
}
OUTPUT_ZONING = {
    "DISPLAY_NAME": "ゾーニング図",
    "FILE_NAME": "zoning",
    "EXTENSION": "tif"
}
OUTPUT_ZONING_THRESHOLDS_JSON = {
    "FILE_NAME": "thresholds",
    "EXTENSION": "json"
}
OUTPUT_AGGREGATE = {
    "DISPLAY_NAME": "ゾーン統計量",
    "FILE_NAME": "aggregate"
}

# 各レイヤーの色定義
RAWDATA_COLORS_SITEIDX_SUGI = (
    "#cdf1c5", "#a3d5a6", "#7ab987", "#509c68", "#268049")
RAWDATA_COLORS_SITEIDX_HINOKI = (
    "#cdf1c5", "#a3d5a6", "#7ab987", "#509c68", "#268049")
RAWDATA_COLORS_SITEIDX_KARAMATSU = (
    "#cdf1c5", "#a3d5a6", "#7ab987", "#509c68", "#268049")
RAWDATA_COLORS_DISTANCE = (
    "#e8eff6", "#c6d8ee", "#a4c0e6", "#81a8de", "#5f91d6", "#3d79cd")
RAWDATA_COLORS_COST = ("#e7e7ff", "#bed7f8", "#95c6f0", "#6cb5e8", "#43a4e0",
                       "#4fc780", "#7be544", "#e3fa4a", "#fece4b", "#fe8c4c", "#ff4d50")
RAWDATA_COLORS_SAVEAREA = ("#98e6ff", "#ffb47f")
RAWDATA_COLORS_SLOPE = ("#6cabd0", "#b5dee4", "#ecf7c9",
                        "#ffdf99", "#feb367", "#f08856", "#d74043")
RAWDATA_COLORS_SHC = ("#f5fff0", "#c5eccb", "#95d9a6", "#66c681", "#36b35b")

SCORING_COLORS_SITEIDX = ("#0000ff", "#00d700", "#ffff00")
SCORING_COLORS_DISTANCE = ("#ffff00", "#00d700", "#0000ff")
SCORING_COLORS_COST = ("#0000ff", "#00d700", "#ffff00")
SCORING_COLORS_SAVEAREA = ("#0000ff", "#ff55ff")
SCORING_COLORS_SLOPE = ("#0000ff", "#a0ffff", "#ff55ff")
SCORING_COLORS_SHC = ("#0000ff", "#a0ffff", "#ff55ff")
SCORING_COLORS_PROFIT = ("#00d7ff", "#ffff00")
SCORING_COLORS_RISK = ("#00d7ff", "#ff55ff")

ZONING_COLORS = ("#ffbb80", "#99ef80", "#1adbff", "#80a7ff")

# 1mDEMをリサンプリングする際の画素数のしきい値
PIXELS_THRESHOLD_RESAMPLING = 25000000
