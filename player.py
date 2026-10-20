"""
Pemutar Video sederhana (MKV, MP4, AVI, WebM, dll.)
Dibuat dengan PySide6 (Qt 6) yang memakai FFmpeg di belakangnya.
Tampilan kontrol mengambang di atas video (mirip aplikasi Films & TV).

Jalankan:   python player.py  [file_video_atau_folder_opsional]
Pintasan:   Spasi = play/pause, F / klik ganda = layar penuh, Esc = keluar layar penuh / PiP,
            Kiri/Kanan = mundur/maju 5 detik, Atas/Bawah = volume, M = bisu,
            Ctrl+Kanan / PageDown = video berikutnya, Ctrl+Kiri / PageUp = video sebelumnya,
            S = lewati intro (01:30 -> 02:30), A = efek ambient, P = Picture-in-Picture, Ctrl+L = tampil/sembunyi pustaka,
            Video terakhir & menitnya otomatis dibuka lagi saat aplikasi dijalankan,
            Ctrl+O = buka file, Ctrl+Shift+O = tambah folder ke pustaka
"""
import hashlib
import json
import os
import re
import sys
from collections import deque

# Pastikan Qt memakai backend FFmpeg (default sejak Qt 6.5, ini hanya untuk jaga-jaga)
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import (
    QElapsedTimer, QLineF, QObject, QPointF, QRectF, QSettings, QSize, QStandardPaths, Qt,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QCursor, QFont, QFontMetrics, QIcon, QImage, QKeySequence,
    QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSizeGrip, QSlider, QSplitter, QStyle,
    QStyledItemDelegate, QStyleOptionSlider, QStyleOptionViewItem, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts", ".m2ts",
    ".m4v", ".3gp", ".mpg", ".mpeg",
}
VIDEO_FILTER = (
    "Video (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTS)) + ");;"
    "Semua file (*.*)"
)
HERE = os.path.dirname(os.path.abspath(__file__))
# Saat dijalankan sebagai .exe (PyInstaller), file pendukung ada di folder _internal
RES_DIR = getattr(sys, "_MEIPASS", HERE)
ICON_FILE = os.path.join(RES_DIR, "icon.ico")
PLAY_HANDLE_FILE = os.path.join(RES_DIR, "icon_play.png")  # gambar pegangan progress bar
NOW_PLAYING_FILE = os.path.join(RES_DIR, "icon_play_sidebar.png")  # penanda video aktif di sidebar
INTRO_START = 90_000    # 01:30 - opening anime biasanya mulai di sini
INTRO_END = 150_000     # 02:30 - tombol "Lewati Intro" lompat ke sini
HIDE_AFTER_MS = 2500    # kontrol disembunyikan setelah mouse diam selama ini
AMBIENT_EVERY_MS = 100   # seberapa sering warna ambient diperbarui
# Pengaturan ambient bawaan (bisa diubah lewat tombol pengaturan ⚙ di aplikasi)
AMBIENT_DEFAULTS = {
    "blur": 6,          # 1..10   makin besar = cahaya makin buram/lembut
    "spread": 50,       # 0..100  seberapa jauh cahaya meluber dari tepi video
    "brightness": 70,   # 0..100  terang/redup cahaya
    "speed": 25,        # 1..100  kecepatan perpindahan warna
    "sidebar": True,    # sidebar ikut berwarna ambient
}


def ambient_size(blur: int):
    """Resolusi mini frame: makin kecil resolusinya, makin buram cahayanya."""
    w = max(6, 54 - int(blur) * 5)
    return w, max(4, round(w * 9 / 16))
ACCENT = "#ff5f6d"
ROLE_PATH = Qt.UserRole
ROLE_IS_DIR = Qt.UserRole + 1
ROLE_ACTIVE = Qt.UserRole + 2
ROLE_COUNT = Qt.UserRole + 3       # jumlah video di dalam folder (-1 = folder tidak ditemukan)
ROLE_HAS_ACTIVE = Qt.UserRole + 4  # folder berisi video yang sedang diputar


def fmt(ms: int) -> str:
    s = max(0, ms // 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_short(ms: int) -> str:
    """Durasi gaya YouTube: 4:48 atau 1:58:57."""
    s = max(0, ms // 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def wrap_text(text: str, fm: QFontMetrics, width: int, max_lines: int) -> list:
    """Bungkus teks ke beberapa baris (juga memotong nama file panjang tanpa spasi)."""
    lines, cur = [], ""
    for word in re.split(r"(?<=[ _\-.])", text):
        if fm.horizontalAdvance(cur + word) <= width:
            cur += word
            continue
        if cur:
            lines.append(cur)
        cur = word
        while fm.horizontalAdvance(cur) > width and len(cur) > 1:
            i = len(cur)
            while i > 1 and fm.horizontalAdvance(cur[:i]) > width:
                i -= 1
            lines.append(cur[:i])
            cur = cur[i:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        rest = "".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [fm.elidedText(rest, Qt.ElideRight, width)]
    return lines


def natural_key(name: str):
    """Urutkan 'Ep 2' sebelum 'Ep 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def videos_in_dir(folder: str) -> list:
    try:
        names = [n for n in os.listdir(folder) if is_video(n) and os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return []
    return [os.path.normpath(os.path.join(folder, n)) for n in sorted(names, key=natural_key)]


# ---------------------------------------------------------------- ikon
def draw_icon(size: int) -> QPixmap:
    """Ikon aplikasi: kotak membulat bergradasi dengan tombol play putih."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    m = size * 0.04
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor(ACCENT))
    grad.setColorAt(1, QColor("#6a3df0"))
    bg = QPainterPath()
    bg.addRoundedRect(m, m, size - 2 * m, size - 2 * m, size * 0.22, size * 0.22)
    p.fillPath(bg, QBrush(grad))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 45))
    r = size * 0.30
    p.drawEllipse(QPointF(size / 2, size / 2), r, r)
    c = size / 2
    tri = QPolygonF([
        QPointF(c - size * 0.11, c - size * 0.17),
        QPointF(c - size * 0.11, c + size * 0.17),
        QPointF(c + size * 0.19, c),
    ])
    p.setBrush(QColor("white"))
    p.drawPolygon(tri)
    p.end()
    return pm


def app_icon() -> QIcon:
    if os.path.isfile(ICON_FILE):
        return QIcon(ICON_FILE)
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(draw_icon(s))
    if getattr(sys, "frozen", False):
        return icon  # .exe: jangan menulis file ke folder program
    try:  # simpan icon.ico agar bisa dipakai untuk shortcut / PyInstaller (--icon icon.ico)
        draw_icon(256).save(ICON_FILE, "ICO")
    except Exception:
        pass
    return icon


def line_icon(name: str, size: int = 64) -> QIcon:
    """Ikon garis putih datar (gaya sederhana seperti Films & TV)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    white = QColor("white")
    p.setPen(QPen(white, s * 0.065, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)

    def pt(x, y):
        return QPointF(x * s, y * s)

    def lines(*pts):
        p.drawPolyline(QPolygonF([pt(x, y) for x, y in pts]))

    def filled(*pts):
        p.save()
        p.setBrush(white)
        p.drawPolygon(QPolygonF([pt(x, y) for x, y in pts]))
        p.restore()

    if name == "play":
        filled((0.32, 0.2), (0.32, 0.8), (0.8, 0.5))
    elif name == "pause":
        lines((0.36, 0.22), (0.36, 0.78))
        lines((0.64, 0.22), (0.64, 0.78))
    elif name == "prev":
        lines((0.62, 0.18), (0.32, 0.5), (0.62, 0.82))
    elif name == "next":
        lines((0.38, 0.18), (0.68, 0.5), (0.38, 0.82))
    elif name in ("back", "fwd"):
        rect = QRectF(0.16 * s, 0.16 * s, 0.68 * s, 0.68 * s)
        if name == "back":
            p.drawArc(rect, 90 * 16, -290 * 16)
            lines((0.56, 0.06), (0.47, 0.16), (0.56, 0.26))
        else:
            p.drawArc(rect, 90 * 16, 290 * 16)
            lines((0.44, 0.06), (0.53, 0.16), (0.44, 0.26))
        f = QFont()
        f.setPixelSize(int(s * 0.32))
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, "5")
    elif name in ("volume", "mute"):
        p.save()
        p.setPen(QPen(white, s * 0.05, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolygon(QPolygonF([pt(0.12, 0.38), pt(0.28, 0.38), pt(0.48, 0.2),
                                 pt(0.48, 0.8), pt(0.28, 0.62), pt(0.12, 0.62)]))
        p.restore()
        if name == "volume":
            p.drawArc(QRectF(0.40 * s, 0.34 * s, 0.24 * s, 0.32 * s), -50 * 16, 100 * 16)
            p.drawArc(QRectF(0.38 * s, 0.2 * s, 0.44 * s, 0.6 * s), -50 * 16, 100 * 16)
        else:
            lines((0.62, 0.38), (0.86, 0.62))
            lines((0.86, 0.38), (0.62, 0.62))
    elif name == "subtitle":
        p.drawRoundedRect(QRectF(0.1 * s, 0.22 * s, 0.8 * s, 0.56 * s), s * 0.08, s * 0.08)
        lines((0.24, 0.44), (0.4, 0.44))
        lines((0.5, 0.44), (0.76, 0.44))
        lines((0.24, 0.6), (0.56, 0.6))
        lines((0.66, 0.6), (0.76, 0.6))
    elif name == "pip":
        p.drawRoundedRect(QRectF(0.08 * s, 0.2 * s, 0.84 * s, 0.6 * s), s * 0.05, s * 0.05)
        p.save()
        p.setBrush(white)
        p.drawRect(QRectF(0.5 * s, 0.5 * s, 0.28 * s, 0.18 * s))
        p.restore()
    elif name == "fullscreen":
        lines((0.14, 0.36), (0.14, 0.14), (0.36, 0.14))
        lines((0.64, 0.14), (0.86, 0.14), (0.86, 0.36))
        lines((0.86, 0.64), (0.86, 0.86), (0.64, 0.86))
        lines((0.36, 0.86), (0.14, 0.86), (0.14, 0.64))
    elif name == "exit_fullscreen":
        lines((0.14, 0.36), (0.36, 0.36), (0.36, 0.14))
        lines((0.64, 0.14), (0.64, 0.36), (0.86, 0.36))
        lines((0.86, 0.64), (0.64, 0.64), (0.64, 0.86))
        lines((0.36, 0.86), (0.36, 0.64), (0.14, 0.64))
    elif name == "library":
        for y in (0.28, 0.5, 0.72):
            lines((0.3, y), (0.86, y))
            p.drawPoint(pt(0.14, y))
    elif name == "folder":
        lines((0.1, 0.78), (0.1, 0.22), (0.38, 0.22), (0.46, 0.32), (0.9, 0.32), (0.9, 0.78), (0.1, 0.78))
    elif name == "ambient":
        p.drawEllipse(pt(0.5, 0.5), s * 0.16, s * 0.16)
        for i in range(8):
            a = i * 45
            v1, v2 = QLineF(pt(0.5, 0.5), pt(0.5, 0.2)), QLineF(pt(0.5, 0.5), pt(0.5, 0.1))
            v1.setAngle(a)
            v2.setAngle(a)
            p.drawLine(v1.p2(), v2.p2())
    elif name == "settings":
        p.drawEllipse(pt(0.5, 0.5), s * 0.12, s * 0.12)
        p.drawEllipse(pt(0.5, 0.5), s * 0.27, s * 0.27)
        p.save()
        p.setPen(QPen(white, s * 0.11, Qt.SolidLine, Qt.FlatCap))
        for i in range(8):
            ln = QLineF(pt(0.5, 0.5), pt(0.5, 0.15))
            ln.setAngle(i * 45 + 22.5)
            inner = QLineF(pt(0.5, 0.5), pt(0.5, 0.24))
            inner.setAngle(i * 45 + 22.5)
            p.drawLine(inner.p2(), ln.p2())
        p.restore()
    elif name == "collapse":
        lines((0.28, 0.46), (0.5, 0.26), (0.72, 0.46))
        lines((0.28, 0.76), (0.5, 0.56), (0.72, 0.76))
    elif name == "plus":
        lines((0.5, 0.2), (0.5, 0.8))
        lines((0.2, 0.5), (0.8, 0.5))
    elif name == "refresh":
        p.drawArc(QRectF(0.2 * s, 0.2 * s, 0.6 * s, 0.6 * s), 60 * 16, 280 * 16)
        lines((0.62, 0.1), (0.66, 0.25), (0.5, 0.29))
    elif name == "trash":
        lines((0.18, 0.28), (0.82, 0.28))
        lines((0.4, 0.28), (0.42, 0.16), (0.58, 0.16), (0.6, 0.28))
        lines((0.26, 0.28), (0.3, 0.86), (0.7, 0.86), (0.74, 0.28))
        lines((0.43, 0.42), (0.43, 0.72))
        lines((0.57, 0.42), (0.57, 0.72))
    elif name == "close":
        lines((0.28, 0.28), (0.72, 0.72))
        lines((0.72, 0.28), (0.28, 0.72))
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- widget bantu
class ClickSlider(QSlider):
    """Slider yang langsung lompat ke posisi yang diklik."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.maximum() > self.minimum():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle.contains(e.position().toPoint()):
                v = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(),
                    int(e.position().x()) - handle.width() // 2, self.width() - handle.width())
                self.setValue(v)
        super().mousePressEvent(e)


