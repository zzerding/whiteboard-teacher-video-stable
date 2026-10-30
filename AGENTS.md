# AGENTS.md

本仓库是 whiteboard-teacher-video 的稳定化 fork（上游：`liangdabiao/whiteboard-teacher-video`，
原 skill：Frametell 视频引擎 + 五步出片流程）。改动只按 issue 切片进行，见
`docs/STABILIZATION_PLAN.md`；已定的设计选择见 `docs/DECISIONS.md`。

## 边界

- 不推 `main`、不合并 PR、不发 tag / release；合入由 Doctor Biz（HITL）执行。
- 不改 `assets/` 示例片源码，不改 `P.*` 绘图 API，不改 `script.js` / `pages/*.js` / `voice.js` 格式。
- 不升级依赖版本、不顺手重构无关代码；引擎固定 `v0.7.4`（commit `ca29a16cdbff06614cd2422d8b246f0e741e3330`）。
- 不调用真实/付费 TTS，不上传任何配音内容；测试用假 runner 或已装的本地二进制。
- 文档与注释跟目标文件现有语言（中文文档写中文）。

## 命令

```bash
python3 -B -c "import ast; ast.parse(open('scripts/setup.py', encoding='utf-8').read())"   # 语法检查
python3 -B -m unittest discover -s tests                                                  # 回归测试
python3 scripts/setup.py --check                                                          # 只体检，不写文件
python3 scripts/setup.py                                                                  # 安装 / 修复（幂等）
uv run python scripts/ftcheck.py <项目>                                                   # 脚本一律走锁定环境
node engine/tools/pages.mjs --json                                                        # 引擎能不能解析依赖（在某个项目目录下跑）
```

## 验证阶梯

1. 每次改动先跑静态语法检查（不要跑整片渲染当编译检查）。
2. 行为变化补/跑对应单测；平台相关处用假 runtime 模拟，并在报告里区分「模拟」与「实机已跑」。
3. 只有需要证据时才跑真实安装/渲染；整片渲染只在交付前跑，且不得以「保险」为由重复全量构建。
4. 报告里未执行的检查写 `not run`，未证实的结论写 `unverified`；不要用 macOS 结果替代 Windows 结论。