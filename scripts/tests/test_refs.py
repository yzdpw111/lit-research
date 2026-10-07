#!/usr/bin/env python3
"""`lit refs` 的离线单测。

★ 为什么必须有：`lit refs` 一度是**零测试**——而我自己立的规矩是
  "新检查器必须有判别力"。它当时已经出过一次真 bug（会议论文被标成 `[J]`）。

覆盖：
  · **载体代码**（`[J]`/`[C]`/`[D]`）——会议与期刊必须分开（曾经全标 `[J]`）
  · **编号顺序**：引用序（默认）/ 年份 / ID；未被引用的排在末尾并列出
  · **作者规范化**：`SRIKANTH V` 式；中文姓名原样；`>3` 位用 `等`/`et al.`
  · **作者顺序为推断时必须标注**（不许悄悄猜）
  · **缺字段登记，不许编卷(期):页码**
  · **已排除的条目不进表**
  · **硬指标数字正确**

跑法：python scripts/tests/test_refs.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import lit_cli  # noqa: E402


def course(root: Path, refs) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "02_候选库").mkdir(exist_ok=True)
    (root / "06_综述").mkdir(exist_ok=True)
    (root / "lit.config.json").write_text(
        json.dumps({"sources": ["ieee"], "tiers": list(lit_cli.TIER_FALLBACK),
                    "capacity": {"refs_min": 2, "now_year": 2026, "recent_years": 5,
                                 "recent_ratio_min": 0.5, "foreign_ratio_min": 0.5}},
                   ensure_ascii=False), encoding="utf-8")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def R(id_, **kw):
    base = {"id": id_, "title": f"Title {id_}", "authors": ["Nimalika Tiwari"],
            "year": 2025, "lang": "en", "tier": "abstract_only", "type": "期刊论文",
            "venue": "IEEE Access"}
    base.update(kw)
    return base


# ─────────────────────────────── ① 载体代码：会议 ≠ 期刊（曾经的 bug）

def test_conference_gets_C_not_J(tmp: Path) -> None:
    """会议论文必须标 `[C]`——曾经所有 IEEE 记录被硬编码成期刊，全标 `[J]`。"""
    root = course(tmp / "c", [
        R("IEEE:1", type="期刊论文", venue="IEEE Access"),
        R("IEEE:2", type="会议论文", venue="2025 4th International Conference on X"),
        R("WF:3", type="硕士论文", venue="某大学", lang="zh"),
    ])
    res = lit_cli.build_refs(root, order="id")
    txt = {e["id"]: e["text"] for e in res["entries"]}
    assert "[J]" in txt["IEEE:1"] and "IEEE Access" in txt["IEEE:1"], txt["IEEE:1"]
    assert "[C]//" in txt["IEEE:2"], txt["IEEE:2"]
    assert "[D]" in txt["WF:3"], txt["WF:3"]


def test_ieee_detail_type_inference() -> None:
    """`publishedIn` 里的关键词决定载体类型。"""
    assert lit_cli.guess_type_from_venue("IEEE Access") == "期刊论文"
    assert lit_cli.guess_type_from_venue("2025 IEEE Symposium on X") == "会议论文"
    assert lit_cli.guess_type_from_venue("Proceedings of the IEEE") == "会议论文"


# ─────────────────────────────── ② 编号顺序

def test_citation_order_follows_text(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1"), R("IEEE:2"), R("IEEE:3")])
    (root / "06_综述" / "综述.md").write_text(
        "先讲 [IEEE:3]，再讲 [IEEE:1]。\n[IEEE:2] 又在后面。\n", encoding="utf-8")
    res = lit_cli.build_refs(root, order="citation")
    order = [e["id"] for e in res["entries"]]
    assert order == ["IEEE:3", "IEEE:1", "IEEE:2"], order
    assert res["uncited"] == []


def test_uncited_listed_last(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1"), R("IEEE:2"), R("IEEE:9")])
    (root / "06_综述" / "综述.md").write_text("引 [IEEE:2] 与 [IEEE:1]。\n", encoding="utf-8")
    res = lit_cli.build_refs(root, order="citation")
    assert [e["id"] for e in res["entries"]] == ["IEEE:2", "IEEE:1", "IEEE:9"]
    assert res["uncited"] == ["IEEE:9"]


def test_no_review_file_falls_back_gracefully(tmp: Path) -> None:
    """没有综述文件时不该崩，且所有条目都算未引用。"""
    root = course(tmp / "c", [R("IEEE:1"), R("IEEE:2")])
    res = lit_cli.build_refs(root, order="citation")
    assert len(res["entries"]) == 2
    assert len(res["uncited"]) == 2


def test_year_and_id_orders(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1", year=2020), R("IEEE:2", year=2026)])
    assert [e["id"] for e in lit_cli.build_refs(root, order="year")["entries"]] == ["IEEE:2", "IEEE:1"]
    assert [e["id"] for e in lit_cli.build_refs(root, order="id")["entries"]] == ["IEEE:1", "IEEE:2"]


# ─────────────────────────────── ③ 作者规范化

def test_english_authors_are_uppercased_family_first() -> None:
    s, inf = lit_cli._gb_authors(["Nimalika Tiwari", "Wei He"], "en")
    assert s == "TIWARI N, HE W", s
    assert inf == 2, "西式假设应记为推断"


def test_initial_form_is_not_inferred() -> None:
    s, inf = lit_cli._gb_authors(["A. One"], "en")
    assert s == "ONE A", s
    assert inf == 0, "已带首字母的形式不算推断"


def test_chinese_names_kept() -> None:
    s, inf = lit_cli._gb_authors(["张三", "李四"], "zh")
    assert s == "张三, 李四", s
    assert inf == 0


def test_more_than_three_authors_uses_deng() -> None:
    s, _ = lit_cli._gb_authors(["A. One", "B. Two", "C. Three", "D. Four"], "en")
    assert s.endswith("et al"), s
    s2, _ = lit_cli._gb_authors(["张三", "李四", "王五", "赵六"], "zh")
    assert s2.endswith("等"), s2


def test_inferred_author_order_is_flagged_in_entry(tmp: Path) -> None:
    """★ 推断必须**在条目里被登记**——不许悄悄猜（S-04 §1）。"""
    root = course(tmp / "c", [R("IEEE:1", authors=["Nimalika Tiwari"])])
    res = lit_cli.build_refs(root, order="id")
    probs = " ".join(res["entries"][0]["problems"])
    assert "推断" in probs and "复核" in probs, probs
    assert res["au_inferred"] >= 1


# ─────────────────────────────── ④ 缺字段：登记，不编造

def test_missing_venue_is_registered_not_invented(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1", venue="")])
    res = lit_cli.build_refs(root, order="id")
    probs = " ".join(res["entries"][0]["problems"])
    assert "缺刊名" in probs, probs
    assert "卷(期)" in probs, "缺卷期页码必须登记"
    # **条目里不许出现编造的卷期页**
    assert "2025,1(" not in res["entries"][0]["text"]


def test_missing_year_registered(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1", year=None)])
    res = lit_cli.build_refs(root, order="id")
    assert "缺年份" in " ".join(res["entries"][0]["problems"])


# ─────────────────────── ⑥ ★ 可疑 DOI：自洽的必须保留（初版粗规则会误丢）

def test_self_consistent_thesis_doi_is_kept(tmp: Path) -> None:
    """`10.7666/D03227905` 里含它自己的 ID → **可信**，必须**保留**进著录。

    ★ 这是实测修正：初版规则"学位论文带 DOI 就报可疑"**太粗**，
      在真实数据上会把这一个**正确**的 DOI 误判并丢掉。
    """
    rid, doi = "WF:D03227905", "10.7666/D03227905"
    assert lit_cli.doi_looks_self_consistent(rid, doi) is True
    root = course(tmp / "c", [R(rid, type="硕士论文", lang="zh", doi=doi, venue="某大学")])
    res = lit_cli.build_refs(root, order="id")
    text = res["entries"][0]["text"]
    assert doi in text, text
    assert "已省略" not in " ".join(res["entries"][0]["problems"])
    assert not lit_cli.check_suspect_doi(
        [{"id": rid, "type": "硕士论文", "doi": doi, "lang": "zh"}]
    ), "自洽的 DOI 不该被报可疑"


def test_inconsistent_thesis_doi_is_omitted_and_flagged(tmp: Path) -> None:
    """中国学位论文挂着 Elsevier 的 DOI（实测：来源把参考文献里第一篇的 DOI 当成了本文的）。"""
    rid, doi = "WF:D03561632", "10.1016/j.measurement.2021.109273"
    assert lit_cli.doi_looks_self_consistent(rid, doi) is False
    root = course(tmp / "c", [R(rid, type="硕士论文", lang="zh", doi=doi, venue="某大学")])
    res = lit_cli.build_refs(root, order="id")
    text = res["entries"][0]["text"]
    assert doi not in text, f"错 DOI 不许写进著录：{text}"
    assert "已省略" in " ".join(res["entries"][0]["problems"]), res["entries"][0]["problems"]
    findings = lit_cli.check_suspect_doi(
        [{"id": rid, "type": "硕士论文", "doi": doi, "lang": "zh"}]
    )
    assert findings and findings[0].code == "AUDIT_SUSPECT_DOI"


# ─────────────────────────────── ⑤ 排除 / 硬指标

def test_excluded_not_in_table(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1"), R("IEEE:2", excluded=True,
                                             excluded_reason="缺年份")])
    res = lit_cli.build_refs(root, order="id")
    assert [e["id"] for e in res["entries"]] == ["IEEE:1"]
    assert res["stats"]["excluded"] == 1


def test_stats_numbers(tmp: Path) -> None:
    root = course(tmp / "c", [
        R("IEEE:1", year=2026, lang="en"),
        R("IEEE:2", year=2020, lang="en"),
        R("WF:3", year=2025, lang="zh"),
    ])
    s = lit_cli.build_refs(root, order="id")["stats"]
    assert s["total"] == 3
    assert s["recent_n"] == 2 and abs(s["recent_ratio"] - 2 / 3) < 1e-9
    assert s["foreign_n"] == 2
    assert s["year_range"] == [2020, 2026]
    assert s["meets_min"] is True


def test_refs_is_readonly(tmp: Path) -> None:
    root = course(tmp / "c", [R("IEEE:1")])
    (root / "06_综述" / "综述.md").write_text("[IEEE:1]\n", encoding="utf-8")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    lit_cli.build_refs(root, order="citation")
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after


if __name__ == "__main__":
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"r{i:02d}"
        d.mkdir(parents=True, exist_ok=True)
        try:
            # ★ 本文件既有"要 tmp 目录"的测试，也有**纯函数**测试（零参数）——
            #   初版 runner 一律 `fn(d)`，于是 5 个纯函数测试全报 TypeError。
            fn() if fn.__code__.co_argcount == 0 else fn(d)
            print(f"PASS  {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            fail += 1
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n{ok}/{ok + fail} 通过")
    raise SystemExit(1 if fail else 0)
