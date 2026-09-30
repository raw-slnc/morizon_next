import requests
from qgis.PyQt.QtCore import Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QDialogButtonBox
)

from . import fgd_auth
from . import fgd_fetcher
from .settings_manager import FgdCredentialsManager


class FgdLoginThread(QThread):
    succeeded = pyqtSignal(object)  # requests.Session（ログイン済み）
    failed = pyqtSignal(str)

    def __init__(self, username: str, password: str):
        super().__init__()
        self.username = username
        self.password = password

    def run(self):
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": "Mozilla/5.0 (MORIZON NEXT QGIS Plugin)"})
            action_url = fgd_auth.start_login(session)
            ok, error, chain = fgd_auth.submit_login(session, action_url, self.username, self.password)
            if not ok:
                self.failed.emit(error or "ログインに失敗しました。")
                return
            if not fgd_fetcher.check_login(session):
                chain_text = "\n".join(chain)
                self.failed.emit(
                    "ログイン処理は完了しましたが、状態を確認できませんでした。\n"
                    f"経路:\n{chain_text}"
                )
                return
            self.succeeded.emit(session)
        except Exception as e:
            self.failed.emit(str(e))


class FgdLoginDialog(QDialog):
    """
    基盤地図情報ダウンロードサービスへのログインダイアログ。

    埋め込みブラウザ（QtWebEngine）での地図アプリ描画は、Qt5環境ではJSエンジンが
    古く起動時にクラッシュし、Qt6環境（少なくともFlatpak）ではWebGL初期化に失敗する
    ことが実機検証で判明した。一方、ログイン自体はKeycloakの標準サーバーレンダリング
    フォーム（プレーンなHTML、JS不要）で完結すると判明したため、ブラウザ描画を介さず
    requestsによるフォームPOSTで直接再現する方式にしている。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("基盤地図情報ダウンロードサービス ログイン")
        self.setMinimumWidth(400)

        self.session = None
        self._thread = None

        layout = QVBoxLayout(self)

        info_label = QLabel(
            "基盤地図情報ダウンロードサービスのログインID・パスワードを入力してください。\n"
            "入力内容は国土地理院のログインサーバーへ直接送信されます。\n"
            "「設定」タブで保存・自動入力を有効にしている場合はそこから読み込まれます。"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        form = QFormLayout()
        self.usernameEdit = QLineEdit()
        self.passwordEdit = QLineEdit()
        self.passwordEdit.setEchoMode(QLineEdit.EchoMode.Password)
        self.usernameEdit.returnPressed.connect(self.passwordEdit.setFocus)
        self.passwordEdit.returnPressed.connect(self._on_login_clicked)
        form.addRow("ログインID", self.usernameEdit)
        form.addRow("パスワード", self.passwordEdit)
        layout.addLayout(form)

        # 「設定」タブで自動入力が有効な場合、保存済みのID/パスワードを読み込む
        saved = FgdCredentialsManager().load()
        if saved["auto_fill"] and saved["username"] and saved["password"]:
            self.usernameEdit.setText(saved["username"])
            self.passwordEdit.setText(saved["password"])

        self.errorLabel = QLabel("")
        self.errorLabel.setStyleSheet("color: #c00;")
        self.errorLabel.setWordWrap(True)
        layout.addWidget(self.errorLabel)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        login_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        login_button.setText("ログイン")
        for button in (login_button, cancel_button):
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.button_box.accepted.connect(self._on_login_clicked)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self._focus_order = [self.usernameEdit, self.passwordEdit, login_button, cancel_button]
        self.setTabOrder(self.usernameEdit, self.passwordEdit)
        self.setTabOrder(self.passwordEdit, login_button)
        self.setTabOrder(login_button, cancel_button)

        self.adjustSize()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._set_initial_focus)
        QTimer.singleShot(50, self._set_initial_focus)

    def _set_initial_focus(self):
        self._clear_button_defaults()
        self.usernameEdit.setFocus()

    def _set_password_focus(self):
        self._clear_button_defaults()
        self.passwordEdit.setFocus()

    def _clear_button_defaults(self):
        for standard_button in (
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Cancel,
        ):
            button = self.button_box.button(standard_button)
            button.setAutoDefault(False)
            button.setDefault(False)

    def keyPressEvent(self, event):
        current = QApplication.focusWidget()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            login_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
            cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
            if current == login_button:
                login_button.click()
                return
            if current == cancel_button:
                cancel_button.click()
                return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, next):
        current = QApplication.focusWidget()
        if current in self._focus_order:
            self._clear_button_defaults()
            step = 1 if next else -1
            idx = (self._focus_order.index(current) + step) % len(self._focus_order)
            reason = Qt.FocusReason.TabFocusReason if next else Qt.FocusReason.BacktabFocusReason
            self._focus_order[idx].setFocus(reason)
            return True
        return super().focusNextPrevChild(next)

    def _on_login_clicked(self):
        username = self.usernameEdit.text().strip()
        password = self.passwordEdit.text()
        if not username or not password:
            self.errorLabel.setText("ログインIDとパスワードを入力してください。")
            self.adjustSize()
            self._clear_button_defaults()
            if not username:
                self.usernameEdit.setFocus()
            else:
                self.passwordEdit.setFocus()
            return

        self.errorLabel.setStyleSheet("color: #444;")
        self.errorLabel.setText("ログイン中…")
        self.adjustSize()
        self._clear_button_defaults()
        self.button_box.setEnabled(False)

        self._thread = FgdLoginThread(username, password)
        self._thread.succeeded.connect(self._on_succeeded)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_succeeded(self, session):
        self.session = session
        self.accept()

    def _on_failed(self, message):
        self.errorLabel.setStyleSheet("color: #c00;")
        self.errorLabel.setText(message)
        self.adjustSize()
        self.button_box.setEnabled(True)
        QTimer.singleShot(0, self._set_password_focus)
        QTimer.singleShot(50, self._set_password_focus)

    def get_session(self):
        return self.session
