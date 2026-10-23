# -*- coding: utf-8 -*-
"""ComfyUI-Anima-MultiSave：多端口自动保存 + 元数据消除。

装了两个节点：
  * AnimaMultiSave         固定 12 路（老版 V1 节点，任何 ComfyUI 版本都能跑）
  * AnimaMultiSaveDynamic  动态端口（V3 节点，输入可以点 + 无限增加）
"""
import logging

from .anima_multisave import (  # noqa: F401
    AnimaMultiSave,
    NODE_CLASS_MAPPINGS as _CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _NAMES,
)

NODE_CLASS_MAPPINGS = dict(_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS = dict(_NAMES)

try:
    from .anima_multisave_dynamic import (  # noqa: F401
        AnimaMultiSaveDynamic,
        NODE_CLASS_MAPPINGS as _CLASSES2,
        NODE_DISPLAY_NAME_MAPPINGS as _NAMES2,
    )

    NODE_CLASS_MAPPINGS.update(_CLASSES2)
    NODE_DISPLAY_NAME_MAPPINGS.update(_NAMES2)
except Exception as exc:  # 老版本 ComfyUI 没有 comfy_api.latest，就只装固定端口版
    logging.warning(
        "[Anima 多路保存] 动态端口版没装上（%s），固定 12 路版不受影响。", exc
    )

__all__ = [
    "AnimaMultiSave",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]
