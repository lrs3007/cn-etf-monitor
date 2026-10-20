"""Build a read-only, reproducible narrative report for all active index factors.

Usage: .venv/bin/python scripts/analyze-factors.py [--output artifacts/factor-analysis]
No collection, database writes, publishing, or trade recommendations occur here.
"""
import argparse
import html
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import DATABASE_URL, INDICES, now_iso  # noqa: E402
from backend.app.factor_analysis import ANALYSIS_VERSION, analyze_factor, outcome_text, MIN_REFERENCE_PERIODS  # noqa: E402


def h(value):
    return html.escape(str(value), quote=True)


def num(value, places=2):
    return "—" if value is None else f"{value:,.{places}f}"


def pct(value, signed=False):
    return "—" if value is None else f"{value * 100:{'+' if signed else ''}.2f}%"


def pp(value):
    return "—" if value is None else f"{value * 100:+.2f}个百分点"


def read_report():
    if not DATABASE_URL.startswith("sqlite:///"):
        raise ValueError("此只读报告入口当前使用SQLite；请勿将其他数据库地址当作文件打开")
    path = Path(DATABASE_URL.removeprefix("sqlite:///"))
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        indices = []
        for spec in INDICES:
            snapshot = connection.execute("SELECT s.* FROM market_indices m JOIN data_snapshots s ON s.id=m.active_snapshot WHERE m.code=? AND s.status='published'", (spec.code,)).fetchone()
            if not snapshot:
                raise ValueError(f"{spec.code}没有完整快照")
            coverage = json.loads(snapshot["coverage"])
            bars = connection.execute("SELECT trade_date,close FROM daily_bars WHERE snapshot_id=? ORDER BY trade_date", (snapshot["id"],)).fetchall()
            dates, close = [b[0] for b in bars], [b[1] for b in bars]
            if len(dates) != coverage["rows"] or dates[-1] != coverage["last_date"]:
                raise ValueError(f"{spec.code}行情覆盖不完整")
            factors = []
            for fid, definition in coverage["factor_definitions"].items():
                values = connection.execute("SELECT trade_date,value FROM factor_values WHERE snapshot_id=? AND factor_id=? ORDER BY trade_date", (snapshot["id"], fid)).fetchall()
                if [v[0] for v in values] != dates:
                    raise ValueError(f"{spec.code}/{fid}日期未对齐")
                evaluations = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT horizon,result FROM factor_evaluations WHERE snapshot_id=? AND factor_id=? ORDER BY horizon", (snapshot["id"], fid))}
                analysis = analyze_factor(dates, close, [v[1] for v in values], definition, evaluations, spec.launch_date)
                analysis["coverage"] = coverage["factor_coverage"][fid]
                factors.append(analysis)
            indices.append({"code": spec.code, "name": spec.short_name, "snapshot_id": snapshot["id"],
                "snapshot_created_at": snapshot["created_at"], "engine_version": snapshot["engine_version"],
                "dataset_hash": snapshot["dataset_hash"], "first_date": dates[0], "as_of": dates[-1],
                "launch_date": spec.launch_date, "price": close[-1], "factors": factors})
        return {"schema_version": 1, "analysis_version": ANALYSIS_VERSION, "generated_at": now_iso(), "indices": indices,
            "method": {"position_window_sessions": 1260, "position_includes_signal_day": True,
                "tie_rule": "at_or_below", "match_band_percentage_points": 5, "horizons": "each factor's stored horizons",
                "return_basis": "price_index_close_to_close", "exclude_prelaunch_signals": True,
                "non_overlapping_rule": "earliest signal first, next start >= previous start + horizon",
                "minimum_reference_periods": MIN_REFERENCE_PERIODS,
                "status": "retrospective descriptive analysis; no significance claim or out-of-sample strategy"}}
    finally:
        connection.close()


