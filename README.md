[English](README.en.md) ｜ **中文**

# 💾 Anima 多路保存（ComfyUI-Anima-MultiSave）

一个保存节点，两件事：

1. **多路 IMAGE 输入，自动存「这次跑到最远的那一路」** —— 哪一级被静音/旁路了，就自动往下退一级，文件名固定、覆盖上一次。
2. **元数据消除** —— 不把 prompt / workflow / EXIF 写进图里，还能逐块清掉，写完再回读校验一遍。

分类：`AnimaArt/save`（节点树上搜 `Anima` 或 `多路保存` 都能找到）。

> **1.2.0 —— UI 默认英文 + 可选翻译。** 控件名、tooltip 和 `info` 输出都改成英文了；
> 同时附带 `locales/en` + `locales/zh`，ComfyUI 语言设成中文时界面仍然是中文。
> 控件键没有改，旧工作流可以直接用。

## 两个版本，挑一个用

| 节点 | 端口 | 适合 |
| --- | --- | --- |
| **💾 Anima 多路保存 (固定 12 路)** | 固定 `images_1`…`images_12` | 现成 12 个口够用；老版本 ComfyUI 也能跑（V1 节点） |
| **💾 Anima 多路保存 (动态端口)** | 先给 3 个口，**点输入下面的 `+` 想加多少加多少**（上限 40） | 以后可能越接越多，不想被固定数量卡住 |

两个节点行为完全一致（挑图 / 覆盖 / 清旧帧 / 去元数据都走同一个内核），
随便挑一个；动态端口那个需要 ComfyUI ≥ 0.3.6x 的新前端（0.38.1 / 前端 1.53.6 实测可用）。

---

## 为什么需要它

Anima 这类流程常见的三段式是：

```
KSampler 1（基础）→ VAE Decode ─┐
KSampler 2（细化）→ VAE Decode ─┼→ 保存
最终放大 / 后处理 → VAE Decode ─┘
```

原生 `SaveImage` 只有一个输入口。你要是想「这次只跑到细化」「这次想跑完整流程」，就得每次手动改线、还得处理输出文件夹里堆了一堆中间图。

这个节点把三路都接上，然后：

| 情况 | 结果 |
| --- | --- |
| 三路都亮 | 存 **端口③**（最终图），文件名 `result_00001.png` |
| 最终级 Ctrl+M 静音 | 存 **端口②**，文件名还是 `result_00001.png`（覆盖） |
| 最终级 + 细化级都静音 | 存 **端口①**，文件名还是 `result_00001.png` |
| 一路都没接 / 全静音 | 不报错、不写文件，只打一行日志 |

接四路、六路、十路都是同一套规则：**端口序号最大的那一路活着，就存它；它没了就退到次大的。**

