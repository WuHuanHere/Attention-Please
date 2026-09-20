"""托盘部件的单测(不需要真的弹托盘)。

托盘是"看得见才敢信"的部件, 所以这里至少把两件事钉住:
  1. 四种状态必须给出**不同的图标颜色**(颜色是托盘上唯一一眼能读的信息);
  2. 菜单能构建出来而且包含暂停/恢复/日报/退出 —— 少一项你在托盘里就找不到入口。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.tray import (  # noqa: E402
    STATE_COLORS,
    Tray,
    find_markdown_viewer,
    make_icon,
    viewer_candidates,
)
from attention_please.ui import strip_markdown  # noqa: E402

RAW = {
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "10:30"}]},
    "report": {"daily_report": False, "report_dir": "data/reports"},
}


class TestIcon(unittest.TestCase):
    def test_all_states_render_distinct_colors(self):
        seen = {}
        for state in STATE_COLORS:
            img = make_icon(state)
            self.assertEqual(img.size, (64, 64))
            self.assertEqual(img.mode, "RGBA")
            # 取圆心下方一点: 避开顶部描边和中间的"眼睛"
            seen[state] = img.getpixel((32, 52))[:3]
        self.assertEqual(len(set(seen.values())), len(STATE_COLORS),
                         f"状态色有重复: {seen}")

    def test_unknown_state_falls_back(self):
        img = make_icon("不存在的状态")
        self.assertEqual(img.size, (64, 64))


class TestMarkdownViewer(unittest.TestCase):
    """守"打开日报毫无反应"这个 bug: .md 没有文件关联时必须显式找程序。"""

    def test_code_cmd_derives_code_exe(self):
        cands = viewer_candidates(
            r"D:\Users\X\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
            {"LOCALAPPDATA": r"C:\Users\X\AppData\Local"})
        self.assertEqual(cands[0][0], "VS Code")
        self.assertTrue(cands[0][1].endswith("Code.exe"))
        self.assertIn("Microsoft VS Code", cands[0][1])
        self.assertNotIn("code.cmd", cands[0][1])

    def test_env_locations_are_included(self):
        cands = viewer_candidates(None, {"LOCALAPPDATA": r"C:\la",
                                         "ProgramFiles": r"C:\pf"})
        paths = [p for _, p in cands]
        self.assertTrue(any("C:\\la" in p for p in paths))
        self.assertTrue(any("C:\\pf" in p for p in paths))

    def test_no_env_no_path_is_empty(self):
        self.assertEqual(viewer_candidates(None, {}), [])

    def test_find_viewer_never_raises(self):
        found = find_markdown_viewer()
        self.assertTrue(found is None or (len(found) == 2 and isinstance(found[1], str)))

    def test_found_viewer_exists(self):
        found = find_markdown_viewer()
        if found is not None:
            self.assertTrue(pathlib.Path(found[1]).exists(),
                            f"挑出来的查看器不存在: {found}")


class TestStripMarkdown(unittest.TestCase):
    """自带日报查看器里显示的是"去掉记号"的纯文本 —— 别把表格和缩进弄坏。"""

    def test_removes_emphasis_and_code_ticks(self):
        self.assertEqual(strip_markdown("有效专注 **2 小时** `bar`"),
                         "有效专注 2 小时 bar")

    def test_removes_heading_marks(self):
        self.assertEqual(strip_markdown("## 最常分心的时段"), "最常分心的时段")

    def test_keeps_tables_and_indentation(self):
        src = "| 指标 | 数值 |\n|---|---|\n| 分心 | 3 分钟 |\n- 缩进项"
        out = strip_markdown(src)
        self.assertIn("| 指标 | 数值 |", out)
        self.assertIn("|---|---|", out)
        self.assertIn("- 缩进项", out)

    def test_empty(self):
        self.assertEqual(strip_markdown(""), "")


class TestTray(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda req: None,
                                           ask_reason=lambda *a: None,
                                           drain=lambda: [])
        self.tray = Tray(self.rt, cfg.report_dir)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_menu_has_all_entries(self):
        menu = self.tray._menu()
        labels = [str(getattr(item, "text", "")) for item in menu.items]
        text = " ".join(labels)
        for expected in ("暂停", "恢复", "日报", "截图", "退出"):
            self.assertIn(expected, text, f"托盘菜单缺少「{expected}」: {labels}")

    def test_state_key_reflects_pause(self):
        self.rt.pause("测试", datetime.now())
        self.assertEqual(self.rt.state_key(), "paused")

    def test_state_key_idle_outside_schedule(self):
        # 时间表只有 08:30-10:30; 用一个必然在区间外的时刻
        self.rt._policy = lambda at: self.rt.cfg and type(
            "P", (), {"judging": False, "block_name": "", "camera_yield": False})()
        self.assertEqual(self.rt.state_key(), "idle")

    def test_status_text_survives_empty_db(self):
        text = self.rt.status_text()
        self.assertIn("t", text.lower())        # attention_please
        self.assertIn("人脸覆盖率", text)

    def test_clear_captures_action(self):
        cap_dir = self.rt.cfg.capture_dir
        cap_dir.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (10, 10)).save(cap_dir / "a.jpg", "JPEG")
        self.tray._clear_captures()
        self.assertEqual(list(cap_dir.glob("*.jpg")), [])

    def test_quit_sets_runtime_stopped(self):
        self.tray.icon = None
        self.tray._quit()
        self.assertFalse(self.rt.running)


class TestTrayDoesNotLieAboutBookkeeping(unittest.TestCase):
    """记账失败时托盘**不许**报成功。

    实测 2026-09-20 13:37:54: 让出摄像头时写库撞上 `database is locked`, 异常被 pystray
    吞掉, 而通知照样说"会记进日报" —— 用户会以为记上了, 日报里却没有这笔。
    状态(内存里)其实已经改了, 所以文案必须把"让出成功了"和"记账失败了"分开说。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.ui = types.SimpleNamespace(show=lambda req: None,
                                           ask_reason=lambda *a: None,
                                           drain=lambda: [])
        self.tray = Tray(self.rt, cfg.report_dir)
        self.notes: list[str] = []
        self.tray._notify = lambda message, title="attention_please": self.notes.append(message)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_yield_bookkeeping_failure_is_visible(self):
        def boom(_mins):
            raise sqlite3.OperationalError("database is locked")

        self.rt.yield_camera = boom
        self.tray._yield()
        self.assertEqual(len(self.notes), 1)
        self.assertIn("记账失败", self.notes[0])
        self.assertNotIn("会记进日报", self.notes[0],
                         "记账失败了还说'会记进日报' —— 这是在骗人")

    def test_reclaim_bookkeeping_failure_is_visible(self):
        def boom():
            raise sqlite3.OperationalError("database is locked")

        self.rt.reclaim_camera = boom
        self.tray._reclaim()
        self.assertIn("记账失败", self.notes[0])

    def test_normal_yield_still_says_success(self):
        self.tray._yield()
        self.assertIn("会记进日报", self.notes[0])
        self.assertNotIn("记账失败", self.notes[0])


if __name__ == "__main__":
    unittest.main()
