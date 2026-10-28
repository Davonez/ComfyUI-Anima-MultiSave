# -*- coding: utf-8 -*-
"""
AnimaMultiSave 离线自测（不启动 ComfyUI，直接调节点函数）。

跑法（在 ComfyUI 根目录下）：
    <ComfyUI 目录>\\python\\python.exe ^
        custom_nodes\\ComfyUI-Anima-MultiSave\\tests\\selftest.py
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import traceback

# 让脚本能 import 到 ComfyUI 本体
HERE = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.dirname(HERE)
COMFY_ROOT = os.path.abspath(os.path.join(NODE_DIR, "..", ".."))
for p in (COMFY_ROOT, os.path.dirname(NODE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
import torch
from PIL import Image

import folder_paths

sys.path.insert(0, NODE_DIR)
import anima_multisave as ams  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [ok] {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


def make(color, h=32, w=48, batch=1, channels=3):
    t = torch.zeros(batch, h, w, channels, dtype=torch.float32)
    for c, v in enumerate(color[:channels]):
        t[..., c] = v
    return t


def png_chunks(path):
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG"
    out, pos = [], 8
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8].decode("latin-1")
        out.append(ctype)
        pos += 12 + length
        if ctype == "IEND":
            break
    return out, data


def meta_of(path):
    with Image.open(path) as im:
        info = dict(im.info)
        exif = dict(im.getexif()) if im.format == "PNG" else {}
    return info, exif


def files(folder):
    return sorted(os.listdir(folder))


def run(node, **kw):
    """调节点，自动补上 hidden 参数（离线时是 None）。"""
    kw.setdefault("filename_prefix", "anima/result")
    # 这一组用例都在测「固定文件名 + 覆盖 + 清旧帧」，显式固定住模式，
    # 免得默认值一改就整片红；默认值（每轮新文件）单独在 [18] 里测。
    kw.setdefault("overwrite", True)
    return node.save(**kw)


def main():
    tmp = tempfile.mkdtemp(prefix="anima_multisave_")
    folder_paths.set_output_directory(tmp)
    folder_paths.set_temp_directory(os.path.join(tmp, 'temp'))
    out = os.path.join(tmp, "anima")
    node = ams.AnimaMultiSave()

    red = make((1.0, 0.0, 0.0))
    green = make((0.0, 1.0, 0.0))
    blue = make((0.0, 0.0, 1.0))

    try:
        # ---- 1. 三路都在 → 存最高优先级（端口3）-----------------------
        print("\n[1] 三路都在，high_port_first")
        r = run(node, images_1=red, images_2=green, images_3=blue)
        got = files(out)
        check("只落 1 个文件", got == ["result_00001.png"], got)
        with Image.open(os.path.join(out, "result_00001.png")) as im:
            px = im.convert("RGB").getpixel((0, 0))
            check("像素是端口3的蓝", px == (0, 0, 255), px)
            check("尺寸正确", im.size == (48, 32), im.size)
        check("info 里写端口 3", "ports 3" in r["result"][1], r["result"][1])
        check("返回的图是端口3", torch.equal(r["result"][0], blue), r["result"][0].shape)
        check("ui 只有 1 张", len(r["ui"]["images"]) == 1, r["ui"]["images"])

        # ---- 2. 最终级被静音 → 存端口2 -------------------------------
        print("\n[2] 端口3 静音（None）")
        r = run(node, images_1=red, images_2=green, images_3=None)
        with Image.open(os.path.join(out, "result_00001.png")) as im:
            px = im.convert("RGB").getpixel((0, 0))
        check("退到端口2的绿", px == (0, 255, 0), px)
        check("没有多出文件", files(out) == ["result_00001.png"], files(out))
        check("info 里写端口 2", "ports 2" in r["result"][1], r["result"][1])

        # ---- 3. 端口2、3 都静音 → 存端口1 ----------------------------
        print("\n[3] 端口2、3 都静音")
        r = run(node, images_1=red)
        with Image.open(os.path.join(out, "result_00001.png")) as im:
            px = im.convert("RGB").getpixel((0, 0))
        check("退到端口1的红", px == (255, 0, 0), px)

        # ---- 4. 全空 ------------------------------------------------
        print("\n[4] 全部没图")
        r = run(node)
        check("不炸，ui 为空", r["ui"]["images"] == [], r)
        check("result 图是 None", r["result"][0] is None)
        check("info 说明原因", "No usable input" in r["result"][1], r["result"][1])

        # ---- 5. 元数据消除 ------------------------------------------
        print("\n[5] 元数据消除")
        fake_prompt = {"1": {"class_type": "KSampler", "inputs": {"seed": 123456789}}}
        fake_extra = {"workflow": {"nodes": [{"id": 1, "type": "KSampler"}]}}
        run(
            node,
            images_1=blue,
            metadata="strip_all",
            prompt=fake_prompt,
            extra_pnginfo=fake_extra,
        )
        path = os.path.join(out, "result_00001.png")
        chunks, raw = png_chunks(path)
        check("没有 tEXt/zTXt/iTXt 块",
              not any(c in ("tEXt", "zTXt", "iTXt") for c in chunks), chunks)
        check("没有 eXIf 块", "eXIf" not in chunks, chunks)
        check("PNG 结构完好(IHDR..IEND)",
              chunks[0] == "IHDR" and chunks[-1] == "IEND", chunks)
        check("文件里搜不到 seed",
              b"123456789" not in raw and b"KSampler" not in raw, "仍然含工作流字符串")
        info, _ = meta_of(path)
        check("PIL 读不到任何 info", info == {}, info)

        # ---- 6. 想保留工作流时可以保留 ------------------------------
        print("\n[6] metadata=embed_workflow 保留工作流")
        run(node, images_1=blue, metadata="embed_workflow",
            prompt=fake_prompt, extra_pnginfo=fake_extra)
        info, _ = meta_of(path)
        check("prompt 文本块回来了", "prompt" in info, list(info))
        check("workflow 文本块回来了", "workflow" in info, list(info))
        check("prompt 内容可解析",
              "KSampler" in info.get("prompt", ""), info.get("prompt", "")[:80])

        # ---- 7. batch + 覆盖 + 清旧帧 ------------------------------
        print("\n[7] 批量帧 / 覆盖 / 清理旧帧")
        run(node, images_1=blue)
        check("单帧只剩 1 个文件", files(out) == ["result_00001.png"], files(out))
        run(node, images_1=make((0.5, 0.5, 0.5), batch=3))
        check("3 帧 → 3 个文件",
              files(out) == ["result_00001.png", "result_00002.png", "result_00003.png"],
              files(out))
        r = run(node, images_1=make((0.5, 0.5, 0.5), batch=2))
        check("回落到 2 帧，第 3 帧被清掉",
              files(out) == ["result_00001.png", "result_00002.png"], files(out))
        check("info 提到清理", "removed 1 stale file" in r["result"][1], r["result"][1])

        # ---- 8. clean_stale=False 时不许删 ------------------------
        print("\n[8] clean_stale=False 不删旧帧")
        run(node, images_1=make((0.5, 0.5, 0.5), batch=3))
        run(node, images_1=blue, clean_stale=False)
        check("3 个文件都还在",
              len(files(out)) == 3, files(out))

        # ---- 9. all_inputs：每路都存，静音的那路被清掉 -------------
        print("\n[9] all_inputs 模式")
        for f in files(out):
            os.remove(os.path.join(out, f))
        r = run(node, output_mode="all_inputs", images_1=red, images_2=green, images_3=blue)
        check("三路各存一份",
              files(out) == ["result_p1_00001.png", "result_p2_00001.png", "result_p3_00001.png"],
              files(out))
        check("ui 有 3 张", len(r["ui"]["images"]) == 3, r["ui"]["images"])
        r = run(node, output_mode="all_inputs", images_1=red, images_3=blue)
        check("端口2静音后旧文件被清",
              files(out) == ["result_p1_00001.png", "result_p3_00001.png"], files(out))

        # ---- 10. 优先级反着来 -------------------------------------
        print("\n[10] low_port_first")
        for f in files(out):
            os.remove(os.path.join(out, f))
        r = run(node, priority_order="low_port_first", images_1=red, images_2=green, images_3=blue)
        with Image.open(os.path.join(out, "result_00001.png")) as im:
            px = im.convert("RGB").getpixel((0, 0))
        check("low_port_first 选端口1", px == (255, 0, 0), px)

        # ---- 11. 不覆盖模式 ---------------------------------------
        print("\n[11] overwrite=False")
        shutil.rmtree(out, ignore_errors=True)
        r = run(node, overwrite=False, images_1=red)
        first = files(out)
        r = run(node, overwrite=False, images_1=red)
        check("两次运行文件名不同", len(files(out)) == 2 and first != files(out),
              (first, files(out)))

        # ---- 12. 格式 ---------------------------------------------
        print("\n[12] jpg / webp")
        shutil.rmtree(out, ignore_errors=True)
        r = run(node, format="jpg", quality=90, images_1=blue)
        check("jpg 落盘", files(out) == ["result_00001.jpg"], files(out))
        with Image.open(os.path.join(out, "result_00001.jpg")) as im:
            check("jpg 尺寸正确", im.size == (48, 32), im.size)
            check("jpg 无 EXIF", dict(im.getexif()) == {}, dict(im.getexif()))
            check("jpg 无 comment/xmp",
                  not any(k.lower() in ("comment", "xml:com.adobe.xmp") for k in im.info),
                  list(im.info))
        r = run(node, format="jpg", quality=90, metadata="embed_workflow",
                images_1=blue, prompt=fake_prompt)
        check("jpg + embed_workflow 不炸、仍然干净",
              "cannot embed" in r["result"][1] and not any(
                  k.lower() in ("prompt", "workflow") for k in meta_of(
                      os.path.join(out, "result_00001.jpg"))[0]),
              r["result"][1])
        r = run(node, format="webp", quality=90, images_1=blue)
        check("webp 落盘（换格式后旧 jpg 被清掉）",
              files(out) == ["result_00001.webp"], files(out))
        with Image.open(os.path.join(out, "result_00001.webp")) as im:
            check("webp 尺寸正确", im.size == (48, 32), im.size)

        # ---- 13. 各种奇怪输入 -------------------------------------
        print("\n[13] 奇怪输入")
        shutil.rmtree(out, ignore_errors=True)
        r = run(node, images_1=[red, blue])  # list 形式
        check("list 输入被合并成 batch", len(r["ui"]["images"]) == 2, r["ui"]["images"])
        r = run(node, images_1=torch.zeros(1, 16, 16, 1))  # 单通道
        with Image.open(os.path.join(out, "result_00001.png")) as im:
            check("单通道存成 L", im.mode == "L", im.mode)
        r = run(node, images_1=torch.cat([blue, blue], dim=-1))  # 6 通道 → 取前 4
        check("多通道不炸", len(r["ui"]["images"]) == 1, r["ui"]["images"])
        r = run(node, images_1=torch.zeros(0, 16, 16, 3))  # 空 batch
        check("空 batch 被跳过", r["ui"]["images"] == [], r["ui"]["images"])
        r = run(node, images_1=torch.zeros(1, 1, 1, 3))  # 1x1 图
        check("1x1 图能存", len(r["ui"]["images"]) == 1, r["ui"]["images"])

        # ---- 14. 子目录 + 变量前缀 --------------------------------
        print("\n[14] 前缀")
        r = run(node, filename_prefix="deep/sub/x", images_1=blue, overwrite=False)
        check("子目录生效",
              r["ui"]["images"][0]["subfolder"].replace("\\", "/") == "deep/sub", r["ui"]["images"])
        check("子目录里真有文件",
              os.path.isfile(os.path.join(tmp, "deep", "sub", "x_00001.png")))

        # ---- 15. 越界前缀必须被拦 --------------------------------
        print("\n[15] 越界前缀")
        raised = False
        try:
            run(node, filename_prefix="../../evil", images_1=blue)
        except Exception as exc:
            raised = "outside the output folder" in str(exc)
        check("越界前缀被拦截", raised, "没有抛出 expected error")

        # ---- 16. 返回值结构 ---------------------------------------
        print("\n[16] 返回值结构")
        r = run(node, images_1=blue)
        check("ui.images 结构正确",
              set(r["ui"]["images"][0]) == {"filename", "subfolder", "type"},
              r["ui"]["images"][0])
        check("覆盖模式下 ui 给的是 temp 预览副本（避免历史串图）",
              r["ui"]["images"][0]["type"] == "temp", r["ui"]["images"][0])
        check("预览副本确实存在于 temp/",
              os.path.isfile(os.path.join(tmp, "temp", r["ui"]["images"][0]["subfolder"],
                                          r["ui"]["images"][0]["filename"])))
        check("result 是 2 元组", isinstance(r["result"], tuple) and len(r["result"]) == 2)
        check("IS_CHANGED 是 NaN（永远重跑）", ams.AnimaMultiSave.IS_CHANGED() != ams.AnimaMultiSave.IS_CHANGED())

        # ---- 17. PNG 块过滤函数本身 -------------------------------
        print("\n[17] strip_png_metadata 单元测试")
        buf = os.path.join(tmp, "chunk_test.png")
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
        with open(buf, "rb") as fh:
            clean = fh.read()
        dirty, _ = ams.strip_png_metadata(clean)
        check("无元数据的 PNG 原样保留", dirty == clean)
        check("坏数据不抛异常", ams.strip_png_metadata(b"not a png")[0] == b"not a png")

        # ---- 18. 默认模式（每轮新文件）：历史缩略图才不会串 ----------
        print("\n[18] overwrite=False（新默认）：每轮独立文件名")
        shutil.rmtree(out, ignore_errors=True)
        r1 = run(node, images_1=red, overwrite=False)
        r2 = run(node, images_1=green, overwrite=False)
        f1 = r1["ui"]["images"][0]["filename"]
        f2 = r2["ui"]["images"][0]["filename"]
        check("两次文件名不同", f1 != f2, (f1, f2))
        check("都是 type=output", {r1["ui"]["images"][0]["type"], r2["ui"]["images"][0]["type"]} == {"output"},
              (r1["ui"]["images"][0], r2["ui"]["images"][0]))
        check("两个文件都在", len(files(out)) == 2, files(out))
        with Image.open(os.path.join(out, f1)) as im:
            check("第一张内容没被第二张顶掉", im.convert("RGB").getpixel((0, 0)) == (255, 0, 0),
                  im.convert("RGB").getpixel((0, 0)))
        check("路径唯一 → 资产库不会把历史合并成一张",
              f1 != f2 and r1["ui"]["images"][0]["subfolder"] == r2["ui"]["images"][0]["subfolder"])

        # ---- 19. 覆盖模式：文件名不变，但喂给前端的预览副本每轮唯一 ----
        print("\n[19] overwrite=True：固定文件名 + 每轮唯一预览副本")
        shutil.rmtree(out, ignore_errors=True)
        t1 = run(node, images_1=red)["ui"]["images"][0]
        t2 = run(node, images_1=green)["ui"]["images"][0]
        check("output 里还是同一个文件名", files(out) == ["result_00001.png"], files(out))
        check("预览副本都是 temp", t1["type"] == "temp" and t2["type"] == "temp", (t1, t2))
        check("两轮预览副本名字不同（历史不会全变最新）",
              t1["filename"] != t2["filename"], (t1["filename"], t2["filename"]))
        temp_dir = os.path.join(tmp, "temp", ams.PREVIEW_SUBFOLDER)
        check("两个预览副本都真实存在",
              os.path.isfile(os.path.join(temp_dir, t1["filename"]))
              and os.path.isfile(os.path.join(temp_dir, t2["filename"])),
              sorted(os.listdir(temp_dir)) if os.path.isdir(temp_dir) else "无")
        with Image.open(os.path.join(temp_dir, t1["filename"])) as im:
            check("第一轮预览副本还是红色（没被覆盖）",
                  im.convert("RGB").getpixel((0, 0)) == (255, 0, 0),
                  im.convert("RGB").getpixel((0, 0)))
        with Image.open(os.path.join(temp_dir, t2["filename"])) as im:
            check("第二轮预览副本是绿色",
                  im.convert("RGB").getpixel((0, 0)) == (0, 255, 0),
                  im.convert("RGB").getpixel((0, 0)))
        chunks2, raw2 = png_chunks(os.path.join(temp_dir, t2["filename"]))
        check("预览副本同样没有元数据", chunks2 == ["IHDR", "IDAT", "IEND"], chunks2)

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
