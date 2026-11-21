"""
ゲームパッド マクロ記録・再生ツール (Gamepad Macro Recorder & Player)

特徴:
- XInput (Xbox互換コントローラー) のボタン操作と待機時間を記録
- メモ帳などで開いて編集しやすいシンプルなテキスト形式 (.txt) で保存・読込
- ViGEmBus (vgamepad) を用いた仮想Xbox 360コントローラーでの高精度な再現
"""

import atexit
import ctypes
from ctypes import wintypes
import gc
import json
import os
import queue
import random
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    vg = None
    HAS_VGAMEPAD = False


# ==========================================
# XInput 定義 (Windows標準DLLから入力を読み取る)
# ==========================================
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [
        ("wLeftMotorSpeed", wintypes.WORD),
        ("wRightMotorSpeed", wintypes.WORD),
    ]


def load_xinput():
    for dll_name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            dll = ctypes.windll.LoadLibrary(dll_name)
            dll.XInputGetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_STATE)]
            dll.XInputGetState.restype = wintypes.DWORD
            dll.XInputSetState.argtypes = [wintypes.DWORD, ctypes.POINTER(XINPUT_VIBRATION)]
            dll.XInputSetState.restype = wintypes.DWORD
            return dll
        except OSError:
            continue
    return None


XINPUT_DLL = load_xinput()

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

try:
    _SET_THREAD_EXEC_STATE = ctypes.windll.kernel32.SetThreadExecutionState
    _SET_THREAD_EXEC_STATE.argtypes = [wintypes.DWORD]
    _SET_THREAD_EXEC_STATE.restype = wintypes.DWORD
except Exception:
    _SET_THREAD_EXEC_STATE = None


def set_system_keep_awake(enable):
    """Windows のシステムスリープを抑止/解除する。"""
    if _SET_THREAD_EXEC_STATE is None:
        return
    try:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enable else ES_CONTINUOUS
        _SET_THREAD_EXEC_STATE(flags)
    except Exception:
        pass


def set_xinput_vibration(controller_index, left_speed=0, right_speed=0):
    """指定スロットの XInput コントローラーに振動値を送信する。"""
    if XINPUT_DLL is None:
        return False
    vib = XINPUT_VIBRATION(int(left_speed), int(right_speed))
    try:
        return XINPUT_DLL.XInputSetState(controller_index, ctypes.byref(vib)) == 0
    except Exception:
        return False


def send_xinput_keepalive(stop_event=None):
    """
    接続中の XInput コントローラーへごく微小な振動パルスを送り、
    無線パッドの自動電源オフ（アイドルスリープ）タイマーをリセットする。
    """
    if XINPUT_DLL is None:
        return
    active_slots = [idx for idx in range(4) if get_xinput_state(idx) is not None]
    if not active_slots:
        return
    try:
        # 8bit変換後にも確実に非ゼロとして届く最小レベルの振動 (約1%) を一瞬だけ送信
        for idx in active_slots:
            set_xinput_vibration(idx, 660, 660)
        if stop_event is not None:
            stop_event.wait(0.06)
        else:
            time.sleep(0.06)
    finally:
        for idx in active_slots:
            set_xinput_vibration(idx, 0, 0)


# ==========================================
# DirectInput / Windows Joystick (winmm.dll) 定義
# HORI製、Switch/PS系など汎用ゲームパッド向け
# ==========================================
class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwXpos", wintypes.DWORD),
        ("dwYpos", wintypes.DWORD),
        ("dwZpos", wintypes.DWORD),
        ("dwRpos", wintypes.DWORD),
        ("dwUpos", wintypes.DWORD),
        ("dwVpos", wintypes.DWORD),
        ("dwButtons", wintypes.DWORD),
        ("dwButtonNumber", wintypes.DWORD),
        ("dwPOV", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("dwReserved2", wintypes.DWORD),
    ]


try:
    WINMM_DLL = ctypes.windll.winmm
    WINMM_DLL.joyGetPosEx.argtypes = [wintypes.UINT, ctypes.POINTER(JOYINFOEX)]
    WINMM_DLL.joyGetPosEx.restype = wintypes.UINT
except OSError:
    WINMM_DLL = None

# ボタン名とXInputビットマスクの対応
BUTTON_MASKS = {
    "UP": 0x0001,
    "DOWN": 0x0002,
    "LEFT": 0x0004,
    "RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "LS": 0x0040,
    "RS": 0x0080,
    "LB": 0x0100,
    "RB": 0x0200,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}

