#!/usr/bin/env python3
"""`lit link` / `resolve_pdf` / `note` 的 PDF 关联测试。

★ 背景（实测教训）：上游按**题名**命名下载文件，规范要求**ID** 命名。
  我曾**手工用题名模糊匹配**，结果**配错一篇**并在修补时**删掉它**。
  根因是"规范没给自动化手段"，所以补了 `lit link`——**只用下载日志的权威映射**。

**判别力要求**：必须有"**两篇题名高度相似**"的用例，
证明映射来自日志而**不是题名匹配**；映射不上时必须**报错，不许猜**。

跑法：python scripts/tests/test_link.py
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
    import fitz

    HAS_FITZ = True
except Exception:
    HAS_FITZ = False


def course(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "01_检索" / "ieee" / "raw").mkdir(parents=True)
    (root / "02_候选库").mkdir(exist_ok=True)
    (root / "03_全文" / "pdf").mkdir(parents=True)
    (root / "lit.config.json").write_text(
        json.dumps({"sources": ["ieee"], "tiers": list(lit_cli.TIER_FALLBACK),
                    "capacity": {"refs_min": 1}}, ensure_ascii=False), encoding="utf-8")
    return root


def w_log(root: Path, rows) -> None:
    (root / "01_检索" / "ieee" / "raw" / "ieee_paper_download-20261007.json").write_text(
        json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False), encoding="utf-8")


def dl(arn: str, name: str, path: str) -> dict:
    return {"arnumber": arn, "download": {"name": name, "path": path, "size": 123}}


def make_pdf(p: Path, text: str = "extractable text for testing purposes") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text, fontsize=11)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(p))
    doc.close()


# ─────────────────────────────── ① 用日志把题名文件入库成 ID 名

def test_link_imports_by_log(tmp: Path) -> None:
    root = course(tmp / "c")
    outside = tmp / "c" / "downloads" / "Some Long Paper Title.pdf"
    make_pdf(outside)
    w_log(root, [dl("11111111", outside.name, str(outside))])
    res = lit_cli.link_pdfs(root)
    assert not res["missing"], res
    assert (root / "03_全文" / "pdf" / "IEEE_11111111.pdf").is_file(), res
    assert res["linked"] and res["linked"][0]["id"] == "IEEE:11111111"


def test_link_does_not_overwrite_existing(tmp: Path) -> None:
    root = course(tmp / "c")
    dst = root / "03_全文" / "pdf" / "IEEE_22222222.pdf"
    dst.write_bytes(b"%PDF-1.4 existing")
    outside = tmp / "c" / "downloads" / "T.pdf"
    make_pdf(outside)
    w_log(root, [dl("22222222", outside.name, str(outside))])
    res = lit_cli.link_pdfs(root)
    assert dst.read_bytes() == b"%PDF-1.4 existing", "已存在的 ID 命名的 PDF 不许被覆盖"
    assert "IEEE:22222222" in res["already"], res


# ─────────────────────────────── ② ★ 关键：两篇题名相似，靠日志分对

def test_similar_titles_are_mapped_by_log_not_title(tmp: Path) -> None:
    """**这是本文件最重要的用例。**

    两篇题名高度相似（实测中就有两篇几乎同名的学位论文），
    若按题名匹配必然出错；按**日志映射**则两条都正确。
    """
    root = course(tmp / "c")
    a = tmp / "c" / "downloads" / "Lithium-Ion Battery State of Health Estimation Based on Reconstructed Features.pdf"
    b = tmp / "c" / "downloads" / "Lithium-Ion Battery State of Health Estimation Based on Reconstructed Features and Fused Neural Networks.pdf"
    make_pdf(a)
    make_pdf(b)
    w_log(root, [dl("10000001", a.name, str(a)), dl("10000002", b.name, str(b))])
    res = lit_cli.link_pdfs(root)
    assert not res["missing"], res
    map_ = {x["id"]: x["from"] for x in res["linked"]}
    assert map_["IEEE:10000001"] != map_["IEEE:10000002"], map_
    # 各自指向自己的那篇（按日志，不按题名）
    assert "Reconstructed Features.pdf" in map_["IEEE:10000001"]
    assert map_["IEEE:10000002"].endswith("Neural Networks.pdf")
    for i in ("10000001", "10000002"):
        assert (root / "03_全文" / "pdf" / f"IEEE_{i}.pdf").is_file()


# ─────────────────────────────── ③ 映射不上时：报错，不许猜

def test_missing_logged_file_is_reported_not_guessed(tmp: Path) -> None:
    root = course(tmp / "c")
    w_log(root, [dl("33333333", "Gone.pdf", str(tmp / "c" / "downloads" / "Gone.pdf"))])
    res = lit_cli.link_pdfs(root)
    assert res["missing"] and res["missing"][0]["id"] == "IEEE:33333333"
    assert not list((root / "03_全文" / "pdf").glob("*.pdf")), "不许凭空造文件"


def test_no_log_entry_no_title_matching(tmp: Path) -> None:
    """目录里有一个**题名命名的 PDF**，但日志里没有这条 → `note` 必须报错，**不许认它**。"""
    root = course(tmp / "c")
    make_pdf(root / "03_全文" / "pdf" / "Some Title That Looks Right.pdf")
    found, notes = lit_cli.resolve_pdf(root, "IEEE:99999999")
    assert found is None, "没有日志映射就不许用题名匹配，即便目录里有个像的"
    assert any("不按题名猜" in n for n in notes), notes


def test_resolve_prefers_canonical(tmp: Path) -> None:
    root = course(tmp / "c")
    dst = root / "03_全文" / "pdf" / "IEEE_44444444.pdf"
    make_pdf(dst)
    outside = tmp / "c" / "downloads" / "Other.pdf"
    make_pdf(outside)
    w_log(root, [dl("44444444", "Other.pdf", str(outside))])
    found, _ = lit_cli.resolve_pdf(root, "IEEE:44444444")
    assert found == dst, "规范位置优先于日志路径"


def test_resolve_falls_back_to_log_when_not_imported(tmp: Path) -> None:
    """还没 `link` 时，`note` 也应能**凭日志**找到题名命名的文件。"""
    root = course(tmp / "c")
    outside = tmp / "c" / "downloads" / "Title Named.pdf"
    make_pdf(outside)
    w_log(root, [dl("55555555", outside.name, str(outside))])
    found, notes = lit_cli.resolve_pdf(root, "IEEE:55555555")
    assert found == outside, (found, notes)


# ─────────────────────────────── ④ 端到端：link → note 升级档位

def test_link_then_note_upgrades_tier(tmp: Path) -> None:
    if not HAS_FITZ:
        print("      SKIP (no PyMuPDF)")
        return
    root = course(tmp / "c")
    (root / "02_候选库" / "refs.json").write_text(
        json.dumps([{"id": "IEEE:66666666", "title": "T", "tier": "abstract_only",
                     "tier_source": "ingest"}], ensure_ascii=False), encoding="utf-8")
    outside = tmp / "c" / "downloads" / "A Title Named File.pdf"
    make_pdf(outside, text="Real extractable full text for this battery paper test. " * 4)
    w_log(root, [dl("66666666", outside.name, str(outside))])
    lit_cli.link_pdfs(root)
    res = lit_cli.note_extract(root, "IEEE:66666666")
    assert res["ok"] and res["tier"] == "full_text", res
    refs = json.loads((root / "02_候选库" / "refs.json").read_text(encoding="utf-8"))
    assert refs[0]["tier"] == "full_text"


def test_link_respects_seal_warning(tmp: Path) -> None:
    root = course(tmp / "c")
    outside = tmp / "c" / "downloads" / "T2.pdf"
    make_pdf(outside)
    w_log(root, [dl("77777777", outside.name, str(outside))])
    lit_cli.write_seal(root)
    res = lit_cli.link_pdfs(root)
    assert res["sealed_needs_reseal"] is True, "入库进只读区后必须提示重新封存"


if __name__ == "__main__":
    tmp_root = HERE.parents[2] / ".tmp-test"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for i, (name, fn) in enumerate(tests):
        d = tmp_root / f"l{i:02d}"
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
