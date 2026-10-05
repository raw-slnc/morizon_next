# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import csv


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
        rows = None
        try:
            with open(csv_filepath, encoding='cp932') as f:
                reader = csv.reader(f)
                rows = [row for row in reader]
        except UnicodeDecodeError as e:
            print(e)
            print("Shift-JISでデコードエラー、UTF-8を試行...")
            with open(csv_filepath, encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = [row for row in reader]

        # 上表と下表を取り出す
        upper_rows = rows[0:11]
        lower_rows = rows[15:26]

        self._thresholds_ruggedness = list(
            map(lambda row: row[0], upper_rows))
        self._thresholds_ruggedness.reverse()

        self._thresholds_slope = upper_rows[0][1:]

        self._scores = list(map(lambda row: row[1:-1], upper_rows[1:]))
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
