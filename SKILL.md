---
name: image
description: "AI 图像生成引擎。自然语言驱动，一句话出图。触发词：生图/画图/封面/信息图/配图/cover/draw/infographic。支持自然语言风格匹配（如'赛博风''温暖治愈''极简'自动对应风格）。"
allowed-tools: [Read, Write, Edit, Bash]
---

# Visual Forge — AI 图像生成引擎

**无输入或输入 -help 时，展示帮助菜单 + 自动打开风格画廊：**

```
Visual Forge — AI 图像生成引擎（自然语言驱动）

用法：/image [描述] — 一句话搞定，自动匹配模式和风格

快速示例：
  /image 封面 AI技能分类学              → 公众号+小红书双封面
  /image 赛博风封面 AI技能              → 赛博科技风封面
  /image 小红书封面 可爱猫咪             → 手绘可爱风（3:4）
  /image 信息图 人工智能发展史           → 信息大图
  /image 手绘一只在雨中撑伞的猫         → 自由生图（自动匹配风格）
  /image 素描风格的山水                 → 铅笔素描风自由生图

指定引擎/模型：
  /image --provider yunwu --model gpt-image-2 封面 AI  → yunwu + gpt-image（推荐，直连）
  /image --provider grsai --model gpt-image-2 封面 AI  → grsai + gpt-image
  /image --provider yunwu --model gemini-3.1-flash 封面 AI → yunwu Gemini（快速）

实际调用示例（不指定风格即自由直出）：
  /image --provider grsai --model gpt-image-2 帮我生成一张卡萨帝洗衣机官网首页，高端大气上档次
                                              → 文生图，直接用用户描述生成
  /image --provider grsai --model gpt-image-2 @/path/to/ref.png 将这张图换成明亮风格
                                              → 图生图，@引用本地文件作为参考图

比例规则（在描述中写明即可，自动覆盖风格默认比例）：
  /image 帮我生成一张官网首页，高端大气。16:9   → 自动提取 16:9，覆盖默认 4:3
  /image 3:4 小红书封面 可爱猫咪                → 竖版 3:4
  不写比例 → 使用风格默认比例

模式自动识别（不用说"模式"这个词）：
  封面/cover/公众号封面/小红书封面  → cover（双封面）
  信息图/infographic/信息大图       → infographic
  其他（默认）                      → freeform（自由生图，不指定风格则直接用用户原始描述）

风格自然语言匹配（举几个例子）：
  赛博/科技/霓虹  → cyberpunk    温暖/治愈/柔和 → warm_healing
  手绘/插画/可爱  → hand_drawn   黑白/极简/高级 → mono_bw
  水彩/文艺/淡雅  → watercolor   商务/专业/都市 → business
  不指定任何风格词 → raw（自由直出，直接用用户原始描述）
  （完整 29 种风格见 config/prompts.yaml 的 keywords 字段）

可视化浏览：PC 端可打开 config/style-gallery.html 查看风格画廊
```

展示完帮助菜单后，**自动用浏览器打开风格画廊**：

```bash
python -c "import webbrowser; webbrowser.open('config/style-gallery.html')"
```

---

## 第一步：模式识别（自动，不问用户）

根据用户输入自动判断模式：

| 用户意图信号 | 模式 | 读取文件 |
|-------------|------|---------|
| 封面/cover/公众号封面/小红书封面/文章封面 | cover | `references/mode-cover.md` |
| 信息图/infographic/高密度/信息大图 | infographic | `references/mode-infographic.md` |
| 其他任何描述（默认） | freeform | `references/mode-freeform.md` |

## 第二步：风格匹配（自然语言，不问用户）

**核心机制**：读取 `config/prompts.yaml`，用用户描述中的关键词匹配每种风格的 `keywords` 字段。

### 匹配算法

