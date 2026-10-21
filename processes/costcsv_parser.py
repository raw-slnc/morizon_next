# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import csv

# 表の形（原版のExcelひな形・手引 5.5 と同じ）。マスの数は固定で、区切りの値だけが変わり得る
# （集材作業効率を「傾斜と地形の複雑さ」で計算するときは、縦軸の区切りを地形の複雑さの値に差し替える：手引 p.107）
N_ROWS = 10           # 縦軸（起伏量または地形の複雑さ）の区間の数
N_COLS = 6            # 横軸（傾斜）の区間の数
LOWER_FIRST_ROW = 15  # 下の表（コードと機材名）の先頭の行（0始まり。ファイルの16行目）
CODES = range(0, 11)  # 機材のコード


def read_costcsv_rows(csv_filepath: str) -> list:
    """作業システムCSVを文字列の行の配列として読む。UTF-8（BOM付きも可）か Shift_JIS（CP932）。
    UTF-8 を先に試す（Shift_JIS の日本語は UTF-8 として読めないため取り違えない。BOM は取り除く）"""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            with open(csv_filepath, encoding=encoding, newline="") as f:
                return [[cell.strip() for cell in row] for row in csv.reader(f)]
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判別できません（Shift_JIS か UTF-8 で保存してください）")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def check_costcsv_rows(rows, lower_fixed=True) -> dict:
    """作業システムCSVの形を調べ、誤りがあれば何行目の何が悪いかを ValueError で知らせる。
    区切りの値は問わない（数として読め、順に並んでいればよい）。
    lower_fixed：下の表（コードと機材名）を原版の解析と同じく16〜26行目に限るか。False なら上の表より下を探し、
    見つからないコードの機材名は空にする（Excelの「CSVで出力」シートを読むとき。空の行が保たれるとは限らないため）。
    戻り値は {"row_thresholds": 縦軸の区切り（昇順11個）, "col_thresholds": 傾斜の区切り（7個）,
    "scores_ascending": 縦軸の昇順に並べたコード（10x6）, "names": {コード: 機材名}}"""
    if len(rows) < N_ROWS + 1:
        raise ValueError("上の表の行が足りません（1〜11行目に、見出しの1行と区切り10行が必要です）")
    header = [_number(cell) for cell in rows[0][:N_COLS + 2]]
    if len(header) < N_COLS + 2 or any(value is None for value in header):
        raise ValueError("1行目（見出し）：先頭のマスと傾斜の区切り7つが数として読めません")
    slopes = header[1:]
    if any(a >= b for a, b in zip(slopes, slopes[1:])):
        raise ValueError("1行目（見出し）：傾斜の区切りが小さい順に並んでいません")

    labels = []
    scores_descending = []
    for index in range(1, N_ROWS + 1):
        row = rows[index]
        line = index + 1
        label = _number(row[0]) if row else None
        if label is None:
            raise ValueError(f"{line}行目：先頭のマス（縦軸の区切り）が数として読めません")
        values = [_number(cell) for cell in row[1:N_COLS + 1]]
        if len(values) < N_COLS:
            raise ValueError(f"{line}行目：マスが足りません（区切りのほかに{N_COLS}マス必要です）")
        for column, value in enumerate(values, start=2):
            if value is None or value != int(value) or int(value) not in CODES:
                raise ValueError(f"{line}行目の{column}列目：0〜10の整数ではありません（空欄も不可）")
        labels.append(label)
        scores_descending.append([int(value) for value in values])
    if any(a <= b for a, b in zip(labels, labels[1:])) or labels[0] >= header[0]:
        raise ValueError("1〜11行目の先頭の列：縦軸の区切りが大きい順に並んでいません（1行目の先頭が最大）")

    names = {}
    if not lower_fixed:
        for row in rows[N_ROWS + 1:]:
            code = _number(row[0]) if row else None
            if code is not None and code == int(code) and int(code) in CODES and len(row) >= 2 and row[1]:
                names[int(code)] = row[1]
        return {
            "row_thresholds": list(reversed(labels)) + [header[0]],
            "col_thresholds": slopes,
            "scores_ascending": list(reversed(scores_descending)),
            "names": names,
        }

    if len(rows) < LOWER_FIRST_ROW + len(CODES):
        raise ValueError("下の表（16〜26行目のコードと機材名）がありません")
    for index in range(LOWER_FIRST_ROW, LOWER_FIRST_ROW + len(CODES)):
        row = rows[index]
        line = index + 1
        code = _number(row[0]) if row else None
        if code is None or code != int(code) or int(code) not in CODES or len(row) < 2:
            raise ValueError(f"{line}行目：コード（0〜10）と機材名の2列になっていません")
        names[int(code)] = row[1]
    missing = [code for code in CODES if code not in names]
    if missing:
        raise ValueError(f"下の表（16〜26行目）：コード {'・'.join(str(code) for code in missing)} がありません")

    return {
        "row_thresholds": list(reversed(labels)) + [header[0]],
        "col_thresholds": slopes,
        "scores_ascending": list(reversed(scores_descending)),
        "names": names,
    }


