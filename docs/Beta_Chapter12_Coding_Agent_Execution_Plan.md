# Beta Agent Chapter 12：Coding Agent 扩展实施计划

> 本文档面向后续 Codex 执行。目标是基于当前 Beta Agent 00～11 章已经形成的 Core / Extension Runtime，在**不重写 Agent Loop、不复制 Runtime 能力**的前提下，实现 Chapter 12 的 Coding Agent 产品层组装。

## 0. 基线与参考版本

执行前必须以以下版本为基线重新确认仓库状态；若目标仓库 `main` 已前移，先比较差异，再按本文“架构约束”适配，不要机械覆盖用户后续修改。

- 目标仓库：`YansenSong/Beta`
  - 当前审阅基线：`d267cd8992418cb2efc9e1110269801251eed6b0`
  - 当前 Core 已覆盖 00～11 章：Agent Loop、Tool Runtime、并行 Tool、Steering / Follow-up、Context Transform、Session Tree、Compaction、Skills、Extension Runtime、Permission / Plan / Subagent 组合。
- 教程参考：`yiz-hhh/learn-pi-agent`
  - 当前参考提交：`fc9dbf67d68743d79ea6b44361faadfeebab4985`
  - 重点目录：`chapters/12-coding-agent/`
- Pi 官方源码参考：`earendil-works/pi`
  - 当前参考提交：`71dca871bc80b6bc97be37f0ca3189399d651fff`
  - 重点目录：
    - `packages/coding-agent/src/core/agent-session-runtime.ts`
    - `packages/coding-agent/src/core/agent-session.ts`
    - `packages/coding-agent/src/core/system-prompt.ts`
    - `packages/coding-agent/src/core/tools/`

### 参考结论

Chapter 12 的本质不是新增一种 Agent Loop，而是把以下既有机制组装成一个具体 Coding Agent 产品：

```text
Model Adapter
    +
Agent / ToolRuntime
    +
Session / Compaction
    +
Skills
    +
ExtensionRunner / ExtensionHost
    +
Coding Tools
    +
Coding Prompt / Workspace
    =
Coding Agent Runtime
```

因此本章实现必须遵循：**新增产品层，不侵入 Core 控制流。**

---

# 1. 当前 Beta 状态判断

## 1.1 已经可以直接复用的能力

当前 Beta 已有以下能力，本章不得重复实现：

1. `Agent`
   - 负责 turn / tool-driven loop；
   - 维护 canonical messages；
   - 支持 `transform_context`、`before_tool_call`、`after_tool_call`；
   - 支持 Steering / Follow-up。

2. `ToolRuntime`
   - Tool lookup；
   - `prepare_arguments`；
   - Pydantic 参数验证；
   - before / after hook；
   - parallel / sequential execution；
   - Tool 异常统一转换成模型可见 error ToolResult；
   - `ToolExecutionContext.progress()` 已存在。

3. `SessionTree`
   - append-only；
   - branchable；
   - `reconstruct_messages()`；
   - JSONL save / load。

4. `compact_session()`
   - append-only compaction entry；
   - 不删除历史；
   - 从 Session 重建压缩后的 canonical context。

5. `SkillCatalog`
   - 启动阶段只加载 `name / description / location`；
   - Skill 正文留在文件系统，按需通过 Tool 读取。

6. Extension Runtime
   - `ExtensionRunner`；
   - `ExtensionHost`；
   - `register_tool / register_command / on(...)`；
   - tool_call 短路语义；
   - context pipeline；
   - active tools 动态切换。

Chapter 12 应只把上述对象组装起来。

## 1.2 Beta 与教程 Chapter 12 的关键差异

这些差异必须按 Beta 现状适配，禁止逐行照抄 TypeScript 教程。

### 差异 A：Session 已由 ExtensionHost 在线写入

当前 `ExtensionHost` 在收到 `message_end` 后会：

```python
runner.session.append_message(event.message)
```

因此 Coding Agent 的 `save_session()` **不能再遍历 `agent.messages` 并 append 到 Session**。

否则会产生重复历史：

```text
Host 已 append
    +
save_session 再 append
    =
同一消息重复两次
```

正确设计：

```text
run through ExtensionHost
    ↓
message_end 自动 append 到 SessionTree
    ↓
save_session()
    ↓
可选 compaction
    ↓
SessionTree.save_jsonl()
```

### 差异 B：Beta 当前 JSONL persistence 是完整重写

教程 Chapter 12 有 `savedCount + appendEntries()` 的增量落盘方案；Beta 当前 `SessionTree.save_jsonl()` 是把整个树重新写入文件。

本章应优先复用现有 API：

```python
session.save_jsonl(path)
```

不要为了 Chapter 12 再造第二套 Session 文件格式。

“原子文件替换、增量 append、崩溃恢复”属于后续 persistence hardening，不是本章必需项。

### 差异 C：Beta Skill 规范是 `**/SKILL.md`

教程 fixture 使用 `skills/verify.md`；Beta 当前 `SkillCatalog.discover()` 只识别：

```text
skills/<skill-name>/SKILL.md
```

Chapter 12 测试和示例应遵守 Beta 已有规范，不要为了复制教程而修改 `skills.py`。

建议 fixture：

```text
skills/
└── verify/
    └── SKILL.md
```

### 差异 D：现有通用 read Tool 有 sandbox-like root 限制

