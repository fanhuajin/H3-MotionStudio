"""作品文件夹批量投稿窗口。"""
import json
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


class WorkRows(ttk.Frame):
    """每个作品独立持有日期和小时控件，不依赖列表焦点或选中行。"""
    def __init__(self, parent, change, timezone):
        super().__init__(parent)
        self.change = change
        self.timezone = timezone
        self.rows = {}
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text="选择", width=6).pack(side="left")
        ttk.Label(header, text="作品文件夹").pack(side="left", fill="x", expand=True)
        ttk.Label(header, text="日期", width=16).pack(side="left")
        ttk.Label(header, text="小时", width=7).pack(side="left")
        ttk.Label(header, text="状态", width=12).pack(side="left")
        self.canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.body = ttk.Frame(self.canvas)
        self.content = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.content, width=event.width))

    def insert(self, parent, position, iid, text, values):
        frame = ttk.Frame(self.body, padding=(0, 4))
        frame.pack(fill="x")
        chosen = tk.BooleanVar(value=False)
        date = tk.StringVar(value=values[0])
        hour = tk.StringVar(value=values[1] if values[1] != "—" else "00")
        state = tk.StringVar(value=values[2])
        ttk.Checkbutton(frame, variable=chosen, width=4).pack(side="left")
        label = ttk.Label(frame, text=text, anchor="w", width=1)
        label.pack(side="left", fill="x", expand=True)
        now = datetime.now(self.timezone)
        dates = ["不定时"] + [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(16)]
        date_box = ttk.Combobox(frame, textvariable=date, values=dates, state="readonly", width=14)
        date_box.pack(side="left", padx=4)
        hour_box = ttk.Combobox(frame, textvariable=hour, values=[f"{i:02d}" for i in range(24)], state="readonly", width=5)
        hour_box.pack(side="left", padx=4)
        ttk.Label(frame, textvariable=state, width=12).pack(side="left", padx=4)
        date_box.bind("<<ComboboxSelected>>", lambda event, key=iid: self.change(key, "date", date.get()))
        hour_box.bind("<<ComboboxSelected>>", lambda event, key=iid: self.change(key, "hour", hour.get()))
        self.rows[iid] = dict(frame=frame, selected=chosen, date=date, hour=hour, state=state,
                              date_box=date_box, hour_box=hour_box)

    def item(self, key, values):
        row = self.rows[key]
        row["date"].set(values[0])
        if values[1] != "—":
            row["hour"].set(values[1])
        row["state"].set(values[2])

    def selection(self):
        return tuple(key for key, row in self.rows.items() if row["selected"].get())

    def select_all(self, selected=True):
        for row in self.rows.values():
            row["selected"].set(selected)

    def delete(self, key):
        self.rows.pop(key)["frame"].destroy()

    def set_enabled(self, enabled):
        for row in self.rows.values():
            for name in ("date_box", "hour_box"):
                row[name].configure(state="readonly" if enabled else "disabled")


