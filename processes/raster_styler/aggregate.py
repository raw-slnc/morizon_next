# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from ...constants import ZONING_COLORS


# 地面（背景の地図）を隠さないよう、塗りは不透明度60%にする（外周線と斜線は不透明のまま）
FILL_OPACITY = 0.6
# 外周線は濃い紺。背景の等高線（茶系）や、重ねた森林計画図の小班線（黒）と見分けやすく、
# 区分3・4の水色・青の塗りの上でも縁が見える。赤は「災害リスク高」の斜線の意味なので使わない
OUTLINE_COLOR = "31,78,156,255"


def _fill_color(hex_color: str) -> str:
    """#rrggbb を QGIS のスタイルの r,g,b,a（不透明度 FILL_OPACITY）にする"""
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"{r},{g},{b},{round(255 * FILL_OPACITY)}"


# 「針葉樹のフィーチャーだけで収益性を判定する」を使ったとき、区分の色は針葉樹のポリゴンだけに付け、
# それ以外のポリゴンは黄みのある灰色（不透明度8%）で塗り、外周線は他の区分と同じ線で描く（区分の対象外であることを示す）。
# 集計の値（_majority など）は変えず、表示の条件だけで分ける。条件は元のポリゴンの列と選んだ値で書く
_CONIFER_RULE_KEY = "{3c0e8f6a-5b7d-4e29-9a61-0d2b7f4c8e15}"


def _conifer_condition(conifer) -> str:
    """(列名, 針葉樹とみなす値) から、針葉樹のポリゴンに当てはまる式（XML 用に書き換え済み）を作る。
    値は文字列として比べる（集計に使った値の一覧と同じ扱い）"""
    import html
    field, values = conifer
    quoted_field = '"' + field.replace('"', '""') + '"'
    literals = ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values))
    return html.escape(f"to_string({quoted_field}) IN ({literals})", quote=True)


_CONIFER_SYMBOL = f"""
      <symbol name="5" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="210,205,165,20"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="{OUTLINE_COLOR}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
        </layer>
      </symbol>"""