`make_read_text_file_tool(root)` 会阻止路径逃出 root。

而 Chapter 12 的 Coding Tool 路径语义明确是：

```text
cwd = 路径解析基点
cwd != sandbox
```

例如：

```text
../outside.txt
/absolute/path/file.txt
```

路径解析器本身不应拒绝。

因此：

- **不要修改**现有 `make_read_text_file_tool()`；
- **不要直接复用**它实现 Chapter 12 的 `read_file`；
- 新增 Coding Agent 专属 path resolver 与 `read_file`。

Trust、Sandbox、文件访问策略必须作为独立边界处理，本章不伪装成已经解决。

### 差异 E：ExtensionHost 才是完整运行入口

如果 Coding Agent 装配了 Extension，却绕过 Host 直接调用：

```python
assembly.agent.run(...)
```

则 Observe event / Session persistence 不会经过 `ExtensionHost`。

因此 Coding Agent 应暴露自己的 facade：

```python
runtime.run(...)
runtime.stream(...)
runtime.run_command(...)
```

内部统一走 Host。

`agent` 可以保留给调试和状态查看，但文档和示例不能把它作为常规运行入口。

---

# 2. 本章目标与非目标

## 2.1 必须完成的目标

实现一个可真实运行的 Python Coding Agent，它至少具备：

```text
read_file
write_file
edit
grep
bash
```

并把它们与以下能力组装：

```text
workspace cwd
coding system prompt
SkillCatalog
ExtensionRunner / ExtensionHost
SessionTree
optional Compaction
ModelAdapter
```

最终必须能够在离线 scripted model 驱动下跑通一次完整 Bug Fix：

```text
危险 bash 被拦截
    ↓
读取 verify Skill
    ↓
grep 定位
    ↓
read_file 读取源码
    ↓
运行测试，看到失败
    ↓
edit 修复
    ↓
重新运行测试，成功
    ↓
最终回答
    ↓
Session 持久化并可恢复继续
```

## 2.2 明确不在本章实现

以下内容不要顺手塞进 Chapter 12：

- 新 Agent Loop；
- 新 Tool Runtime；
- TUI；
- MCP；
- GitHub / Git 集成工作流；
- Docker / OS sandbox；
- 完整 Trust model；
- 远程 workspace；
- SSH execution；
- 图像文件读取；
- Pi 完整 `find / ls / powershell` 工具；
- ripgrep 强依赖；
- fuzzy edit；
- 完整 process-tree manager；
- hot reload；
- Extension package discovery；
- 多 Agent scheduler；
- 自动 Git commit；
- repeated compaction 的高级优化。

如果实现过程中发现必须修改 `Agent._run()` 或复制 ToolRuntime，请先停止并重新评估架构，而不是继续堆逻辑。

---

# 3. 目标目录结构

建议新增一个明确的产品层包：

```text
src/beta_agent/
├── agent.py
├── tools.py
├── session.py
├── compaction.py
├── skills.py
├── extensions/
│   └── ...                     # 00-11 已有机制
│
└── coding/
    ├── __init__.py
    ├── assembly.py              # create_coding_agent / CodingAgentRuntime
    ├── prompt.py                # build_coding_system_prompt
    ├── extensions/
    │   ├── __init__.py
    │   └── permission_gate.py   # 可复用产品策略；仍在 Core 外
    └── tools/
        ├── __init__.py
        ├── path_utils.py
        ├── read_file.py
        ├── write_file.py
        ├── edit.py
        ├── grep.py
        └── bash.py

examples/
├── deepseek_cli.py              # 保留原普通聊天示例
├── coding_agent_cli.py          # 新增 Coding Agent CLI
└── extensions/
    └── ...                      # Chapter 11 示例保留

tests/
├── ...                          # 原测试保留
├── test_coding_tools.py
├── test_coding_assembly.py
└── test_coding_e2e.py

tests/fixtures/coding_project/
├── src/
│   ├── __init__.py
│   └── calculator.py            # 故意存在 a - b bug
├── tests/
│   └── test_calculator.py
├── skills/
│   └── verify/
│       └── SKILL.md
└── keep/
    └── keep.txt

docs/tutorials/
└── 12-coding-agent.md
```

如现有项目目录已经发生变化，可按同等职责映射，但不要把 `coding` 产品代码散进 `agent.py / tools.py / extensions/runner.py`。

---

# 4. Coding Tool 设计规范

# 4.1 `path_utils.py`

新增：

```python
resolve_tool_path(cwd: Path, path: str) -> Path
```

语义：

1. `cwd` 在 assembly 创建时先 `resolve()`；
2. 相对路径：基于 `cwd` 解析；
3. 绝对路径：直接解析；
4. 可 `expanduser()`；
5. 不做 `relative_to(cwd)` 限制；
6. 不把路径解析器命名成 sandbox / safe path；
7. 错误信息必须包含用户传入路径，便于模型自修正。

必须增加测试确认：

```text
cwd/project + ../outside.txt
```

可以解析并读取。

同时在文档明确：这不是安全保证。

---

# 4.2 `read_file`

建议参数模型：

```python
class ReadFileArgs(BaseModel):
    path: str
    offset: int | None = None   # 1-based
    limit: int | None = None
```

约束：

