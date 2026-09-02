#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 同步工具 —— 解决本机 git push 被代理拦截（HTTP2 502）的问题。

原理：
  - git 读操作（fetch/clone）在本机可用，push 常被拦截
  - 因此 push 走 GitHub Contents API（GET 拿 sha → PUT 提交 base64 内容）

用法：
  python3 gh_sync.py status          # 对比本地与远程差异
  python3 gh_sync.py pull            # 拉取远程最新覆盖本地（会先备份）
  python3 gh_sync.py push [文件...]  # 推送本地改动（不传文件=推送全部有差异的）
  python3 gh_sync.py push --all      # 强制推送全部文件

Token 读取顺序：
  1. 环境变量 GITHUB_TOKEN
  2. ~/.config/gh_token（建议 chmod 600）
  3. 命令行参数 --token
"""

import base64
import json
import os
import sys
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ============ 配置 ============
OWNER = "zhangxiaoli1759-tech"
REPO = "westlake-science-portal"
BRANCH = "main"
API = "https://api.github.com"

# 需要同步的文件（相对仓库根目录）
TRACKED_FILES = [
    ".gitignore",
    "PRD_理学院管理后台.md",
    "admin-prototype.html",
    "index.html",
    "website-homepage.html",
    "gh_sync.py",
]

ROOT = Path(__file__).resolve().parent
VERIFY_SSL = True


# ============ Token ============
def get_token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    p = Path.home() / ".config" / "gh_token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    print("❌ 未找到 GitHub Token。")
    print("   方式一：export GITHUB_TOKEN=ghp_xxxx")
    print("   方式二：把 token 写入 ~/.config/gh_token（chmod 600）")
    sys.exit(1)


# ============ HTTP ============
def api_request(path, token, method="GET", data=None):
    url = f"{API}/repos/{OWNER}/{REPO}/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "gh-sync-script",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:600]
        print(f"   ❌ HTTP {e.code}: {detail}")
        return None
    except Exception as e:
        print(f"   ❌ 网络错误: {e}")
        return None


# ============ 工具 ============
def git_blob_sha(filepath: Path) -> str:
    """计算文件的 git blob sha1（与 GitHub API 返回的 sha 同口径）"""
    raw = filepath.read_bytes()
    h = b"blob " + str(len(raw)).encode() + b"\0" + raw
    import hashlib
    return hashlib.sha1(h).hexdigest()


def remote_tree(token):
    """获取远程所有文件的 sha"""
    d = api_request(f"git/trees/{BRANCH}?recursive=1", token)
    if not d or "tree" not in d:
        return {}
    return {f["path"]: f["sha"] for f in d["tree"] if f["type"] == "blob"}


def download_file(token, path) -> bytes | None:
    """下载远程文件原始内容"""
    url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "gh-sync-script"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"   ❌ 下载失败 {path}: {e}")
        return None


# ============ 命令 ============
def cmd_status(token):
    print(f"📊 对比本地与远程（{OWNER}/{REPO}@{BRANCH}）\n")
    tree = remote_tree(token)
    if not tree:
        print("❌ 无法获取远程文件列表，检查 token 是否有效")
        return 1

    diff, same, missing_remote, missing_local = [], [], [], []

    for rel in TRACKED_FILES:
        local = ROOT / rel
        if not local.exists():
            if rel in tree:
                missing_local.append(rel)
            continue
        lsha = git_blob_sha(local)
        rsha = tree.get(rel)
        if rsha is None:
            missing_remote.append(rel)
        elif lsha == rsha:
            same.append(rel)
        else:
            diff.append(rel)

    if same:
        print("✅ 一致（无需同步）：")
        for f in same:
            print(f"   {f}")
    if diff:
        print("\n🔄 有差异（本地≠远程）：")
        for f in diff:
            print(f"   {f}")
    if missing_remote:
        print("\n🆕 仅本地有（远程无，push 会新建）：")
        for f in missing_remote:
            print(f"   {f}")
    if missing_local:
        print("\n⬇️  仅远程有（本地无，pull 会下载）：")
        for f in missing_local:
            print(f"   {f}")

    print()
    if not diff and not missing_remote and not missing_local:
        print("🎉 本地与远程完全一致")
        return 0
    print(f"提示：有差异时 → 本地改完用 `python3 gh_sync.py push` 上传")
    return 0


def cmd_pull(token):
    print(f"⬇️  拉取远程最新代码（{BRANCH}）\n")
    tree = remote_tree(token)
    if not tree:
        print("❌ 无法获取远程文件列表")
        return 1

    changed = []
    for rel in TRACKED_FILES:
        local = ROOT / rel
        rsha = tree.get(rel)
        if rsha is None:
            continue
        if local.exists() and git_blob_sha(local) == rsha:
            continue

        # 有差异 → 备份后覆盖
        if local.exists():
            bak = local.with_suffix(local.suffix + ".bak")
            bak.write_bytes(local.read_bytes())
            print(f"   📦 已备份 {rel} → {bak.name}")

        content = download_file(token, rel)
        if content is None:
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        changed.append(rel)
        print(f"   ✅ 已更新 {rel}（{len(content)} bytes）")

    if not changed:
        print("   本地已是最新，无需拉取")
    else:
        print(f"\n🎉 已拉取 {len(changed)} 个文件")

    # 同步本地 git 引用，避免 git status 出现假未推送
    try:
        subprocess.run(["git", "fetch", "origin", BRANCH], cwd=ROOT,
                       capture_output=True, timeout=30)
        subprocess.run(["git", "reset", "--mixed", f"origin/{BRANCH}"],
                       cwd=ROOT, capture_output=True, timeout=15)
        print("   🔗 本地 git 引用已对齐远程")
    except Exception:
        pass
    return 0


def cmd_push(token, files=None, force_all=False, message=None):
    tree = remote_tree(token)
    if not tree:
        print("❌ 无法获取远程文件列表")
        return 1

    if force_all:
        targets = [f for f in TRACKED_FILES if (ROOT / f).exists()]
    elif files:
        targets = files
    else:
        targets = []
        for rel in TRACKED_FILES:
            local = ROOT / rel
            if not local.exists():
                continue
            lsha = git_blob_sha(local)
            if tree.get(rel) != lsha:
                targets.append(rel)

    if not targets:
        print("✅ 本地与远程一致，无需推送")
        return 0

    print(f"⬆️  准备推送 {len(targets)} 个文件：{', '.join(targets)}\n")

    for rel in targets:
        local = ROOT / rel
        if not local.exists():
            print(f"   ⚠️  跳过（本地不存在）: {rel}")
            continue
        raw = local.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        payload = {
            "message": message or f"chore: update {rel} via gh_sync",
            "content": b64,
            "branch": BRANCH,
        }
        if rel in tree:  # 已存在的文件必须带 sha
            payload["sha"] = tree[rel]

        # URL 里的中文文件名需要编码
        from urllib.parse import quote
        result = api_request(f"contents/{quote(rel)}", token, method="PUT", data=payload)
        if result and "commit" in result:
            print(f"   ✅ {rel}  → {result['commit']['sha'][:8]}")
        else:
            print(f"   ❌ {rel} 推送失败")

    # 同步本地 git 引用
    try:
        subprocess.run(["git", "fetch", "origin", BRANCH], cwd=ROOT,
                       capture_output=True, timeout=30)
        subprocess.run(["git", "reset", "--mixed", f"origin/{BRANCH}"],
                       cwd=ROOT, capture_output=True, timeout=15)
        print("\n   🔗 本地 git 引用已对齐远程")
    except Exception:
        pass

    print(f"\n🎉 推送完成，GitHub Pages 约 30-60 秒后生效")
    print(f"   🔗 https://{OWNER}.github.io/{REPO}/")
    return 0


# ============ 入口 ============
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--token=")]
    tok_arg = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--token=")), None)

    if not args:
        print(__doc__)
        return 0

    cmd = args[0]
    token = tok_arg or get_token()

    if cmd == "status":
        return cmd_status(token)
    elif cmd == "pull":
        return cmd_pull(token)
    elif cmd == "push":
        rest = args[1:]
        force = "--all" in rest
        msg = next((a.split("=", 1)[1] for a in rest if a.startswith("-m=")), None)
        files = [f for f in rest if not f.startswith("-")]
        return cmd_push(token, files if files else None, force, msg)
    else:
        print(__doc__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
