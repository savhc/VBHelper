"""常驻仪表盘（HUD）：CPU / 内存 / 磁盘 / 网络 / GPU 利用率与硬件温度。

数据分两路走，原因是两边的「贵贱」差得太远：

- CPU、内存、网络速率、磁盘空间由 psutil 直接给，一次读下来微秒级，
  主线程按刷新周期读就行；
- GPU 占用、磁盘繁忙度、硬件温度 Windows 上 psutil 拿不到，只能问 WMI /
  性能计数器。每次采样起一个 powershell 的话，进程启动 + WMI 首次查询要 1.5 秒
  以上，所以这里常驻一个 PowerShell 进程：它把 .NET 性能计数器对象建好之后一直
  复用，每次采样只要 ~100ms，结果按行打到标准输出，Python 侧只留最新一条。

温度按「谁能出数就用谁」的顺序合并：装了 LibreHardwareMonitor / OpenHardwareMonitor
就直接读它的传感器（CPU/GPU/硬盘最全），否则退到 ACPI 热区、电池、硬盘 SMART、
NVMe 可靠性计数器、nvidia-smi，几路都拿不到才提示没有温度源。

窗口行为：可拖动、可锁定、三态背景（正常 / 半透明 / 全透明）。
透明状态按用户要求只留透明按钮，其它按钮全部藏起来。
"""
import json
import os
import subprocess
import threading
import time
import tkinter as tk

try:
    import psutil
except Exception:            # psutil 缺失时降级为无监控，不阻断启动
    psutil = None

from . import core
from .core import *
from .ui import icons, render

# ------------------------- 窗口常量 -------------------------
HUD_NAME = "常驻仪表盘"
MIN_W = 292                 # 窗口最小宽度（实际按内容自适应）
BAR_W, BAR_H = 96, 9        # 进度条尺寸
PAD = 10
STYLE_NORMAL, STYLE_SOFT, STYLE_CLEAR = 0, 1, 2
ALPHA_SOFT = 0.80           # 半透明模式的不透明度
ICON_STYLE = {STYLE_NORMAL: "disc", STYLE_SOFT: "circle",
              STYLE_CLEAR: "minus-circle"}
STYLE_TIP = {STYLE_NORMAL: "正常背景", STYLE_SOFT: "半透明背景", STYLE_CLEAR: "全透明背景"}
# 全透明模式的颜色键。这里是关键：文字是抗锯齿（ClearType 子像素）渲染的，
# 边缘像素必然是「文字色 × 颜色键」的混色，键色选什么，字边上就糊一圈什么颜色。
# 早先直接沿用球窗口的洋红 #ff00fe，结果每个字都镶了粉边（用户看到的「文字变异」，
# 实测 33408 个像素里有 1241 个是饱和粉）；换成纯黑之后边缘只剩深浅变化，
# 深色桌面上基本看不出来，浅色背景上也就是一点点描边感。
CLEAR_KEY = "#000000"
# 不用透明时也得给 -transparentcolor 一个值：与其指望「设成空串能关掉」，
# 不如让它去键一个界面上绝对不存在的颜色，行为确定得多
NO_KEY = "#010203"

C_TRACK = "#2f333d"
C_OK = "#4cc38a"
C_WARN = "#e3b341"
C_HOT = "#f0616d"

GB = 1024.0 ** 3
MB = 1024.0 ** 2
CREATE_NO_WINDOW = 0x08000000

# 温度传感器名 -> 中文名，取第一个命中的；认不出来就保留原名
TEMP_ALIAS = (
    ("cpu package", "CPU"), ("cpu core", "CPU"), ("cpu cores", "CPU"),
    ("core ", "CPU"), ("cpu", "CPU"),
    ("gpu hot spot", "GPU热点"), ("gpu core", "GPU"), ("gpu", "GPU"),
    ("storage", "硬盘"), ("ssd", "硬盘"), ("hdd", "硬盘"), ("nvme", "硬盘"),
    ("hard disk", "硬盘"), ("drive", "硬盘"),
    ("motherboard", "主板"), ("mainboard", "主板"), ("acpi", "主板"),
    ("thermal zone", "主板"), ("system", "主板"),
    ("battery", "电池"),
    ("wifi", "无线网卡"), ("wlan", "无线网卡"), ("network", "网卡"),
    ("memory", "内存"), ("dimm", "内存"),
)
TEMP_RANK = {"CPU": 0, "GPU": 1, "GPU热点": 2, "硬盘": 3, "热区": 4, "主板": 5,
             "电池": 6, "无线网卡": 7, "网卡": 8, "内存": 9}
TEMP_MAX_ROWS = 6           # 温度最多显示几行，免得面板被传感器刷满
TEMP_HINT = "无温度源（可装 LibreHardwareMonitor）"