- `offset >= 1`；
- `limit >= 1`；
- UTF-8 文本读取；
- 不存在/不可读 → 抛异常，由 ToolRuntime 转成 error ToolResult；
- 支持按行 offset / limit；
- offset 超过文件末尾 → 明确失败；
- 默认输出限制：
  - `READ_MAX_LINES = 500`
  - `READ_MAX_BYTES = 64 * 1024`
- 超限保留头部，并附续读提示：

```text
[显示第 X-Y 行，共 N 行。用 offset=Z 继续。]
```

- `ToolResult.details` 在截断时带：

```python
{
    "truncation": {
        "truncated": True,
        "output_lines": ...,
        "total_lines": ...,
    }
}
```

执行模式：

```python
execution_mode="parallel"
```

原因：纯读取，可和其他 read / grep 并行。

不要直接把当前 `make_read_text_file_tool()` 改名复用，因为它有不同的 root containment contract。

---

# 4.3 `write_file`

参数：

```python
class WriteFileArgs(BaseModel):
    path: str
    content: str
```

行为：

1. 解析路径；
2. 自动创建 parent directories；
3. 文件存在则覆盖；
4. UTF-8 写入；
5. 成功返回实际 UTF-8 byte count，而不是 Python 字符数；
6. I/O failure 抛异常。

建议成功文本：

```text
Successfully wrote <N> bytes to <path>
```

或保持项目中文风格，但测试不要依赖完整自然语言，仅依赖核心字段。

执行模式：

```python
execution_mode="sequential"
```

原因：Beta 当前还没有 Pi 的 per-file mutation queue。Chapter 12 第一版宁可保守串行，也不要允许两个 mutation Tool 并发改文件。

---

# 4.4 `edit`

这是本章最重要的确定性修改工具。

参数结构：

```python
class EditItem(BaseModel):
    old_text: str = Field(alias="oldText")
    new_text: str = Field(alias="newText")

class EditArgs(BaseModel):
    path: str
    edits: list[EditItem]
```

Pydantic 应允许 JSON alias，使模型 schema 与教程/Pi 接近：

```json
{
  "path": "src/calculator.py",
  "edits": [
    {
      "oldText": "return a - b",
      "newText": "return a + b"
    }
  ]
}
```

## `prepare_arguments`

必须复用现有 Tool 的 `prepare_arguments` seam，实现兼容输入：

1. `edits` 为 JSON string → 尝试 parse；
2. `edits` 为单个 object → 包成 list；
3. legacy 顶层：

```json
{
  "path": "...",
  "oldText": "...",
  "newText": "..."
}
```

→ 归一化到 `edits[]`。

参数归一化只负责 shape，不负责文件语义验证。

## 确定性 edit 规则

所有匹配都必须基于**修改前的同一份原文件**：

```text
original file
  ├─ match edit A
  ├─ match edit B
  └─ match edit C
```

禁止：

```text
apply A
  ↓
在 A 修改后的文件上匹配 B
```

每个 `oldText`：

- 空字符串 → fail；
- 0 次匹配 → fail；
- 1 次匹配 → allowed；
- >1 次匹配 → fail；
- 任意两个匹配区间 overlap / nested → fail。

全部 validation 成功后才执行一次写入，避免半修改状态。

## 文本与 details

建议使用二进制读取后 UTF-8 decode，避免 Python text newline translation 误改 CRLF。

最低要求：

- 未被 replacement 覆盖的内容保持不变；
- 保留原始行尾形式；
- 最好保留 UTF-8 BOM；
- 成功返回：替换数量；
- details 至少：

```python
{
    "diff": "...",
    "first_changed_line": 12,
}
```

推荐同时使用 `difflib.unified_diff()` 提供：

```python
"patch": "..."
```

这部分是对教程教学版的轻量增强，来源于 Pi 官方 edit 的 details 语义，不要求引入 fuzzy edit。

执行模式：

```python
execution_mode="sequential"
```

---

# 4.5 `grep`

参数建议：

```python
class GrepArgs(BaseModel):
    pattern: str
    path: str = "."
    glob: str | None = None
    ignore_case: bool = Field(False, alias="ignoreCase")
    literal: bool = False
    context: int = 0
    limit: int = 100
```

实现范围：纯 Python，不强依赖 `rg`。

行为：

1. path 可以是文件或目录；
2. path 不存在 → fail；
3. 目录递归；
4. 跳过至少：

```text
.git
node_modules
dist
build
__pycache__
.venv
```

5. `literal=False`：Python regex；
6. invalid regex → fail；
7. `literal=True`：`re.escape(pattern)`；
8. `ignoreCase` → `re.IGNORECASE`；
9. glob 只要求最小文件过滤语义；
10. 默认 `limit=100`；
11. 达上限必须有明确提示和 details；
12. 无匹配不是错误，返回：

```text
No matches found
```

输出格式对齐 Chapter 12：

```text
path/to/file.py:12: matched line
```

context line：

```text
path/to/file.py-11- previous
path/to/file.py:12: matched
path/to/file.py-13- next
```

执行模式：

```python
execution_mode="parallel"
```

注意：不要把 grep 放回通用 Core。它是 Coding Agent 产品工作流工具。

---

# 4.6 `bash`

参数：

```python
class BashArgs(BaseModel):
    command: str
    timeout: float | None = None
```

初版建议用：

```python
asyncio.create_subprocess_shell(...)
```

并设置：

