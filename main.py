import sys
import json
import time
import csv
import traceback
import ctypes
import threading
from pathlib import Path
from datetime import datetime


def resource_path(relative_path: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent / relative_path


APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
EVENT_FILE = APP_DIR / "events.csv"
SNAP_DIR = APP_DIR / "snapshots"
SNAP_DIR.mkdir(exist_ok=True)


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    try:
        (APP_DIR / "crash_log.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "程序发生异常。\n\n" + text[:1200] + f"\n\n日志：{APP_DIR / 'crash_log.txt'}",
            "监控变化报警 - 错误",
            0x10,
        )
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = handle_exception

import cv2
import mss
import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal, QObject, QSize
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QIcon, QPolygon, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QPushButton,
    QLabel,
    QComboBox,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QMessageBox,
    QProgressBar,
    QListWidget,
    QListWidgetItem,
    QFrame,
    QScrollArea,
)

try:
    from ultralytics import YOLO
except Exception:
    YOLO = None


class Alarm(QObject):
    def play(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            QApplication.beep()


class Selector(QWidget):
    selected = Signal(list)

    def __init__(self, image, left, top):
        super().__init__()
        self.left = left
        self.top = top
        self.pixmap = QPixmap.fromImage(image)
        self.points = []
        self.current = QPoint()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setGeometry(left, top, image.width(), image.height())
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        # 只加很轻的遮罩，保证底下监控画面清楚可见
        p.fillRect(self.rect(), QColor(0, 0, 0, 28))
        p.setRenderHint(QPainter.Antialiasing)
        if self.points:
            poly = QPolygon([QPoint(x - self.left, y - self.top) for x, y in self.points])
            p.setPen(QPen(QColor("#27d3ff"), 3))
            p.setBrush(QColor(39, 211, 255, 35))
            p.drawPolygon(poly)
            for i, (x, y) in enumerate(self.points):
                q = QPoint(x - self.left, y - self.top)
                p.setBrush(QColor("#27d3ff"))
                p.drawEllipse(q, 6, 6)
                p.setPen(Qt.white)
                p.drawText(q + QPoint(9, -9), str(i + 1))
            p.setPen(QPen(QColor("#ffe66d"), 2, Qt.DashLine))
            p.drawLine(poly[-1], self.current)
        p.setPen(Qt.white)
        p.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        p.drawText(30, 42, "四点框选监控区域")
        p.setFont(QFont("Microsoft YaHei", 10))
        p.drawText(30, 68, f"左键依次点击 4 个点（{len(self.points)}/4） · 右键重置 · ESC 取消")

    def mouseMoveEvent(self, e):
        self.current = e.position().toPoint()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            pos = e.position().toPoint()
            self.points.append([self.left + pos.x(), self.top + pos.y()])
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
            poly = QPolygon([QPoint(x - mon["left"], y - mon["top"]) for x, y in r.points])
            alarm = now - r.last_event < 2.2
            person = r.person_present
            color = QColor("#ff4d67") if alarm else QColor("#27d3ff")
            if person and not alarm:
                color = QColor("#ffb020")
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 3))
            p.drawPolygon(poly)
            top = min(poly, key=lambda q: q.y())
            p.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
            label = f"{i + 1}. {r.name}"
            if person:
                label += "  人员"
            label += f"  变化 {r.display_ratio:.2f}%"
            p.setPen(color)
            p.drawText(top + QPoint(7, -7 if top.y() > 22 else 20), label)


