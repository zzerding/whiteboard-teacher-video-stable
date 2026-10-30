# 决策记录（Decision log）

只记录**已批准并且已落地**的决策；没落地的想法不进这里。

## 2026-10 · Issue #2：可复现的 Windows/macOS 安装与 preflight

### Decision

- **引擎版本固定**：Frametell `v0.7.4` → commit `ca29a16cdbff06614cd2422d8b246f0e741e3330`
  （annotated tag 对象 `68be575…` 指向该 commit，安装用的是 commit 而不是 tag 对象的 SHA）。
  依据：`packages/engine/package.json` 在该 commit 上是 `version: 0.7.4`、`license: AGPL-3.0-only`、
  `private: true`，依赖范围与现有 `package.json` 的 10 个包一致，因此不需要动 npm 版本。
  安装时 `git clone --depth 1 --branch v0.7.4` + sparse checkout，随后 `rev-parse HEAD` 与 pin 比对，
  通过才换入，并写 `engine/provenance.json`（repo / tag / commit / 实际 package version）。
  被否的方案：继续跟 mutable main（同一份 skill 在不同时间装出不同引擎）；
  下载 tarball（拿不到可核对的 commit）；用 tag 对象 SHA 当 commit。
- **安装即事务**：下载到 `.engine-fetch-<pid>` / `.npm-stage-<pid>`，先校验（必需文件、package 版本、
  无符号链接/junction、npm 标记文件齐、无指向树外的链接），再 `os.replace` 换入。
  旧目录先改名让位（`engine.old-<pid>`），换入失败把它原样退回，成功后才删备份。
  被否的方案：原地覆盖（旧 `setup.py` 的 overlay 行为，失败会留下两版本混合）与「先删再装」。
- **删除只拆链接**：`safe_rmtree` 遇到符号链接/junction 只 `unlink` 链接本身，绝不跟进；
  目标目录是链接、或是无法识别的目录（没有 `tools/render.mjs` 且 `package.json` 不是
  `@frametell/engine`）时直接拒绝安装，不覆盖未知数据。
- **npm 用 `npm ci`**：`package.json` / `package-lock.json` 原样保留（版本未升级）；在 staging 目录里跑
  `npm ci`，安装位置仍是 `<skill>/node_modules`（引擎自己向上查找），不建 `engine/node_modules` 链接，
  并顺手清理历史遗留的该链接。
- **Python 只用 uv**：新增 `pyproject.toml`（`requires-python >= 3.9`、`edge-tts==7.2.8`）
  与 `uv.lock`（22 个包，含 aiohttp / certifi / tabulate / typing-extensions 等传递依赖的哈希）。
  下限保留 3.9：现有脚本都在 3.9 语法内、README 早已声明 3.9+，没有收紧的理由。
  `uv sync --frozen` 建 `.venv`，不用 pip。
  edge-tts 7.2.8 已核对本地源码：`edge_tts.communicate.WSS_URL`、
  `edge_tts.constants.TRUSTED_CLIENT_TOKEN`、`Communicate(text, voice, rate=…, connect_timeout=…,
  receive_timeout=…)` 与 `stream()` 的 chunk 形态都与 `scripts/edge_tts_speak.py` 的用法一致。
- **单一安装实现**：`scripts/setup.py` 是唯一的安装 + preflight 实现（`--check` 只读不写，
  `--force` 重装），`scripts/setup.ps1` 退化为薄入口：找 Python（`$env:PYTHON` → `py -3` →
  `python3` → `python`，跳过 Store 占位符的 9009）、原样转发参数、原样透传退出码。
  之后所有脚本用 `uv run python scripts/…` 调用，走锁定环境。
- **ffmpeg 查找顺序**：`FFMPEG_PATH`（显式但无效 → 报错，不回退到别的 ffmpeg）→ 任一
  `candidate_node_modules()` 下的 `ffmpeg-static`（`engine/node_modules`、`<skill>/node_modules`）→
  系统 PATH。修的是旧 bug：只查 `engine/node_modules`，导致「npm 装成功了、但 clean 机器没系统
  ffmpeg」时直到渲染才失败。
