"""桌面壳：启动本地服务 + 程序窗口。

窗口优先用 pywebview（内嵌 WebView2）；如果这台电脑上起不来，
自动退回到用系统默认浏览器打开，保证在每台老师的电脑上都能用。

登录：打开第二个内嵌窗口加载幼师口袋登录页，老师扫码后程序读出 Cookie 交给后端。
（浏览器兜底模式下没有内嵌窗口，改为引导老师手动粘贴，见前端提示。）
"""
import ctypes
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

# PyInstaller 的 --windowed 模式下没有控制台，sys.stdout/stderr 会是 None，
# uvicorn 初始化日志时会调用 sys.stdout.isatty() 直接崩掉。先兜住。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from config import APP_TITLE, DATA_DIR, LOG_PATH, YSK_BASE

# ---- 日志 ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

# ---- 模块级状态：绝对不要挂到 js_api 对象上 ----
# pywebview 会深度遍历 js_api 的属性来生成 JS 代理，挂上 Window 会一路递归进
# WebView2 的 COM 对象里，刷出上百 MB 日志并拖死界面。
_WINDOW = None
_LOGIN_WINDOW = None
_LOGIN_ATTEMPT = None
_LOGIN_LOCK = threading.RLock()
_MAC_CLEAR_PENDING = None
_YSK_SITE_WINDOW = None
_YSK_SITE_OWNER = None
_SITE_NATIVE_PENDING = None
_PORT = 0
_SERVER_ERR = ""
# 本机服务必须直连；macOS 遗留的 ALL_PROXY 不能接管登录凭证交接。
_LOCAL_PROXIES = {"http": None, "https": None, "all": None}


def msgbox(text: str, title: str = APP_TITLE):
    """出了事要让老师看见 —— 打包之后没有控制台，print 是没人看的。"""
    try:
        import plat
        if plat.IS_WIN:
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
            return
        if plat.IS_MAC:
            import subprocess
            body = text.replace('"', "'").replace("\\", "/")
            subprocess.run(["/usr/bin/osascript", "-e",
                            f'display dialog "{body}" with title "{title}" buttons {{"知道了"}}'],
                           timeout=120)
            return
    except Exception:
        pass
    print(f"{title}: {text}")


def _close_splash():
    """单文件版启动时会先弹一张启动图，窗口出来了就把它关掉。"""
    try:
        import pyi_splash  # 只有 PyInstaller 打包后才存在
        pyi_splash.close()
    except Exception:
        pass


def single_instance() -> bool:
    """已经有一个在运行就返回 False。

    Windows 用全局互斥量；Mac 上系统本身就不会把同一个 .app 开两次，
    但从命令行或更新脚本里起来的可能重复，所以用一个带 pid 的锁文件兜一下。
    """
    if os.name == "nt":
        try:
            h = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\kidobs_community_single_instance")
            return not (h and ctypes.windll.kernel32.GetLastError() == 183)  # ERROR_ALREADY_EXISTS
        except Exception:
            return True
    lock = DATA_DIR / "running.pid"
    try:
        if lock.exists():
            old_pid = int((lock.read_text() or "0").strip() or 0)
            if old_pid and old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)      # 还活着
                    return False
                except OSError:
                    pass                     # 上次没退干净留下的，盖掉就是
        lock.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(port: int):
    global _SERVER_ERR
    try:
        import uvicorn
        from server import app
        # log_config=None：不让 uvicorn 重新配置日志（它的默认配置会探测 isatty）
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                      log_level="warning", log_config=None)).run()
    except Exception as e:
        _SERVER_ERR = f"{type(e).__name__}: {e}"
        log.exception("本地服务崩溃")


