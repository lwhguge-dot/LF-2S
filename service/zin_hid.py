"""Finite, passive Windows HID capture for the vendor ZIN_QUAD flight controller.

Uses GENERIC_READ only. No output reports, feature writes, calibration, PID,
arming, motor or flight commands are implemented. Raw reports remain the evidence;
decoded labels follow the supplied vendor ANO_DT.c, not a firmware identity claim.
"""

from __future__ import annotations

import ctypes as C
import struct
import time
from ctypes import wintypes as W


class GUID(C.Structure):
    _fields_ = [
        ("Data1", W.DWORD),
        ("Data2", W.WORD),
        ("Data3", W.WORD),
        ("Data4", C.c_ubyte * 8),
    ]


class InterfaceData(C.Structure):
    _fields_ = [
        ("cbSize", W.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", W.DWORD),
        ("Reserved", C.c_size_t),
    ]


class Attributes(C.Structure):
    _fields_ = [
        ("Size", W.ULONG),
        ("VendorID", W.USHORT),
        ("ProductID", W.USHORT),
        ("VersionNumber", W.USHORT),
    ]


class Caps(C.Structure):
    _fields_ = [
        (name, W.USHORT)
        for name in (
            "Usage",
            "UsagePage",
            "InputReportByteLength",
            "OutputReportByteLength",
            "FeatureReportByteLength",
        )
    ]
    _fields_ += [("Reserved", W.USHORT * 17)]
    _fields_ += [
        (name, W.USHORT)
        for name in (
            "NumberLinkCollectionNodes",
            "NumberInputButtonCaps",
            "NumberInputValueCaps",
            "NumberInputDataIndices",
            "NumberOutputButtonCaps",
            "NumberOutputValueCaps",
            "NumberOutputDataIndices",
            "NumberFeatureButtonCaps",
            "NumberFeatureValueCaps",
            "NumberFeatureDataIndices",
        )
    ]


class Overlapped(C.Structure):
    _fields_ = [
        ("Internal", C.c_size_t),
        ("InternalHigh", C.c_size_t),
        ("Offset", W.DWORD),
        ("OffsetHigh", W.DWORD),
        ("hEvent", W.HANDLE),
    ]


class WindowsHid:
    """Only read/query APIs are exposed, plus handle and read cancellation APIs."""

    def __init__(self):
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.h = C.WinDLL("hid", use_last_error=True)
        self.s = C.WinDLL("setupapi", use_last_error=True)
        signatures = [
            (self.h, "HidD_GetHidGuid", [C.POINTER(GUID)], None),
            (
                self.h,
                "HidD_GetAttributes",
                [W.HANDLE, C.POINTER(Attributes)],
                W.BOOLEAN,
            ),
            (
                self.h,
                "HidD_GetPreparsedData",
                [W.HANDLE, C.POINTER(C.c_void_p)],
                W.BOOLEAN,
            ),
            (self.h, "HidD_FreePreparsedData", [C.c_void_p], W.BOOLEAN),
            (self.h, "HidP_GetCaps", [C.c_void_p, C.POINTER(Caps)], W.LONG),
            (
                self.s,
                "SetupDiGetClassDevsW",
                [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD],
                W.HANDLE,
            ),
            (
                self.s,
                "SetupDiEnumDeviceInterfaces",
                [
                    W.HANDLE,
                    C.c_void_p,
                    C.POINTER(GUID),
                    W.DWORD,
                    C.POINTER(InterfaceData),
                ],
                W.BOOL,
            ),
            (
                self.s,
                "SetupDiGetDeviceInterfaceDetailW",
                [
                    W.HANDLE,
                    C.POINTER(InterfaceData),
                    C.c_void_p,
                    W.DWORD,
                    C.POINTER(W.DWORD),
                    C.c_void_p,
                ],
                W.BOOL,
            ),
            (self.s, "SetupDiDestroyDeviceInfoList", [W.HANDLE], W.BOOL),
            (
                self.k,
                "CreateFileW",
                [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE],
                W.HANDLE,
            ),
            (self.k, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.k, "CreateEventW", [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE),
            (self.k, "ResetEvent", [W.HANDLE], W.BOOL),
            (
                self.k,
                "ReadFile",
                [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(Overlapped)],
                W.BOOL,
            ),
            (self.k, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            (
                self.k,
                "GetOverlappedResult",
                [W.HANDLE, C.POINTER(Overlapped), C.POINTER(W.DWORD), W.BOOL],
                W.BOOL,
            ),
            (self.k, "CancelIoEx", [W.HANDLE, C.POINTER(Overlapped)], W.BOOL),
        ]
        for library, name, arguments, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
        for name in (
            "HidD_GetManufacturerString",
            "HidD_GetProductString",
            "HidD_GetSerialNumberString",
        ):
            function = getattr(self.h, name)
            function.argtypes = [W.HANDLE, C.c_void_p, W.ULONG]
            function.restype = W.BOOLEAN
        self.invalid = C.c_void_p(-1).value

    def open(self, path, *, read=False):
        handle = self.k.CreateFileW(
            path, 0x80000000 if read else 0, 3, None, 3, 0x40000000 if read else 0, None
        )
        if handle == self.invalid:
            raise C.WinError(C.get_last_error())
        return handle

    def enumerate(self):
        guid = GUID()
        self.h.HidD_GetHidGuid(C.byref(guid))
        info = self.s.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
        if info == self.invalid:
            raise C.WinError(C.get_last_error())
        devices = []
        try:
            index = 0
            while True:
                item = InterfaceData(cbSize=C.sizeof(InterfaceData))
                if not self.s.SetupDiEnumDeviceInterfaces(
                    info, None, C.byref(guid), index, C.byref(item)
                ):
                    error = C.get_last_error()
                    if error == 259:
                        break
                    raise C.WinError(error)
                index += 1
                size = W.DWORD()
                self.s.SetupDiGetDeviceInterfaceDetailW(
                    info, C.byref(item), None, 0, C.byref(size), None
                )
                if not size.value:
                    raise C.WinError(C.get_last_error())
                detail = C.create_string_buffer(size.value)
                C.cast(detail, C.POINTER(W.DWORD))[0] = (
                    8 if C.sizeof(C.c_void_p) == 8 else 6
                )
                if not self.s.SetupDiGetDeviceInterfaceDetailW(
                    info, C.byref(item), detail, size.value, None, None
                ):
                    raise C.WinError(C.get_last_error())
                path = C.wstring_at(C.addressof(detail) + 4)
                if "vid_0483&pid_a001" not in path.lower():
                    continue
                handle = self.open(path)
                try:
                    attr = Attributes(Size=C.sizeof(Attributes))
                    if not self.h.HidD_GetAttributes(handle, C.byref(attr)):
                        raise C.WinError(C.get_last_error())
                    entry = {
                        "path": path,
                        "vid": attr.VendorID,
                        "pid": attr.ProductID,
                        "device_revision": attr.VersionNumber,
                        "string_query_errors": [],
                    }
                    for key, name in (
                        ("manufacturer", "HidD_GetManufacturerString"),
                        ("product", "HidD_GetProductString"),
                        ("serial", "HidD_GetSerialNumberString"),
                    ):
                        entry[key] = None
                        for attempt in range(3):
                            value = C.create_unicode_buffer(256)
                            if getattr(self.h, name)(handle, value, C.sizeof(value)):
                                entry[key] = value.value
                                break
                            entry["string_query_errors"].append(
                                {
                                    "field": key,
                                    "attempt": attempt + 1,
                                    "winerror": C.get_last_error(),
                                }
                            )
                            time.sleep(0.02)
                        time.sleep(0.02)
                    preparsed = C.c_void_p()
                    if not self.h.HidD_GetPreparsedData(handle, C.byref(preparsed)):
                        raise C.WinError(C.get_last_error())
                    try:
                        caps = Caps()
                        status = self.h.HidP_GetCaps(preparsed, C.byref(caps))
                        if status != 0x00110000:
                            raise RuntimeError(f"HidP_GetCaps status {status:#x}")
                        entry["caps"] = {
                            name: getattr(caps, name)
                            for name, _ in Caps._fields_
                            if name != "Reserved"
                        }
                    finally:
                        self.h.HidD_FreePreparsedData(preparsed)
                    devices.append(entry)
                finally:
                    self.k.CloseHandle(handle)
        finally:
            self.s.SetupDiDestroyDeviceInfoList(info)
        return devices

    def read(self, handle, size, timeout_ms):
        event = self.k.CreateEventW(None, True, False, None)
        if not event:
            raise C.WinError(C.get_last_error())
        overlapped = Overlapped(hEvent=event)
        buffer = C.create_string_buffer(size)
        pending = False
        try:
            if not self.k.ReadFile(handle, buffer, size, None, C.byref(overlapped)):
                error = C.get_last_error()
                if error != 997:
                    raise C.WinError(error)
                pending = True
                result = self.k.WaitForSingleObject(event, timeout_ms)
                if result != 0:
                    self.k.CancelIoEx(handle, C.byref(overlapped))
                    transferred = W.DWORD()
                    self.k.GetOverlappedResult(
                        handle, C.byref(overlapped), C.byref(transferred), True
                    )
                    pending = False
                    if result == 258:
                        return None
                    raise C.WinError(C.get_last_error())
            transferred = W.DWORD()
            if not self.k.GetOverlappedResult(
                handle, C.byref(overlapped), C.byref(transferred), False
            ):
                raise C.WinError(C.get_last_error())
            pending = False
            return buffer.raw[: transferred.value]
        finally:
            if pending:
                self.k.CancelIoEx(handle, C.byref(overlapped))
                transferred = W.DWORD()
                self.k.GetOverlappedResult(
                    handle, C.byref(overlapped), C.byref(transferred), True
                )
            self.k.CloseHandle(event)


def decode(frame):
    function, payload = frame[2], frame[4:-1]
    result = {"function": f"0x{function:02x}", "payload_hex": payload.hex()}
    if function == 1 and len(payload) == 12:
        roll, pitch, yaw = struct.unpack_from(">hhh", payload)
        result.update(
            roll_deg=roll / 100,
            pitch_deg=pitch / 100,
            yaw_deg=yaw / 100,
            height_raw=struct.unpack_from(">i", payload, 6)[0],
            # Vendor source and standard ANO disagree on the last two bytes.
            # Preserve their positions until the installed firmware is identified.
            status_byte10_raw=payload[10],
            status_byte11_raw=payload[11],
        )
    elif function == 2 and len(payload) == 18:
        names = (
            "acc_x_raw",
            "acc_y_raw",
            "acc_z_raw",
            "gyro_x_raw",
            "gyro_y_raw",
            "gyro_z_raw",
            "mag_x_raw",
            "mag_y_raw",
            "mag_z_raw",
        )
        result.update(zip(names, struct.unpack(">9h", payload), strict=True))
    elif function == 3 and len(payload) == 20:
        result["rc_channels_raw"] = list(struct.unpack(">10H", payload))
    return result


class StreamParser:
    def __init__(self):
        self.buffer = bytearray()
        self.checksum_failures = 0
        self.discarded_bytes = 0

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 2:
            if self.buffer[:2] != b"\xaa\xaa":
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            if len(self.buffer) < 4:
                break
            length = self.buffer[3] + 5
            if len(self.buffer) < length:
                break
            candidate = bytes(self.buffer[:length])
            if sum(candidate[:-1]) & 255 != candidate[-1]:
                self.checksum_failures += 1
                self.discarded_bytes += 1
                del self.buffer[0]
                continue
            frames.append(candidate)
            del self.buffer[:length]
        return frames
