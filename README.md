# Farm Radar

新興DEX（分散型取引所）の「開店ボーナス（エミッション）」を観察して、儲かりそうな局面だけを知らせるシステムです。
仕様は [SPEC.md](SPEC.md)、開発のルールは [CLAUDE.md](CLAUDE.md) にあります。

> **安全について**: このシステムはブロックチェーンのデータを**読むだけ**です。
> 取引を送る機能、秘密鍵（ウォレットの鍵）やシードフレーズを扱う機能はありません。どこにも入力しないでください。

## 今できること（M1）

- 15分ごとに、Robinhood Chain のブロック番号を1つ決めて、その時点のプールのデータを読み取り、SQLite（1ファイルのデータベース）に保存します。
- RPC（ブロックチェーンに問い合わせる窓口）の生の応答も保存します。あとで計算式を変えたときに、過去のデータで計算し直せるようにするためです。
- 24時間分の収集が欠けていないかを確認するコマンドがあります。

- 対象は up. の「ゲージ（報酬の配り口）があるCLプール」です（2026-09-27 時点で87個）。プールの一覧はファクトリーの作成イベントから作ります。
- プールごとに、価格、全体の流動性、ゲージにステークされたレンジ内の流動性、報酬の毎秒量、エポック（報酬の区切り、毎週木曜 09:00 JST 切り替え）の終わりを保存します。
  エポック切り替えから2時間以内のデータには「エポック更新直後」の印が付きます。
- エポックの途中で報酬の毎秒量が30%以上減ったら、通知として記録します（Discord / Telegram への送信は M4 で追加）。
- 仕組みの根拠（どのコントラクトのどの関数で確かめたか）は `venues/up-robinhood.yaml` にあります。

## 今できること（M2）

- 1時間ごと（毎時5分）に、全プールの「純日利」（ボーナス収入から、損とコストを全部引いた1日の儲け）を計算し、
  🟢 攻め候補 / 🟡 様子見 / 🔴 見送り の判定と、日本語の理由（3行以内）を付けます。
- 判定に使うのは**総資産（既定 $1,000）あたり**の日利です。LPに置く額は総資産の55%（$550）。
- レンジ幅（±0.5%〜±15%）ごとに計算し、いちばん儲かる幅を選びます。「ステークしてボーナスをもらう」と
  「ステークせず手数料をもらう」の両方を計算し、良いほうを使います。
- ヘッジ（perp の売りで値動きを打ち消すこと）ができないトークンは、値動きの損を引き、判定は最高でも🟡です。
- 値動きの大きさは、自分の記録が7日分たまるまで、外部の価格サイト（GeckoTerminal）の1時間足で補います。理由文にそう書きます。
- 株トークン・ステーブルコイン・ヘッジ先の一覧は `venues/tokens-robinhood.yaml`（公式の情報から自動で作成）にあります。

## 必要なもの

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)（Mac / Windows）。アプリを入れて起動しておきます。
- このリポジトリをパソコンにダウンロードしたもの。M1 の間はコードが作業ブランチにあるので、ブランチを指定します。
  `git clone -b claude/farm-radar-m1-jvp4sm https://github.com/sanhyokim/farm-radar.git`
  （git がなければ https://github.com/sanhyokim/farm-radar/archive/refs/heads/claude/farm-radar-m1-jvp4sm.zip をダウンロードして展開）

## 準備（最初の1回だけ）

ターミナル（Mac なら「ターミナル」アプリ）で、リポジトリのフォルダに移動してから実行します。

```bash
cd farm-radar
cp .env.example .env
```

Alchemy の API キーを使う場合は、`.env` をテキストエディタで開いて `ALCHEMY_API_KEY=` の後ろに貼り付けます。
キーがなければ空のままで大丈夫です（Robinhood Chain 公式の公開RPCを使います）。
`.env` は Git に保存されない設定になっています。

## 起動

```bash
docker compose up -d --build
```

- `-d` は「裏で動かし続ける」という意味です。ターミナルを閉じても止まりません。
- 起動するとすぐに1回収集し、その後は毎時 0分・15分・30分・45分に収集します。