```
用户输入 → 提取风格暗示词 → 遍历 prompts.yaml 所有场景的 keywords → 匹配度排序 → 选最佳

示例：
  "赛博风封面"     → 提取"赛博" → 匹配 cover.cyberpunk (keywords含"赛博") → ✅
  "温暖治愈的猫咪"  → 提取"温暖""治愈" → 匹配 freeform.hand_drawn? 不，warm_healing? keywords在cover下
                    → 跨场景搜索"温暖""治愈" → cover.warm_healing 匹配 → 但这是freeform模式
                    → freeform 没有warm_healing → 降级到 hand_drawn（手绘/温暖）
  "极简黑白"       → 匹配 cover.mono_bw 或 freeform.minimal
```

### 匹配规则

1. **同场景优先**：先在当前模式对应场景下搜索 keywords
2. **跨场景搜索**：同场景无匹配时，搜索所有场景（cover/infographic/freeform/ppt）
3. **多关键词加权**：用户描述命中 keywords 越多，匹配度越高
4. **比例优先级**（从高到低）：① 用户描述中明确写出比例（`16:9`/`3:4`/`4:3`/`1:1`）或比例关键词（`宽屏`/`竖版`/`横版`/`方形`）→ 自动提取并覆盖；② 风格默认比例；③ 无指定时默认 `4:3`
5. **默认兜底**：
   - cover → 公众号 `visual_note`，小红书 `hand_drawn`
   - infographic → `tech_blueprint`
   - freeform → `raw`（无明确风格时直接用用户原始描述）

### 28 种风格 keywords 速查（从 prompts.yaml 提取）

**封面 cover（11 种）**：
| 风格 ID | keywords |
|---------|----------|
| visual_note | 视觉笔记、手绘笔记、信息图、notion |
| hand_drawn | 手绘、插画、可爱、小红书 |
| mono_bw | 黑白、极简、高级、克制 |
| business | 商务、专业、都市、办公、资讯 |
| cyberpunk | 赛博、科技、霓虹、未来、数字 |
| collage | 拼贴、贴纸、海报、活泼、拼图 |
| tropical | 热带、雨林、自然、绿色、探索 |
| illustration | 手绘、书房、温暖、专注 |
| kawaii | 可爱、萌、粉嫩、卡通、少女 |
| warm_healing | 治愈、温暖、宁静、柔和、安心 |
| watercolor | 水彩、写意、文艺、淡雅、水墨 |

**信息图 infographic（5 种）**：
| 风格 ID | keywords |
|---------|----------|
| tech_blueprint | 蓝图、坐标、实验室、技术 |
| retro_pop | 波普、复古、70s |
| scrapbook | 手帐、拼贴、证据板 |
| clay_doodle | 陶土、手绘、暖色 |
| vector | 矢量、扁平、几何 |

**自由生图 freeform（7 种）**：
| 风格 ID | keywords |
|---------|----------|
| raw | 自由、直出、原始、自定义、raw、直接 |
| visual_note | 视觉笔记、手绘笔记、信息图 |
| hand_drawn | 手绘、插画、可爱 |
| tech | 科技、技术、未来、赛博 |
| blueprint | 蓝图、工程、架构 |
| sketch | 素描、线稿、速写 |
| minimal | 极简、简约、干净 |

**PPT（6 种）**：ppt_retro_pop, ppt_minimal, ppt_cyberpunk, ppt_neo_brutalism, ppt_swiss, ppt_blueprint

### 风格画廊（可视化选择）

**PC 端**：浏览器打开 `config/style-gallery.html`（支持筛选、点击查看 prompt、一键复制）。

> 截图文件：`config/previews/style-gallery-overview.png`（内容更新后需重新截图：Chrome headless 截全页）

## 第三步：读取模式 reference → 执行生成

按模式读取对应 reference 文件，按流程执行。

### 方式一：技能调用（推荐）

任何支持技能的 AI 智能体（Claude Code、Cursor、Copilot 等）均可通过自然语言调用。用户只需描述想要什么图，智能体自动完成模式识别、风格匹配、比例提取、参数组装、生成和推送。