def wait_server(port: int, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


class Api:
    """暴露给前端 JS 的原生能力。只放方法，不放任何对象属性。"""

    def ping(self):
        return "ok"

    def set_title(self, text: str):
        """把授权剩余天数写进窗口标题栏——就在最小化/关闭按钮那一排。"""
        try:
            if _WINDOW:
                _WINDOW.set_title(str(text)[:120])
            return "ok"
        except Exception as e:
            return {"error": str(e)}

    def pick_files(self, kind="image"):
        import webview
        if kind == "excel":
            types = ("Excel 文件 (*.xlsx;*.xlsm)",)
        elif kind == "zip":
            types = ("压缩包 (*.zip)",)
        else:
            types = ("图片 (*.jpg;*.jpeg;*.png;*.bmp;*.webp)", "所有文件 (*.*)")
        try:
            return list(_WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=types) or [])
        except Exception as e:
            log.exception("选择文件失败")
            return {"error": str(e)}

    def pick_folder(self):
        import webview
        try:
            return list(_WINDOW.create_file_dialog(webview.FOLDER_DIALOG) or [])
        except Exception as e:
            log.exception("选择文件夹失败")
            return {"error": str(e)}

    def open_external(self, url):
        webbrowser.open(url)
        return True

    def logout_browser(self):
        """退出账号时，把内嵌浏览器里存的幼师口袋登录状态也清掉。

           为什么必须做这一步：我们这边"退出"只是把程序自己存的那份 Cookie 删了，
           而扫码登录用的那个内嵌浏览器还记着上一个账号。老师点了退出、再点登录，
           那个窗口一闪就登回同一个人 —— 看起来就像"退出根本没起作用"，
           想换个账号进去也换不了。
        """
        global _LOGIN_WINDOW, _LOGIN_ATTEMPT
        try:
            with _LOGIN_LOCK:
                _close_ysk_site()
                attempt, _LOGIN_ATTEMPT = _LOGIN_ATTEMPT, None
                _cancel_login_attempt(attempt)
                if _LOGIN_WINDOW is not None:
                    try:
                        _LOGIN_WINDOW.destroy()
                    except Exception:
                        pass
                    _LOGIN_WINDOW = None
                _clear_login_storage()
            log.info("已清掉内嵌浏览器里的登录状态")
            return True
        except Exception as e:
            log.warning("清内嵌浏览器登录状态失败：%s", e)
            return {"error": str(e)}

    def open_login(self):
        """打开幼师口袋登录窗口；登录成功后自动关闭并把 Cookie 交给后端。"""
        global _LOGIN_WINDOW, _LOGIN_ATTEMPT
        import webview
        with _LOGIN_LOCK:
            _close_ysk_site()
            log.info("收到打开幼师口袋登录窗口请求")
            created = False
            w = None
            attempt = None
            try:
                w = _LOGIN_WINDOW
                if w is None or w.events.closed.is_set():
                    # 每次新建扫码窗口都从干净的浏览器会话开始。程序异常退出、
                    # 或上次退出时清理失败，也不会一闪就登录回上一位老师。
                    log.info("登录窗口：开始本机登录请求")
                    attempt = _begin_login_attempt()
                    log.info("登录窗口：清理上一位老师的浏览器状态")
                    _clear_login_storage()
                    log.info("登录窗口：创建原生窗口")
                    w = webview.create_window("登录幼师口袋（扫码或输入账号）",
                                              YSK_BASE + "/react/pc/home", width=1100, height=780)
                    if w is None:
                        raise RuntimeError("桌面组件没有创建登录窗口")
                    created = True
                    _LOGIN_WINDOW = w
                    _LOGIN_ATTEMPT = attempt
                    def closed():
                        global _LOGIN_WINDOW, _LOGIN_ATTEMPT
                        _cancel_login_attempt(attempt)
                        with _LOGIN_LOCK:
                            if _LOGIN_WINDOW is w:
                                _LOGIN_WINDOW = None
                                _LOGIN_ATTEMPT = None
                    w.events.closed += closed
                else:
                    attempt = _LOGIN_ATTEMPT
                # Cocoa 的创建是异步的，拿到 Window 对象不代表窗口已经显示。
                log.info("登录窗口：等待显示事件")
                if not w.events.shown.wait(10):
                    raise RuntimeError("Mac／桌面组件未能显示登录窗口，请保存这条提示及程序日志")
                if w.events.closed.is_set():
                    raise RuntimeError("登录窗口已关闭，请重新打开")
                log.info("登录窗口：确认原生窗口可见")
                _show_login_window(w)
                if created:
                    threading.Thread(target=_watch_login, args=(w, attempt), daemon=True).start()
                log.info("幼师口袋登录窗口已显示")
                return True
            except Exception as e:
                _cancel_login_attempt(attempt)
                if w is not None:
                    if _LOGIN_WINDOW is w:
                        _LOGIN_WINDOW = None
                        _LOGIN_ATTEMPT = None
                    # Cocoa 可能在超时后才创建原生窗口；失败的旧请求不能留下多余窗口。
                    def discard():
                        if not w.events.closed.is_set():
                            try:
                                w.destroy()
                            except Exception:
                                log.exception("关闭未成功打开的登录窗口失败")
                    w.events.shown += discard
                    if w.events.shown.is_set():
                        discard()
                log.exception("打开登录窗口失败")
                return {"error": str(e)}

    def open_ysk_site(self):
        """用当前软件账号打开官网；凭证只在 Python 和原生 Cookie 仓库之间传递。"""
        global _YSK_SITE_WINDOW, _YSK_SITE_OWNER
        import webview
        import session_state
        with _LOGIN_LOCK, session_state.activity():
            window = None
            created = False
            try:
                if _LOGIN_WINDOW is not None and not _LOGIN_WINDOW.events.closed.is_set():
                    raise RuntimeError("请先完成或关闭当前登录窗口，再打开幼师口袋网站")
                owner, cookies = _ysk_site_session()
                if _YSK_SITE_OWNER != owner:
                    _close_ysk_site()
                browser_cookies = _native_ysk_cookies()
                if not _same_ysk_account(browser_cookies, owner[0]):
                    # 不同账号或过期的窗口必须先关闭，种植软件凭证后才加载官网。
                    _close_ysk_site()
                    _seed_ysk_cookies(cookies)
                    browser_cookies = _native_ysk_cookies()
                    if not _same_ysk_account(browser_cookies, owner[0]):
                        raise RuntimeError("网站登录凭证未能同步，请在软件中重新登录幼师口袋后再打开")
                if not _ysk_site_owner_current(owner):
                    raise RuntimeError("软件账号已变化，请重新打开网站")
                window = _YSK_SITE_WINDOW
                if window is None or window.events.closed.is_set():
                    # 官网窗口没有 js_api，不能调用软件的文件、登录或发布能力。
                    window = webview.create_window("幼师口袋网站", YSK_BASE + "/react/pc/home",
                                                   width=1220, height=820, min_size=(920, 640))
                    if window is None:
                        raise RuntimeError("桌面组件没有创建网站窗口")
                    created = True
                    _YSK_SITE_WINDOW, _YSK_SITE_OWNER = window, owner

                    def closed():
                        global _YSK_SITE_WINDOW, _YSK_SITE_OWNER
                        if _YSK_SITE_WINDOW is window:
                            _YSK_SITE_WINDOW, _YSK_SITE_OWNER = None, None

                    window.events.closed += closed
                if not window.events.shown.wait(10) or window.events.closed.is_set():
                    raise RuntimeError("桌面组件未能显示幼师口袋网站，请稍后重试")
                window.restore()
                window.show()
                if created:
                    threading.Thread(target=_watch_ysk_site, args=(window, owner, browser_cookies),
                                     daemon=True).start()
                return True
            except Exception as error:
                if created:
                    _close_ysk_site()
                log.warning("打开幼师口袋网站失败：%s", type(error).__name__)
                return {"error": str(error)}


