"""三端差异都收在这一个文件里：Windows、Mac（M 芯片）、Mac（Intel）。

外面的代码只管调这里的函数，不去判断"现在是不是 Windows"。
以后再多一个系统，改这一个文件就够了。
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def arch() -> str:
    """arm64（苹果 M 芯片）还是 x86_64（Intel）。"""
    m = (platform.machine() or "").lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return m or "x86_64"


def key() -> str:
    """当前系统和处理器架构的简短名称。"""
    if IS_WIN:
        return "win"
    if IS_MAC:
        return "mac-arm64" if arch() == "arm64" else "mac-x64"
    return "linux"


def name_cn() -> str:
    return {"win": "Windows", "mac-arm64": "Mac（M 芯片）",
            "mac-x64": "Mac（Intel）"}.get(key(), key())


# ---------- 数据放哪 ----------
def data_root() -> Path:
    """各家系统自己的规矩。Windows 放 AppData，Mac 放「资源库/Application Support」。"""
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home()))
    if IS_MAC:
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


# ---------- 导出的东西放哪 ----------
def _win_desktop() -> Path:
    """桌面可能被 OneDrive 搬走过，问注册表最准，问不到就返回空。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        v, _ = winreg.QueryValueEx(k, "Desktop")
        d = Path(os.path.expandvars(str(v)))
        if d.is_dir():
            return d
    except Exception:
        pass
    return Path("")


EXPORT_FOLDER_NAME = "幼儿观察助手·社区版导出"


def _account_exports(root: Path) -> Path:
    # Keep each signed-in teacher's exported documents in a separate folder.
    import db
    uid = str(db.get_setting('current_uid') or '')
    target = root / ('账号-' + uid) if uid.isdigit() else root
    target.mkdir(parents=True, exist_ok=True)
    return target


def export_root() -> Path:
    """导出的 Word 放哪。老师得找得到 —— 先桌面，再文档，实在不行才退回数据目录。"""
    if os.environ.get('KIDOBS_DATA_DIR'):
        out = Path(os.environ['KIDOBS_DATA_DIR']).expanduser().resolve() / 'exports'
        out.mkdir(parents=True, exist_ok=True)
        return _account_exports(out)
    cands = []
    if IS_WIN:
        d = _win_desktop()
        if str(d):
            cands.append(d)
    cands += [Path.home() / "Desktop", Path.home() / "桌面",
              Path.home() / "Documents", Path.home() / "文档"]
    for c in cands:
        try:
            if c.is_dir():
                out = c / EXPORT_FOLDER_NAME
                out.mkdir(parents=True, exist_ok=True)
                return _account_exports(out)
        except Exception:
            continue
    out = data_root() / "kidobs-community" / "exports"
    out.mkdir(parents=True, exist_ok=True)
    return _account_exports(out)


def open_path(p) -> None:
    """用系统默认方式打开一个文件或文件夹。"""
    p = str(p)
    if IS_WIN:
        os.startfile(p)                      # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["/usr/bin/open", p], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", p], start_new_session=True)


def reveal_path(p) -> None:
    """打开文件夹，并把这个文件选中 —— 比只开文件夹更好找。"""
    p = Path(str(p))
    try:
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", str(p)])
            return
        if IS_MAC:
            subprocess.Popen(["/usr/bin/open", "-R", str(p)], start_new_session=True)
            return
    except Exception:
        pass
    open_path(p.parent if p.is_file() else p)


def describe() -> str:
    return f"{name_cn()}·{arch()}·{platform.platform()}"
