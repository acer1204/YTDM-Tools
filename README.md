# YTDM_Tool

輕量化 YouTube 頻道下載管理工具。輸入頻道或播放清單網址，自動批次下載並管理影片。

## 功能

- 批次下載 YouTube 頻道 / 播放清單
- 下載條件過濾（時長、日期、觀看數、關鍵字排除、畫質）
- Cookies 支援（年齡限制 / 會員影片）
- 電視牆影片管理（縮圖預覽、懸浮播放器、無限捲動）
- 相似影片偵測（同節目不同頻道版本自動分組）
- Docker 部署，掛載任意下載路徑

## 快速開始

### 本機執行

```bash
pip install -r requirements.txt
python app.py
```

開啟 http://localhost:5000

### Docker

**1. 修改 `docker-compose.yml` 中的路徑**

```yaml
volumes:
  - D:/YTDownload/downloads:/app/downloads   # 改成你的影片儲存路徑
  - D:/YTDownload/data:/app/data             # 改成你的資料儲存路徑
```

**2. 啟動**

```bash
docker compose up -d --build
```

開啟 http://localhost:7321

## 目錄結構

```
下載路徑/
└── 頻道名稱/
    └── 影片標題/
        ├── 影片標題.mp4
        ├── 影片標題.webp     # 縮圖
        └── 影片標題.info.json
```

## Cookies 設定（選用）

下載年齡限制或會員影片時需要 Cookies：

1. 安裝瀏覽器擴充套件 **Get cookies.txt LOCALLY**
2. 在 YouTube 頁面匯出 cookies.txt
3. 於「新增頻道」頁面上傳 cookies 檔案

## 環境變數

| 變數 | 預設值 | 說明 |
|---|---|---|
| `DOWNLOAD_DIR` | `./data/downloads` | 影片下載路徑 |
| `DATA_DIR` | `./data` | 設定檔、任務紀錄、cookies 路徑 |

## 需求

- Python 3.10+
- （選用）ffmpeg — 用於合併影片與音訊為單一檔案
