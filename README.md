# ゲームパッド マクロ記録・再生ツール / Gamepad Macro Recorder & Player

[日本語](#日本語) | [English](#english)

---

<a id="日本語"></a>
## 日本語

Windows 向けのゲームパッド操作記録・再生ツールです。  
記録した操作はメモ帳などでそのまま編集できるシンプルなテキスト形式（`.txt`）で保存され、`ViGEmBus`（`vgamepad`）を用いた仮想 Xbox 360 コントローラーとして再生できます。

### 主な機能

- **ボタン操作・アナログスティック・待機時間の記録**: XInput（Xbox 互換）および DirectInput（汎用 HID パッド）のボタン・十字キー入力を約 250Hz で記録（オプションで左右アナログスティック `LSTICK` / `RSTICK` にも対応）
- **テキスト形式での保存・直接編集**: `WAIT` / `PRESS` / `RELEASE` / `LSTICK` / `RSTICK` のシンプルな書式（画面内エディタ・メモ帳の両方で編集可能）
- **未保存変更の検知と確認**: 未保存の変更がある場合はタイトルに `*` を表示し、終了・ファイル切替・新規記録時に保存確認ダイアログを表示
- **合計所要時間のリアルタイム表示**: エディタ内の `WAIT` を自動集計し、1周および指定回数ループ時の合計所要時間を表示
- **全 `WAIT` 一括調整**: エディタ内のすべての `WAIT` 秒数を `-0.001` / `+0.001` 秒単位で一括増減（`Ctrl+Z` で元に戻せます）
- **ループ再生・回数カウントダウン・開始前待機**: 無限ループ（`0`）または指定回数ループに対応。1周完了ごとに残り回数がカウントダウンされ、`0` になるとループ設定も自動解除。再生前のカウントダウン待機（0〜30秒）も設定可能
- **ループ時のランダム揺らぎ**: 各 `WAIT` とループ折り返し時に `+0`〜`+指定ms` のプラス方向ランダム遅延を追加可能
- **グローバルホットキー**: ゲーム画面がアクティブなまま `F9` で再生／停止、`F10` で記録／停止（キー変更・ON/OFF 可能）
- **10か国語 GUI 対応 & ダークモード**: 日本語 / English / 简体中文 / 繁體中文 / 한국어 / Español / Français / Deutsch / Português / Русский を再起動なしで切替可能。ライト／ダークモード対応
- **1P 自動入替機能（オプション・要管理者権限）**: 再生中だけ物理パッドを一時無効化して仮想パッドを 1P に割り当て、停止・終了時に物理パッドを 1P に自動復帰
- **起動時の無効パッド自動検知**: 前回の強制終了などで物理パッドが無効化されたまま残っていた場合、起動時に自動で検知して復帰を案内
- **自動電源オフ防止**: 無線 Xbox パッドのアイドルスリープを防ぐため、3分ごとに微小な振動信号を送信（ON/OFF 可能）
- **ウィンドウ状態の自動保存**: 終了時にウィンドウ位置・サイズ・各種オプション・言語・テーマ・最後に開いたファイルを `window_state.json` へ自動保存

### 動作環境

- **OS**: Windows 10 / 11 (64bit)
- **Python**: Python 3.11〜3.13（**安定版推奨**。Python 3.15 などの開発版ビルドでは Tkinter まわりが不安定になる場合があります）
- **ドライバ**: [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases)（仮想 Xbox 360 コントローラーの再生に必須）
- **Python パッケージ**: `vgamepad`（`requirements.txt` に記載）

### セットアップ

1. **ViGEmBus ドライバをインストール**  
   [ViGEmBus Releases](https://github.com/nefarius/ViGEmBus/releases) からインストーラーをダウンロードしてインストールします。
2. **依存パッケージをインストール**
   ```powershell
   pip install -r requirements.txt
   ```
3. **起動**
   - 通常起動（1P自動入替を使わない場合）:
     ```powershell
     python gamepad_macro.py
     ```
   - 管理者権限で起動（1P自動入替を使う場合）:  
     `start_macro.bat` をダブルクリックします（UAC の確認画面が出ます）。

### マクロファイルの書式

```text
# '#' 以降はコメントとして無視されます
PRESS A
WAIT 0.100
RELEASE A
LSTICK 0.00 1.00
WAIT 0.500
LSTICK 0.00 0.00
```

使用可能なボタン名:  
`A`, `B`, `X`, `Y`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `LB`, `RB`, `LT`, `RT`, `LS`, `RS`, `START`, `BACK`（DirectInput 記録時は `BTN1`〜`BTN32`）  
アナログスティック: `LSTICK <x> <y>` / `RSTICK <x> <y>`（`-1.00` 〜 `1.00`）

### ⚠️ 注意事項・トラブルシューティング（必ずお読みください）

#### 1. 「1P自動入替」使用後に物理パッドが反応しなくなった場合
「再生時だけ物理パッドと1Pを自動入替」をオンにしている最中に、PC の強制終了・停電・タスクマネージャーからの強制終了などが起きると、Windows 上で物理パッドが **無効化されたまま** 残ることがあります。

以下のいずれかの方法で簡単に元に戻せます：

1. **ツールを起動し直す（最も簡単）**  
   `start_macro.bat` から管理者権限で起動すると、無効化されたままの物理パッドを自動検知して **「今すぐ有効化（復帰）しますか？」** と案内が出るので「はい」を押してください。
2. **アプリ内の「🔄 物理パッドを戻す」ボタンを押す**  
   「記録・再生コントロール」枠の右端にある `🔄 物理パッドを戻す` ボタンをクリックしてください（管理者権限が必要）。
3. **Windows のデバイスマネージャーから手動で戻す**  
   - スタートボタンを右クリック → **「デバイスマネージャー」** を開く
   - **「ヒューマン インターフェイス デバイス」** や **「Xbox 周辺機器」** の中に **下向き矢印（↓）のアイコン** が付いたコントローラー関連デバイスがあれば、右クリック → **「デバイスを有効にする」** を実行
   - 反映されない場合は物理パッドを一度抜き差し（または再接続）してください。
4. **HidHide を別途インストールしている場合**  
   他のツール（DS4Windows 等）で **HidHide** を使っている場合、HidHide 側で物理パッドが隠されたままになっていることがあります。スタートメニューから **HidHide Configuration Client** を開き、「Devices」タブの **「Enable device hiding」** のチェックを外してからパッドを抜き差ししてください。

#### 2. DirectInput コントローラー（HORI / Switch / PS 系など）利用時の注意
- 本ツールは **XInput（Xbox 互換）コントローラーを推奨** しています。
- DirectInput コントローラーでも **ボタンと十字キーの記録・再生** は可能ですが、以下の制限があります：
  - 再生時は **仮想 Xbox 360 コントローラー（XInput）に変換して出力** されます。
  - メーカーや機種によってボタン番号（`BTN1`〜`BTN12`）の割り当てが異なるため、A/B や X/Y が入れ替わって再生される場合があります。その場合はテキスト上の `BTN1` などを `A` 等に書き換えて調整してください。
  - `BTN13` 以降のボタンおよびアナログスティック入力は記録・再生の対象外です。
  - 「1P自動入替」は XInput ポート 0 を基準に動作するため、DirectInput 専用パッドでは物理パッドの無効化は行われず、仮想 Xbox 360 パッドがそのまま XInput 1P として接続されます。
  - 「自動電源オフ防止」は XInput 振動 API を使用しているため、DirectInput 専用パッドには効きません。

#### 3. アナログスティックについて
- 初期設定ではボタンと十字キーのみ記録します。アナログスティックを記録したい場合は **「アナログスティックも記録する (LSTICK / RSTICK)」** にチェックを入れてください（微細なノイズを抑えるためデッドゾーンが適用されます）。

#### 4. 利用規約・免責事項
- 本ツールは **オフライン環境での検証・個人用途** を想定して作成されています。
- **オンラインゲームなど、利用規約で外部ツールや自動操作（マクロ）が禁止されているソフトウェアでは使用しないでください。** アカウント停止等の不利益が生じても作者は一切の責任を負いません。
- 本ツールの使用によって生じた機器・ソフトウェア・データ等のいかなる損害についても、すべて利用者の自己責任となります。

---

<a id="english"></a>
## English

A Windows GUI tool for recording and playing back gamepad button macros.  
Recorded macros are saved as human-readable plain text (`.txt`) that can be easily edited in Notepad or the built-in editor, and played back through a virtual Xbox 360 controller powered by `ViGEmBus` (`vgamepad`).

### Features

- **Button, Analog Stick & Timing Recording**: Records button and D-pad inputs from XInput (Xbox-compatible) and DirectInput (generic HID) controllers at ~250 Hz, with optional analog stick recording (`LSTICK` / `RSTICK`).
- **Plain-Text Format & Direct Editing**: Uses a simple `WAIT` / `PRESS` / `RELEASE` / `LSTICK` / `RSTICK` format editable directly in the app or in Notepad.
- **Unsaved Changes Warning**: Displays `*` in the title bar when modified and prompts to save before closing, loading another file, or starting a new recording.
- **Live Total Duration Estimate**: Automatically sums all `WAIT` commands and displays both single-loop and total multi-loop durations.
- **Bulk `WAIT` Adjustment**: Shift all `WAIT` durations in the editor by `-0.001` or `+0.001` seconds at once (supports `Ctrl+Z` undo).
- **Loop Playback, Countdown & Start Delay**: Supports infinite looping (`0`) or a fixed count with live countdown (auto-disables looping at `0`), plus an optional pre-playback countdown delay (0–30s).
- **Random Timing Jitter (Loop Mode)**: Optionally adds a positive random delay (`+0` to `+N ms`) to each `WAIT` and between loop iterations.
- **Global Hotkeys**: Start/stop playback (`F9`) and recording (`F10`) even while another window is focused (configurable keys and toggleable).
- **10-Language UI & Dark Mode**: Instant switching across Japanese, English, Simplified/Traditional Chinese, Korean, Spanish, French, German, Portuguese, and Russian, plus Light/Dark themes.
- **Auto 1P Swap (Optional, Requires Admin)**: Temporarily disables the physical controller during playback so the virtual controller takes Player 1 (slot 0), then automatically restores the physical controller on stop or exit.
- **Startup Disabled-Pad Detection**: Automatically checks on startup if a physical controller was left disabled (e.g., after a crash or power loss) and offers one-click recovery.
- **Wireless Auto-Sleep Prevention**: Sends a tiny vibration pulse every 3 minutes to prevent wireless Xbox controllers from powering off while idle (toggleable).
- **Persistent Window & Settings State**: Automatically saves window position, size, maximized state, language, theme, options, and the last opened macro file to `window_state.json`.

### Requirements

- **OS**: Windows 10 / 11 (64-bit)
- **Python**: Python 3.11 – 3.13 (**stable release recommended**; pre-release builds such as Python 3.15 alpha may cause Tkinter instability)
- **Driver**: [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) (required for virtual Xbox 360 controller playback)
- **Python Package**: `vgamepad` (listed in `requirements.txt`)

### Setup

1. **Install the ViGEmBus driver**  
   Download and run the installer from [ViGEmBus Releases](https://github.com/nefarius/ViGEmBus/releases).
2. **Install Python dependencies**
   ```powershell
   pip install -r requirements.txt
   ```
3. **Launch**
   - Standard launch (without Auto 1P Swap):
     ```powershell
     python gamepad_macro.py
     ```
   - Administrator launch (required for Auto 1P Swap):  
     Double-click `start_macro.bat` (you will be prompted by UAC).

### Macro File Format

```text
# Everything after '#' is treated as a comment
PRESS A
WAIT 0.100
RELEASE A
LSTICK 0.00 1.00
WAIT 0.500
LSTICK 0.00 0.00
```

Supported button names:  
`A`, `B`, `X`, `Y`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `LB`, `RB`, `LT`, `RT`, `LS`, `RS`, `START`, `BACK` (`BTN1`–`BTN32` when recording via DirectInput)  
Analog sticks: `LSTICK <x> <y>` / `RSTICK <x> <y>` (`-1.00` to `1.00`)

### ⚠️ Important Notes & Troubleshooting (Please Read)

#### 1. If Your Physical Controller Stops Responding After Using "Auto 1P Swap"
If the PC shuts down unexpectedly, loses power, or the app is force-killed from Task Manager while **Auto 1P Swap** is active, Windows may leave your physical controller in a **disabled** state.

You can easily restore it using any of the following methods:

1. **Relaunch the tool (Easiest)**  
   Start the tool as Administrator via `start_macro.bat`. It will automatically detect any disabled physical controller on startup and prompt you to re-enable it.
2. **Click the "🔄 物理パッドを戻す" (Restore Physical Pad) button**  
   Click the restore button on the right side of the control panel (requires Administrator privileges).
3. **Re-enable manually in Windows Device Manager**  
   - Right-click the Start button → open **Device Manager**.
   - Look under **Human Interface Devices** or **Xbox Peripherals** for any controller entry with a **downward arrow (↓)** icon, right-click it, and select **Enable device**.
   - Unplug and replug (or reconnect) the controller if needed.
4. **If you also have HidHide installed**  
   If another tool (such as DS4Windows) uses **HidHide**, your physical controller may still be hidden by HidHide. Open **HidHide Configuration Client** from the Start menu, go to the **Devices** tab, uncheck **Enable device hiding**, and reconnect your controller.

#### 2. Notes on DirectInput Controllers (HORI / Switch / PlayStation, etc.)
- **XInput (Xbox-compatible) controllers are recommended.**
- DirectInput controllers can record and play back **buttons and D-pad**, with the following limitations:
  - Playback is always output through a **virtual Xbox 360 (XInput) controller**.
  - Because `BTN1`–`BTN12` mappings vary by manufacturer, buttons such as A/B or X/Y may be swapped during playback. You can edit the text file (e.g., replace `BTN1` with `A`) to adjust mappings.
  - `BTN13` and above, as well as analog sticks, are not supported.
  - **Auto 1P Swap** monitors XInput slot 0, so DirectInput-only controllers are not disabled; the virtual Xbox 360 controller simply connects as XInput Player 1.
  - **Auto-Sleep Prevention** uses the XInput vibration API and does not affect DirectInput-only controllers.

#### 3. Analog Sticks
- By default, only buttons and D-pad are recorded. Enable **"Record analog sticks (LSTICK / RSTICK)"** if you also want to capture stick movements (a deadzone is applied to reduce noise).

#### 4. Terms of Use & Disclaimer
- This tool is intended for **offline testing and personal use**.
- **Do not use this tool in online games or software whose Terms of Service prohibit external tools or automation/macros.** The author is not responsible for account bans or any other penalties.
- Use this software at your own risk. The author assumes no liability for any damage to hardware, software, or data resulting from its use.
