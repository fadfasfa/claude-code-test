---
description: Run a controlled TDD loop for explicit development or bug-fix tasks.
argument-hint: "<development or bug-fix task>"
---

# /tdd-task

本命令只在用户明确要求开发、修 bug 或高风险行为变更时使用。文档、规则、小脚本维护不强行 TDD。

执行规则：

- 先定位现有测试体系和最近有效验证命令。
- 没有测试体系时，不强行大建测试框架；只提出最小测试方案。
- 定位源码、文档或测试入口时，不调用原生 `Read` 读取 text/code 文件；使用 `repo-explorer`、`Grep` / `Glob` / `Bash`。
- 遵守 red-green-refactor：先写或确认失败用例，再做最小实现，再只做必要整理。
- 不顺手重构，不扩大任务范围。
- 修改后运行最小必要验证，并报告实际结果。
- 不创建分支，不启动 worktree，除非本轮任务另有明确授权。

输出建议：

1. 目标行为
2. 现有测试入口
3. 最小失败用例或替代验证
4. 实现步骤
5. 验证结果
