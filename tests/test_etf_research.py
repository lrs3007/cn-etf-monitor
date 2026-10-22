import json
import numpy as np
import pandas as pd
import pytest

from backend.app.config import INDICES
from backend.app.etf_research import align_research
from backend.app.etf_sources import parse_nav, szse_share_window, sse_share_day
from backend.app.factors import REGISTRY, calculate_all
from backend.app.factor_analysis import analyze_factor


@pytest.fixture
def source(monkeypatch):
    dates = pd.bdate_range('2026-01-01', periods=50).strftime('%Y-%m-%d').tolist()
    frame = pd.DataFrame({'date': dates, 'close': np.linspace(100, 110, 50)})
    prices = pd.DataFrame({'date': dates[5:], 'close': np.full(45, 1.02), 'volume': np.full(45, 100.), 'amount': np.full(45, 102.)})
    nav = pd.DataFrame({'date': dates[5:], 'nav': np.ones(45), 'nav_return': np.zeros(45)})
    context = {'instruments': {INDICES[0].code: {'code': '510300.SH', 'name': 'test'}},
        'funds': {INDICES[0].code: {'prices': prices, 'nav': nav, 'records': [], 'errors': []}},
        'shares': {('510300.SH', day): {'shares': 1000. + i * 10} for i, day in enumerate(dates[5:])}, 'errors': []}
    monkeypatch.setattr('backend.app.etf_research.capture_breadth', lambda *args: ([], []))
    monkeypatch.setattr('backend.app.etf_research.import_breadth', lambda *args: [])
    return frame, context


def test_external_metrics_same_day_then_next_session_no_prelisting(source):
    frame, context = source
    actual, lineage = align_research(frame, INDICES[0], context, True)
    assert actual.etf_premium_discount_input.iloc[:6].isna().all()
    assert actual.etf_premium_discount_input.iloc[6] == pytest.approx(2)
    assert actual.etf_turnover_input.iloc[6] == pytest.approx(10)
    assert actual.etf_shares_change_5_input.iloc[11] == pytest.approx(5)
    assert actual.etf_amount_ratio_20_input.iloc[26] == pytest.approx(1)
    assert actual.etf_nav_index_gap_20_input.iloc[26] == pytest.approx((1 - frame.close.iloc[25] / frame.close.iloc[5]) * 100)
    link = next(r for r in lineage['inputs'] if r['factor_id'] == 'etf_premium_discount' and r['trade_date'] == frame.date.iloc[6])
    observation = next(r for r in lineage['observations'] if r['id'] == link['observation_id'])
    assert observation['observation_date'] == frame.date.iloc[5]
    assert lineage['sources']['etf_premium_discount']['input_observation_date'] == frame.date.iloc[-2]
    assert not any(r['trade_date'] > frame.date.iloc[-1] for r in lineage['inputs'])
    result = calculate_all(actual)
    assert result['etf_premium_discount'][0].iloc[6] == pytest.approx(2)


def test_missing_sources_never_fill_or_bridge_share_windows(source):
    frame, context = source
    context['shares'].pop(('510300.SH', frame.date.iloc[20]))
    context['funds'][INDICES[0].code]['nav'].loc[15, 'nav'] = np.nan
    actual, _ = align_research(frame, INDICES[0], context, True)
    assert actual.etf_shares_change_5_input.iloc[21:27].isna().all()
    assert pd.isna(actual.etf_premium_discount_input.iloc[21])
    assert pd.notna(actual.etf_premium_discount_input.iloc[22])


def test_conversion_window_not_treated_as_subscription(source):
    frame, context = source
    context['funds'][INDICES[0].code]['nav'].loc[15:, 'nav'] = .5
    actual, _ = align_research(frame, INDICES[0], context, True)
    assert actual.etf_shares_change_5_input.iloc[21:26].isna().all()


def test_breadth_counts_complete_pit_and_live_calendar(source, monkeypatch):
    frame, context = source
    row = {'date': frame.date.iloc[25], 'available_date': frame.date.iloc[26], 'universe_date': frame.date.iloc[25],
        'expected_members': 300, 'valid_members': 300, 'advancing': 180, 'above_ma20': 210, 'ma20_valid_members': 300,
        'ma20_observation_dates': frame.date.iloc[6:26].tolist(), 'records': []}
    monkeypatch.setattr('backend.app.etf_research.capture_breadth', lambda *args: ([row], []))
    actual, _ = align_research(frame, INDICES[0], context, True)
    assert actual.breadth_advancing_ratio_input.iloc[26] == 60
    assert actual.breadth_above_ma20_ratio_input.iloc[26] == 70
    assert pd.isna(actual.breadth_advancing_ratio_input.iloc[27])
    row['ma20_observation_dates'][0] = frame.date.iloc[0]
    actual, _ = align_research(frame, INDICES[0], context, True)
    assert pd.isna(actual.breadth_above_ma20_ratio_input.iloc[26])
    row['advancing'] = 301
    with pytest.raises(ValueError): align_research(frame, INDICES[0], context, True)


def test_nav_json_only_timezone_and_disclosed_return():
    stamp = int(pd.Timestamp('2026-10-22', tz='Asia/Shanghai').timestamp()*1000)
    frame = parse_nav('var Data_netWorthTrend = ' + json.dumps([{'x':stamp,'y':1.23,'equityReturn':2.5}]) + '; throw Error("do not execute");')
    assert frame.iloc[0].to_dict() == {'date':'2026-10-22','nav':1.23,'nav_return':.025}