def _show_login_window(window, timeout=6.0):
    """Mac 的 show 只是排队；在 UI 线程确认原生窗口确实仍存在并可见。"""
    import plat
    if not plat.IS_MAC:
        window.restore()
        window.show()
        return True
    import AppKit
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView
    done, cancelled, errors = threading.Event(), threading.Event(), []

    def present():
        try:
            if cancelled.is_set():
                return
            native = window.native
            view = BrowserView.instances.get(window.uid)
            if window.events.closed.is_set() or native is None or view is None or view.window is not native:
                raise RuntimeError("Mac 登录窗口已失效，请重新打开登录窗口")
            native.deminiaturize_(None)
            native.makeKeyAndOrderFront_(None)
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            if not native.isVisible():
                raise RuntimeError("Mac 登录窗口仍不可见，请正常退出程序后重新打开")
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    try:
        AppHelper.callAfter(present)
    except Exception as error:
        raise RuntimeError("Mac 未能调度登录窗口，请正常退出程序后重新打开") from error
    if not done.wait(timeout):
        cancelled.set()
        raise RuntimeError("Mac 登录窗口显示确认超时，请正常退出程序后重新打开")
    if errors:
        raise RuntimeError("Mac 未能显示登录窗口：%s" % errors[0]) from errors[0]
    return True


def _discard_ysk_site_window(window):
    """异步显示失败后迟到的窗口也要关闭。"""
    def discard():
        if not window.events.closed.is_set():
            try:
                window.destroy()
            except Exception:
                log.warning("关闭幼师口袋网站窗口失败")
    window.events.shown += discard
    if window.events.shown.is_set():
        discard()


def _close_ysk_site():
    global _YSK_SITE_WINDOW, _YSK_SITE_OWNER
    window, _YSK_SITE_WINDOW = _YSK_SITE_WINDOW, None
    _YSK_SITE_OWNER = None
    if window is not None:
        _discard_ysk_site_window(window)


def _ysk_site_owner_current(owner):
    import server
    import session_state
    with session_state.lock:
        return (str(server._ysk.uid or ""), session_state.generation) == owner


def _ysk_site_session():
    """直接读取本机软件会话，不通过 HTTP/JS 暴露 Cookie。"""
    import db
    import server
    import session_state
    with session_state.lock:
        uid = str(server._ysk.uid or "")
        cookies = db.get_setting("ysk_cookies")
        if not uid or not isinstance(cookies, dict) or not cookies:
            raise RuntimeError("请先在软件中登录幼师口袋")
        owner = (uid, session_state.generation)
    if not _same_ysk_account(cookies, uid):
        raise RuntimeError("软件登录已失效，请重新登录幼师口袋")
    return owner, cookies


