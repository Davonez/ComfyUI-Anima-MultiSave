# -*- coding: utf-8 -*-
"""
Anima 多路保存 · 动态端口版
================================================================
同一个保存节点，但 IMAGE 输入是 **Autogrow（可动态增加）** 的：
节点上先给 3 个口，接更多就点输入下面的 `+`，想接几路接几路（上限 40，
ComfyUI 自己 Autogrow 的上限是 100）。挑图 / 落盘 / 清旧帧 / 去元数据
全部复用 `anima_multisave.save_ports`，和固定 12 路那个节点行为完全一致。

端口优先级 = 端口序号（`image_0` < `image_1` < `image_2` …），大的优先，
也就是「越靠后接的（越靠近最终结果）越优先」。
"""

from __future__ import annotations

import logging
from typing import Any

import torch

from comfy_api.latest import IO, UI

try:  # 作为包的一部分被 ComfyUI 加载
    from .anima_multisave import (
        FORMATS,
        METADATA_MODES,
        OUTPUT_MODES,
        PRIORITY_ORDERS,
        as_image_batch,
        save_ports,
    )
except ImportError:  # 被单独 import（跑单测时）
    from anima_multisave import (  # type: ignore
        FORMATS,
        METADATA_MODES,
        OUTPUT_MODES,
        PRIORITY_ORDERS,
        as_image_batch,
        save_ports,
    )

LOG = logging.getLogger("AnimaMultiSave")

IMAGE_PREFIX = "image_"
MIN_PORTS = 3       # 一开始显示几个口（不够就点 +）
MAX_PORTS = 40      # 上限（ComfyUI Autogrow 硬上限是 100）


def _hidden(cls, name: str) -> Any:
    """安全读取 hidden 输入（直接调用 execute 做单测时没有 hidden）。"""
    hidden = getattr(cls, "hidden", None)
    return getattr(hidden, name, None) if hidden is not None else None


def _slot_of(name: str, fallback: int) -> int:
    """`image_7` → 7；解析不出来就按出现顺序当序号。"""
    tail = str(name).rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else fallback


