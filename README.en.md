**English** ｜ [中文](README.md)

# 💾 Anima Multi-Save (ComfyUI-Anima-MultiSave)

One save node, two jobs:

1. **Multi-port IMAGE inputs that save "whichever input ran furthest this time"** — if a stage gets muted or bypassed, it steps back one level automatically, keeps the filename fixed, and overwrites the previous run.
2. **Metadata stripping** — prompt / workflow / EXIF never end up in the image, leftover blocks get scrubbed chunk by chunk, and everything is read back and verified after writing.

Category: `AnimaArt/save` (searching `Anima` or `多路保存` in the node tree finds it either way).

## Two versions — pick one

| Node | Ports | Best for |
| --- | --- | --- |
| **💾 Anima Multi-Save (fixed 12 ports)** | Fixed `images_1`…`images_12` | 12 inputs are enough today; also runs on older ComfyUI (it's a V1 node) |
| **💾 Anima Multi-Save (dynamic ports)** | Starts with 3 inputs, **click the `+` under the inputs and add as many as you want** (capped at 40) | You'll probably keep wiring up more and don't want a fixed count to box you in |

Both nodes behave identically (picking the image / overwriting / cleaning stale frames / stripping metadata all go through the same core), so pick either one;
the dynamic-ports one needs a newer frontend, ComfyUI ≥ 0.3.6x (verified on 0.38.1 / frontend 1.53.6).

---

## Why you need it

The usual three-stage pattern in Anima-style workflows looks like this:

```
KSampler 1 (base) → VAE Decode ─┐
KSampler 2 (refine) → VAE Decode ─┼→ save
final upscale / post-process → VAE Decode ─┘
```

Native `SaveImage` has exactly one input. If you want "this run only goes as far as refine" or "this run goes through the whole pipeline", you have to rewire by hand every single time — and then deal with the pile of intermediate images in your output folder.

This node takes all three inputs, and then:

| Situation | Result |
| --- | --- |
| All three are live | Saves **port ③** (the final image) as `result_00001.png` |
| Final stage muted with Ctrl+M | Saves **port ②**, still as `result_00001.png` (overwritten) |
| Both the final and refine stages muted | Saves **port ①**, still as `result_00001.png` |
| Nothing connected / everything muted | No error, no file written — just one log line |

Four, six, or ten inputs all follow the same rule: **the live input with the highest port number gets saved; if that one is gone, fall back to the next highest.**

> The table above assumes `overwrite=True` (fixed filename, only the newest copy kept). **Since v1.1.1 the default is `overwrite=False`**:
> every run gets its own number instead (`result_00001.png`, `result_00002.png`, …), so browsing history works just like native `SaveImage`
> and the output folder accumulates one result per run. If you want "always exactly one latest result", turn `overwrite` on
> — the trade-off between the two modes is covered below in "[Two naming modes](#two-naming-modes-overwrite)".

> How it works: when ComfyUI turns the canvas into a prompt, muted/bypassed nodes and their links are never sent to the backend at all,
> so the argument this node receives simply "doesn't exist" — i.e. `None` → skipped automatically.
> On top of that, this node's `IS_CHANGED` (`fingerprint_inputs` in the dynamic-ports version) always returns NaN, so **every queued prompt really does rewrite the files**
> (otherwise flipping from "final stage off" back to "on" would hit the cache and leave a different image from the previous run sitting on disk).

---

## Parameters

| Parameter | Default | Description |
| --- | --- | --- |
| `filename_prefix` | `anima/result` | Output prefix; a slash makes it a subdirectory (`output/anima/`). Supports ComfyUI's `%date%`, `%width%` and other variables. |
| `output_mode` | `best_only` | `best_only` saves only the highest-priority input; `all_inputs` saves every input (filenames get `_p1` `_p2`…). |
| `priority_order` | `high_port_first` | Higher port numbers win (wire the final image to a high port); `low_port_first` reverses that. |
| `format` | `png` | `png` / `jpg` / `webp`. |
| `quality` | `95` | jpg / webp quality; not used for png. |
| `png_compress_level` | `4` | PNG compression level, 0–9. Affects file size and speed only, never image quality. |
| `metadata` | `strip_all` | `strip_all` scrubs everything; `embed_workflow` writes the workflow back into the PNG the way native SaveImage does. |
| `overwrite` | `False` | `False`: ComfyUI's auto-increment numbering creates a new file each run (same as native `SaveImage`, each run keeps its own history). `True`: fixed filename, overwrites the previous run. |
| `clean_stale` | `True` | In overwrite mode, also deletes old frames with the same prefix that this run didn't produce (the leftover tail when the last run had 16 frames and this one has 8). |

Outputs: `image` (the input image that got picked — you can keep wiring it into other nodes) and `info` (which port was used this time and which files were written — good candidate for Show Text).

---

## Wiring example

```
① base sample → VAE Decode ──→ images_1
② refine sample → VAE Decode ──→ images_2
③ final upscale → VAE Decode ──→ images_3
```

A single input is fine too (just `images_1`, say), and the node works as an ordinary save node.

Two kinds of "disconnect" behave differently and are worth knowing:

* **Mute (Ctrl+M, purple)**: neither the node nor its links are submitted → this input is empty → fall back one level. ✅ Use this one.
* **Bypass (Ctrl+B, purple slash)**: if the node's input and output types match (e.g. `ImageScale`), the image **passes straight through** to the rest of the graph → this node still receives it, just without that step's processing. In that case the input counts as "has an image".

---

## Metadata stripping

Default is `strip_all`:

* Saving writes **no** PNG text chunks at all (native SaveImage writes `prompt` and `workflow`);
* After writing, the file is filtered **chunk by chunk** according to the PNG chunk structure: `tEXt` / `zTXt` / `iTXt` / `eXIf` / `tIME` / `pCAL` / `sCAL` / `oFFs` / `sPLT` / `gIFg` / `gIFx`;
* Chunks that matter for decoding — `IHDR` `PLTE` `IDAT` `IEND` `tRNS` — are left as they are, CRCs untouched, so the image can't get corrupted; `iCCP` (color profile) is kept as well, so images with a color profile don't come out looking washed out;
* Once the file is on disk, PIL reads it back; if anything like `prompt` / `EXIF` / `comment` survived, a `WARNING` goes to the console instead of failing silently;
* jpg / webp never carry exif / icc anyway, so they're clean too.

If you want to keep the workflow so you can reproduce the image, set `metadata` to `embed_workflow` (png only).

A quick side-by-side: for the same prompt, a PNG from native `SaveImage` contains `"class_type": "KSampler"` and the full seed; images from this node contain neither.

---

## Two naming modes (`overwrite`)

| `overwrite` | Filename | History / queue thumbnails | Best for |
| --- | --- | --- | --- |
| **`False` (default, since v1.1.1)** | `result_00001.png`, `result_00002.png`… a new file every run | Each run stays distinct, just like native `SaveImage` | You want to browse history and compare runs |
| `True` | Always `result_00001.png` (overwritten) | Runs can still stay distinct (see below), but only the newest image is kept | You need one fixed path that some other program or script can point at |

**Why did the default become `False`?** Because the overwrite default in v1.1.0 hit a snag: the newer ComfyUI tracks assets/history using the **file path** as the image's identity, and the source says it plainly —
`producers that write the same output path are not coalesced (unsupported)`,
so when the same filename is rewritten over and over, every thumbnail in the history panel turns into the newest image — which looks like "it changed all my earlier generations".

The fix works on two levels:

1. Default to a new filename each run (matching native `SaveImage`), so history is correct by construction;
2. If you insist on `overwrite=True`, then while writing the real file the node also writes a **uniquely named** preview copy into `temp/anima_multisave/`
   and points `ui.images` at it — so:
   * `output/anima/result_00001.png` is always the newest image (the fixed path you wanted);
   * every run's thumbnail in the history panel stays distinct (history doesn't bleed);
   * the preview copies live only in `temp/`, get cleaned up automatically when ComfyUI restarts, and are capped at 200, so they don't eat disk.

