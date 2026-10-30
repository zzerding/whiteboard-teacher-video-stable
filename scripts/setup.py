"""whiteboard-teacher-video 安装 + preflight。

一条命令装齐固定版本引擎、npm 依赖与锁定 Python 环境，然后严格检查实际就绪状态：

    python scripts/setup.py             # 安装 + 检查
    python scripts/setup.py --check     # 只做 preflight，不动任何文件
    python scripts/setup.py --force     # 重装引擎与 npm 依赖
    powershell -ExecutionPolicy Bypass -File scripts/setup.ps1   # Windows 薄入口，参数原样转发

引擎固定在 Frametell v0.7.4（commit ca29a1…），不再跟上游 mutable main；
装完会核对 package.json 里的真实版本与 commit，写进 engine/provenance.json。

安装流程：下载到本进程私有的临时目录 → 校验 → 原子换入；失败时旧 runtime 原样保留，
不会留下两个版本混在一起的半个 engine。重复运行幂等。

⚠ 为什么不建符号链接 / junction（踩过，别加回来）
  曾经为了省 130MB，把 node_modules 建成链接指向上游 monorepo，
  再让 engine/node_modules 链接回 skill 根目录 —— 形成环。后果：任何递归遍历
  （含删除）都顺着链接钻进上游源码树，表现为「engine 几十万文件、删不掉」，
  并且真的把引擎之外的源码连带删光。现在依赖只有一份真实目录：<skill>/node_modules。
  本文件删除任何目录前都会先拆掉里面的链接，只删链接本身，不跟进去。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ftlib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
NODE_MODULES = ROOT / "node_modules"
VENV = ROOT / ".venv"

REPO = "https://github.com/sitor-ai/frametell.git"
ENGINE_SUBPATH = "packages/engine"
ENGINE_TAG = "v0.7.4"
ENGINE_VERSION = "0.7.4"
ENGINE_COMMIT = "ca29a16cdbff06614cd2422d8b246f0e741e3330"
ENGINE_PACKAGE = "@frametell/engine"
PROVENANCE = "provenance.json"
ENGINE_REQUIRED = [
    "package.json",
    "boot.js",
    "runtime",
    "styles",
    "tools/render.mjs",
    "tools/check.mjs",
    "tools/pages.mjs",
]

NODE_MIN = (22, 19, 0)
PYTHON_MIN = (3, 9)
EDGE_TTS_VERSION = "7.2.8"

FFMPEG_EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
NODE_MARKERS = [
    "roughjs/bundled/rough.js",
    "playwright-core/package.json",
    "katex/package.json",
    "lxgw-wenkai-webfont/lxgwwenkai-regular.css",
    "@fontsource/caveat/500.css",
    "@fontsource/jetbrains-mono/400.css",
    f"ffmpeg-static/{FFMPEG_EXE}",
]

UV_INSTALL_HINT = "https://docs.astral.sh/uv/getting-started/installation/"
GIT_INSTALL_HINT = "https://git-scm.com/downloads"
NODE_INSTALL_HINT = "https://nodejs.org"


class InstallError(RuntimeError):
    """可预期的安装失败：消息直接给用户看，不打堆栈。"""


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""
    fix: str = ""
    required: bool = True


def ok(msg: str) -> None:
    print(f"    ✓ {msg}")


def note(msg: str) -> None:
    print(f"    ⚠ {msg}")


def step(n: int, msg: str) -> None:
    print(f"[{n}] {msg}")


# ── 路径安全（链接、删除、事务换入） ─────────────────────────────

def is_reparse(path: Path) -> bool:
    """符号链接 / Windows junction / 其他重解析点。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    attr = getattr(st, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attr & flag)


