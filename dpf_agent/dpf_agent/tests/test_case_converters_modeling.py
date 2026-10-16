# -*- coding: utf-8 -*-
"""转换器建模口径回归测试（自包含，不依赖引擎）。

锁定已修复的三类"静默改变物理模型"缺陷——它们的共同点是**不报错、只让数值偏移**，
所以只能靠断言把口径钉住：

1. MATPOWER 母线 Vm（第 8 列）→ 源电压设定 `pu=`；
2. PV 发电机 → `Model=3` 电压控制（此前 Model=1 恒功率，Vpu 被架空）；
3. MATPOWER 支路变比 ratio（第 9 列）→ OpenDSS `tap`（此前被整体丢弃，恒为 1）；
4. 变压器阻抗基准：kVA 取**系统基准**（rateA 是热稳限额，不是阻抗基准），
   `%Rs` 双绕组对半拆分（OpenDSS 串联 R 为两绕组之和，写两遍 = 电阻翻倍）。
5. System 表形态：结构化单行必须能读到参数（含频率）；读不出时必须**显式警告**，
   不许静默降级到默认 50 Hz（60 Hz 算例的线路充电电纳 B=2πfC 会整体算错）。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_case_converters_modeling`
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

from ..web.case_converters import build_dss_from_frames, convert_matpower_m

# slack 母线 Vm=1.04；一台 PV 机 Vg=1.02；一台变比 0.98 的变压器支路；
# 线路带充电电纳 b=0.02（当前尚未建模，仅用于说明现状，不作为断言）。
MIN_MPC = """function mpc = case_min
mpc.version = '2';
mpc.baseMVA = 100;

%% bus data
% bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin
mpc.bus = [
	1	 3	 0	  0	 0	 0	 1	 1.04	 0	 110	 1	 1.1	 0.9;
	2	 1	 30	  10	 0	 0	 1	 1.00	 0	 110	 1	 1.1	 0.9;
	3	 2	 20	  8	 0	 0	 1	 1.01	 0	 110	 1	 1.1	 0.9;
	4	 1	 15	  5	 0	 0	 1	 1.00	 0	 20	 1	 1.1	 0.9;
];

%% generator data
% bus Pg Qg Qmax Qmin Vg mBase status Pmax Pmin
mpc.gen = [
	1	 50	 0	 100	 -100	 1.04	 100	 1	 200	 0;
	3	 25	 0	 60	 -60	 1.02	 100	 1	 100	 0;
];

