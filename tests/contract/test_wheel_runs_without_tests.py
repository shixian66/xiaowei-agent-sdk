"""wheel 只含新包 ``xiaowei``（P1-B Task 8 切换正式入口），并在无仓库源码、无 tests/ 的干净环境中
可导入、带齐迁移与静态资源，两个正式入口都能运行。旧包 ``xiaowei_agent`` 不再进入 wheel。
"""

import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_contains_only_the_new_package_and_runs_without_sources(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    cache = tmp_path / "uv-cache"
    subprocess.run(  # noqa: S603 -- uv 是 shutil.which 解析出的绝对路径
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--cache-dir",
            str(cache),
            "--out-dir",
            str(dist),
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        entry_points = archive.read(
            next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        ).decode()
    packages = {name.split("/", 1)[0] for name in names if "/" in name}
    assert {p for p in packages if not p.endswith(".dist-info")} == {"xiaowei"}
    for resource in (
        "xiaowei/migrations/001_initial.sql",
        "xiaowei/migrations/002_p1b_channels.sql",
        "xiaowei/static/index.html",
        "xiaowei/static/app.js",
        "xiaowei/static/app.css",
    ):
        assert resource in names
    assert "xiaowei = xiaowei.cli:main" in entry_points

    subprocess.run(  # noqa: S603 -- 当前 Python 与目标路径均由测试控制
        [
            sys.executable,
            "-m",
            "venv",
            str(venv),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    purelib_result = subprocess.run(  # noqa: S603 -- venv Python 位于测试临时目录
        [
            str(venv / "bin/python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    purelib = Path(purelib_result.stdout.strip())
    (purelib / "locked-runtime-dependencies.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 -- uv 是 shutil.which 解析出的绝对路径
        [
            uv,
            "pip",
            "install",
            "--no-deps",
            "--offline",
            "--cache-dir",
            str(cache),
            "--python",
            str(venv / "bin/python"),
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    probe = """
import importlib.resources
import importlib.util
import pathlib
import xiaowei
assert "venv" in pathlib.Path(xiaowei.__file__).parts
assert importlib.util.find_spec("xiaowei_agent") is None
for name in ("tests", "tests.fakes"):
    try:
        assert importlib.util.find_spec(name) is None
    except ModuleNotFoundError:
        pass
root = importlib.resources.files("xiaowei")
assert root.joinpath("migrations/002_p1b_channels.sql").read_text(encoding="utf-8")
assert root.joinpath("static/index.html").read_bytes()
"""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(  # noqa: S603 -- venv Python 位于本测试创建的临时目录
        [str(venv / "bin/python"), "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    for command in (
        [str(venv / "bin/xiaowei"), "--help"],
        [str(venv / "bin/python"), "-m", "xiaowei", "--help"],
    ):
        result = subprocess.run(  # noqa: S603 -- 本测试创建的 venv 中的入口
            command, cwd=tmp_path, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0 and "serve" in result.stdout, result.stderr
