from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import csv
import json
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, render_template, request, Response

app = Flask(__name__)

APP_VERSION = "2026-05-21-hotfix2"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _yen(value: float | int) -> str:
    return f"{int(round(value)):,}円"


def _safe_raw(data: dict, path: list[str]) -> float:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return 0.0
        current = current[key]
    if isinstance(current, (int, float)):
        return float(current)
    return 0.0


def _fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en-US;q=0.9"})
    with urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="ignore")


def _extract_root_app_main(html: str) -> dict:
    m = re.search(r"root\.App\.main\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        raise ValueError("株式情報ページの解析に失敗しました。")
    return json.loads(m.group(1))


def fetch_symbol_data(code: str) -> dict:
    ticker = f"{code}.T"
    base_url = f"https://finance.yahoo.com/quote/{quote(ticker)}"

    # HTML中の埋め込みJSON（root.App.main）からデータを取得
    quote_html = _fetch_html(base_url)
    root_data = _extract_root_app_main(quote_html)
    store = root_data.get("context", {}).get("dispatcher", {}).get("stores", {})

    quote_store = store.get("QuoteSummaryStore", {})
    price_store = quote_store.get("price", {})
    stats_store = quote_store.get("defaultKeyStatistics", {})

    company_name = (
        price_store.get("longName")
        or price_store.get("shortName")
        or ticker
    )
    price = _safe_raw(price_store, ["regularMarketPrice", "raw"])
    shares_outstanding = _safe_raw(stats_store, ["sharesOutstanding", "raw"])

    # BS値がトップページで不足する場合があるため balance-sheet ページも参照
    bs_html = _fetch_html(f"{base_url}/balance-sheet?p={quote(ticker)}")
    bs_root = _extract_root_app_main(bs_html)
    bs_store = bs_root.get("context", {}).get("dispatcher", {}).get("stores", {}).get("QuoteSummaryStore", {})

    statement = (
        bs_store.get("balanceSheetHistory", {})
        .get("balanceSheetStatements", [{}])[0]
    )

    current_assets = _safe_raw(statement, ["totalCurrentAssets", "raw"])
    investments = _safe_raw(statement, ["investments", "raw"])
    liabilities = _safe_raw(statement, ["totalLiab", "raw"])

    if not price:
        raise ValueError("株価が取得できませんでした。")
    if not shares_outstanding:
        shares_outstanding = _safe_raw(price_store, ["marketCap", "raw"]) / price if price else 0
    if not shares_outstanding:
        raise ValueError("発行済株式数（または時価総額）が取得できませんでした。")

    market_cap = price * shares_outstanding
    net_cash = current_assets + (investments * 0.7) - liabilities
    ncr = (net_cash / market_cap) if market_cap else 0
    undervalued = ncr <= 1

    return {
        "timestamp": datetime.now(timezone.utc).astimezone().strftime("%y/%m/%d"),
        "code": code,
        "company_name": company_name,
        "price": price,
        "current_assets": current_assets,
        "investments": investments,
        "liabilities": liabilities,
        "market_cap": market_cap,
        "net_cash": net_cash,
        "ncr": ncr,
        "undervalued": undervalued,
        "price_formatted": _yen(price),
        "current_assets_formatted": _yen(current_assets),
        "investments_formatted": _yen(investments),
        "liabilities_formatted": _yen(liabilities),
        "market_cap_formatted": _yen(market_cap),
        "net_cash_formatted": _yen(net_cash),
        "ncr_formatted": f"{ncr:.2f}",
        "result_text": "真の割安株です！" if undervalued else "真の割安株ではありません。",
    }


@app.route("/", methods=["GET", "POST"])
def index():
    data = None
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not code.isdigit():
            error = "証券コードは数字で入力してください。"
        else:
            try:
                data = fetch_symbol_data(code)
            except Exception as exc:
                error = f"データ取得に失敗しました: {exc}"
    return render_template("index.html", data=data, error=error, app_version=APP_VERSION)


@app.route("/export_csv", methods=["POST"])
def export_csv():
    code = (request.form.get("code") or "").strip()
    if not code:
        return Response("証券コードが未入力です", status=400)

    try:
        data = fetch_symbol_data(code)
    except Exception as exc:
        return Response(f"CSV出力に失敗しました: {exc}", status=400)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "現在時刻",
            "証券コード",
            "企業名",
            "直近の株価",
            "流動資産",
            "投資有価証券",
            "負債",
            "時価総額",
            "ネットキャッシュ",
            "ネットキャッシュ比率",
            "真の割安株か否か",
        ]
    )
    writer.writerow(
        [
            data["timestamp"],
            data["code"],
            data["company_name"],
            data["price_formatted"],
            data["current_assets_formatted"],
            data["investments_formatted"],
            data["liabilities_formatted"],
            data["market_cap_formatted"],
            data["net_cash_formatted"],
            data["ncr_formatted"],
            data["result_text"],
        ]
    )

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=net_cash_ratio_{code}.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True)