# ------------------------- 采集脚本（常驻 PowerShell） -------------------------
# __PERIOD__ / __PARENT__ 由 Python 侧替换。输出一行一个 JSON：
# {"gpu":12.3,"disk":4.5,"probed":1,"temps":"CPU@58.0|热区1@27.9"}
PS_SCRIPT = r'''
$Period = __PERIOD__
$Parent = __PARENT__
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ci = [Globalization.CultureInfo]::InvariantCulture
function F([double]$v) { [string]::Format($ci, '{0:0.#}', $v) }

# 硬盘温度要不要得上，全看这一条
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
function Emit([string]$s) { [Console]::Out.WriteLine($s); [Console]::Out.Flush() }
function Clean($s) { if ($null -eq $s) { return '?' }; return ([string]$s -replace '[|@]', '_') }
function EmitSample($gpu, $disk, $temps) {
    Emit ('{"gpu":' + (F $gpu) + ',"disk":' + (F $disk) +
          ',"probed":' + $(if ($probed) { '1' } else { '0' }) +
          ',"admin":' + $(if ($isAdmin) { '1' } else { '0' }) +
          ',"ndisk":' + $ndisk +
          ',"temps":"' + $temps + '"}')
}

$nv = ''
foreach ($p in @("$env:windir\System32\nvidia-smi.exe", "${env:ProgramFiles}\NVIDIA Corporation\NVSMI\nvidia-smi.exe")) {
    if (Test-Path $p) { $nv = $p; break }
}

# ---------------- 温度：Windows 上能读温度的几路全挂上，谁有数就显示谁 ----------------
# 优先硬件监控软件的 WMI（最全），其次 ACPI 热区（最通用，就是任务管理器里那条「温度」），
# 再往下是电池、SATA 硬盘 SMART、NVMe 磁盘可靠性计数器、N 卡 nvidia-smi
$tempNs = ''        # 硬件监控软件的命名空间，有它就不用别的了
$tzC = @{}          # ACPI 热区：性能计数器版本，比 WMI 那个类靠谱得多
$hasAcpi = $false   # root/WMI MSAcpi_ThermalZoneTemperature
$hasBat = $false    # root/WMI BatteryTemperature
$hasSmart = $false  # root/WMI MSStorageDriver_ATAPISmartData（SATA 盘温度）
$storTemps = ''     # Get-StorageReliabilityCounter（NVMe 盘温度，cmdlet 慢，30 秒才查一次）
$ndisk = 0          # 机器上物理盘总数：用来告诉用户「还有几块盘的温度没读到」
$probed = $false

function InitTemps {
    foreach ($ns in @('root/LibreHardwareMonitor', 'root/OpenHardwareMonitor')) {
        if (Get-CimInstance -Namespace $ns -ClassName Sensor) { $script:tempNs = $ns; $script:probed = $true; return }
    }
    try {
        # 热区实例名形如 \_TZ.TZ00，同一台机器上 WMI 那个类经常是空的，计数器却有
        foreach ($n in (New-Object Diagnostics.PerformanceCounterCategory('Thermal Zone Information')).GetInstanceNames()) {
            try { $script:tzC[$n] = New-Object Diagnostics.PerformanceCounter('Thermal Zone Information', 'High Precision Temperature', $n) } catch { }
        }
    } catch { }
    $script:hasAcpi = @(Get-CimInstance -Namespace 'root/WMI' -ClassName MSAcpi_ThermalZoneTemperature).Count -gt 0
    $script:hasBat = @(Get-CimInstance -Namespace 'root/WMI' -ClassName BatteryTemperature).Count -gt 0
    $script:hasSmart = @(Get-CimInstance -Namespace 'root/WMI' -ClassName MSStorageDriver_ATAPISmartData).Count -gt 0
    $script:probed = $true
}

function ProbeStorage {
    # Get-StorageReliabilityCounter 的 Temperature 只对管理员返回，普通用户查出来是空值
    # ——硬盘温度绕不开提权就是这个原因（SMART 本来就是特权操作）
    $out = @()
    $i = 0
    foreach ($d in @(Get-PhysicalDisk)) {
        $i++
        $c = $d | Get-StorageReliabilityCounter
        if (-not $c -or $c.Temperature -le 0 -or $c.Temperature -ge 120) { continue }
        $kind = ''
        if ($d.BusType -eq 'NVMe' -or $d.MediaType -eq 'SSD') { $kind = '·固态' }
        elseif ($d.MediaType -eq 'HDD') { $kind = '·机械' }
        $out += ('硬盘' + $i + $kind + '@' + (F $c.Temperature))
    }
    $script:storTemps = ($out -join '|')
    $script:ndisk = $i          # $i 只是数了几块盘，没温度也算数
}

function GetTemps {
    $parts = @()
    if ($tempNs) {
        $rows = @(Get-CimInstance -Namespace $tempNs -ClassName Sensor | Where-Object { $_.SensorType -eq 'Temperature' })
        foreach ($r in $rows) { $parts += (Clean $r.Name) + '@' + (F $r.Value) }
        return ($parts -join '|')
    }
    $tzFound = 0
    foreach ($n in @($tzC.Keys)) {          # ACPI 热区（十分之一开尔文）
        $v = -1.0
        try { $v = [double]$tzC[$n].NextValue() } catch { continue }
        if ($v -le 0) { continue }
        $label = '热区'
        if ($n -match '(\d+)\s*$') { $label = '热区' + ([int]$Matches[1] + 1) }
        $parts += ($label + '@' + (F (($v / 10.0) - 273.15)))
        $tzFound++
    }
    # WMI 那条只在计数器没出数时兜底：两个来源一起上会显示两遍同样的热区
    if ($tzFound -eq 0 -and $hasAcpi) {
        foreach ($r in @(Get-CimInstance -Namespace 'root/WMI' -ClassName MSAcpi_ThermalZoneTemperature)) {
            $parts += ('热区@' + (F (($r.CurrentTemperature / 10.0) - 273.15)))
        }
    }
    if ($hasBat) {
        foreach ($r in @(Get-CimInstance -Namespace 'root/WMI' -ClassName BatteryTemperature)) {
            $parts += ('电池@' + (F (($r.CurrentTemperature / 10.0) - 273.15)))
        }
    }
    # 硬盘同样只用一个来源：可靠性计数器带盘号和类型，优先；它没数才退到 SMART 属性
    if ($storTemps) {
        $parts += $storTemps.Split('|')
    } elseif ($hasSmart) {
        foreach ($d in @(Get-CimInstance -Namespace 'root/WMI' -ClassName MSStorageDriver_ATAPISmartData)) {
            $b = $d.VendorSpecific
            if (-not $b) { continue }
            for ($i = 2; $i -lt [Math]::Min($b.Length, 362); $i += 12) {
                $id = $b[$i]
                if ($id -ne 194 -and $id -ne 190) { continue }
                $t = [int]$b[$i + 5]
                if ($t -gt 0 -and $t -lt 100) { $parts += ('硬盘@' + $t) }
            }
        }
    }
    if ($nv) {                              # N 卡的 GPU 温度只能问它自己
        $t = (& $nv --query=temperature.gpu --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
        if ($t -match '^\s*\d+') { $parts += ('GPU@' + (F ([double]$t))) }
    }
    return ($parts -join '|')
}

$script:gpuC = @{}
$script:diskC = $null
$script:gpuOk = $true

function InitCounters {
    if ($script:gpuOk) {
        $script:gpuC = @{}
        foreach ($n in @((Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine).Name | Select-Object -Unique)) {
            try { $script:gpuC[$n] = New-Object Diagnostics.PerformanceCounter('GPU Engine', 'Utilization Percentage', $n) } catch { }
        }
        if ($script:gpuC.Count -eq 0) { $script:gpuOk = $false }
    }
    if (-not $script:diskC) {
        try { $script:diskC = New-Object Diagnostics.PerformanceCounter('PhysicalDisk', '% Disk Time', '_Total') } catch { $script:diskC = $null }
    }
    # 百分比计数器第一次读出来必然是 0，先读一遍把基线建起来（每次重建后都要）
    foreach ($k in @($script:gpuC.Keys)) { try { $script:gpuC[$k].NextValue() | Out-Null } catch { $script:gpuC.Remove($k) } }
    if ($script:diskC) { try { $script:diskC.NextValue() | Out-Null } catch { $script:diskC = $null } }
}

$nextInit = [DateTime]::MinValue
$nextSlow = [DateTime]::MinValue
while ($true) {
    # 父进程没了就自己退出，别留孤儿进程
    if (-not (Get-Process -Id $Parent)) { break }
    if ((Get-Date) -ge $nextInit) {
        InitCounters
        $nextInit = (Get-Date).AddSeconds(30)     # 30 秒重建一次：进程来去会新增/失效计数器实例
        Start-Sleep -Milliseconds 600
        continue
    }

    $gpu = -1.0
    if ($script:gpuC.Count -gt 0) {
        $sums = @{}
        foreach ($k in @($script:gpuC.Keys)) {
            $v = 0.0
            try { $v = [double]$script:gpuC[$k].NextValue() } catch { $script:gpuC.Remove($k); continue }
            if ($v -le 0) { continue }
            $t = 'other'
            if ($k -match '_engtype_(.+)$') { $t = $Matches[1] }
            $sums[$t] = [double]$sums[$t] + $v
        }
        $gpu = 0.0
        foreach ($v in $sums.Values) { if ($v -gt $gpu) { $gpu = $v } }   # 取最忙的那类引擎，跟任务管理器一个口径
        if ($gpu -gt 100) { $gpu = 100 }
    }

    $disk = -1.0
    if ($script:diskC) {
        try { $disk = [double]$script:diskC.NextValue() } catch { }
        if ($disk -gt 100) { $disk = 100 }
    }

    $temps = GetTemps
    EmitSample $gpu $disk $temps

    if (-not $probed) {
        # 温度源探测要一两秒，放在出数之后（不能挡第一屏数据），探完立刻补一条，
        # 否则界面会白挂一个周期的「未检测到温度传感器」
        InitTemps
        ProbeStorage
        $nextSlow = (Get-Date).AddSeconds(30)
        EmitSample $gpu $disk (GetTemps)
    } elseif ((Get-Date) -ge $nextSlow) {
        ProbeStorage
        $nextSlow = (Get-Date).AddSeconds(30)
    }
    Start-Sleep -Seconds $Period
}
'''

