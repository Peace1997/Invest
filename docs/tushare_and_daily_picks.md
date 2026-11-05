# Tushare sample ingest and daily picks

Two commands. Neither places an order, and neither starts the Streamlit UI.

AkShare remains the source for the existing `sample` / `daily` commands. These
two commands do not import it.

## Environment

Read on each call. There is no default host and no token in the repo.

| Variable | Role |
| --- | --- |
| `TUSHARE_TOKEN` | Token sent in the JSON body. Do not put it on the command line. |
| `TUSHARE_HTTP_URL` | Tushare Pro compatible endpoint (`api_name`, `token`, `params`, `fields`). |

`TUSHARE_HTTP_URL` must not be `www.szse.cn` (or any `*.szse.cn` host). That
site is not a Tushare endpoint; this client refuses it.

The older `ashare.sources.tushare_src.get_pro` helper is a separate path and
still has its own proxy constant. `tushare-sample` does not use that helper.

```bash
export TUSHARE_TOKEN
export TUSHARE_HTTP_URL
```

Set both in the environment of the shell that will run the command. A missing
variable exits 2 and does not create a warehouse.

## Sample ingest

Universe is `sample_targets` in `config.yaml`: index `000300`, ETF `510300`,
stock `000001`. Codes are sent as `000300.SH`, `510300.SH`, and `000001.SZ`.

APIs: `trade_cal` (SSE open days), `index_basic` / `fund_basic` / `stock_basic`,
`index_daily` / `fund_daily` / `daily`, plus `daily_basic` for the stock
(turnover, and PE/PB/market cap into `valuation_daily`, src `tushare`).

Bar units match the existing Tushare bar ingest: volume in 手, amount converted
from 千元 to 元. A bar dated today is skipped before 15:30 local time.

```bash
python -m ashare.cli tushare-sample --start 20240601 --end 20240628
```

From a checkout that uses uv:

```bash
uv run python -m ashare.cli tushare-sample --start 20240601 --end 20240628
```

Exit 0 when calendar, metadata, and bars were stored. Exit 2 when a call
failed or came back empty. Empty means empty: the command does not fill in
prices. Inspect with `python -m ashare.cli query` after a successful run.
`query` does import AkShare.

`index_daily` / `daily` windows are split so one request stays under the
8000-row cap. Pass an explicit `--start` / `--end` on the first run.

## Daily picks

At most three names, and only names present in the input. Output always
starts with `mode=`:

| mode | Meaning |
| --- | --- |
| `fixture` | Payload is marked `"mode": "fixture"` (or `"fixture": true`). `--live` does not promote it. |
| `replay` | A screen file that was not marked live. |
| `live` | `--live` on a non-fixture file, or `--warehouse` against the local database. |

Each candidate has the screen's data date, a reference close, an entry band
(1% below the close through 2% above it), and an invalidation price 3% under
the close. The entry note is a research rule: next session only, no chase
above the cap, no order is sent. The invalidation note is T+1-aware: a fill
on the entry session cannot be sold that same session.

A file needs `"strategy": "dual_low"` and a market data date in `data_date`,
`trade_date`, `as_of`, or `snapshot_trade_date`. `created_at` is not that
date. Missing date, any other strategy, or no usable rows prints
`No candidates.`

```bash
python -m ashare.cli daily-picks --screen /path/to/dual_low.json
python -m ashare.cli daily-picks --screen /path/to/dual_low.json --live --json
```

`--json` prints the same fields for a parent process to parse. `count` is 0
when nothing qualified.

### Warehouse screen

Equivalent hard filters to AlphaSift `dual_low` (PE in (0, 15], PB in (0, 2],
price 3–80, amount at least 50 million yuan, total market cap 50–3000 亿元,
daily change within ±4.5%, not ST). Score is the average PE/PB percentile on
that day's stored cross-section, cheaper higher. Ties break by symbol.

This is not the AlphaSift factor model. It only ranks rows already in
`daily_bar` and `valuation_daily`. Fewer than two names after the filter
produces no candidate, so ingesting the single sample stock `000001` does not
create a pick.

```bash
python -m ashare.cli daily-picks --warehouse
python -m ashare.cli daily-picks --warehouse --as-of 20240603 --json
```

Exit 2 if the warehouse file does not exist. Exit 0 with `No candidates.`
when the filters match nothing.

## What this repo run did not do

Live credentialed calls are left to the operator who has `TUSHARE_TOKEN`.
A green test run only proves the fixture and the in-memory warehouse screen.
