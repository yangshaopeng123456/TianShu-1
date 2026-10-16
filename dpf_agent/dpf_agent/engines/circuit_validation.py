# -*- coding: utf-8 -*-
"""Compile 之后的电路校验规则 —— **单点定义，各通道共用**。

## 为什么单独抽出来

同一套校验规则曾在两个适配器里**各写了一遍**：

* `opendss_adapter.py`（dss-python 通道）—— 用 `ckt.CktElements` / `ckt.Buses`
* `opendss_com_adapter.py`（COM 通道）—— 用 `AllElementNames` + `SetActiveElement`

两段**语义等价、API 形态不同**。规则写两遍 = 早晚漂移，而这两条通道的判定
必须一致：**同一个算例不能在 COM 下拒绝、在 dss-python 下放行**。
本项目已经吃过同类亏（元件求和口径写成两份，导致修基类对生产路径零效果），
所以这里按同样的原则处理：**把"怎么取数据"和"取到数据怎么判定"分开。**

    * 各适配器负责**收集** `CircuitObservation`（引擎相关，API 各不相同）
    * 本模块负责**判定**（引擎无关，纯数据）

将来的 CLI 通道只需从 CSV / 源文件收集这几个观察量，即可复用同一套判定。

## 为什么不能只看 compile 的错误码

OpenDSS 遇到**未知母线名**不会报错 —— 它会自动给这个母线创建一个
**零基准母线**（kVBase=0），然后照常求解，产物看起来"正常"但基准是错的。
所以必须显式检查"元件引用了不存在的母线"和"被引用但缺少电压基准的母线"，
把这类 bad_reference 结果挡在 Solve 之前。

（Vsource 所在母线豁免：它自带 basekv，母线的 kVBase 没回填也具备电压基准来源。）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 错误描述里的采样上限：只列前 N 条，避免把上千条塞进异常消息与日志。
_SAMPLE_LIMIT = 20


def normalize_bus_name(raw: object) -> str:
    """母线名归一化：去相别、去首尾空白、转小写。

    `Bus1.1.2` → `bus1`；`  F1_001  ` → `f1_001`。

    相别在校验里没有意义 —— 校验问的是"这个母线存在吗"，
    而 `F1_001.1` 与 `F1_001` 指向同一个母线对象。
    """
    return str(raw or "").split(".", 1)[0].strip().lower()


@dataclass(frozen=True)
class CircuitObservation:
    """compile 之后从引擎取到的、**与引擎无关**的观察量。

    各适配器按自己的 API 填这个结构；判定逻辑完全不关心引擎是谁。
    """

    #: `Circuit.NumBuses` / `len(Circuit.Buses)`
    bus_count: int
    #: 全部母线名（已归一化）
    bus_names: frozenset[str]
    #: 逐个元件端点：(元件名, 端点母线名原样)。已跳过未启用的元件。
    endpoints: tuple[tuple[str, str], ...]
    #: 每台母线的电压基准：(母线名, kVBase 原值)
    bus_base_kv: tuple[tuple[str, Any], ...]
    #: Vsource 所在母线（已归一化）—— 零基准检查里豁免
    vsource_buses: frozenset[str]


def validate_compiled_circuit(obs: CircuitObservation) -> str:
    """判定 compile 结果是否可用于后续求解。返回错误描述，**空串表示通过**。

    三条规则，按优先级依次检查（先命中先返回）：

    1. **电路无母线** → 算例无效（compile 没真正建立电路）
    2. **元件引用了不存在的母线** → OpenDSS 会静默建零基准母线，必须显式拦
    3. **被元件使用但 kVBase=0 的母线**（Vsource 所在母线豁免）

    规则必须**与通道无关**：同样的算例观察量，在任何通道下都得到同样的判定。
    """
    if obs.bus_count == 0:
        return "Compile 后 Circuit 无母线，算例无效"

    # 规则 2：未解析的母线引用
    suspicious: list[str] = []
    for name, bus in obs.endpoints:
        base = normalize_bus_name(bus)
        if base and base not in obs.bus_names:
            suspicious.append(f"{name}->{base}")
            if len(suspicious) >= _SAMPLE_LIMIT:
                break
    if suspicious:
        return "发现未解析的母线引用：" + ", ".join(suspicious[:10])

    # 规则 3：缺少电压基准的母线（先把豁免的 Vsource 母线扣掉）
    zero_base: set[str] = set()
    for name, kv in obs.bus_base_kv:
        try:
            if not float(kv):
                zero_base.add(normalize_bus_name(name))
        except Exception:  # noqa: BLE001 - 引擎可能给 None / 异常类型
            continue
    zero_base -= obs.vsource_buses
    if not zero_base:
        return ""

    refs: list[str] = []
    for name, bus in obs.endpoints:
        base = normalize_bus_name(bus)
        if base in zero_base:
            refs.append(f"{name}->{base}")
            if len(refs) >= _SAMPLE_LIMIT:
                break
    if not refs:
        # 有零基准母线，但没有元件在用 —— 无害，放行。
        return ""
    bad = sorted({r.split("->", 1)[1] for r in refs})
    return (
        "发现被元件使用但缺少电压基准的母线：" + ", ".join(bad[:10])
        + f"（引用示例：{', '.join(refs[:5])}）"
    )
