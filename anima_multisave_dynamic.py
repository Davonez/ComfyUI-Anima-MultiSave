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
            display_name="💾 Anima 多路保存 (动态端口)",
            category="AnimaArt/save",
            description=(
                "多路保存：IMAGE 输入可以一直点 + 增加，自动挑「这次跑到最远（端口号最大）"
                "的那一路」落盘，文件名固定覆盖，可清旧帧、可去除元数据。"
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
                    tooltip="输出前缀，可带子目录（anima/result → output/anima/），也支持 %date% %width% 等变量。",
                ),
                IO.Combo.Input(
                    "output_mode",
                    options=OUTPUT_MODES,
                    default="best_only",
                    tooltip="best_only：只存优先级最高的那一路（推荐）。all_inputs：每路都存一份，文件名带 _pN。",
                ),
                IO.Combo.Input(
                    "priority_order",
                    options=PRIORITY_ORDERS,
                    default="high_port_first",
                    tooltip="high_port_first：端口号大的优先（最终图接大号口）。low_port_first：反过来。",
                ),
                IO.Combo.Input("format", options=FORMATS, default="png",
                               tooltip="png 无损；jpg / webp 有损但体积小。"),
                IO.Int.Input("quality", default=95, min=1, max=100,
                             tooltip="jpg / webp 质量，png 不用。"),
                IO.Int.Input("png_compress_level", default=4, min=0, max=9, advanced=True,
                             tooltip="PNG 压缩等级，只影响体积和速度，不影响画质。"),
                IO.Combo.Input("metadata", options=METADATA_MODES, default="strip_all",
                               tooltip="strip_all：清掉 prompt / workflow / EXIF 等元数据。embed_workflow：像原生 SaveImage 一样写回工作流（仅 png）。"),
                IO.Boolean.Input("overwrite", default=False,
                                 tooltip="False（推荐）：每轮新建文件，历史缩略图各是各的。True：始终覆盖同一组文件名（固定路径永远是最新图），节点会额外往 temp 写唯一命名的预览副本，避免历史缩略图全变成最新那张。"),
                IO.Boolean.Input("clean_stale", default=True,
                                 tooltip="只在 overwrite=True 时生效：顺手删掉同前缀下这次没产出的旧帧。"),
                IO.Autogrow.Input(
                    "images",
                    template=template,
                    optional=True,   # 一路都不接也不报错：静音掉全部流程时优雅退出
                    tooltip="把 IMAGE 接进来，接多少都行：点输入下面的 + 继续加口。端口序号越大优先级越高。",
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
    "AnimaMultiSaveDynamic": "💾 Anima 多路保存 (动态端口·可无限加)"
}
