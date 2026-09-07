import ctypes
import json
import random
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import keyboard
import pyautogui

# 首次要选择的星级：1～6
SELECTED_STAR = 1

# 星级标签中心坐标（1920×1080）
STAR_POSITIONS = {
    1: (598, 364),
    2: (747, 364),
    3: (898, 364),
    4: (1050, 364),
    5: (1202, 364),
    6: (1352, 364),
}

# 1 = 左边第一张卡；2 = 中间第二张卡；3 = 右边第三张卡
SELECTED_CARD = 2

# 三张卡片中心坐标（1920×1080）
CARD_POSITIONS = {
    1: (628, 550),
    2: (815, 550),
    3: (1008, 550),
}

# “召唤 Boss”按钮中心坐标
SUMMON_BOSS_POSITION = (1347, 804)

# 任务栏中目标程序的位置
TASKBAR_INDEX = "4"

# 网页没有连接时使用的默认值；网页提交后会以网页设置为准。
REPEAT_COUNT = 1
PROGRAM_OPEN_DELAY_SECONDS = 4
INTERVAL_SECONDS = 4.0
AFTER_CLICK_DELAY_MIN = 0.15
AFTER_CLICK_DELAY_MAX = 0.75

# “伤害占比”截图文件，须与本脚本位于同一目录
TARGET_IMAGE = Path(__file__).with_name("damage_ratio.png")
DAMAGE_RATIO_REGION = (580, 115, 140, 70)
IMAGE_CHECK_INTERVAL = 0.08
IMAGE_CONFIDENCE = 0.6
STATUS_PRINT_INTERVAL = 1.0

# 图片必须连续消失这么久，才会松开鼠标。
# 若仍会误松开，可改为 0.8 或 1.0。
IMAGE_LOST_CONFIRM_SECONDS = 1.0

# 所有鼠标操作共用一把锁，防止 click() 的 mouseUp() 冲掉长按。
mouse_lock = threading.Lock()
config_lock = threading.Lock()
config_ready = threading.Event()
active_config = {
    "stars": SELECTED_STAR,
    "card": SELECTED_CARD,
    "quantity": REPEAT_COUNT,
}


def get_config() -> dict:
    """读取网页提交的当前设置。"""
    with config_lock:
        return active_config.copy()


class ControlPanelHandler(BaseHTTPRequestHandler):
    """提供给本机 HTML 控制面板的极简接口。"""

    def log_message(self, format: str, *args) -> None:
        # 不输出浏览器的常规访问日志，避免干扰自动化状态输出。
        return

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self.send_json(200, {"ready": config_ready.is_set(), "config": get_config()})
        else:
            self.send_json(404, {"error": "接口不存在"})

    def do_POST(self) -> None:
        if self.path != "/api/config":
            self.send_json(404, {"error": "接口不存在"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            stars, card, quantity = int(data["stars"]), int(data["card"]), int(data["quantity"])
            if stars not in STAR_POSITIONS or card not in CARD_POSITIONS or not 1 <= quantity <= 999:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.send_json(400, {"error": "设置无效：星级为 1～6，卡片为 1～3，数量为 1～999。"})
            return

        with config_lock:
            active_config.update(stars=stars, card=card, quantity=quantity)
        config_ready.set()
        print(f"已收到控制面板设置：{stars} 星，第 {card} 张，共 {quantity} 次。")
        self.send_json(200, {"ok": True, "message": "设置已发送，自动化即将启动。"})


def start_control_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), ControlPanelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def open_control_panel() -> None:
    """用默认浏览器打开与脚本放在同一目录的控制面板。"""
    panel_path = Path(__file__).with_name("card_control_panel.html")
    if not panel_path.exists():
        raise FileNotFoundError(f"找不到控制面板文件：{panel_path}")
    webbrowser.open(panel_path.resolve().as_uri())


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def hide_console_window() -> None:
    """隐藏 Python 的命令窗口，让控制面板作为唯一可见界面。"""
    try:
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)  # SW_HIDE
    except Exception:
        pass


def wait_or_stop(seconds: float, pause_event: threading.Event) -> None:
    """等待；暂停期间不计时；按空格键立即停止。"""
    remaining = seconds
    last_time = time.monotonic()

    while remaining > 0:
        if keyboard.is_pressed("space"):
            raise KeyboardInterrupt

        if pause_event.is_set():
            time.sleep(0.02)
            last_time = time.monotonic()
            continue

        now = time.monotonic()
        remaining -= now - last_time
        last_time = now
        time.sleep(0.02)


def safe_click(x: int, y: int, pause_event: threading.Event) -> None:
    """点击后，如仍在暂停状态，重新恢复鼠标左键长按。"""
    with mouse_lock:
        pyautogui.click(x, y)

        # pyautogui.click() 会发送 mouseUp()；此处恢复监控所需长按。
        if pause_event.is_set():
            pyautogui.mouseDown(button="left")
            print("点击后仍处于暂停状态：已重新按住鼠标左键。")


