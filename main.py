import sys, json, time, csv, threading
from pathlib import Path
from datetime import datetime

import cv2
import mss
import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QSpinBox, QDoubleSpinBox, QCheckBox, QMessageBox, QFileDialog,
    QAbstractItemView
)

try:
    from ultralytics import YOLO
except Exception:
    YOLO = None

APP_DIR = Path.home() / ".screen_monitor_ai"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "regions.json"
EVENT_FILE = APP_DIR / "events.csv"
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
    selected = Signal(int, int, int, int)

    def __init__(self, image, left, top):
        super().__init__()
        self.left, self.top = left, top
        self.origin = QPoint()
        self.current = QPoint()
        self.dragging = False
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setGeometry(left, top, image.width(), image.height())
        self.pixmap = QPixmap.fromImage(image)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        # 保留底下屏幕内容可见，只做非常轻的遮罩，避免原来黑屏看不到监控画面。
        p.setOpacity(0.08)
        p.fillRect(self.rect(), Qt.black)
        p.setOpacity(1.0)
        if self.dragging:
            r = QRect(self.origin, self.current).normalized()
            p.setPen(QPen(Qt.red, 3))
            p.drawRect(r)
            p.setPen(QPen(Qt.white, 1))
            p.drawText(r.topLeft() + QPoint(5, 20),
                       f"{r.width()} × {r.height()}")
        else:
            p.setPen(Qt.white)
            p.drawText(30, 40, "拖动鼠标框选监控区域，松开鼠标完成；按 ESC 取消")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.origin = e.position().toPoint()
            self.current = self.origin
            self.dragging = True
            self.update()

    def mouseMoveEvent(self, e):
        if self.dragging:
            self.current = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.dragging and e.button() == Qt.LeftButton:
            self.current = e.position().toPoint()
            r = QRect(self.origin, self.current).normalized()
            self.dragging = False
            if r.width() >= 20 and r.height() >= 20:
                self.selected.emit(
                    self.left + r.x(), self.top + r.y(), r.width(), r.height()
                )
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


class RegionOverlay(QWidget):
    """在实际屏幕上显示已框选区域，鼠标完全穿透。"""
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
            if not r.enabled:
                continue
            rect = QRect(r.x - mon['left'], r.y - mon['top'], r.w, r.h)
            p.setPen(QPen(Qt.red, 3))
            p.drawRect(rect)
            p.setPen(QPen(Qt.white, 1))
            p.drawText(rect.topLeft() + QPoint(6, 20), f"{i+1}. {r.name}")



