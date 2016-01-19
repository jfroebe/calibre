#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

import os, string, _winreg as winreg, re, time, sys
from collections import namedtuple, defaultdict
from operator import itemgetter
from ctypes import (
    Structure, POINTER, c_ubyte, windll, byref, c_void_p, WINFUNCTYPE, c_uint,
    WinError, get_last_error, sizeof, c_wchar, create_string_buffer, cast,
    memset, wstring_at, addressof, create_unicode_buffer, string_at, c_uint64 as QWORD
)
from ctypes.wintypes import DWORD, WORD, ULONG, LPCWSTR, HWND, BOOL, LPWSTR, UINT, BYTE, HANDLE, USHORT
from pprint import pprint, pformat
from future_builtins import map

from calibre import prints, as_unicode

is64bit = sys.maxsize > (1 << 32)

class NoRemovableDrives(WindowsError):
    pass

# Data and function type definitions {{{

class GUID(Structure):
    _fields_ = [
        ("data1", DWORD),
        ("data2", WORD),
        ("data3", WORD),
        ("data4", c_ubyte * 8)]

    def __init__(self, l, w1, w2, b1, b2, b3, b4, b5, b6, b7, b8):
        self.data1 = l
        self.data2 = w1
        self.data3 = w2
        self.data4[0] = b1
        self.data4[1] = b2
        self.data4[2] = b3
        self.data4[3] = b4
        self.data4[4] = b5
        self.data4[5] = b6
        self.data4[6] = b7
        self.data4[7] = b8

    def __str__(self):
        return "{%08x-%04x-%04x-%s-%s}" % (
            self.data1,
            self.data2,
            self.data3,
            ''.join(["%02x" % d for d in self.data4[:2]]),
            ''.join(["%02x" % d for d in self.data4[2:]]),
        )

CONFIGRET = DWORD
DEVINST = DWORD
LPDWORD = POINTER(DWORD)
LPVOID = c_void_p
REG_QWORD = 11
IOCTL_STORAGE_MEDIA_REMOVAL = 0x2D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
def CTL_CODE(DeviceType, Function, Method, Access):
    return (DeviceType << 16) | (Access << 14) | (Function << 2) | Method
def USB_CTL(id):
    # CTL_CODE(FILE_DEVICE_USB, (id), METHOD_BUFFERED, FILE_ANY_ACCESS)
    return CTL_CODE(0x22, id, 0, 0)
IOCTL_USB_GET_ROOT_HUB_NAME = USB_CTL(258)
IOCTL_USB_GET_NODE_INFORMATION = USB_CTL(258)
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION = USB_CTL(259)
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = USB_CTL(274)
IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME = USB_CTL(264)
IOCTL_USB_GET_NODE_CONNECTION_NAME = USB_CTL(261)
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = USB_CTL(260)
USB_CONFIGURATION_DESCRIPTOR_TYPE = 2
USB_STRING_DESCRIPTOR_TYPE = 3
USB_INTERFACE_DESCRIPTOR_TYPE = 4
USB_REQUEST_GET_DESCRIPTOR = 0x06
MAXIMUM_USB_STRING_LENGTH = 255

StorageDeviceNumber = namedtuple('StorageDeviceNumber', 'type number partition_number')

class STORAGE_DEVICE_NUMBER(Structure):
    _fields_ = [
        ('DeviceType', DWORD),
        ('DeviceNumber', ULONG),
        ('PartitionNumber', ULONG)
    ]

    def as_tuple(self):
        return StorageDeviceNumber(self.DeviceType, self.DeviceNumber, self.PartitionNumber)

class SP_DEVINFO_DATA(Structure):
    _fields_ = [
        ('cbSize', DWORD),
        ('ClassGuid', GUID),
        ('DevInst', DEVINST),
        ('Reserved', POINTER(ULONG)),
    ]
    def __str__(self):
        return "ClassGuid:%s DevInst:%s" % (self.ClassGuid, self.DevInst)

PSP_DEVINFO_DATA = POINTER(SP_DEVINFO_DATA)

class SP_DEVICE_INTERFACE_DATA(Structure):
    _fields_ = [
        ('cbSize', DWORD),
        ('InterfaceClassGuid', GUID),
        ('Flags', DWORD),
        ('Reserved', POINTER(ULONG)),
    ]
    def __str__(self):
        return "InterfaceClassGuid:%s Flags:%s" % (self.InterfaceClassGuid, self.Flags)

ANYSIZE_ARRAY = 1

class SP_DEVICE_INTERFACE_DETAIL_DATA(Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("DevicePath", c_wchar*ANYSIZE_ARRAY)
    ]

UCHAR = c_ubyte

class USB_DEVICE_DESCRIPTOR(Structure):
    _fields_ = (
        ('bLength', UCHAR),
        ('bDescriptorType', UCHAR),
        ('bcdUSB', USHORT),
        ('bDeviceClass', UCHAR),
        ('bDeviceSubClass', UCHAR),
        ('bDeviceProtocol', UCHAR),
        ('bMaxPacketSize0', UCHAR),
        ('idVendor', USHORT),
        ('idProduct', USHORT),
        ('bcdDevice', USHORT),
        ('iManufacturer', UCHAR),
        ('iProduct', UCHAR),
        ('iSerialNumber', UCHAR),
        ('bNumConfigurations', UCHAR),
    )

    def __repr__(self):
        return 'USBDevice(class=0x%x sub_class=0x%x protocol=0x%x vendor_id=0x%x product_id=0x%x bcd=0x%x manufacturer=%d product=%d serial_number=%d)' % (
            self.bDeviceClass, self.bDeviceSubClass, self.bDeviceProtocol,
            self.idVendor, self.idProduct, self.bcdDevice, self.iManufacturer,
            self.iProduct, self.iSerialNumber)

