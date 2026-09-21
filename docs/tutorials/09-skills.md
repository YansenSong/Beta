# 09：Skills——把领域流程放到 Core 之外，按需进入 Context

> 参考：[`learn-pi-agent` Chapter 09](https://github.com/yiz-hhh/learn-pi-agent/tree/main/chapters/09-skills)。

## 本章目标

理解 Skill 为什么不是 Tool，也不是 Session Entry；它更像一种**可发现、可按需读取的领域工作说明**。

## 1. 为什么不能把所有领域知识都塞进 system prompt

假设 Agent 逐渐支持：

```text
database debugging
payment debugging
deployment debugging
code review
...
```

如果每一种能力都把完整规则正文放进 system prompt，那么每次模型调用都会携带大量与当前任务无关的内容。

这既浪费 Context，也会让系统提示越来越难维护。

更合适的思路是：

```text
先告诉模型“有哪些能力”
    ↓
模型判断当前是否需要
    ↓
需要时再读取完整说明
```

## 2. Skill Catalog 只放 metadata

Beta 的 [`../../src/beta_agent/harness/skill.py`](../../src/beta_agent/harness/skill.py) 会扫描 `SKILL.md`，读取 frontmatter 中的：

```text
name
description
location
```

然后 `prompt_fragment()` 只把这些 metadata 放进 system prompt。

因此模型启动时知道：

- Skill 名字；
- 它适合解决什么问题；
- 完整正文在哪里。

但正文不会默认进入 Context。

## 3. Skill body 通过普通 Tool 进入运行时

当模型判断某个 Skill 与当前任务相关时，可以调用普通 read Tool 读取对应 `SKILL.md`。

Beta 提供 [`../../src/beta_agent/builtin_tools.py`](../../src/beta_agent/builtin_tools.py) 中的受限文本读取 Tool。

于是路径仍然是框架已经理解的路径：

```text
system prompt
└── Skill metadata

assistant
└── read(SKILL.md)

Tool Result
└── Skill body

assistant
└── 根据 Skill 继续工作
```

Agent Loop 不需要新增：

```text
load_skill()
run_skill()
```

这正是 Skill 设计最漂亮的地方：新能力沿已有 Message / Tool 边界进入，而不是要求 Core 增加特殊分支。

## 4. Skill 不是 Tool

两者职责不同：

```text
Skill
→ 告诉 Agent“遇到这类任务应该怎样思考、按什么流程做”

Tool
→ 真正执行一个动作
```

Skill 可以建议使用 `read`、`shell`、`sql`、`search` 等 Tool，但 Skill 自己通常不负责执行这些动作。

所以它更接近“领域操作手册”，而不是“函数调用”。

## 5. Skill 也不是 Session Entry

Skill 文件来自当前项目环境。

Session 记录的是：

```text
已经发生过什么
```

Skill Catalog 表示的是：

```text
当前环境里有哪些可用知识/流程
```

因此发现 Skill 不需要往 Session Tree 追加一条 `SkillEntry`。

重新加载 Session 时，可以重新扫描当前环境中的 Skills。

但如果模型真的读取了 Skill body，那么这次 `read` 返回的 Tool Result 已经属于真实运行历史，自然可以进入 Session，并在未来被 Compaction 摘要。

## 6. Lazy loading 的价值

可以把 Skill 机制理解为两级加载：

```text
一级：metadata catalog
→ 很小，每次都可以放进 system prompt

二级：full body
→ 只有相关任务才读取
```

这种模式特别适合：

- 项目规范；
- 排障 SOP；
- Code Review 指南；
- 发布流程；
- 特定领域操作说明。

## 7. Beta 当前 Skill 实现的边界

当前实现有意保持简单：

- 扫描 `**/SKILL.md`；
- 解析最小 frontmatter；
- 构造 metadata catalog；
- 正文按需读取。

更复杂的优先级、同名冲突、package 来源、诊断等，可以以后放进资源发现层，不需要修改 Agent Loop。

## 8. 这一章背后的框架原则

从 Chapter 00 到 09 可以看到同一条思路反复出现：

```text
新复杂度出现
    ↓
先尝试沿现有稳定边界进入
    ↓
只有边界确实不足时才扩展 Core
```

Skill 之所以重要，不只是因为它能放 Markdown，而是因为它证明了：领域能力可以通过 Prompt + Tool + Message 的既有机制接入，而不必让 Agent Loop 认识每一种业务。

### Catalog 实际读取了什么

[`SkillCatalog.discover()`](../../src/beta_agent/harness/skill.py) 使用 `root.glob("**/SKILL.md")` 找文件，但启动阶段虽然调用了 `read_text()`，只把内容交给 `_parse_frontmatter()`；最终对象只保留：

```python
@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    location: Path
```

`prompt_fragment()` 再把这三个字段转成 `<available_skills>` XML，并用 `xml.sax.saxutils.escape()` 处理元数据，避免名称或描述破坏标签结构。正文没有进入返回字符串。

当前仓库也没有 Core 专用的 `builtin_tools.py`。产品层 Coding Agent 把 [`create_read_file_tool()`](../../src/coding_agent/tools/read_file.py) 注册成普通 Tool；模型从 catalog 得到 location 后，再以普通 Tool Call 读取 `SKILL.md`。因此“按需加载”不是隐藏的 Skill Runtime API，而是一次可观察、可持久化的标准 Tool 往返。

## 9. 掌握标准

你应该能回答：

- Skill 为什么只把 metadata 放进 system prompt？
- Skill body 为什么通过 read Tool 加载，而不是 Runtime 特殊 API？
- Skill 与 Tool 的职责区别是什么？
- 为什么 Skill Catalog 不应该直接存成 Session Entry？
- 模型读取 Skill body 后，为什么对应 Tool Result 又应该进入 Session？

到这里，Beta 当前 Core 的 00～09 学习路径就完整了。接下来如果继续向原教程 Chapter 10～12 前进，重点会从“稳定 Core”转向可加载 Extension 与 Coding Agent Harness。
