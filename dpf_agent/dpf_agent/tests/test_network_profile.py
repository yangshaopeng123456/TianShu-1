"""网络画像：**收集（引擎相关）与组装（纯数据）必须分开**。

## 背景

`build_network_profile` 原先接收一个 `run_with_engine` 回调，把整段画像逻辑
（母线 / 线路 / 变压器 / 发电机 / PV / 调压器 / 电容器 / 单相元件 / 拓扑邻接表）
塞进闭包 `_build(eng)` 里执行。

这对 COM 与 dss-python 都没问题 —— 它们能执行 Python 闭包。但
**`OpenDSScmd.exe` 是独立进程，只认命令流与 CSV，执行不了任意 Python 函数**，
于是 CLI 通道完全复用不了画像逻辑。

现在切成两半：

    * `engines/circuit_inventory.collect_circuit_inventory(eng)` —— 读引擎
    * `domain/network_profile.build_network_profile(case_id, inventory, dss_root)`
      —— 纯数据推导，不知道引擎是谁

本套件锁死两件事：

  A. **组装可以离线复算**：给定一个 `CircuitInventory`，画像的每个字段都算得出来，
     不需要引擎、不需要真实算例。
  B. **契约**：画像代码里不许再出现 `run_with_engine`（防止闭包写法回归），
     收集器只在 `circuit_inventory` 里定义一份，两个通道都转发到它。

全部自包含。
"""

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpf_agent.domain.network_profile import build_network_profile  # noqa: E402
from dpf_agent.engines.circuit_inventory import CircuitInventory  # noqa: E402

_PASS = 0
_FAIL = 0
_RESULTS: list[str] = []
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT_DIR = os.environ.get("DPF_TEST_OUT_DIR") or tempfile.gettempdir()


