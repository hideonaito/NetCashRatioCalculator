from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from io import StringIO
import csv
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, Response, render_template, request

app = Flask(__name__)

APP_VERSION = "2026-05-22-rebuild2"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _yen(value: float | int) -> str:
    return f"{int(round(value)):,}円"


def _fetch_html(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="ignore")


def _fetch_first_available(urls: list[str]) -> str:
    errors: list[str] = []
    for url in urls:
        try:
            return _fetch_html(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
    raise ValueError("候補URLの取得にすべて失敗しました: " + " | ".join(errors))


def _extract_json_ld(html: str) -> dict:
    scripts = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    for raw in scripts:
        text = unescape(raw.strip())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") in {"WebPage", "Organization", "Product"}:
            if "mainEntity" in data:
                return data
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    raise ValueError("株価のJSON-LD解析に失敗しました。")


def _extract_root_app_main(html: str) -> dict:
    m = re.search(r"root\.App\.main\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        raise ValueError("Yahoo埋め込みデータの解析に失敗しました。")
    return json.loads(m.group(1))


def _to_number(value: str) -> float:
    cleaned = value.replace(",", "").replace("円", "").replace("百万円", "").replace("千円", "").strip()
    return float(cleaned)


def _parse_irbank_value(html: str, label: str) -> float:
    pattern = rf"<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>"
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return 0.0
    text = re.sub(r"<.*?>", "", m.group(1))
    text = unescape(text).strip()
    if not text or text == "-":
        return 0.0
    return _to_number(text)




def _extract_first_number(html: str, patterns: list[str]) -> float:
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            return _to_number(m.group(1))
        except Exception:
            continue
    return 0.0

def fetch_symbol_data(code: str) -> dict:
    ticker = f"{code}.T"

    quote_html = _fetch_first_available([
        f"https://finance.yahoo.co.jp/quote/{quote(ticker)}",
        f"https://finance.yahoo.com/quote/{quote(ticker)}",
        f"https://finance.yahoo.com/quote/{quote(ticker)}?p={quote(ticker)}",
    ])

    company_name = f"{code}.T"
    price = 0.0
    shares_outstanding = 0.0

    # try JP Yahoo JSON-LD first
    try:
        ld = _extract_json_ld(quote_html)
        candidate = ld.get("mainEntity", ld)
        company_name = candidate.get("name") or company_name
        offers = candidate.get("offers") or {}
        price = float(offers.get("price") or 0)
    except Exception:
        pass

    # fallback to Yahoo root.App.main (US page etc.)
    try:
        root = _extract_root_app_main(quote_html)
        stores = root.get("context", {}).get("dispatcher", {}).get("stores", {})
        summary = stores.get("QuoteSummaryStore", {})
        price_store = summary.get("price", {})
        stats_store = summary.get("defaultKeyStatistics", {})
        company_name = price_store.get("longName") or price_store.get("shortName") or company_name
        if not price:
            price = float((price_store.get("regularMarketPrice") or {}).get("raw") or 0)
        shares_outstanding = float((stats_store.get("sharesOutstanding") or {}).get("raw") or 0)
        if not shares_outstanding and price:
            market_cap = float((price_store.get("marketCap") or {}).get("raw") or 0)
            shares_outstanding = market_cap / price if market_cap else 0
    except ValueError:
        pass

    # HTMLテキストからの最終フォールバック
    if not price:
        price = _extract_first_number(
            quote_html,
            [
                r'"regularMarketPrice"\s*:\s*\{\s*"raw"\s*:\s*([0-9\.]+)',
                r'"price"\s*:\s*"([0-9,\.]+)"',
                r'<fin-streamer[^>]*data-field="regularMarketPrice"[^>]*value="([0-9\.]+)"',
            ],
        )

    if not shares_outstanding:
        shares_outstanding = _extract_first_number(
            quote_html,
            [
                r"発行済株式数[^0-9]*([0-9,]+)",
                r'"sharesOutstanding"\s*:\s*\{\s*"raw"\s*:\s*([0-9]+)',
                r'"issuedShares"\s*:\s*"([0-9,]+)"',
            ],
        )

    if not price:
        raise ValueError("株価が取得できませんでした。")
    if not shares_outstanding:
        raise ValueError("発行済株式数が取得できませんでした。")

    # Financial values from IR BANK (reliable Japanese financial statement site)
    ir_html = _fetch_first_available([
        f"https://irbank.net/{code}",
        f"https://irbank.net/{code}?f=S",
    ])

    current_assets = _parse_irbank_value(ir_html, "流動資産") * 1_000_000
    investments = _parse_irbank_value(ir_html, "投資有価証券") * 1_000_000
    liabilities = _parse_irbank_value(ir_html, "負債合計") * 1_000_000

    if current_assets <= 0 and liabilities <= 0:
        raise ValueError("IR BANKから主要財務データを取得できませんでした。")

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
        if not re.fullmatch(r"\d{4}", code):
            error = "証券コードは4桁の数字で入力してください。"
        else:
            try:
                data = fetch_symbol_data(code)
            except Exception as exc:
                error = f"データ取得に失敗しました: {type(exc).__name__}: {exc}"
    return render_template("index.html", data=data, error=error, app_version=APP_VERSION)


@app.route("/export_csv", methods=["POST"])
def export_csv():
    code = (request.form.get("code") or "").strip()
    if not code:
        return Response("証券コードが未入力です", status=400)

    try:
        data = fetch_symbol_data(code)
    except Exception as exc:
        return Response(f"CSV出力に失敗しました: {type(exc).__name__}: {exc}", status=400)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
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
    ])
    writer.writerow([
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
    ])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=net_cash_ratio_{code}.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True)