def test_exchange_shares_units_whitespace_and_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.app.etf_sources.DATA_DIR', tmp_path)
    monkeypatch.setattr('backend.app.etf_sources.archive', lambda *args: {'source':'test'})
    calls = []
    def fetch(url, params, **kwargs):
        calls.append(params['PAGENO'])
        return json.dumps([{'metadata':{'pagecount':2,'recordcount':2},'data':[{'size_date':'2026-10-2'+params['PAGENO'], 'fund_code':'159949  ', 'current_size':'1,234.5'}]}])
    monkeypatch.setattr('backend.app.etf_sources.fetch', fetch)
    result = szse_share_window('159949.SZ','2026-10-01','2026-10-22')
    assert calls == ['1','2'] and len(result['rows']) == 2
    assert result['rows'][0]['shares'] == 12345000
    monkeypatch.setattr('backend.app.etf_sources.fetch', lambda *args, **kwargs: json.dumps({'result':[{'SEC_CODE':'510300','STAT_DATE':'2026-10-22','TOT_VOL':'12.5'}],'pageHelp':{'total':1}}))
    assert sse_share_day('2026-10-22', {'510300.SH'})['rows'][0]['shares'] == 125000


def test_new_factors_use_registered_explanation_and_do_not_score(source):
    frame, context = source
    aligned, _ = align_research(frame, INDICES[0], context, True)
    definition = REGISTRY['etf_premium_discount'].metadata()
    analysis = analyze_factor(frame.date, frame.close, aligned.etf_premium_discount_input, definition, {}, frame.date.iloc[0])
    assert analysis['mechanism'] == definition['explanation']
    assert '溢价' in analysis['current_state']
    assert all(not factor.assessment_enabled for factor in REGISTRY.values() if factor.source_group)


def test_pipeline_persists_observation_links_and_publishes_same_analysis(source, tmp_path, monkeypatch):
    import gzip
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from backend.app import pipeline, api
    from backend.app.db import Base
    from backend.app.models import ResearchInput, ResearchObservation
    from backend.app.publication import export_results
    _, context = source
    dates = pd.bdate_range('2026-01-01', periods=80).strftime('%Y-%m-%d')
    frame = pd.DataFrame({'date':dates, 'close':np.linspace(100,120,80)})
    for column in ('open','high','low'): frame[column] = frame.close
    for column in ('volume','amount'): frame[column] = 1000.
    for column in ('pe_ttm','pb','spread_pe_ttm','cgb_10y_yield'): frame[column] = np.nan
    frame['source'], frame['history_type'], frame['availability'] = 'test', 'live', dates
    engine = create_engine(f"sqlite:///{tmp_path/'research.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(pipeline, 'SessionLocal', sessions)
    monkeypatch.setattr(pipeline, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(pipeline, 'init_db', lambda: None)
    monkeypatch.setattr(pipeline, 'full_history', lambda *args, **kwargs: (frame.copy(), {'rows':80,'first_date':dates[0], 'last_date':dates[-1], 'volume_rows':80,'amount_rows':80,'valuation_rows':0,'provider_errors':[]}))
    pipeline.seed()
    result = pipeline.process_index(INDICES[0], research_context=context, offline=True)
    with sessions() as session:
        rows = session.execute(select(ResearchInput,ResearchObservation).join(ResearchObservation,ResearchInput.observation_id == ResearchObservation.id).where(ResearchInput.snapshot_id == result['snapshot'])).all()
        assert rows and all(link.trade_date > obs.observation_date for link,obs in rows)
    monkeypatch.setattr(api, 'SessionLocal', sessions)
    detail = api.factor_detail(INDICES[0].code,'etf_premium_discount',20)
    directory = tmp_path/'published'
    manifest = export_results(directory, sessions, (INDICES[0],))
    summary = json.loads(gzip.decompress((directory/manifest['objects']['index/'+INDICES[0].code]['key']).read_bytes()))
    assert summary['coverage']['factor_coverage']['etf_premium_discount'] == detail['coverage']
    assert summary['analyses']['etf_premium_discount'] == detail['analysis']
    assert 'path' not in json.dumps(summary['coverage']['factor_coverage']['etf_premium_discount']['source'])
    assert pipeline.process_index(INDICES[0], research_context=context, offline=True)['status'] == 'unchanged'
    engine.dispose()


def test_breadth_ma20_chains_ex_rights_reference_prices(tmp_path, monkeypatch):
    from backend.app.etf_sources import save_cache
    from backend.app.market_breadth import capture_breadth
    monkeypatch.setattr('backend.app.market_breadth.DATA_DIR', tmp_path)
    dates = pd.bdate_range('2026-01-01',periods=20).strftime('%Y-%m-%d')
    symbols = ['sh'+str(600000+i) for i in range(50)]
    for i, day in enumerate(dates):
        close = 100. if i < 10 else 50. if i < 19 else 51.
        previous = 100. if i < 10 else 50.
        save_cache(tmp_path/'breadth-cache'/INDICES[2].code/(day+'.json'), {'date':day,'universe_date':day,'symbols':symbols,
            'quotes':{symbol:{'close':close,'previous':previous} for symbol in symbols},'records':[]})
    rows,_ = capture_breadth(INDICES[2],True)
    assert rows[-1]['above_ma20'] == 50
    assert rows[-1]['ma20_valid_members'] == 50
    assert rows[-2]['above_ma20'] is None
