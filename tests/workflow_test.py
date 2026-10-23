# -*- coding: utf-8 -*-
"""
演示工作流测试：把 examples/demo_多路保存.json 当成前端导出的画布，
自己按前端的规则转成 prompt（静音 mode==2 的节点连同连线一起丢掉），
丢给 ComfyUI 真引擎跑 —— 顺便验证「静音哪一级就退到哪一级」这条主链路。

跑法（在 ComfyUI 根目录下）：
    <ComfyUI 目录>\\python\\python.exe ^
        custom_nodes\\ComfyUI-Anima-MultiSave\\tests\\workflow_test.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.dirname(HERE)
COMFY_ROOT = os.path.abspath(os.path.join(NODE_DIR, "..", ".."))
WF_PATH = os.path.join(NODE_DIR, "examples", "demo_多路保存.json")
sys.path.insert(0, COMFY_ROOT)
sys.path.insert(0, os.path.dirname(NODE_DIR))

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


def widget_names(cls):
    """前端规则：required 里能给控件显示的才算 widget（combo / 数字 / 字符串 / 布尔）。"""
    names = []
    for name, spec in cls.INPUT_TYPES().get("required", {}).items():
        itype = spec[0]
        opts = spec[1] if len(spec) > 1 else {}
        if opts.get("forceInput"):
            continue
        if isinstance(itype, list) or itype in ("INT", "FLOAT", "STRING", "BOOLEAN"):
            names.append(name)
    return names


def to_prompt(wf):
    """迷你 graphToPrompt：跳过前端专用节点、跳过被静音(mode==2)的节点和它的连线。"""
    links = {lk[0]: lk for lk in wf.get("links", [])}
    live = {
        n["id"]: n
        for n in wf["nodes"]
        if n.get("mode", 0) != 2 and n.get("type") in comfy_nodes.NODE_CLASS_MAPPINGS
    }
    prompt = {}
    for nid, node in live.items():
        cls = comfy_nodes.NODE_CLASS_MAPPINGS[node["type"]]
        inputs = {}
        for name, val in zip(widget_names(cls), node.get("widgets_values") or []):
            inputs[name] = val
        for slot in node.get("inputs") or []:
            lid = slot.get("link")
            if lid is None or lid not in links:
                continue
            link = links[lid]
            if link[1] not in live:  # 源头被静音 → 这条线不存在
                continue
            inputs[slot["name"]] = [str(link[1]), link[2]]
        prompt[str(nid)] = {"class_type": node["type"], "inputs": inputs}
    return prompt


def outputs_of(wf):
    live = {
        n["id"]: n
        for n in wf["nodes"]
        if n.get("mode", 0) != 2 and n.get("type") in comfy_nodes.NODE_CLASS_MAPPINGS
    }
    out = []
    for nid, node in live.items():
        cls = comfy_nodes.NODE_CLASS_MAPPINGS[node["type"]]
        if getattr(cls, "OUTPUT_NODE", False) is True:
            out.append(str(nid))
    return out


def mute(wf, *ids):
    wf = copy.deepcopy(wf)
    for node in wf["nodes"]:
        if node["id"] in ids:
            node["mode"] = 2
    return wf


def main():
    with open(WF_PATH, encoding="utf-8") as fh:
        wf = json.load(fh)

    print("\n[1] 工作流文件本身是否自洽")
    check("能解析 JSON", isinstance(wf, dict))
    check("有 version 0.4", wf.get("version") == 0.4, wf.get("version"))
    ids = [n["id"] for n in wf["nodes"]]
    check("节点 id 唯一", len(ids) == len(set(ids)), ids)
    save_node = next(n for n in wf["nodes"] if n["type"] == "AnimaMultiSave")
    check("last_node_id 正确", wf["last_node_id"] == max(ids), (wf["last_node_id"], ids))
    check("last_link_id 正确",
          wf["last_link_id"] == max(lk[0] for lk in wf["links"]), wf["last_link_id"])
    for lk in wf["links"]:
        lid, src, sslot, dst, dslot, ltype = lk
        snode = next(n for n in wf["nodes"] if n["id"] == src)
        dnode = next(n for n in wf["nodes"] if n["id"] == dst)
        check(f"连线 {lid} 源插槽存在", snode["outputs"][sslot]["type"] == ltype)
        check(f"连线 {lid} 目标插槽存在",
              dnode["inputs"][dslot]["name"] == f"images_{dslot + 1}", dnode["inputs"][dslot])
        check(f"连线 {lid} 的 link 字段双向一致",
              lid in snode["outputs"][sslot]["links"]
              and dnode["inputs"][dslot]["link"] == lid)

    print("\n[2] 控件值与节点定义对得上")
    asyncio.run(comfy_nodes.load_custom_node(os.path.join("custom_nodes", "ComfyUI-Anima-MultiSave")))
    cls = comfy_nodes.NODE_CLASS_MAPPINGS["AnimaMultiSave"]
    names = widget_names(cls)
    vals = save_node["widgets_values"]
    check("控件数量一致", len(vals) == len(names), (names, vals))
    spec = cls.INPUT_TYPES()["required"]
    for name, val in zip(names, vals):
        itype, opts = spec[name][0], (spec[name][1] if len(spec[name]) > 1 else {})
        if isinstance(itype, list):
            check(f"{name} 值 {val!r} 合法", val in itype, itype)
        elif itype == "INT":
            check(f"{name} 值 {val!r} 在范围内",
                  opts.get("min", 0) <= val <= opts.get("max", 0), opts)

    print("\n[3] 真引擎跑演示工作流")
    tmp = tempfile.mkdtemp(prefix="anima_wf_")
    folder_paths.set_output_directory(tmp)
    folder_paths.set_temp_directory(os.path.join(tmp, 'temp'))
    out = os.path.join(tmp, "anima", "demo_00001.png")

    def run(workflow, tag):
        p = to_prompt(workflow)
        ex = execution.PromptExecutor(Server(), cache_type=False, cache_args={"ram": 0, "ram_inactive": 0})
        ex.execute(p, tag, {"client_id": "wf-test"}, outputs_of(workflow))
        assert ex.success, f"{tag} 失败: {ex.status_messages}"
        return ex

    def px():
        with Image.open(out) as im:
            return im.convert("RGB").getpixel((4, 4))

    try:
        run(wf, "wf1")
        check("三路都在 → 蓝色（端口③）", px() == (0, 0, 255), px())
        check("文件名固定 demo_00001.png",
              sorted(os.listdir(os.path.dirname(out))) == ["demo_00001.png"],
              os.listdir(os.path.dirname(out)))

        run(mute(wf, 3), "wf2")
        check("静音③ → 绿色（端口②）", px() == (0, 255, 0), px())

        run(mute(wf, 2, 3), "wf3")
        check("静音②③ → 红色（端口①）", px() == (255, 0, 0), px())

        run(wf, "wf4")
        check("全部恢复 → 又变蓝", px() == (0, 0, 255), px())
        check("始终只有 1 个文件",
              sorted(os.listdir(os.path.dirname(out))) == ["demo_00001.png"],
              os.listdir(os.path.dirname(out)))

        print("\n[4] 整个工作流里的图都没元数据")
        with open(out, "rb") as fh:
            raw = fh.read()
        check("没有 tEXt 块", b"tEXt" not in raw)
        check("没有 EmptyImage 之类的 prompt 痕迹", b"EmptyImage" not in raw)
        with Image.open(out) as im:
            check("info 为空", dict(im.info) == {}, dict(im.info))

        print("\n[5] 演示工作流的 prompt 能通过校验（前端队列化时会先校验）")
        ok, error, good, bad = asyncio.run(execution.validate_prompt("x", to_prompt(wf), None))
        check("校验通过", ok is True, error)
        check("只剩保存节点是输出节点", list(good) == ["4"], good)
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
