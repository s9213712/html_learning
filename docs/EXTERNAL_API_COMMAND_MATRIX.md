# External API Command Matrix

This document inventories the third-party APIs currently integrated by
`hackme_web`, which commands/endpoints are already used, and which upstream
capabilities remain available but are not wired today.

Scope:

- trading price / order book providers
- Civitai model inspection / download
- ComfyUI backend API

This is not an exhaustive vendor manual for every upstream product. It is the
project's current integration matrix.

External repositories, local executables, and separately running services are
documented in [EXTERNAL_INTEGRATION_PLAYBOOK.md](EXTERNAL_INTEGRATION_PLAYBOOK.md).
Use that playbook for Blockfish/Stockfish, Kociemba/Rubik solver, BTC_trade,
ComfyUI process setup, Civitai model import, and KataGo runtime boundaries.

## Trading Price Providers

The trading engine currently integrates public market-data endpoints only. It
does **not** place real orders with exchanges.

### Binance

Used now:

- `GET https://api.binance.com/api/v3/ticker/price?symbol=<SYMBOL>`
  - live ticker fallback
- `GET https://api.binance.com/api/v3/depth?symbol=<SYMBOL>&limit=<N>`
  - `auto_depth` / price-fusion order book snapshot
- `GET https://api.binance.com/api/v3/klines?symbol=<SYMBOL>&interval=<I>&limit=<N>`
  - reference chart candles and indicator input

Currently used for:

- live price
- fused/reference price
- trading reference chart
- workflow indicator candles

Not currently used but available upstream:

- book ticker
- recent trades / aggTrades
- 24h statistics
- websocket depth / ticker streams

### OKX

Used now:

- `GET https://www.okx.com/api/v5/market/ticker?instId=<INST_ID>`
- `GET https://www.okx.com/api/v5/market/books?instId=<INST_ID>&sz=<N>`

Currently used for:

- live price fallback
- price-fusion order book snapshot

Not currently used but available upstream:

- candles
- trades
- index / mark price
- websocket books / ticker

### Coinbase Exchange

Used now:

- `GET https://api.exchange.coinbase.com/products/<PRODUCT_ID>/ticker`
- `GET https://api.exchange.coinbase.com/products/<PRODUCT_ID>/book?level=2`

Currently used for:

- live price fallback
- price-fusion order book snapshot

Not currently used but available upstream:

- candles
- trades
- websocket level2 feeds

### Kraken

Used now:

- `GET https://api.kraken.com/0/public/Ticker?pair=<PAIR>`
- `GET https://api.kraken.com/0/public/Depth?pair=<PAIR>&count=<N>`

Currently used for:

- live price fallback
- price-fusion order book snapshot

Not currently used but available upstream:

- OHLC
- trades
- websocket depth / ticker

### Gemini

Used now:

- `GET https://api.gemini.com/v2/ticker/<SYMBOL>`
- `GET https://api.gemini.com/v1/book/<SYMBOL>?limit_bids=<N>&limit_asks=<N>`

Currently used for:

- live price fallback
- price-fusion order book snapshot

Not currently used but available upstream:

- trades
- candles / historical data through other endpoints
- websocket market data

### Bitstamp

Used now:

- `GET https://www.bitstamp.net/api/v2/ticker/<PAIR>/`
- `GET https://www.bitstamp.net/api/v2/order_book/<PAIR>/`

Currently used for:

- live price fallback
- price-fusion order book snapshot

Not currently used but available upstream:

- OHLC
- transactions / trades
- websocket order book / trades

### CoinGecko

Used now:

- `GET https://api.coingecko.com/api/v3/simple/price?ids=<ID>&vs_currencies=usd`

Currently used for:

- last-resort ticker fallback only

Not currently used but available upstream:

- market chart
- coins/markets
- exchange tickers

## Trading Provider Mapping In This Repo

Current market catalog support:

