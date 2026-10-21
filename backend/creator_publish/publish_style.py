"""紧凑、接近 Windows 原生的发布窗口样式。"""
from tkinter import ttk


def apply_theme(root):
    background="#f7f7f7"
    root.configure(background=background)
    root.option_add("*TCombobox*Listbox.font",("Microsoft YaHei UI",9))
    style=ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    style.configure(".",font=("Microsoft YaHei UI",9),background=background,foreground="#252525")
    for name in ("TFrame","Page.TFrame","TLabel"):
        style.configure(name,background=background)
    style.configure("Heading.TLabel",foreground="#555555")
    style.configure("Subtitle.TLabel",foreground="#666666",background=background)
    style.configure("TButton",padding=(8,4))
    style.configure("Primary.TButton",padding=(10,4))
    style.configure("Danger.TButton",padding=(8,4))
    style.configure("TCombobox",padding=1)
    for name,color in (("Done","#43805c"),("Skip","#888888"),("Error","#b44646")):
        style.configure(f"{name}.TCombobox",foreground=color)
        style.map(f"{name}.TCombobox",foreground=[("disabled",color),("readonly",color)])
    style.configure("TCheckbutton",background=background)
    return style
