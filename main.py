import sys, json, time, os, traceback, ctypes
from pathlib import Path
from datetime import datetime

# ==================== 1. 静态资源与路径处理 ====================
def resource_path(relative_path):
    """获取静态资源的绝对路径，兼容 PyInstaller 单文件打包模式与本地开发环境"""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).parent / relative_path

# ==================== 2. 全局异常捕获 ====================
def handle_exception(exc_type, exc_value, exc_traceback):
    """捕获未处理的异常，弹出对话框并保存日志"""
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    
    log_dir = Path.home() / ".screen_monitor_ai"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "crash_log.txt"
    try:
        log_file.write_text(err_msg, encoding="utf-8")
    except Exception:
        pass
        
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"程序运行发生异常：\n\n{err_msg[:600]}\n\n完整日志已保存至：\n{log_file}",
            "监控变化报警 - 错误提示",
            0x10
        )
    except Exception:
        pass

    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = handle_exception

# ==================== 3. 核心业务依赖 ====================
import cv2
import mss
import numpy as np
try:
    from ultralytics import YOLO
except Exception:
    YOLO = None
from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QIcon, QPolygon, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QComboBox,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QSpinBox, QDoubleSpinBox,
    QCheckBox, QMessageBox, QFileDialog, QScrollArea, QToolButton, QListWidget, QListWidgetItem, QSizePolicy
)

APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
SNAP_DIR = APP_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)
SCREENSHOT_CONFIG = APP_DIR / "screenshot_config.json"
APP_CONFIG = APP_DIR / "app_config.json"


class CollapsibleBox(QWidget):
    """通用折叠面板组件"""
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.toggle_button = QToolButton()
        # 显式指定 color: #2c3e50，确保深浅色系统模式下字体都清晰
        self.toggle_button.setStyleSheet(
            "QToolButton { border: none; font-weight: bold; font-size: 14px; text-align: left; padding: 6px; color: #2c3e50; background-color: #e8ecef; border-radius: 4px; }"
            "QToolButton:hover { background-color: #dbe2e8; }"
        )
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.DownArrow)
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)

        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(4, 4, 4, 4)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.toggle_button)
        lay.addWidget(self.content_area)

        self.toggle_button.clicked.connect(self.on_toggle)

    def on_toggle(self, checked):
        self.toggle_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.content_area.setVisible(checked)

    def addLayout(self, layout):
        self.content_layout.addLayout(layout)

    def addWidget(self, widget):
        self.content_layout.addWidget(widget)


class Alarm(QObject):
    def play(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            QApplication.beep()


class Selector(QWidget):
    """四点位多边形框选窗口"""
    selected = Signal(list)

    def __init__(self, image, left, top):
        super().__init__()
        self.left, self.top = left, top
        self.points = []
        self.current_pos = QPoint()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setGeometry(left, top, image.width(), image.height())
        self.pixmap = QPixmap.fromImage(image)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        p.setOpacity(0.12)
        p.fillRect(self.rect(), Qt.black)
        p.setOpacity(1.0)
        p.setRenderHint(QPainter.Antialiasing)

        if len(self.points) > 0:
            p.setPen(QPen(Qt.green, 2))
            p.setBrush(Qt.green)
            for pt in self.points:
                p.drawEllipse(QPoint(pt[0] - self.left, pt[1] - self.top), 4, 4)

            poly = QPolygon()
            for pt in self.points:
                poly.append(QPoint(pt[0] - self.left, pt[1] - self.top))

            if len(self.points) > 1:
                p.setPen(QPen(Qt.green, 1, Qt.SolidLine))
                for i in range(len(self.points) - 1):
                    p.drawLine(poly[i], poly[i+1])

            p.setPen(QPen(Qt.yellow, 1, Qt.DashLine))
            p.drawLine(poly[-1], self.current_pos)

        p.setPen(Qt.white)
        tip_str = f"请依次在屏幕上点击 4 个点位确定监控区域（已选择 {len(self.points)}/4 点）。按 Esc 取消，右键重置"
        p.drawText(30, 40, tip_str)

    def mouseMoveEvent(self, e):
        self.current_pos = e.position().toPoint()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            pos = e.position().toPoint()
            abs_x = self.left + pos.x()
            abs_y = self.top + pos.y()
            self.points.append([abs_x, abs_y])
            if len(self.points) == 4:
                self.selected.emit(self.points)
                self.close()
            else:
                self.update()
        elif e.button() == Qt.RightButton:
            self.points.clear()
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


class RegionOverlay(QWidget):
    """桌面多边形边框渲染 Overlay（固定绿色细线）"""
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setGeometry(owner.virtual_geometry())
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        mon = self.owner.sct.monitors[0]

        for i, r in enumerate(self.owner.regions):
            if not r.enabled or len(r.points) < 4:
                continue

            poly = QPolygon()
            for pt in r.points:
                poly.append(QPoint(pt[0] - mon['left'], pt[1] - mon['top']))

            # 固定绿色，线宽保持 1px 细线
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(Qt.green, 1))
            p.drawPolygon(poly)

            top_pt = min([QPoint(pt[0] - mon['left'], pt[1] - mon['top']) for pt in r.points], key=lambda pt: pt.y())
            p.drawText(top_pt + QPoint(6, -6 if top_pt.y() > 20 else 20), f"{i+1}. {r.name}")