```python
cwd=workspace
stdout=PIPE
stderr=STDOUT
```

以获得合并输出。

## timeout

- `timeout` 必须是有限正数；
- 使用 `asyncio.wait_for(process.communicate(), timeout)`；
- timeout 时终止子进程并等待其退出；
- 错误文本保留截至 timeout 的输出。

## cancellation

Beta ToolRuntime 会透传 `asyncio.CancelledError`。

因此 bash handler 必须：

```python
try:
    ... await process ...
except asyncio.CancelledError:
    terminate/kill process
    await process.wait()
    raise
```

禁止让 Agent task 被取消后留下后台 shell 继续执行。

## exit code

- exit code 0 → success ToolResult；
- non-zero → raise RuntimeError，错误文本必须包含：
  - command output；
  - exit code。

由现有 ToolRuntime 转换为：

```text
Message(role="tool", is_error=True)
```

## output truncation

建议：

```text
BASH_MAX_BYTES = 64 KiB
```

超限保留**尾部**，因为编译/测试错误往往位于输出末端。

必须按 UTF-8 安全方式截断，不要把多字节字符切成非法文本。

## 权限边界

`bash.execute()` 不负责判断命令是否“允许”。

策略链仍是：

```text
bash ToolCall
   ↓
before_tool_call
   ↓
permission extension
   ↓
allow / block
   ↓
bash execute
```

Permission Gate 是策略，不是 Bash 实现的一部分。

执行模式建议：

```python
execution_mode="sequential"
```

原因：shell command 可以产生任意 workspace side effect。第一版不要与 edit/write 并发执行。

## 平台范围

Chapter 12 第一版可以声明以 POSIX-like shell 环境为主要支持目标。

不要把 PowerShell 支持硬塞进 `bash`；Pi 官方也是独立的 Bash / PowerShell Tool。跨平台 shell 细化留后续章节。

---

# 5. Coding Tool 工厂

`src/beta_agent/coding/tools/__init__.py` 提供单一组装入口：

```python
def create_coding_tools(cwd: str | Path) -> list[Tool]:
    return [
        create_read_file_tool(cwd),
        create_write_file_tool(cwd),
        create_edit_tool(cwd),
        create_grep_tool(cwd),
        create_bash_tool(cwd),
    ]
```

建议稳定工具名：

```text
read_file
write_file
edit
grep
bash
```

原因：

- 与当前 Chapter 12 教程一致；
- 不破坏现有 `read_text_file`；
- Permission Gate 已监听 `bash`；
- `edit / grep / bash` 与 Pi 官方概念一致。

不要给同一个工具同时注册多个 alias，避免 provider schema 和 active-tools 管理变复杂。

---

# 6. Coding System Prompt

新增：

```python
build_coding_system_prompt(...)
```

输入建议：

```python
cwd: Path
tools: Sequence[Tool]
skills: SkillCatalog
prefix: str | None
append: str | None = None
```

输出至少包含四块：

## 6.1 Role

```text
你是一个编码助手。你可以读取文件、搜索代码、执行命令、精确编辑文件和写入新文件。
```

不要在 prompt 中宣称 Tool 有 sandbox，除非实际实现了 sandbox。

## 6.2 Available tools

从实际 `tools` 生成，不要维护第二份手写列表：

```text
Available tools:
- read_file: ...
- write_file: ...
- edit: ...
- grep: ...
- bash: ...
```

描述可直接来自 `Tool.description`。

## 6.3 Guidelines

至少包括：

- 先探索再修改；
- 精确修改优先用 `edit`；
- 创建/完整覆写文件才用 `write_file`；
- 修改后运行相关测试/验证；
- 文件路径清晰；
- Tool 失败时读取真实错误后再决定下一步；
- 不假设命令成功；
- 不把 Tool Result 伪造成已经执行的事实。

## 6.4 Skills + cwd

调用：

```python
catalog.prompt_fragment()
```

只注入 metadata，不注入 Skill 正文。

最后追加：

```text
Current working directory: <absolute cwd>
```

Pi 官方 system prompt 同样把 tool information、skills、project context、cwd 放在产品层组装，不应塞进 Agent Core。

---

# 7. Coding Agent Assembly

新增 `src/beta_agent/coding/assembly.py`。

## 7.1 Options

建议定义：

```python
@dataclass(slots=True)
class CodingAgentOptions:
    cwd: str | Path
    model: ModelAdapter
    model_name: str | None = None

    system_prompt_prefix: str | None = None
    append_system_prompt: str | None = None

    skill_roots: Sequence[str | Path] = ()
    extensions: Sequence[ExtensionFactory] = ()
    extra_tools: Sequence[Tool] = ()

    session_file: str | Path | None = None
    compaction: CodingCompactionOptions | None = None
```

不要从具体 `OpenAICompatibleAdapter` 类型反向依赖 assembly；输入使用 `ModelAdapter` protocol。

`model_name` 只给 `RuntimeConfig` / diagnostics 使用；如果未给，可：

```python
getattr(model, "model", "")
```

但不要把这个 fallback 当成协议要求。

## 7.2 Compaction options

建议：

```python
@dataclass(slots=True)
class CodingCompactionOptions:
    keep_last_messages: int
    summarize: Summarizer
    estimate_tokens: Callable | None = None
```

默认：

```python
compaction=None
```