class AnimaMultiSaveDynamic(IO.ComfyNode):
    """动态端口版多路保存"""

    @classmethod
    def define_schema(cls) -> IO.Schema:
        template = IO.Autogrow.TemplatePrefix(
            # 模板输入必须 optional：否则前 min 个口会被当成「必填」，
            # 少接一路就报 required input missing，静音流程时会直接失败。
            IO.Image.Input("image", optional=True),
            prefix=IMAGE_PREFIX,
            min=MIN_PORTS,
            max=MAX_PORTS,
        )
        return IO.Schema(
            node_id="AnimaMultiSaveDynamic",
            display_name="💾 Anima Multi-Save (Dynamic Ports)",
            category="AnimaArt/save",
            description=(
                "Multi-port save with growable IMAGE inputs: click + to add as many ports as "
                "needed, and the node saves whichever port ran furthest (highest port number). "
                "Optional fixed filenames, stale-frame cleanup and metadata stripping."
            ),
            search_aliases=[
                "anima save",
                "multi save",
                "dynamic save",
                "多路保存",
                "元数据消除",
                "save without metadata",
            ],
            inputs=[
                IO.String.Input(
                    "filename_prefix",
                    default="anima/result",
                    tooltip="Output prefix. Subfolders are allowed (anima/result -> output/anima/); "
                    "variables such as %date% / %width% work too.",
                ),
                IO.Combo.Input(
                    "output_mode",
                    options=OUTPUT_MODES,
                    default="best_only",
                    tooltip="best_only: save only the highest-priority port (recommended). "
                    "all_inputs: save every port, each with a _pN suffix.",
                ),
                IO.Combo.Input(
                    "priority_order",
                    options=PRIORITY_ORDERS,
                    default="high_port_first",
                    tooltip="high_port_first: higher port numbers win (wire the final image to a "
                    "high port). low_port_first: the opposite.",
                ),
                IO.Combo.Input("format", options=FORMATS, default="png",
                               tooltip="png is lossless; jpg / webp are lossy but much smaller."),
                IO.Int.Input("quality", default=95, min=1, max=100,
                             tooltip="Quality for jpg / webp. Ignored for png."),
                IO.Int.Input("png_compress_level", default=4, min=0, max=9, advanced=True,
                             tooltip="PNG compression level. Affects file size and save speed only, "
                             "not image quality."),
                IO.Combo.Input("metadata", options=METADATA_MODES, default="strip_all",
                               tooltip="strip_all: strip prompt / workflow / EXIF metadata. "
                               "embed_workflow: embed the workflow like the native SaveImage node "
                               "(png only)."),
                IO.Boolean.Input("overwrite", default=False,
                                 tooltip="False (recommended): create a new file each run so "
                                 "history thumbnails stay distinct. True: always overwrite the same "
                                 "filenames (the fixed path is always the latest image); the node "
                                 "then writes a uniquely named preview copy under temp/ so history "
                                 "does not collapse onto the latest image."),
                IO.Boolean.Input("clean_stale", default=True,
                                 tooltip="Only used when overwrite=True: also remove stale frames "
                                 "from the same prefix that this run did not produce."),
                IO.Autogrow.Input(
                    "images",
                    template=template,
                    optional=True,   # 一路都不接也不报错：静音掉全部流程时优雅退出
                    tooltip="Wire IMAGE outputs here; add as many as you like with the + button. "
                    "Higher port numbers have higher priority.",
                ),
            ],
            outputs=[
                IO.Image.Output(display_name="image"),
                IO.String.Output(display_name="info"),
            ],
            hidden=[IO.Hidden.prompt, IO.Hidden.extra_pnginfo, IO.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs) -> float:
        # 等价于 V1 的 IS_CHANGED：永远重新执行，保证「关掉再打开某一级」时也能重新落盘。
        return float("NaN")

    @classmethod
    def execute(
        cls,
        images: IO.Autogrow.Type,
        filename_prefix: str = "anima/result",
        output_mode: str = "best_only",
        priority_order: str = "high_port_first",
        format: str = "png",
        quality: int = 95,
        png_compress_level: int = 4,
        metadata: str = "strip_all",
        overwrite: bool = True,
        clean_stale: bool = True,
    ) -> IO.NodeOutput:
        ports: list[tuple[int, torch.Tensor]] = []
        for fallback, (name, value) in enumerate((images or {}).items()):
            batch = as_image_batch(value)
            if batch is None:
                continue
            ports.append((_slot_of(name, fallback), batch))

        image, info, results = save_ports(
            ports,
            filename_prefix=filename_prefix,
            output_mode=output_mode,
            priority_order=priority_order,
            fmt=format,
            quality=quality,
            png_compress_level=png_compress_level,
            metadata=metadata,
            overwrite=overwrite,
            clean_stale=clean_stale,
            prompt=_hidden(cls, "prompt"),
            extra_pnginfo=_hidden(cls, "extra_pnginfo"),
            unique_id=_hidden(cls, "unique_id"),
        )

        ui_images = [
            UI.SavedResult(
                filename=r["filename"],
                subfolder=r["subfolder"],
                # 覆盖模式下 save_ports 交回来的是 temp 预览副本，别硬写成 output，
                # 否则前端又会去 output/ 找那个同名文件（历史还是串图）。
                type=IO.FolderType.temp if r["type"] == "temp" else IO.FolderType.output,
            )
            for r in results
        ]
        return IO.NodeOutput(image, info, ui={"images": ui_images})


NODE_CLASS_MAPPINGS = {"AnimaMultiSaveDynamic": AnimaMultiSaveDynamic}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaMultiSaveDynamic": "💾 Anima Multi-Save (Dynamic Ports)"
}
