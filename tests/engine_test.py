# -*- coding: utf-8 -*-
"""
AnimaMultiSave 引擎级联调：直接拉起 ComfyUI 自己的 PromptExecutor 跑真实 prompt。

这是最贴近真实使用场景的验证 —— 用的是 ComfyUI 的执行引擎、缓存、校验、UI 事件，
只把「前端静音连线」这一步按前端的做法手工模拟（把节点和连线从 prompt 里去掉）。

跑法（在 ComfyUI 根目录下）：
    <ComfyUI 目录>\\python\\python.exe ^
        custom_nodes\\ComfyUI-Anima-MultiSave\\tests\\engine_test.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.dirname(HERE)
COMFY_ROOT = os.path.abspath(os.path.join(NODE_DIR, "..", ".."))
sys.path.insert(0, COMFY_ROOT)
sys.path.insert(0, os.path.dirname(NODE_DIR))
sys.path.insert(0, NODE_DIR)

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import folder_paths  # noqa: E402
import nodes  # noqa: E402
import execution  # noqa: E402
from PIL import Image  # noqa: E402

import anima_multisave as ams  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [ok] {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


class Server:
    client_id = None

    def __init__(self):
        self.sent = []

    def send_sync(self, event, data, sid=None):
        self.sent.append((event, data))


# 资产登记 / 缩略图这些跟本节点无关，测试里直接旁路（保留原来的 ui 内容）
execution.default_asset_manager = lambda: None  # type: ignore
execution.register_executed_outputs = lambda output_ui, *a, **k: output_ui  # type: ignore

nodes.NODE_CLASS_MAPPINGS["AnimaMultiSave"] = ams.AnimaMultiSave

COLORS = {1: 0xFF0000, 2: 0x00FF00, 3: 0x0000FF}  # 端口 → 颜色
EXPECT = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255)}


def build_prompt(prefix, active_ports, **overrides):
    """按「哪些端口有图」拼一个 prompt；不活跃的端口连节点都不提交（= 前端静音后的样子）。"""
    p = {}
    for port in active_ports:
        p[str(port)] = {
            "class_type": "EmptyImage",
            "inputs": {"width": 64, "height": 40, "batch_size": 1, "color": COLORS[port]},
        }
    inputs = {
        "filename_prefix": prefix,
        "output_mode": "best_only",
        "priority_order": "high_port_first",
        "format": "png",
        "quality": 95,
        "png_compress_level": 4,
        "metadata": "strip_all",
        "overwrite": True,
        "clean_stale": True,
    }
    for port in active_ports:
        inputs[f"images_{port}"] = [str(port), 0]
    inputs.update(overrides)
    p["99"] = {"class_type": "AnimaMultiSave", "inputs": inputs}
    return p


def pixel_of(path):
    with Image.open(path) as im:
        return im.convert("RGB").getpixel((2, 2))


def run(ex, prompt, tag):
    ex.execute(prompt, tag, {"client_id": "selftest"}, ["99"])
    assert ex.success, f"{tag} 执行失败：{ex.status_messages}"
    return ex


def main():
    tmp = tempfile.mkdtemp(prefix="anima_engine_")
    folder_paths.set_output_directory(tmp)
    folder_paths.set_temp_directory(os.path.join(tmp, 'temp'))
    out = os.path.join(tmp, "anima", "result_00001.png")

    try:
        ex = execution.PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})
        print("\n[1] 真实引擎：三路都在 → 存最终图")
        run(ex, build_prompt("anima/result", [1, 2, 3]), "e1")
        check("文件生成", os.path.isfile(out), out)
        check("存的是端口3（蓝）", pixel_of(out) == EXPECT[3], pixel_of(out))
        check("尺寸 64x40", Image.open(out).size == (64, 40), Image.open(out).size)

        print("\n[2] 引擎级：最终级静音（节点+连线都不提交）→ 退到端口2")
        run(ex, build_prompt("anima/result", [1, 2]), "e2")
        check("存的是端口2（绿）", pixel_of(out) == EXPECT[2], pixel_of(out))

        print("\n[3] 引擎级：细化级+最终级都静音 → 退到端口1")
        run(ex, build_prompt("anima/result", [1]), "e3")
        check("存的是端口1（红）", pixel_of(out) == EXPECT[1], pixel_of(out))

        print("\n[4] 引擎级：重新打开最终级（和第 1 轮输入完全一样）→ 必须重新落盘")
        run(ex, build_prompt("anima/result", [1, 2, 3]), "e4")
        check("又被覆盖回端口3（蓝）：缓存没吃掉这次保存",
              pixel_of(out) == EXPECT[3], pixel_of(out))

        print("\n[5] 引擎级：一次都没接线 → 不报错、不产出")
        run(ex, build_prompt("anima/result", []), "e5")
        check("仍然只有 1 个文件", len([f for f in os.listdir(os.path.dirname(out))]) == 1,
              os.listdir(os.path.dirname(out)))

        print("\n[6] 引擎级：元数据仍然是干净的")
        with open(out, "rb") as fh:
            raw = fh.read()
        check("没有 tEXt 块", b"tEXt" not in raw)
        check("没有 workflow 字样", b"workflow" not in raw)
        with Image.open(out) as im:
            check("PIL info 为空", dict(im.info) == {}, dict(im.info))

        print("\n[7] 引擎级：prompt 校验（validate_prompt）能过")
        ok, error, good, bad = asyncio.run(
            execution.validate_prompt("v1", build_prompt("anima/result", [1, 2, 3]), None)
        )
        check("校验通过", ok is True, error)
        check("输出节点被识别", "99" in good, good)

        print("\n[8] 引擎级：静音全部端口时校验也不会报 missing input")
        p = build_prompt("anima/result", [])
        ok, error, good, bad = asyncio.run(execution.validate_prompt("v2", p, None))
        check("校验通过", ok is True, error)

        print("\n[9] 引擎级：batch_size=4 → 4 个文件")
        shutil.rmtree(os.path.join(tmp, "anima"), ignore_errors=True)
        p = build_prompt("anima/result", [1, 2, 3])
        for nid in ("1", "2", "3"):
            p[nid]["inputs"]["batch_size"] = 4
        run(ex, p, "e9")
        got = sorted(os.listdir(os.path.join(tmp, "anima")))
        check("4 帧落盘", got == [f"result_{i:05d}.png" for i in range(1, 5)], got)

        print("\n[10] 引擎级：再跑一次 2 帧 → 多余帧被清理")
        p = build_prompt("anima/result", [1, 2, 3])
        for nid in ("1", "2", "3"):
            p[nid]["inputs"]["batch_size"] = 2
        run(ex, p, "e10")
        got = sorted(os.listdir(os.path.join(tmp, "anima")))
        check("只剩 2 帧", got == ["result_00001.png", "result_00002.png"], got)

        print("\n[11] 引擎级：UI 事件里带回了图片")
        ui = [
            d
            for ev, d in ex.server.sent
            if ev == "executed" and isinstance(d, dict) and d.get("node") == "99"
        ]
        check("收到 executed 事件", len(ui) >= 1, ex.server.sent[-3:])
        if ui:
            images = (ui[-1].get("output") or {}).get("images") or []
            check("事件里 2 张图", len(images) == 2, images)
            check("带 subfolder/filename",
                  images and {"filename", "subfolder", "type"} <= set(images[0]), images[:1])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
