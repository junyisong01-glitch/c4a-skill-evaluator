#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
C4 提交自动评审器（c4-skill-evaluator）

输入：一个包含若干作者 C4 提交文件的文件夹（或含按作者分子目录的结构）
输出：Markdown 评审报告 + CSV 评审详表

评审管线（对应 C4A 挑战 Level 1→3）：
  L1 文件采集   —— 扫描 *_C4_* 文件，按「作者_拼音_C4_描述.扩展名」解析作者并分组
  L2 完整性检查 —— 检查 C4 五个必须文件（skill说明/可执行内容/demo/教学说明/AI日志）是否齐全
  L3 四条件质量 —— 按「可复用/可执行/可验证/IO明确」四条条件做信号命中规则评审并打分
  （Level 4 报告 —— 汇总为 Markdown + CSV，含评审依据，供人工复核）

用法：
  python3 evaluator.py <文件夹路径> [--out 报告.md] [--csv 详表.csv]

零依赖：仅用 Python 标准库（os/sys/re/argparse/datetime/csv/collections）。
"""
import os
import re
import sys
import io
import csv
import argparse
import datetime
import zipfile
import tarfile
from collections import defaultdict

# ===================== 评审规则（与 references/c4_rubric.yaml 同源，固化于此） =====================

REQUIRED_FILES = {
    "skill_doc": {
        "label": "Skill 说明文档",
        "filename": ["skill说明", "skill_doc", "skill-doc", "skill_description", "说明"],
        "content": ["使用场景", "输入", "输出", "解决什么问题", "use case"],
        "ext": [".md", ".pdf", ".docx"],
    },
    "executable": {
        "label": "可执行内容",
        "filename": ["技能", "skill"],
        "content": ["```", "def ", "class ", "import ", "---\nname:", "workflow"],
        "ext": [".skill", ".py", ".md", ".zip"],
    },
    "demo": {
        "label": "Demo（视频/截图）",
        "filename": ["demo", "演示", "截图", "screen", "录屏"],
        "content": [],
        "ext": [".mp4", ".mov", ".webm", ".png", ".jpg", ".gif"],
    },
    "teaching_doc": {
        "label": "教学说明",
        "filename": ["教学说明", "教学", "tutorial", "teaching", "上手指南", "quickstart", "how-to"],
        "content": ["上手", "步骤", "常见坑", "安装", "注意事项", "getting started"],
        "ext": [".md", ".pdf", ".docx"],
    },
    "ai_log": {
        "label": "AI 日志",
        "filename": ["AI日志", "AI_log", "ai-log", "ai_日志", "日志"],
        "content": ["使用的 AI", "AI 工具", "prompt", "迭代", "ChatGPT", "Claude", "DeepSeek", "CogSeed"],
        "ext": [".md", ".pdf", ".docx"],
    },
}

QUALITY_CRITERIA = {
    "reusable": {
        "label": "可复用", "weight": 0.25,
        "positive": ["安装", "install", "环境要求", "requirements", "依赖", "dependencies",
                     "pip install", "npm install", "兼容", "compatible", "README"],
        "negative": ["/Users/", "/home/", "C:\\", "我的电脑", "my_secret", "api_key =", "password ="],
        "checks": ["有安装/环境说明", "无硬编码绝对路径", "注明适用平台或兼容性", "依赖可获取"],
    },
    "executable": {
        "label": "可执行", "weight": 0.25,
        "positive": ["```python", "```bash", "```javascript", "def ", "class ", "---\nname:",
                     "workflow", "pipeline", "步骤", "运行"],
        "negative": [],
        "checks": ["含可运行代码/prompt/workflow", ".skill 包结构有效", "无明显语法错误", "SKILL.md 含 frontmatter"],
    },
    "verifiable": {
        "label": "可验证", "weight": 0.25,
        "positive": ["测试", "test", "example", "示例", "预期", "expected", "demo", "result", "结果", "验收"],
        "negative": [],
        "checks": ["有测试用例或示例", "定义预期输出", "demo 展示真实结果", "成败标准清晰"],
    },
    "clear_io": {
        "label": "IO 明确", "weight": 0.25,
        "positive": ["输入", "输出", "input", "output", "→", "接受", "返回", "produces", "returns", "给定"],
        "negative": [],
        "checks": ["有「输入X→输出Y」一句话", "明确输入类型/格式", "明确输出类型/格式", "注明边界输入"],
    },
}

TEXT_EXT = {".md", ".py", ".skill", ".yaml", ".yml", ".json", ".txt"}
ARCHIVE_EXT = {".skill", ".zip"}


def read_text(path, limit=200000):
    """读取文本文件内容；二进制/失败返回空串。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