# ------------------------- 数据加工 -------------------------
def _rate(bps):
    """字节/秒 -> 紧凑文本，网络那一列宽度有限"""
    for unit, tag in ((GB, "G"), (MB, "M"), (1024.0, "K")):
        if bps >= unit:
            v = bps / unit
            return ("%.1f%s" % (v, tag)) if v < 10 else ("%.0f%s" % (v, tag))
    return "%.0fB" % bps

def _pct_color(p):
    if p >= 90:
        return C_HOT
    if p >= 70:
        return C_WARN
    return C_OK

def _temp_color(v):
    if v >= 85:
        return C_HOT
    if v >= 70:
        return C_WARN
    return C_OK

def _alias(name):
    """传感器名翻成短标签：中文名（采集脚本已经命名的）原样留着，英文名查表"""
    name = (name or "").strip() or "?"
    if any("\u4e00" <= ch <= "\u9fff" for ch in name):
        return name[:10]
    low = name.lower()
    for key, cn in TEMP_ALIAS:
        if key in low:
            return cn
    return name[:10]

def parse_temps(raw):
    """把采集脚本给的 "名字@值|名字@值" 拆开：同名传感器合并取最大值，按重要性排序"""
    group = {}
    for part in str(raw or "").split("|"):
        if "@" not in part:
            continue
        name, _, val = part.rpartition("@")
        try:
            v = float(val)
        except ValueError:
            continue
        if not (0.0 < v < 150.0):        # 0℃ / -273℃ 这类多半是坏值
            continue
        key = _alias(name)
        group[key] = max(group.get(key, -999.0), v)
    rows = sorted(group.items(), key=lambda kv: (_temp_rank(kv[0]), kv[0]))
    return rows[:TEMP_MAX_ROWS]

