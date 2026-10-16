# -*- coding: utf-8 -*-
"""转换产物新鲜度机制回归测试（自包含，不依赖引擎与外部算例文件）。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_conversion_cache`

覆盖的真实缺陷：上传算例的转换产物被长期复用，转换器改进后老算例不会重新转换
→ 修复对它无效且毫无提示。本测试锁定"版本戳 + 自动重建"这条链路。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

from ..web import conversion_cache as cc

# 最小 MATPOWER 算例：slack 母线 Vm=1.02（用于验证母线 Vm 被读入源电压设定）、
# 一台 PV 发电机（用于验证 Model=3 电压控制建模）。
MIN_MPC = """function mpc = case_min
mpc.version = '2';
mpc.baseMVA = 100;

%% bus data
% bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin
mpc.bus = [
	1	 3	 0	  0	 0	 0	 1	 1.02	 0	 110	 1	 1.1	 0.9;
	2	 1	 30	  10	 0	 0	 1	 1.00	 0	 110	 1	 1.1	 0.9;
	3	 2	 20	  8	 0	 0	 1	 1.01	 0	 110	 1	 1.1	 0.9;
];

%% generator data
% bus Pg Qg Qmax Qmin Vg mBase status Pmax Pmin
mpc.gen = [
	1	 50	 0	 100	 -100	 1.02	 100	 1	 200	 0;
	3	 25	 0	 60	 -60	 1.01	 100	 1	 100	 0;
];

