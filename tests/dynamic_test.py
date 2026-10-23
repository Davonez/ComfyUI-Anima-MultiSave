# -*- coding: utf-8 -*-
"""
动态端口版（AnimaMultiSaveDynamic）测试：走 ComfyUI 真加载器 + 真引擎。

重点验证三件事：
  1. Autogrow 输入在 /object_info 里长什么样（前端靠 prefix/min/max 画 + 号）；
  2. prompt 里平铺 image_0…image_N 能被后台正确收成 dict 传给 execute；
  3. 挑图优先级、覆盖、元数据清理跟固定端口版完全一致。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.dirname(HERE)
COMFY_ROOT = os.path.abspath(os.path.join(NODE_DIR, "..", ".."))
sys.path.insert(0, COMFY_ROOT)
sys.path.insert(0, os.path.dirname(NODE_DIR))
sys.path.insert(0, NODE_DIR)

import torch  # noqa: E402
import folder_paths  # noqa: E402
import nodes as comfy_nodes  # noqa: E402
import execution  # noqa: E402
from PIL import Image  # noqa: E402

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


execution.default_asset_manager = lambda: None  # type: ignore
execution.register_executed_outputs = lambda output_ui, *a, **k: output_ui  # type: ignore

COLORS = {i: c for i, c in enumerate([0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF])}
EXPECT = {0: (255, 0, 0), 1: (0, 255, 0), 2: (0, 0, 255), 3: (255, 255, 0), 4: (255, 0, 255)}


def prompt_with(prefix, live_slots, **overrides):
    """live_slots = 要接图的端口序号（0 基，和 image_N 对应）。"""
    p = {}
    for slot in live_slots:
        p[str(slot)] = {
            "class_type": "EmptyImage",
            "inputs": {"width": 64, "height": 48, "batch_size": 1, "color": COLORS.get(slot, 0x808080)},
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
    for slot in live_slots:
        inputs[f"images.image_{slot}"] = [str(slot), 0]   # Autogrow 在 prompt 里是点号命名
    inputs.update(overrides)
    p["99"] = {"class_type": "AnimaMultiSaveDynamic", "inputs": inputs}
    return p


def pixel(path):
    with Image.open(path) as im:
        return im.convert("RGB").getpixel((3, 3)), im.size


def chunks(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    out, pos = [], 8
    while pos + 12 <= len(raw):
        n = int.from_bytes(raw[pos:pos + 4], "big")
        c = raw[pos + 4:pos + 8].decode("latin-1")
        out.append(c)
        pos += 12 + n
        if c == "IEND":
            break
    return out, raw


def main():
    print("\n[1] 加载器把两个节点都装上了")
    ok = asyncio.run(comfy_nodes.load_custom_node(os.path.join("custom_nodes", "ComfyUI-Anima-MultiSave")))
    check("load_custom_node 返回 True", ok is True, ok)
    check("固定端口版在", "AnimaMultiSave" in comfy_nodes.NODE_CLASS_MAPPINGS)
    check("动态端口版在", "AnimaMultiSaveDynamic" in comfy_nodes.NODE_CLASS_MAPPINGS,
          "comfy_api.latest 缺失或 schema 校验失败")
    cls = comfy_nodes.NODE_CLASS_MAPPINGS["AnimaMultiSaveDynamic"]

    print("\n[2] schema：Autogrow 那部分前端要靠它画 + 号")
    info = cls.GET_NODE_INFO_V1()
    blob = json.dumps(info, ensure_ascii=False)
    check("整份信息可序列化", isinstance(blob, str) and len(blob) > 100)
    optional = info["input"].get("optional", {})
    agg = optional.get("images")
    check("有聚合输入 images", agg is not None, list(optional))
    check("类型是 COMFY_AUTOGROW_V3", agg and agg[0] == "COMFY_AUTOGROW_V3", agg and agg[0])
    tpl = (agg[1] or {}).get("template") or {}
    check("template 带 prefix=image_", tpl.get("prefix") == "image_", tpl)
    check("template min/max 合理", tpl.get("min") == 3 and tpl.get("max") == 40, tpl)
    check("is_output_node", info.get("output_node") is True)
    check("两个输出 image / info", list(info.get("output_name")) == ["image", "info"], info.get("output_name"))

    print("\n[3] 真引擎：接 3 路 → 存最后一号口")
    tmp = tempfile.mkdtemp(prefix="anima_dyn_")
    folder_paths.set_output_directory(tmp)
    folder_paths.set_temp_directory(os.path.join(tmp, 'temp'))
    out = os.path.join(tmp, "dyn", "r_00001.png")
    ex = execution.PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})

    def run(p, tag):
        ex.execute(p, tag, {"client_id": "dyn"}, ["99"])
        assert ex.success, f"{tag} 失败: {ex.status_messages}"

    try:
        run(prompt_with("dyn/r", [0, 1, 2]), "d1")
        px, size = pixel(out)
        check("存的是 image_2（蓝）", px == EXPECT[2], px)
        check("尺寸 64x48", size == (64, 48), size)

        print("\n[4] 真引擎：少接一路（模拟静音最终级）→ 自动退到 image_1")
        run(prompt_with("dyn/r", [0, 1]), "d2")
        px, _ = pixel(out)
        check("退到 image_1（绿）", px == EXPECT[1], px)

        print("\n[5] 真引擎：接到第 5 路（image_4）→ 证明「可以比 3 路更多」")
        run(prompt_with("dyn/r", [0, 1, 2, 3, 4]), "d3")
        px, _ = pixel(out)
        check("存的是 image_4（品红）", px == EXPECT[4], px)
        check("文件仍只有一个", sorted(os.listdir(os.path.dirname(out))) == ["r_00001.png"],
              os.listdir(os.path.dirname(out)))

        print("\n[6] 接到第 9 路（超过原来固定 8 路上限）")
        p = prompt_with("dyn/r", [0, 8])
        p["8"]["inputs"]["color"] = 0x00FFFF
        run(p, "d4")
        px, _ = pixel(out)
        check("image_8 生效（青）", px == (0, 255, 255), px)

        print("\n[7] 元数据")
        ch, raw = chunks(out)
        check("PNG 只有 IHDR/IDAT/IEND", ch == ["IHDR", "IDAT", "IEND"], ch)
        check("搜不到 workflow", b"workflow" not in raw)
        with Image.open(out) as im:
            check("PIL info 为空", dict(im.info) == {}, dict(im.info))

        print("\n[8] 全断开：不报错、不落盘")
        before = sorted(os.listdir(os.path.dirname(out)))
        run(prompt_with("dyn/r", []), "d5")
        check("没有新增文件", sorted(os.listdir(os.path.dirname(out))) == before,
              os.listdir(os.path.dirname(out)))

        print("\n[9] all_inputs：每路一份，带 _pN 后缀")
        for f in os.listdir(os.path.dirname(out)):
            os.remove(os.path.join(os.path.dirname(out), f))
        run(prompt_with("dyn/r", [0, 1, 2], output_mode="all_inputs"), "d6")
        got = sorted(os.listdir(os.path.dirname(out)))
        check("三路各一份", got == ["r_p0_00001.png", "r_p1_00001.png", "r_p2_00001.png"], got)

        print("\n[10] prompt 校验（前端排队前会跑）")
        for slots in ([0], [0, 1, 2], [0, 1, 2, 3, 4, 5, 6, 7]):
            p = prompt_with("dyn/r", slots)
            ok2, error, good, bad = asyncio.run(execution.validate_prompt("v", p, None))
            check(f"{len(slots)} 路校验通过", ok2 is True, error)

        print("\n[11] UI 事件里带回了图片")
        ui = [d for ev, d in ex.server.sent
              if ev == "executed" and isinstance(d, dict) and d.get("node") == "99"]
        check("收到 executed 事件", len(ui) >= 1, ex.server.sent[-2:])
        if ui:
            imgs = (ui[-1].get("output") or {}).get("images") or []
            check("事件里带 filename/subfolder/type",
                  imgs and {"filename", "subfolder", "type"} <= set(imgs[0]), imgs[:1])

        print("\n[12] all_inputs：从 5 路缩到 2 路 → 多出来的那几路旧文件被清")
        d = os.path.dirname(out)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        run(prompt_with("dyn/r", [0, 1, 2, 3, 4], output_mode="all_inputs"), "d7")
        before = sorted(os.listdir(d))
        check("先有 5 份", len(before) == 5, before)
        run(prompt_with("dyn/r", [0, 1], output_mode="all_inputs"), "d8")
        after = sorted(os.listdir(d))
        check("缩到 2 份，p2/p3/p4 被清掉",
              after == ["r_p0_00001.png", "r_p1_00001.png"], after)

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
