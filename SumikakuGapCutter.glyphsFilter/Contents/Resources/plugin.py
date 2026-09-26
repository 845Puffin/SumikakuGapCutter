# -*- coding: utf-8 -*-
from __future__ import division, print_function, unicode_literals

"""
SumikakuGapCutter Filter Plugin
================================
Glyphs 3 / 4 向け対話型フィルタプラグイン。
上層（選択中パス）の周囲に一定幅のマージン（余白・隙間）を設け、
下層（未選択パス）を自動的にブーリアン差分（型抜き）します。

【今回の改修点】
1. スペースキーの黒塗りプレビュー表示およびアウトライン復帰の完全同期:
   - スペースキー（KeyCode 49）押下時（KeyDown）:
     テキストボックスからフォーカスを安全に退避し、編集ウィンドウ（doc_win）を Key Window にして
     Space KeyDown を伝達。Glyphs ネイティブの黒塗りプレビューを確実に発火。
   - スペースキーを離した際（KeyUp）:
     イベントを握り潰さずに doc_win、tab、graphicView へ確実に KeyUp を伝達し、
     通常のアウトライン表示へ確実に復帰。
   - スペースキー長押し中（黒塗りプレビュー中）に上下矢印キー（↑ / ↓）を押すと、
     黒塗りプレビューのままリアルタイムに隙間幅（1.0 ずつ、Shiftで 10.0 ずつ）を更新。
2. エンターキー（Return / Enter）によるOK実行:
   - Return（KeyCode 36）および Enter（KeyCode 76）で即座にOK確定。Escapeでキャンセル。
3. 意図しないパスの結合・一体化（Remove Overlap）の完全防止:
   - 下層パスを1つずつ単独で差分演算。他パスとの勝手な合体を防止。
   - マスクと交差していないパスは一切のブーリアン処理を通さず100%無加工保持。
4. 端点形状 Butt（平頭）固定化、常時最前面フロート表示。

動作環境: Glyphs 3 / Glyphs 4 (Python 3, PyObjC)
作成先: SumikakuGapCutter.glyphsFilter/Contents/Resources/plugin.py
"""

import sys
import traceback
import objc
from AppKit import (
    NSClassFromString,
    NSFloatingWindowLevel,
    NSEvent,
)
from Foundation import NSMutableArray, NSArray
from GlyphsApp import Glyphs, GSPath, Message
from GlyphsApp.plugins import FilterWithoutDialog

try:
    import vanilla
except ImportError:
    vanilla = None

SETTING_PREFIX = "com.rikitakahashi.SumikakuGapCutter."
DEFAULT_MARGIN = 20.0
FIXED_CAP_STYLE = 0  # 0: Butt (平頭固定)


# ==============================================================================
# 幾何演算コアロジック (パスの独立保持・個別型抜き)
# ==============================================================================
def is_path_selected(path, layer=None):
    """パス全体、またはパス上のノードが1つ以上選択されているかを判定"""
    if getattr(path, "selected", False):
        return True
    if hasattr(path, "nodes") and any(getattr(node, "selected", False) for node in path.nodes):
        return True
    if layer is not None and hasattr(layer, "selection") and layer.selection:
        if path in layer.selection:
            return True
        if hasattr(path, "nodes") and any(node in layer.selection for node in path.nodes):
            return True
    return False


def rects_intersect(r1, r2):
    """2つの NSRect (origin, size) が交差（接触含む）しているかを判定"""
    try:
        min_x1 = r1.origin.x
        max_x1 = r1.origin.x + r1.size.width
        min_y1 = r1.origin.y
        max_y1 = r1.origin.y + r1.size.height

        min_x2 = r2.origin.x
        max_x2 = r2.origin.x + r2.size.width
        min_y2 = r2.origin.y
        max_y2 = r2.origin.y + r2.size.height

        if max_x1 < min_x2 or min_x1 > max_x2:
            return False
        if max_y1 < min_y2 or min_y1 > max_y2:
            return False
        return True
    except Exception:
        return True


def path_intersects_mask_bounds(path, mask_paths):
    """
    パスのバウンディングボックスが、マスク形状（いずれかのパス）のバウンディングボックスと交差するか判定。
    交差していない場合はブーリアン演算を完全にスキップして無加工で保持できる。
    """
    if not hasattr(path, "bounds"):
        return True
    p_bounds = path.bounds
    for m in mask_paths:
        if hasattr(m, "bounds"):
            if rects_intersect(p_bounds, m.bounds):
                return True
    return False