class USB_ENDPOINT_DESCRIPTOR(Structure):
    _fields_ = (
        ('bLength', UCHAR),
        ('bDescriptorType', UCHAR),
        ('bEndpointAddress', UCHAR),
        ('bmAttributes', UCHAR),
        ('wMaxPacketSize', USHORT),
        ('bInterval', UCHAR)
    )

class USB_PIPE_INFO(Structure):
    _fields_ = (
        ('EndpointDescriptor', USB_ENDPOINT_DESCRIPTOR),
        ('ScheduleOffset', ULONG),
    )


class USB_NODE_CONNECTION_INFORMATION_EX(Structure):
    _fields_ = (
        ('ConnectionIndex', ULONG),
        ('DeviceDescriptor', USB_DEVICE_DESCRIPTOR),
        ('CurrentConfigurationValue', UCHAR),
        ('Speed', UCHAR),
        ('DeviceIsHub', BOOL),
        ('DeviceAddress', USHORT),
        ('NumberOfOpenPipes', ULONG),
        ('ConnectionStatus', c_uint),
        ('PipeList', USB_PIPE_INFO*ANYSIZE_ARRAY),
    )

class USB_STRING_DESCRIPTOR(Structure):
    _fields_ = (
        ('bLength', UCHAR),
        ('bType', UCHAR),
        ('String', UCHAR * ANYSIZE_ARRAY),
    )

class USB_DESCRIPTOR_REQUEST(Structure):

    class SetupPacket(Structure):
        _fields_ = (
            ('bmRequest', UCHAR),
            ('bRequest', UCHAR),
            ('wValue', UCHAR*2),
            ('wIndex', USHORT),
            ('wLength', USHORT),
        )

    _fields_ = (
        ('ConnectionIndex', ULONG),
        ('SetupPacket', SetupPacket),
        ('Data', USB_STRING_DESCRIPTOR),
    )

PUSB_DESCRIPTOR_REQUEST = POINTER(USB_DESCRIPTOR_REQUEST)
PSP_DEVICE_INTERFACE_DETAIL_DATA = POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA)
PSP_DEVICE_INTERFACE_DATA = POINTER(SP_DEVICE_INTERFACE_DATA)
INVALID_HANDLE_VALUE = c_void_p(-1).value
GENERIC_READ = 0x80000000L
GENERIC_WRITE = 0x40000000L
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
GUID_DEVINTERFACE_VOLUME     = GUID(0x53F5630D, 0xB6BF, 0x11D0, 0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B)
GUID_DEVINTERFACE_DISK       = GUID(0x53F56307, 0xB6BF, 0x11D0, 0x94, 0xF2, 0x00, 0xA0, 0xC9, 0x1E, 0xFB, 0x8B)
GUID_DEVINTERFACE_CDROM      = GUID(0x53f56308, 0xb6bf, 0x11d0, 0x94, 0xf2, 0x00, 0xa0, 0xc9, 0x1e, 0xfb, 0x8b)
GUID_DEVINTERFACE_FLOPPY     = GUID(0x53f56311, 0xb6bf, 0x11d0, 0x94, 0xf2, 0x00, 0xa0, 0xc9, 0x1e, 0xfb, 0x8b)
GUID_DEVINTERFACE_USB_DEVICE = GUID(0xA5DCBF10, 0x6530, 0x11D2, 0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED)
GUID_DEVINTERFACE_USB_HUB    = GUID(0xf18a0e88, 0xc30c, 0x11d0, 0x88, 0x15, 0x00, 0xa0, 0xc9, 0x06, 0xbe, 0xd8)
DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 0, 1, 2, 3, 4, 5, 6
DIGCF_PRESENT = 0x00000002
DIGCF_ALLCLASSES = 0x00000004
DIGCF_DEVICEINTERFACE = 0x00000010
ERROR_INSUFFICIENT_BUFFER = 0x7a
ERROR_MORE_DATA = 234
ERROR_INVALID_DATA = 0xd
ERROR_GEN_FAILURE = 31
HDEVINFO = HANDLE
SPDRP_DEVICEDESC = DWORD(0x00000000)
SPDRP_HARDWAREID = DWORD(0x00000001)
SPDRP_COMPATIBLEIDS = DWORD(0x00000002)
SPDRP_UNUSED0 = DWORD(0x00000003)
SPDRP_SERVICE = DWORD(0x00000004)
SPDRP_UNUSED1 = DWORD(0x00000005)
SPDRP_UNUSED2 = DWORD(0x00000006)
SPDRP_CLASS = DWORD(0x00000007)
SPDRP_CLASSGUID = DWORD(0x00000008)
SPDRP_DRIVER = DWORD(0x00000009)
SPDRP_CONFIGFLAGS = DWORD(0x0000000A)
SPDRP_MFG = DWORD(0x0000000B)
SPDRP_FRIENDLYNAME = DWORD(0x0000000C)
SPDRP_LOCATION_INFORMATION = DWORD(0x0000000D)
SPDRP_PHYSICAL_DEVICE_OBJECT_NAME = DWORD(0x0000000E)
SPDRP_CAPABILITIES = DWORD(0x0000000F)
SPDRP_UI_NUMBER = DWORD(0x00000010)
SPDRP_UPPERFILTERS = DWORD(0x00000011)
SPDRP_LOWERFILTERS = DWORD(0x00000012)
SPDRP_BUSTYPEGUID = DWORD(0x00000013)
SPDRP_LEGACYBUSTYPE = DWORD(0x00000014)
SPDRP_BUSNUMBER = DWORD(0x00000015)
SPDRP_ENUMERATOR_NAME = DWORD(0x00000016)
SPDRP_SECURITY = DWORD(0x00000017)
SPDRP_SECURITY_SDS = DWORD(0x00000018)
SPDRP_DEVTYPE = DWORD(0x00000019)
SPDRP_EXCLUSIVE = DWORD(0x0000001A)
SPDRP_CHARACTERISTICS = DWORD(0x0000001B)
SPDRP_ADDRESS = DWORD(0x0000001C)
SPDRP_UI_NUMBER_DESC_FORMAT = DWORD(0x0000001D)
SPDRP_DEVICE_POWER_DATA = DWORD(0x0000001E)
SPDRP_REMOVAL_POLICY = DWORD(0x0000001F)
SPDRP_REMOVAL_POLICY_HW_DEFAULT = DWORD(0x00000020)
SPDRP_REMOVAL_POLICY_OVERRIDE = DWORD(0x00000021)
SPDRP_INSTALL_STATE = DWORD(0x00000022)
SPDRP_LOCATION_PATHS = DWORD(0x00000023)

