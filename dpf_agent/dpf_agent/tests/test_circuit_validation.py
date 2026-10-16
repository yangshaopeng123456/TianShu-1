"""Compile 后的电路校验规则：必须**单点定义**，各通道共用一份。

## 背景（为什么要专门锁这件事）

同一套规则（母线数 / 未解析的母线引用 / 零基准母线）曾在两个适配器里
**各写了一遍**：

* `opendss_adapter.py`（dss-python）—— 用 `ckt.CktElements` / `ckt.Buses`
* `opendss_com_adapter.py`（COM）—— 用 `AllElementNames` + `SetActiveElement`

两段**语义等价、API 形态不同**。危险在于：改一份不影响另一份，两条通道的判定
会慢慢分叉 —— 同一个算例可能"在 COM 下拒绝、在 dss-python 下放行"。
本项目已经吃过同类亏：COM 侧私藏了一份逐行等价的元件求和副本，导致
**修基类对生产路径零效果**，含 PVSystem 的算例照样失败。

本套件锁死三件事：

  A. 归一化：母线名去相别 / 去空白 / 转小写，且幂等。
  B. 判定规则：五类场景的判定结果与错误消息（引擎无关，纯数据）。
  C. 契约：规则**只存在于 `circuit_validation`**，两个适配器不得再各写一份。

全部自包含（假观察量），不依赖真实 COM 或 dss-python。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpf_agent.engines.circuit_validation import (  # noqa: E402
    CircuitObservation,
    normalize_bus_name,
    validate_compiled_circuit,
)

_PASS = 0
_FAIL = 0
_RESULTS: list[str] = []
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 结果落盘在包外隔离目录，避免污染源码树（同其它套件的约定）。
_OUT_DIR = os.environ.get("DPF_TEST_OUT_DIR") or tempfile.gettempdir()


def _flush() -> None:
    try:
        os.makedirs(_OUT_DIR, exist_ok=True)
        with open(os.path.join(_OUT_DIR, "test_circuit_validation.txt"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(_RESULTS) + "\n")
    except Exception:  # noqa: BLE001 - 落盘失败不影响测试结论
        pass


def check(cid: str, desc: str, cond: object, detail: str = "") -> None:
    global _PASS, _FAIL
    ok = bool(cond)
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    line = ("[PASS] " if ok else "[FAIL] ") + f"{cid} {desc}"
    if detail:
        line += f"  {detail}"
    _RESULTS.append(line)
    print(line)
    _flush()


def _read(rel: str) -> str:
    with open(os.path.join(_PKG, rel), encoding="utf-8") as f:
        return f.read()


def _obs(
    bus_count: int = 3,
    bus_names: tuple[str, ...] = ("bus1", "bus2", "bus3"),
    endpoints: tuple[tuple[str, str], ...] = (),
    bus_base_kv: tuple[tuple[str, object], ...] | None = None,
    vsource_buses: tuple[str, ...] = ("bus1",),
) -> CircuitObservation:
    """构造一个观察量。默认是一台"健康"的电路：三母线、kVBase 正常、bus1 是源。"""
    if bus_base_kv is None:
        bus_base_kv = tuple((n, 20.0) for n in bus_names)
    return CircuitObservation(
        bus_count=bus_count,
        bus_names=frozenset(bus_names),
        endpoints=tuple(endpoints),
        bus_base_kv=tuple(bus_base_kv),
        vsource_buses=frozenset(vsource_buses),
    )


def main() -> int:
    print("=" * 78)
    print("A. 母线名归一化")
    print("=" * 78)
    check("A1", "去相别：Bus1.1.2 → bus1",
          normalize_bus_name("Bus1.1.2") == "bus1", normalize_bus_name("Bus1.1.2"))
    check("A2", "去空白 + 转小写：'  F1_001  ' → f1_001",
          normalize_bus_name("  F1_001  ") == "f1_001", normalize_bus_name("  F1_001  "))
    check("A3", "归一化是幂等的",
          normalize_bus_name(normalize_bus_name("Sub.1.2.3"))
          == normalize_bus_name("Sub.1.2.3"))
    check("A4", "None / 空串 → 空串（不抛异常）",
          normalize_bus_name(None) == "" and normalize_bus_name("") == "")

    print()
    print("=" * 78)
    print("B. 判定规则（引擎无关，纯数据）")
    print("=" * 78)
    # B1：电路无母线
    d = validate_compiled_circuit(_obs(bus_count=0, bus_names=(), bus_base_kv=()))
    check("B1", "电路无母线 → 拒绝", "无母线" in d, d)

    # B2：健康电路
    d = validate_compiled_circuit(
        _obs(endpoints=(("Line.L1", "bus1.1"), ("Line.L1", "bus2.1")))
    )
    check("B2", "健康电路 → 放行（空串）", d == "", repr(d))

    # B3：引用不存在的母线
    d = validate_compiled_circuit(
        _obs(endpoints=(("Line.L1", "bus1.1"), ("Line.L1", "ghost.1")))
    )
    check("B3", "引用不存在的母线 → 拒绝并列出元件",
          "未解析的母线引用" in d and "ghost" in d, d)

    # B4：零基准母线被元件使用
    d = validate_compiled_circuit(
        _obs(
            bus_names=("bus1", "bus2", "zero"),
            bus_base_kv=(("bus1", 20.0), ("bus2", 20.0), ("zero", 0.0)),
            endpoints=(("Line.L1", "bus2.1"), ("Line.L2", "zero.1")),
        )
    )
    check("B4", "零基准母线被引用 → 拒绝并给出引用示例",
          "缺少电压基准" in d and "zero" in d, d)

    # B5：零基准母线存在，但没有元件用它 —— 无害
    d = validate_compiled_circuit(
        _obs(
            bus_names=("bus1", "bus2", "zero"),
            bus_base_kv=(("bus1", 20.0), ("bus2", 20.0), ("zero", 0.0)),
            endpoints=(("Line.L1", "bus2.1"),),
        )
    )
    check("B5", "零基准母线无人使用 → 放行", d == "", repr(d))

    # B6：Vsource 所在母线豁免（它自带 basekv，母线基准表没回填也不算错）
    d = validate_compiled_circuit(
        _obs(
            bus_names=("src", "bus2"),
            bus_base_kv=(("src", 0.0), ("bus2", 20.0)),
            endpoints=(("Vsource.source", "src.1"), ("Line.L1", "bus2.1")),
            vsource_buses=("src",),
        )
    )
    check("B6", "Vsource 母线豁免零基准检查 → 放行", d == "", repr(d))

    # B7：相别不影响判定（同一母线的不同相都算存在）
    d = validate_compiled_circuit(
        _obs(endpoints=(("Load.LD1", "bus2.2"), ("Load.LD2", "bus2.3")))
    )
    check("B7", "带相别的端点不误判", d == "", repr(d))

    # B8：采样上限 —— 错误消息只列前 10 条，不能把上千条塞进异常
    many = tuple((f"Line.L{i}", "ghost.1") for i in range(500))
    d = validate_compiled_circuit(_obs(endpoints=many))
    _n = d.count("ghost")
    check("B8", "错误消息有采样上限（不把 500 条全塞进去）",
          "未解析的母线引用" in d and _n <= 11, f"消息里出现 {_n} 次 ghost")

    # B9：消息格式与旧实现一致（运维/日志里的关键词不能变）
    _d3 = validate_compiled_circuit(
        _obs(endpoints=(("Line.L1", "ghost.1"),))
    )
    check("B9", "关键词与旧实现一致（发现未解析的母线引用：…）",
          _d3.startswith("发现未解析的母线引用："), _d3[:60])

    print()
    print("=" * 78)
    print("C. 契约：规则只允许存在一份（防回归）")
    print("=" * 78)
    _src_cv = _read(os.path.join("engines", "circuit_validation.py"))
    _src_com = _read(os.path.join("engines", "opendss_com_adapter.py"))
    _src_base = _read(os.path.join("engines", "opendss_adapter.py"))

    check("C1", "circuit_validation 含判定规则本体",
          "def validate_compiled_circuit" in _src_cv
          and "未解析的母线引用" in _src_cv, "已定义")
    check("C2", "COM 适配器**不再**自带一份规则文本",
          "未解析的母线引用" not in _src_com,
          "（此前它与基类各写一份，改一份不影响另一份）")
    check("C3", "基类适配器**不再**自带一份规则文本",
          "未解析的母线引用" not in _src_base)
    check("C4", "两个适配器都调用同一个判定函数",
          "validate_compiled_circuit" in _src_com
          and "validate_compiled_circuit" in _src_base)
    check("C5", "各通道各有收集器（引擎相关的那一半）",
          "_collect_com_observation" in _src_com
          and "_collect_dss_observation" in _src_base)
    check("C6", "收集器跳过未启用的元件（契约）",
          _src_com.count("if not ce.Enabled") >= 1
          and _src_base.count("if not ce.Enabled") >= 1,
          "未启用元件不应产生端点")
    check("C7", "收集器都用了同一套归一化函数",
          "normalize_bus_name" in _src_com and "normalize_bus_name" in _src_base)

    print()
    print("=" * 78)
    print(f"结果：通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 78)
    _flush()
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
