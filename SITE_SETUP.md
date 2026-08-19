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

### 投稿の種類

**手書きストック**（`threads_pool.json`）

データからは作れない投稿をここに貯めます。参考アカウントを見て「これは効きそう」と
思った型を、自分の言葉で書き溜める場所です。

| キー | 用途 |
|---|---|
| `aruaru` | あるあるネタ。共感で伸ばす枠 |
| `info` | 情報投稿。予約のコツ、選び方など読み物系 |

1件＝1投稿で、改行もそのまま投稿されます。`wakust_state.json` の
`_threads_pool` に使用日を記録し、**一番長く使っていないものから順に**出すので、
同じ文が続きません。ストックが空になった枠は、下のデータ由来テンプレートに
自動で切り替わります（`aruaru`→`cheatsheet`、`info`→`price`）。

参考にした型は4つです。

| 型 | 構成 | テンプレート |
|---|---|---|
| エリア厳選 | 【ラベル】→ 結論 → `【駅名】〜` を5〜10件 → 保存促し → **出し惜しみ** | `pickup` |
| 体験速報 | 短い口語＋結果を先出し＋「いいね多かったら詳細出す」 | `flash` |
| 番号リスト | 「どうぞパクってください」→ 1〜7の実用項目 → 締めの一言 | 手書き `info` |
| 会話劇 | セリフの応酬で実話を再現し、最後にツッコミで落とす | 手書き `aruaru` |
| 長文ストーリー | 権威付け → 共感 → 失敗の痛み →「でも逆に言うと」→ 誘導 | 手書き `info` |

エリア厳選型が最も反応を取っていた型です（❤️88 / ❤️40）。共通しているのは
**最後に情報を出し惜しむ**こと（「一番の本命はあえてここには書いてない」）で、
これがそのままプロフィールへの導線になっています。`pickup` はこの構造を再現し、
リプライにURLを付けずに締めの一文だけで誘導します。

対象エリアは日替わりで回り、実績（販売数）順に選びつつ**1駅あたりの件数に上限**を
設けて偏りを防いでいます。2件以上ある駅だけ《駅名》の見出しを立て、
1件だけの駅は《その他のエリア》にまとめます。
見出しと締めの文は `threads.pickup_heads` / `pickup_tails` で変更できます。

⚠️ **体験談として書くのは実際に行った話だけにしてください。** 行っていない体験を
自動投稿すると、コメントで具体的に聞かれた時に必ず破綻します。
「レポートを上げた」という事実ベースの速報なら `flash` が自動生成します。

**データから自動生成**（ワクストの記事データ由来）

| テンプレート | 内容 |
|---|---|
| `today` | 本日出勤を料金順（安→高）。価格帯全体に散らして選ぶ |
| `cheatsheet` | 「まず安く試すなら → ◯◯」形式の目的別リスト |
| `week` | 1週間分の出勤カレンダー（エリア別の人数） |
| `price` | 料金帯ごとの在籍数（棒グラフ付き） |
| `lineup` | カップ別・タイプ別・エリア別の内訳 |
| `rank` | 販売数の多い順ランキング |
| `pickup` | **エリア厳選型**。`【駅名】特徴 価格` を5〜10件並べ、最後に出し惜しんで締める |
| `flash` | 体験速報型。短文＋「いいね多かったら他も出します」で反応を煽る |
| `story` | 体験談を1本紹介。最近出していないものから選ぶ |
| `new` | 新着1件の告知 |
| `--tag` | 指定タグで絞った一覧 |

### 何をいつ出すか

`site_config.json` の `threads.post_schedule` で、時間帯ごとに**日替わりの
ローテーション**を組みます。リストの長さは自由で、通し日数で順番に回ります。

```json
"post_schedule": {
  "10": ["today"],
  "13": ["pickup", "info", "aruaru", "pickup", "cheatsheet", "aruaru", "pickup"],
  "21": ["flash", "story", "aruaru", "flash", "week", "story", "rank"]
}
```