def _temp_rank(name):
    """"热区1"/"硬盘2" 这类带序号的名字按前缀归类排序"""
    for key in sorted(TEMP_RANK, key=len, reverse=True):
        if name.startswith(key):
            return TEMP_RANK[key]
    return 99

class Collector:
    """采样器：psutil 部分主线程随取随有，PowerShell 部分后台线程持续喂最新值"""

    def __init__(self):
        self._lock = threading.Lock()
        self._ps = {}
        self._error = ""
        self._stop = threading.Event()
        self._proc = None
        self._thread = None
        self._period = 2
        self._last_net = None
        self._link = None                # 网卡标称速率（bit/s），用于算「网络利用率」
        self._link_at = 0.0
        self._drive = (os.environ.get("SystemDrive") or "C:") + "\\"
        if psutil is not None:
            psutil.cpu_percent(None)     # 先空读一次，后面 cpu_percent(None) 才是「距上次的均值」

    # ---- 生命周期 ----
    def start(self, period):
        self._period = self._clamp(period)
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        else:
            self._kill()                 # 周期写死在脚本里，改了就把采集进程重启一遍

    def stop(self):
        self._stop.set()
        self._kill()

    def set_period(self, period):
        period = self._clamp(period)
        if period == self._period:
            return
        self._period = period
        self._kill()

    def _clamp(self, period):
        try:
            return max(1, min(60, int(period)))
        except (TypeError, ValueError):
            return 2

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _loop(self):
        """采集进程掉了就重拉，连续三次拉不起来就放弃（别在这儿死循环刷进程）"""
        fails = 0
        while not self._stop.is_set():
            proc = self._spawn()
            if proc is None:
                fails += 1
                if fails >= 3:
                    self._fail("采集进程启动失败")
                    return
                self._stop.wait(5)
                continue
            self._proc = proc
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    with self._lock:
                        self._ps = data
                    self._error = ""
                    fails = 0
            except Exception:
                pass
            finally:
                self._proc = None
                try:
                    proc.kill()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            fails += 1                 # 进程自己退了：等一下再拉，避免疯狂重启
            if fails >= 3:
                self._fail("采集进程反复退出")
                return
            self._stop.wait(5)

    def _fail(self, msg):
        with self._lock:
            self._error = msg

    def _spawn(self):
        script = PS_SCRIPT.replace("__PERIOD__", str(self._period))
        script = script.replace("__PARENT__", str(os.getpid()))
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                startupinfo=si, text=True, encoding="utf-8", errors="replace")
        except Exception:
            return None

    # ---- 取数 ----
    def _link_speed(self, now):
        if self._link is None or now - self._link_at > 120:
            best = 0
            try:
                for name, st in psutil.net_if_stats().items():
                    if st.isup and st.speed > 0 and "loopback" not in name.lower():
                        best = max(best, st.speed)
            except Exception:
                pass
            self._link = best * 1000000.0      # Mbps -> bit/s，0 表示问不出来
            self._link_at = now
        return self._link

    def snapshot(self):
        """UI 线程按刷新周期调一次；psutil 那几项开销可忽略，不会卡界面"""
        now = time.time()
        out = {"cpu": -1.0, "mem": -1.0, "mem_used": 0.0, "mem_total": 0.0,
               "disk": -1.0, "disk_pct": -1.0, "net": -1.0, "down": 0.0, "up": 0.0,
               "gpu": -1.0, "temps": [], "probed": False, "ready": False,
               "admin": False, "ndisk": 0, "error": ""}
        if psutil is not None:
            try:
                out["cpu"] = psutil.cpu_percent(None)
                vm = psutil.virtual_memory()
                out["mem"], out["mem_used"], out["mem_total"] = vm.percent, vm.used / GB, vm.total / GB
                out["disk_pct"] = psutil.disk_usage(self._drive).percent
                net = psutil.net_io_counters()
                if self._last_net is not None:
                    dt = now - self._last_net[0]
                    if dt > 0.05:
                        down = max(0.0, (net.bytes_recv - self._last_net[1]) / dt)
                        up = max(0.0, (net.bytes_sent - self._last_net[2]) / dt)
                        out["down"], out["up"] = down, up
                        speed = self._link_speed(now) or (100.0 * 1000000.0)
                        out["net"] = min(100.0, (down + up) * 8.0 / speed * 100.0)
                self._last_net = (now, net.bytes_recv, net.bytes_sent)
            except Exception:
                pass
        with self._lock:
            ps, err = dict(self._ps), self._error
        out["error"] = err
        if ps:
            out["ready"] = True
            out["gpu"] = float(ps.get("gpu", -1.0))
            out["disk"] = float(ps.get("disk", -1.0))
            out["probed"] = str(ps.get("probed", "1")) == "1"
            out["admin"] = str(ps.get("admin", "0")) == "1"
            out["ndisk"] = int(ps.get("ndisk", 0) or 0)
            out["temps"] = parse_temps(ps.get("temps"))
        return out

