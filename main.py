import sys, json, time, os, traceback, ctypes
from pathlib import Path
from datetime import datetime

# ==================== 1. 静态资源与路径处理 ====================
def resource_path(relative_path):
    """获取静态资源的绝对路径，兼容 PyInstaller 单文件打包模式"""
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
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QMessageBox
)

APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
SNAP_DIR = APP_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)


class Alarm(QObject):
    def play(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            QApplication.beep()


class Selector(QWidget):
    """四点位多边形框选窗口"""
    selected = Signal(list)  # 发送 4 个点位的屏幕绝对坐标 [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]

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

        # 绘制已点击的点位与连线
        if len(self.points) > 0:
            p.setPen(QPen(Qt.green, 3))
            p.setBrush(Qt.green)
            for pt in self.points:
                p.drawEllipse(QPoint(pt[0] - self.left, pt[1] - self.top), 4, 4)

            poly = QPolygon()
            for pt in self.points:
                poly.append(QPoint(pt[0] - self.left, pt[1] - self.top))

            if len(self.points) > 1:
                p.setPen(QPen(Qt.green, 2, Qt.SolidLine))
                for i in range(len(self.points) - 1):
                    p.drawLine(poly[i], poly[i+1])

            # 绘制跟随鼠标的虚线
            p.setPen(QPen(Qt.yellow, 2, Qt.DashLine))
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
    """在桌面上实时渲染已框选的四点位边框"""
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
        now = time.time()

        for i, r in enumerate(self.owner.regions):
            if not r.enabled or len(r.points) < 4:
                continue

            poly = QPolygon()
            for pt in r.points:
                poly.append(QPoint(pt[0] - mon['left'], pt[1] - mon['top']))

            # 只显示边框：默认绿色，报警后 2 秒内保持红色
            is_alarm = (now - r.last_event) < 2.0
            color = Qt.red if is_alarm else Qt.green

            p.setBrush(Qt.NoBrush)          # 无颜色填充，仅保留外框
            p.setPen(QPen(color, 3))        # 边框粗细 3px
            p.drawPolygon(poly)

            # 绘制区域名字与序号
            top_pt = min([QPoint(pt[0] - mon['left'], pt[1] - mon['top']) for pt in r.points], key=lambda pt: pt.y())
            p.setPen(QPen(color, 1))
            p.drawText(top_pt + QPoint(6, -6 if top_pt.y() > 20 else 20), f"{i+1}. {r.name}")


class Region:
    def __init__(self, data=None):
        d = data or {}
        self.name = d.get("name", "监控区域")
        self.enabled = bool(d.get("enabled", True))
        self.motion = bool(d.get("motion", True))
        
        self.sensitivity = int(d.get("sensitivity", 100))
        self.min_ratio = float(d.get("min_ratio", 4.0))
        
        self.confirm = int(d.get("confirm", 3))
        self.cooldown = int(d.get("cooldown", 10))
        self.auto_pause = bool(d.get("auto_pause", False))
        
        self.points = d.get("points", [])
        if not self.points and "x" in d:
            x, y, w, h = d.get("x", 0), d.get("y", 0), d.get("w", 300), d.get("h", 200)
            self.points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

        self.last_lab = None
        self.confirm_count = 0
        self.last_event = 0.0
        self._update_bounds()

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

    def to_dict(self):
        return {
            "name": self.name,
            "enabled": self.enabled,
            "motion": self.motion,
            "sensitivity": self.sensitivity,
            "min_ratio": self.min_ratio,
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

        self.resize(520, 620)
        self.regions = []
        self.running = False
        self.selected_index = -1
        self.sct = mss.mss()
        self.alarm = Alarm()
        self.region_overlay = RegionOverlay(self)
        self._build_ui()
        self.auto_load_config()

        # 默认开启“显示框选区域”
        self.show_regions_cb.setChecked(True)
        self.toggle_region_overlay()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # 标题与说明
        title = QLabel("监控变化报警")
        title.setStyleSheet("font-size:20px; font-weight:bold;")
        main_layout.addWidget(title)

        tip = QLabel("说明：点击“四点框选屏幕”在屏幕上顺次点击 4 个点建立监控区域。")
        tip.setWordWrap(True)
        main_layout.addWidget(tip)

        # 顶部操作按键区
        btn_layout_1 = QHBoxLayout()
        self.select_btn = QPushButton("四点框选屏幕")
        self.del_btn = QPushButton("删除区域")
        self.test_btn = QPushButton("测试报警")
        self.clear_btn = QPushButton("清理缓存")
        btn_layout_1.addWidget(self.select_btn)
        btn_layout_1.addWidget(self.del_btn)
        btn_layout_1.addWidget(self.test_btn)
        btn_layout_1.addWidget(self.clear_btn)
        main_layout.addLayout(btn_layout_1)

        btn_layout_2 = QHBoxLayout()
        self.start_btn = QPushButton("开始监控")
        self.pause_btn = QPushButton("暂停")
        self.show_regions_cb = QCheckBox("显示框选区域")
        btn_layout_2.addWidget(self.start_btn)
        btn_layout_2.addWidget(self.pause_btn)
        btn_layout_2.addWidget(self.show_regions_cb)
        main_layout.addLayout(btn_layout_2)

        self.select_btn.clicked.connect(self.select_region)
        self.del_btn.clicked.connect(self.delete_region)
        self.test_btn.clicked.connect(self.test_alarm)
        self.clear_btn.clicked.connect(self.clear_cache)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.show_regions_cb.stateChanged.connect(self.toggle_region_overlay)

        # 当前区域参数设置面板
        self.form_box = QGroupBox("当前区域参数设置")
        form = QFormLayout(self.form_box)

        self.region_combo = QComboBox()
        self.region_combo.currentIndexChanged.connect(self.region_changed)
        form.addRow("切换监控区域", self.region_combo)

        self.pos_label = QLabel("-")
        form.addRow("多边形范围", self.pos_label)

        self.enable_cb = QCheckBox("启用该区域")
        self.motion_cb = QCheckBox("检测画面变化")
        self.auto_pause_cb = QCheckBox("报警后自动暂停")

        self.sens = QSpinBox()
        self.sens.setRange(0, 100)
        self.sens.setSuffix(" %")

        self.ratio = QDoubleSpinBox()
        self.ratio.setRange(0.1, 50)
        self.ratio.setSingleStep(0.1)
        self.ratio.setSuffix(" %")

        self.confirm = QSpinBox()
        self.confirm.setRange(1, 30)

        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setSuffix(" 秒")

        form.addRow("", self.enable_cb)
        form.addRow("", self.motion_cb)
        form.addRow("灵敏度", self.sens)
        form.addRow("最小变化面积", self.ratio)
        form.addRow("连续确认帧", self.confirm)
        form.addRow("报警冷却", self.cooldown)
        form.addRow("", self.auto_pause_cb)

        for w in [self.enable_cb, self.motion_cb, self.auto_pause_cb, 
                  self.sens, self.ratio, self.confirm, self.cooldown]:
            if isinstance(w, QCheckBox):
                w.stateChanged.connect(self.apply_form)
            else:
                w.valueChanged.connect(self.apply_form)

        main_layout.addWidget(self.form_box)

        # 底部状态栏
        self.status = QLabel("状态：未监控")
        self.status.setStyleSheet("font-size:15px; font-weight:bold; color: #2c3e50;")
        main_layout.addWidget(self.status)
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
            
            self.enable_cb.blockSignals(True)
            self.motion_cb.blockSignals(True)
            self.sens.blockSignals(True)
            self.ratio.blockSignals(True)
            self.confirm.blockSignals(True)
            self.cooldown.blockSignals(True)
            self.auto_pause_cb.blockSignals(True)

            self.enable_cb.setChecked(r.enabled)
            self.motion_cb.setChecked(r.motion)
            self.sens.setValue(r.sensitivity)
            self.ratio.setValue(r.min_ratio)
            self.confirm.setValue(r.confirm)
            self.cooldown.setValue(r.cooldown)
            self.auto_pause_cb.setChecked(r.auto_pause)

            self.enable_cb.blockSignals(False)
            self.motion_cb.blockSignals(False)
            self.sens.blockSignals(False)
            self.ratio.blockSignals(False)
            self.confirm.blockSignals(False)
            self.cooldown.blockSignals(False)
            self.auto_pause_cb.blockSignals(False)

    def apply_form(self):
        i = self.selected_index
        if not (0 <= i < len(self.regions)):
            return
        r = self.regions[i]
        r.enabled = self.enable_cb.isChecked()
        r.motion = self.motion_cb.isChecked()
        r.sensitivity = self.sens.value()
        r.min_ratio = self.ratio.value()
        r.confirm = self.confirm.value()
        r.cooldown = self.cooldown.value()
        r.auto_pause = self.auto_pause_cb.isChecked()
        
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
                r.last_lab = None
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
        QMessageBox.information(self, "测试报警", "已触发测试报警声音并高亮变红。")

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

    def detect_motion(self, frame, r):
        if not r.motion:
            return False
            
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2Lab)
        lab = cv2.GaussianBlur(lab, (7, 7), 0)
        
        if r.last_lab is None or r.last_lab.shape != lab.shape:
            r.last_lab = lab
            return False

        diff = cv2.absdiff(r.last_lab, lab)
        diff_max = np.max(diff, axis=2)
        r.last_lab = lab

        mask = np.zeros((r.h, r.w), dtype=np.uint8)
        cv2.fillPoly(mask, [r.rel_points], 255)

        # 内缩 Mask 避开外侧 9px，隔离红绿边框切换
        kernel_erode = np.ones((9, 9), np.uint8)
        mask_eroded = cv2.erode(mask, kernel_erode)
        if np.count_nonzero(mask_eroded) > 0:
            mask = mask_eroded

        diff_masked = cv2.bitwise_and(diff_max, diff_max, mask=mask)

        threshold = max(10, int(45 - r.sensitivity * 0.35))
        _, th = cv2.threshold(diff_masked, threshold, 255, cv2.THRESH_BINARY)
        
        kernel_morph = np.ones((3, 3), np.uint8)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel_morph)

        mask_pixels = np.count_nonzero(mask)
        if mask_pixels == 0:
            return False

        ratio = (np.count_nonzero(th) / float(mask_pixels)) * 100.0
        return ratio >= r.min_ratio

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
        self.update_region_overlay()

        if r.auto_pause:
            self.running = False
            self.start_btn.setText("开始监控")
            self.status.setText(f"状态：[{stamp}] {r.name} 触发报警（已自动暂停）")
        return True

    def tick(self):
        if not self.running:
            return
        
        now = time.time()
        need_overlay_refresh = False

        for r in self.regions:
            if not r.enabled:
                continue

            if now - r.last_event < 2.5:
                need_overlay_refresh = True

            frame = self.get_crop(r)
            if frame is None or frame.size == 0:
                continue

            motion = self.detect_motion(frame, r)

            if motion:
                r.confirm_count += 1
            else:
                r.confirm_count = 0

            if r.confirm_count >= r.confirm:
                self.trigger_alarm(r, frame)
                r.confirm_count = 0

        if need_overlay_refresh:
            self.update_region_overlay()

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