## 動いているかの確認

**ログ（動作の記録）を見る**

```bash
docker compose logs -f collector
```

`"collection finished"` という行が15分ごとに出ていれば動いています。`Ctrl + C` で表示を終えます（止まるのは表示だけです）。

**状態をブラウザで見る**

http://localhost:18000/api/health を開きます。

- `last_ok_at`: 最後に収集が成功した時刻（UTC＝日本時間 − 9時間）
- `stale`: `true` なら「データが古い」（しばらく成功していない）
- `last_24h`: 直近24時間の予定回数（expected）と成功回数（ok）

http://localhost:18000/api/venues では会場の警告（例: C4「報酬の上限を決める仕組みのソースが非公開」）と、
コントラクトごとの確認状況（Sourcify の照合が full / partial か）を見られます。
http://localhost:18000/api/alerts では、記録された通知（報酬の急減など）を見られます。
http://localhost:18000/api/scores では、プールごとの最新の判定（信号・純日利・理由）を見られます。

**24時間の欠けチェック（M1の完了条件）**

起動から24時間以上たってから実行します。

```bash
docker compose exec collector python -m farm_radar.check
```

「結果: 合格（欠けなし）」と出れば完了です。欠けた時刻があれば、日本時間で一覧に出ます。

## 停止

```bash
docker compose down
```

集めたデータは `data/` フォルダに残ります。次に起動すると続きから保存します。

## パソコンで動かすときの注意（スリープ・再起動）

**自動で再開する仕組み**
- スリープから復帰すると、寝ていた間の収集を1回にまとめて、すぐに実行します。
- パソコンを再起動しても、Docker Desktop が起動すれば収集も自動で始まります（`restart: unless-stopped` の設定）。
  そのために、Docker Desktop の Settings → General で **「Start Docker Desktop when you sign in to your computer」にチェック**を入れてください。
- 復帰直後でネットがまだつながっていなくても止まりません。その回は「failed」と記録し、次の回に再挑戦します。
- `docker compose down` で止めた場合だけは、自動では再開しません（`docker compose up -d` で再開）。

**止まっていた期間は「欠損」として記録されます**
- スリープ中や電源オフの間は収集できません。その期間は `collection_gaps`（欠損の表）に記録され、
  http://localhost:18000/api/health の `gaps_7d` と、欠けチェックの結果に表示されます。
- 将来のペーパートレード（M5）では、欠損期間中の損益を「推定」として扱い、評価の計算から分けます。

**24時間の確認をするとき**は、スリープしないようにしてください。
- Mac: 電源につないだうえで、ターミナルで `caffeinate -i` を実行したままにします（終了は `Ctrl + C`）。
- Windows: 設定 → システム → 電源 で「スリープ」を「なし」にします。

## 設定

- `config.yaml`: 動作モード、収集の間隔、RPCの再試行回数、予備のRPC など
  - `mode` は `observe`（観察のみ）か `paper`（ペーパートレード＝仮想の売買の練習）だけです。ほかの値だと起動時にエラーで止まります。
  - `scoring`: 総資産と振り分け、試すレンジ幅、置き直すまでの待ち時間（初期値15分）など。`signal`: 判定の基準
- `venues/tokens-robinhood.yaml`: トークンの分類（ステーブルコイン・株トークン・ヘッジ先）。公式の情報から自動で作ったもので、
  手で直しません（作り直しは開発側で `python -m farm_radar.tools.refresh_tokens` を実行します）。
- `venues/up-robinhood.yaml`: 会場（取引所）の情報。アドレスと仕組みの出典URLと確認日を書きます。確認できていないものは `unverified: true` です。

RPC を使う順番: `.env` に Alchemy のキーがあれば Alchemy → 公式の公開RPC → 予備のRPC（`config.yaml` の `rpc.extra_urls` と `.env` の `EXTRA_RPC_URLS`）。
1つのRPCが失敗したら、自動で次に切り替えます。

## 開発者向け: テスト

```bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
