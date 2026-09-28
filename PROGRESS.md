# PROGRESS.md — 作業の現在地

> 新しいセッションは、作業を始める前にこのファイルを読むこと（CLAUDE.md のルール）。作業のたびに更新する。
> 最終更新: 2026-09-28 15:30 JST

## いまの位置
- **M1（up.アダプター + データ収集 + SQLite）の途中。**
- 作業ブランチ: `claude/farm-radar-m1-jvp4sm`。`main` には CLAUDE.md と SPEC.md だけが入っている。M1が終わったら main へのPRにする。
- 付録A（Phase 3）は実装しない。オーナーが「Phase 3aを開始」と言うまで、送信・署名・秘密鍵のコードは書かない。

## M1 の進み具合
| # | 内容 | 状態 |
|---|---|---|
| 1 | 土台（config.yaml, .env.example, docker-compose, README） | 完了 |
| 2 | venues/up-robinhood.yaml（出典つき） | 完了。アドレス・3点・Sourcify照合の種類（full/partial）・警告C4。報酬上限の計算だけ未確認（オーナー承認済み） |
| 3 | SQLite（6章の全テーブル + ブロック番号・流動性2種・生データ・収集記録・欠損） | 完了 |
| 4 | 読み取り専用RPCクライアント（再試行・切り替え・キャッシュ）と Multicall3 | 完了 |
| 5 | up. アダプター（ファクトリーのイベントからプール一覧、状態、ゲージ報酬） | 完了（2026-09-27）。実データで87プールを1回約11秒（2回目以降約1秒）で収集できることを確認 |
| 6 | 15分ごとの収集、JSONログ、欠損の記録、スリープ復帰後の自動再開 | 完了 |
| 7 | 24時間の欠けチェック（`python -m farm_radar.check`）とテスト | 完了（テスト42件） |
| 8 | 報酬の毎秒量・エポック終了時刻の保存、「エポック更新直後」の印、報酬急減（30%）の記録、会場の警告API | 完了（2026-09-27 オーナー指示） |

M1の完了条件: オーナーのパソコンで、全プールのスナップショットが15分ごとに24時間欠けずに保存されること。

## 確認済みの事項（出典つき）
- Robinhood Chain: チェーンID 4663、ガス代はETH、公開RPC `https://rpc.mainnet.chain.robinhood.com`（回数制限あり、本番には非推奨と公式に記載）、推奨は Alchemy、エクスプローラは `https://robinhoodchain.blockscout.com`。
  出典: https://docs.robinhood.com/chain/connecting （2026-09-27 確認）。公開RPCで eth_chainId = 0x1237（4663）を確認。
- up.: Aerodrome（Base の大手 ve(3,3) DEX）の仕組みをほぼそのまま使っている（CLGauge.sol の冒頭に「Aerodrome Slipstream 派生」と明記）。
  主要コントラクトの作成は 2026-07-10。エポック数は 11（Minter.epochCount()、2026-09-27 時点）。

### アドレスと3点の確認（2026-09-27）
確認方法: アドレスは up. 公式サイトが読み込むプログラム（https://up33.xyz/assets/index-Dv2RMzFm.js）の mainnet 設定から取り、
ソースコードは Sourcify（コントラクト検証の公開データベース。チェーン上のバイトコードと一致したものだけ登録される）で読んだ。
さらに公開RPCで、コントラクト同士のつながり（Voter.minter() など）が設定と一致することを確かめた。詳細は venues/up-robinhood.yaml。
**Blockscout は Cloudflare の自動アクセス防止で、この作業環境からは画面も API も読めなかった**（接続自体は通る）。

| 項目 | 結果 | 根拠（コントラクト / 関数） |
|---|---|---|
| ステークしたLPに手数料は入るか | **入らない**。ステーク分の手数料は投票者へ | CLPool.calculateFees / splitFees → gaugeFees、CLPool.collectFees → CLGauge._claimFees → FeesVotingReward.notifyRewardAmount |
| ステークしないLPの手数料 | 手数料の **10%** がゲージ（→投票者）へ取られる | CLPool.applyUnstakedFees、CLFactory.getUnstakedFee（defaultUnstakedFee=100000）。上位5プールで確認 |
| 報酬はレンジ内ステーク流動性に比例するか | **比例する** | CLPool._updateRewardsGrowthGlobal（rewardRate ÷ stakedLiquidity）、CLGauge._earned（getRewardGrowthInside × liquidity） |
| レンジ内ステークが0の時間の報酬 | 次のエポックへ持ち越し | CLPool.rollover、CLGauge._notifyRewardAmount |
| エポック | **7日、木曜 00:00 UTC（日本時間 木曜 09:00）に切り替え** | ProtocolTimeLibrary（EPOCH_DURATION=7 days, EPOCH_OFFSET=0）、Voter.epochStart |
| 【新発見】ゲージ報酬の上限 | **有効**。今エポックは本来の配分 約1,344万 UP のうち約131万 UP（約9.7%）だけ配布、残りはエポック終了時に焼却 | Voter._distributeEnforced / _settleExpiredEpochs、capMode()=2（Enforced）、GaugeCapAccounted イベント |