# ------------------------- 一行指标 -------------------------
class _Row:
    """指标行：名称 + 进度条 + 数值 + 说明。全透明模式下进度条收起来，只留文字"""

    def __init__(self, parent, name, sample_note="", width=4):
        self.frame = tk.Frame(parent, bg=C_BG)
        self.frame.pack(fill="x", padx=PAD, pady=1)
        self.name = tk.Label(self.frame, text=name, bg=C_BG, fg=C_MUTED,
                             font=(core.FONT, 9), width=width, anchor="w")
        self.name.pack(side="left")
        self.canvas = tk.Canvas(self.frame, width=BAR_W, height=BAR_H, bg=C_BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side="left", padx=(2, 6))
        self.canvas.create_rectangle(0, 0, BAR_W, BAR_H, fill=C_TRACK, outline="")
        self.fill = self.canvas.create_rectangle(0, 0, 0, BAR_H, fill=C_OK, outline="")
        self.val = tk.Label(self.frame, text="—", bg=C_BG, fg=C_TEXT,
                            font=(core.FONT, 9, "bold"), width=4, anchor="e")
        self.val.pack(side="left")
        # 说明列先按最宽的样例占位，这样窗口宽度一次定好之后不会再抖
        self.note = tk.Label(self.frame, text=sample_note or "　", bg=C_BG, fg=C_MUTED,
                             font=(core.FONT, 8), anchor="w")
        self.note.pack(side="left", padx=(6, 0))
        self._bar_on = True

    def set(self, pct, note="", unknown="—"):
        if pct is None or pct < 0:
            self.val.configure(text=unknown, fg=C_MUTED)
            self.canvas.itemconfigure(self.fill, fill=C_TRACK, state="hidden")
            self.note.configure(text=note, fg=C_MUTED)
            return
        pct = max(0.0, min(100.0, float(pct)))
        self.val.configure(text="%d%%" % round(pct), fg=_pct_color(pct))
        self.canvas.itemconfigure(self.fill, state="normal", fill=_pct_color(pct))
        self.canvas.coords(self.fill, 0, 0, BAR_W * pct / 100.0, BAR_H)
        self.note.configure(text=note)

    def paint(self, bg, magic):
        """跟着当前背景模式换肤；magic=True 表示全透明，进度条要收起来"""
        for w in (self.frame, self.name, self.val, self.note):
            w.configure(bg=bg)
        if magic:
            if self._bar_on:
                self.canvas.pack_forget()
                self._bar_on = False
        else:
            if not self._bar_on:
                self.canvas.pack(side="left", padx=(2, 6), before=self.val)
                self._bar_on = True
            self.canvas.configure(bg=bg)