def render_factor(index, factor):
    default = next((o for o in factor["outcomes"] if o["horizon"] == 20), None)
    unit = factor["unit"] if factor["current"] is not None else ""
    position = "—" if factor["percentile"] is None else num(factor["percentile"], 1) + "%"
    bounds = factor["match_bounds"]
    match_text = f"{bounds[0]:.1f}%—{bounds[1]:.1f}%" if bounds else "无可用区间"
    table_rows = []
    detail_blocks = []
    for o in factor["outcomes"]:
        stats, sampled, base = o["all_matches"], o["non_overlapping"], o["baseline"]
        status = "无样本" if o["sample_status"] == "no_matches" else "参考有限" if o["sample_status"] == "limited" else "描述统计"
        table_rows.append(f"<tr><th>{o['horizon']}日</th><td>{pct(stats['mean'], True)}</td><td>{pct(stats['median'], True)}</td><td>{pct(stats['win_rate'])}</td><td>{pct(base['mean'], True)}</td><td>{pp(o['mean_difference'])}</td><td>{pct(stats['p10'], True)} / {pct(stats['p90'], True)}</td><td>{stats['count']} / {sampled['count']}</td><td>{num(o['oos_ic'], 3)}</td><td>{status}</td></tr>")
        examples = "".join(f"<tr><td>{h(e['signal_date'])}</td><td>{h(e['end_date'])}</td><td>{num(e['value'])}</td><td>{num(e['percentile'], 1)}%</td><td>{pct(e['return'], True)}</td></tr>" for e in o["examples"][-12:])
        detail_blocks.append(f"""<details class="horizon-detail"><summary>{o['horizon']}日：关联方向、历史表现与可核对样本</summary>
<p>{h(o['relationship'])}</p><p>{h(outcome_text(o))}</p>
<p class="muted">匹配信号日：{h(o['first_signal'] or '—')} 至 {h(o['last_signal'] or '—')}；不重叠样本覆盖 {len(o['years'])} 个年份：{h('、'.join(o['years']) or '—')}。
对应不重叠基准：均值{pct(o['baseline_non_overlapping']['mean'], True)}、上涨比例{pct(o['baseline_non_overlapping']['win_rate'])}；不是配对或显著性检验。</p>
<details><summary>核对最近 {min(12, sampled['count'])} 个不重叠观察期（共{sampled['count']}段）</summary><p class="muted">统计使用全部观察期；完整日期和收益保存在同名JSON文件。</p><div class="table-wrap"><table><thead><tr><th>信号日</th><th>到期日</th><th>当时因子值</th><th>当时分位</th><th>未来收益</th></tr></thead><tbody>{examples}</tbody></table></div></details></details>""")
    missing = "" if factor["current"] is not None else f"<p class='notice'>当天无有效值。历史有效区间：{h(factor['coverage'].get('first_valid') or '—')} 至 {h(factor['coverage'].get('last_valid') or '—')}；不补造数值，不输出当天方向。</p>"
    constant = "<p class='notice'>当前分位窗口内数值为常量；并列上界排名可达100%，不代表有区分能力。</p>" if factor["constant_window"] else ""
    return f"""<article class="factor" id="{h(index['code'] + '-' + factor['factor_id'])}" data-search="{h(factor['name'] + ' ' + factor['category'] + ' ' + factor['factor_id'])}">
<header><div><span class="tag">{h(factor['category'])}</span><h3>{h(factor['name'])}</h3></div><div class="readings"><span>当日读数<strong>{num(factor['current'])}<small>{h(unit)}</small></strong></span><span>近5年分位<strong>{position}</strong></span></div></header>
<p><b>因子与指数、ETF的关系：</b>{h(factor['mechanism'])}</p>
<p><b>当天位置：</b>{h(factor['current_state'])} 该窗口有{factor['percentile_samples']}个有效观测，至少需要{factor['minimum_samples']}个；分位表示不高于当前值的历史观测占比。</p>{missing}{constant}
<p><b>未来收益关联（20日）：</b>{h(default['relationship'] if default else '未计算此期限。')}</p>
<p><b>历史相近位置（20日）：</b>{h(outcome_text(default) if default else '未计算此期限。')}</p>
<p class="muted">固定匹配区间为当时滚动分位 {match_text}，上下各5个百分点，边界截在0—100；不是相同原始数值、相同宏观环境或相同因子组合。</p>
<div class="table-wrap"><table><thead><tr><th>未来期限</th><th>匹配日均值</th><th>中位数</th><th>上涨比例</th><th>基准均值</th><th>均值差</th><th>收益P10 / P90</th><th>匹配日 / 不重叠段</th><th>样本外IC</th><th>参考状态</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>
<p class="muted">表中收益和分位数来自全部匹配日，含重叠；P10/P90为历史结果分布分位，不是置信区间、最大亏损或未来范围。下方列出不重叠取样的敏感性比较。</p>
{''.join(detail_blocks)}</article>"""


