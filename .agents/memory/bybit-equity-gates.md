---
name: Bybit equity and exposure gates
description: Safety rule for admitting new Bybit Demo orders under an exposure cap and an equity floor
---

Новые Bybit Demo-ордера должны проходить два независимых gate-а: `live exchange open exposure + new order notional <= max exposure` и `equity >= reserve`, где exposure и unrealized PnL читаются из живых позиций Bybit, а equity — balance плюс unrealized PnL.

**Why:** Exposure должен включать netted-позиции и вклад параллельных runtime; расчёт по локальному ledger занизил бы риск. Отдельная формула `equity - reserve >= max exposure` также превращает небольшую нереализованную просадку в блокировку всей торговли.

**How to apply:** В коде default max exposure — `$500`, equity reserve — `$100`; production должен явно задавать env-переменную, если нужен лимит `$4000`. Равенство границе разрешает новый ордер. Gate блокирует только новые ордера и не закрывает/не изменяет уже открытые позиции.