| Internal Market | Display Alias | Binance | OKX | Coinbase | Kraken | Gemini | Bitstamp | CoinGecko |
|---|---|---|---|---|---|---|---|---|
| `BTC/POINTS` | `BTC/USDT` | `BTCUSDT` | `BTC-USDT` | `BTC-USD` | `XBTUSD` | `btcusd` | `btcusd` | `bitcoin` |
| `ETH/POINTS` | `ETH/USDT` | `ETHUSDT` | `ETH-USDT` | `ETH-USD` | `ETHUSD` | `ethusd` | `ethusd` | `ethereum` |
| `XRP/POINTS` | `XRP/USDT` | `XRPUSDT` | `XRP-USDT` | `XRP-USD` | `XRPUSD` | - | `xrpusd` | `ripple` |
| `BNB/POINTS` | `BNB/USDT` | `BNBUSDT` | `BNB-USDT` | - | - | - | - | `binancecoin` |
| `PAXG/POINTS` | `PAXG/USDT` | `PAXGUSDT` | - | - | - | - | - | `pax-gold` |

Source:

- `services/trading/markets.py`

## Civitai

Root-only ComfyUI model management uses Civitai for search, inspection, and download.

### Inputs accepted by this project

- model page URL such as:
  - `https://civitai.com/models/<model_id>`
  - `https://civitai.com/models/<model_id>?modelVersionId=<version_id>`

### Used now

- `GET https://civitai.com/api/v1/models?query=...&types=...&baseModels=...&nsfw=...`
  - keyword search with base-model / type / Safe-NSFW filters for the root-only ComfyUI import panel
- `GET https://civitai.com/api/v1/models/<model_id>`
  - inspect model metadata, versions, files, base model, trained words
- `GET https://civitai.com/api/download/models/<version_id>`
  - fallback download URL when file download URL is not already provided
- direct file `downloadUrl` from Civitai version/file metadata
  - actual checkpoint / LoRA / embedding / VAE download

Current project behavior:

- validates public/Civitai host
- appends token for authenticated download
- saves a sidecar `.civitai.json`
- stores `base_model` and `trained_words`

Not currently used but available upstream:

- creator/profile endpoints
- image browsing endpoints
- tag/category discovery
- richer moderation / version history queries

## ComfyUI Backend

These are the upstream ComfyUI API commands currently exercised by the project.

### Used now

- `GET /system_stats`
  - health check
- `GET /object_info/CheckpointLoaderSimple`
  - model list
- `GET /object_info/LoraLoader`
  - LoRA list
- `GET /object_info/VAELoader`
  - VAE list
- `GET /object_info/KSampler`
  - sampler/scheduler options
- `GET /embeddings`
  - embedding list
- `POST /prompt`
  - submit generation workflow
- `GET /history/<prompt_id>`
  - poll generation completion
- `GET /view?filename=<>&subfolder=<>&type=<>`
  - fetch generated image bytes
- `POST /interrupt`
  - interrupt generation
- `WS /ws?clientId=<client_id>`
  - realtime progress/events when websocket is available
- `POST /history` with `{"delete": [prompt_id]}`
  - delete history record during discard flow
- `DELETE /view?...`
  - conditionally attempted only when API delete is available; local mode
    normally prefers direct filesystem delete instead

Current project behavior built on top of those commands:

- status / connection test
- local start / root stop wrappers
- model/LoRA/VAE/embedding discovery
- billing quote + generation
- async job progress
- interrupt
- save/discard/share generated image

### Available upstream but not used in this repo yet

- upload image / image-to-image style helpers
- queue inspection / queue clear endpoints
- extension discovery / plugin-specific routes
- custom node management
- richer workflow import/export helpers

## Security Notes

- Exchange integrations are market-data only, not custodial trading APIs.
- Civitai API key is root-only and used for inspection/download, not exposed to
  normal users.
- ComfyUI remote mode is treated as an untrusted external generation backend;
  the project fetches only returned image outputs and stores them through Cloud
  Drive policy.

## Code Pointers

- exchange market data:
  `services/trading/engine.py`
- market symbol registry:
  `services/trading/markets.py`
- ComfyUI upstream client:
  `services/comfyui/client.py`
- Civitai + ComfyUI route integration:
  `routes/comfyui.py`
