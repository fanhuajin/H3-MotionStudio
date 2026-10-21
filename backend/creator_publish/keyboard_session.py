"""快捷键助手随发布窗口开启和退出，不留常驻进程。"""
import os
from pathlib import Path
import subprocess
import sys

_process=None


def start(copy_file=None):
    global _process
    if _process is None or _process.poll() is not None:
        from fill_helper import AUTOMATION_READY
        AUTOMATION_READY.unlink(missing_ok=True)
        command=[sys.executable,"-B","-u","-X","utf8",
                 str(Path(__file__).with_name("fill_helper.py")),"--owner-pid",str(os.getpid())]
        if copy_file:
            command.append(str(copy_file))
        _process=subprocess.Popen(command,cwd=Path(__file__).parent,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return _process


def stop():
    global _process
    if _process is None:
        return
    process=_process
    _process=None
    if process.poll() is None:
        # Python 启动器可能另起子进程，必须一起退出才能释放热键。
        subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        process.wait(timeout=5)
    from fill_helper import AUTOMATION_READY,read_keyboard_state
    ready=read_keyboard_state(AUTOMATION_READY) or {}
    if ready.get("owner_pid")==os.getpid():
        AUTOMATION_READY.unlink(missing_ok=True)
