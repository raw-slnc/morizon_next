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

    def __init__(self, log_callback):
        # True は従来どおり QGIS の「ログメッセージ」パネルにも記録を残す指定。
        super().__init__(True)
        self._log_callback = log_callback
        self._last_line = None

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
