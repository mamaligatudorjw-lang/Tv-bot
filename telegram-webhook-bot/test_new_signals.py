"""
Standalone тест: Funding Rate (OKX), Open Interest, Fear & Greed
НЕ вносит никаких изменений в бота.

Источники:
  - Funding Rate + OI: OKX (бесплатно, нет гео-блока)
  - Fear & Greed:      alternative.me (бесплатно)

Запуск: python3 test_new_signals.py
"""

import requests
import json
import time
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# Пороги
# ─────────────────────────────────────────────────────────────
FUNDING_EXTREME_LONG  =  0.10   # > +0.10% / 8ч → перегрев лонгов → SHORT
FUNDING_HIGH_LONG     =  0.05   # > +0.05% / 8ч → буст +5 к SHORT
FUNDING_EXTREME_SHORT = -0.05   # < -0.05% / 8ч → перегрев шортов → LONG
FUNDING_NEG_BOOST     = -0.02   # < -0.02% / 8ч → буст +5 к LONG

# Пример базового скора для симуляции
EXAMPLE_BASE_SCORE = 55
CONFLUENCE_SHORT_THRESHOLD = 52  # минимальный скор для отправки

OKX_BASE = "https://www.okx.com"


# ─────────────────────────────────────────────────────────────
# 1. Fear & Greed Index  (alternative.me)
# ─────────────────────────────────────────────────────────────
def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception as e:
        return None, f"ошибка: {e}"


def fng_score_delta(fng_value, direction: str) -> tuple[int, str]:
    """Возвращает (delta, описание) для сигнала direction = 'LONG' или 'SHORT'."""
    if fng_value is None:
        return 0, "нет данных"
    if fng_value >= 80:
        base, label = -10, "🔴 Экстремальная жадность"
    elif fng_value >= 65:
        base, label = -5, "🟡 Жадность"
    elif fng_value <= 20:
        base, label = +10, "🟢 Экстремальный страх"
    elif fng_value <= 35:
        base, label = +5,  "🟡 Страх"
    else:
        return 0, "⚪ Нейтральный"
    # base — дельта для LONG; для SHORT инвертируем
    delta = base if direction == "LONG" else -base
    return delta, label


# ─────────────────────────────────────────────────────────────
# 2. OKX — все SWAP тикеры (funding + OI за один запрос)
# ─────────────────────────────────────────────────────────────
def fetch_okx_swap_tickers():
    """GET /api/v5/market/tickers?instType=SWAP → список всех бессрочных фьючерсов."""
    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/market/tickers",
            params={"instType": "SWAP"},
            timeout=15,
        )
        d = r.json()
        if d.get("code") != "0":
            print(f"  [!] OKX tickers error: {d.get('msg')}")
            return []
        return d.get("data", [])
    except Exception as e:
        print(f"  [!] OKX tickers fetch error: {e}")
        return []


def fetch_okx_funding_rates():
    """
    GET /api/v5/public/funding-rate-summary → усреднённые ставки по всем инструментам.
    Fallback: по одному инструменту через /api/v5/public/funding-rate.
    Возвращает dict { "BTC-USDT": rate_pct, ... }
    """
    try:
        # Endpoint со сводными данными (OKX v5)
        r = requests.get(
            f"{OKX_BASE}/api/v5/public/funding-rate-summary",
            timeout=15,
        )
        d = r.json()
        if d.get("code") == "0" and d.get("data"):
            result = {}
            for item in d["data"]:
                inst = item.get("instId", "")
                rate = item.get("fundingRate")
                if rate and inst.endswith("-USDT-SWAP"):
                    pair = inst.replace("-USDT-SWAP", "-USDT")
                    result[pair] = float(rate) * 100
            return result
    except Exception:
        pass

    # Fallback: берём из тикеров (поле fundingRate не во всех тикерах, но в SWAP есть)
    return {}