def read_archive_text(path, limit=200000):
    """解包 .skill/.zip 归档，拼接内部文本成员（SKILL.md/scripts/references），失败返回空串。"""
    parts = []
    total = 0

    def _add(data):
        nonlocal total
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="ignore")
        parts.append(data)
        total += len(data)

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.endswith("/") or os.path.splitext(n)[1].lower() not in TEXT_EXT:
                        continue
                    _add(z.read(n))
                    if total >= limit:
                        break
        else:
            with tarfile.open(path, "r:*") as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    if os.path.splitext(m.name)[1].lower() not in TEXT_EXT:
                        continue
                    f = t.extractfile(m)
                    if f is None:
                        continue
                    _add(f.read())
                    if total >= limit:
                        break
    except Exception:
        pass
    return "\n".join(parts)


def extract_author(fname):
    """从「作者_C4_描述.扩展名」解析作者（取 _C4 之前的段）。"""
    m = re.match(r"^(.+?)_C4", fname)
    if m:
        return m.group(1).strip()
    return None


def collect_files(folder):
    """递归扫描，返回 {作者: [(绝对路径, 文件名, 内容), ...]}；无法解析作者的归入 _未分类。"""
    by_author = defaultdict(list)
    for root, dirs, files in os.walk(folder):
        for fn in files:
            if fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            author = extract_author(fn)
            key = author if author else "_未分类"
            ext = os.path.splitext(fn)[1].lower()
            if ext in ARCHIVE_EXT:
                content = read_archive_text(full)  # .skill/.zip 解包读内部 SKILL.md/脚本
            elif ext in TEXT_EXT:
                content = read_text(full)
            else:
                content = ""
            by_author[key].append((full, fn, content))
    return by_author


def detect_required(key, files):
    """对某个必须文件类，判断该作者是否有文件命中（文件名命中 > 扩展名+内容命中 > 扩展名命中[仅demo]）。"""
    rule = REQUIRED_FILES[key]
    for full, fn, content in files:
        base = os.path.basename(fn)
        lower = base.lower()
        ext = os.path.splitext(base)[1].lower()
        if any(p.lower() in lower for p in rule["filename"]):
            return True, fn
        if ext in rule["ext"] and content and any(s in content for s in rule["content"]):
            return True, fn
        if key == "demo" and ext in rule["ext"]:
            return True, fn
    return False, None


def completeness(author_files):
    """返回 {必须文件label: bool} 与 files_present。"""
    result, present = {}, 0
    for key, rule in REQUIRED_FILES.items():
        hit, fn = detect_required(key, author_files)
        result[rule["label"]] = bool(hit)
        present += 1 if hit else 0
    return result, present


def criterion_score(criterion, pos_text, neg_text=None):
    """单一条件打分（rubric 规则：✅=≥2 命中、⚠️=1、❌=0），并返回命中证据。

    pos_text  —— 用于正信号匹配的全部文本（技能本体 + 说明，不含 AI 日志/demo 二进制）
    neg_text  —— 用于负信号（硬编码路径/密钥）匹配的文本，默认同 pos_text；
                 实际调用时只传「技能本体」文本，避免 AI 日志里的 /Users 路径被误判为技能缺陷。
    """
    c = QUALITY_CRITERIA[criterion]
    pos_hits = [s for s in c["positive"] if s in pos_text]
    neg_hits = [s for s in c["negative"] if s in (neg_text if neg_text is not None else pos_text)]
    n_pos = len(pos_hits)
    n_neg = len(neg_hits)
    # 负信号逐条抵消一个正信号（硬编码密钥/绝对路径是硬伤，但个别可解释）
    eff = max(0, n_pos - n_neg)
    if eff >= 2:
        mark = "✅"
        score = 1.0
    elif eff == 1:
        mark = "⚠️"
        score = 0.5
    else:
        mark = "❌"
        score = 0.0
    return mark, score, pos_hits, neg_hits


def _file_kind(fn, content):
    """把文件归类到 C4 五件套之一，用于区分「技能本体」vs「AI 日志/demo」。"""
    base = os.path.basename(fn)
    lower = base.lower()
    ext = os.path.splitext(base)[1].lower()
    if ext in REQUIRED_FILES["demo"]["ext"]:
        return "demo"
    if any(p.lower() in lower for p in REQUIRED_FILES["ai_log"]["filename"]):
        return "ai_log"
    if any(p.lower() in lower for p in REQUIRED_FILES["teaching_doc"]["filename"]):
        return "teaching_doc"
    if ext in (".skill", ".zip", ".py") or any(p.lower() in lower for p in REQUIRED_FILES["executable"]["filename"]):
        return "executable"
    if any(p.lower() in lower for p in REQUIRED_FILES["skill_doc"]["filename"]):
        return "skill_doc"
    return "other"


