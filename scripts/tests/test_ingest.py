#!/usr/bin/env python3
"""`lit ingest` 的离线单测。

关键性质（每条都对应一个实测教训）：
  - **只往下判档位**：有全文才 full_text，只有摘要就 abstract_only；
  - **人工档位不被覆盖**（`tier_source: "manual"` 优先）；
  - **跨源去重**：同 DOI / 同归一题名 → 一条；
  - **raw/ 只读**：ingest 不改原始件；
  - 幂等：跑两次结果一致。

跑法：python scripts/tests/test_ingest.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import lit_cli  # noqa: E402


def course(root: Path, *, sources=("ieee", "wanfang")) -> Path:
    root.mkdir(parents=True, exist_ok=True)   # ★ 必须先建目录：后面直接写文件
    (root / "README.md").write_text("目标：x\n", encoding="utf-8")
    (root / "lit.config.json").write_text(
        json.dumps({"sources": list(sources), "tiers": list(lit_cli.TIER_FALLBACK),
                    "capacity": {"refs_min": 1}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "02_候选库").mkdir(parents=True, exist_ok=True)
    for s in sources:
        (root / "01_检索" / s / "raw").mkdir(parents=True, exist_ok=True)
    return root


def w_ieee_detail(root: Path, rows) -> None:
    (root / "01_检索" / "ieee" / "raw" / "ieee_detail-20261006.json").write_text(
        json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False), encoding="utf-8"
    )


def w_wf_detail(root: Path, rows) -> None:
    (root / "01_检索" / "wanfang" / "raw" / "wf_detail-20261006.json").write_text(
        json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False), encoding="utf-8"
    )


IEEE_ROW = {
    "arnumber": "11181461",
    "title": "YOLOv11-FST for PV defect detection",
    "authors": ["A. One", "B. Two"],
    "abstract": "We propose ...",
    "pubDate": "2025",
    "publishedIn": "IEEE Access",
    "doi": "10.1109/X.2025.1",
}
WF_ROW = {
    "url": "https://d.wanfangdata.com.cn/thesis/D04271643",
    "type": "[硕士论文]",
    "title": "基于深度学习的光伏组件缺陷检测方法研究",
    "authors": ["张三"],
    "abstract": "本文研究...",
    "degreeYear": "2024",
    "institution": "某大学",
}


# ───────────────────────────────────────── ① 基本汇总 + 档位只往下判

def test_ingest_writes_refs_with_ids_and_tiers(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    w_wf_detail(root, [WF_ROW])
    res = lit_cli.ingest_course(root)
    assert res["collected"] == 2 and res["kept"] == 2
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    ids = {r["id"] for r in refs}
    assert ids == {"IEEE:11181461", "WF:D04271643"}, ids
    # 都没有全文 → 不得是 full_text
    assert all(r["tier"] == "abstract_only" for r in refs), [
        (r["id"], r["tier"]) for r in refs
    ]
    assert all(r["tier_source"] == "ingest" for r in refs)


def test_pdf_and_text_upgrade_to_full_text(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    (root / "03_全文" / "pdf").mkdir(parents=True)
    (root / "03_全文" / "text").mkdir(parents=True)
    (root / "03_全文" / "pdf" / "IEEE_11181461.pdf").write_bytes(b"%PDF-1.4")
    (root / "03_全文" / "text" / "IEEE_11181461.txt").write_text("=== PAGE 1 ===\n x\n", encoding="utf-8")
    lit_cli.ingest_course(root)
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "full_text"


def test_pdf_without_text_is_unreadable(tmp: Path) -> None:
    """有 PDF 但没抽文字 → `unreadable`，**不许当读过**（也不许直接给 full_text）。"""
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    (root / "03_全文" / "pdf").mkdir(parents=True)
    (root / "03_全文" / "pdf" / "IEEE_11181461.pdf").write_bytes(b"%PDF-1.4")
    lit_cli.ingest_course(root)
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "unreadable", refs[0]


def test_search_only_snippet_is_abstract_only_with_note(tmp: Path) -> None:
    root = course(tmp / "c")
    (root / "01_检索" / "ieee" / "raw" / "ieee_search-1.json").write_text(
        json.dumps({"count": 1, "results": [{"keyword": "x", "items": [
            {"arnumber": "9", "title": "T", "url": "u", "snippet": "..."}]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    lit_cli.ingest_course(root)
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "abstract_only"
    assert "snippet" in refs[0]["tier_note"]


# ───────────────────────────────────────── ② 人工档位优先（关键）

def test_manual_tier_is_not_overwritten(tmp: Path) -> None:
    """`tier_source: "manual"` 的记录，重跑 ingest **不许被推断值覆盖**。"""
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps([{"id": "IEEE:11181461", "title": "T", "tier": "source_conflict",
                     "tier_source": "manual", "tier_note": "摘要与正文对不上"}], ensure_ascii=False),
        encoding="utf-8",
    )
    lit_cli.ingest_course(root)
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    r = [x for x in refs if x["id"] == "IEEE:11181461"][0]
    assert r["tier"] == "source_conflict", r
    assert r["tier_source"] == "manual"


# ───────────────────────────────────────── ③ 跨源去重

def test_cross_source_dedupe_by_doi(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    w_wf_detail(root, [{**WF_ROW, "doi": "10.1109/X.2025.1"}])  # 同 DOI
    res = lit_cli.ingest_course(root)
    assert res["kept"] == 1, res
    assert res["duplicates"] and res["duplicates"][0]["why"] == "DOI 相同"


def test_dedupe_keeps_higher_tier(tmp: Path) -> None:
    """同 DOI 两条，保留档位更高的那条。"""
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    w_wf_detail(root, [{**WF_ROW, "doi": "10.1109/X.2025.1"}])
    (root / "03_全文" / "text").mkdir(parents=True)
    (root / "03_全文" / "text" / "IEEE_11181461.txt").write_text("x", encoding="utf-8")
    lit_cli.ingest_course(root)
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert len(refs) == 1 and refs[0]["tier"] == "full_text", refs


# ───────────────────────────────────────── ④ 只读 / 幂等 / dry-run

def test_raw_zone_is_untouched(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    f = root / "01_检索" / "ieee" / "raw" / "ieee_detail-20261006.json"
    before = f.read_bytes()
    lit_cli.ingest_course(root)
    assert f.read_bytes() == before, "raw/ 是只读证据区，ingest 不许改它"


def test_ingest_is_idempotent(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    lit_cli.ingest_course(root)
    a = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    lit_cli.ingest_course(root)
    b = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert a == b


def test_dry_run_writes_nothing(tmp: Path) -> None:
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    res = lit_cli.ingest_course(root, dry_run=True)
    assert res["collected"] == 1 and res["written"] is False
    assert not (root / "02_候选库" / "refs.json").exists()


# ───────────────────────────────────────── ⑤ 与 audit 接线

def test_ingest_output_feeds_audit(tmp: Path) -> None:
    """ingest 出来的 refs.json 应能被 audit 直接消费（不再报 AUDIT_NO_REFS）。"""
    root = course(tmp / "c")
    w_ieee_detail(root, [IEEE_ROW])
    lit_cli.ingest_course(root)
    res = lit_cli.check_audit(root)
    assert "AUDIT_NO_REFS" not in {f.code for f in res.findings}


if __name__ == "__main__":
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"i{i:02d}"
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