- **显式覆盖不静默失效**：`FRAMETELL_ENGINE` 无效时 ftlib 打印警告并回退内置引擎（运行时保持可跑），
  preflight 判 ✗（严格闸门）；`CHROME_PATH` 无效时警告并继续自动探测。

### Assumptions

- 只承诺 Windows + macOS；Linux 走同一路径但**未实测**（Issue #6 才做跨平台闸门）。
- Node 必须 ≥ 22.19（`package.json` 的 `engines` 早就这么写，本 PR 让 preflight 真的检查）；
  npm 不设版本下限，`npm ci` 只要求 lockfileVersion 3，Node 22/24 自带版本满足。
- uv 视为必需（strict）：它是 edge 后端的唯一安装途径；用 apiz 后端的人也会装（几十 MB）。
  这是最激进的一条假设——若 Doctor Biz 认为 apiz-only 用户不该被 uv 卡住，需要放宽为
  「用 edge 后端时才必需」。
- 手动复制引擎（无 git）是合法入口；此时 commit provenance 无法核对，preflight 报 ⚠ 但不算失败，
  版本本身仍严格校验。
- staging / 备份目录名带 pid，异常退出的残留靠人工清理或下次同名复用，不做 GC。

### Goal check

- 达成：全新 clone → 一条命令（macOS `python3 scripts/setup.py`；Windows `scripts/setup.ps1`）
  装出 v0.7.4 引擎 + 锁定 npm 依赖 + 锁定 Python 环境，并用**同一份代码**给出 strict preflight
  与一致退出码。
- 达成：失败回滚保留旧 runtime、重复运行幂等、拒绝链接/未知目录、无系统 ffmpeg 时找到
  `<skill>/node_modules/ffmpeg-static`、Node/uv/浏览器/依赖/edge-tts 版本严格检查。
- 未验证：**Windows 实机未运行**（本机 macOS；Windows 相关路径只有假 runtime 单测）。
- 未验证：真实 TTS 合成/探针（禁止对外请求）、整片渲染（Issue #6 的 smoke）。
- 刻意留下：`package.json` 的 `version: 0.7.2`（分发包版本，与引擎版本不是同一概念）未改；
  文档全量清理、NOTICE、进度表迁址留给 Issue #5。

### Next

- A. 合入本 PR 后按序推进 #3（增量配音）→ #4（fail-closed 质检）→ #5（Skill 边界与来源）→
  #6（Windows/macOS 发布闸门 + 一页 smoke），顺序见 `docs/STABILIZATION_PLAN.md`。
- B. Issue #6 补真实证据：Windows 实机 `setup.ps1`、坏 Node/无 uv/无浏览器/坏 `CHROME_PATH` 的实跑。
- C. 若以后要承诺 Linux，需在 Issue #6 显式验证并更新 README 的支持矩阵。

### 验证证据与缺口

实跑环境：macOS（Chromium 系浏览器已装）/ Python 3.12.0 / Node v24.19.0 / npm 11.17.0 / uv 0.8.4。

- `python3 scripts/setup.py` → exit 0；`engine/package.json` 版本 0.7.4，
  `engine/provenance.json` commit `ca29a16cdbff06614cd2422d8b246f0e741e3330`。
- 重复运行：三步全部「跳过」（幂等）；`python3 scripts/setup.py --check` → exit 0。
- `node engine/tools/pages.mjs --json`（在 `assets/example-relativity`）跑通：引擎能找到
  `<skill>/node_modules`，无需链接。
- `uv run --frozen --no-sync python -c "import edge_tts"` → 7.2.8，`WSS_URL` 可从
  `edge_tts.communicate` 取到。
- `python3 -B -m unittest discover -s tests` → 39 个用例通过（git/npm 全为假 runner）。
- **not run**：Windows 实机 `setup.ps1`；真实 edge 合成与 `--probe`；整片渲染。