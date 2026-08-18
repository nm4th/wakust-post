# 自社サイトでの記事販売（codoc連携）セットアップ

ワクストに投稿している記事を、**自社サイトでも同じ内容・同じ価格で販売する**ための仕組みです。
codoc は「記事の置き場所」ではなく「決済とペイウォールのレイヤー」として使います。

## 仕組み

```
ワクスト（記事の原本）
   │  無料部分 = edit_text_1 / 有料部分 = edit_text_2 / 販売ポイント
   ▼
wakust_auto_update.py  CODOC_MODE=site_publish
   ├─ codoc にエントリー作成
   │    body_free       … 無料部分
   │    body_paywalled  … 有料部分（★ codocにだけ保存）
   │    price           … ワクストの販売ポイントと同額
   │    binded_url      … 自社サイトの記事URL
   │    limited=1       … codoc.jp の一覧には出さない（販売導線は自社サイトのみ）
   └─ site_content/articles/{記事ID}.json を書き出し（★無料部分のみ）
   ▼
wakust_site.py  → site/  → GitHub Pages
   記事ページ = 無料部分HTML + codocの貼り付けタグ
```

**有料本文は静的HTMLにもリポジトリにも出力しません。** 出力してしまうと
ソースを開くだけで読めてしまうためです。購入後の本文差し込みは codoc の
スクリプトが行います。

## 初回セットアップ

### 1. codoc 側

1. 自社サイトのドメインを codoc に登録する（登録ドメイン外ではペイウォールが動きません）。
   GitHub Pages をそのまま使う場合は `nm4th.github.io` を登録します。
2. `site_config.json` の `codoc.usercode` には、公開URL
   `https://codoc.jp/sites/mKFyLVe4HA/entries/...` から読み取った
   サイトコード **`mKFyLVe4HA`** を設定済みです。
   codoc 管理画面の「タグを貼り付け」に出るスクリプトタグの `data-usercode` と
   一致しているか、最初の1件を公開する前に一度だけ確認してください。
   違っていた場合は Variables の `CODOC_USERCODE` で上書きできます。

### codoc のURL/コード構造（確認済み）

```
https://codoc.jp/sites/mKFyLVe4HA/entries/zKDF0Mzq3A
                       ^^^^^^^^^^         ^^^^^^^^^^
                       サイトコード        エントリーコード
                       = data-usercode    = codoc-entry-{ここ}
```

エントリーコードは記事ごとに変わる公開IDで、`/me/entries/{数字}` の数字とは別物です。
`codoc_fetch_entry_code()` が作成直後に自動で回収し、
`site_content/articles/{記事ID}.json` の `codoc_entry_code` に保存します。

### 2. GitHub 側

**Settings → Pages**
- Source を **GitHub Actions** に変更する。

**Settings → Secrets and variables → Actions → Variables**

| 変数名 | 例 | 説明 |
|---|---|---|
| `CODOC_USERCODE` | `mKFyLVe4HA` | codoc のサイトコード。`site_config.json` に設定済みなので、変更したいときだけ指定。公開HTMLに出る値なので Secret でなくてよい |
| `SITE_BASE_URL` | `https://nm4th.github.io/wakust-post` | 独自ドメインを使う場合はそのURL。未設定なら `site_config.json` の値 |
| `SITE_CNAME` | `example.com` | 独自ドメインを使うときだけ設定（`site/CNAME` を出力する） |
| `CODOC_LIMITED` | `1` | `1`=codoc上は限定公開 / `0`=codoc.jp の一覧にも載せる |

**Secrets**（既存のものをそのまま使用）
`WAKUST_EMAIL` / `WAKUST_PASSWORD` / `WAKUST_COOKIE` / `CODOC_COOKIE`

### 3. サイト情報の記入

`site_config.json` の `tokushoho` セクションは**プレースホルダのままです。**
自社サイトで有料販売する以上、特定商取引法に基づく表記は必須なので、
公開前に事業者名・所在地・連絡先などを実際の値に書き換えてください。
`site_title` / `site_tagline` / `base_url` も合わせて調整します。

## サイトの構造（探しやすさ重視）

記事一覧は、ワクスト側で持っている情報をそのまま絞り込み軸にしています。

| 軸 | 出どころ | UI |
|---|---|---|
| キーワード | タイトル・タグ・エリア | 検索ボックス（スペース区切りAND） |
| 出勤日 | タイトルの `【8/20,21出勤】` を実日付に変換 | 今日 / 明日 / 1週間以内 / 日付プルダウン |
| エリア | 記事のカテゴリー | チップ（単一選択） |
| タグ | 記事ページの `NN(23397)` 形式のタグ（日本語も取得） | チップ（複数選択・AND条件） |
| 並び替え | 公開日 / 販売回数 / 価格 / 直近の出勤日 | プルダウン |

絞り込みの状態は URL に入ります（`?q=&area=&tags=&day=&sort=`）ので、
「新宿で今日出勤」のような条件をそのままリンクとして共有できます。

絞り込みは **サーバー側で出力済みのカードをJavaScriptで出し分けているだけ**なので、
JSを切っていても全記事が表示されます。検索エンジンも全記事を読めます。

### 生成されるURL

```
/                        記事一覧（検索・絞り込み）
/articles/{記事ID}/      記事ページ
/area/shinjuku/          エリア別一覧
/tag/nn/  /tag/巨乳/     タグ別一覧
```

エリア・タグのページは、絞り込み済みの状態で静的に出力されるSEO用の入口です。
`sitemap.xml` にも全部入ります。記事ページには Article + BreadcrumbList の
構造化データと、有料部分を示す `isAccessibleForFree: false` を出しています。

## 運用

| ワークフロー | タイミング | 内容 |
|---|---|---|
| `Wakust Site Publish` | 10:00 / 13:00 / 21:00 JST | 未掲載の記事から販売回数が多い順に1件、codocエントリー作成＋記事JSONをコミット |
| `Wakust Site Deploy` | `site_content/` の更新時 | サイトを生成して GitHub Pages に公開 |
| `Wakust Auto Update` | 0:00 / 16:30 JST | 記事更新に合わせて掲載済み記事のタイトル・価格・無料部分を同期 |

手動で掲載件数を増やしたいときは `Wakust Site Publish` を
`workflow_dispatch` で実行し、`publish_limit` に件数を入れてください。

## ローカルでの確認

```bash
pip install -r requirements.txt

# サイトだけ生成（site_content/ の内容から）
CODOC_USERCODE=xxxxxxxx python wakust_site.py
python -m http.server -d site 8000   # http://localhost:8000/

# 掲載処理そのものを1件だけ実行
CODOC_MODE=site_publish CODOC_COOKIE="..." WAKUST_COOKIE="..." \
  python wakust_auto_update.py
```

## つまずきやすいところ

- **ペイウォールが表示されない** … `CODOC_USERCODE` が未設定か、codoc にドメインを
  登録していない可能性があります。生成ログに
  `⚠️ codoc.usercode が未設定です` が出ていないか確認してください。
- **記事ページに「販売準備中」と出る** … codoc のエントリーコード（`codoc-entry-XXXX`
  の `XXXX` 部分）を自動取得できなかったケースです。codoc 管理画面の「タグを貼り付け」
  からコードをコピーし、`site_content/articles/{記事ID}.json` の
  `codoc_entry_code` に貼ってコミットすれば復旧します。
- **同じ記事が二重に掲載される** … 掲載済み判定は `wakust_state.json` と
  `site_content/articles/*.json` の両方で行っています。ワークフローが
  コミットに失敗していないか確認してください。
