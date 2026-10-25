"""Another program's windows - the game's - seen and driven from outside: found by process, taken
a picture of, given keys and clicks. ctypes and zlib only; Windows only.

What a game reads is not what an ordinary program reads. Future Soldier takes its keys through
DirectInput, which looks at scan codes, so keys go out as scan codes through SendInput - the same
path a keyboard takes - and never as window messages, which a DirectInput game does not see.
SendInput goes to whatever window is in front, so nothing is sent until the target is in front,
and the target is checked again before every key: input meant for the game must never land in
the window of someone who just clicked somewhere else.

A DirectX window is drawn by the GPU, and the classic BitBlt from its device context gives
black. PrintWindow with PW_RENDERFULLCONTENT asks the compositor for what is on screen, which
works for windowed DirectX 11; when even that comes back black, the window is brought to the
front and the screen under it is copied.
"""

import ctypes
import ctypes.wintypes as W
import os
import struct
import time
import zlib

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
gdi32 = ctypes.WinDLL("gdi32") if os.name == "nt" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
SW_RESTORE = 9

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
KEYEVENTF_UNICODE = 0x4
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x1, 0x8000, 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10
MOUSEEVENTF_WHEEL = 0x800
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

# Names for keys; anything else of one character is that character's key.
KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "insert": 0x2D, "delete": 0x2E,
    **{"f%d" % n: 0x6F + n for n in range(1, 13)},
}
# These sit on the extended half of the keyboard; without the flag an arrow is a keypad digit.
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}