def find_links(root: Path) -> list[Path]:
    """树里所有链接（不跟随）。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if is_reparse(p):
                out.append(p)
    return out


def external_links(root: Path) -> list[Path]:
    """树里指向树外的链接。npm 的 node_modules/.bin 是树内相对链接，不算。"""
    base = root.resolve()
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if not is_reparse(p):
                continue
            try:
                target = (p.parent / os.readlink(p)).resolve()
            except OSError:
                out.append(p)
                continue
            if target != base and base not in target.parents:
                out.append(p)
    return out


def safe_rmtree(path: Path) -> bool:
    """删除本流程接管过的目录树；先拆链接，绝不顺着链接删别处。返回是否删干净。"""
    if not path.exists() and not is_reparse(path):
        return True
    if is_reparse(path):
        try:
            os.unlink(path)
        except OSError:
            return False
        return not path.exists()
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        for name in filenames:
            try:
                (Path(dirpath) / name).unlink()
            except OSError:
                pass
        for name in dirnames:
            p = Path(dirpath) / name
            try:
                if is_reparse(p):
                    os.unlink(p)
                else:
                    p.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass
    return not path.exists()


def promote_dir(staged: Path, target: Path, validate) -> None:
    """校验 staged 目录后原子换入 target；换入过程中失败则把旧目录退回去。"""
    validate(staged)
    if is_reparse(target):
        raise InstallError(
            f"{target} 是符号链接 / junction，拒绝覆盖。"
            f"请把它换成真实目录，或用 FRAMETELL_ENGINE 显式指定引擎位置。")
    if target.exists() and not target.is_dir():
        raise InstallError(f"{target} 已存在但不是目录，拒绝覆盖。")
    backup = target.with_name(f"{target.name}.old-{os.getpid()}")
    if backup.exists():
        raise InstallError(f"临时备份 {backup.name} 已存在（可能是并发安装），请稍后重试。")
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except OSError as e:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise InstallError(f"换入 {target.name} 失败，已退回原版本：{e}") from e
    if not safe_rmtree(backup):
        note(f"旧版本备份没删干净：{backup}（不影响使用，可手动删除）")


# ── 引擎 ─────────────────────────────────────────────────────────

def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def engine_package(dir: Path) -> dict:
    try:
        data = json.loads((dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_version(dir: Path) -> str:
    return str(engine_package(dir).get("version") or "")


def engine_provenance(dir: Path) -> dict:
    try:
        data = json.loads((dir / PROVENANCE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_ready(dir: Path) -> bool:
    return (dir / "tools" / "render.mjs").exists() and engine_version(dir) == ENGINE_VERSION


def looks_like_engine(dir: Path) -> bool:
    return (dir / "tools" / "render.mjs").exists() or engine_package(dir).get("name") == ENGINE_PACKAGE


def validate_engine(dir: Path) -> None:
    """staged 引擎能不能用：必需文件齐、版本对、没有链接。"""
    missing = [rel for rel in ENGINE_REQUIRED if not (dir / rel).exists()]
    if missing:
        raise InstallError(f"引擎目录不完整，缺 {', '.join(missing)}")
    pkg = engine_package(dir)
    if pkg.get("name") != ENGINE_PACKAGE:
        raise InstallError(f"package.json 的 name 是 {pkg.get('name')!r}，不是 {ENGINE_PACKAGE}")
    if pkg.get("version") != ENGINE_VERSION:
        raise InstallError(f"引擎版本是 {pkg.get('version')!r}，与锁定的 {ENGINE_VERSION} 不一致")
    links = find_links(dir)
    if links:
        raise InstallError(f"引擎目录里有符号链接 / junction，拒绝安装：{links[0]}")


def fetch_engine(force: bool = False, runner=run_cmd) -> None:
    override = os.environ.get("FRAMETELL_ENGINE")
    if override and (Path(override) / "tools" / "render.mjs").exists() and not force:
        ok(f"FRAMETELL_ENGINE 指定了外部引擎（{ftlib.ENGINE}），跳过下载")
        return
    if not force and engine_ready(ENGINE):
        ok(f"引擎已就位（v{engine_version(ENGINE)}），跳过")
        if engine_provenance(ENGINE).get("commit") != ENGINE_COMMIT:
            note(f"engine/provenance.json 缺失或不匹配（应来自 {ENGINE_COMMIT}），"
                 f"用 --force 可重装以获得可核对来源")
        return
    if ENGINE.exists() and not looks_like_engine(ENGINE):
        raise InstallError(
            f"{ENGINE} 已存在但不是引擎目录，拒绝覆盖未知数据。请先把它移走或改名再运行。")

    stage = ROOT / f".engine-fetch-{os.getpid()}"
    safe_rmtree(stage)
    print(f"    从 GitHub 拉取 {ENGINE_TAG}（只取 {ENGINE_SUBPATH}）…")
    try:
        cmds = [
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
             "--branch", ENGINE_TAG, REPO, str(stage)],
            ["git", "-C", str(stage), "sparse-checkout", "init", "--cone"],
            ["git", "-C", str(stage), "sparse-checkout", "set", ENGINE_SUBPATH],
            ["git", "-C", str(stage), "checkout"],
        ]
        for cmd in cmds:
            r = runner(cmd)
            if r.returncode != 0:
                tail = ((r.stderr or "") + (r.stdout or "")).strip()[-400:]
                raise InstallError(f"{' '.join(cmd[:5])} 失败：\n{tail}")
        head = runner(["git", "-C", str(stage), "rev-parse", "HEAD"])
        commit = (head.stdout or "").strip()
        if head.returncode != 0 or commit != ENGINE_COMMIT:
            raise InstallError(
                f"拉到的 commit 是 {commit or '?'}，与锁定的 {ENGINE_TAG} → {ENGINE_COMMIT} 不一致，拒绝安装。"
                f"（上游 tag 被移动或镜像返回了别的版本）")
        promote_dir(stage / ENGINE_SUBPATH, ENGINE, validate_engine)
        (ENGINE / PROVENANCE).write_text(json.dumps({
            "repo": REPO,
            "tag": ENGINE_TAG,
            "commit": ENGINE_COMMIT,
            "version": engine_version(ENGINE),
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        ok(f"引擎就位：{engine_version(ENGINE)} · {ENGINE_TAG} · {ENGINE_COMMIT[:12]}")
    finally:
        if not safe_rmtree(stage):
            note(f"临时目录没删干净：{stage}（可手动删除）")


# ── npm 依赖 ─────────────────────────────────────────────────────

def validate_deps(dir: Path) -> None:
    missing = [m for m in NODE_MARKERS if not (dir / m).exists()]
    if missing:
        raise InstallError(f"node_modules 不完整，缺 {', '.join(missing)}")
    escaping = external_links(dir)
    if escaping:
        raise InstallError(f"node_modules 里有指向树外的链接，拒绝安装：{escaping[0]}")


def deps_ready(dir: Path) -> bool:
    return all((dir / m).exists() for m in NODE_MARKERS)


def install_deps(force: bool = False, runner=run_cmd) -> None:
    if not force and deps_ready(NODE_MODULES):
        ok("npm 依赖已就位，跳过")
        drop_legacy_engine_link()
        return
    npm = shutil.which("npm")
    if not npm:
        raise InstallError(f"找不到 npm。请先装 Node.js ≥ 22.19：{NODE_INSTALL_HINT}")
    stage = ROOT / f".npm-stage-{os.getpid()}"
    safe_rmtree(stage)
    stage.mkdir(parents=True)
    print("    npm ci（按 package-lock.json 装，约 130MB，1–3 分钟）…")
    try:
        shutil.copy2(ROOT / "package.json", stage / "package.json")
        shutil.copy2(ROOT / "package-lock.json", stage / "package-lock.json")
        r = runner([npm, "ci", "--no-audit", "--no-fund"], cwd=stage)
        if r.returncode != 0:
            tail = ((r.stderr or "") + (r.stdout or "")).strip()[-500:]
            raise InstallError(
                f"npm ci 失败，原 node_modules 未改动：\n{tail}\n"
                f"国内网络可换镜像：npm config set registry https://registry.npmmirror.com")
        promote_dir(stage / "node_modules", NODE_MODULES, validate_deps)
        ok("npm 依赖就位")
    finally:
        if not safe_rmtree(stage):
            note(f"临时目录没删干净：{stage}（可手动删除）")
    drop_legacy_engine_link()


def drop_legacy_engine_link() -> None:
    """历史版本把 engine/node_modules 建成过链接，那是删目录时钻进别处的入口。"""
    legacy = ENGINE / "node_modules"
    if is_reparse(legacy):
        try:
            os.unlink(legacy)
            ok("已移除历史遗留的 engine/node_modules 链接")
        except OSError as e:
            note(f"engine/node_modules 是链接且删不掉：{e}")


# ── Python 锁定环境（edge-tts） ──────────────────────────────────

def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def locked_edge_tts(runner=run_cmd) -> str:
    """锁定环境里 edge_tts 的实际版本；装不起来返回空串。"""
    py = venv_python()
    if not py.exists():
        return ""
    r = runner([str(py), "-c", "import edge_tts; print(getattr(edge_tts, '__version__', '?'))"])
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def install_python_env(force: bool = False, runner=run_cmd) -> None:
    if not force and locked_edge_tts(runner=runner) == EDGE_TTS_VERSION:
        ok(f"锁定环境已就位（edge-tts {EDGE_TTS_VERSION}），跳过")
        return
    uv = shutil.which("uv")
    if not uv:
        raise InstallError(f"找不到 uv。装法：{UV_INSTALL_HINT}")
    if not (ROOT / "uv.lock").exists():
        raise InstallError(f"{ROOT / 'uv.lock'} 缺失，无法按锁定版本安装。")
    print(f"    uv sync --frozen（edge-tts {EDGE_TTS_VERSION} 及传递依赖）…")
    r = runner([uv, "sync", "--frozen"], cwd=ROOT)
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip()[-500:]
        raise InstallError(f"uv sync 失败：\n{tail}")
    got = locked_edge_tts(runner=runner)
    if got != EDGE_TTS_VERSION:
        raise InstallError(f"锁定环境里 edge-tts 是 {got or '不可用'}，应为 {EDGE_TTS_VERSION}")
    ok(f"Python 锁定环境就位（edge-tts {got}）")


# ── preflight ────────────────────────────────────────────────────

def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    if not m:
        return ()
    return tuple(int(g) for g in m.groups() if g is not None)


def version_at_least(got: tuple[int, ...], want: tuple[int, ...]) -> bool:
    if not got:
        return False
    size = max(len(got), len(want))
    return got + (0,) * (size - len(got)) >= want + (0,) * (size - len(want))


def tool_check(label: str, exe_names: list[str], version_args: list[str], minimum, fix: str) -> Check:
    exe = next((p for p in map(shutil.which, exe_names) if p), None)
    if not exe:
        return Check(label, False, "未安装", fix)
    got = parse_version(run_cmd([exe, *version_args]).stdout)
    if minimum and not version_at_least(got, minimum):
        want = ".".join(str(x) for x in minimum)
        return Check(label, False, f"版本 {'.'.join(map(str, got)) or '?'}，要求 ≥ {want}", fix)
    shown = ".".join(map(str, got)) if got else "?"
    need = f"（≥ {'.'.join(map(str, minimum))}）" if minimum else ""
    return Check(label, True, f"{shown}{need}")


def check_engine(effective: Path) -> list[Check]:
    override = os.environ.get("FRAMETELL_ENGINE")
    if override and not (Path(override) / "tools" / "render.mjs").exists():
        return [Check("引擎", False, f"FRAMETELL_ENGINE={override} 下没有 tools/render.mjs",
                      "修正或删除 FRAMETELL_ENGINE，或重跑安装")]
    if not engine_ready(effective):
        return [Check("引擎", False, f"{effective} 未就位或版本不是 {ENGINE_VERSION}",
                      "运行安装（不带 --check）")]
    checks = [Check("引擎", True, f"v{engine_version(effective)} · {effective}")]
    commit = engine_provenance(effective).get("commit")
    if commit == ENGINE_COMMIT:
        checks.append(Check("引擎来源", True, f"{ENGINE_TAG} · {ENGINE_COMMIT[:12]}"))
    else:
        checks.append(Check("引擎来源", False,
                            f"provenance 缺失或 commit 不明（setup 装出来的是 {ENGINE_COMMIT[:12]}）",
                            "用 setup --force 重装即可写入可核对的来源；手动复制的引擎无法核对到 commit",
                            required=False))
    return checks


def check_node_deps() -> list[Check]:
    candidates = ftlib.candidate_node_modules()
    out = [Check(d.name, d.ok, d.detail, "" if d.ok else "运行安装（不带 --check）")
           for d in ftlib.check_deps()]
    for rel, label in [
        ("playwright-core/package.json", "playwright-core"),
        ("katex/package.json", "katex"),
        (f"ffmpeg-static/{FFMPEG_EXE}", "ffmpeg-static"),
    ]:
        hit = next((nm / rel for nm in candidates if (nm / rel).exists()), None)
        out.append(Check(label, bool(hit), str(hit) if hit else "缺失",
                         "" if hit else "运行安装（不带 --check）"))
    return out


def check_ffmpeg() -> Check:
    try:
        return Check("ffmpeg", True, ftlib.ffmpeg())
    except ftlib.SkillError as e:
        return Check("ffmpeg", False, str(e).splitlines()[0],
                     "删掉无效的 FFMPEG_PATH，或运行安装以获取 ffmpeg-static")


def check_chrome() -> Check:
    override = os.environ.get("CHROME_PATH")
    chrome = ftlib.find_chrome()
    if override and not Path(override).exists():
        return Check("浏览器", False, f"CHROME_PATH={override} 不存在",
                     "修正或删除 CHROME_PATH，让 setup 自动探测 Chrome/Edge")
    if not chrome:
        return Check("浏览器", False, "没找到 Chrome / Edge",
                     "装一个 Chrome 或 Edge；渲染靠驱动本机浏览器")
    return Check("浏览器", True, str(chrome))


def check_edge_tts() -> Check:
    got = locked_edge_tts()
    if got == EDGE_TTS_VERSION:
        return Check("edge-tts", True, f"{got}（uv 锁定环境）")
    if not got:
        return Check("edge-tts", False, "锁定环境里不可用",
                     f"运行安装（uv sync --frozen 会装 {EDGE_TTS_VERSION}）")
    return Check("edge-tts", False, f"锁定环境里是 {got}，应为 {EDGE_TTS_VERSION}",
                 "运行安装 --force 重建 .venv")


def tool_checks() -> list[Check]:
    checks = [
        Check("Python", sys.version_info >= PYTHON_MIN,
              f"{sys.version.split()[0]}（≥ {'.'.join(map(str, PYTHON_MIN))}）",
              "装 Python 3.9+，或用 uv 管理的 Python 重新运行"),
        tool_check("Node.js", ["node"], ["-v"], NODE_MIN, f"装 Node.js ≥ 22.19：{NODE_INSTALL_HINT}"),
        tool_check("npm", ["npm"], ["--version"], (), "装 Node.js ≥ 22.19（自带 npm）"),
        tool_check("Git", ["git"], ["--version"], (), f"装 Git：{GIT_INSTALL_HINT}"),
        tool_check("uv", ["uv"], ["--version"], (), f"装 uv：{UV_INSTALL_HINT}"),
    ]
    return checks


def install_checks() -> list[Check]:
    checks = list(check_engine(ftlib.ENGINE))
    checks += check_node_deps()
    checks += [check_ffmpeg(), check_chrome(), check_edge_tts()]
    apiz = shutil.which("apiz")
    checks.append(Check("TTS apiz", bool(apiz), apiz or "未安装（可选，付费后端；默认用 edge）",
                        required=False))
    return checks


def report(title: str, checks: list[Check], final: bool = False) -> bool:
    print(title)
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.required else "⚠")
        print(f"    {mark} {c.label}")
        if c.detail:
            print(f"      {c.detail}")
        if not c.ok and c.fix:
            print(f"      修复：{c.fix}")
    failed = [c for c in checks if c.required and not c.ok]
    if final:
        print()
        print(f"{'✓ 就绪' if not failed else f'✗ {len(failed)} 项未就绪'}  {ROOT}")
    return not failed


# ── main ─────────────────────────────────────────────────────────

USAGE = """whiteboard-teacher-video 安装 + preflight

  python scripts/setup.py            安装（引擎 + npm 依赖 + Python 锁定环境）并检查
  python scripts/setup.py --check    只检查，不写任何文件
  python scripts/setup.py --force    重装引擎、npm 依赖与 Python 锁定环境

