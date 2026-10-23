# -*- coding: utf-8 -*-
"""
Anima 多路保存 / Anima Multi-Slot Save
================================================================
给 Anima（anima-art / AnimaArt 系）工作流用的「多端口自动落盘」保存节点。

两个版本，同一个内核
-------------------
* `AnimaMultiSave`        固定 12 路。老版 V1 节点，任何 ComfyUI 版本都能加载。
* `AnimaMultiSaveDynamic` 动态端口版（V3 / Autogrow）：先给 3 个口，接不够就点输入上的
                          `+` 继续加，最多 40。以后还要接更多路就用这个。
挑图 / 落盘 / 清旧帧 / 去元数据全在 `save_ports()` 里，两个节点行为完全一致。

解决两个痛点
------------
1. **一个保存节点吃多路图，自动挑「跑得最远的那张」**
   比如：KSampler 1 → 端口1，KSampler 2（细化）→ 端口2，最终放大 / 后处理 → 端口3。
   哪一路被静音（mute / Ctrl+M）或者被旁路（bypass / Ctrl+B），那一路在提交给后端时
   整条连线就没了 —— 本节点收到的就是 None，于是自动往下一级退：
     * 三级都亮 → 存端口3（最终图）
     * 最终级灭了 → 存端口2
     * 最终级 + 细化级都灭 → 存端口1
   overwrite=True 时每次运行都用**同一组文件名**，永远只有一份最新结果、不会堆历史图；
   默认 overwrite=False 则每轮另起编号（result_00001.png、result_00002.png…），和原生
   SaveImage 一样方便翻历史 —— 两种取舍见 README「两种命名方式」。

2. **元数据消除**
   ComfyUI 原生 SaveImage 会把整条工作流塞进 PNG 的 tEXt（prompt / workflow），
   图一发出去就等于把提示词、模型、LoRA 全送人。本节点默认 `metadata=strip_all`：
   * 保存时不写入任何 PNG 文本块；
   * 写出后再按 PNG chunk 结构**逐块过滤** tEXt / zTXt / iTXt / eXIf / tIME 等元数据；
   * 最后重新打开文件校验一遍，万一还有残留会在控制台报警（不会静默放过）。
   想保留工作流就切 `metadata=embed_workflow`（和原生 SaveImage 一样写 prompt/workflow）。

参数速查
--------
filename_prefix     输出前缀，支持子目录（`anima/result` → output/anima/）和 ComfyUI 的
                    %date% / %width% 之类变量。
output_mode         best_only   只存「优先级最高的那一路」（推荐）
                    all_inputs  每一路都存一份，各自带 `_p<端口号>` 后缀
priority_order      端口号大的优先（high_port_first）/ 端口号小的优先（low_port_first）
format              png / jpg / webp
quality             jpg / webp 的质量（1-100）
png_compress_level  PNG 压缩等级 0-9（只影响体积和速度，不影响画质）
metadata            strip_all 清干净 / embed_workflow 写回工作流
overwrite           True = 每次覆盖同一组文件名；False = 用 ComfyUI 的自增编号
clean_stale         True = 顺手删掉同前缀下上次多出来、这次没产出的旧帧
                    （只删 `前缀[_pN]_00001.png` 这种严格匹配的文件，删的时候会打日志）

输出
----
image  被选中的那一路图（4D IMAGE batch），可以继续接别的节点
info   人类可读的说明（用到了哪个端口、存了哪些文件、元数据清没清），可接 Show Text

作者备注：节点被设计成「永远重新执行」（IS_CHANGED 返回 NaN）。因为静音/取消静音来回切换时，
输入组合可能和上上次完全一样，如果走缓存就不会重新落盘，磁盘上会留着一张上一轮别的图。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import uuid
from typing import Any, Optional

import numpy as np
import torch
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths

# 精简版 ComfyUI（或者被裁过）可能没有 comfy_execution，容错一下
try:  # pragma: no cover
    from comfy_execution.graph import ExecutionBlocker  # type: ignore
except Exception:  # pragma: no cover
    ExecutionBlocker = None  # type: ignore

LOG = logging.getLogger("AnimaMultiSave")

NODE_VERSION = "1.1.1"
INPUT_SLOTS = 12

# ---- 下拉框取值 -------------------------------------------------------------
OUTPUT_MODES = ["best_only", "all_inputs"]
PRIORITY_ORDERS = ["high_port_first", "low_port_first"]
FORMATS = ["png", "jpg", "webp"]
METADATA_MODES = ["strip_all", "embed_workflow"]

_EXT = {"png": "png", "jpg": "jpg", "webp": "webp"}
# clean_stale 时把换格式之前的旧图也一起清掉
_STALE_EXTS = ("png", "jpg", "jpeg", "webp")

# PNG 里属于「元数据」的块：文本 / EXIF / 时间 / 物理标定，一律丢掉。
# 注意保留 iCCP（色彩配置），否则带色彩配置的图会被看成褪色。PIL 本身不写 iCCP，
# 所以这里保留它既安全也不会漏。
_PNG_METADATA_CHUNKS = frozenset(
    b"tEXt zTXt iTXt eXIf tIME pCAL sCAL oFFs sPLT gIFg gIFx".split()
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 回读校验时认为「不该出现」的键（PIL 把 tEXt/iTXt 直接摊在 info 里）
_SUSPECT_INFO_KEYS = {
    "prompt",
    "workflow",
    "parameters",
    "exif",
    "comment",
    "description",
    "software",
    "source",
    "author",
    "title",
    "creation time",
    "xml:com.adobe.xmp",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _is_blocker(value: Any) -> bool:
    return ExecutionBlocker is not None and isinstance(value, ExecutionBlocker)


def as_image_batch(value: Any) -> Optional[torch.Tensor]:
    """把任意端口值规整成 (B, H, W, C) 的 CPU float32 tensor；不可用就返回 None。

    静音 / 旁路 / 未接线的端口，ComfyUI 根本不会把对应的 key 传进来，
    所以这里的 None 分支才是「这一路没有输出」的主路径。
    """
    if value is None or _is_blocker(value):
        return None

    # 极少数节点会把一批图当 list/tuple 传出来，顺手合并
    if isinstance(value, (list, tuple)):
        parts = [t for t in (as_image_batch(v) for v in value) if t is not None]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        if len({(int(p.shape[1]), int(p.shape[2]), int(p.shape[3])) for p in parts}) != 1:
            LOG.warning("[Anima 多路保存] 收到形状不一致的图片列表，只用第一组")
            return parts[0]
        return torch.cat(parts, dim=0)

    if not torch.is_tensor(value):
        try:
            value = torch.as_tensor(value)
        except Exception:
            return None

    t = value.detach()
    if t.device.type != "cpu":
        t = t.cpu()
    if t.dtype == torch.uint8:
        t = t.float() / 255.0
    elif t.dtype != torch.float32:
        t = t.float()

    # HWC / HW / 其它维度
    if t.dim() == 3:
        t = t.unsqueeze(0)
    elif t.dim() == 2:
        t = t.unsqueeze(0).unsqueeze(-1)
    elif t.dim() != 4:
        return None

    if min(int(s) for s in t.shape) <= 0:
        return None
    return t.contiguous()


def to_pil(frame: torch.Tensor) -> Image.Image:
    """单帧 tensor (H, W, C) → PIL。和原生 SaveImage 一样用截断而不是四舍五入。"""
    arr = np.clip(frame.numpy() * 255.0, 0.0, 255.0).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    channels = arr.shape[-1]
    if channels >= 4:
        return Image.fromarray(arr[:, :, :4], mode="RGBA")
    if channels == 3:
        return Image.fromarray(arr[:, :, :3], mode="RGB")
    return Image.fromarray(arr[:, :, 0], mode="L")


def strip_png_metadata(data: bytes) -> tuple[bytes, list[str]]:
    """按 chunk 结构过滤 PNG 元数据块。

    只删黑名单里的块（文本 / EXIF / 时间戳等），IHDR、PLTE、IDAT、IEND、tRNS、iCCP
    这些影响解码和色彩的都原样保留；CRC 也原样搬运，所以不会把图弄坏。
    """
    if not data.startswith(_PNG_SIGNATURE):
        return data, []

    out = bytearray(_PNG_SIGNATURE)
    removed: list[str] = []
    pos = len(_PNG_SIGNATURE)
    total = len(data)
    while pos + 12 <= total:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > total:  # 结构损坏，宁可不改
            return data, []
        if ctype in _PNG_METADATA_CHUNKS:
            removed.append(ctype.decode("latin-1"))
        else:
            out += data[pos:end]
        pos = end
        if ctype == b"IEND":
            break
    return bytes(out), removed


def encode_image(
    img: Image.Image,
    fmt: str,
    quality: int,
    compress_level: int,
    png_text: dict[str, str],
    strip: bool,
) -> tuple[bytes, list[str]]:
    """编码成字节流；strip=True 时顺带清掉元数据。"""
    buf = io.BytesIO()
    removed: list[str] = []

    if fmt == "png":
        pnginfo = None
        if png_text and not strip:
            pnginfo = PngInfo()
            for key, val in png_text.items():
                pnginfo.add_text(str(key), str(val))
        img.save(
            buf,
            format="PNG",
            pnginfo=pnginfo,
            compress_level=int(max(0, min(9, compress_level))),
        )
        data = buf.getvalue()
        if strip:
            data, removed = strip_png_metadata(data)
        return data, removed

    if fmt == "jpg":
        im = img if img.mode in ("L", "RGB") else img.convert("RGB")
        im.save(buf, format="JPEG", quality=int(max(1, min(100, quality))))
        return buf.getvalue(), removed

    # webp
    im = img
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    im.save(buf, format="WEBP", quality=int(max(1, min(100, quality))), method=4)
    return buf.getvalue(), removed


def leftover_metadata(path: str) -> list[str]:
    """回读文件，看还有没有残留的用户元数据（只做校验，不改文件）。"""
    found: list[str] = []
    try:
        with Image.open(path) as im:
            for key in im.info:
                if str(key).strip().lower() in _SUSPECT_INFO_KEYS:
                    found.append(str(key))
            try:
                if dict(im.getexif()):
                    found.append("EXIF")
            except Exception:
                pass
    except Exception as exc:  # 读不出来就别拦着保存流程
        LOG.warning("[Anima 多路保存] 校验 %s 失败：%s", path, exc)
    return found


def clean_stale_files(
    folder: str,
    base: str,
    keep: dict[str, int],
    current_ext: str = "",
    keep_unknown_tags: bool = False,
) -> list[str]:
    """删掉 `base<tag>_00001.png` 这一类文件里「这次不该再有」的那些。

    keep 的 key 是端口后缀（best_only 用空串，all_inputs 用 `_p0`、`_p1`…），
    值是这次写出的帧数。满足下面任一条就删：
      * 序号 > keep[tag]               —— 上次 16 帧、这次 8 帧留下的尾巴
      * 扩展名不是这次的格式           —— png 换成 jpg 之后留在旁边的旧图
      * tag 这次压根没出现（仅当 keep_unknown_tags）—— 这一路被静音/删掉了，
        上次写的 `_pN` 就是垃圾（动态端口越接越少时尤其需要这条）
    只做严格匹配，不会碰同目录里其它文件。
    """
    if not keep and not keep_unknown_tags:
        return []
    pattern = re.compile(
        r"^"
        + re.escape(base)
        + r"(_p\d+)?_(\d{5,})\.("
        + "|".join(_STALE_EXTS)
        + r")$",
        re.IGNORECASE,
    )
    removed: list[str] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed

    for name in names:
        m = pattern.match(name)
        if not m:
            continue
        tag = m.group(1) or ""
        if tag in keep:
            limit = keep[tag]
        elif keep_unknown_tags and tag:
            limit = 0          # 这一路这次没有产出 → 该路旧文件全删
        else:
            continue
        index = int(m.group(2))
        file_ext = (m.group(3) or "").lower()
        wrong_ext = bool(current_ext) and file_ext != current_ext.lower()
        if index <= limit and not wrong_ext:
            continue
        target = os.path.join(folder, name)
        try:
            if os.path.isfile(target):
                os.remove(target)
                removed.append(name)
        except OSError as exc:
            LOG.warning("[Anima 多路保存] 删除旧文件 %s 失败：%s", target, exc)
    return removed


# ---------------------------------------------------------------------------
# 节点本体
# ---------------------------------------------------------------------------
class AnimaMultiSave:
    """多端口自动落盘保存节点（含元数据消除）"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "anima/result",
                        "tooltip": "输出文件名前缀，可带子目录（anima/result → output/anima/），"
                        "也支持 %date% %width% 等 ComfyUI 变量。",
                    },
                ),
                "output_mode": (
                    OUTPUT_MODES,
                    {
                        "default": "best_only",
                        "tooltip": "best_only：只保存优先级最高的那一路（推荐）。\n"
                        "all_inputs：每一路都存一份，文件名带 _p1/_p2… 后缀。",
                    },
                ),
                "priority_order": (
                    PRIORITY_ORDERS,
                    {
                        "default": "high_port_first",
                        "tooltip": "high_port_first：端口号大的优先（后面接最终图）。\n"
                        "low_port_first：端口号小的优先。",
                    },
                ),
                "format": (
                    FORMATS,
                    {
                        "default": "png",
                        "tooltip": "png 无损；jpg / webp 有损但体积小。",
                    },
                ),
                "quality": (
                    "INT",
                    {
                        "default": 95,
                        "min": 1,
                        "max": 100,
                        "step": 1,
                        "tooltip": "jpg / webp 的质量，png 不用这个参数。",
                    },
                ),
                "png_compress_level": (
                    "INT",
                    {
                        "default": 4,
                        "min": 0,
                        "max": 9,
                        "step": 1,
                        "tooltip": "PNG 压缩等级，只影响体积和保存速度，不影响画质。",
                    },
                ),
                "metadata": (
                    METADATA_MODES,
                    {
                        "default": "strip_all",
                        "tooltip": "strip_all：不写入、并逐块清除 prompt / workflow / EXIF 等元数据。\n"
                        "embed_workflow：和原生 SaveImage 一样把工作流写进 PNG（仅 png 生效）。",
                    },
                ),
                "overwrite": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "False（推荐）：每轮用 ComfyUI 自增编号新建文件，和原生 SaveImage 一样，\n"
                        "历史/队列里的缩略图各是各的。\n"
                        "True：始终覆盖同一组文件名（result_00001.png…），好处是固定路径永远是最新图；\n"
                        "代价是 ComfyUI 资产库不支持同一路径多次产出 —— 为了不让历史全变成最新那张，\n"
                        "本节点会另外往 temp/ 写一份唯一命名的预览副本供前端显示。",
                    },
                ),
                "clean_stale": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "只在 overwrite=True 时生效：顺手删掉同前缀下这次没产出的旧帧"
                        "（例如上次 16 帧、这次 8 帧）。只删严格匹配「前缀[_pN]_00001」的文件。",
                    },
                ),
            },
            "optional": {
                f"images_{i}": (
                    "IMAGE",
                    {
                        "tooltip": f"第 {i} 路图片。没接线 / 被静音 / 被旁路时这里就是空的，节点会自动往下退一级。",
                    },
                )
                for i in range(1, INPUT_SLOTS + 1)
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "info")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "AnimaArt/save"
    DESCRIPTION = (
        "多端口保存：12 个 IMAGE 输入（动态端口版可以无限加），自动挑「跑得最远"
        "（优先级最高）的那一路」落盘，支持覆盖写 / 清旧帧 / 元数据消除。"
    )
    SEARCH_ALIASES = [
        "save image",
        "anima save",
        "multi save",
        "多路保存",
        "元数据消除",
        "save without metadata",
    ]

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> float:
        # 永远重新执行：静音/取消静音来回切时，输入组合可能和上上轮一模一样，
        # 走缓存的话磁盘上会留着上一轮别的图，所以这里强制每次都重存。
        return float("NaN")

    # -- 主流程（固定 12 路版；挑图/落盘/清理全在 save_ports 里，动态版共用）----
    def save(
        self,
        filename_prefix: str = "anima/result",
        output_mode: str = "best_only",
        priority_order: str = "high_port_first",
        format: str = "png",
        quality: int = 95,
        png_compress_level: int = 4,
        metadata: str = "strip_all",
        overwrite: bool = False,
        clean_stale: bool = True,
        images_1=None,
        images_2=None,
        images_3=None,
        images_4=None,
        images_5=None,
        images_6=None,
        images_7=None,
        images_8=None,
        images_9=None,
        images_10=None,
        images_11=None,
        images_12=None,
        unique_id=None,
        prompt=None,
        extra_pnginfo=None,
    ):
        ports: list[tuple[int, torch.Tensor]] = []
        for slot, value in enumerate(
            (
                images_1, images_2, images_3, images_4,
                images_5, images_6, images_7, images_8,
                images_9, images_10, images_11, images_12,
            ),
            start=1,
        ):
            batch = as_image_batch(value)
            if batch is not None:
                ports.append((slot, batch))

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
            prompt=prompt,
            extra_pnginfo=extra_pnginfo,
            unique_id=unique_id,
        )
        return {"ui": {"images": results}, "result": (image, info)}


