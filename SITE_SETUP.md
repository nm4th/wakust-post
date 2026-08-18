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

## SNS投稿文の自動生成（wakust_threads.py）

Threads / X に貼る投稿文を、掲載済みの記事データから組み立てます。
投稿APIは叩かず、**文面を出力するだけ**です。

```bash
python wakust_threads.py                      # 全テンプレートを表示
python wakust_threads.py --template today     # 本日出勤・料金順だけ
python wakust_threads.py --area 新宿          # エリアで絞る
python wakust_threads.py --tag NN --json      # JSON出力（API連携用）
```

| テンプレート | 内容 |
|---|---|
| `today` | 本日出勤を料金順（安→高）に並べる |
| `cheatsheet` | 「まず安く試すなら → ◯◯」形式の目的別リスト |
| `week` | 1週間分の出勤カレンダー |
| `new` | 新着1件の告知 |
| `--tag` | 指定タグで絞った一覧 |

各テンプレートは **本文とリプライ文をセットで返します。** 本文にはリンクを入れず、
自分の投稿へのリプライにURLを置く運用を想定しています（本文にリンクを入れると
リーチが落ちると言われているため。Threads API ではリプライは投稿数上限に
カウントされません）。

リプライに入るURLは絞り込み済みの一覧URL（`?area=新宿&day=today` など）なので、
投稿の内容とサイトの表示が一致します。

締めの一言は `site_config.json` の `threads.closers` からローテーションします。
同じ文面が続くとスパム判定されやすいので、文言は適宜足してください。

## Threads への自動投稿

`wakust_threads_api.py` が Threads Graph API を叩きます。
コンテナ作成 → 公開 → リプライ の3ステップです。

### セットアップ

1. Threads アカウントをプロアカウントに切り替える
2. [Meta for Developers](https://developers.facebook.com/) でアプリを作り、
   Threads API のプロダクトを追加する
3. `threads_basic` と `threads_content_publish` の権限を取得する
4. 長期アクセストークン（有効期限60日）を発行する
5. GitHub の Secrets に登録する

| Secret | 内容 |
|---|---|
| `THREADS_USER_ID` | Threads のユーザーID（数値） |
| `THREADS_ACCESS_TOKEN` | 長期アクセストークン |

**トークンは60日で切れます。** `ThreadsClient.refresh_token()` で延長できるので、
期限前に実行して Secrets を更新してください。

### 誘導先の切り替え

`site_config.json` の `threads.link_target` で変えられます。

| 値 | リプライに入るURL |
|---|---|
| `wakust` | 記事単体は `source_url`（ワクストの記事URL）、一覧系は `wakust_landing_url` |
| `site` | 自社サイトの絞り込み済み一覧URL（`?area=新宿&day=today`） |

`link_target` が `wakust` で `wakust_landing_url` が空の場合、一覧系テンプレートは
**リンクなしで投稿されます**（誤ったURLを貼らないため）。一覧から誘導したいときは
ワクストの公開プロフィールURLなどを設定してください。

### 実行

```bash
# 文面の確認だけ（投稿しない）
python wakust_threads.py --template today

# 実投稿。認証情報が無ければ自動的に dry-run になる
THREADS_USER_ID=... THREADS_ACCESS_TOKEN=... \
  python wakust_threads.py --template today --post

# 残り投稿数の確認（1日250件まで）
python wakust_threads_api.py
```

GitHub Actions の `Wakust Threads Post` が 10:00 / 13:00 / 21:00 JST に
それぞれ `today` / `cheatsheet` / `week` を投稿します。
同じ日に同じテンプレートを二度投げないよう、`wakust_state.json` の
`_threads` に投稿履歴を残しています。

手動実行では `dry_run` が既定で true なので、まず文面をログで確認してから
false にして本番投稿してください。

## 有料本文の扱い（どこにも出していないことの確認）

有料部分（ワクストの `edit_text_2`）を読むのは、**codoc にエントリーを作る処理だけ**です。
`wakust_site.py` / `wakust_threads.py` / `wakust_threads_api.py` は
`edit_text_2` も `body_paywalled` も一切参照していません。

```bash
# 確認コマンド
grep -n "body_paywalled\|edit_text_2" wakust_site.py wakust_threads.py wakust_threads_api.py
# → 何も出なければ、サイト出力にもSNS投稿にも有料本文は入っていない
```

### SNS投稿だけを回す構成（codocを使わない）

ワクストに誘導するだけなら、codoc も自社サイトも経由する必要がありません。
`CODOC_MODE=meta_export` は、**本文を一切保存せず**メタデータだけを書き出します。

| 保存する | 保存しない |
|---|---|
| タイトル / エリア / タグ / 出勤日 / 価格 / 販売回数 / ワクストの記事URL | 有料部分・無料部分の本文、codocエントリー |

`Wakust Meta Export` ワークフローが毎朝 9:30 JST に実行され、その結果を使って
`Wakust Threads Post` が投稿します。codoc関連のワークフロー
（`Wakust Site Publish` / `Wakust Site Deploy`）を止めても、SNS投稿は動きます。

### 投稿に名前を出さない

`site_config.json` の `threads.show_names` を `false`（既定）にすると、
投稿から嬢の名前が消え、代わりにタグで表されます。

```
全エリア 本日8/18(火)出勤 料金順（安→高）

¥1,000　NN / 巨乳（新宿）
¥1,200　PZ / 巨乳（池袋）
```

出勤カレンダーも、名前ではなくエリアごとの人数になります。

```
8/19(水)　池袋2名・新宿2名
```

`true` にすると名前入り（`【8/20,21出勤】ゆい Fカップ` → `ゆい`）になります。
