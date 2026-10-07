#!/usr/bin/env python3
"""`lit audit` 的离线单测。

**判别力要求**：合规课题目录必须 0 错；每注入一条违规，**必须**报出对应 code；
**缺 config / 缺 refs 必须报错，绝不报"通过"**（恒真检查器的教训）。

跑法：python scripts/tests/test_audit.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import lit_cli  # noqa: E402

CAP = {
    "refs_min": 3,
    "recent_years": 5,
    "now_year": 2026,
    "recent_ratio_min": 0.3333,
    "foreign_ratio_min": 0.3333,
}


def make_course(root: Path, *, refs=None, config=True, pdf=True) -> Path:
    """造一个**合规**的课题目录（足够 audit 通过）。"""
    (root / "01_检索" / "ieee" / "raw").mkdir(parents=True)
    (root / "01_检索" / "ieee" / "检索式.md").write_text("关键词：x\n", encoding="utf-8")
    (root / "02_候选库").mkdir()
    (root / "03_全文" / "pdf").mkdir(parents=True)
    (root / "03_全文" / "text").mkdir(parents=True)
    (root / "04_精读").mkdir()
    (root / "05_聚合").mkdir()
    (root / "README.md").write_text("目标：x\n", encoding="utf-8")
    if config:
        (root / "lit.config.json").write_text(
            json.dumps(
                {
                    "sources": ["ieee", "wanfang"],
                    "tiers": list(lit_cli.TIER_FALLBACK),
                    "capacity": CAP,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if refs is None:
        refs = [
            {"id": "IEEE:1", "title": "A", "authors": ["X"], "year": 2025,
             "lang": "en", "tier": "full_text", "type": "期刊论文", "venue": "V"},
            {"id": "WF:2", "title": "B", "authors": ["Y"], "year": 2024,
             "lang": "en", "tier": "abstract_only", "type": "学位论文"},
            {"id": "WF:3", "title": "C", "authors": ["Z"], "year": 2023,
             "lang": "en", "tier": "full_text", "type": "学位论文"},
        ]
    if refs is not None:
        (root / "02_候选库" / "refs.json").write_text(
            json.dumps(refs, ensure_ascii=False), encoding="utf-8"
        )
        (root / "02_候选库" / "排除清单.md").write_text("筛掉 1 条\n", encoding="utf-8")
    if pdf:
        for i in ("IEEE_1", "WF_3"):
            (root / "03_全文" / "pdf" / f"{i}.pdf").write_bytes(b"%PDF-1.4")
            (root / "03_全文" / "text" / f"{i}.txt").write_text("=== PAGE 1 ===\n", encoding="utf-8")
        (root / "03_全文" / "获取失败.md").write_text("未订阅：1\n", encoding="utf-8")
    return root


def codes(res) -> set:
    return {f.code for f in res.findings}


# ───────────────────────────────── ① 合规必须 0 错（防恒真/误报）

def test_compliant_course_passes(tmp: Path) -> None:
    res = lit_cli.check_audit(make_course(tmp / "c"))
    assert not res.errors, [f.as_dict() for f in res.errors]
    assert not res.warns, [f.as_dict() for f in res.warns]


# ───────────────────────────────── ② 缺前置必须报错，绝不报通过

def test_missing_config_fails_closed(tmp: Path) -> None:
    """没有 config 时必须报错——**实测踩过"什么都没查却报通过"**。"""
    res = lit_cli.check_audit(make_course(tmp / "c", config=False))
    assert "AUDIT_NO_CONFIG" in codes(res)
    assert res.errors, "缺 config 绝不能判为通过"


def test_missing_refs_fails_closed_and_stops(tmp: Path) -> None:
    root = make_course(tmp / "c", refs=[])
    (root / "02_候选库" / "refs.json").unlink()
    res = lit_cli.check_audit(root)
    assert "AUDIT_NO_REFS" in codes(res)
    assert res.errors


def test_nonexistent_dir_is_an_error(tmp: Path) -> None:
    res = lit_cli.check_audit(tmp / "not-here")
    assert res.errors


# ───────────────────────────────── ③ 每条违规报对

def test_capacity_short(tmp: Path) -> None:
    refs = [{"id": "IEEE:1", "title": "A", "authors": ["X"], "year": 2025,
             "lang": "en", "tier": "abstract_only", "type": "学位论文"}]
    res = lit_cli.check_audit(make_course(tmp / "c", refs=refs, pdf=False))
    assert "AUDIT_CAPACITY_SHORT" in codes(res)


def test_recent_ratio_low(tmp: Path) -> None:
    refs = [
        {"id": f"IEEE:{i}", "title": "A", "authors": ["X"], "year": 2001,
         "lang": "en", "tier": "abstract_only", "type": "学位论文"}
        for i in range(3)
    ]
    res = lit_cli.check_audit(make_course(tmp / "c", refs=refs, pdf=False))
    assert "AUDIT_RATIO_RECENT" in codes(res)


def test_foreign_ratio_low(tmp: Path) -> None:
    refs = [
        {"id": f"WF:{i}", "title": "A", "authors": ["X"], "year": 2025,
         "lang": "zh", "tier": "abstract_only", "type": "学位论文"}
        for i in range(3)
    ]
    res = lit_cli.check_audit(make_course(tmp / "c", refs=refs, pdf=False))
    assert "AUDIT_RATIO_FOREIGN" in codes(res)


def test_abstract_only_must_not_be_deep_read(tmp: Path) -> None:
    """红线 1 的落地：摘要只用于筛，不许进精读。"""
    root = make_course(tmp / "c")
    (root / "04_精读" / "WF_2.md").write_text("读了摘要就当读过\n", encoding="utf-8")
    assert "AUDIT_ABSTRACT_ONLY_DEEP_READ" in codes(lit_cli.check_audit(root))


def test_full_text_must_be_backed(tmp: Path) -> None:
    root = make_course(tmp / "c")
    (root / "03_全文" / "pdf" / "WF_3.pdf").unlink()
    (root / "03_全文" / "text" / "WF_3.txt").unlink()
    assert "AUDIT_TIER_UNBACKED" in codes(lit_cli.check_audit(root))


def test_number_without_source_is_warned(tmp: Path) -> None:
    root = make_course(tmp / "c")
    (root / "04_精读" / "IEEE_1.md").write_text("准确率达到 94.05%，效果很好。\n", encoding="utf-8")
    res = lit_cli.check_audit(root)
    assert "AUDIT_NUMBER_WITHOUT_SOURCE" in codes(res)
    # 带了页码就不该报
    (root / "04_精读" / "IEEE_1.md").write_text("准确率达到 94.05%（p3）。\n", encoding="utf-8")
    assert "AUDIT_NUMBER_WITHOUT_SOURCE" not in codes(lit_cli.check_audit(root))


def test_comparability_declaration_required(tmp: Path) -> None:
    root = make_course(tmp / "c")
    (root / "05_聚合" / "指标对比表.md").write_text("| 方法 | mAP |\n|---|---|\n| A | 94.0 |\n", encoding="utf-8")
    assert "AUDIT_NO_COMPARABILITY" in codes(lit_cli.check_audit(root))
    # 声明了就不该报
    (root / "05_聚合" / "指标对比表.md").write_text(
        "> 注：以下数字**不可直接比较**（同一数据集但子集不同）。\n\n| 方法 | mAP |\n|---|---|\n| A | 94.0 |\n",
        encoding="utf-8",
    )
    assert "AUDIT_NO_COMPARABILITY" not in codes(lit_cli.check_audit(root))


def test_ref_field_missing(tmp: Path) -> None:
    refs = [
        {"id": "IEEE:1", "authors": ["X"], "year": 2025, "lang": "en",
         "tier": "abstract_only", "type": "学位论文"},                      # 缺 title
        {"id": "IEEE:2", "title": "B", "year": 2024, "lang": "en",
         "tier": "abstract_only", "type": "学位论文"},                      # 缺 authors
        {"id": "IEEE:3", "title": "C", "authors": ["Z"], "year": 2023, "lang": "en",
         "tier": "abstract_only", "type": "期刊论文"},                      # 期刊缺 venue
    ]
    res = lit_cli.check_audit(make_course(tmp / "c", refs=refs, pdf=False))
    msgs = " ".join(f.message for f in res.findings if f.code == "AUDIT_REF_FIELD_MISSING")
    assert "title" in msgs and "authors" in msgs and "venue" in msgs, msgs


# ───────────────────────────────── ④ 只读

def test_audit_only_reads_the_tree(tmp: Path) -> None:
    root = make_course(tmp / "c")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    lit_cli.check_audit(root)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after


if __name__ == "__main__":
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"a{i:02d}"
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