# vgamepad 定数マッピング
VG_BUTTON_MAP = {}
if HAS_VGAMEPAD:
    VG_BUTTON_MAP = {
        "UP": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        "DOWN": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        "LEFT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        "RIGHT": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        "START": vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        "BACK": vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        "LS": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        "RS": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
        "LB": vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        "RB": vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
        "Y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    }

# DirectInput (BTN1〜BTN12) -> 仮想Xbox360ボタンへの標準マッピング
DINPUT_TO_XBOX = {
    "BTN1": "Y",
    "BTN2": "B",
    "BTN3": "A",
    "BTN4": "X",
    "BTN5": "LB",
    "BTN6": "RB",
    "BTN7": "LT",
    "BTN8": "RT",
    "BTN9": "BACK",
    "BTN10": "START",
    "BTN11": "LS",
    "BTN12": "RS",
}

TRIGGER_THRESHOLD = 128  # 0〜255 のうち半分以上でONとみなす

MACRO_HEADER = """# ==========================================
# ゲームパッド マクロ記録ファイル
# ==========================================
# 書式 (メモ帳などで自由に編集・調整できます):
#   WAIT <秒数>        ... 指定した秒数だけ待機 (例: WAIT 0.500)
#   PRESS <ボタン名>   ... ボタンを押す (例: PRESS A)
#   RELEASE <ボタン名> ... ボタンを離す (例: RELEASE A)
#
# 使用可能なボタン名:
#   A, B, X, Y, UP, DOWN, LEFT, RIGHT,
#   LB, RB, LT, RT, LS, RS, START, BACK
# ==========================================
"""


def get_xinput_state(controller_index=0):
    """指定インデックスのXInputコントローラー状態を取得する。未接続時はNone。"""
    if XINPUT_DLL is None:
        return None
    state = XINPUT_STATE()
    res = XINPUT_DLL.XInputGetState(controller_index, ctypes.byref(state))
    if res != 0:  # 0 = ERROR_SUCCESS
        return None
    gp = state.Gamepad
    pressed = set()
    for name, mask in BUTTON_MASKS.items():
        if gp.wButtons & mask:
            pressed.add(name)
    if gp.bLeftTrigger >= TRIGGER_THRESHOLD:
        pressed.add("LT")
    if gp.bRightTrigger >= TRIGGER_THRESHOLD:
        pressed.add("RT")
    return pressed


def get_directinput_state(joy_index=0):
    """winmm.dll (DirectInput/HID互換) でコントローラー状態を取得する。未接続時はNone。"""
    if WINMM_DLL is None:
        return None
    info = JOYINFOEX()
    info.dwSize = ctypes.sizeof(JOYINFOEX)
    info.dwFlags = 0xFF  # JOY_RETURNALL
    res = WINMM_DLL.joyGetPosEx(joy_index, ctypes.byref(info))
    if res != 0:
        return None

    pressed = set()
    # ボタン1〜16 (最大32)
    for bit in range(32):
        if info.dwButtons & (1 << bit):
            pressed.add(f"BTN{bit + 1}")

    # 十字キー (POV: 0=上, 9000=右, 18000=下, 27000=左, 65535=未入力)
    pov = info.dwPOV
    if pov != 0xFFFF and pov < 36000:
        if pov in (31500, 0, 4500):
            pressed.add("UP")
        if pov in (4500, 9000, 13500):
            pressed.add("RIGHT")
        if pov in (13500, 18000, 22500):
            pressed.add("DOWN")
        if pov in (22500, 27000, 31500):
            pressed.add("LEFT")
    return pressed


def get_controller_state(controller_index=0, mode="AUTO"):
    """
    指定モードでコントローラー状態を取得する。
    戻り値: (pressed_set, detected_type) または (None, None)
    """
    if mode == "XInput":
        s = get_xinput_state(controller_index)
        return (s, "XInput") if s is not None else (None, None)
    if mode == "DirectInput":
        s = get_directinput_state(controller_index)
        return (s, "DirectInput") if s is not None else (None, None)

    # AUTO: まず XInput を確認し、未接続なら DirectInput を確認
    s = get_xinput_state(controller_index)
    if s is not None:
        return s, "XInput"
    s = get_directinput_state(controller_index)
    if s is not None:
        return s, "DirectInput"
    return None, None


def parse_macro_text(text):
    """
    テキスト形式のマクロをパースして命令リスト [(line_num, cmd, arg), ...] を返す。
    不正な行がある場合は ValueError を発生させる。
    """
    valid_buttons = set(BUTTON_MASKS.keys()) | {"LT", "RT"} | {f"BTN{i}" for i in range(1, 33)}
    commands = []
    for line_num, raw_line in enumerate(text.splitlines(), start=1):
        # '#' 以降はコメントとして無視
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "WAIT":
            if len(parts) != 2:
                raise ValueError(f"{line_num}行目: WAIT の形式が不正です -> {raw_line}")
            try:
                sec = float(parts[1])
                if sec < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"{line_num}行目: WAIT の秒数が不正です -> {parts[1]}")
            commands.append((line_num, "WAIT", sec))
        elif cmd in ("PRESS", "RELEASE"):
            if len(parts) != 2:
                raise ValueError(f"{line_num}行目: {cmd} の形式が不正です -> {raw_line}")
            btn = parts[1].upper()
            if btn not in valid_buttons:
                raise ValueError(f"{line_num}行目: 未知のボタン名です -> {btn}")
            commands.append((line_num, cmd, btn))
        else:
            raise ValueError(f"{line_num}行目: 未知のコマンドです -> {cmd}")
    return commands


# ==========================================
# Windows PnP デバイス制御（物理パッドと仮想パッドの1P自動入替用）
# ==========================================
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIRTUAL_DEVICE_EXCLUSIONS = ("PID_028E", "VIGEM", "SYNTHETICGAMEPAD", "ROOT\\SYSTEM")
NON_GAMEPAD_IDS = ("PID_02F3",)  # Xbox共有ボタン/チャットパッド等のキーボード系I/Fを除外
NON_GAMEPAD_KEYWORDS = ("KEYBOARD", "キーボード", "AUDIO", "HEADSET", "MICROPHONE", "MOUSE", "マウス")


