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

APP_VERSION = "2026-05-22-rebuild5"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _yen(value: float | int) -> str:
    return f"{int(round(value)):,}円"


def _to_number(value: str) -> float:
    cleaned = re.sub(r"\s+", "", value)
    cleaned = cleaned.replace(",", "").replace("円", "").replace("株", "")
    cleaned = cleaned.replace("△", "-").replace("▲", "-")
    if not cleaned:
        return 0.0
    return float(cleaned)


def _fetch_html(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en-US;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=25) as res:
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
        text = unescape(m.group(1))
        text = re.sub(r"<.*?>", "", text)
        try:
            return _to_number(text)
        except ValueError:
            continue
    return 0.0


def _extract_company_name(html: str, code: str) -> str:
    patterns = [
        r"<title>\s*([^<\-｜\|]+?)[\-｜\|]",
        r'"name"\s*:\s*"([^"]+?)"',
        r"<h1[^>]*>(.*?)</h1>",
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        text = unescape(re.sub(r"<.*?>", "", m.group(1))).strip()
        text = re.sub(r"\s+", " ", text)
        if text and len(text) < 80:
            return text
    return f"{code}.T"


def _extract_irbank_multiplier(html: str) -> int:
    if re.search(r"単位[:：]\s*百万円", html):
        return 1_000_000
    if re.search(r"単位[:：]\s*千円", html):
        return 1_000
    if re.search(r"単位[:：]\s*円", html):
        return 1
    return 1_000_000


def _parse_irbank_value(html: str, labels: list[str], multiplier: int) -> float:
    for label in labels:
        pattern = rf"<th[^>]*>\s*{re.escape(label)}\s*</th>\s*<td[^>]*>(.*?)</td>"
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            continue
        text = re.sub(r"<.*?>", "", m.group(1))
        text = unescape(text).strip()
        if not text or text == "-":
            continue
        try:
            return _to_number(text) * multiplier
        except ValueError:
            continue
    return 0.0




def _parse_table_value(html: str, labels: list[str], multiplier: int = 1) -> float:
    for label in labels:
        pattern = rf"(?:<th[^>]*>|<dt[^>]*>)\s*{re.escape(label)}\s*(?:</th>|</dt>)\s*(?:<td[^>]*>|<dd[^>]*>)(.*?)(?:</td>|</dd>)"
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        text = re.sub(r"<.*?>", "", unescape(m.group(1))).strip()
        if not text or text == "-":
            continue
        try:
            return _to_number(text) * multiplier
        except ValueError:
            continue
    return 0.0

def _fetch_irbank_financials(code: str) -> tuple[float, float, float]:
    candidates = [
        f"https://irbank.net/{code}",
        f"https://irbank.net/{code}?f=S",
        f"https://irbank.net/{code}/results",
        f"https://irbank.net/{code}/financial",
    ]
    last_error = None
    for url in candidates:
        try:
            html = _fetch_html(url)
            multiplier = _extract_irbank_multiplier(html)
            current_assets = _parse_irbank_value(html, ["流動資産", "流動資産合計"], multiplier)
            investments = _parse_irbank_value(html, ["投資有価証券", "投資その他の資産"], multiplier)
            liabilities = _parse_irbank_value(html, ["負債合計", "負債の部合計", "負債"], multiplier)
            if current_assets > 0 and liabilities > 0:
                return current_assets, investments, liabilities
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise ValueError(f"IR BANKから主要財務データを取得できませんでした: {last_error}")
    raise ValueError("IR BANKから主要財務データを取得できませんでした。")




def _fetch_kabutan_financials(code: str) -> tuple[float, float, float]:
    urls = [
        f"https://kabutan.jp/stock/finance?code={code}",
        f"https://kabutan.jp/stock/?code={code}",
    ]
    last_error: Exception | None = None
    for url in urls:
        try:
            html = _fetch_html(url)
            multiplier = 1_000_000 if re.search(r"単位[:：]\s*百万円", html) else 1
            current_assets = _parse_table_value(html, ["流動資産", "流動資産合計"], multiplier)
            investments = _parse_table_value(html, ["投資有価証券", "投資その他の資産"], multiplier)
            liabilities = _parse_table_value(html, ["負債合計", "負債", "負債の部合計"], multiplier)
            if current_assets > 0 and liabilities > 0:
                return current_assets, investments, liabilities
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise ValueError(f"Kabutanから主要財務データを取得できませんでした: {last_error}")
    raise ValueError("Kabutanから主要財務データを取得できませんでした。")

def fetch_symbol_data(code: str) -> dict:
    ticker = f"{code}.T"

    quote_html = _fetch_first_available([
        f"https://finance.yahoo.co.jp/quote/{quote(ticker)}",
        f"https://kabutan.jp/stock/?code={code}",
        f"https://finance.yahoo.com/quote/{quote(ticker)}",
    ])

    company_name = _extract_company_name(quote_html, code)

    price = _extract_first_number(
        quote_html,
        [
            r'<span[^>]*class="[^"]*\b_3rXWJKZF\b[^"]*"[^>]*>([0-9,\.]+)</span>',
            r'"regularMarketPrice"\s*:\s*\{\s*"raw"\s*:\s*([0-9\.]+)',
            r'<fin-streamer[^>]*data-field="regularMarketPrice"[^>]*value="([0-9\.]+)"',
            r'現在値[^0-9]{0,20}([0-9,]+)',
            r'株価[^0-9]{0,20}([0-9,]+)',
        ],
    )

    shares_outstanding = _extract_first_number(
        quote_html,
        [
            r"発行済株式数[^0-9]{0,30}([0-9,]+)",
            r'"sharesOutstanding"\s*:\s*\{\s*"raw"\s*:\s*([0-9]+)',
            r'発行済株式総数[^0-9]{0,30}([0-9,]+)',
        ],
    )

    ir_html_for_shares = _fetch_first_available([
        f"https://irbank.net/{code}",
        f"https://irbank.net/{code}?f=S",
    ])
    if not shares_outstanding:
        shares_outstanding = _extract_first_number(
            ir_html_for_shares,
            [
                r"発行済株式総数[^0-9]{0,40}([0-9,]+)",
                r"発行済株式数[^0-9]{0,40}([0-9,]+)",
            ],
        )

    if not price:
        raise ValueError("株価が取得できませんでした。")
    if not shares_outstanding:
        raise ValueError("発行済株式数が取得できませんでした。")

    try:
        current_assets, investments, liabilities = _fetch_irbank_financials(code)
    except ValueError:
        current_assets, investments, liabilities = _fetch_kabutan_financials(code)

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
