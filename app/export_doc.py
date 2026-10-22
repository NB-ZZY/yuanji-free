"""把一批观察记录表导成 Word（.docx），老师可以直接打印或交给园里。"""
import json
import time
from pathlib import Path

import db
import plat
from config import DATA_DIR
from knowledge import grade_score

LEVEL_CN = {1: "小班（3-4岁）", 3: "中班（4-5岁）", 5: "大班（5-6岁）", 128: "托班（24—36个月）"}


def _set_cn_font(run, name="仿宋", size=None):
    from docx.oxml.ns import qn
    from docx.shared import Pt
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size:
        run.font.size = Pt(size)


def batch_to_docx(batch_id: int, out_path: str = None, only_ids=None) -> str:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    b = db.one("SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise ValueError("这一批不在了")
    cls = db.one("SELECT * FROM classes WHERE class_id=?", (b["class_id"],))
    items = db.rows("SELECT * FROM items WHERE batch_id=? ORDER BY id", (batch_id,))
    if only_ids:
        want = {int(x) for x in only_ids}
        items = [i for i in items if int(i["id"]) in want]
    items = [i for i in items if i.get("table_json") or i.get("text")]
    if not items:
        raise ValueError("选中的这些还没写好内容，先点「AI 生成」把内容写出来再导出")
    is_table = (b.get("mode") == "table")
    title = "幼儿观察记录表" if is_table else "幼儿观察记录"

    from docx.oxml.ns import qn
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "仿宋"
    st.font.size = Pt(11)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "仿宋")

    for n, it in enumerate(items):
        if n:
            doc.add_page_break()
        h = doc.add_paragraph()
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = h.add_run(title)
        r.bold = True
        _set_cn_font(r, "黑体", 18)

        t = doc.add_table(rows=2, cols=4)
        t.style = "Table Grid"
        age_level = it.get("level") or (cls or {}).get("level") or 5
        if (cls or {}).get("level") == 128:
            age_level = 128  # 修正旧版曾把托班条目标记为中班的年龄标签。
        grade_score(age_level)  # 拒绝未知月龄档，不导出含糊或错误的年龄标签。
        vals = [("姓名", it.get("student_name") or "待补充"),
                ("班级 / 年龄", f"{cls['name'] if cls else ''}  {LEVEL_CN.get(age_level, '')}"),
                ("观察时间", it.get("record_date") or b.get("record_date") or ""),
                ("观察情境", b.get("note") or "待补充")]
        for c, (k, v) in enumerate(vals):
            t.cell(0, c).text = k
            t.cell(1, c).text = str(v)
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in p.runs:
                        _set_cn_font(run, "仿宋", 10.5)

        try:
            import pipeline as _pl
            imgs = _pl.item_paths(it)
            if imgs:
                doc.add_paragraph()
                pp = doc.add_paragraph()
                pp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if len(imgs) == 1:
                    pp.add_run().add_picture(imgs[0], width=Cm(9))
                else:
                    # 多张照片：两张一行，各占一半宽，看着不挤
                    w = Cm(7.2) if len(imgs) == 2 else Cm(5.4)
                    for n, ip in enumerate(imgs[:6]):
                        pp.add_run().add_picture(ip, width=w)
                        pp.add_run("  ")
        except Exception:
            pass

        data = {}
        if it.get("table_json"):
            try:
                data = json.loads(it["table_json"])
            except Exception:
                data = {}
        if not data:
            fallback = it.get("text") or ""
            if is_table:
                import record_table
                fallback = record_table.clean_analysis(fallback)
            data = {"观察内容": fallback}

        if not is_table:
            # 随手记本来就是给家长看的一小段话，没有四段结构 ——
            # 导出就照原样一段话，别给它硬安上【观察内容】这种小标题。
            one = (data.get("观察内容") or it.get("text") or "").strip()
            if one:
                for para in one.split("\n"):
                    q = doc.add_paragraph()
                    q.paragraph_format.first_line_indent = Pt(24)
                    _set_cn_font(q.add_run(para.strip()), "仿宋", 11)
            continue

        import record_table
        for key, v in record_table.sections(data):
            if not v:
                continue
            if key is not None:
                hp = doc.add_paragraph()
                hr = hp.add_run(f"【{key}】")
                hr.bold = True
                _set_cn_font(hr, "黑体", 12)
            if isinstance(v, list):
                for i, one in enumerate(v):
                    p = doc.add_paragraph()
                    p.paragraph_format.first_line_indent = Pt(24)
                    _set_cn_font(p.add_run(str(one) if key == '回应策略' else f"{i+1}. {one}"), "仿宋", 11)
            else:
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Pt(24)
                _set_cn_font(p.add_run(str(v)), "仿宋", 11)

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        d = plat.export_root()
        day = (b.get("record_date") or time.strftime("%Y-%m-%d"))
        who = ((cls or {}).get("name") or "").strip()
        one = items[0].get("student_name") if len(items) == 1 else ""
        base = "_".join(x for x in (who, (one or ""), title.replace("幼儿", ""), str(day)) if x)
        out = _free_name(d, base)
    doc.save(str(out))
    return str(out)


def _free_name(d: Path, base: str) -> Path:
    """同一天导第二次不覆盖上一份，自动排成 (2)(3)。"""
    base = "".join(c for c in base if c not in '\\/:*?"<>|').strip() or "观察记录"
    out = d / f"{base}.docx"
    n = 2
    while out.exists():
        out = d / f"{base}({n}).docx"
        n += 1
    return out