```
# 文生图（不指定风格 → raw 自由直出）
/image --provider grsai --model gpt-image-2 帮我生成一张卡萨帝洗衣机官网首页，高端大气上档次。16:9

# 图生图（@引用本地文件作为参考图）
/image --provider grsai --model gpt-image-2 @/path/to/ref.png 将这张图换成明亮风格，主体元素不变

# 指定风格
/image 赛博风封面 AI技能
/image 信息图 人工智能发展史
/image 手绘一只在雨中撑伞的猫
```

智能体内部会自动调用下方的 `generate.py` 脚本完成生成。

### 方式二：脚本直接执行（高级）

也可直接通过命令行调用生图脚本：

```bash
# 指定风格（--style 自动读取比例和模板）：
python scripts/generate.py \
  --config config/engine.json \
  --style [风格ID] --prompt "[描述]" \
  --out "[输出路径]"

# 指定引擎+模型：
python scripts/generate.py \
  --config config/engine.json \
  --provider grsai --model gpt-image-2 --style cyberpunk --prompt "..." --out "..."

# 手动方式（不用 --style）：
python scripts/generate.py \
  --config config/engine.json \
  --prompt "[完整prompt]" --out "[输出路径]" --aspect-ratio "[比例]"
```

## 图生图（参考图）

支持两种参考图输入方式：

| 参数 | 说明 | 引擎支持 |
|------|------|---------|
| `--reference` | 本地文件路径（自动上传 OSS 获取 URL） | yunwu（base64 直传）、grsai（OSS 上传→URL） |
| `--reference-url` | 已有图片 URL | grsai（gpt-image / nano-banana） |

```bash
# 本地文件参考图（自动上传 OSS）
python scripts/generate.py \
  --config config/engine.json \
  --provider grsai --model gpt-image-2 \
  --reference ./photo.jpg --prompt "watercolor style" \
  --out output.jpg

# URL 参考图
python scripts/generate.py \
  --config config/engine.json \
  --provider grsai \
  --reference-url "https://example.com/ref.png" --prompt "cartoon style" \
  --out output.jpg
```

**注意**：grsai 引擎的本地文件上传依赖阿里云 OSS（需配置 `.env` 中的 OSS 变量）。未配置时自动跳过参考图，仅执行文生图。

## 引擎与模型

| 引擎 | 模型 | 格式 | 特点 | 认证 |
|------|------|------|------|------|
| yunwu（云雾） | gemini-3.1-flash-image-preview、gemini-3-pro-image-preview | Gemini 原生 | 高质量，支持 base64 参考图 | x-goog-api-key / Bearer |
| yunwu（云雾） | gpt-image-2 | OpenAI Images（dall-e-3） | 高质量，yunwu 直连 | Bearer |
| grsai | nano-banana-2、nano-banana-pro | grsai 专有 | 快速，支持 URL 参考图 | Bearer |
| grsai | gpt-image-2 | grsai 专有 | 高质量，支持多 URL 参考图 | Bearer |

## 执行规则

1. **先读 reference**：每次触发必读对应模式的 reference 文件
2. **统一引擎**：所有生图都通过 `scripts/generate.py`
3. **自然语言优先**：用户说"赛博风"不说"cyberpunk"，也能匹配
4. **Prompt 保持原语言**：用户给什么语言就用什么语言，不翻译（中文效果已足够好）
5. **禁止扩写用户 Prompt** — **严禁对用户描述进行细节扩充、改写或"优化"。** 拼接规则：
   - raw 风格：`用户原始描述`（原样传递）
   - 有 modifier 的风格：`[modifier], 用户原始描述`（modifier 前缀 + 原文）
   - 有完整模板的风格（含 `{METAPHOR}`/`{TOPIC}` 等变量）：用用户原文的**核心名词**替换变量，不做扩写
   - **绝对禁止**：添加用户未提及的场景、元素、氛围词、构图指令
6. **不问废话** — 自动判断模式和风格，判断错了用户会纠正
7. **不解释流程** — 直接执行，输出结果
