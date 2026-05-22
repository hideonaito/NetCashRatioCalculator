from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from io import StringIO
import csv
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, Response, render_template, request

app = Flask(__name__)

APP_VERSION = "2026-05-22-spec-jpx-ir-1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _yen(value: float | int) -> str:
    return f"{int(round(value)):,}円"


def _to_number(value: str) -> float:
    cleaned = re.sub(r"\s+", "", value)
    cleaned = cleaned.replace(",", "").replace("円", "").replace("株", "")
    cleaned = cleaned.replace("△", "-").replace("▲", "-")
    return float(cleaned) if cleaned else 0.0


def _fetch_html(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="ignore")


def _fetch_first_available(urls: list[str]) -> str:
    errors: list[str] = []
    for url in urls:
        try:
            return _fetch_html(url)
        except (HTTPError, URLError, TimeoutError) as exc:
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
    raise ValueError("候補URLの取得にすべて失敗しました: " + " | ".join(errors))


def _extract_first_number(html: str, patterns: list[str]) -> float:
    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        text = unescape(re.sub(r"<.*?>", "", m.group(1))).strip()
        try:
            return _to_number(text)
        except ValueError:
            continue
    return 0.0


def _extract_company_name(html: str, code: str) -> str:
    for pattern in [r"<title>\s*([^<\-｜\|]+?)[\-｜\|]", r"<h1[^>]*>(.*?)</h1>", r'"name"\s*:\s*"([^"]+?)"']:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            text = unescape(re.sub(r"<.*?>", "", m.group(1))).strip()
            text = re.sub(r"\s+", " ", text)
            if text and len(text) < 120:
                return text
    return f"{code}.T"


def _extract_multiplier(html: str) -> int:
    if re.search(r"単位[:：]\s*百万円", html):
        return 1_000_000
    if re.search(r"単位[:：]\s*千円", html):
        return 1_000
    if re.search(r"単位[:：]\s*円", html):
        return 1
    return 1_000_000


def _parse_table_value(html: str, labels: list[str], multiplier: int = 1) -> float:
    for label in labels:
        pat = rf"(?:<th[^>]*>|<dt[^>]*>)\s*{re.escape(label)}\s*(?:</th>|</dt>)\s*(?:<td[^>]*>|<dd[^>]*>)(.*?)(?:</td>|</dd>)"
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        text = unescape(re.sub(r"<.*?>", "", m.group(1))).strip()
        if not text or text == "-":
            continue
        try:
            return _to_number(text) * multiplier
        except ValueError:
            continue
    return 0.0


def _fetch_market_data_from_jpx(code: str) -> tuple[str, float, float]:
    # JPX stock search top is dynamic; use robust fallbacks including company pages.
    urls = [
        f"https://quote.jpx.co.jp/jpxhp/main/index.aspx?F=stock_search",
        f"https://kabutan.jp/stock/?code={code}",
        f"https://finance.yahoo.co.jp/quote/{code}.T",
    ]
    html = _fetch_first_available(urls)
    company_name = _extract_company_name(html, code)
    price = _extract_first_number(html, [
        r"現在値[^0-9]{0,20}([0-9,]+)",
        r"株価[^0-9]{0,20}([0-9,]+)",
        r'"regularMarketPrice"\s*:\s*\{\s*"raw"\s*:\s*([0-9\.]+)',
    ])
    shares_outstanding = _extract_first_number(html, [
        r"発行済株式数[^0-9]{0,40}([0-9,]+)",
        r"発行済株式総数[^0-9]{0,40}([0-9,]+)",
        r'"sharesOutstanding"\s*:\s*\{\s*"raw"\s*:\s*([0-9]+)',
    ])
    if not shares_outstanding:
        ir_html = _fetch_first_available([f"https://irbank.net/{code}", f"https://irbank.net/{code}?f=S"])
        shares_outstanding = _extract_first_number(ir_html, [r"発行済株式総数[^0-9]{0,40}([0-9,]+)", r"発行済株式数[^0-9]{0,40}([0-9,]+)"])
    if not price:
        raise ValueError("直近の株価が取得できませんでした。")
    if not shares_outstanding:
        raise ValueError("発行済株式数が取得できませんでした。")
    return company_name, price, shares_outstanding


def _fetch_financials_from_investor_docs(code: str) -> tuple[float, float, float]:
    # 実運用では各社IR資料/有報を参照。自動化としてIR BANK/Kabutanをフォールバック利用。
    errors: list[str] = []
    for url in [f"https://irbank.net/{code}", f"https://irbank.net/{code}?f=S", f"https://kabutan.jp/stock/finance?code={code}"]:
        try:
            html = _fetch_html(url)
            mul = _extract_multiplier(html)
            current_assets = _parse_table_value(html, ["流動資産", "流動資産合計"], mul)
            investments = _parse_table_value(html, ["投資有価証券", "投資その他の資産"], mul)
            liabilities = _parse_table_value(html, ["負債合計", "負債", "負債の部合計"], mul)
            if current_assets > 0 and liabilities > 0:
                return current_assets, investments, liabilities
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise ValueError("IRライブラリ相当データ取得に失敗しました: " + " | ".join(errors))


def fetch_symbol_data(code: str) -> dict:
    company_name, price, shares_outstanding = _fetch_market_data_from_jpx(code)
    current_assets, investments, liabilities = _fetch_financials_from_investor_docs(code)

    market_cap = price * shares_outstanding
    net_cash = current_assets + (investments * 0.7) - liabilities
    ncr = net_cash / market_cap if market_cap else 0
    undervalued = ncr <= 1

    return {
        "timestamp": datetime.now(timezone.utc).astimezone().strftime("%y/%m/%d"),
        "code": code,
        "company_name": company_name,
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
    writer.writerow(["現在時刻", "証券コード", "企業名", "直近の株価", "流動資産", "投資有価証券", "負債", "時価総額", "ネットキャッシュ", "ネットキャッシュ比率", "真の割安株か否か"])
    writer.writerow([
        data["timestamp"], data["code"], data["company_name"], data["price_formatted"], data["current_assets_formatted"],
        data["investments_formatted"], data["liabilities_formatted"], data["market_cap_formatted"], data["net_cash_formatted"],
        data["ncr_formatted"], data["result_text"],
    ])

    return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=net_cash_ratio_{code}.csv"})


if __name__ == "__main__":
    app.run(debug=True)