CR_CODES, CR_CODE_NAMES = {}, {}
for line in '''\
#define CR_SUCCESS                  			0x00000000
#define CR_DEFAULT                        0x00000001
#define CR_OUT_OF_MEMORY                  0x00000002
#define CR_INVALID_POINTER                0x00000003
#define CR_INVALID_FLAG                   0x00000004
#define CR_INVALID_DEVNODE                0x00000005
#define CR_INVALID_DEVINST          			CR_INVALID_DEVNODE
#define CR_INVALID_RES_DES                0x00000006
#define CR_INVALID_LOG_CONF               0x00000007
#define CR_INVALID_ARBITRATOR             0x00000008
#define CR_INVALID_NODELIST               0x00000009
#define CR_DEVNODE_HAS_REQS               0x0000000A
#define CR_DEVINST_HAS_REQS         			CR_DEVNODE_HAS_REQS
#define CR_INVALID_RESOURCEID             0x0000000B
#define CR_DLVXD_NOT_FOUND                0x0000000C
#define CR_NO_SUCH_DEVNODE                0x0000000D
#define CR_NO_SUCH_DEVINST          			CR_NO_SUCH_DEVNODE
#define CR_NO_MORE_LOG_CONF               0x0000000E
#define CR_NO_MORE_RES_DES                0x0000000F
#define CR_ALREADY_SUCH_DEVNODE           0x00000010
#define CR_ALREADY_SUCH_DEVINST     			CR_ALREADY_SUCH_DEVNODE
#define CR_INVALID_RANGE_LIST             0x00000011
#define CR_INVALID_RANGE                  0x00000012
#define CR_FAILURE                        0x00000013
#define CR_NO_SUCH_LOGICAL_DEV            0x00000014
#define CR_CREATE_BLOCKED                 0x00000015
#define CR_NOT_SYSTEM_VM                  0x00000016
#define CR_REMOVE_VETOED                  0x00000017
#define CR_APM_VETOED                     0x00000018
#define CR_INVALID_LOAD_TYPE              0x00000019
#define CR_BUFFER_SMALL                   0x0000001A
#define CR_NO_ARBITRATOR                  0x0000001B
#define CR_NO_REGISTRY_HANDLE             0x0000001C
#define CR_REGISTRY_ERROR                 0x0000001D
#define CR_INVALID_DEVICE_ID              0x0000001E
#define CR_INVALID_DATA                   0x0000001F
#define CR_INVALID_API                    0x00000020
#define CR_DEVLOADER_NOT_READY            0x00000021
#define CR_NEED_RESTART                   0x00000022
#define CR_NO_MORE_HW_PROFILES            0x00000023
#define CR_DEVICE_NOT_THERE               0x00000024
#define CR_NO_SUCH_VALUE                  0x00000025
#define CR_WRONG_TYPE                     0x00000026
#define CR_INVALID_PRIORITY               0x00000027
#define CR_NOT_DISABLEABLE                0x00000028
#define CR_FREE_RESOURCES                 0x00000029
#define CR_QUERY_VETOED                   0x0000002A
#define CR_CANT_SHARE_IRQ                 0x0000002B
#define CR_NO_DEPENDENT                   0x0000002C
#define CR_SAME_RESOURCES                 0x0000002D
#define CR_NO_SUCH_REGISTRY_KEY           0x0000002E
#define CR_INVALID_MACHINENAME            0x0000002F
#define CR_REMOTE_COMM_FAILURE            0x00000030
#define CR_MACHINE_UNAVAILABLE            0x00000031
#define CR_NO_CM_SERVICES                 0x00000032
#define CR_ACCESS_DENIED                  0x00000033
#define CR_CALL_NOT_IMPLEMENTED           0x00000034
#define CR_INVALID_PROPERTY               0x00000035
#define CR_DEVICE_INTERFACE_ACTIVE        0x00000036
#define CR_NO_SUCH_DEVICE_INTERFACE       0x00000037
#define CR_INVALID_REFERENCE_STRING       0x00000038
#define CR_INVALID_CONFLICT_LIST          0x00000039
#define CR_INVALID_INDEX                  0x0000003A
#define CR_INVALID_STRUCTURE_SIZE         0x0000003B'''.splitlines():
    line = line.strip()
    if line:
        name, code = line.split()[1:]
        if code.startswith('0x'):
            code = int(code, 16)
        else:
            code = CR_CODES[code]
        CR_CODES[name] = code
        CR_CODE_NAMES[code] = name
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0
CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES = 1
CM_GET_DEVICE_INTERFACE_LIST_BITS = 1
setupapi = windll.setupapi
cfgmgr = windll.CfgMgr32
kernel32 = windll.Kernel32

