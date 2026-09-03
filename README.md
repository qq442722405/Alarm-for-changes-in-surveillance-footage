# 屏幕监控智能报警

这是一个 Windows 屏幕区域监控程序。

## 功能

- 不打开、不导入视频文件。
- 框选时保留实际屏幕画面可见，不再出现黑色不透明遮罩。
- 支持“显示框选区域”开关，可随时在屏幕上显示/隐藏已选区域。
- EXE 使用软件专属 ICO 图标。
- 直接监控 Windows 实际屏幕。
- 点击“框选屏幕区域”后，在屏幕上拖动选择区域。
- 支持多个监控区域。
- 每个区域独立设置：
  - 启用/停用
  - 画面变化检测
  - 人员检测
  - 灵敏度
  - 最小变化面积
  - 连续确认帧
  - 报警冷却时间
  - 报警后自动暂停
- 检测到事件后：
  - Windows 报警提示音
  - 保存事件截图
  - 记录事件时间、区域、原因
  - 可导出 CSV
- 配置自动保存到用户目录。
- GitHub Actions 只允许手动触发打包。

## 本地运行

Python 3.11 推荐。

```bash
pip install -r requirements.txt
python main.py
```

## 本地打包

双击：

`build.bat`

生成：

`dist/屏幕监控智能报警.exe`

## GitHub 在线打包

上传整个工程到 GitHub。

进入：

Actions → Windows EXE → Run workflow

本工程的 workflow 使用：

```yaml
on:
  workflow_dispatch:
```

因此不会因为 push 自动打包，只能手动点击 Run workflow。

## 人员识别

人员检测使用 Ultralytics YOLO。

首次启用人员检测时可能需要联网下载 `yolo11n.pt` 模型。

如果无法下载模型：

- 画面变化检测仍然可以使用；
- 人员检测暂时不可用。

## 数据目录

程序会在：

`%USERPROFILE%\.screen_monitor_ai`

保存：

- `regions.json`：监控区域配置
- `snapshots`：报警截图

