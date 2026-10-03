# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import bisect
import csv
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QImage, QPixmap, QPainter, QColor, QPen
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QTextBrowser, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QSlider, QSizePolicy,
)

from .constants import RAWDATA_COLORS_COST

# もりぞん実データ(ZoningKit_08 集材作業効率の設定220113.xlsx「入力例」シート)から実測した値
RUGGEDNESS_THRESHOLDS = [0, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550]
SLOPE_THRESHOLDS = [0, 15, 20, 25, 30, 35, 40]

# 起伏量の昇順(行0=0-100側)で並べたスコア(機材コード0-10)。実データそのまま
DEFAULT_SCORES_ASCENDING = [
    [10, 9, 9, 6, 4, 2],   # 0-100
    [10, 9, 8, 6, 4, 2],   # 100-150
    [10, 9, 8, 6, 4, 2],   # 150-200
    [10, 8, 7, 5, 3, 2],   # 200-250
    [10, 8, 7, 5, 3, 2],   # 250-300
    [7, 7, 7, 5, 2, 2],    # 300-350
    [7, 7, 7, 5, 2, 2],    # 350-400
    [7, 7, 7, 2, 2, 2],    # 400-450
    [7, 7, 7, 2, 2, 2],    # 450-500
    [7, 7, 7, 2, 2, 2],    # 500-550
]

# コード降順(10→0)。コードは数値自体に意味は無く、機材名への参照キーに過ぎない
DEFAULT_LEGEND = [
    (10, "CTL"),
    (9, "9-13tグラップル"),
    (8, "9-13tウィンチ"),
    (7, "9-13tスイングヤーダ"),
    (6, "6-8tウィンチ"),
    (5, "6-8tスイングヤーダ"),
    (4, "3-4tウィンチ"),
    (3, "タワーヤーダ"),
    (2, "本架線"),
    (1, "未設定"),
    (0, "未設定"),
]

# 作業マージン: 傾斜(度)を実際よりどれだけ緩やかに(厳しく)見なすかのシフト量。
# 機材の物理的な限界がある以上、この余地はごくわずかであるべき(ユーザー指示により±5度を上限とする)
MARGIN_DEGREES_MAX = 5


def read_costcsv(csv_path: str):
    """作業システムCSV（原版のExcelひな形「CSVで出力」シート、またはこのエディタの書き出し）を読み、
    (起伏量昇順のスコア10x6, {コード: 機材名}) を返す。
    このエディタは起伏量・傾斜のしきい値が標準（手引・ひな形と同じ）であることを前提に描くため、
    しきい値が違うCSVは ValueError にする（解析にはそのまま使える）。"""
    rows = None
    for encoding in ("cp932", "utf-8-sig"):
        try:
            with open(csv_path, encoding=encoding, newline="") as f:
                rows = [[cell.strip() for cell in row] for row in csv.reader(f)]
            break
        except UnicodeDecodeError:
            continue
    if rows is None:
        raise ValueError("文字コードを判別できません（Shift_JIS か UTF-8 で保存してください）")

    def to_number(text):
        try:
            return float(text)
        except ValueError:
            return None

    n_rows = len(RUGGEDNESS_THRESHOLDS) - 1
    n_cols = len(SLOPE_THRESHOLDS) - 1
    if len(rows) < n_rows + 1:
        raise ValueError("表の行数が足りません")
    header = [to_number(cell) for cell in rows[0][:n_cols + 2]]
    labels = [to_number(row[0]) if row else None for row in rows[1:n_rows + 1]]
    if header != [RUGGEDNESS_THRESHOLDS[-1]] + SLOPE_THRESHOLDS or labels != RUGGEDNESS_THRESHOLDS[-2::-1]:
        raise ValueError("起伏量・傾斜のしきい値が標準と異なるため、表示には反映できません（解析にはそのまま使えます）")

    scores_descending = []
    for row in rows[1:n_rows + 1]:
        values = [to_number(cell) for cell in row[1:n_cols + 1]]
        if len(values) < n_cols or any(v is None or v != int(v) or not 0 <= v <= 10 for v in values):
            raise ValueError("表に0〜10以外の値か空欄があります")
        scores_descending.append([int(v) for v in values])

    # 表の下の「コード, 機材名」（見出し行の有無はどちらでもよい）
    names = {}
    for row in rows[n_rows + 1:]:
        if len(row) >= 2 and to_number(row[0]) is not None and row[1]:
            code = int(to_number(row[0]))
            if 0 <= code <= 10:
                names[code] = row[1]
    return list(reversed(scores_descending)), names


