#!/usr/bin/env python3
"""lit layout 的离线单测。

**判别力要求**：合规树必须 0 错；每注入一条违规，**必须**报出对应的 code。
照 `ieee-research` / `wanfang-research` 的体例：不需要网络、不需要浏览器。

跑法：
    python scripts/tests/test_layout.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))  # scripts/ 可导入

import lit_cli  # noqa: E402


def make_tree(root: Path, *, with_refs: bool = True, with_pdf: bool = True) -> Path:
    """造一棵**合规**的课题目录。"""
    (root / "00_方向地图").mkdir(parents=True)
    for src in ("ieee", "wanfang"):
        (root / "01_检索" / src / "raw").mkdir(parents=True)
        (root / "01_检索" / src / "检索式.md").write_text("关键词：x\n命中：1\n日期：2026-10-06\n", encoding="utf-8")
        (root / "01_检索" / src / "raw" / f"{src}_search-1.json").write_text("{}", encoding="utf-8")
    (root / "01_检索" / "去重报告.md").write_text("无重复\n", encoding="utf-8")
    (root / "02_候选库").mkdir()
    (root / "03_全文" / "pdf").mkdir(parents=True)
    (root / "03_全文" / "text").mkdir(parents=True)
    (root / "04_精读").mkdir()
    (root / "05_聚合").mkdir()
    (root / "06_综述").mkdir()
    (root / "07_核验").mkdir()
    (root / "README.md").write_text("目标：x\n", encoding="utf-8")
    (root / "lit.config.json").write_text(
        json.dumps({"tiers": list(lit_cli.TIER_FALLBACK), "sources": ["ieee", "wanfang"]}, ensure_ascii=False), encoding="utf-8"
    )
    if with_refs:
        (root / "02_候选库" / "refs.json").write_text(
            json.dumps(
                [
                    {"id": "IEEE:11181461", "title": "A", "tier": "full_text"},
                    {"id": "WF:D04271643", "title": "B", "tier": "abstract_only"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "02_候选库" / "排除清单.md").write_text("筛掉 1 条：同词异义\n", encoding="utf-8")
    if with_pdf:
        (root / "03_全文" / "pdf" / "IEEE_11181461.pdf").write_bytes(b"%PDF-1.4 x")
        (root / "03_全文" / "text" / "IEEE_11181461.txt").write_text("=== PAGE 1 ===\n x\n", encoding="utf-8")
        (root / "03_全文" / "获取失败.md").write_text("未订阅：1\n", encoding="utf-8")
    return root


def codes(res: lit_cli.LayoutResult) -> set:
    return {f.code for f in res.findings}


# ───────────────────────────────────────────── ① 合规树必须 0 错

def test_compliant_tree_has_no_errors(tmp: Path) -> None:
    root = make_tree(tmp / "课题A")
    res = lit_cli.check_layout(root)
    assert not res.errors, [f.as_dict() for f in res.errors]


# ───────────────────────────────────────────── ② 每条违规都要报出对应 code

def test_unknown_top_entry(tmp: Path) -> None:
    root = make_tree(tmp / "课题B")
    (root / "_recon").mkdir()
    assert "LAYOUT_UNKNOWN_TOP" in codes(lit_cli.check_layout(root))


def test_dup_stage_from_rename(tmp: Path) -> None:
    root = make_tree(tmp / "课题C")
    (root / "01_文献检索_IEEE").mkdir()  # 改名残骸
    assert "LAYOUT_DUP_STAGE" in codes(lit_cli.check_layout(root))


def test_raw_non_json(tmp: Path) -> None:
    root = make_tree(tmp / "课题D")
    (root / "01_检索" / "ieee" / "raw" / "note.txt").write_text("x", encoding="utf-8")
    assert "LAYOUT_RAW_NON_JSON" in codes(lit_cli.check_layout(root))


def test_raw_log_is_allowed(tmp: Path) -> None:
    """★ 实测让步：抓取工具会把自己的启动日志写进 raw/（典型 chrome-launch.log）。

    规范要求把日志变量指到 `raw/`，工具顺带也写自己的启动日志——**冲突在规范这一侧**。
    实测被它挡住两次，故 `raw/` 允许 `.log`。这条单测钉住该让步，防止被顺手改回去。
    """
    root = make_tree(tmp / "课题D2")
    (root / "01_检索" / "ieee" / "raw" / "chrome-launch.log").write_text("x", encoding="utf-8")
    assert "LAYOUT_RAW_NON_JSON" not in codes(lit_cli.check_layout(root))


def test_pdf_filename_must_be_id(tmp: Path) -> None:
    root = make_tree(tmp / "课题E")
    (root / "03_全文" / "pdf" / "一篇很长的论文标题.pdf").write_bytes(b"%PDF-1.4")
    assert "LAYOUT_ID_FILENAME" in codes(lit_cli.check_layout(root))


def test_missing_tier_and_bad_tier_and_dup_id(tmp: Path) -> None:
    root = make_tree(tmp / "课题F")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps(
            [
                {"id": "IEEE:1", "title": "a"},                       # 缺 tier
                {"id": "IEEE:2", "title": "b", "tier": "读过"},         # 非法 tier
                {"id": "IEEE:3", "title": "c", "tier": "full_text"},
                {"id": "IEEE:3", "title": "d", "tier": "full_text"},   # 重复 ID
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    got = codes(lit_cli.check_layout(root))
    assert "LAYOUT_MISSING_TIER" in got
    assert "LAYOUT_BAD_TIER" in got
    assert "LAYOUT_DUP_ID" in got


def test_dangling_citation(tmp: Path) -> None:
    root = make_tree(tmp / "课题G")
    (root / "04_精读" / "IEEE_11181461.md").write_text("见 [IEEE:99999999] 与 [WF:D04271643]。\n", encoding="utf-8")
    res = lit_cli.check_layout(root)
    assert "LAYOUT_DANGLING_CITE" in codes(res)
    # 存在的那个键不该被报成悬空
    msgs = " ".join(f.message for f in res.findings if f.code == "LAYOUT_DANGLING_CITE")
    assert "WF:D04271643" not in msgs, msgs


def test_missing_failure_lists(tmp: Path) -> None:
    root = make_tree(tmp / "课题H", with_refs=True, with_pdf=False)
    (root / "03_全文" / "pdf").mkdir(parents=True, exist_ok=True)  # 有 pdf 目录但没有失败清单
    assert "LAYOUT_MISSING_FAILURE_LIST" in codes(lit_cli.check_layout(root))


def test_missing_query_log(tmp: Path) -> None:
    root = make_tree(tmp / "课题I")
    (root / "01_检索" / "ieee" / "检索式.md").unlink()
    assert "LAYOUT_MISSING_QUERY_LOG" in codes(lit_cli.check_layout(root))


def test_hard_number_in_prose_is_warned(tmp: Path) -> None:
    root = make_tree(tmp / "课题J")
    (root / "README.md").write_text("本课题要求著录不少于 80 篇，外文 ≥1/3。\n", encoding="utf-8")
    res = lit_cli.check_layout(root)
    assert "LAYOUT_HARD_NUMBER_IN_PROSE" in codes(res)
    assert all(f.severity == "warn" for f in res.findings if f.code == "LAYOUT_HARD_NUMBER_IN_PROSE")


def test_missing_top_files(tmp: Path) -> None:
    root = make_tree(tmp / "课题K")
    (root / "lit.config.json").unlink()
    assert "LAYOUT_UNKNOWN_TOP" in codes(lit_cli.check_layout(root))


# ───────────────────────────────────────────── ③ 封存：改动只读区必须报

def test_seal_detects_readonly_change(tmp: Path) -> None:
    root = make_tree(tmp / "课题L")
    lit_cli.write_seal(root)
    assert not lit_cli.check_layout(root).errors, "封存后立刻校验应当合规"

    p = root / "03_全文" / "text" / "IEEE_11181461.txt"
    p.write_text("=== PAGE 1 ===\n 被我改了\n", encoding="utf-8")
    assert "LAYOUT_SEAL_CHANGED" in codes(lit_cli.check_layout(root))


def test_seal_detects_deletion(tmp: Path) -> None:
    root = make_tree(tmp / "课题M")
    lit_cli.write_seal(root)
    (root / "01_检索" / "ieee" / "raw" / "ieee_search-1.json").unlink()
    assert "LAYOUT_SEAL_CHANGED" in codes(lit_cli.check_layout(root))


# ───────────────────────────────────────────── ④ 判别力自证：合规树不该误报

def test_no_false_positive_on_compliant_tree(tmp: Path) -> None:
    """合规树上**任何** code 都不该出现——防"恒真"检查器。"""
    root = make_tree(tmp / "课题N")
    res = lit_cli.check_layout(root)
    assert not res.findings, [f.as_dict() for f in res.findings]


def test_layout_only_reads_the_tree(tmp: Path) -> None:
    """`check_layout` 必须是**只读**的：跑完目录树不能变。"""
    root = make_tree(tmp / "课题O")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    lit_cli.check_layout(root)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after


if __name__ == "__main__":
    # ★ 临时目录必须建在**工作区内**且**不用 tempfile**：
    #   DSH 受限沙箱会拒绝写系统 %TEMP%（实测 `PermissionError: [WinError 5]`），
    #   连 tempfile 在工作区内建的子目录也踩过一次。用固定目录 + 序号最稳。
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"case{i:02d}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            fn(d)
            print(f"PASS  {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            fail += 1
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n{ok}/{ok + fail} 通过")
    raise SystemExit(1 if fail else 0)
