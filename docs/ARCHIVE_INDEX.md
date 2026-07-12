# Documentation Archive Index

本索引記錄 `docs/` 內不再作為第一入口、但仍需保留追溯價值的資料。歸檔原則是不刪證據、不覆蓋歷史，只把一次性報告與實驗輸出移出日常入口。

## 目前歸檔

| 路徑 | 內容 | 狀態 |
|---|---|---|
| `archive/competition_2026-05-06/` | Workflow Template Competition 回測競賽報告、方法、資料腳本 | 歷史證據包；若要重跑，先檢查內部硬編碼輸出路徑 |
| `archive/agent_qa_reports/` | 舊 AGENTS QA 報告與 2026-06-23 AI Agent media context 稽核 | 歷史 QA 證據；新報告仍寫回 `AGENTS/reports/` |
| `archive/history/` | 專案歷史、已完成 phase plan、舊分支故事與已放棄方向 | 歷史脈絡；不是現行操作指南 |
| `archive/pointschain_rc1/` | 已完成的 RC1 scope、signoff、load profile 與 scanner baseline | 歷史 release 證據；現行入口見 `BLOCKCHAIN/README.md` 與 `RELEASE/RC1_RELEASE_GATE.md` |
| `archive/FINAL_CODE_REVIEW_2026-05-14.md` | 2026-05-14 一次性 code review / freeze report | 歷史審查證據；部署者不要把它當成現行 runbook |
| `games/archive/` | 西洋棋 debug、exp3/exp4/exp5 歷史 ledger、舊 replay 證據 | 遊戲 AI 歷史紀錄；主入口見 `games/ARCHIVE_INDEX.md` |

## 維護規則

- 日常操作文件留在 `docs/` 根層或各 domain 目錄。
- 一次性 benchmark、競賽、過期 QA 報告放 `archive/`。
- 大型可機讀證據依 domain 留在該 domain 的 `evidence/` 或 `experiments/`。
- 若搬移會破壞常用入口，保留相容 README 指到新位置。
