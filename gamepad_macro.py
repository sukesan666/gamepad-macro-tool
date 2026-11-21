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

from i18n import LANG_CODE_TO_LABEL, LANG_LABEL_TO_CODE, LANGUAGES, tr

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
STICK_DEADZONE = 0.15
STICK_CHANGE_THRESHOLD = 0.08

HOTKEY_VKS = {
    "NONE": 0,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
    "Pause": 0x13,
    "ScrollLock": 0x91,
}
HOTKEY_NAMES = list(HOTKEY_VKS.keys())

try:
    _GET_ASYNC_KEY_STATE = ctypes.windll.user32.GetAsyncKeyState
    _GET_ASYNC_KEY_STATE.argtypes = [ctypes.c_int]
    _GET_ASYNC_KEY_STATE.restype = ctypes.c_short
except Exception:
    _GET_ASYNC_KEY_STATE = None


def is_vk_pressed(vk_code):
    if not vk_code or _GET_ASYNC_KEY_STATE is None:
        return False
    try:
        return bool(_GET_ASYNC_KEY_STATE(vk_code) & 0x8000)
    except Exception:
        return False


def set_dark_titlebar(window, dark):
    """Windows 10/11 のウィンドウタイトルバーをダーク/ライトに切り替える（非対応環境では無視）。"""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / Win10 1903+)
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
            )
            if res == 0:
                break
    except Exception:
        pass