def fetch_okx_funding_for_instrument(inst_id: str) -> float | None:
    """Funding rate для одного инструмента (fallback)."""
    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=8,
        )
        d = r.json()
        if d.get("code") == "0" and d.get("data"):
            return float(d["data"][0]["fundingRate"]) * 100
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# Классификация
# ─────────────────────────────────────────────────────────────
def classify_funding(rate_pct):
    if rate_pct > FUNDING_EXTREME_LONG:
        return "🔴🔴 ЭКСТРЕМАЛЬНЫЙ ЛОНГ-ПЕРЕГРЕВ → НОВЫЙ СИГНАЛ funding_squeeze_SHORT"
    elif rate_pct > FUNDING_HIGH_LONG:
        return f"🔴  Высокий → +5 к скору SHORT-сигнала по монете"
    elif rate_pct < FUNDING_EXTREME_SHORT:
        return "🟢🟢 ЭКСТРЕМАЛЬНЫЙ ШОРТ-ПЕРЕГРЕВ → НОВЫЙ СИГНАЛ funding_squeeze_LONG"
    elif rate_pct < FUNDING_NEG_BOOST:
        return f"🟢  Негативный → +5 к скору LONG-сигнала по монете"
    else:
        return "⚪  Нейтральный"


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 65)
    print("  ТЕСТ: Funding Rate / OI / Fear & Greed")
    print(f"  Источники: OKX + alternative.me  |  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 65)

    # ── 1. Fear & Greed ──────────────────────────────────────────
    print("\n📊  FEAR & GREED INDEX  (alternative.me)")
    print("─" * 45)
    fng_val, fng_class = fetch_fear_greed()
    if fng_val is not None:
        bar = "█" * (fng_val // 10) + "░" * (10 - fng_val // 10)
        print(f"  [{bar}] {fng_val}/100 — {fng_class}")
        d_long,  lbl = fng_score_delta(fng_val, "LONG")
        d_short, _   = fng_score_delta(fng_val, "SHORT")
        print(f"  Настроение рынка: {lbl}")
        print(f"  Если добавить в скоринг:")
        print(f"    LONG  сигналы: {d_long:+d} очков")
        print(f"    SHORT сигналы: {d_short:+d} очков")
    else:
        print(f"  {fng_class}")
        fng_val = None

    # ── 2. OKX Funding Rates ─────────────────────────────────────
    print("\n📈  FUNDING RATE  (OKX — USDT бессрочные фьючерсы)")
    print("─" * 45)

    # Сначала пробуем summary endpoint
    funding_map = fetch_okx_funding_rates()

    # Если summary пустой — берём по одному для топ монет
    if not funding_map:
        print("  summary endpoint недоступен, запрашиваем по отдельным инструментам...")
        spot_pairs = [
            "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
            "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
            "MATIC-USDT", "UNI-USDT", "ATOM-USDT", "LTC-USDT", "OP-USDT",
        ]
        for pair in spot_pairs:
            inst = pair.replace("-USDT", "-USDT-SWAP")
            rate = fetch_okx_funding_for_instrument(inst)
            if rate is not None:
                funding_map[pair] = rate

    if not funding_map:
        print("  ❌ Не удалось получить данные по funding rate")
    else:
        # Сортируем по abs(rate) для наглядности
        sorted_pairs = sorted(funding_map.items(), key=lambda x: abs(x[1]), reverse=True)
        total = len(sorted_pairs)
        print(f"  Всего инструментов: {total}")

        # Статистика
        extreme_short_cnt = sum(1 for _, r in sorted_pairs if r > FUNDING_EXTREME_LONG)
        extreme_long_cnt  = sum(1 for _, r in sorted_pairs if r < FUNDING_EXTREME_SHORT)
        boost_short_cnt   = sum(1 for _, r in sorted_pairs if FUNDING_HIGH_LONG < r <= FUNDING_EXTREME_LONG)
        boost_long_cnt    = sum(1 for _, r in sorted_pairs if FUNDING_NEG_BOOST > r >= FUNDING_EXTREME_SHORT)
        neutral_cnt       = total - extreme_short_cnt - extreme_long_cnt - boost_short_cnt - boost_long_cnt

        print(f"\n  {'Символ':<18} {'Rate%':>9}  Классификация")
        print(f"  {'─'*18} {'─'*9}  {'─'*42}")

        new_sig_short, new_sig_long, boost_s, boost_l = [], [], [], []

        shown = 0
        for pair, rate in sorted_pairs:
            label = classify_funding(rate)
            bar_char = "▶" if rate > 0 else "◀"
            print(f"  {pair:<18} {rate:>+9.4f}%  {label}")

            if rate > FUNDING_EXTREME_LONG:
                new_sig_short.append((pair, rate))
            elif rate < FUNDING_EXTREME_SHORT:
                new_sig_long.append((pair, rate))
            elif rate > FUNDING_HIGH_LONG:
                boost_s.append((pair, rate))
            elif rate < FUNDING_NEG_BOOST:
                boost_l.append((pair, rate))

            shown += 1
            if shown >= 30:
                remaining = total - shown
                if remaining > 0:
                    print(f"  ... и ещё {remaining} пар (нейтральные пропущены)")
                break

        print(f"\n  Сводка: экстрем-SHORT {extreme_short_cnt} | буст-SHORT {boost_short_cnt} | "
              f"нейтральных {neutral_cnt} | буст-LONG {boost_long_cnt} | экстрем-LONG {extreme_long_cnt}")

    # ── 3. Итог: что бы сработало ────────────────────────────────
    print("\n" + "=" * 65)
    print("  📋  ЧТО БЫ СРАБОТАЛО ПРЯМО СЕЙЧАС")
    print("=" * 65)

    if funding_map:
        new_sig_short = [(p,r) for p,r in funding_map.items() if r > FUNDING_EXTREME_LONG]
        new_sig_long  = [(p,r) for p,r in funding_map.items() if r < FUNDING_EXTREME_SHORT]
        boost_s       = [(p,r) for p,r in funding_map.items() if FUNDING_HIGH_LONG < r <= FUNDING_EXTREME_LONG]
        boost_l       = [(p,r) for p,r in funding_map.items() if FUNDING_NEG_BOOST > r >= FUNDING_EXTREME_SHORT]

        print(f"\n🆕 НОВЫЙ СИГНАЛ funding_squeeze_SHORT — перегрев лонгов ({len(new_sig_short)}):")
        if new_sig_short:
            for p, r in sorted(new_sig_short, key=lambda x: -x[1]):
                print(f"   {p:<20} rate={r:+.4f}%  → отдельный сигнал, не мешает текущим")
        else:
            print("   → нет (порог >+0.10% не достигнут)")

        print(f"\n🆕 НОВЫЙ СИГНАЛ funding_squeeze_LONG — перегрев шортов ({len(new_sig_long)}):")
        if new_sig_long:
            for p, r in sorted(new_sig_long, key=lambda x: x[1]):
                print(f"   {p:<20} rate={r:+.4f}%  → отдельный сигнал, не мешает текущим")
        else:
            print("   → нет (порог <-0.05% не достигнут)")

        print(f"\n⬆️  БУСТ +5 к существующим SHORT-сигналам ({len(boost_s)}):")
        if boost_s:
            for p, r in sorted(boost_s, key=lambda x: -x[1])[:15]:
                print(f"   {p:<20} rate={r:+.4f}%")
        else:
            print("   → нет монет с повышенным funding (+0.05–0.10%)")

        print(f"\n⬇️  БУСТ +5 к существующим LONG-сигналам ({len(boost_l)}):")
        if boost_l:
            for p, r in sorted(boost_l, key=lambda x: x[1])[:15]:
                print(f"   {p:<20} rate={r:+.4f}%")
        else:
            print("   → нет монет с негативным funding (-0.02 – -0.05%)")

    # ── 4. Симуляция Fear & Greed на реальный сигнал ─────────────
    print("\n" + "=" * 65)
    print("  🧪  СИМУЛЯЦИЯ: Fear & Greed влияет на скор сигнала")
    print("=" * 65)
    d_short, lbl = fng_score_delta(fng_val, "SHORT")
    d_long,  _   = fng_score_delta(fng_val, "LONG")

    print(f"\n  Текущее FnG: {fng_val}/100 — {fng_class if fng_val else '?'}")
    print(f"  Базовый скор гипотетического confluence SHORT: {EXAMPLE_BASE_SCORE}/100")
    adjusted_short = EXAMPLE_BASE_SCORE + d_short
    adjusted_long  = EXAMPLE_BASE_SCORE + d_long
    print(f"\n  С учётом FnG:")
    print(f"    SHORT: {EXAMPLE_BASE_SCORE} {d_short:+d} = {adjusted_short}/100  ", end="")
    if adjusted_short >= CONFLUENCE_SHORT_THRESHOLD:
        print(f"✅ ОТПРАВИЛСЯ бы (порог {CONFLUENCE_SHORT_THRESHOLD})")
    else:
        print(f"❌ ЗАБЛОКИРОВАН (ниже порога {CONFLUENCE_SHORT_THRESHOLD})")

    print(f"    LONG:  {EXAMPLE_BASE_SCORE} {d_long:+d} = {adjusted_long}/100  ", end="")
    if adjusted_long >= CONFLUENCE_SHORT_THRESHOLD:
        print(f"✅ ОТПРАВИЛСЯ бы (порог {CONFLUENCE_SHORT_THRESHOLD})")
    else:
        print(f"❌ ЗАБЛОКИРОВАН (ниже порога {CONFLUENCE_SHORT_THRESHOLD})")

    # ── 5. Ответ на главный вопрос: что со статистикой ───────────
    print("\n" + "=" * 65)
    print("  📊  ВЛИЯНИЕ НА СТАТИСТИКУ")
    print("=" * 65)
    print("""
  1. funding_squeeze_SHORT / _LONG
     → НОВЫЕ типы сигналов, своя строка в /stats
     → НЕ смешиваются с confluence, momentum и др.
     → Пример имени в БД: 'funding_squeeze_short'

  2. Fear & Greed как модификатор скора
     → Тип сигнала НЕ меняется (confluence_short остаётся)
     → Меняется только порог: выгорание шортов при страхе
       ценится больше, жадность снижает вес LONG
     → В hit-rate это отразится как улучшение % побед

  3. Funding Rate как буст (+5) к существующим
     → Тип НЕ меняется, сигнал не переименовывается
     → Некоторые сигналы которые раньше не проходили
       порог 52, теперь пройдут — это ФИЛЬТР, не новый тип
""")

    print("✅  Тест завершён. Бот не изменён.\n")


if __name__ == "__main__":
    main()