def cwrap(name, restype, *argtypes, **kw):
    errcheck = kw.pop('errcheck', None)
    use_last_error = bool(kw.pop('use_last_error', True))
    prototype = WINFUNCTYPE(restype, *argtypes, use_last_error=use_last_error)
    lib = cfgmgr if name.startswith('CM') else setupapi
    func = prototype((name, kw.pop('lib', lib)))
    if kw:
        raise TypeError('Unknown keyword arguments: %r' % kw)
    if errcheck is not None:
        func.errcheck = errcheck
    return func

def handle_err_check(result, func, args):
    if result == INVALID_HANDLE_VALUE:
        raise WinError(get_last_error())
    return result

def bool_err_check(result, func, args):
    if not result:
        raise WinError(get_last_error())
    return result

def config_err_check(result, func, args):
    if result != CR_CODES['CR_SUCCESS']:
        raise WindowsError(result, 'The cfgmgr32 function failed with err: %s' % CR_CODE_NAMES.get(result, result))
    return args

GetLogicalDrives = cwrap('GetLogicalDrives', DWORD, errcheck=bool_err_check, lib=kernel32)
GetDriveType = cwrap('GetDriveTypeW', UINT, LPCWSTR, lib=kernel32)
GetVolumeNameForVolumeMountPoint = cwrap('GetVolumeNameForVolumeMountPointW', BOOL, LPCWSTR, LPWSTR, DWORD, errcheck=bool_err_check, lib=kernel32)
GetVolumePathNamesForVolumeName = cwrap('GetVolumePathNamesForVolumeNameW', BOOL, LPCWSTR, LPWSTR, DWORD, POINTER(DWORD), errcheck=bool_err_check, lib=kernel32)
ExpandEnvironmentStrings = cwrap('ExpandEnvironmentStringsW', DWORD, LPCWSTR, LPWSTR, DWORD, errcheck=bool_err_check, lib=kernel32)
CreateFile = cwrap('CreateFileW', HANDLE, LPCWSTR, DWORD, DWORD, c_void_p, DWORD, DWORD, HANDLE, errcheck=handle_err_check, lib=kernel32)
DeviceIoControl = cwrap('DeviceIoControl', BOOL, HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD, POINTER(DWORD), LPVOID, errcheck=bool_err_check, lib=kernel32)
CloseHandle = cwrap('CloseHandle', BOOL, HANDLE, errcheck=bool_err_check, lib=kernel32)
QueryDosDevice = cwrap('QueryDosDeviceW', DWORD, LPCWSTR, LPWSTR, DWORD, errcheck=bool_err_check, lib=kernel32)

SetupDiGetClassDevs = cwrap('SetupDiGetClassDevsW', HDEVINFO, POINTER(GUID), LPCWSTR, HWND, DWORD, errcheck=handle_err_check)
SetupDiEnumDeviceInterfaces = cwrap('SetupDiEnumDeviceInterfaces', BOOL, HDEVINFO, PSP_DEVINFO_DATA, POINTER(GUID), DWORD, PSP_DEVICE_INTERFACE_DATA)
SetupDiDestroyDeviceInfoList = cwrap('SetupDiDestroyDeviceInfoList', BOOL, HDEVINFO, errcheck=bool_err_check)
SetupDiGetDeviceInterfaceDetail = cwrap(
    'SetupDiGetDeviceInterfaceDetailW', BOOL, HDEVINFO, PSP_DEVICE_INTERFACE_DATA, PSP_DEVICE_INTERFACE_DETAIL_DATA, DWORD, POINTER(DWORD), PSP_DEVINFO_DATA)
SetupDiEnumDeviceInfo = cwrap('SetupDiEnumDeviceInfo', BOOL, HDEVINFO, DWORD, PSP_DEVINFO_DATA)
SetupDiGetDeviceRegistryProperty = cwrap(
    'SetupDiGetDeviceRegistryPropertyW', BOOL, HDEVINFO, PSP_DEVINFO_DATA, DWORD, POINTER(DWORD), POINTER(BYTE), DWORD, POINTER(DWORD))

CM_Get_Parent = cwrap('CM_Get_Parent', CONFIGRET, POINTER(DEVINST), DEVINST, ULONG, errcheck=config_err_check)
CM_Get_Child = cwrap('CM_Get_Child', CONFIGRET, POINTER(DEVINST), DEVINST, ULONG, errcheck=config_err_check)
CM_Get_Sibling = cwrap('CM_Get_Sibling', CONFIGRET, POINTER(DEVINST), DEVINST, ULONG, errcheck=config_err_check)
CM_Get_Device_ID_Size = cwrap('CM_Get_Device_ID_Size', CONFIGRET, POINTER(ULONG), DEVINST, ULONG)
CM_Get_Device_ID = cwrap('CM_Get_Device_IDW', CONFIGRET, DEVINST, LPWSTR, ULONG, ULONG)
CM_Request_Device_Eject = cwrap('CM_Request_Device_EjectW', CONFIGRET, DEVINST, c_void_p, LPWSTR, ULONG, ULONG, errcheck=config_err_check)
CM_Get_Device_Interface_List = cwrap('CM_Get_Device_Interface_ListW', CONFIGRET, POINTER(GUID), LPCWSTR, LPWSTR, ULONG, ULONG, errcheck=config_err_check)