%% branch data
% fbus tbus r x b rateA rateB rateC ratio angle status
mpc.branch = [
	1	 2	 0.005	 0.05	 0.02	 55	 55	 55	 0	 0	 1;
	2	 3	 0.004	 0.04	 0.02	 55	 55	 55	 0	 0	 1;
	3	 4	 0.0025	 0.04	 0.00	 55	 55	 55	 0.98	 0	 1;
];
"""

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def _kv(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}=([0-9.]+)", text)
    return float(m.group(1)) if m else None


def _arr_kv(text: str, key: str) -> float | None:
    """读取 OpenDSS 数组型参数的首个值，如 kvas=[100000.0 100000.0]。"""
    m = re.search(rf"\b{key}=\[([0-9.]+)", text)
    return float(m.group(1)) if m else None


def main() -> int:
    root = tempfile.mkdtemp(prefix="modeling_")
    try:
        src = os.path.join(root, "case_min.m")
        with open(src, "w", encoding="utf-8") as f:
            f.write(MIN_MPC)
        out = os.path.join(root, "converted")
        master = convert_matpower_m(src, out)
        text = open(master, encoding="utf-8").read()
        circuit = next((l for l in text.splitlines() if "New Circuit" in l), "")
        gens = [l.strip() for l in text.splitlines() if l.strip().lower().startswith("new generator")]
        xfmrs = [l.strip() for l in text.splitlines() if l.strip().lower().startswith("new transformer")]

        # ① 母线 Vm → 源电压设定
        check("① 源电压采用 slack 母线 Vm（pu=1.04）", _kv(circuit, "pu") == 1.04, circuit[:110])

        # ② PV 发电机建模为电压控制，并带自身 Vg
        #    注意：slack 母线由 source 承担，不会再生成 Generator，所以这里只有 1 台机。
        pv_gens = [g for g in gens if "Bus1=1" not in g]
        check("② PV 发电机被生成（slack 母线除外）", len(pv_gens) == 1, f"发电机数={len(gens)}")
        check("② PV 机以 Model=3（电压控制）建模",
              bool(gens) and all(_kv(g, "Model") == 3 for g in gens),
              f"Model={_kv(gens[0], 'Model') if gens else None}")
        check("② PV 机携带自身 Vg 作为 Vpu（1.02）",
              bool(pv_gens) and _kv(pv_gens[0], "Vpu") == 1.02,
              pv_gens[0][:110] if pv_gens else "无 PV 机")

        # ③ 支路变比 → tap（此前被整体丢弃，恒为 1）
        check("③ 存在跨电压等级支路被建模为变压器", len(xfmrs) == 1, f"变压器数={len(xfmrs)}")
        if xfmrs:
            xf = xfmrs[0]
            check("③ MATPOWER ratio 写入 OpenDSS tap（0.98）", _kv(xf, "tap") == 0.98, xf[:130])
            # ④ 阻抗基准：kVA = 系统基准 100 MVA（而非 rateA=55 MVA）
            kva = _arr_kv(xf, "kvas")
            check("④ 变压器 kVA 基准取系统基准（100 MVA）", kva == 100000.0, f"kvas={kva}")
            pct = re.search(r"%Rs=\[([0-9.]+)", xf)
            r_pct = float(pct.group(1)) if pct else None
            check("④ %Rs 双绕组对半拆分（r_pu×100/2 = 0.125）", r_pct == 0.125, f"%Rs={r_pct}")
            xhl = _kv(xf, "XHL")
            check("④ XHL 为总漏抗百分值（x_pu×100 = 4.0）", xhl == 4.0, f"XHL={xhl}")

        # ⑤ 变压器不得再被当成普通线路（会丢掉变比与电压等级）
        line_ids = [l for l in text.splitlines() if l.strip().lower().startswith("new line.")]
        check("⑤ 跨电压等级支路未被降级为普通线路", len(line_ids) == 2, f"线路数={len(line_ids)}")

        # 说明性检查（不作为通过条件，只打印，避免"静默不建模"被误当成已完成）：
        #   1) PV 无功限值默认**不执行**（参考解的求解口径是 flat-start NR +
        #      unconstrained PV-Q；且表格算例给的 qmin/qmax 常极小）。字段已被解析，
        #      需要严格限值语义时用 System 表的 ENFORCE_Q_LIMITS=1 打开。
        #   2) OpenDSS 负荷默认 Vminpu=0.95，电压低于 0.95 pu 时负荷自动转恒阻抗，
        #      与参考解的恒功率口径不一致 —— 这是**已知未决项**，须先确认以谁为准。
        notes: list[str] = []
        if not any("minkvar=" in g for g in pv_gens):
            notes.append("PV 无功限值未执行（按参考解口径，默认关闭）")
        if "Vminpu" not in text:
            notes.append("负荷未显式设 Vminpu（沿用 OpenDSS 默认 0.95 会隐性转恒阻抗）")
        for n in notes:
            print(f"⚠ 建模口径说明：{n}")

        # ---- ⑥ System 表形态：参数必须读得到；读不到必须**说出来** ----
        #
        # 起因：`_read_system_params` 的结构化单行分支原先要求"必须存在 base_mva 或 base_kv 列"
        # 才进入，于是**只声明 frequency_hz 的 system 表整行被弃**，频率回落硬编码 50 Hz。
        # 实测复现（改前）：只给 frequency_hz=60 → 产物 `Set DefaultBaseFrequency=50`，
        # **且产物里没有任何提示**。60 Hz 算例的线路充电电纳 B=2πfC 会整体算错。
        import pandas as _pd

        _buses = _pd.DataFrame([
            {"bus_id": "B1", "base_kv": 11.0, "bus_type": "3", "vm_set_pu": 1.0,
             "p_kw": 0.0, "q_kvar": 0.0},
            {"bus_id": "B2", "base_kv": 11.0, "bus_type": "1", "p_kw": 1000.0, "q_kvar": 300.0},
        ])
        _lines = _pd.DataFrame([
            {"line_id": "L1", "from_bus": "B1", "to_bus": "B2", "r": 0.5, "x": 1.2}
        ])

        def _freq_and_warn(sys_df: _pd.DataFrame) -> tuple[str | None, bool]:
            work = tempfile.mkdtemp(prefix="cvt_sys_")
            try:
                path = build_dss_from_frames(sys_df, _buses, _lines, work, source_label="t")
                text = open(path, encoding="utf-8").read()
                m = re.search(r"Set DefaultBaseFrequency=([\d.]+)", text)
                return (m.group(1) if m else None), ("未识别出任何系统参数" in text)
            finally:
                shutil.rmtree(work, ignore_errors=True)

        fq, wn = _freq_and_warn(_pd.DataFrame([{"frequency_hz": 60, "slack_pu": 1.0}]))
        check("⑥ 结构化单行系统表（无 base_kv/base_mva）也能读到频率",
              fq == "60" and not wn, f"频率={fq} 警告={wn}")

        fq, _wn = _freq_and_warn(_pd.DataFrame([{"key": "FREQUENCY_HZ", "value": 60}]))
        check("⑥ 单行 key|value 表不得被结构化分支误读（频率仍 60）", fq == "60", f"频率={fq}")

        fq, _wn = _freq_and_warn(_pd.DataFrame([{"parameter": "FREQUENCY_HZ", "value": 60}]))
        check("⑥ parameter|value 行式表原有路径不变（频率 60）", fq == "60", f"频率={fq}")

        fq, wn = _freq_and_warn(_pd.DataFrame([{"a": 1, "b": 2}, {"a": 3, "b": 4}]))
        check("⑥ 系统表读不出参数时必须显式警告（不许静默降级到 50 Hz）",
              fq == "50" and wn, f"频率={fq} 警告={wn}")

        # ---- ⑦ 目录 / zip 形态（独立代码分支，此前**没有任何覆盖**）----
        # `convert_csv` 对"目录"与"zip"走 `_convert_csv_dir`，与"混合行 CSV"是两条不同分支
        # （zip 还多一步解包）。这里只做**契约级**检查：能转出、且关键参数读对了；
        # 逐项数值保真由 `evals/run_conversion_fidelity`（含目录形态变体）覆盖。
        import zipfile

        from ..web.case_converters import convert_csv

        pack = tempfile.mkdtemp(prefix="cvt_pack_")
        try:
            _pd.DataFrame([
                {"bus_id": "B1", "base_kv": 11.0, "bus_type": "3", "vm_set_pu": 1.0,
                 "p_kw": 0.0, "q_kvar": 0.0},
                {"bus_id": "B2", "base_kv": 11.0, "bus_type": "1", "p_kw": 1000.0, "q_kvar": 300.0},
            ]).to_csv(os.path.join(pack, "bus.csv"), index=False)
            _pd.DataFrame([
                {"line_id": "L1", "from_bus": "B1", "to_bus": "B2", "r": 0.5, "x": 1.2}
            ]).to_csv(os.path.join(pack, "line.csv"), index=False)
            _pd.DataFrame([
                {"V_BASE_KV": 11.0, "S_BASE_MVA": 100.0, "FREQUENCY_HZ": 60}
            ]).to_csv(os.path.join(pack, "system.csv"), index=False)

            d_out = os.path.join(pack, "out_dir")
            p_dir = convert_csv(pack, d_out)
            t_dir = open(p_dir, encoding="utf-8").read()
            check("⑦ 目录形态（bus/line/system.csv）可转换且读到频率",
                  "Set DefaultBaseFrequency=60" in t_dir and "New Line.L1" in t_dir)

            zip_path = os.path.join(pack, "bundle.zip")
            with zipfile.ZipFile(zip_path, "w") as z:
                for name in ("bus.csv", "line.csv", "system.csv"):
                    z.write(os.path.join(pack, name), name)
            z_out = os.path.join(pack, "out_zip")
            p_zip = convert_csv(zip_path, z_out)
            check("⑦ zip 形态可转换（先解包再走同一分支）",
                  "Set DefaultBaseFrequency=60" in open(p_zip, encoding="utf-8").read())
        finally:
            shutil.rmtree(pack, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    for name in FAIL:
        print(f"  ✗ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
