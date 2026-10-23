# -*- coding: utf-8 -*-
"""
AnimaMultiSave 加载测试：走 ComfyUI 真正的自定义节点加载器，并复刻 /object_info 的输出结构。

相当于验证「ComfyUI 启动时能不能把这个包装上、右键菜单里长什么样、前端拿到的 JSON 合不合法」。

跑法（在 ComfyUI 根目录下）：
    <ComfyUI 目录>\\python\\python.exe ^
        custom_nodes\\ComfyUI-Anima-MultiSave\\tests\\loader_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.dirname(HERE)
COMFY_ROOT = os.path.abspath(os.path.join(NODE_DIR, "..", ".."))
sys.path.insert(0, COMFY_ROOT)
sys.path.insert(0, os.path.dirname(NODE_DIR))

import folder_paths  # noqa: E402
import nodes as comfy_nodes  # noqa: E402
import execution  # noqa: E402
from comfy_execution.graph import get_input_info  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [ok] {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


def node_info(node_class):
    """server.py 里 node_info() 的等价实现（只保留和本节点相关的分支）。"""
    obj_class = comfy_nodes.NODE_CLASS_MAPPINGS[node_class]
    info = {}
    info["input"] = obj_class.INPUT_TYPES()
    info["input_order"] = {
        key: list(value.keys()) for (key, value) in obj_class.INPUT_TYPES().items()
    }
    info["is_input_list"] = getattr(obj_class, "INPUT_IS_LIST", False)
    info["output"] = obj_class.RETURN_TYPES
    info["output_is_list"] = getattr(
        obj_class, "OUTPUT_IS_LIST", [False] * len(obj_class.RETURN_TYPES)
    )
    info["output_name"] = getattr(obj_class, "RETURN_NAMES", info["output"])
    info["name"] = node_class
    info["display_name"] = comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.get(node_class, node_class)
    info["description"] = getattr(obj_class, "DESCRIPTION", "")
    info["python_module"] = getattr(obj_class, "RELATIVE_PYTHON_MODULE", "nodes")
    info["category"] = getattr(obj_class, "CATEGORY", "sd")
    info["output_node"] = bool(getattr(obj_class, "OUTPUT_NODE", False))
    info["search_aliases"] = getattr(obj_class, "SEARCH_ALIASES", [])
    return info


def main():
    print("\n[1] 用 ComfyUI 原生加载器加载")
    ok = asyncio.run(
        comfy_nodes.load_custom_node(os.path.join("custom_nodes", "ComfyUI-Anima-MultiSave"))
    )
    check("load_custom_node 返回 True", ok is True, ok)
    check("节点已注册", "AnimaMultiSave" in comfy_nodes.NODE_CLASS_MAPPINGS)
    check("有中文显示名", "AnimaMultiSave" in comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS)

    cls = comfy_nodes.NODE_CLASS_MAPPINGS["AnimaMultiSave"]

    print("\n[2] /object_info 结构（前端就是靠它画节点的）")
    info = node_info("AnimaMultiSave")
    try:
        blob = json.dumps(info, ensure_ascii=False)
        dumped = True
    except TypeError as exc:
        blob, dumped = "", False
        print("   ", exc)
    check("信息可 JSON 序列化", dumped)
    check("output = (IMAGE, STRING)", info["output"] == ("IMAGE", "STRING"), info["output"])
    check("output_name = (image, info)", info["output_name"] == ("image", "info"))
    check("是输出节点（会进执行队列）", info["output_node"] is True)
    check("分类是 AnimaArt/save", info["category"] == "AnimaArt/save", info["category"])
    check("有 12 个 IMAGE 可选输入",
          len([k for k in info["input"].get("optional", {}) if k.startswith("images_")]) == 12)

    print("\n[3] 每个输入都能被 ComfyUI 解析")
    all_names = []
    for group in ("required", "optional", "hidden"):
        for name in info["input"].get(group, {}):
            all_names.append(name)
            itype, icat, iextra = get_input_info(cls, name, info["input"])
            check(f"{name}: {group} / {itype}", icat == group and iextra is not None,
                  (itype, icat, iextra))
    check("输入名唯一", len(all_names) == len(set(all_names)), all_names)

    print("\n[4] 控件顺序（节点上从左到右的显示顺序）")
    order = info["input_order"]["required"]
    check("前缀在最前", order[0] == "filename_prefix", order)
    check("12 个端口在 optional 里",
          info["input_order"]["optional"] == [f"images_{i}" for i in range(1, 13)],
          info["input_order"]["optional"])

    print("\n[5] 默认值合法（下拉框默认值必须在候选里）")
    req = info["input"]["required"]
    for name, spec in req.items():
        itype, extra = spec[0], (spec[1] if len(spec) > 1 else {})
        if isinstance(itype, list):
            default = extra.get("default", itype[0])
            check(f"{name} 默认值 {default!r} 在候选内", default in itype, itype)
        elif itype == "INT":
            check(f"{name} 默认值在 min/max 内",
                  extra["min"] <= extra.get("default", 0) <= extra["max"], extra)
        elif itype == "BOOLEAN":
            check(f"{name} 默认值是 bool", isinstance(extra.get("default"), bool), extra)

    print("\n[6] IS_CHANGED 能被引擎用 kwargs 调用（引擎就是 getattr(cls,'IS_CHANGED')(**inputs)）")
    raw = getattr(cls, "IS_CHANGED")(filename_prefix="x", images_1=None, prompt=None)
    check("返回 NaN（每次重跑）", raw != raw, raw)

    print("\n[7] 12 个端口全接上时 prompt 校验通过")
    prompt = {}
    inputs = {}
    for name, spec in req.items():
        itype = spec[0]
        extra = spec[1] if len(spec) > 1 else {}
        if isinstance(itype, list):
            inputs[name] = extra.get("default", itype[0])
        elif itype == "INT":
            inputs[name] = extra.get("default", 0)
        elif itype == "BOOLEAN":
            inputs[name] = extra.get("default", False)
        else:
            inputs[name] = extra.get("default", "")
    for i in range(1, 13):
        prompt[str(i)] = {
            "class_type": "EmptyImage",
            "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": i},
        }
        inputs[f"images_{i}"] = [str(i), 0]
    prompt["99"] = {"class_type": "AnimaMultiSave", "inputs": inputs}
    ok, error, good, bad = asyncio.run(execution.validate_prompt("t", prompt, None))
    check("校验通过", ok is True, error)
    check("被认成输出节点", "99" in good, good)

    print("\n[8] 缺 required 时应当报错（确认 required 真的在生效）")
    broken = json.loads(json.dumps(prompt))
    broken["99"]["inputs"].pop("filename_prefix")
    ok, error, good, bad = asyncio.run(execution.validate_prompt("t2", broken, None))
    check("缺 filename_prefix 被拦下", ok is False, ok)
    check("错误信息指向该输入",
          "filename_prefix" in json.dumps(error, ensure_ascii=False), error)

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