# }}}

# Utility functions {{{

_devid_pat = None
def devid_pat():
    global _devid_pat
    if _devid_pat is None:
        _devid_pat = re.compile(r'VID_([a-f0-9]{4})&PID_([a-f0-9]{4})&REV_([a-f0-9:]{4})', re.I)
    return _devid_pat


class DeviceSet(object):

    def __init__(self, guid=GUID_DEVINTERFACE_VOLUME, enumerator=None, flags=DIGCF_PRESENT | DIGCF_DEVICEINTERFACE):
        self.guid_ref, self.enumerator, self.flags = (None if guid is None else byref(guid)), enumerator, flags
        self.dev_list = SetupDiGetClassDevs(self.guid_ref, self.enumerator, None, self.flags)

    def __del__(self):
        SetupDiDestroyDeviceInfoList(self.dev_list)
        del self.dev_list

    def interfaces(self, ignore_errors=False, yield_devlist=False):
        interface_data = SP_DEVICE_INTERFACE_DATA()
        interface_data.cbSize = sizeof(SP_DEVICE_INTERFACE_DATA)
        buf = None
        i = -1
        while True:
            i += 1
            if not SetupDiEnumDeviceInterfaces(self.dev_list, None, self.guid_ref, i, byref(interface_data)):
                break
            try:
                buf, devinfo, devpath = get_device_interface_detail_data(self.dev_list, byref(interface_data), buf)
            except WindowsError:
                if ignore_errors:
                    continue
                raise
            if yield_devlist:
                yield self.dev_list, devinfo, devpath
            else:
                yield devinfo, devpath

    def devices(self):
        devinfo = SP_DEVINFO_DATA()
        devinfo.cbSize = sizeof(SP_DEVINFO_DATA)
        i = -1
        while True:
            i += 1
            if not SetupDiEnumDeviceInfo(self.dev_list, i, byref(devinfo)):
                break
            yield self.dev_list, devinfo


def iterchildren(parent_devinst):
    child = DEVINST(0)
    NO_MORE = CR_CODES['CR_NO_SUCH_DEVINST']
    try:
        CM_Get_Child(byref(child), parent_devinst, 0)
    except WindowsError as err:
        if err.winerror == NO_MORE:
            return
        raise
    yield child.value
    while True:
        try:
            CM_Get_Sibling(byref(child), child, 0)
        except WindowsError as err:
            if err.winerror == NO_MORE:
                break
            raise
        yield child.value

def iterdescendants(parent_devinst):
    for child in iterchildren(parent_devinst):
        yield child
        for gc in iterdescendants(child):
            yield gc

def iterancestors(devinst):
    NO_MORE = CR_CODES['CR_NO_SUCH_DEVINST']
    parent = DEVINST(devinst)
    while True:
        try:
            CM_Get_Parent(byref(parent), parent, 0)
        except WindowsError as err:
            if err.winerror == NO_MORE:
                break
            raise
        yield parent.value

def device_io_control(handle, which, inbuf, outbuf, initbuf):
    bytes_returned = DWORD(0)
    while True:
        initbuf(inbuf)
        try:
            DeviceIoControl(handle, which, inbuf, len(inbuf), outbuf, len(outbuf), byref(bytes_returned), None)
        except WindowsError as err:
            if err.winerror not in (ERROR_INSUFFICIENT_BUFFER, ERROR_MORE_DATA):
                raise
            outbuf = create_string_buffer(2*len(outbuf))
        else:
            return outbuf, bytes_returned

def get_storage_number(devpath):
    sdn = STORAGE_DEVICE_NUMBER()
    handle = CreateFile(devpath, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    bytes_returned = DWORD(0)
    try:
        DeviceIoControl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0, byref(sdn), sizeof(STORAGE_DEVICE_NUMBER), byref(bytes_returned), None)
    finally:
        CloseHandle(handle)
    return sdn.as_tuple()

def get_all_removable_drives(allow_fixed=False):
    mask = GetLogicalDrives()
    ans = {}
    buf = create_unicode_buffer(100)
    for drive_letter in string.ascii_uppercase:
        drive_present = bool(mask & 0b1)
        mask >>= 1
        drive_root = drive_letter + ':' + os.sep
        if not drive_present:
            continue
        drive_type = GetDriveType(drive_root)
        if drive_type == DRIVE_REMOVABLE or (allow_fixed and drive_type == DRIVE_FIXED):  # Removable, present drive
            try:
                GetVolumeNameForVolumeMountPoint(drive_root, buf, len(buf))
            except WindowsError:
                continue
            ans[buf.value] = drive_letter
    return ans

def get_device_id(devinst, buf=None):
    if buf is None:
        buf = create_unicode_buffer(512)
    while True:
        ret = CM_Get_Device_ID(devinst, buf, len(buf), 0)
        if ret == CR_CODES['CR_BUFFER_SMALL']:
            devid_size = ULONG(0)
            CM_Get_Device_ID_Size(byref(devid_size), devinst, 0)
            buf = create_unicode_buffer(devid_size.value)
            continue
        if ret != CR_CODES['CR_SUCCESS']:
            raise WindowsError((ret, 'The cfgmgr32 function failed with err: %s' % CR_CODE_NAMES.get(ret, ret)))
        break
    return wstring_at(buf), buf