def generate_mask_paths(top_paths, margin, cap_style=0, parent_layer=None):
    """
    上層パス群を外側に margin ユニット分オフセットしたマスク形状（GSPath群）を生成。
    端点形状（cap_style）は 0: Butt 固定。
    """
    errors = []
    mask_paths = []

    OffsetCurve = (
        objc.lookUpClass("GlyphsFilterOffsetCurve")
        or NSClassFromString("GlyphsFilterOffsetCurve")
        or objc.lookUpClass("GSOffsetCurve")
        or NSClassFromString("GSOffsetCurve")
    )

    for path in top_paths:
        is_open = not getattr(path, "closed", True)

        if is_open:
            make_stroke = True
            position = 0.5
            base_stroke_width = getattr(path, "strokeWidth", 0) or 0
            try:
                if not base_stroke_width and hasattr(path, "attributes") and path.attributes:
                    base_stroke_width = path.attributes.get("strokeWidth", 0) or 0
            except Exception:
                base_stroke_width = 0
            offset_x = float(base_stroke_width) + float(margin) * 2.0
            offset_y = float(base_stroke_width) + float(margin) * 2.0
        else:
            make_stroke = False
            position = 0.0
            offset_x = float(margin)
            offset_y = float(margin)

        path_expanded = False

        # ----------------------------------------------------------------------
        # Tier 1: offsetPath (NSMutableArray を渡して結果を格納)
        # ----------------------------------------------------------------------
        if OffsetCurve:
            targets = [OffsetCurve]
            try:
                inst = OffsetCurve.alloc().init()
                if inst:
                    targets.append(inst)
            except Exception:
                pass

            for target in targets:
                # 1-A: 8引数
                if hasattr(target, "offsetPath_offsetX_offsetY_makeStroke_position_objects_capStyleStart_capStyleEnd_"):
                    try:
                        ret_objs = NSMutableArray.array()
                        res = target.offsetPath_offsetX_offsetY_makeStroke_position_objects_capStyleStart_capStyleEnd_(
                            path,
                            float(offset_x),
                            float(offset_y),
                            bool(make_stroke),
                            float(position),
                            ret_objs,
                            int(cap_style),
                            int(cap_style),
                        )
                        found = []
                        if res:
                            for p in res:
                                found.append(p.copy())
                        if ret_objs:
                            for p in ret_objs:
                                found.append(p.copy())
                        if found:
                            mask_paths.extend(found)
                            path_expanded = True
                            break
                    except Exception as e:
                        errors.append("Tier 1-A failed: {}".format(e))

                # 1-B: 12引数 (GSOffsetCurve)
                if hasattr(target, "offsetPath_offsetX_offsetY_makeStroke_position_capStyleStart_capStyleEnd_join_italicAngle_grid_keepCompatibleOutlines_extraHandles_"):
                    try:
                        res = target.offsetPath_offsetX_offsetY_makeStroke_position_capStyleStart_capStyleEnd_join_italicAngle_grid_keepCompatibleOutlines_extraHandles_(
                            path,
                            float(offset_x),
                            float(offset_y),
                            bool(make_stroke),
                            float(position),
                            int(cap_style),
                            int(cap_style),
                            0,
                            0.0,
                            1.0,
                            False,
                            None
                        )
                        if res:
                            for p in res:
                                mask_paths.append(p.copy())
                            path_expanded = True
                            break
                    except Exception as e:
                        errors.append("Tier 1-B failed: {}".format(e))

        # ----------------------------------------------------------------------
        # Tier 2: Glyphs.filters インスタンスによる processLayer_withArguments_
        # ----------------------------------------------------------------------
        if not path_expanded:
            the_filter = None
            if hasattr(Glyphs, "filters") and Glyphs.filters:
                for f in Glyphs.filters:
                    if "OffsetCurve" in f.__class__.__name__:
                        the_filter = f
                        break
            if not the_filter and OffsetCurve:
                try:
                    the_filter = OffsetCurve.alloc().init()
                except Exception:
                    pass

            if the_filter and hasattr(the_filter, "processLayer_withArguments_"):
                try:
                    temp_layer = parent_layer.copy() if parent_layer else None
                    if not temp_layer:
                        temp_layer = Glyphs.font.selectedLayers[0].copy() if (Glyphs.font and Glyphs.font.selectedLayers) else None

                    if temp_layer:
                        if hasattr(temp_layer, "shapes"):
                            temp_layer.shapes = [path.copy()]
                        else:
                            temp_layer.paths = [path.copy()]

                        args = ['', str(offset_x), str(offset_y), '1' if make_stroke else '0', str(position)]
                        the_filter.processLayer_withArguments_(temp_layer, args)

                        out_paths = list(temp_layer.paths) if hasattr(temp_layer, "paths") else []
                        if out_paths:
                            for p in out_paths:
                                mask_paths.append(p.copy())
                            path_expanded = True
                except Exception as e:
                    errors.append("Tier 2 failed: {}".format(e))

        # ----------------------------------------------------------------------
        # Tier 3: offsetLayer 直接呼び出し
        # ----------------------------------------------------------------------
        if not path_expanded and OffsetCurve:
            targets = [OffsetCurve]
            try:
                inst = OffsetCurve.alloc().init()
                if inst:
                    targets.append(inst)
            except Exception:
                pass

            for target in targets:
                if hasattr(target, "offsetLayer_offsetX_offsetY_makeStroke_autoStroke_position_metrics_error_shadow_capStyleStart_capStyleEnd_join_keepCompatibleOutlines_"):
                    try:
                        temp_layer = parent_layer.copy() if parent_layer else None
                        if not temp_layer:
                            temp_layer = Glyphs.font.selectedLayers[0].copy() if (Glyphs.font and Glyphs.font.selectedLayers) else None

                        if temp_layer:
                            if hasattr(temp_layer, "shapes"):
                                temp_layer.shapes = [path.copy()]
                            else:
                                temp_layer.paths = [path.copy()]

                            target.offsetLayer_offsetX_offsetY_makeStroke_autoStroke_position_metrics_error_shadow_capStyleStart_capStyleEnd_join_keepCompatibleOutlines_(
                                temp_layer,
                                float(offset_x),
                                float(offset_y),
                                bool(make_stroke),
                                False,
                                float(position),
                                None,
                                None,
                                None,
                                int(cap_style),
                                int(cap_style),
                                0,
                                False
                            )
                            if temp_layer.paths:
                                for p in temp_layer.paths:
                                    mask_paths.append(p.copy())
                                path_expanded = True
                                break
                    except Exception as e:
                        errors.append("Tier 3-A failed: {}".format(e))

    if not mask_paths and errors:
        print("=== SumikakuGapCutter Mask Generation Errors ===")
        for err in errors:
            print(err)

    return mask_paths, errors


