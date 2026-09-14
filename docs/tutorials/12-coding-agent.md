# 12：Coding Agent——把 Runtime primitive 组装成产品

前 00～11 章分别建立了 Model Adapter、Agent Loop、Tool Runtime、Session、Compaction、Skills 和 Extension Runtime。Chapter 12 不再新增一套循环，而是把这些 primitive 组装成一个可以真实修改工作区的 Python Coding Agent。

```text
Model Adapter
    + Agent / ToolRuntime
    + Session / Compaction
    + Skills
    + ExtensionRunner / ExtensionHost
    + Coding Tools
    + Coding Prompt / Workspace
    = Coding Agent Runtime
```

## 1. 产品层入口

产品层位于 [`../../src/beta_agent/coding/`](../../src/beta_agent/coding/)：

- `create_coding_agent()`：按 workspace、Tool、Skill、Session、Extension、Prompt 的顺序完成装配；
- `CodingAgentRuntime`：暴露 `stream()`、`run()`、`run_command()`、`save_session()` 和 `close()`；
- `create_coding_tools()`：创建稳定的 `read_file`、`write_file`、`edit`、`grep`、`bash` 集合；
- `build_coding_system_prompt()`：从实际 Tool 和 Skill metadata 生成产品提示。

正常运行应该经过 facade：

```python
from beta_agent.coding import CodingAgentOptions, create_coding_agent

runtime = await create_coding_agent(
    CodingAgentOptions(cwd=".", model=model, session_file=".beta/session.jsonl")
)
try:
    await runtime.run("检查并修复这个项目的问题")
    await runtime.save_session()
finally:
    runtime.close()
```

`runtime.agent` 仍然保留用于调试和查看状态，但常规调用不应绕过 `ExtensionHost` 直接调用它。这样 `message_end` 观察、Extension handler 和 Session persistence 会走同一条入口。

## 2. 五个 Coding Tool

这些工具都是普通 `Tool`，因此仍然由 Core 的 Tool Runtime 负责 lookup、参数验证、before/after hook、异常归一化和 history 写回。

```text
read_file   parallel    UTF-8 按行读取，可 offset / limit，过长时提示续读
grep        parallel    纯 Python 递归搜索，支持 regex / literal / glob / context
write_file  sequential  创建或完整覆写文件，自动建立父目录
edit        sequential  exact + unique + non-overlap 的原子文本替换
bash        sequential  在 workspace 执行 POSIX-like shell，合并 stdout/stderr
```

`edit` 的每个 `oldText` 都在同一份原文件快照上验证；全部验证成功后才一次写入，因此不会出现前一个 replacement 成功、后一个失败的半修改文件。

`bash` 的命令失败、超时和取消都会让模型看到可理解的错误；取消时会终止子进程，输出超过 64 KiB 时保留尾部。

## 3. Skills 是 metadata + lazy read

装配阶段沿用 Beta 的 `**/SKILL.md` 规范。system prompt 只包含 `name`、`description` 和 `location`，不包含 Skill 正文。模型需要时通过 `read_file` 读取对应的 `SKILL.md`，所以 Skill body 会作为真实 Tool Result 进入历史。

如果 workspace 下存在 `skills/`，它会被自动发现；也可以在 `CodingAgentOptions.skill_roots` 中显式提供多个根目录。同名 Skill 会 fail fast。

## 4. Session、恢复与压缩

`ExtensionHost` 在收到 `message_end` 后已经调用 `SessionTree.append_message()`。因此 `save_session()` 只负责可选 compaction 和 `SessionTree.save_jsonl()`，不能再次遍历 `agent.messages` 追加消息。

恢复时使用：

```text
SessionTree.load_jsonl()
    ↓
session.reconstruct_messages()
    ↓
Agent(initial_messages=...)
```

可选的 `CodingCompactionOptions` 仍然使用 Core 的 `compact_session()`：压缩记录是 append-only，旧 Entry 不删除；压缩后 facade 会用当前 branch 重建的 summary + retained tail 替换 Agent canonical messages。

## 5. 权限边界

`src/beta_agent/coding/extensions/permission_gate.py` 中的 Permission Gate 只是一个演示 Extension。它在 `before_tool_call` seam 拦截示例危险命令（如 `rm -rf`、`sudo`、危险的 `chmod/chown 777`），不属于 `bash` Tool 实现。

两个边界必须区分：

1. `cwd` 只是路径解析基点，不是 sandbox。绝对路径和 `../outside` 都可以被 resolver 解析；真正的 trust、sandbox 和文件访问策略需要独立实现。
2. Permission Gate 是示例策略，不是完整命令安全系统。生产环境仍需要单独的 sandbox、确认机制和更完整的 trust model。

Plan Mode 的 mutation tool 集合也同步包含 `write_file`，但 Plan Mode 仍然是 Extension，不进入 Coding Tool 或 Agent Core。

## 6. 离线验证

`tests/test_coding_tools.py` 覆盖各工具边界，`tests/test_coding_assembly.py` 覆盖装配、恢复、无重复持久化和 compaction，`tests/test_coding_e2e.py` 使用 `ScriptedModelAdapter` 真实完成：

```text
permission block
    → read Skill
    → grep
    → read source
    → pytest fail
    → edit
    → pytest pass
    → final answer
    → save / reload / resume
```

Chapter 12 的关键结论是：00～11 提供稳定能力，12 负责产品组装；Agent Loop、ToolRuntime、SessionTree 都不需要认识“Bug Fix workflow”。
