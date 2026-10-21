"""真实菜单定位规则的完整分区流程回归；不发送桌面输入。"""
from _ctypes import COMError
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from bilibili_category import category_opener, category_option, read_menu, set_category


def node(name="", x=100, y=100, *, width=50, height=20, kind="TextControl", cls="", parent=None, offscreen=False):
    return NS(Name=name, ControlTypeName=kind, ClassName=cls, IsOffscreen=offscreen,
              BoundingRectangle=NS(left=x, top=y, right=x+width, bottom=y+height),
              GetParentControl=lambda: parent)


class CategoryTests(unittest.TestCase):
    def test_real_menu_accepts_new_category_names_without_losing_scroll_region(self):
        draft,_,current,popup,options=self.harness(wrapper=True)
        current.Name="vlog"
        for option,name in zip(options,("时尚美妆","家装房产","健身")):
            option.Name=name
        menu=read_menu(draft,[popup]+options)
        self.assertIs(menu.container,popup)
        self.assertEqual([c.Name for c in menu.options],["时尚美妆","家装房产","健身"])

    def test_visible_recommendation_under_popup_is_not_a_menu_option(self):
        draft,_,_,popup,options=self.harness(wrapper=True)
        tag_parent=node(kind="GroupControl",cls="hot-tag-container")
        recommendation=node("舞蹈",y=195,parent=tag_parent)
        menu=read_menu(draft,[popup]+options+[recommendation])
        self.assertEqual([c for c in menu.options if c.Name=="舞蹈"],[options[1]])

    def test_browser_bookmark_is_not_an_open_category_menu(self):
        draft,state,current,_,options=self.harness()
        toolbar=node(kind="ToolBarControl",cls="BookmarkBarView")
        bookmark=node("娱乐",x=100,y=20,kind="ListItemControl",cls="BookmarkFolderButton",parent=toolbar)
        read=draft.controls
        draft.controls=lambda:read()+[bookmark]
        self.assertIsNone(read_menu(draft))
        with patch("bilibili_category.time.sleep"):
            set_category(draft,"舞蹈")
        self.assertEqual([call.args[0] for call in draft.click.call_args_list],[current,options[1]])
        draft.scroll_in_control.assert_not_called()

    def harness(self, *, wrapper=False, delayed=0, readback_delay=0):
        draft = NS()
        label = node("分区", x=30)
        current = node("生活")
        popup = node(y=135, width=260, height=220, kind="GroupControl", cls="drop-list-v2-content-wrp" if wrapper else "")
        options = [node(name, y=150+i*45, parent=popup) for i, name in enumerate(("音乐", "舞蹈", "动画"))]
        state = {"open": False, "chosen": False, "reads": 0, "after_choose": 0, "now": 0}
        def clock():
            state["now"] += .1
            return state["now"]
        def controls():
            if state["chosen"]:
                state["after_choose"] += 1
                if state["after_choose"] > readback_delay:
                    current.Name = "舞蹈"
            result = [label, current]
            if state["open"]:
                state["reads"] += 1
                if state["reads"] > delayed:
                    result += ([popup] if wrapper else []) + options
            return result
        def click(control):
            if control is current:
                state["open"] = True
            elif control is options[1]:
                state["chosen"], state["open"] = True, False
            else:
                raise AssertionError("未识别的点击目标")
        draft.controls = controls
        draft.visible = lambda c: not c.IsOffscreen
        draft.input_column = lambda: 100
        draft.find = Mock(return_value=label)
        draft.click = Mock(side_effect=click)
        draft.check = Mock()
        draft.scroll_in_control = Mock(side_effect=AssertionError("可见目标不应滚动"))
        draft.scroll_page = Mock(side_effect=AssertionError("菜单不应滚整页"))
        draft.now = clock
        return draft, state, current, popup, options

    def test_complete_category_with_missing_wrapper_selects_visible_menu(self):
        draft, state, current, _, options = self.harness()
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        self.assertTrue(state["chosen"])
        self.assertEqual([call.args[0] for call in draft.click.call_args_list], [current, options[1]])
        draft.scroll_in_control.assert_not_called()
        draft.scroll_page.assert_not_called()

    def test_waits_for_delayed_menu_and_selected_value_without_reclick(self):
        draft, state, _, _, _ = self.harness(wrapper=True, delayed=3, readback_delay=4)
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        self.assertGreater(state["after_choose"], 4)
        self.assertEqual(draft.click.call_count, 2)

    def test_correct_category_needs_no_click_or_scroll(self):
        draft, _, current, _, _ = self.harness()
        current.Name = "舞蹈"
        set_category(draft, "舞蹈")
        draft.click.assert_not_called()
        draft.scroll_in_control.assert_not_called()

    def test_expanded_menu_does_not_toggle_opener(self):
        draft, state, _, _, options = self.harness(wrapper=True)
        state["open"] = True
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        draft.click.assert_called_once_with(options[1])

    def test_actual_current_value_is_used_as_opener(self):
        draft, _, current, _, _ = self.harness()
        self.assertIs(category_opener(draft), current)

    def test_actual_combobox_is_preferred_when_exposed(self):
        draft, _, current, _, _ = self.harness()
        combo = node(kind="ComboBoxControl", width=260)
        label = node("分区", x=30)
        self.assertIs(category_opener(draft, [label, current, combo]), combo)

    def observed_closed_controls(self):
        label = node("分区", x=1249, y=710, width=33, height=25)
        container = node(x=1387, y=702, width=254, height=41,
                         kind="GroupControl", cls="select-container")
        selected = node(x=1401, y=710, width=34, height=25, kind="GroupControl",
                        cls="select-item-cont select-item-cont-inserted", parent=container)
        value = node("vlog", x=1401, y=711, width=34, height=22, parent=selected)
        arrow = node(x=1613, y=715, width=14, height=15, kind="ImageControl",
                     cls="selebox-box-v2-drop-icon icon-ic_collapse icon-sprite icon-sprite-pulldown", parent=container)
        return [label, container, selected, value, arrow]

    def test_observed_closed_select_item_is_not_expanded_menu(self):
        draft, _, _, _, _ = self.harness()
        draft.input_column = lambda: 1387
        controls = self.observed_closed_controls()
        self.assertIsNone(read_menu(draft, controls))
        self.assertIs(category_opener(draft, controls), controls[-1])

    def test_observed_container_without_arrow_is_real_opener(self):
        draft, _, _, _, _ = self.harness()
        draft.input_column = lambda: 1387
        controls = self.observed_closed_controls()[:-1]
        self.assertIs(category_opener(draft, controls), controls[1])

    def test_complete_flow_opens_observed_arrow_instead_of_scrolling_closed_field(self):
        draft, state, current, popup, options = self.harness(wrapper=True)
        baseline = self.observed_closed_controls()
        label, container, selected, value, arrow = baseline
        for index, option in enumerate(options):
            option.BoundingRectangle.left = 1401
            option.BoundingRectangle.right = 1451
            option.BoundingRectangle.top = 760 + index*45
            option.BoundingRectangle.bottom = 780 + index*45
        popup.BoundingRectangle = NS(left=1387, top=749, right=1641, bottom=1047)
        draft.input_column = lambda: 1387
        draft.find.return_value = label
        def controls():
            if state["chosen"]:
                value.Name = "舞蹈"
            return baseline + ([popup] + options if state["open"] else [])
        def click(control):
            if control is arrow:
                state["open"] = True
            elif control is options[1]:
                state["chosen"], state["open"] = True, False
            else:
                raise AssertionError("必须点击真实箭头或菜单选项")
        draft.controls, draft.click = controls, Mock(side_effect=click)
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        self.assertEqual([c.args[0] for c in draft.click.call_args_list], [arrow, options[1]])
        draft.scroll_in_control.assert_not_called()
        draft.scroll_page.assert_not_called()

    def test_low_row_moves_once_and_opener_is_relocated(self):
        draft, state, current, _, _ = self.harness()
        original_controls = draft.controls
        old = node("生活", y=750)
        low_label = node("分区", x=30, y=750)
        draft.window = NS(BoundingRectangle=NS(bottom=1000))
        def controls():
            return original_controls() if state.get("moved") else [low_label, old]
        draft.controls = controls
        draft.scroll_page = Mock(side_effect=lambda *args: state.update(moved=True))
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        draft.scroll_page.assert_called_once_with("down", 1)
        self.assertIs(draft.click.call_args_list[0].args[0], current)

    def test_real_classless_container_provides_clipping_bounds(self):
        draft, _, current, popup, options = self.harness()
        popup.ClassName = ""
        controls = [node("分区", x=30), current, popup] + options
        self.assertIs(read_menu(draft, controls).container, popup)
        outside = node("舞蹈", y=500, parent=popup)
        self.assertIs(category_option(draft, "舞蹈", controls + [outside]), options[1])

    def test_stale_parent_is_discarded_until_structural_read_recovers(self):
        draft, _, current, popup, options = self.harness(wrapper=True)
        options[1].GetParentControl = Mock(side_effect=COMError(-2147220991, "旧节点", None))
        controls = [node("分区", x=30), current, popup] + options
        self.assertIsNone(category_option(draft, "舞蹈", controls))
        options[1].GetParentControl=lambda:popup
        self.assertIs(category_option(draft,"舞蹈",controls),options[1])

    def test_missing_flat_wrapper_uses_real_ancestor_clip(self):
        draft, _, current, popup, options = self.harness()
        popup.ClassName = "drop-list-v2-content-wrp"
        outside = node("舞蹈", y=500, parent=popup)
        controls = [node("分区", x=30), current] + options + [outside]
        menu = read_menu(draft, controls)
        self.assertIs(menu.container.control, popup)
        self.assertIs(category_option(draft, "舞蹈", controls), options[1])

    def test_stale_parent_without_menu_evidence_is_not_selected(self):
        draft, _, _, _, _ = self.harness()
        orphan = node("舞蹈", y=450)
        orphan.GetParentControl = Mock(side_effect=COMError(-2147220991, "旧节点", None))
        self.assertIsNone(category_option(draft, "舞蹈", [orphan]))

    def test_recommended_horizontal_labels_are_not_menu_evidence(self):
        draft, _, current, _, _ = self.harness()
        controls = [node("分区", x=30), current]
        controls += [node(name, x=100+i*60, y=180) for i, name in enumerate(("音乐", "舞蹈", "动画"))]
        self.assertIsNone(read_menu(draft, controls))

    def test_never_calls_scroll_when_menu_does_not_appear(self):
        draft, _, _, _, _ = self.harness(delayed=1000)
        with patch("bilibili_category.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "分区菜单未就绪"):
                set_category(draft, "舞蹈")
        self.assertEqual(draft.click.call_count, 1)
        draft.scroll_in_control.assert_not_called()
        draft.scroll_page.assert_not_called()

    def test_selected_readback_failure_does_not_repeat_selection(self):
        draft, _, _, _, _ = self.harness(readback_delay=1000)
        with patch("bilibili_category.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "未确认舞蹈分区"):
                set_category(draft, "舞蹈")
        self.assertEqual(draft.click.call_count, 2)

    def test_scrolling_stays_in_real_menu_container_and_reloads_options(self):
        draft, state, current, popup, options = self.harness(wrapper=True)
        target = options[1]
        hidden = [node(name, y=150+i*45, parent=popup) for i, name in enumerate(("知识", "科技", "运动"))]
        original_controls = draft.controls
        def controls():
            result = original_controls()
            if state["open"] and not state.get("scrolled"):
                result = [c for c in result if c not in options] + hidden
            return result
        draft.controls = controls
        def scroll(anchor, direction, amount):
            self.assertIs(anchor, popup)
            self.assertEqual((direction, amount), ("up", 1))
            state["scrolled"] = True
        draft.scroll_in_control = Mock(side_effect=scroll)
        with patch("bilibili_category.time.sleep"):
            set_category(draft, "舞蹈")
        draft.scroll_in_control.assert_called_once()
        self.assertIs(draft.click.call_args.args[0], target)
        draft.scroll_page.assert_not_called()

    def test_disappearing_menu_stops_without_scrolling_page(self):
        draft, state, _, popup, options = self.harness(wrapper=True)
        for option, name in zip(options, ("知识", "科技", "运动")):
            option.Name = name
        draft.scroll_in_control = Mock(side_effect=lambda *args: state.update(open=False))
        with patch("bilibili_category.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "分区菜单滚动后消失"):
                set_category(draft, "舞蹈")
        draft.scroll_in_control.assert_called_once_with(popup, "up", 1)
        self.assertEqual(draft.click.call_count, 1)
        draft.scroll_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
