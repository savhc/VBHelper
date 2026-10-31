"""导出可发布的源码快照，不带开发历史。

本地仓库保留完整提交历史；对外仓库只放一次提交的源码快照。

用法::

    python tools/release.py                  # 默认输出到 ../VBHelper-release
    python tools/release.py --out D:/tmp/rel
    python tools/release.py --tag v0.1.0     # 快照内建立标签

快照白名单之外的目录（ref/、_t*/、_shots/、dist*/、build*/、.workbuddy/、
docs/plan/）一律不复制。
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要进入发布包的顶层条目
INCLUDE = [
    "vbhelper",
    "tools",
    "docs/dev",
    "docs/dev",
    "run.py",
    "build.bat",
    "VBHelper.spec",
    "requirements.txt",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.en.md",
    ".gitignore",
    ".gitattributes",
]

# 白名单内仍需剔除的内容
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "legacy"}
EXCLUDE_SUFFIX = (".pyc", ".pyo")


def copy_tree(src, dst):
    skipped = []
    for base, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(base, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for name in names:
            if name.endswith(EXCLUDE_SUFFIX):
                skipped.append(os.path.join(rel, name))
                continue
            shutil.copy2(os.path.join(base, name), os.path.join(target, name))
    return skipped


def run(cmd, cwd):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("命令失败：%s\n%s" % (" ".join(cmd), proc.stderr.strip()))
        sys.exit(1)
    return proc.stdout.strip()


def version() -> str:
    path = os.path.join(ROOT, "vbhelper", "__init__.py")
    for line in open(path, encoding="utf-8"):
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    return "0.0.0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "VBHelper-release"))
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)

    copied = 0
    for item in INCLUDE:
        src = os.path.join(ROOT, item)
        dst = os.path.join(out, item)
        if not os.path.exists(src):
            print("缺少条目：%s" % item)
            sys.exit(1)
        if os.path.isdir(src):
            skipped = copy_tree(src, dst)
            copied += 1
        else:
            shutil.copy2(src, dst)
            copied += 1

    run(["git", "init", "-b", "main"], out)
    run(["git", "add", "-A"], out)
    msg = "VBHelper %s" % version()
    run(["git", "-c", "user.name=VBHelper", "-c", "user.email=noreply@localhost",
         "commit", "-q", "-m", msg], out)
    if args.tag:
        run(["git", "tag", "-a", args.tag, "-m", msg], out)

    tracked = run(["git", "ls-files"], out).splitlines()
    size = sum(os.path.getsize(os.path.join(out, p)) for p in tracked if os.path.isfile(os.path.join(out, p)))

    print("输出目录：%s" % out)
    print("提交：%s" % msg)
    print("文件：%d 个，%.1f MB" % (len(tracked), size / 1024 / 1024))
    if args.tag:
        print("标签：%s" % args.tag)
    print("\n下一步：")
    print("  cd \"%s\"" % out)
    print("  git remote add origin git@github.com:<账号>/VBHelper.git")
    print("  git push -u origin main %s" % ("--tags" if args.tag else ""))


if __name__ == "__main__":
    main()