class Region:
    def __init__(self, data=None):
        d = data or {}
        self.name = d.get("name", "监控区域")
        self.enabled = bool(d.get("enabled", True))
        self.motion = bool(d.get("motion", True))
        self.person = bool(d.get("person", True))
        self.sensitivity = int(d.get("sensitivity", 70))
        self.min_ratio = float(d.get("min_ratio", 0.35))
        self.min_blob_ratio = float(d.get("min_blob_ratio", 0.12))
        self.confirm = int(d.get("confirm", 2))
        self.cooldown = int(d.get("cooldown", 8))
        self.auto_pause = bool(d.get("auto_pause", False))
        self.person_conf = float(d.get("person_conf", 0.35))
        self.person_confirm = int(d.get("person_confirm", 2))
        self.person_interval = int(d.get("person_interval", 2))
        self.points = d.get("points", [])
        if not self.points and "x" in d:
            x, y = int(d.get("x", 0)), int(d.get("y", 0))
            w, h = int(d.get("w", 300)), int(d.get("h", 200))
            self.points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        self.last_frame = None
        self.background = None
        self.confirm_count = 0
        self.person_count = 0
        self.person_present = False
        self.last_person_check = 0
        self.last_event = 0.0
        self.display_ratio = 0.0
        self.raw_ratio = 0.0
        self.last_reason = ""
        self._update_bounds()

    def _update_bounds(self):
        if len(self.points) >= 4:
            xs = [int(p[0]) for p in self.points]
            ys = [int(p[1]) for p in self.points]
            self.x, self.y = min(xs), min(ys)
            self.w = max(10, max(xs) - self.x)
            self.h = max(10, max(ys) - self.y)
            self.rel_points = np.array([[x - self.x, y - self.y] for x, y in self.points], np.int32)
        else:
            self.x, self.y, self.w, self.h = 0, 0, 100, 100
            self.rel_points = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], np.int32)

    def reset_detector(self):
        self.last_frame = None
        self.background = None
        self.confirm_count = 0
        self.person_count = 0
        self.person_present = False
        self.display_ratio = 0.0
        self.raw_ratio = 0.0

    def to_dict(self):
        return {
            "name": self.name,
            "enabled": self.enabled,
            "motion": self.motion,
            "person": self.person,
            "sensitivity": self.sensitivity,
            "min_ratio": self.min_ratio,
            "min_blob_ratio": self.min_blob_ratio,
            "confirm": self.confirm,
            "cooldown": self.cooldown,
            "auto_pause": self.auto_pause,
            "person_conf": self.person_conf,
            "person_confirm": self.person_confirm,
            "person_interval": self.person_interval,
            "points": self.points,
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("监控变化报警")
        icon = resource_path("app.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(760, 760)
        self.regions = []
        self.events = []
        self.running = False
        self.paused = False
        self.selected_index = -1
        self.sct = mss.mss()
        self.alarm = Alarm()
        self.model = None
        self.model_loading = False
        self.model_error = ""
        self.frame_counter = 0
        self._build_ui()
        # 区域显示层必须在首次调用 toggle_region_overlay() 之前创建，
        # 否则启动时会出现 AttributeError: region_overlay。
        self.region_overlay = RegionOverlay(self)
        self.auto_load_config()
        self.show_regions_cb.setChecked(True)
        self.toggle_region_overlay()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(220)

    def _build_ui(self):
        self.setStyleSheet("""
        QWidget { font-family: 'Microsoft YaHei'; font-size: 10pt; }
        QMainWindow, QWidget { background: #11161d; color: #e9eef5; }
        QGroupBox { border: 1px solid #27313d; border-radius: 12px; margin-top: 12px; padding: 14px; background: #171e27; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 7px; color: #8fdfff; }
        QPushButton { background: #202a35; border: 1px solid #344252; border-radius: 9px; padding: 9px 13px; color: #eef5fb; }
        QPushButton:hover { background: #293744; }
        QPushButton:pressed { background: #16202a; }
        QComboBox, QSpinBox, QDoubleSpinBox { background: #10161d; border: 1px solid #33404e; border-radius: 7px; padding: 6px; color: #eef5fb; }
        QCheckBox { spacing: 8px; }
        QListWidget { background: #10161d; border: 1px solid #27313d; border-radius: 9px; padding: 5px; }
        QListWidget::item { padding: 8px; border-radius: 7px; }
        QListWidget::item:selected { background: #173d4a; }
        QProgressBar { border: 1px solid #33404e; border-radius: 6px; background: #0e1319; height: 13px; text-align: center; }
        QProgressBar::chunk { background: #27d3ff; border-radius: 5px; }
        """)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("监控变化报警")
        title.setStyleSheet("font-size: 24px; font-weight: 800; color: #f4f8fc;")
        subtitle = QLabel("屏幕区域智能监控 · 变化过滤 · 人员检测")
        subtitle.setStyleSheet("color:#8b9aaa;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        head.addLayout(title_box)
        head.addStretch()
        self.status_dot = QLabel("● 已停止")
        self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#8794a3;")
        head.addWidget(self.status_dot)
        root.addLayout(head)

        actions = QHBoxLayout()
        self.select_btn = QPushButton("＋ 四点框选屏幕")
        self.start_btn = QPushButton("▶ 开始监控")
        self.pause_btn = QPushButton("Ⅱ 暂停")
        self.test_btn = QPushButton("🔔 测试报警")
        self.reset_btn = QPushButton("↻ 重置检测基准")
        for b in [self.select_btn, self.start_btn, self.pause_btn, self.test_btn, self.reset_btn]:
            actions.addWidget(b)
        root.addLayout(actions)

        display_row = QHBoxLayout()
        self.show_regions_cb = QCheckBox("显示框选区域")
        self.show_regions_cb.setStyleSheet("color:#8fdfff; font-weight:bold;")
        display_row.addWidget(self.show_regions_cb)
        display_row.addStretch()
        self.model_status = QLabel("人员模型：未加载")
        self.model_status.setStyleSheet("color:#8b9aaa;")
        display_row.addWidget(self.model_status)
        root.addLayout(display_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        left_box = QGroupBox("监控区域")
        left_layout = QVBoxLayout(left_box)
        self.region_combo = QComboBox()
        left_layout.addWidget(self.region_combo)
        self.region_list = QListWidget()
        left_layout.addWidget(self.region_list, 1)
        del_btn = QPushButton("删除当前区域")
        left_layout.addWidget(del_btn)
        body.addWidget(left_box, 2)

        right_col = QVBoxLayout()
        body.addLayout(right_col, 3)

        self.param_box = QGroupBox("检测参数")
        form = QFormLayout(self.param_box)
        form.setSpacing(9)
        self.pos_label = QLabel("-")
        self.enable_cb = QCheckBox("启用该区域")
        self.motion_cb = QCheckBox("检测画面变化")
        self.person_cb = QCheckBox("检测人员（半身进入也尽量识别）")
        self.auto_pause_cb = QCheckBox("报警后自动暂停")
        self.sens = QSpinBox(); self.sens.setRange(0, 100); self.sens.setSuffix(" %")
        self.ratio = QDoubleSpinBox(); self.ratio.setRange(0.05, 30); self.ratio.setSingleStep(0.05); self.ratio.setSuffix(" %")
        self.blob = QDoubleSpinBox(); self.blob.setRange(0.01, 10); self.blob.setSingleStep(0.01); self.blob.setSuffix(" %")
        self.confirm = QSpinBox(); self.confirm.setRange(1, 20); self.confirm.setSuffix(" 帧")
        self.cooldown = QSpinBox(); self.cooldown.setRange(0, 3600); self.cooldown.setSuffix(" 秒")
        self.person_conf = QDoubleSpinBox(); self.person_conf.setRange(0.10, 0.90); self.person_conf.setSingleStep(0.05)
        self.person_confirm = QSpinBox(); self.person_confirm.setRange(1, 10); self.person_confirm.setSuffix(" 次")
        self.person_interval = QSpinBox(); self.person_interval.setRange(1, 10); self.person_interval.setSuffix(" 帧")
        form.addRow("范围", self.pos_label)
        form.addRow("", self.enable_cb)
        form.addRow("", self.motion_cb)
        form.addRow("", self.person_cb)
        form.addRow("画面灵敏度", self.sens)
        form.addRow("变化触发面积", self.ratio)
        form.addRow("有效大块变化", self.blob)
        form.addRow("变化确认", self.confirm)
        form.addRow("报警冷却", self.cooldown)
        form.addRow("人员置信度", self.person_conf)
        form.addRow("人员确认", self.person_confirm)
        form.addRow("人员检测间隔", self.person_interval)
        form.addRow("", self.auto_pause_cb)
        right_col.addWidget(self.param_box)

        live_box = QGroupBox("实时检测")
        live = QFormLayout(live_box)
        self.raw_label = QLabel("0.00 %")
        self.smooth_label = QLabel("0.00 %")
        self.threshold_label = QLabel("≥ 0.35 %")
        self.person_label = QLabel("未检测")
        self.reason_label = QLabel("-")
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0)
        live.addRow("原始变化", self.raw_label)
        live.addRow("平滑变化", self.smooth_label)
        live.addRow("触发阈值", self.threshold_label)
        live.addRow("变化强度", self.progress)
        live.addRow("人员状态", self.person_label)
        live.addRow("最近事件", self.reason_label)
        right_col.addWidget(live_box)

        event_box = QGroupBox("报警事件")
        event_layout = QVBoxLayout(event_box)
        self.event_list = QListWidget()
        event_layout.addWidget(self.event_list)
        export_btn = QPushButton("导出事件 CSV")
        clear_btn = QPushButton("清理截图缓存")
        row = QHBoxLayout(); row.addWidget(export_btn); row.addWidget(clear_btn)
        event_layout.addLayout(row)
        right_col.addWidget(event_box, 1)

        self.select_btn.clicked.connect(self.select_region)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.test_btn.clicked.connect(self.test_alarm)
        self.reset_btn.clicked.connect(self.reset_selected)
        self.show_regions_cb.stateChanged.connect(self.toggle_region_overlay)
        del_btn.clicked.connect(self.delete_region)
        export_btn.clicked.connect(self.export_csv)
        clear_btn.clicked.connect(self.clear_cache)
        self.region_combo.currentIndexChanged.connect(self.region_changed)
        self.region_list.currentRowChanged.connect(self.region_list_changed)
        for w in [self.enable_cb, self.motion_cb, self.person_cb, self.auto_pause_cb]:
            w.stateChanged.connect(self.apply_form)
        for w in [self.sens, self.ratio, self.blob, self.confirm, self.cooldown, self.person_conf, self.person_confirm, self.person_interval]:
            w.valueChanged.connect(self.apply_form)

    def virtual_geometry(self):
        m = self.sct.monitors[0]
        return QRect(m["left"], m["top"], m["width"], m["height"])

    def screen_image(self):
        mon = self.sct.monitors[0]
        shot = np.array(self.sct.grab(mon))
        rgb = cv2.cvtColor(shot, cv2.COLOR_BGRA2RGB)
        h, w, _ = rgb.shape
        return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy(), mon

    def select_region(self):
        try:
            image, mon = self.screen_image()
            self.selector = Selector(image, mon["left"], mon["top"])
            self.selector.selected.connect(self.add_region)
            self.selector.show(); self.selector.raise_(); self.selector.activateWindow()
        except Exception as e:
            QMessageBox.critical(self, "框选失败", str(e))

    def add_region(self, points):
        r = Region({"points": points, "name": f"监控区域 {len(self.regions) + 1}"})
        self.regions.append(r)
        self.refresh_region_ui()
        self.region_combo.setCurrentIndex(len(self.regions) - 1)
        self.auto_save_config()
        self.update_region_overlay()

    def refresh_region_ui(self):
        current = self.selected_index
        self.region_combo.blockSignals(True); self.region_list.blockSignals(True)
        self.region_combo.clear(); self.region_list.clear()
        for i, r in enumerate(self.regions):
            text = f"{i+1}. {r.name}  [{r.w}×{r.h}]"
            if not r.enabled: text += " · 停用"
            self.region_combo.addItem(text)
            self.region_list.addItem(QListWidgetItem(text))
        self.region_combo.blockSignals(False); self.region_list.blockSignals(False)
        if self.regions:
            idx = max(0, min(current if current >= 0 else 0, len(self.regions) - 1))
            self.region_combo.setCurrentIndex(idx); self.region_list.setCurrentRow(idx)
            self.region_changed(idx)

    def region_list_changed(self, idx):
        if idx >= 0 and idx != self.region_combo.currentIndex():
            self.region_combo.setCurrentIndex(idx)

    def region_changed(self, idx):
        self.selected_index = idx
        if not (0 <= idx < len(self.regions)):
            self.pos_label.setText("-")
            return
        r = self.regions[idx]
        self.pos_label.setText(f"X={r.x}  Y={r.y}  {r.w}×{r.h}")
        widgets = [self.enable_cb, self.motion_cb, self.person_cb, self.auto_pause_cb, self.sens, self.ratio, self.blob, self.confirm, self.cooldown, self.person_conf, self.person_confirm, self.person_interval]
        for w in widgets: w.blockSignals(True)
        self.enable_cb.setChecked(r.enabled); self.motion_cb.setChecked(r.motion); self.person_cb.setChecked(r.person); self.auto_pause_cb.setChecked(r.auto_pause)
        self.sens.setValue(r.sensitivity); self.ratio.setValue(r.min_ratio); self.blob.setValue(r.min_blob_ratio); self.confirm.setValue(r.confirm); self.cooldown.setValue(r.cooldown)
        self.person_conf.setValue(r.person_conf); self.person_confirm.setValue(r.person_confirm); self.person_interval.setValue(r.person_interval)
        for w in widgets: w.blockSignals(False)
        self.update_live_labels(r)

    def apply_form(self):
        i = self.selected_index
        if not (0 <= i < len(self.regions)): return
        r = self.regions[i]
        r.enabled = self.enable_cb.isChecked(); r.motion = self.motion_cb.isChecked(); r.person = self.person_cb.isChecked(); r.auto_pause = self.auto_pause_cb.isChecked()
        r.sensitivity = self.sens.value(); r.min_ratio = self.ratio.value(); r.min_blob_ratio = self.blob.value(); r.confirm = self.confirm.value(); r.cooldown = self.cooldown.value()
        r.person_conf = self.person_conf.value(); r.person_confirm = self.person_confirm.value(); r.person_interval = self.person_interval.value()
        self.refresh_region_ui(); self.region_combo.setCurrentIndex(i); self.region_list.setCurrentRow(i); self.auto_save_config(); self.update_region_overlay()

    def delete_region(self):
        i = self.selected_index
        if 0 <= i < len(self.regions):
            del self.regions[i]
            self.selected_index = -1
            self.refresh_region_ui(); self.auto_save_config(); self.update_region_overlay()

    def reset_selected(self):
        if 0 <= self.selected_index < len(self.regions):
            self.regions[self.selected_index].reset_detector()
            self.update_live_labels(self.regions[self.selected_index])
            self.status_dot.setText("● 检测基准已重置")
            self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#27d3ff;")

    def toggle_region_overlay(self):
        if self.show_regions_cb.isChecked():
            self.region_overlay.setGeometry(self.virtual_geometry()); self.region_overlay.show(); self.region_overlay.raise_()
        else:
            self.region_overlay.hide()
        self.region_overlay.update()

    def update_region_overlay(self):
        if self.show_regions_cb.isChecked():
            self.region_overlay.setGeometry(self.virtual_geometry()); self.region_overlay.update()

    def toggle_monitor(self):
        if self.running:
            self.running = False; self.paused = False; self.start_btn.setText("▶ 开始监控"); self.pause_btn.setText("Ⅱ 暂停")
            self.status_dot.setText("● 已停止"); self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#8794a3;")
        else:
            if not self.regions:
                QMessageBox.warning(self, "没有监控区域", "请先框选至少一个监控区域。")
                return
            self.running = True; self.paused = False; self.start_btn.setText("■ 停止监控")
            self.pause_btn.setText("Ⅱ 暂停")
            for r in self.regions: r.reset_detector()
            self.status_dot.setText("● 正在监控"); self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#35e68a;")
            if any(r.person and r.enabled for r in self.regions): self.ensure_model()

    def toggle_pause(self):
        if not self.running: return
        self.paused = not self.paused
        self.pause_btn.setText("▶ 继续" if self.paused else "Ⅱ 暂停")
        if self.paused:
            self.status_dot.setText("● 已暂停"); self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#ffb020;")
        else:
            self.status_dot.setText("● 正在监控"); self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#35e68a;")

    def test_alarm(self):
        self.alarm.play()
        if 0 <= self.selected_index < len(self.regions):
            self.regions[self.selected_index].last_event = time.time(); self.update_region_overlay()

    def ensure_model(self):
        if YOLO is None or self.model is not None or self.model_loading: return
        self.model_loading = True
        self.model_status.setText("人员模型：正在加载/首次运行可能下载模型…")
        def load():
            try:
                self.model = YOLO("yolo11n.pt")
                self.model_status.setText("人员模型：已加载")
            except Exception as e:
                self.model_error = str(e)
                self.model_status.setText("人员模型：加载失败（变化检测仍可用）")
            finally:
                self.model_loading = False
        threading.Thread(target=load, daemon=True).start()

    def roi_mask(self, r, shape):
        mask = np.zeros(shape[:2], np.uint8)
        pts = r.rel_points.copy()
        pts[:, 0] = np.clip(pts[:, 0], 0, shape[1] - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, shape[0] - 1)
        cv2.fillPoly(mask, [pts], 255)
        # 避免监控框本身造成变化
        erode = max(2, min(8, int(min(r.w, r.h) * 0.006)))
        if erode > 1:
            k = np.ones((erode, erode), np.uint8)
            e = cv2.erode(mask, k)
            if np.count_nonzero(e) > 100: mask = e
        return mask

    def detect_motion(self, frame, r):
        if not r.motion: return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        mask = self.roi_mask(r, gray.shape)
        if r.last_frame is None:
            r.last_frame = gray.copy(); r.background = gray.astype(np.float32); return False

        # 一阶帧差：抓快速进入；背景差：过滤长期稳定画面
        prev_diff = cv2.absdiff(r.last_frame, gray)
        r.last_frame = gray.copy()
        bg_u8 = cv2.convertScaleAbs(r.background)
        bg_diff = cv2.absdiff(bg_u8, gray)
        cv2.accumulateWeighted(gray, r.background, 0.018)

        # 灵敏度越高，像素阈值越低
        pix_threshold = max(12, int(42 - r.sensitivity * 0.28))
        _, a = cv2.threshold(prev_diff, pix_threshold, 255, cv2.THRESH_BINARY)
        _, b = cv2.threshold(bg_diff, pix_threshold + 4, 255, cv2.THRESH_BINARY)
        th = cv2.bitwise_and(a, b)
        th = cv2.bitwise_and(th, th, mask=mask)

        # 去掉草叶、噪点等大量细小碎片，只统计有意义的大块变化
        open_k = np.ones((3, 3), np.uint8)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, open_k, iterations=1)
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(th, 8)
        min_blob_px = max(16, int(r.w * r.h * r.min_blob_ratio / 100.0))
        valid = np.zeros_like(th)
        for j in range(1, n):
            area = int(stats[j, cv2.CC_STAT_AREA])
            bw = int(stats[j, cv2.CC_STAT_WIDTH]); bh = int(stats[j, cv2.CC_STAT_HEIGHT])
            if area >= min_blob_px and bw >= 8 and bh >= 8:
                valid[labels == j] = 255
        mask_pixels = max(1, np.count_nonzero(mask))
        raw_ratio = np.count_nonzero(valid) / mask_pixels * 100.0
        # 指数平滑，让 UI 不再疯狂跳动
        r.raw_ratio = raw_ratio
        r.display_ratio = r.display_ratio * 0.72 + raw_ratio * 0.28
        return r.display_ratio >= r.min_ratio

    def detect_person(self, frame, r):
        if not r.person or self.model is None: return False
        now_frame = self.frame_counter
        if now_frame - r.last_person_check < r.person_interval: return r.person_present
        r.last_person_check = now_frame
        try:
            result = self.model.predict(frame, conf=r.person_conf, classes=[0], imgsz=640, verbose=False)[0]
            found = False
            if result.boxes is not None and len(result.boxes):
                boxes = result.boxes.xyxy.cpu().numpy()
                # 只要人员框与四点区域有足够交集即可，半身进入也能触发
                roi = r.rel_points.reshape((-1, 1, 2))
                roi_mask = np.zeros(frame.shape[:2], np.uint8)
                cv2.fillPoly(roi_mask, [r.rel_points], 255)
                for x1, y1, x2, y2 in boxes:
                    x1 = max(0, min(frame.shape[1] - 1, int(x1))); x2 = max(0, min(frame.shape[1], int(x2)))
                    y1 = max(0, min(frame.shape[0] - 1, int(y1))); y2 = max(0, min(frame.shape[0], int(y2)))
                    if x2 <= x1 or y2 <= y1: continue
                    box = np.zeros(frame.shape[:2], np.uint8); box[y1:y2, x1:x2] = 255
                    inter = cv2.countNonZero(cv2.bitwise_and(box, roi_mask))
                    box_area = max(1, (x2 - x1) * (y2 - y1))
                    if inter / box_area >= 0.08:
                        found = True; break
            if found: r.person_count += 1
            else: r.person_count = max(0, r.person_count - 1)
            r.person_present = r.person_count >= r.person_confirm
            return r.person_present
        except Exception:
            return False

    def trigger_alarm(self, r, frame, reasons):
        now = time.time()
        if now - r.last_event < r.cooldown: return False
        r.last_event = now
        r.last_reason = " + ".join(reasons)
        stamp_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename = SNAP_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg")
        try: cv2.imwrite(str(filename), frame)
        except Exception: filename = ""
        self.events.append({"time": stamp_full, "region": r.name, "reason": r.last_reason, "snapshot": str(filename)})
        self.event_list.insertItem(0, f"{datetime.now().strftime('%H:%M:%S')}  ·  {r.name}  ·  {r.last_reason}")
        self.alarm.play()
        self.status_dot.setText(f"● 报警：{r.name}")
        self.status_dot.setStyleSheet("font-size:14px; font-weight:bold; color:#ff4d67;")
        self.reason_label.setText(r.last_reason)
        self.update_region_overlay()
        if r.auto_pause:
            self.running = False; self.paused = False; self.start_btn.setText("▶ 开始监控"); self.pause_btn.setText("Ⅱ 暂停")
        return True

    def tick(self):
        if not self.running or self.paused: return
        self.frame_counter += 1
        for r in self.regions:
            if not r.enabled: continue
            frame = self.get_crop(r)
            if frame is None or frame.size == 0: continue
            motion = self.detect_motion(frame, r)
            person = self.detect_person(frame, r)
            if motion or person:
                r.confirm_count += 1
            else:
                r.confirm_count = max(0, r.confirm_count - 1)
            if r.confirm_count >= min(r.confirm, r.person_confirm if person else r.confirm):
                reasons = []
                if motion: reasons.append("画面变化")
                if person: reasons.append("检测到人员")
                self.trigger_alarm(r, frame, reasons)
                r.confirm_count = 0
            if r is self.regions[self.selected_index] if 0 <= self.selected_index < len(self.regions) else False:
                self.update_live_labels(r)
        self.update_region_overlay()

    def get_crop(self, r):
        try:
            img = np.array(self.sct.grab({"left": r.x, "top": r.y, "width": r.w, "height": r.h}))
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        except Exception:
            return None

    def update_live_labels(self, r):
        self.raw_label.setText(f"{r.raw_ratio:.2f} %")
        self.smooth_label.setText(f"{r.display_ratio:.2f} %")
        self.threshold_label.setText(f"≥ {r.min_ratio:.2f} %")
        self.person_label.setText("发现人员" if r.person_present else "未检测到人员")
        self.person_label.setStyleSheet("color:#ffb020; font-weight:bold;" if r.person_present else "color:#8b9aaa;")
        self.reason_label.setText(r.last_reason or "-")
        value = int(max(0, min(100, r.display_ratio / max(r.min_ratio, 0.05) * 100)))
        self.progress.setValue(value)

    def export_csv(self):
        if not self.events:
            QMessageBox.information(self, "没有事件", "当前没有报警事件。"); return
        path = APP_DIR / "events.csv"
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["time", "region", "reason", "snapshot"])
                writer.writeheader(); writer.writerows(self.events)
            QMessageBox.information(self, "导出成功", f"已导出：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def clear_cache(self):
        count = 0
        for p in SNAP_DIR.glob("*.jpg"):
            try: p.unlink(); count += 1
            except Exception: pass
        QMessageBox.information(self, "清理完成", f"已清理 {count} 张报警截图。")

    def auto_save_config(self):
        try:
            CONFIG_FILE.write_text(json.dumps([r.to_dict() for r in self.regions], ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception: pass

    def auto_load_config(self):
        if not CONFIG_FILE.exists(): return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.regions = [Region(x) for x in data]
            self.selected_index = 0 if self.regions else -1
            self.refresh_region_ui()
        except Exception: pass

    def closeEvent(self, event):
        try: self.region_overlay.close()
        except Exception: pass
        self.auto_save_config()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