> No reinstall needed for this to take effect: it's a change to node code, and once ComfyUI is restarted the newly queued runs will be kept apart.

## Overwrite / cleanup rules (`overwrite=True`)

With `overwrite=True` a filename is "prefix + port suffix (only with `all_inputs`) + 5-digit sequence number":

```
output/anima/result_00001.png        # best_only
output/anima/result_p1_00001.png     # all_inputs
```

`clean_stale=True` deletes files that "obviously were written by this node last time", using two rules:

1. Sequence number > this run's frame count (last run 16 frames, this run 8 → the trailing 8 get deleted);
2. Extension isn't this run's format (after switching png → jpg, the stale `result_00001.png` next to it gets deleted).

Only files matching exactly that naming pattern are touched, and everything deleted is written to the log. **If you don't trust it, turn `clean_stale` off** — then it only overwrites and never deletes.

Two save nodes sharing the same `filename_prefix` will fight each other over files, so remember to change the prefix.

---

## Installation

Drop the whole `ComfyUI-Anima-MultiSave` folder into:

```
ComfyUI/custom_nodes/ComfyUI-Anima-MultiSave/
```

(Or `git clone https://github.com/Davonez/ComfyUI-Anima-MultiSave.git` inside `custom_nodes`.)

Restart ComfyUI, or just use ComfyUI-Manager's "restart"; after changing code a restart is required for it to take effect (there's no hot reload).
The only dependencies are `torch` / `numpy` / `Pillow` / `folder_paths`, all bundled with ComfyUI — nothing to install.