class PublishQueue:
    def __init__(self, root, draft_class, select_hour, prepare, cover, category,
                 timezone, status_file, folder=None, publish_at=None, platform="bilibili"):
        self.root = root
        self.draft_class = draft_class
        self.select_hour = select_hour
        self.prepare, self.cover, self.category = prepare, cover, category
        self.timezone, self.status_file = timezone, status_file
        self.items = {}
        self.completed = []
        self.queue_file = Path(__file__).with_name("publish_queue_state.json" if platform == "bilibili" else f"{platform}_queue_state.json")
        self.loading = False
        self.running = False
        self.paused = False
        self.closed = False
        self.directory = str(folder.parent) if folder else "E:/AI_Exports/H3-MotionStudio/往期成品/20261008/dance"
        now = datetime.now(timezone)
        initial = datetime.strptime(publish_at, "%Y-%m-%d %H:%M") if publish_at else now + timedelta(days=1)
        self.date = tk.StringVar(value=initial.strftime("%Y-%m-%d"))
        self.hour = tk.StringVar(value=initial.strftime("%H"))
        self.scheduled = tk.BooleanVar(value=publish_at != "")
        self.notice = tk.StringVar(value="添加作品文件夹，每个作品后面的日期和小时可直接选择。")
        root.title({"bilibili": "B站批量投稿", "douyin": "抖音批量发布", "xiaohongshu": "小红书批量发布"}[platform])
        root.geometry("1000x600")
        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="批量选择作品文件夹", command=self.choose_many).pack(side="left")
        ttk.Button(toolbar, text="添加一个作品文件夹", command=self.choose_one).pack(side="left", padx=8)
        ttk.Button(toolbar, text="移除选中作品", command=self.remove).pack(side="left")
        self.tree = WorkRows(panel, self.set_item_time, timezone)
        self.tree.pack(fill="both", expand=True, pady=14)
        ttk.Label(panel, textvariable=self.notice, wraplength=940).pack(anchor="w", pady=12)
        ttk.Label(panel, text="运行时按 Esc 暂停；成功作品自动移除。完成后自动进入下一条作品上传页。").pack(anchor="w")
        self.start_button = ttk.Button(panel, text="开始批量投稿", command=self.start)
        self.start_button.pack(anchor="e", pady=10)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.request_pause())
        self.restore_queue()
        if folder:
            self.add(folder)

    def save_queue(self):
        if not getattr(self, "queue_file", None) or getattr(self, "loading", False):
            return
        state = {"directory": self.directory, "completed": self.completed,
                 "items": [dict(item, folder=str(item["folder"])) for item in self.items.values()]}
        temporary = self.queue_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.queue_file)

    def restore_queue(self):
        if not self.queue_file.exists():
            return
        self.loading = True
        try:
            state = json.loads(self.queue_file.read_text(encoding="utf-8"))
            self.directory = state.get("directory", self.directory)
            self.completed = state.get("completed", [])
            for saved in state.get("items", []):
                self.date.set(saved["date"])
                self.hour.set(saved["hour"])
                self.scheduled.set(saved["scheduled"])
                self.add(Path(saved["folder"]))
            self.notice.set(f"已恢复 {len(self.items)} 个待发布作品和各自时间，点击开始继续。")
        except Exception as error:
            self.notice.set(f"恢复队列时发生错误：{error}")
        finally:
            self.loading = False

    def show(self):
        from keyboard_session import start,stop
        start()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.mainloop()
        finally:
            stop()

    def close(self):
        self.save_queue()
        self.closed = True
        from keyboard_session import stop
        stop()
        if not self.running:
            self.root.quit()

    def request_pause(self):
        if self.running:
            self.paused = True

    def add(self, folder):
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise ValueError("请选择作品文件夹。")
        self.prepare(folder)
        self.cover(folder)
        self.category(folder)
        key = str(folder)
        if key in self.items:
            return
        self.items[key] = {"folder": folder, "date": self.date.get(), "hour": self.hour.get(), "scheduled": self.scheduled.get()}
        self.tree.insert("", "end", iid=key, text=folder.name, values=self.values(self.items[key]))
        self.save_queue()

    def values(self, item, state="待发布"):
        return (item["date"] if item["scheduled"] else "不定时", item["hour"] if item["scheduled"] else "—", state)

    def choose_one(self):
        if self.running:
            return
        chosen = filedialog.askdirectory(parent=self.root, initialdir=self.directory, title="选择作品文件夹")
        if chosen:
            try:
                self.add(chosen)
                self.directory = str(Path(chosen).parent)
            except Exception as error:
                messagebox.showerror("素材错误", str(error), parent=self.root)

    def choose_many(self):
        if self.running:
            return
        from native_folder_picker import choose_folders
        try:
            chosen = choose_folders(self.root.winfo_id(), self.directory)
        except Exception as error:
            messagebox.showerror("无法选择文件夹", str(error), parent=self.root)
            return
        errors = []
        for folder in chosen:
            try:
                self.add(folder)
            except Exception as error:
                errors.append(f"{Path(folder).name}：{error}")
        if chosen:
            self.directory = str(Path(chosen[0]).parent)
        if errors:
            messagebox.showerror("部分素材未添加", "\n".join(errors), parent=self.root)

    def set_item_time(self, key, field, value):
        if self.running:
            return
        item = self.items[key]
        if field == "date" and value == "不定时":
            item["scheduled"] = False
        else:
            item[field] = value
            item["scheduled"] = True
        self.tree.item(key, values=self.values(item))
        self.save_queue()

    def load_selection(self, event=None):
        selected = self.tree.selection()
        if len(selected) == 1:
            item = self.items[selected[0]]
            self.date.set(item["date"])
            self.hour.set(item["hour"])
            self.scheduled.set(item["scheduled"])

    def apply_time(self):
        if self.running:
            return
        for key in self.tree.selection():
            item = self.items[key]
            item.update(date=self.date.get(), hour=self.hour.get(), scheduled=self.scheduled.get())
            self.tree.item(key, values=self.values(item))

    def remove(self):
        if not self.running:
            for key in self.tree.selection():
                self.items.pop(key)
                self.tree.delete(key)
                self.save_queue()

    def tick(self):
        self.root.update()
        if self.closed:
            raise RuntimeError("用户关闭队列，流程已停止。")

    def pause(self):
        self.paused = True
        self.root.deiconify()
        self.root.lift()
        self.notice.set("已暂停。点击继续后恢复当前操作，已完成作品不会再次投稿。")
        self.start_button.configure(text="继续", state="normal", command=self.resume)
        while self.paused and not self.closed:
            self.root.update()
            time.sleep(0.05)
        if self.closed:
            raise RuntimeError("用户关闭队列，流程已停止。")
        self.root.withdraw()

    def resume(self):
        self.paused = False
        self.start_button.configure(text="运行中", state="disabled")

    def start(self):
        if self.running or not self.items:
            return
        try:
            for item in self.items.values():
                self.prepare(item["folder"])
                self.cover(item["folder"])
                self.category(item["folder"])
                if item["scheduled"]:
                    self.select_hour(item["date"], item["hour"])
        except Exception as error:
            messagebox.showerror("请检查队列", str(error), parent=self.root)
            return
        self.running = True
        self.tree.set_enabled(False)
        self.start_button.configure(state="disabled", text="运行中")
        self.root.withdraw()
        try:
            for key in list(self.items):
                item = self.items[key]
                folder = item["folder"]
                draft = self.draft_class()
                draft.on_tick = self.tick
                draft.on_pause = self.pause
                selected = self.select_hour(item["date"], item["hour"]) if item["scheduled"] else None
                self.tree.item(key, values=self.values(item, "发布中"))
                self.notice.set(f"正在发布：{folder.name}")
                draft.run(*self.prepare(folder), self.cover(folder), self.category(folder), selected)
                # 只有明确收到成功页面才剔除，后续跳转失败也不会重复提交。
                self.completed.append({"folder": str(folder), "publish_at": str(getattr(draft, "actual_publish_at", selected) or "不定时")})
                self.items.pop(key)
                self.tree.delete(key)
                self.save_queue()
                self.status_file.write_text(json.dumps({"ok": True, "completed": self.completed, "pending": list(self.items)}, ensure_ascii=False), encoding="utf-8")
                draft.next_upload()
            self.notice.set(f"全部完成，已成功投稿 {len(self.completed)} 个作品。待发布列表已清空。")
        except Exception as error:
            traceback.print_exc()
            self.notice.set(f"流程停止：{error}。已完成 {len(self.completed)} 个，剩余 {len(self.items)} 个。")
            for key, item in self.items.items():
                self.tree.item(key, values=self.values(item))
            self.status_file.write_text(json.dumps({"ok": False, "message": str(error), "traceback": traceback.format_exc(), "completed": self.completed, "pending": list(self.items)}, ensure_ascii=False), encoding="utf-8")
        finally:
            self.running = False
            self.tree.set_enabled(True)
            self.start_button.configure(text="继续发布剩余作品", state="normal", command=self.start)
            if self.closed:
                self.root.quit()
            else:
                self.root.deiconify()
                self.root.lift()
