#!/usr/bin/env python3
"""
Photo Scanner - Watches a directory for scanned images, renames, moves, and tags them.
"""

import calendar
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk
import piexif
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from tkinterdnd2 import TkinterDnD, DND_FILES


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}

PREFS_FILE = Path.home() / ".photo_scanner_prefs.json"

HANDLE_R    = 5    # crop handle half-size in canvas px
MIN_CROP    = 10   # minimum crop dimension in image px
PREVIEW_MAX = 800  # max dimension for interactive preview (slider speed)

MONTH_NAMES = (
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

DATE_REGEXES = [
    # YYYY-MM-DD / YYYY.MM.DD / YYYY_MM_DD
    re.compile(r"\b(?P<y>\d{4})[.\-_](?P<m>\d{1,2})[.\-_](?P<d>\d{1,2})\b"),
    # MM-DD-YYYY / MM.DD.YYYY / MM_DD_YYYY
    re.compile(r"\b(?P<m>\d{1,2})[.\-_](?P<d>\d{1,2})[.\-_](?P<y>\d{4})\b"),
    # "December 25 2023" / "Dec 25, 2023"
    re.compile(MONTH_NAMES + r"\s+(?P<d>\d{1,2})[,\s]+(?P<y>\d{4})", re.IGNORECASE),
    # "25 December 2023"
    re.compile(r"(?P<d>\d{1,2})\s+" + MONTH_NAMES.replace("?P<month>", "?P<_m>") + r"\s+(?P<y>\d{4})", re.IGNORECASE),
    # "August 2008" / "Aug 2008" — day defaults to 1
    re.compile(MONTH_NAMES.replace("?P<month>", "?P<month2>") + r"\s+(?P<y>\d{4})(?!\s*\d)", re.IGNORECASE),
    # "2008 August" / "2008 Aug" — day defaults to 1
    re.compile(r"(?<!\d)(?P<y>\d{4})\s+" + MONTH_NAMES.replace("?P<month>", "?P<month3>"), re.IGNORECASE),
    # bare "2008" — defaults to January 1st (matched last, least specific)
    re.compile(r"(?<!\d)(?P<y>\d{4})(?!\d)"),
]

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_to_int(s: str) -> int:
    return MONTH_MAP[s[:3].lower()]


# ---------------------------------------------------------------------------
# Holiday engine (ported from holidays.cs)
# ---------------------------------------------------------------------------

# Fixed holidays: (name, month, day)
_HOLIDAYS_FIXED = [
    ("New Year's",     1,  1),
    ("Valentine's",    2, 14),
    ("St Patrick's",   3, 17),
    ("Independence Day", 7, 4),
    ("Halloween",     10, 31),
    ("Veterans Day",  11, 11),
    ("Christmas Eve", 12, 24),
    ("Christmas",     12, 25),
    ("New Year's Eve",12, 31),
]

# Floating holidays: (name, month, cs_dow, week_of_month)
# cs_dow: 1=Sunday, 2=Monday, 3=Tuesday, 4=Wednesday, 5=Thursday, 6=Friday, 7=Saturday
_HOLIDAYS_FLOATING = [
    ("Martin Luther King Jr", 1,  2, 3),  # 3rd Monday of January
    ("Washington's Birthday", 2,  2, 3),  # 3rd Monday of February
    ("Memorial Day",          5,  2, 4),  # 4th Monday of May
    ("Mother's Day",          5,  1, 2),  # 2nd Sunday of May
    ("Father's Day",          6,  1, 3),  # 3rd Sunday of June
    ("Labor Day",             9,  2, 1),  # 1st Monday of September
    ("Columbus Day",         10,  2, 2),  # 2nd Monday of October
    ("Thanksgiving",         11,  5, 4),  # 4th Thursday of November
]

_REMOVE_PUNCT = re.compile(r"[.,?!'\":;@#$%^&*()\-]")


def _normalize(s: str) -> str:
    return _REMOVE_PUNCT.sub("", s).lower()


def _nth_weekday(year: int, month: int, cs_dow: int, week: int) -> Optional[datetime]:
    # Convert C# DayOfWeek (1=Sun..7=Sat) to Python weekday() (0=Mon..6=Sun)
    py_dow = (cs_dow - 2) % 7
    count = 0
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        dt = datetime(year, month, day)
        if dt.weekday() == py_dow:
            count += 1
            if count == week:
                return dt
    return None


def _easter(year: int) -> datetime:
    # Anonymous Gregorian algorithm (mirrors the C# implementation)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = (h + l - 7 * m + 114) % 31 + 1
    return datetime(year, month, day)


_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _parse_holiday(text: str) -> Optional[datetime]:
    """Return a holiday date if the text names a known holiday and includes a year."""
    m = _YEAR_RE.search(text)
    if not m:
        return None
    year = int(m.group(1))
    normed = _normalize(text)

    for name, month, day in _HOLIDAYS_FIXED:
        if _normalize(name) in normed:
            return datetime(year, month, day)

    for name, month, cs_dow, week in _HOLIDAYS_FLOATING:
        if _normalize(name) in normed:
            return _nth_weekday(year, month, cs_dow, week)

    if "easter" in normed:
        return _easter(year)

    return None


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

# Split regexes so holidays can be tried before the year-only fallback
_DATE_REGEXES_SPECIFIC = DATE_REGEXES[:-1]
_DATE_REGEX_YEAR_ONLY  = DATE_REGEXES[-1]


def _apply_regex(pattern: re.Pattern, text: str) -> Optional[datetime]:
    m = pattern.search(text)
    if not m:
        return None
    gd = m.groupdict()
    try:
        y = int(gd["y"])
        raw_month_name = gd.get("month") or gd.get("_m") or gd.get("month2") or gd.get("month3")
        raw_d = gd.get("d")
        if raw_month_name:
            mo = _month_to_int(raw_month_name)
        elif gd.get("m"):
            mo = int(gd["m"])
        else:
            mo = 1
        d = int(raw_d) if raw_d else 1
        return datetime(y, mo, d)
    except (ValueError, KeyError):
        return None


def parse_date_from_text(text: str) -> Optional[datetime]:
    """Return first parseable date found in text, or None."""
    # 1. Specific date patterns (full date, month+year)
    for pattern in _DATE_REGEXES_SPECIFIC:
        result = _apply_regex(pattern, text)
        if result:
            return result
    # 2. Holiday names (e.g. "Christmas 2000")
    result = _parse_holiday(text)
    if result:
        return result
    # 3. Year-only fallback (e.g. "Summer 2005" → Jan 1 2005)
    return _apply_regex(_DATE_REGEX_YEAR_ONLY, text)


def set_exif_date(filepath: Path, dt: datetime) -> None:
    """Set DateTimeOriginal and DateTimeDigitized EXIF tags on the image file."""
    ext = filepath.suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".tiff", ".tif"}:
        return

    date_str = dt.strftime("%Y:%m:%d 12:00:00").encode()

    # Build EXIF payload with only the tags we care about merged into existing data
    try:
        exif_dict = piexif.load(str(filepath))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = date_str
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = date_str
    exif_bytes = piexif.dump(exif_dict)

    try:
        if ext in {".jpg", ".jpeg"}:
            # In-place splice — no re-encoding, no quality loss
            piexif.insert(exif_bytes, str(filepath))
        else:
            # TIFF / PNG: re-save via Pillow with the new EXIF block
            with Image.open(filepath) as img:
                save_kwargs: dict = {"exif": exif_bytes}
                if ext in {".tiff", ".tif"}:
                    save_kwargs["compression"] = img.info.get("compression", "tiff_lzw")
                img.save(filepath, **save_kwargs)
    except Exception as exc:
        print(f"EXIF write failed ({filepath.name}): {exc}")