class ScreenshotSelector(QWidget):
    """单独框选报警截图区域，支持在整个桌面上拖动选择矩形。"""
    selected = Signal(list)

    def __init__(self, image, left, top):
        super().__init__()
        self.left, self.top = left, top
        self.image = image
        self.start = None
        self.end = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setGeometry(left, top, image.width(), image.height())
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start = event.position().toPoint()
            self.end = self.start
            self.update()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.start is not None:
            self.end = event.position().toPoint()
            x1, y1 = self.start.x(), self.start.y()
            x2, y2 = self.end.x(), self.end.y()
            x, y = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)
            if w >= 20 and h >= 20:
                self.selected.emit([self.left + x, self.top + y, w, h])
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawImage(0, 0, self.image)
        p.fillRect(self.rect(), QColor(0, 0, 0, 70))
        if self.start is not None and self.end is not None:
            rect = QRect(self.start, self.end).normalized()
            p.drawImage(rect, self.image.copy(rect))
            p.setPen(QPen(Qt.red, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)
            p.setPen(Qt.white)
            p.drawText(rect.topLeft() + QPoint(8, -8), f"截图区域 {rect.width()}×{rect.height()}")


class Region:
    def __init__(self, data=None):
        d = data or {}
        self.name = d.get("name", "监控区域")
        self.enabled = bool(d.get("enabled", True))
        
        # 高级画面检测参数
        self.sensitivity = int(d.get("sensitivity", 70))       # 灵敏度 (1-100)
        self.min_ratio = float(d.get("min_ratio", 2.0))         # 触发面积占比%
        self.noise_filter = int(d.get("noise_filter", 3))       # 抗草木抖动/噪点等级 (1-10)
        self.learn_speed = float(d.get("learn_speed", 0.01))    # 日夜/光线适应速度 (0.001 - 0.1)

        self.confirm = int(d.get("confirm", 3))
        self.cooldown = int(d.get("cooldown", 10))
        self.auto_pause = bool(d.get("auto_pause", False))

        # 可独立开启的“人员检测”开关。
        # 与画面变化检测相互独立：即使人物走得很慢、穿黑衣导致画面变化面积很小，
        # 只要 YOLO 识别到 person，也可以直接进入报警确认流程。
        self.person_enabled = bool(d.get("person_enabled", False))
        self.person_conf = float(d.get("person_conf", 0.20))
        self.person_interval = float(d.get("person_interval", 0.8))

        # 画面变化检测调试开关：可以分别关闭/打开不同处理环节，方便现场逐项测试。
        self.motion_enabled = bool(d.get("motion_enabled", True))
        self.frame_diff_enabled = bool(d.get("frame_diff_enabled", True))
        self.mog2_enabled = bool(d.get("mog2_enabled", True))
        self.shadow_filter = bool(d.get("shadow_filter", True))
        self.morph_enabled = bool(d.get("morph_enabled", True))
        self.background_learning = bool(d.get("background_learning", True))
        self.pixel_threshold = int(d.get("pixel_threshold", 25))
        self.min_blob_ratio = float(d.get("min_blob_ratio", 0.05))

        self.points = d.get("points", [])
        if not self.points and "x" in d:
            x, y, w, h = d.get("x", 0), d.get("y", 0), d.get("w", 300), d.get("h", 200)
            self.points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

        self.confirm_count = 0
        self.last_event = 0.0
        self.raw_ratio = 0.0
        self.display_ratio = 0.0
        self.preview_original = None
        self.preview_raw = np.zeros((10, 10), dtype=np.uint8)
        self.preview_processed = np.zeros((10, 10), dtype=np.uint8)
        self.preview_boxes = []
        self.preview_person_boxes = []
        self.preview_motion = False
        self._update_bounds()
        self.init_subtractor()

    def _update_bounds(self):
        if len(self.points) >= 4:
            xs = [pt[0] for pt in self.points]
            ys = [pt[1] for pt in self.points]
            self.x = min(xs)
            self.y = min(ys)
            self.w = max(10, max(xs) - self.x)
            self.h = max(10, max(ys) - self.y)
            self.rel_points = np.array([[pt[0] - self.x, pt[1] - self.y] for pt in self.points], dtype=np.int32)
        else:
            self.x, self.y, self.w, self.h = 0, 0, 100, 100
            self.rel_points = np.array([[0,0], [100,0], [100,100], [0,100]], dtype=np.int32)

    def init_subtractor(self):
        """初始化混合高斯背景提取器，适应复杂的户外/白天黑夜环境"""
        var_threshold = max(8, int(100 - self.sensitivity * 0.8))
        self.subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=var_threshold,
            detectShadows=True
        )
        self.last_gray = None

    def to_dict(self):
        return {
            "name": self.name,
            "enabled": self.enabled,
            "sensitivity": self.sensitivity,
            "min_ratio": self.min_ratio,
            "noise_filter": self.noise_filter,
            "learn_speed": self.learn_speed,
            "confirm": self.confirm,
            "cooldown": self.cooldown,
            "auto_pause": self.auto_pause,
            "person_enabled": self.person_enabled,
            "person_conf": self.person_conf,
            "person_interval": self.person_interval,
            "motion_enabled": self.motion_enabled,
            "frame_diff_enabled": self.frame_diff_enabled,
            "mog2_enabled": self.mog2_enabled,
            "shadow_filter": self.shadow_filter,
            "morph_enabled": self.morph_enabled,
            "background_learning": self.background_learning,
            "pixel_threshold": self.pixel_threshold,
            "min_blob_ratio": self.min_blob_ratio,
            "points": self.points
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("监控变化报警")
        
        icon_path = resource_path("app.ico")
        if icon_path.exists():
            try:
                self.setWindowIcon(QIcon(str(icon_path)))
            except Exception:
                pass

        # 允许自由调整大小
        self.setMinimumSize(480, 520)
        self.resize(540, 680)

        self.regions = []
        self.running = False
        self.selected_index = -1
        self.sct = mss.mss()
        self.alarm = Alarm()
        self.region_overlay = RegionOverlay(self)

        # YOLO 人员检测：首次启用时懒加载，模型已随工程打包，默认不需要联网下载。
        self.person_model = None
        self.person_model_loading = False
        self.person_model_error = ""
        self.person_last_detect = {}
        self.person_last_result = {}

        # 报警截图：与监控区域完全独立，默认保存到用户目录下的 snapshots。
        self.screenshot_enabled = True
        self.screenshot_rect = None
        self.screenshot_interval = 5
        self.screenshot_dir = SNAP_DIR
        self.last_screenshot_time = 0.0
        self.load_screenshot_config()

        self._build_ui()
        self.auto_load_config()
        self.update_screenshot_form()

        self.show_regions_cb.setChecked(True)
        self.toggle_region_overlay()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        main_layout = QVBoxLayout(content)

        # 1. 主标题（显式设置深灰色文字 color: #1a1a1a，防止白底白字）
        title = QLabel("监控变化报警")
        title.setStyleSheet("font-size:18px; font-weight:bold; color: #1a1a1a; padding: 2px;")
        main_layout.addWidget(title)

        # 2. 面板一：监控区域与选择器（可折叠）
        box_region = CollapsibleBox("一、 监控区域设置")
        
        btn_layout_1 = QHBoxLayout()
        self.select_btn = QPushButton("四点框选屏幕")
        self.del_btn = QPushButton("删除当前区域")
        self.test_btn = QPushButton("测试报警")
        self.clear_btn = QPushButton("清理缓存")
        btn_layout_1.addWidget(self.select_btn)
        btn_layout_1.addWidget(self.del_btn)
        btn_layout_1.addWidget(self.test_btn)
        btn_layout_1.addWidget(self.clear_btn)
        box_region.addLayout(btn_layout_1)

        form_region = QFormLayout()
        self.region_combo = QComboBox()
        self.region_combo.currentIndexChanged.connect(self.region_changed)
        form_region.addRow("切换监控区域", self.region_combo)

        self.pos_label = QLabel("-")
        form_region.addRow("多边形坐标范围", self.pos_label)
        box_region.addLayout(form_region)
        main_layout.addWidget(box_region)

        # 2.5 报警截图设置：截图区域与监控区域独立。
        box_shot = CollapsibleBox("二、 报警截图画面")
        shot_btns = QHBoxLayout()
        self.select_shot_btn = QPushButton("框选截图区域")
        self.clear_shot_btn = QPushButton("清除截图区域")
        shot_btns.addWidget(self.select_shot_btn)
        shot_btns.addWidget(self.clear_shot_btn)
        box_shot.addLayout(shot_btns)
        shot_form = QFormLayout()
        self.screenshot_cb = QCheckBox("报警时自动截图")
        self.screenshot_interval_spin = QSpinBox()
        self.screenshot_interval_spin.setRange(1, 3600)
        self.screenshot_interval_spin.setSuffix(" 秒（两次截图最小间隔）")
        self.screenshot_path_label = QLabel(str(self.screenshot_dir))
        self.screenshot_path_label.setWordWrap(True)
        self.select_screenshot_path_btn = QPushButton("选择保存位置")
        self.screenshot_region_label = QLabel("尚未设置（未设置时不截图）")
        self.screenshot_region_label.setWordWrap(True)
        shot_form.addRow("", self.screenshot_cb)
        shot_form.addRow("截图间隔", self.screenshot_interval_spin)
        shot_form.addRow("截图区域", self.screenshot_region_label)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.screenshot_path_label, 1)
        path_layout.addWidget(self.select_screenshot_path_btn)
        shot_form.addRow("截图保存位置", path_layout)
        box_shot.addLayout(shot_form)
        main_layout.addWidget(box_shot)

        # 3. 面板二：算法优化参数（可折叠）
        box_params = CollapsibleBox("二、 画面检测抗干扰参数")
        form_params = QFormLayout()

        self.enable_cb = QCheckBox("启用该区域监控")
        
        self.sens = QSpinBox()
        self.sens.setRange(1, 100)
        self.sens.setSuffix(" %")

        self.ratio = QDoubleSpinBox()
        self.ratio.setRange(0.1, 50.0)
        self.ratio.setSingleStep(0.1)
        self.ratio.setSuffix(" %")

        self.noise = QSpinBox()
        self.noise.setRange(1, 10)
        self.noise.setToolTip("数字越大，越能过滤风吹草动、雨雪微小颗粒的晃动干扰")

        self.learn_rate = QDoubleSpinBox()
        self.learn_rate.setRange(0.001, 0.1)
        self.learn_rate.setSingleStep(0.005)
        self.learn_rate.setDecimals(3)
        self.learn_rate.setToolTip("调整光线渐变适应速度（如云影过境、白天黑夜切换）")

        # 画面变化检测的独立调试开关
        self.motion_cb = QCheckBox("启用画面变化检测")
        self.frame_diff_cb = QCheckBox("启用相邻帧差分")
        self.mog2_cb = QCheckBox("启用背景模型（MOG2）")
        self.shadow_cb = QCheckBox("过滤阴影")
        self.morph_cb = QCheckBox("启用形态学去噪")
        self.learn_cb = QCheckBox("启用自适应背景学习")

        self.pixel_threshold = QSpinBox()
        self.pixel_threshold.setRange(1, 100)
        self.pixel_threshold.setValue(25)
        self.pixel_threshold.setSuffix(" 灰度差")
        self.pixel_threshold.setToolTip("越小越敏感，越容易把小变化/噪声算进去")

        self.min_blob = QDoubleSpinBox()
        self.min_blob.setRange(0.01, 10.0)
        self.min_blob.setSingleStep(0.01)
        self.min_blob.setDecimals(2)
        self.min_blob.setSuffix(" %")
        self.min_blob.setToolTip("过滤很小的变化块；越小越容易保留小目标")

        self.confirm = QSpinBox()
        self.confirm.setRange(1, 30)

        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setSuffix(" 秒")

        self.auto_pause_cb = QCheckBox("报警后自动暂停监控")

        form_params.addRow("", self.enable_cb)
        form_params.addRow("", self.motion_cb)
        form_params.addRow("", self.frame_diff_cb)
        form_params.addRow("", self.mog2_cb)
        form_params.addRow("", self.shadow_cb)
        form_params.addRow("", self.morph_cb)
        form_params.addRow("", self.learn_cb)
        form_params.addRow("变化像素阈值", self.pixel_threshold)
        form_params.addRow("最小变化块面积", self.min_blob)
        form_params.addRow("动静灵敏度", self.sens)
        form_params.addRow("触发变化面积", self.ratio)
        form_params.addRow("抗草木抖动等级", self.noise)
        form_params.addRow("光线适应速度", self.learn_rate)
        form_params.addRow("连续确认帧", self.confirm)
        form_params.addRow("报警冷却", self.cooldown)
        form_params.addRow("", self.auto_pause_cb)

        # 人员检测开关：用户可以单独打开测试，不影响原来的画面变化算法。
        self.person_cb = QCheckBox("启用人员检测（YOLO，推荐测试）")
        self.person_conf_spin = QDoubleSpinBox()
        self.person_conf_spin.setRange(0.05, 0.95)
        self.person_conf_spin.setSingleStep(0.05)
        self.person_conf_spin.setDecimals(2)
        self.person_conf_spin.setSuffix(" 置信度")
        self.person_interval_spin = QDoubleSpinBox()
        self.person_interval_spin.setRange(0.2, 5.0)
        self.person_interval_spin.setSingleStep(0.1)
        self.person_interval_spin.setDecimals(1)
        self.person_interval_spin.setSuffix(" 秒/次")
        self.person_status = QLabel("人员检测：未启用")
        self.person_status.setStyleSheet("color:#555;")

        form_params.addRow("", self.person_cb)
        form_params.addRow("人员识别最低置信度", self.person_conf_spin)
        form_params.addRow("人员识别间隔", self.person_interval_spin)
        form_params.addRow("", self.person_status)

        box_params.addLayout(form_params)
        main_layout.addWidget(box_params)

        # 绑定参数控制事件
        for w in [self.enable_cb, self.motion_cb, self.frame_diff_cb, self.mog2_cb,
                  self.shadow_cb, self.morph_cb, self.learn_cb, self.auto_pause_cb, self.person_cb]:
            w.stateChanged.connect(self.apply_form)
        for w in [self.sens, self.ratio, self.noise, self.learn_rate, self.pixel_threshold,
                  self.min_blob, self.confirm, self.cooldown]:
            w.valueChanged.connect(self.apply_form)
        for w in [self.person_conf_spin, self.person_interval_spin]:
            w.valueChanged.connect(self.apply_form)

        # 4. 面板三：实时控制与状态（可折叠）
        box_control = CollapsibleBox("三、 实时监测与控制")
        btn_layout_2 = QHBoxLayout()
        self.start_btn = QPushButton("开始监控")
        self.pause_btn = QPushButton("暂停")
        self.show_regions_cb = QCheckBox("显示桌面框选轮廓")
        btn_layout_2.addWidget(self.start_btn)
        btn_layout_2.addWidget(self.pause_btn)
        btn_layout_2.addWidget(self.show_regions_cb)
        box_control.addLayout(btn_layout_2)

        self.status = QLabel("状态：未监控")
        self.status.setStyleSheet("font-size:14px; font-weight:bold; color: #2c3e50; padding: 4px;")
        box_control.addWidget(self.status)
        main_layout.addWidget(box_control)

        # 4. 实时识别效果预览：显示原始画面、前景掩膜、去噪结果以及人员框。
        box_preview = CollapsibleBox("四、 实时识别效果（调试）")
        preview_top = QHBoxLayout()
        self.preview_cb = QCheckBox("开启实时识别效果预览")
        self.preview_cb.setChecked(True)
        self.preview_mode = QComboBox()
        self.preview_mode.addItems(["处理后画面", "原始画面", "前景掩膜", "去噪后掩膜", "人员检测框"])
        preview_top.addWidget(self.preview_cb)
        preview_top.addWidget(QLabel("显示："))
        preview_top.addWidget(self.preview_mode)
        box_preview.addLayout(preview_top)
        self.preview_label = QLabel("等待开始监控……")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(320, 220)
        self.preview_label.setMaximumHeight(360)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preview_label.setScaledContents(False)
        self.preview_label.setStyleSheet("background:#101418; color:#aaa; border:1px solid #444; padding:4px;")
        box_preview.addWidget(self.preview_label)
        self.preview_info = QLabel("实时效果：未运行")
        self.preview_info.setStyleSheet("color:#555;")
        box_preview.addWidget(self.preview_info)
        self.preview_cb.stateChanged.connect(self.update_preview_visibility)
        self.preview_mode.currentIndexChanged.connect(self.refresh_preview)
        main_layout.addWidget(box_preview)

        # 5. 面板五：报警日志记录（可折叠）
        box_log = CollapsibleBox("四、 报警日志记录")
        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(120)
        box_log.addWidget(self.log_list)
        main_layout.addWidget(box_log)

        # 按钮槽函数连接
        self.select_btn.clicked.connect(self.select_region)
        self.select_shot_btn.clicked.connect(self.select_screenshot_region)
        self.clear_shot_btn.clicked.connect(self.clear_screenshot_region)
        self.screenshot_cb.stateChanged.connect(self.apply_screenshot_form)
        self.screenshot_interval_spin.valueChanged.connect(self.apply_screenshot_form)
        self.del_btn.clicked.connect(self.delete_region)
        self.test_btn.clicked.connect(self.test_alarm)
        self.clear_btn.clicked.connect(self.clear_cache)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.show_regions_cb.stateChanged.connect(self.toggle_region_overlay)
        self.select_screenshot_path_btn.clicked.connect(self.select_screenshot_path)

        # 配置管理：手动保存 / 恢复默认配置。
        box_config = CollapsibleBox("六、 配置管理")
        config_btns = QHBoxLayout()
        self.save_config_btn = QPushButton("保存配置")
        self.reset_config_btn = QPushButton("配置重置")
        config_btns.addWidget(self.save_config_btn)
        config_btns.addWidget(self.reset_config_btn)
        box_config.addLayout(config_btns)
        self.config_info = QLabel("程序运行中会自动保存；也可以手动保存。")
        self.config_info.setStyleSheet("color:#555;")
        box_config.addWidget(self.config_info)
        main_layout.addWidget(box_config)

        self.save_config_btn.clicked.connect(self.manual_save_config)
        self.reset_config_btn.clicked.connect(self.reset_all_config)

        main_layout.addStretch()

    def virtual_geometry(self):
        mon = self.sct.monitors[0]
        return QRect(mon["left"], mon["top"], mon["width"], mon["height"])

    def toggle_region_overlay(self):
        if self.show_regions_cb.isChecked():
            self.region_overlay.setGeometry(self.virtual_geometry())
            self.region_overlay.show()
            self.region_overlay.raise_()
        else:
            self.region_overlay.hide()
        self.region_overlay.update()

    def update_region_overlay(self):
        if self.show_regions_cb.isChecked():
            self.region_overlay.setGeometry(self.virtual_geometry())
            self.region_overlay.update()

    def screen_image(self):
        mon = self.sct.monitors[0]
        shot = np.array(self.sct.grab(mon))
        rgb = cv2.cvtColor(shot, cv2.COLOR_BGRA2RGB)
        h, w, _ = rgb.shape
        return QImage(rgb.data, w, h, 3*w, QImage.Format_RGB888).copy(), mon

    def select_region(self):
        try:
            image, mon = self.screen_image()
            self.selector = Selector(image, mon["left"], mon["top"])
            self.selector.selected.connect(self.add_region)
            self.selector.show()
            self.selector.raise_()
            self.selector.activateWindow()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def select_screenshot_region(self):
        try:
            image, mon = self.screen_image()
            self.screenshot_selector = ScreenshotSelector(image, mon["left"], mon["top"])
            self.screenshot_selector.selected.connect(self.set_screenshot_region)
            self.screenshot_selector.show()
            self.screenshot_selector.raise_()
            self.screenshot_selector.activateWindow()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def set_screenshot_region(self, rect):
        self.screenshot_rect = list(map(int, rect))
        x, y, w, h = self.screenshot_rect
        self.screenshot_region_label.setText(f"X={x}  Y={y}  W={w}  H={h}")
        self.save_screenshot_config()
        self.log_event("截图设置", f"已设置报警截图区域 {w}×{h}")

    def clear_screenshot_region(self):
        self.screenshot_rect = None
        self.screenshot_region_label.setText("尚未设置（未设置时不截图）")
        self.save_screenshot_config()

    def apply_screenshot_form(self):
        self.screenshot_enabled = self.screenshot_cb.isChecked()
        self.screenshot_interval = self.screenshot_interval_spin.value()
        self.save_screenshot_config()

    def save_screenshot_config(self):
        try:
            SCREENSHOT_CONFIG.write_text(json.dumps({
                "enabled": self.screenshot_enabled,
                "rect": self.screenshot_rect,
                "interval": self.screenshot_interval,
                "save_dir": str(self.screenshot_dir)
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_screenshot_config(self):
        try:
            if SCREENSHOT_CONFIG.exists():
                d = json.loads(SCREENSHOT_CONFIG.read_text(encoding="utf-8"))
                self.screenshot_enabled = bool(d.get("enabled", True))
                rect = d.get("rect")
                self.screenshot_rect = list(map(int, rect)) if isinstance(rect, list) and len(rect) == 4 else None
                self.screenshot_interval = max(1, int(d.get("interval", 5)))
                save_dir = d.get("save_dir")
                if save_dir:
                    self.screenshot_dir = Path(save_dir)
                    self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def update_screenshot_form(self):
        self.screenshot_cb.blockSignals(True)
        self.screenshot_interval_spin.blockSignals(True)
        self.screenshot_path_label.setText(str(self.screenshot_dir))
        self.screenshot_cb.setChecked(self.screenshot_enabled)
        self.screenshot_interval_spin.setValue(self.screenshot_interval)
        if self.screenshot_rect:
            x, y, w, h = self.screenshot_rect
            self.screenshot_region_label.setText(f"X={x}  Y={y}  W={w}  H={h}")
        else:
            self.screenshot_region_label.setText("尚未设置（未设置时不截图）")
        self.screenshot_cb.blockSignals(False)
        self.screenshot_interval_spin.blockSignals(False)

    def capture_alarm_screenshot(self, reason=""):
        """按独立截图区域保存报警截图；受截图间隔限制，不影响报警冷却。"""
        if not self.screenshot_enabled or not self.screenshot_rect:
            return None
        now = time.time()
        if now - self.last_screenshot_time < self.screenshot_interval:
            return None
        x, y, w, h = self.screenshot_rect
        if w < 20 or h < 20:
            return None
        try:
            shot = np.array(self.sct.grab({"left": x, "top": y, "width": w, "height": h}))
            frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            filename = self.screenshot_dir / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_报警.jpg")
            if cv2.imwrite(str(filename), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
                self.last_screenshot_time = now
                return filename
        except Exception:
            pass
        return None

    def select_screenshot_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择报警截图保存文件夹", str(self.screenshot_dir))
        if path:
            self.screenshot_dir = Path(path)
            try:
                self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            self.screenshot_path_label.setText(str(self.screenshot_dir))
            self.save_screenshot_config()
            self.log_event("截图设置", f"报警截图保存位置：{self.screenshot_dir}")

    def manual_save_config(self):
        self.apply_screenshot_form()
        self.auto_save_config()
        self.save_screenshot_config()
        self.config_info.setText(f"配置已保存：{datetime.now().strftime('%H:%M:%S')}")

    def reset_all_config(self):
        reply = QMessageBox.question(
            self, "确认重置",
            "确定要恢复全部配置为默认值吗？\n监控区域、截图区域和检测参数都会恢复默认。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self.running = False
        self.start_btn.setText("开始监控")
        self.pause_btn.setText("暂停")
        self.status.setText("状态：未监控")
        self.regions = []
        self.selected_index = -1
        self.screenshot_enabled = True
        self.screenshot_rect = None
        self.screenshot_interval = 5
        self.screenshot_dir = SNAP_DIR
        self.last_screenshot_time = 0.0
        self.refresh_combo()
        self.pos_label.setText("-")
        self.screenshot_region_label.setText("尚未设置（未设置时不截图）")
        self.update_screenshot_form()
        self.auto_save_config()
        self.save_screenshot_config()
        self.update_region_overlay()
        self.preview_label.clear()
        self.preview_label.setText("等待开始监控……")
        self.config_info.setText("已恢复默认配置。")

    def add_region(self, points):
        r = Region({
            "points": points,
            "name": f"四点区域 {len(self.regions)+1}"
        })
        self.regions.append(r)
        self.refresh_combo()
        self.region_combo.setCurrentIndex(len(self.regions) - 1)
        self.update_region_overlay()
        self.auto_save_config()

    def refresh_combo(self):
        self.region_combo.blockSignals(True)
        self.region_combo.clear()
        for i, r in enumerate(self.regions):
            text = f"{i+1}. {r.name} [{r.w}×{r.h}]"
            if not r.enabled:
                text += "（已停用）"
            self.region_combo.addItem(text)
        self.region_combo.blockSignals(False)

    def region_changed(self, idx):
        self.selected_index = idx
        if 0 <= idx < len(self.regions):
            r = self.regions[idx]
            self.pos_label.setText(f"范围: {r.w}×{r.h} (X={r.x}, Y={r.y})")
            
            widgets = [self.enable_cb, self.motion_cb, self.frame_diff_cb, self.mog2_cb,
                       self.shadow_cb, self.morph_cb, self.learn_cb, self.pixel_threshold,
                       self.min_blob, self.sens, self.ratio, self.noise, self.learn_rate,
                       self.confirm, self.cooldown, self.auto_pause_cb,
                       self.person_cb, self.person_conf_spin, self.person_interval_spin]
            for w in widgets:
                w.blockSignals(True)

            self.enable_cb.setChecked(r.enabled)
            self.motion_cb.setChecked(r.motion_enabled)
            self.frame_diff_cb.setChecked(r.frame_diff_enabled)
            self.mog2_cb.setChecked(r.mog2_enabled)
            self.shadow_cb.setChecked(r.shadow_filter)
            self.morph_cb.setChecked(r.morph_enabled)
            self.learn_cb.setChecked(r.background_learning)
            self.pixel_threshold.setValue(r.pixel_threshold)
            self.min_blob.setValue(r.min_blob_ratio)
            self.sens.setValue(r.sensitivity)
            self.ratio.setValue(r.min_ratio)
            self.noise.setValue(r.noise_filter)
            self.learn_rate.setValue(r.learn_speed)
            self.confirm.setValue(r.confirm)
            self.cooldown.setValue(r.cooldown)
            self.auto_pause_cb.setChecked(r.auto_pause)
            self.person_cb.setChecked(r.person_enabled)
            self.person_conf_spin.setValue(r.person_conf)
            self.person_interval_spin.setValue(r.person_interval)
            self.person_status.setText(
                "人员检测：已开启（模型将在监控时加载）" if r.person_enabled else "人员检测：未启用"
            )

            for w in widgets:
                w.blockSignals(False)

    def apply_form(self):
        i = self.selected_index
        if not (0 <= i < len(self.regions)):
            return
        r = self.regions[i]
        r.enabled = self.enable_cb.isChecked()
        r.motion_enabled = self.motion_cb.isChecked()
        r.frame_diff_enabled = self.frame_diff_cb.isChecked()
        r.mog2_enabled = self.mog2_cb.isChecked()
        r.shadow_filter = self.shadow_cb.isChecked()
        r.morph_enabled = self.morph_cb.isChecked()
        r.background_learning = self.learn_cb.isChecked()
        r.pixel_threshold = self.pixel_threshold.value()
        r.min_blob_ratio = self.min_blob.value()
        
        old_sens = r.sensitivity
        r.sensitivity = self.sens.value()
        r.min_ratio = self.ratio.value()
        r.noise_filter = self.noise.value()
        r.learn_speed = self.learn_rate.value()
        r.confirm = self.confirm.value()
        r.cooldown = self.cooldown.value()
        r.auto_pause = self.auto_pause_cb.isChecked()
        
        # 若灵敏度变更，重新初始化提取器
        if old_sens != r.sensitivity:
            r.init_subtractor()

        self.refresh_combo()
        self.region_combo.setCurrentIndex(i)
        self.update_region_overlay()
        self.auto_save_config()

    def delete_region(self):
        i = self.selected_index
        if 0 <= i < len(self.regions):
            del self.regions[i]
            self.refresh_combo()
            if self.regions:
                new_idx = max(0, i - 1)
                self.region_combo.setCurrentIndex(new_idx)
                self.region_changed(new_idx)
            else:
                self.selected_index = -1
                self.pos_label.setText("-")
            self.update_region_overlay()
            self.auto_save_config()

    def clear_cache(self):
        count = 0
        try:
            if SNAP_DIR.exists():
                for f in SNAP_DIR.glob("*.jpg"):
                    try:
                        f.unlink()
                        count += 1
                    except Exception:
                        pass
            log_file = APP_DIR / "crash_log.txt"
            if log_file.exists():
                try:
                    log_file.unlink()
                    count += 1
                except Exception:
                    pass
            QMessageBox.information(self, "清理完成", f"成功清理了 {count} 个缓存快照与临时文件。")
        except Exception as e:
            QMessageBox.critical(self, "清理失败", str(e))

    def toggle_monitor(self):
        self.running = not self.running
        if self.running:
            self.start_btn.setText("停止监控")
            self.status.setText("状态：正在监控屏幕")
            for r in self.regions:
                r.init_subtractor()
                r.confirm_count = 0
        else:
            self.start_btn.setText("开始监控")
            self.status.setText("状态：已停止")

    def toggle_pause(self):
        self.running = not self.running
        self.pause_btn.setText("暂停" if self.running else "继续")
        self.status.setText("状态：" + ("正在监控屏幕" if self.running else "已暂停"))

    def test_alarm(self):
        self.alarm.play()
        if self.regions and 0 <= self.selected_index < len(self.regions):
            self.regions[self.selected_index].last_event = time.time()
            self.update_region_overlay()
        self.log_event("测试报警", "手动触发测试报警")
        QMessageBox.information(self, "测试报警", "已触发测试报警声音。")

    def get_crop(self, r):
        mon = self.sct.monitors[0]
        x = r.x - mon["left"]
        y = r.y - mon["top"]
        if x < 0 or y < 0:
            return None
        try:
            img = np.array(self.sct.grab({
                "left": r.x, "top": r.y, "width": r.w, "height": r.h
            }))
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception:
            return None

    def ensure_person_model(self):
        """加载随工程打包的 YOLO 人员检测模型。"""
        if self.person_model is not None:
            return True
        if self.person_model_loading:
            return False
        self.person_model_loading = True
        try:
            if YOLO is None:
                raise RuntimeError("ultralytics 未正确安装")
            model_path = resource_path("yolo11n.pt")
            if not model_path.exists():
                raise FileNotFoundError(f"找不到人员检测模型：{model_path}")
            self.person_status.setText("人员检测：正在加载 YOLO 模型……")
            self.person_model = YOLO(str(model_path))
            self.person_status.setText("人员检测：模型已加载")
            self.person_model_error = ""
            return True
        except Exception as e:
            self.person_model_error = str(e)
            self.person_status.setText(f"人员检测失败：{str(e)[:60]}")
            self.person_model = None
            return False
        finally:
            self.person_model_loading = False

    def detect_person(self, frame, r):
        """
        独立人员检测。
        只识别 COCO 的 person 类别，不依赖画面变化面积。
        因此慢走、黑衣、背景颜色接近时，也有机会单独触发报警。
        """
        if not r.person_enabled:
            return False

        now = time.time()
        key = id(r)
        last = self.person_last_detect.get(key, 0.0)
        if now - last < max(0.2, r.person_interval):
            return bool(self.person_last_result.get(key, False))
        self.person_last_detect[key] = now

        if not self.ensure_person_model():
            self.person_last_result[key] = False
            return False

        try:
            # 适当降低输入尺寸，保证车机/普通 CPU 上不会过慢。
            results = self.person_model.predict(
                source=frame,
                conf=max(0.05, min(0.95, r.person_conf)),
                classes=[0],
                imgsz=640,
                verbose=False,
                device="cpu"
            )
            found = False
            person_boxes = []
            if results:
                boxes = results[0].boxes
                if boxes is not None and len(boxes) > 0:
                    poly = r.rel_points
                    xyxys = boxes.xyxy.cpu().numpy()
                    confs = boxes.conf.cpu().numpy() if boxes.conf is not None else np.zeros(len(xyxys))
                    for idx, xyxy in enumerate(xyxys):
                        x1, y1, x2, y2 = [int(v) for v in xyxy[:4]]
                        x1 = max(0, min(r.w - 1, x1))
                        y1 = max(0, min(r.h - 1, y1))
                        x2 = max(0, min(r.w - 1, x2))
                        y2 = max(0, min(r.h - 1, y2))
                        if x2 <= x1 or y2 <= y1:
                            continue

                        # 判断人员框与四点区域的重叠：只要人员框有明显部分进入区域即可。
                        box_mask = np.zeros((r.h, r.w), dtype=np.uint8)
                        cv2.rectangle(box_mask, (x1, y1), (x2, y2), 255, -1)
                        roi_mask = np.zeros((r.h, r.w), dtype=np.uint8)
                        cv2.fillPoly(roi_mask, [poly], 255)
                        inter = cv2.countNonZero(cv2.bitwise_and(box_mask, roi_mask))
                        box_area = max(1, cv2.countNonZero(box_mask))
                        conf = float(confs[idx]) if idx < len(confs) else 0.0
                        if inter / float(box_area) >= 0.05:
                            found = True
                        person_boxes.append((x1, y1, x2, y2, conf))

            r.preview_person_boxes = person_boxes
            self.person_last_result[key] = found
            self.person_status.setText(
                "人员检测：检测到人员" if found else "人员检测：运行中，未检测到人员"
            )
            return found
        except Exception as e:
            self.person_last_result[key] = False
            self.person_status.setText(f"人员识别异常：{str(e)[:60]}")
            return False

    def detect_event(self, frame, r):
        """可调试的画面变化检测。返回触发结果，并保存各阶段图像供实时预览。"""
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        roi_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(roi_mask, [r.rel_points], 255)
        raw_mask = np.zeros((h, w), dtype=np.uint8)
        masks = []

        threshold = max(1, int(r.pixel_threshold))

        if r.frame_diff_enabled and r.last_gray is not None:
            diff = cv2.absdiff(r.last_gray, gray)
            _, frame_mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
            masks.append(frame_mask)
        r.last_gray = gray.copy()

        if r.mog2_enabled:
            lr = r.learn_speed if r.background_learning else 0.0
            fg = r.subtractor.apply(frame, learningRate=lr)
            if r.shadow_filter:
                _, fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)
            else:
                _, fg = cv2.threshold(fg, 20, 255, cv2.THRESH_BINARY)
            masks.append(fg)

        if masks:
            # 两种检测方式取并集，避免单一算法漏掉慢走/黑衣等目标。
            raw_mask = masks[0].copy()
            for m in masks[1:]:
                raw_mask = cv2.bitwise_or(raw_mask, m)
            raw_mask = cv2.bitwise_and(raw_mask, roi_mask)

        processed = raw_mask.copy()
        if r.morph_enabled and np.any(processed):
            k_size = max(1, r.noise_filter * 2 - 1)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            processed = cv2.morphologyEx(processed, cv2.MORPH_OPEN, kernel)
            processed = cv2.morphologyEx(processed, cv2.MORPH_CLOSE, kernel)

        # 过滤过小连通块，保留真正有意义的变化区域。
        min_blob_px = max(4, int(w * h * r.min_blob_ratio / 100.0))
        filtered = np.zeros_like(processed)
        contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid_area = 0
        boxes = []
        for c in contours:
            area = cv2.contourArea(c)
            if area >= min_blob_px:
                cv2.drawContours(filtered, [c], -1, 255, -1)
                valid_area += area
                boxes.append(cv2.boundingRect(c))

        roi_pixels = max(1, cv2.countNonZero(roi_mask))
        ratio = valid_area / float(roi_pixels) * 100.0
        r.raw_ratio = ratio
        r.display_ratio = r.display_ratio * 0.72 + ratio * 0.28

        # 保存调试画面。
        r.preview_original = frame.copy()
        r.preview_raw = raw_mask.copy()
        r.preview_processed = filtered.copy()
        r.preview_boxes = boxes
        r.preview_motion = bool(r.motion_enabled and r.display_ratio >= r.min_ratio)
        return r.preview_motion if r.motion_enabled else False

    def make_preview(self, r, person_boxes=None):
        """生成实时识别效果图：原图/前景/去噪/人员框/处理后。"""
        if not hasattr(r, 'preview_original'):
            return None
        mode = self.preview_mode.currentIndex()
        original = r.preview_original.copy()
        if mode == 1:
            img = original
        elif mode == 2:
            img = cv2.cvtColor(r.preview_raw, cv2.COLOR_GRAY2BGR)
        elif mode == 3:
            img = cv2.cvtColor(r.preview_processed, cv2.COLOR_GRAY2BGR)
        else:
            img = original.copy()
            for x, y, w, h in getattr(r, 'preview_boxes', []):
                cv2.rectangle(img, (x, y), (x+w, y+h), (0, 255, 255), 2)
            for x1, y1, x2, y2, conf in (person_boxes or []):
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
                cv2.putText(img, f"PERSON {conf:.2f}", (x1, max(20, y1-8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)

        # 在预览图上标注当前检测状态。
        cv2.putText(img, f"change: {getattr(r, 'display_ratio', 0.0):.2f}% / {r.min_ratio:.2f}%",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 255), 2, cv2.LINE_AA)
        if getattr(r, 'preview_motion', False):
            cv2.putText(img, "MOTION", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
        if person_boxes:
            cv2.putText(img, f"PERSON: {len(person_boxes)}", (10, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
        return img

    def refresh_preview(self):
        if not self.preview_cb.isChecked():
            return
        if 0 <= self.selected_index < len(self.regions):
            r = self.regions[self.selected_index]
            boxes = getattr(r, 'preview_person_boxes', [])
            img = self.make_preview(r, boxes)
            if img is not None:
                self.set_preview_image(img)

    def set_preview_image(self, img):
        if img is None or not self.preview_cb.isChecked():
            return
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        qimg = QImage(rgb.data, w, h, 3*w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(qimg)
        self.preview_label.setPixmap(pix.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def update_preview_visibility(self):
        self.preview_label.setVisible(self.preview_cb.isChecked())
        self.preview_info.setVisible(self.preview_cb.isChecked())
        if self.preview_cb.isChecked():
            self.refresh_preview()

    def log_event(self, region_name, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_list.addItem(QListWidgetItem(f"[{stamp}] {region_name}: {msg}"))
        self.log_list.scrollToBottom()

    def trigger_alarm(self, r, frame, reason="检测到显著画面变动"):
        now = time.time()
        if now - r.last_event < r.cooldown:
            return False
        r.last_event = now

        # 报警截图使用单独框选的区域，而不是监控检测区域。
        screenshot_file = self.capture_alarm_screenshot(reason)

        self.alarm.play()
        stamp = datetime.now().strftime("%H:%M:%S")
        self.status.setText(f"状态：[{stamp}] {r.name} 触发报警！")
        self.log_event(r.name, reason)
        if screenshot_file:
            self.log_event(r.name, f"已保存报警截图：{screenshot_file}")
        self.update_region_overlay()

        if r.auto_pause:
            self.running = False
            self.start_btn.setText("开始监控")
            self.status.setText(f"状态：[{stamp}] {r.name} 触发报警（已自动暂停）")
        return True

    def tick(self):
        if not self.running:
            return

        for r in self.regions:
            if not r.enabled:
                continue

            frame = self.get_crop(r)
            if frame is None or frame.size == 0:
                continue

            motion_triggered = self.detect_event(frame, r) if r.motion_enabled else False
            person_triggered = self.detect_person(frame, r)
            r.preview_person_boxes = getattr(r, 'preview_person_boxes', [])

            is_selected = 0 <= self.selected_index < len(self.regions) and r is self.regions[self.selected_index]
            if is_selected and self.preview_cb.isChecked():
                    img = self.make_preview(r, r.preview_person_boxes)
                    self.set_preview_image(img)
                    self.preview_info.setText(
                        f"区域：{r.name} ｜ 画面变化：{r.display_ratio:.2f}% ｜ 阈值：{r.min_ratio:.2f}% ｜ "
                        f"人员：{'已检测到' if person_triggered else '未检测到'}"
                    )

            # 人员检测可以独立报警；画面变化仍然按原来的连续确认帧逻辑。
            if person_triggered:
                r.confirm_count += 1
            elif motion_triggered:
                r.confirm_count += 1
            else:
                r.confirm_count = 0

            # 人员检测开启时单独采用人员确认帧，默认可快速验证。
            needed = 1 if person_triggered else r.confirm
            if r.confirm_count >= needed:
                if person_triggered and motion_triggered:
                    reason = "检测到人员 + 画面显著变化"
                elif person_triggered:
                    reason = "检测到人员（YOLO）"
                else:
                    reason = "检测到显著画面变动"
                self.trigger_alarm(r, frame, reason)
                r.confirm_count = 0

    def auto_save_config(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps([r.to_dict() for r in self.regions], ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def auto_load_config(self):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.regions = [Region(x) for x in data]
            self.refresh_combo()
            if self.regions:
                self.region_combo.setCurrentIndex(0)
                self.region_changed(0)
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self.region_overlay.close()
        except Exception:
            pass
        self.auto_save_config()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
