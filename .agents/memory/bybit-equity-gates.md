---
name: Bybit equity and exposure gates
description: Safety rule for admitting new Bybit Demo orders under an exposure cap and an equity floor
---

Новые Bybit Demo-ордера должны проходить два независимых gate-а: `open nominal exposure + new order notional <= max exposure` и `equity >= reserve`, где equity — balance плюс unrealized PnL открытых позиций.

**Why:** Формула `equity - reserve >= max exposure` превращает небольшую нереализованную просадку в блокировку всей торговли и смешивает лимит экспозиции с аварийным буфером.

**How to apply:** По умолчанию держать max exposure `$500` и equity reserve `$100`; равенство границе разрешает новый ордер. Gate блокирует только новые ордера и не закрывает/не изменяет уже открытые позиции.