def _same_ysk_account(cookies, uid):
    if not cookies:
        return False
    from ysk_client import NotLoggedIn, YskClient
    try:
        client = YskClient(cookies)
        user = client._unwrap(client._send("GET", "/api/user/getMyUserInfo",
                                           params=client._q(), timeout=(4, 8)))
        if not isinstance(user, dict):
            return False
        return str(user.get("id") or "") == str(uid)
    except NotLoggedIn:
        return False
    except Exception as error:
        raise YskSiteCheckUnavailable("暂时无法确认网站登录，请检查网络后重试") from error


class YskSiteCheckUnavailable(RuntimeError):
    pass


def _native_ysk_cookies():
    import plat
    if plat.IS_MAC:
        return _mac_cookies()
    if plat.IS_WIN:
        return _win_ysk_store()
    raise RuntimeError("当前浏览器模式不能安全复用软件登录，请使用桌面内嵌窗口")


def _is_ysk_cookie_domain(domain):
    domain = str(domain).lower().lstrip(".")
    return domain == "youshikoudai.com" or domain.endswith(".youshikoudai.com")


def _wait_site_native(timeout):
    global _SITE_NATIVE_PENDING
    pending = _SITE_NATIVE_PENDING
    if pending is not None:
        if not pending[0].wait(timeout):
            raise RuntimeError("网站登录同步尚未完成，请稍后重试")
        _SITE_NATIVE_PENDING = None


def _seed_ysk_cookies(cookies, timeout=8.0):
    """恢复丢失的共享浏览器会话；只清幼师口袋域，保留其他网站数据。"""
    import plat
    import re
    _wait_site_native(timeout)
    if not isinstance(cookies, dict) or not cookies:
        raise RuntimeError("软件没有可同步的登录凭证，请重新登录")
    for name, value in cookies.items():
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(name)) or
                not isinstance(value, str) or re.search(r"[\x00-\x20\x7f;,]", value)):
            raise RuntimeError("登录凭证格式不完整，请重新登录")
    if plat.IS_WIN:
        _win_ysk_store(cookies, timeout)
    elif plat.IS_MAC:
        _mac_seed_ysk_cookies(cookies, timeout)
    else:
        raise RuntimeError("当前浏览器模式不能安全复用软件登录")


def _win_ysk_store(cookies=None, timeout=8.0):
    """WebView2 CookieManager；所有原生对象仅在窗口 UI 线程使用。"""
    global _SITE_NATIVE_PENDING
    from webview.platforms import winforms, edgechromium
    if _WINDOW is None:
        raise RuntimeError("主窗口尚未准备好，请使用软件桌面窗口")
    form = winforms.BrowserView.instances.get(_WINDOW.uid)
    edge = getattr(form, "browser", None)
    native = getattr(edge, "webview", None)
    if native is None:
        raise RuntimeError("当前桌面组件不支持同步网站登录，请更新 WebView2")
    done, cancelled, errors, jar = threading.Event(), threading.Event(), [], {}
    if cookies is not None:
        _SITE_NATIVE_PENDING = (done, errors)

    def dispatch_read():
        try:
            manager = native.CoreWebView2.CookieManager

            def received(task):
                try:
                    if cancelled.is_set():
                        return
                    for cookie in task.Result:
                        if _is_ysk_cookie_domain(cookie.Domain):
                            if cookies is None:
                                jar[str(cookie.Name)] = str(cookie.Value)
                            else:
                                manager.DeleteCookie(cookie)
                    if cookies is not None:
                        for name, value in cookies.items():
                            cookie = manager.CreateCookie(name, value, "www.youshikoudai.com", "/")
                            cookie.IsSecure = True
                            cookie.IsHttpOnly = True
                            manager.AddOrUpdateCookie(cookie)
                except Exception as error:
                    errors.append(error)
                finally:
                    done.set()

            # 空 URI 读取该 profile 全部 Cookie，筛掉非幼师口袋域。
            callback = edgechromium.Action[edgechromium.Task[
                edgechromium.List[edgechromium.CoreWebView2Cookie]]](received)
            manager.GetCookiesAsync("").ContinueWith(callback, edge.syncContextTaskScheduler)
        except Exception as error:
            errors.append(error)
            done.set()

    try:
        native.Invoke(edgechromium.Action(dispatch_read))
    except Exception:
        done.set()
        if cookies is not None:
            _SITE_NATIVE_PENDING = None
        raise
    if not done.wait(timeout):
        cancelled.set()
        raise RuntimeError("读取或同步网站登录超时，请稍后重试")
    if cookies is not None:
        _SITE_NATIVE_PENDING = None
    if errors:
        raise RuntimeError("桌面组件未能同步网站登录，请重新登录") from errors[0]
    return jar


