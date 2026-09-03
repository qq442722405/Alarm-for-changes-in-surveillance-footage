import sys, os, json, csv, time, math
from pathlib import Path
from dataclasses import dataclass, asdict
import cv2
import numpy as np

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QVBoxLayout, QHBoxLayout,
    QGridLayout, QSlider, QSpinBox, QDoubleSpinBox, QCheckBox, QGroupBox,
    QProgressBar, QComboBox, QSplitter, QInputDialog
)

APP_NAME = "监控回放智能监控"
CONFIG_DIR = Path.home() / ".monitor_replay_ai"
CONFIG_DIR.mkdir(exist_ok=True)
DEFAULT_CONFIG = CONFIG_DIR / "config.json"

VIDEO_EXTS = "*.mp4 *.avi *.mov *.mkv *.wmv *.m4v"

@dataclass
class Region:
    name: str
    x: int
    y: int
    w: int
    h: int
    sensitivity: int = 45
    min_area: float = 0.8
    confirm_frames: int = 3
    cooldown: float = 5.0
    motion: bool = True
    person: bool = True
    enabled: bool = True

class VideoCanvas(QLabel):
    region_added = Signal(tuple)
    clicked_pos = Signal(int, int)
    def __init__(self):
        super().__init__()
        self.setMinimumSize(720, 405)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#101010;border:1px solid #444;")
        self.frame = None
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.drawing = False
        self.start = None
        self.current = None
        self.select_mode = False
        self.regions = []
    def set_frame(self, frame):
        self.frame = frame
        self.update()
    def set_regions(self, regions):
        self.regions = regions
        self.update()
    def mousePressEvent(self, e):
        if not self.frame:
            return
        if e.button() == Qt.LeftButton and self.select_mode:
            self.drawing = True
            self.start = (e.position().x(), e.position().y())
            self.current = self.start
        elif e.button() == Qt.LeftButton:
            self.clicked_pos.emit(int(e.position().x()), int(e.position().y()))
    def mouseMoveEvent(self, e):
        if self.drawing:
            self.current = (e.position().x(), e.position().y())
            self.update()
    def mouseReleaseEvent(self, e):
        if self.drawing and e.button() == Qt.LeftButton:
            self.drawing = False
            x1,y1 = self.start; x2,y2 = self.current
            x1,x2 = sorted([x1,x2]); y1,y2 = sorted([y1,y2])
            if x2-x1 > 12 and y2-y1 > 12 and self.frame is not None:
                fx = max(1, int((x1-self.offset_x)/self.scale))
                fy = max(1, int((y1-self.offset_y)/self.scale))
                fw = max(1, int((x2-x1)/self.scale))
                fh = max(1, int((y2-y1)/self.scale))
                h,w = self.frame.shape[:2]
                fx=max(0,min(fx,w-1)); fy=max(0,min(fy,h-1))
                fw=max(1,min(fw,w-fx)); fh=max(1,min(fh,h-fy))
                self.region_added.emit((fx,fy,fw,fh))
            self.select_mode=False
            self.update()
    def paintEvent(self, e):
        super().paintEvent(e)
        if self.frame is None:
            return
        h,w = self.frame.shape[:2]
        pix = QImage(self.frame.data, w,h,self.frame.strides[0],QImage.Format_BGR888)
        pm = QPixmap.fromImage(pix)
        scaled = pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.scale = scaled.width()/w
        self.offset_x = (self.width()-scaled.width())/2
        self.offset_y = (self.height()-scaled.height())/2
        p = QPainter(self)
        p.drawPixmap(int(self.offset_x),int(self.offset_y),scaled)
        p.setFont(QFont("Microsoft YaHei", 10))
        for i,r in enumerate(self.regions):
            rx=int(self.offset_x+r.x*self.scale); ry=int(self.offset_y+r.y*self.scale)
            rw=int(r.w*self.scale); rh=int(r.h*self.scale)
            pen=QPen(QColor(0,220,120),2)
            p.setPen(pen)
            p.drawRect(rx,ry,rw,rh)
            p.drawText(rx+4,ry+16,f"{i+1} {r.name}")
        if self.drawing and self.start and self.current:
            x1,y1=self.start; x2,y2=self.current
            p.setPen(QPen(QColor(255,200,0),2,Qt.DashLine))
            p.drawRect(int(x1),int(y1),int(x2-x1),int(y2-y1))
        p.end()