def format_duration(seconds):
    """秒数を読みやすい形式 (例: 28.50s / 4m 45s / 1h 02m 10s) に整形する。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return f"{seconds:.2f}s"
    total_sec = int(round(seconds))
    mins, secs = divmod(total_sec, 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins:02d}m {secs:02d}s"


def _norm_xinput_axis(raw_val):
    val = max(-1.0, min(1.0, raw_val / 32767.0))
    return 0.0 if abs(val) < STICK_DEADZONE else round(val, 2)


def _norm_dinput_axis(raw_val, invert=False):
    val = (raw_val - 32767.5) / 32767.5
    if invert:
        val = -val
    val = max(-1.0, min(1.0, val))
    return 0.0 if abs(val) < STICK_DEADZONE else round(val, 2)


MACRO_HEADER = """# ==========================================
# ゲームパッド マクロ記録ファイル
# ==========================================
# 書式 (メモ帳などで自由に編集・調整できます):
#   WAIT <秒数>            ... 指定した秒数だけ待機 (例: WAIT 0.500)
#   PRESS <ボタン名>       ... ボタンを押す (例: PRESS A)
#   RELEASE <ボタン名>     ... ボタンを離す (例: RELEASE A)
#   LSTICK <x> <y>         ... 左スティック (-1.00〜1.00, 例: LSTICK 0.00 1.00)
#   RSTICK <x> <y>         ... 右スティック (-1.00〜1.00, 例: RSTICK 0.50 0.00)
#
# 使用可能なボタン名:
#   A, B, X, Y, UP, DOWN, LEFT, RIGHT,
#   LB, RB, LT, RT, LS, RS, START, BACK
# ==========================================
"""


def get_xinput_state_full(controller_index=0):
    """XInputコントローラーのボタンと左右スティック(-1.0〜1.0)を取得する。未接続時はNone。"""
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
    ls = (_norm_xinput_axis(gp.sThumbLX), _norm_xinput_axis(gp.sThumbLY))
    rs = (_norm_xinput_axis(gp.sThumbRX), _norm_xinput_axis(gp.sThumbRY))
    return pressed, ls, rs


def get_xinput_state(controller_index=0):
    """指定インデックスのXInputコントローラー状態を取得する。未接続時はNone。"""
    full = get_xinput_state_full(controller_index)
    return full[0] if full is not None else None


def get_directinput_state_full(joy_index=0):
    """winmm.dll (DirectInput/HID互換) でボタンと左右スティックを取得する。未接続時はNone。"""
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

    ls = (_norm_dinput_axis(info.dwXpos, False), _norm_dinput_axis(info.dwYpos, True))
    rs = (_norm_dinput_axis(info.dwZpos, False), _norm_dinput_axis(info.dwRpos, True))
    return pressed, ls, rs


def get_directinput_state(joy_index=0):
    """winmm.dll (DirectInput/HID互換) でコントローラー状態を取得する。未接続時はNone。"""
    full = get_directinput_state_full(joy_index)
    return full[0] if full is not None else None


def get_controller_state_full(controller_index=0, mode="AUTO"):
    """
    指定モードでコントローラー状態（ボタン + 左右スティック）を取得する。
    戻り値: (pressed_set, ls, rs, detected_type) または (None, None, None, None)
    """
    if mode == "XInput":
        f = get_xinput_state_full(controller_index)
        return (f[0], f[1], f[2], "XInput") if f is not None else (None, None, None, None)
    if mode == "DirectInput":
        f = get_directinput_state_full(controller_index)
        return (f[0], f[1], f[2], "DirectInput") if f is not None else (None, None, None, None)

    f = get_xinput_state_full(controller_index)
    if f is not None:
        return f[0], f[1], f[2], "XInput"
    f = get_directinput_state_full(controller_index)
    if f is not None:
        return f[0], f[1], f[2], "DirectInput"
    return None, None, None, None


def get_controller_state(controller_index=0, mode="AUTO"):
    """
    指定モードでコントローラー状態を取得する。
    戻り値: (pressed_set, detected_type) または (None, None)
    """
    pressed, _, _, detected = get_controller_state_full(controller_index, mode)
    return (pressed, detected) if pressed is not None else (None, None)


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
                raise ValueError(f"Line {line_num}: Invalid WAIT format -> {raw_line}")
            try:
                sec = float(parts[1])
                if sec < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Line {line_num}: Invalid WAIT seconds -> {parts[1]}")
            commands.append((line_num, "WAIT", sec))
        elif cmd in ("PRESS", "RELEASE"):
            if len(parts) != 2:
                raise ValueError(f"Line {line_num}: Invalid {cmd} format -> {raw_line}")
            btn = parts[1].upper()
            if btn not in valid_buttons:
                raise ValueError(f"Line {line_num}: Unknown button -> {btn}")
            commands.append((line_num, cmd, btn))
        elif cmd in ("LSTICK", "RSTICK"):
            if len(parts) != 3:
                raise ValueError(f"Line {line_num}: Invalid {cmd} format (expected X Y) -> {raw_line}")
            try:
                x = float(parts[1])
                y = float(parts[2])
                if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
                    raise ValueError
            except ValueError:
                raise ValueError(f"Line {line_num}: {cmd} values must be between -1.0 and 1.0 -> {raw_line}")
            commands.append((line_num, cmd, (x, y)))
        else:
            raise ValueError(f"Line {line_num}: Unknown command -> {cmd}")
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
        self.lang = "ja"
        self.dark_mode_var = tk.BooleanVar(value=False)

        self.root.geometry("760x690")
        self.root.minsize(680, 580)

        self.is_recording = False
        self.is_playing = False
        self.stop_event = threading.Event()
        self._closing_event = threading.Event()
        self.current_file = os.path.join(os.getcwd(), "macro_record.txt")
        self._last_normal_geometry = "760x690"
        self._saved_content = MACRO_HEADER + "\n"

        self.gamepad_out = None
        self.vgamepad_error = None
        self.disabled_device_ids = []
        self._device_lock = threading.Lock()
        self._vgamepad_lock = threading.RLock()
        self._ui_queue = queue.Queue()
        self._active_worker = None
        self._keep_awake_flag = True

        # バックグラウンドのホットキー監視用スレッドセーフ設定
        self._hk_lock = threading.Lock()
        self._hk_enabled = True
        self._hk_play_vk = HOTKEY_VKS["F9"]
        self._hk_rec_vk = HOTKEY_VKS["F10"]

        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self._build_ui()
        self._load_window_state()
        self._apply_language()
        self._apply_theme()
        self._sync_hotkey_config()
        self._update_duration_label()
        self._update_window_title()

        self.root.bind("<Configure>", self._on_window_configure)
        self._init_vgamepad()
        self._drain_ui_queue()
        self._poll_controller_status()
        self._on_toggle_keep_awake()
        threading.Thread(target=self._keepalive_worker, daemon=True).start()
        threading.Thread(target=self._hotkey_worker, daemon=True).start()
        threading.Thread(target=self._check_disabled_pads_on_startup, daemon=True).start()

        # 異常終了時も含め、無効化した物理パッドを必ず元に戻す
        atexit.register(self._restore_physical_1p)
        atexit.register(lambda: set_system_keep_awake(False))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _t(self, key, **kwargs):
        return tr(self.lang, key, **kwargs)

    def _is_dirty(self):
        try:
            return self.text_area.get("1.0", "end-1c") != self._saved_content.rstrip("\n")
        except Exception:
            return False

    def _mark_saved(self):
        self._saved_content = self.text_area.get("1.0", "end-1c")
        self._update_window_title()

    def _update_window_title(self):
        tag = self._t("admin_tag") if self.is_admin_user else self._t("user_tag")
        dirty = " *" if self._is_dirty() else ""
        self.root.title(self._t("app_title") + tag + dirty)

    def _apply_theme(self):
        dark = bool(self.dark_mode_var.get())
        if dark:
            bg = "#1e1e1e"
            panel = "#252526"
            field = "#2d2d30"
            fg = "#e6e6e6"
            muted = "#aaaaaa"
            border = "#3e3e42"
            btn_bg = "#333337"
            btn_active = "#44444a"
            sel_bg = "#264f78"
            hl_bg = "#665200"
            hl_fg = "#ffffff"
        else:
            bg = "#f0f0f0"
            panel = "#f0f0f0"
            field = "#ffffff"
            fg = "#111111"
            muted = "#555555"
            border = "#c8c8c8"
            btn_bg = "#e4e4e4"
            btn_active = "#d4d4d4"
            sel_bg = "#cce8ff"
            hl_bg = "#ffe066"
            hl_fg = "#000000"

        self.root.configure(bg=bg)
        self.style.configure(".", background=panel, foreground=fg, bordercolor=border)
        self.style.configure("TFrame", background=panel)
        self.style.configure("TLabel", background=panel, foreground=fg)
        self.style.configure("Muted.TLabel", background=panel, foreground=muted)
        self.style.configure("TLabelframe", background=panel, foreground=fg, bordercolor=border)
        self.style.configure("TLabelframe.Label", background=panel, foreground=fg)
        self.style.configure("TCheckbutton", background=panel, foreground=fg)
        self.style.map("TCheckbutton", background=[("active", panel)], foreground=[("disabled", muted)])
        self.style.configure("TButton", background=btn_bg, foreground=fg, bordercolor=border)
        self.style.map(
            "TButton",
            background=[("active", btn_active), ("disabled", panel)],
            foreground=[("disabled", muted)],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=field,
            background=btn_bg,
            foreground=fg,
            arrowcolor=fg,
            bordercolor=border,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", field), ("disabled", panel)],
            foreground=[("readonly", fg), ("disabled", muted)],
        )
        self.style.configure(
            "TSpinbox",
            fieldbackground=field,
            background=btn_bg,
            foreground=fg,
            arrowcolor=fg,
            bordercolor=border,
        )
        self.style.map(
            "TSpinbox",
            fieldbackground=[("disabled", panel)],
            foreground=[("disabled", muted)],
        )
        self.style.configure("Vertical.TScrollbar", background=btn_bg, troughcolor=bg, arrowcolor=fg)

        self.text_area.configure(
            bg=field,
            fg=fg,
            insertbackground=fg,
            selectbackground=sel_bg,
            selectforeground=fg,
        )
        self.text_area.tag_configure(
            "current_line",
            background=hl_bg,
            foreground=hl_fg,
            font=("Consolas", 10, "bold"),
        )
        if not self.is_playing and not self.is_recording:
            self.state_label.config(foreground=muted)
        set_dark_titlebar(self.root, dark)

    def _on_language_selected(self, _event=None):
        label = self.lang_combo_var.get()
        code = LANG_LABEL_TO_CODE.get(label, "ja")
        if code != self.lang:
            self.lang = code
            self._apply_language()

    def _apply_language(self):
        self._update_window_title()
        self.lbl_lang.config(text=self._t("lang_label"))
        self.chk_dark.config(text=self._t("dark_mode"))
        self.status_frame.config(text=self._t("status_frame"))
        self.lbl_port.config(text=self._t("port_label"))
        self.lbl_mode.config(text=self._t("mode_label"))
        self.chk_keep_awake.config(text=self._t("keep_awake"))

        self.ctrl_frame.config(text=self._t("ctrl_frame"))
        self.btn_record.config(text=self._t("btn_record"))
        self.btn_stop.config(text=self._t("btn_stop"))
        self.btn_play.config(text=self._t("btn_play"))
        self.chk_loop.config(text=self._t("loop_play"))
        self.lbl_loop_count.config(text=self._t("loop_count_label"))
        self.lbl_countdown.config(text=self._t("countdown_label"))
        self.chk_wait_first.config(text=self._t("wait_first"))
        self.chk_record_sticks.config(text=self._t("record_sticks"))
        self.chk_auto_swap.config(text=self._t("auto_swap"))
        self.btn_restore_pad.config(text=self._t("btn_restore_pad"))
        self.chk_jitter.config(text=self._t("jitter_enable"))
        self.lbl_jitter_ms.config(text=self._t("jitter_ms_label"))
        self.chk_hotkey.config(text=self._t("hotkey_enable"))
        self.lbl_hk_play.config(text=self._t("hk_play_label"))
        self.lbl_hk_rec.config(text=self._t("hk_rec_label"))

        self.file_frame.config(text=self._t("file_frame"))
        self.btn_open.config(text=self._t("btn_open"))
        self.btn_save.config(text=self._t("btn_save"))
        self.btn_save_as.config(text=self._t("btn_save_as"))
        self.btn_notepad.config(text=self._t("btn_notepad"))
        self.file_label.config(text=self._t("current_file", path=self.current_file))

        self.editor_frame.config(text=self._t("editor_frame"))
        self.chk_auto_scroll.config(text=self._t("auto_scroll"))
        self.lbl_bulk_wait.config(text=self._t("bulk_wait_label"))

        if not self.is_playing and not self.is_recording:
            self.state_label.config(text=self._t("idle"))
        self._update_duration_label()

    def _sync_hotkey_config(self, _event=None):
        with self._hk_lock:
            self._hk_enabled = bool(self.hotkey_var.get())
            self._hk_play_vk = HOTKEY_VKS.get(self.hk_play_var.get(), 0)
            self._hk_rec_vk = HOTKEY_VKS.get(self.hk_rec_var.get(), 0)
        state = "readonly" if self.hotkey_var.get() else "disabled"
        if not (self.is_playing or self.is_recording):
            self.hk_play_combo.config(state=state)
            self.hk_rec_combo.config(state=state)

    def _hotkey_worker(self):
        """バックグラウンドでグローバルホットキーの押下エッジを監視する。"""
        prev_play = False
        prev_rec = False
        while not self._closing_event.wait(0.03):
            with self._hk_lock:
                enabled = self._hk_enabled
                play_vk = self._hk_play_vk
                rec_vk = self._hk_rec_vk
            if not enabled:
                prev_play = False
                prev_rec = False
                continue
            cur_play = is_vk_pressed(play_vk)
            cur_rec = is_vk_pressed(rec_vk)
            if cur_play and not prev_play:
                self._post_ui(self._on_hotkey_play)
            elif cur_rec and not prev_rec:
                self._post_ui(self._on_hotkey_record)
            prev_play = cur_play
            prev_rec = cur_rec

    def _on_hotkey_play(self):
        if self.is_playing:
            self.stop_all()
        elif not self.is_recording:
            self.start_playing()

    def _on_hotkey_record(self):
        if self.is_recording:
            self.stop_all()
        elif not self.is_playing:
            self.start_recording()

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
                    self._t("startup_disabled_title"),
                    self._t("startup_disabled_admin"),
                ):
                    self.manual_restore_physical_pad()
            else:
                messagebox.showwarning(
                    self._t("startup_disabled_title"),
                    self._t("startup_disabled_user"),
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

        lang = data.get("lang")
        if lang in LANG_CODE_TO_LABEL:
            self.lang = lang
            self.lang_combo_var.set(LANG_CODE_TO_LABEL[lang])

        if isinstance(data.get("dark_mode"), bool):
            self.dark_mode_var.set(data["dark_mode"])

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
        if isinstance(data.get("countdown"), str):
            try:
                if float(data["countdown"]) >= 0:
                    self.countdown_var.set(data["countdown"])
            except ValueError:
                pass
        if isinstance(data.get("jitter_ms"), str) and data["jitter_ms"].isdigit():
            self.jitter_ms_var.set(data["jitter_ms"])
        if isinstance(data.get("jitter"), bool):
            self.jitter_var.set(data["jitter"])
            self._on_toggle_jitter()
        if isinstance(data.get("wait_first"), bool):
            self.wait_first_var.set(data["wait_first"])
        if isinstance(data.get("record_sticks"), bool):
            self.record_sticks_var.set(data["record_sticks"])
        if isinstance(data.get("auto_swap"), bool):
            self.auto_swap_var.set(data["auto_swap"])
        if isinstance(data.get("auto_scroll"), bool):
            self.auto_scroll_var.set(data["auto_scroll"])
        if isinstance(data.get("hotkey_enabled"), bool):
            self.hotkey_var.set(data["hotkey_enabled"])
        if data.get("hk_play") in HOTKEY_VKS:
            self.hk_play_var.set(data["hk_play"])
        if data.get("hk_rec") in HOTKEY_VKS:
            self.hk_rec_var.set(data["hk_rec"])

        saved_file = data.get("current_file")
        if isinstance(saved_file, str) and saved_file:
            self.current_file = saved_file
            self.file_label.config(text=self._t("current_file", path=self.current_file))
            if os.path.isfile(saved_file):
                try:
                    with open(saved_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    self.text_area.delete("1.0", tk.END)
                    self.text_area.insert("1.0", content)
                    self._saved_content = self.text_area.get("1.0", "end-1c")
                except OSError:
                    pass

    def _save_window_state(self):
        """現在のウィンドウ位置・サイズ・各種設定を JSON に保存する。"""
        try:
            win_state = self.root.state()
            if win_state == "normal":
                self._last_normal_geometry = self.root.geometry()
            data = {
                "lang": self.lang,
                "dark_mode": bool(self.dark_mode_var.get()),
                "geometry": self._last_normal_geometry,
                "window_state": "zoomed" if win_state == "zoomed" else "normal",
                "port": int(self.port_var.get()),
                "mode": self.mode_var.get(),
                "keep_awake": bool(self.keep_awake_var.get()),
                "loop": bool(self.loop_var.get()),
                "loop_count": self.loop_count_var.get().strip(),
                "countdown": self.countdown_var.get().strip(),
                "jitter": bool(self.jitter_var.get()),
                "jitter_ms": self.jitter_ms_var.get().strip(),
                "wait_first": bool(self.wait_first_var.get()),
                "record_sticks": bool(self.record_sticks_var.get()),
                "auto_swap": bool(self.auto_swap_var.get()),
                "auto_scroll": bool(self.auto_scroll_var.get()),
                "hotkey_enabled": bool(self.hotkey_var.get()),
                "hk_play": self.hk_play_var.get(),
                "hk_rec": self.hk_rec_var.get(),
                "current_file": self.current_file,
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _init_vgamepad(self):
        if not HAS_VGAMEPAD:
            self.vgamepad_error = self._t("err_no_vgamepad")
            return
        # 自動入替がOFFのときだけ常時接続にする（ONのときは再生時だけ仮想パッドを接続）
        if not self.auto_swap_var.get():
            self._connect_virtual_gamepad()

    def _connect_virtual_gamepad(self):
        if not HAS_VGAMEPAD:
            self.vgamepad_error = self._t("err_no_vgamepad")
            return False
        with self._vgamepad_lock:
            if self.gamepad_out is not None:
                return True
            try:
                self.gamepad_out = vg.VX360Gamepad()
                self.vgamepad_error = None
                return True
            except Exception as e:
                self.vgamepad_error = self._t("err_vgamepad_init", err=e)
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
        pad = {"padx": 10, "pady": 4}

        # ヘッダーバー（言語・ダークモード切替）
        header_bar = ttk.Frame(self.root)
        header_bar.pack(fill="x", padx=10, pady=(6, 0))

        self.lbl_lang = ttk.Label(header_bar, text="言語:")
        self.lbl_lang.pack(side="left")
        self.lang_combo_var = tk.StringVar(value=LANG_CODE_TO_LABEL[self.lang])
        self.lang_combo = ttk.Combobox(
            header_bar,
            textvariable=self.lang_combo_var,
            values=[label for _, label in LANGUAGES],
            width=12,
            state="readonly",
        )
        self.lang_combo.pack(side="left", padx=(4, 12))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_selected)

        self.chk_dark = ttk.Checkbutton(
            header_bar,
            text="🌙 ダークモード",
            variable=self.dark_mode_var,
            command=self._apply_theme,
        )
        self.chk_dark.pack(side="right")

        # ステータスバー（上部）
        self.status_frame = ttk.LabelFrame(self.root, text="コントローラー状態", padding=6)
        self.status_frame.pack(fill="x", **pad)

        top_row = ttk.Frame(self.status_frame)
        top_row.pack(fill="x")

        self.lbl_port = ttk.Label(top_row, text="入力ポート:")
        self.lbl_port.pack(side="left")
        self.port_var = tk.IntVar(value=0)
        port_combo = ttk.Combobox(
            top_row, textvariable=self.port_var, values=list(range(16)), width=4, state="readonly"
        )
        port_combo.pack(side="left", padx=(4, 10))

        self.lbl_mode = ttk.Label(top_row, text="方式:")
        self.lbl_mode.pack(side="left")
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

        self.state_label = ttk.Label(top_row, text="待機中", style="Muted.TLabel")
        self.state_label.pack(side="right")

        keep_row = ttk.Frame(self.status_frame)
        keep_row.pack(fill="x", pady=(4, 0))

        self.keep_awake_var = tk.BooleanVar(value=True)
        self.chk_keep_awake = ttk.Checkbutton(
            keep_row,
            text="起動中は物理パッドの自動電源オフを防止（3分ごとに微小信号を送信）",
            variable=self.keep_awake_var,
            command=self._on_toggle_keep_awake,
        )
        self.chk_keep_awake.pack(side="left")

        # 操作ボタンエリア
        self.ctrl_frame = ttk.LabelFrame(self.root, text="記録・再生コントロール", padding=6)
        self.ctrl_frame.pack(fill="x", **pad)

        btn_row1 = ttk.Frame(self.ctrl_frame)
        btn_row1.pack(fill="x", pady=(0, 6))

        self.btn_record = tk.Button(
            btn_row1,
            text="● 記録開始 (Record)",
            bg="#d9534f",
            fg="white",
            font=("Meiryo", 9, "bold"),
            width=16,
            command=self.start_recording,
        )
        self.btn_record.pack(side="left", padx=(0, 6))

        self.btn_stop = tk.Button(
            btn_row1,
            text="■ 停止 (Stop)",
            bg="#5a6268",
            fg="white",
            font=("Meiryo", 9, "bold"),
            width=11,
            state="disabled",
            command=self.stop_all,
        )
        self.btn_stop.pack(side="left", padx=(0, 8))

        self.btn_play = tk.Button(
            btn_row1,
            text="▶ 再生 (Play)",
            bg="#28a745",
            fg="white",
            font=("Meiryo", 9, "bold"),
            width=14,
            command=self.start_playing,
        )
        self.btn_play.pack(side="left", padx=(0, 8))

        self.loop_var = tk.BooleanVar(value=False)
        self.chk_loop = ttk.Checkbutton(
            btn_row1,
            text="ループ再生",
            variable=self.loop_var,
            command=self._on_toggle_loop,
        )
        self.chk_loop.pack(side="left", padx=(2, 4))

        self.lbl_loop_count = ttk.Label(btn_row1, text="回数(0=無限):")
        self.lbl_loop_count.pack(side="left")
        self.loop_count_var = tk.StringVar(value="0")
        self.loop_count_var.trace_add("write", lambda *_: self._update_duration_label())
        self.loop_count_spin = ttk.Spinbox(
            btn_row1,
            from_=0,
            to=99999,
            textvariable=self.loop_count_var,
            width=5,
            state="disabled",
        )
        self.loop_count_spin.pack(side="left", padx=(3, 8))

        self.lbl_countdown = ttk.Label(btn_row1, text="開始待機(秒):")
        self.lbl_countdown.pack(side="left")
        self.countdown_var = tk.StringVar(value="0")
        self.countdown_spin = ttk.Spinbox(
            btn_row1,
            from_=0,
            to=30,
            textvariable=self.countdown_var,
            width=4,
        )
        self.countdown_spin.pack(side="left", padx=(3, 0))

        # オプション行2: 記録設定
        btn_row2 = ttk.Frame(self.ctrl_frame)
        btn_row2.pack(fill="x")

        self.wait_first_var = tk.BooleanVar(value=True)
        self.chk_wait_first = ttk.Checkbutton(
            btn_row2,
            text="最初のボタンが押された瞬間から記録する（冒頭の無操作時間をカット）",
            variable=self.wait_first_var,
        )
        self.chk_wait_first.pack(side="left")

        self.record_sticks_var = tk.BooleanVar(value=False)
        self.chk_record_sticks = ttk.Checkbutton(
            btn_row2,
            text="アナログスティックも記録する (LSTICK / RSTICK)",
            variable=self.record_sticks_var,
        )
        self.chk_record_sticks.pack(side="right")

        # オプション行3: 1P自動入替
        btn_row3 = ttk.Frame(self.ctrl_frame)
        btn_row3.pack(fill="x", pady=(4, 0))

        self.auto_swap_var = tk.BooleanVar(value=False)
        self.chk_auto_swap = ttk.Checkbutton(
            btn_row3,
            text="再生時だけ物理パッドと1Pを自動入替（停止で元に戻す・要管理者権限）",
            variable=self.auto_swap_var,
            command=self._on_toggle_auto_swap,
        )
        self.chk_auto_swap.pack(side="left")

        self.btn_restore_pad = ttk.Button(
            btn_row3,
            text="🔄 物理パッドを戻す",
            command=self.manual_restore_physical_pad,
        )
        self.btn_restore_pad.pack(side="right")

        # オプション行4: ランダム揺らぎ + グローバルホットキー
        btn_row4 = ttk.Frame(self.ctrl_frame)
        btn_row4.pack(fill="x", pady=(4, 0))

        self.jitter_var = tk.BooleanVar(value=False)
        self.chk_jitter = ttk.Checkbutton(
            btn_row4,
            text="ループ時にタイミングを少しずらす（ランダム揺らぎ）",
            variable=self.jitter_var,
            command=self._on_toggle_jitter,
        )
        self.chk_jitter.pack(side="left")

        self.lbl_jitter_ms = ttk.Label(btn_row4, text="追加幅(+ms):")
        self.lbl_jitter_ms.pack(side="left", padx=(8, 0))
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

        btn_row5 = ttk.Frame(self.ctrl_frame)
        btn_row5.pack(fill="x", pady=(4, 0))

        self.hotkey_var = tk.BooleanVar(value=True)
        self.chk_hotkey = ttk.Checkbutton(
            btn_row5,
            text="グローバルホットキー",
            variable=self.hotkey_var,
            command=self._sync_hotkey_config,
        )
        self.chk_hotkey.pack(side="left")

        self.lbl_hk_play = ttk.Label(btn_row5, text="再生/停止:")
        self.lbl_hk_play.pack(side="left", padx=(10, 0))
        self.hk_play_var = tk.StringVar(value="F9")
        self.hk_play_combo = ttk.Combobox(
            btn_row5,
            textvariable=self.hk_play_var,
            values=HOTKEY_NAMES,
            width=10,
            state="readonly",
        )
        self.hk_play_combo.pack(side="left", padx=(4, 10))
        self.hk_play_combo.bind("<<ComboboxSelected>>", self._sync_hotkey_config)

        self.lbl_hk_rec = ttk.Label(btn_row5, text="記録/停止:")
        self.lbl_hk_rec.pack(side="left")
        self.hk_rec_var = tk.StringVar(value="F10")
        self.hk_rec_combo = ttk.Combobox(
            btn_row5,
            textvariable=self.hk_rec_var,
            values=HOTKEY_NAMES,
            width=10,
            state="readonly",
        )
        self.hk_rec_combo.pack(side="left", padx=(4, 0))
        self.hk_rec_combo.bind("<<ComboboxSelected>>", self._sync_hotkey_config)

        # ファイル操作エリア
        self.file_frame = ttk.LabelFrame(self.root, text="マクロファイル操作（メモ帳などで編集可能）", padding=6)
        self.file_frame.pack(fill="x", **pad)

        file_row = ttk.Frame(self.file_frame)
        file_row.pack(fill="x")

        self.btn_open = ttk.Button(file_row, text="📂 ファイルを開く", command=self.load_file)
        self.btn_open.pack(side="left", padx=(0, 6))
        self.btn_save = ttk.Button(file_row, text="💾 保存する", command=self.save_file)
        self.btn_save.pack(side="left", padx=(0, 6))
        self.btn_save_as = ttk.Button(file_row, text="💾 名前を付けて保存", command=self.save_file_as)
        self.btn_save_as.pack(side="left", padx=(0, 6))
        self.btn_notepad = ttk.Button(file_row, text="📝 メモ帳で開く", command=self.open_in_notepad)
        self.btn_notepad.pack(side="left", padx=(12, 0))

        self.file_label = ttk.Label(self.file_frame, text=f"現在のファイル: {self.current_file}", style="Muted.TLabel")
        self.file_label.pack(anchor="w", pady=(4, 0))

        # テキストエディタエリア
        self.editor_frame = ttk.LabelFrame(self.root, text="記録内容プレビュー / 直接編集", padding=6)
        self.editor_frame.pack(fill="both", expand=True, **pad)

        editor_top = ttk.Frame(self.editor_frame)
        editor_top.pack(fill="x", pady=(0, 4))

        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.chk_auto_scroll = ttk.Checkbutton(
            editor_top,
            text="再生・記録中の自動スクロール",
            variable=self.auto_scroll_var,
        )
        self.chk_auto_scroll.pack(side="left")

        self.duration_label = ttk.Label(editor_top, text="", style="Muted.TLabel")
        self.duration_label.pack(side="left", padx=(12, 0))

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

        self.lbl_bulk_wait = ttk.Label(editor_top, text="全WAIT一括調整:")
        self.lbl_bulk_wait.pack(side="right")

        text_container = ttk.Frame(self.editor_frame)
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
        self._saved_content = self.text_area.get("1.0", "end-1c")
        self.text_area.bind("<KeyRelease>", self._on_editor_changed)

    def _on_editor_changed(self, _event=None):
        self._update_duration_label()
        self._update_window_title()

    def _update_duration_label(self):
        """エディタ内の WAIT 合計から1周および全体の所要時間を計算して表示する。"""
        if not hasattr(self, "text_area") or not hasattr(self, "duration_label"):
            return
        raw_text = self.text_area.get("1.0", "end-1c")
        total_wait = 0.0
        pattern = re.compile(r"^\s*WAIT\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)
        for line in raw_text.splitlines():
            clean = line.split("#", 1)[0]
            m = pattern.match(clean)
            if m:
                try:
                    total_wait += float(m.group(1))
                except ValueError:
                    pass

        sec_str = format_duration(total_wait)
        if self.loop_var.get():
            raw_cnt = self.loop_count_var.get().strip()
            try:
                cnt = int(raw_cnt)
            except ValueError:
                cnt = 0
            if cnt > 0:
                text = self._t("duration_loop", sec=sec_str, count=cnt, total=format_duration(total_wait * cnt))
            else:
                text = self._t("duration_inf", sec=sec_str)
        else:
            text = self._t("duration_single", sec=sec_str)
        self.duration_label.config(text=text)

    def _confirm_save_if_dirty(self):
        """未保存の変更がある場合に確認ダイアログを出す。続行可能なら True、キャンセルなら False。"""
        if not self._is_dirty():
            return True
        ans = messagebox.askyesnocancel(self._t("unsaved_title"), self._t("unsaved_msg"))
        if ans is None:
            return False
        if ans is True:
            return self.save_file()
        return True

    def _poll_controller_status(self):
        if self._closing_event.is_set():
            return
        idx = self.port_var.get()
        mode = self.mode_var.get()
        pressed, detected = get_controller_state(idx, mode)
        if pressed is None:
            self.conn_label.config(text=self._t("disconnected", idx=idx), foreground="#d9534f")
        else:
            btns = ", ".join(sorted(pressed)) if pressed else self._t("none_input")
            self.conn_label.config(
                text=self._t("connected", idx=idx, detected=detected, btns=btns),
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
            self.countdown_spin.config(state="disabled")
            self.jitter_ms_spin.config(state="disabled")
            self.btn_wait_minus.config(state="disabled")
            self.btn_wait_plus.config(state="disabled")
            self.hk_play_combo.config(state="disabled")
            self.hk_rec_combo.config(state="disabled")
        else:
            self.btn_record.config(state="normal")
            self.btn_play.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.countdown_spin.config(state="normal")
            self._on_toggle_loop()
            self._on_toggle_jitter()
            self._sync_hotkey_config()
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
        self._update_duration_label()
        self._update_window_title()

    def start_recording(self):
        if self.is_recording or self.is_playing:
            return
        if not self._confirm_save_if_dirty():
            return
        idx = self.port_var.get()
        mode = self.mode_var.get()
        pressed, _ = get_controller_state(idx, mode)
        if pressed is None:
            messagebox.showwarning(
                self._t("warn_disconnected_title"),
                self._t("warn_disconnected_msg", idx=idx, mode=mode),
            )
            return

        self.is_recording = True
        self.stop_event.clear()
        self._update_buttons_state(True)
        self.state_label.config(text=self._t("recording_status"), foreground="#d9534f")

        self.text_area.delete("1.0", tk.END)
        self.text_area.insert(tk.END, MACRO_HEADER + "\n")
        self._update_duration_label()
        self._update_window_title()

        wait_first = bool(self.wait_first_var.get())
        record_sticks = bool(self.record_sticks_var.get())
        t = threading.Thread(
            target=self._record_worker,
            args=(idx, mode, wait_first, record_sticks),
            daemon=True,
        )
        self._active_worker = t
        t.start()

    def _record_worker(self, idx, mode, wait_first=True, record_sticks=False):
        prev_pressed, prev_ls, prev_rs, _ = get_controller_state_full(idx, mode)
        if prev_pressed is None:
            prev_pressed = set()
            prev_ls = (0.0, 0.0)
            prev_rs = (0.0, 0.0)
        last_event_time = None
        started = not wait_first
        if started:
            last_event_time = time.perf_counter()

        def _stick_changed(prev_s, curr_s):
            if prev_s == curr_s:
                return False
            if curr_s == (0.0, 0.0) and prev_s != (0.0, 0.0):
                return True
            return (
                abs(curr_s[0] - prev_s[0]) >= STICK_CHANGE_THRESHOLD
                or abs(curr_s[1] - prev_s[1]) >= STICK_CHANGE_THRESHOLD
            )

        while not self.stop_event.is_set():
            curr_pressed, curr_ls, curr_rs, _ = get_controller_state_full(idx, mode)
            if curr_pressed is None:
                time.sleep(0.01)
                continue

            now = time.perf_counter()
            newly_pressed = sorted(curr_pressed - prev_pressed)
            newly_released = sorted(prev_pressed - curr_pressed)
            ls_moved = record_sticks and _stick_changed(prev_ls, curr_ls)
            rs_moved = record_sticks and _stick_changed(prev_rs, curr_rs)

            if newly_pressed or newly_released or ls_moved or rs_moved:
                if not started:
                    started = True
                    last_event_time = now
                else:
                    elapsed = now - last_event_time
                    if elapsed >= 0.001:
                        self._post_ui(self._append_text, f"WAIT {elapsed:.3f}\n")
                    last_event_time = now

                for btn in newly_pressed:
                    self._post_ui(self._append_text, f"PRESS {btn}\n")

                for btn in newly_released:
                    self._post_ui(self._append_text, f"RELEASE {btn}\n")

                if ls_moved:
                    self._post_ui(self._append_text, f"LSTICK {curr_ls[0]:.2f} {curr_ls[1]:.2f}\n")
                    prev_ls = curr_ls

                if rs_moved:
                    self._post_ui(self._append_text, f"RSTICK {curr_rs[0]:.2f} {curr_rs[1]:.2f}\n")
                    prev_rs = curr_rs

            prev_pressed = curr_pressed
            time.sleep(0.004)  # 約250Hzポーリング

        # 記録終了時に押しっぱなしのボタンや傾いたスティックがあれば自動で戻す
        need_cleanup = started and (prev_pressed or (record_sticks and (prev_ls != (0.0, 0.0) or prev_rs != (0.0, 0.0))))
        if need_cleanup:
            elapsed = time.perf_counter() - last_event_time
            if elapsed >= 0.001:
                self._post_ui(self._append_text, f"WAIT {elapsed:.3f}\n")
            for btn in sorted(prev_pressed):
                self._post_ui(self._append_text, f"RELEASE {btn}\n")
            if record_sticks and prev_ls != (0.0, 0.0):
                self._post_ui(self._append_text, "LSTICK 0.00 0.00\n")
            if record_sticks and prev_rs != (0.0, 0.0):
                self._post_ui(self._append_text, "RSTICK 0.00 0.00\n")

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
        self._update_duration_label()

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
                self._t("warn_admin_title"),
                self._t("warn_admin_restore"),
            )
            return

        auto_swap = bool(self.auto_swap_var.get())

        def _worker():
            self._set_status_async(self._t("restoring_pad"), "#007bff")
            self._disconnect_virtual_gamepad()
            self._wait_xinput_slot0(want_connected=False, timeout=2.0, abort_on_stop=False)
            time.sleep(0.3)
            with self._device_lock:
                disabled_snapshot = list(self.disabled_device_ids)
                restored = self._enable_and_wait_physical_1p(disabled_snapshot)
                self.disabled_device_ids = []
            if not auto_swap:
                self._connect_virtual_gamepad()
            self._set_status_async(self._t("idle"), "#aaaaaa" if self.dark_mode_var.get() else "#555555")
            self._post_ui(
                messagebox.showinfo,
                self._t("restore_done_title"),
                self._t("restore_done_msg", count=restored) if restored > 0 else self._t("restore_none_msg"),
            )

        t = threading.Thread(target=_worker, daemon=True)
        self._active_worker = t
        t.start()

    def start_playing(self):
        if self.is_recording or self.is_playing:
            return
        if not HAS_VGAMEPAD:
            messagebox.showerror(self._t("err_play_title"), self._t("err_no_vgamepad"))
            return

        auto_swap = self.auto_swap_var.get()
        if auto_swap and not self.is_admin_user:
            messagebox.showwarning(self._t("warn_admin_title"), self._t("warn_admin_swap"))
            return

        if not auto_swap and not self._connect_virtual_gamepad():
            messagebox.showerror(
                self._t("err_play_title"),
                self.vgamepad_error or self._t("err_virtual_connect"),
            )
            return

        raw_text = self.text_area.get("1.0", tk.END)
        try:
            commands = parse_macro_text(raw_text)
        except ValueError as e:
            messagebox.showerror(self._t("err_syntax_title"), str(e))
            return

        if not commands:
            messagebox.showinfo(self._t("info_title"), self._t("info_no_cmd"))
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
                messagebox.showerror(self._t("err_input_title"), self._t("err_loop_count"))
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
                messagebox.showerror(self._t("err_input_title"), self._t("err_jitter"))
                return

        raw_cd = self.countdown_var.get().strip() or "0"
        try:
            countdown_sec = float(raw_cd)
            if countdown_sec < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(self._t("err_input_title"), self._t("err_countdown"))
            return

        self.is_playing = True
        self.stop_event.clear()
        self._update_buttons_state(True)
        if countdown_sec > 0:
            self.state_label.config(
                text=self._t("countdown_status", sec=int(countdown_sec) if countdown_sec.is_integer() else f"{countdown_sec:.1f}"),
                foreground="#e0a800",
            )
        else:
            self.state_label.config(
                text=self._t("switching_virtual") if auto_swap else self._t("playing_status"),
                foreground="#007bff" if auto_swap else "#28a745",
            )

        t = threading.Thread(
            target=self._play_worker,
            args=(commands, loop, loop_limit, auto_swap, jitter_sec, countdown_sec),
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
            self.gamepad_out.left_joystick_float(x_value_float=0.0, y_value_float=0.0)
            self.gamepad_out.right_joystick_float(x_value_float=0.0, y_value_float=0.0)
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
                self.text_area.yview_moveto(0.0)
                self.text_area.see(start_idx)
            else:
                self.text_area.see(f"{line_num + 3}.0")
                self.text_area.see(start_idx)

        if is_loop:
            if loop_limit > 0:
                self.state_label.config(
                    text=self._t("playing_loop_lim", loop=loop_count, limit=loop_limit, line=line_num),
                    foreground="#28a745",
                )
            else:
                self.state_label.config(
                    text=self._t("playing_loop_inf", loop=loop_count, line=line_num),
                    foreground="#28a745",
                )
        else:
            self.state_label.config(
                text=self._t("playing_line", line=line_num),
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

    def _play_worker(self, commands, loop, loop_limit=0, auto_swap=False, jitter_sec=0.0, countdown_sec=0.0):
        loop_count = 0
        swapped = False
        try:
            # 開始前カウントダウン
            if countdown_sec > 0:
                deadline = time.perf_counter() + countdown_sec
                while not self.stop_event.is_set():
                    rem = deadline - time.perf_counter()
                    if rem <= 0:
                        break
                    disp = int(rem) + 1 if rem >= 1.0 else round(rem, 1)
                    self._set_status_async(self._t("countdown_status", sec=disp), "#e0a800")
                    time.sleep(min(0.1, rem))
                if self.stop_event.is_set():
                    return

            if auto_swap:
                self._set_status_async(self._t("switching_virtual"), "#007bff")
                swapped = True
                ok, err = self._switch_to_virtual_1p()
                if not ok:
                    if err:
                        self._post_ui(messagebox.showerror, self._t("err_swap_title"), err)
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
                    elif cmd == "LSTICK":
                        x, y = arg
                        with self._vgamepad_lock:
                            if self.gamepad_out is None:
                                break
                            self.gamepad_out.left_joystick_float(x_value_float=x, y_value_float=y)
                            self.gamepad_out.update()
                    elif cmd == "RSTICK":
                        x, y = arg
                        with self._vgamepad_lock:
                            if self.gamepad_out is None:
                                break
                            self.gamepad_out.right_joystick_float(x_value_float=x, y_value_float=y)
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
            if auto_swap and swapped:
                self._set_status_async(self._t("restoring_physical"), "#007bff")
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
        self._update_duration_label()
        self._update_window_title()
        self.state_label.config(
            text=self._t("idle"),
            foreground="#aaaaaa" if self.dark_mode_var.get() else "#555555",
        )

    def save_file(self):
        if not self.current_file:
            return self.save_file_as()
        try:
            with open(self.current_file, "w", encoding="utf-8") as f:
                f.write(self.text_area.get("1.0", tk.END))
            self._mark_saved()
            self.file_label.config(text=self._t("current_file", path=self.current_file))
            messagebox.showinfo(self._t("save_done_title"), self._t("save_done_msg", path=self.current_file))
            return True
        except OSError as e:
            messagebox.showerror(self._t("save_err_title"), str(e))
            return False

    def save_file_as(self):
        path = filedialog.asksaveasfilename(
            title=self._t("dlg_save_title"),
            defaultextension=".txt",
            filetypes=[(self._t("ft_txt"), "*.txt"), (self._t("ft_all"), "*.*")],
            initialfile=os.path.basename(self.current_file),
        )
        if path:
            self.current_file = path
            return self.save_file()
        return False

    def load_file(self):
        if not self._confirm_save_if_dirty():
            return
        path = filedialog.askopenfilename(
            title=self._t("dlg_open_title"),
            filetypes=[(self._t("ft_txt"), "*.txt"), (self._t("ft_all"), "*.*")],
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.text_area.delete("1.0", tk.END)
                self.text_area.insert("1.0", content)
                self.current_file = path
                self.file_label.config(text=self._t("current_file", path=self.current_file))
                self._mark_saved()
                self._update_duration_label()
            except OSError as e:
                messagebox.showerror(self._t("load_err_title"), str(e))

    def open_in_notepad(self):
        # まず現在の内容をファイルに保存してからメモ帳で開く
        if not os.path.exists(self.current_file):
            with open(self.current_file, "w", encoding="utf-8") as f:
                f.write(self.text_area.get("1.0", tk.END))
            self._mark_saved()
        else:
            if messagebox.askyesno(self._t("notepad_title"), self._t("notepad_msg")):
                with open(self.current_file, "w", encoding="utf-8") as f:
                    f.write(self.text_area.get("1.0", tk.END))
                self._mark_saved()
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
        if not self._confirm_save_if_dirty():
            return
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
