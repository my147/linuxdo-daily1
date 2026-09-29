#!/usr/bin/env python3
"""
从本机 Chrome profile 导出 linux.do 的登录 cookie，供 GitHub Secrets 使用。

用法：
    python export_cookies.py                     # 用默认 profile
    python export_cookies.py --profile <路径>     # 指定 profile 目录
    python export_cookies.py --list              # 只列出，不输出敏感值

输出：一段 base64 字符串，粘到 GitHub 仓库的 Secret `LINUXDO_COOKIES` 即可。

安全提示：
  - 这段字符串等同于你的登录凭证，**不要提交到仓库、不要发给任何人**
  - 导出后如果你后悔了，可以在 linux.do 里「登出所有设备」使其立即失效
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

# 与登录相关的 cookie
#   注意：cf_clearance 是 **Cloudflare 绑定出口 IP** 的通行证，
#   在 A 地导出、拿到 B 地（比如 GitHub runner）用是无效的，甚至可能
#   让 CF 更警觉。所以默认**不导出**，让 runner 自己重新获取。
WANTED = {
    "_t",                # Discourse 会话令牌（最关键，唯一必需）
    "_forum_session",    # 论坛会话
    "g_state",
}
OPTIONAL = {
    "cf_clearance",      # 默认排除，见上
    "_cfuvid", "__cfuvid",
}

DEFAULT_PROFILE = r"C:\Users\shen_ovo\.dsh\chrome-ldc-clean"


# ------------------------------------------------------------------ 解密
def _blob(data: bytes):
    class B(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
    return B(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                    ctypes.POINTER(ctypes.c_char)))


def get_master_key(profile: str) -> bytes:
    p = os.path.join(profile, "Local State")
    with open(p, encoding="utf-8") as f:
        st = json.load(f)
    enc = base64.b64decode(st["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise RuntimeError("密钥前缀不是 DPAPI")
    out = _blob(b"")
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob(enc[5:])), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("DPAPI 解密失败（是否同一 Windows 用户？）")
    key = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return key


def decrypt(enc: bytes, key: bytes) -> str:
    if enc[:3] in (b"v10", b"v20"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = AESGCM(key).decrypt(enc[3:15], enc[15:], None)
        # Chrome 127+ 在明文前有 32 字节域绑定前缀
        if len(raw) > 32:
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                raw = raw[32:]
        return raw.decode("utf-8", "replace")
    # 老格式：DPAPI
    out = _blob(b"")
    if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob(enc)), None, None, None, None, 0, ctypes.byref(out)):
        d = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return d.decode("utf-8", "replace")
    return ""


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="Chrome profile 目录")
    ap.add_argument("--list", action="store_true", help="只列出 cookie 名，不输出值")
    ap.add_argument("--raw", action="store_true", help="输出 Cookie 头格式而非 base64")
    args = ap.parse_args()

    db = os.path.join(args.profile, "Default", "Network", "Cookies")
    if not os.path.exists(db):
        print(f"[!] 找不到 Cookies: {db}")
        print("    确认路径是否正确，或先在该 profile 里登录 linux.do")
        return 1

    # 复制副本，避免占用锁
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "c.db")
    try:
        shutil.copy2(db, dst)
    except Exception as e:
        print(f"[!] 复制 cookie 数据库失败（Chrome 可能在运行）: {e}")
        return 1

    try:
        key = get_master_key(args.profile)
    except Exception as e:
        print(f"[!] 获取解密密钥失败: {e}")
        return 1

    con = sqlite3.connect(dst)
    cur = con.cursor()
    cur.execute("""SELECT host_key, name, encrypted_value, expires_utc
                   FROM cookies WHERE host_key LIKE '%linux.do%'""")
    rows = cur.fetchall()
    con.close()

    now = datetime.now()
    picked: dict[str, str] = {}
    print("=" * 74)
    print(f" 从 {args.profile} 找到 {len(rows)} 条 linux.do cookie")
    print("=" * 74)
    for host, name, enc, exp in rows:
        if name not in WANTED:
            continue
        exp_s = ""
        expired = False
        if exp and exp > 0:
            try:
                dt = datetime(1601, 1, 1) + timedelta(microseconds=exp)
                exp_s = dt.strftime("%Y-%m-%d %H:%M")
                expired = dt < now
            except Exception:
                pass
        try:
            val = decrypt(enc, key)
        except Exception as e:
            print(f"  ✗ {name:26s} 解密失败: {e}")
            continue
        if not val:
            print(f"  ✗ {name:26s} 空值")
            continue
        mark = "⚠️已过期" if expired else "✅"
        print(f"  {mark} {name:26s} len={len(val):<5} exp={exp_s}")
        # 过期的 cf_clearance 不必带（云端 IP 不同，本来也会重新拿）
        if name == "cf_clearance" and expired:
            continue
        picked[name] = val

    shutil.rmtree(tmp, ignore_errors=True)

    if not picked:
        print("\n[!] 没有可用的 cookie")
        return 1

    if "_t" not in picked:
        print("\n[!] ⚠️ 缺少关键的 `_t` cookie —— 这个 profile 可能没有登录")
        print("    请先用该 profile 登录 linux.do")
        return 1

    print("\n" + "=" * 74)
    print(f" 将使用 {len(picked)} 个 cookie: {', '.join(sorted(picked))}")
    print("=" * 74)

    if args.list:
        print("\n（--list 模式，不输出敏感值）")
        return 0

    if args.raw:
        out = "; ".join(f"{k}={v}" for k, v in sorted(picked.items()))
    else:
        out = base64.b64encode(
            json.dumps(picked, ensure_ascii=False).encode("utf-8")).decode("ascii")

    print("\n下面是 Secret 的值（复制整段，含所有字符）：\n")
    print("-" * 74)
    print(out)
    print("-" * 74)
    print(f"\n长度: {len(out)} 字符")
    print("\n下一步：")
    print("  1. 打开你的 GitHub 仓库 -> Settings -> Secrets and variables -> Actions")
    print("  2. New repository secret")
    print("  3. Name 填  LINUXDO_COOKIES")
    print("  4. Secret 粘贴上面那段")
    print("\n安全提醒：")
    print("  - 这段等同于你的登录凭证，不要提交进仓库、不要外发")
    print("  - cookie 有效期约到 _t 的过期时间；失效后重新跑本脚本导出即可")
    print("  - 想立即作废：在 linux.do 设置里「登出所有设备」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
