# YTDM Tool

**English** | [中文](#中文)

A lightweight YouTube channel / playlist download manager inspired by [Pinchflat](https://github.com/kieraneglin/pinchflat).
Single-page Web UI + Python/Flask backend, powered by yt-dlp.

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