確認済みアドレス: v3ファクトリー 0x1ac9…4F3、v2ファクトリー 0xFA54…c28、Voter 0x7F74…2a7、UP 0x57C0…4F1、
VotingEscrow 0x5d32…7B6、Minter 0x912E…Da5、Multicall3 0xcA11…A11 ほか（全桁は venues/up-robinhood.yaml）。
- 参考（出典には使わない）: DefiLlama では up v3（集中流動性）と up v2 の2種類があり、監査情報は載っていない。https://defillama.com/protocol/up

## 未確認の事項（推測で埋めない）
- **ゲージ報酬の上限の計算方法**: GaugeCapController（0x5739…687d）のソースが Sourcify に無く、中身が読めない。
  報酬は「票の割合 × 週の発行量」ではなく、実際にゲージへ渡った量（CLGauge.rewardRate）で計算する必要がある。
- v2プール（6個）のゲージの手数料の扱い（CLと同じと思われるが未確認。今回はCLの87ゲージのみ確認）。
- 監査の有無。
- Blockscout 上で「verified」表示になっているか（オーナーがブラウザで確認できる）。
- 第三者ツール labrinyang/lp-terminal は照合用の参考のみ。出典に使わない（今回は使っていない）。

## 次にやること
1. **オーナーのパソコン（Windows）で 2026-09-27 23:33 JST に収集開始**（初回 ok、87プール）。2026-09-28 23:45 JST 以降に `docker compose exec collector python -m farm_radar.check` で合格を確認する → M1 完了の報告。
   状態ページは http://localhost:18000/api/health（オーナーのPCでは 8000 番がほかのアプリと重なったため 18000 に変更）。
2. M2（スコアリング）で、下の承認済みの式を実装する。

## 承認済みの計算方針（2026-09-27 オーナー承認。M2 で実装する）
1. up. で確認した式は「収入」の部分。純日利は SPEC 3.2 のとおり 収入 − ガンマ − リバランス − ヘッジ − 報酬トークンの値下がり。
2. 自分の比率は L_mine / (レンジ内のステーク流動性 + L_mine)（分母に自分を含める）。
3. 収入に、レンジ幅ごとの「レンジ内にいる時間の割合」を掛ける。**割合の求め方（どのモデルで見積もるか）は M2 の最初に案を出して確認する。**
4. 報酬の毎秒量とエポックの終了時刻はスナップショットごとに保存（M1で実装済み）。エポックをまたいだ予測はしない。
   切り替え直後の判定には「エポック更新直後」の印を付ける（M1で印は保存済み。判定への反映はM2）。
5. ステークする場合（UP報酬のみ、手数料0）と、ステークしない場合（手数料の90%、UP報酬0）の両方を計算し、良いほうを採用する。
6. GaugeCapController のソース非公開を会場の警告 C4 として出す（yaml と /api/venues で実装済み。画面は M3）。
   報酬の毎秒量がエポックの途中で30%以上減ったら通知（alerts 表への記録とログは実装済み。送信は M4）。
7. Sourcify の照合の種類をコントラクトごとに yaml に記載（実装済み）。
- 報酬の毎秒量は、票の割合ではなくゲージの実際の rewardRate を使う（配布期間が切れていれば0）。

## 今後の要件（2026-09-28 オーナー追加。SPEC.md に反映済み）
- M2: 総資産（既定 $1,000 を LP 55%・ヘッジ証拠金 40%・予備 5% に振り分け）に対する日利で計算・判定する。income に in_range_ratio を掛ける。ヘッジできないプールは direction_risk を引き、判定は最高でも🟡（SPEC 3.2章・4章）。in_range_ratio と direction_risk の計算式は M2 の最初に案を出して承認を得る
- M3・M5: 損益表示の要件（SPEC 7.6章）と、総資産・時間別の表示（SPEC 7.7章）を実装する
- Phase 3（記録のみ・実装しない）: ウォレット候補の比較軸（SPEC 付録A A1.1）

## M1 の実装メモ
- 対象: ゲージのある CL プール（`collection.snapshot_scope: gauged`、87個）。2026-09-27 オーナーが「87か所だけ」と決定。M1完了条件の「全プール」はこの87個を指す。警告C4は「軽微（黄色）」と決定。CLプールは全部で1,887個。`all` にすると全部を記録する（データ量が約20倍）。
- v2 プール（ゲージ6個）は手数料の仕組みを確かめていないので対象外。
- 1回の収集は Multicall で約12回のRPC呼び出しにまとめる。生データは1回あたり約0.35MB（1日約34MB）。
- 実データでは、週の途中でも今週の報酬が配られていないゲージが約2割（87個中18個）あった。これは印ではなく「実際の報酬レート=0」として記録する。
- 古いデータベースには、起動時に足りない列を自動で追加する（schema_version 2）。

## 未解決の質問・環境の問題
- 2026-09-27 22:52 JST、新しい環境（Network access: Full）で7つの接続先をすべて確認: いずれも拒否されずに応答あり
  （RPC は eth_blockNumber / eth_chainId に正常応答、Blockscout / up33.xyz 200、GeckoTerminal API 200、DefiLlama yields API 200、github.com・raw 応答あり）。
- ただし Blockscout は Cloudflare のボット確認画面を返すため、プログラムからは読めない。今回は Sourcify で代用した。
- 公開RPCは Python 標準の urllib の User-Agent だと 403 を返す（httpx の既定値は通る）。短時間に多く呼ぶと 429（回数制限）になるので Multicall でまとめる。
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
