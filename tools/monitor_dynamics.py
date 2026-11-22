#!/usr/bin/env python3
"""KARDS 官方号动态监控（由工作区 kards/fetch.py 整合而来）。

与插件的分工：
  抓取     复用 dynamic_fetch.DynamicClient（wbi 签名 + 首页激活 buvid3 + 风控重试）
  去重     沿用原脚本的 state.json「seen」机制，并保留 --force
  归档     沿用原脚本的 archive/YYYYMMDD_<动态id>.txt
  推送     只把没播过的动态打到 stdout，外层 cron 有输出才转发到群里

用法：
    python3 tools/monitor_dynamics.py            # 增量：只输出新动态
    python3 tools/monitor_dynamics.py --force    # 无视去重记录，全部重发
    python3 tools/monitor_dynamics.py --limit 20 # 本次最多看多少条，默认 10
    python3 tools/monitor_dynamics.py --quiet    # 有更新也静默，只落盘

数据落在 data/plugin_data/bilikards/monitor/ 下，跟牌组缓存、卡图缓存放一起。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import data_paths  # noqa: E402
import dynamic_fetch  # noqa: E402

UID = "2127596945"
DATA = data_paths.MONITOR_DIR
ARCHIVE = DATA / "archive"
STATE = DATA / "state.json"
KEEP_SEEN = 500


def load_state():
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"seen": [], "last_run": "", "last_id": ""}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


CN_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日")
ISO_DATE = re.compile(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})")


def when_from_item(item):
    """优先用接口给的相对/绝对时间，拿不到就用归档时刻。"""
    text = (item.get("pub_time") or "").strip()
    return text or time.strftime("%m-%d %H:%M")


def day_key(when):
    """把「11月6日」「2026-11-06」「3小时前」统一成 YYYYMMDD 归档目录名。"""
    now = time.localtime()
    m = ISO_DATE.search(when or "")
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    m = CN_DATE.search(when or "")
    if m:
        return f"{now.tm_year:04d}{int(m.group(1)):02d}{int(m.group(2)):02d}"
    return time.strftime("%Y%m%d")


def archive_path(item, when):
    return ARCHIVE / "{}_{}.txt".format(day_key(when), item.get("id") or "unknown")


def render(item, when, link):
    lines = ["[{}] {}".format(item.get("type_label") or "动态", when)]
    if item.get("title"):
        lines.append(item["title"])
    if item.get("body"):
        lines.append(item["body"])
    if link:
        lines.append("链接: " + link)
    for url in (item.get("pics") or [])[:9]:
        lines.append(f"图: {url}")
    return "\n".join(lines)


def collect(limit):
    import asyncio

    client = dynamic_fetch.DynamicClient(uid=UID, cache_ttl=0)

    async def run():
        try:
            return await client.fetch(limit)
        finally:
            await client.close()

    return asyncio.run(run())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="无视去重记录")
    parser.add_argument("--limit", type=int, default=20, help="本次检查条数")
    parser.add_argument("--quiet", action="store_true", help="有更新也不打印")
    args = parser.parse_args()

    state = load_state()
    seen = set(state.get("seen") or [])
    fresh = []

    for item in collect(max(1, args.limit)):
        did = item.get("id") or ""
        if not did:
            continue
        if did in seen and not args.force:
            continue
        seen.add(did)
        fresh.append(item)

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    blocks = []
    for item in fresh:
        when = when_from_item(item)
        link = item.get("url") or ""
        text = render(item, when, link)
        try:
            archive_path(item, when).write_text(text + "\n", encoding="utf-8")
        except Exception as exc:
            print("归档失败 {}: {}".format(item.get("id"), exc), file=sys.stderr)
        blocks.append("\n".join([text, "链接: " + link] if link else [text]))

    state["seen"] = list(seen)[-KEEP_SEEN:]
    state["last_id"] = fresh[0].get("id") if fresh else state.get("last_id", "")
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

    if blocks and not args.quiet:
        print("\n\n".join(blocks))


if __name__ == "__main__":
    main()
