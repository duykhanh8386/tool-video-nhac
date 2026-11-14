"""Native Windows interface for the standalone YouTube MP3 Downloader."""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import ssl
import sys
import threading
from ctypes import wintypes
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import certifi
from PIL import Image, ImageOps

from downloader_app.downloader import (
    ListingStopped,
    YoutubeMp3DownloadController,
    app_data_dir,
    extract_youtube_entries,
    parse_youtube_urls,
)

if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    comctl32 = ctypes.windll.comctl32
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
else:
    user32 = None
    kernel32 = None
    gdi32 = None
    comctl32 = None
    shell32 = None
    ole32 = None

# Windows types & structures
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HICON),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwICC", wintypes.DWORD),
    ]


class LVCOLUMNW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT),
        ("fmt", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("iOrder", ctypes.c_int),
        ("cxMin", ctypes.c_int),
        ("cxDefault", ctypes.c_int),
        ("cxIdeal", ctypes.c_int),
    ]


class LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wintypes.UINT),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


class NMHDR(ctypes.Structure):
    _fields_ = [
        ("hwndFrom", wintypes.HWND),
        ("idFrom", ctypes.c_size_t),
        ("code", wintypes.UINT),
    ]


class NMLISTVIEW(ctypes.Structure):
    _fields_ = [
        ("hdr", NMHDR),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("uNewState", wintypes.UINT),
        ("uOldState", wintypes.UINT),
        ("uChanged", wintypes.UINT),
        ("ptAction", wintypes.POINT),
        ("lParam", wintypes.LPARAM),
    ]


# Window Styles & Messages
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
WS_EX_CLIENTEDGE = 0x00000200

# Edit control styles
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_WANTRETURN = 0x1000
ES_NUMBER = 0x2000
ES_READONLY = 0x0800

# Combobox styles
CBS_DROPDOWNLIST = 0x0003
CBS_HASSTRINGS = 0x0200
CB_ADDSTRING = 0x0143
CB_SETCURSEL = 0x014E
CB_GETCURSEL = 0x0147
CBN_SELCHANGE = 1

# Static styles
SS_BITMAP = 0x000E
SS_LEFT = 0x0000
STM_SETIMAGE = 0x0172
IMAGE_BITMAP = 0

# Progress bar styles & messages
PBS_SMOOTH = 0x0001
PBM_SETRANGE32 = 0x0406
PBM_SETPOS = 0x0402
PBM_SETBARCOLOR = 0x0409
PBM_SETBKCOLOR = 0x2001

# ListView styles & messages
LVS_REPORT = 0x0001
LVS_SINGLESEL = 0x0004
LVS_SHOWSELALWAYS = 0x0008
LVS_EX_GRIDLINES = 0x00000001
LVS_EX_CHECKBOXES = 0x00000004
LVS_EX_FULLROWSELECT = 0x00000020
LVS_EX_DOUBLEBUFFER = 0x00010000

LVM_FIRST = 0x1000
LVM_SETITEMW = LVM_FIRST + 76
LVM_INSERTITEMW = LVM_FIRST + 77
LVM_DELETEALLITEMS = LVM_FIRST + 9
LVM_GETITEMTEXTW = LVM_FIRST + 115
LVM_SETITEMTEXTW = LVM_FIRST + 116
LVM_GETITEMSTATE = LVM_FIRST + 44
LVM_GETNEXTITEM = LVM_FIRST + 12
LVM_INSERTCOLUMNW = LVM_FIRST + 97
LVM_SETCOLUMNWIDTH = LVM_FIRST + 30
LVM_SETEXTENDEDLISTVIEWSTYLE = LVM_FIRST + 54
LVM_SETIMAGELIST = LVM_FIRST + 3
LVSIL_SMALL = 1

LVIF_TEXT = 0x0001
LVIF_IMAGE = 0x0002
LVIF_STATE = 0x0008
LVCF_FMT = 0x0001
LVCF_WIDTH = 0x0002
LVCF_TEXT = 0x0004
LVCF_SUBITEM = 0x0008
LVCFMT_LEFT = 0x0000
LVCFMT_RIGHT = 0x0001
LVCFMT_CENTER = 0x0002
LVIS_SELECTED = 0x0002
LVIS_STATEIMAGEMASK = 0xF000
LVNI_SELECTED = 0x0002
LVN_FIRST = -100
LVN_ITEMCHANGED = LVN_FIRST - 1

# Standard Messages
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_COMMAND = 0x0111
WM_NOTIFY = 0x004E
WM_CLOSE = 0x0010
WM_SETFONT = 0x0030
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLOREDIT = 0x0133
WM_CTLCOLORBTN = 0x0135
WM_ERASEBKGND = 0x0014

# Custom App Messages
WM_APP_REFRESH = 0x8001
WM_APP_LIST_DONE = 0x8002
WM_APP_PREVIEW_DONE = 0x8003
WM_APP_THUMBNAIL_DONE = 0x8004

# Control IDs
ID_URLS = 101
ID_QUANTITY = 102
ID_LIST = 103
ID_STOP_LIST = 104
ID_BROWSER_SESSION = 105
ID_FOLDER = 106
ID_BROWSE = 107
ID_OPEN = 108
ID_SAVE = 109
ID_START = 110
ID_STOP = 111
ID_ITEMS = 112
ID_SELECT_ALL = 113
ID_SELECT_NONE = 114
ID_PROGRESS = 115
ID_PROGRESS_TEXT = 116