def drive_letter_from_volume_devpath(devpath, drive_map):
    pbuf = create_unicode_buffer(512)
    if not devpath.endswith(os.sep):
        devpath += os.sep
    try:
        GetVolumeNameForVolumeMountPoint(devpath, pbuf, len(pbuf))
    except WindowsError:
        pass
    else:
        return drive_map.get(pbuf.value)

def expand_environment_strings(src):
    sz = ExpandEnvironmentStrings(src, None, 0)
    while True:
        buf = create_unicode_buffer(sz)
        nsz = ExpandEnvironmentStrings(src, buf, len(buf))
        if nsz <= sz:
            return buf.value
        sz = nsz

def convert_registry_data(raw, size, dtype):
    if dtype == winreg.REG_NONE:
        return None
    if dtype == winreg.REG_BINARY:
        return string_at(raw, size)
    if dtype in (winreg.REG_SZ, winreg.REG_EXPAND_SZ, winreg.REG_MULTI_SZ):
        ans = wstring_at(raw, size // 2).rstrip('\0')
        if dtype == winreg.REG_MULTI_SZ:
            ans = tuple(ans.split('\0'))
        elif dtype == winreg.REG_EXPAND_SZ:
            ans = expand_environment_strings(ans)
        return ans
    if dtype == winreg.REG_DWORD:
        if size == 0:
            return 0
        return cast(raw, LPDWORD).contents.value
    if dtype == REG_QWORD:
        if size == 0:
            return 0
        return cast(raw, POINTER(QWORD)).contents.value
    raise ValueError('Unsupported data type: %r' % dtype)

def get_device_registry_property(dev_list, p_devinfo, property_type=SPDRP_HARDWAREID, buf=None):
    if buf is None:
        buf = create_string_buffer(1024)
    data_type = DWORD(0)
    required_size = DWORD(0)
    ans = None
    while True:
        if not SetupDiGetDeviceRegistryProperty(dev_list, p_devinfo, property_type, byref(data_type), cast(buf, POINTER(BYTE)), len(buf), byref(required_size)):
            err = get_last_error()
            if err == ERROR_INSUFFICIENT_BUFFER:
                buf = create_string_buffer(required_size)
                continue
            if err == ERROR_INVALID_DATA:
                break
            raise WinError(err)
        ans = convert_registry_data(buf, required_size.value, data_type.value)
        break
    return buf, ans

def get_device_interface_detail_data(dev_list, p_interface_data, buf=None):
    if buf is None:
        buf = create_string_buffer(512)
    detail = cast(buf, PSP_DEVICE_INTERFACE_DETAIL_DATA)
    # See http://stackoverflow.com/questions/10728644/properly-declare-sp-device-interface-detail-data-for-pinvoke
    # for why cbSize needs to be hardcoded below
    detail.contents.cbSize = 8 if is64bit else 6
    required_size = DWORD(0)
    devinfo = SP_DEVINFO_DATA()
    devinfo.cbSize = sizeof(devinfo)
    while True:
        if not SetupDiGetDeviceInterfaceDetail(dev_list, p_interface_data, detail, len(buf), byref(required_size), byref(devinfo)):
            err = get_last_error()
            if err == ERROR_INSUFFICIENT_BUFFER:
                buf = create_string_buffer(required_size + 50)
                detail = cast(buf, PSP_DEVICE_INTERFACE_DETAIL_DATA)
                detail.contents.cbSize = 8 if is64bit else 6
                continue
            raise WinError(err)
        break
    return buf, devinfo, wstring_at(addressof(buf) + sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA._fields_[0][1]))

# }}}

# Enumerate USB devices {{{

_USBDevice = namedtuple('USBDevice', 'vendor_id product_id bcd devid devinst')

class USBDevice(_USBDevice):

    def __repr__(self):
        def r(x):
            if x is None:
                return 'None'
            return '0x%x' % x
        return 'USBDevice(vendor_id=%s product_id=%s bcd=%s devid=%s devinst=%s)' % (
            r(self.vendor_id), r(self.product_id), r(self.bcd), self.devid, self.devinst)

def parse_hex(x):
    return int(x.replace(':', 'a'), 16)

def iterusbdevices():
    buf = None
    pat = devid_pat()
    for dev_list, devinfo in DeviceSet(guid=None, enumerator='USB', flags=DIGCF_PRESENT | DIGCF_ALLCLASSES).devices():
        buf, devid = get_device_registry_property(dev_list, byref(devinfo), buf=buf)
        if devid:
            devid = devid[0].lower()
            m = pat.search(devid)
            if m is None:
                yield USBDevice(None, None, None, devid, devinfo.DevInst)
            else:
                try:
                    vid, pid, bcd = map(parse_hex, m.group(1, 2, 3))
                except Exception:
                    yield USBDevice(None, None, None, devid, devinfo.DevInst)
                else:
                    yield USBDevice(vid, pid, bcd, devid, devinfo.DevInst)

def scan_usb_devices():
    return tuple(iterusbdevices())

# }}}