class SeekSlider(QSlider):
    """Progress bar video yang digambar sendiri, dengan gambar icon_play.png sebagai pegangan."""

    HANDLE = 32  # ukuran gambar pegangan (px)

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setFixedHeight(self.HANDLE + 6)
        self.setMouseTracking(True)
        self._hover = False
        pm = QPixmap(PLAY_HANDLE_FILE) if os.path.isfile(PLAY_HANDLE_FILE) else QPixmap()
        # Simpan versi besar (saat hover) dan normal, sudah diperkecil dengan halus
        self._pm = {}
        if not pm.isNull():
            dpr = self.devicePixelRatioF() or 1.0
            for key, size in (("normal", self.HANDLE), ("hover", self.HANDLE + 6)):
                scaled = pm.scaled(int(size * dpr), int(size * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                scaled.setDevicePixelRatio(dpr)
                self._pm[key] = scaled

    def _span(self):
        return max(1, self.width() - self.HANDLE)

    def _x_for_value(self, v):
        return self.HANDLE / 2 + QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), v, self._span())

    def _value_for_x(self, x):
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x - self.HANDLE / 2), self._span())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        cy = self.height() / 2
        left, right = self.HANDLE / 2, self.width() - self.HANDLE / 2
        x = self._x_for_value(self.sliderPosition()) if self.maximum() > self.minimum() else left
        gh = 6 if self._hover else 4
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 80))
        p.drawRoundedRect(QRectF(left, cy - gh / 2, right - left, gh), gh / 2, gh / 2)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(left, cy - gh / 2, max(0.0, x - left), gh), gh / 2, gh / 2)
        pm = self._pm.get("hover" if (self._hover or self.isSliderDown()) else "normal")
        if pm is not None:
            w = pm.width() / pm.devicePixelRatio()
            h = pm.height() / pm.devicePixelRatio()
            p.drawPixmap(QPointF(x - w / 2, cy - h / 2), pm)
        else:  # cadangan jika gambar tidak ditemukan
            p.setBrush(QColor("#111"))
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawEllipse(QPointF(x, cy), 7, 7)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.maximum() > self.minimum():
            self.setSliderDown(True)
            self.setSliderPosition(self._value_for_x(e.position().x()))  # memicu sliderMoved
            self.update()

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self.setSliderPosition(self._value_for_x(e.position().x()))
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.isSliderDown():
            self.setSliderDown(False)  # memicu sliderReleased
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()


SLIDER_CSS = f"""
QSlider::groove:horizontal {{ height: 4px; background: rgba(255,255,255,80); border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0; border-radius: 7px;
                              background: #111; border: 2px solid {ACCENT}; }}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
"""

BUTTON_CSS = """
QToolButton { background: transparent; border: none; border-radius: 6px; padding: 4px; }
QToolButton:hover { background: rgba(255,255,255,40); }
QToolButton:pressed { background: rgba(255,255,255,70); }
QToolButton:checked { background: rgba(255,255,255,30); border-bottom: 2px solid #ff5f6d; }
"""


def tool_btn(icon_name, tip, icon_px=24, parent=None):
    b = QToolButton(parent)
    b.setIcon(line_icon(icon_name))
    b.setIconSize(QSize(icon_px, icon_px))
    b.setToolTip(tip)
    b.setFocusPolicy(Qt.NoFocus)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(BUTTON_CSS)
    return b


class ControlsPanel(QWidget):
    """Panel kontrol di bawah video dengan latar gradasi gelap transparan."""

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(0, 0, 0, 0))
        g.setColorAt(0.35, QColor(0, 0, 0, 150))
        g.setColorAt(1, QColor(0, 0, 0, 210))
        p.fillRect(self.rect(), g)


class SkipIntroBox(QWidget):
    """Tombol "Lewati Intro" + tombol tutup, mengambang di kanan bawah video."""

    def __init__(self, win, parent):
        super().__init__(parent)
        self.setStyleSheet("""
            QPushButton { color: white; background: transparent; border: 1px solid rgba(255,255,255,150);
                          border-radius: 4px; padding: 7px 14px; font-weight: bold; }
            QPushButton:hover { background: white; color: black; }
        """)
        self.btn_skip = QPushButton("Lewati Intro")
        self.btn_skip.setToolTip("Lompat ke 02:30 (S)")
        self.btn_close = tool_btn("close", "Tutup (tidak muncul lagi di video ini)", 14)
        for b in (self.btn_skip, self.btn_close):
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 6, 8)
        row.setSpacing(6)
        row.addWidget(self.btn_skip)
        row.addWidget(self.btn_close)
        self.btn_skip.clicked.connect(win.skip_intro)
        self.btn_close.clicked.connect(win.dismiss_skip_intro)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 20, 210))
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)