def _mac_seed_ysk_cookies(cookies, timeout=8.0):
    global _SITE_NATIVE_PENDING
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper
    done, cancelled, errors = threading.Event(), threading.Event(), []
    _SITE_NATIVE_PENDING = (done, errors)

    def begin():
        try:
            store = WebKit.WKWebsiteDataStore.defaultDataStore().httpCookieStore()

            def read(native_cookies):
                try:
                    if cancelled.is_set():
                        done.set()
                        return
                    old = [cookie for cookie in native_cookies or []
                           if _is_ysk_cookie_domain(cookie.domain())]

                    def install():
                        if cancelled.is_set():
                            done.set()
                            return
                        prepared = []
                        for name, value in cookies.items():
                            header = (f"{name}={value}; Domain=www.youshikoudai.com; "
                                      "Path=/; Secure; HttpOnly; SameSite=Lax")
                            url = Foundation.NSURL.URLWithString_(YSK_BASE + "/")
                            parsed = Foundation.NSHTTPCookie.cookiesWithResponseHeaderFields_forURL_(
                                {"Set-Cookie": header}, url)
                            if len(parsed) != 1:
                                raise RuntimeError("原生浏览器未能解析登录凭证")
                            prepared.append(parsed[0])
                        pending = {"active": 0, "scheduling": True}

                        def installed():
                            pending["active"] -= 1
                            if not pending["active"] and not pending["scheduling"]:
                                done.set()

                        for cookie in prepared:
                            if cancelled.is_set():
                                break
                            pending["active"] += 1
                            try:
                                store.setCookie_completionHandler_(cookie, installed)
                            except Exception as error:
                                pending["active"] -= 1
                                errors.append(error)
                                break
                        pending["scheduling"] = False
                        if not pending["active"]:
                            done.set()

                    def safe_install():
                        try:
                            install()
                        except Exception as error:
                            errors.append(error)
                            done.set()

                    if not old:
                        safe_install()
                    else:
                        pending = {"active": 0, "scheduling": True}

                        def deleted():
                            pending["active"] -= 1
                            if not pending["active"] and not pending["scheduling"]:
                                if errors:
                                    done.set()
                                else:
                                    safe_install()

                        for cookie in old:
                            if cancelled.is_set():
                                break
                            pending["active"] += 1
                            try:
                                store.deleteCookie_completionHandler_(cookie, deleted)
                            except Exception as error:
                                pending["active"] -= 1
                                errors.append(error)
                                break
                        pending["scheduling"] = False
                        if not pending["active"]:
                            if errors:
                                done.set()
                            else:
                                safe_install()
                except Exception as error:
                    errors.append(error)
                    done.set()

            store.getAllCookies_(read)
        except Exception as error:
            errors.append(error)
            done.set()

    try:
        AppHelper.callAfter(begin)
    except Exception:
        _SITE_NATIVE_PENDING = None
        raise
    if not done.wait(timeout):
        cancelled.set()
        raise RuntimeError("同步网站登录超时，请稍后重试")
    _SITE_NATIVE_PENDING = None
    if errors:
        raise RuntimeError("原生浏览器未能同步网站登录，请重新登录") from errors[0]


def _watch_ysk_site(window, owner, initial_cookies):
    """官网可以操作，但换软件账号或官网另登他人后关闭旧窗口。"""
    last = initial_cookies
    failed_checks = 0
    while not window.events.closed.is_set() and _YSK_SITE_WINDOW is window:
        time.sleep(2)
        if window.events.closed.is_set() or _YSK_SITE_WINDOW is not window:
            return
        if not _ysk_site_owner_current(owner):
            with _LOGIN_LOCK:
                if _YSK_SITE_WINDOW is window:
                    _close_ysk_site()
            return
        try:
            cookies = _native_ysk_cookies()
            if cookies != last:
                if not _same_ysk_account(cookies, owner[0]):
                    with _LOGIN_LOCK:
                        if _YSK_SITE_WINDOW is window:
                            _close_ysk_site()
                    _tell_main("网站登录状态已变化。请从软件设置重新打开幼师口袋网站。")
                    return
                last = cookies
                failed_checks = 0
        except YskSiteCheckUnavailable:
            failed_checks += 1
            if failed_checks <= 1:
                continue
            with _LOGIN_LOCK:
                if _YSK_SITE_WINDOW is window:
                    _close_ysk_site()
            _tell_main("网站登录状态暂时无法确认，请检查网络后重新打开网站。")
            return
        except Exception:
            with _LOGIN_LOCK:
                if _YSK_SITE_WINDOW is window:
                    _close_ysk_site()
            _tell_main("网站登录状态暂时无法确认，请重新打开网站。")
            return