def evaluate(author, author_files):
    """对单个作者做完整评审，返回结果 dict。"""
    comp_map, present = completeness(author_files)
    # 正信号文本：技能本体（skill说明 + 可执行内容 + 教学说明）；排除 AI 日志与 demo 二进制。
    # 负信号文本：仅「可执行内容」（.skill 包 / .py / 脚本）——硬编码绝对路径/密钥只对技能本体判缺陷，
    # 不扫 AI 日志（学生记录过程时写 /Users/... 是正常内容，不是技能缺陷）。
    body_parts, exec_parts = [], []
    for full, fn, content in author_files:
        kind = _file_kind(fn, content)
        if kind == "executable":
            body_parts.append(content)
            exec_parts.append(content)
        elif kind in ("skill_doc", "teaching_doc"):
            body_parts.append(content)
    pos_text = "\n".join(body_parts)
    neg_text = "\n".join(exec_parts) or pos_text
    crit = {}
    for key in QUALITY_CRITERIA:
        mark, sc, pos, neg = criterion_score(key, pos_text, neg_text)
        crit[key] = {"label": QUALITY_CRITERIA[key]["label"], "mark": mark,
                     "score": sc, "pos": pos, "neg": neg}
    completeness_score = present / 5.0
    quality_score = sum(crit[k]["score"] for k in crit) / 4.0
    composite = completeness_score * 0.4 + quality_score * 0.6
    if composite >= 0.8:
        grade = "A"
    elif composite >= 0.6:
        grade = "B"
    elif composite >= 0.4:
        grade = "C"
    else:
        grade = "D"
    return {
        "author": author, "files": author_files, "completeness": comp_map,
        "present": present, "crit": crit, "completeness_score": completeness_score,
        "quality_score": quality_score, "composite": composite, "grade": grade,
    }


def render_markdown(results, folder, count_by_author):
    lines = []
    lines.append("# C4 提交自动评审报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 评审目录：`{folder}`")
    lines.append(f"- 检测到作者数：{len(results)}")
    lines.append("")
    lines.append("## 总览（按综合分排序）")
    lines.append("")
    lines.append("| 排名 | 作者 | 五文件完整 | 质量分 | 综合分 | 评级 |")
    lines.append("|---|---|---|---|---|---|")
    ordered = sorted(results, key=lambda r: r["composite"], reverse=True)
    for i, r in enumerate(ordered, 1):
        lines.append(f"| {i} | {r['author']} | {r['present']}/5 | "
                     f"{r['quality_score']:.2f} | {r['composite']:.2f} | {r['grade']} |")
    lines.append("")
    for r in ordered:
        lines.append(f"## 作者：{r['author']}")
        lines.append("")
        lines.append("### 完整性（C4 五文件）")
        lines.append("")
        for label, hit in r["completeness"].items():
            lines.append(f"- {'✅' if hit else '❌'} {label}")
        lines.append("")
        lines.append("### 质量评审（四条件，各 0.25）")
        lines.append("")
        lines.append("| 条件 | 判定 | 命中正信号 | 命中负信号 |")
        lines.append("|---|---|---|---|")
        for k, c in r["crit"].items():
            pos = "、".join(c["pos"]) if c["pos"] else "—"
            neg = "、".join(c["neg"]) if c["neg"] else "—"
            lines.append(f"| {c['label']} | {c['mark']} | {pos} | {neg} |")
        lines.append("")
        lines.append(f"- 完整性分：{r['completeness_score']:.2f}（{r['present']}/5）")
        lines.append(f"- 质量分：{r['quality_score']:.2f}")
        lines.append(f"- **综合分：{r['composite']:.2f}（评级 {r['grade']}）**")
        lines.append("")
    return "\n".join(lines)


def render_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "作者", "完整文件数", "完整性分", "质量分", "综合分", "评级",
                    "可复用", "可执行", "可验证", "IO明确"])
        ordered = sorted(results, key=lambda r: r["composite"], reverse=True)
        for i, r in enumerate(ordered, 1):
            w.writerow([i, r["author"], f"{r['present']}/5", f"{r['completeness_score']:.2f}",
                        f"{r['quality_score']:.2f}", f"{r['composite']:.2f}", r["grade"],
                        r["crit"]["reusable"]["mark"], r["crit"]["executable"]["mark"],
                        r["crit"]["verifiable"]["mark"], r["crit"]["clear_io"]["mark"]])
    return path


def main():
    ap = argparse.ArgumentParser(description="C4 提交自动评审器")
    ap.add_argument("folder", help="包含 C4 提交文件的文件夹")
    ap.add_argument("--out", default="评审报告.md", help="Markdown 报告输出路径")
    ap.add_argument("--csv", default="评审详表.csv", help="CSV 详表输出路径")
    args = ap.parse_args()

    by_author = collect_files(args.folder)
    # 过滤掉一个作者都没有实际文件的空组
    by_author = {a: fs for a, fs in by_author.items() if fs}

    results = [evaluate(a, fs) for a, fs in by_author.items()]

    md = render_markdown(results, args.folder, len(by_author))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    csv_path = render_csv(results, args.csv)

    print(f"✅ 评审完成：{len(results)} 位作者")
    for r in sorted(results, key=lambda x: x["composite"], reverse=True):
        print(f"  - {r['author']}: 完整 {r['present']}/5, 综合 {r['composite']:.2f} ({r['grade']})")
    print(f"📄 报告：{args.out}")
    print(f"📊 详表：{csv_path}")


if __name__ == "__main__":
    main()