# ---------------------------------------------------------------------------
# 共用核心：给定 [(端口号, 图片批次)]，挑图 → 落盘 → 清理 → 汇报
# 固定端口版（AnimaMultiSave）和动态端口版（AnimaMultiSaveDynamic）都走这里，
# 保证两个节点的行为一模一样。
# ---------------------------------------------------------------------------
PREVIEW_SUBFOLDER = "anima_multisave"
PREVIEW_KEEP = 200      # temp 里最多留多少个预览副本，别让它把盘吃光


def _prune_preview_dir(folder: str, keep: int = PREVIEW_KEEP) -> int:
    """只保留最近 keep 个预览副本（都是本节点写的，删了不影响正式输出）。"""
    try:
        items = [
            (os.path.getmtime(os.path.join(folder, f)), f) for f in os.listdir(folder)
        ]
    except OSError:
        return 0
    if len(items) <= keep:
        return 0
    items.sort()
    removed = 0
    for _, name in items[: len(items) - keep]:
        try:
            os.remove(os.path.join(folder, name))
            removed += 1
        except OSError:
            pass
    return removed



def _write_preview_copy(
    output_name: str, tag: str, index: int, ext: str, data: bytes
) -> dict[str, Any]:
    """把这一轮的图再写一份到 temp/，文件名带唯一后缀，专门喂给前端预览。

    ComfyUI 的资产/历史是拿「文件路径」当身份的，同一个 output 路径反复重写
    （= 覆盖模式）会让历史列表里每一条缩略图都变成最新那张 —— 官方源码里也写着
    "producers that write the same output path are not coalesced (unsupported)"。
    所以覆盖模式下再多写一份唯一命名的 temp 副本交给 ui.images，
    历史就各是各的，而 output 里仍然是那个固定文件名。
    """
    folder = os.path.join(folder_paths.get_temp_directory(), PREVIEW_SUBFOLDER)
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(output_name)[0]
    name = f"{stem}{tag}__{uuid.uuid4().hex[:10]}.{ext}"
    try:
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(data)
    except OSError as exc:
        LOG.warning("[Anima 多路保存] 写预览副本失败（不影响正式输出）：%s", exc)
        return {"filename": output_name, "subfolder": "", "type": "output"}
    return {"filename": name, "subfolder": PREVIEW_SUBFOLDER, "type": "temp"}


