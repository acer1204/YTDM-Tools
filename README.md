# YTDM Tool

**English** | [中文](#中文)

A lightweight YouTube channel / playlist download manager inspired by [Pinchflat](https://github.com/kieraneglin/pinchflat).
Single-page Web UI + Python/Flask backend, powered by yt-dlp.

---

## Screenshots

<table>
  <tr>
    <td align="center"><b>Add Channel</b></td>
    <td align="center"><b>Download Filters</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Add Channel 01.png" width="400"/></td>
    <td><img src="screencapture/Add Channel 02.png" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>Download Queue</b></td>
    <td align="center"><b>Media Library</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Download Queue 01.png" width="400"/></td>
    <td><img src="screencapture/Media Library 01.png" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>Duplicate Detection</b></td>
    <td align="center"><b>Settings</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Duplicate Detection 01.png" width="400"/></td>
    <td><img src="screencapture/Settings 01.png" width="400"/></td>
  </tr>
</table>

---

## Features

### Download Management
- Batch download from YouTube channels, playlists, or single video URLs
- Supports both **Videos** and **Shorts**, displayed as separate sections in the queue
- Real-time per-video progress (percentage + speed) with color-coded segmented bar (done / exists / skipped / error / downloading)
- Automatic archive checking to skip already-downloaded videos
- **Global concurrency limit** — cap how many channels download simultaneously; extras are queued with an orange badge
- **Configurable download interval** — optional delay between each video to reduce bot-detection risk (min 5 s)
- **Scheduled trigger** — weekly schedule (day checkboxes + time picker) to auto-start all channels; requires concurrency = 1

### Update Check Modes
- **Full mode** — fetches complete video list from YouTube, compares with downloaded archive
- **Recent-N mode** — only fetches the latest N videos for comparison, much faster for regular checks
- **Fast mode** — skips list pre-fetch; lets yt-dlp scan the channel URL directly and skip already-downloaded videos

### Download Filters
- Quality selection (best / 720p / 480p / audio only)
- Duration range, upload date range, minimum view count
- Required keywords / exclude keywords (title-based filtering, applied before any API call)
- Maximum video count limit

### Cookies Support
- Upload a cookies.txt file to download **age-restricted** or **members-only** videos
- Automatic validation on upload to verify the cookies contain required auth fields
- Built-in tutorial explaining how to correctly export cookies

### Media Library (TV Wall)
- Thumbnail grid with hover-to-preview
- Floating player (draggable, resizable, multi-window support) or fullscreen overlay player
- Sort by name / date / size, filter by channel, keyword search — all instant, client-side
- Infinite scroll for large libraries
- In-memory list with persistent cache — media page loads instantly after first scan, even after service restart

### Other
- Duplicate video detection (groups same content from different channels)
- Multi-language UI — English, Traditional Chinese (繁體中文), Simplified Chinese (简体中文)
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

## 截圖

<table>
  <tr>
    <td align="center"><b>新增頻道</b></td>
    <td align="center"><b>下載條件篩選</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Add Channel 01.png" width="400"/></td>
    <td><img src="screencapture/Add Channel 02.png" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>下載佇列</b></td>
    <td align="center"><b>影片管理</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Download Queue 01.png" width="400"/></td>
    <td><img src="screencapture/Media Library 01.png" width="400"/></td>
  </tr>
  <tr>
    <td align="center"><b>重複偵測</b></td>
    <td align="center"><b>設定</b></td>
  </tr>
  <tr>
    <td><img src="screencapture/Duplicate Detection 01.png" width="400"/></td>
    <td><img src="screencapture/Settings 01.png" width="400"/></td>
  </tr>
</table>

---

## 功能特色

### 下載管理
- 輸入 YouTube 頻道、播放清單或單一影片網址，自動批次下載
- 同時支援**影片（Videos）**與 **Shorts**，分類顯示於下載佇列
- 即時顯示每部影片的下載進度（百分比 + 速度），頻道卡片顯示彩色分段進度條（完成 / 已存在 / 略過 / 錯誤 / 下載中）
- 自動偵測已下載影片，避免重複下載
- **全域頻道併發限制** — 設定最多同時下載幾個頻道，超過上限自動排隊並顯示橘色標籤
- **下載間隔** — 每部影片之間可設定等待秒數，降低被偵測為機器人的風險（最低 5 秒）
- **定時觸發下載** — 可設定每週排程（勾選星期 + 時間），到時自動啟動所有頻道；需將併發數設為 1

### 更新檢查模式
- **完整模式** — 從 YouTube 取得完整影片清單，與已下載紀錄對比
- **最近 N 部模式** — 只抓最新 N 部影片比對，大幅縮短等待時間
- **快速模式** — 略過清單預取，直接讓 yt-dlp 掃描頻道並略過已下載影片

### 下載條件篩選
- 畫質選擇（最佳畫質 / 720p / 480p / 僅音訊）
- 時長範圍、上傳日期範圍、最低觀看數限制
- 必須關鍵字 / 排除關鍵字（標題篩選，於任何 API 呼叫前提前過濾）
- 最多下載數量限制

### Cookies 支援
- 支援上傳 cookies.txt，用於下載**年齡限制**或**會員專屬**影片
- 上傳時自動驗證 cookies 是否包含必要的認證欄位
- 內建教學說明如何正確匯出 cookies

### 影片管理（電視牆）
- 縮圖網格展示，滑鼠懸停自動預覽
- 支援懸浮播放器（可拖曳、縮放，多視窗模式）或網頁全屏播放器
- 依名稱 / 日期 / 大小排序，頻道篩選，關鍵字搜尋 — 全部前端即時完成
- 無限捲動，大量影片流暢瀏覽
- 記憶體清單搭配持久化快取 — 首次掃描後，後續載入與服務重啟均瞬間完成

### 其他
- 相似影片偵測（自動群組同節目不同頻道版本）
- 多語系介面 — 英文、繁體中文、簡體中文
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