def union_paths(paths):
    """マスク形状群の重複部分のみを結合（カッター形状の整理用）"""
    if not paths or len(paths) <= 1:
        return paths

    GSPathOperator = objc.lookUpClass("GSPathOperator") or NSClassFromString("GSPathOperator")
    if not GSPathOperator:
        return paths

    paths_array = NSMutableArray.arrayWithArray_([p.copy() for p in paths])
    try:
        if hasattr(GSPathOperator, "removeOverlapPaths_error_"):
            GSPathOperator.removeOverlapPaths_error_(paths_array, None)
        else:
            op = GSPathOperator.alloc().init()
            op.removeOverlapPaths_error_(paths_array, None)
        return list(paths_array)
    except Exception as e:
        print("SumikakuGapCutter Union Error on mask: {}".format(e))
        return paths


def subtract_mask_from_single_path(bottom_path, mask_paths):
    """
    【重要】1つの下層パスに対してのみマスク形状をブーリアン差分（Subtract）適用。
    決して複数の下層パスを同じ target_array にまとめて渡さないことで、
    無関係なパス同士が勝手に結合・一体化（Remove Overlap）されるのを完全に防止します。
    """
    if not mask_paths:
        return [bottom_path.copy()]

    GSPathOperator = objc.lookUpClass("GSPathOperator") or NSClassFromString("GSPathOperator")
    if not GSPathOperator:
        raise RuntimeError("GSPathOperator クラスが見つかりませんでした。")

    # 対象配列には「この下層パス1つだけ」を格納
    target_array = NSMutableArray.arrayWithObject_(bottom_path.copy())
    cutter_array = NSArray.arrayWithArray_([p.copy() for p in mask_paths])

    try:
        if hasattr(GSPathOperator, "subtractPaths_from_error_"):
            GSPathOperator.subtractPaths_from_error_(cutter_array, target_array, None)
        else:
            op = GSPathOperator.alloc().init()
            op.subtractPaths_from_error_(cutter_array, target_array, None)

        # 型抜きによって分割・残存したパス群（有効なパスのみ）
        results = [p for p in target_array if len(getattr(p, "nodes", [])) >= 2]
        return results

    except Exception as e:
        print("SumikakuGapCutter Subtract Error on single path: {}".format(e))
        return [bottom_path.copy()]