class VideoCanvas(QWidget):
    """Area video. Frame digambar sendiri (lewat QVideoSink) supaya kontrol,
    tombol next/prev, overlay pause dan subtitle bisa tampil di atas video."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.image = None
        self.subtitle = ""
        self.ambient = None       # frame mini yang dicampur pelan-pelan untuk efek ambient
        self.ambient_on = True
        self.amb = dict(AMBIENT_DEFAULTS)
        self._ambient_clock = QElapsedTimer()
        self.setMinimumSize(200, 120)
        self.setFocusPolicy(Qt.StrongFocus)  # pemegang fokus keyboard (Spasi = play/pause)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._press_pos = None
        self._win_pos = None
        self._dragged = False
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.doubleClickInterval())
        self._click_timer.timeout.connect(win.toggle_play)

    def set_frame(self, frame):
        if frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self.image = img
                self._update_ambient(img)
                self.update()

    def _update_ambient(self, img):
        """Kecilkan frame lalu campur dengan warna sebelumnya agar transisinya halus."""
        if not self.ambient_on:
            return
        if self.ambient is not None and self._ambient_clock.isValid() \
                and self._ambient_clock.elapsed() < AMBIENT_EVERY_MS:
            return
        self._ambient_clock.restart()
        size = ambient_size(self.amb["blur"])
        small = img.scaled(*size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        if self.ambient is None or self.ambient.width() != size[0]:
            self.ambient = small.convertToFormat(QImage.Format_RGB32)
        else:
            p = QPainter(self.ambient)
            p.setOpacity(max(0.01, self.amb["speed"] / 100))
            p.drawImage(0, 0, small)
            p.end()
        self.win.sidebar.update()  # sidebar ikut berwarna ambient

    def set_subtitle(self, text):
        self.subtitle = re.sub(r"<[^>]+>", "", text or "").strip()
        self.update()

    def clear(self):
        self.image = None
        self.subtitle = ""
        self.ambient = None
        self.update()
        self.win.sidebar.update()

    def _draw_ambient(self, p, vr):
        """Cahaya lembut di sekitar video, warnanya mengikuti isi video."""
        w, h = self.width(), self.height()
        spread = self.amb["spread"] / 100
        bright = self.amb["brightness"] / 100
        dx = vr.width() * (0.02 + 0.38 * spread) + 20
        dy = vr.height() * (0.05 + 0.8 * spread) + 20
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setOpacity(min(1.0, 0.25 + bright * 1.07))
        p.drawImage(vr.adjusted(-dx, -dy, dx, dy), self.ambient)
        p.setOpacity(1.0)
        # Gelapkan ke arah tepi agar cahaya memudar (vignette)
        dark = 1.7 - bright
        g = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * (0.5 + 0.4 * spread))
        g.setColorAt(0.3, QColor(0, 0, 0, min(255, int(20 * dark))))
        g.setColorAt(0.7, QColor(0, 0, 0, min(255, int(120 * dark))))
        g.setColorAt(1.0, QColor(0, 0, 0, min(255, int(220 * dark))))
        p.fillRect(self.rect(), g)
        p.restore()

    def video_rect(self) -> QRectF:
        if self.image is None:
            return QRectF(self.rect())
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        w, h = self.width(), self.height()
        if self.image is not None:
            vr = self.video_rect()
            if self.ambient_on and self.ambient is not None:
                self._draw_ambient(p, vr)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(vr, self.image)
        elif not self.win.current:
            p.setPen(QColor(160, 160, 160))
            f = QFont(self.font())
            f.setPixelSize(16)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Pilih video dari Pustaka di kiri,\natau seret file / folder video ke sini")

        if self.subtitle:
            self._draw_subtitle(p, w, h)

        # Overlay saat pause: video digelapkan + ikon play besar di tengah
        if self.win.current and self.win.player.playbackState() != QMediaPlayer.PlayingState \
                and not self.win._seeking:
            p.fillRect(self.rect(), QColor(0, 0, 0, 120))
            p.setRenderHint(QPainter.Antialiasing)
            r = max(28, min(56, int(min(w, h) * 0.09)))
            c = QPointF(w / 2, h / 2)
            p.setPen(QPen(QColor(255, 255, 255, 220), 2.5))
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawEllipse(c, r, r)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("white"))
            t = r * 0.42
            p.drawPolygon(QPolygonF([
                QPointF(c.x() - t * 0.7, c.y() - t), QPointF(c.x() - t * 0.7, c.y() + t),
                QPointF(c.x() + t * 1.1, c.y()),
            ]))
            if self.win.resume_note:  # mis. "Lanjutkan dari 00:12:34"
                f = QFont(self.font())
                f.setPixelSize(15)
                f.setBold(True)
                p.setFont(f)
                p.setPen(QColor("white"))
                p.drawText(QRectF(0, c.y() + r + 10, w, 24), Qt.AlignCenter, self.win.resume_note)

    def _draw_subtitle(self, p, w, h):
        font = QFont(self.font())
        font.setPixelSize(max(14, int(h * 0.05)))
        font.setBold(True)
        fm = QFontMetrics(font)
        lines = self.subtitle.split("\n")
        bottom = self.win.controls_overlap() + h * 0.05
        y = h - bottom - (len(lines) - 1) * fm.lineSpacing() - fm.descent()
        path = QPainterPath()
        for i, line in enumerate(lines):
            x = (w - fm.horizontalAdvance(line)) / 2
            path.addText(x, y + i * fm.lineSpacing(), font, line)
        p.setRenderHint(QPainter.Antialiasing)
        p.strokePath(path, QPen(QColor(0, 0, 0, 230), max(3.0, font.pixelSize() * 0.14),
                                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.fillPath(path, QColor("white"))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.win.layout_overlays()

    # Klik = play/pause, klik ganda = layar penuh, seret = pindah jendela (mode PiP)
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._win_pos = self.win.pos()
            self._dragged = False

    def mouseMoveEvent(self, e):
        if self._press_pos is not None and self.win.pip:
            delta = e.globalPosition().toPoint() - self._press_pos
            if delta.manhattanLength() > 4:
                self._dragged = True
            if self._dragged:
                self.win.move(self._win_pos + delta)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press_pos is not None:
            if not self._dragged and self.win.current:
                self._click_timer.start()
            self._press_pos = None

    def mouseDoubleClickEvent(self, e):
        self._click_timer.stop()
        if self.win.pip:
            self.win.exit_pip()
        else:
            self.win.toggle_fullscreen()


# ---------------------------------------------------------------- sidebar pustaka
class ThumbnailLoader(QObject):
    """Membuat thumbnail & membaca durasi video satu per satu di latar belakang.
    Hasilnya disimpan di cache, jadi lain kali langsung tampil."""

    updated = Signal(str)
    W, H = 320, 180

    def __init__(self, parent=None):
        super().__init__(parent)
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation) or os.path.join(HERE, ".cache")
        self.dir = os.path.join(base, "thumbs")
        os.makedirs(self.dir, exist_ok=True)
        self.meta_file = os.path.join(self.dir, "meta.json")
        try:
            with open(self.meta_file, encoding="utf-8") as f:
                self.meta = json.load(f)
        except (OSError, ValueError):
            self.meta = {}
        self.pix = {}       # key -> QPixmap
        self.keys = {}      # path -> key
        self.queue = deque()
        self.queued = set()
        self.cur = None
        self.target = None
        self.dur = None
        self.frames = 0

        self.player = QMediaPlayer(self)   # tanpa audio output -> tidak bersuara
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(lambda *_: self._finish(None))
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.setInterval(12000)
        self.timeout.timeout.connect(lambda: self._finish(None))
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(1500)
        self.save_timer.timeout.connect(self.save)

    def key(self, path):
        if path not in self.keys:
            try:
                st = os.stat(path)
            except OSError:
                return None
            raw = f"{os.path.normcase(path)}|{st.st_size}|{int(st.st_mtime)}"
            self.keys[path] = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return self.keys[path]

    def info(self, path):
        """Return (QPixmap atau None, durasi ms atau None). Otomatis antre jika belum ada."""
        k = self.key(path)
        if k is None:
            return None, None
        m = self.meta.get(k)
        if m is None or m.get("pending"):
            self.request(path)
        if m is None:
            return None, None
        pm = self.pix.get(k)
        if pm is None and m.get("thumb"):
            f = os.path.join(self.dir, k + ".jpg")
            pm = QPixmap(f) if os.path.isfile(f) else QPixmap()
            self.pix[k] = pm
        return (pm if pm is not None and not pm.isNull() else None), m.get("dur")

    def set_duration(self, path, dur):
        """Durasi dari pemutar utama (lebih cepat daripada menunggu antrean)."""
        k = self.key(path)
        if k and dur > 0:
            m = self.meta.setdefault(k, {"thumb": False, "pending": True})
            m["dur"] = dur

    def request(self, path):
        if path in self.queued:
            return
        self.queued.add(path)
        self.queue.append(path)
        if self.cur is None:
            QTimer.singleShot(0, self._next)

    def _next(self):
        if self.cur is not None:
            return
        while self.queue:
            path = self.queue.popleft()
            k = self.key(path)
            m = self.meta.get(k) if k else None
            if k is None or (m is not None and not m.get("pending")):
                self.queued.discard(path)
                continue
            self.cur, self.target, self.dur, self.frames = (path, k), None, None, 0
            self.player.setSource(QUrl.fromLocalFile(path))
            self.timeout.start()
            return

    def _on_status(self, status):
        if self.cur is None:
            return
        if status == QMediaPlayer.LoadedMedia and self.target is None:
            self.dur = self.player.duration()
            self.target = int(self.dur * 0.3) if self.dur > 0 else 0
            if self.target:
                self.player.setPosition(self.target)
            self.player.play()
        elif status == QMediaPlayer.InvalidMedia:
            self._finish(None)

    def _on_frame(self, frame):
        if self.cur is None or self.target is None or not frame.isValid():
            return
        self.frames += 1
        # Lewati frame lama yang masih tersisa sebelum seek selesai
        if 0 <= frame.startTime() < (self.target - 10_000) * 1000 and self.frames < 60:
            return
        img = frame.toImage()
        if not img.isNull():
            self._finish(img)

    def _finish(self, img):
        if self.cur is None:
            return
        path, k = self.cur
        self.cur = None
        self.timeout.stop()
        has = False
        if img is not None:
            t = img.scaled(self.W, self.H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            t = t.copy((t.width() - self.W) // 2, (t.height() - self.H) // 2, self.W, self.H)
            has = t.save(os.path.join(self.dir, k + ".jpg"), "JPG", 85)
            if has:
                self.pix[k] = QPixmap.fromImage(t)
        old = self.meta.get(k, {})
        self.meta[k] = {"dur": self.dur or old.get("dur"), "thumb": has}
        self.save_timer.start()
        self.queued.discard(path)
        self.updated.emit(path)
        QTimer.singleShot(0, self._reset_and_next)  # jangan stop di dalam sinyal frame

    def _reset_and_next(self):
        self.player.stop()
        self.player.setSource(QUrl())
        QTimer.singleShot(30, self._next)

    def save(self):
        try:
            with open(self.meta_file, "w", encoding="utf-8") as f:
                json.dump(self.meta, f)
        except OSError:
            pass


class VideoItemDelegate(QStyledItemDelegate):
    """Item sidebar gaya YouTube: thumbnail + durasi di kiri, judul & keterangan di kanan."""

    TH_W, TH_H = 160, 90

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._now_pm = None

    def now_playing_pixmap(self, size):
        if self._now_pm is None:
            img = QImage(NOW_PLAYING_FILE) if os.path.isfile(NOW_PLAYING_FILE) else QImage()
            dpr = self.win.devicePixelRatioF() or 1.0
            if img.isNull():
                self._now_pm = line_icon("play").pixmap(int(size * dpr))
            else:
                self._now_pm = QPixmap.fromImage(
                    img.scaled(int(size * dpr), int(size * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._now_pm.setDevicePixelRatio(dpr)
        return self._now_pm

    def sizeHint(self, option, index):
        if index.data(ROLE_IS_DIR):
            return QSize(0, 56 if not index.parent().isValid() else 48)
        return QSize(0, self.TH_H + 12)

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(option.rect)
        active = bool(index.data(ROLE_ACTIVE))
        hover = bool(option.state & QStyle.State_MouseOver)
        bg = r.adjusted(2, 1, -4, -1)
        p.setPen(Qt.NoPen)
        if index.data(ROLE_IS_DIR):
            mouse = QPointF(self.win.tree.viewport().mapFromGlobal(QCursor.pos()))
            self._paint_dir(p, option, index, bg, hover, mouse)
            p.restore()
            return
        if active:
            p.setBrush(QColor(255, 95, 109, 55))      # latar khusus video yang sedang diputar
            p.drawRoundedRect(bg, 8, 8)
            p.setBrush(QColor(ACCENT))                 # garis di sisi kanan
            p.drawRoundedRect(QRectF(bg.right() - 4, bg.top() + 6, 4, bg.height() - 12), 2, 2)
        elif hover:
            p.setBrush(QColor(255, 255, 255, 22))
            p.drawRoundedRect(bg, 8, 8)

        self._paint_video(p, index, r, active)
        p.restore()

    @staticmethod
    def trash_rect(bg: QRectF) -> QRectF:
        """Area tombol hapus di kartu folder (bg = kotak baris yang sama dengan paint)."""
        bg = bg.adjusted(0, 3, 0, -3)
        return QRectF(bg.right() - 66, bg.center().y() - 15, 30, 30)

    def _paint_dir(self, p, option, index, bg, hover, mouse=None):
        """Kartu folder: ikon, nama, jumlah video, tombol hapus dan panah buka/tutup di kanan."""
        can_remove = not index.parent().isValid()  # hanya folder utama yang bisa dihapus dari pustaka
        trash = self.trash_rect(bg)
        bg = bg.adjusted(0, 3, 0, -3)
        has_active = bool(index.data(ROLE_HAS_ACTIVE))
        p.setBrush(QColor(255, 255, 255, 34 if hover else 18))
        p.drawRoundedRect(bg, 10, 10)
        if has_active:  # penanda: folder ini berisi video yang sedang diputar
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(bg.left(), bg.top() + 8, 4, bg.height() - 16), 2, 2)

        ico = 22
        p.drawPixmap(QPointF(bg.left() + 12, bg.center().y() - ico / 2), line_icon("folder").pixmap(ico, ico))

        opened = bool(option.state & QStyle.State_Open)
        cx, cy = bg.right() - 18, bg.center().y()
        p.setPen(QPen(QColor(230, 230, 230), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if opened:
            p.drawPolyline(QPolygonF([QPointF(cx - 5, cy - 2.5), QPointF(cx, cy + 2.5), QPointF(cx + 5, cy - 2.5)]))
        else:
            p.drawPolyline(QPolygonF([QPointF(cx - 2.5, cy - 5), QPointF(cx + 2.5, cy), QPointF(cx - 2.5, cy + 5)]))

        if can_remove:
            on_trash = mouse is not None and trash.contains(mouse)
            if on_trash:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(196, 43, 28, 210))
                p.drawRoundedRect(trash, 6, 6)
            p.save()
            p.setOpacity(1.0 if (on_trash or hover) else 0.45)
            p.drawPixmap(QPointF(trash.center().x() - 9, trash.center().y() - 9),
                         line_icon("trash").pixmap(18, 18))
            p.restore()

        x = bg.left() + 12 + ico + 10
        width = int((trash.left() - 8 if can_remove else bg.right() - 36) - x)
        nf = QFont(option.font)
        nf.setBold(True)
        nf.setPointSizeF(nf.pointSizeF() + 0.5)
        nfm = QFontMetrics(nf)
        sf = QFont(option.font)
        sf.setPointSizeF(8.5)
        sfm = QFontMetrics(sf)
        top = bg.center().y() - (nfm.height() + sfm.height()) / 2
        p.setFont(nf)
        p.setPen(QColor("white"))
        p.drawText(QRectF(x, top, width, nfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   nfm.elidedText(index.data(Qt.DisplayRole), Qt.ElideRight, width))
        n = index.data(ROLE_COUNT)
        info = "Folder tidak ditemukan" if n == -1 else f"{n} video"
        p.setFont(sf)
        p.setPen(QColor(170, 170, 170))
        sub = QRectF(x, top + nfm.height(), width, sfm.height())
        p.drawText(sub, Qt.AlignLeft | Qt.AlignVCenter, info)
        if has_active:
            p.setPen(QColor(ACCENT))
            p.drawText(sub.adjusted(sfm.horizontalAdvance(info), 0, 0, 0),
                       Qt.AlignLeft | Qt.AlignVCenter, "  ·  Sedang diputar")

    def _paint_video(self, p, index, r, active):
        path = index.data(ROLE_PATH)
        pm, dur = self.win.thumbs.info(path)
        tr = QRectF(r.x() + 6, r.y() + 6, self.TH_W, self.TH_H)
        clip = QPainterPath()
        clip.addRoundedRect(tr, 8, 8)
        p.setClipPath(clip)
        if pm is not None:
            p.drawPixmap(tr, pm, QRectF(pm.rect()))
        else:
            p.fillRect(tr, QColor(45, 45, 45))
            p.drawPixmap(QPointF(tr.center().x() - 12, tr.center().y() - 12), line_icon("play").pixmap(24))
        if active:
            p.fillRect(tr, QColor(0, 0, 0, 120))
            npm = self.now_playing_pixmap(52)
            w = npm.width() / npm.devicePixelRatio()
            h = npm.height() / npm.devicePixelRatio()
            p.drawPixmap(QPointF(tr.center().x() - w / 2, tr.center().y() - h / 2), npm)
        p.setClipping(False)

        if dur:
            f = QFont(self.win.font())
            f.setPointSizeF(8)
            f.setBold(True)
            fm = QFontMetrics(f)
            txt = fmt_short(dur)
            bw, bh = fm.horizontalAdvance(txt) + 8, fm.height() + 2
            badge = QRectF(tr.right() - bw - 5, tr.bottom() - bh - 5, bw, bh)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRoundedRect(badge, 4, 4)
            p.setFont(f)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignCenter, txt)

        # Judul (maks. 2 baris) + keterangan
        x = tr.right() + 10
        width = int(r.right() - x - 12)
        if width < 30:
            return
        tf = QFont(self.win.font())
        tf.setPointSizeF(9.5)
        tf.setBold(True)
        tfm = QFontMetrics(tf)
        p.setFont(tf)
        p.setPen(QColor("white"))
        y = r.y() + 8
        title = os.path.splitext(os.path.basename(path))[0]
        for line in wrap_text(title, tfm, width, 2):
            p.drawText(QRectF(x, y, width, tfm.height()), Qt.AlignLeft | Qt.AlignVCenter, line)
            y += tfm.height()
        sf = QFont(self.win.font())
        sf.setPointSizeF(8.5)
        p.setFont(sf)
        sfm = QFontMetrics(sf)
        y += 4
        folder = os.path.basename(os.path.dirname(path))
        p.setPen(QColor(170, 170, 170))
        p.drawText(QRectF(x, y, width, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   sfm.elidedText(folder, Qt.ElideRight, width))
        y += sfm.height()
        if active:
            p.setPen(QColor(ACCENT))
            p.drawText(QRectF(x, y, width, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter, "Sedang diputar")
        elif dur:
            p.drawText(QRectF(x, y, width, sfm.height()), Qt.AlignLeft | Qt.AlignVCenter, fmt_short(dur))


SIDEBAR_CSS = """
QLabel { color: white; background: transparent; }
QLineEdit { color: white; background: rgba(255,255,255,18); border: 1px solid rgba(255,255,255,40);
            border-radius: 16px; padding: 6px 12px; selection-background-color: #ff5f6d; }
QLineEdit:focus { border: 1px solid rgba(255,255,255,110); }
QTreeWidget { background: transparent; border: none; outline: 0; }
QTreeView::item, QTreeView::item:hover, QTreeView::item:selected { background: transparent; }
QTreeView::branch { background: transparent; image: none; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: rgba(255,255,255,60); border-radius: 4px; min-height: 30px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,110); }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { background: none; height: 0; }
""" + BUTTON_CSS


class SidebarPanel(QWidget):
    """Latar sidebar gelap yang ikut diwarnai efek ambient dari video."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(15, 15, 15))
        c = self.win.canvas
        amb = c.ambient if c.ambient_on and c.amb["sidebar"] else None
        if amb is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            # Ambil warna dari sisi kanan video (yang bersebelahan dengan sidebar)
            src = QRectF(amb.width() * 0.6, 0, amb.width() * 0.4, amb.height())
            p.drawImage(QRectF(self.rect()), amb, src)
            dark = 1.7 - c.amb["brightness"] / 100
            g = QLinearGradient(0, 0, self.width(), 0)
            g.setColorAt(0, QColor(12, 12, 12, min(250, int(140 * dark))))
            g.setColorAt(1, QColor(12, 12, 12, min(250, int(205 * dark))))
            p.fillRect(self.rect(), g)
            v = QLinearGradient(0, 0, 0, self.height())
            v.setColorAt(0, QColor(0, 0, 0, 0))
            v.setColorAt(1, QColor(0, 0, 0, 90))
            p.fillRect(self.rect(), v)


