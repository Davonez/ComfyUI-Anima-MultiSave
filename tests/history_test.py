# -*- coding: utf-8 -*-
"""
回归测试：**历史/队列缩略图全变成最新那张** 那个 bug。

起因：ComfyUI 新版资产库按「输出文件路径」认图，源码里明确写着
      producers that write the same output path are not coalesced (unsupported)
所以只要反复重写同一个 output 文件名，前端历史里每一条缩略图都会显示成最新图。

修法：
  * 默认 overwrite=False → 每轮新文件名（和原生 SaveImage 一样）；
  * 用户坚持 overwrite=True（固定文件名）时，节点额外往 temp/ 写一份**每轮唯一**的
    预览副本，并把 ui.images 指向它 —— output 里仍是固定文件名，但历史不再串。

这个脚本直接看「前端每轮到底收到了什么」，两个节点都测。
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

COLORS = [0xFF0000, 0x00FF00, 0x0000FF]


def prompt(kind, overwrite, prefix, color):
    node = "empty"
    p = {node: {"class_type": "EmptyImage",
                "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": color}}}
    if kind == "fixed":
        p["99"] = {"class_type": "AnimaMultiSave",
                   "inputs": {"filename_prefix": prefix, "overwrite": overwrite,
                              "images_1": [node, 0]}}
    else:
        p["99"] = {"class_type": "AnimaMultiSaveDynamic",
                   "inputs": {"filename_prefix": prefix, "overwrite": overwrite,
                              "images.image_0": [node, 0]}}
    return p


def run_series(ex, kind, overwrite, prefix, tmp):
    """跑 3 轮不同颜色，返回每轮前端收到的 images 列表。"""
    got = []
    for i, color in enumerate(COLORS):
        ex.server.sent.clear()
        ex.execute(prompt(kind, overwrite, prefix, color), f"{kind}-{overwrite}-{i}",
                   {"client_id": "hist"}, ["99"])
        assert ex.success, ex.status_messages[-3:]
        events = [d for ev, d in ex.server.sent
                  if ev == "executed" and isinstance(d, dict) and d.get("node") == "99"]
        assert events, ex.status_messages[-3:]
        got.append((events[-1].get("output") or {}).get("images") or [])
    return got


def abs_of(entry, tmp):
    base = os.path.join(tmp, "temp" if entry["type"] == "temp" else "output")
    return os.path.join(base, entry["subfolder"], entry["filename"])


def count_out(tmp, prefix):
    """output 根目录下以 prefix 开头的文件数（前缀不带子目录）。"""
    root = os.path.join(tmp, "output")
    return sorted(f for f in os.listdir(root) if f.startswith(prefix))


def main():
    tmp = tempfile.mkdtemp(prefix="anima_hist_")
    folder_paths.set_output_directory(os.path.join(tmp, "output"))
    folder_paths.set_temp_directory(os.path.join(tmp, "temp"))
    os.makedirs(os.path.join(tmp, "output"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "temp"), exist_ok=True)

    asyncio.run(comfy_nodes.load_custom_node(os.path.join("custom_nodes", "ComfyUI-Anima-MultiSave")))
    ex = execution.PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})

    try:
        for kind, label in (("dynamic", "动态端口版"), ("fixed", "固定 12 路版")):
            print(f"\n[1] {label} · overwrite=True（固定文件名）")
            got = run_series(ex, kind, True, f"{kind}_stable", tmp)
            names = [g[0]["filename"] for g in got]
            types = {g[0]["type"] for g in got}
            stable = count_out(tmp, kind + "_stable")
            check("输出文件始终是同一个", len(stable) == 1, stable)
            check("前端拿到的 3 个预览文件名互不相同", len(set(names)) == 3, names)
            check("预览都是 temp 类型", types == {"temp"}, types)
            px = []
            for g in got:
                with Image.open(abs_of(g[0], tmp)) as im:
                    px.append(im.convert("RGB").getpixel((1, 1)))
            check("3 张预览内容各不相同（= 历史不会全变最新）",
                  px == [(255, 0, 0), (0, 255, 0), (0, 0, 255)], px)

            print(f"\n[2] {label} · overwrite=False（默认，每轮新文件）")
            got = run_series(ex, kind, False, f"{kind}_unique", tmp)
            names = [g[0]["filename"] for g in got]
            check("3 轮 3 个不同文件名", len(set(names)) == 3, names)
            check("都是 output 类型", {g[0]["type"] for g in got} == {"output"},
                  {g[0]["type"] for g in got})
            uniq = count_out(tmp, kind + "_unique")
            check("3 个文件都在磁盘上", len(uniq) == 3, uniq)
            px = []
            for g in got:
                with Image.open(abs_of(g[0], tmp)) as im:
                    px.append(im.convert("RGB").getpixel((1, 1)))
            check("3 个文件内容各不相同", px == [(255, 0, 0), (0, 255, 0), (0, 0, 255)], px)

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
