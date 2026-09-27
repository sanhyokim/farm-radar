# PROGRESS.md — 作業の現在地

> 新しいセッションは、作業を始める前にこのファイルを読むこと（CLAUDE.md のルール）。作業のたびに更新する。
> 最終更新: 2026-09-27 21:58 JST

## いまの位置
- **M1（up.アダプター + データ収集 + SQLite）の途中。**
- 作業ブランチ: `claude/farm-radar-m1-jvp4sm`。`main` には CLAUDE.md と SPEC.md だけが入っている。M1が終わったら main へのPRにする。
- 付録A（Phase 3）は実装しない。オーナーが「Phase 3aを開始」と言うまで、送信・署名・秘密鍵のコードは書かない。

## M1 の進み具合
| # | 内容 | 状態 |
|---|---|---|
| 1 | 土台（config.yaml, .env.example, docker-compose, README） | 完了 |
| 2 | venues/up-robinhood.yaml（出典つき） | チェーン情報のみ確認済み。コントラクトと仕組みは未確認 |
| 3 | SQLite（6章の全テーブル + ブロック番号・流動性2種・生データ・収集記録・欠損） | 完了 |
| 4 | 読み取り専用RPCクライアント（再試行・切り替え・キャッシュ）と Multicall3 | 完了 |
| 5 | up. アダプター（ファクトリーのイベントからプール一覧、状態、ゲージ報酬） | **未着手**（下の未確認事項待ち）。今は AdapterNotReady を出して skipped と記録する |
| 6 | 15分ごとの収集、JSONログ、欠損の記録、スリープ復帰後の自動再開 | 完了 |
| 7 | 24時間の欠けチェック（`python -m farm_radar.check`）とテスト | 完了（テスト34件） |

M1の完了条件: オーナーのパソコンで、全プールのスナップショットが15分ごとに24時間欠けずに保存されること。

## 確認済みの事項（出典つき）
- Robinhood Chain: チェーンID 4663、ガス代はETH、公開RPC `https://rpc.mainnet.chain.robinhood.com`（回数制限あり、本番には非推奨と公式に記載）、推奨は Alchemy、エクスプローラは `https://robinhoodchain.blockscout.com`。
  出典: https://docs.robinhood.com/chain/connecting （2026-09-27 確認）
- up.: 公式サイトで「ve(3,3)型の取引所」と説明されている。出典: https://up33.xyz/docs/overview （2026-09-27。本文はJavaScriptで表示されるため中身は未取得）
- 参考（出典には使わない）: DefiLlama では up v3（集中流動性）と up v2 の2種類があり、監査情報は載っていない。https://defillama.com/protocol/up

## 未確認の事項（推測で埋めない）
Blockscout の検証済みコントラクトのソースコードを最優先で確認し、根拠のコントラクト名と関数名を venues/up-robinhood.yaml に書く。
- ゲージにステークしたLPが手数料を受け取るか（手数料の行き先が投票者か）
- 報酬が「レンジ内でステークされた流動性」に比例するか
- エポックの長さと切り替え時刻
- アドレス: v3ファクトリー、Voter、報酬トークン（UP）、Multicall3（このチェーンに実在するか）
- 監査の有無、稼働開始日
- 第三者ツール labrinyang/lp-terminal は照合用の参考のみ。出典に使わない。

## 次にやること
1. 接続が通るか確認する（下の「環境の問題」）。
2. Blockscout で上の未確認事項を確認し、venues/up-robinhood.yaml に出典・確認日・根拠を書く。
3. up. アダプターを実装する（Multicallで読む、ブロック固定、プール全体の流動性とステーク流動性を別々に取る、生データ保存）。
4. オーナーのパソコンで24時間動かしてもらい、欠けチェックで合格を確認する → M1の報告。

## 未解決の質問・環境の問題
- **クラウドの作業環境から RPC / Blockscout / up33.xyz / GeckoTerminal / DefiLlama に接続できない**（2026-09-27 21:49 JST 時点でも403）。
  オーナーは Project settings で許可済み。設定は新しいセッションにしか反映されない可能性がある。
- GitHub の既定のブランチを main に変える作業はオーナーが行う。

## 決まった方針
- RPC: `.env` に ALCHEMY_API_KEY があれば Alchemy を優先、なければ公式の公開RPC、その次に予備RPC。キーはオーナーが自分で設定し、チャットには貼らない。
- 出典の優先順位: Blockscout の検証済みソースコード → 公式ドキュメント。第三者ツールは出典にしない。
- 常時稼働: 当面VPSなし。M1〜M4はオーナーのパソコンで docker-compose により確認。M5（2週間のペーパートレード評価）の前にVPSを用意する予定。
- **欠損の扱い**: 収集が止まっていた期間（スリープ・再起動・Docker停止など）は `collection_gaps` に「欠損」として記録し、APIと画面に表示する。
  M5では、欠損期間中のペーパートレードの損益を推定扱いにし（`position_pnl.is_estimated = 1`、判定は `database.in_gap()`）、評価の計算から分ける。
- **自動再開**: スリープ復帰後は、寝ていた間の予定を1回にまとめてすぐ実行する（APScheduler の coalesce と misfire_grace_time=None）。
  再起動後は Docker の `restart: unless-stopped` と Docker Desktop の自動起動で再開する。
  起動直後にネットがつながっていなくても止まらず、その回を failed として記録して次の回に再挑戦する。
- データの保存: スナップショットごとにブロック番号を保存。流動性は「プール全体」と「ゲージにステークされたレンジ内」を別の列に。RPCの生の応答も `raw_rpc` に保存する。
- `.env` と `data/` は .gitignore で除外（テスト `test_secrets_and_data_are_gitignored` で確認）。
