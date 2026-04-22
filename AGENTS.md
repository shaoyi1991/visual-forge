# AGENTS.md

> 本文件面向 AI 编程助手（Claude Code、Cursor、Copilot 等），帮助 AI 快速理解项目并正确操作。

## 项目概要

Visual Forge 是一个零核心依赖的 AI 图像生成引擎。Python stdlib 实现，通过自然语言驱动，支持 28 种预设风格和双引擎 fallback。支持图生图（本地文件自动上传 OSS）。

## 安装步骤（AI 助手执行）

收到「帮我安装 Visual Forge」的请求后，按以下步骤执行：

### 1. 克隆仓库

```bash
git clone https://github.com/shaoyi1991/visual-forge.git
cd visual-forge
```

### 2. 检查 Python 版本

```bash
python --version  # 需要 3.10+
```

如果版本低于 3.10，提示用户安装或升级 Python。

### 3. 配置环境变量

```bash
cp .env.example .env
```

然后引导用户填入至少一组 API 密钥。两种引擎只需配置一种：

- **yunwu** 注册地址：https://yunwu.ai/register?aff=ml8W
- **grsai** 注册地址：海外 https://grsai.com/zh / 国内直连 https://grsai.ai/zh

**方案 A：yunwu（Gemini 代理，推荐）**
```bash
LLM_API_KEY=sk-xxx        # 必填
LLM_BASE_URL=https://yunwu.ai/v1  # 默认值，一般不改
```

**方案 B：grsai（nano-banana / gpt-image，备用）**
```bash
BANANA_API_URL=http://grsai.dakka.com.cn/v1/draw/nano-banana  # 默认值
BANANA_API_KEY=sk-xxx      # 必填
GRSAI_DRAW_API_URL=https://grsai.dakka.com.cn/v1/draw/completions  # gpt-image 端点
```

**可选：图生图 OSS 上传（不配置则跳过参考图上传，继续文生图）**
```bash
OSS_ACCESS_KEY_ID=xxx
OSS_ACCESS_KEY_SECRET=xxx
OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com
OSS_BUCKET=your-bucket
```

### 4. 验证安装

```bash
# 使用任意引擎生成一张测试图
python scripts/generate.py \
  --config config/engine.json \
  --style visual_note --prompt "a cute cat reading a book" \
  --out test_output.jpg
```

成功输出 `test_output.jpg` 即安装完成。如果失败，根据错误信息排查：
- `缺少 API Key` → 检查 .env 文件是否正确填写
- `连接超时` → 检查网络，尝试切换 `VF_PROVIDER=grsai`
- `未找到 Python` → 确认 python3 在 PATH 中

### 5. 浏览风格画廊

```bash
# 用浏览器打开风格画廊，查看 28 种预设风格
python -c "import webbrowser; webbrowser.open('config/style-gallery.html')"
# 或直接双击 config/style-gallery.html
```


## 技术栈

- Python 3.10+（stdlib only，可选 oss2 用于图生图本地上传）
- Gemini generateContent API（yunwu 代理）
- grsai 统一引擎（nano-banana + gpt-image）

## 关键文件

| 文件 | 作用 | 修改频率 |
|------|------|---------|
| `scripts/generate.py` | 生图引擎入口（CLI） | 低 |
| `config/engine.json` | 引擎注册、模型列表、非敏感配置 | 中（加模型时改） |
| `config/prompts.yaml` | 28 种风格定义（名称、关键词、Prompt 模板） | 中（加风格时改） |
| `config/style-gallery.html` | 风格画廊可视化浏览器（内含 STYLES JS 数据） | 低 |
| `config/previews/` | 28 张风格预览图 + 画廊总览截图 | 低（风格变更时更新） |
| `.env` | API 密钥和输出参数（不提交到 Git） | 低 |

## 运行方式

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 API Key

# 2. 生成图片
python scripts/generate.py \
  --config config/engine.json \
  --style cyberpunk --prompt "a cat in neon city" \
  --out output.jpg

# 3. 自由 Prompt（不使用预设风格）
python scripts/generate.py \
  --config config/engine.json \
  --prompt "a watercolor painting of mountains" \
  --out output.jpg --aspect-ratio "4:3"
```

## 架构要点

### 双引擎 Fallback

`VF_PROVIDER=auto`（默认）时：
1. 先尝试 yunwu：
   - `gpt-image-*` 模型 → OpenAI Images 格式（`/v1/images/generations`，dall-e-3 兼容）
   - 其他模型（Gemini 系列）→ Gemini 原生格式（`/v1/models/{model}:generateContent`）
   - 重试 2 次
2. 全部失败后 fallback 到 grsai 统一引擎（按模型名自动路由 nano-banana 或 gpt-image 端点），重试 1 次
3. 都失败则报错

### yunwu 代理兼容

Python `urllib` 在系统代理（HTTP_PROXY）环境下可能连接断开。`_generate_via_openai_images` 函数自动处理：
- 先清除代理变量尝试 urllib
- 失败则降级 curl 子进程（同样清代理）

### 图生图（参考图）

- `--reference`：本地文件路径（yunwu 用 base64 inlineData，grsai 自动上传 OSS 获取 URL）
- `--reference-url`：已有图片 URL（grsai 专用）
- OSS 未配置时，本地参考图上传跳过，继续执行文生图

### --style 参数

指定 `--style <风格ID>` 时，从 `config/prompts.yaml` 自动读取：
- prompt 模板（`prompt` 或 `modifier` 字段）
- 默认比例（`ratio` 字段）
- 用户 `--prompt` 内容替换模板中的 `{METAPHOR}`/`{TOPIC}` 等变量

### 简易 YAML 解析器

`_load_prompts_yaml()` 是 stdlib-only 的简易解析器，仅支持 prompts.yaml 的二级嵌套结构。如果 prompts.yaml 的结构发生变化，需要同步修改该函数。

## 扩展指南（AI 操作时参考）

### 添加新模型

编辑 `config/engine.json` → `providers.<provider>.models` 数组追加新模型对象。

### 添加新风格

编辑 `config/prompts.yaml` → 对应场景节下追加新风格。规则：
- 封面用 `{METAPHOR}` 变量
- 信息图用 `{TOPIC}` 变量
- 自由生图用 `modifier` 字段（用户描述追加在后面）
- PPT 用 `{title}`, `{subtitle}`, `{stats}` 变量
- Prompt 模板必须是英文（keywords 匹配和模板渲染使用英文），用户输入中英文均可不翻译

### 添加新引擎（Provider）

1. `.env` 添加环境变量（URL + Key）
2. `config/engine.json` 的 `providers` 下注册新 provider
3. `scripts/generate.py` 添加 `_generate_via_<engine>()` 函数
4. 在 `main()` 的 provider 分发逻辑中加入新引擎分支

## 注意事项

- `.env` 文件包含 API 密钥，已被 `.gitignore` 忽略，**禁止提交**
- Prompt **模板**必须是英文（AI 图像模型对英文理解更好），但用户输入描述保持原语言不翻译
- `config/previews/` 中的预览图需要包含在 Git 中（!config/previews/ 在 .gitignore 中有例外规则）
- 风格画廊 HTML 中的 STYLES 数据需要与 prompts.yaml 保持同步
