"""测试共用的小工具：把脚本目录加进导入路径，准备临时目录。所有测试都不连网络。"""
import io
import json
import contextlib
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="fb-test-"))


def run_main(main, argv):
    """运行一个脚本的 main()，返回 (退出码, stdout 文本)。stderr 丢掉。"""
    old = sys.argv
    sys.argv = argv
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main()
            except SystemExit as e:
                code = e.code
    finally:
        sys.argv = old
    return code, out.getvalue()


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
