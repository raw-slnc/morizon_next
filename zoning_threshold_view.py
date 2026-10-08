# This file is part of MORIZON NEXT.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""ゾーニングタブで、収益性・災害リスクの値の段階と面積の割合を、しきい値の位置と一緒に見せる図。

収益性・災害リスクのラスターは3要素の点数の合計（整数）を持つが、地図ではしきい値で切った2色しか見えない。
しきい値を決める前に、データがどの値にどれだけの面積で分かれているかを見られるようにする。
計算には関わらない（表示だけ）。"""

from qgis.PyQt.QtCore import Qt, QRectF, QPointF, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPolygonF, QFont
from qgis.PyQt.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSizePolicy, QPushButton


def value_shares(layer):
    """ラスターの整数値ごとの面積の割合 [(値, 割合), ...]（値の小さい順）。数えられなければ []。
    QGIS のヒストグラムで数える（値1つにつき1区間）"""
    if layer is None or not layer.isValid():
        return []
    provider = layer.dataProvider()
    try:
        stats = provider.bandStatistics(1)  # 既存の呼び方に合わせる（QGIS 3/4 で列挙の書き方が違うため）
        low, high = int(round(stats.minimumValue)), int(round(stats.maximumValue))
    except Exception:
        return []
    if high < low or high - low > 200:
        return []  # 合計点のラスターではない
    bins = high - low + 1
    histogram = provider.histogram(1, bins, low - 0.5, high + 0.5)
    counts = list(histogram.histogramVector)
    total = sum(counts)
    if total <= 0:
        return []
    return [(low + i, count / total) for i, count in enumerate(counts)]


class _Scale(QWidget):
    """値のマス（割合つき）と、しきい値の位置の▲を描く。
    マスの色は今のしきい値で分ける（地図の色分けと同じ）。▲は今のしきい値の位置。
    初期値（自動で入る境目）からどう動かしたかは、下の説明文で示す。
    ▲はつまんで左右に動かせる（マスの境目に吸い付く）。動かすと thresholdChanged を出す"""

    thresholdChanged = pyqtSignal(int)

    CELL_H = 22
    MARK_H = 16

    def __init__(self, lower_color, higher_color, parent=None):
        super().__init__(parent)
        self.lower_color = QColor(lower_color)
        self.higher_color = QColor(higher_color)
        self.shares = []
        self.threshold = None
        self.default = None
        self.setMinimumHeight(self.CELL_H + 18 + self.MARK_H + 4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._dragging = False

    def set_data(self, shares, threshold, default):
        self.shares = shares
        self.threshold = threshold
        self.default = default
        self.update()

    # ── ▲をつまんで動かす ──
    def _cell_w(self):
        return (self.width() - 2) / len(self.shares)

    def _marker_x(self):
        boundary = sum(1 for value, _ in self.shares if value <= self.threshold)
        return 1 + boundary * self._cell_w()

    def _near_marker(self, pos):
        if not self.shares or self.threshold is None:
            return False
        y = self.CELL_H + 18
        return abs(pos.x() - self._marker_x()) <= 10 and y - 4 <= pos.y() <= y + self.MARK_H + 4

    def _threshold_at(self, x):
        """x に最も近いマスの境目のしきい値（境目より左のマスの値。いちばん左なら最小値−1）"""
        boundary = int(round((x - 1) / self._cell_w()))
        boundary = min(max(boundary, 0), len(self.shares))
        return self.shares[boundary - 1][0] if boundary > 0 else self.shares[0][0] - 1

    def mousePressEvent(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()
        if event.button() == Qt.MouseButton.LeftButton and self._near_marker(pos):
            self._dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()
        if self._dragging:
            value = self._threshold_at(pos.x())
            if value != self.threshold:
                self.threshold = value
                self.update()
                self.thresholdChanged.emit(value)
            return
        near = self._near_marker(pos)
        self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.ArrowCursor)
        self.setToolTip("▲を左右に動かすと、しきい値が変わります" if near else "")

    def mouseReleaseEvent(self, event):
        self._dragging = False
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        text_color = palette.color(palette.ColorRole.WindowText)
        if not self.shares:
            painter.setPen(palette.color(palette.ColorRole.PlaceholderText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "レイヤーを選ぶと表示します")
            painter.end()
            return
        n = len(self.shares)
        width = self.width() - 2
        cell_w = width / n
        top = 1
        small = QFont(self.font())
        small.setPointSizeF(max(self.font().pointSizeF() - 1, 7))
        for i, (value, share) in enumerate(self.shares):
            rect = QRectF(1 + i * cell_w, top, cell_w, self.CELL_H)
            higher = self.threshold is not None and value > self.threshold
            fill = QColor(self.higher_color if higher else self.lower_color)
            painter.fillRect(rect, fill)
            painter.setPen(QColor(60, 60, 60))
            painter.drawRect(rect)
            painter.setPen(QColor(20, 20, 20))
            painter.setFont(self.font())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(value))
            painter.setPen(text_color)
            painter.setFont(small)
            share_rect = QRectF(rect.left(), rect.bottom() + 1, cell_w, 16)
            painter.drawText(share_rect, Qt.AlignmentFlag.AlignCenter, f"{share:.0%}" if share >= 0.005
                             else ("<1%" if share > 0 else "0%"))
        if self.threshold is not None:
            # しきい値以下（左）としきい値より大きい（右）の境目に▲を置く
            boundary = sum(1 for value, _ in self.shares if value <= self.threshold)
            x = 1 + boundary * cell_w
            x = min(max(x, 1 + self.MARK_H / 2), width + 1 - self.MARK_H / 2)
            y = top + self.CELL_H + 18
            bottom = y + self.MARK_H - 2
            triangle = QPolygonF([QPointF(x, y), QPointF(x - 7, bottom), QPointF(x + 7, bottom)])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(text_color)
            painter.drawPolygon(triangle)
        painter.end()


class ThresholdView(QWidget):
    """軸1つ分（見出し・マス・▲）"""

    def __init__(self, title, lower_color, higher_color, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight: bold;")
        self.scale = _Scale(lower_color, higher_color)
        self.thresholdChanged = self.scale.thresholdChanged  # ▲を動かしたとき
        self.note = QLabel()
        self.note.setStyleSheet("color: gray;")
        self.note.setWordWrap(True)
        # 地図での表示の ON/OFF（カラーバーと同じ幅）。押すとレイヤーパネルのチェックを切り替える
        self.visible_button = QPushButton()
        self.visible_button.setCheckable(True)
        self.visible_button.setEnabled(False)
        self.visible_button.toggled.connect(self._update_visible_text)
        self.visibilityToggled = self.visible_button.toggled
        self._update_visible_text(False)
        layout.addWidget(self.title)
        layout.addWidget(self.scale)
        layout.addWidget(self.note)
        layout.addWidget(self.visible_button)
        self._layer_id = None
        self._shares = []
        self._default = None

    def set_layer(self, layer, valid, initial_threshold):
        """レイヤーが変わったときだけ数え直す。initial_threshold(layer) は初期値を求める関数"""
        layer_id = layer.id() if (layer is not None and valid) else None
        if layer_id != self._layer_id:
            self._layer_id = layer_id
            self._shares = value_shares(layer) if layer_id else []
            self._default = initial_threshold(layer) if self._shares else None
        return self._shares

    def _update_visible_text(self, checked):
        self.visible_button.setText("地図に表示：ON" if checked else "地図に表示：OFF")

    def set_visible_state(self, enabled, checked):
        """レイヤーパネルの状態をボタンに写す（合図は出さない）"""
        self.visible_button.blockSignals(True)
        self.visible_button.setEnabled(enabled)
        self.visible_button.setChecked(bool(checked))
        self.visible_button.blockSignals(False)
        self._update_visible_text(bool(checked))

    def _higher(self, threshold):
        return sum(share for value, share in self._shares if value > threshold)

    def set_threshold(self, threshold):
        self.scale.set_data(self._shares, threshold if self._shares else None, self._default)
        if not self._shares or threshold is None:
            self.note.setText("")
            return
        text = f"初期値 {self._default}（高い側 {self._higher(self._default):.0%}）" if self._default is not None else ""
        if threshold != self._default:
            text += f" → しきい値 {threshold}（高い側 {self._higher(threshold):.0%}）"
        self.note.setText(text)


class ZoningThresholdPanel(QWidget):
    """収益性・災害リスクを左右に並べる"""

    def __init__(self, profit_colors, risk_colors, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(24)
        self.profit = ThresholdView("収益性軸レイヤー", *profit_colors)
        self.risk = ThresholdView("災害リスク軸レイヤー", *risk_colors)
        layout.addWidget(self.profit, 1)
        layout.addWidget(self.risk, 1)
