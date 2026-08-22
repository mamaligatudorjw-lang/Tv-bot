---
name: Shadow cooldown persistence
description: Shadow-уведомления не должны повторяться после перезапуска процесса.
---

Каждая shadow-стратегия с per-symbol cooldown должна восстанавливать последний сигнал из постоянного хранилища при старте, иначе перезапуск сбрасывает память и повторно отправляет тот же сигнал.

**Why:** In-memory cooldowns disappear on workflow restarts, while the existing shadow position remains in the database; this produces duplicate Telegram notifications for the same setup.

**How to apply:** При добавлении новой shadow-стратегии проверять не только cooldown перед отправкой, но и его restore из `demo_positions`/журнала после старта.