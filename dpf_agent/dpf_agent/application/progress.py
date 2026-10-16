"""执行进度（`_current_progress.json`）的**单点定义**。

这份文件是全局复用的临时状态：同一时刻只跑一个任务，所以后端只维护「一个
当前进度」，UI 侧有**两个消费者** —— 轮询端点 `/api/task-progress` 与实时流
`/api/tasks/stream`。

## 它为什么会变成"僵尸"

`ApplicationService._write_progress` 每个阶段入口都写一次，但**只有正常收尾**
才会由 `_clear_progress` 写成 `done=True`。好在 `run()` 的三个 except 分支
（`GuardRejection` / `ToolFailure` / `Exception`）都**不 return**，会继续走到
收尾，所以 **Python 层面的失败不会留僵尸**。

唯一会留下 `done=False` 的是**进程被 Kill / 原生库段错误** —— 连 `finally`
都跑不到（`service.run` 的注释里写着这一点）。

## 为什么阈值与判据必须放在这里

僵尸留下后，两个消费者的行为**必须一致**：都把它判成"已结束"。否则界面会拿
一个早已死掉的任务当"当前任务"展示。

2026-10-09 实测的正是这种不一致：轮询端点早就有「超 10 分钟未更新即视为脏」
的判据，而实时流只认 `done` 字段 —— 于是用户发一句"你好"（闲聊，不产生任何
任务），右侧执行过程面板却显示 10-04 卡在 `validate_case` 的旧任务。

⇒ **阈值与判据只在这里写一份，两个消费者都调它。**（同"同一逻辑写两遍 =
早晚漂移"的项目纪律。）
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

#: 进度文件多久没更新就认定它已经不在跑了。
#:
#: 取 10 分钟的理由：必须**远大于任一单个阶段的静默时长**（大算例的 OpenDSS
#: 求解可达数十秒，阶段内不写进度），否则会把"正在跑的任务"误判成僵尸，
#: 那是比原缺陷更严重的回归（界面全程推不出阶段）；同时又要远小于跨天残留。
#: 原先是 `/api/task-progress` 里的字面量 600，这里提升为单点常量。
STALE_AFTER_S = 600.0

#: 进度文件名。与 `ApplicationService` 写入时用的相对路径一致。
PROGRESS_FILENAME = "_current_progress.json"


def progress_path(workspace_root: str) -> str:
    """进度文件在给定工作区下的绝对路径。"""
    return os.path.join(workspace_root, PROGRESS_FILENAME)


def read_progress(workspace_root: str) -> dict[str, Any] | None:
    """读进度快照。不存在 / 损坏 / 不是对象一律返回 None（按"没有任务"处理）。"""
    try:
        with open(progress_path(workspace_root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_finished(data: dict[str, Any] | None, now: float | None = None) -> bool:
    """这份进度是否已经"不该被当成正在跑的任务"。

    两种情形都算结束：
      1. 已标 `done`（任务正常收尾或被用户取消）；
      2. 超过 `STALE_AFTER_S` 没更新 —— 上次进程被 Kill / 原生库段错误的残留。

    没有任何时间戳时返回 False（宁可当成在跑，也不要把真任务判死）。
    """
    if not isinstance(data, dict):
        return False
    if data.get("done"):
        return True
    updated = data.get("updated_at")
    if not isinstance(updated, (int, float)):
        return False
    return ((time.time() if now is None else now) - float(updated)) > STALE_AFTER_S


def retire_stale(workspace_root: str) -> str | None:
    """把上次进程异常退出留下的"进行中"进度置为终态，返回被清理的 task_id。

    **只该在后端启动时调用。** 理由是这一刻的语义很确定：上一个进程已经不在，
    而同一时刻只跑一个任务 ⇒ 之前所有任务必然已结束。所以此刻若见到
    `done=False`，它一定是僵尸，直接置成终态。

    全程吞异常：这是收尾清理，不能变成新的故障源（与 `_write_progress`
    「写失败一律忽略」同一取舍）。
    """
    try:
        data = read_progress(workspace_root)
        if not isinstance(data, dict) or data.get("done"):
            return None
        task_id = data.get("task_id")
        data.update({
            "stage": "stale",
            "label": "上次运行未正常结束",
            "percent": 100,
            "updated_at": time.time(),
            "done": True,
        })
        with open(progress_path(workspace_root), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return task_id if isinstance(task_id, str) else ""
    except Exception:  # noqa: BLE001 - 清理失败不得阻塞启动
        return None
