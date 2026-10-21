"""三端发布队列：保留状态，继续未完成平台，由用户删除记录。"""
import json
import hashlib
import re
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from publish_queue import PublishQueue, WorkRows

PLATFORMS = ("douyin", "xiaohongshu", "bilibili")
LABELS = {"bilibili": "B站", "douyin": "抖音", "xiaohongshu": "小红书"}
STATE_FILE = Path(__file__).with_name("multi_publish_state.json")


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def source_fingerprint(platform, prepared, covers, category):
    """确认本次素材与上次相同，不能只凭标题或历史阶段跳过正文和封面。"""
    def file_signature(value):
        path = Path(value).resolve()
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"读取时素材发生变化，请完成保存后再继续：{path.name}")
        return {"path": str(path), "sha256": digest.hexdigest()}

    video, title, description, tags = prepared
    copy_path = Path(video).parent / "发布文案.txt"
    cover_paths = covers if isinstance(covers, (list, tuple)) else (covers,)
    payload = {
        "version": 1, "platform": platform,
        "video": file_signature(video),
        "title": title, "description": description, "tags": list(tags),
        "copy": file_signature(copy_path) if copy_path.is_file() else None,
        "covers": [file_signature(path) for path in cover_paths],
        "category": category,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def settings_resume_allowed(previous, fingerprint, platform):
    """仅最新一次未提交、素材一致且已完成编辑的尝试可以从设置续跑。"""
    if previous.get("submitted") or previous.get("source_fingerprint") != fingerprint:
        return False
    stage = previous.get("stage") or ""
    if stage in ("schedule", "upload_ready", "submit_ready") or stage.startswith("schedule_"):
        return True
    return platform == "douyin" and (stage == "settings" or previous.get("error") in (
        "未确认保存权限为不允许。",
        "没有找到“不允许”，页面或浏览器可访问性可能发生变化。",
    ))


def recover_item(item):
    item.setdefault("states", {p: "待发布" for p in PLATFORMS})
    item.setdefault("details", {})
    for p in PLATFORMS:
        item["states"].setdefault(p,"待发布")
        if item["states"].get(p) == "发布中":
            item["states"][p] = "待核对" if item["details"].get(p, {}).get("submitted") else "失败"
    return item


def pending_platforms(item):
    return [p for p in PLATFORMS if item["states"][p] in ("待发布", "失败")]


def mark_manual_completed(item, platform, completed_at):
    previous = dict(item.setdefault("details", {}).get(platform, {}))
    item["states"][platform] = "已完成"
    item["details"][platform] = {
        "submitted": True, "manual": True, "completed_at": completed_at,
        "requested_mode": "定时" if item.get("scheduled") else "立即发布",
        "requested_time": f"{item['date']} {item['hour']}:00" if item.get("scheduled") else None,
        # 用户确认完成不等于确认页面的实际日期、小时及分钟。
        "publish_at": previous.get("publish_at"),
        "publish_time_verified": bool(previous.get("publish_at")),
        "previous_attempt": previous,
    }


class ThreeRows(WorkRows):
    def __init__(self, parent, change, timezone, state_change):
        super().__init__(parent, change, timezone)
        self.state_change=state_change
        self.max_content_height = min(700, max(140, self.winfo_screenheight() - 300))
        self.layout_job = None
        self.wheel_remainder = 0
        self.scrollbar = next(w for w in self.winfo_children() if isinstance(w, ttk.Scrollbar))
        header = self.winfo_children()[0]
        header.winfo_children()[-1].destroy()
        header.configure(padding=(0,4))
        for widget in header.winfo_children():
            widget.configure(style="Heading.TLabel")
        for p in PLATFORMS:
            ttk.Label(header, text=LABELS[p], width=12,style="Heading.TLabel").pack(side="left")
        ttk.Label(header, text="操作", width=10,style="Heading.TLabel").pack(side="left")
        self.column_header=header
        header.pack_configure(fill="x")
        self.canvas.bind("<Configure>",self.align_header,add="+")
        self.canvas.configure(background="#f7f7f7")
        self.canvas.bind("<MouseWheel>",self.wheel)
        self.body.bind("<MouseWheel>",self.wheel)
        self.body.bind("<Configure>", self.schedule_layout)
        self.canvas.bind("<Configure>", self.schedule_layout, add="+")
        self.canvas.configure(height=70)
        self.schedule_layout()

    def schedule_layout(self, event=None):
        if self.layout_job is None:
            self.layout_job = self.after_idle(self.fit_content)

    def fit_content(self):
        self.layout_job = None
        content_height = self.body.winfo_reqheight() if self.rows else 70
        viewport_height = min(self.max_content_height, max(70, content_height))
        if int(self.canvas.cget("height")) != viewport_height:
            self.canvas.configure(height=viewport_height)
        overflow = content_height > viewport_height
        if overflow and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        elif not overflow and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(),
                                            max(content_height, self.canvas.winfo_height())))
        if not overflow:
            self.canvas.yview_moveto(0)
            self.wheel_remainder = 0
        if self.rows:
            self.canvas.configure(yscrollincrement=next(iter(self.rows.values()))["frame"].winfo_reqheight())
        root = self.winfo_toplevel()
        requested_height = root.winfo_reqheight()
        if root.winfo_height() != requested_height:
            root.geometry(f"{max(1100, root.winfo_width())}x{requested_height}")

    def align_header(self,event=None):
        if not self.rows:
            return
        self.update_idletasks()
        row=next(iter(self.rows.values()))
        widgets=row["frame"].winfo_children()
        columns=[widgets[0],widgets[1],row["date_box"],row["hour_box"]]+[row["platform_boxes"][p] for p in PLATFORMS]+[row["preview_button"]]
        for label,control in zip(self.column_header.winfo_children(),columns):
            label.pack_forget()
            label.place(x=control.winfo_x(),y=0,width=control.winfo_width(),height=24)
            label.configure(anchor="w")
        self.column_header.configure(height=24)

    def wheel(self,event):
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return "break"
        self.wheel_remainder += event.delta
        steps = int(self.wheel_remainder / 120)
        self.wheel_remainder -= steps * 120
        if steps:
            self.canvas.yview_scroll(-steps,"units")
        return "break"

    def insert(self, parent, position, iid, text, values):
        display=re.sub(r"_\d{10,}$","",text).replace("_跳舞_"," · ").replace("_唱歌_"," · ")
        super().insert(parent, position, iid, display, values)
        row = self.rows[iid]
        row["frame"].configure(padding=(0,1))
        row["frame"].bind("<MouseWheel>",self.wheel)
        for widget in row["frame"].winfo_children():
            if not isinstance(widget,ttk.Combobox):
                widget.bind("<MouseWheel>",self.wheel)
        row["frame"].winfo_children()[-1].destroy()
        row["platforms"] = {}
        row["platform_boxes"] = {}
        for p in PLATFORMS:
            var = tk.StringVar(value="待发布")
            row["platforms"][p] = var
            box=ttk.Combobox(row["frame"],textvariable=var,values=("待发布","不发布"),state="readonly",width=10)
            box.pack(side="left",padx=4)
            box.bind("<<ComboboxSelected>>",lambda event,key=iid,platform=p,variable=var:self.state_change(key,platform,variable.get()))
            box.bind("<Double-Button-1>",lambda event,key=iid,platform=p:self.state_change(key,platform,"核对"))
            row["platform_boxes"][p]=box
        row["preview_button"] = ttk.Button(row["frame"], text="查看视频", width=9,
                                            command=lambda key=iid: self.preview(key))
        row["preview_button"].pack(side="left", padx=4)
        for widget in row["frame"].winfo_children():
            widget.bind("<MouseWheel>", self.wheel)
        self.after_idle(self.align_header)
        self.schedule_layout()

    def delete(self, key):
        super().delete(key)
        self.schedule_layout()

    def update_states(self, key, states):
        for p, var in self.rows[key]["platforms"].items():
            var.set(states[p])
            box=self.rows[key]["platform_boxes"][p]
            box.configure(style={"已完成":"Done.TCombobox","不发布":"Skip.TCombobox","失败":"Error.TCombobox","待核对":"Error.TCombobox"}.get(states[p],"TCombobox"))
            box.configure(state="disabled" if states[p] in ("已完成","待核对","发布中") else "readonly")

    def set_enabled(self, enabled):
        super().set_enabled(enabled)
        for row in self.rows.values():
            row["preview_button"].configure(state="normal" if enabled else "disabled")
            for p,box in row["platform_boxes"].items():
                box.configure(state="readonly" if enabled and row["platforms"][p].get() not in ("已完成","待核对","发布中") else "disabled")