def set_file_times(filepath: Path, dt: datetime) -> None:
    """Set mtime (and attempt birth-time on macOS via SetFile)."""
    noon = dt.replace(hour=12, minute=0, second=0, microsecond=0)
    ts = noon.timestamp()
    os.utime(filepath, (ts, ts))
    # macOS birth-time via Xcode SetFile (graceful no-op if unavailable)
    if os.name == "posix":
        date_str = noon.strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(
            ["SetFile", "-d", date_str, str(filepath)],
            capture_output=True,
            check=False,
        )


def _rotate_fill(img: Image.Image, angle: float) -> Image.Image:
    """Rotate img by angle degrees CW, scaling up to fill original dimensions."""
    angle = angle % 360
    if angle == 0.0:
        return img
    W, H = img.size
    rotated = img.rotate(-angle, expand=True, resample=Image.BICUBIC)
    rW, rH = rotated.size
    scale = max(W / rW, H / rH)
    sW, sH = int(rW * scale), int(rH * scale)
    scaled = rotated.resize((sW, sH), Image.LANCZOS)
    left = (sW - W) // 2
    top  = (sH - H) // 2
    return scaled.crop((left, top, left + W, top + H))


def _parse_date_tag(date_text: str) -> Optional[datetime]:
    """Parse the date tag field into a datetime, trying common formats then free-text."""
    if not date_text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue
    return parse_date_from_text(date_text)


def _build_stem(box: str, caption: str, date_cap: str) -> str:
    parts = [p for p in (box, caption) if p]
    stem = " - ".join(parts)
    if date_cap:
        stem = f"{stem} ({date_cap})" if stem else f"({date_cap})"
    return stem