def get_removable_drives(debug=False):  # {{{
    drive_map = get_all_removable_drives(allow_fixed=False)
    if debug:
        prints('Drive map: %s' % drive_map)
    if not drive_map:
        raise NoRemovableDrives('No removable drives found!')

    ans, buf = {}, None
    for devinfo, devpath in DeviceSet().interfaces():
        candidates = []
        # Get the devpaths for all parents of this device. This is not
        # actually necessary on Vista+, so we simply ignore any windows API
        # failures.
        for parent in iterancestors(devinfo.DevInst):
            try:
                devid, buf = get_device_id(parent, buf=buf)
            except WindowsError:
                break
            candidates.append(devid)
        candidates.append(devpath)

        drive_letter = drive_letter_from_volume_devpath(devpath, drive_map)
        if drive_letter:
            ans[drive_letter] = candidates
        if debug:
            prints('Found volume with device path:', devpath, ' Drive letter:', drive_letter, 'Is removable:', drive_letter in ans)
    return ans
# }}}

def get_drive_letters_for_device(usbdev, storage_number_map=None, debug=False):  # {{{
    '''
    Get the drive letters for a connected device (usbdev must be a USBDevice
    instance). The drive letters are sorted by storage number, which (I think)
    corresponds to the order they are exported by the firmware.
    '''
    ans = {'pnp_id_map': {}, 'drive_letters':[]}
    sort_map = {}

    sn_map = get_storage_number_map(debug=debug) if storage_number_map is None else storage_number_map
    if debug:
        prints('Storage number map:')
        prints(pformat(sn_map))
    if not sn_map:
        return ans

    descendants = frozenset(iterdescendants(usbdev.devinst))
    for devinfo, devpath in DeviceSet(GUID_DEVINTERFACE_DISK).interfaces():
        if devinfo.DevInst in descendants:
            if debug:
                try:
                    devid = get_device_id(devinfo.DevInst)[0]
                except Exception as err:
                    devid = 'Unknown'
            try:
                storage_number = get_storage_number(devpath)
            except WindowsError as err:
                if debug:
                    prints('Failed to get storage number for: %s with error: %s' % (devid, as_unicode(err)))
                continue
            if debug:
                prints('Storage number for %s: %s' % (devid, storage_number))
            if storage_number:
                partitions = sn_map.get(storage_number[:2])
                drive_letters = []
                for partition_number, dl in partitions or ():
                    drive_letters.append(dl)
                    sort_map[dl] = storage_number.number, partition_number
                if drive_letters:
                    for dl in drive_letters:
                        ans['pnp_id_map'][dl] = devpath
                        ans['drive_letters'].append(dl)
    ans['drive_letters'].sort(key=sort_map.get)
    return ans

def get_storage_number_map(drive_types=(DRIVE_REMOVABLE, DRIVE_FIXED), debug=False):
    mask = GetLogicalDrives()
    type_map = {letter:GetDriveType(letter + ':' + os.sep) for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)}
    drives = (letter for letter, dt in type_map.iteritems() if dt in drive_types)
    ans = defaultdict(list)
    for letter in drives:
        try:
            sn = get_storage_number('\\\\.\\' + letter + ':')
            ans[sn[:2]].append((sn[2], letter))
        except WindowsError as err:
            if debug:
                prints('Failed to get storage number for drive: %s with error: %s' % (letter, as_unicode(err)))
            continue
    for val in ans.itervalues():
        val.sort(key=itemgetter(0))
    return dict(ans)


# }}}

def get_usb_devices():  # {{{
    return list(x.devid for x in iterusbdevices())
# }}}

def is_usb_device_connected(vendor_id, product_id):  # {{{
    for usbdev in iterusbdevices():
        if usbdev.vendor_id == vendor_id and usbdev.product_id == product_id:
            return True
    return False
# }}}

def eject_drive(drive_letter):  # {{{
    drive_letter = type('')(drive_letter)[0]
    volume_access_path = '\\\\.\\' + drive_letter + ':'
    try:
        sn = get_storage_number(volume_access_path)[:2]
    except WindowsError:
        return True
    for devinfo, devpath in DeviceSet(guid=GUID_DEVINTERFACE_DISK).interfaces(ignore_errors=True):
        try:
            dsn = get_storage_number(devpath)[:2]
        except WindowsError:
            continue
        if dsn == sn:
            break
    else:
        return False

    parent = DEVINST(0)
    CM_Get_Parent(byref(parent), devinfo.DevInst, 0)
    max_tries = 3
    while max_tries >= 0:
        max_tries -= 1
        try:
            CM_Request_Device_Eject(parent, None, None, 0, 0)
        except WindowsError:
            if max_tries < 0:
                raise
            time.sleep(0.5)
            continue
        return True
    return False

# }}}