def save_ports(
    ports: list[tuple[int, torch.Tensor]],
    *,
    filename_prefix: str = "anima/result",
    output_mode: str = "best_only",
    priority_order: str = "high_port_first",
    fmt: str = "png",
    quality: int = 95,
    png_compress_level: int = 4,
    metadata: str = "strip_all",
    overwrite: bool = False,
    clean_stale: bool = True,
    prompt: Any = None,
    extra_pnginfo: Any = None,
    unique_id: Any = None,
    log_tag: str = "[Anima 多路保存]",
) -> tuple[Optional[torch.Tensor], str, list[dict[str, Any]]]:
    """返回 (被选中的图, info 文本, ui.images 列表)；没有可用图时图是 None、列表为空。"""
    output_mode = output_mode if output_mode in OUTPUT_MODES else "best_only"
    priority_order = (
        priority_order if priority_order in PRIORITY_ORDERS else "high_port_first"
    )
    fmt = fmt if fmt in FORMATS else "png"
    strip = metadata != "embed_workflow"

    # 1) 端口按编号从小到大排（动态端口也靠这个保证优先级稳定）
    ports = sorted(
        [(int(slot), t) for slot, t in ports if t is not None], key=lambda p: p[0]
    )
    if not ports:
        info = "没有可用输入：所有端口都没有图（未连接 / 被静音 / 上游没有产出）"
        LOG.warning("%s %s（节点 %s），本次不保存。", log_tag, info, unique_id)
        return None, info, []

    # 2) 挑要保存的端口
    if output_mode == "all_inputs":
        chosen = list(ports)
    else:
        ordered = (
            list(reversed(ports)) if priority_order == "high_port_first" else list(ports)
        )
        chosen = [ordered[0]]

    # 3) 输出路径
    first_batch = chosen[0][1]
    height, width = int(first_batch.shape[1]), int(first_batch.shape[2])
    full_folder, base_name, counter, subfolder, _resolved_prefix = (
        folder_paths.get_save_image_path(
            filename_prefix,
            folder_paths.get_output_directory(),
            width,
            height,
        )
    )

    # 4) 元数据写不写（strip 模式一律不写）
    embed_workflow = (not strip) and fmt == "png"
    png_text: dict[str, str] = {}
    if not strip and fmt != "png":
        LOG.warning(
            "%s %s 不支持嵌入工作流元数据，本次输出是干净的。想保留工作流请用 png。",
            log_tag,
            fmt,
        )
    if embed_workflow:
        if prompt is not None:
            png_text["prompt"] = json.dumps(prompt, ensure_ascii=False)
        if extra_pnginfo:
            for key, val in extra_pnginfo.items():
                try:
                    png_text[str(key)] = json.dumps(val, ensure_ascii=False)
                except (TypeError, ValueError):
                    LOG.warning("%s 跳过无法序列化的 pnginfo: %s", log_tag, key)

    ext = _EXT[fmt]
    results: list[dict[str, Any]] = []
    out_names: list[str] = []
    written: dict[str, int] = {}
    leftovers: list[str] = []
    sequence = int(counter)

    for port, batch in chosen:
        tag = "" if output_mode == "best_only" else f"_p{port}"
        count = int(batch.shape[0])
        written[tag] = written.get(tag, 0) + count
        for index in range(count):
            img = to_pil(batch[index])
            data, removed = encode_image(
                img, fmt, quality, png_compress_level, png_text, strip
            )
            number = index + 1 if overwrite else sequence
            if not overwrite:
                sequence += 1
            name = f"{base_name}{tag}_{number:05d}.{ext}"
            path = os.path.join(full_folder, name)
            with open(path, "wb") as fh:
                fh.write(data)
            out_names.append(name)

            if strip and fmt == "png":
                LOG.debug(
                    "%s %s 清除元数据块：%s",
                    log_tag,
                    name,
                    ",".join(removed) if removed else "（无）",
                )
            if strip:
                leftovers.extend(f"{name}:{k}" for k in leftover_metadata(path))

            # 覆盖模式下同一个文件名会被反复重写，而 ComfyUI 的资产/历史是靠
            # 「输出路径」认图的（官方注释：同一路径多次产出不支持），
            # 于是历史里每一条预览都会显示成最新那张。为了既不放弃固定文件名、
            # 又不污染历史，这里再往 temp 写一份「这一轮专属」的预览副本交给前端。
            if overwrite:
                results.append(_write_preview_copy(name, tag, index, ext, data))
            else:
                results.append({"filename": name, "subfolder": subfolder, "type": "output"})

    # 5) 清掉这次没产出的旧帧
    deleted: list[str] = []
    if clean_stale and overwrite:
        if output_mode == "best_only":
            keep = {"": written.get("", 0)}
        else:
            # all_inputs：这次写出的按帧数保留，其余的 _pN（这一路被静音/删掉了）全清
            keep = dict(written)
        deleted = clean_stale_files(
            full_folder,
            base_name,
            keep,
            ext,
            keep_unknown_tags=(output_mode != "best_only"),
        )

    # 6.5) 覆盖模式会往 temp 写预览副本，顺手限个量（只留最近 PREVIEW_KEEP 个）
    if overwrite:
        pruned = _prune_preview_dir(
            os.path.join(folder_paths.get_temp_directory(), PREVIEW_SUBFOLDER)
        )
        if pruned:
            LOG.debug("%s 清理了 %d 个旧预览副本", log_tag, pruned)

    # 7) 汇总信息
    used = ",".join(str(port) for port, _ in chosen)
    available = ",".join(str(port) for port, _ in ports)
    files = ", ".join(out_names)
    bits = [
        f"{len(out_names)} 张 → {subfolder or '.'}/{out_names[0]}"
        + (f" 等 {len(out_names)} 个文件" if len(out_names) > 1 else ""),
        f"端口 {used}（可用：{available}）",
        f"模式 {output_mode}/{priority_order}"
        + ("（固定文件名覆盖）" if overwrite else "（每轮新文件）"),
        "元数据 "
        + (
            "已清除"
            if strip
            else ("已保存为工作流" if embed_workflow else f"已清除（{fmt} 不支持嵌入）")
        ),
    ]
    if deleted:
        bits.append(f"清理旧文件 {len(deleted)} 个")
    info = " | ".join(bits)

    if leftovers:
        LOG.warning(
            "%s 这些文件仍有元数据残留：%s",
            log_tag,
            ", ".join(sorted(set(leftovers))[:10]),
        )
    LOG.info("%s %s | 文件：%s", log_tag, info, files)

    return chosen[0][1], info, results


NODE_CLASS_MAPPINGS = {"AnimaMultiSave": AnimaMultiSave}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaMultiSave": "💾 Anima 多路保存 (固定 12 路)"
}