def monitor_damage_ratio(
    stop_event: threading.Event,
    pause_event: threading.Event,
) -> None:
    """图片出现时长按并暂停；稳定消失后松开并恢复。"""
    is_holding = False
    image_missing_since = None
    last_status_time = 0.0

    print("图片检测已启动：等待检测“伤害占比”……")

    try:
        while not stop_event.is_set():
            try:
                found = pyautogui.locateOnScreen(
                    str(TARGET_IMAGE),
                    region=DAMAGE_RATIO_REGION,
                    confidence=IMAGE_CONFIDENCE,
                    grayscale=True,
                ) is not None
            except pyautogui.ImageNotFoundException:
                found = False

            now = time.monotonic()

            if found:
                image_missing_since = None
                pause_event.set()

                with mouse_lock:
                    if not is_holding:
                        pyautogui.mouseDown(button="left")
                        is_holding = True
                        print("【检测到图片】已暂停，并按住鼠标左键。")
            else:
                if image_missing_since is None:
                    image_missing_since = now

                if now - image_missing_since >= IMAGE_LOST_CONFIRM_SECONDS:
                    pause_event.clear()

                    with mouse_lock:
                        if is_holding:
                            pyautogui.mouseUp(button="left")
                            is_holding = False
                            print("【图片已消失】松开鼠标左键，继续执行。")

            if now - last_status_time >= STATUS_PRINT_INTERVAL:
                if found:
                    print("检测状态：已检测到图片，当前暂停并按住左键。")
                elif pause_event.is_set():
                    print("检测状态：图片短暂未识别，仍保持暂停与按住。")
                else:
                    print("检测状态：未检测到图片，正常运行中。")
                last_status_time = now

            time.sleep(IMAGE_CHECK_INTERVAL)

    finally:
        with mouse_lock:
            if is_holding:
                pyautogui.mouseUp(button="left")


def select_and_summon_boss(
    pause_event: threading.Event,
    first_time: bool,
    stars: int,
    card: int,
) -> None:
    if first_time:
        if stars not in STAR_POSITIONS:
            raise ValueError("星级只能设置为 1～6。")
        if card not in CARD_POSITIONS:
            raise ValueError("卡片只能设置为 1、2 或 3。")

        print(f"首次选择 {stars} 星……")
        safe_click(*STAR_POSITIONS[stars], pause_event)

        wait_or_stop(0.5, pause_event)

    card_x, card_y = CARD_POSITIONS[card]
    safe_click(card_x, card_y, pause_event)

    wait_or_stop(random.uniform(0.5, 1.0), pause_event)

    print("正在点击召唤 Boss……")
    safe_click(*SUMMON_BOSS_POSITION, pause_event)


def run(config: dict) -> None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1

    if not TARGET_IMAGE.exists():
        raise FileNotFoundError(
            f"找不到图片：{TARGET_IMAGE}\n"
            "请将“伤害占比”的截图保存为 damage_ratio.png，并放到脚本同目录。"
        )

    stop_event = threading.Event()
    pause_event = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_damage_ratio,
        args=(stop_event, pause_event),
        daemon=True,
    )
    monitor_thread.start()

    try:
        stars, card, repeat_count = config["stars"], config["card"], config["quantity"]
        print("正在运行新版防误松开脚本。")
        print("将在 3 秒后开始。按空格键或将鼠标移到屏幕左上角可紧急停止。")
        wait_or_stop(3, pause_event)

        pyautogui.hotkey("win", TASKBAR_INDEX)
        print(f"等待目标程序加载完成（{PROGRAM_OPEN_DELAY_SECONDS} 秒）……")
        wait_or_stop(PROGRAM_OPEN_DELAY_SECONDS, pause_event)

        screen_width, screen_height = pyautogui.size()
        pyautogui.moveTo(screen_width // 2, screen_height // 2, duration=0.2)

        iteration = 0
        while iteration < repeat_count:
            wait_or_stop(
                random.uniform(AFTER_CLICK_DELAY_MIN, AFTER_CLICK_DELAY_MAX),
                pause_event,
            )
            print("正在按下 E……")
            pyautogui.press("e")

            wait_or_stop(0.5, pause_event)
            select_and_summon_boss(pause_event, first_time=(iteration == 0), stars=stars, card=card)

            iteration += 1
            print(f"已执行第 {iteration} 次")
            wait_or_stop(INTERVAL_SECONDS, pause_event)

    finally:
        stop_event.set()
        pause_event.clear()
        with mouse_lock:
            pyautogui.mouseUp(button="left")
        monitor_thread.join(timeout=0.5)


if __name__ == "__main__":
    hide_console_window()
    if not is_admin():
        arguments = subprocess.list2cmdline(sys.argv)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        launcher = str(pythonw) if pythonw.exists() else sys.executable
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            launcher,
            arguments,
            None,
            1,
        )
        if result <= 32:
            print("未能以管理员身份启动，请在授权窗口中选择“是”。")
        raise SystemExit

    server = None
    try:
        server = start_control_server()
        print("控制面板已就绪：请打开 card_control_panel.html，设置后点击“发送设置并启动”。")
        open_control_panel()
        print("正在等待网页设置……")
        config_ready.wait()
        run(get_config())
    except pyautogui.FailSafeException:
        print("检测到鼠标位于屏幕左上角，已紧急停止。")
    except KeyboardInterrupt:
        print("检测到空格键，已停止。")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
