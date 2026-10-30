# 稳定化计划（Issue #1 → PR 1–5）

- 追踪 issue：https://github.com/zzerding/whiteboard-teacher-video-stable/issues/1
- Project：https://github.com/users/zzerding/projects/3
- 这个仓库是 fork：`zzerding/whiteboard-teacher-video-stable`（upstream 是 `liangdabiao/whiteboard-teacher-video`）

每条 issue 一个独立分支 + 一个 PR + 一个 fresh worker；合入后（或经明确 stacked 决策）才开下一条。
**禁止直接推 `main`、禁止 agent 自行合并、禁止发 tag / release**：合并由 Doctor Biz（HITL）执行。

| 序 | Issue | 分支 | 依赖 | 状态 |
|---|---|---|---|---|
| 1 | [#2 可复现的安装与 preflight](https://github.com/zzerding/whiteboard-teacher-video-stable/issues/2) | `fix/2-reproducible-setup` | 无 | 本轮：实现完成，待复核合并 |
| 2 | [#3 增量配音与全片 user 音频索引](https://github.com/zzerding/whiteboard-teacher-video-stable/issues/3) | — | #2 | 未开始 |
| 3 | [#4 Fail-closed 质检与本次渲染产物交付](https://github.com/zzerding/whiteboard-teacher-video-stable/issues/4) | — | #2 | 未开始 |
| 4 | [#5 通用 Skill 边界、来源与许可说明](https://github.com/zzerding/whiteboard-teacher-video-stable/issues/5) | — | #2 | 未开始 |
| 5 | [#6 Windows/macOS 发布闸门与一页 smoke](https://github.com/zzerding/whiteboard-teacher-video-stable/issues/6) | — | #2（#3/#4 的产物定义其闸门） | 未开始 |

## 已明确的缺口（按 issue 归属）

- **Windows 实机**安装与 preflight：Issue #6（PR 1 只有假 runtime 单测 + macOS 实跑）。
- **真实 TTS 合成 / 探针**：需要外部服务，任何 PR 默认都不跑；只允许用户本机自行探针。
- **许可 / NOTICE / 文档全量整理**：Issue #5（PR 1 只动与安装直接相关的说明）。
- **Linux 支持**：未承诺（沿用同一路径，未实测）。
- **整片渲染闸门**：Issue #6（PR 1 只验证引擎能解析依赖并输出时间轴）。

## 本轮（PR 1）的边界

已做：版本固定、事务式安装与回滚、幂等、`npm ci`、`uv` 锁定 `edge-tts`、单一 preflight、
`setup.ps1` 薄入口、ffmpeg/浏览器/引擎覆盖的显式校验、`tests/test_setup.py` 回归。

刻意不做：渲染器重写、`P.*` API 与 `script.js` / `pages/` / `voice.js` 格式变更、依赖升级、
Docker/SaaS/Linux 承诺、真实 TTS 调用、文档全量重排。