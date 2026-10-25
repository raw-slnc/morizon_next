# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""Processing のメッセージを進捗ダイアログへ中継する部品。"""

from qgis.core import QgsProcessingFeedback


class LogForwardingFeedback(QgsProcessingFeedback):
    """Processing のログを QThread のシグナル等へ1行ずつ中継する。

    保守メモ:
    QgsApplication.messageLog() の監視だけでは、外部コマンド（GRASS・GDAL・SAGA）の
    stdout が Linux では見えても Windows では届かない場合がある。OS ごとのコンソール
    起動方法に依存させないため、processing.run() にこの feedback を明示的に渡し、
    Processing が pushConsoleInfo() 等へ送る情報を直接受け取る。

    QGIS 3.44 も対応対象なので、QGIS 4.2 で追加された *InfoPushed シグナルには依存せず、
    QGIS 3 系から仮想メソッドである push*() をオーバーライドしている。
    """

    def __init__(self, log_callback, detail_callback=None):
        # True は従来どおり QGIS の「ログメッセージ」パネルにも記録を残す指定。
        super().__init__(True)
        self._log_callback = log_callback
        self._last_line = None
        # 工程の中の段階名と、処理ツール（GRASS など）が送る進み具合（%）を、進捗の窓の詳細行へ出す。
        # 処理の速さは変えず、止まって見えないようにするための表示
        self._detail_callback = detail_callback
        self._stage = None
        self._percent = None
        if detail_callback is not None:
            self.progressChanged.connect(self._on_progress)

    def set_stage(self, name):
        """段階名を出す。None で消す（次の工程に移るとき）"""
        if self._detail_callback is None:
            return
        self._stage = name
        self._percent = None
        self._detail_callback(name or "")

    def _on_progress(self, percent):
        if self._stage is None:
            return
        percent = int(percent)
        if percent == self._percent:
            return
        self._percent = percent
        self._detail_callback(f"{self._stage} … {percent}%")

    def _forward(self, text):
        # 外部コマンドは CR だけで進捗行を書き換えることがあるため、CR/LF の双方で分ける。
        lines = [
            line.strip()
            for line in str(text or "").replace("\r", "\n").splitlines()
            if line.strip()
        ]
        if not lines:
            return
        line = lines[-1]
        if line == self._last_line:
            return
        self._last_line = line
        self._log_callback(line)

    def reportError(self, error, fatalError=False):
        super().reportError(error, fatalError)
        self._forward(error)

    def pushWarning(self, warning):
        super().pushWarning(warning)
        self._forward(warning)

    def pushInfo(self, info):
        super().pushInfo(info)
        self._forward(info)

    def pushCommandInfo(self, info):
        super().pushCommandInfo(info)
        self._forward(info)

    def pushDebugInfo(self, info):
        super().pushDebugInfo(info)
        self._forward(info)

    def pushConsoleInfo(self, info):
        super().pushConsoleInfo(info)
        self._forward(info)

    def pushFormattedMessage(self, html, text):
        super().pushFormattedMessage(html, text)
        self._forward(text)

    def setProgressText(self, text):
        super().setProgressText(text)
        self._forward(text)


def set_stage(feedback, name):
    """feedback が段階名を受け取れるときだけ渡す（ゾーン統計量など、他の工程から同じ関数を呼んでも何もしない）"""
    if isinstance(feedback, LogForwardingFeedback):
        feedback.set_stage(name)