# ------------------------- 仪表盘窗口 -------------------------
class HudWindow:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        hud_cfg = dict(app.cfg.get("hud") or {})
        self.style = int(hud_cfg.get("style") or 0) % 3
        self.locked = bool(hud_cfg.get("locked"))
        self.temp_hint = None
        self._closed = False
        self._job = None
        self._tip_job = None
        self._drag = None
        self._moved = False
        self._temp_names = None
        self._temp_rows = []
        self._w_fixed = None      # 首次排版定下来的窗口宽度，之后不再改
        self._pos = None          # 最近一次自己设过的坐标（Tk 的 winfo_x/y 是滞后的）
        self.collector = Collector()

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.configure(bg=C_BORDER)
        try:
            self.win.wm_attributes("-topmost", True)
        except Exception:
            pass

        self.outer = tk.Frame(self.win, bg=C_BG)
        self.outer.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_head()
        self.body = tk.Frame(self.outer, bg=C_BG)
        self.body.pack(fill="x")

        self.row_cpu = _Row(self.body, "CPU", "100°C")
        self.row_mem = _Row(self.body, "内存", "999/999G")
        self.row_disk = _Row(self.body, "磁盘", "C: 100%")
        self.row_net = _Row(self.body, "网络", "下行 999M  上行 999M")
        self.row_gpu = _Row(self.body, "GPU", "100°C")

        self.sep = tk.Frame(self.outer, bg=C_BORDER, height=1)
        self.sep.pack(fill="x", padx=PAD, pady=(6, 0))
        self.temp_head = tk.Label(self.outer, text="温度", bg=C_BG, fg=C_MUTED,
                                  font=(core.FONT, 8, "bold"), anchor="w")
        self.temp_head.pack(fill="x", padx=PAD, pady=(4, 0))
        self.temp_box = tk.Frame(self.outer, bg=C_BG)
        self.temp_box.pack(fill="x", pady=(0, 4))
        self.temp_empty = None
        self._show_no_temp()

        self.rows = (self.row_cpu, self.row_mem, self.row_disk, self.row_net, self.row_gpu)
        for w in (self.head, self.title, self.sep, self.temp_head):
            self._bind_drag(w)
        for r in self.rows:
            for w in (r.frame, r.name, r.val, r.note, r.canvas):
                self._bind_drag(w)

        self._paint()
        self._place()
        self.collector.start(period(self.app))
        self._tick()

    # ---- 外观 ----
    def _build_head(self):
        self.head = tk.Frame(self.outer, bg=C_BG)
        self.head.pack(fill="x")
        self.title = tk.Label(self.head, text=HUD_NAME, bg=C_BG, fg=C_MUTED,
                              font=(core.FONT, 8, "bold"), anchor="w")
        self.title.pack(side="left", padx=(PAD, 0), pady=(4, 2))
        self.btn_style = self._icon(self.head, ICON_STYLE[self.style], self._cycle_style)
        self.btn_style.pack(side="right", padx=(0, 5))
        self.btn_lock = self._icon(self.head, self._lock_icon(), self._toggle_lock)
        self.btn_lock.pack(side="right", padx=2)
        self.btn_close = self._icon(self.head, "x", lambda: close_hud(self.app))
        self.btn_close.pack(side="right", padx=2)

    def _icon(self, parent, icon, cmd):
        lbl = tk.Label(parent, bg=C_BG, padx=4, pady=1, cursor="hand2")
        self._set_icon(lbl, icon, C_MUTED)
        self._bind_drag(lbl)
        # 必须先绑拖动再补点击，而且要用 add：同一个 <Button-1> 重复 bind 是覆盖，
        # 早先直接把点击绑在拖动后面，结果三个按钮全哑了（只剩拖动生效）
        lbl.bind("<Button-1>", lambda e: cmd(), add="+")
        lbl.bind("<Enter>", lambda e: (
            lbl.configure(bg=C_HOVER),
            self._set_icon(lbl, lbl._icon_name, C_TEXT)))
        lbl.bind("<Leave>", lambda e: (
            lbl.configure(bg=self._bg()),
            self._set_icon(lbl, lbl._icon_name, self._icon_fg(lbl))))
        return lbl

    def _set_icon(self, lbl, icon, color=None):
        """按钮图标用 Feather 图片，换档/锁定/悬停时按新颜色重画。"""
        color = color or self._icon_fg(lbl)
        lbl._icon_name = icon
        ph = render.photo(icons.draw(icon, 16, color),
                          key="hud:ico:%s:16:%s" % (icon, color))
        lbl.configure(image=ph)
        lbl._icon_photo = ph

    def _icon_fg(self, lbl):
        if lbl is self.btn_lock and self.locked:
            return C_ACCENT_H
        if lbl is self.btn_style and self.style != STYLE_NORMAL:
            return C_ACCENT_H
        return C_MUTED

    def _bg(self):
        return CLEAR_KEY if self.style == STYLE_CLEAR else C_BG

    def _lock_icon(self):
        return "unlock" if self.locked else "lock"

    def _paint(self):
        """按当前模式刷一遍底色；全透明模式下每一层的底色都得是颜色键，不然会露出黑框"""
        bg = self._bg()
        magic = self.style == STYLE_CLEAR
        try:
            self.win.configure(bg=CLEAR_KEY if magic else C_BORDER)
            for w in (self.outer, self.body, self.head, self.title,
                      self.temp_head, self.temp_box):
                w.configure(bg=bg)
            self.sep.configure(bg=CLEAR_KEY if magic else C_BORDER)
            for b in (self.btn_close, self.btn_lock, self.btn_style):
                b.configure(bg=bg)
                self._set_icon(b, b._icon_name, self._icon_fg(b))
            for r in self.rows:
                r.paint(bg, magic)
            for f in self.temp_box.winfo_children():      # 温度行是动态生成的，整体刷一遍
                f.configure(bg=bg)
                for c in f.winfo_children():
                    c.configure(bg=bg)
            if self.temp_hint is not None and self.temp_hint.winfo_exists():
                self.temp_hint.configure(fg=C_ACCENT_H if self.style == STYLE_NORMAL else C_MUTED)
        except tk.TclError:
            pass
        try:
            self.win.attributes("-alpha", ALPHA_SOFT if self.style == STYLE_SOFT else 1.0)
            self.win.attributes("-transparentcolor", CLEAR_KEY if magic else NO_KEY)
        except Exception:
            pass
        # 透明模式按用户要求只留透明按钮：关闭、锁定都收起来
        if self.style == STYLE_NORMAL:
            for b in (self.btn_close, self.btn_lock):
                if not b.winfo_ismapped():
                    b.pack(side="right", padx=2)
            self.btn_style.pack_configure(padx=(0, 5))
        else:
            self.btn_close.pack_forget()
            self.btn_lock.pack_forget()
            self.btn_style.pack_configure(padx=(0, 6))

    # ---- 布局 ----
    def _fit(self, keep_xy=True, grow=False):
        """窗口尺寸跟着内容走，但宽度算好之后就钉死（只允许在明确场合变大）。

        数值文本每帧都在变长短（"下行 2.5K" 和 "下行 999M" 差好几十像素），每帧重排窗口
        会一直抽动，所以建窗时用最宽的样例定一次宽度；高度则跟着温度行数走。
        grow=True 只在切显示档位时用：全透明档没有进度条，量出来的宽度偏小，
        切回正常档必须放它长回去，否则进度条会被裁掉。
        """
        try:
            self.win.update_idletasks()
            req = self.win.winfo_reqwidth()
            if self._w_fixed is None or grow:
                self._w_fixed = max(self._w_fixed or 0, MIN_W, req)
            w = self._w_fixed
            h = max(40, self.win.winfo_reqheight())
            L, T, R, B = self.app.work
            if keep_xy:
                x, y = self.win.winfo_x(), self.win.winfo_y()
            else:
                x, y = -1, -1
            if x < 0 or y < 0:
                pos = (self.cfg.get("hud") or {}).get("pos")
                if isinstance(pos, (list, tuple)) and len(pos) == 2:
                    x, y = int(pos[0]), int(pos[1])
                else:
                    x, y = R - w - 24, T + 24
            x = max(L, min(x, R - w))
            y = max(T, min(y, B - h))
            self._pos = (x, y)
            self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass

    def _place(self):
        self._fit(keep_xy=False)

    def _save_pos(self):
        if self._pos is None:
            return
        self._save({"pos": [self._pos[0], self._pos[1]]})

    def _save(self, patch):
        try:
            hud_cfg = dict(self.cfg.get("hud") or {})
            hud_cfg.update(patch)
            self.cfg["hud"] = hud_cfg
            save_cfg(self.cfg)
        except Exception:
            pass

    # ---- 拖动 ----
    def _bind_drag(self, w):
        w.bind("<Button-1>", self._press)
        w.bind("<B1-Motion>", self._motion)
        w.bind("<ButtonRelease-1>", self._release)

    def _press(self, e):
        if self.locked:
            return
        try:
            self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
            self._moved = False
        except tk.TclError:
            self._drag = None

    def _motion(self, e):
        if self.locked or self._drag is None:
            return
        try:
            x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
            if abs(x - self.win.winfo_x()) > 2 or abs(y - self.win.winfo_y()) > 2:
                self._moved = True
            if self._moved:
                self._pos = (x, y)
                self.win.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def _release(self, e):
        self._drag = None
        if not self._moved or self._pos is None:
            return
        self._moved = False
        try:                                  # 拖动越界时拉回工作区，避免窗口不可见
            L, T, R, B = self.app.work
            w, h = self.win.winfo_width(), self.win.winfo_height()
            x = max(L, min(self._pos[0], R - w))
            y = max(T, min(self._pos[1], B - h))
            self._pos = (x, y)
            self.win.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            return
        self._save_pos()

    # ---- 按钮 ----
    def _toggle_lock(self):
        self.locked = not self.locked
        self._set_icon(self.btn_lock, self._lock_icon(), self._icon_fg(self.btn_lock))
        self._save({"locked": self.locked})

    def _cycle_style(self):
        self.style = (self.style + 1) % 3
        self._set_icon(self.btn_style, ICON_STYLE[self.style])
        self._paint()
        self._fit(grow=True)
        self._save({"style": self.style})
        # 切换后标题临时显示档位名：仅凭数值无法判断当前档位
        try:
            self.title.configure(text=STYLE_TIP[self.style])
            if self._tip_job:
                self.app.root.after_cancel(self._tip_job)
            self._tip_job = self.app.root.after(1500, self._restore_title)
        except Exception:
            pass

    def _restore_title(self):
        self._tip_job = None
        try:
            self.title.configure(text=HUD_NAME)
        except tk.TclError:
            pass

    # ---- 刷新 ----
    def set_period(self, seconds):
        self.collector.set_period(seconds)

    def _tick(self):
        if self._closed:
            return
        try:
            snap = self.collector.snapshot()
        except Exception:
            snap = None
        if snap:
            self._render(snap)
        try:
            self._job = self.app.root.after(int(period(self.app) * 1000), self._tick)
        except tk.TclError:
            self._job = None

    def _render(self, s):
        temps = dict(s["temps"])
        # 磁盘这一行：进度条给「读写繁忙度」（拿不到才退回空间占用），文字旁注系统盘空间
        disk = s["disk"] if s["disk"] >= 0 else s["disk_pct"]
        disk_note = "C: %d%%" % round(s["disk_pct"]) if s["disk_pct"] >= 0 else ""
        try:
            self.row_cpu.set(s["cpu"], self._temp_note("CPU", temps))
            self.row_mem.set(s["mem"], "%s/%s" % (
                ("%.1f" % s["mem_used"]) if s["mem_used"] < 100 else "%.0f" % s["mem_used"],
                "%.0fG" % s["mem_total"]))
            self.row_disk.set(disk, disk_note)
            net_note = "下行 %s  上行 %s" % (_rate(s["down"]), _rate(s["up"])) if s["ready"] else ""
            self.row_net.set(s["net"], net_note)
            gpu_note = self._temp_note("GPU", temps)
            if not gpu_note and not s["ready"]:
                gpu_note = "启动中…"
            self.row_gpu.set(s["gpu"], gpu_note)
        except tk.TclError:
            return
        self._render_temps(s["temps"], s["probed"], s["admin"], s["ndisk"])

    def _temp_note(self, key, temps):
        v = temps.get(key)
        return "%d°C" % round(v) if v is not None else ""

    def _show_no_temp(self, text=None):
        """没有温度数据时的兜底提示（内容一变就重建，所以这里每次都新建）"""
        text = text or TEMP_HINT
        if self.temp_empty is not None and self.temp_empty.cget("text") == text:
            return
        for w in self.temp_box.winfo_children():
            w.destroy()
        self.temp_hint = None
        self.temp_empty = tk.Label(self.temp_box, text=text, bg=self._bg(),
                                   fg=C_MUTED, font=(core.FONT, 8), anchor="w")
        self.temp_empty.pack(fill="x", padx=PAD)
        self._bind_drag(self.temp_empty)

    def _render_temps(self, temps, probed=True, admin=False, ndisk=0):
        names = [n for n, _ in temps]
        if not names and not probed:
            self._temp_names = None
            self._show_no_temp("温度读取中…")
            return
        if names != self._temp_names:
            self._temp_names = names
            for w in self.temp_box.winfo_children():
                w.destroy()
            self._temp_rows = []
            self.temp_hint = None
            if names:
                for n in names:
                    f = tk.Frame(self.temp_box, bg=self._bg())
                    f.pack(fill="x", padx=PAD)
                    lab = tk.Label(f, text=n, bg=self._bg(), fg=C_MUTED,
                                   font=(core.FONT, 9), width=8, anchor="w")
                    lab.pack(side="left")
                    val = tk.Label(f, text="—", bg=self._bg(), fg=C_TEXT,
                                   font=(core.FONT, 9, "bold"), width=5, anchor="e")
                    val.pack(side="left")
                    self._bind_drag(f)
                    self._bind_drag(lab)
                    self._bind_drag(val)
                    self._temp_rows.append((lab, val))
            else:
                self._show_no_temp()
            self._fit()
        for (lab, val), (_n, v) in zip(self._temp_rows, temps):
            try:
                val.configure(text="%d°C" % round(v), fg=_temp_color(v))
            except tk.TclError:
                return
        self._render_disk_hint(names, admin, ndisk)

    def _render_disk_hint(self, names, admin, ndisk):
        """缺几块盘的温度就直说：普通权限下 NVMe 那块是读不到的，含糊过去等于骗人。

        这里只做文字提醒，不提供提权入口；要看全部温度需用户自行以管理员身份启动。
        """
        got = sum(1 for n in names if n.startswith("硬盘"))
        missing = ndisk - got if not admin else 0
        text = ("还有 %d 块盘温度需以管理员身份运行才能显示" % missing
                if missing > 0 else "")
        if not text:
            if self.temp_hint is not None:
                try:
                    self.temp_hint.destroy()
                except tk.TclError:
                    pass
                self.temp_hint = None
                self._fit()
            return
        if self.temp_hint is not None and self.temp_hint.winfo_exists():
            if self.temp_hint.cget("text") != text:
                self.temp_hint.configure(text=text)
            return
        lab = tk.Label(self.temp_box, text=text, bg=self._bg(), fg=C_MUTED,
                       font=(core.FONT, 8), anchor="w")
        lab.pack(fill="x", padx=PAD)
        self._bind_drag(lab)
        self.temp_hint = lab
        self._fit()

    # ---- 收尾 ----
    def close(self):
        if self._closed:
            return
        self._closed = True
        for job_attr in ("_job", "_tip_job"):
            job = getattr(self, job_attr)
            if job:
                try:
                    self.app.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_attr, None)
        self._save_pos()
        self._save({"style": self.style, "locked": self.locked})
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.collector.stop()

