#!/usr/bin/env python3
"""`lit plan` / `lit note` 的离线单测。

关键性质：
  - `plan` **不猜硬线**：不给 `--refs-min` / `--now-year` 就必须报错（S-02 §1.2 / §2.2 / §3.1）；
  - `plan` 写出的 config 能被 `audit` 直接消费；
  - `note` 抽出文字 → `full_text`；**抽不出 → `unreadable`，且不写空文件**（S-04 §3）；
  - 两者都**不覆盖人工档位**（`tier_source: "manual"`）。

`note` 依赖 PyMuPDF；缺库时那几条会被跳过并打印 SKIP（不算失败）。

跑法：python scripts/tests/test_plan_note.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import lit_cli  # noqa: E402

try:
    import fitz  # noqa: F401

    HAS_FITZ = True
except Exception:
    HAS_FITZ = False


def bare(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "02_候选库").mkdir(exist_ok=True)
    return root


def make_pdf(path: Path, text: str = "", pages: int = 1) -> None:
    """造一个真 PDF：有文字层 → 可抽；无文字 → 模拟扫描件。"""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


# ───────────────────────────────────────── ① plan：不许猜

def test_plan_refuses_to_guess(tmp: Path) -> None:
    res = lit_cli.plan_course(bare(tmp / "c"))
    assert res["ok"] is False
    assert any("refs-min" in p for p in res["problems"]), res
    assert any("now-year" in p for p in res["problems"]), res
    assert not (tmp / "c" / "lit.config.json").exists(), "拒绝了就不该写文件"


def test_plan_writes_usable_config(tmp: Path) -> None:
    root = bare(tmp / "c")
    res = lit_cli.plan_course(root, refs_min=40, now_year=2026,
                             recent_ratio=1 / 3, foreign_ratio=1 / 3)
    assert res["ok"] and res["written"]
    cfg = json.loads((root / "lit.config.json").read_text(encoding="utf-8"))
    assert cfg["capacity"]["refs_min"] == 40
    assert cfg["capacity"]["now_year"] == 2026
    # audit 应当不再报 AUDIT_NO_CONFIG
    assert "AUDIT_NO_CONFIG" not in {f.code for f in lit_cli.check_audit(root).findings}


# ───────────── 定位修正：这是通用研究规范，不是学位论文专用 ─────────────

def test_plan_error_is_not_thesis_bound(tmp: Path) -> None:
    """★ 报错**不许**假定"学位论文 / 学校 / 学历层级"。

    容量可来自外部规范，**也可以由使用者自己承诺**。
    曾经报错里写死"查你所在学校的规范原文。例：南理工硕士 ≥40、博士 ≥80"。
    """
    res = lit_cli.plan_course(bare(tmp / "c"))
    joined = " ".join(res["problems"])
    for banned in ("南理工", "所在学校", "本科", "硕士", "博士"):
        assert banned not in joined, f"报错里不该出现「{banned}」：{joined}"
    assert "外部规范" in joined and "你自己承诺" in joined, joined


def test_plan_records_capacity_source(tmp: Path) -> None:
    """`capacity.source` 记下"这个数从哪来" —— 检查的是承诺 vs 兑现。"""
    root = bare(tmp / "c")
    res = lit_cli.plan_course(root, refs_min=40, now_year=2026,
                             capacity_source="自定义（预研摸底）")
    assert res["ok"] and res["written"]
    cfg = json.loads((root / "lit.config.json").read_text(encoding="utf-8"))
    assert cfg["capacity"]["source"] == "自定义（预研摸底）"


def test_refs_style_other_than_gb7714_is_refused(tmp: Path) -> None:
    """★ 选了非 gb7714 的风格 → **明确说"我没做"**，绝不假装支持。"""
    import argparse
    root = bare(tmp / "c")
    (root / "02_候选库" / "refs.json").write_text("[]", encoding="utf-8")
    (root / "lit.config.json").write_text(
        json.dumps({"refs_style": "apa", "capacity": {"refs_min": 1}}, ensure_ascii=False),
        encoding="utf-8")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = lit_cli.cmd_refs(argparse.Namespace(dir=str(root), scan=None, order="id",
                                                 write=False, json=False))
    out = buf.getvalue()
    assert rc == 2, "非 gb7714 必须返回非零，而不是照样输出国标"
    assert "只实现了 gb7714" in out, out


def test_plan_keeps_existing_keys(tmp: Path) -> None:
    root = bare(tmp / "c")
    (root / "lit.config.json").write_text(
        json.dumps({"tiers": list(lit_cli.TIER_FALLBACK), "sources": ["arxiv"], "custom": 1},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    lit_cli.plan_course(root, refs_min=10, now_year=2026)
    cfg = json.loads((root / "lit.config.json").read_text(encoding="utf-8"))
    assert cfg["sources"] == ["arxiv"], "已有来源不该被覆盖"
    assert cfg["custom"] == 1


def test_plan_reports_unchecked_items(tmp: Path) -> None:
    res = lit_cli.plan_course(bare(tmp / "c"), refs_min=40, now_year=2026)
    joined = " ".join(res["not_checked"])
    assert "近两年" in joined and "相关" in joined, joined


# ───────────────────────────────────────── ② note：抽出 / 抽不出

def test_note_extracts_and_sets_full_text(tmp: Path) -> None:
    if not HAS_FITZ:
        print("      SKIP (no PyMuPDF)")
        return
    root = bare(tmp / "c")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps([{"id": "IEEE:1", "title": "T", "tier": "abstract_only",
                     "tier_source": "ingest"}], ensure_ascii=False), encoding="utf-8")
    make_pdf(root / "03_全文" / "pdf" / "IEEE_1.pdf", text="Hello world, this is a test paper.", pages=2)
    res = lit_cli.note_extract(root, "IEEE:1")
    assert res["ok"] and res["tier"] == "full_text", res
    txt = Path(res["text_path"]).read_text(encoding="utf-8")
    assert "=== PAGE 1 ===" in txt and "=== PAGE 2 ===" in txt, "必须带页码标记"
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "full_text"


def test_note_unreadable_when_no_text_layer(tmp: Path) -> None:
    """扫描件：抽不出文字 → `unreadable`，**不写空文件**，**不许给 full_text**。"""
    if not HAS_FITZ:
        print("      SKIP (no PyMuPDF)")
        return
    root = bare(tmp / "c")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps([{"id": "IEEE:2", "title": "T", "tier": "abstract_only",
                     "tier_source": "ingest"}], ensure_ascii=False), encoding="utf-8")
    make_pdf(root / "03_全文" / "pdf" / "IEEE_2.pdf", text="", pages=1)
    res = lit_cli.note_extract(root, "IEEE:2")
    assert res["ok"] is False and res["tier"] == "unreadable", res
    assert res["text_path"] is None, "抽不出文字时不许写空文件"
    assert not (root / "03_全文" / "text" / "IEEE_2.txt").exists()
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "unreadable"


def test_note_manual_tier_not_overwritten(tmp: Path) -> None:
    if not HAS_FITZ:
        print("      SKIP (no PyMuPDF)")
        return
    root = bare(tmp / "c")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps([{"id": "IEEE:3", "title": "T", "tier": "source_conflict",
                     "tier_source": "manual"}], ensure_ascii=False), encoding="utf-8")
    make_pdf(root / "03_全文" / "pdf" / "IEEE_3.pdf", text="Some extractable text here.", pages=1)
    lit_cli.note_extract(root, "IEEE:3")
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "source_conflict", "人工档位不许被 note 覆盖"


def test_note_missing_pdf_tells_you_where(tmp: Path) -> None:
    root = bare(tmp / "c")
    res = lit_cli.note_extract(root, "IEEE:404")
    assert res["ok"] is False
    joined = " ".join(res["problems"])
    # 消息已改为更准确的版本：明说「不按题名猜」并给出规范路径
    assert "03_全文" in joined, res
    assert "不按题名猜" in joined or "ID 形式" in joined, res


if __name__ == "__main__":
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"p{i:02d}"
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
