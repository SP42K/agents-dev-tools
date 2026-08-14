# Project Plan: Example

整體目標與背景描述(implementer 每個 milestone 都會拿到全文 + 自己那段)。

## Milestone 1: 建立資料模型

- 定義 User / Session 資料模型
- 加上 migration
- 驗收條件:`pytest tests/test_models.py` 全綠

## Milestone 2: 實作 API endpoints

- CRUD endpoints for User
- 驗收條件:OpenAPI schema 通過 lint,整合測試全綠

## Milestone 3: 加上認證

- JWT 認證 middleware
- 驗收條件:未帶 token 的請求回 401