def is_admin():
    """管理者権限で実行されているかを返す。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _enum_pnp_devices(*extra_args):
    """pnputil /enum-devices の出力をパースしてデバイス辞書のリストを返す。"""
    try:
        res = subprocess.run(
            ["pnputil", "/enum-devices", *extra_args],
            capture_output=True,
            text=True,
            encoding="cp932",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        return []

    devices = []
    cur = {}
    key_map = {
        "instance id": "instance_id",
        "インスタンス id": "instance_id",
        "device description": "description",
        "デバイスの説明": "description",
        "class name": "class_name",
        "クラス名": "class_name",
        "status": "status",
        "状態": "status",
    }
    for raw_line in res.stdout.splitlines():
        s = raw_line.strip()
        if not s:
            if cur.get("instance_id"):
                devices.append(cur)
            cur = {}
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            norm = key_map.get(k.strip().lower())
            if norm:
                cur[norm] = v.strip()
    if cur.get("instance_id"):
        devices.append(cur)
    return devices


def _device_tier(instance_id, cls_up=""):
    """
    無効化時に優先する階層を数値化する（小さいほど先に試す＝安全にXInputだけ外せる）。
    1: HID 層の XINPUT 互換デバイス (親のUSB/GIP接続を切らずにXInputだけ外せるため最安全)
    2: &IG_ 付きの USB / Bluetooth インターフェース子デバイス
    3: XboxComposite / &IG_ なしの USB 本体親 (無効化するとGIP再接続に抜き差しが必要な場合があるため最終手段)
    4: 無線アダプター
    """
    iid_up = instance_id.upper()
    if "VID_045E&PID_02FE" in iid_up:
        return 4
    if iid_up.startswith("HID\\"):
        return 1
    if cls_up == "XBOXCOMPOSITE":
        return 3
    if iid_up.startswith(("USB\\", "BTH")):
        return 2 if "&IG_" in iid_up else 3
    return 2


def _classify_gamepad_device(dev):
    """デバイスが物理コントローラー本体('controller')か無線アダプター('adapter')かを判定する。"""
    iid = dev.get("instance_id", "")
    desc = dev.get("description", "")
    cls = dev.get("class_name", "")
    iid_up = iid.upper()
    desc_up = desc.upper()
    cls_up = cls.upper()

    if not iid_up:
        return None
    if any(ex in iid_up or ex in desc_up for ex in VIRTUAL_DEVICE_EXCLUSIONS):
        return None
    if any(ng in iid_up for ng in NON_GAMEPAD_IDS):
        return None
    if any(kw in desc_up or kw in cls_up for kw in NON_GAMEPAD_KEYWORDS):
        return None

    # Xbox Wireless Adapter for Windows (無線レシーバー)
    if "VID_045E&PID_02FE" in iid_up or "XBOX WIRELESS ADAPTER" in desc_up:
        return "adapter"

    # 物理コントローラー本体（無線/有線 Xbox、XInput、汎用ゲームコントローラー）
    if "VID_045E" in iid_up and (
        "&IG_" in iid_up
        or cls_up == "XBOXCOMPOSITE"
        or any(pid in iid_up for pid in ("PID_0B12", "PID_02FF", "PID_02EA", "PID_0B13", "PID_02FD"))
    ):
        return "controller"
    if "XINPUT" in desc_up or "GAME CONTROLLER" in desc_up or "ゲーム コントローラー" in desc_up:
        return "controller"

    return None


def find_physical_gamepad_devices():
    """
    現在接続・稼働中の物理ゲームパッドの Instance ID を階層順で返す。
    戻り値: (controller_ids, adapter_ids)
    """
    devices = _enum_pnp_devices("/connected")
    controllers = []
    adapters = []
    for dev in devices:
        status = dev.get("status", "").lower()
        if status and status not in ("started", "開始済み", "開始"):
            continue
        kind = _classify_gamepad_device(dev)
        if kind == "controller":
            cls_up = dev.get("class_name", "").upper()
            controllers.append((_device_tier(dev["instance_id"], cls_up), dev["instance_id"]))
        elif kind == "adapter":
            adapters.append(dev["instance_id"])

    # 最上位の親デバイス (tier 1) -> 子I/F (tier 2) -> HID孫 (tier 3) の順に並べる
    controllers.sort(key=lambda item: (item[0], item[1]))
    return [iid for _, iid in controllers], adapters


def find_disabled_gamepad_devices():
    """無効化されている物理コントローラー／無線アダプターの Instance ID 一覧を上位→下位順で返す。"""
    devices = _enum_pnp_devices("/problem")
    result = []
    for dev in devices:
        kind = _classify_gamepad_device(dev)
        if kind in ("controller", "adapter"):
            cls_up = dev.get("class_name", "").upper()
            result.append((_device_tier(dev["instance_id"], cls_up), dev["instance_id"]))
    result.sort(key=lambda item: (item[0], item[1]))
    return [iid for _, iid in result]


def set_pnp_device_enabled(instance_id, enable=True):
    """pnputil でデバイスの有効化／無効化を行う（要管理者権限）。"""
    action = "/enable-device" if enable else "/disable-device"
    try:
        res = subprocess.run(
            ["pnputil", action, instance_id],
            capture_output=True,
            text=True,
            encoding="cp932",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except OSError as e:
        return False, str(e)


def restart_pnp_device(instance_id):
    """pnputil /restart-device でデバイスを再起動する（要管理者権限）。"""
    try:
        res = subprocess.run(
            ["pnputil", "/restart-device", instance_id],
            capture_output=True,
            text=True,
            encoding="cp932",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except OSError as e:
        return False, str(e)


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window_state.json")


class GamepadMacroApp:
    def __init__(self, root):
        self.root = root
        self.is_admin_user = is_admin()
        admin_tag = " [管理者]" if self.is_admin_user else " [一般権限]"
        self.root.title("ゲームパッド マクロ記録・再生ツール" + admin_tag)
        self.root.geometry("700x610")
        self.root.minsize(620, 520)

        self.is_recording = False
        self.is_playing = False
        self.stop_event = threading.Event()
        self._closing_event = threading.Event()
        self.current_file = os.path.join(os.getcwd(), "macro_record.txt")
        self._last_normal_geometry = "700x610"

        self.gamepad_out = None
        self.vgamepad_error = None
        self.disabled_device_ids = []
        self._device_lock = threading.Lock()
        self._vgamepad_lock = threading.RLock()
        self._ui_queue = queue.Queue()
        self._active_worker = None
        self._keep_awake_flag = True

        self._build_ui()
        self._load_window_state()
        self.root.bind("<Configure>", self._on_window_configure)
        self._init_vgamepad()
        self._drain_ui_queue()
        self._poll_controller_status()
        self._on_toggle_keep_awake()
        threading.Thread(target=self._keepalive_worker, daemon=True).start()
        threading.Thread(target=self._check_disabled_pads_on_startup, daemon=True).start()

        # 異常終了時も含め、無効化した物理パッドを必ず元に戻す
        atexit.register(self._restore_physical_1p)
        atexit.register(lambda: set_system_keep_awake(False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _check_disabled_pads_on_startup(self):
        """起動時に無効化されたままの物理パッドが残っていないか確認し、あれば復帰を案内する。"""
        try:
            disabled = find_disabled_gamepad_devices()
        except Exception:
            return
        if not disabled or self._closing_event.is_set():
            return

        def _prompt():
            if self._closing_event.is_set():
                return
            if self.is_admin_user:
                if messagebox.askyesno(
                    "無効な物理パッドを検出",
                    "前回の強制終了などで無効化されたままの物理コントローラーが見つかりました。\n"
                    "今すぐ有効化（復帰）しますか？",
                ):
                    self.manual_restore_physical_pad()
            else:
                messagebox.showwarning(
                    "無効な物理パッドを検出",
                    "無効化されたままの物理コントローラーが見つかりました。\n\n"
                    "start_macro.bat から管理者権限で起動し直し、\n"
                    "「🔄 物理パッドを戻す」ボタンで復帰してください。",
                )

        self._post_ui(_prompt)

    def _post_ui(self, func, *args):
        """バックグラウンドスレッドから安全に UI 更新を予約する（Tkinter 直接呼び出し禁止）。"""
        if not self._closing_event.is_set():
            self._ui_queue.put((func, args))

    def _drain_ui_queue(self):
        """メインスレッド上で UI 更新キューを処理する。"""
        if self._closing_event.is_set():
            return
        try:
            for _ in range(100):
                func, args = self._ui_queue.get_nowait()
                try:
                    func(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        if not self._closing_event.is_set():
            self.root.after(20, self._drain_ui_queue)

    def _on_window_configure(self, event):
        if event.widget is self.root and self.root.state() == "normal":
            self._last_normal_geometry = self.root.geometry()

    def _load_window_state(self):
        """前回のウィンドウ位置・サイズ・各種設定を読み込んで復元する。"""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        geom = data.get("geometry")
        if isinstance(geom, str) and geom:
            try:
                self.root.geometry(geom)
                self._last_normal_geometry = geom
            except Exception:
                pass

        if data.get("window_state") == "zoomed":
            try:
                self.root.state("zoomed")
            except Exception:
                pass

        if isinstance(data.get("port"), int) and 0 <= data["port"] <= 15:
            self.port_var.set(data["port"])
        if data.get("mode") in ("AUTO", "XInput", "DirectInput"):
            self.mode_var.set(data["mode"])
        if isinstance(data.get("keep_awake"), bool):
            self.keep_awake_var.set(data["keep_awake"])
        if isinstance(data.get("loop_count"), str) and data["loop_count"].isdigit():
            self.loop_count_var.set(data["loop_count"])
        if isinstance(data.get("loop"), bool):
            self.loop_var.set(data["loop"])
            self._on_toggle_loop()
        if isinstance(data.get("jitter_ms"), str) and data["jitter_ms"].isdigit():
            self.jitter_ms_var.set(data["jitter_ms"])
        if isinstance(data.get("jitter"), bool):
            self.jitter_var.set(data["jitter"])
            self._on_toggle_jitter()
        if isinstance(data.get("wait_first"), bool):
            self.wait_first_var.set(data["wait_first"])
        if isinstance(data.get("auto_swap"), bool):
            self.auto_swap_var.set(data["auto_swap"])
        if isinstance(data.get("auto_scroll"), bool):
            self.auto_scroll_var.set(data["auto_scroll"])

        saved_file = data.get("current_file")
        if isinstance(saved_file, str) and saved_file:
            self.current_file = saved_file
            self.file_label.config(text=f"現在のファイル: {self.current_file}")
            if os.path.isfile(saved_file):
                try:
                    with open(saved_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    self.text_area.delete("1.0", tk.END)
                    self.text_area.insert("1.0", content)
                except OSError:
                    pass

    def _save_window_state(self):
        """現在のウィンドウ位置・サイズ・各種設定を JSON に保存する。"""
        try:
            win_state = self.root.state()
            if win_state == "normal":
                self._last_normal_geometry = self.root.geometry()
            data = {
                "geometry": self._last_normal_geometry,
                "window_state": "zoomed" if win_state == "zoomed" else "normal",
                "port": int(self.port_var.get()),
                "mode": self.mode_var.get(),
                "keep_awake": bool(self.keep_awake_var.get()),
                "loop": bool(self.loop_var.get()),
                "loop_count": self.loop_count_var.get().strip(),
                "jitter": bool(self.jitter_var.get()),
                "jitter_ms": self.jitter_ms_var.get().strip(),
                "wait_first": bool(self.wait_first_var.get()),
                "auto_swap": bool(self.auto_swap_var.get()),
                "auto_scroll": bool(self.auto_scroll_var.get()),
                "current_file": self.current_file,
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _init_vgamepad(self):
        if not HAS_VGAMEPAD:
            self.vgamepad_error = "vgamepad ライブラリがインストールされていません。"
            return
        # 自動入替がOFFのときだけ常時接続にする（ONのときは再生時だけ仮想パッドを接続）
        if not self.auto_swap_var.get():
            self._connect_virtual_gamepad()

    def _connect_virtual_gamepad(self):
        if not HAS_VGAMEPAD:
            self.vgamepad_error = "vgamepad ライブラリがインストールされていません。"
            return False
        with self._vgamepad_lock:
            if self.gamepad_out is not None:
                return True
            try:
                self.gamepad_out = vg.VX360Gamepad()
                self.vgamepad_error = None
                return True
            except Exception as e:
                self.vgamepad_error = (
                    "仮想コントローラー (ViGEmBus) を初期化できませんでした。\n"
                    "再生機能を使うには ViGEmBus ドライバのインストールが必要です。\n"
                    f"詳細: {e}"
                )
                return False

    def _disconnect_virtual_gamepad(self):
        with self._vgamepad_lock:
            if self.gamepad_out is not None:
                try:
                    self._release_all_virtual_buttons()
                except Exception:
                    pass
                self.gamepad_out = None
                gc.collect()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # ステータスバー（上部）
        status_frame = ttk.LabelFrame(self.root, text="コントローラー状態", padding=8)
        status_frame.pack(fill="x", **pad)

        top_row = ttk.Frame(status_frame)
        top_row.pack(fill="x")

        ttk.Label(top_row, text="入力ポート:").pack(side="left")
        self.port_var = tk.IntVar(value=0)
        port_combo = ttk.Combobox(
            top_row, textvariable=self.port_var, values=list(range(16)), width=4, state="readonly"
        )
        port_combo.pack(side="left", padx=(4, 10))

        ttk.Label(top_row, text="方式:").pack(side="left")
        self.mode_var = tk.StringVar(value="AUTO")
        mode_combo = ttk.Combobox(
            top_row,
            textvariable=self.mode_var,
            values=["AUTO", "XInput", "DirectInput"],
            width=11,
            state="readonly",
        )
        mode_combo.pack(side="left", padx=(4, 12))

        self.conn_label = ttk.Label(top_row, text="確認中...", font=("Meiryo", 9, "bold"))
        self.conn_label.pack(side="left")

        self.state_label = ttk.Label(top_row, text="待機中", foreground="gray")
        self.state_label.pack(side="right")

        keep_row = ttk.Frame(status_frame)
        keep_row.pack(fill="x", pady=(6, 0))

        self.keep_awake_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            keep_row,
            text="起動中は物理パッドの自動電源オフを防止（3分ごとに微小信号を送信）",
            variable=self.keep_awake_var,
            command=self._on_toggle_keep_awake,
        ).pack(side="left")

        # 操作ボタンエリア
        ctrl_frame = ttk.LabelFrame(self.root, text="記録・再生コントロール", padding=8)
        ctrl_frame.pack(fill="x", **pad)

        btn_row1 = ttk.Frame(ctrl_frame)
        btn_row1.pack(fill="x", pady=(0, 6))

        self.btn_record = tk.Button(
            btn_row1,
            text="● 記録開始 (Record)",
            bg="#d9534f",
            fg="white",
            font=("Meiryo", 10, "bold"),
            width=18,
            command=self.start_recording,
        )
        self.btn_record.pack(side="left", padx=(0, 6))

        self.btn_stop = tk.Button(
            btn_row1,
            text="■ 停止 (Stop)",
            bg="#5a6268",
            fg="white",
            font=("Meiryo", 10, "bold"),
            width=13,
            state="disabled",
            command=self.stop_all,
        )
        self.btn_stop.pack(side="left", padx=(0, 10))

        self.btn_play = tk.Button(
            btn_row1,
            text="▶ 再生 (Play)",
            bg="#28a745",
            fg="white",
            font=("Meiryo", 10, "bold"),
            width=16,
            command=self.start_playing,
        )
        self.btn_play.pack(side="left", padx=(0, 8))

        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_row1,
            text="ループ再生",
            variable=self.loop_var,
            command=self._on_toggle_loop,
        ).pack(side="left", padx=(2, 4))

        ttk.Label(btn_row1, text="回数(0=無限):").pack(side="left")
        self.loop_count_var = tk.StringVar(value="0")
        self.loop_count_spin = ttk.Spinbox(
            btn_row1,
            from_=0,
            to=99999,
            textvariable=self.loop_count_var,
            width=6,
            state="disabled",
        )
        self.loop_count_spin.pack(side="left", padx=(4, 0))

        # オプション行
        btn_row2 = ttk.Frame(ctrl_frame)
        btn_row2.pack(fill="x")

        self.wait_first_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            btn_row2,
            text="最初のボタンが押された瞬間から記録する（冒頭の無操作時間をカット）",
            variable=self.wait_first_var,
        ).pack(side="left")

        btn_row3 = ttk.Frame(ctrl_frame)
        btn_row3.pack(fill="x", pady=(4, 0))

        self.auto_swap_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_row3,
            text="再生時だけ物理パッドと1Pを自動入替（停止で元に戻す・要管理者権限）",
            variable=self.auto_swap_var,
            command=self._on_toggle_auto_swap,
        ).pack(side="left")

        self.btn_restore_pad = ttk.Button(
            btn_row3,
            text="🔄 物理パッドを戻す",
            command=self.manual_restore_physical_pad,
        )
        self.btn_restore_pad.pack(side="right")

        btn_row4 = ttk.Frame(ctrl_frame)
        btn_row4.pack(fill="x", pady=(4, 0))

        self.jitter_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_row4,
            text="ループ時にタイミングを少しずらす（ランダム揺らぎ）",
            variable=self.jitter_var,
            command=self._on_toggle_jitter,
        ).pack(side="left")

        ttk.Label(btn_row4, text="追加幅(+ms):").pack(side="left", padx=(10, 0))
        self.jitter_ms_var = tk.StringVar(value="30")
        self.jitter_ms_spin = ttk.Spinbox(
            btn_row4,
            from_=0,
            to=5000,
            increment=10,
            textvariable=self.jitter_ms_var,
            width=6,
            state="disabled",
        )
        self.jitter_ms_spin.pack(side="left", padx=(4, 0))

        # ファイル操作エリア
        file_frame = ttk.LabelFrame(self.root, text="マクロファイル操作（メモ帳などで編集可能）", padding=8)
        file_frame.pack(fill="x", **pad)

        file_row = ttk.Frame(file_frame)
        file_row.pack(fill="x")

        ttk.Button(file_row, text="📂 ファイルを開く", command=self.load_file).pack(side="left", padx=(0, 6))
        ttk.Button(file_row, text="💾 保存する", command=self.save_file).pack(side="left", padx=(0, 6))
        ttk.Button(file_row, text="💾 名前を付けて保存", command=self.save_file_as).pack(side="left", padx=(0, 6))
        ttk.Button(file_row, text="📝 メモ帳で開く", command=self.open_in_notepad).pack(side="left", padx=(12, 0))

        self.file_label = ttk.Label(file_frame, text=f"現在のファイル: {self.current_file}", foreground="#555")
        self.file_label.pack(anchor="w", pady=(6, 0))

        # テキストエディタエリア
        editor_frame = ttk.LabelFrame(self.root, text="記録内容プレビュー / 直接編集", padding=8)
        editor_frame.pack(fill="both", expand=True, **pad)

        editor_top = ttk.Frame(editor_frame)
        editor_top.pack(fill="x", pady=(0, 4))

        self.auto_scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            editor_top,
            text="再生・記録中の自動スクロール",
            variable=self.auto_scroll_var,
        ).pack(side="left")

        self.btn_wait_plus = ttk.Button(
            editor_top,
            text="+0.001",
            width=8,
            command=lambda: self.adjust_all_waits(0.001),
        )
        self.btn_wait_plus.pack(side="right", padx=(4, 0))

        self.btn_wait_minus = ttk.Button(
            editor_top,
            text="-0.001",
            width=8,
            command=lambda: self.adjust_all_waits(-0.001),
        )
        self.btn_wait_minus.pack(side="right", padx=(4, 0))

        ttk.Label(editor_top, text="全WAIT一括調整:").pack(side="right")

        text_container = ttk.Frame(editor_frame)
        text_container.pack(fill="both", expand=True)

        text_scroll = ttk.Scrollbar(text_container)
        text_scroll.pack(side="right", fill="y")

        self.text_area = tk.Text(
            text_container,
            font=("Consolas", 10),
            undo=True,
            yscrollcommand=text_scroll.set,
        )
        self.text_area.pack(fill="both", expand=True)
        text_scroll.config(command=self.text_area.yview)

        # 実行中の行を強調表示するタグ
        self.text_area.tag_configure(
            "current_line",
            background="#ffe066",
            foreground="#000000",
            font=("Consolas", 10, "bold"),
        )

        self.text_area.insert("1.0", MACRO_HEADER + "\n")

    def _poll_controller_status(self):
        if self._closing_event.is_set():
            return
        idx = self.port_var.get()
        mode = self.mode_var.get()
        pressed, detected = get_controller_state(idx, mode)
        if pressed is None:
            self.conn_label.config(text=f"ポート {idx}: 未接続", foreground="#d9534f")
        else:
            btns = ", ".join(sorted(pressed)) if pressed else "なし"
            self.conn_label.config(
                text=f"ポート {idx} [{detected}]: 接続済み (入力中: {btns})",
                foreground="#28a745",
            )
        if not self._closing_event.is_set():
            self.root.after(150, self._poll_controller_status)

    def _update_buttons_state(self, running):
        if running:
            self.btn_record.config(state="disabled")
            self.btn_play.config(state="disabled")
            self.btn_stop.config(state="normal")
            self.loop_count_spin.config(state="disabled")
            self.jitter_ms_spin.config(state="disabled")
            self.btn_wait_minus.config(state="disabled")
            self.btn_wait_plus.config(state="disabled")
        else:
            self.btn_record.config(state="normal")
            self.btn_play.config(state="normal")
            self.btn_stop.config(state="disabled")
            self._on_toggle_loop()
            self._on_toggle_jitter()
            self.btn_wait_minus.config(state="normal")
            self.btn_wait_plus.config(state="normal")

    def adjust_all_waits(self, delta):
        """エディタ内のすべての WAIT 行の秒数を delta 秒だけ一括で増減する。"""
        if self.is_recording or self.is_playing:
            return
        raw_text = self.text_area.get("1.0", "end-1c")
        lines = raw_text.splitlines()
        changed = False
        new_lines = []
        pattern = re.compile(r"^(\s*WAIT\s+)(\d+(?:\.\d+)?)(.*)$", re.IGNORECASE)

        for line in lines:
            m = pattern.match(line)
            if m:
                prefix, num_str, suffix = m.groups()
                try:
                    val = float(num_str)
                    new_val = max(0.0, round(val + delta, 3))
                    new_lines.append(f"{prefix}{new_val:.3f}{suffix}")
                    changed = True
                    continue
                except ValueError:
                    pass
            new_lines.append(line)

        if not changed:
            return

        yview = self.text_area.yview()
        self.text_area.edit_separator()
        self.text_area.delete("1.0", tk.END)
        self.text_area.insert("1.0", "\n".join(new_lines) + "\n")
        self.text_area.edit_separator()
        self.text_area.yview_moveto(yview[0])

    def start_recording(self):
        if self.is_recording or self.is_playing:
            return
        idx = self.port_var.get()
        mode = self.mode_var.get()
        pressed, _ = get_controller_state(idx, mode)
        if pressed is None:
            messagebox.showwarning("未接続", f"ポート {idx} ({mode}) にコントローラーが接続されていません。")
            return

        self.is_recording = True
        self.stop_event.clear()
        self._update_buttons_state(True)
        self.state_label.config(text="● 記録中...", foreground="#d9534f")

        self.text_area.delete("1.0", tk.END)
        self.text_area.insert(tk.END, MACRO_HEADER + "\n")

        wait_first = bool(self.wait_first_var.get())
        t = threading.Thread(target=self._record_worker, args=(idx, mode, wait_first), daemon=True)
        self._active_worker = t
        t.start()

    def _record_worker(self, idx, mode, wait_first=True):
        prev_pressed, _ = get_controller_state(idx, mode)
        if prev_pressed is None:
            prev_pressed = set()
        last_event_time = None
        started = not wait_first
        if started:
            last_event_time = time.perf_counter()

        lines_buffer = []

        while not self.stop_event.is_set():
            curr_pressed, _ = get_controller_state(idx, mode)
            if curr_pressed is None:
                time.sleep(0.01)
                continue

            now = time.perf_counter()
            newly_pressed = sorted(curr_pressed - prev_pressed)
            newly_released = sorted(prev_pressed - curr_pressed)

            if newly_pressed or newly_released:
                if not started:
                    started = True
                    last_event_time = now
                else:
                    elapsed = now - last_event_time
                    if elapsed >= 0.001:
                        line = f"WAIT {elapsed:.3f}\n"
                        lines_buffer.append(line)
                        self._post_ui(self._append_text, line)
                    last_event_time = now

                for btn in newly_pressed:
                    line = f"PRESS {btn}\n"
                    lines_buffer.append(line)
                    self._post_ui(self._append_text, line)

                for btn in newly_released:
                    line = f"RELEASE {btn}\n"
                    lines_buffer.append(line)
                    self._post_ui(self._append_text, line)

            prev_pressed = curr_pressed
            time.sleep(0.004)  # 約250Hzポーリング

        # 記録終了時に押しっぱなしのボタンがあれば自動でRELEASEを追加
        if prev_pressed and started:
            elapsed = time.perf_counter() - last_event_time
            if elapsed >= 0.001:
                self._post_ui(self._append_text, f"WAIT {elapsed:.3f}\n")
            for btn in sorted(prev_pressed):
                self._post_ui(self._append_text, f"RELEASE {btn}\n")

        self._post_ui(self._on_worker_finished)

    def _append_text(self, line):
        self.text_area.insert(tk.END, line)
        if self.auto_scroll_var.get():
            self.text_area.see(tk.END)

    def _on_toggle_loop(self):
        if self.loop_var.get():
            self.loop_count_spin.config(state="normal")
        else:
            self.loop_count_spin.config(state="disabled")

    def _on_toggle_jitter(self):
        if self.jitter_var.get():
            self.jitter_ms_spin.config(state="normal")
        else:
            self.jitter_ms_spin.config(state="disabled")

    def _on_toggle_auto_swap(self):
        if self.is_playing or self.is_recording:
            return
        if self.auto_swap_var.get():
            # 自動入替ONの待機中は仮想パッドを切断し、物理パッドを1Pに保つ
            self._disconnect_virtual_gamepad()
        else:
            # 自動入替OFFなら従来通り仮想パッドを常時接続
            self._connect_virtual_gamepad()

    def _set_status_async(self, text, color="gray"):
        self._post_ui(lambda: self.state_label.config(text=text, foreground=color))

    def _wait_xinput_slot0(self, want_connected, timeout=1.5, abort_on_stop=False):
        """ポート0 (1P) の接続状態が want_connected になるまで最大 timeout 秒待機する。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if abort_on_stop and self.stop_event.is_set():
                return False
            is_conn = get_xinput_state(0) is not None
            if is_conn == want_connected:
                return True
            time.sleep(0.05)
        return (get_xinput_state(0) is not None) == want_connected

    def _switch_to_virtual_1p(self):
        """
        物理パッドを一時無効化してポート0(1P)を空け、仮想パッドを1Pとして接続する。
        親デバイスが存在する場合は親のみを無効化し、子・孫デバイスの不整合を防ぐ。
        戻り値: (ok: bool, error_message: str | None)
        """
        with self._device_lock:
            self._disconnect_virtual_gamepad()

            controllers, adapters = find_physical_gamepad_devices()
            disabled = []

            # 1) 最も安全な HID 層 (tier 1: XINPUT 互換 HID デバイス) から1階層ずつ無効化し、
            #    ポート0が空いた時点で止める（親の XboxComposite / USB 接続を切らないため復帰が確実）
            for tier in (1, 2, 3):
                if self.stop_event.is_set() or get_xinput_state(0) is None:
                    break
                tier_ids = [iid for iid in controllers if _device_tier(iid) == tier]
                if not tier_ids:
                    continue
                disabled_in_tier = False
                for iid in tier_ids:
                    if self.stop_event.is_set():
                        break
                    ok, _ = set_pnp_device_enabled(iid, enable=False)
                    if ok:
                        disabled.append(iid)
                        disabled_in_tier = True
                if disabled_in_tier:
                    self._wait_xinput_slot0(want_connected=False, timeout=1.2, abort_on_stop=True)

            # 2) まだポート0が空いていなければ無線アダプターも無効化（フォールバック）
            if not self.stop_event.is_set() and get_xinput_state(0) is not None and adapters:
                for iid in adapters:
                    if self.stop_event.is_set():
                        break
                    ok, _ = set_pnp_device_enabled(iid, enable=False)
                    if ok:
                        disabled.append(iid)
                self._wait_xinput_slot0(want_connected=False, timeout=1.5, abort_on_stop=True)

            self.disabled_device_ids = disabled

            if self.stop_event.is_set():
                return False, None

            # 3) 仮想コントローラーを接続して1Pとして認識させる
            if not self._connect_virtual_gamepad():
                return False, self.vgamepad_error or "仮想コントローラーの接続に失敗しました。"

            self._wait_xinput_slot0(want_connected=True, timeout=1.5, abort_on_stop=True)
            # ゲーム側が新しい1Pコントローラーを認識するための待機
            self._precise_wait(1.25)
            return True, None

    def _enable_and_wait_physical_1p(self, extra_ids=None):
        """
        無効化されている物理パッドを親→子の順に再有効化し、
        XInput ポート0 (1P) に復帰するまで確実に待機・必要に応じて再起動する。
        戻り値: 操作対象になったデバイス件数
        """
        targets = sorted(
            set(extra_ids or []) | set(find_disabled_gamepad_devices()),
            key=lambda x: (-_device_tier(x), x),
        )
        restored = 0
        for idx, iid in enumerate(targets):
            ok, _ = set_pnp_device_enabled(iid, enable=True)
            if ok:
                restored += 1
            # 親デバイスを有効化した直後は子デバイスの再列挙を待つ
            if _device_tier(iid) >= 2 and idx < len(targets) - 1:
                time.sleep(0.6)

        # 親の有効化後に遅れて現れた無効状態の関連デバイスがあれば追加で有効化
        time.sleep(0.25)
        for iid in find_disabled_gamepad_devices():
            ok, _ = set_pnp_device_enabled(iid, enable=True)
            if ok:
                restored += 1

        # stop_event が立っていても（停止ボタン・終了時）復帰完了まで必ず待つ
        if self._wait_xinput_slot0(want_connected=True, timeout=3.5, abort_on_stop=False):
            return restored

        # まだ1Pに戻っていない場合は tier 1 (HID) → tier 2 (&IG_子I/F) の順にリフレッシュを試す
        controllers, _ = find_physical_gamepad_devices()
        candidates = sorted(set(targets) | set(controllers), key=lambda x: (_device_tier(x), x))
        for tier in (1, 2):
            tier_ids = [iid for iid in candidates if _device_tier(iid) == tier]
            if not tier_ids:
                continue
            restarted = False
            for iid in tier_ids:
                ok, _ = restart_pnp_device(iid)
                if ok:
                    restored += 1
                    restarted = True
            if restarted and self._wait_xinput_slot0(want_connected=True, timeout=3.0, abort_on_stop=False):
                return restored

        return restored

    def _restore_physical_1p(self):
        """仮想パッドを切断し、無効化していた物理パッドを親→子の順に再有効化して1Pに戻す。"""
        with self._device_lock:
            disabled_snapshot = list(self.disabled_device_ids)
            if not disabled_snapshot and self.gamepad_out is None:
                return
            if disabled_snapshot or find_disabled_gamepad_devices():
                # 1) まず仮想パッドを切断し、ポート0が確実に空くまで待機する
                self._disconnect_virtual_gamepad()
                self._wait_xinput_slot0(want_connected=False, timeout=2.0, abort_on_stop=False)
                time.sleep(0.3)

                # 2) 物理パッドを親→子の順に再有効化し、1P復帰まで確実に待つ
                self._enable_and_wait_physical_1p(disabled_snapshot)
                self.disabled_device_ids = []

    def manual_restore_physical_pad(self):
        """万が一物理パッドが無効化されたまま／未接続になった場合の手動復帰ボタン。"""
        if self.is_playing:
            self.stop_all()
            return
        if not self.is_admin_user:
            messagebox.showwarning(
                "管理者権限が必要",
                "物理パッドの再有効化には管理者権限が必要です。\nstart_macro.bat から起動し直してください。",
            )
            return

        auto_swap = bool(self.auto_swap_var.get())

        def _worker():
            self._set_status_async("🔄 物理パッドを復帰中...", "#007bff")
            self._disconnect_virtual_gamepad()
            self._wait_xinput_slot0(want_connected=False, timeout=2.0, abort_on_stop=False)
            time.sleep(0.3)
            with self._device_lock:
                disabled_snapshot = list(self.disabled_device_ids)
                restored = self._enable_and_wait_physical_1p(disabled_snapshot)
                self.disabled_device_ids = []
            if not auto_swap:
                self._connect_virtual_gamepad()
            self._set_status_async("待機中", "gray")
            self._post_ui(
                messagebox.showinfo,
                "復帰完了",
                f"物理コントローラーの復帰処理を実行しました（対象: {restored}件）。"
                if restored > 0
                else "無効化されている物理コントローラーは見つかりませんでした。",
            )

        t = threading.Thread(target=_worker, daemon=True)
        self._active_worker = t
        t.start()

    def start_playing(self):
        if self.is_recording or self.is_playing:
            return
        if not HAS_VGAMEPAD:
            messagebox.showerror("再生エラー", "vgamepad ライブラリがインストールされていません。")
            return

        auto_swap = self.auto_swap_var.get()
        if auto_swap and not self.is_admin_user:
            messagebox.showwarning(
                "管理者権限が必要",
                "「再生時だけ物理パッドと1Pを自動入替」を使うには管理者権限が必要です。\n\n"
                "・start_macro.bat をダブルクリックして起動する（自動で管理者権限を要求します）\n"
                "・またはチェックを外して再生してください。",
            )
            return

        if not auto_swap and not self._connect_virtual_gamepad():
            messagebox.showerror("再生エラー", self.vgamepad_error or "仮想コントローラーを初期化できませんでした。")
            return

        raw_text = self.text_area.get("1.0", tk.END)
        try:
            commands = parse_macro_text(raw_text)
        except ValueError as e:
            messagebox.showerror("書式エラー", str(e))
            return

        if not commands:
            messagebox.showinfo("情報", "実行できるコマンドがありません。")
            return

        loop = self.loop_var.get()
        loop_limit = 1
        if loop:
            raw_count = self.loop_count_var.get().strip()
            try:
                loop_limit = int(raw_count)
                if loop_limit < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("入力エラー", "ループ回数には 0 以上の整数を入力してください（0 = 無限ループ）。")
                return

        jitter_sec = 0.0
        if loop and self.jitter_var.get():
            raw_jitter = self.jitter_ms_var.get().strip()
            try:
                jitter_ms = int(raw_jitter)
                if jitter_ms < 0:
                    raise ValueError
                jitter_sec = jitter_ms / 1000.0
            except ValueError:
                messagebox.showerror("入力エラー", "ランダム揺らぎの幅(ms)には 0 以上の整数を入力してください。")
                return

        self.is_playing = True
        self.stop_event.clear()
        self._update_buttons_state(True)
        self.state_label.config(
            text="🔄 1Pを仮想パッドへ切替中..." if auto_swap else "▶ 再生中...",
            foreground="#007bff" if auto_swap else "#28a745",
        )

        t = threading.Thread(
            target=self._play_worker,
            args=(commands, loop, loop_limit, auto_swap, jitter_sec),
            daemon=True,
        )
        self._active_worker = t
        t.start()

    def _precise_wait(self, seconds):
        end_time = time.perf_counter() + seconds
        while not self.stop_event.is_set():
            remaining = end_time - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(0.005, remaining))

    def _release_all_virtual_buttons(self):
        with self._vgamepad_lock:
            if not self.gamepad_out:
                return
            for btn_code in VG_BUTTON_MAP.values():
                self.gamepad_out.release_button(button=btn_code)
            self.gamepad_out.left_trigger_float(value_float=0.0)
            self.gamepad_out.right_trigger_float(value_float=0.0)
            self.gamepad_out.update()

    def _highlight_line(self, line_num, loop_count=1, is_loop=False, loop_limit=0, reset_scroll=False):
        """実行中の行をハイライトし、自動スクロールする（UIスレッドから実行）。"""
        if not self.is_playing:
            return
        self.text_area.tag_remove("current_line", "1.0", tk.END)
        start_idx = f"{line_num}.0"
        end_idx = f"{line_num + 1}.0"
        self.text_area.tag_add("current_line", start_idx, end_idx)

        if self.auto_scroll_var.get():
            if reset_scroll:
                # ループ開始時や先頭行実行時は先頭へ戻してから該当行を表示
                self.text_area.yview_moveto(0.0)
                self.text_area.see(start_idx)
            else:
                # 下方向に余裕を持たせて自動スクロール
                self.text_area.see(f"{line_num + 3}.0")
                self.text_area.see(start_idx)

        if is_loop:
            if loop_limit > 0:
                self.state_label.config(
                    text=f"▶ 再生中 ({loop_count}/{loop_limit}周目 / {line_num}行目)",
                    foreground="#28a745",
                )
            else:
                self.state_label.config(
                    text=f"▶ 再生中 ({loop_count}周目 / {line_num}行目)",
                    foreground="#28a745",
                )
        else:
            self.state_label.config(
                text=f"▶ 再生中 ({line_num}行目)",
                foreground="#28a745",
            )

    def _clear_highlight(self):
        self.text_area.tag_remove("current_line", "1.0", tk.END)

    def _update_remaining_loop_count(self, remaining):
        """ループ1周完了ごとに残り回数をカウントダウンし、0になったらループ設定も解除する。"""
        self.loop_count_var.set(str(remaining))
        if remaining <= 0:
            self.loop_var.set(False)
            self._on_toggle_loop()

    def _play_worker(self, commands, loop, loop_limit=0, auto_swap=False, jitter_sec=0.0):
        loop_count = 0
        try:
            if auto_swap:
                ok, err = self._switch_to_virtual_1p()
                if not ok:
                    if err:
                        self._post_ui(messagebox.showerror, "切替エラー", err)
                    return

            while not self.stop_event.is_set():
                loop_count += 1
                for idx, (line_num, cmd, arg) in enumerate(commands):
                    if self.stop_event.is_set():
                        break
                    self._post_ui(
                        self._highlight_line,
                        line_num,
                        loop_count,
                        loop,
                        loop_limit,
                        idx == 0,
                    )
                    if cmd == "WAIT":
                        wait_sec = arg + random.uniform(0.0, jitter_sec) if jitter_sec > 0.0 else arg
                        self._precise_wait(wait_sec)
                    elif cmd == "PRESS":
                        target = DINPUT_TO_XBOX.get(arg, arg)
                        with self._vgamepad_lock:
                            if self.gamepad_out is None:
                                break
                            if target == "LT":
                                self.gamepad_out.left_trigger_float(value_float=1.0)
                            elif target == "RT":
                                self.gamepad_out.right_trigger_float(value_float=1.0)
                            elif target in VG_BUTTON_MAP:
                                self.gamepad_out.press_button(button=VG_BUTTON_MAP[target])
                            self.gamepad_out.update()
                    elif cmd == "RELEASE":
                        target = DINPUT_TO_XBOX.get(arg, arg)
                        with self._vgamepad_lock:
                            if self.gamepad_out is None:
                                break
                            if target == "LT":
                                self.gamepad_out.left_trigger_float(value_float=0.0)
                            elif target == "RT":
                                self.gamepad_out.right_trigger_float(value_float=0.0)
                            elif target in VG_BUTTON_MAP:
                                self.gamepad_out.release_button(button=VG_BUTTON_MAP[target])
                            self.gamepad_out.update()

                if not self.stop_event.is_set() and loop and loop_limit > 0:
                    remaining = max(0, loop_limit - loop_count)
                    self._post_ui(self._update_remaining_loop_count, remaining)

                if not loop or (loop_limit > 0 and loop_count >= loop_limit):
                    break

                # ループ折り返し時にもランダムな微小待機を入れて周回間隔をずらす
                if not self.stop_event.is_set() and jitter_sec > 0.0:
                    self._precise_wait(random.uniform(0.0, jitter_sec))
        finally:
            self._release_all_virtual_buttons()
            if auto_swap:
                self._set_status_async("🔄 物理パッドを1Pへ復帰中...", "#007bff")
                self._restore_physical_1p()
                # 自動入替ONのときは、物理パッドが未接続だった場合も仮想パッドを切断して元の状態に戻す
                self._disconnect_virtual_gamepad()
            self._post_ui(self._on_worker_finished)

    def stop_all(self):
        self.stop_event.set()

    def _on_worker_finished(self):
        self.is_recording = False
        self.is_playing = False
        self._clear_highlight()
        self._update_buttons_state(False)
        self.state_label.config(text="待機中", foreground="gray")

    def save_file(self):
        if not self.current_file:
            self.save_file_as()
            return
        try:
            with open(self.current_file, "w", encoding="utf-8") as f:
                f.write(self.text_area.get("1.0", tk.END))
            self.file_label.config(text=f"現在のファイル: {self.current_file}")
            messagebox.showinfo("保存完了", f"保存しました:\n{self.current_file}")
        except OSError as e:
            messagebox.showerror("保存エラー", str(e))

    def save_file_as(self):
        path = filedialog.asksaveasfilename(
            title="マクロファイルを保存",
            defaultextension=".txt",
            filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
            initialfile=os.path.basename(self.current_file),
        )
        if path:
            self.current_file = path
            self.save_file()

    def load_file(self):
        path = filedialog.askopenfilename(
            title="マクロファイルを開く",
            filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.text_area.delete("1.0", tk.END)
                self.text_area.insert("1.0", content)
                self.current_file = path
                self.file_label.config(text=f"現在のファイル: {self.current_file}")
            except OSError as e:
                messagebox.showerror("読み込みエラー", str(e))

    def open_in_notepad(self):
        # まず現在の内容をファイルに保存してからメモ帳で開く
        if not os.path.exists(self.current_file):
            with open(self.current_file, "w", encoding="utf-8") as f:
                f.write(self.text_area.get("1.0", tk.END))
        else:
            if messagebox.askyesno(
                "メモ帳で開く",
                "現在のエディタ内容をファイルに上書き保存してからメモ帳で開きますか？\n"
                "（「いいえ」を選ぶと、ディスクに保存済みのファイルをそのまま開きます）",
            ):
                with open(self.current_file, "w", encoding="utf-8") as f:
                    f.write(self.text_area.get("1.0", tk.END))
        subprocess.Popen(["notepad.exe", self.current_file])

    def _on_toggle_keep_awake(self):
        self._keep_awake_flag = bool(self.keep_awake_var.get())
        set_system_keep_awake(self._keep_awake_flag)

    def _keepalive_worker(self):
        """起動中、定期的に XInput パッドへ微小信号を送って自動電源オフを防ぐ。"""
        interval_sec = 180  # 3分ごと（Xbox無線パッドの約15分スリープ対策）
        while not self._closing_event.wait(interval_sec):
            if not self._keep_awake_flag:
                continue
            if self.is_recording:
                continue
            try:
                send_xinput_keepalive(self._closing_event)
            except Exception:
                pass

    def _on_close(self):
        self._save_window_state()
        self._closing_event.set()
        self.stop_event.set()
        worker = self._active_worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=5.0)
        set_system_keep_awake(False)
        self._restore_physical_1p()
        self._disconnect_virtual_gamepad()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = GamepadMacroApp(root)
    root.mainloop()