def execute_cutout_from_source_paths(source_paths, selected_indices, margin=20.0, cap_style=0, parent_layer=None):
    """
    起動時に記憶した元パス配列（source_paths）と選択インデックス（selected_indices）を用いて
    型抜き処理を行い、各ストロークの独立性を維持したまま再構築します。

    - 接触していない別々のパス同士の勝手な結合（Union）を完全に排除
    - 交差していないパスは無加工で100%保持
    - 上層パス群も個別のGSPathとしてそのまま維持
    """
    if not source_paths or len(source_paths) < 2:
        return None, None

    # 上層パス群（マスク生成用）を抽出
    top_paths = [source_paths[i].copy() for i in selected_indices if i < len(source_paths)]
    if not top_paths:
        return None, None

    try:
        mask_paths, errors = generate_mask_paths(top_paths, margin, cap_style=cap_style, parent_layer=parent_layer)
        if not mask_paths:
            return None, None

        # マスク同士が重なる場合に不要なスリットや反転が起きないよう、カッターマスクのみ結合
        unified_mask = union_paths(mask_paths)

        final_paths = []
        top_paths_to_select = []

        # 元のパス順序を保持しながら、各パスを個別に評価・処理
        for i, p in enumerate(source_paths):
            if i in selected_indices:
                # 【上層パス】ブーリアン処理を通さず、元の独立した個別GSPathとしてそのまま格納
                top_copy = p.copy()
                final_paths.append(top_copy)
                top_paths_to_select.append(top_copy)
            else:
                # 【下層パス】
                if not path_intersects_mask_bounds(p, unified_mask):
                    # マスクと交差していないパスは、ブーリアン処理を一切介さず100%無加工で保持
                    final_paths.append(p.copy())
                else:
                    # 交差しているパスのみ、単独で差分演算を実行（他パスとの合体を物理的に防ぐ）
                    cut_pieces = subtract_mask_from_single_path(p, unified_mask)
                    final_paths.extend(cut_pieces)

        return final_paths, top_paths_to_select

    except Exception as e:
        traceback.print_exc()
        return None, None


