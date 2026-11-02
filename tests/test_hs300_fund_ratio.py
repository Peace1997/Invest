"""口径与占比计算。不访问网络。"""
from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from ashare.ingest.hs300_fund_ratio import (
    build_ratio_view,
    is_plain_hs300_etf,
    parse_gmbd_payload,
)


_HTML = """
var gmbd_apidata={ content:"<table><thead><tr><th>日期</th><th>期间申购（亿份）</th><th>期间赎回（亿份）</th><th>期末总份额（亿份）</th><th>期末净资产（亿元）</th><th>净资产变动率</th></tr></thead><tbody><tr><td>2026-09-30</td><td>1.5</td><td>0.5</td><td>10</td><td>40</td><td>1%</td></tr><tr><td>2026-06-30</td><td>---</td><td>---</td><td>8</td><td>---</td><td>---</td></tr></tbody></table>",records:2}
"""


class ParseTests(unittest.TestCase):
    def test_parse_keeps_numeric_net_asset_in_yuan(self):
        df = parse_gmbd_payload(_HTML)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.loc[0, "report_date"], date(2026, 9, 30))
        self.assertAlmostEqual(df.loc[0, "net_asset"], 40e8)
        self.assertAlmostEqual(df.loc[0, "shares"], 10e8)

    def test_parse_empty_payload(self):
        self.assertTrue(parse_gmbd_payload("no table").empty)

    def test_parse_json_net_asset_already_in_yuan(self):
        payload = (
            'var gmbd_apidata={ content:"",summary:"x","data":['
            '{"FSRQ":"2026-09-30","QMJZC":"","NETNAV":106879396730.03,"QMZFE":24119587690.0},'
            '{"FSRQ":"2026-06-30","QMJZC":94872183996.4,"NETNAV":1,"QMZFE":""}'
            ']};'
        )
        df = parse_gmbd_payload(payload)
        self.assertEqual(list(df["report_date"]), [date(2026, 6, 30), date(2026, 9, 30)])
        sept = df[df["report_date"] == date(2026, 9, 30)].iloc[0]
        self.assertAlmostEqual(sept["net_asset"], 106879396730.03)
        self.assertAlmostEqual(sept["shares"], 24119587690.0)
        june = df[df["report_date"] == date(2026, 6, 30)].iloc[0]
        self.assertAlmostEqual(june["net_asset"], 94872183996.4)
        self.assertTrue(pd.isna(june["shares"]))


class FilterTests(unittest.TestCase):
    def test_plain_csi300_etf_only(self):
        self.assertTrue(is_plain_hs300_etf("沪深300ETF华夏"))
        self.assertTrue(is_plain_hs300_etf("沪深300ETF易方达"))
        self.assertFalse(is_plain_hs300_etf("沪深300ETF联接A"))
        self.assertFalse(is_plain_hs300_etf("沪深300增强ETF招商"))
        self.assertFalse(is_plain_hs300_etf("沪深300成长ETF万家"))
        self.assertFalse(is_plain_hs300_etf("沪深300ESGETF富国"))
        self.assertFalse(is_plain_hs300_etf("沪深300红利ETF建信"))
        self.assertFalse(is_plain_hs300_etf("中证500ETF"))


class RatioTests(unittest.TestCase):
    def _scales(self):
        return pd.DataFrame([
            {"symbol": "510300", "name": "华泰", "report_date": date(2024, 3, 31), "net_asset": 80e8},
            {"symbol": "510300", "name": "华泰", "report_date": date(2024, 6, 30), "net_asset": 100e8},
            {"symbol": "510310", "name": "易方达", "report_date": date(2024, 6, 30), "net_asset": 50e8},
            {"symbol": "510300", "name": "华泰", "report_date": date(2024, 9, 30), "net_asset": 110e8},
            {"symbol": "510310", "name": "易方达", "report_date": date(2024, 9, 30), "net_asset": 40e8},
            {"symbol": "159919", "name": "嘉实", "report_date": date(2024, 9, 30), "net_asset": 10e8},
        ])

    def test_top_n_full_coverage_and_scaled_denominator(self):
        closes = pd.DataFrame([
            {"trade_date": date(2024, 6, 28), "close": 3000.0},
            {"trade_date": date(2024, 9, 30), "close": 3600.0},
            {"trade_date": date(2024, 11, 1), "close": 4000.0},
        ])
        mvs = pd.DataFrame([
            {"trade_date": date(2024, 11, 1), "close": 4000.0, "total_mv": 1200e8, "float_mv": 1000e8},
        ])
        view = build_ratio_view(self._scales(), closes, mvs, top_n=2)
        self.assertEqual(list(view.funds["symbol"]), ["510300", "510310"])
        self.assertEqual(list(view.series["report_date"]), [date(2024, 6, 30), date(2024, 9, 30)])
        june, sept = view.series.itertuples(index=False)
        self.assertAlmostEqual(june.ratio_pct, 20.0)
        self.assertEqual(june.basis, "回推")
        self.assertAlmostEqual(sept.ratio_pct, 150e8 / (1000e8 * 3600 / 4000) * 100)
        self.assertEqual(sept.basis, "回推")

    def test_same_day_anchor_uses_actual_float_mv(self):
        scales = pd.DataFrame([
            {"symbol": "510300", "name": "华泰", "report_date": date(2024, 9, 30), "net_asset": 200e8},
        ])
        closes = pd.DataFrame([{"trade_date": date(2024, 9, 30), "close": 3600.0}])
        mvs = pd.DataFrame([
            {"trade_date": date(2024, 9, 30), "close": 3610.0, "total_mv": None, "float_mv": 800e8},
        ])
        view = build_ratio_view(scales, closes, mvs, top_n=10)
        self.assertEqual(view.series.loc[0, "basis"], "实值")
        self.assertAlmostEqual(view.series.loc[0, "ratio_pct"], 25.0)

    def test_without_closes_keeps_latest_cross_section(self):
        view = build_ratio_view(self._scales(), pd.DataFrame(), pd.DataFrame([
            {"trade_date": date(2024, 11, 1), "close": 4000.0, "total_mv": None, "float_mv": 1000e8},
        ]), top_n=2)
        self.assertEqual(len(view.series), 1)
        self.assertEqual(view.series.loc[0, "basis"], "截面")
        self.assertEqual(view.series.loc[0, "report_date"], date(2024, 9, 30))
        self.assertAlmostEqual(view.series.loc[0, "ratio_pct"], 15.0)


if __name__ == "__main__":
    unittest.main()