def render(report):
    first, last = min(i["as_of"] for i in report["indices"]), max(i["as_of"] for i in report["indices"])
    as_of = first if first == last else first + "—" + last
    tabs = "".join(f"<button type='button' data-index='{h(i['code'])}' aria-pressed='false'>{h(i['name'])}</button>" for i in report["indices"])
    sections = []
    for index in report["indices"]:
        missing = [f["name"] for f in index["factors"] if f["current"] is None]
        high = [f["name"] for f in index["factors"] if f["percentile"] is not None and f["percentile"] >= 80]
        low = [f["name"] for f in index["factors"] if f["percentile"] is not None and f["percentile"] <= 20]
        factors = "".join(render_factor(index, f) for f in index["factors"])
        sections.append(f"""<section class="index-section" data-code="{h(index['code'])}"><div class="index-overview"><h2>{h(index['name'])} <small>{h(index['code'])}</small></h2><p>数据日 {h(index['as_of'])} · 收盘 {num(index['price'])} · 正式发布日期 {h(index['launch_date'])}</p>
<p><b>较高分位（≥80%）：</b>{h('、'.join(high) or '无')}。<b>较低分位（≤20%）：</b>{h('、'.join(low) or '无')}。</p><p><b>当天缺失：</b>{h('、'.join(missing) or '无')}。高低只描述数值位置，各因子的方向含义不同，不能计票当作买卖信号。</p></div>{factors}</section>""")
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>宽基指数逐因子分析报告</title><style>
:root{color-scheme:light;--ink:#2d392a;--muted:#737d6b;--green:#526d4e;--line:#e2e7dc}*{box-sizing:border-box}body{margin:0;background:#f5f6f0;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.8;font-size:14px}main{max-width:1440px;margin:auto;padding:36px 28px 70px}h1{font-size:30px;letter-spacing:-.5px;margin:0 0 8px}h2{font-size:24px;margin:0}h3{display:inline;font-size:19px;margin:0 0 0 10px}p{margin:10px 0}small,.muted{color:var(--muted);font-size:12px}small{font-weight:400}.intro,.method,.factor,.index-overview{background:white;border:1px solid var(--line);border-radius:12px;padding:24px;margin:18px 0}.intro{border-top:4px solid var(--green)}.intro p{max-width:1100px}.tag{font-size:12px;padding:3px 8px;background:#f0f3e9;border-radius:4px;color:var(--green)}.toolbar{position:sticky;top:0;z-index:2;background:#f5f6f0ed;backdrop-filter:blur(10px);padding:14px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}button,input{font:inherit;border:1px solid #d5dccd;border-radius:7px;background:white;padding:8px 14px;color:var(--ink)}button{cursor:pointer}button[aria-pressed=true]{background:var(--green);color:white;border-color:var(--green)}input{margin-left:auto;min-width:220px}.factor header{display:flex;justify-content:space-between;align-items:center;gap:24px;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:18px}.readings{display:flex;gap:40px}.readings span{font-size:12px;color:var(--muted);display:block}.readings strong{font-size:23px;color:var(--ink);display:block;font-variant-numeric:tabular-nums}.readings small{margin-left:5px}.table-wrap{overflow:auto;margin:16px 0}table{width:100%;border-collapse:collapse;font-size:12px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}th,td{padding:10px 12px;border-bottom:1px solid var(--line)}thead{background:#f4f6ef}th:first-child{text-align:left}tbody th{font-weight:500}tbody tr:hover{background:#f8faf5}.notice{padding:12px;background:#fff7eb;border-left:3px solid #b68c51}.horizon-detail{border-top:1px solid var(--line);padding:12px 0}summary{cursor:pointer;color:var(--green)}.method summary{font-weight:600}.method li{margin:8px 0}a{color:var(--green)}[hidden]{display:none!important}.count{font-size:12px;color:var(--muted)}.index-overview{background:#edf1e7}.footer{margin-top:30px;font-size:12px;color:var(--muted)}@media(max-width:700px){main{padding:20px 12px}.factor,.intro,.method,.index-overview{padding:18px}.factor header{align-items:flex-start;flex-direction:column}.readings{gap:35px}input{margin-left:0;width:100%}h1{font-size:25px}.toolbar{position:static}}@media print{body{background:white}.toolbar{display:none}.index-section,.factor{display:block!important}.factor{break-inside:avoid}main{max-width:none;padding:0}details>p,details>.table-wrap{display:block}a{color:inherit}}
</style></head><body><main><h1>宽基指数 · 逐因子分析报告</h1>""" + f"""<p class="muted">数据截至 {h(as_of)} · {len(report['indices'])}个指数 × {sum(len(i['factors']) for i in report['indices']) // len(report['indices'])}个因子 · 生成于 {h(report['generated_at'])}</p>
<div class="intro"><p>逐项回答三个问题：<b>因子描述什么、与未来收益有什么历史关联？当天处于什么位置？历史上相近位置之后实际发生过什么？</b></p>
<p>当前项目记录的是指数，报告对相应指数ETF提供底层市场解释；ETF实际表现还受分红、费用、跟踪差异、折溢价和交易成本影响，未选择或比较具体产品。指数价格收益不是ETF持有总收益。</p>
<p><b>阅读顺序：</b>先看当天位置，再看20日解释和多期限表；用样本外IC核对整体排序关系，用匹配样本核对局部历史表现。两者问题不同，方向可以不一致。匹配统计是回顾性描述，不是已经验证的预测概率。</p></div>
<details class="method"><summary>统一计算口径、匹配方法和解释边界</summary><ol>
<li>当日位置：最近1260个指数交易日内，因子有效值中“≤当日读数”的比例，包含当日，并列值取上界排名。普通因子至少60个有效值，股债收益差至少756个。窗口不足5年时使用已有日期，明确有效样本数，不声称已有完整5年。</li>
<li>相近位置：在每个历史信号日，只用该日及之前的数据重算滚动分位；固定选择“当前分位±5个百分点”，截在0—100，不因结果好坏扩大范围。只保留指数正式发布之后、未来收益已在数据日之前完整成熟的日期。发布前回溯可用于滚动状态预热；第三方当前历史版本无法还原当时尚未修订的数据。</li>
<li>收益：信号日收盘至未来h个交易日收盘的指数价格涨跌，未扣费，未含分红；当日收盘因子不保证能在同一收盘价成交。未成熟标签不补零。不同行情与期限的有效样本区间不同。</li>
<li>全部匹配日保留完整描述，但连续日期的未来收益常重叠。另按日期从早到晚固定选样，后一信号日与上一信号日至少间隔h个交易日；这只是收益区间不重叠，不保证统计独立。少于30段标“参考有限”，30段也不是显著性的保证。</li>
<li>基准：同一指数、同一期限、历史滚动分位可用且标签成熟的全部发布后日期；不是零收益，不是选取表现最好的另一因子。另列不重叠基准作取样敏感性比较，非配对实验。</li>
<li>IC：直接读取项目既有年度扩展训练样本外检验，每年训练至少504个有效样本、剔除跨边界标签。正IC表示较高因子值对应较高未来收益，负IC表示反向排序。单项分块区间未经多重比较校正。历史位置匹配本身没有另行做前瞻验证或显著性筛选。</li>
<li>上涨比例是历史正收益的频率；P10/P90是历史收益分布分位，不是未来概率、置信区间或最差/最好结果。相同分位不代表相同宏观环境；量价活跃度不等于净流入，估值吸引力不等于盈利增长，相关关系不证明因果。</li>
</ol></details><nav class="toolbar" aria-label="指数筛选">{tabs}<button type="button" data-index="all" aria-pressed="true">全部指数</button><input id="search" type="search" aria-label="搜索因子" placeholder="搜索因子名称、类别或ID"><span id="count" class="count"></span></nav>{''.join(sections)}
<p id="no-results" hidden>没有符合筛选的因子。</p><div class="footer">本报告由本机已发布快照生成，不触发采集或交易。完整指标、样本日期和快照指纹保存在同名JSON文件中。统计定义参考 <a href="https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.spearmanr.html">SciPy Spearman相关系数说明</a>；ETF跟踪与流动性参考 <a href="https://edu.sse.com.cn/etfgame/gametopic/c/4940396.shtml">上交所ETF投资者教育</a>。</div>
</main><script>
let selected='000300.SH';const buttons=[...document.querySelectorAll('[data-index]')],sections=[...document.querySelectorAll('.index-section')],search=document.querySelector('#search');
function filter(){{let count=0;const query=search.value.trim().toLowerCase();buttons.forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.index===selected)));sections.forEach(section=>{{let visible=0;section.querySelectorAll('.factor').forEach(f=>{{const show=f.dataset.search.toLowerCase().includes(query);f.hidden=!show;if(show)visible++;}});section.hidden=(selected!=='all'&&section.dataset.code!==selected)||!visible;if(!section.hidden)count+=visible;}});document.querySelector('#count').textContent=count+'项分析';document.querySelector('#no-results').hidden=count>0;}}
buttons.forEach(b=>b.addEventListener('click',()=>{{selected=b.dataset.index;filter();}}));search.addEventListener('input',filter);filter();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/factor-analysis", help="输出文件的公共前缀，生成.html和.json")
    args = parser.parse_args()
    report = read_report()
    prefix = Path(args.output)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    prefix.with_suffix(".html").write_text(render(report), encoding="utf-8")
    print(json.dumps({"html": str(prefix.with_suffix('.html')), "json": str(prefix.with_suffix('.json')),
        "indices": [{"code": i["code"], "as_of": i["as_of"], "factors": len(i["factors"])} for i in report["indices"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
