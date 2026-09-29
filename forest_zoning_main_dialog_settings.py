import json

from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from .settings_manager import SettingsManager, DEFAULT_SETTINGS


class ForestZoningMainDialogSettings:
    """
    メイン画面の「設定」タブの処理を実装するクラス
    """

    def __init__(self, widget, main):
        self.widget = widget
        self.main = main
        self.init_ui()

    def init_ui(self):
        self.widget.storeSettingsPushButton.clicked.connect(self.store_settings)
        self.widget.abortSettingsPushButton.clicked.connect(
            self.set_values_from_stored_settings)
        self.widget.restoreDefaultSettingsPushButton.clicked.connect(
            self.restore_default_settings)
        self.widget.writeFilePushbutton.clicked.connect(self.write_settings_to_file)
        self.widget.readFilePushbutton.clicked.connect(self.read_settings_from_file)

        self.widget.settingsRuggednessSpinbox.valueChanged.connect(
            lambda: self.force_odd(self.widget.settingsRuggednessSpinbox))
        self.widget.settingsShcSpinbox.valueChanged.connect(
            lambda: self.force_odd(self.widget.settingsShcSpinbox))

        self.set_values_from_stored_settings()

    @staticmethod
    def force_odd(spinbox):
        """
        Spinboxの値を強制的に奇数にする
        """
        if spinbox.value() % 2 == 0:
            spinbox.setValue(
                min(spinbox.maximum(), max(spinbox.minimum(), spinbox.value() - 1)))

    def set_ui_values(self, settings: dict):
        self.widget.settingsSiteidxScore1Spinbox.setValue(
            int(settings["scores_siteidx"][0]))
        self.widget.settingsSiteidxScore2Spinbox.setValue(
            int(settings["scores_siteidx"][1]))
        self.widget.settingsSiteidxScore3Spinbox.setValue(
            int(settings["scores_siteidx"][2]))
        self.widget.settingsCostScore1Spinbox.setValue(
            int(settings["scores_cost"][0]))
        self.widget.settingsCostScore2Spinbox.setValue(
            int(settings["scores_cost"][1]))
        self.widget.settingsCostScore3Spinbox.setValue(
            int(settings["scores_cost"][2]))
        self.widget.settingsDistanceScore1Spinbox.setValue(
            int(settings["scores_distance"][0]))
        self.widget.settingsDistanceScore2Spinbox.setValue(
            int(settings["scores_distance"][1]))
        self.widget.settingsDistanceScore3Spinbox.setValue(
            int(settings["scores_distance"][2]))
        self.widget.settingsShcScore1Spinbox.setValue(
            int(settings["scores_shc"][0]))
        self.widget.settingsShcScore2Spinbox.setValue(
            int(settings["scores_shc"][1]))
        self.widget.settingsShcScore3Spinbox.setValue(
            int(settings["scores_shc"][2]))
        self.widget.settingsSlopeScore1Spinbox.setValue(
            int(settings["scores_slope"][0]))
        self.widget.settingsSlopeScore2Spinbox.setValue(
            int(settings["scores_slope"][1]))
        self.widget.settingsSlopeScore3Spinbox.setValue(
            int(settings["scores_slope"][2]))
        self.widget.settingsSaveareaScore1Spinbox.setValue(
            int(settings["scores_savearea"][0]))
        self.widget.settingsSaveareaScore2Spinbox.setValue(
            int(settings["scores_savearea"][1]))
        self.widget.settingsSiteidxSugiBaseSpinbox.setValue(
            float(settings["siteidx_sugi_params"][0]))
        self.widget.settingsSiteidxSugiNpp1Spinbox.setValue(
            float(settings["siteidx_sugi_params"][1]))
        self.widget.settingsSiteidxSugiNpp2Spinbox.setValue(
            float(settings["siteidx_sugi_params"][2]))
        self.widget.settingsSiteidxSugiSrad1Spinbox.setValue(
            float(settings["siteidx_sugi_params"][3]))
        self.widget.settingsSiteidxSugiSrad2Spinbox.setValue(
            float(settings["siteidx_sugi_params"][4]))
        self.widget.settingsSiteidxSugiVtex1Spinbox.setValue(
            float(settings["siteidx_sugi_params"][5]))
        self.widget.settingsSiteidxSugiVtex2Spinbox.setValue(
            float(settings["siteidx_sugi_params"][6]))
        self.widget.settingsSiteidxHinokiBaseSpinbox.setValue(
            float(settings["siteidx_hinoki_params"][0]))
        self.widget.settingsSiteidxHinokiNpp1Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][1]))
        self.widget.settingsSiteidxHinokiNpp2Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][2]))
        self.widget.settingsSiteidxHinokiSrad1Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][3]))
        self.widget.settingsSiteidxHinokiSrad2Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][4]))
        self.widget.settingsSiteidxHinokiVtex1Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][5]))
        self.widget.settingsSiteidxHinokiVtex2Spinbox.setValue(
            float(settings["siteidx_hinoki_params"][6]))
        self.widget.settingsSiteidxKaramatsuBaseSpinbox.setValue(
            float(settings["siteidx_karamatsu_params"][0]))
        self.widget.settingsSiteidxKaramatsuNpp1Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][1]))
        self.widget.settingsSiteidxKaramatsuNpp2Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][2]))
        self.widget.settingsSiteidxKaramatsuSrad1Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][3]))
        self.widget.settingsSiteidxKaramatsuSrad2Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][4]))
        self.widget.settingsSiteidxKaramatsuVtex1Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][5]))
        self.widget.settingsSiteidxKaramatsuVtex2Spinbox.setValue(
            float(settings["siteidx_karamatsu_params"][6]))
        self.widget.settingsRuggednessSpinbox.setValue(
            int(float(settings["ruggedness_param"])))
        self.widget.settingsShcSpinbox.setValue(int(float(settings["shc_param"])))

        if settings["cost_algorithm"] == "ruggedness":
            self.widget.costAlgoRuggednessRadio.setChecked(True)  # 起伏量
        else:
            self.widget.costAlgoShcRadio.setChecked(True)  # SHC

    def set_values_from_stored_settings(self):
        smanager = SettingsManager()
        settings = smanager.get_settings()
        self.set_ui_values(settings)

    def restore_default_settings(self):
        answer = QMessageBox.question(self.widget,
                                      "",
                                      "全ての設定値を初期化してよろしいですか？",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if answer == QMessageBox.StandardButton.No:
            return

        smanager = SettingsManager()
        smanager.restore_default_settings()
        self.set_values_from_stored_settings()
        self.main.scoring.set_scoring_score_labels_from_settings()
        QMessageBox.information(self.widget, "完了", "初期設定を復元しました")

    def make_settings_dict(self):
        scores_siteidx = [
            self.widget.settingsSiteidxScore1Spinbox.value(),
            self.widget.settingsSiteidxScore2Spinbox.value(),
            self.widget.settingsSiteidxScore3Spinbox.value(),
        ]
        scores_cost = [
            self.widget.settingsCostScore1Spinbox.value(),
            self.widget.settingsCostScore2Spinbox.value(),
            self.widget.settingsCostScore3Spinbox.value(),
        ]
        scores_distance = [
            self.widget.settingsDistanceScore1Spinbox.value(),
            self.widget.settingsDistanceScore2Spinbox.value(),
            self.widget.settingsDistanceScore3Spinbox.value(),
        ]
        scores_shc = [
            self.widget.settingsShcScore1Spinbox.value(),
            self.widget.settingsShcScore2Spinbox.value(),
            self.widget.settingsShcScore3Spinbox.value(),
        ]
        scores_slope = [
            self.widget.settingsSlopeScore1Spinbox.value(),
            self.widget.settingsSlopeScore2Spinbox.value(),
            self.widget.settingsSlopeScore3Spinbox.value(),
        ]
        scores_savearea = [
            self.widget.settingsSaveareaScore1Spinbox.value(),
            self.widget.settingsSaveareaScore2Spinbox.value()
        ]

        digits = 5  # 小数点桁丸め

        return {
            "scores_siteidx": scores_siteidx,
            "scores_cost": scores_cost,
            "scores_distance": scores_distance,
            "scores_shc": scores_shc,
            "scores_slope": scores_slope,
            "scores_savearea": scores_savearea,
            "siteidx_sugi_params": [
                round(self.widget.settingsSiteidxSugiBaseSpinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiNpp1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiNpp2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiSrad1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiSrad2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiVtex1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxSugiVtex2Spinbox.value(), digits)],
            "siteidx_hinoki_params": [
                round(self.widget.settingsSiteidxHinokiBaseSpinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiNpp1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiNpp2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiSrad1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiSrad2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiVtex1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxHinokiVtex2Spinbox.value(), digits)],
            "siteidx_karamatsu_params": [
                round(self.widget.settingsSiteidxKaramatsuBaseSpinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuNpp1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuNpp2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuSrad1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuSrad2Spinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuVtex1Spinbox.value(), digits),
                round(self.widget.settingsSiteidxKaramatsuVtex2Spinbox.value(), digits)],
            "ruggedness_param": self.widget.settingsRuggednessSpinbox.value(),
            "shc_param": self.widget.settingsShcSpinbox.value(),
            "cost_algorithm": "ruggedness" if self.widget.costAlgoRuggednessRadio.isChecked() else "shc"
        }

    def store_settings(self):
        smanager = SettingsManager()
        new_settings = self.make_settings_dict()
        smanager.store_settings(new_settings)
        self.main.scoring.set_scoring_score_labels_from_settings()
        QMessageBox.information(self.widget, "完了", "設定を保存しました")

    def write_settings_to_file(self):
        current_settings = self.make_settings_dict()
        output_filepath = QFileDialog.getSaveFileName(self.widget,
                                                      "設定ファイルの保存先を選択",
                                                      "settings.json",
                                                      "*.json")[0]

        if output_filepath == "":
            return

        with open(output_filepath, mode='w') as f:
            json.dump(current_settings, f, indent=2)

        QMessageBox.information(self.widget, "完了", "設定ファイルを保存しました")

    def read_settings_from_file(self):
        settings_filepath = QFileDialog.getOpenFileName(self.widget,
                                                        "設定ファイルを選択",
                                                        None,
                                                        "*.json")[0]

        if settings_filepath == "":
            return

        try:
            with open(settings_filepath) as f:
                settings_json = json.load(f)
        except Exception as e:
            QMessageBox.information(
                self.widget, "エラー", f"設定ファイルが正しいJSON形式ではありません\n{e}")
            return

        new_settings = DEFAULT_SETTINGS()
        # バリデーションに通った値だけデフォルト設定値に対して上書き（通常はすべての値が上書きされる）
        for key in DEFAULT_SETTINGS().keys():
            if settings_json.get(key) is not None:
                if SettingsManager.validate_setting(key, settings_json[key]) is None:
                    new_settings[key] = settings_json[key]

        self.set_ui_values(new_settings)
        QMessageBox.information(self.widget, "完了", "設定ファイルを読み込みました\nまだ保存はされていません")
