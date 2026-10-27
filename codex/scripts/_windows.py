"""Native Windows process primitives. No third-party packages or POSIX signals."""
import ctypes
from ctypes import wintypes as w
import contextlib

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")


def api(name, args, result=w.BOOL):
    function = getattr(k32, name)
    function.argtypes, function.restype = args, result
    return function


close = api("CloseHandle", [w.HANDLE])
open_process = api("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
exit_code = api("GetExitCodeProcess", [w.HANDLE, ctypes.POINTER(w.DWORD)])
times = api("GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4)
terminate_process = api("TerminateProcess", [w.HANDLE, w.UINT])
snapshot_handle = api("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)


class Entry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("exe", w.WCHAR * 260)]


first = api("Process32FirstW", [w.HANDLE, ctypes.POINTER(Entry)])
next_entry = api("Process32NextW", [w.HANDLE, ctypes.POINTER(Entry)])
suspend = ntdll.NtSuspendProcess
suspend.argtypes, suspend.restype = [w.HANDLE], w.LONG
resume = ntdll.NtResumeProcess
resume.argtypes, resume.restype = [w.HANDLE], w.LONG


@contextlib.contextmanager
def handle(pid, access=0x1000):
    value = open_process(access, False, pid)
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield value
    finally:
        close(value)


def identity(value):
    stamps = [w.FILETIME() for _ in range(4)]
    if not times(value, *[ctypes.byref(stamp) for stamp in stamps]):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)


def alive(pid, expected=None):
    try:
        with handle(pid) as value:
            if expected is not None and identity(value) != expected:
                return False
            code = w.DWORD()
            if not exit_code(value, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
    except OSError as exc:
        if exc.winerror == 87:  # invalid PID
            return False
        if exc.winerror == 5:  # access denied is not proof of exit
            return True
        raise


def snapshot():
    value = snapshot_handle(2, 0)
    if value == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        rows = {}
        more = first(value, ctypes.byref(entry))
        if not more and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            return None
        while more:
            if entry.pid:
                try:
                    with handle(entry.pid) as process:
                        code = w.DWORD()
                        if not exit_code(process, ctypes.byref(code)):
                            raise ctypes.WinError(ctypes.get_last_error())
                        rows[entry.pid] = (entry.parent, 0, "R" if code.value == 259 else "Z", identity(process))
                except OSError:
                    pass  # protected/exiting process; never infer ownership from its PID
            more = next_entry(value, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            return None
        return rows
    finally:
        close(value)


def signal_verified(pid, expected, stop=False):
    """Validate creation time on the very handle that is suspended/terminated."""
    try:
        with handle(pid, 0x1000 | 0x0800 | 0x0001) as value:
            if identity(value) != expected:
                return False
            if stop:
                if suspend(value) < 0:
                    raise OSError("cannot suspend verified process %s" % pid)
            elif not terminate_process(value, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 87 or not alive(pid):
            return False
        raise


def resume_verified(pid, expected):
    try:
        with handle(pid, 0x1000 | 0x0800) as value:
            if identity(value) == expected:
                resume(value)
    except OSError:
        pass


def command_line(pid):
    # ProcessCommandLineInformation is supported on Windows 8.1 and later.
    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", ctypes.c_void_p)]
    query = ntdll.NtQueryInformationProcess
    query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
    query.restype = w.LONG
    with handle(pid) as process:
        size = w.ULONG()
        query(process, 60, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError("cannot query process command line")
        buffer = ctypes.create_string_buffer(size.value)
        if query(process, 60, buffer, size, ctypes.byref(size)) < 0:
            raise OSError("cannot read process command line")
        text = ctypes.cast(buffer, ctypes.POINTER(UnicodeString)).contents
        return ctypes.wstring_at(text.buffer, text.length // 2)


def split_args(text):
    """Use Windows command-line parsing; keep native backslashes intact."""
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    split = shell.CommandLineToArgvW
    split.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(w.LPWSTR)
    local_free = api("LocalFree", [w.HLOCAL], w.HLOCAL)
    count = ctypes.c_int()
    # Prefix a dummy argv[0], whose Windows parsing has special rules.
    result = split('bridge.exe ' + text, ctypes.byref(count))
    if not result:
        raise ValueError("invalid Windows argument string")
    try:
        return [result[index] for index in range(1, count.value)]
    finally:
        local_free(ctypes.cast(result, w.HLOCAL))


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                ("scheduling", w.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


create_job = api("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE)
open_job = api("OpenJobObjectW", [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE)
set_job = api("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD])
assign_job = api("AssignProcessToJobObject", [w.HANDLE, w.HANDLE])
query_job = api("QueryInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p])
terminate_job = api("TerminateJobObject", [w.HANDLE, w.UINT])


def contain(process, name):
    """Assign a suspended child before any of its code can spawn descendants."""
    job = create_job(None, name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not set_job(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not assign_job(job, w.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        if resume(w.HANDLE(int(process._handle))) < 0:
            raise OSError("cannot resume contained Codex process")
        return job
    except BaseException:
        close(job)
        process.kill()
        raise


def job_members(name):
    if not name:
        return set()
    job = open_job(4, False, name)
    if not job:
        error = ctypes.get_last_error()
        if error == 2:
            return set()
        raise ctypes.WinError(error)
    try:
        capacity = 64
        while capacity <= 65536:
            buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
            if query_job(job, 3, buffer, len(buffer), None):
                count = w.DWORD.from_buffer(buffer, 4).value
                return set((ctypes.c_size_t * count).from_buffer(buffer, 8))
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            capacity *= 2
        raise OSError("Windows job process list exceeded limit")
    finally:
        close(job)


def kill_job(name):
    job = open_job(8, False, name)
    if not job:
        if ctypes.get_last_error() == 2:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not terminate_job(job, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(job)


def cancel_io(thread_id):
    """Release a thread blocked in synchronous anonymous-pipe ReadFile/WriteFile."""
    open_thread = api("OpenThread", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    cancel = api("CancelSynchronousIo", [w.HANDLE])
    thread = open_thread(1, False, thread_id)  # THREAD_TERMINATE is required by this API
    if thread:
        try:
            cancel(thread)  # ERROR_NOT_FOUND simply means no outstanding operation
        finally:
            close(thread)
