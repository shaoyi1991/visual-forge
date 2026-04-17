# AGENTS.md

> 本文件面向 AI 编程助手（Claude Code、Cursor、Copilot 等），帮助 AI 快速理解项目并正确操作。

## 项目概要

Visual Forge 是一个零外部依赖的 AI 图像生成引擎。Python stdlib 实现，通过自然语言驱动，支持 28 种预设风格和双引擎 fallback。

## 技术栈

- Python 3.10+（stdlib only，无 pip install 依赖）
- Gemini generateContent API（yunwu 代理）
- grsai nano-banana API（备用引擎）

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
1. 先尝试 yunwu（Gemini API），重试 2 次
2. 全部失败后 fallback 到 grsai（nano-banana），重试 1 次
3. 都失败则报错

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
- Prompt 必须英文，必须包含 `no text no watermark`

### 添加新引擎（Provider）

1. `.env` 添加环境变量（URL + Key）
2. `config/engine.json` 的 `providers` 下注册新 provider
3. `scripts/generate.py` 添加 `_generate_via_<engine>()` 函数
4. 在 `main()` 的 provider 分发逻辑中加入新引擎分支

## 注意事项

- `.env` 文件包含 API 密钥，已被 `.gitignore` 忽略，**禁止提交**
- 所有 Prompt 模板必须英文（AI 图像模型对英文理解更好）
- `config/previews/` 中的预览图需要包含在 Git 中（!config/previews/ 在 .gitignore 中有例外规则）
- 风格画廊 HTML 中的 STYLES 数据需要与 prompts.yaml 保持同步

## 更新同步流程

当主项目的生图技能发生变更时，按以下步骤同步到独立仓库：

```bash
# 1. 进入独立仓库目录
cd C:/Users/Administrator/AppData/Local/Temp/visual-forge

# 2. 复制变更文件（按需选择）
cp <主项目>/.claude/skills/image/config/prompts.yaml   config/
cp <主项目>/.claude/skills/image/config/engine.json     config/
cp <主项目>/.claude/skills/image/scripts/generate.py    scripts/
# 路径注意：generate.py 中 _init_dotenv() 的 project_root 路径需要适配：
#   主项目：script_dir.parent.parent.parent.parent
#   独立仓库：script_dir.parent.parent

# 3. 提交推送
git add -A
git commit -m "feat: 描述变更"
git push
```

**不需要同步的文件**：
- `SKILL.md` — Claude Code 技能定义，仅主项目使用
- 飞书推送相关的 chat_id 配置
