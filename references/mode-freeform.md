# 自由生图模式（freeform）

最灵活的模式。用户提供任意描述，选择风格和比例，直接生成图片。适合配图、插图、概念可视化等场景。

所有风格修饰词统一存储在 `config/prompts.yaml` 的 `freeform` 节。

## 一、6 种风格速查

按类别组织，选择时优先匹配用户意图中的风格暗示词。

| # | 风格 ID | 名称 | 关键词 | 适用 | 支持比例 |
|---|---------|------|--------|------|---------|
| 1 | visual_note | 视觉笔记 | 视觉笔记/手绘笔记/信息图 | 扁平信息图风，白底蓝橙点缀 | 4:3, 16:9 |
| 2 | hand_drawn | 手绘插画 | 手绘/插画/可爱 | 手绘彩铅风，暖色系，可爱比例 | 3:4, 1:1 |
| 3 | tech | 科技蓝图 | 科技/技术/未来/赛博 | 科技感，电路/数据流，深色背景 | 4:3, 16:9 |
| 4 | blueprint | 工程蓝图 | 蓝图/工程/架构/系统 | 工程蓝图风，坐标网格，标注线 | 4:3, 16:9 |
| 5 | sketch | 铅笔素描 | 素描/线稿/速写 | 铅笔素描，黑白，简洁线条 | 4:3, 1:1 |
| 6 | minimal | 苹果极简 | 极简/简约/干净 | 极简主义，大量留白，单色点缀 | 4:3, 1:1 |

**默认风格**：无明确暗示时使用 **visual_note**。

## 二、工作流

### Step 1：解析用户 Prompt

- 提取用户的图像描述（中文或英文均可）
- 如果是中文描述，翻译为英文以获得更好效果
- 翻译原则：保留关键视觉元素，补充风格化描述词

### Step 2：选择风格

- 扫描用户描述中的风格暗示词（如"手绘""科技感""极简"）
- 匹配上表中的风格
- 无明确暗示时，默认使用 **visual_note**

### Step 3：选择比例

| 触发条件 | 比例 |
|---------|------|
| 公众号/横版/landscape/4:3 | 4:3 |
| 小红书/竖版/portrait/3:4 | 3:4 |
| 16:9/宽屏/widescreen | 16:9 |
| 方形/square/1:1 | 1:1 |
| 无指定 | 4:3（默认） |

### Step 4：组装 Prompt

从 `config/prompts.yaml` 的 `freeform` 节读取对应风格的 `modifier`，拼接：

```
[风格 modifier] [用户描述翻译], no text no watermark, [比例] composition
```

### Step 5：调用 generate.py

```bash
# 快捷方式（--style 自动读取 modifier 和比例）：
python scripts/generate.py \
  --config config/engine.json \
  --style tech --prompt "a futuristic city with flying cars" \
  --out "[输出路径]"

# 手动方式：
python scripts/generate.py \
  --config config/engine.json \
  --prompt "[组装好的英文 prompt]" \
  --out "[输出路径]" \
  --aspect-ratio "[比例]"
```

### Step 6：报告输出 + 飞书推送

```
生图完成
- 风格：[风格名]（[风格ID]）
- 路径：[绝对路径]
- 比例：[比例]
```

**自动推送飞书**（生成完成后自动执行）：
```bash
lark-cli im +messages-send --chat-id oc_77e72a8fa29356bf8942ac739bbd6dd5 --as bot --file [图片路径]
```

## 三、注意事项

1. **中文描述要翻译** — AI 图像生成模型对英文 prompt 效果更好
2. **风格修饰词在前** — 先写风格词，再写内容描述
3. **固定追加 `no text no watermark`** — 避免生成带文字/水印的图片
4. **输出目录** — 默认写入 `output/` 下按主题命名的子目录
5. **快速路径** — 用户只给一句话描述时，跳过所有确认，直接生成
