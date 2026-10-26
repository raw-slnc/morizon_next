# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""地位指数マップの「取込」の窓。

手引 4.2（p.80）の、航空レーザから作った地位指数マップを使う方法を、Morizon Next では
要素計算タブの「取込」で行う。原版は、マップをレイヤとして読み込んでスコアリングの欄で選ぶ手順だが、
Morizon Next のスコアリングの欄は自分の出力しか選べないため、入力として取り込み、要素計算で
「地位（独自）」の要素として作る。窓ではファイルを選び、座標系・マス目・値の範囲を見せてから取り込む。
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout
from qgis.gui import QgsFileWidget

# 地位指数（40年生時点の樹高 m）としてありそうな値の範囲。外れていれば注意を出す
PLAUSIBLE_RANGE = (0.0, 50.0)

EXPLANATION = (
    "航空レーザの計測データから作った地位指数マップ（GeoTIFF、1枚）を取り込みます。"
    "作り方は、林野庁「航空機 LiDAR データを使った地位指数分布図の作成の手引き」を参照してください。"
    "<br><br>"
    "取り込んだマップは、要素計算の「地位」で DEM のマス目にそろえ、<b>「地位（独自）」</b>として作ります。"
    "自動で計算する3樹種（スギ・ヒノキ・カラマツ）の地位はそのまま残るので、スコアリングタブの"
    "「地位スコア」の欄で、どちらを使うか選べます。"
    "<br><br>"
    "全国一律の式で求めた地位は相対値として扱いますが、独自に作った地位指数は絶対値として扱えます"
    "（例：樹高 20m で収益性が高いなら、20 以上を3点）。しきい値の初期値は、ほかの地位と同じく"
    "面積でおおよそ3等分する値です。"
    "<br><span style='color:#666;'>手引 4.2 p.80、3.7.2 (1) p.65–66</span>"
)


def describe_raster(path, dem_path=""):
    """(説明の文, 注意の文の配列)。読めなければ (None, [理由])"""
    from osgeo import gdal, osr
    dataset = gdal.Open(path)
    if dataset is None:
        return None, ["ラスターとして読み込めません。"]
    transform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    try:
        minimum, maximum = band.ComputeRasterMinMax(False)
    except RuntimeError:
        minimum = maximum = None
    srs = osr.SpatialReference(wkt=dataset.GetProjection()) if dataset.GetProjection() else None
    crs_text = "不明"
    if srs is not None:
        srs.AutoIdentifyEPSG()
        code = srs.GetAuthorityCode(None)
        crs_text = f"EPSG:{code}" if code else (srs.GetName() or "不明")
    lines = [
        f"座標系：{crs_text}",
        f"マス目：{abs(transform[1]):g} × {abs(transform[5]):g}（{dataset.RasterXSize} × {dataset.RasterYSize} マス）",
        "値の範囲：" + (f"{minimum:.2f} 〜 {maximum:.2f}" if minimum is not None else "値がありません"),
    ]
    # 作った側が残した条件（fcloud_shizuoka の「地位指数マップ」は SI_ で始まる項目に残す）
    labels = (("SI_METHOD", "作り方"), ("SI_CURVE", "曲線"), ("SI_HEIGHT", "樹高"), ("SI_AGE", "林齢"),
              ("SI_MEASURE_YEAR", "森林計測年"), ("SI_AGE_CORRECTION", "林齢の補正"), ("SI_EXCLUDED", "値なし"),
              ("SI_REGISTER_SPECIES", "森林簿との樹種の食い違い"), ("SI_CREATED", "作成日"))
    metadata = dataset.GetMetadata() or {}
    recorded = [f"{label}：{metadata[key]}" for key, label in labels if metadata.get(key)]
    if recorded:
        lines += ["", "作ったときの条件（ファイルの記録）"] + recorded
    warnings = []
    if srs is None:
        warnings.append("座標系がありません。DEM と同じ平面直角座標系を付けてから取り込んでください。")
    elif dem_path and os.path.isfile(dem_path):
        dem = gdal.Open(dem_path)
        if dem is not None and dem.GetProjection():
            dem_srs = osr.SpatialReference(wkt=dem.GetProjection())
            if not srs.IsSame(dem_srs):
                warnings.append("DEM と座標系が違います。計算のときに DEM の座標系へ変換します。")
    if minimum is None:
        warnings.append("値のあるマスがありません。")
    elif minimum < PLAUSIBLE_RANGE[0] or maximum > PLAUSIBLE_RANGE[1]:
        warnings.append(
            f"値が地位指数（樹高 m）としては大きすぎるか小さすぎます（{PLAUSIBLE_RANGE[0]:g}〜{PLAUSIBLE_RANGE[1]:g} の外）。"
            "データの無い所の値（-9999 など）が NoData になっているか確かめてください。"
        )
    return "\n".join(lines), warnings


class SiteIdxMapDialog(QDialog):
    """結果は action（"import"・"remove"・None）と path で返す"""

    def __init__(self, parent=None, current_path="", dem_path=""):
        super().__init__(parent)
        self.setWindowTitle("地位指数マップの取込")
        self.setMinimumWidth(520)
        self.action = None
        self.path = ""
        self._dem_path = dem_path

        layout = QVBoxLayout(self)
        explanation = QLabel(EXPLANATION)
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(explanation)

        if current_path:
            current = QLabel(f"取り込み済み：{os.path.basename(current_path)}")
            current.setStyleSheet("font-weight: bold;")
            layout.addWidget(current)

        self.file_widget = QgsFileWidget()
        self.file_widget.setFilter("GeoTIFF (*.tif *.tiff)")
        self.file_widget.setDialogTitle("地位指数マップを選ぶ")
        self.file_widget.fileChanged.connect(self._on_file_changed)
        layout.addWidget(self.file_widget)

        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color:#c05000;")
        layout.addWidget(self.warning_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("取り込む")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self._accept_import)
        self.buttons.rejected.connect(self.reject)
        if current_path:
            remove_button = QPushButton("外す")
            remove_button.setToolTip("取り込んだマップを作業フォルダから外します。次の要素計算で「地位（独自）」も消えます")
            self.buttons.addButton(remove_button, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove_button.clicked.connect(self._accept_remove)
        layout.addWidget(self.buttons)
        self.warning_label.setVisible(False)
        self._fit_to_contents()

    def _fit_to_contents(self):
        """折り返しの説明文も含めて、中身が収まる大きさにする（既定の大きさだと文字が下の欄と重なる）"""
        width = max(self.minimumWidth(), self.width(), self.sizeHint().width())
        self.resize(width, self.layout().totalHeightForWidth(width))

    def _on_file_changed(self, path):
        ok = bool(path) and os.path.isfile(path)
        info, warnings = describe_raster(path, self._dem_path) if ok else ("", [])
        self.info_label.setText(info or "")
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok and info is not None)
        self._fit_to_contents()

    def _accept_import(self):
        self.action, self.path = "import", self.file_widget.filePath()
        self.accept()

    def _accept_remove(self):
        self.action = "remove"
        self.accept()