class MultiQueue(PublishQueue):
    def __init__(self, root, adapters, timezone):
        # 延用已有文件夹选择、逐行时间修改和 Esc 暂停流程。
        self.root, self.adapters, self.timezone = root, adapters, timezone
        self.items, self.completed = {}, []
        self.queue_file = STATE_FILE
        self.loading = self.running = self.paused = self.closed = False
        self.directory = "E:/AI_Exports/H3-MotionStudio/往期成品/20261008/dance"
        tomorrow = datetime.now(timezone)+timedelta(days=1)
        self.date = tk.StringVar(value=tomorrow.strftime("%Y-%m-%d"))
        self.hour = tk.StringVar(value="01")
        self.scheduled = tk.BooleanVar(value=True)
        self.notice = tk.StringVar(value="选择作品文件夹，设置日期和小时。可单独运行一端，三端完成后保留记录。")
        root.title("三端批量发布")
        from publish_style import apply_theme
        apply_theme(root)
        icon=Path(__file__).with_name("assets")/"creator-publish.ico"
        if icon.exists():
            root.iconbitmap(str(icon))
        root.geometry("1100x300")
        root.minsize(920,240)
        panel = ttk.Frame(root, padding=12,style="Page.TFrame")
        panel.pack(fill="both", expand=True)
        self.summary=tk.StringVar(value="整理作品，安排时间，一次完成三端发布")
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x")
        ttk.Button(toolbar,text="批量添加作品",command=self.choose_many,style="Primary.TButton").pack(side="left")
        ttk.Button(toolbar,text="添加单个作品",command=self.choose_one).pack(side="left",padx=8)
        ttk.Button(toolbar,text="全选",command=lambda:self.tree.select_all()).pack(side="left",padx=4)
        ttk.Button(toolbar,text="取消全选",command=lambda:self.tree.select_all(False)).pack(side="left",padx=4)
        ttk.Button(toolbar,text="删除选中记录",command=self.remove,style="Danger.TButton").pack(side="right")
        card=ttk.Frame(panel)
        card.pack(fill="x",pady=(10,8))
        self.tree=ThreeRows(card,self.set_item_time,timezone,self.set_platform_state)
        self.tree.preview=self.preview_video
        self.tree.pack(fill="both",expand=True)
        ttk.Label(panel,textvariable=self.notice,wraplength=1180,style="Subtitle.TLabel").pack(anchor="w",pady=(0,6))
        ttk.Label(panel,text="点击“查看视频”预览并选择发布平台  ·  ↑ / ↓ 切换视频  ·  Esc 暂停",style="Subtitle.TLabel").pack(anchor="w")
        actions=ttk.Frame(panel)
        actions.pack(fill="x",pady=(10,0))
        self.platform_buttons={}
        for p in PLATFORMS:
            button=ttk.Button(actions,text=f"只运行{LABELS[p]}",command=lambda platform=p:self.start(platform))
            button.pack(side="left",padx=4)
            self.platform_buttons[p]=button
        self.start_button=ttk.Button(actions,text="开始 / 继续三端发布",command=self.start,style="Primary.TButton")
        self.start_button.pack(side="right")
        root.protocol("WM_DELETE_WINDOW",self.close)
        root.bind("<Escape>",lambda event:self.request_pause())
        self.restore_queue()

    def preview_video(self, key):
        if self.running or key not in self.items:
            return
        from video_preview import VideoPreview
        try:
            from bilibili_auto import prepare_files
            video, *_ = prepare_files(self.items[key]["folder"])
            keys = list(self.items)
            def resolve(index):
                selected = keys[index]
                video, *_ = prepare_files(self.items[selected]["folder"])
                return video, self.items[selected], lambda p, v: self.set_platform_state(selected, p, v)
            VideoPreview(self.root, video, self.items[key], LABELS,
                         lambda platform, value: self.set_platform_state(key, platform, value),
                         navigate=resolve, index=keys.index(key), total=len(keys))
        except Exception as error:
            messagebox.showerror("无法预览视频", str(error), parent=self.root)

    def add(self, folder):
        folder=Path(folder).resolve()
        key=str(folder)
        if key in self.items:
            return
        self.validate(folder)
        date, hour = self.date.get(), "01"
        if self.items:
            previous = next(reversed(self.items.values()))
            next_time = datetime.strptime(f"{previous['date']} {previous['hour']}", "%Y-%m-%d %H") + timedelta(hours=1)
            date, hour = next_time.strftime("%Y-%m-%d"), next_time.strftime("%H")
        item={"folder":folder,"date":date,"hour":hour,"scheduled":self.scheduled.get(),
              "states":{p:"待发布" for p in PLATFORMS},"details":{}}
        self.items[key]=item
        self.tree.insert("","end",iid=key,text=folder.name,values=self.values(item))
        self.tree.update_states(key,item["states"])
        self.save_queue()

    def validate(self, folder):
        # 添加时只检查共用视频/文案，各端限制留到该端开始前检查。
        # 例如小红书标题过长，不能阻止用户单独发布B站。
        from bilibili_auto import prepare_files
        prepare_files(folder)

    def set_platform_state(self,key,platform,value):
        item=self.items[key]
        if value=="核对" and not self.running and item["states"][platform]=="待核对":
            answer=messagebox.askyesnocancel("核对发布结果",f"请核对{LABELS[platform]}《{item['folder'].name}》。\n已发布成功选“是”；确认未发布选“否”；未确认选“取消”。",parent=self.root)
            if answer is not None:
                previous=dict(item["details"].get(platform,{}))
                item["states"][platform]="已完成" if answer else "待发布"
                item["details"][platform]={"manual_review":True,"submitted":bool(answer),"previous_attempt":previous}
                self.tree.update_states(key,item["states"])
                self.save_queue()
            return
        if (self.running or value not in ("待发布","不发布")
                or item["states"][platform] in ("已完成","待核对","发布中")):
            self.tree.update_states(key,item["states"])
            return
        item["states"][platform]=value
        self.tree.update_states(key,item["states"])
        self.save_queue()
        self.notice.set(f"《{item['folder'].name}》{LABELS[platform]}：{value}。")

    def save_queue(self):
        if hasattr(self,"summary"):
            total=len(self.items)
            done=sum(all(s in ("已完成","不发布") for s in i["states"].values()) for i in self.items.values())
            self.summary.set(f"{total} 个作品  ·  {done} 个已处理  ·  {total-done} 个待处理")
        if not self.loading and not getattr(self,"state_read_failed",False):
            save_state(self.queue_file,{"directory":self.directory,
                       "items":[dict(i,folder=str(i["folder"])) for i in self.items.values()]})

    def restore_queue(self):
        if not self.queue_file.exists():
            return
        self.loading=True
        restored=False
        self.state_read_failed=False
        try:
            state=json.loads(self.queue_file.read_text(encoding="utf-8"))
            self.directory=state.get("directory",self.directory)
            for saved in state.get("items",[]):
                item=recover_item(saved)
                item["folder"]=Path(item["folder"])
                key=str(item["folder"])
                self.items[key]=item
                self.tree.insert("","end",iid=key,text=item["folder"].name,values=self.values(item))
                self.tree.update_states(key,item["states"])
            self.notice.set(f"已恢复 {len(self.items)} 条记录；已完成平台会跳过。")
            restored=True
        except Exception as error:
            self.state_read_failed=True
            self.notice.set(f"读取列表失败：{error}")
        finally:
            self.loading=False
        if restored:
            self.save_queue()

    def manual_completed(self, platform):
        if self.running:
            return
        keys = self.tree.selection()
        for key in keys:
            item = self.items[key]
            if item["states"][platform] == "已完成":
                continue
            mark_manual_completed(item, platform, datetime.now(self.timezone).isoformat(timespec="seconds"))
            self.tree.update_states(key, item["states"])
        self.save_queue()
        self.notice.set(f"已将选中的 {len(keys)} 条{LABELS[platform]}记录标记为手动完成，继续时会跳过。")

    def review(self):
        if self.running:
            return
        for key in self.tree.selection():
            item=self.items[key]
            for p in PLATFORMS:
                if item["states"][p]!="待核对":
                    continue
                answer=messagebox.askyesnocancel("核对发布结果",
                    f"请在{LABELS[p]}核对《{item['folder'].name}》。\n已发布成功选“是”；确认未发布选“否”；未确认选“取消”。",parent=self.root)
                if answer is None:
                    continue
                item["states"][p]="已完成" if answer else "待发布"
                previous=dict(item["details"].get(p,{}))
                item["details"][p]={"manual_review":True,"submitted":bool(answer),"previous_attempt":previous}
                self.tree.update_states(key,item["states"])
                self.save_queue()

    def start(self, platform=None):
        if self.running or not self.items:
            return
        if platform is not None and platform not in PLATFORMS:
            raise ValueError(f"未知发布平台：{platform}")
        platforms=(platform,) if platform else PLATFORMS
        label=LABELS[platform] if platform else "三端"
        if getattr(self,"state_read_failed",False):
            self.notice.set("队列文件未完整读取，请修复后重新打开，当前记录不会覆盖原文件。")
            return
        if any(i["states"][p]=="待核对" for i in self.items.values() for p in platforms):
            self.notice.set(f"{label}有待核对作品，请双击该平台的“待核对”状态确认是否已发布。其他端可单独运行。")
            return
        if not any(p in pending_platforms(i) for i in self.items.values() for p in platforms):
            self.notice.set(f"{label}没有待发布记录，已完成作品会跳过。")
            return
        try:
            for item in self.items.values():
                for p in platforms:
                    if p not in pending_platforms(item):
                        continue
                    _,prepare,cover,category,select=self.adapters[p]
                    prepare(item["folder"]); cover(item["folder"]); category(item["folder"])
                    if item["scheduled"]:
                        select(item["date"],item["hour"])
        except Exception as error:
            messagebox.showerror("请检查作品与时间",str(error),parent=self.root)
            return
        self.running=True
        self.tree.set_enabled(False)
        self.start_button.configure(text="运行中",state="disabled")
        for button in getattr(self,"platform_buttons",{}).values():
            button.configure(state="disabled")
        self.root.withdraw()
        try:
            # 一端处理完再切换，减少每条作品在三张网页间切换。
            for p in platforms:
                for key,item in self.items.items():
                    if p in pending_platforms(item):
                        self.run_platform(key,item,p)
            self.notice.set(f"{label}全部完成，记录已保留。你可勾选后主动删除。")
        except Exception as error:
            self.notice.set(f"{label}流程已停止：{str(error) or type(error).__name__}。记录已保存，可单独运行其他端。")
        finally:
            self.running=False
            self.tree.set_enabled(True)
            self.start_button.configure(text="开始 / 继续三端发布",state="normal",command=self.start)
            for button in getattr(self,"platform_buttons",{}).values():
                button.configure(state="normal")
            if not self.closed:
                self.root.deiconify(); self.root.lift()
            else:
                self.root.quit()

    def run_platform(self, key, item, platform):
        """只更新本端状态，初始化、选择时间和网页失败都保存到本端历史。"""
        cls,prepare,cover,category,select=self.adapters[platform]
        previous=item["details"].get(platform,{})
        if previous.get("submitted"):
            item["states"][platform]="待核对"
            self.tree.update_states(key,item["states"])
            self.save_queue()
            raise RuntimeError(f"{LABELS[platform]}已记录点击发布，请先核对发布结果，避免重复提交。")
        detail={"submitted":False}
        if previous:
            history=list(previous.get("attempts",[]))
            history.append({k:v for k,v in previous.items() if k!="attempts"})
            detail["attempts"]=history
        if platform=="bilibili" and previous.get("stage"):
            detail["stage"]=previous["stage"]
        item["details"][platform]=detail
        item["states"][platform]="发布中"
        self.tree.update_states(key,item["states"])
        self.save_queue()
        self.notice.set(f"正在发布 {item['folder'].name} → {LABELS[platform]}")
        try:
            prepared=prepare(item["folder"])
            covers=cover(item["folder"])
            selected_category=category(item["folder"])
            fingerprint=source_fingerprint(platform,prepared,covers,selected_category)
            detail["source_fingerprint"]=fingerprint
            resume_settings=settings_resume_allowed(previous,fingerprint,platform)
            if resume_settings:
                detail["stage"]=previous.get("stage") or "settings"
            self.save_queue()
            draft=cls()
            if platform=="xiaohongshu":
                draft.resume_settings=resume_settings
            if platform=="bilibili" and not previous.get("submitted"):
                draft.bilibili_checkpoint=previous.get("bilibili_checkpoint")
                def save_bilibili_checkpoint(value):
                    detail["bilibili_checkpoint"]=value
                    self.save_queue()
                draft.on_bilibili_checkpoint=save_bilibili_checkpoint
                if draft.bilibili_checkpoint:
                    detail["bilibili_checkpoint"]=draft.bilibili_checkpoint
            if platform=="douyin":
                draft.resume_stage="settings" if resume_settings else None
            draft.on_tick=self.tick
            draft.on_stage=lambda stage:self.stage(item,platform,stage)
            draft.on_pause=lambda:self.pause_platform(item,platform)
            draft.on_submit=lambda:self.submitted(item,platform)
            selected=select(item["date"],item["hour"]) if item["scheduled"] else None
            detail["requested_mode"]="定时" if selected else "立即发布"
            detail["requested_time"]=str(selected) if selected else None
            self.save_queue()
            draft.run(*prepared,covers,selected_category,selected)
        except Exception as error:
            item["states"][platform]="待核对" if detail["submitted"] else "失败"
            detail["error"]=str(error) or f"{type(error).__name__}：查找控件失败，详见错误位置"
            detail["traceback"]=traceback.format_exc()
            self.tree.update_states(key,item["states"])
            self.save_queue()
            raise
        item["states"][platform]="已完成"
        detail["publish_at"]=str(getattr(draft,"actual_publish_at",selected) or "不定时")
        self.tree.update_states(key,item["states"])
        self.save_queue()

    def submitted(self,item,p):
        item["details"][p]["submitted"]=True
        item["details"][p]["submitted_at"] = datetime.now(self.timezone).isoformat(timespec="seconds")
        self.save_queue()

    def stage(self,item,p,stage):
        item["details"][p]["stage"] = stage
        item["details"][p]["stage_at"] = datetime.now(self.timezone).isoformat(timespec="seconds")
        self.save_queue()

    def pause_platform(self, item, platform):
        detail = item["details"][platform]
        detail["paused"] = True
        detail["paused_at"] = datetime.now(self.timezone).isoformat(timespec="seconds")
        self.save_queue()
        try:
            self.pause()
        finally:
            detail["paused"] = False
            self.save_queue()


