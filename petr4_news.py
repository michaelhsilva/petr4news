#!/usr/bin/env python3
"""
Monitor de PETR4 -> notificações no Telegram.
  - Notícias novas (RSS) filtradas por palavras-chave.
  - Alerta de preço (API brapi.dev) quando a variação no dia passar de 1,5%
    (configurável com PRICE_THRESHOLD_PCT), uma vez por dia por direção.

Uso:
    pip install feedparser requests
    export TELEGRAM_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="123456789"

    python petr4_news.py              # roda uma vez (ideal para cron)
    python petr4_news.py --loop 10    # roda a cada 10 minutos

Na primeira execução, as notícias já existentes são só registradas
(sem notificar), para você não receber uma enxurrada de mensagens.
"""

import argparse
import html
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import requests

# ----------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------
DB_PATH = os.environ.get("PETR4_DB", "petr4_seen.db")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Palavras-chave (minúsculas). A notícia precisa conter pelo menos uma.
KEYWORDS = ["petr4", "petrobras", "petrobrás"]


def google_news_feed(query: str) -> str:
    """Monta a URL de um feed RSS do Google Notícias (Brasil)."""
    return (
        "https://news.google.com/rss/search?q="
        f"{quote_plus(query)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    )


# Fontes: adicione/remova à vontade. Qualquer URL de RSS/Atom funciona.
# O Google Notícias aceita operadores como site: e when:, o que permite
# "transformar" sites sem RSS em feeds.
FEEDS = {
    "Google Notícias - PETR4": google_news_feed("PETR4 when:1d"),
    "Google Notícias - Petrobras": google_news_feed("Petrobras ações when:1d"),
    "Fatos relevantes": google_news_feed("Petrobras \"fato relevante\" when:7d"),
    "RI Petrobras": google_news_feed(
        "site:investidorpetrobras.com.br Petrobras when:7d"
    ),
    # Exemplo de feed direto (confira a URL atual no site antes de usar):
    # "InfoMoney": "https://www.infomoney.com.br/feed/",
}

MAX_PER_RUN = 15  # limite de mensagens por rodada (evita flood)

# Alerta de preço: avisa quando a variação do dia passar deste limite (em %)
PRICE_SYMBOL = "PETR4"  # ticker na brapi.dev
BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")  # opcional para PETR4 (ticker de teste)
PRICE_THRESHOLD_PCT = float(os.environ.get("PRICE_THRESHOLD_PCT", "1.5"))
BRT = timezone(timedelta(hours=-3))  # horário de Brasília


# ----------------------------------------------------------------------
# Banco de dados (itens já vistos)
# ----------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        "id TEXT PRIMARY KEY, title TEXT, link TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_alerts ("
        "day TEXT, direction TEXT, PRIMARY KEY (day, direction))"
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, item_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen WHERE id = ?", (item_id,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, item_id: str, title: str, link: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen (id, title, link) VALUES (?, ?, ?)",
        (item_id, title, link),
    )
    conn.commit()


def db_is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


# ----------------------------------------------------------------------
# Coleta
# ----------------------------------------------------------------------
def matches_keywords(text: str) -> bool:
    text = text.lower()
    return any(k in text for k in KEYWORDS)


def fetch_items() -> list[dict]:
    """Lê todos os feeds e devolve itens únicos que batem com as palavras-chave."""
    items: dict[str, dict] = {}
    for source, url in FEEDS.items():
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # rede fora, feed quebrado etc.
            print(f"[aviso] falha ao ler '{source}': {exc}", file=sys.stderr)
            continue

        if parsed.bozo and not parsed.entries:
            print(f"[aviso] feed '{source}' sem entradas válidas.", file=sys.stderr)
            continue

        for entry in parsed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "")
            if not title or not link:
                continue
            if not matches_keywords(f"{title} {summary}"):
                continue
            item_id = entry.get("id") or link
            if item_id not in items:
                items[item_id] = {
                    "id": item_id,
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": entry.get("published_parsed"),
                }

    # Mais antigas primeiro, para as mensagens chegarem em ordem cronológica
    return sorted(items.values(), key=lambda i: i["published"] or time.gmtime(0))


