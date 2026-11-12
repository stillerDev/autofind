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

## Результат vs Kensaku v1.0.17

Warm ×4.6, Cold ×2.2, Контент ×3.2, Имена ×2, Индексация ×1.02, Размер ⚪, RSS ⚪