def write_qml(output_shp_path: str, threshold=0.3, conifer=None) -> str:
    """conifer：「針葉樹のフィーチャーだけで収益性を判定する」の (列名, 針葉樹とみなす値)。None なら使わない"""
    output_filepath = output_shp_path.replace(".shp", ".qml")
    if conifer:
        condition = _conifer_condition(conifer)
        only = f" AND {condition}"  # 区分の規則に足す条件（下の @ONLY@ に入れる）
        conifer_rule = (f' <rule filter="NOT coalesce({condition}, false)" label="針葉樹以外（区分なし）"'
                        f' symbol="5" key="{_CONIFER_RULE_KEY}"/>\n')
    else:
        only = conifer_rule = ""
    with open(output_filepath, mode="w") as f:
        f.write((
            """
<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis styleCategories="Symbology" version="3.16.16-Hannover">
  <renderer-v2 symbollevels="0" enableorderby="0" type="RuleRenderer" forceraster="0">
    <rules key="{6a3a95d2-314d-4000-98b7-31ceb5d2b96b}">
      <rule filter=" &quot;_majority&quot;  =  1 @ONLY@" label="最頻値：第1象限（災害リスクに注意）" symbol="0" key="{eb45d76d-6f58-45ad-84df-c1edae84be50}"/>
      <rule filter=" &quot;_majority&quot;  =  2 @ONLY@" label="最頻値：第2象限（林業経営適地）" symbol="1" key="{257ac20f-feb8-4aa5-bcc8-a07114ca49a5}"/>
      <rule filter=" &quot;_majority&quot;  =  3 @ONLY@" label="最頻値：第3象限（要収益性向上）" symbol="2" key="{83974236-af15-4a37-b0d4-d3da28f0b3ba}"/>
      <rule filter=" &quot;_majority&quot;  =  4 @ONLY@" label="最頻値：第4象限（災害に強い森林管理）" symbol="3" key="{dbe878d9-8f12-47f6-9969-fe00150b9ad8}"/>
"""
            + conifer_rule
            + f'<rule filter=" &quot;ratio_1_4&quot; >= {threshold / 100}" label="災害リスク高≧{threshold}%" symbol="4" '
            + ' key="{e8aa2cf8-9152-4a40-8a66-4bc0a62c2259}"/> '
            + f"""
    </rules>
    <symbols>
      <symbol name="0" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="{_fill_color(ZONING_COLORS[0])}"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="{OUTLINE_COLOR}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
        </layer>
      </symbol>
      <symbol name="1" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="{_fill_color(ZONING_COLORS[1])}"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="{OUTLINE_COLOR}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
        </layer>
      </symbol>
      <symbol name="2" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="{_fill_color(ZONING_COLORS[2])}"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="{OUTLINE_COLOR}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
        </layer>
      </symbol>
      <symbol name="3" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="{_fill_color(ZONING_COLORS[3])}"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="{OUTLINE_COLOR}"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
        </layer>
      </symbol>
      <symbol name="4" alpha="1" force_rhr="0" clip_to_extent="1" type="fill">
        <layer enabled="1" pass="0" class="LinePatternFill" locked="0">
          <prop k="angle" v="45"/>
          <prop k="color" v="255,0,0,255"/>
          <prop k="distance" v="1.2"/>
          <prop k="distance_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="distance_unit" v="MM"/>
          <prop k="line_width" v="0.26"/>
          <prop k="line_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="line_width_unit" v="MM"/>
          <prop k="offset" v="0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="outline_width_unit" v="MM"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
          <symbol name="@4@0" alpha="1" force_rhr="0" clip_to_extent="1" type="line">
            <layer enabled="1" pass="0" class="SimpleLine" locked="0">
              <prop k="align_dash_pattern" v="0"/>
              <prop k="capstyle" v="square"/>
              <prop k="customdash" v="5;2"/>
              <prop k="customdash_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="customdash_unit" v="MM"/>
              <prop k="dash_pattern_offset" v="0"/>
              <prop k="dash_pattern_offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="dash_pattern_offset_unit" v="MM"/>
              <prop k="draw_inside_polygon" v="0"/>
              <prop k="joinstyle" v="bevel"/>
              <prop k="line_color" v="255,0,0,255"/>
              <prop k="line_style" v="solid"/>
              <prop k="line_width" v="0.26"/>
              <prop k="line_width_unit" v="MM"/>
              <prop k="offset" v="0"/>
              <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="offset_unit" v="MM"/>
              <prop k="ring_filter" v="0"/>
              <prop k="tweak_dash_pattern_on_corners" v="0"/>
              <prop k="use_custom_dash" v="0"/>
              <prop k="width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <data_defined_properties>
                <Option type="Map">
                  <Option name="name" value="" type="QString"/>
                  <Option name="properties"/>
                  <Option name="type" value="collection" type="QString"/>
                </Option>
              </data_defined_properties>
            </layer>
          </symbol>
        </layer>
        <layer enabled="1" pass="0" class="LinePatternFill" locked="0">
          <prop k="angle" v="135"/>
          <prop k="color" v="0,0,255,255"/>
          <prop k="distance" v="1.2"/>
          <prop k="distance_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="distance_unit" v="MM"/>
          <prop k="line_width" v="0.26"/>
          <prop k="line_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="line_width_unit" v="MM"/>
          <prop k="offset" v="0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="outline_width_unit" v="MM"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
          <symbol name="@4@1" alpha="1" force_rhr="0" clip_to_extent="1" type="line">
            <layer enabled="1" pass="0" class="SimpleLine" locked="0">
              <prop k="align_dash_pattern" v="0"/>
              <prop k="capstyle" v="square"/>
              <prop k="customdash" v="5;2"/>
              <prop k="customdash_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="customdash_unit" v="MM"/>
              <prop k="dash_pattern_offset" v="0"/>
              <prop k="dash_pattern_offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="dash_pattern_offset_unit" v="MM"/>
              <prop k="draw_inside_polygon" v="0"/>
              <prop k="joinstyle" v="bevel"/>
              <prop k="line_color" v="255,0,4,255"/>
              <prop k="line_style" v="solid"/>
              <prop k="line_width" v="0.26"/>
              <prop k="line_width_unit" v="MM"/>
              <prop k="offset" v="0"/>
              <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <prop k="offset_unit" v="MM"/>
              <prop k="ring_filter" v="0"/>
              <prop k="tweak_dash_pattern_on_corners" v="0"/>
              <prop k="use_custom_dash" v="0"/>
              <prop k="width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
              <data_defined_properties>
                <Option type="Map">
                  <Option name="name" value="" type="QString"/>
                  <Option name="properties"/>
                  <Option name="type" value="collection" type="QString"/>
                </Option>
              </data_defined_properties>
            </layer>
          </symbol>
        </layer>
        <layer enabled="1" pass="0" class="SimpleFill" locked="0">
          <prop k="border_width_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="color" v="0,0,255,0"/>
          <prop k="joinstyle" v="bevel"/>
          <prop k="offset" v="0,0"/>
          <prop k="offset_map_unit_scale" v="3x:0,0,0,0,0,0"/>
          <prop k="offset_unit" v="MM"/>
          <prop k="outline_color" v="35,35,35,255"/>
          <prop k="outline_style" v="solid"/>
          <prop k="outline_width" v="0.2"/>
          <prop k="outline_width_unit" v="MM"/>
          <prop k="style" v="solid"/>
          <data_defined_properties>
            <Option type="Map">
              <Option name="name" value="" type="QString"/>
              <Option name="properties"/>
              <Option name="type" value="collection" type="QString"/>
            </Option>
          </data_defined_properties>
        </layer>
      </symbol>{_CONIFER_SYMBOL if conifer else ""}
    </symbols>
  </renderer-v2>
  <blendMode>0</blendMode>
  <featureBlendMode>0</featureBlendMode>
  <layerGeometryType>2</layerGeometryType>
</qgis>
    """
        ).replace("@ONLY@", only))

    return output_filepath