不要在 Chapter 12 强行给所有 Coding Agent 自动开摘要。

## 7.3 返回 facade

建议：

```python
@dataclass(slots=True)
class CodingAgentRuntime:
    agent: Agent
    host: ExtensionHost
    runner: ExtensionRunner
    session: SessionTree
    tools: list[Tool]
    skills: SkillCatalog
    cwd: Path
    session_file: Path | None
```

暴露：

```python
stream(prompt)
run(prompt)
run_command(command)
save_session()
close()
```

常规调用应走：

```python
runtime.run(...)
```

而不是：

```python
runtime.agent.run(...)
```

## 7.4 `create_coding_agent()` 构造顺序

必须按以下顺序：

### Step 1：workspace

```python
cwd = Path(options.cwd).resolve()
```

不存在或不是目录时 fail fast。

### Step 2：Tools

```python
product_tools = create_coding_tools(cwd)
tools = [*product_tools, *extra_tools]
```

必须检查 Tool name duplicate：

```text
如果 extra tool 与 product tool 重名 -> fail fast
```

禁止静默覆盖。

### Step 3：Skills

Skill roots 规则建议：

1. 如果调用方显式传 `skill_roots`，逐个 discover；
2. 如果未传且 `<cwd>/skills` 存在，则自动 discover；
3. 多 root 合并时按 skill name 检查重复；
4. 同名 Skill → fail fast 或明确 precedence；第一版推荐 fail fast。

不要读取 Skill body 进入 prompt。

### Step 4：Session restore

如果 `session_file` 存在：

```python
session = SessionTree.load_jsonl(session_file)
initial_messages = session.reconstruct_messages()
```

否则：

```python
session = SessionTree()
initial_messages = []
```

不要另造 CodingSession。

### Step 5：ExtensionRunner

```python
config = RuntimeConfig(
    model=model_name,
    active_tools=[tool.name for tool in tools],
    services={...},
)
runner = ExtensionRunner(cwd=cwd, config=config, session=session)
await runner.load(list(extensions))
```

本章默认不自动加载全部 Chapter 11 Extension。

调用方决定加载哪些 policy。

Chapter 12 demo 只需要 Permission Gate。

### Step 6：System prompt

```python
system_prompt = build_coding_system_prompt(...)
```

### Step 7：Agent

```python
agent = Agent(
    model=options.model,
    system_prompt=system_prompt,
    tools=tools,
    messages=initial_messages,
)
```

### Step 8：ExtensionHost

```python
host = bind_extensions(
    agent,
    runner,
    persist_messages=True,
)
```

这一点是 Beta 与教程实现差异最大的地方。

**禁止在 assembly 再手动把 run 产生的 Message append 到 Session。**

---

# 8. Session / Compaction 生命周期

## 8.1 正常 run

```text
runtime.run(user input)
     ↓
ExtensionHost.stream
     ↓
Agent EventStream
     ↓
message_end
     ↓
SessionTree.append_message
```

此时内存 Session 已是最新状态。

## 8.2 `save_session()`

推荐顺序：

```text
current SessionTree
   ↓
optional compaction
   ↓
if compacted:
    agent.replace_messages(session.reconstruct_messages())
   ↓
session.save_jsonl(session_file)
```

若 `session_file is None`：

- 可以仍执行 compaction；
- 不落盘。

## 8.3 Compaction 注意事项

当前 Beta `compact_session()` 是 Chapter 08 的最小版本。

本章只要求：

- 单次压缩正确；
- 压缩后 agent canonical messages 刷新；
- save / reload 后恢复成 summary + retained tail；
- 原始 Message Entry 不删除。

不要在 Chapter 12 顺手重写复杂 repeated-compaction 算法。

如果 active branch 已经存在 compaction，测试只验证当前 Core 明确定义的行为；如发现 repeated compaction 会重复摘要旧前缀，应记录为后续 Chapter / issue，而不是在 assembly 中写隐式 hack。

---

# 9. Permission Gate 产品化与兼容性

当前 Chapter 11 的 Permission Gate 位于：

```text
examples/extensions/permission_gate.py
```

Chapter 12 的可执行产品层不应该依赖 `examples` 目录才能加载策略。

推荐新增：

```text
src/beta_agent/coding/extensions/permission_gate.py
```

导出：

```python
permission_gate_extension
is_dangerous_command
```

然后把：

```text
examples/extensions/permission_gate.py
```

改为薄示例 / re-export，避免两份规则长期漂移。

重要：Permission Gate 只是**演示策略**，不能在文档中描述成完整 shell security sandbox。

至少继续覆盖：

```text
rm -rf
sudo
chmod/chown 777
```

但明确：真正安全隔离需要独立 sandbox / trust model。

## Plan Mode compatibility

当前 Chapter 11 Plan Mode mutation tool 名称里已有 `write / edit / delete_file`。

Chapter 12 新增的是：

```text
write_file
```

因此应同步把 `write_file` 加入 Plan Mode 的 mutation Tool 集合，避免以后加载 Plan Mode 时仍能写文件。

这是兼容性修复，不是把 Plan Mode 塞进 Coding Agent Core。

---

# 10. CLI 示例

新增：

```text
examples/coding_agent_cli.py
```

职责只做 Harness/UI：

1. `argparse`：
   - `--cwd`，默认当前目录；
   - `--session`，可选；
   - `--no-permission-gate`，可选；