class SubmissionTracking:
    tracks_submit_click=True
    on_submit=None
    tab_hint=""

    def submit(self, control):
        self.submission_requested=True
        try:
            self.click(control)
        finally:
            self.submission_requested=False

    def select_browser(self):
        candidates=[]
        for window in self.uia.GetRootControl().GetChildren():
            if not any(b in window.Name for b in ("Edge","Chrome","Firefox")):
                continue
            for tab,_ in self.uia.WalkControl(window,maxDepth=20):
                if tab.ControlTypeName=="TabItemControl" and self.tab_hint in tab.Name:
                    candidates.append((window,tab))
        if len(candidates)!=1:
            raise RuntimeError(f"请保留一个{self.tab_hint}标签页。")
        self.window,tab=candidates[0]
        self.window.SetFocus()
        time.sleep(0.4)
        r=tab.BoundingRectangle
        if r.width()<=0:
            raise RuntimeError("目标平台标签页不可见，请展开浏览器标签栏。")
        self.click_at(((r.left+r.right)//2,(r.top+r.bottom)//2))
        time.sleep(0.4)
        return super().select_browser()

    def click(self, control):
        submission = getattr(self,"submission_requested",False)
        previous = getattr(self, "before_click_press", None)
        if submission:
            def before_press():
                live = getattr(control, "control", control)
                for attempt in range(3):
                    if not self.visible(live) or not live.IsEnabled:
                        raise RuntimeError("发布按钮不可见或尚不可用，未点击发布。")
                    rect = live.BoundingRectangle
                    x, y = self.uia.GetCursorPos()
                    if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
                        break
                    if attempt < 2:
                        self.move_pointer(((rect.left + rect.right)//2, (rect.top + rect.bottom)//2))
                        self.check()
                else:
                    raise RuntimeError(f"发布按钮位置核验失败：按钮范围=({rect.left}, {rect.top}, {rect.right}, {rect.bottom})，鼠标=({x}, {y})，未点击发布。")
                if self.on_submit:
                    self.on_submit()
            self.before_click_press = before_press
        try:
            return super().click(control)
        finally:
            self.before_click_press = previous


def main():
    import ctypes
    # UIA初始化会设置进程DPI模式；必须先于Tk窗口创建，避免首次运行时窗口缩小。
    import windows_mcp.uia
    from bilibili_auto import BilibiliDraft,prepare_files,cover_from_folder,category_from_folder,select_hour,CHINA_TIME
    from douyin_auto import DouyinDraft,prepare_douyin,douyin_covers,select_douyin_hour
    from xiaohongshu_auto import XiaohongshuDraft,prepare_xiaohongshu,xiaohongshu_cover,select_xiaohongshu_hour
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    mutex=kernel.CreateMutexW(None,False,"Local\\CreatorPublishHelperMulti")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error()==183:
        kernel.CloseHandle(mutex)
        return
    root=tk.Tk(); root.withdraw()
    try:
        adapters={
            "bilibili":(type("TrackedBili",(SubmissionTracking,BilibiliDraft),{"tab_hint":"创作中心"}),prepare_files,cover_from_folder,category_from_folder,select_hour),
            "douyin":(type("TrackedDouyin",(SubmissionTracking,DouyinDraft),{"tab_hint":"抖音创作者中心"}),prepare_douyin,douyin_covers,lambda f:None,select_douyin_hour),
            "xiaohongshu":(type("TrackedXhs",(SubmissionTracking,XiaohongshuDraft),{"tab_hint":"小红书创作服务平台"}),prepare_xiaohongshu,xiaohongshu_cover,lambda f:None,select_xiaohongshu_hour)}
        MultiQueue(root,adapters,CHINA_TIME).show()
    finally:
        root.destroy(); kernel.CloseHandle(mutex)


if __name__=="__main__":
    main()