BROWSER_SESSION_OPTIONS = [
    ("auto", "Tự động (khuyên dùng)"),
    ("none", "Không dùng cookie"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("edge", "Microsoft Edge"),
]

# Set up ctypes prototypes
if user32:
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.EnableWindow.restype = wintypes.BOOL
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SetWindowTextW.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None

if gdi32:
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetBkMode.restype = ctypes.c_int
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.SetTextColor.restype = wintypes.COLORREF

if comctl32:
    comctl32.ImageList_Create.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, ctypes.c_int, ctypes.c_int]
    comctl32.ImageList_Create.restype = ctypes.c_void_p
    comctl32.ImageList_Destroy.argtypes = [ctypes.c_void_p]
    comctl32.ImageList_Destroy.restype = wintypes.BOOL
    comctl32.ImageList_Add.argtypes = [ctypes.c_void_p, wintypes.HBITMAP, wintypes.HBITMAP]
    comctl32.ImageList_Add.restype = ctypes.c_int


def _loword(lparam: int) -> int:
    return int(lparam) & 0xFFFF


def _pointer_value(struct_instance: ctypes.Structure) -> int:
    return ctypes.cast(ctypes.byref(struct_instance), ctypes.c_void_p).value or 0


def _create_browse_info(hwnd: int) -> tuple[BROWSEINFOW, ctypes.Array[ctypes.c_wchar]]:
    """Create a folder-picker structure with a real writable LPWSTR buffer."""
    display_name = ctypes.create_unicode_buffer(260)
    info = BROWSEINFOW()
    info.hwndOwner = hwnd
    info.pszDisplayName = ctypes.cast(display_name, wintypes.LPWSTR)
    info.lpszTitle = "Chọn thư mục lưu MP3"
    info.ulFlags = 0x00000041  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    return info, display_name