# ==============================================================================
# 対話型ダイアログクラス (スペースキー長押しプレビュー・上下キー増減・EnterでOK)
# ==============================================================================
class SumikakuGapCutterInteractiveDialog(object):
    """
    メニュー（Filter > SumikakuGapCutter...）実行ごとに開く対話型ダイアログ。
    - スペースキー長押し中: Glyphsの黒塗りプレビューを表示
    - スペースキー長押し中にも ↑ / ↓ キーでリアルタイムにマージン値を変更可能
    - スペースキーを離すと通常のアウトライン表示へ確実に復帰
    - 上下矢印キー（↑ / ↓）でパラメータ値を 1.0 ずつ即時増減（Shift: 10.0, Option: 0.1）
    - エンターキー（Return / Enter）で即座にOK（適用）を実行
    - エスケープキー（Escape）でキャンセル実行
    - 端点形状は Butt 固定（UI項目削除）
    - NSFloatingWindowLevel による常時最前面フロート表示
    """

    def __init__(self, layer):
        if not layer:
            return

        self.layer = layer
        self.glyph = layer.parent
        self.is_applied = False
        self.key_monitor = None
        self.is_space_down = False

        # 1. 起動時点のレイヤー内パス一覧を取得
        layer_paths = list(layer.paths) if hasattr(layer, "paths") else []
        if len(layer_paths) < 2:
            Message("レイヤー内に2つ以上のパスが存在しません。", title="SumikakuGapCutter")
            return

        # 2. 起動前の元パス群・元形状のスナップショットを不変参照としてクローン保持
        self.original_paths = [p.copy() for p in layer_paths]
        if hasattr(layer, "shapes"):
            self.other_shapes = [s.copy() for s in layer.shapes if not isinstance(s, GSPath)]
        else:
            self.other_shapes = []

        # 3. 起動時（ダイアログ表示直前）の時点で選択されているパスのインデックスを確実に記録
        self.selected_path_indices = []
        for i, p in enumerate(layer_paths):
            selected = False
            if getattr(p, "selected", False):
                selected = True
            elif hasattr(p, "nodes") and any(getattr(n, "selected", False) for n in p.nodes):
                selected = True
            elif hasattr(layer, "selection") and layer.selection:
                if p in layer.selection:
                    selected = True
                elif hasattr(p, "nodes") and any(n in layer.selection for n in p.nodes):
                    selected = True
            if selected:
                self.selected_path_indices.append(i)

        # 選択パスが見つからない場合は最前面（末尾）パスをデフォルトとして扱う
        if not self.selected_path_indices:
            self.selected_path_indices = [len(self.original_paths) - 1]
        elif len(self.selected_path_indices) >= len(self.original_paths):
            # すべて選択されていた場合は末尾のみを上層とし、残りを下層とする
            self.selected_path_indices = [len(self.original_paths) - 1]

        saved_margin = float(Glyphs.defaults.get(SETTING_PREFIX + "margin", DEFAULT_MARGIN))
        val_display = str(int(saved_margin)) if saved_margin == int(saved_margin) else str(round(saved_margin, 1))

        window_width = 290
        window_height = 135

        if vanilla:
            # フローティングウィンドウを作成
            self.w = vanilla.FloatingWindow(
                (window_width, window_height),
                "SumikakuGapCutter",
                minSize=(window_width, window_height),
                maxSize=(window_width, window_height),
            )

            # 1. Margin 設定行
            self.w.marginLabel = vanilla.TextBox((15, 18, 80, 20), "Margin (u):")
            self.w.marginInput = vanilla.EditText(
                (95, 15, 55, 22),
                text=val_display,
                callback=self.marginInputCallback
            )
            self.w.marginSlider = vanilla.Slider(
                (158, 16, 117, 20),
                minValue=1.0,
                maxValue=100.0,
                value=min(100.0, max(1.0, saved_margin)),
                callback=self.marginSliderCallback
            )

            # 2. プレビュー チェックボックス
            self.w.previewCheckbox = vanilla.CheckBox(
                (15, 52, 180, 22),
                "プレビュー (Preview)",
                value=True,
                callback=self.togglePreview
            )

            # 3. キャンセル & OK ボタン
            self.w.cancelButton = vanilla.Button(
                (95, 95, 85, 26),
                "キャンセル",
                callback=self.cancel
            )
            self.w.okButton = vanilla.Button(
                (190, 95, 85, 26),
                "OK",
                callback=self.apply
            )

            # デフォルトボタンの設定
            self.w.setDefaultButton(self.w.okButton)
            try:
                ok_ns_btn = self.w.okButton.getNSButton()
                if ok_ns_btn:
                    ok_ns_btn.setKeyEquivalent_("\r")
                cancel_ns_btn = self.w.cancelButton.getNSButton()
                if cancel_ns_btn:
                    cancel_ns_btn.setKeyEquivalent_("\033")
            except Exception:
                pass

            self.w.bind("close", self.windowWillClose)

            # ウィンドウの表示と最前面フローティング維持設定
            self.w.open()
            ns_window = self.w.getNSWindow()
            if ns_window:
                ns_window.setLevel_(NSFloatingWindowLevel)
                ns_window.setHidesOnDeactivate_(False)
                if hasattr(ns_window, "setFloatingPanel_"):
                    ns_window.setFloatingPanel_(True)
                if hasattr(ns_window, "setBecomesKeyOnlyIfNeeded_"):
                    ns_window.setBecomesKeyOnlyIfNeeded_(False)
                # 起動直後にテキスト入力欄へ勝手にフォーカスが入らないようにする
                ns_window.makeFirstResponder_(None)
                ns_window.makeKeyWindow()

            # キーボード操作（KeyDown: 1024, KeyUp: 2048）のローカル監視ハンドラを登録
            try:
                self.key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    (1 << 10) | (1 << 11),  # NSKeyDownMask | NSKeyUpMask
                    self.handleKeyEvent_
                )
            except Exception:
                self.key_monitor = None

            # 初期プレビューを実行
            self.updatePreview()

    # --------------------------------------------------------------------------
    # キーボードイベントハンドラ (Space長押し対応, 上下キー増減, EnterでOK, Escapeでキャンセル)
    # --------------------------------------------------------------------------
    def handleKeyEvent_(self, event):
        try:
            ev_type = event.type()
            k_code = event.keyCode()

            # ------------------------------------------------------------------
            # 1. スペースキー (KeyCode 49): 編集ビューの黒塗りプレビュー切替
            # ------------------------------------------------------------------
            if k_code == 49:
                font = Glyphs.font
                tab = font.currentTab if font and hasattr(font, "currentTab") else None
                edit_view = getattr(tab, "graphicView", lambda: None)() if tab else None
                doc_win = edit_view.window() if edit_view and hasattr(edit_view, "window") else None

                if ev_type == 10:  # NSKeyDown
                    if not getattr(self, "is_space_down", False):
                        self.is_space_down = True
                        ns_window = self.w.getNSWindow() if hasattr(self, "w") else None
                        if ns_window:
                            fr = ns_window.firstResponder()
                            if hasattr(self, "w") and hasattr(self.w, "marginInput"):
                                tf = self.w.marginInput.getNSTextField()
                                if fr == tf or (hasattr(tf, "currentEditor") and fr == tf.currentEditor()):
                                    ns_window.makeFirstResponder_(None)

                        if doc_win:
                            doc_win.makeKeyWindow()
                            if edit_view:
                                doc_win.makeFirstResponder_(edit_view)
                            try:
                                doc_win.sendEvent_(event)
                            except Exception:
                                pass

                        if edit_view and hasattr(edit_view, "keyDown_"):
                            try:
                                edit_view.keyDown_(event)
                            except Exception:
                                pass

                        if tab and hasattr(tab, "keyDown_"):
                            try:
                                tab.keyDown_(event)
                            except Exception:
                                pass

                        if edit_view and hasattr(edit_view, "setNeedsDisplay_"):
                            try:
                                edit_view.setNeedsDisplay_(True)
                            except Exception:
                                pass

                        if hasattr(Glyphs, "redraw"):
                            Glyphs.redraw()

                    return event

                elif ev_type == 11:  # NSKeyUp (スペースを離した時: アウトライン表示へ復帰)
                    self.is_space_down = False

                    if doc_win:
                        try:
                            doc_win.sendEvent_(event)
                        except Exception:
                            pass
                        try:
                            if hasattr(doc_win, "keyUp_"):
                                doc_win.keyUp_(event)
                        except Exception:
                            pass

                    if tab:
                        try:
                            if hasattr(tab, "keyUp_"):
                                tab.keyUp_(event)
                        except Exception:
                            pass
                        try:
                            wc = tab.windowController() if hasattr(tab, "windowController") else None
                            if wc and hasattr(wc, "keyUp_"):
                                wc.keyUp_(event)
                        except Exception:
                            pass

                    if edit_view:
                        try:
                            if hasattr(edit_view, "keyUp_"):
                                edit_view.keyUp_(event)
                        except Exception:
                            pass
                        try:
                            if hasattr(edit_view, "setNeedsDisplay_"):
                                edit_view.setNeedsDisplay_(True)
                        except Exception:
                            pass

                    if hasattr(Glyphs, "redraw"):
                        Glyphs.redraw()

                    # イベントをそのまま通してGlyphsネイティブのキーアップを完了させる
                    return event

            # 以下のキーは KeyDown (10) のみ処理
            if ev_type != 10:
                return event

            # ------------------------------------------------------------------
            # 2. 上下矢印キー (KeyCode 126: Up, 125: Down): マージン数値の増減
            # スペースキー長押し中（黒塗りプレビュー中）でも確実に動作！
            # ------------------------------------------------------------------
            if k_code in (126, 125):
                ns_window = self.w.getNSWindow() if hasattr(self, "w") else None
                if ns_window and ns_window.isVisible():
                    is_up = (k_code == 126)
                    step = 1.0
                    try:
                        flags = event.modifierFlags()
                        if flags & (1 << 17):    # Shiftキー: 10.0
                            step = 10.0
                        elif flags & (1 << 19):  # Optionキー: 0.1
                            step = 0.1
                    except Exception:
                        step = 1.0

                    self.stepMargin(step if is_up else -step)
                    return None

            # ------------------------------------------------------------------
            # 3. Enter / Return (KeyCode 36: Return, 76: Numpad Enter): OK確定
            # ------------------------------------------------------------------
            elif k_code in (36, 76):
                ns_window = self.w.getNSWindow() if hasattr(self, "w") else None
                if ns_window and ns_window.isVisible():
                    try:
                        if hasattr(self, "w") and hasattr(self.w, "marginInput"):
                            val_str = self.w.marginInput.get().replace(" ", "").strip()
                            if val_str:
                                Glyphs.defaults[SETTING_PREFIX + "margin"] = max(0.5, float(val_str))
                    except Exception:
                        pass
                    self.apply()
                    return None

            # ------------------------------------------------------------------
            # 4. Escape (KeyCode 53): キャンセル
            # ------------------------------------------------------------------
            elif k_code == 53:
                ns_window = self.w.getNSWindow() if hasattr(self, "w") else None
                if ns_window and ns_window.isVisible():
                    self.cancel()
                    return None

        except Exception:
            pass

        return event

    # --------------------------------------------------------------------------
    # 数値ステップ増減メソッド (上下キー連動)
    # --------------------------------------------------------------------------
    def stepMargin(self, delta):
        try:
            current_val = float(Glyphs.defaults.get(SETTING_PREFIX + "margin", DEFAULT_MARGIN))
            if hasattr(self, "w") and hasattr(self.w, "marginInput"):
                val_str = self.w.marginInput.get().replace(" ", "").strip()
                if val_str:
                    try:
                        current_val = float(val_str)
                    except ValueError:
                        pass

            new_val = max(0.5, round(current_val + delta, 1))
            Glyphs.defaults[SETTING_PREFIX + "margin"] = new_val

            val_display = str(int(new_val)) if new_val == int(new_val) else str(round(new_val, 1))

            if hasattr(self, "w") and hasattr(self.w, "marginInput"):
                self.w.marginInput.set(val_display)
                tf = self.w.marginInput.getNSTextField()
                if tf and hasattr(tf, "currentEditor") and tf.currentEditor():
                    editor = tf.currentEditor()
                    editor.setString_(val_display)
                    editor.selectAll_(None)

            if hasattr(self, "w") and hasattr(self.w, "marginSlider"):
                self.w.marginSlider.set(min(100.0, max(1.0, new_val)))

            # リアルタイムプレビューを更新
            self.updatePreview()

            # 編集ビューの再描画を促す
            font = Glyphs.font
            if font and hasattr(font, "currentTab") and font.currentTab:
                edit_view = getattr(font.currentTab, "graphicView", lambda: None)()
                if edit_view and hasattr(edit_view, "setNeedsDisplay_"):
                    edit_view.setNeedsDisplay_(True)

        except Exception as e:
            print("SumikakuGapCutter stepMargin error: {}".format(e))

    # --------------------------------------------------------------------------
    # UIコールバック群
    # --------------------------------------------------------------------------
    def marginInputCallback(self, sender):
        try:
            val_str = sender.get().replace(" ", "").strip()
            if val_str:
                val = max(0.5, float(val_str))
                Glyphs.defaults[SETTING_PREFIX + "margin"] = val
                if hasattr(self.w, "marginSlider"):
                    self.w.marginSlider.set(min(100.0, max(1.0, val)))
                self.updatePreview()
        except Exception:
            pass

    def marginSliderCallback(self, sender):
        try:
            ns_window = self.w.getNSWindow() if hasattr(self, "w") else None
            if ns_window:
                ns_window.makeFirstResponder_(None)

            val = round(float(sender.get()), 1)
            Glyphs.defaults[SETTING_PREFIX + "margin"] = val
            val_display = str(int(val)) if val == int(val) else str(round(val, 1))
            if hasattr(self.w, "marginInput"):
                self.w.marginInput.set(val_display)
            self.updatePreview()
        except Exception:
            pass

    def togglePreview(self, sender=None):
        self.updatePreview()

    # --------------------------------------------------------------------------
    # プレビュー更新および形状復元処理
    # --------------------------------------------------------------------------
    def updatePreview(self):
        if not self.layer:
            return

        is_preview_on = True
        if hasattr(self, "w") and hasattr(self.w, "previewCheckbox"):
            is_preview_on = bool(self.w.previewCheckbox.get())

        if is_preview_on:
            margin = float(Glyphs.defaults.get(SETTING_PREFIX + "margin", DEFAULT_MARGIN))
            final_paths, top_to_select = execute_cutout_from_source_paths(
                self.original_paths,
                self.selected_path_indices,
                margin=margin,
                cap_style=FIXED_CAP_STYLE,
                parent_layer=self.layer
            )
            if final_paths is not None:
                new_shapes = final_paths + [s.copy() for s in self.other_shapes]
                if hasattr(self.layer, "shapes"):
                    self.layer.shapes = new_shapes
                else:
                    self.layer.paths = final_paths
                # 上層パスのみを選択状態として表示
                for p in top_to_select:
                    p.selected = True
            else:
                self.restoreOriginal()
        else:
            self.restoreOriginal()

        if hasattr(Glyphs, "redraw"):
            Glyphs.redraw()

        font = Glyphs.font
        if font and hasattr(font, "currentTab") and font.currentTab:
            edit_view = getattr(font.currentTab, "graphicView", lambda: None)()
            if edit_view and hasattr(edit_view, "setNeedsDisplay_"):
                edit_view.setNeedsDisplay_(True)

    def restoreOriginal(self):
        """レイヤーを起動前の初期スナップショットおよび選択状態へ完全復元"""
        if hasattr(self.layer, "shapes"):
            self.layer.shapes = [p.copy() for p in self.original_paths] + [s.copy() for s in self.other_shapes]
        else:
            self.layer.paths = [p.copy() for p in self.original_paths]

        # 起動時の選択状態（パス単位およびノード単位）を復元
        try:
            current_paths = list(self.layer.paths)
            for i, p in enumerate(current_paths):
                is_sel = (i in self.selected_path_indices)
                p.selected = is_sel
                if hasattr(p, "nodes"):
                    for n in p.nodes:
                        n.selected = is_sel
        except Exception:
            pass

    # --------------------------------------------------------------------------
    # 完了アクション（キャンセル / 適用）
    # --------------------------------------------------------------------------
    def cancel(self, sender=None):
        """キャンセル: 変更を破棄して起動時のパス形状・選択状態に完全復帰"""
        if self.is_applied:
            return
        self.cleanupMonitor()
        self.restoreOriginal()
        if hasattr(Glyphs, "redraw"):
            Glyphs.redraw()
        if hasattr(self, "w"):
            self.w.close()

    def apply(self, sender=None):
        """OK: 起動時のインデックスに基づいて型抜きを確定し、Undo履歴に正しく記録"""
        if self.is_applied:
            return
        self.is_applied = True
        self.cleanupMonitor()

        # 一度元形状に戻してから beginUndo / endUndo トランザクション内で確定
        self.restoreOriginal()

        margin = float(Glyphs.defaults.get(SETTING_PREFIX + "margin", DEFAULT_MARGIN))

        if self.glyph:
            self.glyph.beginUndo()

        try:
            final_paths, top_to_select = execute_cutout_from_source_paths(
                self.original_paths,
                self.selected_path_indices,
                margin=margin,
                cap_style=FIXED_CAP_STYLE,
                parent_layer=self.layer
            )
            if final_paths is not None:
                new_shapes = final_paths + [s.copy() for s in self.other_shapes]
                if hasattr(self.layer, "shapes"):
                    self.layer.shapes = new_shapes
                else:
                    self.layer.paths = final_paths
                # 上層パスのみを選択状態にして完了
                for p in top_to_select:
                    p.selected = True
        finally:
            if self.glyph:
                self.glyph.endUndo()

        if hasattr(Glyphs, "redraw"):
            Glyphs.redraw()

        if hasattr(self, "w"):
            self.w.close()

    def cleanupMonitor(self):
        """キーイベント監視を解除し、プレビューモードが残っていれば確実に終了"""
        if getattr(self, "is_space_down", False):
            self.is_space_down = False
            font = Glyphs.font
            tab = font.currentTab if font and hasattr(font, "currentTab") else None
            edit_view = getattr(tab, "graphicView", lambda: None)() if tab else None
            doc_win = edit_view.window() if edit_view and hasattr(edit_view, "window") else None
            if doc_win:
                try:
                    dummy_event = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                        11, (0, 0), 0, 0, 0, None, " ", " ", False, 49
                    )
                    doc_win.sendEvent_(dummy_event)
                except Exception:
                    pass
            if edit_view and hasattr(edit_view, "setNeedsDisplay_"):
                try:
                    edit_view.setNeedsDisplay_(True)
                except Exception:
                    pass
            if hasattr(Glyphs, "redraw"):
                Glyphs.redraw()

        if hasattr(self, "key_monitor") and self.key_monitor:
            try:
                NSEvent.removeMonitor_(self.key_monitor)
            except Exception:
                pass
            self.key_monitor = None

    def windowWillClose(self, sender=None):
        """ウィンドウのクローズボタンやEscキーで閉じられた場合の安全ガード"""
        self.cleanupMonitor()
        if not self.is_applied:
            self.restoreOriginal()
            if hasattr(Glyphs, "redraw"):
                Glyphs.redraw()


