# Illustrated-Guide

MLB圖鑑。
把《MLB 9 局職棒 勁旅對決 26》遊戲內「球員圖鑑」的卡片數值抓出來，
做成一個可以篩選、排序、看細節的網頁圖鑑。

全程走 ADB，不佔用實體滑鼠鍵盤、也不需要模擬器視窗在前景，所以掃描時可以正常用電腦。

- `dex.py` — 掃描、辨識、匯出
- `docs/index.html` — 網頁圖鑑（篩選介面照遊戲的「進階搜尋」排）

線上版：https://cloud-lab404.github.io/Illustrated-Guide/

## 需要什麼

- Windows
- Python 3.12+ 與 `pip install opencv-python numpy pytesseract`
- [LDPlayer](https://www.ldplayer.tw/) 安卓模擬器（預設路徑 `C:\LDPlayer\LDPlayer14`）
- Tesseract OCR（裝在 `C:\Program Files\Tesseract-OCR\tesseract.exe`）
- 模擬器裡裝好遊戲，**解析度 1600x900 / DPI 240、語言繁體中文**

解析度與語言不能改：畫面上的座標與模板都是照這個環境裁的。

## 快速開始

1. 模擬器開機、遊戲開起來，手動走到 **球隊管理 → 球員圖鑑**（之後程式會自己回到這裡）。
2. 開始掃描。兩台模擬器可以同時跑，會透過同一份工作清單自動分工：

```powershell
python dex.py run --serial emulator-5554
python dex.py run --serial emulator-5556     # 有第二台才需要
```

3. 隨時看進度（含剩餘時間估計）：

```powershell
python dex.py status
```

4. 掃完之後補讀名字、匯出網頁資料：

```powershell
bash name_boot.sh                # 名字：自我擴張字形庫 -> 重讀 -> 匯出（一次做完）
python dex.py export             # 只重新匯出的話
```

`name_boot.sh` 裡面做的事拆開來是：

```powershell
python dex.py names extend       # 用現有樣本對齊 OCR 字串，補進新字元（可重複跑）
python dex.py names harvest      # 只收「整串都讀對」的名字當樣本
python dex.py names read         # 從存下來的名字裁圖重讀，寫成對照表
python dex.py export             # 產生 web/cards.js（名字會優先用對照表）
```

5. 用瀏覽器打開 `docs/index.html`。資料是 `docs/cards.js`，用 `file://` 直接開也載得到。

想放到網路上看的話，`docs/` 就是 GitHub Pages 的來源：
repo 的 **Settings → Pages → Build and deployment → Deploy from a branch**，
選 `main` 分支、資料夾選 `/docs`，存檔後幾十秒就會有網址
（免費方案的 Pages 只支援公開 repo）。

掃描可以隨時中斷（Ctrl+C）；工作清單記著哪些切片做完了，再執行 `run` 會接著跑。

## 網頁怎麼用

左邊的篩選欄位跟遊戲裡的「進階搜尋」一樣：名稱、OVR 區間、球隊（AL／NL 分頁）、
卡片類型、位置，再加上潛力（可多選，全部符合才列出）。

- **打者／投手要選一邊**：兩邊的六項數值欄位名稱不同（力量／準確／選球／耐力／跑壘／守備
  對 球速／變化／球威／控球／持久力／守備），混在一張表裡看不懂。
- 表格每一欄都能點欄位標題排序，也能用右上的排序下拉。
- 點任一列會開右側詳情：數值長條、紅區九宮格（紅＝熱區、藍＝冷區）、平均擊球仰角、
  潛力可練級數、投手的球種與等級。
- 名字後面的 `*` 表示這張卡有欄位沒讀到，點開會列出是哪一項。

## 抓到什麼

| 欄位 | 來源 |
|---|---|
| OVR、名字、守位 | 卡面左上與下緣 |
| 球隊、卡片類型 | **篩選條件**（不是辨識出來的，所以一定對）|
| 六項數值（基本／合計）| 詳情第 1 頁「培育數值」表 |
| 潛力 5 項名稱與可練級數 | 詳情第 2 頁（亮點＝現在等級、灰點＝可解鎖、鎖頭＝要界限突破）|
| 紅區九宮格＋冷熱、平均擊球仰角 | 詳情第 3 頁（**只有打者有**）|
| 球種與等級 | 詳情第 3 頁（**投手**那一頁換成體力與球種）|

沒抓的東西與原因：**技能**在圖鑑裡對未持有的卡是空白；**潛力等級**一律顯示 D
（那是帳號的解鎖進度，不是卡片資料）；**Live 與 Season** 兩個卡片類型不在收集範圍內。

## 運作方式（以及為什麼這樣做）

- **遊戲自己的資料表是加密的。** `designData/` 有 698 張表（`BAT_DATA` 14MB 等），
  但全是加密內容，只有 `STRING_*.xml` 語系檔是明文。所以只能從畫面讀。
- **一次進場抓完三頁。** 詳情頁的 `›` 可以直接翻下一張卡，而且切到第 2/3 頁之後
  翻卡不會跳回第 1 頁。所以走法是「1→2→3、翻下一張、3→2→1、再翻下一張」，
  兩張卡一組共 6 張截圖；每張截圖底部的頁碼圓點就是它是第幾頁的證據，
  脫拍會被抓出來，不會把別張卡的數值寫到這張卡上。
- **截圖在模擬器裡連拍。** `screencap` 存到 `/data/local/tmp`，一批拍完才 `adb pull`
  回來，省掉每張截圖的連線來回；解析丟給另外幾個行程，跟擷取重疊進行。
- **切片＝卡片類型 × 球隊（14×30）。** 球隊用篩選器指定，球隊欄位就變成已知條件——
  卡面上的球隊 logo 疊在會變動的卡圖上，比對只有 78% 準，不能拿來當資料。
  切片也讓「掃到一半失敗」只需要重跑那一片。
- **數字不用 tesseract。** 它對這款字型有系統性錯誤（7→1、5→9、76→16），
  而錯的數字寫進資料庫沒人看得出來。改成自建字形樣本比對（`dex_data/glyphs/`），
  對不上就留空，不猜。
- **名字用自我擴張的字形庫讀。** 名字是最難的一欄，tesseract 在這個字型上大概只讀對三成。
  但名字的字型也是固定的，所以做法是：先用 OCR 讀得準的名字收一批字形樣本，
  之後每一輪用「已知的字都跟 OCR 對得上、只剩一兩個未知」的名字去補那幾個未知字，
  一輪一輪把整個字母表、數字與撇號學完（`names extend`）。
- **名字離線讀。** 名字是最難讀的一欄（字疊在會發光的卡圖上），而且現場做 OCR 佔掉
  三分之二的解析時間。所以掃描時只把名字那一條存成小圖，之後再慢慢讀、可以反覆改進，
  不必為了名字重掃一次。
- **OVR 用清單排序補洞。** 圖鑑按 OVR 遞減排列，所以讀到的 OVR 必須是不遞增的；
  `export` 會取最長不遞增子序列當骨幹，前後相同的空洞就補起來，不同的標成不確定。

## 資料品質怎麼保證

| 機制 | 擋掉什麼 |
|---|---|
| 頁碼圓點驗證 | 脫拍（把上一張卡的畫面當成這張）|
| 籤與按鈕驗證 | 篩選沒套上就開始掃——實測發生過，整批掃成別的卡片類型 |
| 等捲動停穩再點 | 面板慣性滑動導致點到隔壁的類型 |
| `dex.py check` | 字形樣本自相矛盾（不同標籤長得一樣＝有人標錯）|
| `dex.py audit` | 各欄位讀到幾成、OVR 排序有無矛盾、哪些切片張數不足 |
| `dex.py status` | 每個卡片類型掃到的張數對不對得上篩選器數出來的總數 |
| `_review/` | 有數字欄位讀不到的卡會留下那一頁的現場截圖，可離線重跑 |

## 指令一覽

```powershell
python dex.py run --serial emulator-5554        # 照工作清單一路掃
python dex.py sweep --type Moment --team NYY    # 只掃一個切片
python dex.py status                            # 進度與剩餘時間
python dex.py audit                             # 資料體檢
python dex.py export                            # 產生 web/cards.js

python dex.py filter --type HOF --team NYY      # 只套篩選，印出張數（除錯用）
python dex.py shots --type HOF --page 1 --n 40 --out shots_hof   # 連拍存檔
python dex.py calibrate shots_hof/f0.png --page 1                # 畫出取樣框
python dex.py parse shots_hof/f0.png --page 1                    # 解析單張

python dex.py glyphs collect --font stat --frames "shots_hof/*.png" --out g.png
python dex.py glyphs label --font stat --digits "7,8,0,..."       # 照montage順序標
python dex.py glyphs harvest --font stat --frames "shots_hof/*.png"
python dex.py check                             # 檢查字形樣本有沒有標錯

python dex.py names collect --out n.png         # 名字字元分群（附上下文，方便人工標）
python dex.py names label --chars "a,b,c,..."   # 照順序標
python dex.py names extend                      # 自我擴張：已知字對齊 OCR，補未知字
python dex.py names harvest                     # 只收整串讀對的名字
python dex.py names read                        # 重讀所有名字

python dex.py pots collect --frames "p2/*.png" --out p.png   # 潛力名稱（中文）分群
python dex.py pots label --names "揮大棒,關鍵,..."
python dex.py teams --type Moment                # 收球隊 logo 樣本（備援用）
```

## 檔案

| 路徑 | 用途 |
|---|---|
| `dex.py` | 掃描、辨識、字形樣本管理、匯出 |
| `templates/圖鑑_*.png` | 畫面定位用的模板（面板錨點、狀態判斷）|
| `dex_data/glyphs/` | 自建字形樣本（ovr／stat／zone／pos／name）|
| `dex_data/badges/` | 守位徽章樣本 |
| `dex_data/potential_labels.json` | 潛力名稱（中文）的點陣指紋對照表 |
| `dex_data/queue.json` | 420 個切片的工作清單（兩台共用，有檔案鎖）|
| `dex_data/<類型>/<球隊>.jsonl` | 掃描結果，一行一張卡（不進版控）|
| `dex_data/<類型>/names/` | 每張卡的名字裁圖，給離線重讀用（不進版控）|
| `docs/index.html` | 網頁圖鑑（同時是 GitHub Pages 的來源）|
| `docs/cards.js` | `dex.py export` 產生的資料（要上 Pages 才需要進版控）|

字形樣本、徽章樣本、潛力對照表都放進版控了，所以 clone 下來可以直接跑，
不用重新標記。掃描結果與名字裁圖太大，沒有進版控；`docs/cards.js` 為了 Pages 例外收進版控。