def _begin_login_attempt() -> str:
    import requests
    response = requests.post(f"http://127.0.0.1:{_PORT}/api/login/begin", timeout=5,
                             proxies=_LOCAL_PROXIES)
    body = response.json()
    if response.status_code != 200 or not body.get("attempt"):
        raise RuntimeError(body.get("error") or "暂时无法开始登录，请稍后再试")
    return str(body["attempt"])


def _cancel_login_attempt(attempt):
    if not attempt:
        return
    try:
        import requests
        requests.post(f"http://127.0.0.1:{_PORT}/api/login/cancel",
                      json={"attempt": attempt}, timeout=5, proxies=_LOCAL_PROXIES)
    except Exception:
        log.warning("取消登录请求暂未送达；旧窗口不会继续提交凭证")


def _clear_login_storage(timeout: float = 8.0):
    """等待共享浏览器存储清理完成，再允许下一位老师扫码。"""
    global _MAC_CLEAR_PENDING
    import plat
    _wait_site_native(timeout)
    if not plat.IS_MAC:
        if _WINDOW is None:
            raise RuntimeError("主窗口尚未准备好，无法清理上一位老师的登录状态")
        _WINDOW.clear_cookies()  # WebView2 Invoke 同步派发 DeleteAllCookies。
        return
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper
    pending = _MAC_CLEAR_PENDING
    done, errors = pending or (threading.Event(), [])

    def clear():
        try:
            WebKit.WKWebsiteDataStore.defaultDataStore().removeDataOfTypes_modifiedSince_completionHandler_(
                WebKit.WKWebsiteDataStore.allWebsiteDataTypes(),
                Foundation.NSDate.dateWithTimeIntervalSince1970_(0), done.set)
        except Exception as error:
            errors.append(error)
            done.set()

    if pending is None:
        _MAC_CLEAR_PENDING = (done, errors)
        try:
            AppHelper.callAfter(clear)
        except Exception:
            _MAC_CLEAR_PENDING = None
            raise
    if not done.wait(timeout):
        raise RuntimeError("清理上一个登录账号超时。请正常退出程序后重新打开，再登录；重复点击不能完成尚未结束的清理")
    _MAC_CLEAR_PENDING = None
    if errors:
        raise RuntimeError("清理上一个登录账号失败，请重新打开登录窗口") from errors[0]


def _mac_cookies(timeout: float = 6.0) -> dict:
    """Mac 上自己去 WKWebView 的 Cookie 仓库要全部 Cookie。

       为什么不用 pywebview 自带的那个 get_cookies：
         · 它会拿"窗口当前网址"去过滤域名，登录过程中页面一跳，管用的那几条就被筛掉了；
         · 它内部等一个 Cocoa 回调，而且**没有超时**——回调里一出岔子就永远等下去。
       这两种坏法在老师那边看起来一模一样：登录完了，窗口不关，程序还说没登录。
       所以这里自己问一遍：只保留幼师口袋域名、带超时、出错就返回空。
    """
    jar = {}
    try:
        import WebKit
        from PyObjCTools import AppHelper
    except Exception as e:
        log.warning("Mac 取 Cookie：PyObjC 不可用 %s", e)
        return jar
    done = threading.Event()

    def handler(cookies):
        try:
            for c in cookies or []:
                try:
                    domain = str(c.domain()).lower().lstrip(".")
                    if domain == "youshikoudai.com" or domain.endswith(".youshikoudai.com"):
                        jar[str(c.name())] = str(c.value())
                except Exception:
                    pass
        finally:
            done.set()

    def read_cookies():
        try:
            store = WebKit.WKWebsiteDataStore.defaultDataStore().httpCookieStore()
            store.getAllCookies_(handler)
        except Exception as e:
            log.warning("Mac 取 Cookie 出错：%s", e)
            done.set()

    try:
        # 取得 WK 数据仓库也属于 Cocoa 操作，整段都在 UI 线程执行。
        AppHelper.callAfter(read_cookies)
    except Exception as e:
        log.warning("Mac 取 Cookie 出错：%s", e)
        return jar
    if not done.wait(timeout):
        log.warning("Mac 取 Cookie：等了 %s 秒没回话", timeout)
    return jar


def _login_jar(w) -> dict:
    """把登录窗口里的 Cookie 取出来。Mac 走自己那条路，Windows 用 pywebview 的。"""
    import plat
    if plat.IS_MAC:
        # 空结果也直接返回：原生 get_cookies 的 Cocoa 回调没有超时，不能回落。
        return _mac_cookies()
    try:
        raw = w.get_cookies()
    except Exception as e:
        log.warning("取 Cookie 失败：%s", e)
        return {}
    jar = {}
    for c in raw or []:
        try:
            for k, m in c.items():
                jar[k] = getattr(m, "value", m)
        except Exception:
            pass
    return jar


