import sys
import json
import time
import csv
import traceback
import ctypes
from pathlib import Path
from datetime import datetime

import cv2
import mss
import numpy as np

from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QIcon, QPolygon, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QComboBox,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QSpinBox,
    QDoubleSpinBox, QCheckBox, QMessageBox, QFrame, QProgressBar,
    QScrollArea, QSizePolicy
)


APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
SNAP_DIR = APP_DIR / "snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)


def resource_path(name: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / name


def handle_exception(exc_type, exc_value, exc_traceback):
    if exc_type is KeyboardInterrupt:
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    log = APP_DIR / "crash_log.txt"
    try:
        log.write_text(text, encoding="utf-8")
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            0, f"程序发生异常。\n\n{text[:1000]}\n\n日志：{log}",
            "屏幕监控智能报警", 0x10
        )
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = handle_exception


class Region:
    def __init__(self, data=None):
        d = data or {}
        self.name = str(d.get("name", "监控区域"))
        self.enabled = bool(d.get("enabled", True))
        self.motion = bool(d.get("motion", True))
        self.sensitivity = int(d.get("sensitivity", 70))
        self.min_ratio = float(d.get("min_ratio", 0.8))
        self.confirm = int(d.get("confirm", 2))
        self.cooldown = int(d.get("cooldown", 8))
        self.auto_pause = bool(d.get("auto_pause", False))
        self.points = d.get("points", [])
        if not self.points and "x" in d:
            x, y = int(d.get("x", 0)), int(d.get("y", 0))
            w, h = int(d.get("w", 300)), int(d.get("h", 200))
            self.points = [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]
        self.last_gray = None
        self.reference = None
        self.confirm_count = 0
        self.last_event = 0.0
        self.last_score = 0.0
        self.last_reason = ""
        self._update_bounds()

    def _update_bounds(self):
        if len(self.points) >= 4:
            xs = [int(p[0]) for p in self.points[:4]]
            ys = [int(p[1]) for p in self.points[:4]]
            self.x, self.y = min(xs), min(ys)
            self.w = max(20, max(xs) - self.x + 1)
            self.h = max(20, max(ys) - self.y + 1)
            self.rel_points = np.array([[int(p[0])-self.x, int(p[1])-self.y] for p in self.points[:4]], dtype=np.int32)
        else:
            self.x, self.y, self.w, self.h = 0, 0, 100, 100
            self.rel_points = np.array([[0,0],[99,0],[99,99],[0,99]], dtype=np.int32)

    def reset_detection(self):
        self.last_gray = None
        self.reference = None
        self.confirm_count = 0
        self.last_score = 0.0
        self.last_reason = ""

    def to_dict(self):
        return {
            "name": self.name, "enabled": self.enabled, "motion": self.motion,
            "sensitivity": self.sensitivity, "min_ratio": self.min_ratio,
            "confirm": self.confirm, "cooldown": self.cooldown,
            "auto_pause": self.auto_pause, "points": self.points
        }


class Alarm:
    @staticmethod
    def play():
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            QApplication.beep()