Windows 薄入口：powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 [--check|--force]
"""


def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    unknown = [a for a in argv if a not in ("--check", "--force")]
    if unknown:
        print(f"未知参数：{' '.join(unknown)}\n")
        print(USAGE)
        return 2
    check_only = "--check" in argv
    force = "--force" in argv

    print("\nwhiteboard-teacher-video" + ("（只检查）" if check_only else " 安装") + "\n")
    tools = tool_checks()
    if not report("[1] 前置工具", tools):
        print("先修好上面标 ✗ 的工具，再来一遍。")
        return 1
    if check_only:
        ok_all = report("[2] 运行时", install_checks(), final=True)
        return 0 if ok_all else 1

    try:
        step(2, "视频引擎（Frametell " + ENGINE_TAG + "）")
        fetch_engine(force=force)
        step(3, "npm 依赖（约 130MB）")
        install_deps(force=force)
        step(4, "Python 锁定环境（edge-tts）")
        install_python_env(force=force)
    except InstallError as e:
        print(f"\n✗ 安装中止：{e}")
        print("  已装好的部分保持可用；修好上面的问题后重跑即可（重复运行是安全的）。")
        return 1

    ok_all = report("[5] 运行时", install_checks(), final=True)
    print("""
下一步：
  uv run python scripts/ftnew.py D:/films/my-film --title "标题" --style whiteboard
  # 然后填 script.js → uv run python scripts/ftvoice.py → 画 pages/ → ftcheck.py → ftrender.py

细节见 SKILL.md 与 references/
""")
    return 0 if ok_all else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(main(sys.argv[1:]))