def _tell_main(msg: str):
    """把一句话推到主窗口上，让老师看得见 —— 不然登录卡住了没人知道为什么。"""
    try:
        _WINDOW.evaluate_js("window.__loginTrouble && window.__loginTrouble(%s)"
                            % __import__("json").dumps(msg, ensure_ascii=False))
    except Exception:
        pass


def _watch_login(w, attempt, timeout=15 * 60):
    """轮询登录窗口的 Cookie，拿到有效会话就交给后端并关窗。

       坑记：原来这里只认 200，别的一律当作"还没登录"接着转圈。
       可后端还会回 403（这台电脑绑了别的账号）——那是永远不会自己变好的，
       老师却只看到窗口一直开着、程序一直说没登录。现在每一种回复都说清楚。
    """
    global _LOGIN_WINDOW, _LOGIN_ATTEMPT
    import requests
    deadline = time.monotonic() + timeout
    time.sleep(2)
    said = {"empty": 0, "why": ""}
    i = -1
    while time.monotonic() < deadline:
        i += 1
        time.sleep(1)
        if w.events.closed.is_set() or _LOGIN_WINDOW is not w or _LOGIN_ATTEMPT != attempt:
            return
        jar = _login_jar(w)
        if w.events.closed.is_set() or _LOGIN_WINDOW is not w or _LOGIN_ATTEMPT != attempt:
            return
        if not jar:
            said["empty"] += 1
            if said["empty"] in (20, 60):
                log.warning("登录窗口轮询 %d 次后，一条 Cookie 都没取到", said["empty"])
                _tell_main("登录窗口开着，但程序一条登录凭证都没取到。"
                           "先把登录窗口关掉，重新点一次登录试试；还是不行就把这句话告诉管理员。")
            continue
        if i % 20 == 0:
            log.info("取到 %d 条 Cookie：%s", len(jar), ",".join(sorted(jar)[:12]))
        try:
            # 后端验证最多包含一次30秒请求和一次直连重试。过早放弃会使
            # 后端已登录、窗口却漏掉成功回包；关窗取消由独立事件处理。
            r = requests.post(f"http://127.0.0.1:{_PORT}/api/login/cookies",
                              json={"cookies": jar, "attempt": attempt}, timeout=75,
                              proxies=_LOCAL_PROXIES)
        except Exception as e:
            log.warning("把 Cookie 交给后端失败：%s", e)
            continue
        if w.events.closed.is_set() or _LOGIN_WINDOW is not w or _LOGIN_ATTEMPT != attempt:
            return
        if r.status_code == 200:
            log.info("登录成功")
            with _LOGIN_LOCK:
                if _LOGIN_WINDOW is not w or w.events.closed.is_set() or _LOGIN_ATTEMPT != attempt:
                    return
                try:
                    w.destroy()
                except Exception:
                    pass
                if _LOGIN_WINDOW is w:
                    _LOGIN_WINDOW = None
                    _LOGIN_ATTEMPT = None
            try:
                try:
                    result = r.json()
                    identity = {"id": (result.get("user") or {}).get("id"),
                                "session_generation": result.get("session_generation")}
                except Exception:
                    identity = {}
                _WINDOW.evaluate_js("window.__onLoggedIn && window.__onLoggedIn(%s)"
                                    % __import__("json").dumps(identity, ensure_ascii=False))
            except Exception:
                pass
            return
        why = ""
        try:
            why = (r.json() or {}).get("error") or ""
        except Exception:
            pass
        if r.status_code in (403, 409):
            # 登录尝试失效或当前有任务，停止等待并显示后端原因。
            log.warning("登录被拒（%s）：%s", r.status_code, why)
            with _LOGIN_LOCK:
                if _LOGIN_WINDOW is not w or w.events.closed.is_set() or _LOGIN_ATTEMPT != attempt:
                    return
                try:
                    w.destroy()
                except Exception:
                    pass
                if _LOGIN_WINDOW is w:
                    _LOGIN_WINDOW = None
                    _LOGIN_ATTEMPT = None
            _cancel_login_attempt(attempt)
            _tell_main(why or "登录已取消或有任务正在处理，请稍后重新打开登录窗口。")
            return
        if why and why != said["why"]:
            said["why"] = why
            log.info("后端暂时不认这份登录：%s", why)
        said["bad"] = said.get("bad", 0) + 1
        if said["bad"] in (15, 60):
            # 一直过不去就不能干等着了：把原话摆到老师眼前，顺手请她点「自查」
            log.warning("拿到 %d 条 Cookie，但一直登不上：%s", len(jar), why)
            _tell_main("已经拿到登录凭证，但幼师口袋那边一直没认下来。"
                       "原话是：%s。点登录窗口里的「自查」看看卡在哪一步。" % (why or "（没给原因）"))
    # 监听停止后不能留下一个仍可复用、却再也不接收凭证的窗口。
    # 只收尾当前请求，较晚结束的旧监听不能关闭下一次登录的窗口。
    with _LOGIN_LOCK:
        if _LOGIN_WINDOW is not w or _LOGIN_ATTEMPT != attempt:
            return
        _LOGIN_WINDOW = None
        _LOGIN_ATTEMPT = None
        try:
            w.destroy()
        except Exception:
            log.exception("关闭超时登录窗口失败")
    log.info("登录窗口超时结束")
    _cancel_login_attempt(attempt)
    _tell_main("等了十五分钟还没能拿到登录状态。把登录窗口关掉重来一次；"
               "要是一直这样，把这句话告诉管理员。")