class PersonDetector:
    def __init__(self):
        self.model = None
        self.error = ""
    def load(self):
        if self.model is not None:
            return True
        try:
            from ultralytics import YOLO
            self.model = YOLO("yolo11n.pt")
            return True
        except Exception as e:
            self.error = str(e)
            return False
    def detect(self, frame, conf=0.35):
        if not self.load():
            return []
        try:
            result = self.model.predict(frame, conf=conf, classes=[0], verbose=False, imgsz=640)[0]
            out=[]
            if result.boxes is not None:
                for b in result.boxes:
                    xyxy=b.xyxy[0].cpu().numpy().astype(int)
                    score=float(b.conf[0])
                    out.append((xyxy,score))
            return out
        except Exception as e:
            self.error=str(e)
            return []

class MotionDetector:
    def __init__(self):
        self.prev = {}
    def check(self, idx, crop, sensitivity, min_area):
        if crop is None or crop.size == 0:
            return False, 0.0
        gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
        gray=cv2.GaussianBlur(gray,(5,5),0)
        old=self.prev.get(idx)
        self.prev[idx]=gray
        if old is None or old.shape != gray.shape:
            return False,0.0
        diff=cv2.absdiff(old,gray)
        # sensitivity 0..100; higher means lower threshold
        threshold=int(55 - sensitivity*0.45)
        threshold=max(8,min(55,threshold))
        mask=cv2.threshold(diff,threshold,255,cv2.THRESH_BINARY)[1]
        kernel=np.ones((3,3),np.uint8)
        mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,kernel)
        mask=cv2.dilate(mask,kernel,iterations=1)
        changed=float(np.count_nonzero(mask))/mask.size*100
        return changed >= min_area, changed

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1450,850)
        self.video=None
        self.video_path=""
        self.frame_count=0
        self.fps=25
        self.frame_index=0
        self.playing=False
        self.auto_pause=True
        self.regions=[]
        self.events=[]
        self.confirm={}
        self.last_alarm={}
        self.detector=MotionDetector()
        self.person_detector=PersonDetector()
        self.last_display=None
        self.setup_ui()
        self.timer=QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self.timer.setInterval(40)
        self.load_config()
    def setup_ui(self):
        central=QWidget(); self.setCentralWidget(central)
        root=QVBoxLayout(central)
        top=QHBoxLayout()
        self.open_btn=QPushButton("打开录像")
        self.select_btn=QPushButton("框选区域")
        self.start_btn=QPushButton("开始智能监控")
        self.pause_btn=QPushButton("暂停")
        self.step_back=QPushButton("◀ 单帧")
        self.step_fwd=QPushButton("单帧 ▶")
        self.jump_btn=QPushButton("跳到下一事件")
        self.save_btn=QPushButton("保存配置")
        self.load_btn=QPushButton("加载配置")
        for b in [self.open_btn,self.select_btn,self.start_btn,self.pause_btn,self.step_back,self.step_fwd,self.jump_btn,self.save_btn,self.load_btn]:
            top.addWidget(b)
        root.addLayout(top)
        split=QSplitter(Qt.Horizontal)
        left=QWidget(); lv=QVBoxLayout(left)
        self.canvas=VideoCanvas(); lv.addWidget(self.canvas,1)
        self.progress=QSlider(Qt.Horizontal); self.progress.setRange(0,1000); lv.addWidget(self.progress)
        self.time_label=QLabel("00:00:00 / 00:00:00"); lv.addWidget(self.time_label)
        split.addWidget(left)
        right=QWidget(); rv=QVBoxLayout(right)
        rg=QGroupBox("监控区域")
        rgl=QVBoxLayout(rg)
        self.region_list=QListWidget(); rgl.addWidget(self.region_list,1)
        btnrow=QHBoxLayout()
        self.rename_btn=QPushButton("重命名"); self.del_btn=QPushButton("删除区域")
        btnrow.addWidget(self.rename_btn); btnrow.addWidget(self.del_btn); rgl.addLayout(btnrow)
        rv.addWidget(rg,2)
        sg=QGroupBox("当前区域参数")
        form=QGridLayout(sg)
        self.enabled=QCheckBox("启用"); self.enabled.setChecked(True)
        self.motion=QCheckBox("变化检测"); self.motion.setChecked(True)
        self.person=QCheckBox("人员检测"); self.person.setChecked(True)
        self.sensitivity=QSlider(Qt.Horizontal); self.sensitivity.setRange(0,100); self.sensitivity.setValue(45)
        self.sens_label=QLabel("45")
        self.min_area=QDoubleSpinBox(); self.min_area.setRange(0.05,30); self.min_area.setSingleStep(0.05); self.min_area.setValue(0.8)
        self.confirm_spin=QSpinBox(); self.confirm_spin.setRange(1,30); self.confirm_spin.setValue(3)
        self.cooldown=QDoubleSpinBox(); self.cooldown.setRange(0,300); self.cooldown.setValue(5); self.cooldown.setSuffix(" 秒")
        self.auto_pause_cb=QCheckBox("发现事件自动暂停"); self.auto_pause_cb.setChecked(True)
        form.addWidget(self.enabled,0,0); form.addWidget(self.motion,0,1); form.addWidget(self.person,0,2)
        form.addWidget(QLabel("灵敏度"),1,0); form.addWidget(self.sensitivity,1,1); form.addWidget(self.sens_label,1,2)
        form.addWidget(QLabel("最小变化比例 %"),2,0); form.addWidget(self.min_area,2,1)
        form.addWidget(QLabel("连续确认帧"),3,0); form.addWidget(self.confirm_spin,3,1)
        form.addWidget(QLabel("报警冷却"),4,0); form.addWidget(self.cooldown,4,1)
        form.addWidget(self.auto_pause_cb,5,0,1,3)
        rv.addWidget(sg)
        ag=QGroupBox("事件记录")
        al=QVBoxLayout(ag)
        self.event_list=QListWidget(); al.addWidget(self.event_list)
        self.export_btn=QPushButton("导出事件 CSV"); al.addWidget(self.export_btn)
        rv.addWidget(ag,2)
        split.addWidget(right); split.setSizes([1000,430])
        root.addWidget(split,1)
        self.status=QLabel("请打开监控录像，然后框选区域。")
        root.addWidget(self.status)
        self.open_btn.clicked.connect(self.open_video)
        self.select_btn.clicked.connect(self.start_select)
        self.start_btn.clicked.connect(self.toggle_monitor)
        self.pause_btn.clicked.connect(self.toggle_play)
        self.step_back.clicked.connect(lambda:self.seek(self.frame_index-1))
        self.step_fwd.clicked.connect(lambda:self.seek(self.frame_index+1))
        self.progress.sliderMoved.connect(lambda v:self.seek(int(v/1000*(self.frame_count-1)) if self.frame_count else 0))
        self.region_list.currentRowChanged.connect(self.select_region)
        self.rename_btn.clicked.connect(self.rename_region)
        self.del_btn.clicked.connect(self.delete_region)
        self.sensitivity.valueChanged.connect(self.apply_region)
        self.sens_label.setText(str(self.sensitivity.value()))
        self.min_area.valueChanged.connect(self.apply_region)
        self.confirm_spin.valueChanged.connect(self.apply_region)
        self.cooldown.valueChanged.connect(self.apply_region)
        self.enabled.stateChanged.connect(self.apply_region)
        self.motion.stateChanged.connect(self.apply_region)
        self.person.stateChanged.connect(self.apply_region)
        self.auto_pause_cb.stateChanged.connect(lambda:self._set_autopause())
        self.event_list.itemDoubleClicked.connect(self.jump_event)
        self.export_btn.clicked.connect(self.export_csv)
        self.canvas.region_added.connect(self.add_region)
    def _set_autopause(self):
        self.auto_pause=self.auto_pause_cb.isChecked()
    def open_video(self):
        path,_=QFileDialog.getOpenFileName(self,"选择监控录像","",f"视频文件 ({VIDEO_EXTS.replace(' ',';')})")
        if not path:return
        if self.video: self.video.release()
        self.video=cv2.VideoCapture(path)
        if not self.video.isOpened():
            QMessageBox.critical(self,"错误","无法打开视频。"); return
        self.video_path=path
        self.frame_count=int(self.video.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps=self.video.get(cv2.CAP_PROP_FPS) or 25
        self.frame_index=0; self.playing=False; self.events=[]; self.event_list.clear()
        self.detector=MotionDetector(); self.read_frame()
        self.status.setText(f"已打开：{Path(path).name}  | {self.frame_count} 帧 | {self.fps:.2f} FPS")
    def read_frame(self):
        if not self.video:return
        self.video.set(cv2.CAP_PROP_POS_FRAMES,self.frame_index)
        ok,frame=self.video.read()
        if not ok:return
        self.last_display=frame
        self.canvas.set_frame(frame)
        self.progress.blockSignals(True)
        if self.frame_count>1:self.progress.setValue(int(self.frame_index/(self.frame_count-1)*1000))
        self.progress.blockSignals(False)
        sec=self.frame_index/self.fps
        total=self.frame_count/self.fps
        self.time_label.setText(f"{self.fmt(sec)} / {self.fmt(total)}")
    def fmt(self,s):
        s=int(max(0,s)); return f"{s//3600:02d}:{s%3600//60:02d}:{s%60:02d}"
    def next_frame(self):
        if not self.video:return
        if self.frame_index>=self.frame_count-1:
            self.playing=False; self.timer.stop(); return
        self.frame_index+=1
        self.video.set(cv2.CAP_PROP_POS_FRAMES,self.frame_index)
        ok,frame=self.video.read()
        if not ok:return
        self.last_display=frame
        self.detect_frame(frame)
        self.canvas.set_frame(frame)
        self.progress.blockSignals(True); self.progress.setValue(int(self.frame_index/(self.frame_count-1)*1000)); self.progress.blockSignals(False)
        sec=self.frame_index/self.fps; total=self.frame_count/self.fps
        self.time_label.setText(f"{self.fmt(sec)} / {self.fmt(total)}")
    def seek(self,index):
        if not self.video:return
        self.frame_index=max(0,min(self.frame_count-1,index)); self.detector=MotionDetector(); self.read_frame()
    def toggle_play(self):
        self.playing=not self.playing
        if self.playing:self.timer.start()
        else:self.timer.stop()
        self.pause_btn.setText("暂停" if self.playing else "播放")
    def toggle_monitor(self):
        if not self.video:
            QMessageBox.information(self,"提示","请先打开录像。"); return
        if not self.regions:
            QMessageBox.information(self,"提示","请先框选至少一个区域。"); return
        self.playing=not self.playing
        if self.playing:
            self.timer.start()
            self.start_btn.setText("停止智能监控")
            self.status.setText("智能监控运行中……")
        else:
            self.timer.stop()
            self.start_btn.setText("开始智能监控")
            self.status.setText("智能监控已停止。")
    def start_select(self):
        if self.last_display is None:
            QMessageBox.information(self,"提示","请先打开录像。"); return
        self.canvas.select_mode=True
        self.status.setText("请在视频画面上拖动鼠标框选一个区域。")
    def add_region(self,rect):
        n=len(self.regions)+1
        r=Region(f"区域 {n}",*rect)
        self.regions.append(r)
        self.refresh_regions()
        self.region_list.setCurrentRow(len(self.regions)-1)
        self.status.setText(f"已添加 {r.name}，可以继续框选其它区域。")
    def refresh_regions(self):
        self.region_list.clear()
        for r in self.regions:
            it=QListWidgetItem(f"{r.name}  {'●' if r.enabled else '○'}")
            self.region_list.addItem(it)
        self.canvas.set_regions(self.regions)
    def select_region(self,row):
        if row<0 or row>=len(self.regions):return
        r=self.regions[row]
        for w,val in [(self.sensitivity,r.sensitivity),(self.min_area,r.min_area),(self.confirm_spin,r.confirm_frames),(self.cooldown,r.cooldown)]:
            w.blockSignals(True); w.setValue(val); w.blockSignals(False)
        for w,val in [(self.enabled,r.enabled),(self.motion,r.motion),(self.person,r.person)]:
            w.blockSignals(True); w.setChecked(val); w.blockSignals(False)
        self.sens_label.setText(str(r.sensitivity))
    def apply_region(self):
        row=self.region_list.currentRow()
        if row<0 or row>=len(self.regions):return
        r=self.regions[row]
        r.sensitivity=self.sensitivity.value(); self.sens_label.setText(str(r.sensitivity))
        r.min_area=self.min_area.value(); r.confirm_frames=self.confirm_spin.value(); r.cooldown=self.cooldown.value()
        r.enabled=self.enabled.isChecked(); r.motion=self.motion.isChecked(); r.person=self.person.isChecked()
        self.refresh_regions(); self.region_list.setCurrentRow(row)
    def rename_region(self):
        row=self.region_list.currentRow()
        if row<0:return
        name,ok=QInputDialog.getText(self,"重命名","区域名称：",text=self.regions[row].name)
        if ok and name.strip():
            self.regions[row].name=name.strip(); self.refresh_regions(); self.region_list.setCurrentRow(row)
    def delete_region(self):
        row=self.region_list.currentRow()
        if row<0:return
        self.regions.pop(row); self.refresh_regions()
    def detect_frame(self,frame):
        for idx,r in enumerate(self.regions):
            if not r.enabled: continue
            crop=frame[r.y:r.y+r.h,r.x:r.x+r.w]
            motion=False; change=0
            if r.motion:
                motion,change=self.detector.check(idx,crop,r.sensitivity,r.min_area)
            person=False
            boxes=[]
            if r.person:
                # Only run YOLO periodically to keep replay processing usable.
                if self.frame_index % max(1,int(self.fps/4)) == 0:
                    boxes=self.person_detector.detect(crop,conf=max(0.15,0.60-r.sensitivity*0.004))
                    person=len(boxes)>0
                    self._person_cache=(idx,self.frame_index,person,boxes)
                elif getattr(self,"_person_cache",(None,))[0:1] == (idx,):
                    person=self._person_cache[2]; boxes=self._person_cache[3]
            trigger_type=None
            if motion or person:
                key=(idx, "person" if person else "motion")
                self.confirm[key]=self.confirm.get(key,0)+1
                if self.confirm[key] >= r.confirm_frames:
                    now=time.time()
                    if now-self.last_alarm.get(key,0)>=r.cooldown:
                        trigger_type="人员" if person else "画面变化"
                        self.last_alarm[key]=now
                        self.confirm[key]=0
            else:
                for key in [(idx,"motion"),(idx,"person")]:
                    self.confirm[key]=0
            if trigger_type:
                self.add_event(r,trigger_type,change,frame,boxes)
    def add_event(self,r,typ,change,frame,boxes):
        sec=self.frame_index/self.fps
        snap_dir=Path(__file__).resolve().parent/"snapshots"
        snap_dir.mkdir(exist_ok=True)
        stamp=time.strftime("%Y%m%d_%H%M%S")
        snap=snap_dir/f"{stamp}_{self.frame_index}_{typ}.jpg"
        out=frame.copy()
        cv2.rectangle(out,(r.x,r.y),(r.x+r.w,r.y+r.h),(0,255,0),2)
        if boxes:
            for xyxy,_ in boxes:
                x1,y1,x2,y2=map(int,xyxy); cv2.rectangle(out,(r.x+x1,r.y+y1),(r.x+x2,r.y+y2),(0,0,255),2)
        cv2.imwrite(str(snap),out)
        item={"frame":self.frame_index,"time":sec,"region":r.name,"type":typ,"change":round(float(change),3),"snapshot":str(snap)}
        self.events.append(item)
        li=QListWidgetItem(f"{self.fmt(sec)} | {r.name} | {typ} | 变化 {change:.2f}%")
        li.setData(Qt.UserRole,item)
        self.event_list.addItem(li)
        self.status.setText(f"⚠ 发现{typ}：{r.name}，时间 {self.fmt(sec)}")
        QApplication.beep()
        if self.auto_pause:
            self.playing=False; self.timer.stop(); self.start_btn.setText("开始智能监控")
    def jump_event(self,item):
        data=item.data(Qt.UserRole)
        if data:self.seek(data["frame"])
    def jump_btn_action(self):
        pass
    def export_csv(self):
        if not self.events:
            QMessageBox.information(self,"提示","目前没有事件记录。"); return
        path,_=QFileDialog.getSaveFileName(self,"导出事件 CSV","监控事件.csv","CSV (*.csv)")
        if not path:return
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=["frame","time","region","type","change","snapshot"])
            w.writeheader(); w.writerows(self.events)
        QMessageBox.information(self,"完成",f"已导出 {len(self.events)} 条事件。")
    def save_config(self):
        data=[asdict(r) for r in self.regions]
        path,_=QFileDialog.getSaveFileName(self,"保存区域配置","监控区域配置.json","JSON (*.json)")
        if not path:return
        Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    def load_config(self):
        if not DEFAULT_CONFIG.exists():return
        try:
            data=json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            self.regions=[Region(**x) for x in data]; self.refresh_regions()
        except: pass
    def load_config_file(self):
        pass
    def closeEvent(self,e):
        if self.video:self.video.release()
        e.accept()

def main():
    app=QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w=MainWindow(); w.show()
    sys.exit(app.exec())

if __name__=="__main__":
    main()