朝は出勤情報で固定、昼は手書き（情報・あるある）を主軸にデータ系を混ぜ、
夜は反応が取れる `flash` と体験談を厚めに、という配分です。手書きの比率を
上げたいときはリスト内の `info` / `aruaru` を増やしてください。

`flash` の1行目と締めの煽り文は `threads.flash_heads` / `threads.flash_hooks` で
変えられます。`{area}` と `{play}` が記事の値に置き換わります。

```bash
python wakust_threads.py --slot 13     # その日の昼枠が何になるか確認
```

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

**プロアカウントへの切り替えは不要です。** Threads API に必要なのは
Meta の開発者アカウントと「Threads Tester」への登録で、アカウント種別は問いません
（プロアカウントが要るのは Instagram Graph API の方）。

1. **[Meta for Developers](https://developers.facebook.com/) でアプリを作る**
   アプリ作成時にユースケースで **「Threads API へのアクセス」** を選ぶ
2. **権限を追加する** — 最低限 `threads_basic` と `threads_content_publish`。
   分析も取るなら `threads_manage_insights` も追加
3. **ユースケースの「設定」画面を開く**
   左メニュー **ユースケース → 「Threads APIへのアクセス」→ カスタマイズ → 設定**
   ここにテスター追加・リダイレクトURL・トークン生成がすべて集約されている

4. **リダイレクトURLを3か所とも埋めて保存する**
   空のままだとトークンを生成できない。自前のコールバックが無ければ
   Postman のもので構わない（3欄すべてに同じ値でよい）

   ```
   https://oauth.pstmn.io/v1/callback
   ```

5. **自分を Threads テスターに追加する**
   同じ画面のテスター欄、または 左メニュー **「アプリの役割」→「役割」**
   →「メンバーを追加」→ **「Threadsテスター」** → ユーザー名 `masa0_menes` を入力

   ⚠️ **Instagramテスターでは動きません。** Threads テスターは別の役割で、
   承認場所も Threads 側です。

6. **Threads 側で招待を承認する**
   Threads アプリ → **設定 → アカウント → ウェブサイトのアクセス許可 → Invites**
   → 作成したアプリの「同意する」をタップ

7. **アクセストークンを生成する**
   ユースケースの「設定」画面に戻り、ページ下部の **「ユーザートークン生成ツール」**
   にテスターのアカウント名が出ているので **「アクセストークンを生成」** をクリック。
   ダイアログの **「理解しました」にチェックを入れると「コピー」が押せる**ようになる。

   ここで発行されるのは **有効期限60日の長期トークン**（短期トークンではない）。
   交換は不要で、そのまま使える。

   ⚠️ トークンは**公開Threadsアカウントにのみ**発行されます。非公開設定だと
   生成できません。

8. **ユーザーIDを引いて、Secretsに貼る値を出す**（下記コマンド）
9. 出力された2つの値を GitHub の Secrets に登録する

自分のアカウントにだけ投稿する用途なら、**アプリ審査も Tech Provider Verification も
不要**です。テスターとして登録されたアカウントに対しては、審査前でもAPIが使えます。
他人のアカウントを扱うツールとして公開する場合だけ、これらの審査が必要になります。

```bash
pip install requests
python wakust_threads_setup.py secrets --token "コピーしたトークン"
```

OAuth フローなどで短期トークン（1時間）しか手に入らない場合だけ、
長期トークンへの交換が必要です。

```bash
python wakust_threads_setup.py exchange \
  --short-token "短期トークン" --app-secret "アプリのシークレット"
```

```
✅ 確認できました。GitHub の Secrets に以下を登録してください。

  THREADS_ACCESS_TOKEN  THQVJ...
  THREADS_USER_ID       17841...   ※省略可（未設定なら自動で自分を指します）

  アカウント : @masa0_menes
```

### トークンの期限管理

**長期トークンは60日で失効し、切れた後は延長できません。** 切れる前に延長します。

```bash
THREADS_ACCESS_TOKEN=... python wakust_threads_setup.py refresh   # 60日延長
THREADS_ACCESS_TOKEN=... python wakust_threads_setup.py check     # 生存確認＋残り投稿数
```

`Wakust Threads Token Check` が毎週月曜9:00 JSTに生存確認をして、
トークンが死んでいたらワークフローを失敗させて通知します。

| Secret | 内容 | 必須 |
|---|---|---|
| `THREADS_ACCESS_TOKEN` | 長期アクセストークン（60日） | 必須 |
| `THREADS_USER_ID` | Threads のユーザーID（`17841...` のような**数値**） | 任意 |

`THREADS_USER_ID` は省略できます。未設定なら Threads API の `me`
（自分自身）として扱うので、実質トークンだけあれば動きます。
**ユーザー名（`masa0_menes`）ではありません** — 数値IDです。

**トークンは60日で切れます。** `ThreadsClient.refresh_token()` で延長できるので、
期限前に実行して Secrets を更新してください。

### 誘導先の切り替え

`site_config.json` の `threads.link_target` で変えられます。

| 値 | リプライに入るURL |
|---|---|
| `wakust` | 記事単体は `source_url`（ワクストの記事URL）、一覧系は `wakust_landing_url` |
| `site` | 自社サイトの絞り込み済み一覧URL（`?area=新宿&day=today`） |

一覧系の着地先には **ワクストの公開プロフィール** を設定しています。

```
https://wakust.com/user/Risingnoboru/
```

このURLは固定のまま、**中身が1日2回更新されます**。`run_organize_sets()` の最後で
`_update_profile_with_sets()` が走り、プロフィールのフリーリンク5枠
（東京都内・新宿・池袋・神奈川・埼玉）を張り替えているためです。

| タイミング | 貼られるセット |
|---|---|
| 0:00 JST | 本日出勤セット |
| 16:30 JST | 明日出勤セット |

Threads のプロフィール欄（bioは最大5リンク）にもこのURLを置いてください。
**Threads API にはプロフィールを書き換える権限が存在しない**ので、bioの自動更新は
できません。「固定URLを置いて、その先の中身を毎日更新する」形にすることで、
同じ効果を追加実装ゼロで実現しています。

セットURL（`https://wakust.com/setlist/?set_id=...`）を直接bioに置いてはいけません。
0時に全セットを削除・再作成しているので `set_id` が毎日変わり、翌日には死にます。

`wakust_landing_url` が空の場合、一覧系テンプレートは**リンクなしで投稿され**、
代わりに `profile_cta`（「詳細はプロフィールのリンクから」）が本文末尾に付きます。

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

## 現在の構成（ワクスト誘導・codoc停止）

```
ワクスト（記事の原本・販売もここ）
   │  タイトル / エリア / タグ / 出勤日 / 価格 / 販売回数 / 記事URL
   ▼  CODOC_MODE=meta_export  ※本文は保存しない
site_content/articles/*.json
   ▼  wakust_threads.py
Threads 投稿（名前は伏せ、タグで表現）
   └─ 記事1件の投稿 → リプライにワクストの記事URL
   └─ 一覧系の投稿   → リンクなし。「詳細はプロフィールのリンクから」
```

| ワークフロー | 状態 |
|---|---|
| `Wakust Meta Export` | 稼働（毎朝9:30 JST） |
| `Wakust Threads Post` | 稼働（10:00 / 13:00 / 21:00 JST） |
| `Wakust Auto Update` | 稼働（0:00 / 16:30 JST） |
| `Wakust Site Publish` | **停止**（手動実行のみ残置） |
| `Wakust Site Deploy` | **停止**（手動実行のみ残置） |

codoc掲載と自社サイトは `site_config.json` の `site_enabled: false` で切ってあります。
コードは残してあるので、再開したくなったら `site_enabled` を `true` に戻し、
各ワークフローの `schedule` / `push` トリガーのコメントを外すだけです。