class ElidedLabel(QLabel):
    """入りきらない文言は途中を「…」で省略して表示するラベル（省略したときは全文をツールチップに出す）。
    読み込んだCSVのファイル名が長くても、一覧の幅を広げないようにするため。
    elide_part を渡すと、その部分（ファイル名）だけを省略し、前後の文言は残す。
    tooltip を渡すと、省略の有無にかかわらずそれをツールチップに出す（読み込んだファイルの場所など）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self._elide_part = None
        self._tooltip = None
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text, elide_part=None, tooltip=None):
        self._full_text = text or ""
        self._tooltip = tooltip
        self._elide_part = elide_part if elide_part and elide_part in self._full_text else None
        self._update_elided()

    def text(self):
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        metrics = self.fontMetrics()
        width = max(self.width(), 0)
        if self._elide_part:
            prefix, suffix = self._full_text.split(self._elide_part, 1)
            room = width - metrics.horizontalAdvance(prefix + suffix)
            elided = prefix + metrics.elidedText(self._elide_part, Qt.TextElideMode.ElideMiddle, max(room, 0)) + suffix
        else:
            elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, width)
        super().setText(elided)
        if self._tooltip:
            self.setToolTip(self._tooltip)
        else:
            self.setToolTip(self._full_text if elided != self._full_text else "")


class CostCsvEditorWidget(QWidget):
    """
    作業システムCSV（起伏量×傾斜と機材コードの対応）を編集し、
    CostcsvParserが読める形式のCSVへ書き出すウィジェット。

    グリッド表は見た目上の格子に過ぎず本質ではないため廃止し、代わりに
    (傾斜, 起伏量)平面上の色分布パネルとして表示する。実際に現場で調整余地が
    あるのは「①機材ランキング(どの機材を持っているか)」と、パターン全体を
    どこまで強気に地形の厳しい側へ押し出すかという「作業マージン」の2つだけ、
    という理解に基づく（2026-09-30、ユーザーとの議論により設計）。
    """

    PLOT_WIDTH = 150
    PLOT_HEIGHT = 150
    GUTTER_LEFT = 38
    GUTTER_RIGHT = 14
    GUTTER_BOTTOM = 16
    SETTINGS_GROUP = "/MORIZON/costcsv_editor"
    SETTINGS_DISABLED_CODES = "disabled_codes"
    SETTINGS_MARGIN = "margin"

    def __init__(self, parent=None, on_export=None, on_open_template=None, on_import=None):
        super().__init__(parent)
        self._on_export = on_export
        # 起伏量昇順(行0=0-100側)で保持する、60マス分の基準スコア(機材コード0-10)
        self._base_scores = [row[:] for row in DEFAULT_SCORES_ASCENDING]
        self._margin = 0.0
        # 保有していない機材のコード集合。該当マスは0(該当なし)として扱う
        self._disabled_codes = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel(
            "横軸=傾斜、縦軸=起伏量。色は右の機材名一覧に対応します。"
        ))
        columns_row = QHBoxLayout()

        left_col = QVBoxLayout()
        # 一覧の見出しの行に、いま表示しているパターンの出どころ（標準パターンか、読み込んだCSVか）を
        # 一覧の幅の中で右寄せに並べる
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("機材名の一覧（色は右のパネルに対応。名前は編集できます）"))
        self.sourceLabel = ElidedLabel()
        self.sourceLabel.setStyleSheet("color:#555;")
        self.sourceLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.sourceLabel, stretch=1)
        left_col.addLayout(title_row)
        legend_grid = QGridLayout()
        legend_grid.setHorizontalSpacing(6)
        legend_grid.setVerticalSpacing(2)
        self._legend_codes = [code for code, _ in DEFAULT_LEGEND]
        self.legendSwatches = []
        self.legendNameEdits = []
        n_sub_rows = (len(DEFAULT_LEGEND) + 1) // 2  # 11項目を2列(6+5)に分ける
        for i, (code, name) in enumerate(DEFAULT_LEGEND):
            sub_col = i // n_sub_rows
            row = i % n_sub_rows
            col_base = sub_col * 2

            swatch = QLabel()
            swatch.setFixedSize(16, 16)
            if code > 0:
                swatch.setCursor(Qt.CursorShape.PointingHandCursor)
                swatch.setToolTip("クリックでこの機材を保有していない扱いにする")
                swatch.mousePressEvent = lambda event, c=code: self._toggle_code(c)
            legend_grid.addWidget(swatch, row, col_base)
            self.legendSwatches.append(swatch)

            name_edit = QLineEdit(name)
            name_edit.setFixedHeight(18)
            legend_grid.addWidget(name_edit, row, col_base + 1)
            self.legendNameEdits.append(name_edit)

        # 右側サブカラム(項目数が1つ少ない)の余った最終行に作業マージンを収める。
        # スウォッチ用の狭い列(2)にラベルを置くと列幅が広がってしまうため、2列分(2,3)をまたがせる
        margin_row_index = n_sub_rows - 1
        margin_row = QHBoxLayout()
        margin_row.setSpacing(4)
        margin_row.addWidget(QLabel("マージン"))
        self.marginSlider = QSlider(Qt.Orientation.Horizontal)
        self.marginSlider.setMinimum(-MARGIN_DEGREES_MAX)
        self.marginSlider.setMaximum(MARGIN_DEGREES_MAX)
        self.marginSlider.setValue(0)
        self.marginSlider.valueChanged.connect(self._on_margin_changed)
        margin_row.addWidget(self.marginSlider, stretch=1)
        self.marginValueLabel = QLabel("0°")
        self.marginValueLabel.setFixedWidth(28)
        margin_row.addWidget(self.marginValueLabel)
        legend_grid.addLayout(margin_row, margin_row_index, 2, 1, 2)

        left_col.addLayout(legend_grid)
        left_col.addStretch(1)
        columns_row.addLayout(left_col, stretch=1)

        right_col = QVBoxLayout()
        self.panelLabel = QLabel()
        self.panelLabel.setFixedSize(
            self.GUTTER_LEFT + self.PLOT_WIDTH + self.GUTTER_RIGHT, self.PLOT_HEIGHT + self.GUTTER_BOTTOM
        )
        right_col.addWidget(self.panelLabel)
        right_col.addStretch(1)
        columns_row.addLayout(right_col, stretch=1)

        layout.addLayout(columns_row)

        button_row = QHBoxLayout()
        self.loadDefaultButton = QPushButton("初期値を読み込む")
        self.loadDefaultButton.clicked.connect(lambda: self.load_defaults())
        button_row.addWidget(self.loadDefaultButton)
        self.exportButton = QPushButton("CSVとして設定に反映する")
        self.exportButton.setToolTip("表示中のパターンをCSVに書き出し、作業システムCSV欄に設定します")
        self.exportButton.clicked.connect(self._on_export_clicked)
        button_row.addWidget(self.exportButton)
        # 地域の作業システムを細かく決めたい場合は、原版のExcelひな形で作ったCSVを読み込む
        self.openTemplateButton = QPushButton("デフォルトExcelを開く")
        self.openTemplateButton.setToolTip(
            "原版の「集材作業効率の設定」Excelを、プロジェクトの DATA/SAGYO-SYSTEM_CSV フォルダに置いて開きます"
        )
        if on_open_template:
            self.openTemplateButton.clicked.connect(on_open_template)
        button_row.addWidget(self.openTemplateButton)
        self.importButton = QPushButton("CSVインポート")
        self.importButton.setToolTip(
            "作業システムCSVのパターンと機材名を表示に読み込みます（設定への反映は「CSVとして設定に反映する」で行います）"
        )
        if on_import:
            self.importButton.clicked.connect(on_import)
        button_row.addWidget(self.importButton)
        # 凡例で機材を選び、パネルをクリック・ドラッグして割り当てる機能の予定地（未実装）
        self.interactiveButton = QPushButton("インタラクティブモード（準備中）")
        self.interactiveButton.setToolTip("準備中の機能です。押すと予定している内容を表示します")
        self.interactiveButton.clicked.connect(self._show_interactive_plan)
        button_row.addWidget(self.interactiveButton)
        layout.addLayout(button_row)

        self.load_defaults(persist=False)
        self._disabled_codes = self._load_disabled_codes()
        self.marginSlider.setValue(self._load_margin())
        self._update_legend_visuals()
        self._redraw_panel()

    def load_defaults(self, persist=True):
        self._set_pattern(
            DEFAULT_SCORES_ASCENDING, dict(DEFAULT_LEGEND),
            "表示中：標準パターン（実データに基づく）", persist,
        )

    def load_from_csv(self, csv_path: str, persist=True):
        """作業システムCSVのパターンと機材名を表示に反映する。読めない場合は ValueError（表示は変えない）。
        読み込んだパターンをそのまま基準にするため、保有していない機材の指定とマージンは初期化する"""
        scores, names = read_costcsv(csv_path)
        merged_names = dict(DEFAULT_LEGEND)
        merged_names.update(names)
        file_name = os.path.basename(csv_path)
        self._set_pattern(scores, merged_names, f"表示中：{file_name}", persist,
                          elide_part=file_name, tooltip=csv_path)

    def _set_pattern(self, scores_ascending, names_by_code, source_text, persist, elide_part=None, tooltip=None):
        self._base_scores = [row[:] for row in scores_ascending]
        self._margin = 0
        self._disabled_codes = set()
        self.marginSlider.setValue(0)
        self.marginValueLabel.setText("0°")

        for i, code in enumerate(self._legend_codes):
            self.legendNameEdits[i].setText(names_by_code.get(code, ""))
        self.sourceLabel.setText(source_text, elide_part, tooltip)

        self._update_legend_visuals()
        self._redraw_panel()
        if persist:
            self._store_disabled_codes()
            self._store_margin()

    def _load_disabled_codes(self) -> set:
        settings = QSettings()
        settings.beginGroup(self.SETTINGS_GROUP)
        raw_value = settings.value(self.SETTINGS_DISABLED_CODES, "")
        settings.endGroup()

        if isinstance(raw_value, (list, tuple)):
            raw_codes = raw_value
        else:
            raw_codes = str(raw_value).split(",")

        valid_codes = {code for code in self._legend_codes if code > 0}
        disabled_codes = set()
        for raw_code in raw_codes:
            try:
                code = int(str(raw_code).strip())
            except (TypeError, ValueError):
                continue
            if code in valid_codes:
                disabled_codes.add(code)
        return disabled_codes

    def _store_disabled_codes(self):
        settings = QSettings()
        settings.beginGroup(self.SETTINGS_GROUP)
        settings.setValue(
            self.SETTINGS_DISABLED_CODES,
            ",".join(str(code) for code in sorted(self._disabled_codes)),
        )
        settings.endGroup()

    def _load_margin(self) -> int:
        settings = QSettings()
        settings.beginGroup(self.SETTINGS_GROUP)
        raw_value = settings.value(self.SETTINGS_MARGIN, 0)
        settings.endGroup()
        try:
            margin = int(raw_value)
        except (TypeError, ValueError):
            margin = 0
        return max(-MARGIN_DEGREES_MAX, min(MARGIN_DEGREES_MAX, margin))

    def _store_margin(self):
        settings = QSettings()
        settings.beginGroup(self.SETTINGS_GROUP)
        settings.setValue(self.SETTINGS_MARGIN, int(self._margin))
        settings.endGroup()

    def _toggle_code(self, code: int):
        if code in self._disabled_codes:
            self._disabled_codes.discard(code)
        else:
            self._disabled_codes.add(code)
        self._store_disabled_codes()
        self._update_legend_visuals()
        self._redraw_panel()

    def _update_legend_visuals(self):
        for code, swatch, name_edit in zip(self._legend_codes, self.legendSwatches, self.legendNameEdits):
            disabled = code in self._disabled_codes
            swatch_color = "#dddddd" if disabled else RAWDATA_COLORS_COST[max(0, code)]
            swatch.setStyleSheet(f"background-color:{swatch_color}; border:1px solid #888;")
            name_edit.setStyleSheet("color:#aaa; background-color:#eee;" if disabled else "")
            name_edit.setEnabled(not disabled)

    def _get_legend(self) -> list:
        legend = []
        for code, name_edit in zip(self._legend_codes, self.legendNameEdits):
            legend.append((code, name_edit.text().strip()))
        return legend

    def _get_color_for_code(self, code: int) -> QColor:
        idx = max(0, min(10, code))
        return QColor(RAWDATA_COLORS_COST[idx])

    def _score_at(self, ruggedness: float, slope: float) -> int:
        """起伏量・傾斜の実数値から、機材コードを返す(マージン適用済み)。
        コードは連続量ではなくカテゴリ値(機材の参照キー)なので、最終的な値は必ず
        _base_scoresに実在する値のどれかにする(最近傍参照)。

        起伏量方向・傾斜方向それぞれの重みを「その軸自身の位置だけ」から独立に計算する
        （両軸共通の重みを使うと、片方の軸を固定してももう片方の軸の値の変化につられて
        重みが変わってしまい、丸め境界の通過タイミングが軸ごとにずれて非単調な折れ込みが
        起きるバグを実際に踏んだため、軸ごとに完全独立にした。これにより「起伏量を固定して
        傾斜だけ動かす」「傾斜を固定して起伏量だけ動かす」の両方で表の単調性がそのまま
        保たれることを保証できる）。

        左下(良い)・右上(悪い)の両端は機材の物理的限界で動かしようがないため固定し、
        中間部分だけが対角線方向にマージンに応じて弓なりに動く。"""
        r_max = RUGGEDNESS_THRESHOLDS[-1]
        s_max = SLOPE_THRESHOLDS[-1]

        weight_r = self._axis_weight(ruggedness / r_max)
        weight_s = self._axis_weight(slope / s_max)
        effective_ruggedness = ruggedness - self._margin * (r_max / s_max) * weight_r
        effective_slope = slope - self._margin * weight_s
        return self._nearest_lookup(effective_ruggedness, effective_slope)

    @staticmethod
    def _axis_weight(fraction: float) -> float:
        """軸上の位置(0-1)に対する重み。両端(0,1)で0、中央(0.5)で最大の三角重み"""
        return max(0.0, 1 - abs((fraction - 0.5) / 0.5))

    def _nearest_lookup(self, ruggedness: float, slope: float) -> int:
        """その起伏量・傾斜を含むマスのコードをそのまま返す(補間しない、必ず表に実在する値になる)。
        しきい値は等間隔ではない(起伏量 0,100,150,…／傾斜 0,15,20,…)ため、
        位置の比率ではなく、しきい値で区切られた区間からマスを引く"""
        r_max = RUGGEDNESS_THRESHOLDS[-1]
        s_max = SLOPE_THRESHOLDS[-1]
        r = max(0.0, min(r_max, ruggedness))
        s = max(0.0, min(s_max, slope))

        n_rows = len(self._base_scores)
        n_cols = len(self._base_scores[0])
        # 区間 [T[i], T[i+1]) を i 番目のマスとする（上端の値は最後のマスに含める）
        row = min(bisect.bisect_right(RUGGEDNESS_THRESHOLDS, r) - 1, n_rows - 1)
        col = min(bisect.bisect_right(SLOPE_THRESHOLDS, s) - 1, n_cols - 1)
        code = self._base_scores[row][col]
        # 保有していない機材が担当するマスは0(該当なし)扱いにする
        if code in self._disabled_codes:
            return 0
        return code

    def _on_margin_changed(self, raw_value):
        self._margin = raw_value
        self.marginValueLabel.setText(f"{self._margin:+d}°")
        self._store_margin()
        self._redraw_panel()

    def _redraw_panel(self, *_args):
        total_w = self.GUTTER_LEFT + self.PLOT_WIDTH + self.GUTTER_RIGHT
        total_h = self.PLOT_HEIGHT + self.GUTTER_BOTTOM
        image = QImage(total_w, total_h, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))

        r_max = RUGGEDNESS_THRESHOLDS[-1]
        s_max = SLOPE_THRESHOLDS[-1]
        for py in range(self.PLOT_HEIGHT):
            # 画面上は上ほど起伏量が大きい
            ruggedness = r_max * (1 - py / (self.PLOT_HEIGHT - 1))
            for px in range(self.PLOT_WIDTH):
                slope = s_max * (px / (self.PLOT_WIDTH - 1))
                code = self._score_at(ruggedness, slope)
                image.setPixelColor(self.GUTTER_LEFT + px, py, self._get_color_for_code(code))

        pixmap = QPixmap.fromImage(image)

        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor(255, 255, 255, 90)))
        for s in SLOPE_THRESHOLDS[1:-1]:
            x = self.GUTTER_LEFT + int(self.PLOT_WIDTH * (s / s_max))
            painter.drawLine(x, 0, x, self.PLOT_HEIGHT)
        for r in RUGGEDNESS_THRESHOLDS[1:-1]:
            y = int(self.PLOT_HEIGHT * (1 - r / r_max))
            painter.drawLine(self.GUTTER_LEFT, y, self.GUTTER_LEFT + self.PLOT_WIDTH, y)

        # 軸の数値
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#000000")))
        for r in RUGGEDNESS_THRESHOLDS:
            y = int(self.PLOT_HEIGHT * (1 - r / r_max))
            painter.drawText(0, max(0, y - 6), self.GUTTER_LEFT - 3, 12,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(r))
        for s in SLOPE_THRESHOLDS:
            x = self.GUTTER_LEFT + int(self.PLOT_WIDTH * (s / s_max))
            painter.drawText(x - 12, self.PLOT_HEIGHT + 1, 24, self.GUTTER_BOTTOM,
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, str(s))
        painter.end()

        self.panelLabel.setPixmap(pixmap)

    def export_to_csv(self, output_path: str) -> str:
        """
        作業システムCSVを書き出す(processes/costcsv_parser.pyのCostcsvParserが読める形式)。
        入力に不正があればValueErrorを送出する。
        """
        legend = self._get_legend()
        if len(legend) != len(DEFAULT_LEGEND):
            raise ValueError("機材名の一覧が不正です（行数が変わっています）")

        # 実際の10x6セル(閾値の中間点)に対してマージン適用後のスコアを計算する
        n_rows = len(RUGGEDNESS_THRESHOLDS) - 1
        n_cols = len(SLOPE_THRESHOLDS) - 1
        scores = []
        for i in range(n_rows):
            r_mid = (RUGGEDNESS_THRESHOLDS[i] + RUGGEDNESS_THRESHOLDS[i + 1]) / 2
            row = []
            for j in range(n_cols):
                s_mid = (SLOPE_THRESHOLDS[j] + SLOPE_THRESHOLDS[j + 1]) / 2
                row.append(self._score_at(r_mid, s_mid))
            scores.append(row)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="cp932", newline="") as f:
            writer = csv.writer(f)
            # 上表: ヘッダー行(先頭=起伏量の最大値) + 傾斜の閾値7個
            # データ行は各ビンの下端の閾値をラベルとして降順に並べる(実データの並びに合わせる)、末尾に空欄1つ
            writer.writerow([RUGGEDNESS_THRESHOLDS[-1]] + SLOPE_THRESHOLDS)
            for i in reversed(range(n_rows)):
                writer.writerow([RUGGEDNESS_THRESHOLDS[i]] + scores[i] + [""])
            for _ in range(4):
                writer.writerow([])
            for code, name in legend:
                writer.writerow([code, name])

        return output_path

    # 準備中の「インタラクティブモード」の宣言と、操作方法の検討メモ（例）。実装したらこの案内は外す
    INTERACTIVE_PLAN_TEXT = (
        "<p>機材名の一覧で機材を選び、右のパネルを直接クリック・ドラッグして、"
        "その地形（傾斜×起伏量のマス）に使う機材を割り当てられるようにする予定です。"
        "Excel のひな形を使わなくても、地域の作業システムをマスごとに決められるようになります。</p>"
        "<p><b>操作の例：</b>（検討中のもので、変わることがあります）</p>"
        "<ul>"
        "<li>一覧の色の四角をクリック：保有する／しないの切り替え（今と同じ）</li>"
        "<li>一覧の色の四角を右クリック：その機材でパネルを塗るモードに入る</li>"
        "<li>塗るモード中：一覧のクリックで塗る機材を選び、パネルのクリック・ドラッグで塗る</li>"
        "<li>「該当なし」を選んで塗ると、そのマスを空ける（消しゴム）</li>"
        "<li>塗るモード中は、一覧の空き（マージンの位置）に終了ボタンを出す</li>"
        "<li>塗った操作は1回ずつ元に戻せる</li>"
        "</ul>"
        "<p><b>あわせて変える案の例：</b>（上の例と食い違う案も、検討用にそのまま残しています）</p>"
        "<ul>"
        "<li>「保有していない機材」の指定は、各行のチェックに移す</li>"
        "<li>塗っている間はマージンを 0° にして、マスの区切りどおりに表示する</li>"
        "<li>一覧は上ほど集材効率が高い扱いであることを、一覧に明記する</li>"
        "</ul>"
        "<p>それまでは、「デフォルトExcelを開く」でひな形を編集し、"
        "「CSVインポート」で読み込んでください。</p>"
    )

    def _show_interactive_plan(self):
        # QMessageBox は幅を広げられず文章が細長くなり、折り返すQLabelは箇条書きの高さを
        # 少なく見積もって下の行が切れるため、文章表示用の部品に載せる
        dialog = QDialog(self)
        dialog.setWindowTitle("インタラクティブモード（準備中）")
        layout = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setHtml(self.INTERACTIVE_PLAN_TEXT)
        text.setFrameShape(QFrame.Shape.NoFrame)
        text.setStyleSheet("background: transparent;")
        text.setMinimumSize(560, 520)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _on_export_clicked(self):
        if self._on_export:
            self._on_export(self)