2. 读取 `.env`；
3. 创建 `OpenAICompatibleAdapter`；
4. 调用 `create_coding_agent()`；
5. 默认显式加载 Permission Gate；
6. 多轮输入统一走：

```python
runtime.stream(user_input)
```

7. 显示：
   - assistant streaming text；
   - tool start：工具名 + 精简参数；
   - tool end error：错误摘要；
8. 每轮结束调用：

```python
await runtime.save_session()
```

9. exit 时 `runtime.close()`。

不要把业务规则写在 CLI 中。

当前 `examples/deepseek_cli.py` 保持为“最小普通 Agent 聊天示例”，不要直接改造成 Coding Agent，避免失去两个层级不同的教学入口。

---

# 11. Python 化 E2E Fixture

教程 Chapter 12 使用 TypeScript calculator + `npm test`。

Beta 是 Python 项目，推荐把 E2E fixture 改为 Python，但保持同一个行为链。

建议：

```text
tests/fixtures/coding_project/
├── src/
│   ├── __init__.py
│   └── calculator.py
├── tests/
│   └── test_calculator.py
├── skills/
│   └── verify/
│       └── SKILL.md
└── keep/
    └── keep.txt
```

`src/calculator.py`：

```python
def add(a: int, b: int) -> int:
    return a - b  # intentional bug
```

`tests/test_calculator.py`：

```python
from src.calculator import add


def test_add():
    assert add(2, 3) == 5
```

`verify/SKILL.md`：

```markdown
---
name: verify
 description: 修改代码后运行测试验证结果
---

修改代码前先确认失败；修改后运行 `python -m pytest -q` 验证。
```

注意实际 frontmatter 不要保留上面示例中的错误缩进，必须符合 Beta 当前简单 parser：

```markdown
---
name: verify
description: 修改代码后运行测试验证结果
---
```

E2E 测试应复制 fixture 到 `tmp_path`，绝不能直接修改仓库 fixture 原件。

---

# 12. 测试计划

所有新测试必须离线，不调用真实 DeepSeek / OpenAI API。

现有全部测试必须继续通过。

## 12.1 `test_coding_tools.py`

### path resolver

- relative path 基于 cwd；
- absolute path；
- `../outside` 可解析；
- 这项测试用于锁定“resolver != sandbox”。

### read_file

- full read；
- offset；
- limit；
- limit 后有 continuation hint；
- missing file → error；
- offset > EOF → error；
- >500 lines → truncate；
- >64KiB → truncate；
- UTF-8 多字节内容不产生坏字符；
- trailing newline 行为稳定。

### write_file

- 自动创建父目录；
- 新建；
- 覆盖；
- 返回 UTF-8 byte count；
- execution_mode == sequential。

### edit

- 单 replacement；
- 多个 disjoint replacement；
- 全部基于 original content；
- oldText missing → fail；
- oldText duplicate → fail；
- oldText empty → fail；
- overlap → fail；
- 单 object → prepare 成 list；
- JSON string edits → prepare；
- legacy top-level oldText/newText → prepare；
- details.diff；
- first_changed_line；
- 如实现 patch，则验证 unified diff 基本结构；
- CRLF 不被无关转换；
- execution_mode == sequential。

### grep

- regex match；
- literal match；
- ignoreCase；
- glob；
- recursive；
- skip `.git/node_modules/.venv/__pycache__`；
- context before/after；
- limit；
- no match 非 error；
- invalid regex → error；
- missing path → error；
- execution_mode == parallel。

### bash

- cwd 正确；
- stdout；
- stderr 合并；
- non-zero → ToolRuntime 最终 error ToolResult；
- timeout；
- cancellation 后子进程被结束；
- large output tail truncate；
- timeout 参数非法；
- execution_mode == sequential。

对于 OS 相关测试：

- POSIX-only 用例应有 `pytest.mark.skipif(os.name == "nt", ...)`；
- 不要让 CI 因平台差异出现随机失败。

---

## 12.2 `test_coding_assembly.py`

至少覆盖：

1. 创建 runtime 后 Tool 列表为 Chapter 12 预期；
2. duplicate tool name fail fast；
3. system prompt 包含 Tool metadata；
4. system prompt 包含 Skill metadata；
5. system prompt **不包含 Skill body**；
6. cwd 在 prompt 中；
7. Extension factory 被加载；
8. active tools 与 Agent 实际 tools 一致；
9. Host run 后 Session 自动新增消息；
10. `save_session()` 不重复追加消息；
11. JSONL save -> reload -> reconstruct；
12. resumed runtime 新消息接在旧 leaf 后；
13. session restore 不重复发旧 message_end；
14. optional compaction：
    - append compaction entry；
    - old entries 不删；
    - `agent.messages` 替换为 reconstructed context；
    - save / reload 后仍然是 summary + retained tail。

必须专门写一个 regression：

```text
run 一轮 -> session message count = N
save_session -> session message count 仍然 = N
```

用来防止把教程 `savedCount` 逻辑错误照搬到 Beta。

---

## 12.3 `test_coding_e2e.py`

使用 `ScriptedModelAdapter` 驱动完整流程。

推荐脚本：

### Turn 1：危险命令

```text
bash("rm -rf keep")
```

预期：

- Permission Gate block；
- Tool Result `is_error=True`；
- `keep/keep.txt` 仍存在。

