"""Register the borderless timer explicitly with the Windows taskbar."""

import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
from subprocess import list2cmdline
import sys
from uuid import UUID

APP_ID = "Potatorious.MiniTimer"
APP_NAME = "Mini Timer"
CLSCTX_INPROC_SERVER = 1
RPC_E_CHANGED_MODE = -2147417850
VT_EMPTY, VT_LPWSTR = 0, 31

ole = c.WinDLL("ole32")
shell = c.WinDLL("shell32")
ole.CoInitialize.argtypes = [c.c_void_p]
ole.CoInitialize.restype = w.LONG
ole.CoUninitialize.argtypes = []
ole.CoUninitialize.restype = None
ole.CoCreateInstance.argtypes = [c.c_void_p, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(c.c_void_p)]
ole.CoCreateInstance.restype = w.LONG
shell.SetCurrentProcessExplicitAppUserModelID.argtypes = [w.LPCWSTR]
shell.SetCurrentProcessExplicitAppUserModelID.restype = w.LONG
shell.SHGetPropertyStoreForWindow.argtypes = [w.HWND, c.c_void_p, c.POINTER(c.c_void_p)]
shell.SHGetPropertyStoreForWindow.restype = w.LONG

TASKBAR_CLASS = c.create_string_buffer(UUID("56FDF344-FD6D-11D0-958A-006097C9A090").bytes_le, 16)
TASKBAR_INTERFACE = c.create_string_buffer(UUID("56FDF342-FD6D-11D0-958A-006097C9A090").bytes_le, 16)
PROPERTY_STORE_INTERFACE = c.create_string_buffer(UUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99").bytes_le, 16)
GUID_BYTES = c.c_ubyte * 16
APP_PROPERTIES = GUID_BYTES.from_buffer_copy(UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le)

RELAUNCH_COMMAND, RELAUNCH_ICON, RELAUNCH_NAME, APP_USER_MODEL_ID = 2, 3, 4, 5


class PropertyKey(c.Structure):
    _fields_ = [("format_id", GUID_BYTES), ("id", w.DWORD)]


class StringProperty(c.Structure):
    # PROPVARIANT's union must fit both pointer-sized fields even for a single string.
    _fields_ = [("type", w.WORD), ("reserved", w.WORD * 3), ("value", w.LPWSTR), ("padding", c.c_void_p)]


def com_method(interface, index, result, *arguments):
    table = c.cast(interface, c.POINTER(c.POINTER(c.c_void_p)))[0]
    return c.WINFUNCTYPE(result, c.c_void_p, *arguments)(table[index])


def identify_process():
    shell.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def relaunch_command(icon):
    executable = Path(sys.executable)
    windowed = executable.with_name("pythonw.exe")
    launcher = windowed if windowed.is_file() else executable
    return list2cmdline([str(launcher), str(icon.parents[1] / "MiniTimer.pyw")])


class TaskbarTab:
    def __init__(self, hwnd, icon):
        self.hwnd = hwnd
        self.interface = c.c_void_p()
        self.store = c.c_void_p()
        self.properties = []
        self.registered = False
        result = ole.CoInitialize(None)
        self.uninitialize = result >= 0
        self.initialized = self.uninitialize or result == RPC_E_CHANGED_MODE
        if not self.initialized:
            return
        if shell.SHGetPropertyStoreForWindow(hwnd, PROPERTY_STORE_INTERFACE, c.byref(self.store)) < 0:
            return
        # Windows reads the relaunch properties when the window-level ID is set.
        for key, value in (
            (RELAUNCH_COMMAND, relaunch_command(icon)),
            (RELAUNCH_ICON, f"{icon},0"),
            (RELAUNCH_NAME, APP_NAME),
            (APP_USER_MODEL_ID, APP_ID),
        ):
            if self.set_property(key, value) < 0:
                self.clear_properties()
                return
            self.properties.append(key)

    def register(self):
        if not self.initialized:
            return
        if ole.CoCreateInstance(TASKBAR_CLASS, None, CLSCTX_INPROC_SERVER,
                                TASKBAR_INTERFACE, c.byref(self.interface)) >= 0:
            if com_method(self.interface, 3, w.LONG)(self.interface) >= 0:  # HrInit
                self.registered = com_method(self.interface, 4, w.LONG, w.HWND)(self.interface, self.hwnd) >= 0  # AddTab

    def set_property(self, property_id, value):
        key = PropertyKey(APP_PROPERTIES, property_id)
        variant = StringProperty(VT_LPWSTR if value is not None else VT_EMPTY)
        variant.value = value
        set_value = com_method(self.store, 6, w.LONG, c.POINTER(PropertyKey), c.POINTER(StringProperty))
        return set_value(self.store, c.byref(key), c.byref(variant))

    def clear_properties(self):
        for key in reversed(self.properties):
            self.set_property(key, None)
        self.properties.clear()

    def close(self):
        if self.interface:
            if self.registered:
                com_method(self.interface, 5, w.LONG, w.HWND)(self.interface, self.hwnd)  # DeleteTab
            com_method(self.interface, 2, w.ULONG)(self.interface)  # Release
            self.interface = c.c_void_p()
        if self.store:
            self.clear_properties()
            com_method(self.store, 2, w.ULONG)(self.store)  # Release
            self.store = c.c_void_p()
        if self.uninitialize:
            ole.CoUninitialize()
            self.uninitialize = False