# ----------------------------------------------------------------------
# Notificação (Telegram)
# ----------------------------------------------------------------------
def send_text(text: str) -> bool:
    """Envia um texto (HTML) para o chat configurado no Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[erro] defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.", file=sys.stderr)
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=15,
        )
        if resp.status_code == 429:  # limite do Telegram
            wait = resp.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait)
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[erro] Telegram: {exc}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------
# Notícia formatada
# ----------------------------------------------------------------------
def send_telegram(item: dict) -> bool:
    """Formata e envia uma notícia."""
    return send_text(
        f"📰 <b>{html.escape(item['title'])}</b>\n"
        f"<i>{html.escape(item['source'])}</i>",
        button=("🔗 Ler notícia", item["link"]),
    )


# ----------------------------------------------------------------------
# Alerta de preço
# ----------------------------------------------------------------------
def _market_day(value) -> str:
    """Data do pregão (horário de Brasília) a partir do campo de horário da API."""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return datetime.now(BRT).strftime("%Y-%m-%d")


def fetch_quote() -> dict | None:
    """Cotação atual e variação do dia via brapi.dev."""
    headers = {"Accept": "application/json"}
    if BRAPI_TOKEN:
        headers["Authorization"] = f"Bearer {BRAPI_TOKEN}"
    try:
        resp = requests.get(
            f"https://brapi.dev/api/quote/{PRICE_SYMBOL}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["results"][0]
        price = float(result["regularMarketPrice"])
        change_pct = float(result["regularMarketChangePercent"])
        prev = result.get("regularMarketPreviousClose")
        prev_close = float(prev) if prev else price / (1 + change_pct / 100)
        day = _market_day(result.get("regularMarketTime"))
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"[aviso] não consegui obter a cotação: {exc}", file=sys.stderr)
        return None

    return {
        "price": price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "day": day,
    }


def check_price(conn: sqlite3.Connection) -> None:
    """Avisa uma vez por dia (por direção) quando |variação| >= limite."""
    quote = fetch_quote()
    if quote is None:
        return

    change = quote["change_pct"]
    print(f"{PRICE_SYMBOL}: R$ {quote['price']:.2f} ({change:+.2f}% no dia)")
    if abs(change) < PRICE_THRESHOLD_PCT:
        return

    direction = "alta" if change > 0 else "queda"
    already = conn.execute(
        "SELECT 1 FROM price_alerts WHERE day = ? AND direction = ?",
        (quote["day"], direction),
    ).fetchone()
    if already:
        return

    emoji = "🟢" if change > 0 else "🔴"
    text = (
        f"{emoji} <b>PETR4 em {direction}: {change:+.2f}%</b>\n"
        f"Preço: R$ {quote['price']:.2f} "
        f"(fechamento anterior: R$ {quote['prev_close']:.2f})"
    )
    if send_text(text):
        conn.execute(
            "INSERT OR IGNORE INTO price_alerts (day, direction) VALUES (?, ?)",
            (quote["day"], direction),
        )
        conn.commit()


# ----------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------
def run_once() -> None:
    conn = init_db()
    check_price(conn)
    first_run = db_is_empty(conn)
    items = fetch_items()

    new_items = [i for i in items if not is_seen(conn, i["id"])]
    print(f"{len(items)} itens relevantes, {len(new_items)} novos.")

    if first_run:
        for item in new_items:
            mark_seen(conn, item["id"], item["title"], item["link"])
        print("Primeira execução: itens existentes registrados sem notificar.")
        conn.close()
        return

    sent = 0
    for item in new_items:
        if sent >= MAX_PER_RUN:
            print("Limite por rodada atingido; o restante vai na próxima.")
            break
        if send_telegram(item):
            mark_seen(conn, item["id"], item["title"], item["link"])
            sent += 1
            time.sleep(1)  # respeita o limite de mensagens do Telegram
    print(f"{sent} notificações enviadas.")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de notícias PETR4")
    parser.add_argument(
        "--loop",
        type=int,
        metavar="MIN",
        help="repete a cada MIN minutos (sem isso, roda uma vez)",
    )
    args = parser.parse_args()

    if args.loop:
        while True:
            run_once()
            time.sleep(args.loop * 60)
    else:
        run_once()


if __name__ == "__main__":
    main()
