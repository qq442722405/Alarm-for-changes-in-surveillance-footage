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
from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QIcon, QPolygon, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QComboBox,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QSpinBox, QDoubleSpinBox,
    QCheckBox, QMessageBox, QScrollArea, QToolButton, QListWidget, QListWidgetItem
)

APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
SNAP_DIR = APP_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)


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
        
        self.points = d.get("points", [])
        if not self.points and "x" in d:
            x, y, w, h = d.get("x", 0), d.get("y", 0), d.get("w", 300), d.get("h", 200)
            self.points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

        self.confirm_count = 0
        self.last_event = 0.0
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

        self._build_ui()
        self.auto_load_config()

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

        self.confirm = QSpinBox()
        self.confirm.setRange(1, 30)

        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setSuffix(" 秒")

        self.auto_pause_cb = QCheckBox("报警后自动暂停监控")

        form_params.addRow("", self.enable_cb)
        form_params.addRow("动静灵敏度", self.sens)
        form_params.addRow("触发变化面积", self.ratio)
        form_params.addRow("抗草木抖动等级", self.noise)
        form_params.addRow("光线适应速度", self.learn_rate)
        form_params.addRow("连续确认帧", self.confirm)
        form_params.addRow("报警冷却", self.cooldown)
        form_params.addRow("", self.auto_pause_cb)

        box_params.addLayout(form_params)
        main_layout.addWidget(box_params)

        # 绑定参数控制事件
        for w in [self.enable_cb, self.auto_pause_cb]:
            w.stateChanged.connect(self.apply_form)
        for w in [self.sens, self.ratio, self.noise, self.learn_rate, self.confirm, self.cooldown]:
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

        # 5. 面板四：报警日志记录（可折叠）
        box_log = CollapsibleBox("四、 报警日志记录")
        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(120)
        box_log.addWidget(self.log_list)
        main_layout.addWidget(box_log)

        # 按钮槽函数连接
        self.select_btn.clicked.connect(self.select_region)
        self.del_btn.clicked.connect(self.delete_region)
        self.test_btn.clicked.connect(self.test_alarm)
        self.clear_btn.clicked.connect(self.clear_cache)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.show_regions_cb.stateChanged.connect(self.toggle_region_overlay)

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
            
            widgets = [self.enable_cb, self.sens, self.ratio, self.noise,
                       self.learn_rate, self.confirm, self.cooldown, self.auto_pause_cb]
            for w in widgets:
                w.blockSignals(True)

            self.enable_cb.setChecked(r.enabled)
            self.sens.setValue(r.sensitivity)
            self.ratio.setValue(r.min_ratio)
            self.noise.setValue(r.noise_filter)
            self.learn_rate.setValue(r.learn_speed)
            self.confirm.setValue(r.confirm)
            self.cooldown.setValue(r.cooldown)
            self.auto_pause_cb.setChecked(r.auto_pause)

            for w in widgets:
                w.blockSignals(False)

    def apply_form(self):
        i = self.selected_index
        if not (0 <= i < len(self.regions)):
            return
        r = self.regions[i]
        r.enabled = self.enable_cb.isChecked()
        
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

    def detect_event(self, frame, r):
        """优化的混合高斯算法：抗草木晃动、日夜光线变动及阴影剔除"""
        # 1. MOG2 混合高斯背景提取（混合多模态背景，自动学习摇摆的草木）
        learning_rate = max(0.001, min(0.1, r.learn_speed))
        fg_mask = r.subtractor.apply(frame, learningRate=learning_rate)

        # 2. 剔除地面/画面动态阴影 (MOG2 中 127 为阴影，255 为真正前景)
        _, fg_mask = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

        # 3. 施加多边形区域掩膜
        mask = np.zeros((r.h, r.w), dtype=np.uint8)
        cv2.fillPoly(mask, [r.rel_points], 255)
        fg_mask = cv2.bitwise_and(fg_mask, fg_mask, mask=mask)

        # 4. 形态学开闭运算：消除高频风吹微小噪点，连接连贯实体目标
        k_size = max(1, r.noise_filter * 2 - 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k_size, k_size))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)

        # 5. 计算变化面积比例
        mask_pixels = np.count_nonzero(mask)
        if mask_pixels == 0:
            return False

        changed_pixels = np.count_nonzero(fg_mask)
        ratio = (changed_pixels / float(mask_pixels)) * 100.0

        return ratio >= r.min_ratio

    def log_event(self, region_name, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_list.addItem(QListWidgetItem(f"[{stamp}] {region_name}: {msg}"))
        self.log_list.scrollToBottom()

    def trigger_alarm(self, r, frame):
        now = time.time()
        if now - r.last_event < r.cooldown:
            return False
        r.last_event = now

        try:
            filename = SNAP_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg")
            cv2.imwrite(str(filename), frame)
        except Exception:
            pass

        self.alarm.play()
        stamp = datetime.now().strftime("%H:%M:%S")
        self.status.setText(f"状态：[{stamp}] {r.name} 触发报警！")
        self.log_event(r.name, "检测到显著画面变动")
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

            triggered = self.detect_event(frame, r)

            if triggered:
                r.confirm_count += 1
            else:
                r.confirm_count = 0

            if r.confirm_count >= r.confirm:
                self.trigger_alarm(r, frame)
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
