# 健康休息提醒（Rest Reminder）

基于 Python Tkinter 的 Windows 桌面久坐提醒工具。定时提醒起身活动，帮助养成健康的工作节奏。

## 功能特性

- **定时提醒**：可自定义工作时长（默认 45 分钟），时间到后弹出提醒并进入休息倒计时（默认 5 分钟）
- **锁屏检测**：通过 Windows API（`OpenInputDesktop`）检测真正的锁屏/解锁动作，解锁回来后自动重置工作时间；短暂锁屏（如 UAC 安全桌面）不会误重置
- **任务栏图标倒计时**：用 PIL 动态生成带剩余分钟数的任务栏图标，工作阶段显示绿色、休息阶段显示橙色，无需打开窗口即可查看剩余时间
- **忙碌时一键隐藏**：休息弹窗支持"隐藏"按钮 / `Esc` / 弹出 3 秒后自动收起，最小化到任务栏后工作与休息计时照常进行
- **单实例保护**：通过命名互斥量（Mutex）防止重复启动；再次启动时会激活已运行实例的窗口
- **设置持久化**：工作/休息时长、自动收起选项保存到 `rest_reminder_config.json`，重启后自动加载
- **一键跳过/重置**：支持"立刻休息""跳过休息""重置计时"操作

## 环境要求

- Windows 系统
- Python 3.x（[python.org](https://www.python.org/downloads/) 安装时务必勾选 **Add python.exe to PATH**，会同时提供后台运行所需的 `pythonw.exe`）
- [Pillow](https://pillow.readthedocs.io/)（用于生成任务栏图标，唯一的第三方依赖）

### 安装步骤（Windows）

1. 安装 Python：从 [python.org/downloads](https://www.python.org/downloads/) 下载安装包，勾选 "Add python.exe to PATH"；
2. 安装 Pillow：按 `Win + R` 输入 `cmd` 打开命令提示符，执行：

   ```
   pip install pillow
   ```

3. 将 `rest_reminder.pyw`、`rest_reminder_config.json`、`启动提醒.bat` 放在同一目录，双击 `启动提醒.bat` 启动。

## 使用方法

双击 `rest_reminder.pyw` 直接运行，或双击 `启动提醒.bat` 后台启动（无控制台窗口）。

程序主界面：

- 顶部显示当前阶段（"工作中..." / "该活动了！"）与倒计时
- 中部进度条直观显示时间进度
- 可随时修改工作/休息分钟数并点击"应用"，设置会自动保存

休息弹窗弹出时如果正在忙：

- 点"隐藏"按钮，或按 `Esc` / `Ctrl+H`，窗口最小化到任务栏
- 勾选底部"3 秒后自动收起"后，弹窗若没有切到前台，3 秒后自动最小化
- 最小化后倒计时不会中断，任务栏图标上的数字继续走；再点一下任务栏按钮即可恢复窗口

> 注意：程序有单实例保护。更新版本后请先关闭正在运行的旧实例，再启动新版，否则会激活旧窗口而不加载新代码。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `rest_reminder.pyw` | 主程序 |
| `rest_reminder_config.json` | 配置文件（工作/休息时长、自动收起） |
| `启动提醒.bat` | 后台启动脚本 |

## 技术要点

- 任务栏图标用 PIL 绘制多尺寸 ICO，再通过 `WM_SETICON` + `SetClassLongPtr` 写入窗口图标；Win11 资源管理器只对后台窗口缓存按钮图标，因此仅在窗口已最小化时用 `SW_HIDE` → `SW_SHOWMINNOACTIVE` 重建按钮（可见窗口不重建，避免窗口被顶到前台）
- 计时在主线程用 250 ms 定时循环 + `time.monotonic()` 时间差累加推进，不靠后台线程改 UI；锁屏期间暂停计时，短暂锁屏（如 UAC）不重置，休眠超过 2 分钟回来重新计时
- 所有 Win32 调用（`LoadImageW`、`GetAncestor`、`SendMessageW` 等）显式声明 `restype`/`argtypes`，避免 64 位句柄被截断；替换图标时用 `DestroyIcon` 释放旧句柄

## License

MIT