# ==============================================================================
# プラグイン本体 (FilterWithoutDialog: 起動時クラッシュ完全防止)
# ==============================================================================
class SumikakuGapCutterFilter(FilterWithoutDialog):

    current_dialog = None

    @objc.python_method
    def settings(self):
        self.menuName = Glyphs.localize({
            'en': 'SumikakuGapCutter...',
            'ja': 'SumikakuGapCutter...',
        })
        self.keyboardShortcut = None

    @objc.python_method
    def filter(self, layer, inEditView, customParameters):
        # 1. カスタムパラメータ（フォント書き出し等）で呼ばれた場合
        if customParameters:
            margin = customParameters.get("margin", DEFAULT_MARGIN)
            cap_style = FIXED_CAP_STYLE
            if layer and hasattr(layer, "paths") and len(layer.paths) >= 2:
                source_paths = [p.copy() for p in layer.paths]
                selected_indices = [len(source_paths) - 1]
                final_paths, _ = execute_cutout_from_source_paths(
                    source_paths,
                    selected_indices,
                    margin=float(margin),
                    cap_style=cap_style,
                    parent_layer=layer
                )
                if final_paths is not None:
                    other = [s.copy() for s in layer.shapes if not isinstance(s, GSPath)] if hasattr(layer, "shapes") else []
                    if hasattr(layer, "shapes"):
                        layer.shapes = final_paths + other
                    else:
                        layer.paths = final_paths
            return

        # 2. メニューから実行された場合: 対象レイヤーに対して対話型ダイアログを開く
        target_layer = layer
        if not target_layer:
            font = Glyphs.font
            if font and font.selectedLayers:
                target_layer = font.selectedLayers[0]

        if not target_layer:
            Message("対象グリフまたはレイヤーが選択されていません。", title="SumikakuGapCutter")
            return

        SumikakuGapCutterFilter.current_dialog = SumikakuGapCutterInteractiveDialog(target_layer)

    @objc.python_method
    def __file__(self):
        return __file__