# ------------------------- 对外接口 -------------------------
def period(app):
    """刷新周期（秒），设置窗口里改，默认 2 秒"""
    try:
        return max(1, min(60, int(app.set.get("hud_period", 2))))
    except (TypeError, ValueError):
        return 2

def is_open(app):
    w = getattr(app, "_hud_win", None)
    if w is None:
        return False
    try:
        return bool(w.win.winfo_exists()) and not w._closed
    except tk.TclError:
        return False

def open_hud(app):
    """菜单里点「启用」：开着就拉到前面，没开就新建一个"""
    if is_open(app):
        w = app._hud_win
        try:
            w.win.deiconify()
            w.win.lift()
        except tk.TclError:
            pass
        return w
    win = HudWindow(app)
    app._hud_win = win
    cfg = app.cfg
    hud_cfg = dict(cfg.get("hud") or {})
    hud_cfg["enabled"] = True
    cfg["hud"] = hud_cfg
    save_cfg(cfg)
    return win

def close_hud(app):
    """菜单 / 关闭按钮：关掉并记住「不要了」，下次启动就不再自己冒出来"""
    w = getattr(app, "_hud_win", None)
    if w is not None:
        w.close()
    app._hud_win = None
    cfg = app.cfg
    hud_cfg = dict(cfg.get("hud") or {})
    hud_cfg["enabled"] = False
    cfg["hud"] = hud_cfg
    save_cfg(cfg)