if os.name == "nt":
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                    ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                    ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                    ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                    ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                    ("biClrImportant", W.DWORD)]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                    ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                    ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD),
                    ("szExeFile", W.WCHAR * 260)]

    user32.GetForegroundWindow.restype = W.HWND
    user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user32.SendInput.argtypes = [W.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.GetDC.restype = W.HDC
    user32.GetDC.argtypes = [W.HWND]
    user32.ReleaseDC.argtypes = [W.HWND, W.HDC]
    user32.PrintWindow.argtypes = [W.HWND, W.HDC, W.UINT]
    user32.MonitorFromWindow.restype = W.HMONITOR
    user32.WindowFromPoint.argtypes = [W.POINT]
    user32.WindowFromPoint.restype = W.HWND
    user32.GetAncestor.argtypes = [W.HWND, W.UINT]
    user32.GetAncestor.restype = W.HWND
    user32.GetDpiForWindow.restype = W.UINT
    gdi32.CreateCompatibleDC.restype = W.HDC
    gdi32.CreateCompatibleDC.argtypes = [W.HDC]
    gdi32.CreateDIBSection.restype = W.HBITMAP
    gdi32.CreateDIBSection.argtypes = [W.HDC, ctypes.c_void_p, W.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD]
    gdi32.SelectObject.restype = W.HGDIOBJ
    gdi32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [W.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [W.HDC]
    gdi32.BitBlt.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             W.HDC, ctypes.c_int, ctypes.c_int, W.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel32.OpenProcess.restype = W.HANDLE


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("dwTime", W.DWORD)]


# A person at the machine comes first: nothing takes the front, and their keys with it, unless
# the keyboard and the mouse have been left alone this long.
IDLE_NEEDED = 1.5


_sent_at = None      # GetTickCount when this process last sent input


def idle_seconds() -> float:
    """Since the last key or mouse movement a person made: input this process sent itself
    does not count, input an earlier run of it sent does."""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 1e9
    if _sent_at is not None and ((info.dwTime - _sent_at) & 0xFFFFFFFF) < 100:
        return 1e9
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def wait_idle(patience: float = 10.0) -> None:
    deadline = time.monotonic() + patience
    while True:
        busy = idle_seconds()
        if busy >= IDLE_NEEDED:
            return
        if time.monotonic() >= deadline:
            raise DesktopError("someone is using the keyboard or the mouse (%.1f s ago);"
                               " nothing was sent - the window would have taken their keys"
                               % busy)
        time.sleep(0.25)


class DesktopError(Exception):
    """Something on the desktop is not as the caller needs it: no window, not in front."""


def dpi_aware() -> None:
    """Window rectangles and mouse positions in real pixels, not scaled ones."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


# ----- processes ------------------------------------------------------------------------------
def processes() -> list:
    """(pid, parent pid, exe name) of every process."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    out = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(0x1000, False, pid)          # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = W.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                                # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def children(pid: int) -> list:
    return [(p, name) for p, parent, name in processes() if parent == pid]


# ----- windows --------------------------------------------------------------------------------
class Window:
    # How long input waits for a person to leave the keyboard and the mouse alone.
    patience = 10.0

    def __init__(self, hwnd: int):
        self.hwnd = hwnd

    def __eq__(self, other):
        return isinstance(other, Window) and other.hwnd == self.hwnd

    def __repr__(self):
        return "<Window %#x %r %s>" % (self.hwnd, self.title, self.rect)

    @property
    def pid(self) -> int:
        pid = W.DWORD()
        user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        return pid.value

    @property
    def title(self) -> str:
        n = user32.GetWindowTextLengthW(self.hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(self.hwnd, buf, n + 1)
        return buf.value

    @property
    def rect(self) -> tuple:
        """The client area - what the game draws into - as (x, y, width, height) on screen."""
        r = W.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        origin = W.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(origin))
        return origin.x, origin.y, r.right - r.left, r.bottom - r.top

    def exists(self) -> bool:
        return bool(user32.IsWindow(self.hwnd))

    def in_front(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    def move(self, x: int, y: int, width: int, height: int) -> None:
        """Place the whole window, frame included."""
        user32.MoveWindow(self.hwnd, x, y, width, height, True)

    def focus(self, tries: int = 4) -> bool:
        """Bring the window in front. Windows refuses that to a process that is not in front
        itself; tapping Alt first (what Windows itself takes as the user being active) and
        attaching to the foreground thread are the two ways it allows."""
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        for attempt in range(tries):
            if self.in_front():
                return True
            if attempt:
                _send([_key_input(KEYS["alt"], False), _key_input(KEYS["alt"], True)])
            front = user32.GetForegroundWindow()
            theirs = user32.GetWindowThreadProcessId(front, None) if front else 0
            mine = kernel32.GetCurrentThreadId()
            attached = bool(theirs) and theirs != mine and \
                user32.AttachThreadInput(mine, theirs, True)
            try:
                user32.BringWindowToTop(self.hwnd)
                user32.SetForegroundWindow(self.hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(mine, theirs, False)
            time.sleep(0.15)
        return self.in_front()

    # ----- pictures -------------------------------------------------------------------------
    def capture(self) -> tuple:
        """(width, height, top-down BGRA bytes) of the client area."""
        x, y, w, h = self.rect
        if w <= 0 or h <= 0:
            raise DesktopError("the window has no client area (minimised?)")
        pixels = _grab(w, h, lambda dc: user32.PrintWindow(
            self.hwnd, dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT))
        scale = self.scale()
        if scale != 1:
            # A window that does not know about DPI paints for PrintWindow at its own, smaller
            # size, into the top left corner; the screen shows it stretched.
            pixels = _stretch(pixels, w, round(w / scale), round(h / scale), w, h)
        if not any(pixels[i] for i in range(0, len(pixels), 97)):
            # Black: a DirectX surface the compositor would not hand over. Copy the screen.
            if not self.focus():
                raise DesktopError("cannot bring the window in front to copy it")
            time.sleep(0.2)
            x, y, w, h = self.rect
            pixels = grab_screen(x, y, w, h)[2]
        return w, h, pixels

    def scale(self) -> float:
        """How much the screen stretches this window: 2 for a DPI-unaware window on a screen
        at 200%, 1 for one that draws in real pixels."""
        try:
            mine = user32.GetDpiForWindow(W.HWND(self.hwnd))
            monitor = user32.MonitorFromWindow(W.HWND(self.hwnd), 2)     # NEAREST
            x, y = W.UINT(), W.UINT()
            ctypes.WinDLL("shcore").GetDpiForMonitor(W.HMONITOR(monitor), 0, ctypes.byref(x),
                                                     ctypes.byref(y))
        except (OSError, AttributeError):
            return 1
        return x.value / mine if mine and x.value else 1

    def screenshot(self, path: str) -> str:
        w, h, pixels = self.capture()
        write_png(path, w, h, pixels)
        return path

    # ----- input ----------------------------------------------------------------------------
    def _front(self) -> None:
        wait_idle(self.patience)
        if not self.focus():
            raise DesktopError("the window %r would not come in front; nothing was sent"
                               % self.title)

    def _under_cursor(self, x: int, y: int) -> None:
        """The cursor where it was put, over this window: a person at the mouse, or a window
        that came up on top, would otherwise get the click."""
        at = W.POINT()
        user32.GetCursorPos(ctypes.byref(at))
        if abs(at.x - x) > 2 or abs(at.y - y) > 2:
            raise DesktopError("the mouse was moved to (%d, %d); the click was not sent"
                               % (at.x, at.y))
        under = user32.WindowFromPoint(at)
        if not self.in_front() or (under and user32.GetAncestor(under, 2) != self.hwnd):
            raise DesktopError("another window is under the cursor; the click was not sent")

    def keys(self, names, hold: float = 0.06, gap: float = 0.25) -> None:
        """Press keys one after another: 'enter', 'down', 'f1', 'a', or 'ctrl+a' for a chord."""
        self._front()
        for name in names:
            if not self.in_front():
                raise DesktopError("the window lost the front before %r; stopped" % name)
            codes = [_vk(part) for part in name.split("+")]
            _send([_key_input(c, False) for c in codes])
            time.sleep(hold)
            _send([_key_input(c, True) for c in reversed(codes)])
            time.sleep(gap)

    def type(self, text: str, gap: float = 0.03) -> None:
        """Characters as characters, for a text field."""
        self._front()
        for ch in text:
            if not self.in_front():
                raise DesktopError("the window lost the front while typing; stopped")
            _send([_char_input(ch, False), _char_input(ch, True)])
            time.sleep(gap)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        """A click at (x, y) inside the client area, in the pixels of its screenshot."""
        self._front()
        left, top, w, h = self.rect
        if not (0 <= x < w and 0 <= y < h):
            raise DesktopError("(%d, %d) is outside the %dx%d window" % (x, y, w, h))
        down, up = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if button == "left"
                    else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
        _send([_mouse_move(left + x, top + y)])
        time.sleep(0.08)
        for _ in range(2 if double else 1):
            self._under_cursor(left + x, top + y)
            _send([_mouse_input(down)])
            time.sleep(0.05)
            _send([_mouse_input(up)])
            time.sleep(0.08)

    def scroll(self, x: int, y: int, clicks: int) -> None:
        self._front()
        left, top, _, _ = self.rect
        _send([_mouse_move(left + x, top + y)])
        time.sleep(0.05)
        self._under_cursor(left + x, top + y)
        _send([_mouse_input(MOUSEEVENTF_WHEEL, data=120 * clicks)])


def windows(pid: int = None, visible: bool = True) -> list:
    """Top-level windows, of one process or of all, largest first."""
    found = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def each(hwnd, _):
        window = Window(hwnd)
        if (not visible or user32.IsWindowVisible(hwnd)) and (pid is None or window.pid == pid):
            found.append(window)
        return True
    user32.EnumWindows(each, 0)
    return sorted(found, key=lambda w: -(w.rect[2] * w.rect[3]))


def main_window(pid: int):
    """The window a process draws its picture in, or None."""
    for window in windows(pid):
        if window.rect[2] > 64 and window.rect[3] > 64:
            return window
    return None


# ----- the screen -----------------------------------------------------------------------------
def _grab(w: int, h: int, paint) -> bytes:
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFOHEADER()
    info.biSize = ctypes.sizeof(info)
    info.biWidth, info.biHeight, info.biPlanes, info.biBitCount = w, -h, 1, 32
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, bitmap)
    try:
        paint(dc)
        return ctypes.string_at(bits, w * h * 4)
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)


def grab_screen(x: int = None, y: int = None, w: int = None, h: int = None) -> tuple:
    """(width, height, BGRA) of a part of the screen - the whole of it by default."""
    if w is None:
        x, y = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(
            SM_YVIRTUALSCREEN)
        w, h = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(
            SM_CYVIRTUALSCREEN)
    screen = user32.GetDC(None)
    try:
        pixels = _grab(w, h, lambda dc: gdi32.BitBlt(dc, 0, 0, w, h, screen, x, y,
                                                     SRCCOPY | CAPTUREBLT))
    finally:
        user32.ReleaseDC(None, screen)
    return w, h, pixels


def screenshot(path: str) -> str:
    write_png(path, *grab_screen())
    return path


def _stretch(bgra: bytes, stride_px: int, w: int, h: int, to_w: int, to_h: int) -> bytes:
    """The w x h corner of a picture stride_px wide, stretched to to_w x to_h (nearest pixel)."""
    stride = stride_px * 4
    xs = [min(w - 1, x * w // to_w) * 4 for x in range(to_w)]
    out, last_y, last_row = [], None, b""
    for y in range(to_h):
        source = min(h - 1, y * h // to_h)
        if source != last_y:
            row = bgra[source * stride:source * stride + w * 4]
            last_row = b"".join(row[o:o + 4] for o in xs)
            last_y = source
        out.append(last_row)
    return b"".join(out)


def write_png(path: str, width: int, height: int, bgra: bytes) -> None:
    rgb = bytearray(width * height * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[row * stride:(row + 1) * stride])
                   for row in range(height))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                                                   8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def read_png(path: str) -> tuple:
    """(width, height, RGB bytes) of a PNG written by write_png - for tests and comparisons."""
    with open(path, "rb") as f:
        data = f.read()
    width, height = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3
    rows = [raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)] for r in range(height)]
    return width, height, b"".join(rows)


# ----- input primitives -----------------------------------------------------------------------
def _vk(name: str) -> int:
    name = name.strip().lower()
    if name in KEYS:
        return KEYS[name]
    # A key, not a character: the key code of a letter is the same on every layout, while
    # VkKeyScan answers for the current one, and on a Russian layout there is no 'a'.
    if len(name) == 1 and ("a" <= name <= "z" or "0" <= name <= "9"):
        return ord(name.upper())
    if len(name) == 1:
        code = user32.VkKeyScanW(ord(name))
        if code not in (-1, 0xFFFF):
            return code & 0xFF
    raise ValueError("no such key: %r" % name)


def _key_input(vk: int, up: bool):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    item = INPUT(type=INPUT_KEYBOARD)
    item.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def _char_input(ch: str, up: bool):
    item = INPUT(type=INPUT_KEYBOARD)
    item.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return item


def _mouse_move(x: int, y: int):
    vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(
        SM_YVIRTUALSCREEN)
    vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(
        SM_CYVIRTUALSCREEN)
    item = INPUT(type=INPUT_MOUSE)
    item.u.mi = MOUSEINPUT(int((x - vx) * 65535 / max(vw - 1, 1)),
                           int((y - vy) * 65535 / max(vh - 1, 1)), 0,
                           MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
    return item


def _mouse_input(flags: int, data: int = 0):
    item = INPUT(type=INPUT_MOUSE)
    item.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, 0)
    return item


def _send(items) -> None:
    global _sent_at
    _sent_at = kernel32.GetTickCount()
    array = (INPUT * len(items))(*items)
    sent = user32.SendInput(len(items), array, ctypes.sizeof(INPUT))
    if sent != len(items):
        raise DesktopError("SendInput sent %d of %d (error %d) - a window of a program run as"
                           " administrator in front?" % (sent, len(items),
                                                         ctypes.get_last_error()))
