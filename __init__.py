# -*- coding: utf-8 -*-
"""ComfyUI-Anima-MultiSave: multi-port auto save + metadata stripping.

Two nodes are installed:
  * AnimaMultiSave         fixed 12 ports (V1 node, loads on any ComfyUI version)
  * AnimaMultiSaveDynamic  dynamic ports (V3 node, add inputs with the + button)
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
        "[Anima Multi-Save] the dynamic-port node failed to load (%s); the fixed "
        "12-port node is unaffected.",
        exc,
    )

__all__ = [
    "AnimaMultiSave",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]
