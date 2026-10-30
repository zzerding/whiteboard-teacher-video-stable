"""setup.py / ftlib.py 回归测试。

不联网、不跑真实 TTS、不碰真实 skill 目录：git 与 npm 用假 runner 模拟，
涉及的路径都落在临时目录里。仅安装逻辑、校验、回滚、覆盖与路径解析被覆盖。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import ftlib  # noqa: E402


def _load_setup():
    spec = importlib.util.spec_from_file_location("wtv_setup", SCRIPTS / "setup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


setup = _load_setup()


def make_engine_tree(root: Path, version: str = setup.ENGINE_VERSION,
                     name: str = setup.ENGINE_PACKAGE, skip: list[str] | None = None) -> Path:
    skip = skip or []
    files = ["boot.js", "runtime/timing.js", "styles/whiteboard.css", "tools/render.mjs",
             "tools/check.mjs", "tools/pages.mjs"]
    for rel in files:
        if rel in skip:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("// stub\n", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"name": name, "version": version}),
                                       encoding="utf-8")
    return root


def completed(cmd: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeGit:
    """模拟 git：clone 时造出假仓库树，rev-parse 报指定 commit。"""

    def __init__(self, commit: str = setup.ENGINE_COMMIT, version: str = setup.ENGINE_VERSION,
                 skip: list[str] | None = None, clone_rc: int = 0):
        self.commit = commit
        self.version = version
        self.skip = skip
        self.clone_rc = clone_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            if self.clone_rc:
                return completed(cmd, self.clone_rc, stderr="clone failed")
            stage = Path(cmd[-1])
            make_engine_tree(stage / "packages" / "engine", version=self.version, skip=self.skip)
            return completed(cmd)
        if "rev-parse" in cmd:
            return completed(cmd, stdout=self.commit + "\n")
        return completed(cmd)


class FakeNpm:
    """模拟 npm ci：在 cwd 下造出（可能不完整的）node_modules。"""

    def __init__(self, missing: str | None = None, rc: int = 0):
        self.missing = missing
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None):
        self.calls.append(list(cmd))
        if self.rc:
            return completed(list(cmd), self.rc, stderr="npm failed")
        nm = Path(cwd) / "node_modules"
        for marker in setup.NODE_MARKERS:
            if marker == self.missing:
                continue
            p = nm / marker
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("stub\n", encoding="utf-8")
        return completed(list(cmd))


def fake_deps(root: Path, missing: str | None = None) -> Path:
    nm = root / "node_modules"
    for marker in setup.NODE_MARKERS:
        if marker == missing:
            continue
        p = nm / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stub\n", encoding="utf-8")
    return nm


class PinTests(unittest.TestCase):
    def test_engine_pin_is_the_reviewed_release(self):
        self.assertEqual(setup.ENGINE_TAG, "v0.7.4")
        self.assertEqual(setup.ENGINE_VERSION, "0.7.4")
        self.assertEqual(setup.ENGINE_COMMIT, "ca29a16cdbff06614cd2422d8b246f0e741e3330")
        src = (SCRIPTS / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"--branch", ENGINE_TAG', src)
        self.assertEqual(setup.EDGE_TTS_VERSION, "7.2.8")

    def test_python_lock_pins_edge_tts(self):
        pyproject = (SKILL / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("edge-tts==7.2.8", pyproject)
        lock = (SKILL / "uv.lock").read_text(encoding="utf-8")
        self.assertRegex(lock, r'name = "edge-tts"\nversion = "7\.2\.8"')

    def test_setup_uses_ci_not_install(self):
        src = (SCRIPTS / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"ci", "--no-audit"', src)
        self.assertNotIn('"install", "--no-audit"', src)


class VersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(setup.parse_version("v22.19.0"), (22, 19, 0))
        self.assertEqual(setup.parse_version("11.17.0"), (11, 17, 0))
        self.assertEqual(setup.parse_version("uv 0.8.4"), (0, 8, 4))
        self.assertEqual(setup.parse_version("git version 2.39.5"), (2, 39, 5))
        self.assertEqual(setup.parse_version("no digits"), ())

    def test_version_at_least(self):
        self.assertTrue(setup.version_at_least((22, 19, 0), (22, 19, 0)))
        self.assertTrue(setup.version_at_least((24, 19, 0), (22, 19, 0)))
        self.assertTrue(setup.version_at_least((22, 19), (22, 19, 0)))
        self.assertFalse(setup.version_at_least((22, 18, 9), (22, 19, 0)))
        self.assertFalse(setup.version_at_least((21, 99, 0), (22, 19, 0)))
        self.assertFalse(setup.version_at_least((), (22, 19, 0)))


class EngineValidateTests(unittest.TestCase):
    def test_accepts_complete_tree(self):
        with tempfile.TemporaryDirectory() as td:
            setup.validate_engine(make_engine_tree(Path(td)))

    def test_rejects_missing_tool(self):
        with tempfile.TemporaryDirectory() as td:
            tree = make_engine_tree(Path(td), skip=["runtime/timing.js"])
            with self.assertRaises(setup.InstallError):
                setup.validate_engine(tree)

    def test_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as td:
            tree = make_engine_tree(Path(td), version="0.7.2")
            with self.assertRaises(setup.InstallError) as ctx:
                setup.validate_engine(tree)
            self.assertIn("0.7.2", str(ctx.exception))

    def test_rejects_wrong_package_name(self):
        with tempfile.TemporaryDirectory() as td:
            tree = make_engine_tree(Path(td), name="something-else")
            with self.assertRaises(setup.InstallError):
                setup.validate_engine(tree)

    def test_rejects_symlink_inside_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tree = make_engine_tree(root / "engine")
            outside = root / "outside"
            outside.mkdir()
            try:
                os.symlink(outside, tree / "linked")
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不允许创建符号链接")
            with self.assertRaises(setup.InstallError):
                setup.validate_engine(tree)


class PromotionTests(unittest.TestCase):
    def test_replaces_target_and_drops_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = make_engine_tree(root / "engine", version="0.7.2")
            staged = make_engine_tree(root / "staged")
            setup.promote_dir(staged, target, setup.validate_engine)
            self.assertEqual(setup.engine_version(target), setup.ENGINE_VERSION)
            self.assertFalse(staged.exists())
            self.assertEqual([p.name for p in root.iterdir()], ["engine"])

    def test_validation_failure_keeps_previous_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = make_engine_tree(root / "engine", version="0.7.2")
            staged = make_engine_tree(root / "staged", skip=["tools/render.mjs"])
            with self.assertRaises(setup.InstallError):
                setup.promote_dir(staged, target, setup.validate_engine)
            self.assertEqual(setup.engine_version(target), "0.7.2")
            self.assertTrue((target / "runtime" / "timing.js").exists())
            self.assertFalse((root / f"engine.old-{os.getpid()}").exists())

    def test_rename_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = make_engine_tree(root / "engine", version="0.7.2")
            staged = make_engine_tree(root / "staged")
            real_replace = os.replace
            calls = []

            def flaky(src, dst):
                calls.append((str(src), str(dst)))
                if len(calls) == 2:
                    raise OSError("simulated rename failure")
                return real_replace(src, dst)

            with mock.patch("os.replace", new=flaky):
                with self.assertRaises(setup.InstallError):
                    setup.promote_dir(staged, target, setup.validate_engine)
            self.assertEqual(setup.engine_version(target), "0.7.2")
            self.assertTrue(staged.exists())

    def test_refuses_symlinked_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = make_engine_tree(root / "elsewhere")
            link = root / "engine"
            try:
                os.symlink(real, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不允许创建符号链接")
            staged = make_engine_tree(root / "staged")
            with self.assertRaises(setup.InstallError):
                setup.promote_dir(staged, link, setup.validate_engine)
            self.assertTrue((real / "tools" / "render.mjs").exists())

    def test_refuses_file_at_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "engine").write_text("not a dir", encoding="utf-8")
            staged = make_engine_tree(root / "staged")
            with self.assertRaises(setup.InstallError):
                setup.promote_dir(staged, root / "engine", setup.validate_engine)


class SafeDeleteTests(unittest.TestCase):
    def test_removes_tree_without_following_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            tree = root / "tree"
            tree.mkdir()
            (tree / "file.txt").write_text("x", encoding="utf-8")
            try:
                os.symlink(outside, tree / "link")
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不允许创建符号链接")
            self.assertTrue(setup.safe_rmtree(tree))
            self.assertFalse(tree.exists())
            self.assertTrue((outside / "keep.txt").exists())

    def test_external_links_ignores_internal_bin_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nm = root / "node_modules"
            (nm / "roughjs" / "bin").mkdir(parents=True)
            (nm / "roughjs" / "bin" / "cli.js").write_text("x", encoding="utf-8")
            (nm / ".bin").mkdir()
            try:
                os.symlink(Path("..") / "roughjs" / "bin" / "cli.js", nm / ".bin" / "roughjs")
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不允许创建符号链接")
            self.assertEqual(setup.external_links(nm), [])

            outside = root / "outside"
            outside.mkdir()
            os.symlink(outside, nm / "escape")
            self.assertEqual([p.name for p in setup.external_links(nm)], ["escape"])


class FetchEngineTests(unittest.TestCase):
    def _ctx(self, root: Path):
        return mock.patch.multiple(setup, ROOT=root, ENGINE=root / "engine")

    def test_installs_pinned_engine_with_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git = FakeGit()
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=git)
            engine = root / "engine"
            self.assertEqual(setup.engine_version(engine), setup.ENGINE_VERSION)
            prov = setup.engine_provenance(engine)
            self.assertEqual(prov["commit"], setup.ENGINE_COMMIT)
            self.assertEqual(prov["tag"], setup.ENGINE_TAG)
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".engine-fetch")], [])

    def test_repeat_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=FakeGit())
                git = FakeGit()
                setup.fetch_engine(runner=git)
            self.assertEqual(git.calls, [])

    def test_force_reinstalls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=FakeGit())
                git = FakeGit()
                setup.fetch_engine(force=True, runner=git)
            self.assertEqual(len(git.calls), 5)

    def test_commit_mismatch_keeps_previous_engine(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=FakeGit())
                before = setup.engine_version(root / "engine")
                with self.assertRaises(setup.InstallError):
                    setup.fetch_engine(force=True, runner=FakeGit(commit="deadbeef"))
            self.assertEqual(setup.engine_version(root / "engine"), before)
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".engine-fetch")], [])

    def test_partial_download_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=FakeGit())
                with self.assertRaises(setup.InstallError):
                    setup.fetch_engine(force=True, runner=FakeGit(skip=["tools/check.mjs"]))
            self.assertEqual(setup.engine_version(root / "engine"), setup.ENGINE_VERSION)
            self.assertTrue((root / "engine" / "tools" / "check.mjs").exists())

    def test_clone_failure_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.InstallError):
                    setup.fetch_engine(runner=FakeGit(clone_rc=128))
            self.assertFalse((root / "engine").exists())

    def test_valid_override_skips_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            external = make_engine_tree(root / "shared-engine")
            git = FakeGit()
            with self._ctx(root), \
                    mock.patch.dict(os.environ, {"FRAMETELL_ENGINE": str(external)}, clear=False), \
                    redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=git)
            self.assertEqual(git.calls, [])
            self.assertFalse((root / "engine").exists())

    def test_refuses_unknown_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = root / "engine"
            engine.mkdir()
            (engine / "notes.txt").write_text("user data", encoding="utf-8")
            git = FakeGit()
            with self._ctx(root), redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.InstallError):
                    setup.fetch_engine(runner=git)
            self.assertEqual(git.calls, [])
            self.assertTrue((engine / "notes.txt").exists())

    def test_old_version_is_replaced_not_overlaid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = make_engine_tree(root / "engine", version="0.7.2")
            (old / "runtime" / "legacy-only.js").write_text("stale", encoding="utf-8")
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.fetch_engine(runner=FakeGit())
            engine = root / "engine"
            self.assertEqual(setup.engine_version(engine), setup.ENGINE_VERSION)
            self.assertFalse((engine / "runtime" / "legacy-only.js").exists())


class InstallDepsTests(unittest.TestCase):
    def _ctx(self, root: Path):
        (root / "package.json").write_text('{"name":"x","version":"0.7.2"}', encoding="utf-8")
        (root / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
        return mock.patch.multiple(setup, ROOT=root, ENGINE=root / "engine",
                                   NODE_MODULES=root / "node_modules")

    def test_installs_from_lockfile_into_fresh_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            npm = FakeNpm()
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.install_deps(runner=npm)
            self.assertTrue(setup.deps_ready(root / "node_modules"))
            self.assertEqual(len(npm.calls), 1)
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".npm-stage")], [])

    def test_partial_install_keeps_previous_node_modules(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = fake_deps(root)
            (old / "sentinel.txt").write_text("previous install", encoding="utf-8")
            self.assertTrue(setup.deps_ready(old))
            with self._ctx(root), redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.InstallError):
                    setup.install_deps(force=True, runner=FakeNpm(missing="katex/package.json"))
            self.assertTrue(setup.deps_ready(root / "node_modules"))
            self.assertTrue((root / "node_modules" / "sentinel.txt").exists())
            self.assertFalse(any(p.name.startswith(".npm-stage") for p in root.iterdir()))

    def test_npm_failure_changes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = fake_deps(root)
            with self._ctx(root), redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.InstallError):
                    setup.install_deps(force=True, runner=FakeNpm(rc=1))
            self.assertTrue(setup.deps_ready(old))

    def test_skip_when_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_deps(root)
            npm = FakeNpm()
            with self._ctx(root), redirect_stdout(io.StringIO()):
                setup.install_deps(runner=npm)
            self.assertEqual(npm.calls, [])


class FfmpegLookupTests(unittest.TestCase):
    def _env(self, engine: Path, **env):
        patch_env = {k: v for k, v in os.environ.items() if k not in ("FFMPEG_PATH", "PATH")}
        patch_env.update(env)
        patch_env.setdefault("PATH", "")
        return mock.patch.multiple(ftlib, SKILL=engine.parent, ENGINE=engine), \
            mock.patch.dict(os.environ, patch_env, clear=True)

    def test_finds_ffmpeg_static_without_system_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td)
            bundled = skill / "node_modules" / "ffmpeg-static" / setup.FFMPEG_EXE
            bundled.parent.mkdir(parents=True)
            bundled.write_text("", encoding="utf-8")
            engine_patch, env_patch = self._env(skill / "engine")
            with engine_patch, env_patch:
                self.assertEqual(ftlib.ffmpeg(), ftlib.win(bundled))

    def test_invalid_override_is_an_error_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td)
            bundled = skill / "node_modules" / "ffmpeg-static" / setup.FFMPEG_EXE
            bundled.parent.mkdir(parents=True)
            bundled.write_text("", encoding="utf-8")
            engine_patch, env_patch = self._env(skill / "engine",
                                                FFMPEG_PATH=str(skill / "missing-ffmpeg"))
            with engine_patch, env_patch:
                with self.assertRaises(ftlib.SkillError):
                    ftlib.ffmpeg()

    def test_valid_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td)
            custom = skill / "custom-ffmpeg"
            custom.write_text("", encoding="utf-8")
            engine_patch, env_patch = self._env(skill / "engine", FFMPEG_PATH=str(custom))
            with engine_patch, env_patch:
                self.assertEqual(ftlib.ffmpeg(), ftlib.win(custom))

    def test_missing_everything_raises(self):
        with tempfile.TemporaryDirectory() as td:
            skill = Path(td)
            engine_patch, env_patch = self._env(skill / "engine")
            with engine_patch, env_patch:
                with self.assertRaises(ftlib.SkillError):
                    ftlib.ffmpeg()


class ChromeLookupTests(unittest.TestCase):
    def test_invalid_chrome_path_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            bogus = str(Path(td) / "no-such-browser")
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"CHROME_PATH": bogus}, clear=False), \
                 redirect_stderr(err):
                found = ftlib.find_chrome()
            self.assertNotEqual(str(found), bogus)
            self.assertIn("CHROME_PATH", err.getvalue())


class SetupScriptEntryTests(unittest.TestCase):
    def test_ps1_is_a_thin_forwarder(self):
        src = (SCRIPTS / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("setup.py", src)
        self.assertIn("exit $code", src)
        self.assertIn("$LASTEXITCODE", src)
        for duplicated in ("git clone", "npm install", "ffmpeg-static"):
            self.assertNotIn(duplicated, src)



class EngineSourceCheckTests(unittest.TestCase):
    def test_manual_copy_without_provenance_warns_but_passes(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"FRAMETELL_ENGINE": ""}, clear=False):
            tree = make_engine_tree(Path(td))
            checks = setup.check_engine(tree)
            with redirect_stdout(io.StringIO()):
                self.assertTrue(setup.report("t", checks, final=True))
            labels = {c.label: c for c in checks}
            self.assertFalse(labels["引擎来源"].ok)
            self.assertFalse(labels["引擎来源"].required)

    def test_invalid_override_fails_preflight(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"FRAMETELL_ENGINE": str(Path(td) / "nope")}, clear=False):
            checks = setup.check_engine(Path(td) / "engine")
            self.assertFalse(checks[0].ok)
            self.assertTrue(checks[0].required)

class ReportTests(unittest.TestCase):
    def test_required_failure_fails_report(self):
        checks = [setup.Check("引擎", False, "未就位", "跑 setup")]
        with redirect_stdout(io.StringIO()):
            self.assertFalse(setup.report("t", checks, final=True))

    def test_optional_failure_does_not_fail_report(self):
        checks = [setup.Check("apiz", False, "未安装", required=False)]
        with redirect_stdout(io.StringIO()):
            self.assertTrue(setup.report("t", checks, final=True))


if __name__ == "__main__":
    unittest.main()