## Demo workflow

`examples/demo_多路保存.json` — three solid-color `EmptyImage` nodes wired into this node; no models, output in a second:

> The walkthrough below assumes `overwrite=True` (fixed filename), so you can watch the same image file change color.
> With the default `overwrite=False`, each run adds a new number — just look at the **newest** file.

1. Run once → `output/anima/demo_00001.png` is **blue** (port ③ wins);
2. `Ctrl+M` to mute node ③ and run again → the same file turns **green**;
3. Mute node ② as well → it turns **red**;
4. Unmute everything and run again → back to **blue**, and the filename is always `demo_00001.png`.

## Self-tests

Six scripts, all of which call ComfyUI itself directly (no server started, none of your models touched). Run them from the **ComfyUI root** (the folder one level above `custom_nodes`):

```powershell
$py = "<ComfyUI dir>\python\python.exe"   # bundled with the portable package; a system python works too, as long as torch / Pillow are installed
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\selftest.py      # 65 checks: picking logic / metadata / overwrite cleanup / odd inputs
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\loader_test.py    # 49 checks: native loader / object_info / validation
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\engine_test.py    # 18 checks: real engine running prompts (including fallback after mute, cache not swallowing images)
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\workflow_test.py  # 32 checks: demo workflow + frontend mute rules
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\dynamic_test.py   # 28 checks: dynamic ports (autogrow) end to end
& $py -X utf8 custom_nodes\ComfyUI-Anima-MultiSave\tests\history_test.py   # 16 checks: history thumbnails no longer bleed (regression test for this fix)
```

Current status: **all 208 checks passing**.

## Known limitations

* `best_only` + `overwrite=True` keeps just one copy, so **older versions get overwritten** (that's the point, not a bug). If you want to keep history, stay on the default `overwrite=False`, or put `%date%` / `%hour%%minute%` in the prefix.
* When the animation frame count shrinks, stale frames get cleaned up (`clean_stale`); in `all_inputs` mode, if one input produces nothing this run (muted / unlinked), the `_pN` files it wrote last time are removed too. If you keep manually named files in the same folder that happen to match `prefix_pN_00001.png`, they'll be treated as the node's own — just use a different prefix.
* The dynamic-ports version caps out at 40 (ComfyUI's own Autogrow limit is 100, so I left some headroom); if you genuinely need more, change `MAX_PORTS` in `anima_multisave_dynamic.py`.
* Metadata stripping only covers the image container — watermarks or text drawn into the pixels are not metadata.
* This node always re-runs the save (`IS_CHANGED=NaN`), so with a large output folder every queued prompt pays for one directory scan, same as native SaveImage.
* In a prompt, the dynamic-ports version uses dot naming (`images.image_0`…), so calling it through the API directly requires that format; when wiring in the UI you don't have to care.

---

## File layout

```
ComfyUI-Anima-MultiSave/
├─ __init__.py                       # node registration (falls back to fixed-only if the dynamic version can't be installed)
├─ anima_multisave.py                # fixed 12-port node + the picking / saving / metadata-stripping core
├─ anima_multisave_dynamic.py        # dynamic-ports version (V3 / Autogrow, up to 40 ports)
├─ pyproject.toml                    # package metadata (for ComfyUI Registry)
├─ LICENSE                           # MIT
├─ README.md                         # Chinese version
├─ README.en.md                      # this file (English)
├─ examples/
│  └─ demo_多路保存.json              # demo workflow (three solid-color EmptyImage nodes)
└─ tests/
   ├─ selftest.py                    # 65 checks: picking logic / metadata / overwrite cleanup
   ├─ loader_test.py                 # 49 checks: native loader / object_info / validation
   ├─ engine_test.py                 # 18 checks: real engine running prompts
   ├─ workflow_test.py               # 32 checks: demo workflow + frontend mute rules
   ├─ dynamic_test.py                # 28 checks: dynamic ports end to end
   └─ history_test.py                # 16 checks: history thumbnails no longer bleed
```

## License

MIT — see [LICENSE](LICENSE).
