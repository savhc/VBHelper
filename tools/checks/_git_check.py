# -*- coding: utf-8 -*-
"""Git 服务自检：python _git_check.py

在临时目录建真实仓库，覆盖：
  detect / is_repo / status(porcelain v2 -z) / log / diff / branches /
  stage / unstage / commit / rename / untracked / conflicted 解析。
不联网、不碰当前项目仓库。
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from vbhelper.services.git import GitService, GitError, parse_status

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    svc = GitService()
    info = svc.detect()
    if info is None:
        print("SKIP  未找到 git.exe")
        return 0
    check("detect 返回版本与路径",
          "git version" in info.version and os.path.exists(info.path),
          info.version)

    tmp = tempfile.mkdtemp(prefix="vbgit_")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    try:
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "VBHelper Tester")
        git(repo, "config", "user.email", "tester@example.invalid")

        check("is_repo 识别工作区", svc.is_repo(repo) is True)
        check("is_repo 拒绝普通目录", svc.is_repo(tmp) is False)

        write(os.path.join(repo, "a.txt"), "one\n")
        write(os.path.join(repo, "b.txt"), "bee\n")
        st = svc.status(repo)
        check("初始仓库干净但有两个未跟踪",
              st.clean is False and sorted(st.untracked) == ["a.txt", "b.txt"],
              st.untracked)

        svc.stage(repo, ["a.txt", "b.txt"])
        st = svc.status(repo)
        check("stage 后两个新增进 staged",
              len(st.staged) == 2 and not st.untracked,
              [(f.path, f.status) for f in st.staged])
        check("stage 后 unstaged 为空", st.unstaged == [])

        out = svc.commit(repo, "第一笔", "说明行")
        check("commit 返回输出", "第一笔" in out or out.strip() != "")
        st = svc.status(repo)
        check("提交后干净", st.clean is True)
        check("分支名 main", st.branch == "main", st.branch)

        log = svc.log(repo)
        check("log 一条提交", len(log) == 1)
        check("log 字段完整",
              log[0].subject == "第一笔" and log[0].author == "VBHelper Tester"
              and len(log[0].hash) == 40 and log[0].timestamp > 0,
              log[0])

        # 工作区修改 -> unstaged + diff
        write(os.path.join(repo, "a.txt"), "one\nmore\n")
        st = svc.status(repo)
        check("修改进 unstaged",
              len(st.unstaged) == 1 and st.unstaged[0].path == "a.txt"
              and st.unstaged[0].status == "M",
              [(f.path, f.status) for f in st.unstaged])
        d = svc.diff(repo, "a.txt")
        check("工作区 diff 含新增行", "+more" in d.text and not d.truncated)

        svc.stage(repo, ["a.txt"])
        st = svc.status(repo)
        check("再次 stage 进 staged", len(st.staged) == 1 and not st.unstaged)
        d2 = svc.diff(repo, "a.txt", staged=True)
        check("暂存区 diff 含新增行", "+more" in d2.text)

        svc.unstage(repo, ["a.txt"])
        st = svc.status(repo)
        check("unstage 回到工作区",
              not st.staged and len(st.unstaged) == 1,
              [(f.path, f.status) for f in st.unstaged])

        svc.stage(repo, ["a.txt"])
        svc.commit(repo, "第二次")
        check("第二次提交后干净", svc.status(repo).clean is True)

        # rename（porcelain v2 的 "2" 记录）
        git(repo, "mv", "b.txt", "d.txt")
        st = svc.status(repo)
        renames = [f for f in st.staged if f.is_rename]
        check("rename 进 staged 且保留原名",
              len(renames) == 1 and renames[0].path == "d.txt"
              and renames[0].orig_path == "b.txt"
              and renames[0].status == "R",
              [(f.path, f.orig_path, f.status) for f in st.staged])

        # untracked + branches + log 限条
        write(os.path.join(repo, "c.txt"), "see\n")
        st = svc.status(repo)
        check("新增未跟踪文件", "c.txt" in st.untracked, st.untracked)

        git(repo, "branch", "feature")
        branches = svc.branches(repo)
        names = {b.name for b in branches}
        check("branches 列全部分支", {"main", "feature"} <= names, names)
        check("branches 标出当前分支",
              next(b for b in branches if b.name == "main").current is True)
        check("log 限条生效", len(svc.log(repo, limit=1)) == 1)
        check("log 指定分支可用", len(svc.log(repo, limit=10, branch="feature")) >= 1)

        try:
            svc.commit(repo, "   ")
            check("空提交消息被拒绝", False)
        except ValueError:
            check("空提交消息被拒绝", True)

        # 解析器边界：冲突记录 / detached HEAD
        raw = ("# branch.head (detached)\0"
               "u UU N... 100644 100644 100644 100644 aaa bbb x.txt\0")
        parsed = parse_status(raw.encode("utf-8"))
        check("解析冲突记录",
              parsed.detached is True and parsed.conflicted == ["x.txt"],
              parsed.conflicted)

        # 错误分类：非仓库执行 status
        try:
            svc.status(tmp)
            check("非仓库 status 抛 GitError", False)
        except GitError as exc:
            check("非仓库 status 抛 GitError", bool(str(exc)), str(exc)[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：%s" % ("全部通过" if not FAILS else "失败 %d 项" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