def toggle(app):
    if is_open(app):
        close_hud(app)
    else:
        open_hud(app)

def shutdown(app):
    """程序退出时收尾：停掉采集进程（不动 enabled，下次启动照样常驻）"""
    w = getattr(app, "_hud_win", None)
    if w is not None:
        w.close()
    app._hud_win = None

# ------------------------- 设置窗口里的一行 -------------------------
def period_row(app, parent):
    """设置窗口里的「仪表盘刷新周期」：改完立刻生效，不用重开仪表盘"""
    tk.Frame(parent, bg=C_BORDER, height=1).pack(fill="x", padx=18, pady=(6, 0))
    row = tk.Frame(parent, bg=C_BG)
    row.pack(fill="x", padx=18, pady=(10, 0))
    tk.Label(row, text="常驻仪表盘刷新周期（秒）", bg=C_BG, fg=C_TEXT,
             font=(core.FONT, 9)).pack(side="left")
    var = tk.StringVar(value=str(period(app)))

    def apply(*_e):
        try:
            v = max(1, min(60, int(float(var.get()))))
        except (TypeError, ValueError):
            var.set(str(period(app)))
            return
        var.set(str(v))
        app.set["hud_period"] = v
        app.cfg["settings"] = app.set
        save_cfg(app.cfg)
        w = getattr(app, "_hud_win", None)
        if w is not None and not w._closed:
            w.set_period(v)

    spin = tk.Spinbox(row, from_=1, to=60, width=4, textvariable=var, command=apply,
                      bg=C_PANEL, fg=C_TEXT, buttonbackground=C_PANEL, bd=0,
                      relief="flat", highlightthickness=0, justify="center",
                      insertbackground=C_TEXT, font=(core.FONT, 9), cursor="hand2")
    spin.pack(side="left", padx=8, ipady=2)
    spin.bind("<Return>", apply)
    spin.bind("<FocusOut>", apply)
    # 边打字边生效会一路触发重启采集进程（敲 "12" 就先按 1 跑一趟），所以停顿一下再应用：
    # 用户输完直接点关闭按钮时不会经过回车，这条是兜底
    spin.bind("<KeyRelease>", lambda e: _later(row, lambda: apply(), 700))
    tk.Label(parent, text="数值越小刷新越快，采集开销也越大（1~60 秒，默认 2 秒）",
             bg=C_BG, fg=C_MUTED, font=(core.FONT, 8), justify="left",
             wraplength=320).pack(anchor="w", padx=44, pady=(2, 0))
    return spin

def _later(widget, fn, ms):
    job = getattr(widget, "_vb_later", None)
    if job:
        try:
            widget.after_cancel(job)
        except Exception:
            pass
    try:
        widget._vb_later = widget.after(ms, fn)
    except tk.TclError:
        pass