> 上表是按 `overwrite=True`（固定文件名、只留最新一份）描述的。**v1.1.1 起默认是 `overwrite=False`**：
> 每轮另起编号（`result_00001.png`、`result_00002.png`…），和原生 `SaveImage` 一样好翻历史，
> 输出文件夹里会累积每一轮的结果。想要「永远只有一份最新结果」，把 `overwrite` 打开即可
> —— 两种模式的取舍见下面「[两种命名方式](#两种命名方式overwrite)」。

> 原理：ComfyUI 在把画布转成 prompt 时，被静音/旁路的节点和它的连线根本不发给后端，
> 所以本节点收到的那个参数就是「不存在」，等于 `None` → 自动跳过。
> 另外本节点 `IS_CHANGED`（动态端口版是 `fingerprint_inputs`）永远返回 NaN，**每次排队都会真的重写文件**
> （否则「关掉最终级→再打开」这种来回切换会命中缓存，磁盘上留着上一轮的别的图）。

---

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `filename_prefix` | `anima/result` | 输出前缀，带斜杠就是子目录（`output/anima/`）。支持 ComfyUI 的 `%date%` `%width%` 等变量。 |
| `output_mode` | `best_only` | `best_only` 只存优先级最高的一路；`all_inputs` 每路都存一份（文件名带 `_p1` `_p2`…）。 |
| `priority_order` | `high_port_first` | 端口号大的优先（最终图接大号端口）；`low_port_first` 反过来。 |
| `format` | `png` | `png` / `jpg` / `webp`。 |
| `quality` | `95` | jpg / webp 质量，png 用不到。 |
| `png_compress_level` | `4` | PNG 压缩等级 0–9，只影响体积和速度，不影响画质。 |
| `metadata` | `strip_all` | `strip_all` 清干净；`embed_workflow` 像原生 SaveImage 一样把工作流写回 PNG。 |
| `overwrite` | `False` | `False`：每轮用 ComfyUI 自增编号新建文件（和原生 `SaveImage` 一样，历史各是各的）。`True`：固定文件名、覆盖上一次。 |
| `clean_stale` | `True` | 覆盖模式下顺手删掉「同前缀、这次没产出」的旧帧（上次 16 帧、这次 8 帧的那种尾巴）。 |

输出：`image`（被选中的那一路图，可以继续接别的节点）、`info`（这次用了哪个端口、存了哪些文件，可以接 Show Text）。

---

## 接线示例

```
① 基础采样 → VAE Decode ──→ images_1
② 细化采样 → VAE Decode ──→ images_2
③ 最终放大 → VAE Decode ──→ images_3
```

只接一路也行（比如只接 `images_1`），节点当普通保存节点用。

需要注意的两种「断开」方式行为不同：

* **静音 Mute（Ctrl+M，紫色）**：节点和连线都不提交 → 本节点这一路为空 → 往前退一级。✅ 推荐用这个。
* **旁路 Bypass（Ctrl+B，紫色斜杠）**：如果这个节点输入输出类型相同（比如 `ImageScale`），图会**穿过去**继续往下走 → 本节点仍然收得到图，只是没经过那一步处理。这种情况它会被当作「这一路有图」。

---

## 元数据消除

默认 `strip_all`：

* 保存时**不写**任何 PNG 文本块（原生 SaveImage 会写 `prompt` 和 `workflow`）；
* 写完之后按 PNG chunk 结构**逐块过滤** `tEXt` / `zTXt` / `iTXt` / `eXIf` / `tIME` / `pCAL` / `sCAL` / `oFFs` / `sPLT` / `gIFg` / `gIFx`；
* `IHDR` `PLTE` `IDAT` `IEND` `tRNS` 这些影响解码的块原样保留，CRC 不动，所以不会把图画坏；`iCCP`（色彩配置）也保留，避免带色彩配置的图看着褪色；
* 落盘后再用 PIL 回读一遍，万一还有 `prompt` / `EXIF` / `comment` 之类残留，会在控制台打 `WARNING`，不会静默放过；
* jpg / webp 本来就不传 exif / icc，所以也是干净的。

想保留工作流就能复现：把 `metadata` 改成 `embed_workflow`（仅 png 生效）。

顺手对比一下：同一个提示词，原生 `SaveImage` 出的 PNG 里能搜到 `"class_type": "KSampler"` 和完整 seed；本节点出的图里搜不到。

---

## 两种命名方式（`overwrite`）

| `overwrite` | 文件名 | 历史/队列缩略图 | 适合 |
| --- | --- | --- | --- |
| **`False`（默认，v1.1.1 起）** | `result_00001.png`、`result_00002.png`… 每轮新建 | 每轮各是各的，和原生 `SaveImage` 一样 | 想翻历史、想对比多轮结果 |
| `True` | 始终是 `result_00001.png`（覆盖） | 也能各是各的（见下），但图本身只留最新一张 | 就要一个固定路径，别的程序/脚本按路径取图 |

**为什么默认改成了 `False`？** 因为 v1.1.0 默认覆盖时踩到一个坑：ComfyUI 新版的资产/历史是拿**文件路径**当身份认图的，源码里明确写着
`producers that write the same output path are not coalesced (unsupported)`，
所以同一个文件名反复重写时，左侧历史里每一条缩略图都会变成最新那张 —— 看起来像“把之前生成的图全改了”。

修法分两层：

1. 默认改成每轮新文件名（和原生 `SaveImage` 一致），历史天然正确；
2. 你如果坚持 `overwrite=True`，节点在写正式文件的同时，会往 `temp/anima_multisave/`
   再写一份**文件名唯一**的预览副本，并把 `ui.images` 指过去 —— 于是：
   * `output/anima/result_00001.png` 始终是最新那张（你要的固定路径）；
   * 左侧每一轮的缩略图各是各的（历史不会串）；
   * 预览副本只在 `temp/`，重启 ComfyUI 自动清掉，而且最多留 200 个，不占盘。

> 没重启也能生效：改的是节点代码，重启 ComfyUI 后新排队的两轮就会分开。

## 覆盖 / 清理规则（`overwrite=True` 时）

`overwrite=True` 时文件名是「前缀 + 端口后缀（`all_inputs` 才有）+ 5 位序号」：

```
output/anima/result_00001.png        # best_only
output/anima/result_p1_00001.png     # all_inputs
```

`clean_stale=True` 会删掉这些「一看就是这个节点上次写的」文件，两条规则：

1. 序号 > 这次写出的帧数（上次 16 帧、这次 8 帧 → 后 8 帧被删）；
2. 扩展名不是这次格式（png 换 jpg 之后，旁边那张旧的 `result_00001.png` 被删）。

只删严格匹配上面那种命名的文件，删了什么都会写进日志。**不放心就把 `clean_stale` 关掉**，那它就只覆盖、不删除。

两个保存节点用同一个 `filename_prefix` 时会互相抢文件，记得改前缀。

---

## 安装

把整个 `ComfyUI-Anima-MultiSave` 文件夹丢进：

```
ComfyUI/custom_nodes/ComfyUI-Anima-MultiSave/
```

（也可以在 `custom_nodes` 里 `git clone https://github.com/Davonez/ComfyUI-Anima-MultiSave.git`）

重启 ComfyUI，或直接用 ComfyUI-Manager 的「重启」；改代码后必须重启才生效（没有热重载）。
依赖只有 `torch` / `numpy` / `Pillow` / `folder_paths`，ComfyUI 自带，不用装东西。

## 演示工作流

`examples/demo_多路保存.json` —— 三个纯色 `EmptyImage` 接到本节点，不用模型、秒出图：

> 下面按 `overwrite=True`（固定文件名）描述，方便对照「同一张图被换成不同颜色」。
> 用默认的 `overwrite=False` 时每轮会多一个编号，看**最新**那个文件即可。

1. 跑一次 → `output/anima/demo_00001.png` 是**蓝色**（端口③ 优先）；
2. 把③号节点 `Ctrl+M` 静音再跑 → 同一个文件变成**绿色**；
3. 再把②号静音 → 变成**红色**；
4. 全部恢复再跑 → 变回**蓝色**，文件名始终是 `demo_00001.png`。

## 自测

六个脚本，都直接调 ComfyUI 本体（不启服务、不动你的模型）。在 **ComfyUI 根目录**（`custom_nodes` 的上一层）下执行：

```powershell
$py = "<ComfyUI 目录>\python\python.exe"   # 整合包自带的；用系统 python 也行，只要装了 torch / Pillow
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\selftest.py      # 65 项：挑图逻辑 / 元数据 / 覆盖清理 / 各种奇怪输入
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\loader_test.py    # 49 项：原生加载器 / object_info / 校验
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\engine_test.py    # 18 项：真引擎跑 prompt（含静音后退、缓存不吞图）
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\workflow_test.py  # 32 项：演示工作流 + 前端静音规则
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\dynamic_test.py   # 28 项：动态端口（autogrow）端到端
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\history_test.py   # 16 项：历史缩略图不再串图（本次修复的回归测试）
```

当前状态：**208 项全通过**。

## 已知边界

* `best_only` + `overwrite=True` 只保留一份，**历史版本会被覆盖**（这是设计目的，不是 bug）。要留历史就保持默认的 `overwrite=False`，或把前缀改成带 `%date%` / `%hour%%minute%` 的。
* 动画帧数变少时旧帧会被清理（`clean_stale`）；`all_inputs` 模式下某一路这次没产出（被静音/删线）时，它上次写的 `_pN` 文件也会被清掉。如果你在同一个目录里放了别的手工命名文件、且刚好叫 `前缀_pN_00001.png`，会被当成自己的文件 —— 换个前缀就行。
* 动态端口版的上限是 40（ComfyUI 自身 Autogrow 上限 100，我留了余量）；真需要更多改 `anima_multisave_dynamic.py` 里的 `MAX_PORTS`。
* 元数据只清理图片容器里的，画在像素里的水印/文字不属于元数据。
* 本节点总是重跑一次保存（IS_CHANGED=NaN），所以输出文件夹很大时每次排队会有一次目录扫描，跟原生 SaveImage 一样。
* 动态端口版在 prompt 里是点号命名（`images.image_0`…），用 API 直接调的时候要按这个格式传；在 UI 里连线不用管。

---

## 文件结构

```
ComfyUI-Anima-MultiSave/
├─ __init__.py                       # 节点注册（动态端口版装不上时自动降级为只装固定版）
├─ anima_multisave.py                # 固定 12 路节点 + 挑图 / 落盘 / 去元数据内核
├─ anima_multisave_dynamic.py        # 动态端口版（V3 / Autogrow，最多 40 路）
├─ pyproject.toml                    # 包信息（ComfyUI Registry 用）
├─ LICENSE                           # MIT
├─ README.md                         # 本文件
├─ README.en.md                      # English version
├─ examples/
│  └─ demo_多路保存.json              # 演示工作流（三个纯色 EmptyImage）
└─ tests/
   ├─ selftest.py                    # 65 项：挑图逻辑 / 元数据 / 覆盖清理
   ├─ loader_test.py                 # 49 项：原生加载器 / object_info / 校验
   ├─ engine_test.py                 # 18 项：真引擎跑 prompt
   ├─ workflow_test.py               # 32 项：演示工作流 + 前端静音规则
   ├─ dynamic_test.py                # 28 项：动态端口端到端
   └─ history_test.py                # 16 项：历史缩略图不串图
```

## 许可

MIT，见 [LICENSE](LICENSE)。
