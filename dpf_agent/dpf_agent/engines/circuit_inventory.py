# -*- coding: utf-8 -*-
"""网络画像所需的原始观察量 —— 「读引擎」这一半收敛在这里。

## 为什么需要它

`domain/network_profile.build_network_profile` 原先接收一个 `run_with_engine`
回调，把**整段画像逻辑**（母线 / 线路 / 变压器 / 发电机 / PV / 调压器 / 电容器 /
单相元件 / 拓扑邻接表）塞进一个闭包 `_build(eng)` 里执行。

这对 COM 与 dss-python 都没问题 —— 它们都能执行 Python 闭包。但 **CLI 通道不能**：
`OpenDSScmd.exe` 是独立进程，只认命令流与 CSV 输出，执行不了任意 Python 函数。

所以按职责切开：

    * **本文负责收集**：从引擎读出画像所需的原始量，装进 `CircuitInventory`
    * **`build_network_profile` 负责组装**：纯数据推导，完全不知道引擎是谁

收集器**单点定义**（不按适配器分）：它只用 dss-python 与 COM 共有的 API
（`AllBusNames` / `SetActiveBus` / `ActiveBus`、集合 `First`/`Next`、
`ActiveCktElement`），两个通道跑的是同一份代码。差别只在**谁来调用它**——
COM 必须在专用 worker 线程内调用，dss-python 直接调用。

CLI 通道将来自己填一个 `CircuitInventory` 即可（从 CSV 与源文件扫描凑），
画像组装逻辑一行都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CircuitInventory:
    """网络画像所需的原始观察量（引擎无关，纯数据）。

    「原始」的意思是：这里只放**从引擎直接读到的值**，不放任何推导结果。
    电压等级集合、松弛母线数、环网秩等都由 `build_network_profile` 从这些值算出来，
    这样它们与引擎无关、可离线复算、也可被测试直接构造。
    """

    bus_count: int
    line_count: int
    transformer_count: int
    load_count: int
    generator_count: int
    pv_system_count: int
    storage_count: int
    capacitor_count: int
    regulator_count: int
    #: 每台母线：(母线名, kVBase 原值)。kVBase 可能是 None 或引擎专有类型。
    bus_kv_base: tuple[tuple[str, Any], ...]
    #: 逐个负荷：(额定 kW, 相数)
    loads: tuple[tuple[float, int], ...]
    #: 逐条线路：(是否启用, 端点1 母线名, 端点2 母线名)。母线名已去相别、小写。
    line_endpoints: tuple[tuple[bool, str, str], ...]


def _collection_count(ckt: Any, attr: str) -> int:
    """读某类元件集合的数量；引擎不暴露该集合时返回 0。

    不同 OpenDSS 版本暴露的集合不同（例如老版本没有 `PVSystems`），
    缺了就当作 0，不做假设、不抛异常。
    """
    try:
        coll = getattr(ckt, attr, None)
        return int(coll.Count) if coll is not None else 0
    except Exception:  # noqa: BLE001 - 引擎专有异常类型，一律按"没有"处理
        return 0


def collect_circuit_inventory(eng: Any) -> CircuitInventory:
    """从引擎读出网络画像所需的原始量。

    只用 dss-python 与 COM **共有**的 API，因此两个通道共用这一份实现。
    """
    ckt = eng.ActiveCircuit

    # 母线：名字 + 电压基准。kVBase 缺失/异常时记 None，由组装侧决定怎么处理。
    #
    # 存 **AllBusNames 的原始名**（而非 `ActiveBus.Name`）：组装侧要按母线名识别
    # slack 母线（source / sourcebus），输入必须与改造前逐字一致。
    bus_kv_base: list[tuple[str, Any]] = []
    for name in (ckt.AllBusNames or ()):
        ckt.SetActiveBus(name)
        try:
            kv = ckt.ActiveBus.kVBase
        except Exception:  # noqa: BLE001
            kv = None
        bus_kv_base.append((str(name), kv))

    # 负荷：额定 kW + 相数（用于单相/三相元件计数）
    loads: list[tuple[float, int]] = []
    loads_coll = ckt.Loads
    idx = loads_coll.First
    while idx > 0:
        try:
            kw = float(loads_coll.kW)
        except Exception:  # noqa: BLE001
            kw = 0.0
        # 注意：win32com 动态派发解析不了 `Loads.Phases`（实测 <unknown>.Phases），
        # 改经 `ActiveCktElement.NumPhases` 取相数（Loads 迭代会同步推进活动元件）。
        try:
            n_phases = int(ckt.ActiveCktElement.NumPhases)
        except Exception:  # noqa: BLE001
            n_phases = int(getattr(loads_coll, "Phases", 3) or 3)
        loads.append((kw, n_phases))
        idx = loads_coll.Next

    # 线路：启停 + 前两个端点（拓扑画像只需要端点对）
    line_endpoints: list[tuple[bool, str, str]] = []
    lines_coll = ckt.Lines
    idx = lines_coll.First
    while idx > 0:
        ce = ckt.ActiveCktElement
        try:
            enabled = bool(ce.Enabled)
        except Exception:  # noqa: BLE001
            enabled = True
        buses = [str(x).split(".")[0].lower() for x in (ce.BusNames or ())[:2]]
        line_endpoints.append((
            enabled,
            buses[0] if len(buses) > 0 else "",
            buses[1] if len(buses) > 1 else "",
        ))
        idx = lines_coll.Next

    return CircuitInventory(
        bus_count=int(ckt.NumBuses),
        line_count=int(ckt.Lines.Count),
        transformer_count=int(ckt.Transformers.Count),
        load_count=int(ckt.Loads.Count),
        generator_count=int(ckt.Generators.Count),
        pv_system_count=_collection_count(ckt, "PVSystems"),
        storage_count=_collection_count(ckt, "Storages"),
        capacitor_count=_collection_count(ckt, "Capacitors"),
        regulator_count=_collection_count(ckt, "RegControls"),
        bus_kv_base=tuple(bus_kv_base),
        loads=tuple(loads),
        line_endpoints=tuple(line_endpoints),
    )
