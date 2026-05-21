from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import csv

import requests
from flask import Flask, render_template, request, Response

app = Flask(__name__)

YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}


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


def fetch_symbol_data(code: str) -> dict:
    ticker = f"{code}.T"

    quote_url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
    q = requests.get(quote_url, headers=YAHOO_HEADERS, timeout=15)
    q.raise_for_status()
    quote_result = q.json().get("quoteResponse", {}).get("result", [])
    if not quote_result:
        raise ValueError("証券コードに対応する株価情報が見つかりませんでした。")

    quote = quote_result[0]
    company_name = quote.get("longName") or quote.get("shortName") or ticker
    price = float(quote.get("regularMarketPrice") or 0)
    shares_outstanding = float(quote.get("sharesOutstanding") or 0)

    bs_url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        "?modules=balanceSheetHistory"
    )
    bs_resp = requests.get(bs_url, headers=YAHOO_HEADERS, timeout=15)
    bs_resp.raise_for_status()
    bs_json = bs_resp.json()
    result = bs_json.get("quoteSummary", {}).get("result") or []
    if not result:
        raise ValueError("財務情報が取得できませんでした。")

    statement = (
        result[0]
        .get("balanceSheetHistory", {})
        .get("balanceSheetStatements", [{}])[0]
    )

    current_assets = _safe_raw(statement, ["totalCurrentAssets", "raw"])
    investments = _safe_raw(statement, ["investments", "raw"])
    liabilities = _safe_raw(statement, ["totalLiab", "raw"])

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
    return render_template("index.html", data=data, error=error)


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