def _flush() -> None:
    try:
        os.makedirs(_OUT_DIR, exist_ok=True)
        with open(os.path.join(_OUT_DIR, "test_network_profile.txt"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(_RESULTS) + "\n")
    except Exception:  # noqa: BLE001
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


def _inv(**kw: object) -> CircuitInventory:
    """构造一个最小可用的观察量：3 母线 / 2 线路 / 2 负荷（1 三相 + 1 单相）。

    拓扑是链式 sourcebus—b2—b3（无环），电压等级 12.47 与 0.4。
    """
    base: dict[str, object] = dict(
        bus_count=3,
        line_count=2,
        transformer_count=1,
        load_count=2,
        generator_count=0,
        pv_system_count=0,
        storage_count=0,
        capacitor_count=0,
        regulator_count=0,
        bus_kv_base=(("SourceBus", 12.47), ("b2", 12.47), ("b3", 0.4)),
        loads=((1000.0, 3), (200.0, 1)),
        line_endpoints=((True, "sourcebus", "b2"), (True, "b2", "b3")),
    )
    base.update(kw)
    return CircuitInventory(**base)  # type: ignore[arg-type]


def main() -> int:
    print("=" * 78)
    print("A. 组装是纯数据：给定观察量即可离线算出画像")
    print("=" * 78)

    p = build_network_profile("case-x", _inv())
    check("A1", "基础计数透传",
          (p.bus_count, p.line_count, p.transformer_count, p.load_count) == (3, 2, 1, 2),
          f"bus={p.bus_count} line={p.line_count} tr={p.transformer_count} load={p.load_count}")
    check("A2", "电压等级去重并按数值升序",
          p.voltage_levels_kv == [0.4, 12.47], str(p.voltage_levels_kv))
    check("A3", "无 dss 文本时按母线名兜底识别 slack（source / sourcebus，忽略大小写）",
          p.slack_bus_count == 1, str(p.slack_bus_count))

    # A3b~A3d：松弛节点应以**算例结构**（`New Circuit.… bus1=`）为准，而不是猜母线名。
    # 实测 case31 的源母线名为 GRID：只按名字猜会得 slack=0，并连带把它算进 PQ 母线
    # —— 对外答「平衡节点 0 个」，属说错话。
    _slack_dir = tempfile.mkdtemp(prefix="np-slack-")
    with open(os.path.join(_slack_dir, "Master.dss"), "w", encoding="utf-8") as _fh:
        _fh.write("Clear\nNew Circuit.Case31 basekv=220 pu=1.03 bus1=GRID\n"
                  "New Line.L1 bus1=GRID bus2=SUB35A\n")
    _p_grid = build_network_profile(
        "case-grid", _inv(bus_kv_base=(("GRID", 220.0), ("SUB35A", 35.0))), _slack_dir)
    check("A3b", "按 Circuit.bus1 识别 slack（源母线名为 GRID 也认）",
          _p_grid.slack_bus_count == 1, f"slack={_p_grid.slack_bus_count}")
    check("A3c", "pq 母线随之修正（不再把 GRID 算进 PQ）",
          _p_grid.pq_bus_count == 3 - 1 - 0, f"pq={_p_grid.pq_bus_count}")
    check("A3d", "无 dss 文本且名字不匹配时不误判为 slack",
          build_network_profile(
              "case-none", _inv(bus_kv_base=(("SOMEBUS", 10.0),)), None
          ).slack_bus_count == 0)

    # ── A3e：方括号多值属性的分词（真机 case31 实测缺陷）─────────────
    # `New Transformer.TA ... buses=[GRID SUB110A]` 内部**含空格**。
    # 旧分词器只把引号当引号对，按空白切分会把 `buses=[GRID` 与`SUB110A]`
    # 拆成两个 token → 端点只剩 1 个 → 16 台变压器端点全空，
    # 而线路（`bus1=/bus2=`）264 条全正常。差别**只在这一个写法**。
    from dpf_agent.engines.dss_source import parse_dss_text

    _tx = parse_dss_text(
        "New Transformer.TA phases=3 windings=2 buses=[GRID SUB110A] "
        "conns=[wye wye] kvs=[220 110] kvas=[150000 150000] %rs=[0.35 0.35]")
    check("A3e ★ 方括号多值属性不被空格拆散（buses=[A B] 两端点都在）",
          bool(_tx) and _tx[0].buses == ("grid", "sub110a"),
          f"buses={_tx[0].buses if _tx else '(空)'}")
    # 同一条定义里 kvas/conns 也必须保持完整 —— 它们影响 rated_kva
    check("A3f 其它方括号属性同样完整（kvas 不被拆成两个 token）",
          any(t.startswith("kvas=[150000 150000]") for t in _tx[0].tokens),
          str(list(_tx[0].tokens)[:8]))
    # 回归护栏：引号写法与嵌套括号不能被这次改动破坏。
    # ⚠️ 判据必须**真的能判**：第一版写的是 `== ("a b","c")` 但实测值是
    # `('"a b"', 'c')`（引号没剥掉），却依然显示 PASS ——
    # 因为 `check` 只收布尔，`and` 之后的表达式没被独立断言。
    # 这里改成显式比较并把实际值打进 detail，判错时一眼看得出。
    _q = parse_dss_text('New Line.L1 bus1="A B" bus2=C')
    _qb = _q[0].buses if _q else ()
    check("A3g 引号写法剥掉外层引号且不被空格拆散",
          _qb == ("a b", "c"), f"buses={_qb!r}")

    # A3h：原创语法夹具覆盖圆括号、多值引号和两相端口归一化。
    # 圆括号内的空格不能拆断 token，母线名也不能保留开头的括号。
    _paren = parse_dss_text(
        "new transformer.synthetic phases=1 windings=2 bank=demo "
        "buses=(B1.1.2 B2.1.2) conns='delta delta' kvs=\"10 10\" kvas=\"150 150\"")
    _paren_buses = _paren[0].buses if _paren else ()
    check("A3h ★ 圆括号多值属性两端点都在，且括号被剥掉",
          _paren_buses == ("b1", "b2"), f"buses={_paren_buses!r}")

    # A3i：原创两绕组夹具覆盖 `~` 行续接。
    _cont = parse_dss_text(
        "New Transformer.SubXF Phases=3 Windings=2 Xhl=5\n"
        "~ wdg=1 bus=sourcebus conn=Delta kv=20 kva=1000 %r=0.5\n"
        "~ wdg=2 bus=b2        conn=Delta kv=10 kva=1000 %r=0.5\n")
    _cbuses = sorted(b for e in _cont for b in e.buses)
    check("A3i ★ `~` 续行的两段绕组母线都进端点",
          _cbuses == ["b2", "sourcebus"], f"buses={_cbuses}")

    check("A4", "pq_bus = bus − slack − pv",
          p.pq_bus_count == 3 - 1 - 0, str(p.pq_bus_count))
    check("A5", "单相 / 三相元件按相数分列",
          (p.three_phase_element_count, p.single_phase_element_count) == (1, 1),
          f"3ph={p.three_phase_element_count} 1ph={p.single_phase_element_count}")
    check("A6", "总装机负荷为各负荷额定 kW 之和",
          p.total_installed_load_kw == 1200.0, str(p.total_installed_load_kw))
    check("A7", "链式拓扑 → 辐射状（cycle_rank=0，is_radial=True）",
          p.cycle_rank == 0 and p.is_radial is True,
          f"cycle_rank={p.cycle_rank} is_radial={p.is_radial}")

    # 加一条 b3—sourcebus 的线路 → 成环
    p2 = build_network_profile("case-y", _inv(
        line_count=3,
        line_endpoints=((True, "sourcebus", "b2"), (True, "b2", "b3"), (True, "b3", "sourcebus")),
    ))
    check("A8", "三角形拓扑 → cycle_rank=1、is_radial=False",
          p2.cycle_rank == 1 and p2.is_radial is False,
          f"cycle_rank={p2.cycle_rank} is_radial={p2.is_radial}")
    check("A9", "检出环路并写进 notes",
          any("环路" in n for n in p2.notes), str(p2.notes)[:80])

    # 同一对端点两条线 → 并行线路对
    p3 = build_network_profile("case-z", _inv(
        line_count=3,
        line_endpoints=((True, "sourcebus", "b2"), (True, "sourcebus", "b2"), (True, "b2", "b3")),
    ))
    check("A10", "并行线路端点对计数（同端点对 >1 条）",
          p3.parallel_line_pairs == 1, str(p3.parallel_line_pairs))

    # 含分布式电源时 notes 必须提示"反向潮流不应仅因负号判非法"
    p4 = build_network_profile("case-pv", _inv(pv_system_count=2, generator_count=1))
    check("A11", "含 PV/发电机时 notes 提示反向潮流不可仅凭负号判非法",
          any("反向潮流" in n for n in p4.notes), str(p4.notes)[:90])
    check("A12", "pv_bus = PVSystem + Generator",
          p4.pv_bus_count == 3, str(p4.pv_bus_count))

    # dss_root=None 时启停计数由端点表自己数（不从副本文本补齐）
    p5 = build_network_profile("case-off", _inv(
        line_endpoints=((True, "sourcebus", "b2"), (False, "b2", "b3")),
    ))
    check("A13", "无副本文本时按端点表自行统计线路启停",
          p5.active_line_count == 1 and p5.inactive_line_count == 1,
          f"active={p5.active_line_count} inactive={p5.inactive_line_count}")

    print()
    print("=" * 78)
    print("B. 契约：画像里不许再有闭包；收集器只允许一份")
    print("=" * 78)
    _src_np = _read(os.path.join("domain", "network_profile.py"))
    _src_ci = _read(os.path.join("engines", "circuit_inventory.py"))
    _src_base = _read(os.path.join("engines", "opendss_adapter.py"))
    _src_com = _read(os.path.join("engines", "opendss_com_adapter.py"))

    check("B1", "network_profile.py 不再接收或调用 run_with_engine（闭包写法已移除）",
          "run_with_engine:" not in _src_np and "run_with_engine(" not in _src_np,
          "它曾让 CLI 通道无法复用画像逻辑（注释里提旧写法是允许的）")
    check("B2", "build_network_profile 的第二个参数是 inventory（观察量，不是回调）",
          list(inspect.signature(build_network_profile).parameters)[:2]
          == ["case_id", "inventory"],
          str(list(inspect.signature(build_network_profile).parameters)))
    check("B3", "收集器只用两通道共有的 API（AllBusNames / SetActiveBus / ActiveCktElement）",
          all(k in _src_ci for k in ("AllBusNames", "SetActiveBus", "ActiveCktElement")),
          "两个通道跑同一份收集代码")
    check("B4", "两个适配器都实现了 collect_circuit_inventory",
          "def collect_circuit_inventory" in _src_base
          and "def collect_circuit_inventory" in _src_com)
    check("B5", "COM 侧经 _worker.call 转发（引擎对象只能在 worker 线程内访问）",
          "_worker.call(collect_circuit_inventory" in _src_com)
    check("B6", "收集器在引擎层只定义一次（不在 domain 里重复实现）",
          "ActiveCircuit" not in _src_np,
          "domain 层不该出现引擎 API")

    print()
    print("=" * 78)
    print(f"结果：通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 78)
    _flush()
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
