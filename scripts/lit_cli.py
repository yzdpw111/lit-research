#!/usr/bin/env python3
"""lit_cli —— lit-research 规范的可执行检查器。

设计原则（照 `ieee-research` / `wanfang-research` 的体例）：
  1. **纯函数**：所有判定都是"输入路径/对象 → 输出 Finding 列表"，**不联网、不起浏览器**；
  2. **离线可测**：构造临时目录树即可单测，不需要任何外部依赖；
  3. **可读的失败信息**：每条 Finding 要说清"哪条规矩、在哪、怎么改"。

当前实现 `layout`（对着 `standards/S-07-目录与产物.md` §8 的 code 表）。
`audit` 尚未实现。

用法：
    python scripts/lit_cli.py layout "<课题目录>"
    python scripts/lit_cli.py layout "<课题目录>" --json
    python scripts/lit_cli.py layout "<课题目录>" --seal      # 封存只读区哈希
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────── 规范常量（S-07）

#: 顶层只允许这些条目（S-07 §1.4）。`line-*` 之类不属于顶层。
STAGE_DIRS: Tuple[str, ...] = (
    "00_方向地图",
    "01_检索",
    "02_候选库",
    "03_全文",
    "04_精读",
    "05_聚合",
    "06_综述",
    "07_核验",
)
TOP_FILES: Tuple[str, ...] = ("README.md", "lit.config.json")

#: ★ **来源目录不写死**：规范不绑定任何具体检索来源（配 ieee/wanfang、CNKI、arXiv、
#: Google Scholar…都行）。真正的来源列表在 `lit.config.json` 的 `sources`；
#: 这里只是**没配时的兜底**，且它同时是"常见来源"的示例，不是依赖。
SOURCE_FALLBACK: Tuple[str, ...] = ("ieee", "wanfang")

#: 会话文件：`layout --seal` 写、`layout` 读（S-07 §3.3）
SEAL_NAME = ".lit-seal.json"

#: 证据档位白名单（S-01）。**权威定义在 lit.config.json**，这里只是兜底。
TIER_FALLBACK: Tuple[str, ...] = (
    "full_text",
    "abstract_only",
    "unreadable",
    "source_conflict",
    "insufficient_evidence",
    "bad_source",
)

#: 引用键：`[IEEE:11181461]` / `[WF:D04271643]` / `[IEEE:1,2]` 也算多键
RE_CITE_KEY = re.compile(r"\[(IEEE|WF):([A-Za-z0-9]+)\]")
#: 文件名即 ID：`IEEE_11181461.pdf` / `WF_D04271643.txt`
RE_ID_FILENAME = re.compile(r"^(IEEE|WF)_([A-Za-z0-9]+)\.(pdf|txt)$")

#: "散文里写死了规范**阈值**"的典型形态（S-07 §4.1）。
#: ★ 实测修正：**不许把"近 N 年"当违规**——那是 S-02 里的**概念词**，
#:   正文提"近 2 年论文数趋势"完全正常；只有**带阈值的断言**（"不少于 40 篇"/"≥1/3"）
#:   才是"把规范数值写死在散文里"。初版把 `近\s*[一二三四五六七八九十\d]+\s*年` 也算了进去，
#:   在真实课题上一次报出 3 条假阳性。
HARD_NUMBER_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"[≥>]=?\s*\d+\s*篇", "篇数硬线"),
    (r"不少于\s*\d+\s*篇", "篇数硬线"),
    (r"至少\s*\d+\s*篇", "篇数硬线"),
    # ★ 实测修正：初版 `\d+\s*/\s*[13]\b` 会把**真实研究数据**当违规——
    #   实测在真实对比表里匹配到 "2.41 / 1.47"（两个 R² 值）里的「41 / 1」，报了假阳性。
    #   收紧：分子不许紧跟小数点/数字，分母后也不许再接数字或小数点。
    (r"(?<![\d.])\b\d+\s*/\s*[13](?![\d.])", "比例硬线"),
    (r"[≥>]=?\s*1\s*/\s*[0-9]", "比例硬线"),
    (r"[≥>]=?\s*\d+(?:\.\d+)?\s*%", "比例硬线"),
)


# ───────────────────────────────────────────────────────────────── 数据结构


@dataclass
class Finding:
    """一条检查结果。`severity` ∈ {error, warn}。"""

    code: str
    severity: str
    message: str
    loc: str = ""
    fix: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "loc": self.loc,
            "fix": self.fix,
        }


@dataclass
class LayoutResult:
    root: str
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "counts": {"error": len(self.errors), "warn": len(self.warns)},
            "findings": [f.as_dict() for f in self.findings],
        }


# ─────────────────────────────────────────────────────────── 读配置（纯函数）


def load_config(root: Path) -> Dict[str, Any]:
    """读 `lit.config.json`。**缺文件不报错**——由 `check_layout` 报成一条 finding。"""
    p = root / "lit.config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tier_whitelist(config: Dict[str, Any]) -> Tuple[str, ...]:
    """档位白名单**优先取配置**（S-07 §4.1：数值只在配置里）。"""
    tiers = config.get("tiers")
    if isinstance(tiers, list) and tiers:
        return tuple(str(t) for t in tiers)
    if isinstance(tiers, dict) and tiers:
        return tuple(str(k) for k in tiers)
    return TIER_FALLBACK


def sources_of(config: Dict[str, Any]) -> Tuple[str, ...]:
    """来源目录列表**优先取配置**（`lit.config.json` 的 `sources`）。

    ★ 规范**不绑定任何具体检索来源**：一个课题可以配 `["ieee","wanfang"]`，
    另一个可以配 `["arxiv","cnki","scholar"]`；检查器只认配置里的这一份。
    """
    src = config.get("sources")
    if isinstance(src, list) and src:
        return tuple(str(s) for s in src)
    return SOURCE_FALLBACK


def load_refs(root: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """读 `02_候选库/refs.json`。

    允许两种形状：`[{...}, ...]` 或 `{"refs": [...]}`。
    返回 `(记录列表, 错误说明)`；文件不存在时返回 `([], None)`（是否该存在由上层判）。
    """
    p = root / "02_候选库" / "refs.json"
    if not p.is_file():
        return [], None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - 病态文件
        return [], f"refs.json 无法解析：{exc}"
    if isinstance(data, dict):
        data = data.get("refs") or data.get("items") or []
    if not isinstance(data, list):
        return [], "refs.json 顶层既不是数组也不是含 refs/items 的对象"
    return [x for x in data if isinstance(x, dict)], None


# ────────────────────────────────────────────────────────────── 各项检查


def check_top_entries(root: Path) -> List[Finding]:
    """S-07 §1.4：顶层只允许白名单条目。"""
    out: List[Finding] = []
    allowed = set(STAGE_DIRS) | set(TOP_FILES) | {SEAL_NAME}
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name.startswith(".") and child.name != SEAL_NAME:
            continue  # 隐藏文件（.git 等）不参与判定
        if child.name in allowed:
            continue
        out.append(
            Finding(
                "LAYOUT_UNKNOWN_TOP",
                "error",
                f"顶层出现了白名单外的「{child.name}」",
                loc=child.name,
                fix=f"移出课题目录，或改用合法阶段目录（{'、'.join(STAGE_DIRS)}）",
            )
        )
    return out


def check_dup_stage(root: Path) -> List[Finding]:
    """S-07 §6.1：同一阶段编号只许有一个目录（多个＝改名残骸）。"""
    out: List[Finding] = []
    seen: Dict[str, List[str]] = {}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = re.match(r"^(\d{2})_", child.name)
        if m:
            seen.setdefault(m.group(1), []).append(child.name)
    for num, names in sorted(seen.items()):
        if len(names) > 1:
            out.append(
                Finding(
                    "LAYOUT_DUP_STAGE",
                    "error",
                    f"阶段编号 {num} 下有 {len(names)} 个目录：{'、'.join(names)}",
                    loc=num,
                    fix="目录**只增不改名**（S-07 §6.1）；请合并或删除改名残骸",
                )
            )
    return out


def check_readonly_zones(root: Path, sources: Sequence[str]) -> List[Finding]:
    """S-07 §3.1/§3.2：`raw/` 只许落盘产物；`pdf|text` 下文件名必须是 ID。

    ★ 实测修正：`raw/` 里**允许 `.log`**。抓取工具会把**自己的启动日志**
    （典型是 `chrome-launch.log`）写进 `IE_LOGS_DIR`/`WF_LOGS_DIR` 指向的目录，
    而规范要求的正是把该变量指到 `raw/`——**冲突在规范这一侧，不在工具那一侧**。
    这些日志同属"原始落盘"，不该报错（初版会反复报，实测被它挡住两次）。
    """
    out: List[Finding] = []
    allowed_raw_suffix = {".json", ".log"}
    for src in sources:
        raw = root / "01_检索" / src / "raw"
        if not raw.is_dir():
            continue
        for f in sorted(raw.iterdir()):
            if f.is_file() and f.suffix.lower() not in allowed_raw_suffix:
                out.append(
                    Finding(
                        "LAYOUT_RAW_NON_JSON",
                        "error",
                        f"只读证据区 raw/ 里有既非 .json 也非 .log 的文件「{f.name}」",
                        loc=str(f.relative_to(root)),
                        fix="原始落盘只应是抓取工具产出的 .json/.log；别的文件请移出只读区",
                    )
                )
    for sub in ("pdf", "text"):
        d = root / "03_全文" / sub
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and not RE_ID_FILENAME.match(f.name):
                out.append(
                    Finding(
                        "LAYOUT_ID_FILENAME",
                        "error",
                        f"{sub}/ 下文件名不是 ID 形式：「{f.name}」",
                        loc=str(f.relative_to(root)),
                        fix="改成 `IEEE_<arnumber>." + f.suffix.lstrip(".") + "` 或 `WF_<文档ID>."
                        + f.suffix.lstrip(".") + "`（S-07 §3.2：文件名即 ID，别用标题）",
                    )
                )
    return out


def check_refs(root: Path) -> List[Finding]:
    """S-07 §2.2：refs.json 的 ID 唯一、每篇有合法 `tier`。"""
    out: List[Finding] = []
    refs, err = load_refs(root)
    if err:
        out.append(Finding("LAYOUT_MISSING_TIER", "error", err, loc="02_候选库/refs.json"))
        return out
    cfg = tier_whitelist(load_config(root))
    seen: Dict[str, int] = {}
    for i, r in enumerate(refs):
        rid = r.get("id")
        if not rid:
            out.append(
                Finding(
                    "LAYOUT_MISSING_TIER",
                    "error",
                    f"第 {i + 1} 条没有 `id`",
                    loc="02_候选库/refs.json",
                    fix="每篇必须有 ID（`IEEE:<arnumber>` / `WF:<文档ID>`），见 S-07 §2.1",
                )
            )
            continue
        seen[str(rid)] = seen.get(str(rid), 0) + 1
        tier = r.get("tier")
        if not tier:
            out.append(
                Finding(
                    "LAYOUT_MISSING_TIER",
                    "error",
                    f"{rid} 缺 `tier`（证据档位）",
                    loc="02_候选库/refs.json",
                    fix=f"补 tier，取值见 S-01：{'、'.join(cfg)}",
                )
            )
        elif str(tier) not in cfg:
            out.append(
                Finding(
                    "LAYOUT_BAD_TIER",
                    "error",
                    f"{rid} 的 tier=「{tier}」不在白名单",
                    loc="02_候选库/refs.json",
                    fix=f"合法取值：{'、'.join(cfg)}（白名单在 lit.config.json 的 tiers）",
                )
            )
    for rid, n in sorted(seen.items()):
        if n > 1:
            out.append(
                Finding(
                    "LAYOUT_DUP_ID",
                    "error",
                    f"ID 重复 {n} 次：{rid}",
                    loc="02_候选库/refs.json",
                    fix="ID 必须唯一（S-07 §2.2）——题名相近的文献尤其容易并成一条",
                )
            )
    return out


def check_dangling_cites(root: Path) -> List[Finding]:
    """S-07 §2.3：产物 md 里的 `[IEEE:x]` / `[WF:y]` 必须在 refs.json 里存在。"""
    out: List[Finding] = []
    refs, _ = load_refs(root)
    known = {str(r.get("id")) for r in refs if r.get("id")}
    for p in sorted(root.rglob("*.md")):
        if p.name in ("README.md",) or "standards" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # pragma: no cover
            continue
        for m in RE_CITE_KEY.finditer(text):
            key = f"{m.group(1)}:{m.group(2)}"
            if key not in known:
                out.append(
                    Finding(
                        "LAYOUT_DANGLING_CITE",
                        "error",
                        f"引用了 refs.json 里没有的 {key}",
                        loc=f"{p.relative_to(root)}:{text[: m.start()].count(chr(10)) + 1}",
                        fix="补进 02_候选库/refs.json，或改掉这个引用键",
                    )
                )
    return out


def check_hard_numbers_in_prose(root: Path) -> List[Finding]:
    """S-07 §4.1：规范数值只放 `lit.config.json`，md 里不许重写（**warn**——引用语境也算）。"""
    out: List[Finding] = []
    for p in sorted(root.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # pragma: no cover
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for pat, kind in HARD_NUMBER_PATTERNS:
                m = re.search(pat, line)
                if m:
                    out.append(
                        Finding(
                            "LAYOUT_HARD_NUMBER_IN_PROSE",
                            "warn",
                            f"正文里出现{kind}「{m.group(0)}」——规范数值应只在 lit.config.json",
                            loc=f"{p.relative_to(root)}:{line_no}",
                            fix="改成引用配置（或注明「见 lit.config.json」），避免两处漂移",
                        )
                    )
                    break
    return out


def check_failure_lists(root: Path) -> List[Finding]:
    """S-07 §5：有获取动作就要有 `获取失败.md`；有筛选动作就要有 `排除清单.md`。"""
    out: List[Finding] = []
    has_pdf = (root / "03_全文" / "pdf").is_dir()
    has_refs = (root / "02_候选库" / "refs.json").is_file()
    if has_pdf and not (root / "03_全文" / "获取失败.md").is_file():
        out.append(
            Finding(
                "LAYOUT_MISSING_FAILURE_LIST",
                "error",
                "已有 03_全文/pdf/ 但没有 03_全文/获取失败.md",
                loc="03_全文/",
                fix="失败清单分五类记录（未订阅 / 书·标准 / 非 PDF 权限页 / 站点改版 / 限流），见 S-07 §5.1",
            )
        )
    if has_refs and not (root / "02_候选库" / "排除清单.md").is_file():
        out.append(
            Finding(
                "LAYOUT_MISSING_FAILURE_LIST",
                "error",
                "已有 refs.json 但没有 02_候选库/排除清单.md",
                loc="02_候选库/",
                fix="记录筛掉了哪些、为什么（同词异义 / 不相关），见 S-07 §5.2",
            )
        )
    return out


def check_query_logs(root: Path, sources: Sequence[str]) -> List[Finding]:
    """S-07 §7.2：检索式必须留档，否则调研不可复现。"""
    out: List[Finding] = []
    for src in sources:
        raw = root / "01_检索" / src / "raw"
        if raw.is_dir() and any(raw.glob("*.json")):
            if not (root / "01_检索" / src / "检索式.md").is_file():
                out.append(
                    Finding(
                        "LAYOUT_MISSING_QUERY_LOG",
                        "error",
                        f"01_检索/{src}/raw/ 有落盘结果，但缺 检索式.md",
                        loc=f"01_检索/{src}/",
                        fix="留档：关键词组 + 命中数 + 检索日期（S-07 §7.2）",
                    )
                )
    return out


# ────────────────────────────────────────────────────────────── 封存（只读区）


def seal_files(root: Path, sources: Sequence[str]) -> Dict[str, str]:
    """把只读区里所有文件算 sha256（S-07 §3.3）。"""
    out: Dict[str, str] = {}
    zones: List[Path] = []
    for src in sources:
        zones.append(root / "01_检索" / src / "raw")
    zones += [root / "03_全文" / "pdf", root / "03_全文" / "text"]
    for z in zones:
        if not z.is_dir():
            continue
        for f in sorted(z.rglob("*")):
            if f.is_file():
                out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def write_seal(root: Path, sources: Optional[Sequence[str]] = None) -> Path:
    if sources is None:
        sources = sources_of(load_config(root))
    p = root / SEAL_NAME
    p.write_text(
        json.dumps({"files": seal_files(root, sources)}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def check_seal(root: Path, sources: Sequence[str]) -> List[Finding]:
    """S-07 §3.1：只读区被封存后不许改动。"""
    p = root / SEAL_NAME
    if not p.is_file():
        return []
    try:
        old = json.loads(p.read_text(encoding="utf-8")).get("files") or {}
    except Exception:
        return [Finding("LAYOUT_SEAL_MISSING", "error", "封存文件无法解析", loc=SEAL_NAME)]
    new = seal_files(root, sources)
    out: List[Finding] = []
    for rel, digest in sorted(old.items()):
        if rel not in new:
            out.append(
                Finding(
                    "LAYOUT_SEAL_CHANGED",
                    "error",
                    f"只读区文件被删除：{rel}",
                    loc=rel,
                    fix="只读证据区不许删（S-07 §3.1）——改写证据＝毁证",
                )
            )
        elif new[rel] != digest:
            out.append(
                Finding(
                    "LAYOUT_SEAL_CHANGED",
                    "error",
                    f"只读区文件被改动：{rel}",
                    loc=rel,
                    fix="只读证据区不许改（S-07 §3.1）",
                )
            )
    for rel in sorted(set(new) - set(old)):
        out.append(
            Finding(
                "LAYOUT_SEAL_CHANGED",
                "error",
                f"封存后只读区新增了文件：{rel}",
                loc=rel,
                fix="新增原始证据请重新 --seal；已封存区不许追加",
            )
        )
    return out


# ────────────────────────────────────────────────────────────────── 主检查


def check_layout(root: Path) -> LayoutResult:
    """跑全部 `layout` 检查（纯函数：只读文件系统，不写、不联网）。"""
    res = LayoutResult(root=str(root))
    if not root.is_dir():
        res.findings.append(
            Finding("LAYOUT_UNKNOWN_TOP", "error", f"目录不存在：{root}", loc=str(root))
        )
        return res
    for name in TOP_FILES:
        if not (root / name).is_file():
            res.findings.append(
                Finding(
                    "LAYOUT_UNKNOWN_TOP",
                    "error",
                    f"缺 {name}",
                    loc=name,
                    fix="课题目录必须有 README.md 与 lit.config.json（S-07 §1.3）",
                )
            )
    res.findings += check_top_entries(root)
    res.findings += check_dup_stage(root)
    sources = sources_of(load_config(root))
    res.findings += check_readonly_zones(root, sources)
    res.findings += check_refs(root)
    res.findings += check_dangling_cites(root)
    res.findings += check_failure_lists(root)
    res.findings += check_query_logs(root, sources)
    res.findings += check_seal(root, sources)
    res.findings += check_hard_numbers_in_prose(root)
    return res


# ─────────────────────────────────────────── skill 自身文档守卫（selfcheck）
#
# ★ 为什么需要它：`layout` 检查的是**课题目录**，检查不到 **skill 自己的文档**。
#   实测就出过事——`SKILL.md` 的索引指向了 6 个当时还不存在的 `standards/S-0X-*.md`，
#   属于**悬空引用**（正是 S-07 §2.3 禁止的），但**没有任何检查能抓到**。
#   这条守卫把那个缺口补上，照 `njust-thesis-writer` 的 `check_docs.py` 体例。

#: 文档里引用的 skill 内部路径。
#: ★ 必须排除**通配与占位符**——实测 `standards/*.md`、`references/xxx.md` 这类散文里的
#: 举例被当成了真实引用，报出假阳性（检查器自己的 bug，比漏报更烦人）。
PLACEHOLDER_RE = re.compile(r"^[xX×]+$|\.\.\.|^<|^[a-zA-Z]$")
RE_DOC_REF = re.compile(
    r"`?((?:standards|integrations|references|assets|scripts)/[^`\s）)、，。*]+?\.(?:md|py|json))`?"
)
#: 条款引用：`S-01 §3` / `见 S-03 §2.1`
RE_CLAUSE_REF = re.compile(r"S-(\d{2})\s*§\s*([0-9]+(?:\.[0-9]+)*)")
#: 条款定义：既要认 `**§3.1**` 这种加粗，**也要认 `## §7 标题` 这种二级标题**
#: （实测只认前者会把 `§7` 报成不存在）。
RE_CLAUSE_DEF_BOLD = re.compile(r"\*\*§([0-9]+(?:\.[0-9]+)*)\*\*")
RE_CLAUSE_DEF_HEAD = re.compile(r"^#{2,4}\s*§([0-9]+(?:\.[0-9]+)*)\b", re.M)
#: 形如 `LAYOUT_XXX` / `AUDIT_XXX` 的 code
RE_CODE_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]{3,}(?:_[A-Z0-9]+)+)\b")
#: ★ 占位 code（注释/文档里的示例，不是真 code）——实测 `LAYOUT_XXX`/`AUDIT_XXX` 被当成了真 code 误报。
RE_CODE_PLACEHOLDER = re.compile(r"_X{2,}$|_XXX?$|^X+$")


def _skill_root() -> Path:
    """skill 根目录 = 本文件的上一级的上一级。"""
    return Path(__file__).resolve().parents[1]


def check_self_docs(root: Optional[Path] = None) -> List[Finding]:
    """守卫 skill 自己的文档：悬空引用 / 索引漏项 / 条款号对不上 / code 未登记。"""
    root = root or _skill_root()
    out: List[Finding] = []

    skill_md = root / "SKILL.md"
    doc_files = [p for p in [skill_md] + sorted((root / "standards").glob("*.md")) if p.is_file()]

    # ① 悬空引用：文档里提到的内部路径必须存在
    for p in doc_files:
        text = p.read_text(encoding="utf-8")
        for m in RE_DOC_REF.finditer(text):
            rel = m.group(1)
            stem = Path(rel).stem
            if PLACEHOLDER_RE.search(stem):
                continue  # 散文里的举例（`xxx.md` / `<名>.md`），不是真引用
            if not (root / rel).exists():
                out.append(
                    Finding(
                        "SELF_DANGLING_REF",
                        "error",
                        f"文档引用了不存在的文件：{rel}",
                        loc=f"{p.relative_to(root)}",
                        fix="建这个文件，或改掉引用（别留悬空引用——S-07 §2.3 同理）",
                    )
                )

    # ② 索引漏项：standards/ 下的条款必须在 SKILL.md 里被索引到
    if skill_md.is_file():
        idx_text = skill_md.read_text(encoding="utf-8")
        for p in sorted((root / "standards").glob("*.md")):
            if p.name not in idx_text:
                out.append(
                    Finding(
                        "SELF_INDEX_MISSING",
                        "error",
                        f"{p.name} 存在，但 SKILL.md 的索引里没有它",
                        loc="SKILL.md",
                        fix="加进 §2 索引表，否则使用者查不到这条规范",
                    )
                )

    # ③ 条款号悬空：`见 S-0X §N` 里的小节号必须在目标文件里真实存在
    clause_of: Dict[str, set] = {}
    for p in sorted((root / "standards").glob("S-*.md")):
        num = p.name[2:4]
        body = p.read_text(encoding="utf-8")
        clause_of[num] = set(RE_CLAUSE_DEF_BOLD.findall(body)) | set(RE_CLAUSE_DEF_HEAD.findall(body))
    for p in doc_files:
        for m in RE_CLAUSE_REF.finditer(p.read_text(encoding="utf-8")):
            num, clause = m.group(1), m.group(2)
            if num not in clause_of:
                out.append(
                    Finding(
                        "SELF_CLAUSE_UNKNOWN",
                        "error",
                        f"引用了不存在的条款文件 S-{num}",
                        loc=p.relative_to(root).as_posix(),
                        fix="改成真实存在的条款编号",
                    )
                )
            elif clause not in clause_of[num]:
                out.append(
                    Finding(
                        "SELF_CLAUSE_UNKNOWN",
                        "error",
                        f"引用了 S-{num} 里不存在的 §{clause}",
                        loc=p.relative_to(root).as_posix(),
                        fix=f"S-{num} 现有条款：{'、'.join(sorted(clause_of[num])) or '（无）'}",
                    )
                )

    # ④ code 未登记：检查器会报的 code 必须有人解释它。
    #    ★ 按前缀分工（实测踩过）：`SELF_*` 是**自检自己的 code**，归 `SKILL.md` 管；
    #      `LAYOUT_*` / `AUDIT_*` 是**研究流程的 code**，归 `standards/` 条款管。
    src = Path(__file__).read_text(encoding="utf-8")
    emitted = {
        c
        for c in RE_CODE_TOKEN.findall(src)
        if c.startswith(("LAYOUT_", "AUDIT_", "SELF_")) and not RE_CODE_PLACEHOLDER.search(c)
    }
    emitted.discard("SELF_OK")
    in_standards: set = set()
    for p in (root / "standards").glob("*.md"):
        in_standards |= set(RE_CODE_TOKEN.findall(p.read_text(encoding="utf-8")))
    in_skill = set(RE_CODE_TOKEN.findall(skill_md.read_text(encoding="utf-8"))) if skill_md.is_file() else set()

    for code in sorted(emitted):
        if code.startswith("SELF_"):
            if code not in in_skill:
                out.append(
                    Finding(
                        "SELF_CODE_UNDOCUMENTED",
                        "error",
                        f"自检会报 {code}，但 SKILL.md 里没有它",
                        loc="SKILL.md",
                        fix="在 SKILL.md 里写明这个自检 code 的含义与处置（自检 code 归 SKILL.md 管）",
                    )
                )
        elif code not in in_standards:
            out.append(
                Finding(
                    "SELF_CODE_UNDOCUMENTED",
                    "error",
                    f"检查器会报 {code}，但没有任何条款解释它",
                    loc="standards/",
                    fix="在对应条款的 code 表里登记它（附「怎么改」）",
                )
            )
    return out


def cmd_selfcheck(args: argparse.Namespace) -> int:
    res = LayoutResult(root=str(args.dir or _skill_root()))
    res.findings = check_self_docs(Path(args.dir).resolve() if args.dir else None)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"# lit selfcheck —— skill 自身文档（{res.root}）")
        if not res.findings:
            print("✓ 文档自检通过（无悬空引用 / 索引完整 / 条款号可解析 / code 均已登记）")
        for f in res.findings:
            mark = "X" if f.severity == "error" else "!"
            print(f"{mark} [{f.code}] {f.message}")
            if f.fix:
                print(f"    怎么改：{f.fix}")
        print(f"\n共 {len(res.errors)} 个错误 / {len(res.warns)} 个警告")
    return 1 if res.errors else 0


# ────────────────────────────────────────────────────────── 流程检查（audit）


@dataclass
class AuditResult:
    root: str
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "counts": {"error": len(self.errors), "warn": len(self.warns)},
            "findings": [f.as_dict() for f in self.findings],
        }


def _capacity_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    cap = config.get("capacity")
    return cap if isinstance(cap, dict) else {}


def check_capacity(root: Path, config: Dict[str, Any], refs: List[Dict[str, Any]]) -> List[Finding]:
    """S-02：著录篇数与比例必须达标（硬线在 `lit.config.json`，不写死在代码里）。"""
    out: List[Finding] = []
    cap = _capacity_cfg(config)
    src = str(cap.get("source") or "").strip()
    src_hint = f"（出处：{src}）" if src else "（未写 capacity.source——建议记下这个数从哪来，S-02 §1.3）"
    need = cap.get("refs_min")
    if isinstance(need, int) and len(refs) < need:
        out.append(
            Finding(
                "AUDIT_CAPACITY_SHORT",
                "error",
                f"著录 {len(refs)} 条 < 你承诺的 {need} 条{src_hint}",
                loc="02_候选库/refs.json",
                fix=f"补到 ≥{need} 条，或**显式把承诺改小并写明理由**（S-02）——"
                "检查的是「承诺 vs 兑现」，不是「你违抗了谁」",
            )
        )
    # 近五年 / 外文比例
    def year_of(r: Dict[str, Any]) -> Optional[int]:
        m = re.search(r"(19|20)\d{2}", str(r.get("year") or r.get("pubDate") or ""))
        return int(m.group(0)) if m else None

    recent_window = int(cap.get("recent_years") or 5)
    now_year = int(cap.get("now_year") or 2026)
    if refs and cap.get("recent_ratio_min"):
        recent = [r for r in refs if (year_of(r) or 0) >= now_year - recent_window + 1]
        ratio = len(recent) / len(refs)
        if ratio < float(cap["recent_ratio_min"]):
            out.append(
                Finding(
                    "AUDIT_RATIO_RECENT",
                    "error",
                    f"近 {recent_window} 年（{now_year - recent_window + 1}–{now_year}）只占 {ratio:.0%}，"
                    f"低于 {float(cap['recent_ratio_min']):.0%}",
                    loc="02_候选库/refs.json",
                    fix="补检索近几年文献；口径以 lit.config.json 的 capacity 为准（S-02）",
                )
            )
    if refs and cap.get("foreign_ratio_min"):
        foreign = [r for r in refs if str(r.get("lang") or "").lower() in ("en", "eng", "foreign")]
        ratio = len(foreign) / len(refs)
        if ratio < float(cap["foreign_ratio_min"]):
            out.append(
                Finding(
                    "AUDIT_RATIO_FOREIGN",
                    "error",
                    f"外文只占 {ratio:.0%}，低于 {float(cap['foreign_ratio_min']):.0%}",
                    loc="02_候选库/refs.json",
                    fix="补外文来源；`lang` 字段缺失也会算成非外文（S-02）",
                )
            )
    return out


def check_tier_consistency(root: Path, refs: List[Dict[str, Any]]) -> List[Finding]:
    """S-01：`full_text` 必须真有抽出文字的全文；`abstract_only` 不许进精读笔记。"""
    out: List[Finding] = []
    for r in refs:
        rid = str(r.get("id") or "")
        tier = str(r.get("tier") or "")
        if tier == "full_text":
            ok = any((root / "03_全文" / sub / f"{rid.replace(':', '_')}{ext}").is_file()
                     for sub, ext in (("text", ".txt"), ("pdf", ".pdf")))
            if not ok:
                out.append(
                    Finding(
                        "AUDIT_TIER_UNBACKED",
                        "error",
                        f"{rid} 声明 full_text，但 03_全文/ 下找不到它的 pdf/text",
                        loc="02_候选库/refs.json",
                        fix="补全文，或把档位降为 abstract_only（S-01）",
                    )
                )
        if tier == "abstract_only":
            note = root / "04_精读" / f"{rid.replace(':', '_')}.md"
            if note.is_file():
                out.append(
                    Finding(
                        "AUDIT_ABSTRACT_ONLY_DEEP_READ",
                        "error",
                        f"{rid} 是 abstract_only，却出现在 04_精读/ 里",
                        loc=str(note.relative_to(root)),
                        fix="摘要是用来**筛**的，不能当读过全文（S-01 / 红线 1）",
                    )
                )
    return out


def check_notes_have_sources(root: Path, refs: List[Dict[str, Any]]) -> List[Finding]:
    """红线 2：精读笔记里的**数字必须带页码/表号**。"""
    out: List[Finding] = []
    tier_of = {str(r.get("id")): str(r.get("tier")) for r in refs}
    for p in sorted((root / "04_精读").glob("*.md")) if (root / "04_精读").is_dir() else []:
        rid = p.stem.replace("_", ":", 1)
        tier = tier_of.get(rid, "")
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            # ★ 实测修正：**引用块（`>` 开头）算"论述"不算"断言"**——
            #   实测在真实精读笔记里，"矛盾并列"一节的引言行复述了数字但没带页码，
            #   报出来是**按字面对但无实用价值**的噪音（该页上方就有带页码的表格）。
            if line.lstrip().startswith(">"):
                continue
            # 含小数或百分号 = 疑似数值；同一行必须有页码/表号/节号标记
            if not re.search(r"\d+\.\d+|\d+\s*%", line):
                continue
            if re.search(r"p\.?\s*\d|第\s*\d+\s*页|表\s*[IVX0-9]|Table\s*[IVX0-9]|§|节\s*\d", line):
                continue
            out.append(
                Finding(
                    "AUDIT_NUMBER_WITHOUT_SOURCE",
                    "warn",
                    f"疑似数字没有页码/表号来源：{line.strip()[:48]}",
                    loc=f"{p.relative_to(root)}:{i}",
                    fix="每个数字都要能指回原文的页码/表号（红线 2）"
                    + (f"；该篇档位是 {tier}" if tier else ""),
                )
            )
    return out


def check_comparability_declaration(root: Path) -> List[Finding]:
    """S-03：有指标对比表就必须有"不可直接比较"的显式声明。"""
    out: List[Finding] = []
    for name in ("指标对比表.md", "数据集对比表.md"):
        p = root / "05_聚合" / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if not re.search(r"不可(直接)?(比较|横比)|不能(直接)?(比较|横比)|可比性", text):
            out.append(
                Finding(
                    "AUDIT_NO_COMPARABILITY",
                    "error",
                    f"{name} 里没有任何「不可直接比较」的声明",
                    loc=str(p.relative_to(root)),
                    fix="按 S-03 显式声明三条红线（同库不同子集 / 不同数据集 / 跨任务）",
                )
            )
    return out


def check_ref_fields(refs: List[Dict[str, Any]]) -> List[Finding]:
    """S-05：题录**必填字段**齐全（`id`/`title`/`authors`/`year`；期刊会议还要 `venue`）。

    ★ **只对"已抓过详情"的记录报**：`needs_detail: true` 的是**流程中态**
    （还没抓详情），报成 N 条同类错误会**淹没真问题**——实测在真实小课题上
    45 条里 25 条属这一类，audit 直接变成噪音。它们由 `check_needs_detail` **聚合报一条**。
    """
    out: List[Finding] = []
    for r in refs:
        if r.get("needs_detail") or r.get("excluded"):
            continue
        rid = str(r.get("id") or "?")
        missing: List[str] = []
        if not r.get("title"):
            missing.append("title")
        if not (r.get("authors") or r.get("author")):
            missing.append("authors")
        if not (r.get("year") or r.get("pubDate")):
            missing.append("year")
        typ = str(r.get("type") or "")
        if any(k in typ for k in ("期刊", "会议", "Journal", "Conference")) and not r.get("venue"):
            missing.append("venue")
        if missing:
            out.append(
                Finding(
                    "AUDIT_REF_FIELD_MISSING",
                    "error",
                    f"{rid} 缺字段：{'、'.join(missing)}",
                    loc="02_候选库/refs.json",
                    fix="三条对策（S-05 §1.3）：去别的来源找同篇 → 从 PDF 取 → **取不到就不用它**；"
                    "禁止用占位糊过去",
                )
            )
    return out


def doi_looks_self_consistent(rid: str, doi: str) -> bool:
    """DOI 是否**自洽**：DOI 字符串里含该记录**自己的来源 ID** → 可信。

    ★ 实测修正：初版规则是"学位论文带 DOI 就报可疑"——**太粗**。
    真实数据里 `WF:D03227905` 的 DOI 是 `10.7666/D03227905`：`10.7666` 是**该来源自有的前缀**，
    且 **DOI 里就含它自己的 ID** → **这个 DOI 是对的**，初版会把它误判。
    而 `10.1016/j.measurement...` 挂在学位论文上则明显是**参考文献里某篇的**。

    → 规则：**含自身 ID ⇒ 可信；不含 ⇒ 可疑**（可机械判定，无需联网核库）。
    """
    sid = str(rid).split(":")[-1].strip().lower()
    return bool(sid) and sid in str(doi).lower()


def check_suspect_doi(refs: List[Dict[str, Any]]) -> List[Finding]:
    """S-01 `bad_source`：**来源给的字段本身不可靠**时要标出来。

    实测：某来源的**学位论文**详情页把**参考文献里第一篇的 DOI** 当成本文 DOI
    （Elsevier 的 `10.1016/...` 挂在一篇中国学位论文上）。**错误归属比缺字段更坏。**
    判据见 `doi_looks_self_consistent`。
    """
    out: List[Finding] = []
    for r in refs:
        if r.get("excluded"):
            continue
        typ = str(r.get("type") or "")
        doi = str(r.get("doi") or "").strip()
        if not doi:
            continue
        is_thesis = "学位" in typ or "硕士" in typ or "博士" in typ
        if is_thesis and not doi_looks_self_consistent(str(r.get("id") or ""), doi):
            out.append(
                Finding(
                    "AUDIT_SUSPECT_DOI",
                    "warn",
                    f"{r.get('id')} 是学位论文，但 DOI（{doi[:40]}…）里**不含它自己的 ID**"
                    "——疑似抓到了参考文献里某篇的 DOI",
                    loc="02_候选库/refs.json",
                    fix="人工核实；核实不了就让 `lit refs` 省略它（它已自动省略不可信的学位论文 DOI）。"
                    "注：DOI 里含自身 ID 的（如 `10.7666/D03227905`）是**可信**的，不报",
                )
            )
    return out


def check_excluded(refs: List[Dict[str, Any]]) -> List[Finding]:
    """把标了 `excluded: true` 的记录**聚合报一条**（S-05 §1.3 第 ③ 条"取不到就不用它"）。

    ★ 实测补口：没有这个机制时，"字段取不到"的记录会**反复报错且无法消除**——
    `ingest` 每次都从 raw/ 重新加回来，改 `refs.json` 也没用。
    排除标记是**人工裁决**，`ingest` 必须尊重它（同 `tier_source: manual`）。
    """
    ex = [r for r in refs if r.get("excluded")]
    if not ex:
        return []
    return [
        Finding(
            "AUDIT_EXCLUDED",
            "warn",
            f"已排除 {len(ex)} 条（{ '、'.join(str(r.get('id')) for r in ex[:6]) }"
            f"{'…' if len(ex) > 6 else ''}）——按 S-05 §1.3 第③条弃用，不进著录",
            loc="02_候选库/refs.json",
            fix="确保 `02_候选库/排除清单.md` 里记了**为什么弃用**（S-04 §5.2）",
        )
    ]


def check_needs_detail(refs: List[Dict[str, Any]]) -> List[Finding]:
    """把"只有检索 snippet、还没抓详情"的记录**聚合成一条**（而不是 N 条）。

    实测教训：真实小课题里 45 条有 25 条属这一类，逐条报会让 `audit` 变成噪音，
    使用者会**直接忽略整份报告**——那比漏报更坏。
    """
    need = [r for r in refs if r.get("needs_detail")]
    if not need:
        return []
    by_src: Dict[str, int] = {}
    for r in need:
        by_src[str(r.get("source") or "?")] = by_src.get(str(r.get("source") or "?"), 0) + 1
    desc = "、".join(f"{k} {v} 条" for k, v in sorted(by_src.items()))
    return [
        Finding(
            "AUDIT_NEEDS_DETAIL",
            "warn",
            f"{len(need)} 条只有检索 snippet、**还没抓详情**（{desc}）——"
            "这不是缺陷，是流程中态；抓完详情后作者/年份/摘要才会齐",
            loc="02_候选库/refs.json",
            fix="对这些 ID 跑一次 `ieee_detail.py` / `wf_detail.py`，再 `lit ingest` 重跑；"
            "或把它们从候选库剔除（**别拿 snippet 支撑论断**——S-01）",
        )
    ]


def check_audit(root: Path) -> AuditResult:
    """跑全部 audit 检查（只读）。"""
    res = AuditResult(root=str(root))
    if not root.is_dir():
        res.findings.append(Finding("AUDIT_CAPACITY_SHORT", "error", f"目录不存在：{root}"))
        return res
    config = load_config(root)
    refs, err = load_refs(root)
    # ★ **前置存在性检查**——实测踩过：没有 config / 没有 refs 时，后面所有检查都被
    #   `if refs` 之类的条件跳过，于是**什么都没查却报"合规通过"**（恒真检查器）。
    #   宁可报"查不了"，也不许报"通过"。
    if not (root / "lit.config.json").is_file():
        res.findings.append(
            Finding(
                "AUDIT_NO_CONFIG",
                "error",
                "缺 lit.config.json —— 容量/比例/档位白名单都在它里面，没有它无法判定合规",
                loc="lit.config.json",
                fix="按 S-02 §1.1 建 config（sources / tiers / capacity）",
            )
        )
    if not (root / "02_候选库" / "refs.json").is_file():
        res.findings.append(
            Finding(
                "AUDIT_NO_REFS",
                "error",
                "缺 02_候选库/refs.json —— 没有题录库，档位与容量都无从检查",
                loc="02_候选库/refs.json",
                fix="先跑检索并落盘，再把记录汇成 refs.json（每篇带 tier，见 S-01）",
            )
        )
        return res  # 没有 refs，后续检查全部无意义——**返回错误，不返回"通过"**
    if err:
        res.findings.append(Finding("AUDIT_TIER_UNBACKED", "error", err, loc="02_候选库/refs.json"))
    res.findings += check_excluded(refs)
    res.findings += check_suspect_doi(refs)
    res.findings += check_needs_detail(refs)
    res.findings += check_ref_fields(refs)
    res.findings += check_capacity(root, config, refs)
    res.findings += check_tier_consistency(root, refs)
    res.findings += check_notes_have_sources(root, refs)
    res.findings += check_comparability_declaration(root)
    return res


def cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    res = check_audit(root)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"# lit audit —— {res.root}")
        if not res.findings:
            print("✓ 合规检查通过（档位 / 容量 / 数字出处 / 可比性声明）")
        for f in res.findings:
            mark = "X" if f.severity == "error" else "!"
            print(f"{mark} [{f.code}] {f.message}")
            if f.loc:
                print(f"    位置：{f.loc}")
            if f.fix:
                print(f"    怎么改：{f.fix}")
        print(f"\n共 {len(res.errors)} 个错误 / {len(res.warns)} 个警告")
        print("※ 审计全过 ≠ 可交：它只覆盖上面列出的检查项。")
    return 1 if res.errors else 0


# ─────────────────────────────────────────────────── 汇总（ingest）
#
# 把 `01_检索/<来源>/raw/` 下的落盘 JSON 汇成 `02_候选库/refs.json`。
# 设计要点（每条都对应一个实测教训）：
#   1. **只读原始件**：raw/ 是只读证据区（S-07 §3.1），ingest 不改它；
#   2. **档位只往下判，不往上判**：有全文才给 full_text；只有摘要就给 abstract_only。
#      **不许因为"看起来重要"就升档**；
#   3. **不覆盖人工档位**：已有 `tier_source: manual` 的记录保留原档（人的判断优先）；
#   4. **跨源去重**：DOI 相同、或题名归一后相同 → 视为同一工作（实战里没做，是缺口）。

RE_WF_ID = re.compile(r"/(?:thesis|periodical|conference|patent)/([A-Za-z0-9]+)")


def _norm_title(t: str) -> str:
    """题名归一：去空白、去标点、转小写——用于跨源去重。"""
    return re.sub(r"[\s\W_]+", "", str(t or "")).lower()


def _iter_raw_files(root: Path, sources: Sequence[str]) -> Iterable[Tuple[str, Path]]:
    for src in sources:
        d = root / "01_检索" / src / "raw"
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                yield src, p


def _first_year(*vals: Any) -> Optional[int]:
    """从若干候选字符串里取**第一个四位年份**，统一返回 `int`。

    ★ 实测踩过：IEEE 那边曾返回**字符串**年份、万方返回 `int`，同一字段两种类型，
      下游比较/排序会悄悄出错。**同一字段必须同一类型**。
    """
    for v in vals:
        m = re.search(r"(?:19|20)\d{2}", str(v or ""))
        if m:
            return int(m.group(0))
    return None


#: 从 `publishedIn` 判断载体类型的关键词（IEEE 同时收录期刊与会议）
CONF_HINTS = ("conference", "symposium", "workshop", "proceedings", "congress", "meeting")


def guess_type_from_venue(venue: Any) -> str:
    """★ 实测修正：初版把**所有** IEEE 记录硬编码成"期刊论文"，
    结果会议论文在著录里被标成 `[J]`（实测样本里第 6、7、9、10 条都是会议）。
    这里按 `publishedIn` 里的关键词区分。
    """
    v = str(venue or "").lower()
    return "会议论文" if any(k in v for k in CONF_HINTS) else "期刊论文"


def _rec_from_ieee_detail(x: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    arn = x.get("arnumber")
    if not arn:
        return None
    authors = x.get("authors")
    if isinstance(authors, str):
        authors = [a for a in re.split(r"[;,]", authors) if a.strip()]
    venue = x.get("publishedIn") or ""
    return {
        "id": f"IEEE:{arn}",
        "source": "ieee",
        "type": guess_type_from_venue(venue),
        "title": x.get("title") or "",
        "authors": authors or [],
        "year": _first_year(x.get("pubDate")),
        "venue": venue,
        "doi": x.get("doi") or "",
        "url": f"https://ieeexplore.ieee.org/document/{arn}",
        "abstract": x.get("abstract") or "",
        "keywords": x.get("keywords") or "",
        "lang": "en",
    }


def _rec_from_wf_detail(x: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = str(x.get("url") or "")
    m = RE_WF_ID.search(url)
    if not m:
        return None
    typ = str(x.get("type") or "")
    authors = x.get("authors")
    if isinstance(authors, str):
        authors = [a for a in re.split(r"[;,，、]", authors) if a.strip()]
    year = x.get("degreeYear") or x.get("pubDate") or x.get("conferenceDate") or ""
    return {
        "id": f"WF:{m.group(1)}",
        "source": "wanfang",
        "type": re.sub(r"[\[\]]", "", typ) or "期刊论文",
        "title": x.get("title") or "",
        "authors": authors or [],
        "year": _first_year(year),
        "venue": x.get("institution") or "",
        "doi": x.get("doi") or "",
        "url": url,
        "abstract": x.get("abstract") or "",
        "keywords": x.get("keywords") or [],
        "institution": x.get("institution") or "",
        "advisor": x.get("advisor") or "",
        "chapters": x.get("chapters") or [],
        "lang": "zh",
    }


def _recs_from_search(x: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
    """检索结果只有 snippet——**只能进"待筛"**，字段残缺。

    ★ 实测（真实小课题）暴露两处，这里一并修：
      1. 这类记录**必须标 `needs_detail: true`**——它们是"**还没抓详情**"的**流程中态**，
         不是"字段缺失的缺陷"。不标就会让 `audit` 报出 25 条同类错误，**噪音淹没真问题**；
      2. 这类记录**必须带 `lang`**——`lang` 由**来源**就能确定（ieee→en / wanfang→zh），
         与是否抓过详情无关。不写会让外文比例被严重低估，**假报 `AUDIT_RATIO_FOREIGN`**。
    """
    out: List[Dict[str, Any]] = []
    items = x.get("items") or []
    for it in items:
        if not isinstance(it, dict):
            continue
        if source == "ieee":
            arn = it.get("arnumber")
            if not arn:
                continue
            out.append(
                {
                    "id": f"IEEE:{arn}",
                    "source": "ieee",
                    "title": it.get("title") or "",
                    "snippet": it.get("snippet") or "",
                    "url": it.get("url") or "",
                    "lang": "en",
                    "from_search": True,
                    "needs_detail": True,
                }
            )
        else:
            url = str(it.get("url") or "")
            m = RE_WF_ID.search(url)
            if not m:
                continue
            out.append(
                {
                    "id": f"WF:{m.group(1)}",
                    "source": "wanfang",
                    "title": it.get("title") or "",
                    "snippet": it.get("snippet") or "",
                    "type": re.sub(r"[\[\]]", "", str(it.get("type") or "")),
                    "url": url,
                    "lang": "zh",
                    "from_search": True,
                    "needs_detail": True,
                }
            )
    return out


def collect_raw(root: Path, sources: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """扫 raw/ 收集记录。**detail 优先于 search**（字段更全）。返回 (id→记录, 警告)。"""
    by_id: Dict[str, Dict[str, Any]] = {}
    warns: List[str] = []
    for src, path in _iter_raw_files(root, sources):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            warns.append(f"{path.name} 解析失败：{exc}")
            continue
        results = obj.get("results") if isinstance(obj, dict) else None
        if not isinstance(results, list):
            continue
        is_detail = "_detail" in path.name
        for x in results:
            if not isinstance(x, dict):
                continue
            if is_detail:
                rec = _rec_from_ieee_detail(x) if src == "ieee" else _rec_from_wf_detail(x)
                if rec:
                    rec["detail_seen"] = True
                    merged = {**by_id.get(rec["id"], {}), **rec}
                    # ★ **抓到详情后必须清掉 `needs_detail`**——实测踩过：
                    #   详情记录本身没有这个键，合并时它会从**检索记录**里留下来，
                    #   于是"抓了详情也永远显示待详情"，`audit` 的警告永不消失。
                    merged.pop("needs_detail", None)
                    merged.pop("snippet", None)
                    by_id[rec["id"]] = merged
            else:
                for rec in _recs_from_search(x, src):
                    # **detail 已见过的不被 search 覆盖**
                    if by_id.get(rec["id"], {}).get("detail_seen"):
                        continue
                    by_id.setdefault(rec["id"], {}).update(
                        {k: v for k, v in rec.items() if k not in by_id[rec["id"]]}
                    )
    return by_id, warns


def infer_tier(root: Path, rec: Dict[str, Any]) -> Tuple[str, str]:
    """**只往下判，不往上判**：返回 (tier, 依据说明)。"""
    rid = str(rec.get("id"))
    stem = rid.replace(":", "_")
    txt = root / "03_全文" / "text" / f"{stem}.txt"
    pdf = root / "03_全文" / "pdf" / f"{stem}.pdf"
    if txt.is_file() and txt.stat().st_size > 0:
        return "full_text", "03_全文/text 下有抽出文字的全文"
    if pdf.is_file():
        return "unreadable", "有 PDF 但 03_全文/text 下没有抽出文字（先抽文字再升级）"
    if str(rec.get("abstract") or "").strip():
        return "abstract_only", "只有摘要（detail 返回的 abstract）"
    if str(rec.get("snippet") or "").strip():
        return "abstract_only", "只有检索 snippet——**待筛**，不足以支撑论断"
    return "abstract_only", "无摘要无全文——仅凭题录，**必须人工核**"


def dedupe(recs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """跨源去重：DOI 相同 或 题名归一后相同 → 同一工作。保留档位更高的一条。"""
    rank = {"full_text": 0, "source_conflict": 1, "unreadable": 2,
            "insufficient_evidence": 3, "bad_source": 4, "abstract_only": 5}
    by_doi: Dict[str, Dict[str, Any]] = {}
    by_title: Dict[str, Dict[str, Any]] = {}
    dups: List[Dict[str, str]] = []
    kept: List[Dict[str, Any]] = []
    for r in recs:
        doi = str(r.get("doi") or "").strip().lower()
        ti = _norm_title(r.get("title"))
        other = by_doi.get(doi) if doi else None
        if other is None and ti:
            other = by_title.get(ti)
        if other is not None and other is not r:
            keep, drop = (r, other) if rank.get(str(r.get("tier")), 9) < rank.get(
                str(other.get("tier")), 9
            ) else (other, r)
            dups.append({"kept": str(keep.get("id")), "dropped": str(drop.get("id")),
                         "why": "DOI 相同" if doi and by_doi.get(doi) is other else "题名归一后相同"})
            if drop in kept:
                kept.remove(drop)
            if keep not in kept:
                kept.append(keep)
            if doi:
                by_doi[doi] = keep
            if ti:
                by_title[ti] = keep
            continue
        kept.append(r)
        if doi:
            by_doi[doi] = r
        if ti:
            by_title[ti] = r
    return kept, dups


def ingest_course(root: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """把 raw/ 汇成 `02_候选库/refs.json`。**幂等**：人工档位不被覆盖。"""
    config = load_config(root)
    sources = sources_of(config)
    by_id, warns = collect_raw(root, sources)
    refs_path = root / "02_候选库" / "refs.json"
    existing: Dict[str, Dict[str, Any]] = {}
    if refs_path.is_file():
        old, _ = load_refs(root)
        existing = {str(r.get("id")): r for r in old if r.get("id")}

    out: List[Dict[str, Any]] = []
    for rid, rec in sorted(by_id.items()):
        rec = dict(rec)
        rec.pop("from_search", None)
        prev = existing.get(rid)
        if prev and prev.get("excluded"):
            # ★ **排除是人工裁决**：`ingest` 必须尊重，否则坏记录每次重跑都会被加回来
            rec["excluded"] = True
            rec["excluded_reason"] = prev.get("excluded_reason") or ""
        if prev and prev.get("tier_source") == "manual":
            rec["tier"] = prev.get("tier")  # **人的判断优先**
            rec["tier_source"] = "manual"
            rec["tier_note"] = prev.get("tier_note") or ""
        elif prev and prev.get("tier_source") == "manual":
            pass
        else:
            tier, why = infer_tier(root, rec)
            rec["tier"] = tier
            rec["tier_source"] = "ingest"
            rec["tier_note"] = why
        rec.setdefault("authors", [])
        out.append(rec)

    out, dups = dedupe(out)
    result = {
        "root": str(root),
        "sources": list(sources),
        "collected": len(by_id),
        "kept": len(out),
        "duplicates": dups,
        "warnings": warns,
        "by_tier": {},
        "written": False,
    }
    for r in out:
        result["by_tier"][str(r.get("tier"))] = result["by_tier"].get(str(r.get("tier")), 0) + 1
    if not dry_run:
        (root / "02_候选库").mkdir(parents=True, exist_ok=True)
        refs_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        result["written"] = True
    return result


def cmd_ingest(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    res = ingest_course(root, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"# lit ingest —— {res['root']}")
        print(f"  来源：{'、'.join(res['sources'])}")
        print(f"  从 raw/ 收集 {res['collected']} 条 → 去重后 {res['kept']} 条")
        if res["by_tier"]:
            for t, n in sorted(res["by_tier"].items()):
                print(f"    {t}: {n}")
        if res["duplicates"]:
            print(f"  跨源重复 {len(res['duplicates'])} 组：")
            for d in res["duplicates"][:8]:
                print(f"    保留 {d['kept']}，丢弃 {d['dropped']}（{d['why']}）")
        for w in res["warnings"]:
            print(f"  ! {w}")
        print("  已写入 02_候选库/refs.json" if res["written"] else "  （--dry-run，未写入）")
        print("  提示：档位是**推断**的（只往下判）；人工改过的 tier 请置 tier_source=\"manual\"，重跑不会被覆盖。")
    return 0


# ─────────────────────────────────────────── 容量规划（plan）

def plan_course(
    root: Path,
    *,
    refs_min: Optional[int] = None,
    now_year: Optional[int] = None,
    recent_years: int = 5,
    recent_ratio: Optional[float] = None,
    foreign_ratio: Optional[float] = None,
    sources: Optional[Sequence[str]] = None,
    capacity_source: Optional[str] = None,
    refs_style: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """生成/更新课题目录的 `lit.config.json`（容量与比例部分）。

    ★ **刻意不内置任何具体数字**：S-02 §1.2 明文要求"规范条文里不写具体数字"，
    因为**每个产出的约定来源各不相同**（学位论文规范 / 期刊投稿指南 / 基金要求 /
    或者——**没有外部规范、由你自己承诺**）。所以**硬线必须由使用者显式给出**，
    不给就报错，**不许猜**：猜一个数比报错更坏，报错会让人去查，猜数会让人直接用。

    `capacity_source`（可选）：记下**这个数是哪来的**（外部规范名 / 期刊名 / "自定义"）。
    写进去之后，`audit` 报的是「你承诺 40 条、实际 31 条」，而不是"你违抗了谁的规定"。
    """
    root = Path(root)
    problems: List[str] = []
    if refs_min is None:
        problems.append(
            "缺 `--refs-min`：参考文献条数**必须由你显式给出**，本工具不替你猜（S-02 §2.2）。"
            "它来自哪里由你决定 —— 外部规范（学位论文规范 / 期刊投稿指南 / 基金要求），"
            "**或者没有外部规范时由你自己承诺一个数**。建议同时用 `--source` 把出处记下来。"
        )
    if now_year is None:
        problems.append(
            "缺 `--now-year`：\"近 N 年\"的窗口必须**显式写死**，不许用系统时间（S-02 §3.1），"
            "否则规范不可复现。例：--now-year 2026。"
        )
    if problems:
        return {"root": str(root), "ok": False, "problems": problems, "written": False}

    config = load_config(root)
    config.setdefault("sources", list(sources) if sources else ["ieee", "wanfang"])
    config.setdefault("tiers", list(TIER_FALLBACK))
    cap = config.setdefault("capacity", {})
    cap["refs_min"] = int(refs_min)
    cap["now_year"] = int(now_year)
    cap["recent_years"] = int(recent_years)
    if capacity_source is not None:
        cap["source"] = str(capacity_source)
    if refs_style is not None:
        config["refs_style"] = str(refs_style)
    if recent_ratio is not None:
        cap["recent_ratio_min"] = float(recent_ratio)
    if foreign_ratio is not None:
        cap["foreign_ratio_min"] = float(foreign_ratio)
    if sources:
        config["sources"] = list(sources)

    written = False
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        (root / "lit.config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written = True
    missing_ratio = [k for k in ("recent_ratio_min", "foreign_ratio_min") if k not in cap]
    return {
        "root": str(root),
        "ok": True,
        "config": config,
        "problems": [],
        "missing_ratio": missing_ratio,
        "written": written,
        "not_checked": [
            "\"近两年\"是部分规范的独立要求，本工具**未做自动检查**（S-02 §4）",
            "引用是否**真正相关**属语义判断，检查器做不到（S-02 §4）",
        ],
    }


def cmd_plan(args: argparse.Namespace) -> int:
    res = plan_course(
        Path(args.dir),
        refs_min=args.refs_min,
        now_year=args.now_year,
        recent_years=args.recent_years,
        recent_ratio=args.recent_ratio,
        foreign_ratio=args.foreign_ratio,
        sources=[s for s in (args.sources or "").split(",") if s] or None,
        capacity_source=args.source,
        refs_style=args.refs_style,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"# lit plan —— {res['root']}")
        if not res["ok"]:
            print("X 不能生成：")
            for p in res["problems"]:
                print(f"    {p}")
            return 2
        for k, v in res["config"]["capacity"].items():
            print(f"    capacity.{k} = {v}")
        src = res["config"]["capacity"].get("source")
        print(f"    容量出处：「{src}」" if src else
              "  ! 没写 `--source`：**建议记下这个数的出处**（外部规范名 / 「自定义」），"
              "否则以后没人知道为什么是 40 条（S-02 §1.3）")
        if res["config"].get("refs_style"):
            print(f"    refs_style = {res['config']['refs_style']}")
        if res["missing_ratio"]:
            print(f"  ! 还没设比例的阈值：{'、'.join(res['missing_ratio'])}（S-02 §3）")
        print("  已写入 lit.config.json" if res["written"] else "  （--dry-run，未写入）")
        for n in res["not_checked"]:
            print(f"  ※ 未检查：{n}")
    return 0 if res["ok"] else 2


# ────────────────────────────────────── PDF 入库（link）
#
# 上游抓取工具**按论文题名命名**下载的文件，而 S-07 §3.2 要求 `03_全文/pdf/<ID>.pdf`。
# ★ 实测教训：这一步我曾**手工用题名模糊匹配**完成，结果**配错了一篇**，并在修补时
#   **把那篇 PDF 删掉了**。根因不是"我粗心"，是**规范没给自动化手段**，逼人做最容易错的一步。
#
# 正确做法：**下载日志里有权威映射**（`<来源>_paper_download-*.json` 记录了
# `arnumber`/`url` ↔ `download.path`），根本不需要匹配题名。
# **硬约束：只用权威映射，绝不回退到题名匹配**——匹配不上就报错，不许猜。


def download_index(root: Path, sources: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """扫 `*download*.json` 日志，建 `ID → 落盘路径` 的**权威映射**。"""
    idx: Dict[str, str] = {}
    warns: List[str] = []
    for src, path in _iter_raw_files(root, sources):
        if "download" not in path.name:
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            warns.append(f"{path.name} 解析失败：{exc}")
            continue
        for r in obj.get("results") or []:
            if not isinstance(r, dict):
                continue
            d = r.get("download") or {}
            p = d.get("path") or d.get("name")
            if not p:
                continue
            rid: Optional[str] = None
            if r.get("arnumber"):
                rid = f"IEEE:{r['arnumber']}"
            elif r.get("url"):
                m = RE_WF_ID.search(str(r["url"]))
                if m:
                    rid = f"WF:{m.group(1)}"
            if rid:
                if rid in idx and idx[rid] != str(p):
                    warns.append(f"{rid} 在下载日志里出现多个不同路径，取最后一个：{p}")
                idx[rid] = str(p)
    return idx, warns


def resolve_pdf(root: Path, rid: str, pdf: Optional[str] = None) -> Tuple[Optional[Path], List[str]]:
    """找到某条文献的 PDF。**顺序**：显式参数 → `pdf/<ID>.pdf` → **下载日志的权威路径**。

    ★ **绝不按题名匹配**。三处都找不到就返回 `None` 并说明。
    """
    notes: List[str] = []
    stem = rid.replace(":", "_")
    canonical = root / "03_全文" / "pdf" / f"{stem}.pdf"
    if pdf:
        p = Path(pdf)
        return (p if p.is_file() else None), ([f"指定的 PDF 不存在：{p}"] if not p.is_file() else [])
    if canonical.is_file():
        return canonical, notes
    idx, warns = download_index(root, sources_of(load_config(root)))
    notes += warns
    logged = idx.get(rid)
    if not logged:
        notes.append(
            f"下载日志里没有 {rid} 的记录——**不按题名猜**（实测按题名匹配曾配错一篇），"
            f"请把 PDF 放到 03_全文/pdf/{stem}.pdf，或确认它确实下载过"
        )
        return None, notes
    lp = Path(logged)
    if lp.is_file():
        notes.append(f"用下载日志的权威映射找到：{lp}")
        return lp, notes
    # 日志记录了路径但文件不在原处 → 按**日志里的 basename** 在规范区里找。
    # ★ basename 本身是**权威的**（来自日志），所以这不是猜；只在 `03_全文/` 下找。
    for cand in [canonical.parent / lp.name] + sorted(
        (root / "03_全文").rglob(lp.name)
    ):
        if cand.is_file():
            notes.append(f"按日志的 basename 找到：{cand}")
            return cand, notes
    notes.append(f"下载日志记录的文件不存在：{lp}（也不在 03_全文/ 下）")
    return None, notes


def _under(path: Path, root: Path) -> bool:
    """`path` 是否在 `root` 之内（用于决定 link 是改名还是复制）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def link_pdfs(root: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """把下载日志里记录的 PDF **按 ID 入库**到 `03_全文/pdf/`。"""
    pdf_dir = root / "03_全文" / "pdf"
    idx, warns = download_index(root, sources_of(load_config(root)))
    linked, already, missing, conflicts = [], [], [], []
    for rid, logged in sorted(idx.items()):
        stem = rid.replace(":", "_")
        dst = pdf_dir / f"{stem}.pdf"
        if dst.is_file():
            already.append(rid)
            continue
        src = Path(logged)
        if not src.is_file():
            # basename 权威（来自日志）：在 `03_全文/` 下按它找
            alt = pdf_dir / src.name
            if not alt.is_file():
                found = [c for c in sorted((root / "03_全文").rglob(src.name)) if c.is_file()]
                alt = found[0] if found else alt
            if alt.is_file():
                src = alt
            else:
                missing.append({"id": rid, "logged": logged})
                continue
        if src.name == dst.name:
            already.append(rid)
            continue
        linked.append({"id": rid, "from": src.name, "to": dst.name,
                       "mode": "rename" if _under(src, root) else "copy"})
        if not dry_run:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            # ★ 源文件**已在课题目录内**（通常在 `03_全文/pdf/` 里但名字是题名）→
            #   **改名**，别 copy：否则规范区里同时留下题名版与 ID 版，
            #   下次 `layout` 就报 `LAYOUT_ID_FILENAME`（实测踩过：8 个文件、4 个错）。
            #   源在课题目录**外**（上游的 save-dir）→ copy，保留用户原文件。
            if _under(src, root):
                src.replace(dst)
            else:
                shutil.copy2(src, dst)
    sealed = (root / SEAL_NAME).is_file()
    return {
        "root": str(root),
        "index_size": len(idx),
        "linked": linked,
        "already": already,
        "missing": missing,
        "conflicts": conflicts,
        "warnings": warns,
        "sealed_needs_reseal": bool(sealed and linked and not dry_run),
        "dry_run": dry_run,
    }


def cmd_link(args: argparse.Namespace) -> int:
    res = link_pdfs(Path(args.dir).resolve(), dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res["missing"] else 0
    print(f"# lit link —— {res['root']}")
    print(f"  下载日志里共 {res['index_size']} 条权威映射")
    print(f"  已在规范位置：{len(res['already'])} 条")
    for x in res["linked"]:
        print(f"  入库：{x['from'][:52]} → {x['to']}")
    for x in res["missing"]:
        print(f"  ! {x['id']}：日志记录「{x['logged']}」，但文件不存在（**不猜**）")
    for w in res["warnings"]:
        print(f"  ! {w}")
    if res["sealed_needs_reseal"]:
        print("  ⚠️ 只读区已封存，入库后需重新封存：`lit layout --seal`")
    print("  （--dry-run，未复制）" if res["dry_run"] else "  完成")
    return 1 if res["missing"] else 0


# ─────────────────────────────────────────── 全文抽取（note）

#: 抽出文字少于这个字符数 → 视为"抽不出文字"（扫描件/图片版）
TEXT_MIN_CHARS = 50


def note_extract(
    root: Path, rid: str, *, pdf: Optional[str] = None, set_tier: bool = True
) -> Dict[str, Any]:
    """用 `fitz` 把 PDF 抽成带页码标记的文本，落到 `03_全文/text/<ID>.txt`。

    **抽不出文字时不写空文件**——而是给出 `unreadable` 的判定（S-01 / S-04 §3）。
    """
    root = Path(root)
    stem = rid.replace(":", "_")
    found, notes = resolve_pdf(root, rid, pdf)
    out = {
        "id": rid,
        "pdf": str(found) if found else str(root / "03_全文" / "pdf" / f"{stem}.pdf"),
        "text_path": None,
        "pages": 0,
        "chars": 0,
        "tier": None,
        "ok": False,
        "problems": [],
        "resolve_notes": notes,
    }
    if found is None:
        out["problems"] = notes or [
            f"找不到 PDF：{root / '03_全文' / 'pdf' / (stem + '.pdf')}"
            f"（先下载到 03_全文/pdf/{stem}.pdf，文件名必须是 ID 形式——S-07 §3.2）"
        ]
        return out
    src = found
    try:
        import fitz  # PyMuPDF；`toc` 侧同款依赖
    except Exception as exc:  # pragma: no cover - 环境缺库
        out["problems"].append(f"缺 PyMuPDF（`pip install PyMuPDF`）：{exc}")
        return out
    try:
        doc = fitz.open(str(src))
        parts: List[str] = []
        for i, page in enumerate(doc, 1):
            parts.append(f"=== PAGE {i} ===\n{page.get_text()}")
        doc.close()
    except Exception as exc:
        out["problems"].append(f"PDF 解析失败：{exc}")
        return out

    text = "\n".join(parts)
    out["pages"] = len(parts)
    # ★ 实测修正：初版 `out["chars"]` 算的是**含 `=== PAGE n ===` 标记**的长度，
    #   而判据用的是**去掉标记后**的长度 —— 于是报出"有效字符 58 < 50"这种
    #   **数字与判据自相矛盾**的消息。两处必须用同一个数。
    body = re.sub(r"=== PAGE \d+ ===", "", text)
    body_chars = len(re.sub(r"\s+", "", body))
    out["chars"] = body_chars
    out["chars_with_markers"] = len(re.sub(r"\s+", "", text))
    if body_chars < TEXT_MIN_CHARS:
        out["tier"] = "unreadable"
        out["problems"].append(
            f"抽不出文字（有效字符 {body_chars} < {TEXT_MIN_CHARS}）——多半是扫描件/图片版 PDF。"
            "档位记 `unreadable`，**不许当读过**（S-04 §3）；要读请人工处理或做 OCR。"
        )
        if set_tier:
            _set_tier(root, rid, "unreadable", "ingest：PDF 抽不出文字（S-04 §3）")
        return out

    d = root / "03_全文" / "text"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{stem}.txt"
    dst.write_text(text, encoding="utf-8")
    out["text_path"] = str(dst)
    out["tier"] = "full_text"
    out["ok"] = True
    if set_tier:
        _set_tier(root, rid, "full_text", "ingest：已抽出带页码的全文")
    return out


def _set_tier(root: Path, rid: str, tier: str, note: str) -> None:
    """回写 `refs.json` 的档位——**人工档位不动**。"""
    refs_path = root / "02_候选库" / "refs.json"
    if not refs_path.is_file():
        return
    refs, _ = load_refs(root)
    changed = False
    for r in refs:
        if str(r.get("id")) != rid:
            continue
        if r.get("tier_source") == "manual":
            return  # 人的判断优先
        r["tier"] = tier
        r["tier_source"] = "ingest"
        r["tier_note"] = note
        changed = True
    if changed:
        refs_path.write_text(json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_note(args: argparse.Namespace) -> int:
    res = note_extract(
        Path(args.dir), args.id, pdf=args.pdf, set_tier=not args.no_set_tier
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"# lit note —— {res['id']}")
        print(f"  PDF：{res['pdf']}")
        if res["ok"]:
            print(f"  抽出 {res['pages']} 页 / {res['chars']} 字 → {res['text_path']}")
            print(f"  档位 → {res['tier']}")
        for p in res["problems"]:
            print(f"  X {p}")
    return 0 if res["ok"] else 1


# ─────────────────────────────────────────── 著录生成（refs）

#: 文献类型 → GB/T 7714 的载体代码
GB_TYPE_CODE: Dict[str, str] = {
    "期刊论文": "J",
    "期刊": "J",
    "学位论文": "D",
    "硕士论文": "D",
    "博士论文": "D",
    "会议论文": "C",
    "会议": "C",
    "专利": "P",
    "专著": "M",
    "标准": "S",
}


def _gb_name(name: str) -> Tuple[str, bool]:
    """把单个作者名规范化成 GB/T 7714 的 `姓 名首字母` 形式。返回 (规范名, 是否含推断)。

    **规则**（并**明确标注哪些是推断**——S-04 §1 不许悄悄猜）：
    · 有**单个字母/首字母**的 token（如 `A. One`、`Srikanth V`）→ 它作名首字母，另一 token 作姓；
    · 全是完整单词（如 `Nimalika Tiwari`）→ **假名最后一个是姓**（西式顺序）→ 标 `inferred`；
    · 中文名（含汉字）→ **原样输出**（GB/T 7714 中文姓名不缩写）。
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    if not s:
        return "", False
    if re.search(r"[\u4e00-\u9fff]", s):   # 中文姓名：原样
        return s, False
    toks = [t for t in s.split(" ") if t]
    if len(toks) == 1:
        return toks[0].upper(), False
    initial_idx = [i for i, t in enumerate(toks) if re.fullmatch(r"[A-Za-z]\.?", t)]
    if initial_idx and len(toks) == 2:
        ini = initial_idx[0]
        family = toks[1 - ini]
        return f"{family.upper()} {toks[ini][0].upper()}", False
    # 全是完整单词 → 假定最后一个为姓（西式）→ **这是推断**
    family, given = toks[-1], toks[:-1]
    initials = " ".join(g[0].upper() for g in given if g)
    return f"{family.upper()} {initials}".strip(), True


def _gb_authors(authors: Any, lang: str = "") -> Tuple[str, int]:
    """作者串 + **推断条数**。`>3` 位按 GB/T 7714 用 `等`/`et al.`。"""
    if isinstance(authors, str):
        authors = [a for a in re.split(r"[;,，、]", authors) if a.strip()]
    names = [str(a).strip() for a in (authors or []) if str(a).strip()]
    if not names:
        return "", 0
    out, inferred = [], 0
    for n in names:
        gb, inf = _gb_name(n)
        if gb:
            out.append(gb)
            inferred += 1 if inf else 0
    if len(out) > 3:
        tail = "等" if lang == "zh" or any(re.search(r"[\u4e00-\u9fff]", n) for n in out[:3]) else "et al"
        return ", ".join(out[:3]) + f", {tail}", inferred
    return ", ".join(out), inferred


def gbt7714_entry(r: Dict[str, Any], idx: int) -> Tuple[str, List[str]]:
    """生成一条 GB/T 7714-2015 著录，并返回 (条目, 缺失/未规范的项)。

    ★ **只输出真实字段，缺的留空并登记**——`refs.json` 里没有卷期页就**不写**，
      不许编一个"看起来完整"的条目（S-04 §1 / S-05 §1.1）。
    """
    problems: List[str] = []
    lang = str(r.get("lang") or "")
    au, au_inferred = _gb_authors(r.get("authors"), lang)
    if not au:
        problems.append("缺作者")
    if au_inferred:
        problems.append(f"作者姓名**推断**了 {au_inferred} 个的姓/名顺序（西式假设），需人工复核")
    title = str(r.get("title") or "").strip()
    if not title:
        problems.append("缺题名")
    year = r.get("year")
    if not year:
        problems.append("缺年份")
    typ = str(r.get("type") or "")
    code = GB_TYPE_CODE.get(typ)
    if not code:
        # 学位论文在 `type` 里常写成 "[硕士论文]" 之类，已在 ingest 去掉括号
        code = "D" if "学位" in typ or "硕士" in typ or "博士" in typ else "J"
        problems.append(f"类型「{typ or '空'}」未匹配，按 [{code}] 处理")
    venue = str(r.get("venue") or "").strip()
    if code in ("J", "C") and not venue:
        problems.append("缺刊名/会议名")
    doi = str(r.get("doi") or "").strip()
    url = str(r.get("url") or "").strip()
    # ★ 只对**不可信**的学位论文 DOI 省略；DOI 里含自身 ID 的（如 `10.7666/D03227905`）**保留**。
    #   实测修正：初版"学位论文一律省略 DOI"太粗，会丢掉**正确**的 DOI。
    doi_unreliable = code == "D" and bool(doi) and not doi_looks_self_consistent(
        str(r.get("id") or ""), doi
    )
    if doi_unreliable:
        problems.append("来源给的 DOI **不含本文 ID**，疑似参考文献里某篇的——**已省略**，需人工核")

    y = str(year) if year else ""
    if code == "D":
        body = f"{au}. {title}[D]. {venue + ', ' if venue else ''}{y}."
    elif code == "C":
        body = f"{au}. {title}[C]//{venue}. {y}."
    elif code == "P":
        body = f"{au}. {title}[P]. {y}."
    elif code == "M":
        body = f"{au}. {title}[M]. {venue + ': ' if venue else ''}{y}."
    else:
        body = f"{au}. {title}[J]. {venue + ', ' if venue else ''}{y}."
    if doi and not doi_unreliable:
        body += f" DOI: {doi}."
    elif url:
        body += f" {url}."
    # **没有卷(期):页码** —— 如实登记，不编
    if code in ("J", "C"):
        problems.append("无卷(期):页码（数据源未提供）")
    problems.append("作者姓名未做缩写规范") if r.get("lang") == "en" else None
    return f"[{idx}] {body}", problems


def citation_order(root: Path, scan: Optional[Path] = None) -> Dict[str, int]:
    """按**正文里的首次出现顺序**给出 ID 次序（GB/T 7714 要求按引用顺序编号）。

    **只扫指定目录**（默认 `06_综述/`）——不扫 `02_候选库/` 等，否则"排除清单里提了一句"
    会被当成"正文引用"（实测会踩）。
    """
    base = scan or (root / "06_综述")
    order: Dict[str, int] = {}
    if not Path(base).exists():
        return order
    paths = [Path(base)] if Path(base).is_file() else sorted(Path(base).rglob("*.md"))
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in RE_CITE_KEY.finditer(text):
            key = f"{m.group(1)}:{m.group(2)}"
            if key not in order:
                order[key] = len(order)
    return order


def build_refs(root: Path, *, order: str = "citation",
               scan: Optional[Path] = None) -> Dict[str, Any]:
    """从 `refs.json` 生成著录清单 + 硬指标核查。**已排除的条目不进表。**

    `order`：
      · `citation`（默认，GB/T 7714 要求）——按正文首次引用顺序；**未被引用的排在末尾并单独列出**
      · `year` —— 年份新→旧
      · `id` —— ID 升序（稳定，便于比对）
    """
    config = load_config(root)
    refs, err = load_refs(root)
    usable = [r for r in refs if not r.get("excluded")]
    excluded = [r for r in refs if r.get("excluded")]

    cited = citation_order(root, scan) if order == "citation" else {}
    if order == "citation":
        usable.sort(key=lambda r: (cited.get(str(r.get("id")), 10**6),
                                   -(r.get("year") or 0), str(r.get("title") or "")))
    elif order == "id":
        usable.sort(key=lambda r: str(r.get("id") or ""))
    else:
        usable.sort(key=lambda r: (-(r.get("year") or 0), str(r.get("title") or "")))
    uncited = ([str(r.get("id")) for r in usable if str(r.get("id")) not in cited]
               if order == "citation" else [])

    entries, field_problems, au_inferred = [], [], 0
    for i, r in enumerate(usable, 1):
        line, probs = gbt7714_entry(r, i)
        au_inferred += sum(1 for p in probs if "推断" in p)
        entries.append({"n": i, "id": r.get("id"), "text": line,
                        "tier": r.get("tier"), "problems": probs,
                        "cited": (str(r.get("id")) in cited) if order == "citation" else None})
        for p in probs:
            field_problems.append({"id": r.get("id"), "problem": p})

    cap = _capacity_cfg(config)
    n = len(usable)
    years = [r.get("year") for r in usable if r.get("year")]
    recent_window = int(cap.get("recent_years") or 5)
    now_year = int(cap.get("now_year") or 2026)
    recent = sum(1 for y in years if y >= now_year - recent_window + 1)
    foreign = sum(1 for r in usable if str(r.get("lang") or "").lower() in ("en", "eng", "foreign"))
    stats = {
        "total": n,
        "excluded": len(excluded),
        "refs_min": cap.get("refs_min"),
        "meets_min": (n >= int(cap["refs_min"])) if cap.get("refs_min") else None,
        "year_range": [min(years), max(years)] if years else None,
        "recent_window": recent_window,
        "recent_n": recent,
        "recent_ratio": (recent / n) if n else 0.0,
        "recent_ratio_min": cap.get("recent_ratio_min"),
        "foreign_n": foreign,
        "foreign_ratio": (foreign / n) if n else 0.0,
        "foreign_ratio_min": cap.get("foreign_ratio_min"),
        "by_tier": {},
    }
    for r in usable:
        stats["by_tier"][str(r.get("tier"))] = stats["by_tier"].get(str(r.get("tier")), 0) + 1
    return {"root": str(root), "entries": entries, "stats": stats,
            "field_problems": field_problems, "parse_error": err,
            "order": order, "uncited": uncited, "au_inferred": au_inferred,
            "sources": list(sources_of(config))}


def render_refs_md(res: Dict[str, Any]) -> str:
    s = res["stats"]
    order_note = {
        "citation": "> ★ **编号按正文引用顺序**（GB/T 7714 要求）：扫 `06_综述/` 里 `[ID]` 的首次出现；"
                    "**未被引用的条目排在末尾**，见 §5。",
        "year": "> ★ 编号按**年份新→旧**——**这不是 GB/T 7714 要求的引用顺序**，仅供浏览。",
        "id": "> ★ 编号按 **ID 升序**（稳定，便于比对）——**不是引用顺序**。",
    }[res["order"]]
    L: List[str] = [
        "# 参考文献（GB/T 7714-2015）",
        "",
        "> **本表由 `lit refs` 从 `02_候选库/refs.json` 生成**（唯一权威题录库）。",
        "> ★ **缺字段的条目留空并登记**，未编造卷期页（S-04 §1 / S-05 §1.1）。",
        order_note,
        "",
        "## 1 著录条目",
        "",
    ]
    for e in res["entries"]:
        tag = "`full_text`" if e["tier"] == "full_text" else f"`{e['tier']}`"
        L.append(f"{e['text']}  <!-- {e['id']} · {tag} -->")
    L += ["", "## 2 硬指标核查（阈值取自 `lit.config.json`）", "",
          "| 项 | 要求 | 实际 | 达标 |", "|---|---|---|---|"]
    if s["refs_min"]:
        L.append(f"| 著录篇数 | ≥{s['refs_min']} | **{s['total']}** | {'是' if s['meets_min'] else '**否**'} |")
    if s["recent_ratio_min"]:
        ok = s["recent_ratio"] >= float(s["recent_ratio_min"])
        L.append(f"| 近 {s['recent_window']} 年占比 | ≥{float(s['recent_ratio_min']):.2f} | "
                 f"{s['recent_n']}/{s['total']} = {s['recent_ratio']:.2f} | {'是' if ok else '**否**'} |")
    if s["foreign_ratio_min"]:
        ok = s["foreign_ratio"] >= float(s["foreign_ratio_min"])
        L.append(f"| 外文占比 | ≥{float(s['foreign_ratio_min']):.2f} | "
                 f"{s['foreign_n']}/{s['total']} = {s['foreign_ratio']:.2f} | {'是' if ok else '**否**'} |")
    L.append(f"| 已排除（不进表） | — | {s['excluded']} 条 | — |")
    if s["year_range"]:
        L.append(f"| 年份范围 | — | {s['year_range'][0]}–{s['year_range'][1]} | — |")
    L += ["", "### 档位分布", ""]
    for t, c in sorted(s["by_tier"].items()):
        L.append(f"- `{t}`：{c} 条")
    L += ["", "## 3 字段缺失登记（S-05 §1.3 / §3）", "",
          "> **本表逐条列出所有「取不到的字段」**。按 S-05 §1.3 三条对策："
          "去别的来源找 → 从 PDF 取 → **取不到就不用它**。", ""]
    if res["field_problems"]:
        L += ["| ID | 问题 |", "|---|---|"]
        for p in res["field_problems"]:
            L.append(f"| {p['id']} | {p['problem']} |")
    else:
        L.append("（无）")
    L += ["", "## 4 本工具**没有**做的（S-05 §3 如实声明）", "",
          "| 项 | 状态 |", "|---|---|",
          "| GB/T 7714 的**标点与著录顺序**逐类型校验 | **未做** |",
          "| **卷(期):页码** | **未做**——数据源不提供，**不许编** |",
          "| 中英文条目**式样是否统一** | **未做** |",
          "| 作者姓名**规范性** | **已做缩写**（`SRIKANTH V` 式）；但**姓/名顺序为推断**，见下 |",
          "", "> **审计全过 ≠ 著录合格**：本表只保证「字段齐、编号连续、引用不悬空」。", ""]
    if res.get("au_inferred"):
        L += ["", "### ⚠️ 作者姓名推断提示", "",
              f"共 **{res['au_inferred']} 处**作者的姓/名顺序是按「**最后一个词是姓**」的西式假设推断的，"
              "**需人工复核**（印度、匈牙利等姓名顺序可能相反）——见 §3 逐条登记。", ""]
    if res["order"] == "citation" and res.get("uncited"):
        L += ["", "## 5 未被正文引用的条目", "",
              f"共 **{len(res['uncited'])}** 条进了题录库但正文没引（**这本身正常**，"
              "列出来是为了发现「是不是漏引了关键文献」——S-05 §4）：", ""]
        for i in res["uncited"]:
            L.append(f"- `{i}`")
    return "\n".join(L)


def cmd_refs(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    # ★ 通用化：著录风格可配（`lit.config.json` 的 `refs_style`，默认 `gb7714`）。
    #   **本工具只实现了 gb7714** —— 选了别的就**明确说"我没做"**，而不是悄悄按国标输出
    #   （S-05 §3 已声明不校验国标标点；把它伪装成通用格式器更坏）。
    style = str(load_config(root).get("refs_style") or "gb7714").lower()
    if style not in ("gb7714", "gbt7714", "gb/t7714", "gb7714-2015"):
        print(f"X 你的 lit.config.json 指定 `refs_style = \"{style}\"`，但本工具**只实现了 gb7714**。")
        print("  它**不会**假装支持——按你的风格自行著录，或用 `--order`/`--json` 取结构化数据后另做渲染。")
        print("  （改回 gb7714 只需删掉 config 里的 refs_style，或写成 \"gb7714\"）")
        return 2
    scan = Path(args.scan).resolve() if args.scan else None
    res = build_refs(root, order=args.order, scan=scan)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    md = render_refs_md(res)
    if args.write:
        d = root / "06_综述"
        d.mkdir(parents=True, exist_ok=True)
        (d / "参考文献.md").write_text(md, encoding="utf-8")
    print(f"# lit refs —— {res['root']}")
    s = res["stats"]
    print(f"  编号顺序：{res['order']}"
          + (f"（扫 {scan or root / '06_综述'}）；**未被引用 {len(res['uncited'])} 条**"
             if res["order"] == "citation" else ""))
    if res["order"] == "citation":
        if res["uncited"] and len(res["uncited"]) == s["total"]:
            print("  ⚠️ **一条引用都没识别到**——多半不是「真没引」，而是"
                  "引用键没写成 `[IEEE:x]` / `[WF:y]` 形式（S-07 §2.3）。已有检查抓不到这一点。")
        else:
            print(f"  编号按正文引用顺序；未被引用 {len(res['uncited'])} 条排在末尾（见 §5）")
    print(f"  著录 {s['total']} 条（已排除 {s['excluded']} 条不进表）")
    if s["refs_min"]:
        print(f"  篇数硬线 {s['refs_min']}：{'达标' if s['meets_min'] else '**未达标**'}")
    if s["year_range"]:
        print(f"  年份范围 {s['year_range'][0]}–{s['year_range'][1]}；"
              f"近 {s['recent_window']} 年 {s['recent_n']}/{s['total']} = {s['recent_ratio']:.0%}")
    print(f"  外文 {s['foreign_n']}/{s['total']} = {s['foreign_ratio']:.0%}")
    print(f"  档位：{s['by_tier']}")
    if res["au_inferred"]:
        print(f"  ! 作者姓名推断 {res['au_inferred']} 处——**需人工复核**")
    if res["field_problems"]:
        print(f"  字段问题 {len(res['field_problems'])} 处（见参考文献.md §3）")
    print("  已写入 06_综述/参考文献.md" if args.write else "  （加 --write 才会落盘）")
    return 0


# ───────────────────────────────────────────────────────────────────── CLI


def cmd_layout(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    if args.seal:
        p = write_seal(root)
        print(f"已封存只读区：{p}")
        return 0
    res = check_layout(root)
    if args.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"# lit layout —— {res.root}")
        if not res.findings:
            print("✓ 目录结构合规（S-07 全部检查通过）")
        else:
            for f in res.findings:
                mark = "❌" if f.severity == "error" else "⚠️"
                print(f"{mark} [{f.code}] {f.message}")
                if f.loc:
                    print(f"    位置：{f.loc}")
                if f.fix:
                    print(f"    怎么改：{f.fix}")
        print(f"\n共 {len(res.errors)} 个错误 / {len(res.warns)} 个警告")
    return 1 if res.errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lit", description="lit-research 规范检查器")
    sub = p.add_subparsers(dest="cmd", required=True)

    lay = sub.add_parser("layout", help="检查课题目录结构是否合规（S-07）")
    lay.add_argument("dir", help="课题目录")
    lay.add_argument("--json", action="store_true", help="输出 JSON")
    lay.add_argument("--seal", action="store_true", help="封存只读区哈希（供之后校验）")
    lay.set_defaults(func=cmd_layout)

    aud = sub.add_parser("audit", help="合规检查：档位 / 容量 / 数字出处 / 可比性（S-01~S-03）")
    aud.add_argument("dir", help="课题目录")
    aud.add_argument("--json", action="store_true", help="输出 JSON")
    aud.set_defaults(func=cmd_audit)

    sc = sub.add_parser("selfcheck", help="检查 skill 自身的文档（悬空引用 / 索引 / 条款号 / code 登记）")
    sc.add_argument("dir", nargs="?", default=None, help="skill 根目录（默认自动推断）")
    sc.add_argument("--json", action="store_true", help="输出 JSON")
    sc.set_defaults(func=cmd_selfcheck)

    ing = sub.add_parser("ingest", help="把 01_检索/*/raw/ 的落盘 JSON 汇成 02_候选库/refs.json")
    ing.add_argument("dir", help="课题目录")
    ing.add_argument("--json", action="store_true", help="输出 JSON")
    ing.add_argument("--dry-run", action="store_true", help="只看结果，不写文件")
    ing.set_defaults(func=cmd_ingest)

    pl = sub.add_parser("plan", help="生成 lit.config.json（容量与比例；**硬线必须显式给出**）")
    pl.add_argument("dir", help="课题目录")
    pl.add_argument("--refs-min", type=int, default=None,
                    help="参考文献条数（**必填**：来自外部规范，或你自己承诺）")
    pl.add_argument("--now-year", type=int, default=None, help="\"今年\"（必填，不许用系统时间）")
    pl.add_argument("--recent-years", type=int, default=5, help="近 N 年（默认 5）")
    pl.add_argument("--recent-ratio", type=float, default=None, help="近 N 年最低占比（如 0.3333）")
    pl.add_argument("--foreign-ratio", type=float, default=None, help="外文最低占比（如 0.3333）")
    pl.add_argument("--sources", default="", help="来源名单，逗号分隔（默认 ieee,wanfang）")
    pl.add_argument("--source", default=None,
                    help="容量出处：外部规范名 / 期刊指南 / 「自定义」（建议写，S-02 §1.3）")
    pl.add_argument("--refs-style", default=None,
                    help="著录风格：gb7714（默认）/ ieee / apa / 其它（非 gb7714 时 lit refs 会明确拒做）")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--dry-run", action="store_true")
    pl.set_defaults(func=cmd_plan)

    nt = sub.add_parser("note", help="用 fitz 把 PDF 抽成带页码的全文；抽不出就给 unreadable")
    nt.add_argument("dir", help="课题目录")
    nt.add_argument("--id", required=True, help="文献 ID（如 IEEE:11181461 / WF:D04271643）")
    nt.add_argument("--pdf", default=None, help="PDF 路径（默认 03_全文/pdf/<ID>.pdf）")
    nt.add_argument("--no-set-tier", action="store_true", help="不回写 refs.json 的档位")
    nt.add_argument("--json", action="store_true")
    nt.set_defaults(func=cmd_note)

    rf = sub.add_parser("refs", help="生成 GB/T 7714-2015 著录清单 + 硬指标核查")
    rf.add_argument("dir", help="课题目录")
    rf.add_argument("--write", action="store_true", help="写入 06_综述/参考文献.md")
    rf.add_argument("--order", choices=("citation", "year", "id"), default="citation",
                    help="编号顺序：citation=正文引用顺序（GB/T 7714 要求，默认）；year/id 仅备查")
    rf.add_argument("--scan", default=None,
                    help="按引用顺序编号时扫哪个文件/目录（默认 <课题>/06_综述）")
    rf.add_argument("--json", action="store_true")
    rf.set_defaults(func=cmd_refs)

    lk = sub.add_parser("link", help="按下载日志的权威映射把 PDF 入库（**不按题名猜**）")
    lk.add_argument("dir", help="课题目录")
    lk.add_argument("--dry-run", action="store_true", help="只报告，不复制")
    lk.add_argument("--json", action="store_true")
    lk.set_defaults(func=cmd_link)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
