# PROGRESS.md — 作業の現在地

> 新しいセッションは、作業を始める前にこのファイルを読むこと（CLAUDE.md のルール）。作業のたびに更新する。
> 最終更新: 2026-09-27 23:00 JST

## いまの位置
- **M1（up.アダプター + データ収集 + SQLite）の途中。**
- 作業ブランチ: `claude/farm-radar-m1-jvp4sm`。`main` には CLAUDE.md と SPEC.md だけが入っている。M1が終わったら main へのPRにする。
- 付録A（Phase 3）は実装しない。オーナーが「Phase 3aを開始」と言うまで、送信・署名・秘密鍵のコードは書かない。

## M1 の進み具合
| # | 内容 | 状態 |
|---|---|---|
| 1 | 土台（config.yaml, .env.example, docker-compose, README） | 完了 |
| 2 | venues/up-robinhood.yaml（出典つき） | アドレスと3点は確認済み（2026-09-27）。報酬上限の計算だけ未確認 |
| 3 | SQLite（6章の全テーブル + ブロック番号・流動性2種・生データ・収集記録・欠損） | 完了 |
| 4 | 読み取り専用RPCクライアント（再試行・切り替え・キャッシュ）と Multicall3 | 完了 |
| 5 | up. アダプター（ファクトリーのイベントからプール一覧、状態、ゲージ報酬） | **未着手**（オーナーの確認待ち。純日利の式に関わるため）。今は AdapterNotReady を出して skipped と記録する |
| 6 | 15分ごとの収集、JSONログ、欠損の記録、スリープ復帰後の自動再開 | 完了 |
| 7 | 24時間の欠けチェック（`python -m farm_radar.check`）とテスト | 完了（テスト34件） |

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
1. **オーナーの確認待ち**: 上の3点と「報酬の上限」を踏まえた純日利の式（下の案）でよいか。
2. 承認後、up. アダプターを実装する（Multicallで読む、ブロック固定、プール全体の流動性とステーク流動性を別々に取る、生データ保存、ゲージの rewardRate を保存）。
3. オーナーのパソコンで24時間動かしてもらい、欠けチェックで合格を確認する → M1の報告。

### 純日利の式の案（未承認）
- ステークする場合: 日利 ≒ rewardRate × 86400 × UP価格 × (自分の流動性 ÷ レンジ内ステーク流動性) ÷ 自分の元本。手数料収入は0。
- ステークしない場合: 手数料の90%だけ（UP報酬は0）。
- rewardRate は上限適用後の実際の値なので、票の割合から計算しない。

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