def unique_dest(directory: Path, filename: str) -> Path:
    """Return a path that doesn't exist, appending ' (N)' as needed."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = directory / filename
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def wait_for_file_ready(path: Path, timeout: float = 10.0) -> bool:
    """Wait until file size stabilises (scanner still writing), return True if ready."""
    deadline = time.time() + timeout
    prev_size = -1
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.3)
            continue
        if size == prev_size and size > 0:
            return True
        prev_size = size
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

class ScanHandler(FileSystemEventHandler):
    def __init__(self, event_queue: queue.Queue) -> None:
        super().__init__()
        self._q = event_queue

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() in IMAGE_EXTENSIONS:
            self._q.put(p)

    def on_moved(self, event):
        # Some scanners write to a temp name then rename
        if event.is_directory:
            return
        p = Path(event.dest_path)
        if p.suffix.lower() in IMAGE_EXTENSIONS:
            self._q.put(p)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class PhotoScannerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Scanner")
        self.root.minsize(900, 620)

        # Auto mode state
        self._file_queue = queue.Queue()
        self._observer: Optional[Observer] = None
        self._watching = False
        self._photo_ref = None

        # Manual mode state
        self._m_files: "list[Path]" = []
        self._m_photo_ref = None

        # Image editor state (Auto Mode)
        self._edit_path: Optional[Path] = None
        self._edit_discrete: Optional[Image.Image] = None  # full-res master
        self._edit_preview:  Optional[Image.Image] = None  # downsampled for UI
        self._edit_angle: float = 0.0
        self._crop: "list[int]" = [0, 0, 0, 0]
        self._disp_offset = (0, 0)
        self._disp_size = (0, 0)
        self._disp_full_size = (0, 0)
        self._drag_mode: Optional[str] = None
        self._drag_start = (0, 0)
        self._crop_start = [0, 0, 0, 0]
        self._editor_photo_ref = None
        self._icons: dict = {}
        self._window_focused = True

        self._load_icons()
        self._build_ui()
        self._load_prefs()
        self._poll_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    @staticmethod
    def _is_dark_mode() -> bool:
        try:
            out = subprocess.check_output(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            return out.lower() == "dark"
        except Exception:
            return False

    def _load_icons(self) -> None:
        base = Path(__file__).parent / "resources" / "light"
        max_size = (22, 22)
        names = {
            "ccw":       "rotate-ccw.png",
            "cw":        "rotate-cw.png",
            "180":       "180.png",
            "flip_h":    "flip-horizontal.png",
            "flip_v":    "flip-vertical.png",
            "punch_in":  "punch-in.png",
            "punch_out": "punch-out.png",
            "save":      "save.png",
        }
        for key, filename in names.items():
            try:
                img = Image.open(base / filename).convert("RGBA")
                img.thumbnail(max_size, Image.LANCZOS)
                self._icons[key] = ImageTk.PhotoImage(img)
            except Exception:
                self._icons[key] = None

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.bind("<FocusIn>",  lambda _: self._set_focused(True))
        self.root.bind("<FocusOut>", lambda _: self._set_focused(False))

        mod = "Command" if platform.system() == "Darwin" else "Control"
        self.root.bind(f"<{mod}-r>",                    lambda _: self._rotate_cw())
        self.root.bind(f"<{mod}-Shift-R>",              lambda _: self._rotate_ccw())
        self.root.bind_all("<KeyPress>", self._on_rotate180_key, add="+")
        self.root.bind(f"<{mod}-f>",                    lambda _: self._flip_h())
        self.root.bind(f"<{mod}-Shift-F>",              lambda _: self._flip_v())
        self.root.bind(f"<{mod}-i>",                    lambda _: self._punch_in())
        self.root.bind(f"<{mod}-o>",                    lambda _: self._punch_out())
        self.root.bind(f"<{mod}-s>",                    lambda _: self._save_edit())

        # Crop edge nudge shortcuts (5px per press)
        # No Shift: shrink from that side. Shift: expand from that side.
        self.root.bind(f"<{mod}-Left>",         lambda _: self._nudge_crop(right=-5))
        self.root.bind(f"<{mod}-Shift-Left>",   lambda _: self._nudge_crop(right=+5))
        self.root.bind(f"<{mod}-Right>",        lambda _: self._nudge_crop(left=+5))
        self.root.bind(f"<{mod}-Shift-Right>",  lambda _: self._nudge_crop(left=-5))
        self.root.bind(f"<{mod}-Up>",           lambda _: self._nudge_crop(bottom=-5))
        self.root.bind(f"<{mod}-Shift-Up>",     lambda _: self._nudge_crop(bottom=+5))
        self.root.bind(f"<{mod}-Down>",         lambda _: self._nudge_crop(top=+5))
        self.root.bind(f"<{mod}-Shift-Down>",   lambda _: self._nudge_crop(top=-5))

        nb = ttk.Notebook(self.root)
        nb.grid(row=0, column=0, sticky="nsew")

        auto_frame = ttk.Frame(nb)
        manual_frame = ttk.Frame(nb)
        nb.add(auto_frame, text="  Auto Mode  ")
        nb.add(manual_frame, text="  Manual Mode  ")

        self._build_auto_tab(auto_frame)
        self._build_manual_tab(manual_frame)

    def _build_auto_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        # Left control panel
        ctrl = ttk.Frame(parent, padding=12)
        ctrl.grid(row=0, column=0, sticky="ns")

        # Watch directory
        ttk.Label(ctrl, text="Watch Directory").grid(row=0, column=0, columnspan=2, sticky="w")
        self._watch_dir = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._watch_dir, width=32).grid(row=1, column=0, sticky="ew")
        ttk.Button(ctrl, text="…", width=3, command=self._browse_watch).grid(row=1, column=1, padx=(4, 0))

        # Target directory
        ttk.Label(ctrl, text="Move Files To").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._target_dir = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._target_dir, width=32).grid(row=3, column=0, sticky="ew")
        ttk.Button(ctrl, text="…", width=3, command=self._browse_target).grid(row=3, column=1, padx=(4, 0))

        # Box name
        ttk.Label(ctrl, text="Box Name").grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._box_name = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._box_name, width=32).grid(row=5, column=0, columnspan=2, sticky="ew")

        # Image caption
        ttk.Label(ctrl, text="Image Caption").grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._image_caption = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._image_caption, width=32).grid(row=7, column=0, columnspan=2, sticky="ew")

        # Date caption
        ttk.Label(ctrl, text="Date Caption").grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._date_caption = tk.StringVar()
        self._date_caption.trace_add("write", self._on_date_caption_change)
        ttk.Entry(ctrl, textvariable=self._date_caption, width=32).grid(row=9, column=0, columnspan=2, sticky="ew")

        # Date tag
        ttk.Label(ctrl, text="Date Tag").grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._date_str = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._date_str, width=32).grid(row=11, column=0, columnspan=2, sticky="ew")

        # Date hint
        self._date_hint = ttk.Label(ctrl, text="", foreground="#4a9a4a", font=("TkDefaultFont", 9))
        self._date_hint.grid(row=12, column=0, columnspan=2, sticky="w")

        # Filename preview
        self._filename_preview = ttk.Label(ctrl, text="", foreground="#555", font=("TkDefaultFont", 9),
                                           wraplength=240, justify=tk.LEFT)
        self._filename_preview.grid(row=13, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Start / Stop
        self._watch_btn = ttk.Button(ctrl, text="▶  Start Watching", command=self._toggle_watch)
        self._watch_btn.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        # Status indicator
        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(ctrl, textvariable=self._status_var, foreground="gray").grid(
            row=15, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        # Activity log
        ttk.Label(ctrl, text="Activity Log").grid(row=16, column=0, columnspan=2, sticky="w", pady=(12, 0))
        log_frame = ttk.Frame(ctrl)
        log_frame.grid(row=17, column=0, columnspan=2, sticky="nsew")
        ctrl.rowconfigure(17, weight=1)

        self._log = tk.Text(log_frame, width=34, height=12, state=tk.DISABLED,
                            wrap=tk.WORD, font=("TkFixedFont", 10))
        sb = ttk.Scrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Right panel — image editor
        editor_lf = ttk.LabelFrame(parent, text="Last Scanned Image", padding=4)
        editor_lf.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=10)
        editor_lf.rowconfigure(0, weight=1)
        editor_lf.columnconfigure(0, weight=1)

        # Canvas
        self._img_canvas = tk.Canvas(editor_lf, bg="#1e1e1e", highlightthickness=0)
        self._img_canvas.grid(row=0, column=0, sticky="nsew")
        self._img_canvas.bind("<Configure>",      lambda _: self._editor_redraw())
        self._img_canvas.bind("<ButtonPress-1>",  self._on_canvas_press)
        self._img_canvas.bind("<B1-Motion>",      self._on_canvas_drag)
        self._img_canvas.bind("<ButtonRelease-1>",self._on_canvas_release)
        self._img_canvas.bind("<Motion>",         self._on_canvas_motion)

        # Toolbar row 1 — transforms + save
        tb1 = ttk.Frame(editor_lf)
        tb1.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        def _icon_btn(parent, key: str, tooltip: str, command) -> tk.Button:
            ico = self._icons.get(key)
            try:
                bg = parent.cget("background")
            except tk.TclError:
                bg = self.root.cget("background")
            btn = tk.Button(parent, command=command, relief=tk.FLAT,
                            bd=0, padx=4, pady=4, cursor="hand2",
                            bg=bg, activebackground=bg, highlightthickness=0)
            if ico:
                btn.config(image=ico)
            else:
                btn.config(text=tooltip)
            btn.tooltip_text = tooltip
            return btn

        _icon_btn(tb1, "ccw",       "Rotate CCW",      self._rotate_ccw).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "cw",        "Rotate CW",       self._rotate_cw).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "180",       "Rotate 180°",     self._rotate_180).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "flip_h",    "Flip Horizontal", self._flip_h).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "flip_v",    "Flip Vertical",   self._flip_v).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "punch_in",  "Punch In",        self._punch_in).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "punch_out", "Punch Out",       self._punch_out).pack(side=tk.LEFT, padx=2)
        _icon_btn(tb1, "save",      "Save",            self._save_edit).pack(side=tk.RIGHT, padx=(2, 4))

        # Toolbar row 2 — angle slider
        tb2 = ttk.Frame(editor_lf)
        tb2.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(tb2, text="Angle:").pack(side=tk.LEFT, padx=(4, 2))
        self._angle_var = tk.DoubleVar(value=0.0)
        ttk.Scale(tb2, from_=-360, to=360, variable=self._angle_var,
                  command=self._on_angle_change,
                  orient=tk.HORIZONTAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._angle_label = ttk.Label(tb2, text="0.0°", width=6)
        self._angle_label.pack(side=tk.LEFT, padx=(0, 4))

        # Image info line
        self._img_info = ttk.Label(editor_lf, text="No image", anchor=tk.W,
                                   foreground="gray", font=("TkDefaultFont", 9))
        self._img_info.grid(row=3, column=0, sticky="ew", pady=(2, 2))

    def _build_manual_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        # Left control panel
        ctrl = ttk.Frame(parent, padding=12)
        ctrl.grid(row=0, column=0, sticky="ns")

        # Target directory
        ttk.Label(ctrl, text="Move Files To").grid(row=0, column=0, columnspan=2, sticky="w")
        self._m_target_dir = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._m_target_dir, width=32).grid(row=1, column=0, sticky="ew")
        ttk.Button(ctrl, text="…", width=3, command=self._m_browse_target).grid(row=1, column=1, padx=(4, 0))

        # Box name
        ttk.Label(ctrl, text="Box Name").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._m_box_name = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._m_box_name, width=32).grid(row=3, column=0, columnspan=2, sticky="ew")

        # Image caption
        ttk.Label(ctrl, text="Image Caption").grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._m_image_caption = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._m_image_caption, width=32).grid(row=5, column=0, columnspan=2, sticky="ew")

        # Date caption
        ttk.Label(ctrl, text="Date Caption").grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._m_date_caption = tk.StringVar()
        self._m_date_caption.trace_add("write", self._m_on_date_caption_change)
        ttk.Entry(ctrl, textvariable=self._m_date_caption, width=32).grid(row=7, column=0, columnspan=2, sticky="ew")

        # Date tag
        ttk.Label(ctrl, text="Date Tag").grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._m_date_str = tk.StringVar()
        ttk.Entry(ctrl, textvariable=self._m_date_str, width=32).grid(row=9, column=0, columnspan=2, sticky="ew")

        # Date hint
        self._m_date_hint = ttk.Label(ctrl, text="", foreground="#4a9a4a", font=("TkDefaultFont", 9))
        self._m_date_hint.grid(row=10, column=0, columnspan=2, sticky="w")

        # Filename preview
        self._m_filename_preview = ttk.Label(ctrl, text="", foreground="#555", font=("TkDefaultFont", 9),
                                             wraplength=240, justify=tk.LEFT)
        self._m_filename_preview.grid(row=11, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Process button
        self._m_process_btn = ttk.Button(ctrl, text="⚙  Process", command=self._m_process)
        self._m_process_btn.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        # Status
        self._m_status_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self._m_status_var, foreground="gray",
                  wraplength=240).grid(row=13, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Right panel
        right = ttk.Frame(parent, padding=(0, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=2)
        right.rowconfigure(2, weight=3)

        # Drop zone
        drop_lf = ttk.LabelFrame(right, text="Import Images", padding=6)
        drop_lf.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        drop_lf.columnconfigure(0, weight=1)

        self._m_drop_label = tk.Label(
            drop_lf,
            text="Drop images here  —  or  —  click to browse",
            background="#f0f4f8", foreground="#333333",
            relief="groove", cursor="hand2",
            padx=12, pady=18, anchor=tk.CENTER,
        )
        self._m_drop_label.grid(row=0, column=0, sticky="ew")
        self._m_drop_label.bind("<Button-1>", lambda _: self._m_select_files())
        self._m_drop_label.drop_target_register(DND_FILES)
        self._m_drop_label.dnd_bind("<<Drop>>", self._m_on_drop)

        # File list
        list_lf = ttk.LabelFrame(right, text="Files to Process", padding=6)
        list_lf.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        list_lf.columnconfigure(0, weight=1)
        list_lf.rowconfigure(0, weight=1)

        self._m_listbox = tk.Listbox(list_lf, selectmode=tk.EXTENDED, activestyle="none",
                                     font=("TkFixedFont", 10))
        list_sb = ttk.Scrollbar(list_lf, command=self._m_listbox.yview)
        self._m_listbox.configure(yscrollcommand=list_sb.set)
        self._m_listbox.grid(row=0, column=0, sticky="nsew")
        list_sb.grid(row=0, column=1, sticky="ns")

        btn_row = ttk.Frame(list_lf)
        btn_row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(btn_row, text="Remove Selected", command=self._m_remove_selected).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="Clear All", command=self._m_clear_files).pack(side=tk.RIGHT, padx=(0, 6))

        # Image preview
        preview_lf = ttk.LabelFrame(right, text="Last Processed Image", padding=6)
        preview_lf.grid(row=2, column=0, sticky="nsew")
        preview_lf.rowconfigure(0, weight=1)
        preview_lf.columnconfigure(0, weight=1)

        self._m_img_label = ttk.Label(preview_lf, text="No image yet", anchor=tk.CENTER)
        self._m_img_label.grid(row=0, column=0, sticky="nsew")

        self._m_img_info = ttk.Label(preview_lf, text="", anchor=tk.W, foreground="gray",
                                     font=("TkDefaultFont", 9))
        self._m_img_info.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    # ------------------------------------------------------------------
    # Directory browsing
    # ------------------------------------------------------------------

    def _browse_watch(self) -> None:
        d = filedialog.askdirectory(title="Select Watch Directory")
        if d:
            self._watch_dir.set(d)

    def _browse_target(self) -> None:
        d = filedialog.askdirectory(title="Select Target Directory")
        if d:
            self._target_dir.set(d)

    def _m_browse_target(self) -> None:
        d = filedialog.askdirectory(title="Select Target Directory")
        if d:
            self._m_target_dir.set(d)

    # ------------------------------------------------------------------
    # Manual mode — file import
    # ------------------------------------------------------------------

    def _m_select_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select Images",
            filetypes=[("Image files", " ".join(f"*{e}" for e in IMAGE_EXTENSIONS)),
                       ("All files", "*.*")],
        )
        if paths:
            self._m_add_files([Path(p) for p in paths])

    def _m_on_drop(self, event) -> None:
        # tkinterdnd2 wraps space-containing paths in braces
        raw = event.data.strip()
        paths = []
        while raw:
            if raw.startswith("{"):
                end = raw.index("}")
                paths.append(Path(raw[1:end]))
                raw = raw[end + 1:].strip()
            else:
                parts = raw.split(None, 1)
                paths.append(Path(parts[0]))
                raw = parts[1].strip() if len(parts) > 1 else ""
        self._m_add_files(paths)

    def _m_add_files(self, paths: list[Path]) -> None:
        for p in paths:
            if p.suffix.lower() in IMAGE_EXTENSIONS and p not in self._m_files:
                self._m_files.append(p)
                self._m_listbox.insert(tk.END, p.name)

    def _m_remove_selected(self) -> None:
        for idx in reversed(self._m_listbox.curselection()):
            self._m_listbox.delete(idx)
            self._m_files.pop(idx)

    def _m_clear_files(self) -> None:
        self._m_listbox.delete(0, tk.END)
        self._m_files.clear()

    # ------------------------------------------------------------------
    # Filename construction + date auto-detect
    # ------------------------------------------------------------------

    def _build_filename(self) -> str:
        return _build_stem(
            self._box_name.get().strip(),
            self._image_caption.get().strip(),
            self._date_caption.get().strip(),
        )

    def _on_date_caption_change(self, *_) -> None:
        preview = self._build_filename()
        self._filename_preview.config(text=f"→ {preview}" if preview else "")
        self._sync_date_hint(self._date_caption, self._date_str, self._date_hint)

    def _m_build_filename(self) -> str:
        return _build_stem(
            self._m_box_name.get().strip(),
            self._m_image_caption.get().strip(),
            self._m_date_caption.get().strip(),
        )

    def _m_on_date_caption_change(self, *_) -> None:
        preview = self._m_build_filename()
        self._m_filename_preview.config(text=f"→ {preview}" if preview else "")
        self._sync_date_hint(self._m_date_caption, self._m_date_str, self._m_date_hint)

    @staticmethod
    def _sync_date_hint(caption_var: tk.StringVar, date_var: tk.StringVar,
                        hint_label: ttk.Label) -> None:
        dt = parse_date_from_text(caption_var.get())
        if dt:
            date_var.set(dt.strftime("%Y-%m-%d"))
            hint_label.config(text=f"Detected: {dt.strftime('%B %d, %Y')}", foreground="#4a9a4a")
        else:
            hint_label.config(text="")

    # ------------------------------------------------------------------
    # Watching
    # ------------------------------------------------------------------

    def _toggle_watch(self) -> None:
        if self._watching:
            self._stop_watching()
        else:
            self._start_watching()

    def _start_watching(self) -> None:
        watch_dir = self._watch_dir.get().strip()
        target_dir = self._target_dir.get().strip()

        if not watch_dir or not Path(watch_dir).is_dir():
            messagebox.showerror("Error", "Please choose a valid watch directory.")
            return
        if not target_dir:
            messagebox.showerror("Error", "Please choose a target directory.")
            return

        Path(target_dir).mkdir(parents=True, exist_ok=True)

        self._save_prefs()
        self._observer = Observer()
        self._observer.schedule(ScanHandler(self._file_queue), watch_dir, recursive=False)
        self._observer.start()
        self._watching = True
        self._watch_btn.config(text="■  Stop Watching")
        self._status_var.set(f"Watching  {watch_dir}")
        self._log_line(f"Started watching: {watch_dir}")

    def _stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self._watching = False
        self._watch_btn.config(text="▶  Start Watching")
        self._status_var.set("Idle")
        self._log_line("Stopped watching.")

    # ------------------------------------------------------------------
    # Queue polling (runs on main thread)
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                src = self._file_queue.get_nowait()
                threading.Thread(target=self._process_file, args=(src,), daemon=True).start()
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    # ------------------------------------------------------------------
    # File processing (runs on background thread)
    # ------------------------------------------------------------------

    def _process_file(self, src: Path) -> None:
        if not src.exists():
            return  # duplicate event for a file already moved

        self._ui_log(f"Detected: {src.name}")

        if not wait_for_file_ready(src):
            self._ui_log(f"Timeout waiting for {src.name} — skipped.")
            return

        target_dir = Path(self._target_dir.get().strip())
        filename_base = self._build_filename() or src.stem
        date_text = self._date_str.get().strip()

        if not target_dir.is_dir():
            self._ui_log("Error: target directory missing.")
            return

        dest = unique_dest(target_dir, f"{filename_base}{src.suffix.lower()}")

        dt = _parse_date_tag(date_text)

        try:
            shutil.move(str(src), str(dest))
            self._ui_log(f"Moved  →  {dest.name}")
        except Exception as exc:
            self._ui_log(f"Move failed: {exc}")
            return

        if dt:
            set_exif_date(dest, dt)
            set_file_times(dest, dt)
            self._ui_log(f"Date set: {dt.strftime('%Y-%m-%d')}")

        # Update UI from main thread
        self.root.after(0, lambda p=dest: self._show_image(p))

    # ------------------------------------------------------------------
    # Image preview (main thread)
    # ------------------------------------------------------------------

    def _show_image(self, path: Path) -> None:
        self.root.after(0, lambda p=path: self._editor_load(p))

    # ------------------------------------------------------------------
    # Image editor (Auto Mode)
    # ------------------------------------------------------------------

    def _editor_load(self, path: Path) -> None:
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            self._ui_log(f"Load error: {exc}")
            return
        self._edit_path = path
        self._edit_discrete = img.copy()
        self._edit_preview  = self._make_preview(img)
        self._edit_angle = 0.0
        self._angle_var.set(0.0)
        self._angle_label.config(text="0.0°")
        self._reset_crop()
        self._editor_redraw()
        self._img_info.config(text=f"{path.name}   ({img.width} × {img.height} px)")

    @staticmethod
    def _make_preview(img: Image.Image) -> Image.Image:
        preview = img.copy()
        preview.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.LANCZOS)
        return preview

    def _get_display_image(self) -> Optional[Image.Image]:
        """Returns the preview-resolution image with slider rotation applied."""
        if self._edit_preview is None:
            return None
        return _rotate_fill(self._edit_preview, self._edit_angle)

    def _reset_crop(self) -> None:
        disp = self._get_display_image()
        self._crop = [0, 0, disp.width, disp.height] if disp else [0, 0, 0, 0]

    # -- drawing --

    def _on_rotate180_key(self, event) -> None:
        if platform.system() == "Darwin":
            # Option key transforms the keysym so we can't rely on the binding
            # string. Instead check state bits directly.
            # macOS virtual keycode 15 = physical R key on any layout.
            # Command=0x8, Shift=0x1. Option's bit varies by Tk version,
            # so accept any extra modifier beyond Shift, CapsLock, and Command.
            shift   = bool(event.state & 0x0001)
            command = bool(event.state & 0x0008)
            option  = bool(event.state & ~(0x0001 | 0x0002 | 0x0008) & 0xFFFF)
            if shift and command and option and event.keycode == 15:
                self._rotate_180()
        else:
            # Windows/Linux: Ctrl+Alt+Shift+R
            # Alt state bit on Windows is 0x20000; keycode not transformed.
            shift = bool(event.state & 0x0001)
            ctrl  = bool(event.state & 0x0004)
            alt   = bool(event.state & 0x20000)
            if shift and ctrl and alt and event.keysym.lower() == 'r':
                self._rotate_180()

    def _set_focused(self, focused: bool) -> None:
        self._window_focused = focused
        self._editor_redraw()

    def _editor_redraw(self) -> None:
        canvas = self._img_canvas
        canvas.delete("all")

        disp = self._get_display_image()
        if disp is None:
            cw = canvas.winfo_width() or 200
            ch = canvas.winfo_height() or 150
            canvas.create_text(cw // 2, ch // 2, text="No image yet",
                                fill="#888", font=("TkDefaultFont", 12))
            return

        cw = canvas.winfo_width() or 400
        ch = canvas.winfo_height() or 400

        thumb = disp.copy()
        thumb.thumbnail((cw, ch), Image.LANCZOS)
        tw, th = thumb.size

        ox = (cw - tw) // 2
        oy = (ch - th) // 2
        self._disp_offset    = (ox, oy)
        self._disp_size      = (tw, th)
        self._disp_full_size = (disp.width, disp.height)

        photo = ImageTk.PhotoImage(thumb)
        self._editor_photo_ref = photo
        canvas.create_image(ox, oy, anchor=tk.NW, image=photo)

        if not self._window_focused:
            return

        cx0, cy0, cx1, cy1 = self._canvas_crop_rect()

        # Dim areas outside the crop
        DIM, STI = "#000000", "gray50"
        for x1r, y1r, x2r, y2r in [
            (ox, oy,    ox+tw, cy0),   # top
            (ox, cy1,   ox+tw, oy+th), # bottom
            (ox, cy0,   cx0,   cy1),   # left
            (cx1, cy0,  ox+tw, cy1),   # right
        ]:
            if x2r > x1r and y2r > y1r:
                canvas.create_rectangle(x1r, y1r, x2r, y2r,
                                        fill=DIM, stipple=STI, outline="")

        # Crop border
        canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="white", width=2)

        # Handles
        for hx, hy in self._handle_positions():
            canvas.create_rectangle(hx - HANDLE_R, hy - HANDLE_R,
                                     hx + HANDLE_R, hy + HANDLE_R,
                                     fill="white", outline="#444", width=1)

    def _canvas_crop_rect(self) -> tuple:
        ox, oy = self._disp_offset
        tw, th = self._disp_size
        fw, fh = self._disp_full_size
        if fw == 0 or fh == 0:
            return (ox, oy, ox + tw, oy + th)
        sx, sy = tw / fw, th / fh
        x0, y0, x1, y1 = self._crop
        return (int(ox + x0*sx), int(oy + y0*sy),
                int(ox + x1*sx), int(oy + y1*sy))

    def _canvas_to_img(self, cx: int, cy: int) -> tuple:
        ox, oy = self._disp_offset
        tw, th = self._disp_size
        fw, fh = self._disp_full_size
        if tw == 0 or th == 0:
            return (0, 0)
        return (int((cx - ox) / tw * fw), int((cy - oy) / th * fh))

    def _handle_positions(self) -> list:
        cx0, cy0, cx1, cy1 = self._canvas_crop_rect()
        mx, my = (cx0 + cx1) // 2, (cy0 + cy1) // 2
        return [(cx0, cy0), (mx, cy0), (cx1, cy0),
                (cx0, my),             (cx1, my),
                (cx0, cy1), (mx, cy1), (cx1, cy1)]

    _HANDLE_NAMES = ["tl", "tc", "tr", "ml", "mr", "bl", "bc", "br"]
    _HANDLE_CURSORS = {
        "tl": "top_left_corner",  "br": "bottom_right_corner",
        "tr": "top_right_corner", "bl": "bottom_left_corner",
        "tc": "top_side",  "bc": "bottom_side",
        "ml": "left_side", "mr": "right_side",
        "move": "fleur",
    }

    def _hit_test(self, cx: int, cy: int) -> Optional[str]:
        HR = HANDLE_R + 2
        for name, (hx, hy) in zip(self._HANDLE_NAMES, self._handle_positions()):
            if abs(cx - hx) <= HR and abs(cy - hy) <= HR:
                return name
        bx0, by0, bx1, by1 = self._canvas_crop_rect()
        if bx0 <= cx <= bx1 and by0 <= cy <= by1:
            return "move"
        return None

    # -- mouse events --

    def _on_canvas_press(self, event) -> None:
        if self._edit_discrete is None:
            return
        mode = self._hit_test(event.x, event.y)
        if mode:
            self._drag_mode  = mode
            self._drag_start = (event.x, event.y)
            self._crop_start = list(self._crop)

    def _on_canvas_drag(self, event) -> None:
        if not self._drag_mode or self._edit_discrete is None:
            return
        fw, fh = self._disp_full_size
        tw, th = self._disp_size
        if tw == 0 or th == 0:
            return

        dx = int((event.x - self._drag_start[0]) / tw * fw)
        dy = int((event.y - self._drag_start[1]) / th * fh)
        x0, y0, x1, y1 = self._crop_start
        mode = self._drag_mode

        if mode == "move":
            w, h = x1 - x0, y1 - y0
            nx0 = max(0, min(fw - w, x0 + dx))
            ny0 = max(0, min(fh - h, y0 + dy))
            self._crop = [nx0, ny0, nx0 + w, ny0 + h]
        else:
            nx0, ny0, nx1, ny1 = x0, y0, x1, y1
            if "l" in mode: nx0 = max(0,       min(x1 - MIN_CROP, x0 + dx))
            if "r" in mode: nx1 = max(x0 + MIN_CROP, min(fw,      x1 + dx))
            if "t" in mode: ny0 = max(0,       min(y1 - MIN_CROP, y0 + dy))
            if "b" in mode: ny1 = max(y0 + MIN_CROP, min(fh,      y1 + dy))
            self._crop = [nx0, ny0, nx1, ny1]

        self._editor_redraw()

    def _on_canvas_release(self, *_) -> None:
        self._drag_mode = None

    def _on_canvas_motion(self, event) -> None:
        mode = self._hit_test(event.x, event.y)
        self._img_canvas.config(
            cursor=self._HANDLE_CURSORS.get(mode, "crosshair")
        )

    # -- transforms --

    def _rotate_cw(self) -> None:
        if self._edit_discrete is None:
            return
        _, ph = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        self._edit_discrete = self._edit_discrete.rotate(-90, expand=True)
        self._edit_preview  = self._edit_preview.rotate(-90, expand=True)
        self._crop = [ph - y1, x0, ph - y0, x1]
        self._editor_redraw()

    def _rotate_ccw(self) -> None:
        if self._edit_discrete is None:
            return
        pw, _ = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        self._edit_discrete = self._edit_discrete.rotate(90, expand=True)
        self._edit_preview  = self._edit_preview.rotate(90, expand=True)
        self._crop = [y0, pw - x1, y1, pw - x0]
        self._editor_redraw()

    def _rotate_180(self) -> None:
        if self._edit_discrete is None:
            return
        pw, ph = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        self._edit_discrete = self._edit_discrete.rotate(180, expand=True)
        self._edit_preview  = self._edit_preview.rotate(180, expand=True)
        self._crop = [pw - x1, ph - y1, pw - x0, ph - y0]
        self._editor_redraw()

    def _flip_h(self) -> None:
        if self._edit_discrete is None:
            return
        pw, _ = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        self._edit_discrete = self._edit_discrete.transpose(Image.FLIP_LEFT_RIGHT)
        self._edit_preview  = self._edit_preview.transpose(Image.FLIP_LEFT_RIGHT)
        self._crop = [pw - x1, y0, pw - x0, y1]
        self._editor_redraw()

    def _flip_v(self) -> None:
        if self._edit_discrete is None:
            return
        _, ph = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        self._edit_discrete = self._edit_discrete.transpose(Image.FLIP_TOP_BOTTOM)
        self._edit_preview  = self._edit_preview.transpose(Image.FLIP_TOP_BOTTOM)
        self._crop = [x0, ph - y1, x1, ph - y0]
        self._editor_redraw()

    def _nudge_crop(self, left: int = 0, right: int = 0,
                    top: int = 0, bottom: int = 0) -> None:
        if self._edit_preview is None:
            return
        pw, ph = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        x0 = max(0,  min(x1 - MIN_CROP, x0 + left))
        x1 = min(pw, max(x0 + MIN_CROP, x1 + right))
        y0 = max(0,  min(y1 - MIN_CROP, y0 + top))
        y1 = min(ph, max(y0 + MIN_CROP, y1 + bottom))
        self._crop = [x0, y0, x1, y1]
        self._editor_redraw()

    def _punch_crop(self, delta: int) -> None:
        if self._edit_preview is None:
            return
        pw, ph = self._edit_preview.size
        x0, y0, x1, y1 = self._crop
        x0 = max(0,  x0 + delta)
        y0 = max(0,  y0 + delta)
        x1 = min(pw, x1 - delta)
        y1 = min(ph, y1 - delta)
        if x1 - x0 < MIN_CROP or y1 - y0 < MIN_CROP:
            return
        self._crop = [x0, y0, x1, y1]
        self._editor_redraw()

    def _punch_in(self)  -> None: self._punch_crop(+5)
    def _punch_out(self) -> None: self._punch_crop(-5)

    def _on_angle_change(self, val) -> None:
        self._edit_angle = float(val)
        self._angle_label.config(text=f"{self._edit_angle:.1f}°")
        self._editor_redraw()

    # -- save --

    def _save_edit(self) -> None:
        if not self._edit_path or self._edit_discrete is None:
            return

        # Apply rotation to full-res master
        full = _rotate_fill(self._edit_discrete, self._edit_angle)

        # Map crop rect from preview space → full-res space
        pw, ph = self._edit_preview.size
        fw, fh = full.size
        sx, sy = fw / pw, fh / ph
        x0, y0, x1, y1 = self._crop
        result = full.crop((
            int(x0 * sx), int(y0 * sy),
            int(x1 * sx), int(y1 * sy),
        ))

        save_kwargs: dict = {}
        ext = self._edit_path.suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            try:
                exif_dict = piexif.load(str(self._edit_path))
                exif_dict.get("0th", {}).pop(piexif.ImageIFD.Orientation, None)
                exif_dict["1st"] = {}
                exif_dict["thumbnail"] = None
                save_kwargs["exif"] = piexif.dump(exif_dict)
            except Exception:
                pass

        stat = self._edit_path.stat()
        try:
            result.save(str(self._edit_path), **save_kwargs)
        except Exception as exc:
            self._ui_log(f"Save failed: {exc}")
            return
        os.utime(self._edit_path, (stat.st_atime, stat.st_mtime))

        # Reset editor to saved result
        self._edit_discrete = result.copy()
        self._edit_preview  = self._make_preview(result)
        self._edit_angle = 0.0
        self._angle_var.set(0.0)
        self._angle_label.config(text="0.0°")
        self._reset_crop()
        self._editor_redraw()
        self._img_info.config(
            text=f"{self._edit_path.name}   ({result.width} × {result.height} px)")
        self._ui_log(f"Saved: {self._edit_path.name}")

    # ------------------------------------------------------------------
    # Manual mode — processing
    # ------------------------------------------------------------------

    def _m_process(self) -> None:
        if not self._m_files:
            messagebox.showinfo("No files", "Add images to process first.")
            return

        target_dir_str = self._m_target_dir.get().strip()
        if not target_dir_str:
            messagebox.showerror("Error", "Please choose a target directory.")
            return

        target_dir = Path(target_dir_str)
        target_dir.mkdir(parents=True, exist_ok=True)

        filename_base = self._m_build_filename()
        date_text = self._m_date_str.get().strip()
        dt = _parse_date_tag(date_text)

        self._save_prefs()
        self._m_process_btn.config(state=tk.DISABLED)

        def run():
            processed, errors = 0, 0
            last_dest: Optional[Path] = None
            for src in list(self._m_files):
                if not src.exists():
                    self._m_ui_status(f"Not found, skipped: {src.name}")
                    errors += 1
                    continue
                stem = filename_base or src.stem
                dest = unique_dest(target_dir, f"{stem}{src.suffix.lower()}")
                try:
                    shutil.move(str(src), str(dest))
                except Exception as exc:
                    self._m_ui_status(f"Move failed: {src.name} — {exc}")
                    errors += 1
                    continue
                if dt:
                    set_exif_date(dest, dt)
                    set_file_times(dest, dt)
                processed += 1
                last_dest = dest

            summary = f"Done: {processed} processed"
            if errors:
                summary += f", {errors} error(s)"
            self.root.after(0, lambda: self._m_after_process(last_dest, summary))

        threading.Thread(target=run, daemon=True).start()

    def _m_after_process(self, last_dest: Optional[Path], summary: str) -> None:
        self._m_status_var.set(summary)
        self._m_process_btn.config(state=tk.NORMAL)
        self._m_clear_files()
        if last_dest:
            self._display_image(last_dest, self._m_img_label, self._m_img_info,
                                "_m_photo_ref", lambda msg: self._m_status_var.set(msg))

    def _m_ui_status(self, msg: str) -> None:
        self.root.after(0, lambda m=msg: self._m_status_var.set(m))

    def _m_show_image(self, path: Path) -> None:
        self._display_image(path, self._m_img_label, self._m_img_info,
                            "_m_photo_ref", lambda msg: self._m_status_var.set(msg))

    # ------------------------------------------------------------------
    # Shared image display
    # ------------------------------------------------------------------

    def _display_image(self, path: Path, label: ttk.Label, info: ttk.Label,
                       ref_attr: str, log_fn) -> None:
        try:
            with Image.open(path) as img:
                img.load()
                orig_w, orig_h = img.size
                self.root.update_idletasks()
                box_w = max(label.winfo_width() - 10, 300)
                box_h = max(label.winfo_height() - 10, 300)
                img.thumbnail((box_w, box_h), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            setattr(self, ref_attr, photo)
            label.config(image=photo, text="")
            info.config(text=f"{path.name}   ({orig_w} × {orig_h} px)")
        except Exception as exc:
            log_fn(f"Preview error: {exc}")

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def _load_prefs(self) -> None:
        try:
            prefs = json.loads(PREFS_FILE.read_text())
        except Exception:
            return
        self._watch_dir.set(prefs.get("watch_dir", ""))
        self._target_dir.set(prefs.get("target_dir", ""))
        self._box_name.set(prefs.get("box_name", ""))
        self._image_caption.set(prefs.get("image_caption", ""))
        self._date_caption.set(prefs.get("date_caption", ""))
        self._date_str.set(prefs.get("date", ""))
        self._m_target_dir.set(prefs.get("m_target_dir", ""))
        self._m_box_name.set(prefs.get("m_box_name", ""))
        self._m_image_caption.set(prefs.get("m_image_caption", ""))
        self._m_date_caption.set(prefs.get("m_date_caption", ""))
        self._m_date_str.set(prefs.get("m_date", ""))

    def _save_prefs(self) -> None:
        prefs = {
            "watch_dir": self._watch_dir.get(),
            "target_dir": self._target_dir.get(),
            "box_name": self._box_name.get(),
            "image_caption": self._image_caption.get(),
            "date_caption": self._date_caption.get(),
            "date": self._date_str.get(),
            "m_target_dir": self._m_target_dir.get(),
            "m_box_name": self._m_box_name.get(),
            "m_image_caption": self._m_image_caption.get(),
            "m_date_caption": self._m_date_caption.get(),
            "m_date": self._m_date_str.get(),
        }
        try:
            PREFS_FILE.write_text(json.dumps(prefs, indent=2))
        except Exception as exc:
            print(f"Could not save prefs: {exc}")

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_line(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, f"[{ts}] {msg}\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _ui_log(self, msg: str) -> None:
        """Thread-safe log from background threads."""
        self.root.after(0, lambda m=msg: self._log_line(m))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        self._save_prefs()
        self._stop_watching()
        self.root.destroy()


# ---------------------------------------------------------------------------

def main() -> None:
    root = TkinterDnD.Tk()
    app = PhotoScannerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