class CostcsvParser:
    """
    下記のような構造の作業システムCSVをパースするクラス

    550, 10, 15, 20, 25, 30, 35, 40
    500,  0,  0,  7,  2,  2,  2,
    450,  0,  0,  7,  2,  2,  2,
    400,  0,  0,  7,  2,  2,  2,
    350,  0,  7,  7,  5,  2,  2,
    300,  0,  7,  7,  5,  2,  2,
    250, 10,  8,  7,  5,  3,  2,
    200, 10,  8,  7,  5,  3,  2,
    150, 10,  9,  8,  6,  4,  0,
    100, 10,  9,  8,  6,  4,  0,
    50,  10,  9,  9,  0,  0,  0,



    code, name
    10, CTL
     9, 9-13tグラップル
     8, 9-13tウィンチ
     7, 9-13tスイングヤーダ
     6, 6-8tウィンチ
     5, 6-8tスイングヤーダ
     4, 3-4tウィンチ
     3, タワーヤーダ
     2, 本架線
     1, 該当なし
     0, 該当なし


    左端の列の全要素は起伏量のしきい値: 50-550
    1列目を除く1行目の全要素は傾斜のしきい値を示す: 10-40

    2つの要素に対し各セルでスコアが設定されている
    例: 起伏量151, 傾斜16 -> スコア9
    例: 起伏量400, 傾斜16 -> スコア0

    表の欄外のスコアはゼロ
    例: 起伏量550, 傾斜16 -> スコア0
    例: 起伏量49, 傾斜16 -> スコア0
    例: 起伏量151, 傾斜40 -> スコア0

    """

    def __init__(self, csv_filepath: str):
        rows = read_costcsv_rows(csv_filepath)
        # 形が崩れたCSVを黙って読むと、表に無い範囲が0点（作業不可）になったり、原因の分からない
        # エラーになったりするため、先に調べて何行目が悪いかを知らせる
        try:
            check_costcsv_rows(rows)
        except ValueError as e:
            raise ValueError(f"作業システムCSVの形が正しくありません。{e}") from e

        # 上表と下表を取り出す（マスは10x6で固定。原版は行末の空欄を前提に row[1:-1] で取っていた）
        upper_rows = rows[0:N_ROWS + 1]
        lower_rows = rows[LOWER_FIRST_ROW:LOWER_FIRST_ROW + len(CODES)]

        self._thresholds_ruggedness = list(
            map(lambda row: row[0], upper_rows))
        self._thresholds_ruggedness.reverse()

        self._thresholds_slope = upper_rows[0][1:N_COLS + 2]

        self._scores = list(map(lambda row: row[1:N_COLS + 1], upper_rows[1:]))
        self._scores.reverse()

        self._score_dict = {}
        for row in lower_rows:
            self._score_dict[row[0]] = row[1]

    def _get_score_tuples(self):
        score_tuples = []
        for i in range(len(self._thresholds_ruggedness) - 1):
            for j in range(len(self._thresholds_slope) - 1):
                score_tuple = (self._thresholds_ruggedness[i],
                               self._thresholds_ruggedness[i + 1],
                               self._thresholds_slope[j],
                               self._thresholds_slope[j + 1],
                               self._scores[i][j])
                score_tuples.append(score_tuple)
        return score_tuples

    def generate_expression_for_raster_calculator(self,
                                                  ruggedness_entry_name: str,
                                                  slope_entry_name: str) -> str:
        """
        CSVをパースした結果をもとにラスター計算機のExpressionを生成する

        Args:
            ruggedness_entry_name (str):
            slope_entry_name (str):

        Returns:
            str: Expression文字列
        """

        expressions = []

        for score_tuple in self._get_score_tuples():
            expression = (
                f'{score_tuple[4]} * ('
                f'{score_tuple[0]} <= "{ruggedness_entry_name}" and "{ruggedness_entry_name}" < {score_tuple[1]}'
                f' and {score_tuple[2]} <= "{slope_entry_name}" and "{slope_entry_name}" < {score_tuple[3]})'
            )
            expressions.append(expression)

        return ' + '.join(expressions)

    def get_score_names(self) -> list:
        """
        作業システムスコアの整数値0-10に対応する文字列を配列で返す

        Returns:
            [str]: スコアに対応する文字列の配列, インデックス=スコア
        """
        sorted_by_key = sorted(self._score_dict.items(),
                               key=lambda kv: int(kv[0]))
        return list(map(lambda row: row[1], sorted_by_key))