def _cached_thumbnail_bitmap(thumbnail_url: str, size: tuple[int, int]) -> Path | None:
    """Download, resize, and atomically cache a thumbnail as a Win32 BMP."""
    cache_dir = app_data_dir() / "thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(thumbnail_url.encode("utf-8")).hexdigest()
    width, height = size
    path = cache_dir / f"{digest}-{width}x{height}.bmp"
    if path.is_file():
        return path

    request = Request(
        thumbnail_url,
        headers={"User-Agent": "Mozilla/5.0 YouTubeMp3Downloader"},
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(request, context=ssl_context, timeout=12) as response:
            payload = response.read()
    except Exception:
        return None

    try:
        with Image.open(BytesIO(payload)) as img:
            fitted = ImageOps.fit(img.convert("RGB"), size, Image.Resampling.LANCZOS)
            temp_path = path.with_suffix(".tmp")
            fitted.save(temp_path, "BMP")
            os.replace(temp_path, path)
            return path
    except Exception:
        return None


class NativeYoutubeMp3App:
    """Modern Windows interface with smooth scrolling and overall progress bar."""

    def __init__(self, controller: YoutubeMp3DownloadController | None = None) -> None:
        if sys.platform != "win32":
            raise RuntimeError("YouTube MP3 Downloader hiện hỗ trợ Windows.")
        self.hwnd: int | None = None
        self.controls: dict[str, int] = {}
        self._listing = False
        self._listing_stopping = False
        self._listing_count = 0
        self._list_stop_event = threading.Event()
        self._list_result: tuple[list[dict[str, Any]] | None, Exception | None] = (None, None)
        self._list_lock = threading.Lock()

        self._preview_generation = 0
        self._preview_url = ""
        self._preview_bitmap: int | None = None
        self._preview_result: tuple[int, Path | None, Exception | None] = (0, None, None)
        self._preview_lock = threading.Lock()

        self._thumbnail_generation = 0
        self._thumbnail_results: list[tuple[int, Path | None]] = []
        self._thumbnail_lock = threading.Lock()
        self._thumbnail_image_list: int | None = None

        self._rendered_urls: tuple[str, ...] = ()
        self._refreshing_items = False
        self._wndproc = WNDPROC(self._window_proc)

        self.controller = controller or YoutubeMp3DownloadController()
        self.controller.on_change = self._signal_change

        # Fonts
        self.title_font = self._create_font(24, 700)
        self.sub_font = self._create_font(14, 400)
        self.bold_font = self._create_font(15, 600)
        self.font = self._create_font(15, 400)

        # Brushes & Colors
        # Modern Slate Light Theme: Background #F8FAFC (RGB: 248, 250, 252)
        self._bg_color = 0xFCFAF8  # COLORREF (BGR)
        self._bg_brush = gdi32.CreateSolidBrush(self._bg_color)
        self._text_color = 0x0F172A
        self._muted_color = 0x64748B
        self._primary_color = 0x0284C7

    def _create_font(self, height: int, weight: int) -> int:
        return gdi32.CreateFontW(
            -height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI"
        )

    def run(self) -> None:
        init_ctrls = INITCOMMONCONTROLSEX()
        init_ctrls.dwSize = ctypes.sizeof(INITCOMMONCONTROLSEX)
        init_ctrls.dwICC = 0x00000001 | 0x00000020  # ICC_LISTVIEW_CLASSES | ICC_PROGRESS_CLASS
        comctl32.InitCommonControlsEx(ctypes.byref(init_ctrls))

        instance = kernel32.GetModuleHandleW(None)
        class_name = "YouTubeMp3DownloaderWindow"

        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.style = 0x0003  # CS_HREDRAW | CS_VREDRAW
        wndclass.lpfnWndProc = self._wndproc
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = instance
        wndclass.hIcon = self._load_icon(instance)
        wndclass.hCursor = user32.LoadCursorW(0, 32512)  # IDC_ARROW
        wndclass.hbrBackground = self._bg_brush
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = class_name
        wndclass.hIconSm = wndclass.hIcon

        user32.RegisterClassExW(ctypes.byref(wndclass))

        self.hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "YouTube MP3 Downloader",
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            120,
            70,
            1240,
            780,
            0,
            0,
            instance,
            None,
        )

        self._create_controls(instance)
        self._refresh()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _load_icon(self, instance: int) -> int:
        icon_path = Path(__file__).resolve().parent / "icon.ico"
        if not icon_path.is_file():
            icon_path = Path(sys.executable).parent / "logo.ico"
        if icon_path.is_file():
            return user32.LoadImageW(0, str(icon_path), 1, 0, 0, 0x00000010)
        return user32.LoadIconW(0, 32512)

    def _control(
        self,
        name: str,
        class_name: str,
        text: str,
        style: int,
        control_id: int = 0,
        ex_style: int = 0,
    ) -> int:
        handle = user32.CreateWindowExW(
            ex_style,
            class_name,
            text,
            WS_VISIBLE | WS_CHILD | style,
            0,
            0,
            10,
            10,
            self.hwnd,
            control_id,
            kernel32.GetModuleHandleW(None),
            None,
        )
        # Apply font
        use_font = self.title_font if name == "title" else (self.bold_font if name in {"start", "save", "list", "preview_title"} else self.font)
        user32.SendMessageW(handle, WM_SETFONT, use_font, 1)
        self.controls[name] = handle
        return handle

    def _create_controls(self, instance: int) -> None:
        self._control("title", "STATIC", "YouTube MP3 Downloader", 0)
        self._control(
            "description",
            "STATIC",
            "Tool tải nhạc YouTube tốc độ cao • Hỗ trợ Playlist, Channel, Shorts • Tự động xếp hạng top lượt xem • Kéo cuộn mượt mà khi đang tải",
            0,
        )
        self._control(
            "urls",
            "EDIT",
            "\r\n".join(self.controller.urls),
            WS_TABSTOP | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
            ID_URLS,
            ex_style=WS_EX_CLIENTEDGE,
        )

        self._control("quantity_label", "STATIC", "Số lượng (0 = tất cả):", 0)
        self._control("quantity", "EDIT", "0", WS_TABSTOP | ES_NUMBER, ID_QUANTITY, ex_style=WS_EX_CLIENTEDGE)
        self._control("list", "BUTTON", "Lấy top lượt xem", WS_TABSTOP, ID_LIST)
        self._control("list_stop", "BUTTON", "Dừng lấy video", WS_TABSTOP, ID_STOP_LIST)

        self._control("session_label", "STATIC", "Phiên YouTube:", 0)
        session = self._control(
            "session",
            "COMBOBOX",
            "",
            WS_TABSTOP | WS_VSCROLL | CBS_DROPDOWNLIST | CBS_HASSTRINGS,
            ID_BROWSER_SESSION,
        )
        for _, label in BROWSER_SESSION_OPTIONS:
            label_buf = ctypes.create_unicode_buffer(label)
            user32.SendMessageW(session, CB_ADDSTRING, 0, ctypes.cast(label_buf, ctypes.c_void_p).value)

        self._control("folder_label", "STATIC", "Thư mục lưu:", 0)
        self._control("folder", "EDIT", str(self.controller.output_dir), ES_READONLY, ID_FOLDER, ex_style=WS_EX_CLIENTEDGE)
        self._control("browse", "BUTTON", "Chọn thư mục", WS_TABSTOP, ID_BROWSE)
        self._control("open", "BUTTON", "Mở", WS_TABSTOP, ID_OPEN)

        self._control("save", "BUTTON", "Lưu danh sách", WS_TABSTOP, ID_SAVE)
        self._control("start", "BUTTON", "Tải MP3 / Tiếp tục", WS_TABSTOP, ID_START)
        self._control("stop", "BUTTON", "Dừng", WS_TABSTOP, ID_STOP)

        # Thanh tiến độ tổng thể (Overall Progress Bar)
        self._control("progress", "msctls_progress32", "", PBS_SMOOTH, ID_PROGRESS)
        user32.SendMessageW(self.controls["progress"], PBM_SETRANGE32, 0, 1000)
        self._control("progress_text", "STATIC", "Sẵn sàng", SS_LEFT, ID_PROGRESS_TEXT)

        # Selection Toolbar
        self._control("selection_help", "STATIC", "Tích chọn video cần tải:", 0)
        self._control("select_all", "BUTTON", "Chọn tất cả", WS_TABSTOP, ID_SELECT_ALL)
        self._control("select_none", "BUTTON", "Bỏ chọn", WS_TABSTOP, ID_SELECT_NONE)

        # Danh sách video (SysListView32)
        self._control(
            "items",
            "SysListView32",
            "",
            WS_TABSTOP | WS_BORDER | LVS_REPORT | LVS_SINGLESEL | LVS_SHOWSELALWAYS,
            ID_ITEMS,
        )
        user32.SendMessageW(
            self.controls["items"],
            LVM_SETEXTENDEDLISTVIEWSTYLE,
            0,
            LVS_EX_GRIDLINES | LVS_EX_CHECKBOXES | LVS_EX_FULLROWSELECT | LVS_EX_DOUBLEBUFFER,
        )
        self._initialize_item_columns()

        # Preview Card bên phải
        self._control("preview_title", "STATIC", "Ảnh xem trước (Thumbnail)", 0)
        self._control("preview", "STATIC", "", WS_BORDER | SS_BITMAP)
        self._control("preview_meta", "STATIC", "Chọn một video trong danh sách để xem thông tin chi tiết.", 0)

        self._layout(1220, 750)

    def _initialize_item_columns(self) -> None:
        columns = [
            ("Chọn / Ảnh", 80, LVCFMT_LEFT),
            ("Tiêu đề video", 470, LVCFMT_LEFT),
            ("Link YouTube", 230, LVCFMT_LEFT),
            ("Lượt xem", 110, LVCFMT_RIGHT),
            ("Trạng thái tải", 160, LVCFMT_LEFT),
        ]
        for idx, (title, col_width, fmt) in enumerate(columns):
            text_buf = ctypes.create_unicode_buffer(title)
            col = LVCOLUMNW()
            col.mask = LVCF_FMT | LVCF_WIDTH | LVCF_TEXT | LVCF_SUBITEM
            col.fmt = fmt
            col.cx = col_width
            col.pszText = ctypes.cast(text_buf, wintypes.LPWSTR)
            col.iSubItem = idx
            user32.SendMessageW(self.controls["items"], LVM_INSERTCOLUMNW, idx, _pointer_value(col))

    def _layout(self, width: int, height: int) -> None:
        if not self.controls:
            return
        margin = 18
        content_width = max(500, width - margin * 2)
        preview_width = 340
        item_width = max(300, content_width - preview_width - 16)
        preview_x = margin + item_width + 16

        positions: dict[str, tuple[int, int, int, int]] = {
            "title": (margin, 12, content_width, 32),
            "description": (margin, 46, content_width, 22),
            "urls": (margin, 72, content_width, 140),

            # Hàng cài đặt lọc & phiên
            "quantity_label": (margin, 224, 155, 26),
            "quantity": (margin + 160, 222, 65, 28),
            "list": (margin + 235, 220, 135, 32),
            "list_stop": (margin + 378, 220, 135, 32),
            "session_label": (margin + 525, 224, 110, 26),
            "session": (margin + 640, 221, max(140, content_width - 640), 160),

            # Hàng thư mục lưu
            "folder_label": (margin, 263, 100, 26),
            "folder": (margin + 105, 260, max(200, content_width - 325), 28),
            "browse": (width - margin - 205, 259, 120, 30),
            "open": (width - margin - 75, 259, 75, 30),

            # Hàng Nút Bấm & Thanh Tiến Độ Tổng Thể
            "save": (margin, 300, 125, 34),
            "start": (margin + 135, 300, 165, 34),
            "stop": (margin + 308, 300, 85, 34),
            "progress": (margin + 405, 302, max(220, content_width - 405), 16),
            "progress_text": (margin + 405, 320, max(220, content_width - 405), 20),

            # Hàng Chọn Tất Cả / Bỏ Chọn
            "selection_help": (margin, 348, 200, 24),
            "select_all": (margin + 205, 344, 105, 28),
            "select_none": (margin + 318, 344, 90, 28),

            # Bảng danh sách & Preview
            "items": (margin, 380, item_width, max(150, height - 398)),
            "preview_title": (preview_x, 348, preview_width, 24),
            "preview": (preview_x, 380, preview_width, 192),
            "preview_meta": (preview_x, 580, preview_width, max(60, height - 598)),
        }

        for key, (x, y, w, h) in positions.items():
            if key in self.controls:
                user32.MoveWindow(self.controls[key], x, y, w, h, True)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == WM_COMMAND:
                control_id = _loword(wparam)
                notification = _loword(wparam >> 16)
                self._handle_command(control_id, notification)
                return 0
            if message == WM_NOTIFY:
                self._handle_notify(lparam)
                return 0
            if message == WM_SIZE:
                w = _loword(lparam)
                h = _loword(lparam >> 16)
                self._layout(w, h)
                return 0
            if message == WM_CTLCOLORSTATIC:
                hdc = wparam
                ctrl_hwnd = lparam
                gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
                if ctrl_hwnd == self.controls.get("title"):
                    gdi32.SetTextColor(hdc, self._text_color)
                elif ctrl_hwnd == self.controls.get("description"):
                    gdi32.SetTextColor(hdc, self._muted_color)
                elif ctrl_hwnd == self.controls.get("progress_text"):
                    gdi32.SetTextColor(hdc, self._primary_color)
                else:
                    gdi32.SetTextColor(hdc, self._text_color)
                return self._bg_brush
            if message == WM_APP_REFRESH:
                self._refresh()
                return 0
            if message == WM_APP_LIST_DONE:
                self._finish_listing()
                return 0
            if message == WM_APP_PREVIEW_DONE:
                self._finish_preview()
                return 0
            if message == WM_APP_THUMBNAIL_DONE:
                self._finish_list_thumbnails()
                return 0
            if message == WM_CLOSE:
                if self._listing:
                    self._list_stop_event.set()
                if not self.controller.is_running():
                    self._sync_checked_selection()
                if self.controller.is_running():
                    self.controller.request_stop()
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                if self._preview_bitmap:
                    gdi32.DeleteObject(self._preview_bitmap)
                    self._preview_bitmap = None
                self._destroy_thumbnail_image_list()
                if self._bg_brush:
                    gdi32.DeleteObject(self._bg_brush)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception as exc:
            self._message("Lỗi", str(exc), error=True)
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _handle_command(self, control_id: int, notification: int) -> None:
        if control_id == ID_BROWSER_SESSION and notification == CBN_SELCHANGE:
            self._browser_session_changed()
            return
        if control_id == ID_SAVE:
            self._save_urls(show_message=True)
            return
        if control_id == ID_START:
            self._start()
            return
        if control_id == ID_STOP:
            self.controller.request_stop()
            return
        if control_id == ID_LIST:
            self._expand_list()
            return
        if control_id == ID_STOP_LIST:
            self._request_stop_listing()
            return
        if control_id == ID_BROWSE:
            self._choose_folder()
            return
        if control_id == ID_OPEN:
            self._open_folder()
            return
        if control_id == ID_SELECT_ALL:
            self._set_all_checked(True)
            return
        if control_id == ID_SELECT_NONE:
            self._set_all_checked(False)
            return

    def _handle_notify(self, lparam: int) -> None:
        if not lparam or self._refreshing_items:
            return
        notification = ctypes.cast(lparam, ctypes.POINTER(NMLISTVIEW)).contents
        if notification.hdr.hwndFrom != self.controls.get("items"):
            return
        code = ctypes.c_int(notification.hdr.code).value
        if code != LVN_ITEMCHANGED:
            return
        if notification.uChanged & LVIF_STATE:
            old_check = notification.uOldState & LVIS_STATEIMAGEMASK
            new_check = notification.uNewState & LVIS_STATEIMAGEMASK
            if old_check != new_check:
                self._sync_checked_selection()
            old_selected = notification.uOldState & LVIS_SELECTED
            new_selected = notification.uNewState & LVIS_SELECTED
            if old_selected != new_selected and new_selected:
                self._preview_selected_video()

    def _get_text(self, handle: int) -> str:
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def _set_text(self, handle: int, text: str) -> None:
        user32.SetWindowTextW(handle, text)

    def _browser_session_changed(self) -> None:
        index = int(user32.SendMessageW(self.controls["session"], CB_GETCURSEL, 0, 0))
        if 0 <= index < len(BROWSER_SESSION_OPTIONS):
            self.controller.set_browser_source(BROWSER_SESSION_OPTIONS[index][0])

    def _message(self, title: str, message: str, *, error: bool = False) -> None:
        flags = 0x00000010 if error else 0x00000040
        user32.MessageBoxW(self.hwnd, message, title, flags)

    def _signal_change(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    def _refresh(self) -> None:
        if not self.controls:
            return
        snapshot = self.controller.snapshot()
        self._set_text(self.controls["folder"], snapshot["output_dir"])

        browser_sources = [source for source, _ in BROWSER_SESSION_OPTIONS]
        try:
            browser_index = browser_sources.index(str(snapshot["browser_source"]))
        except ValueError:
            browser_index = 0
        current_browser_index = int(user32.SendMessageW(self.controls["session"], CB_GETCURSEL, 0, 0))
        if current_browser_index != browser_index:
            user32.SendMessageW(self.controls["session"], CB_SETCURSEL, browser_index, 0)

        # Tính toán thanh tiến độ tổng thể (Overall Progress Bar)
        selected_urls = [u for u in snapshot["urls"] if snapshot["items"].get(u, {}).get("selected", True)]
        total_selected = len(selected_urls)
        completed = sum(1 for u in selected_urls if snapshot["items"].get(u, {}).get("status") == "successful")
        downloading_item = next(
            (snapshot["items"][u] for u in selected_urls if snapshot["items"].get(u, {}).get("status") == "downloading"),
            None,
        )
        downloading_progress = downloading_item.get("progress", 0) if downloading_item else 0

        if total_selected > 0:
            ratio = (completed + (downloading_progress / 100.0)) / total_selected
            prog_val = int(max(0.0, min(1.0, ratio)) * 1000)
            pct = int(ratio * 100)
        else:
            prog_val = 0
            pct = 0

        user32.SendMessageW(self.controls["progress"], PBM_SETPOS, prog_val, 0)

        if self._listing_stopping:
            prog_text = f"Đang dừng quét... Đã tìm thấy {self._listing_count} video."
        elif self._listing:
            prog_text = f"Đang quét toàn bộ kênh/playlist... Đã tìm thấy {self._listing_count} video."
        elif snapshot["running"]:
            active_title = downloading_item.get("title", "") if downloading_item else ""
            if active_title:
                active_str = f" • {active_title[:45]} ({downloading_progress}%)"
            else:
                active_str = f" • {snapshot.get('status_text', '')}"
            prog_text = f"Tiến trình tổng: {pct}% ({completed}/{total_selected} video){active_str}"
        elif total_selected > 0 and completed == total_selected:
            prog_text = f"✔ Đã hoàn tất 100% ({completed}/{total_selected} video đã chọn)"
        elif total_selected > 0:
            prog_text = f"Sẵn sàng: {completed}/{total_selected} video ({pct}%) • {snapshot.get('status_text', '')}"
        else:
            prog_text = snapshot.get("status_text", "Sẵn sàng")

        self._set_text(self.controls["progress_text"], prog_text)

        busy = bool(snapshot["running"] or self._listing)
        user32.EnableWindow(self.controls["start"], not busy)
        user32.EnableWindow(self.controls["list"], not busy)
        user32.EnableWindow(self.controls["list_stop"], self._listing and not self._listing_stopping)
        self._set_text(
            self.controls["list_stop"],
            "Đang dừng..." if self._listing_stopping else "Dừng lấy video",
        )
        user32.EnableWindow(self.controls["save"], not busy)
        user32.EnableWindow(self.controls["session"], not busy)
        user32.EnableWindow(self.controls["stop"], snapshot["running"])

        # QUAN TRỌNG: Cho phép kéo danh sách kể cả khi tool đang tải!
        user32.EnableWindow(self.controls["items"], True)
        user32.EnableWindow(self.controls["select_all"], not busy)
        user32.EnableWindow(self.controls["select_none"], not busy)

        urls = tuple(snapshot["urls"])
        if urls != self._rendered_urls:
            self._rebuild_item_list(snapshot)
        else:
            self._update_item_rows(snapshot)

        if urls:
            if self._selected_item_index() < 0:
                self._set_item_selected(0)
            self._preview_selected_video()
        else:
            self._preview_url = ""
            old_bitmap = user32.SendMessageW(self.controls["preview"], STM_SETIMAGE, IMAGE_BITMAP, 0)
            if old_bitmap:
                gdi32.DeleteObject(old_bitmap)
            self._preview_bitmap = None
            self._set_text(self.controls["preview_meta"], "Chọn một video trong danh sách để xem thumbnail.")

    def _set_listview_text(self, index: int, column: int, text: str) -> None:
        text_buffer = ctypes.create_unicode_buffer(text)
        item = LVITEMW()
        item.iSubItem = column
        item.pszText = ctypes.cast(text_buffer, wintypes.LPWSTR)
        user32.SendMessageW(self.controls["items"], LVM_SETITEMTEXTW, index, _pointer_value(item))

    def _get_listview_text(self, index: int, column: int) -> str:
        text_buffer = ctypes.create_unicode_buffer(2048)
        item = LVITEMW()
        item.iSubItem = column
        item.pszText = ctypes.cast(text_buffer, wintypes.LPWSTR)
        item.cchTextMax = len(text_buffer)
        user32.SendMessageW(self.controls["items"], LVM_GETITEMTEXTW, index, _pointer_value(item))
        return text_buffer.value

    def _set_item_checked(self, index: int, checked: bool) -> None:
        item = LVITEMW()
        item.mask = LVIF_STATE
        item.iItem = index
        item.stateMask = LVIS_STATEIMAGEMASK
        item.state = 0x2000 if checked else 0x1000
        user32.SendMessageW(self.controls["items"], LVM_SETITEMW, 0, _pointer_value(item))

    def _is_item_checked(self, index: int) -> bool:
        state = user32.SendMessageW(self.controls["items"], LVM_GETITEMSTATE, index, LVIS_STATEIMAGEMASK)
        return bool(state & 0x2000)

    def _selected_item_index(self) -> int:
        return int(user32.SendMessageW(self.controls["items"], LVM_GETNEXTITEM, -1, LVNI_SELECTED))

    def _set_item_selected(self, index: int) -> None:
        item = LVITEMW()
        item.mask = LVIF_STATE
        item.iItem = index
        item.stateMask = LVIS_SELECTED
        item.state = LVIS_SELECTED
        user32.SendMessageW(self.controls["items"], LVM_SETITEMW, 0, _pointer_value(item))

    @staticmethod
    def _row_values(url: str, item: dict[str, Any]) -> tuple[str, str, str, str]:
        title = str(item.get("title") or "Chưa lấy được tiêu đề")
        views = int(item.get("view_count") or 0)
        view_label = f"{views:,}".replace(",", ".") if views else "—"
        status = str(item.get("status") or "pending")
        progress = int(item.get("progress") or 0)
        error = str(item.get("error") or "")
        if status == "successful":
            status_label = "Hoàn tất 100%"
        elif status == "downloading":
            status_label = f"Đang tải... {progress}%"
        elif status == "error":
            status_label = f"Lỗi: {error[:25]}" if error else "Lỗi"
        else:
            status_label = "Chờ tải"
        return title, url, view_label, status_label

    def _rebuild_item_list(self, snapshot: dict[str, Any]) -> None:
        self._refreshing_items = True
        try:
            user32.SendMessageW(self.controls["items"], LVM_DELETEALLITEMS, 0, 0)
            self._destroy_thumbnail_image_list()
            self._rendered_urls = tuple(snapshot["urls"])
            for index, url in enumerate(self._rendered_urls):
                item_data = snapshot["items"].get(url) or {}
                item = LVITEMW()
                item.mask = LVIF_TEXT | LVIF_STATE
                item.iItem = index
                item.iSubItem = 0
                item.pszText = ctypes.cast(ctypes.create_unicode_buffer(""), wintypes.LPWSTR)
                user32.SendMessageW(self.controls["items"], LVM_INSERTITEMW, 0, _pointer_value(item))
                self._set_item_checked(index, bool(item_data.get("selected", True)))
                title, link, views, status = self._row_values(url, item_data)
                self._set_listview_text(index, 1, title)
                self._set_listview_text(index, 2, link)
                self._set_listview_text(index, 3, views)
                self._set_listview_text(index, 4, status)
            self._start_list_thumbnail_loading(snapshot)
        finally:
            self._refreshing_items = False

    def _update_item_rows(self, snapshot: dict[str, Any]) -> None:
        self._refreshing_items = True
        try:
            for index, url in enumerate(self._rendered_urls):
                item_data = snapshot["items"].get(url) or {}
                desired_checked = bool(item_data.get("selected", True))
                if self._is_item_checked(index) != desired_checked:
                    self._set_item_checked(index, desired_checked)
                title, link, views, status = self._row_values(url, item_data)
                if self._get_listview_text(index, 1) != title:
                    self._set_listview_text(index, 1, title)
                if self._get_listview_text(index, 2) != link:
                    self._set_listview_text(index, 2, link)
                if self._get_listview_text(index, 3) != views:
                    self._set_listview_text(index, 3, views)
                if self._get_listview_text(index, 4) != status:
                    self._set_listview_text(index, 4, status)
        finally:
            self._refreshing_items = False

    def _destroy_thumbnail_image_list(self) -> None:
        if self._thumbnail_image_list:
            if self.controls.get("items"):
                user32.SendMessageW(self.controls["items"], LVM_SETIMAGELIST, LVSIL_SMALL, 0)
            comctl32.ImageList_Destroy(self._thumbnail_image_list)
            self._thumbnail_image_list = None
            self._thumbnail_generation += 1

    def _start_list_thumbnail_loading(self, snapshot: dict[str, Any]) -> None:
        urls = list(snapshot["urls"])
        items = dict(snapshot["items"])
        generation = self._thumbnail_generation + 1
        self._thumbnail_generation = generation

        def worker() -> None:
            results: list[tuple[int, Path | None]] = []
            for index, url in enumerate(urls):
                thumb_url = str(items.get(url, {}).get("thumbnail_url") or "")
                bmp_path = _cached_thumbnail_bitmap(thumb_url, (64, 36)) if thumb_url else None
                results.append((index, bmp_path))
            with self._thumbnail_lock:
                if generation == self._thumbnail_generation:
                    self._thumbnail_results = results
                    if self.hwnd:
                        user32.PostMessageW(self.hwnd, WM_APP_THUMBNAIL_DONE, 0, 0)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_list_thumbnails(self) -> None:
        with self._thumbnail_lock:
            results = self._thumbnail_results
            self._thumbnail_results = []
        if not results or not self.controls.get("items"):
            return

        image_list = comctl32.ImageList_Create(64, 36, 0x0020, len(results), len(results))
        self._thumbnail_image_list = image_list
        user32.SendMessageW(self.controls["items"], LVM_SETIMAGELIST, LVSIL_SMALL, image_list)

        for index, bmp_path in results:
            if not bmp_path or not bmp_path.is_file():
                continue
            hbitmap = user32.LoadImageW(0, str(bmp_path), 0, 64, 36, 0x00000010)
            if not hbitmap:
                continue
            image_index = comctl32.ImageList_Add(image_list, hbitmap, 0)
            gdi32.DeleteObject(hbitmap)
            if image_index >= 0:
                item = LVITEMW()
                item.mask = LVIF_IMAGE
                item.iItem = index
                item.iSubItem = 0
                item.iImage = image_index
                user32.SendMessageW(self.controls["items"], LVM_SETITEMW, 0, _pointer_value(item))

    def _sync_checked_selection(self) -> None:
        if not self._rendered_urls or self.controller.is_running():
            return
        selected: set[str] = set()
        for index, url in enumerate(self._rendered_urls):
            if self._is_item_checked(index):
                selected.add(url)
        self.controller.set_selected_urls(selected)

    def _set_all_checked(self, checked: bool) -> None:
        if self.controller.is_running():
            return
        self._refreshing_items = True
        try:
            for index in range(len(self._rendered_urls)):
                self._set_item_checked(index, checked)
        finally:
            self._refreshing_items = False
        self._sync_checked_selection()

    def _preview_selected_video(self) -> None:
        index = self._selected_item_index()
        if index < 0 or index >= len(self._rendered_urls):
            return
        url = self._rendered_urls[index]
        if url == self._preview_url and self._preview_bitmap:
            return
        self._preview_url = url
        snapshot = self.controller.snapshot()
        item = snapshot["items"].get(url) or {}
        title = item.get("title") or "Chưa lấy được tiêu đề"
        views = int(item.get("view_count") or 0)
        view_label = f"{views:,}".replace(",", ".") if views else "—"
        thumb_url = item.get("thumbnail_url") or ""

        self._set_text(self.controls["preview_meta"], f"{title}\r\nLượt xem: {view_label}")
        if not thumb_url:
            return

        generation = self._preview_generation + 1
        self._preview_generation = generation

        def worker() -> None:
            bmp_path = _cached_thumbnail_bitmap(thumb_url, (340, 192))
            with self._preview_lock:
                if generation == self._preview_generation:
                    self._preview_result = (generation, bmp_path, None)
                    if self.hwnd:
                        user32.PostMessageW(self.hwnd, WM_APP_PREVIEW_DONE, 0, 0)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_preview(self) -> None:
        with self._preview_lock:
            generation, path, error = self._preview_result
            self._preview_result = (0, None, None)
        if generation != self._preview_generation or not path or not path.is_file():
            return

        hbitmap = user32.LoadImageW(0, str(path), 0, 340, 192, 0x00000010)
        if not hbitmap:
            return
        old = user32.SendMessageW(self.controls["preview"], STM_SETIMAGE, IMAGE_BITMAP, hbitmap)
        if old:
            gdi32.DeleteObject(old)
        self._preview_bitmap = hbitmap

    def _read_urls(self) -> tuple[list[str], list[str]]:
        return parse_youtube_urls(self._get_text(self.controls["urls"]))

    def _save_urls(self, *, show_message: bool) -> bool:
        if self._listing:
            self._message("Đang xử lý", "Hãy chờ lấy xong danh sách video.")
            return False
        urls, invalid = self._read_urls()
        if invalid:
            self._message("Link không hợp lệ", f"Đã bỏ qua {len(invalid)} link không phải YouTube.")
        self.controller.update_urls(urls)
        self._refresh()
        if show_message:
            self._message("Đã lưu", f"Đã lưu {len(urls)} link.")
        return bool(urls)

    def _start(self) -> None:
        if not self._save_urls(show_message=False):
            self._message("Thiếu link", "Hãy nhập ít nhất một link YouTube.")
            return
        try:
            self._sync_checked_selection()
            self.controller.start()
        except Exception as exc:
            self._message("Không thể tải", str(exc), error=True)

    def _expand_list(self) -> None:
        if self._listing:
            return
        urls, invalid = self._read_urls()
        if not urls:
            self._message("Thiếu link", "Hãy nhập link playlist, kênh hoặc video YouTube.")
            return
        try:
            limit = int(self._get_text(self.controls["quantity"]).strip() or "0")
        except ValueError:
            self._message("Số lượng không hợp lệ", "Hãy nhập một số nguyên.")
            return

        self._listing = True
        self._listing_stopping = False
        self._listing_count = 0
        self._list_stop_event.clear()
        self._refresh()

        browser_source = str(self.controller.snapshot()["browser_source"])

        def on_prog(count: int) -> None:
            self._listing_count = count
            self._signal_change()

        def worker() -> None:
            entries: list[dict[str, Any]] | None = None
            error: Exception | None = None
            try:
                entries = extract_youtube_entries(
                    urls,
                    limit=limit,
                    browser_source=browser_source,
                    stop_event=self._list_stop_event,
                    on_progress=on_prog,
                )
            except ListingStopped as exc:
                entries = exc.entries
            except Exception as exc:
                error = exc
            with self._list_lock:
                self._list_result = (entries, error)
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_APP_LIST_DONE, 0, 0)

        threading.Thread(target=worker, daemon=True).start()

    def _request_stop_listing(self) -> None:
        if not self._listing or self._listing_stopping:
            return
        self._listing_stopping = True
        self._list_stop_event.set()
        self._refresh()

    def _finish_listing(self) -> None:
        with self._list_lock:
            entries, error = self._list_result
            self._list_result = (None, None)
        self._listing = False
        self._listing_stopping = False

        if error:
            self._refresh()
            self._message("Không thể quét video", str(error), error=True)
            return

        if entries:
            self.controller.update_entries(entries)
            url_text = "\r\n".join(self.controller.urls)
            self._set_text(self.controls["urls"], url_text)
        self._refresh()

    def _choose_folder(self) -> None:
        info, display_name = _create_browse_info(self.hwnd or 0)
        item_id_list = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not item_id_list:
            return
        try:
            selected = ctypes.create_unicode_buffer(32768)
            selected_pointer = ctypes.cast(selected, wintypes.LPWSTR)
            if shell32.SHGetPathFromIDListW(item_id_list, selected_pointer):
                self.controller.set_output_dir(selected.value)
                self._refresh()
        finally:
            ole32.CoTaskMemFree(item_id_list)

    def _open_folder(self) -> None:
        try:
            self.controller.output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.controller.output_dir))
        except Exception as exc:
            self._message("Không thể mở thư mục", str(exc), error=True)


def run_app() -> None:
    app = NativeYoutubeMp3App()
    app.run()


def native_ui_smoke_test() -> None:
    class VolatileStateStore:
        def __init__(self) -> None:
            self.value = {}

        def load(self) -> dict[str, Any] | None:
            return self.value

        def save(self, value: dict[str, Any]) -> bool:
            self.value = dict(value)
            return True

    app = NativeYoutubeMp3App(YoutubeMp3DownloadController(state_store=VolatileStateStore()))
    assert app.controller is not None

