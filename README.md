# YTDM Tool

**English** | [中文](#中文)

A lightweight YouTube channel / playlist download manager inspired by [Pinchflat](https://github.com/kieraneglin/pinchflat).
Single-page Web UI + Python/Flask backend, powered by yt-dlp.

---

## Changelog

### v1.1 (2026-04-02)

**New Features**
- **Fullscreen overlay player** — clicking a video when the floating player is disabled now opens a fullscreen overlay player with close / fullscreen controls
- **Three check modes** — choose in Settings: *Full* (fetch entire channel), *Recent-N* (fetch latest N videos), or *Fast* (skip prefetch, start downloading immediately)
- **Force stop download** — the "更新檢查" button changes to "同步檢查中…" while running; clicking again force-stops the download
- **YouTube title sync** — when a video's title changes on YouTube, the local folder and all files are automatically renamed to match (Full / Recent-N modes)
- **Media library cache** — switching to the Media tab is now instant; data is served from cache while a background refresh runs silently
- **Per-job cookies selector** — each channel card has its own cookies dropdown to override the global setting
- **Cookies upload validation** — the app checks for required Google auth tokens when you upload a cookies.txt file, with a warning if they are missing
- **Cookies tutorial modal** — a step-by-step guide inside the "Add Channel" page explains how to export cookies correctly

**Changes**
- "更新檢查" button moved to the channel card header (always visible, no need to expand)
- Cookie badge shown next to the channel name when cookies are configured
- Adding a duplicate channel URL now triggers an update check instead of creating a new entry

**Bug Fixes**
- **Cookies corruption** — yt-dlp was writing updated tokens back to the original cookies file; now uses a temporary copy during download to preserve the original
- **Service restart recovery** — jobs stuck in "running" state after a server restart are automatically reset to an error state on the next startup
- **Mobile floating player** — fixed: browser context menu appearing on long press, TV wall scrolling during drag, player width/height exceeding the viewport
- **Real error messages** — yt-dlp errors are now surfaced per-video in the queue instead of showing a generic failure message
- **Fast mode empty list** — switching to Fast mode no longer clears the existing video list immediately

---

## Features

### Download Management
- Batch download from YouTube channels, playlists, or single video URLs
- Supports both **Videos** and **Shorts**, displayed as separate sections in the queue
- Real-time per-video progress (percentage + speed)
- Automatic archive checking to skip already-downloaded videos

### Download Filters
- Quality selection (best / 720p / 480p / audio only)
- Duration range, upload date range, minimum view count
- Required keywords / exclude keywords (title-based filtering)
- Maximum video count limit

### Cookies Support
- Upload a cookies.txt file to download **age-restricted** or **members-only** videos
- Automatic validation on upload to verify the cookies contain required auth fields
- Built-in tutorial explaining how to correctly export cookies

### Media Library (TV Wall)
- Thumbnail grid with hover-to-preview
- Floating player (draggable, resizable, multi-window support) or fullscreen overlay player
- Sort by name / date / size, filter by channel, keyword search
- Infinite scroll for large libraries

### Other
- Duplicate video detection (groups same content from different channels)
- Docker deployment with configurable mount paths
- Mobile support (long-press to drag floating player)

---

## Quick Start

### Local

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

### Docker (Recommended)

**1. Edit paths in `docker-compose.yml`**

```yaml
volumes:
  - D:/YTDownload/downloads:/app/downloads   # video storage path
  - D:/YTDownload/data:/app/data             # settings, job history, cookies
```

**2. Start**

```bash
docker compose up -d --build
```

Open http://localhost:7321

---

## Cookies Setup (Age-Restricted / Members-Only Videos)

1. Install **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** browser extension
2. Log in to YouTube and open an **age-restricted video** — confirm it plays normally
3. Click the extension on that page and choose **"Export All Cookies"**
4. Upload the exported `.txt` file in the tool's "Add Channel" page

---

## File Structure

```
downloads/
└── channel-name/
    └── video-title/
        ├── video-title.mp4
        ├── video-title.webp        # thumbnail
        └── video-title.info.json
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DOWNLOAD_DIR` | `./data/downloads` | Video download path |
| `DATA_DIR` | `./data` | Settings, job history, cookies |

## Requirements

- Python 3.10+
- Node.js (included in Docker image, required for yt-dlp JS runtime)
- (Optional) ffmpeg — for merging video and audio into a single file

---

---

<a name="中文"></a>

# YTDM Tool

輕量化 YouTube 頻道 / 播放清單下載管理工具，靈感來自 [Pinchflat](https://github.com/kieraneglin/pinchflat)。
單頁 Web 介面 + Python/Flask 後端，核心下載引擎為 yt-dlp。

---

## 更新紀錄

### v1.1 (2026-04-02)

**新功能**
- **全螢幕覆蓋播放器** — 關閉懸浮視窗後，點擊電視牆影片將開啟全螢幕覆蓋播放器，支援關閉與進入全螢幕操作
- **三種同步檢查模式** — 可在設定頁面選擇：*完整*（抓取全部影片）、*最近 N 部*（只抓最新的 N 部）、*快速*（跳過預抓，直接開始下載）
- **強制停止下載** — 點擊「更新檢查」後按鈕變為「同步檢查中…」；再次點擊可立即強制終止
- **YouTube 標題同步** — 若影片在 YouTube 上改名，本地資料夾與所有相關檔案將自動更名（完整 / 最近模式）
- **影片管理快取** — 切換至影片管理分頁時立即顯示快取資料，同時在背景靜默刷新
- **逐頻道 Cookies 選擇** — 每張頻道卡片可獨立設定使用的 Cookies 檔案
- **Cookies 上傳驗證** — 上傳 cookies.txt 時自動檢查是否包含必要的 Google 認證 Token，若缺少則發出警告
- **Cookies 匯出教學彈窗** — 「新增頻道」頁面內建步驟說明，引導使用者正確匯出 Cookies

**異動**
- 「更新檢查」按鈕移至頻道卡片標題列，無需展開即可點擊
- 已設定 Cookies 的頻道名稱旁顯示 🍪 徽章
- 輸入重複的頻道網址時，自動觸發更新檢查而非新增重複項目

**錯誤修復**
- **Cookies 被覆寫** — yt-dlp 下載後會回寫 Token 至原始 Cookies 檔案；現改為使用暫存副本，保護原始檔案不被修改
- **服務重啟後任務卡住** — 重啟服務後，原本停在「下載中」的任務現在會自動重設為錯誤狀態
- **手機版懸浮播放器** — 修正長按出現系統右鍵選單、拖曳時電視牆跟著滾動、播放器超出螢幕邊界等問題
- **真實錯誤訊息** — yt-dlp 的錯誤訊息現在會逐部影片顯示在下載佇列，不再只顯示通用失敗提示
- **快速模式清空影片清單** — 切換為快速模式後不再立即清空原有的影片清單

---

## 功能特色

### 下載管理
- 輸入 YouTube 頻道、播放清單或單一影片網址，自動批次下載
- 同時支援**影片（Videos）**與 **Shorts**，分類顯示於下載佇列
- 即時顯示每部影片的下載進度（百分比 + 速度）
- 自動偵測已下載影片，避免重複下載

### 下載條件篩選
- 畫質選擇（最佳畫質 / 720p / 480p / 僅音訊）
- 時長範圍、上傳日期範圍、最低觀看數限制
- 必須關鍵字 / 排除關鍵字（影片標題篩選）
- 最多下載數量限制

### Cookies 支援
- 支援上傳 cookies.txt，用於下載**年齡限制**或**會員專屬**影片
- 上傳時自動驗證 cookies 是否包含必要的認證欄位
- 內建教學說明如何正確匯出 cookies

### 影片管理（電視牆）
- 縮圖網格展示，滑鼠懸停自動預覽
- 支援懸浮播放器（可拖曳、縮放，多視窗模式）或網頁全屏播放器
- 依名稱 / 日期 / 大小排序，頻道篩選，關鍵字搜尋
- 無限捲動，大量影片流暢瀏覽

### 其他
- 相似影片偵測（自動群組同節目不同頻道版本）
- Docker 部署，下載路徑可自由掛載
- 行動裝置支援（懸浮播放器長按拖曳）

---

## 快速開始

### 本機執行

```bash
pip install -r requirements.txt
python app.py
```

開啟 http://localhost:5000

### Docker（推薦）

**1. 修改 `docker-compose.yml` 中的路徑**

```yaml
volumes:
  - D:/YTDownload/downloads:/app/downloads   # 影片儲存路徑
  - D:/YTDownload/data:/app/data             # 設定、任務紀錄、cookies
```

**2. 啟動**

```bash
docker compose up -d --build
```

開啟 http://localhost:7321

---

## Cookies 設定（年齡限制 / 會員影片）

1. 安裝瀏覽器擴充套件 **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)**
2. 登入 YouTube 帳號，開啟並播放一部**需要年齡認證的影片**（確認可正常播放）
3. 在該頁面點擊擴充套件，選擇 **「Export All Cookies」**
4. 至工具「新增頻道」頁面上傳 cookies 檔案

---

## 目錄結構

```
下載路徑/
└── 頻道名稱/
    └── 影片標題/
        ├── 影片標題.mp4
        ├── 影片標題.webp        # 縮圖
        └── 影片標題.info.json
```

## 環境變數

| 變數 | 預設值 | 說明 |
|---|---|---|
| `DOWNLOAD_DIR` | `./data/downloads` | 影片下載路徑 |
| `DATA_DIR` | `./data` | 設定檔、任務紀錄、cookies |

## 需求

- Python 3.10+
- Node.js（容器內已包含，用於 yt-dlp JS runtime）
- （選用）ffmpeg — 用於合併影片與音訊為單一檔案