class Selector(QWidget):
    selected = Signal(list)

    def __init__(self, image: QImage, left: int, top: int):
        super().__init__()
        self.left, self.top = left, top
        self.points = []
        self.current = QPoint()
        self.pixmap = QPixmap.fromImage(image)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setGeometry(left, top, image.width(), image.height())
        self.setMouseTracking(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        # 透明度很低，保持底下监控画面清楚可见
        p.fillRect(self.rect(), QColor(0, 0, 0, 28))
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(Qt.white, 1))
        p.drawText(28, 34, "四点框选：依次点击区域四个角 · Esc取消 · 右键重置")

        if self.points:
            poly = QPolygon([QPoint(x-self.left, y-self.top) for x, y in self.points])
            p.setPen(QPen(QColor(0, 220, 255), 3))
            p.setBrush(QColor(0, 220, 255, 35))
            p.drawPolygon(poly)
            p.setBrush(QColor(0, 220, 255))
            for i, (x, y) in enumerate(self.points):
                pt = QPoint(x-self.left, y-self.top)
                p.drawEllipse(pt, 6, 6)
                p.setPen(Qt.white)
                p.drawText(pt + QPoint(9, -8), str(i+1))
                p.setPen(QPen(QColor(0, 220, 255), 3))
            if len(self.points) < 4:
                p.setPen(QPen(Qt.yellow, 2, Qt.DashLine))
                p.drawLine(QPoint(self.points[-1][0]-self.left, self.points[-1][1]-self.top), self.current)

    def mouseMoveEvent(self, e):
        self.current = e.position().toPoint()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.points.clear(); self.update(); return
        if e.button() == Qt.LeftButton and len(self.points) < 4:
            pos = e.position().toPoint()
            self.points.append([self.left + pos.x(), self.top + pos.y()])
            if len(self.points) == 4:
                self.selected.emit(self.points[:4])
                self.close()
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
            poly = QPolygon([QPoint(int(x)-mon["left"], int(y)-mon["top"]) for x,y in r.points[:4]])
            alarm = now - r.last_event < 2.2
            color = QColor(255, 65, 80) if alarm else QColor(0, 220, 255)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 3))
            p.drawPolygon(poly)
            top = min(poly, key=lambda q:q.y())
            p.setPen(color)
            p.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            p.drawText(top + QPoint(7, 18), f"{i+1}  {r.name}")
            if r.last_score > 0:
                p.setFont(QFont("Microsoft YaHei", 9))
                p.drawText(top + QPoint(7, 36), f"变化 {r.last_score:.2f}%")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("屏幕监控智能报警")
        icon = resource_path("app.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(920, 700)
        self.setMinimumSize(820, 620)
        self.sct = mss.mss()
        self.regions = []
        self.selected_index = -1
        self.monitoring = False
        self.paused = False
        self.event_count = 0
        self.selector = None
        self.overlay = RegionOverlay(self)
        self.build_ui()
        self.load_config()
        self.setStyleSheet(self.styles())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)

    def styles(self):
        return """
        QWidget { font-family: 'Microsoft YaHei'; font-size: 13px; color: #e8eef7; }
        QMainWindow, QWidget { background: #0b111a; }
        QFrame#card, QGroupBox { background: #111a26; border: 1px solid #223247; border-radius: 12px; }
        QGroupBox { margin-top: 12px; padding: 16px 12px 12px 12px; }
        QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 7px; color: #7ee7ff; font-weight: bold; }
        QLabel#title { font-size: 25px; font-weight: 700; color: #ffffff; }
        QLabel#sub { color: #8ea2ba; font-size: 12px; }
        QLabel#status { background: #101d2a; border: 1px solid #23384e; border-radius: 9px; padding: 10px; color: #b9cbe0; }
        QPushButton { background: #182536; border: 1px solid #2a3c53; border-radius: 8px; padding: 9px 13px; color: #eaf4ff; }
        QPushButton:hover { background: #20334a; border-color: #00cfff; }
        QPushButton:pressed { background: #13202f; }
        QPushButton#primary { background: #0799c7; border: 1px solid #19cfff; font-weight: bold; }
        QPushButton#primary:hover { background: #0bb0e2; }
        QPushButton#danger { background: #39202a; border-color: #743040; }
        QComboBox, QSpinBox, QDoubleSpinBox { background: #0d1622; border: 1px solid #2a3c53; border-radius: 7px; padding: 7px; min-height: 18px; }
        QComboBox QAbstractItemView { background: #111a26; color: white; selection-background-color: #0a7ea3; }
        QCheckBox { spacing: 8px; }
        QCheckBox::indicator { width: 17px; height: 17px; }
        QCheckBox::indicator:unchecked { border: 1px solid #53657a; border-radius: 4px; background: #0d1622; }
        QCheckBox::indicator:checked { border: 1px solid #00cfff; border-radius: 4px; background: #0799c7; }
        QProgressBar { background: #0b141f; border: 1px solid #23374e; border-radius: 6px; height: 8px; text-align: center; }
        QProgressBar::chunk { background: #08bde9; border-radius: 5px; }
        QScrollArea { border: none; }
        """

    def virtual_geometry(self):
        m = self.sct.monitors[0]
        return QRect(m["left"], m["top"], m["width"], m["height"])

    def build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(22,18,22,18); outer.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout(); title_box.setSpacing(2)
        title = QLabel("屏幕监控智能报警"); title.setObjectName("title")
        sub = QLabel("直接监控 Windows 屏幕 · 多区域 · 变化检测 · 自动截图报警"); sub.setObjectName("sub")
        title_box.addWidget(title); title_box.addWidget(sub)
        header.addLayout(title_box); header.addStretch()
        self.state_badge = QLabel("● 未启动"); self.state_badge.setStyleSheet("color:#8ea2ba;font-weight:bold;padding:8px 12px;")
        header.addWidget(self.state_badge)
        outer.addLayout(header)

        actions = QHBoxLayout(); actions.setSpacing(8)
        self.select_btn = QPushButton("＋  四点框选区域"); self.select_btn.setObjectName("primary")
        self.start_btn = QPushButton("▶  开始监控"); self.pause_btn = QPushButton("Ⅱ  暂停")
        self.show_cb = QCheckBox("显示框选区域"); self.show_cb.setChecked(True)
        self.reset_btn = QPushButton("重置检测基准")
        self.test_btn = QPushButton("测试报警")
        for b in [self.select_btn,self.start_btn,self.pause_btn,self.reset_btn,self.test_btn]: actions.addWidget(b)
        actions.addStretch(); actions.addWidget(self.show_cb)
        outer.addLayout(actions)
        self.select_btn.clicked.connect(self.select_region); self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause); self.reset_btn.clicked.connect(self.reset_current)
        self.test_btn.clicked.connect(self.test_alarm); self.show_cb.stateChanged.connect(self.toggle_overlay)

        body = QHBoxLayout(); body.setSpacing(12)
        left_card = QGroupBox("监控区域")
        lv = QVBoxLayout(left_card); lv.setContentsMargins(12,18,12,12)
        self.region_combo = QComboBox(); lv.addWidget(self.region_combo)
        self.region_combo.currentIndexChanged.connect(self.region_changed)
        self.region_info = QLabel("暂无区域\n请先框选屏幕区域"); self.region_info.setObjectName("sub"); self.region_info.setWordWrap(True); lv.addWidget(self.region_info)
        self.score_label = QLabel("实时变化：—"); score_label = self.score_label
        lv.addWidget(score_label)
        self.score_bar = QProgressBar(); self.score_bar.setRange(0,100); self.score_bar.setValue(0); lv.addWidget(self.score_bar)
        lv.addStretch()
        del_btn = QPushButton("删除当前区域"); del_btn.setObjectName("danger"); del_btn.clicked.connect(self.delete_region); lv.addWidget(del_btn)
        save_btn = QPushButton("保存配置"); save_btn.clicked.connect(self.save_config); lv.addWidget(save_btn)
        body.addWidget(left_card, 1)

        right_card = QGroupBox("检测参数")
        form = QFormLayout(right_card); form.setContentsMargins(16,18,16,12); form.setVerticalSpacing(10)
        self.enable_cb = QCheckBox("启用该区域"); self.motion_cb = QCheckBox("检测画面变化")
        self.sens = QSpinBox(); self.sens.setRange(0,100); self.sens.setValue(70); self.sens.setSuffix(" %")
        self.ratio = QDoubleSpinBox(); self.ratio.setRange(0.05,30); self.ratio.setDecimals(2); self.ratio.setSingleStep(.1); self.ratio.setSuffix(" %")
        self.confirm = QSpinBox(); self.confirm.setRange(1,20); self.confirm.setValue(2); self.confirm.setSuffix(" 帧")
        self.cooldown = QSpinBox(); self.cooldown.setRange(0,3600); self.cooldown.setValue(8); self.cooldown.setSuffix(" 秒")
        self.auto_pause_cb = QCheckBox("报警后自动暂停")
        form.addRow("状态", self.enable_cb); form.addRow("检测", self.motion_cb); form.addRow("灵敏度", self.sens)
        form.addRow("最小变化面积", self.ratio); form.addRow("连续确认", self.confirm); form.addRow("报警冷却", self.cooldown); form.addRow("动作", self.auto_pause_cb)
        body.addWidget(right_card, 2)
        outer.addLayout(body, 1)

        bottom = QGroupBox("运行说明")
        bv = QVBoxLayout(bottom); bv.setContentsMargins(14,18,14,12)
        self.status = QLabel("状态：未启动"); self.status.setObjectName("status"); self.status.setWordWrap(True); bv.addWidget(self.status)
        hint = QLabel("提示：如果画面持续变化但没有报警，优先提高“灵敏度”、降低“最小变化面积”，并把“连续确认”设为 1～2 帧。点击“重置检测基准”可让程序重新学习当前画面。")
        hint.setObjectName("sub"); hint.setWordWrap(True); bv.addWidget(hint)
        outer.addWidget(bottom)

        for w in [self.enable_cb,self.motion_cb,self.auto_pause_cb,self.sens,self.ratio,self.confirm,self.cooldown]:
            if isinstance(w,QCheckBox): w.stateChanged.connect(self.apply_form)
            else: w.valueChanged.connect(self.apply_form)

    def screen_image(self):
        mon = self.sct.monitors[0]
        shot = np.asarray(self.sct.grab(mon))
        rgb = cv2.cvtColor(shot, cv2.COLOR_BGRA2RGB)
        h,w,_ = rgb.shape
        return QImage(rgb.data,w,h,3*w,QImage.Format_RGB888).copy(), mon

    def select_region(self):
        try:
            image, mon = self.screen_image()
            self.selector = Selector(image, mon["left"], mon["top"])
            self.selector.selected.connect(self.add_region)
            self.selector.show(); self.selector.raise_(); self.selector.activateWindow()
        except Exception as e:
            QMessageBox.critical(self,"框选失败",str(e))

    def add_region(self, points):
        r = Region({"points":points,"name":f"监控区域 {len(self.regions)+1}"})
        self.regions.append(r); self.refresh_combo(); self.region_combo.setCurrentIndex(len(self.regions)-1)
        self.update_overlay(); self.auto_save()

    def refresh_combo(self):
        idx = self.selected_index
        self.region_combo.blockSignals(True); self.region_combo.clear()
        for i,r in enumerate(self.regions):
            text = f"{i+1}. {r.name}   {r.w}×{r.h}"
            if not r.enabled: text += "  ·已停用"
            self.region_combo.addItem(text)
        self.region_combo.blockSignals(False)
        if self.regions:
            self.region_combo.setCurrentIndex(max(0,min(idx,len(self.regions)-1)))
        else:
            self.selected_index=-1; self.update_region_info()

    def region_changed(self, idx):
        self.selected_index=idx; self.update_region_info()
        if not 0 <= idx < len(self.regions): return
        r=self.regions[idx]
        widgets=[self.enable_cb,self.motion_cb,self.auto_pause_cb,self.sens,self.ratio,self.confirm,self.cooldown]
        for w in widgets: w.blockSignals(True)
        self.enable_cb.setChecked(r.enabled); self.motion_cb.setChecked(r.motion); self.auto_pause_cb.setChecked(r.auto_pause)
        self.sens.setValue(r.sensitivity); self.ratio.setValue(r.min_ratio); self.confirm.setValue(r.confirm); self.cooldown.setValue(r.cooldown)
        for w in widgets: w.blockSignals(False)

    def update_region_info(self):
        if not 0 <= self.selected_index < len(self.regions):
            self.region_info.setText("暂无区域\n请先框选屏幕区域"); self.score_label.setText("实时变化：—"); self.score_bar.setValue(0); return
        r=self.regions[self.selected_index]
        self.region_info.setText(f"{r.name}\n坐标：X {r.x}  Y {r.y}\n范围：{r.w} × {r.h}px")
        self.score_label.setText(f"实时变化：{r.last_score:.2f}%  / 阈值 {r.min_ratio:.2f}%")
        self.score_bar.setValue(min(100,int(r.last_score)))

    def apply_form(self):
        if not 0 <= self.selected_index < len(self.regions): return
        r=self.regions[self.selected_index]
        r.enabled=self.enable_cb.isChecked(); r.motion=self.motion_cb.isChecked(); r.auto_pause=self.auto_pause_cb.isChecked()
        r.sensitivity=self.sens.value(); r.min_ratio=self.ratio.value(); r.confirm=self.confirm.value(); r.cooldown=self.cooldown.value()
        self.update_overlay(); self.auto_save()

    def delete_region(self):
        if not 0 <= self.selected_index < len(self.regions): return
        del self.regions[self.selected_index]
        self.selected_index=min(self.selected_index,len(self.regions)-1)
        self.refresh_combo(); self.update_overlay(); self.auto_save()

    def reset_current(self):
        if 0 <= self.selected_index < len(self.regions):
            self.regions[self.selected_index].reset_detection()
            self.status.setText("状态：已重置当前区域检测基准，正在重新学习画面。")
            self.update_region_info()

    def toggle_monitor(self):
        if not self.regions:
            QMessageBox.information(self,"还没有区域","请先点击“＋ 四点框选区域”。")
            return
        self.monitoring=not self.monitoring
        if self.monitoring:
            self.paused=False
            for r in self.regions: r.reset_detection()
            self.start_btn.setText("■  停止监控"); self.pause_btn.setText("Ⅱ  暂停")
            self.set_state("● 正在监控", "#24d6a3")
        else:
            self.paused=False
            self.start_btn.setText("▶  开始监控"); self.pause_btn.setText("Ⅱ  暂停")
            self.set_state("● 已停止", "#8ea2ba")

    def toggle_pause(self):
        if not self.monitoring: return
        self.paused=not self.paused
        self.pause_btn.setText("▶  继续" if self.paused else "Ⅱ  暂停")
        self.status.setText("状态：已暂停" if self.paused else "状态：正在监控屏幕")
        self.set_state("● 已暂停" if self.paused else "● 正在监控", "#ffbd5a" if self.paused else "#24d6a3")

    def set_state(self,text,color):
        self.state_badge.setText(text); self.state_badge.setStyleSheet(f"color:{color};font-weight:bold;padding:8px 12px;")
        self.status.setText("状态：" + text.replace("● ",""))

    def toggle_overlay(self):
        if self.show_cb.isChecked():
            self.overlay.setGeometry(self.virtual_geometry()); self.overlay.show(); self.overlay.raise_()
        else: self.overlay.hide()
        self.overlay.update()

    def update_overlay(self):
        if self.show_cb.isChecked():
            self.overlay.setGeometry(self.virtual_geometry()); self.overlay.update()

    def get_crop(self,r):
        try:
            if r.w < 20 or r.h < 20: return None
            arr=np.asarray(self.sct.grab({"left":int(r.x),"top":int(r.y),"width":int(r.w),"height":int(r.h)}))
            return cv2.cvtColor(arr,cv2.COLOR_BGRA2GRAY)
        except Exception:
            return None

    def make_mask(self,r,shape):
        mask=np.zeros(shape,dtype=np.uint8)
        pts=r.rel_points.copy()
        pts[:,0]=np.clip(pts[:,0],0,shape[1]-1); pts[:,1]=np.clip(pts[:,1],0,shape[0]-1)
        cv2.fillPoly(mask,[pts],255)
        # 只去掉很窄的边框，避免把真正画面变化吃掉
        erode=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5))
        inner=cv2.erode(mask,erode,iterations=1)
        return inner if np.count_nonzero(inner)>max(100,shape[0]*shape[1]*0.02) else mask

    def detect_motion(self,gray,r):
        if not r.motion: return False
        # 缩小后再比较：减少视频压缩噪点，同时明显降低 CPU 占用
        h,w=gray.shape
        scale=min(1.0, 720.0/max(h,w))
        if scale<1:
            nw=max(40,int(w*scale)); nh=max(40,int(h*scale))
            cur=cv2.resize(gray,(nw,nh),interpolation=cv2.INTER_AREA)
        else: cur=gray
        cur=cv2.GaussianBlur(cur,(5,5),0)

        if r.last_gray is None or r.last_gray.shape!=cur.shape:
            r.last_gray=cur.copy(); r.reference=cur.copy(); r.last_score=0; return False

        mask_full=self.make_mask(r,gray.shape)
        mask=cv2.resize(mask_full,(cur.shape[1],cur.shape[0]),interpolation=cv2.INTER_NEAREST)
        mask_bool=mask>0
        pixels=int(np.count_nonzero(mask_bool))
        if pixels<100: r.last_gray=cur.copy(); return False

        # 1) 相邻帧：检测人移动、画面切换、物体出现/消失
        d1=cv2.absdiff(r.last_gray,cur)
        # 2) 基准帧：即使变化已经稳定，也能捕捉到“原来无人、现在有人”等状态改变
        d2=cv2.absdiff(r.reference,cur)
        r.last_gray=cur.copy()

        threshold=max(7,int(34 - r.sensitivity*0.27))
        a=(d1>threshold) & mask_bool
        b=(d2>threshold) & mask_bool

        # 去掉孤立噪点；小面积变化仍然允许通过高灵敏度触发
        ma=(a.astype(np.uint8)*255); mb=(b.astype(np.uint8)*255)
        kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3))
        ma=cv2.morphologyEx(ma,cv2.MORPH_OPEN,kernel)
        mb=cv2.morphologyEx(mb,cv2.MORPH_OPEN,kernel)
        score1=np.count_nonzero(ma)/pixels*100.0
        score2=np.count_nonzero(mb)/pixels*100.0

        # 两个指标取较高值，但基准差异只负责发现状态变化，不会一直累积
        score=max(score1,score2*0.85)
        r.last_score=float(score)
        r.last_reason=f"帧差 {score1:.2f}% / 基准差 {score2:.2f}%"

        # 当前画面作为新基准缓慢跟随，防止视频整体亮度变化后长期报警
        if score < r.min_ratio*0.65:
            r.reference=cv2.addWeighted(r.reference,0.96,cur,0.04,0)
        return score>=r.min_ratio

    def trigger_alarm(self,r,gray):
        now=time.time()
        if now-r.last_event<r.cooldown: return False
        r.last_event=now
        stamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename=SNAP_DIR/(datetime.now().strftime("%Y%m%d_%H%M%S_%f")+".jpg")
        try: cv2.imwrite(str(filename),gray)
        except Exception: filename=""
        Alarm.play()
        self.status.setText(f"状态：[{datetime.now().strftime('%H:%M:%S')}] {r.name} 检测到画面变化！ {r.last_score:.2f}%")
        if r.auto_pause:
            self.monitoring=False; self.paused=False; self.start_btn.setText("▶  开始监控")
            self.set_state("● 报警后已暂停", "#ff5267")
        self.update_overlay()
        return True

    def tick(self):
        if not self.monitoring or self.paused: return
        any_alarm=False
        for r in self.regions:
            if not r.enabled: continue
            frame=self.get_crop(r)
            if frame is None: continue
            changed=self.detect_motion(frame,r)
            if changed: r.confirm_count+=1
            else: r.confirm_count=0
            if r.confirm_count>=r.confirm:
                self.trigger_alarm(r,frame); r.confirm_count=0; any_alarm=True
        self.update_region_info()
        if self.show_cb.isChecked(): self.overlay.update()

    def test_alarm(self):
        Alarm.play()
        if 0<=self.selected_index<len(self.regions):
            self.regions[self.selected_index].last_event=time.time(); self.update_overlay()
        self.status.setText("状态：测试报警已触发。")

    def auto_save(self):
        try: CONFIG_FILE.write_text(json.dumps([r.to_dict() for r in self.regions],ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception: pass

    def save_config(self):
        self.auto_save(); QMessageBox.information(self,"保存成功","监控区域和检测参数已保存。")

    def load_config(self):
        if not CONFIG_FILE.exists(): return
        try:
            data=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data,list): self.regions=[Region(x) for x in data if isinstance(x,dict)]
            self.refresh_combo()
            if self.regions:
                self.selected_index=0; self.region_combo.setCurrentIndex(0); self.region_changed(0)
            self.update_overlay()
        except Exception:
            pass

    def closeEvent(self,event):
        self.auto_save()
        try: self.overlay.close()
        except Exception: pass
        event.accept()


def main():
    app=QApplication(sys.argv)
    app.setStyle("Fusion")
    w=MainWindow(); w.show()
    sys.exit(app.exec())


if __name__=="__main__": main()