MENU_CSS = """
QMenu { background: #202020; color: white; border: 1px solid #3a3a3a; border-radius: 6px; padding: 4px; }
QMenu::item { padding: 6px 26px 6px 10px; border-radius: 4px; }
QMenu::item:selected { background: rgba(255,255,255,30); }
QMenu::item:disabled { color: #777; }
QMenu::separator { height: 1px; background: #3a3a3a; margin: 4px 6px; }
QMenu::indicator { width: 14px; height: 14px; }
"""


class StickyFolderHeader(QWidget):
    """Kartu folder yang menempel di atas daftar saat isi folder di-scroll,
    jadi folder bisa ditutup kapan saja tanpa scroll balik ke atas."""

    def __init__(self, win, tree):
        super().__init__(tree)
        self.win = win
        self.tree = tree
        self.item = None
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.hide()

    def refresh(self):
        tree = self.tree
        vp = tree.viewport()
        it = tree.itemAt(10, 1)
        folder = None
        if it is not None:
            if it.data(0, ROLE_IS_DIR):
                # Kartu folder sendiri sudah terlihat utuh di atas -> tidak perlu sticky
                if it.isExpanded() and tree.visualItemRect(it).top() < 0:
                    folder = it
            else:
                folder = it.parent()
        if folder is None:
            self.item = None
            self.hide()
            return
        self.item = folder
        h = tree.itemDelegate().sizeHint(None, tree.indexFromItem(folder)).height()
        g = vp.geometry()
        self.setGeometry(g.x(), g.y(), g.width(), h + 4)
        self.show()
        self.raise_()
        self.update()

    def _bg(self):
        return QRectF(0, 0, self.width(), self.height() - 4).adjusted(2, 1, -4, -1)

    def paintEvent(self, e):
        if self.item is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # latar solid agar thumbnail yang lewat di belakangnya tidak tembus
        p.fillRect(QRectF(0, 0, self.width(), self.height() - 4), QColor(22, 22, 22, 250))
        g = QLinearGradient(0, self.height() - 4, 0, self.height())
        g.setColorAt(0, QColor(0, 0, 0, 120))
        g.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, self.height() - 4, self.width(), 4), g)
        opt = QStyleOptionViewItem()
        opt.font = self.tree.font()
        opt.state = QStyle.State_Open
        mouse = QPointF(self.mapFromGlobal(QCursor.pos()))
        hover = self.rect().contains(mouse.toPoint())
        p.setPen(Qt.NoPen)
        self.tree.itemDelegate()._paint_dir(p, opt, self.tree.indexFromItem(self.item), self._bg(), hover, mouse)

    def mouseMoveEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def mousePressEvent(self, e):
        if self.item is None:
            return
        item = self.item
        if e.button() == Qt.RightButton:
            self.win.folder_menu(item, e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        if not item.parent() and VideoItemDelegate.trash_rect(self._bg()).contains(e.position()):
            self.win.remove_folder(item)
            return
        # Klik = tutup folder ini, lalu tampilkan kartunya di atas
        item.setExpanded(False)
        self.tree.scrollToItem(item, QTreeWidget.PositionAtTop)
        self.win.canvas.setFocus()
        QTimer.singleShot(0, self.refresh)


class AmbientSettingsDialog(QDialog):
    """Pengaturan efek ambient: perubahan langsung terlihat."""

    FIELDS = [
        ("blur", "Blur (kelembutan cahaya)", 1, 10),
        ("spread", "Sebaran (seberapa jauh cahaya meluber)", 0, 100),
        ("brightness", "Kecerahan", 0, 100),
        ("speed", "Kecepatan perubahan warna", 1, 100),
    ]

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Pengaturan Ambient")
        self.setMinimumWidth(380)
        self.setStyleSheet(f"""
            QDialog {{ background: #1c1c1c; }}
            QLabel, QCheckBox {{ color: white; }}
            QLabel#val {{ color: #bbb; min-width: 34px; }}
            QPushButton {{ color: white; background: rgba(255,255,255,20); border: 1px solid #444;
                          border-radius: 6px; padding: 6px 14px; }}
            QPushButton:hover {{ background: rgba(255,255,255,40); }}
            QPushButton#primary {{ background: {ACCENT}; border: none; }}
        """ + SLIDER_CSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(6)

        self.on = QCheckBox("Aktifkan efek ambient")
        self.on.setChecked(win.btn_amb.isChecked())
        self.on.toggled.connect(win.btn_amb.setChecked)
        lay.addWidget(self.on)
        lay.addSpacing(6)

        self.sliders = {}
        for key, label, lo, hi in self.FIELDS:
            lay.addWidget(QLabel(label))
            row = QHBoxLayout()
            s = ClickSlider(Qt.Horizontal)
            s.setRange(lo, hi)
            s.setValue(int(win.canvas.amb[key]))
            val = QLabel(str(s.value()))
            val.setObjectName("val")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            s.valueChanged.connect(lambda v, k=key, lbl=val: (lbl.setText(str(v)), self._changed(k, v)))
            row.addWidget(s, 1)
            row.addWidget(val)
            lay.addLayout(row)
            self.sliders[key] = s

        self.chk_sidebar = QCheckBox("Sidebar ikut berwarna ambient")
        self.chk_sidebar.setChecked(bool(win.canvas.amb["sidebar"]))
        self.chk_sidebar.toggled.connect(lambda v: self._changed("sidebar", v))
        lay.addSpacing(4)
        lay.addWidget(self.chk_sidebar)

        btns = QHBoxLayout()
        reset = QPushButton("Atur ulang")
        reset.clicked.connect(self.reset)
        close = QPushButton("Selesai")
        close.setObjectName("primary")
        close.clicked.connect(self.close)
        btns.addWidget(reset)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addSpacing(10)
        lay.addLayout(btns)

    def _changed(self, key, value):
        self.win.set_ambient_option(key, value)

    def reset(self):
        for key, s in self.sliders.items():
            s.setValue(AMBIENT_DEFAULTS[key])
        self.chk_sidebar.setChecked(AMBIENT_DEFAULTS["sidebar"])


# ---------------------------------------------------------------- jendela utama
class Player(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pemutar Video")
        self.resize(1200, 700)
        self.setAcceptDrops(True)
        self.settings = QSettings("PemutarVideo", "PemutarVideo")

        self.playlist = []      # video dalam folder yang sama dengan video aktif
        self.index = -1
        self.current = None
        self.pip = False
        self._items = {}        # path file -> item di pohon pustaka
        self._active_item = None
        self._hover_dir = None
        self._amb_dialog = None
        self.thumbs = ThumbnailLoader(self)
        self._normal_geom = None
        self._was_maximized = False
        self._sidebar_was_visible = True
        self._intro_done = False  # True setelah "Lewati Intro" diklik / ditutup (reset tiap video)
        self._controls_visible = True
        self._last_cursor = None
        self._pending_seek = 0
        self._last_saved_pos = 0
        self.resume_note = ""
        # Lompat posisi: pause -> setPosition -> play, agar audio tidak tertinggal dari video
        self._seeking = False
        self._seek_target = 0
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.setInterval(150)  # tunggu jika tombol ditekan berulang-ulang
        self._resume_timer.timeout.connect(self._finish_seek)

        # Mesin pemutar
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.canvas = VideoCanvas(self)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.canvas.set_frame)
        self.sink.subtitleTextChanged.connect(self.canvas.set_subtitle)

        self._build_library()
        self._build_overlays()

        # Video di kiri, pustaka di kanan (seperti YouTube)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.canvas)
        self.splitter.addWidget(self.sidebar)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setCollapsible(0, False)
        self.splitter.setHandleWidth(1)
        self.splitter.setStyleSheet("QSplitter::handle { background: #000; }")
        self.splitter.setSizes([860, 420])
        self.setCentralWidget(self.splitter)
        self.resize(1280, 720)

        # Sinyal pemutar
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.errorOccurred.connect(self.on_error)

        # Sembunyikan kontrol otomatis saat mouse diam
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(HIDE_AFTER_MS)
        self.hide_timer.timeout.connect(self.hide_controls)
        self.cursor_timer = QTimer(self)
        self.cursor_timer.setInterval(150)
        self.cursor_timer.timeout.connect(self._check_cursor)
        self.cursor_timer.start()

        # Pintasan keyboard
        self._shortcut("Space", self.toggle_play)
        self._shortcut("F", self.toggle_fullscreen)
        self._shortcut("Escape", self.on_escape)
        self._shortcut("Right", lambda: self.seek(5000))
        self._shortcut("Left", lambda: self.seek(-5000))
        self._shortcut("Up", lambda: self.vol.setValue(self.vol.value() + 5))
        self._shortcut("Down", lambda: self.vol.setValue(self.vol.value() - 5))
        self._shortcut("M", self.toggle_mute)
        self._shortcut("Ctrl+Right", self.next_video)
        self._shortcut("Ctrl+Left", self.prev_video)
        self._shortcut("PgDown", self.next_video)
        self._shortcut("PgUp", self.prev_video)
        self._shortcut(QKeySequence(Qt.Key_MediaNext), self.next_video)
        self._shortcut(QKeySequence(Qt.Key_MediaPrevious), self.prev_video)
        self._shortcut(QKeySequence(Qt.Key_MediaTogglePlayPause), self.toggle_play)
        self._shortcut("S", lambda: self.skip_box.isVisible() and self.skip_intro())
        self._shortcut("A", self.btn_amb.toggle)
        self._shortcut("P", self.toggle_pip)
        self._shortcut("Ctrl+L", self.toggle_sidebar)
        self._shortcut("Ctrl+O", self.open_file)
        self._shortcut("Ctrl+Shift+O", self.add_folder)

        self._restore_settings()
        self.refresh_library()
        self._update_nav_buttons()
        # Fokus keyboard ke video, supaya Spasi langsung play/pause (bukan mengetik di kotak cari)
        QTimer.singleShot(0, self.canvas.setFocus)

    # ---- Bangun UI ----
    def _build_library(self):
        self.sidebar = SidebarPanel(self)
        self.sidebar.setMinimumWidth(300)
        self.sidebar.setStyleSheet(SIDEBAR_CSS)
        lay = QVBoxLayout(self.sidebar)
        lay.setContentsMargins(10, 10, 4, 6)
        lay.setSpacing(8)

        title = QLabel("Pustaka")
        tf = title.font()
        tf.setPointSize(tf.pointSize() + 4)
        tf.setBold(True)
        title.setFont(tf)
        btn_add = tool_btn("plus", "Tambah folder ke pustaka (Ctrl+Shift+O)", 20)
        btn_add.clicked.connect(lambda: self.add_folder())
        btn_refresh = tool_btn("refresh", "Muat ulang isi folder", 20)
        btn_refresh.clicked.connect(self.refresh_library)
        btn_settings = tool_btn("settings", "Pengaturan ambient (blur, sebaran, kecerahan)", 20)
        btn_settings.clicked.connect(self.open_ambient_settings)
        btn_collapse = tool_btn("collapse", "Tutup semua folder (hanya tampil nama folder)", 20)
        btn_collapse.clicked.connect(self.collapse_all)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 6, 0)
        head.addWidget(title, 1)
        for b in (btn_add, btn_collapse, btn_refresh, btn_settings):
            head.addWidget(b)
        lay.addLayout(head)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Cari video...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        self.search.returnPressed.connect(self.canvas.setFocus)
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setFocusPolicy(Qt.NoFocus)  # agar Spasi/panah tetap mengendalikan video
        self.tree.setSelectionMode(QTreeWidget.NoSelection)
        self.tree.setIndentation(12)
        self.tree.setRootIsDecorated(False)
        self.tree.setExpandsOnDoubleClick(False)  # buka/tutup folder cukup 1 klik
        self.tree.itemExpanded.connect(lambda it: self._remember_expanded(it, True))
        self.tree.itemCollapsed.connect(lambda it: self._remember_expanded(it, False))
        self.tree.setMouseTracking(True)
        self.tree.viewport().setAttribute(Qt.WA_Hover)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollPerPixel)
        self.tree.verticalScrollBar().setSingleStep(24)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setItemDelegate(VideoItemDelegate(self))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        self.tree.itemClicked.connect(self.on_tree_click)
        self.thumbs.updated.connect(lambda _p: self.tree.viewport().update())
        lay.addWidget(self.tree, 1)

        # Kartu folder yang menempel di atas saat di-scroll
        self.sticky = StickyFolderHeader(self, self.tree)
        self.tree.verticalScrollBar().valueChanged.connect(lambda _v: self.sticky.refresh())
        self.tree.itemExpanded.connect(lambda _i: QTimer.singleShot(0, self.sticky.refresh))
        self.tree.itemCollapsed.connect(lambda _i: QTimer.singleShot(0, self.sticky.refresh))
        self.tree.installEventFilter(self)
        self.tree.viewport().installEventFilter(self)

        self.lbl_empty = QLabel("Belum ada folder.\nKlik + atau seret folder ke sini.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setStyleSheet("color: #aaa;")
        lay.addWidget(self.lbl_empty)

    def _build_overlays(self):
        """Semua kontrol adalah anak dari kanvas video, sehingga tampil di atas video."""
        c = self.canvas

        # Tombol next / prev di kiri & kanan video
        self.btn_prev = tool_btn("prev", "Video sebelumnya (Ctrl+Kiri)", 36, c)
        self.btn_next = tool_btn("next", "Video berikutnya (Ctrl+Kanan)", 36, c)
        self.btn_prev.clicked.connect(self.prev_video)
        self.btn_next.clicked.connect(self.next_video)

        # Panel kontrol bawah
        self.controls = ControlsPanel(c)
        self.controls.setStyleSheet("QLabel { color: white; background: transparent; }" + BUTTON_CSS)

        self.lbl_title = QLabel("")
        f = self.lbl_title.font()
        f.setPointSize(f.pointSize() + 5)
        self.lbl_title.setFont(f)

        self.slider = SeekSlider()
        self.slider.setRange(0, 0)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.lbl_pos = QLabel("00:00:00")
        self.lbl_dur = QLabel("00:00:00")
        for lbl in (self.lbl_pos, self.lbl_dur):
            lf = lbl.font()
            lf.setPointSize(max(7, lf.pointSize() - 1))
            lbl.setFont(lf)

        self.btn_vol = tool_btn("volume", "Bisu / suara (M)")
        self.vol = ClickSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        self.vol.setStyleSheet(SLIDER_CSS)
        self.audio.setVolume(0.8)
        self.btn_sub = tool_btn("subtitle", "Audio & subtitle")
        self.btn_back = tool_btn("back", "Mundur 5 detik (Kiri)", 30)
        self.btn_play = tool_btn("play", "Play / Pause (Spasi)", 34)
        self.btn_fwd = tool_btn("fwd", "Maju 5 detik (Kanan)", 30)
        self.btn_amb = tool_btn("ambient", "Efek ambient: warna sekitar video mengikuti video (A)")
        self.btn_amb.setCheckable(True)
        self.btn_amb.setChecked(True)
        self.btn_amb.setContextMenuPolicy(Qt.CustomContextMenu)  # klik kanan = pengaturan
        self.btn_amb.customContextMenuRequested.connect(lambda _p: self.open_ambient_settings())
        self.btn_amb_cfg = tool_btn("settings", "Pengaturan ambient (blur, sebaran, kecerahan)")
        self.btn_amb_cfg.clicked.connect(self.open_ambient_settings)
        self.btn_lib = tool_btn("library", "Tampil/sembunyikan pustaka (Ctrl+L)")
        self.btn_open = tool_btn("folder", "Buka file video (Ctrl+O)")
        self.btn_pip = tool_btn("pip", "Picture-in-Picture (P)")
        self.btn_full = tool_btn("fullscreen", "Layar penuh (F)")
        for w in (self.slider, self.vol):
            w.setFocusPolicy(Qt.NoFocus)

        self.btn_vol.clicked.connect(self.toggle_mute)
        self.vol.valueChanged.connect(self.on_volume)
        self.btn_sub.clicked.connect(self.show_tracks_menu)
        self.btn_back.clicked.connect(lambda: self.seek(-5000))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_fwd.clicked.connect(lambda: self.seek(5000))
        self.btn_amb.toggled.connect(self.set_ambient)
        self.btn_lib.clicked.connect(self.toggle_sidebar)
        self.btn_open.clicked.connect(self.open_file)
        self.btn_pip.clicked.connect(self.toggle_pip)
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        self.slider.sliderPressed.connect(self._begin_seek)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.slider.sliderReleased.connect(lambda: self.seek_to(self.slider.value()))

        col = QVBoxLayout(self.controls)
        col.setContentsMargins(20, 36, 20, 10)
        col.setSpacing(2)
        col.addWidget(self.lbl_title)
        col.addSpacing(6)
        col.addWidget(self.slider)
        times = QHBoxLayout()
        times.addWidget(self.lbl_pos)
        times.addStretch(1)
        times.addWidget(self.lbl_dur)
        col.addLayout(times)

        # Baris tombol: kiri | tengah | kanan (tengah selalu di tengah)
        self.left_box = QWidget()
        lrow = QHBoxLayout(self.left_box)
        lrow.setContentsMargins(0, 0, 0, 0)
        lrow.addWidget(self.btn_vol)
        lrow.addWidget(self.vol)
        lrow.addWidget(self.btn_sub)
        mid = QWidget()
        mrow = QHBoxLayout(mid)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(14)
        for b in (self.btn_back, self.btn_play, self.btn_fwd):
            mrow.addWidget(b)
        right = QWidget()
        rrow = QHBoxLayout(right)
        rrow.setContentsMargins(0, 0, 0, 0)
        for b in (self.btn_amb, self.btn_amb_cfg, self.btn_lib, self.btn_open, self.btn_pip, self.btn_full):
            rrow.addWidget(b)
        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.addWidget(self.left_box, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
        grid.addWidget(mid, 0, 1, Qt.AlignCenter)
        grid.addWidget(right, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        col.addLayout(grid)

        # Lewati intro
        self.skip_box = SkipIntroBox(self, c)
        self.skip_box.hide()

        # Pegangan ubah ukuran (hanya mode PiP)
        self.grip = QSizeGrip(c)
        self.grip.setFixedSize(16, 16)
        self.grip.hide()

    def _shortcut(self, keys, fn):
        act = QAction(self)
        act.setShortcut(QKeySequence(keys))
        act.setShortcutContext(Qt.ApplicationShortcut)
        act.triggered.connect(fn)
        self.addAction(act)

    # ---- Overlay: posisi & tampil/sembunyi ----
    def layout_overlays(self):
        if not hasattr(self, "controls"):
            return
        w, h = self.canvas.width(), self.canvas.height()
        ch = self.controls.sizeHint().height()
        self.controls.setGeometry(0, h - ch, w, ch)

        bw, bh = 56, 96
        top = max(0, (h - bh) // 2)
        self.btn_prev.setGeometry(10, top, bw, bh)
        self.btn_next.setGeometry(w - bw - 10, top, bw, bh)

        self.skip_box.adjustSize()
        sw, sh = self.skip_box.width(), self.skip_box.height()
        bottom = self.controls.y() + 30 if self.controls.isVisible() else h - 16
        self.skip_box.move(w - sw - 20, bottom - sh - 8)

        self.grip.move(w - 16, h - 16)
        for wdg in (self.btn_prev, self.btn_next, self.controls, self.skip_box, self.grip):
            wdg.raise_()

    def controls_overlap(self) -> int:
        """Tinggi area bawah yang tertutup kontrol (agar subtitle naik di atasnya)."""
        return self.controls.height() - 30 if self._controls_visible else 0

    def show_controls(self):
        if not self._controls_visible:
            self._controls_visible = True
            self.controls.show()
            self._update_nav_buttons()
            self.layout_overlays()
            self.canvas.update()
        self.canvas.unsetCursor()
        self.hide_timer.start()

    def hide_controls(self):
        if not self.current or self.player.playbackState() != QMediaPlayer.PlayingState:
            return  # saat pause / belum ada video, kontrol tetap tampil
        for wdg in (self.controls, self.skip_box, self.btn_prev, self.btn_next):
            if wdg.isVisible() and wdg.rect().contains(wdg.mapFromGlobal(QCursor.pos())):
                self.hide_timer.start()  # mouse sedang di atas kontrol
                return
        self._controls_visible = False
        self.controls.hide()
        self.btn_prev.hide()
        self.btn_next.hide()
        self.layout_overlays()
        self.canvas.update()
        if self.canvas.rect().contains(self.canvas.mapFromGlobal(QCursor.pos())):
            self.canvas.setCursor(Qt.BlankCursor)

    def _check_cursor(self):
        pos = QCursor.pos()
        if pos == self._last_cursor:
            return
        self._last_cursor = pos
        if self.isActiveWindow() and self.canvas.rect().contains(self.canvas.mapFromGlobal(pos)):
            self.show_controls()

    # ---- Pengaturan tersimpan ----
    def _folders(self) -> list:
        v = self.settings.value("folders", [])
        if isinstance(v, str):
            v = [v]
        return [f for f in (v or []) if f]

    def _restore_settings(self):
        vol = self.settings.value("volume", 80, type=int)
        self.vol.setValue(vol)
        self.on_volume(vol)
        state = self.settings.value("splitter_v2")  # v2: sidebar sekarang di kanan
        if state is not None:
            self.splitter.restoreState(state)
        self.sidebar.setVisible(self.settings.value("sidebar", True, type=bool))
        self.btn_amb.setChecked(self.settings.value("ambient", True, type=bool))
        try:
            saved = json.loads(self.settings.value("ambient_cfg", "") or "{}")
            self.canvas.amb.update({k: v for k, v in saved.items() if k in AMBIENT_DEFAULTS})
        except (TypeError, ValueError):
            pass

    def set_ambient(self, on):
        self.canvas.ambient_on = on
        if not on:
            self.canvas.ambient = None
        elif self.canvas.image is not None:
            self.canvas._update_ambient(self.canvas.image)
        self.canvas.update()
        self.sidebar.update()

    def closeEvent(self, e):
        if self.pip:
            self.exit_pip()
        if self.isFullScreen():
            self.exit_fullscreen()
        self._save_progress(force=True)
        self.settings.setValue("volume", self.vol.value())
        self.settings.setValue("splitter_v2", self.splitter.saveState())
        self.thumbs.save()
        self.settings.setValue("sidebar", self.sidebar.isVisible())
        self.settings.setValue("ambient", self.btn_amb.isChecked())
        super().closeEvent(e)

    # ---- Pustaka folder ----
    def add_folder(self, folder=None):
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Pilih folder video")
        if not folder:
            return
        folder = os.path.normpath(folder)
        folders = self._folders()
        if folder not in folders:
            folders.append(folder)
            self.settings.setValue("folders", folders)
        if not self.pip and not self.isFullScreen():
            self.sidebar.show()
        self.refresh_library()

    def remove_folder(self, item=None):
        """Hapus folder dari pustaka (file video di komputer TIDAK dihapus)."""
        item = item or self.tree.currentItem()
        while item and item.parent():
            item = item.parent()
        if not item:
            return
        folder = item.data(0, ROLE_PATH)
        box = QMessageBox(QMessageBox.Question, "Hapus dari pustaka",
                          f"Hapus folder \"{item.text(0)}\" dari pustaka?\n\n"
                          "File video di komputer tidak akan dihapus.", parent=self)
        yes = box.addButton("Hapus", QMessageBox.DestructiveRole)
        box.addButton("Batal", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        folders = [f for f in self._folders() if os.path.normcase(f) != os.path.normcase(folder)]
        self.settings.setValue("folders", folders)
        self.refresh_library()

    def folder_menu(self, item, global_pos):
        """Menu klik kanan untuk kartu folder."""
        path = item.data(0, ROLE_PATH)
        menu = QMenu(self)
        menu.setStyleSheet(MENU_CSS)
        if item.isExpanded():
            menu.addAction(line_icon("collapse"), "Tutup folder", lambda: item.setExpanded(False))
        else:
            menu.addAction(line_icon("next"), "Buka folder", lambda: item.setExpanded(True))
        menu.addAction(line_icon("folder"), "Buka di File Explorer", lambda: os.startfile(path))
        menu.addAction(line_icon("refresh"), "Muat ulang", self.refresh_library)
        if not item.parent():
            menu.addSeparator()
            menu.addAction(line_icon("trash"), "Hapus folder dari pustaka", lambda: self.remove_folder(item))
        menu.exec(global_pos)

    def eventFilter(self, obj, e):
        tree = getattr(self, "tree", None)
        if tree is not None:
            if obj is tree and e.type() == e.Type.Resize:
                QTimer.singleShot(0, self.sticky.refresh)
            elif obj is tree.viewport() and e.type() in (e.Type.MouseMove, e.Type.Leave):
                # gambar ulang kartu folder agar tombol hapus menyala saat disorot
                it = tree.itemAt(e.position().toPoint()) if e.type() == e.Type.MouseMove else None
                if it is not self._hover_dir:
                    if self._hover_dir is not None:
                        tree.viewport().update(tree.visualItemRect(self._hover_dir))
                    self._hover_dir = it if it is not None and it.data(0, ROLE_IS_DIR) else None
                if self._hover_dir is not None:
                    tree.viewport().update(tree.visualItemRect(self._hover_dir))
        return super().eventFilter(obj, e)

    # ---- Pengaturan ambient ----
    def open_ambient_settings(self):
        if self._amb_dialog is None:
            self._amb_dialog = AmbientSettingsDialog(self)
        self._amb_dialog.show()
        self._amb_dialog.raise_()
        self._amb_dialog.activateWindow()

    def set_ambient_option(self, key, value):
        self.canvas.amb[key] = value
        if key == "blur":
            self.canvas.ambient = None  # buat ulang dengan resolusi baru
            if self.canvas.image is not None:
                self.canvas._update_ambient(self.canvas.image)
        self.settings.setValue("ambient_cfg", json.dumps(self.canvas.amb))
        self.canvas.update()
        self.sidebar.update()

    def refresh_library(self):
        self.tree.clear()
        self._items.clear()
        self._active_item = None
        self.thumbs.keys.clear()  # file bisa saja berubah

        def fill(parent_item, folder):
            """Isi item dengan subfolder & video. Return jumlah video di dalamnya."""
            try:
                entries = list(os.scandir(folder))
            except OSError:
                return 0
            dirs = sorted((e for e in entries if e.is_dir() and not e.name.startswith(".")),
                          key=lambda e: natural_key(e.name))
            files = sorted((e for e in entries if e.is_file() and is_video(e.name)),
                           key=lambda e: natural_key(e.name))
            total = 0
            for d in dirs:
                sub = QTreeWidgetItem([d.name])
                sub.setToolTip(0, os.path.normpath(d.path))
                sub.setData(0, ROLE_PATH, os.path.normpath(d.path))
                sub.setData(0, ROLE_IS_DIR, True)
                n = fill(sub, d.path)
                if n:  # sembunyikan folder tanpa video
                    sub.setData(0, ROLE_COUNT, n)
                    parent_item.addChild(sub)
                    total += n
            for f in files:
                path = os.path.normpath(f.path)
                it = QTreeWidgetItem([f.name])
                it.setToolTip(0, path)
                it.setData(0, ROLE_PATH, path)
                it.setData(0, ROLE_IS_DIR, False)
                parent_item.addChild(it)
                self._items[os.path.normcase(path)] = it
                total += 1
            return total

        self._restoring = True  # jangan simpan status buka/tutup saat membangun ulang
        for folder in self._folders():
            top = QTreeWidgetItem([os.path.basename(folder) or folder])
            top.setToolTip(0, folder)
            top.setData(0, ROLE_PATH, folder)
            top.setData(0, ROLE_IS_DIR, True)
            self.tree.addTopLevelItem(top)
            if not os.path.isdir(folder):
                top.setData(0, ROLE_COUNT, -1)
                continue
            top.setData(0, ROLE_COUNT, fill(top, folder))
        self._apply_expanded_state()
        self._restoring = False

        self.lbl_empty.setVisible(self.tree.topLevelItemCount() == 0)
        self.apply_filter(self.search.text())
        self._highlight_current()
        QTimer.singleShot(0, self.sticky.refresh)

    # ---- Folder buka / tutup (diingat) ----
    def _expanded_set(self) -> set:
        v = self.settings.value("expanded_folders", [])
        if isinstance(v, str):
            v = [v]
        return {os.path.normcase(x) for x in (v or []) if x}

    def _dir_items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it.data(0, ROLE_IS_DIR):
                yield it
                stack.extend(it.child(i) for i in range(it.childCount()))

    def _apply_expanded_state(self):
        """Folder tertutup secara default; yang pernah dibuka akan tetap terbuka."""
        opened = self._expanded_set()
        for it in self._dir_items():
            it.setExpanded(os.path.normcase(it.data(0, ROLE_PATH)) in opened)

    def _remember_expanded(self, item, expanded):
        if getattr(self, "_restoring", False) or self.search.text().strip():
            return  # pembukaan otomatis (saat mencari / membangun ulang) tidak disimpan
        opened = self._expanded_set()
        key = os.path.normcase(item.data(0, ROLE_PATH))
        if expanded:
            opened.add(key)
        else:
            opened.discard(key)
        self.settings.setValue("expanded_folders", sorted(opened))

    def collapse_all(self):
        self._restoring = True
        for it in self._dir_items():
            it.setExpanded(False)
        self._restoring = False
        self.settings.setValue("expanded_folders", [])
        self.tree.scrollToTop()

    def apply_filter(self, text):
        """Cari berdasarkan nama video ATAU nama folder (folder cocok = semua isinya tampil)."""
        text = text.strip().lower()

        def visit(item, parent_match=False):
            name_match = bool(text) and text in item.text(0).lower()
            if not item.data(0, ROLE_IS_DIR):
                show = not text or parent_match or name_match
            else:
                show = False
                for i in range(item.childCount()):
                    show = visit(item.child(i), parent_match or name_match) or show
                show = show or not text or name_match or parent_match
                if text and show:
                    item.setExpanded(True)
            item.setHidden(not show)
            return show

        self._restoring = True
        if not text:
            self._apply_expanded_state()  # kembali ke status buka/tutup pilihanmu
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))  # folder yang tidak cocok ikut disembunyikan
        self._restoring = False
        if not text:
            self._highlight_current(expand=False)
        QTimer.singleShot(0, self.sticky.refresh)

    def on_tree_click(self, item, _col):
        self.canvas.setFocus()  # Spasi tetap untuk play/pause, bukan kotak cari
        if item.data(0, ROLE_IS_DIR):
            # Klik pada tombol hapus di kartu folder?
            pos = QPointF(self.tree.viewport().mapFromGlobal(QCursor.pos()))
            row = QRectF(self.tree.visualItemRect(item)).adjusted(2, 1, -4, -1)
            if not item.parent() and VideoItemDelegate.trash_rect(row).contains(pos):
                self.remove_folder(item)
                return
            item.setExpanded(not item.isExpanded())
        else:
            self.load(item.data(0, ROLE_PATH))

    def tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        gpos = self.tree.viewport().mapToGlobal(pos)
        if item is not None and item.data(0, ROLE_IS_DIR):
            self.folder_menu(item, gpos)
            return
        menu = QMenu(self)
        menu.setStyleSheet(MENU_CSS)
        if item:
            path = item.data(0, ROLE_PATH)
            menu.addAction(line_icon("play"), "Putar", lambda: self.load(path))
            menu.addAction(line_icon("folder"), "Buka di File Explorer",
                           lambda: os.startfile(os.path.dirname(path)))
            top = item
            while top.parent():
                top = top.parent()
            menu.addAction(line_icon("trash"), f"Hapus folder \"{top.text(0)}\" dari pustaka",
                           lambda: self.remove_folder(top))
            menu.addSeparator()
        menu.addAction(line_icon("plus"), "Tambah folder...", lambda: self.add_folder())
        menu.addAction(line_icon("refresh"), "Muat ulang", self.refresh_library)
        menu.exec(gpos)

    def _highlight_current(self, expand=True):
        if self._active_item is not None:
            self._active_item.setData(0, ROLE_ACTIVE, False)
            parent = self._active_item.parent()
            while parent:
                parent.setData(0, ROLE_HAS_ACTIVE, False)
                parent = parent.parent()
            self._active_item = None
        if not self.current:
            return
        it = self._items.get(os.path.normcase(self.current))
        if it:
            it.setData(0, ROLE_ACTIVE, True)
            self._active_item = it
            parent = it.parent()
            while parent:  # buka folder yang berisi video yang sedang diputar
                parent.setData(0, ROLE_HAS_ACTIVE, True)
                if expand:
                    parent.setExpanded(True)
                parent = parent.parent()
            if expand:  # scrollToItem juga akan membuka folder yang tertutup
                self.tree.setCurrentItem(it)
                self.tree.scrollToItem(it, QTreeWidget.PositionAtCenter)

    def toggle_sidebar(self):
        if not self.pip and not self.isFullScreen():
            self.sidebar.setVisible(not self.sidebar.isVisible())

    # ---- Aksi ----
    def open_file(self):
        start = os.path.dirname(self.current) if self.current else ""
        path, _ = QFileDialog.getOpenFileName(self, "Pilih video", start, VIDEO_FILTER)
        if path:
            self.load(path)

    def load(self, path: str, start_ms: int = 0, autoplay: bool = True):
        path = os.path.normpath(os.path.abspath(path))
        self._resume_timer.stop()
        self._seeking = False
        self._pending_seek = start_ms  # dijalankan saat media selesai dimuat (on_media_status)
        self.resume_note = "" if autoplay else f"Lanjutkan dari {fmt(start_ms)}"
        self._last_saved_pos = start_ms
        self.current = path
        self.playlist = videos_in_dir(os.path.dirname(path))
        keys = [os.path.normcase(p) for p in self.playlist]
        try:
            self.index = keys.index(os.path.normcase(path))
        except ValueError:
            self.playlist, self.index = [path], 0
        self._intro_done = False
        self.skip_box.hide()
        self.canvas.clear()
        self.player.setSource(QUrl.fromLocalFile(path))
        name = os.path.splitext(os.path.basename(path))[0]
        self.lbl_title.setText(name)
        pos = f"  [{self.index + 1}/{len(self.playlist)}]" if len(self.playlist) > 1 else ""
        self.setWindowTitle(f"{os.path.basename(path)}{pos} - Pemutar Video")
        if autoplay:
            self.player.play()
        else:
            self.player.pause()  # dimuat dalam keadaan pause, tinggal tekan Spasi
        self._highlight_current()
        self._update_nav_buttons()
        self.show_controls()
        self.settings.setValue("last_file", path)
        self.settings.setValue("last_pos", start_ms)

    def next_video(self):
        # Sekali klik langsung pindah ke video berikutnya
        if 0 <= self.index < len(self.playlist) - 1:
            self.load(self.playlist[self.index + 1])

    def prev_video(self):
        # Sekali klik langsung pindah ke video sebelumnya (video pertama: ulang dari awal)
        if self.index > 0:
            self.load(self.playlist[self.index - 1])
        else:
            self.player.setPosition(0)

    # ---- Lanjutkan video terakhir ----
    def resume_last(self):
        """Buka lagi video terakhir di menit terakhir saat aplikasi ditutup."""
        path = self.settings.value("last_file", "")
        if path and os.path.isfile(path):
            self.load(path, self.settings.value("last_pos", 0, type=int), autoplay=False)

    def _save_progress(self, force=False):
        if not self.current:
            return
        pos, dur = self.player.position(), self.player.duration()
        if not force and abs(pos - self._last_saved_pos) < 5000:
            return
        if dur > 0 and pos >= dur - 5000:
            pos = 0  # sudah hampir selesai -> lain kali mulai dari awal
        self._last_saved_pos = pos
        self.settings.setValue("last_file", self.current)
        self.settings.setValue("last_pos", pos)

    def _update_nav_buttons(self):
        has_prev = self.index > 0
        has_next = 0 <= self.index < len(self.playlist) - 1
        self.btn_prev.setVisible(has_prev and self._controls_visible)
        self.btn_next.setVisible(has_next and self._controls_visible)
        if has_next:
            self.btn_next.setToolTip(f"Berikutnya: {os.path.basename(self.playlist[self.index + 1])}")
        if has_prev:
            self.btn_prev.setToolTip(f"Sebelumnya: {os.path.basename(self.playlist[self.index - 1])}")

    def toggle_play(self):
        if not self.current:
            return
        if self._seeking:  # sedang melompat (pause sementara) -> batalkan play otomatis
            self._resume_timer.stop()
            self._seeking = False
            self.on_state(self.player.playbackState())
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, delta_ms: int):
        base = self._seek_target if self._seeking else self.player.position()
        self.seek_to(base + delta_ms)
        self.show_controls()

    def _begin_seek(self):
        """Pause sementara selama melompat (hanya jika video sedang diputar)."""
        if not self._seeking and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._seeking = True
            self.player.pause()

    def seek_to(self, pos: int):
        pos = max(0, min(self.player.duration(), int(pos)))
        self._begin_seek()
        self._seek_target = pos
        self.player.setPosition(pos)
        if self._seeking:
            self._resume_timer.start()

    def _finish_seek(self):
        # play ulang membuat audio & video mulai bersamaan lagi dari posisi baru
        if self._seeking:
            self._seeking = False
            self.player.play()

    def on_volume(self, v):
        self.audio.setVolume(v / 100)
        if v > 0 and self.audio.isMuted():
            self.audio.setMuted(False)
        self._update_vol_icon()

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        self._update_vol_icon()

    def _update_vol_icon(self):
        muted = self.audio.isMuted() or self.vol.value() == 0
        self.btn_vol.setIcon(line_icon("mute" if muted else "volume"))

    def on_escape(self):
        if self.search.hasFocus():
            self.canvas.setFocus()
        elif self.pip:
            self.exit_pip()
        else:
            self.exit_fullscreen()

    def toggle_fullscreen(self):
        if self.pip:
            self.exit_pip()
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._sidebar_was_visible = self.sidebar.isVisible()
            self.sidebar.hide()
            self.btn_full.setIcon(line_icon("exit_fullscreen"))
            self.btn_full.setToolTip("Keluar layar penuh (Esc)")
            self.showFullScreen()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.sidebar.setVisible(self._sidebar_was_visible)
            self.btn_full.setIcon(line_icon("fullscreen"))
            self.btn_full.setToolTip("Layar penuh (F)")

    # ---- Lewati Intro ----
    def skip_intro(self):
        self._intro_done = True
        self.skip_box.hide()
        self.seek_to(INTRO_END)

    def dismiss_skip_intro(self):
        self._intro_done = True
        self.skip_box.hide()

    def _update_skip_intro(self, pos):
        show = (not self._intro_done and self.player.duration() > INTRO_END
                and INTRO_START <= pos < INTRO_END)
        if show != self.skip_box.isVisible():
            self.skip_box.setVisible(show)
            self.layout_overlays()

    # ---- Picture-in-Picture ----
    def toggle_pip(self):
        if self.pip:
            self.exit_pip()
        else:
            self.enter_pip()

    def enter_pip(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        self._was_maximized = self.isMaximized()
        if self._was_maximized:
            self.showNormal()
        self._normal_geom = self.saveGeometry()
        self._sidebar_was_visible = self.sidebar.isVisible()
        self.pip = True
        self.sidebar.hide()
        # Mode ringkas: sembunyikan bagian kontrol yang tidak perlu
        for w in (self.lbl_title, self.left_box, self.btn_lib, self.btn_open, self.btn_full):
            w.hide()
        self.btn_pip.setToolTip("Kembali ke jendela normal (P / Esc)")
        self.controls.layout().setContentsMargins(10, 20, 10, 4)
        self.grip.show()
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.show()
        # Tunggu layout menyesuaikan, lalu kecilkan ke pojok kanan bawah layar
        QTimer.singleShot(0, self._place_pip)

    def _place_pip(self):
        if not self.pip:
            return
        area = self.screen().availableGeometry()
        w, h = 480, 290
        self.setGeometry(area.right() - w - 24, area.bottom() - h - 24, w, h)
        self.layout_overlays()

    def exit_pip(self):
        if not self.pip:
            return
        self.pip = False
        for w in (self.lbl_title, self.left_box, self.btn_lib, self.btn_open, self.btn_full):
            w.show()
        self.btn_pip.setToolTip("Picture-in-Picture (P)")
        self.controls.layout().setContentsMargins(20, 36, 20, 10)
        self.grip.hide()
        self.sidebar.setVisible(self._sidebar_was_visible)
        self.setWindowFlags(Qt.Window)
        self.show()
        if self._normal_geom is not None:
            self.restoreGeometry(self._normal_geom)
        if self._was_maximized:
            self.showMaximized()
        QTimer.singleShot(0, self.layout_overlays)

    def show_tracks_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(MENU_CSS)

        audio_menu = menu.addMenu("Audio")
        for i, md in enumerate(self.player.audioTracks()):
            act = audio_menu.addAction(self._track_name(md, i))
            act.setCheckable(True)
            act.setChecked(i == self.player.activeAudioTrack())
            act.triggered.connect(lambda _=False, i=i: self.player.setActiveAudioTrack(i))
        if not audio_menu.actions():
            audio_menu.addAction("(tidak ada)").setEnabled(False)

        sub_menu = menu.addMenu("Subtitle")
        off = sub_menu.addAction("Mati")
        off.setCheckable(True)
        off.setChecked(self.player.activeSubtitleTrack() == -1)
        off.triggered.connect(lambda: self.player.setActiveSubtitleTrack(-1))
        for i, md in enumerate(self.player.subtitleTracks()):
            act = sub_menu.addAction(self._track_name(md, i))
            act.setCheckable(True)
            act.setChecked(i == self.player.activeSubtitleTrack())
            act.triggered.connect(lambda _=False, i=i: self.player.setActiveSubtitleTrack(i))

        menu.exec(self.btn_sub.mapToGlobal(self.btn_sub.rect().topLeft()))

    @staticmethod
    def _track_name(md, i):
        parts = []
        for key in (QMediaMetaData.Title, QMediaMetaData.Language):
            try:
                v = md.stringValue(key)
            except Exception:
                v = ""
            if v and v not in parts:
                parts.append(v)
        return f"Track {i + 1}" + (f" - {' / '.join(parts)}" if parts else "")

    # ---- Update UI ----
    def on_position(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.lbl_pos.setText(fmt(pos))
        self._update_skip_intro(pos)
        if not self._pending_seek:
            self._save_progress()  # simpan tiap ~5 detik (aman jika aplikasi tertutup paksa)

    def on_duration(self, dur):
        self.slider.setRange(0, dur)
        self.lbl_dur.setText(fmt(dur))
        if self.current and dur > 0:
            self.thumbs.set_duration(self.current, dur)
            self.tree.viewport().update()

    def on_state(self, state):
        if self._seeking:
            return  # pause sementara saat melompat: jangan tampilkan overlay pause
        playing = state == QMediaPlayer.PlayingState
        if playing:
            self.resume_note = ""
        self.btn_play.setIcon(line_icon("pause" if playing else "play"))
        self.show_controls()
        self.canvas.update()  # tampil / hilangkan overlay pause

    def on_media_status(self, status):
        # Lompat ke posisi tersimpan (lanjutkan video terakhir) setelah media siap
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self._pending_seek:
            pos, self._pending_seek = self._pending_seek, 0
            self.player.setPosition(pos)
        # Otomatis lanjut ke video berikutnya di folder yang sama
        if status == QMediaPlayer.EndOfMedia and self.index < len(self.playlist) - 1:
            QTimer.singleShot(300, self.next_video)

    def on_error(self, error, message):
        if error != QMediaPlayer.NoError:
            QMessageBox.warning(self, "Gagal memutar", message or "Video tidak bisa diputar.")

    # ---- Seret & lepas file / folder ke jendela ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        played = False
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isdir(path):
                self.add_folder(path)  # folder -> masuk pustaka
            elif os.path.isfile(path) and not played:
                self.load(path)
                played = True


def main():
    if sys.platform == "win32":
        # Agar taskbar Windows memakai ikon aplikasi ini, bukan ikon Python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PemutarVideo.Player")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Pemutar Video")
    app.setWindowIcon(app_icon())
    win = Player()
    win.show()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.path.isdir(arg):
        win.add_folder(arg)
        win.resume_last()
    elif os.path.isfile(arg):
        win.load(arg)  # untuk "Open with" dari File Explorer
    else:
        win.resume_last()  # buka lagi video & menit terakhir
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