### Turn 2：读取 Skill

```text
read_file(".../skills/verify/SKILL.md")
```

预期 Skill body 进入 Tool Result。

### Turn 3：grep

```text
grep(pattern="def add", path="src")
```

预期带文件路径 + 行号。

### Turn 4：read source

```text
read_file("src/calculator.py")
```

预期读到 `return a - b`。

### Turn 5：先跑测试

```text
bash("python -m pytest -q")
```

预期：

- non-zero；
- error Tool Result；
- 内容包含 assertion failure。

### Turn 6：edit

```json
{
  "path": "src/calculator.py",
  "edits": [
    {
      "oldText": "return a - b",
      "newText": "return a + b"
    }
  ]
}
```

### Turn 7：重跑测试

```text
bash("python -m pytest -q")
```

预期成功。

### Turn 8：final assistant

```text
已修复并验证通过
```

最终断言：

- Tool chain 完整；
- Permission extension 真正生效；
- 文件真实修改；
- 测试真实执行；
- Session 包含整条消息链；
- `save_session()` 后可恢复；
- resumed runtime 能继续一轮；
- Parent Core 文件无需知道“Bug Fix workflow”。

---

# 13. 文档更新

必须新增：

```text
docs/tutorials/12-coding-agent.md
```

内容重点不是重复源码，而是解释组装关系：

```text
00-11 = primitives
12    = product assembly
```

需要更新：

```text
docs/tutorials/README.md
```

把 Chapter 12 从“下一章”改为已实现。

建议同步更新：

```text
README.md
docs/README.md
docs/FRAMEWORK.md
docs/ARCHITECTURE.md
```

但只增加 Coding Agent 产品层，不重写 00～11 的说明。

文档必须显式说明两个安全事实：

1. `cwd` 不是 sandbox；
2. Permission Gate 是示例策略，不是完整命令安全系统。

---

# 14. `__init__` 与公共 API

推荐只导出产品层主要入口：

`src/beta_agent/coding/__init__.py`：

```python
from .assembly import (
    CodingAgentOptions,
    CodingAgentRuntime,
    CodingCompactionOptions,
    create_coding_agent,
)
from .prompt import build_coding_system_prompt
from .tools import create_coding_tools
```

顶层 `beta_agent/__init__.py` 是否 re-export `create_coding_agent` 可选。

推荐第一版**不**大量污染顶层 namespace；用户可：

```python
from beta_agent.coding import create_coding_agent
```

这能清楚表达：Coding Agent 是产品层，不是 Core primitive。

---

# 15. 建议执行阶段

Codex 应按以下阶段执行，每阶段结束先跑对应测试，再进入下一阶段。

## Phase 0：Baseline

1. checkout 最新 `main`；
2. `git status` 必须 clean；
3. 记录 HEAD；
4. 运行：

```bash
python -m compileall -q src tests examples
pytest -q
```

5. 如果 baseline 已失败，先停止，不把旧失败混入 Chapter 12 修改。

## Phase 1：Coding Tools

实现：

```text
coding/tools/path_utils.py
coding/tools/read_file.py
coding/tools/write_file.py
coding/tools/edit.py
coding/tools/grep.py
coding/tools/bash.py
coding/tools/__init__.py
```

然后只跑：

```bash
pytest -q tests/test_coding_tools.py
```

## Phase 2：Prompt + Assembly

实现：

```text
coding/prompt.py
coding/assembly.py
coding/__init__.py
```

重点确认：

- Host 自动 session append；
- no duplicate persistence；
- resume；
- active tools；
- skill metadata；
- optional compaction。

运行：

```bash
pytest -q tests/test_coding_assembly.py
```

## Phase 3：Permission 产品策略 + Chapter 11 compatibility

1. 产品化 Permission Gate；
2. examples re-export / thin wrapper；
3. Plan Mode 增加 `write_file` mutation name；
4. 跑原 Chapter 10/11 tests。

## Phase 4：E2E Fixture

创建 Python bug fixture + scripted full chain。

运行：

```bash
pytest -q tests/test_coding_e2e.py
```

## Phase 5：CLI

新增 `examples/coding_agent_cli.py`。

CLI 只调用 Assembly API，不自己拼 Agent / Session / Extension。

## Phase 6：Docs

更新 Chapter 12 教程、索引、架构说明。

## Phase 7：Full Regression

运行：

```bash
python -m compileall -q src tests examples
pytest -q
```

如果仓库后续增加 lint / typecheck，则一并执行。

---

# 16. Architecture Invariants（必须锁死）

最终 code review 必须逐条确认：

- [ ] `Agent._run()` 没有 Coding Agent 特判；
- [ ] `ToolRuntime` 没有 read/edit/bash 特判；
- [ ] `SessionTree` 没有 Coding Session entry 类型；
- [ ] `read_file / write_file / edit / grep / bash` 都是普通 `Tool`；
- [ ] Permission policy 仍在 Extension；
- [ ] Coding Agent 常规运行经过 `ExtensionHost`；
- [ ] Host 已写 Session 时，assembly 不重复 append；
- [ ] Skill body 不进启动 system prompt；
- [ ] Skill 可通过 `read_file` 按需读取；
- [ ] tool failure 仍由 ToolRuntime 统一转换成 error ToolResult；
- [ ] session restore 走 `SessionTree.reconstruct_messages()`；
- [ ] compaction 仍属于 Session 生命周期，不塞进 context extension；
- [ ] `cwd` 只负责路径解析，不声称提供 sandbox；
- [ ] mutation Tool 不并行执行；
- [ ] E2E 真实修改临时文件并真实运行测试；
- [ ] 原 00～11 测试全部通过。