def run_browser_mode(port: int):
    _close_splash()
    log.info("退回浏览器模式")
    webbrowser.open(f"http://127.0.0.1:{port}/?mode=browser")
    msgbox("程序已在你的默认浏览器中打开。\n\n请不要关闭这个提示框之外的黑色窗口（如果有），\n"
           "用完直接关闭浏览器标签页即可。", APP_TITLE)
    while True:
        time.sleep(3600)


def selftest() -> int:
    """Local startup checks only; no network calls, machine IDs or live credentials."""
    import importlib.util
    import config
    required = ['fastapi', 'uvicorn', 'requests', 'PIL', 'openpyxl', 'docx']
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    print('Yuanji Free', config.VERSION)
    print('data directory:', config.DATA_DIR)
    print('settings:', 'configured' if config.public_settings()['api_key_set'] else 'needs API key')
    print('dependencies:', ', '.join(missing) if missing else 'OK')
    print('webview:', 'available' if importlib.util.find_spec('webview') else 'browser mode available')
    print('static UI:', 'OK' if (config.STATIC_DIR / 'index.html').is_file() else 'missing')
    return int(bool(missing) or not (config.STATIC_DIR / 'index.html').is_file())


def main():
    global _WINDOW, _PORT
    if "--体检" in sys.argv or "--selftest" in sys.argv:
        sys.exit(selftest())
    if not single_instance():
        msgbox("「园记自由版」已经在运行了。\n请在任务栏里找到它的窗口。")
        return

    _PORT = free_port()
    log.info("=== 启动 v%s，端口 %s ===", __import__("config").VERSION, _PORT)
    threading.Thread(target=start_server, args=(_PORT,), daemon=True).start()
    if not wait_server(_PORT):
        msgbox("本地服务启动失败。\n\n" + (_SERVER_ERR or "未知原因") +
               "\n\n详细信息见 %APPDATA%\\kidobs-community\\kidobs.log")
        return
    log.info("本地服务就绪")

    if "--browser" in sys.argv:
        return run_browser_mode(_PORT)

    try:
        import webview
        # import webview 会重设 logger，必须在它之后调整；Mac 保留加载失败的诊断。
        logging.getLogger("pywebview").setLevel(logging.CRITICAL if os.name == "nt" else logging.WARNING)
    except Exception:
        log.exception("pywebview 不可用")
        return run_browser_mode(_PORT)

    try:
        storage = str(DATA_DIR / "webview")
        os.makedirs(storage, exist_ok=True)
        _WINDOW = webview.create_window(APP_TITLE, f"http://127.0.0.1:{_PORT}/",
                                        width=1280, height=820, min_size=(1024, 680),
                                        js_api=Api())
        _WINDOW.events.loaded += lambda: log.info("主窗口页面已加载")
        _WINDOW.events.closed += _close_ysk_site
        log.info("窗口已创建，正在启动内嵌浏览器")
        _close_splash()
        # Windows 用 WebView2（Edge 内核），Mac 用系统自带的 WKWebView ——
        # gui 传 None 让 pywebview 自己挑，省得在 Mac 上去找一个不存在的 edgechromium。
        import plat
        webview.start(gui=("edgechromium" if plat.IS_WIN else None),
                      debug="--debug" in sys.argv,
                      private_mode=False, storage_path=storage)
        log.info("窗口已关闭，程序退出")
    except Exception as e:
        log.exception("内嵌浏览器启动失败")
        msgbox(f"内嵌窗口启动失败，将改用浏览器打开。\n\n原因：{e}")  # noqa: E501
        run_browser_mode(_PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("程序异常退出")
        msgbox(f"程序异常退出：{e}\n\n详细信息见 {LOG_PATH}")