class Region:
    def __init__(self, data=None):
        d = data or {}
        self.x = int(d.get("x", 0))
        self.y = int(d.get("y", 0))
        self.w = int(d.get("w", 300))
        self.h = int(d.get("h", 200))
        self.name = d.get("name", "监控区域")
        self.enabled = bool(d.get("enabled", True))
        self.motion = bool(d.get("motion", True))
        self.person = bool(d.get("person", True))
        self.sensitivity = int(d.get("sensitivity", 50))
        self.min_ratio = float(d.get("min_ratio", 2.0))
        self.confirm = int(d.get("confirm", 3))
        self.cooldown = int(d.get("cooldown", 10))
        self.auto_pause = bool(d.get("auto_pause", False))
        self.last_gray = None
        self.confirm_count = 0
        self.last_event = 0.0

    def to_dict(self):
        return {
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "name": self.name, "enabled": self.enabled,
            "motion": self.motion, "person": self.person,
            "sensitivity": self.sensitivity, "min_ratio": self.min_ratio,
            "confirm": self.confirm, "cooldown": self.cooldown,
            "auto_pause": self.auto_pause
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("屏幕监控智能报警")
        icon_path = Path(__file__).with_name("app.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1120, 720)
        self.regions = []
        self.events = []
        self.running = False
        self.selected_index = -1
        self.model = None
        self.model_loading = False
        self.sct = mss.mss()
        self.alarm = Alarm()
        self.region_overlay = RegionOverlay(self)
        self._build_ui()
        self.load_config(silent=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QHBoxLayout(central)

        left = QVBoxLayout()
        title = QLabel("屏幕监控智能报警")
        title.setStyleSheet("font-size:24px;font-weight:bold;")
        left.addWidget(title)

        tip = QLabel(
            "说明：程序监控的是 Windows 实际屏幕，不打开视频文件。\n"
            "先打开监控回放窗口，再点击“框选屏幕区域”。"
        )
        tip.setWordWrap(True)
        left.addWidget(tip)

        buttons = QHBoxLayout()
        self.select_btn = QPushButton("框选屏幕区域")
        self.start_btn = QPushButton("开始监控")
        self.pause_btn = QPushButton("暂停")
        self.test_btn = QPushButton("测试报警")
        buttons.addWidget(self.select_btn)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.pause_btn)
        buttons.addWidget(self.test_btn)
        left.addLayout(buttons)

        self.select_btn.clicked.connect(self.select_region)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.test_btn.clicked.connect(self.test_alarm)

        self.show_regions_cb = QCheckBox("显示框选区域")
        self.show_regions_cb.setChecked(False)
        self.show_regions_cb.stateChanged.connect(self.toggle_region_overlay)
        buttons.addWidget(self.show_regions_cb)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentRowChanged.connect(self.region_changed)
        left.addWidget(QLabel("监控区域"))
        left.addWidget(self.list)

        manage = QHBoxLayout()
        for text, fn in [
            ("删除区域", self.delete_region),
            ("保存配置", self.save_config),
            ("加载配置", lambda: self.load_config(False)),
            ("导出事件CSV", self.export_csv),
        ]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            manage.addWidget(b)
        left.addLayout(manage)

        right = QVBoxLayout()
        self.form_box = QGroupBox("当前区域设置")
        form = QFormLayout(self.form_box)

        self.name_edit = QLabel("-")
        form.addRow("区域名称", self.name_edit)

        self.pos_label = QLabel("-")
        form.addRow("屏幕坐标", self.pos_label)

        self.enable_cb = QCheckBox("启用")
        self.motion_cb = QCheckBox("检测画面变化")
        self.person_cb = QCheckBox("检测人员")
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
        form.addRow("", self.person_cb)
        form.addRow("灵敏度", self.sens)
        form.addRow("最小变化面积", self.ratio)
        form.addRow("连续确认帧", self.confirm)
        form.addRow("报警冷却", self.cooldown)
        form.addRow("", self.auto_pause_cb)

        for w in [self.enable_cb, self.motion_cb, self.person_cb,
                  self.auto_pause_cb, self.sens, self.ratio,
                  self.confirm, self.cooldown]:
            if isinstance(w, QCheckBox):
                w.stateChanged.connect(self.apply_form)
            else:
                w.valueChanged.connect(self.apply_form)

        right.addWidget(self.form_box)

        right.addWidget(QLabel("事件记录"))
        self.event_list = QListWidget()
        right.addWidget(self.event_list)

        self.status = QLabel("状态：未监控")
        self.status.setStyleSheet("font-size:16px;")
        right.addWidget(self.status)

        main.addLayout(left, 3)
        main.addLayout(right, 2)

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

    def add_region(self, x, y, w, h):
        r = Region({"x": x, "y": y, "w": w, "h": h,
                    "name": f"监控区域 {len(self.regions)+1}"})
        self.regions.append(r)
        self.refresh_list()
        self.list.setCurrentRow(len(self.regions)-1)
        self.update_region_overlay()

    def refresh_list(self):
        self.list.clear()
        for i, r in enumerate(self.regions):
            text = f"{i+1}. {r.name}  [{r.w}×{r.h}]"
            if not r.enabled:
                text += "（停用）"
            self.list.addItem(QListWidgetItem(text))

    def region_changed(self, idx):
        self.selected_index = idx
        if 0 <= idx < len(self.regions):
            r = self.regions[idx]
            self.name_edit.setText(r.name)
            self.pos_label.setText(f"X={r.x}  Y={r.y}  {r.w}×{r.h}")
            self.enable_cb.setChecked(r.enabled)
            self.motion_cb.setChecked(r.motion)
            self.person_cb.setChecked(r.person)
            self.sens.setValue(r.sensitivity)
            self.ratio.setValue(r.min_ratio)
            self.confirm.setValue(r.confirm)
            self.cooldown.setValue(r.cooldown)
            self.auto_pause_cb.setChecked(r.auto_pause)

    def apply_form(self):
        i = self.selected_index
        if not (0 <= i < len(self.regions)):
            return
        r = self.regions[i]
        r.enabled = self.enable_cb.isChecked()
        r.motion = self.motion_cb.isChecked()
        r.person = self.person_cb.isChecked()
        r.sensitivity = self.sens.value()
        r.min_ratio = self.ratio.value()
        r.confirm = self.confirm.value()
        r.cooldown = self.cooldown.value()
        r.auto_pause = self.auto_pause_cb.isChecked()
        self.refresh_list()
        self.list.setCurrentRow(i)
        self.update_region_overlay()

    def delete_region(self):
        i = self.selected_index
        if 0 <= i < len(self.regions):
            del self.regions[i]
            self.selected_index = -1
            self.refresh_list()
            self.update_region_overlay()

    def toggle_monitor(self):
        self.running = not self.running
        if self.running:
            self.start_btn.setText("停止监控")
            self.status.setText("状态：正在监控屏幕")
            for r in self.regions:
                r.last_gray = None
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
        QMessageBox.information(self, "测试报警", "已发送 Windows 报警提示音。")

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

    def ensure_model(self):
        if YOLO is None or self.model is not None or self.model_loading:
            return
        self.model_loading = True
        self.status.setText("状态：正在准备人员识别模型，首次运行可能需要联网下载……")

        def load():
            try:
                model = YOLO("yolo11n.pt")
                self.model = model
            except Exception as e:
                self.status.setText("状态：人员模型加载失败，画面变化检测仍可使用")
            finally:
                self.model_loading = False

        threading.Thread(target=load, daemon=True).start()

    def detect_person(self, frame, r):
        if not r.person:
            return False
        self.ensure_model()
        if self.model is None:
            return False
        try:
            conf = max(0.15, 0.65 - r.sensitivity * 0.004)
            result = self.model.predict(frame, conf=conf, classes=[0],
                                        verbose=False, imgsz=640)[0]
            return result.boxes is not None and len(result.boxes) > 0
        except Exception:
            return False

    def detect_motion(self, frame, r):
        if not r.motion:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if r.last_gray is None:
            r.last_gray = gray
            return False

        diff = cv2.absdiff(r.last_gray, gray)
        r.last_gray = gray
        threshold = max(8, int(45 - r.sensitivity * 0.35))
        _, th = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        kernel = np.ones((3, 3), np.uint8)
        th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
        th = cv2.dilate(th, kernel, iterations=2)
        ratio = float(np.count_nonzero(th)) / float(th.size) * 100.0
        return ratio >= r.min_ratio

    def make_event(self, r, reason, frame):
        now = time.time()
        if now - r.last_event < r.cooldown:
            return False
        r.last_event = now

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename = SNAP_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg")
        try:
            cv2.imwrite(str(filename), frame)
        except Exception:
            filename = None

        self.events.append({
            "time": stamp, "region": r.name, "reason": reason,
            "snapshot": str(filename) if filename else ""
        })
        self.event_list.insertItem(
            0, f"{stamp} | {r.name} | {reason}"
        )
        self.alarm.play()
        if r.auto_pause:
            self.running = False
            self.start_btn.setText("开始监控")
            self.status.setText("状态：报警后自动暂停")
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

            motion = self.detect_motion(frame, r)
            person = self.detect_person(frame, r)

            if motion or person:
                r.confirm_count += 1
            else:
                r.confirm_count = 0

            if r.confirm_count >= r.confirm:
                reasons = []
                if motion:
                    reasons.append("画面发生变化")
                if person:
                    reasons.append("检测到人员")
                self.make_event(r, " + ".join(reasons), frame)
                r.confirm_count = 0

    def save_config(self):
        try:
            CONFIG_FILE.write_text(
                json.dumps([r.to_dict() for r in self.regions],
                           ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            QMessageBox.information(self, "保存成功", "监控区域配置已保存。")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def load_config(self, silent=False):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.regions = [Region(x) for x in data]
            self.refresh_list()
            if self.regions:
                self.list.setCurrentRow(0)
            if not silent:
                QMessageBox.information(self, "加载成功", "配置已加载。")
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "加载失败", str(e))

    def export_csv(self):
        if not self.events:
            QMessageBox.information(self, "没有事件", "目前还没有报警事件。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出事件CSV", "屏幕监控事件.csv", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["time", "region", "reason", "snapshot"]
                )
                writer.writeheader()
                writer.writerows(self.events)
            QMessageBox.information(self, "导出成功", path)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event):
        try:
            self.region_overlay.close()
        except Exception:
            pass
        try:
            CONFIG_FILE.write_text(
                json.dumps([r.to_dict() for r in self.regions],
                           ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