def get_usb_info(usbdev, debug=False):  # {{{
    ans = {}
    try:
        parent = next(iterancestors(usbdev.devinst))
    except StopIteration:
        if debug:
            prints('Cannot get USB info as device has no parent (was probably disconnected)')
        return ans
    for devinfo, parent_path in DeviceSet(guid=GUID_DEVINTERFACE_USB_HUB).interfaces():
        if devinfo.DevInst == parent:
            break
    else:
        if debug:
            prints('Cannot get USB info as parent of device is not a HUB')
        return ans
    for devlist, devinfo in DeviceSet(guid=GUID_DEVINTERFACE_USB_DEVICE).devices():
        if devinfo.DevInst == usbdev.devinst:
            device_port = get_device_registry_property(devlist, byref(devinfo), SPDRP_ADDRESS)[1]
            break
    else:
        return ans
    if not device_port:
        if debug:
            prints('Cannot get usb info as the SPDRP_ADDRESS property is not present int he registry (can happen with broken USB hub drivers)')
        return ans
    handle = CreateFile(parent_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    try:
        buf, dd = get_device_descriptor(handle, device_port)
        if dd.idVendor == usbdev.vendor_id and dd.idProduct == usbdev.product_id and dd.bcdDevice == usbdev.bcd:
            # Dont need to read language since we only care about english names
            # buf, langs = get_device_languages(handle, device_port)
            # print(111, langs)
            for index, name in ((dd.iManufacturer, 'manufacturer'), (dd.iProduct, 'product'), (dd.iSerialNumber, 'serial_number')):
                if index:
                    try:
                        buf, ans[name] = get_device_string(handle, device_port, index, buf=buf)
                    except WindowsError as err:
                        if debug:
                            # Note that I have observed that this fails
                            # randomly after some time of my Kindle being
                            # connected. Disconnecting and reconnecting causes
                            # it to start working again.
                            prints('Failed to read %s from device, with error: [%d] %s' % (name, err.winerror, as_unicode(err)))
    finally:
        CloseHandle(handle)
    return ans

def alloc_descriptor_buf(buf):
    if buf is None:
        buf = create_string_buffer(sizeof(USB_DESCRIPTOR_REQUEST) + 700)
    else:
        memset(buf, 0, len(buf))
    return buf

def get_device_descriptor(hub_handle, device_port, buf=None):
    buf = alloc_descriptor_buf(buf)

    def initbuf(b):
        cast(b, POINTER(USB_NODE_CONNECTION_INFORMATION_EX)).contents.ConnectionIndex = device_port

    buf, bytes_returned = device_io_control(hub_handle, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, buf, buf, initbuf)
    return buf, USB_DEVICE_DESCRIPTOR.from_buffer_copy(cast(buf, POINTER(USB_NODE_CONNECTION_INFORMATION_EX)).contents.DeviceDescriptor)

def get_device_string(hub_handle, device_port, index, buf=None, lang=0x409):
    buf = alloc_descriptor_buf(buf)

    def initbuf(b):
        p = cast(b, PUSB_DESCRIPTOR_REQUEST).contents
        p.ConnectionIndex = device_port
        sp = p.SetupPacket
        sp.bmRequest, sp.bRequest = 0x80, USB_REQUEST_GET_DESCRIPTOR
        sp.wValue[0], sp.wValue[1] = index, USB_STRING_DESCRIPTOR_TYPE
        sp.wIndex = lang
        sp.wLength = MAXIMUM_USB_STRING_LENGTH + 2

    buf, bytes_returned = device_io_control(hub_handle, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, buf, buf, initbuf)
    data = cast(buf, PUSB_DESCRIPTOR_REQUEST).contents.Data
    sz, dtype = data.bLength, data.bType
    if dtype != 0x03:
        raise WindowsError('Invalid datatype for string descriptor: 0x%x' % dtype)
    return buf, wstring_at(addressof(data.String), sz // 2).rstrip('\0')

def get_device_languages(hub_handle, device_port, buf=None):
    ' Get the languages supported by the device for strings '
    buf = alloc_descriptor_buf(buf)

    def initbuf(b):
        p = cast(b, PUSB_DESCRIPTOR_REQUEST).contents
        p.ConnectionIndex = device_port
        sp = p.SetupPacket
        sp.bmRequest, sp.bRequest = 0x80, USB_REQUEST_GET_DESCRIPTOR
        sp.wValue[1] = USB_STRING_DESCRIPTOR_TYPE
        sp.wLength = MAXIMUM_USB_STRING_LENGTH + 2

    buf, bytes_returned = device_io_control(hub_handle, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, buf, buf, initbuf)
    data = cast(buf, PUSB_DESCRIPTOR_REQUEST).contents.Data
    sz, dtype = data.bLength, data.bType
    if dtype != 0x03:
        raise WindowsError('Invalid datatype for string descriptor: 0x%x' % dtype)
    data = cast(data.String, POINTER(USHORT*(sz//2)))
    return buf, filter(None, data.contents)

# }}}

def develop(do_eject=False):  # {{{
    from calibre.customize.ui import device_plugins
    usb_devices = scan_usb_devices()
    pprint(usb_devices)
    print()
    print('\nAll removable drives:')
    pprint(get_all_removable_drives(allow_fixed=False))
    print('\nRemovable drives:')
    rd = get_removable_drives(debug=True)
    pprint(rd)
    print()
    devplugins = list(sorted(device_plugins(), cmp=lambda
            x,y:cmp(x.__class__.__name__, y.__class__.__name__)))
    for dev in devplugins:
        dev.startup()
    for dev in devplugins:
        if dev.MANAGES_DEVICE_PRESENCE:
            continue
        connected, usbdev = dev.is_usb_connected_generic(usb_devices, debug=True)
        if connected:
            print('\n')
            print('Potentially connected device: %s at %s' % (dev.get_gui_name(), usbdev))
            print()
            print('Drives for this device:')
            pprint(get_drive_letters_for_device(usbdev, debug=True))
            print()
            print('Is device connected:', is_usb_device_connected(*usbdev[:2]))
            print()
            print('Device USB data:', get_usb_info(usbdev, debug=True))
    if do_eject:
        for drive in rd:
            eject_drive(drive)

def drives_for(vendor_id, product_id=None):
    usb_devices = scan_usb_devices()
    pprint(usb_devices)
    for usbdev in usb_devices:
        if usbdev.vendor_id == vendor_id and (product_id is None or usbdev.product_id == product_id):
            print('Drives for: {}'.format(usbdev))
            pprint(get_drive_letters_for_device(usbdev, debug=True))
            print('USB info:', get_usb_info(usbdev, debug=True))

if __name__ == '__main__':
    develop()
# }}}
