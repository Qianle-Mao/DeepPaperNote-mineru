# DeepPaperNote-mineru

这是基于 DeepPaperNote 改造的 MinerU 版 Codex/agent 论文精读笔记 skill。

这个版本保留原链路中的证据 grounding、图表决策、lint 校验和 Obsidian 写入流程，但改了两个默认行为：

- 文档解析默认使用 MinerU v4 精准 API，并开启 OCR
- 数据挖掘 / AI 类论文默认输出为定制的 16 节中文精读版格式

## 功能

给定论文标题、DOI、URL、Zotero 条目或本地 PDF 后，skill 会：

1. 解析论文身份和元数据
2. 获取或复用最佳 PDF
3. 通过 MinerU v4 精准 API 解析文档
4. 生成 canonical source artifacts 和 evidence manifests
5. 规划图表放置
6. 用 source sections/pages 校验 note plan
7. 写出 Markdown 深度笔记
8. 运行结构、风格、公式、图表、planning 和实质内容 lint
9. 在配置了 Obsidian vault 时写入 Obsidian

## MinerU 配置

运行前设置环境变量：

```bash
export MINERU_API_TOKEN="your-mineru-token"
```

或：

```bash
export DEEPPAPERNOTE_MINERU_API_TOKEN="your-mineru-token"
```

不要把 API token 写进 `SKILL.md`、脚本、笔记、日志或 Git 提交文件。

默认 MinerU 参数：

- API 模式：v4 精准 API
- 模型版本：`vlm`
- OCR：开启
- 公式识别：开启
- 表格识别：开启
- 语言：`ch`

可覆盖的环境变量：

- `MINERU_MODEL_VERSION`
- `MINERU_IS_OCR`
- `MINERU_ENABLE_FORMULA`
- `MINERU_ENABLE_TABLE`
- `MINERU_LANGUAGE`
- `MINERU_PAGE_RANGES`
- `MINERU_TIMEOUT_SECONDS`
- `MINERU_POLL_INTERVAL_SECONDS`

这些变量也都有对应的 `DEEPPAPERNOTE_...` 写法。

## 输出格式

数据挖掘和 AI 类论文默认使用 `references/data-mining-ai-note-format.md` 中的精读版结构：

- `# 论文精读笔记`
- 从 `1. 论文基本信息` 到 `16. 总评` 的 16 个编号章节
- 强调技术动机、方法框架、核心公式、算法流程、实验设计、创新判断、局限性和后续研究选题

## 安装

克隆到 Codex skills 目录：

```bash
git clone <this-repo-url> ~/.codex/skills/DeepPaperNote-mineru
```

然后重启 Codex，让 skill metadata 重新加载。

skill 名称是：

```text
deeppapernote-mineru
```

## Python 依赖

本地运行脚本和测试：

```bash
python3 -m pip install -e '.[dev]'
```

虽然文本解析默认走 MinerU，但链路仍使用 `PyMuPDF` 处理 PDF 页面信息、图像资产和显式开启的 fallback，因此依赖中仍保留 `PyMuPDF`。

## Obsidian 输出

如果希望直接写入 Obsidian，设置：

```bash
export DEEPPAPERNOTE_OBSIDIAN_VAULT="/absolute/path/to/your/Obsidian Vault"
```

如果没有配置 vault，skill 会先询问，再决定是否使用工作区 fallback。

## 开发检查

运行测试：

```bash
python3 -m pytest -q
```

运行语法检查：

```bash
python3 -m py_compile scripts/*.py
```

## 安全约束

- MinerU token 只从环境变量读取。
- MinerU 失败时默认 fail closed，除非显式设置 `DEEPPAPERNOTE_CUSTOM_ALLOW_PYMUPDF_FALLBACK=1`。
- 最终笔记必须通过 lint 才会写入 Obsidian。

## 上游

这是 DeepPaperNote 的本地定制 fork。建议把上游更新和本地解析、格式、保存策略改造分开维护。