---

# 17. 失败处理原则

Codex 遇到以下情况不要“顺手修大架构”：

## 情况 1：需要修改 Agent Loop 才能实现某功能

先检查是否能通过：

```text
Tool
Extension
ExtensionHost
Assembly facade
```

解决。

如果能，就禁止修改 Core。

## 情况 2：Session persistence 出现重复消息

第一检查点：是否同时存在：

```text
ExtensionHost.persist_messages=True
+
assembly.save_session 手动 append agent.messages
```

只能保留前者。

## 情况 3：Plan Mode 仍能调用 write_file

更新 Extension 的 mutation tool name，不要在 `write_file.execute()` 内加 Plan Mode 判断。

## 情况 4：Permission Gate 漏掉危险命令

这是 policy completeness 问题，不要把 security logic 移进 bash Tool。

## 情况 5：Bash cancel 后进程仍存在

修复 bash process lifecycle，不要吞掉 `CancelledError`。

## 情况 6：Edit 多 replacement 出现部分成功

必须改成：

```text
全部匹配/校验成功
    ↓
一次性构造 new_content
    ↓
一次 write
```

禁止 incremental write。

---

# 18. 推荐后续 backlog（本章不做）

Chapter 12 完成后，可单独规划：

1. file mutation queue（按 path 串行，而不是整批串行）；
2. atomic Session persistence；
3. repeated compaction；
4. project trust；
5. sandbox / container execution；
6. shell allowlist / confirmation UI；
7. `find / ls`；
8. ripgrep backend；
9. binary/image read；
10. fuzzy edit + richer patch；
11. full process tree cleanup；
12. Git diff / Git status Tool；
13. TUI；
14. workspace-scoped project instructions；
15. remote execution operations abstraction。

这些都不应该成为 Chapter 12 第一版的前置条件。

---

# 19. Definition of Done

只有同时满足以下条件，Chapter 12 才算完成：

## 功能

- [ ] `create_coding_agent()` 可创建真实 Coding Agent；
- [ ] 五个 Coding Tool 可用；
- [ ] Skill metadata 注入 + body lazy read；
- [ ] Permission extension 可以 block bash；
- [ ] Session 可保存、恢复、继续；
- [ ] optional compaction 能与 assembly 配合；
- [ ] CLI 可以指定 cwd 执行多轮 coding task。

## 工程

- [ ] 无新的 Agent Loop；
- [ ] 无新的 Tool Runtime；
- [ ] Core 未被产品 workflow 污染；
- [ ] 新 API 有类型注解；
- [ ] Pydantic Tool schema 可被 OpenAI-compatible adapter 正常序列化；
- [ ] 错误对模型可见且可理解；
- [ ] side-effect Tool 使用 sequential policy。

## 测试

- [ ] 原测试全部通过；
- [ ] Coding Tool 单测全部通过；
- [ ] Assembly 集成测试通过；
- [ ] E2E Bug Fix 通过；
- [ ] Permission blocked command 未碰到 fixture victim file；
- [ ] Session save 无 duplicate；
- [ ] resume 后能继续；
- [ ] compileall 通过。

## 文档

- [ ] Chapter 12 教程完成；
- [ ] tutorial index 更新；
- [ ] README / Architecture 能找到 Coding Agent 入口；
- [ ] 明确写出 cwd != sandbox；
- [ ] 明确写出 Permission Gate != complete security sandbox。

---

# 20. 最终建议的 Codex 执行指令摘要

Codex 开始实现时，应把下面这段作为最高优先级执行约束：

```text
基于 Beta 当前 main 实现 Chapter 12 Coding Agent。

不要重写 Agent Loop、ToolRuntime、SessionTree、ExtensionRunner。
Chapter 12 是产品组装层。

新增 beta_agent.coding 包，提供：
- create_coding_agent
- CodingAgentRuntime facade
- coding prompt
- read_file / write_file / edit / grep / bash

所有 Coding Tool 必须继续走已有 ToolRuntime。
所有 Extension 必须继续走已有 ExtensionRunner / ExtensionHost。
ExtensionHost 已自动在 message_end 时 append Session，save_session 禁止再次 append agent.messages。
Session restore 使用 SessionTree.load_jsonl + reconstruct_messages。
Skill 使用现有 **/SKILL.md 规范，只把 metadata 放进 prompt，正文通过 read_file 按需读取。

read/grep 可 parallel；write/edit/bash 设 sequential。
edit 必须 exact + unique + non-overlap + all-match-original + atomic apply。
bash 的 permission 判断不写进 Tool；继续由 Extension before_tool_call 拦截。
cwd 是路径解析基点，不是 sandbox。

新增离线单测、assembly 测试和 Python Bug Fix E2E：
permission block -> read skill -> grep -> read -> pytest fail -> edit -> pytest pass -> final -> save -> resume。

全过程保留 00-11 现有测试和行为。
若发现必须修改 Core 控制流，先停止并重新评估，不要直接加 Coding-specific branch。
```