%% branch data
% fbus tbus r x b rateA rateB rateC ratio angle status angmin angmax
mpc.branch = [
	1	 2	 0.005	 0.05	 0.001	 250	 250	 250	 0	 0	 1	 -360	 360;
	2	 3	 0.004	 0.04	 0.001	 250	 250	 250	 0	 0	 1	 -360	 360;
];
"""

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def _legacy_layout(root: str, sess: str, stem: str) -> tuple[str, str]:
    """造"历史产物"：源文件在 <root>/<sess>/<stem>.m，产物在 <root>/<sess>/<stem>/Master.dss。"""
    sess_dir = os.path.join(root, sess)
    os.makedirs(os.path.join(sess_dir, stem), exist_ok=True)
    src = os.path.join(sess_dir, f"{stem}.m")
    with open(src, "w", encoding="utf-8") as f:
        f.write(MIN_MPC)
    master = os.path.join(sess_dir, stem, "Master.dss")
    with open(master, "w", encoding="utf-8") as f:
        f.write("Clear\nNew Circuit.legacy basekv=110 pu=1\n"
                "New Generator.G1 Bus1=b1 kV=110 kW=1000 kvar=0 Model=1 Vpu=1.02\n")
    return src, master


def main() -> int:
    root = tempfile.mkdtemp(prefix="convtest_")
    print(f"临时工作目录: {root}\n")
    try:
        # ① 历史产物（无 meta）应被自动重建，且采用新转换逻辑
        src, master = _legacy_layout(root, "sess-a", "case_min")
        before = open(master, encoding="utf-8").read()
        new_master, how = cc.refresh_master_if_stale(master)
        after = open(new_master, encoding="utf-8").read()
        check("① 历史产物被判定为需重建", how in ("no-meta-rebuilt", "rebuilt"), f"how={how}")
        check("① 重建后内容发生变化", before != after)
        check("① 重建后形成 PV 电压控制（Model=3）", "Model=3" in after, f"Model=3 数量={after.count('Model=3')}")
        check("① 重建后源电压采用母线 Vm（pu=1.02）", "pu=1.02" in after,
              [l.strip() for l in after.splitlines() if "New Circuit" in l][:1])
        meta_dir = os.path.dirname(new_master)
        check("① 重建后写入 .conversion.json", os.path.isfile(os.path.join(meta_dir, cc.META_NAME)))
        meta = cc.read_meta(meta_dir) or {}
        check("① meta 记录源文件与当前版本",
              meta.get("version") == cc.CONVERTER_VERSIONS["dss_from_tables"]
              and os.path.basename(str(meta.get("source"))) == "case_min.m",
              f"version={meta.get('version')}")
        check("① meta 记录了源文件指纹", bool(meta.get("source_sha256")))

        # ② 已是最新版本时不应重复重建（幂等，避免每次引用都白跑转换）
        mtime = os.path.getmtime(new_master)
        again, how2 = cc.refresh_master_if_stale(new_master)
        check("② 最新产物不再重建", how2 == "fresh", f"how={how2}")
        check("② 产物文件未被触碰", os.path.getmtime(again) == mtime)

        # ③ 转换器版本升级后，旧版本产物应被重建（这是本机制的核心开关）
        meta = cc.read_meta(meta_dir) or {}
        meta["version"] = "0.0.1"
        with open(os.path.join(meta_dir, cc.META_NAME), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        _, how3 = cc.refresh_master_if_stale(new_master)
        check("③ 版本不一致被判定过期并重建", how3.startswith("rebuilt"), f"how={how3}")
        check("③ 重建后 meta 版本已刷新",
              (cc.read_meta(meta_dir) or {}).get("version") == cc.CONVERTER_VERSIONS["dss_from_tables"])

        # ④ 非转换产物（内置算例 / 直传 .dss）不应被干预
        plain_dir = os.path.join(root, "cases", "ieee33")
        os.makedirs(plain_dir, exist_ok=True)
        plain = os.path.join(plain_dir, "Master.dss")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("Clear\nNew Circuit.ieee33 basekv=12.66\n")
        _, how4 = cc.refresh_master_if_stale(plain)
        check("④ 无源文件可推断时不干预", how4 == "not-applicable", f"how={how4}")

        # ⑤ 源文件丢失时应沿用旧产物而不是报错/删产物
        src5, master5 = _legacy_layout(root, "sess-b", "case_lost")
        meta5 = cc.write_meta(os.path.dirname(master5), "dss_from_tables", src5)
        meta5["version"] = "0.0.1"
        with open(os.path.join(os.path.dirname(master5), cc.META_NAME), "w", encoding="utf-8") as f:
            json.dump(meta5, f, ensure_ascii=False)
        os.remove(src5)
        kept, how5 = cc.refresh_master_if_stale(master5)
        check("⑤ 源文件丢失时沿用旧产物且不报错",
              how5 == "stale-unresolved" and os.path.isfile(kept), f"how={how5}")

        # ⑥ 强制重建开关可用（调试转换器时用）
        src6, master6 = _legacy_layout(root, "sess-c", "case_force")
        first, _ = cc.refresh_master_if_stale(master6)
        _, how6 = cc.refresh_master_if_stale(first, force=True)
        check("⑥ force=True 时无视版本一致性重建",
              how6.startswith("rebuilt") or how6 == "no-meta-rebuilt", f"how={how6}")

        # ⑦ 空路径/不存在的路径应安全返回
        check("⑦ 空路径安全返回", cc.refresh_master_if_stale(None)[0] is None)
        missing = os.path.join(root, "nope", "Master.dss")
        check("⑦ 路径不存在安全返回", cc.refresh_master_if_stale(missing)[1] == "not-applicable")

        # ⑧ 歧义保护：同名源文件有多个扩展名时不猜（从错误的源重建会静默换掉整个模型）
        src8, master8 = _legacy_layout(root, "sess-d", "case_ambig")
        extra = os.path.join(root, "sess-d", "case_ambig.xlsx")
        with open(extra, "wb") as f:
            f.write(b"not a real xlsx")
        old8 = open(master8, encoding="utf-8").read()
        _, how8 = cc.refresh_master_if_stale(master8)
        check("⑧ 源文件歧义时沿用旧产物", how8 == "not-applicable", f"how={how8}")
        check("⑧ 歧义时产物未被改动", open(master8, encoding="utf-8").read() == old8)
        # meta 记录了确切的源文件，因此不受"同名多扩展名"歧义影响
        cc.write_meta(os.path.dirname(master8), "dss_from_tables", src8)
        _, how8b = cc.refresh_master_if_stale(master8)
        check("⑧ meta 优先于推断（有 meta 时无歧义）", how8b == "fresh", f"how={how8b}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
