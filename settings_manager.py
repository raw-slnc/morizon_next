from os import error
from qgis.PyQt.QtCore import QSettings

# QSettings holds variables as list or dict or str.
# if int or bool value is set, they are converted to str in the Class.


def DEFAULT_SETTINGS():
    return {
        'scores_siteidx': ['1', '2', '3'],
        'scores_cost': ['1', '2', '3'],
        'scores_distance': ['3', '2', '1'],
        'scores_shc': ['1', '2', '3'],
        'scores_slope': ['1', '2', '3'],
        'scores_savearea': ['1', '2'],
        'siteidx_sugi_params': ['21.280', '15.82', '1.028', '1347.09', '1.001', '58.85', '1.111'],
        'siteidx_hinoki_params': ['16.830', '11.05', '0.9688', '1396.0', '0.09319', '61.92', '0.8617'],
        'siteidx_karamatsu_params': ['22.510', '11.61', '0.129', '1264.0', '0.2939', '44.3', '1.125'],
        'ruggedness_param': '49',
        'shc_param': '49',
        'cost_algorithm': 'ruggedness' # ruggedness or shc
    }


class SettingsManager:
    SETTING_GROUP = '/MORIZON'

    def __init__(self):
        self.__settings = DEFAULT_SETTINGS()

        self.load_settings()
        self.validate_settings()

    def load_setting(self, key):
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        value = qsettings.value(key)
        qsettings.endGroup()
        if value:
            self.__settings[key] = value

    def load_settings(self):
        for key in self.__settings:
            self.load_setting(key)

    def validate_settings(self):
        """
        読込済の設定値をチェックして不正な値があれば初期値で上書きする
        """
        for key, value in self.__settings.items():
            if self.validate_setting(key, value) is not None:
                self.store_setting(key, DEFAULT_SETTINGS()[key])

    @staticmethod
    def validate_setting(key, value) -> str:
        """
        設定値のエラーチェック
        エラーがあればエラーメッセージが、なければNoneが返る
        """
        if isinstance(value, list):
            if not isinstance(DEFAULT_SETTINGS().get(key), list):
                return "値の型が定義と一致しません"

            if len(value) != len(DEFAULT_SETTINGS().get(key)):
                return "値の数が定義と一致しません"

        if key in ["ruggedness_param", "shc_param"]:
            if int(value) % 2 == 0:
                return "値は奇数でなければなりません"

        return None

    def store_settings(self, settings_dict: dict):
        for key, value in settings_dict.items():
            self.store_setting(key, value)

    def store_setting(self, key, value):
        error_message = self.validate_setting(key, value)
        if error_message is not None:
            raise Exception(f"{key}:{value} -> {error_message}")

        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        qsettings.setValue(key, value)
        qsettings.endGroup()
        self.load_settings()

    def restore_default_settings(self):
        self.store_settings(DEFAULT_SETTINGS())

    def get_setting(self, key):
        return self.__settings[key]

    def get_settings(self):
        return self.__settings


class OutputLayerStyleManager:
    """
    MORIZON出力レイヤーの描画合成設定。

    原版と同じく乗算合成を初期値とする（重ね順に関係なく、要素を重ねて目視確認できるように）。
    チェックを外した場合だけ、不透明度だけの通常の重ね方にする。
    """

    SETTING_GROUP = '/MORIZON/output_layer_style'
    APPLY_MULTIPLY_KEY = 'apply_multiply_output'

    def load_apply_multiply(self) -> bool:
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        value = qsettings.value(self.APPLY_MULTIPLY_KEY, True, type=bool)
        qsettings.endGroup()
        return bool(value)

    def store_apply_multiply(self, apply_multiply: bool):
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        qsettings.setValue(self.APPLY_MULTIPLY_KEY, bool(apply_multiply))
        qsettings.endGroup()


class FgdCredentialsManager:
    """
    基盤地図情報ダウンロードサービスのログインID・パスワードの保存。

    平文でQSettingsに保存する(ユーザー指示による)。SettingsManagerとは別の
    設定グループを使い、「ファイル書き出し」で出力されるsettings.jsonには
    絶対に含まれないようにする(パスワードが意図せずファイルとして
    共有されるのを防ぐため)。
    """

    SETTING_GROUP = '/MORIZON/fgd_credentials'

    def load(self) -> dict:
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        username = qsettings.value('username', '')
        password = qsettings.value('password', '')
        auto_fill = qsettings.value('auto_fill', False, type=bool)
        qsettings.endGroup()
        return {'username': username, 'password': password, 'auto_fill': auto_fill}

    def store(self, username: str, password: str, auto_fill: bool):
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        qsettings.setValue('username', username)
        qsettings.setValue('password', password)
        qsettings.setValue('auto_fill', auto_fill)
        qsettings.endGroup()

    def clear(self):
        self.store('', '', False)


class ShcMethodManager:
    """
    地形の複雑さの前半（DEMの平滑化・平面曲率）の計算方法。
    既定はプラグイン内の計算（processes/raster_writer/terrain_numpy.py）。
    「SAGA ON」にした場合だけ、従来どおり SAGA（Processing Saga NextGen Provider）を使う。
    環境ごとの選択なので settings.json（解析の設定）には含めず、QGISの設定に保存する
    """

    SETTING_GROUP = '/MORIZON/shc_method'
    USE_SAGA_KEY = 'use_saga'

    def load_use_saga(self) -> bool:
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        value = qsettings.value(self.USE_SAGA_KEY, False, type=bool)
        qsettings.endGroup()
        return bool(value)

    def store_use_saga(self, use_saga: bool):
        qsettings = QSettings()
        qsettings.beginGroup(self.SETTING_GROUP)
        qsettings.setValue(self.USE_SAGA_KEY, bool(use_saga))
        qsettings.endGroup()
