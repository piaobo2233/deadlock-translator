"""Tk workflows on synthetic feedback; never use the real private archive."""

from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

from tools.feedback_library import FeedbackLibrary
from tools.feedback_manager import FeedbackWindow


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def button(window, text):
    return next(widget for widget in descendants(window)
                if widget.winfo_class() == "TButton" and widget.cget("text") == text)


class FeedbackWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "docs/plans").mkdir(parents=True)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.destroy_window)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.app = FeedbackWindow(self.root, FeedbackLibrary(self.project))
        self.root.update()
        self.dialogs = patch("tools.feedback_manager.messagebox.showerror")
        self.error_dialog = self.dialogs.start()
        self.addCleanup(self.dialogs.stop)

    def destroy_window(self):
        self.root.update()
        self.root.destroy()

    def fill(self, title="合成反馈"):
        self.app.title.set(title)
        self.app.source.set("测试来源")
        self.app.original.insert("1.0", "测试原话：识别区域需要调整")
        self.app.analysis.insert("1.0", "测试分析：展示可调选框")

    def test_save_reopen_image_preview_search_and_dirty_close(self):
        self.fill()
        image = self.project / "synthetic.png"
        Image.new("RGB", (100, 50), "green").save(image)
        self.app.pending_images.append(str(image))
        button(self.root, "保存反馈 / 分析  Ctrl+S").invoke()
        self.root.update()
        self.assertIsNotNone(self.app.current)
        self.assertEqual(str(self.app.original.cget("state")), "disabled")
        image.unlink()
        self.assertIsNotNone(self.app.image_grid.tile_image(0))
        self.app.analysis.insert("end", "\n后续补充")
        with patch("tools.feedback_manager.messagebox.askyesnocancel", return_value=None):
            self.app.close()
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue(self.app.save())
        self.app.query.set("后续补充")
        self.app.search()
        self.assertEqual(len(self.app.rows), 1)
        self.app.reload()
        self.assertIn("后续补充", self.app.analysis.get("1.0", "end"))
        self.assertEqual(self.errors, [])
        self.error_dialog.assert_not_called()

    def test_screenshot_grid_previews_images_without_selecting_them(self):
        self.root.geometry("900x620")
        self.root.deiconify()
        self.root.update()
        sizes = ((120, 60), (40, 160), (90, 90))
        for index, size in enumerate(sizes):
            path = self.project / f"grid-{index}.png"
            Image.new("RGB", size, "steelblue").save(path)
            self.app.pending_images.append(str(path))
        self.app.update_images()
        self.root.update()
        grid = self.app.image_grid
        self.assertEqual(len(grid.tiles), 3)
        self.assertIsNone(grid.selected, "预览不应要求先选中某张截图")
        self.assertGreaterEqual(grid.columns, 1)
        for index, (width, height) in enumerate(sizes):
            photo = grid.tile_image(index)
            self.assertIsNotNone(photo)
            self.assertLessEqual(photo.width(), grid.TILE_W)
            self.assertLessEqual(photo.height(), grid.TILE_H)
            self.assertAlmostEqual(photo.width() / photo.height(), width / height, delta=0.05)
            info = grid.tiles[index].grid_info()
            self.assertEqual((int(info["row"]), int(info["column"])),
                             (index // grid.columns, index % grid.columns))
        self.assertEqual(self.errors, [])
        self.error_dialog.assert_not_called()

    def test_unsaved_new_record_can_cancel_navigation_and_failed_save_stays_editable(self):
        self.fill()
        with patch("tools.feedback_manager.messagebox.askyesnocancel", return_value=None):
            self.app.new()
        self.assertEqual(self.app.title.get(), "合成反馈")
        with patch.object(self.app.library, "save", side_effect=OSError("模拟写入失败")):
            self.assertFalse(self.app.save())
        self.assertIn("识别区域", self.app.original.get("1.0", "end"))
        self.error_dialog.assert_called_once()

    def test_requirement_preview_requires_check_then_saves_only_draft_and_links(self):
        self.fill()
        self.assertTrue(self.app.save())
        self.root.deiconify()
        self.root.update()
        window = self.app.requirement_dialog()
        editor = next(widget for widget in descendants(window) if widget.winfo_class() == "Text")
        editor.delete("1.0", "end")
        editor.insert("1.0", "# 可公开需求\n\n- 支持框选")
        publish = button(window, "确认并保存为新需求")
        self.assertEqual(str(publish.cget("state")), "disabled")
        button(window, "预览需求").invoke()
        self.assertEqual(str(editor.cget("state")), "disabled")
        with patch("tools.feedback_manager.messagebox.showinfo") as information:
            publish.invoke()
        information.assert_called_once()
        checkbox = next(widget for widget in descendants(window) if widget.winfo_class() == "TCheckbutton")
        checkbox.invoke()
        button(window, "返回修改").invoke()
        self.assertEqual(str(publish.cget("state")), "disabled")
        self.assertFalse(window.getvar(checkbox.cget("variable")))
        button(window, "预览需求").invoke()
        checkbox.invoke()
        target = self.project / "docs/plans/published.md"
        with patch("tools.feedback_manager.filedialog.asksaveasfilename", return_value=str(target)):
            publish.invoke()
        self.root.update()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "# 可公开需求\n\n- 支持框选")
        self.assertIn("docs/plans/published.md", self.app.documents)
        self.assertEqual(self.errors, [])
        self.error_dialog.assert_not_called()

    def test_attach_documents_and_reverse_filter(self):
        self.fill()
        document = self.project / "docs/plans/shared.md"
        document.write_text("# 共享需求", encoding="utf-8")
        with patch("tools.feedback_manager.filedialog.askopenfilenames", return_value=(str(document),)):
            self.app.attach_document()
        self.assertTrue(self.app.save())
        self.app.document_list.selection_set(0)
        self.app.preview_document()
        self.assertIn("共享需求", self.app.document_preview.get("1.0", "end"))
        self.app.document_filter.set("docs/plans/shared.md")
        self.app.search()
        self.assertEqual(len(self.app.rows), 1)
        self.assertEqual(self.errors, [])

    def test_save_buttons_remain_visible_at_minimum_window_size(self):
        self.root.geometry("900x620")
        self.root.deiconify()
        self.root.update()
        for label in ("保存反馈 / 分析  Ctrl+S", "整理为新需求…"):
            control = button(self.root, label)
            self.assertTrue(control.winfo_viewable())
            self.assertGreater(control.winfo_height(), 15)
            self.assertLessEqual(control.winfo_rooty() + control.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
