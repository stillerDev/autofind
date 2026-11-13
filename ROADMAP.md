# AutoFind ROADMAP — запомнить навсегда

## Оставшиеся фичи (приоритет по эффекту)

| # | Фича | Эффект | Усилия | Статус |
|---|---|---|---|---|
| 7 | Контентно-адресованные чанки | размер 114→60 МБ, контент ещё ×2 | 3-4ч | TODO |
| 8 | USS 146→105 (nameindex + BM25 CSR в mmap) | паритет памяти с kensaku | 2ч | TODO |
| 9 | .msg Outlook | корпоративный сегмент | 2ч | TODO |
| 10 | zh-CN UI | рынок 1.4 млрд | 1ч | TODO |
| 1 | Параноидальный режим v2: пароль (Argon2id) | "Поисковик который шифрует" | 2ч | TODO |
| 2 | Визуальный поиск (CLIP) — "найди скриншот с красным графиком" | kensaku не умеет | 4ч | TODO |

## Уже сделано в этой сессии

- In-RAM BM25 (×6.5 контент), LUT scoring, mmap блобы, blob vocab, uint16 tfs
- Сканер ×30, батчи IPC, 8 воркеров, AND-first FTS
- Параноидальный режим AES-256-GCM + DPAPI + кнопка в UI + полоска %
- Рельс слева (6 кружков), дровер с 5 вкладками 1080px, живые статусы
- Контекстное меню, dbl-Enter, drag-drop
- GitHub updater (core/updater.py), lazy BM25 sidecar для CLI
- rowid alias fix, _repack seal-check, segfault fix, NAME_CACHE eviction

## Результат vs Kensaku v1.0.17 (100k корпус, 2026-11-14)

Cold ×2.1 (295 vs 624 ms), Warm ×2.6 (8.9 vs 23.2 ms), RSS −24% (99 vs 131 MiB USS),
Размер ⚪ (109+42 vs 112 MB), Контент-покрытие 104k чанков vs 8192 docs, CLIP ru/zh — только у нас.
Память теперь НЕ паритет, а победа: sidecar nameindex реально работает (44 МБ блобов больше не в RAM),
CLIP-сессии ленивые и по одной башне.
